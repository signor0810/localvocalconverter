"""Local-only storage of teach samples (JSONL under <cache_dir>/teach/)."""
from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional


@dataclass
class TeachSample:
    mode: str
    raw_text: str
    output_text: str
    expected_text: str
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])


class TeachStore:
    def __init__(self, cache_dir) -> None:
        self.path = Path(cache_dir) / "teach" / "samples.jsonl"
        self._lock = threading.Lock()

    def _read(self) -> List[TeachSample]:
        if not self.path.exists():
            return []
        out: List[TeachSample] = []
        for line in self.path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                out.append(TeachSample(**{k: str(d[k]) for k in (
                    "mode", "raw_text", "output_text", "expected_text", "created_at", "id")}))
            except (ValueError, KeyError, TypeError):
                continue  # skip corrupt rows
        return out

    def _write_all(self, rows: List[TeachSample]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".jsonl.tmp")
        tmp.write_text(
            "".join(json.dumps(asdict(r), ensure_ascii=False) + "\n" for r in rows),
            encoding="utf-8")
        os.replace(tmp, self.path)

    def add(self, mode: str, raw_text: str, output_text: str, expected_text: str) -> TeachSample:
        sample = TeachSample(mode=mode, raw_text=raw_text, output_text=output_text,
                             expected_text=expected_text)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(asdict(sample), ensure_ascii=False) + "\n")
        return sample

    def list(self, mode: Optional[str] = None) -> List[TeachSample]:
        with self._lock:
            rows = self._read()
        return [r for r in rows if mode is None or r.mode == mode]

    def delete(self, sample_id: str) -> bool:
        with self._lock:
            rows = self._read()
            kept = [r for r in rows if r.id != sample_id]
            if len(kept) == len(rows):
                return False
            self._write_all(kept)
            return True

    def clear(self, mode: Optional[str] = None) -> int:
        with self._lock:
            rows = self._read()
            kept = [r for r in rows if mode is not None and r.mode != mode]
            if len(kept) != len(rows):
                self._write_all(kept)
            return len(rows) - len(kept)
