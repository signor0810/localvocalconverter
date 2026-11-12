"""LLM client abstract base class."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List


class LLMClient(ABC):
    @abstractmethod
    def refine(self, raw_text: str, system_prompt: str) -> str:
        """Return refined text. Must raise LLMError subclasses on any failure
        (never return an empty string silently)."""

    @abstractmethod
    def list_models(self) -> List[str]:
        """Return model ids from the server. Raises LLMError on failure."""

    def ping(self) -> List[str]:
        return self.list_models()
