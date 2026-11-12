"""Silence-based audio chunker for streaming transcription."""
from __future__ import annotations

import logging
from typing import List, Optional

import numpy as np

from local_typeless.types import AudioChunk

logger = logging.getLogger(__name__)


class SilenceChunker:
    """Chunks 16-bit little-endian mono PCM audio at silence points or max chunk limits."""

    def __init__(
        self,
        sample_rate: int = 16000,
        min_chunk_sec: float = 12.0,
        max_chunk_sec: float = 28.0,
        silence_sec: float = 0.5,
        frame_ms: int = 30,
        abs_threshold: float = 120.0,
        rel_threshold: float = 0.08,
    ) -> None:
        self.sample_rate = sample_rate
        self.min_chunk_sec = min_chunk_sec
        self.max_chunk_sec = max_chunk_sec
        self.silence_sec = silence_sec
        self.frame_ms = frame_ms
        self.abs_threshold = abs_threshold
        self.rel_threshold = rel_threshold

        self.frame_samples: int = sample_rate * frame_ms // 1000
        self.frame_bytes: int = self.frame_samples * 2

        self._buffer: bytes = b""
        self._frames: List[bytes] = []
        self._frame_rms: List[float] = []
        self._frame_silent: List[bool] = []
        self._peak: float = 0.0
        self._emitted_samples: int = 0
        self._chunk_index: int = 0

    def reset(self) -> None:
        """Reset internal state and counters."""
        self._buffer = b""
        self._frames.clear()
        self._frame_rms.clear()
        self._frame_silent.clear()
        self._peak = 0.0
        self._emitted_samples = 0
        self._chunk_index = 0

    def feed(self, pcm: bytes) -> List[AudioChunk]:
        """Feed raw 16-bit mono PCM bytes, returning any chunks cut during this feed."""
        data = self._buffer + pcm
        self._buffer = b""
        offset = 0
        chunks: List[AudioChunk] = []

        while offset + self.frame_bytes <= len(data):
            f_bytes = data[offset : offset + self.frame_bytes]
            offset += self.frame_bytes

            samples = np.frombuffer(f_bytes, dtype="<i2")
            rms = float(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))
            self._peak = max(self._peak * 0.9995, rms)
            is_silent = rms < max(self.abs_threshold, self.rel_threshold * self._peak)

            self._frames.append(f_bytes)
            self._frame_rms.append(rms)
            self._frame_silent.append(is_silent)

            curr_samples = len(self._frames) * self.frame_samples
            curr_sec = curr_samples / self.sample_rate

            # Cut rule 1: duration >= min_chunk_sec and current run of silent frames >= silence_sec
            if curr_sec >= self.min_chunk_sec:
                k = 0
                while k < len(self._frame_silent) and self._frame_silent[-(k + 1)]:
                    k += 1
                silent_sec = k * self.frame_samples / self.sample_rate
                if silent_sec >= self.silence_sec:
                    # Cut in the middle of that silent run
                    cut_idx = len(self._frames) - k + (k // 2)
                    chunk_pcm = b"".join(self._frames[:cut_idx])
                    chunk = AudioChunk(
                        index=self._chunk_index,
                        start_sec=self._emitted_samples / self.sample_rate,
                        pcm=chunk_pcm,
                        sample_rate=self.sample_rate,
                    )
                    chunks.append(chunk)
                    self._emitted_samples += len(chunk_pcm) // 2
                    self._chunk_index += 1

                    self._frames = self._frames[cut_idx:]
                    self._frame_rms = self._frame_rms[cut_idx:]
                    self._frame_silent = self._frame_silent[cut_idx:]
                    continue

            # Cut rule 2: duration >= max_chunk_sec
            if curr_sec >= self.max_chunk_sec:
                n_frames = len(self._frames)
                min_frames_1s = int(np.ceil(1.0 * self.sample_rate / self.frame_samples))
                window_frames_3s = int(round(3.0 * self.sample_rate / self.frame_samples))
                start_search = max(min_frames_1s, n_frames - window_frames_3s)

                best_idx = start_search
                best_rms = self._frame_rms[start_search]
                for j in range(start_search + 1, n_frames):
                    if self._frame_rms[j] < best_rms:
                        best_rms = self._frame_rms[j]
                        best_idx = j

                cut_idx = best_idx
                chunk_pcm = b"".join(self._frames[:cut_idx])
                chunk = AudioChunk(
                    index=self._chunk_index,
                    start_sec=self._emitted_samples / self.sample_rate,
                    pcm=chunk_pcm,
                    sample_rate=self.sample_rate,
                )
                chunks.append(chunk)
                self._emitted_samples += len(chunk_pcm) // 2
                self._chunk_index += 1

                self._frames = self._frames[cut_idx:]
                self._frame_rms = self._frame_rms[cut_idx:]
                self._frame_silent = self._frame_silent[cut_idx:]

        self._buffer = data[offset:]
        return chunks

    def flush(self) -> Optional[AudioChunk]:
        """Return everything still buffered as the final chunk, or None if empty, then reset counters."""
        pcm = b"".join(self._frames) + self._buffer
        if not pcm:
            return None
        chunk = AudioChunk(
            index=self._chunk_index,
            start_sec=self._emitted_samples / self.sample_rate,
            pcm=pcm,
            sample_rate=self.sample_rate,
        )
        self.reset()
        return chunk
