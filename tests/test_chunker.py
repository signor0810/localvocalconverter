"""Tests for SilenceChunker (Task 8)."""
from __future__ import annotations

import numpy as np
import pytest

from local_typeless.recorder.chunker import SilenceChunker
from local_typeless.types import AudioChunk


def make_sine(duration_sec: float, freq: float = 440.0, amplitude: float = 8000.0, sample_rate: int = 16000) -> bytes:
    t = np.linspace(0, duration_sec, int(sample_rate * duration_sec), endpoint=False)
    samples = (amplitude * np.sin(2 * np.pi * freq * t)).astype("<i2")
    return samples.tobytes()


def make_silence(duration_sec: float, noise_amp: int = 0, sample_rate: int = 16000) -> bytes:
    n_samples = int(sample_rate * duration_sec)
    if noise_amp == 0:
        samples = np.zeros(n_samples, dtype="<i2")
    else:
        rng = np.random.default_rng(42)
        samples = rng.integers(-noise_amp, noise_amp + 1, size=n_samples, dtype="<i2")
    return samples.tobytes()


# 1. 14 s speech + 1 s silence + 5 s speech -> first chunk cut inside silence (14.0s .. 15.0s); flush returns rest
def test_silence_cut_after_min_chunk():
    sr = 16000
    chunker = SilenceChunker(sample_rate=sr, min_chunk_sec=12.0, max_chunk_sec=28.0, silence_sec=0.5)

    speech1 = make_sine(14.0, sample_rate=sr)
    silence = make_silence(1.0, sample_rate=sr)
    speech2 = make_sine(5.0, sample_rate=sr)

    # Feed in chunks
    audio = speech1 + silence + speech2
    chunks: list[AudioChunk] = []
    # Feed in 0.5s increments
    step = int(0.5 * sr * 2)
    for i in range(0, len(audio), step):
        chunks.extend(chunker.feed(audio[i : i + step]))

    tail = chunker.flush()
    if tail:
        chunks.append(tail)

    assert len(chunks) == 2
    c1, c2 = chunks[0], chunks[1]
    dur1 = len(c1.pcm) / (sr * 2)
    assert 14.0 <= dur1 <= 15.0
    assert c1.start_sec == 0.0
    assert c2.start_sec == dur1
    assert c1.index == 0
    assert c2.index == 1
    assert b"".join(c.pcm for c in chunks) == audio


# 2. 5 s speech + 1 s silence + 5 s speech (silence before min_chunk_sec) -> no cut at that silence
def test_silence_before_min_chunk_no_cut():
    sr = 16000
    chunker = SilenceChunker(sample_rate=sr, min_chunk_sec=12.0, max_chunk_sec=28.0, silence_sec=0.5)

    speech1 = make_sine(5.0, sample_rate=sr)
    silence = make_silence(1.0, sample_rate=sr)
    speech2 = make_sine(5.0, sample_rate=sr)

    audio = speech1 + silence + speech2  # 11 s total (< 12 s)
    chunks = chunker.feed(audio)
    assert chunks == []

    tail = chunker.flush()
    assert tail is not None
    assert len(tail.pcm) == len(audio)


# 3. 40 s continuous speech -> forced cut at <= 28 s and >= 25 s
def test_forced_cut_on_continuous_speech():
    sr = 16000
    chunker = SilenceChunker(sample_rate=sr, min_chunk_sec=12.0, max_chunk_sec=28.0, silence_sec=0.5)

    speech = make_sine(40.0, sample_rate=sr)
    chunks: list[AudioChunk] = []
    step = int(1.0 * sr * 2)
    for i in range(0, len(speech), step):
        chunks.extend(chunker.feed(speech[i : i + step]))
    tail = chunker.flush()
    if tail:
        chunks.append(tail)

    assert len(chunks) >= 2
    c1 = chunks[0]
    dur1 = len(c1.pcm) / (sr * 2)
    assert 25.0 <= dur1 <= 28.0
    assert b"".join(c.pcm for c in chunks) == speech


# 4. Low-level noise as "silence" (amplitude 20) is treated as silent via relative threshold
def test_low_level_noise_treated_as_silence():
    sr = 16000
    chunker = SilenceChunker(sample_rate=sr, min_chunk_sec=12.0, max_chunk_sec=28.0, silence_sec=0.5)

    speech1 = make_sine(13.0, sample_rate=sr, amplitude=8000.0)
    noise_silence = make_silence(1.0, noise_amp=20, sample_rate=sr)
    speech2 = make_sine(3.0, sample_rate=sr, amplitude=8000.0)

    audio = speech1 + noise_silence + speech2
    chunks = chunker.feed(audio)
    tail = chunker.flush()
    if tail:
        chunks.append(tail)

    assert len(chunks) == 2
    dur1 = len(chunks[0].pcm) / (sr * 2)
    assert 13.0 <= dur1 <= 14.0


# 5. Invariant: random-sized feed slices (seeded) over mixed signal
def test_feed_slices_invariant():
    sr = 16000
    chunker = SilenceChunker(sample_rate=sr, min_chunk_sec=12.0, max_chunk_sec=28.0, silence_sec=0.5)

    # Build mixed audio: 15s speech, 1s silence, 20s speech, 2s silence, 5s speech
    audio = (
        make_sine(15.0, sample_rate=sr)
        + make_silence(1.0, sample_rate=sr)
        + make_sine(20.0, sample_rate=sr)
        + make_silence(2.0, sample_rate=sr)
        + make_sine(5.0, sample_rate=sr)
    )

    rng = np.random.default_rng(12345)
    chunks: list[AudioChunk] = []
    idx = 0
    while idx < len(audio):
        size = int(rng.integers(100, 5000))
        slice_bytes = audio[idx : idx + size]
        chunks.extend(chunker.feed(slice_bytes))
        idx += size

    tail = chunker.flush()
    if tail:
        chunks.append(tail)

    # Concatenation equals input
    assert b"".join(c.pcm for c in chunks) == audio
    # Indices are 0, 1, 2...
    assert [c.index for c in chunks] == list(range(len(chunks)))
    # start_sec is consistent
    cum_samples = 0
    for c in chunks:
        assert c.start_sec == pytest.approx(cum_samples / sr)
        cum_samples += len(c.pcm) // 2


# 6. Odd-sized feeds, empty feed, flush on empty, reset
def test_odd_sized_feeds_and_reset():
    sr = 16000
    chunker = SilenceChunker(sample_rate=sr)

    # Empty feed
    assert chunker.feed(b"") == []

    # Odd-sized feeds (e.g. 3 bytes, 7 bytes)
    c = chunker.feed(b"\x01\x02\x03")
    assert c == []
    c = chunker.feed(b"\x04\x05\x06\x07")
    assert c == []

    # Flush delivers the partial data
    tail = chunker.flush()
    assert tail is not None
    assert tail.pcm == b"\x01\x02\x03\x04\x05\x06\x07"

    # Second flush on empty -> None
    assert chunker.flush() is None

    # Reset
    chunker.reset()
    assert chunker.flush() is None
