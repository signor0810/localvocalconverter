"""Tests for SessionManager (Task 9)."""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from local_typeless.sessions import Job, SessionManager
from local_typeless.types import (
    AudioChunk,
    CapturedImage,
    Mode,
    PipelineResult,
    Segment,
    TranscriptResult,
)


class FakeRecorder:
    def __init__(self, tmp_path: Path):
        self.tmp_path = tmp_path
        self.on_chunk = None
        self._is_rec = False
        self.wav_path = tmp_path / "last_recording.wav"
        self.fail_start = False
        self.fail_stop = False

    @property
    def is_recording(self) -> bool:
        return self._is_rec

    def start(self, on_chunk=None):
        if self.fail_start:
            raise RuntimeError("recorder start failed")
        self._is_rec = True
        self.on_chunk = on_chunk

    def stop(self):
        if self.fail_stop:
            raise RuntimeError("recorder stop failed")
        self._is_rec = False
        self.wav_path.write_bytes(b"RIFF...")
        return self.wav_path

    def cancel(self):
        self._is_rec = False


class FakeSTT:
    def __init__(self, text="chunk_text"):
        self.text = text
        self.calls: list = []
        self.block_event: threading.Event | None = None
        self.exc: Exception | None = None

    def transcribe(self, audio_path, **kwargs):
        if self.block_event:
            self.block_event.wait(timeout=5)
        if self.exc:
            raise self.exc
        self.calls.append((audio_path, kwargs))
        seg = Segment(start=0.0, end=1.0, text=self.text)
        return TranscriptResult(
            raw_text=self.text,
            duration_sec=1.0,
            stt_elapsed_sec=0.05,
            used_device="cpu",
            used_model="small",
            segments=[seg],
        )


class FakeGlossary:
    def build_whisper_initial_prompt(self):
        return "GLOSS_PROMPT"


class FakePipeline:
    def __init__(self, stt=None):
        self.stt = stt or FakeSTT()
        self.glossary = FakeGlossary()
        self.calls_run_text: list = []

    def run_text(self, text, mode, images=None, record_history=True):
        self.calls_run_text.append((text, mode, images))
        return PipelineResult(
            raw_text=text,
            final_text=f"REFINED: {text}",
            mode=mode,
            stt_profile_name="cpu_fast",
            llm_profile_name="dev",
            stt_elapsed_sec=0.1,
            llm_elapsed_sec=0.05,
            images=list(images or []),
        )


class FakeWatcher:
    def __init__(self, images=None):
        self._images = images or []
        self.started = False
        self.stopped = False
        self.cancelled = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True
        return list(self._images)

    def cancel(self):
        self.cancelled = True

    @property
    def images(self):
        return list(self._images)


def make_chunk(index: int, start_sec: float, duration_sec: float = 1.0, sample_rate: int = 16000) -> AudioChunk:
    pcm = b"\x00\x00" * int(sample_rate * duration_sec)
    return AudioChunk(index=index, start_sec=start_sec, pcm=pcm, sample_rate=sample_rate)


# 1. Happy path: begin -> 2 chunks -> end -> on_result once
def test_happy_path(tmp_path: Path):
    pipeline = FakePipeline()
    recorder = FakeRecorder(tmp_path)
    results = []
    job_events = []

    mgr = SessionManager(
        pipeline,
        recorder,
        cache_dir=tmp_path,
        on_result=lambda j, r: results.append((j, r)),
        on_job=lambda j, p: job_events.append((j.id, j.status, p)),
    )
    mgr.start()

    job = mgr.begin(Mode.CLEAN)
    assert job.id == 1
    assert job.status == "recording"

    c0 = make_chunk(0, 0.0, 1.0)
    c1 = make_chunk(1, 1.0, 1.0)
    recorder.on_chunk(c0)
    recorder.on_chunk(c1)

    ended = mgr.end()
    assert ended is job
    assert mgr.wait_idle(timeout=5) is True

    assert len(results) == 1
    res_job, res_result = results[0]
    assert res_job.status == "done"
    assert len(res_job.segments) == 2
    # Timestamps shifted
    assert res_job.segments[0].start == 0.0
    assert res_job.segments[1].start == 1.0
    assert res_job.segments[1].end == 2.0
    assert len(pipeline.calls_run_text) == 1
    mgr.shutdown(timeout=5)


# 2. Live transcript: on_segment fires after first chunk BEFORE end()
def test_live_transcript_on_segment(tmp_path: Path):
    pipeline = FakePipeline()
    recorder = FakeRecorder(tmp_path)
    seen_segments = []

    mgr = SessionManager(
        pipeline,
        recorder,
        cache_dir=tmp_path,
        on_segment=lambda j: seen_segments.append(j.text),
    )
    mgr.start()

    mgr.begin(Mode.CLEAN)
    recorder.on_chunk(make_chunk(0, 0.0, 1.0))

    deadline = time.monotonic() + 5.0
    while not seen_segments and time.monotonic() < deadline:
        time.sleep(0.01)

    assert len(seen_segments) >= 1
    assert seen_segments[0] == "chunk_text"

    mgr.end()
    assert mgr.wait_idle(timeout=5) is True
    mgr.shutdown(timeout=5)


# 3. Non-blocking: with STT fake blocked, end() returns and begin() for next job succeeds
def test_non_blocking_while_stt_busy(tmp_path: Path):
    stt = FakeSTT()
    stt.block_event = threading.Event()
    pipeline = FakePipeline(stt=stt)
    recorder = FakeRecorder(tmp_path)

    mgr = SessionManager(pipeline, recorder, cache_dir=tmp_path)
    mgr.start()

    j1 = mgr.begin(Mode.CLEAN)
    recorder.on_chunk(make_chunk(0, 0.0, 1.0))

    # end() must return immediately even though STT is blocked
    ended_j1 = mgr.end()
    assert ended_j1 is j1

    # begin() for job 2 succeeds immediately
    j2 = mgr.begin(Mode.SUMMARY)
    assert j2.id == 2
    assert mgr.is_recording is True

    # Unblock STT
    stt.block_event.set()
    mgr.end()
    assert mgr.wait_idle(timeout=5) is True
    mgr.shutdown(timeout=5)


# 4. Order: two jobs, results delivered in job order
def test_results_delivered_in_job_order(tmp_path: Path):
    pipeline = FakePipeline()
    recorder = FakeRecorder(tmp_path)
    delivered = []

    mgr = SessionManager(
        pipeline,
        recorder,
        cache_dir=tmp_path,
        on_result=lambda j, r: delivered.append(j.id),
    )
    mgr.start()

    mgr.begin(Mode.CLEAN)
    recorder.on_chunk(make_chunk(0, 0.0, 1.0))
    mgr.end()

    mgr.begin(Mode.SUMMARY)
    recorder.on_chunk(make_chunk(0, 0.0, 1.0))
    mgr.end()

    assert mgr.wait_idle(timeout=5) is True
    assert delivered == [1, 2]
    mgr.shutdown(timeout=5)


# 5. Held result: job 1's LLM finishes while job 2 is recording -> on_result for job 1 only after job 2's end()/cancel()
def test_held_result_during_recording(tmp_path: Path):
    pipeline = FakePipeline()
    recorder = FakeRecorder(tmp_path)
    delivered = []

    mgr = SessionManager(
        pipeline,
        recorder,
        cache_dir=tmp_path,
        on_result=lambda j, r: delivered.append(j.id),
    )
    mgr.start()

    mgr.begin(Mode.CLEAN)
    recorder.on_chunk(make_chunk(0, 0.0, 0.5))
    mgr.end()

    # Immediately begin job 2 so job 1's LLM result must be held
    mgr.begin(Mode.SUMMARY)

    # Poll briefly: delivered must NOT contain job 1 yet because job 2 is recording
    time.sleep(0.3)
    assert delivered == []

    # End job 2 -> idle gate opens -> both delivered
    mgr.end()
    assert mgr.wait_idle(timeout=5) is True
    assert 1 in delivered
    mgr.shutdown(timeout=5)


# 6. Image chunk: watcher image at t=13 -> chunk covering 12-20s transcribed with word_timestamps=True
def test_image_chunk_word_timestamps(tmp_path: Path):
    img = CapturedImage(index=1, t_sec=13.0, path=str(tmp_path / "img1.png"))
    watcher = FakeWatcher(images=[img])
    stt = FakeSTT()
    pipeline = FakePipeline(stt=stt)
    recorder = FakeRecorder(tmp_path)

    mgr = SessionManager(pipeline, recorder, watcher=watcher, cache_dir=tmp_path)
    mgr.start()

    mgr.begin(Mode.CLEAN)
    # Chunk 1: 0..10s (no image)
    recorder.on_chunk(make_chunk(0, 0.0, 10.0))
    # Chunk 2: 12..20s (contains t=13s)
    recorder.on_chunk(make_chunk(1, 12.0, 8.0))
    mgr.end()

    assert mgr.wait_idle(timeout=5) is True
    assert len(stt.calls) == 2
    assert "word_timestamps" not in stt.calls[0][1]
    assert stt.calls[1][1].get("word_timestamps") is True
    # run_text received the images
    assert pipeline.calls_run_text[0][2] == [img]
    mgr.shutdown(timeout=5)


# 7. cancel(): no result, status cancelled, queued chunks skipped
def test_cancel_skips_chunks(tmp_path: Path):
    stt = FakeSTT()
    pipeline = FakePipeline(stt=stt)
    recorder = FakeRecorder(tmp_path)
    results = []

    mgr = SessionManager(
        pipeline,
        recorder,
        cache_dir=tmp_path,
        on_result=lambda j, r: results.append(j),
    )
    mgr.start()

    j = mgr.begin(Mode.CLEAN)
    recorder.on_chunk(make_chunk(0, 0.0, 1.0))
    recorder.on_chunk(make_chunk(1, 1.0, 1.0))
    mgr.cancel()

    assert j.status == "cancelled"
    assert mgr.wait_idle(timeout=5) is True
    assert results == []
    mgr.shutdown(timeout=5)


# 8. abandon(): oldest pending job result never delivered; later jobs still complete
def test_abandon(tmp_path: Path):
    stt = FakeSTT()
    stt.block_event = threading.Event()
    pipeline = FakePipeline(stt=stt)
    recorder = FakeRecorder(tmp_path)
    results = []

    mgr = SessionManager(
        pipeline,
        recorder,
        cache_dir=tmp_path,
        on_result=lambda j, r: results.append(j.id),
    )
    mgr.start()

    # Job 1
    mgr.begin(Mode.CLEAN)
    recorder.on_chunk(make_chunk(0, 0.0, 1.0))
    mgr.end()

    # Job 2
    mgr.begin(Mode.SUMMARY)
    recorder.on_chunk(make_chunk(0, 0.0, 1.0))
    mgr.end()

    # Abandon job 1
    assert mgr.abandon(job_id=1) is True
    stt.block_event.set()

    assert mgr.wait_idle(timeout=5) is True
    assert 1 not in results
    assert 2 in results
    # Nothing pending -> returns False
    assert mgr.abandon() is False
    mgr.shutdown(timeout=5)


# 9. STT raises on a chunk -> status error with message, on_job fired, next job processed
def test_stt_chunk_error(tmp_path: Path):
    stt = FakeSTT()
    stt.exc = RuntimeError("cuda boom")
    pipeline = FakePipeline(stt=stt)
    recorder = FakeRecorder(tmp_path)
    job_statuses = []

    mgr = SessionManager(
        pipeline,
        recorder,
        cache_dir=tmp_path,
        on_job=lambda j, p: job_statuses.append((j.id, j.status, j.error)),
    )
    mgr.start()

    mgr.begin(Mode.CLEAN)
    recorder.on_chunk(make_chunk(0, 0.0, 1.0))
    mgr.end()

    assert mgr.wait_idle(timeout=5) is True
    assert any(s == "error" and "cuda boom" in err for _, s, err in job_statuses)

    # Next job works
    stt.exc = None
    mgr.begin(Mode.SUMMARY)
    recorder.on_chunk(make_chunk(0, 0.0, 1.0))
    mgr.end()

    assert mgr.wait_idle(timeout=5) is True
    assert any(j_id == 2 and s == "done" for j_id, s, _ in job_statuses)
    mgr.shutdown(timeout=5)


# 10. retry(): missing wav -> None; existing wav -> file job transcribed and delivered; manifest restored
def test_retry(tmp_path: Path):
    pipeline = FakePipeline()
    recorder = FakeRecorder(tmp_path)
    results = []

    mgr = SessionManager(
        pipeline,
        recorder,
        cache_dir=tmp_path,
        on_result=lambda j, r: results.append((j, r)),
    )
    mgr.start()

    # Missing wav -> None
    assert mgr.retry(Mode.CLEAN) is None

    # Write a fake wav and manifest
    wav = tmp_path / "last_recording.wav"
    wav.write_bytes(b"RIFF...")
    img_file = tmp_path / "img.png"
    img_file.write_bytes(b"1")
    img = CapturedImage(index=1, t_sec=0.5, path=str(img_file))
    from local_typeless.pipeline import save_image_manifest
    save_image_manifest(tmp_path, [img])

    retried_job = mgr.retry(Mode.CLEAN)
    assert retried_job is not None
    assert mgr.wait_idle(timeout=5) is True
    assert len(results) == 1
    assert results[0][0].images == [img]
    mgr.shutdown(timeout=5)


# 11. recorder.start raising -> begin raises and is_recording is False; recorder.stop raising -> job error, no hang
def test_recorder_start_and_stop_exceptions(tmp_path: Path):
    pipeline = FakePipeline()
    recorder = FakeRecorder(tmp_path)
    recorder.fail_start = True

    mgr = SessionManager(pipeline, recorder, cache_dir=tmp_path)
    mgr.start()

    with pytest.raises(RuntimeError, match="recorder start failed"):
        mgr.begin(Mode.CLEAN)
    assert mgr.is_recording is False

    # recorder.stop raising
    recorder.fail_start = False
    recorder.fail_stop = True

    j = mgr.begin(Mode.CLEAN)
    stopped_job = mgr.end()
    assert stopped_job is j
    assert stopped_job.status == "error"
    assert "recorder stop failed" in stopped_job.error
    assert mgr.is_recording is False
    mgr.shutdown(timeout=5)


# 12. shutdown() while recording finishes the job and returns True; pending counts correct throughout
def test_shutdown_while_recording(tmp_path: Path):
    pipeline = FakePipeline()
    recorder = FakeRecorder(tmp_path)
    pending_records = []

    mgr = SessionManager(
        pipeline,
        recorder,
        cache_dir=tmp_path,
        on_job=lambda j, p: pending_records.append(p),
    )
    mgr.start()

    mgr.begin(Mode.CLEAN)
    recorder.on_chunk(make_chunk(0, 0.0, 1.0))
    ok = mgr.shutdown(timeout=5)
    assert ok is True
    assert mgr.is_recording is False
    assert mgr.pending == 0


# 13. A callback that raises does not kill the worker threads
def test_callback_raising_does_not_kill_workers(tmp_path: Path):
    pipeline = FakePipeline()
    recorder = FakeRecorder(tmp_path)

    def bad_on_segment(j):
        raise RuntimeError("callback explosion")

    def bad_on_result(j, r):
        raise RuntimeError("result callback explosion")

    mgr = SessionManager(
        pipeline,
        recorder,
        cache_dir=tmp_path,
        on_segment=bad_on_segment,
        on_result=bad_on_result,
    )
    mgr.start()

    mgr.begin(Mode.CLEAN)
    recorder.on_chunk(make_chunk(0, 0.0, 1.0))
    mgr.end()

    assert mgr.wait_idle(timeout=5) is True
    # Next job still executes fine
    mgr.begin(Mode.SUMMARY)
    recorder.on_chunk(make_chunk(0, 0.0, 1.0))
    mgr.end()
    assert mgr.wait_idle(timeout=5) is True
    mgr.shutdown(timeout=5)


# 14. A chunk whose STT fails while recording is retried at end, in the right position
def test_failed_chunk_is_retried_at_end_not_dropped(tmp_path: Path):
    class FlakySTT(FakeSTT):
        def __init__(self):
            super().__init__()
            self.fail_once_for = "job1_1.wav"

        def transcribe(self, audio_path, **kwargs):
            name = Path(audio_path).name
            if name == self.fail_once_for:
                self.fail_once_for = None
                raise RuntimeError("transient decode error")
            self.text = f"<{name}>"
            return super().transcribe(audio_path, **kwargs)

    pipeline = FakePipeline(stt=FlakySTT())
    recorder = FakeRecorder(tmp_path)
    results = []
    mgr = SessionManager(pipeline, recorder, cache_dir=tmp_path,
                         on_result=lambda j, r: results.append(r))
    mgr.begin(Mode.CLEAN)
    for i in range(3):
        recorder.on_chunk(make_chunk(i, float(i)))
    mgr.end()
    assert mgr.wait_idle(timeout=5) is True

    assert len(results) == 1
    assert pipeline.calls_run_text[0][0] == "<job1_0.wav><job1_1.wav><job1_2.wav>"
    assert not list((tmp_path / "chunks").glob("*.wav"))
    mgr.shutdown(timeout=5)


# 15. A chunk that keeps failing errors the job instead of emitting a transcript with a hole
def test_chunk_failing_twice_errors_the_job(tmp_path: Path):
    class BrokenSTT(FakeSTT):
        def transcribe(self, audio_path, **kwargs):
            if Path(audio_path).name == "job1_1.wav":
                raise RuntimeError("decode error")
            return super().transcribe(audio_path, **kwargs)

    pipeline = FakePipeline(stt=BrokenSTT())
    recorder = FakeRecorder(tmp_path)
    results, events = [], []
    mgr = SessionManager(pipeline, recorder, cache_dir=tmp_path,
                         on_result=lambda j, r: results.append(r),
                         on_job=lambda j, p: events.append((j.status, j.error)))
    mgr.begin(Mode.CLEAN)
    for i in range(3):
        recorder.on_chunk(make_chunk(i, float(i)))
    job = mgr.end()
    assert mgr.wait_idle(timeout=5) is True

    assert results == [] and pipeline.calls_run_text == []
    assert job.status == "error" and "decode error" in job.error
    assert ("error", job.error) in events
    assert mgr.pending == 0
    mgr.shutdown(timeout=5)


# 16. paste_image delegates to watcher.capture_now() while recording, 0 otherwise
def test_session_manager_paste_image(tmp_path: Path):
    class FakeWatcher:
        def __init__(self):
            self.calls = 0

        def capture_now(self):
            self.calls += 1
            return 1

        def start(self):
            pass

        def stop(self):
            return []

        def cancel(self):
            pass

    rec = FakeRecorder(tmp_path)
    pipe = FakePipeline()
    watcher = FakeWatcher()
    sm = SessionManager(pipe, rec, watcher=watcher, cache_dir=tmp_path)

    # 1. Not recording -> 0
    assert sm.paste_image() == 0
    assert watcher.calls == 0

    # 2. Recording -> calls capture_now
    sm.begin(Mode.CLEAN)
    assert sm.paste_image() == 1
    assert watcher.calls == 1

    sm.end()
    assert sm.paste_image() == 0

    # 3. Watcher is None -> 0
    sm_nowatcher = SessionManager(pipe, rec, watcher=None, cache_dir=tmp_path)
    sm_nowatcher.begin(Mode.CLEAN)
    assert sm_nowatcher.paste_image() == 0
    sm_nowatcher.end()

    sm.shutdown(timeout=2.0)
    sm_nowatcher.shutdown(timeout=2.0)
