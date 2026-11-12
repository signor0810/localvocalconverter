"""STT engine abstract base class."""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Callable, Optional

from local_typeless.types import TranscriptResult


class STTEngine(ABC):
    """Local speech-to-text engine. Audio must never leave the machine."""

    @abstractmethod
    def transcribe(
        self,
        audio_path: Path,
        initial_prompt: Optional[str] = None,
        on_segment: Optional[Callable[[str], None]] = None,
        word_timestamps: bool = False,
    ) -> TranscriptResult:
        """Transcribe an audio file and return the raw transcript.

        on_segment(text_so_far) is an optional progress callback invoked after each decoded
        segment (from the calling thread). Errors raised by it must not abort decoding."""
