"""State tracking for record review (scanned files and ignored terms)."""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Set

logger = logging.getLogger(__name__)


@dataclass
class ReviewState:
    """Tracks examined record files (by path + mtime) and ignored terms.

    Atomic write ensures no partial file writes on crash.
    Corrupted files fall back to an empty state.
    """

    files: Dict[str, float] = field(default_factory=dict)
    ignored: Set[str] = field(default_factory=set)

    @classmethod
    def load(cls, path: Path | str) -> ReviewState:
        p = Path(path)
        if not p.is_file():
            return cls()
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                logger.warning("Corrupt state file (not a dict): %s", p)
                return cls()
            raw_files = data.get("files", {})
            if not isinstance(raw_files, dict):
                raw_files = {}
            raw_ignored = data.get("ignored", [])
            if not isinstance(raw_ignored, list):
                raw_ignored = []

            files = {str(k): float(v) for k, v in raw_files.items()}
            ignored = {str(x).strip() for x in raw_ignored if str(x).strip()}
            return cls(files=files, ignored=ignored)
        except Exception as e:
            logger.warning("Failed to load review state from %s: %s", p, e)
            return cls()

    def save(self, path: Path | str) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        data = {
            "files": self.files,
            "ignored": sorted(list(self.ignored)),
        }
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, p)

    def _norm_path(self, path: Path | str) -> str:
        try:
            return str(Path(path).resolve())
        except Exception:
            return str(path)

    def is_file_changed(self, path: Path | str, mtime: float) -> bool:
        norm = self._norm_path(path)
        recorded = self.files.get(norm)
        if recorded is None:
            # Check un-normalized string key as fallback
            recorded = self.files.get(str(path))
        if recorded is None:
            return True
        return abs(recorded - mtime) > 1e-4

    def update_file(self, path: Path | str, mtime: float) -> None:
        norm = self._norm_path(path)
        self.files[norm] = mtime

    def add_ignored(self, term: str) -> None:
        t = term.strip()
        if t:
            self.ignored.add(t)

    def is_ignored(self, term: str) -> bool:
        t = term.strip().lower()
        return any(t == ig.lower() for ig in self.ignored)

    def reset_files(self) -> None:
        self.files.clear()
