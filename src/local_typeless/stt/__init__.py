"""Local STT engines."""
from __future__ import annotations

from local_typeless.stt.base import STTEngine
from local_typeless.types import STTProfileConfig


def create_stt_engine(profile: STTProfileConfig, profile_name: str = "") -> STTEngine:
    """profile_name is only used to make the 'model not downloaded' hint precise."""
    from local_typeless.stt.faster_whisper_stt import FasterWhisperSTT

    return FasterWhisperSTT(profile, profile_name)


__all__ = ["STTEngine", "create_stt_engine"]
