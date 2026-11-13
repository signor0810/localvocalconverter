"""Load and parse dictation record Markdown files from storage."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

_MONTH_DIR_RE = re.compile(r"^\d{4}-\d{2}$")


@dataclass
class Record:
    path: Path
    mtime: float
    raw_text: str
    result_text: str
    mode: str = ""


def parse_record_file(path: Path) -> Optional[Record]:
    """Parse a single record Markdown file. Returns None on parse failure."""
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as e:
        logger.debug("Failed to read record %s: %s", path, e)
        return None

    res_marker = "## 結果"
    raw_marker = "## 原文"
    res_idx = text.find(res_marker)
    raw_idx = text.find(raw_marker)
    if res_idx == -1 or raw_idx == -1 or raw_idx < res_idx:
        logger.debug("Record missing required sections: %s", path)
        return None

    result_text = text[res_idx + len(res_marker):raw_idx].strip()
    raw_text = text[raw_idx + len(raw_marker):].strip()

    mode = ""
    if text.startswith("---"):
        end_fm = text.find("\n---", 3)
        if end_fm != -1:
            fm_text = text[3:end_fm]
            for line in fm_text.splitlines():
                if line.strip().startswith("mode:"):
                    mode = line.split(":", 1)[1].strip()

    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0

    return Record(
        path=path,
        mtime=mtime,
        raw_text=raw_text,
        result_text=result_text,
        mode=mode,
    )


def load_records(records_dir: Path | str) -> List[Record]:
    """Read all record Markdown files under records_dir (<YYYY-MM>/*.md)."""
    p = Path(records_dir)
    if not p.is_dir():
        return []

    records: List[Record] = []
    try:
        month_dirs = sorted([d for d in p.iterdir() if d.is_dir() and _MONTH_DIR_RE.match(d.name)])
    except Exception as e:
        logger.debug("Failed to list month dirs in %s: %s", p, e)
        return []

    for m_dir in month_dirs:
        try:
            files = sorted(m_dir.glob("*.md"))
        except Exception as e:
            logger.debug("Failed to list files in %s: %s", m_dir, e)
            continue

        for f in files:
            rec = parse_record_file(f)
            if rec is not None:
                records.append(rec)

    return records
