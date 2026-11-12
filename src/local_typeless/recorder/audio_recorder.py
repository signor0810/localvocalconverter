"""Audio Recorder (WP2-A, owned by flash-infra).

Implements Recorder protocol defined in local_typeless.types.
Records 16kHz mono 16-bit PCM audio, writes last_recording.wav atomically on stop(),
discards buffer on cancel() without touching existing files, and supports sound cues and streaming chunking.
"""
from __future__ import annotations

import logging
import os
import sys
import time
import uuid
import wave
from pathlib import Path
from typing import Any, Callable, List, Optional

from local_typeless.types import (
    AudioChunk,
    Recorder,
    RecorderConfig,
    StreamingConfig,
)

logger = logging.getLogger(__name__)


def _play_beep(freq: int, duration_ms: int) -> None:
    """Emit a brief sound cue on Windows; no-op on non-Windows platforms."""
    if sys.platform != "win32":
        return
    try:
        import winsound

        winsound.Beep(freq, duration_ms)
    except Exception as exc:
        logger.debug("Beep cue omitted: %s", exc)


def _get_sounddevice() -> Any:
    """Lazy import of sounddevice to avoid hard runtime dependency in unit tests."""
    import sounddevice as sd

    return sd


class AudioRecorder(Recorder):
    """Microphone audio recorder implementing the local_typeless.types.Recorder contract."""

    def __init__(
        self,
        cfg: RecorderConfig,
        cache_dir: Path,
        streaming: Optional[StreamingConfig] = None,
    ) -> None:
        self.cfg = cfg
        self.cache_dir = Path(cache_dir)
        self.streaming = streaming
        self._is_recording: bool = False
        self._frames: List[bytes] = []
        self._stream: Any = None
        self._chunker: Optional[Any] = None
        self._on_chunk: Optional[Callable[[AudioChunk], None]] = None

    @property
    def is_recording(self) -> bool:
        return self._is_recording

    @property
    def supports_streaming(self) -> bool:
        return self.cfg.channels == 1

    def _audio_callback(
        self, indata: Any, frames: int, time_info: Any, status: Any
    ) -> None:
        if status:
            logger.warning("Audio input status: %s", status)
        if self._is_recording or self._stream is not None:
            if hasattr(indata, "tobytes"):
                data = indata.tobytes()
            elif isinstance(indata, (bytes, bytearray)):
                data = bytes(indata)
            else:
                data = bytes(indata)
            self._frames.append(data)
            if self._chunker is not None and self._on_chunk is not None:
                try:
                    chunks = self._chunker.feed(data)
                    for c in chunks:
                        try:
                            self._on_chunk(c)
                        except Exception as e:
                            logger.warning(
                                "on_chunk callback failed: %s: %s",
                                type(e).__name__,
                                e,
                            )
                except Exception as e:
                    logger.warning("chunker feed failed: %s: %s", type(e).__name__, e)

    def start(self, on_chunk: Optional[Callable[[AudioChunk], None]] = None) -> None:
        """Start capturing microphone audio.

        Opens and starts the InputStream FIRST. Sets _is_recording=True only after success.
        On exception, closes stream, resets _is_recording=False, clears _frames, and re-raises.
        Emits beep only after successful startup.
        """
        if self._is_recording:
            logger.warning("AudioRecorder is already recording.")
            return

        self._frames.clear()
        if on_chunk is not None and self.supports_streaming:
            from local_typeless.recorder.chunker import SilenceChunker
            from local_typeless.types import StreamingConfig

            st = self.streaming or StreamingConfig()
            self._chunker = SilenceChunker(
                sample_rate=self.cfg.sample_rate,
                min_chunk_sec=st.min_chunk_sec,
                max_chunk_sec=st.max_chunk_sec,
                silence_sec=st.silence_sec,
            )
            self._on_chunk = on_chunk
        else:
            self._chunker = None
            self._on_chunk = None

        sd = _get_sounddevice()
        try:
            self._stream = sd.InputStream(
                samplerate=self.cfg.sample_rate,
                channels=self.cfg.channels,
                dtype="int16",
                callback=self._audio_callback,
            )
            self._stream.start()
            self._is_recording = True
        except Exception:
            self._is_recording = False
            self._frames.clear()
            self._chunker = None
            self._on_chunk = None
            if self._stream is not None:
                try:
                    self._stream.stop()
                    self._stream.close()
                except Exception:
                    pass
                self._stream = None
            raise

        if self.cfg.beep:
            _play_beep(1000, 100)

    def stop(self) -> Path:
        """Stop recording and atomically write <cache_dir>/last_recording.wav.

        Raises RuntimeError if not actively recording.
        Stops and closes the stream BEFORE resetting state/taking frames to avoid clipping tails.
        Raises if no frames were captured and leaves existing last_recording.wav untouched.
        Retries os.replace against Windows PermissionError and ensures tmp file cleanup.
        """
        if not self._is_recording:
            raise RuntimeError("AudioRecorder was not recording.")

        # Stop and close the stream BEFORE flipping state/taking frames to capture full tail
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception as exc:
                logger.debug("Error stopping audio stream: %s", exc)
            self._stream = None

        self._is_recording = False

        if self._chunker is not None and self._on_chunk is not None:
            try:
                tail = self._chunker.flush()
                if tail is not None:
                    try:
                        self._on_chunk(tail)
                    except Exception as e:
                        logger.warning(
                            "on_chunk callback failed on flush: %s: %s",
                            type(e).__name__,
                            e,
                        )
            except Exception as e:
                logger.warning("chunker flush failed: %s: %s", type(e).__name__, e)
            self._chunker = None
            self._on_chunk = None

        if not self._frames or not any(self._frames):
            raise RuntimeError("No audio frames were captured during recording.")

        self.cache_dir.mkdir(parents=True, exist_ok=True)
        target_path = self.cache_dir / "last_recording.wav"
        tmp_path = self.cache_dir / f"tmp_{uuid.uuid4().hex}.wav"

        try:
            with wave.open(str(tmp_path), "wb") as wf:
                wf.setnchannels(self.cfg.channels)
                wf.setsampwidth(2)  # 16-bit PCM = 2 bytes per sample
                wf.setframerate(self.cfg.sample_rate)
                wf.writeframes(b"".join(self._frames))

            # Retry os.replace a few times in case of Windows PermissionError from antivirus / file readers
            for attempt in range(5):
                try:
                    os.replace(tmp_path, target_path)
                    break
                except PermissionError:
                    if attempt == 4:
                        raise
                    time.sleep(0.05 * (attempt + 1))
        finally:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass

        if self.cfg.beep:
            _play_beep(1200, 100)

        return target_path

    def cancel(self) -> None:
        """Cancel the current recording, discarding buffered audio without modifying last_recording.wav."""
        self._is_recording = False
        self._chunker = None
        self._on_chunk = None
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception as exc:
                logger.debug("Error closing cancelled audio stream: %s", exc)
            self._stream = None

        self._frames.clear()

        if self.cfg.beep:
            _play_beep(600, 150)
