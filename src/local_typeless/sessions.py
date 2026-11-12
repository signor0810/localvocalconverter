"""Session manager coordinating streaming STT, LLM background queue, and recording jobs."""
from __future__ import annotations

import logging
import queue
import threading
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Union

from local_typeless.pipeline import (
    load_image_manifest,
    save_image_manifest,
)
from local_typeless.textblocks import marker, place_markers
from local_typeless.types import (
    AudioChunk,
    CapturedImage,
    Mode,
    PipelineResult,
    Segment,
    Word,
)

logger = logging.getLogger(__name__)


@dataclass
class Job:
    id: int
    mode: Mode
    status: str = "recording"  # recording | transcribing | refining | done | error | abandoned | cancelled
    segments: List[Segment] = field(default_factory=list)
    images: List[CapturedImage] = field(default_factory=list)
    text: str = ""  # live transcript with markers
    stt_elapsed: float = 0.0
    error: str = ""
    result: Optional[PipelineResult] = None
    failed_chunks: list = field(default_factory=list)   # [(AudioChunk, wav path)]


class SessionManager:
    """Manages audio recording jobs, streaming STT chunks, and background LLM refinement."""

    def __init__(
        self,
        pipeline,
        recorder,
        *,
        watcher=None,
        cache_dir: Union[str, Path],
        on_job: Optional[Callable[[Job, int], None]] = None,
        on_segment: Optional[Callable[[Job], None]] = None,
        on_result: Optional[Callable[[Job, PipelineResult], None]] = None,
    ) -> None:
        self.pipeline = pipeline
        self.recorder = recorder
        self.watcher = watcher
        self.cache_dir = Path(cache_dir)
        self.on_job = on_job
        self.on_segment = on_segment
        self.on_result = on_result

        self._lock = threading.RLock()
        self._stt_queue: queue.Queue = queue.Queue()
        self._llm_queue: queue.Queue = queue.Queue()

        self._jobs: List[Job] = []
        self._current_job: Optional[Job] = None
        self._next_job_id: int = 0
        self._is_recording: bool = False

        self._idle_gate = threading.Event()
        self._idle_gate.set()  # set when NOT recording

        self._started = False
        self._stopping = False
        self._stt_in_flight = False
        self._llm_in_flight = False

        self._stt_thread: Optional[threading.Thread] = None
        self._llm_thread: Optional[threading.Thread] = None

    @property
    def is_recording(self) -> bool:
        with self._lock:
            return self._is_recording

    @property
    def pending(self) -> int:
        with self._lock:
            return sum(1 for j in self._jobs if j.status in ("transcribing", "refining"))

    def _fire_on_job(self, job: Job, pending_count: int) -> None:
        if self.on_job is not None:
            try:
                self.on_job(job, pending_count)
            except Exception as e:
                logger.warning("on_job callback raised: %s: %s", type(e).__name__, e)

    def _fire_on_segment(self, job: Job) -> None:
        if self.on_segment is not None:
            try:
                self.on_segment(job)
            except Exception as e:
                logger.warning("on_segment callback raised: %s: %s", type(e).__name__, e)

    def _fire_on_result(self, job: Job, result: PipelineResult) -> None:
        if self.on_result is not None:
            try:
                self.on_result(job, result)
            except Exception as e:
                logger.warning("on_result callback raised: %s: %s", type(e).__name__, e)

    def start(self) -> None:
        """Start worker threads (idempotent)."""
        with self._lock:
            if self._started:
                return
            self._started = True
            self._stopping = False
            self._stt_thread = threading.Thread(
                target=self._stt_worker,
                name="SessionManagerSTTThread",
                daemon=True,
            )
            self._llm_thread = threading.Thread(
                target=self._llm_worker,
                name="SessionManagerLLMThread",
                daemon=True,
            )
            self._stt_thread.start()
            self._llm_thread.start()

    def begin(self, mode: Mode) -> Job:
        """Start recording a new job."""
        self.start()
        with self._lock:
            if self._is_recording:
                raise RuntimeError("Recording is already in progress.")

            self._next_job_id += 1
            job = Job(id=self._next_job_id, mode=mode, status="recording")
            self._jobs.append(job)
            if len(self._jobs) > 50:
                self._jobs.pop(0)

            self._current_job = job
            self._is_recording = True
            self._idle_gate.clear()

        def _on_chunk(chunk: AudioChunk) -> None:
            self._stt_queue.put(("chunk", job, chunk))

        try:
            self.recorder.start(on_chunk=_on_chunk)
        except Exception as e:
            with self._lock:
                self._is_recording = False
                self._current_job = None
                self._idle_gate.set()
                job.status = "error"
                job.error = f"{type(e).__name__}: {e}"
                pending_count = self.pending
            self._fire_on_job(job, pending_count)
            raise

        if self.watcher is not None:
            try:
                self.watcher.start()
            except Exception as e:
                logger.warning("watcher.start failed: %s: %s", type(e).__name__, e)

        return job

    def end(self) -> Optional[Job]:
        """Stop recording and queue job for refinement; returns immediately."""
        with self._lock:
            if not self._is_recording or self._current_job is None:
                return None
            job = self._current_job
            self._current_job = None
            self._is_recording = False

        try:
            self.recorder.stop()
        except Exception as e:
            if self.watcher is not None:
                try:
                    self.watcher.cancel()
                except Exception:
                    pass
            with self._lock:
                self._idle_gate.set()
                job.status = "error"
                job.error = f"{type(e).__name__}: {e}"
                pending_count = self.pending
            self._fire_on_job(job, pending_count)
            return job

        images: List[CapturedImage] = []
        if self.watcher is not None:
            try:
                images = self.watcher.stop()
            except Exception as e:
                logger.warning("watcher.stop failed: %s: %s", type(e).__name__, e)

        with self._lock:
            job.images = images
            try:
                save_image_manifest(self.cache_dir, job.images)
            except Exception as e:
                logger.warning("save_image_manifest failed: %s: %s", type(e).__name__, e)

            if job.status == "recording":
                job.status = "transcribing"
                self._stt_queue.put(("end", job))
            self._idle_gate.set()
            pending_count = self.pending

        self._fire_on_job(job, pending_count)
        return job

    def cancel(self) -> None:
        """Discard the recording in progress."""
        with self._lock:
            if not self._is_recording or self._current_job is None:
                return
            job = self._current_job
            self._current_job = None
            self._is_recording = False

        try:
            self.recorder.cancel()
        except Exception as e:
            logger.warning("recorder.cancel failed: %s: %s", type(e).__name__, e)

        if self.watcher is not None:
            try:
                self.watcher.cancel()
            except Exception as e:
                logger.warning("watcher.cancel failed: %s: %s", type(e).__name__, e)

        with self._lock:
            job.status = "cancelled"
            self._idle_gate.set()
            pending_count = self.pending

        self._fire_on_job(job, pending_count)

    def paste_image(self) -> int:
        """Capture the clipboard image into the currently recording session."""
        with self._lock:
            recording = self._is_recording
            watcher = self.watcher
        if not recording or watcher is None:
            return 0
        try:
            return watcher.capture_now()
        except Exception as e:
            logger.warning("watcher.capture_now failed: %s: %s", type(e).__name__, e)
            return 0

    def retry(self, mode: Mode) -> Optional[Job]:
        """Re-run <cache_dir>/last_recording.wav as a new job."""
        self.start()
        wav_path = self.cache_dir / "last_recording.wav"
        if not wav_path.exists():
            return None

        images = load_image_manifest(self.cache_dir)
        with self._lock:
            self._next_job_id += 1
            job = Job(id=self._next_job_id, mode=mode, status="transcribing", images=images)
            self._jobs.append(job)
            if len(self._jobs) > 50:
                self._jobs.pop(0)

            self._stt_queue.put(("file", job, wav_path))
            pending_count = self.pending

        self._fire_on_job(job, pending_count)
        return job

    def abandon(self, job_id: Optional[int] = None) -> bool:
        """Abandon the given job or oldest pending job; returns False if none."""
        with self._lock:
            if job_id is not None:
                target = next((j for j in self._jobs if j.id == job_id), None)
            else:
                target = next(
                    (j for j in self._jobs if j.status in ("transcribing", "refining")),
                    None,
                )

            if target is None or target.status not in ("transcribing", "refining"):
                return False

            target.status = "abandoned"
            pending_count = self.pending

        self._fire_on_job(target, pending_count)
        return True

    def wait_idle(self, timeout: Optional[float] = None) -> bool:
        """Wait until both queues are drained and no job is in flight."""
        deadline = time.monotonic() + timeout if timeout is not None else None
        while True:
            with self._lock:
                idle = (
                    not self._is_recording
                    and self._stt_queue.unfinished_tasks == 0
                    and self._llm_queue.unfinished_tasks == 0
                    and not self._stt_in_flight
                    and not self._llm_in_flight
                )
            if idle:
                return True
            if deadline is not None and time.monotonic() >= deadline:
                return False
            time.sleep(0.01)

    def shutdown(self, timeout: float = 60.0) -> bool:
        """End recording if active, drain queues, and stop worker threads."""
        if self.is_recording:
            self.end()
        self._stopping = True
        ok = self.wait_idle(timeout=timeout)

        self._stt_queue.put(None)
        self._llm_queue.put(None)

        if self._stt_thread is not None:
            self._stt_thread.join(timeout=2.0)
        if self._llm_thread is not None:
            self._llm_thread.join(timeout=2.0)

        with self._lock:
            self._started = False

        return ok

    # ------------------------------------------------------------ chunk STT
    @staticmethod
    def _unlink(path: Path) -> None:
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            pass

    def _handle_chunk(self, job: Job, chunk: AudioChunk) -> None:
        chunks_dir = self.cache_dir / "chunks"
        chunks_dir.mkdir(parents=True, exist_ok=True)
        chunk_path = chunks_dir / f"job{job.id}_{chunk.index}.wav"
        with wave.open(str(chunk_path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(chunk.sample_rate)
            wf.writeframes(chunk.pcm)
        try:
            self._transcribe_chunk(job, chunk, chunk_path)
        except Exception as e:
            logger.warning("job %d chunk %d STT failed (%s: %s); will retry at end",
                           job.id, chunk.index, type(e).__name__, e)
            with self._lock:
                job.failed_chunks.append((chunk, chunk_path))
            return
        self._unlink(chunk_path)

    def _transcribe_chunk(self, job: Job, chunk: AudioChunk, chunk_path: Path) -> None:
        duration = len(chunk.pcm) / (chunk.sample_rate * 2)
        with self._lock:
            imgs = list(job.images) if job.images else (
                list(self.watcher.images) if self.watcher is not None else [])
        wt = any(chunk.start_sec <= img.t_sec < chunk.start_sec + duration for img in imgs)
        kwargs: dict = {"initial_prompt": self.pipeline.glossary.build_whisper_initial_prompt()}
        if wt:
            kwargs["word_timestamps"] = True
        res = self.pipeline.stt.transcribe(chunk_path, **kwargs)

        off = chunk.start_sec
        shifted: List[Segment] = [
            Segment(
                start=float(seg.start) + off,
                end=float(seg.end) + off,
                text=seg.text,
                words=tuple(
                    Word(float(w.start) + off, float(w.end) + off, w.text)
                    for w in (getattr(seg, "words", None) or [])
                ),
            )
            for seg in (getattr(res, "segments", None) or [])
        ]
        if not shifted and res.raw_text:
            shifted.append(Segment(off, off + duration, res.raw_text, ()))

        fire_seg = False
        with self._lock:
            if job.status not in ("cancelled", "abandoned", "error"):
                job.segments.extend(shifted)
                job.segments.sort(key=lambda sg: sg.start)   # retried chunks arrive late
                job.stt_elapsed += getattr(res, "stt_elapsed_sec", 0.0)
                job.text = place_markers(job.segments, imgs)
                fire_seg = True
        if fire_seg:
            self._fire_on_segment(job)

    # -------------------------------------------------------- worker threads
    def _stt_worker(self) -> None:
        while True:
            item = self._stt_queue.get()
            if item is None:
                self._stt_queue.task_done()
                break

            self._stt_in_flight = True
            kind, job = item[0], item[1]

            with self._lock:
                skip = job.status in ("cancelled", "abandoned", "error")

            if skip:
                self._stt_queue.task_done()
                self._stt_in_flight = False
                continue

            try:
                if kind == "chunk":
                    self._handle_chunk(job, item[2])

                elif kind == "end":
                    # A chunk that failed while recording is retried once here; if it fails
                    # again the job errors out (the audio stays in last_recording.wav for
                    # retry) rather than emitting a transcript with a silent hole.
                    with self._lock:
                        failed, job.failed_chunks = list(job.failed_chunks), []
                    for chunk, chunk_path in failed:
                        self._transcribe_chunk(job, chunk, chunk_path)
                        self._unlink(chunk_path)

                    fire_job = False
                    with self._lock:
                        if job.status not in ("cancelled", "abandoned", "error"):
                            job.text = place_markers(job.segments, job.images)
                            job.status = "refining"
                            self._llm_queue.put(job)
                            pending_count = self.pending
                            fire_job = True

                    if fire_job:
                        self._fire_on_job(job, pending_count)

                elif kind == "file":
                    wav_path = item[2]

                    def on_file_seg(text_so_far: str) -> None:
                        fire_seg_cb = False
                        with self._lock:
                            if job.status not in ("cancelled", "abandoned", "error"):
                                job.text = text_so_far
                                fire_seg_cb = True
                        if fire_seg_cb:
                            self._fire_on_segment(job)

                    kwargs = {
                        "initial_prompt": self.pipeline.glossary.build_whisper_initial_prompt(),
                        "on_segment": on_file_seg,
                    }
                    if job.images:
                        kwargs["word_timestamps"] = True

                    res = self.pipeline.stt.transcribe(wav_path, **kwargs)

                    fire_job = False
                    with self._lock:
                        if job.status not in ("cancelled", "abandoned", "error"):
                            job.stt_elapsed = getattr(res, "stt_elapsed_sec", 0.0)
                            if getattr(res, "segments", None):
                                job.segments = list(res.segments)
                                job.text = place_markers(job.segments, job.images)
                            else:
                                job.text = res.raw_text + "".join(
                                    marker(img.index)
                                    for img in sorted(job.images, key=lambda x: x.index)
                                )
                            job.status = "refining"
                            self._llm_queue.put(job)
                            pending_count = self.pending
                            fire_job = True

                    if fire_job:
                        self._fire_on_job(job, pending_count)

            except Exception as e:
                logger.warning("STT worker exception on job %d: %s", job.id, e)
                fire_err = False
                with self._lock:
                    if job.status not in ("cancelled", "abandoned"):
                        job.status = "error"
                        job.error = f"{type(e).__name__}: {e}"
                        pending_count = self.pending
                        fire_err = True
                if fire_err:
                    self._fire_on_job(job, pending_count)

            finally:
                self._stt_queue.task_done()
                self._stt_in_flight = False

    def _llm_worker(self) -> None:
        while True:
            job = self._llm_queue.get()
            if job is None:
                self._llm_queue.task_done()
                break

            self._llm_in_flight = True

            with self._lock:
                skip = job.status in ("abandoned", "cancelled", "error")

            if skip:
                self._llm_queue.task_done()
                self._llm_in_flight = False
                continue

            try:
                result = self.pipeline.run_text(
                    job.text, job.mode, images=job.images or None
                )
                result.stt_elapsed_sec = job.stt_elapsed

                # Wait for the idle gate in a loop with 0.2s timeout
                while not self._idle_gate.wait(0.2):
                    if self._stopping:
                        break
                    with self._lock:
                        if job.status in ("abandoned", "cancelled"):
                            break

                fire_res = False
                with self._lock:
                    if job.status not in ("abandoned", "cancelled"):
                        job.result = result
                        job.status = "done"
                        pending_count = self.pending
                        fire_res = True

                if fire_res:
                    self._fire_on_result(job, result)
                    self._fire_on_job(job, pending_count)

            except Exception as e:
                logger.warning("LLM worker exception on job %d: %s", job.id, e)
                fire_err = False
                with self._lock:
                    if job.status not in ("abandoned", "cancelled"):
                        job.status = "error"
                        job.error = f"{type(e).__name__}: {e}"
                        pending_count = self.pending
                        fire_err = True
                if fire_err:
                    self._fire_on_job(job, pending_count)

            finally:
                self._llm_queue.task_done()
                self._llm_in_flight = False
