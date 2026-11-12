"""Obsidian Output Adapter (WP2-A, owned by flash-infra).

Implements OutputAdapter protocol defined in local_typeless.types.
Appends slide contents with markdown separators to the specified Obsidian note.
"""
from __future__ import annotations

from pathlib import Path
from typing import Union

from local_typeless.types import Mode, OutputAdapter


def append_to_file(note_path: Union[Path, str], text: str) -> None:
    """Append text preceded by a separator block to the target markdown file.

    Creates parent directories and the file if missing, using UTF-8 encoding.
    """
    p = Path(note_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(f"\n\n---\n{text}")


class ObsidianOutput(OutputAdapter):
    """Output adapter writing to an Obsidian vault note."""

    def __init__(self, note_path: Union[Path, str]) -> None:
        self.note_path = Path(note_path)

    def emit(self, text: str, mode: Mode) -> None:
        """Append formatted text to the note path."""
        append_to_file(self.note_path, text)
