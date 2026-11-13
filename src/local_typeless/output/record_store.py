"""Record store for archiving finished dictations as Markdown files."""
from __future__ import annotations

import logging
import re
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

from local_typeless.textblocks import ANY_MARKER_RE, MARKER_RE, marker_for
from local_typeless.types import Mode, PipelineResult

logger = logging.getLogger(__name__)

_MODE_NAMES = {
    Mode.CLEAN: "整理",
    Mode.SUMMARY: "摘要",
    Mode.SLIDE: "投影片",
    Mode.TRANSFORM: "轉換",
}

_ILLEGAL_FILENAME_CHARS = set(r'\/:*?"<>|')
_MONTH_DIR_RE = re.compile(r"^\d{4}-\d{2}$")
_IMAGE_DIR_RE = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{6}$")


def format_record_name(fmt: str, when: datetime, mode: Mode, text: str) -> str:
    """Format a record file name according to a template string."""
    # 1. Remove markers and [?]
    cleaned = ANY_MARKER_RE.sub("", text).replace("[?]", "")

    # 2. Remove characters not allowed in Windows file names and control chars
    cleaned = "".join(c for c in cleaned if c not in _ILLEGAL_FILENAME_CHARS and ord(c) >= 32)

    # 3. Collapse whitespace to single spaces and strip
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    # 4. Take first 20 characters and strip leading/trailing dots and spaces
    title = cleaned[:20].strip(". ")
    if not title:
        title = "無標題"

    date_str = when.strftime("%Y%m%d")
    time_str = when.strftime("%H%M%S")
    mode_str = _MODE_NAMES.get(mode, mode.value)

    return fmt.format(date=date_str, time=time_str, mode=mode_str, title=title)


class RecordStore:
    """Archives finished dictation results and cleans up expired records and images."""

    def __init__(
        self,
        records_dir: Path,
        images_dir: Path,
        retention_days: int = 90,
        filename_format: str = "{date}-{time}_{mode}_{title}",
        now: Callable[[], datetime] = datetime.now,
    ) -> None:
        self.records_dir = Path(records_dir)
        self.images_dir = Path(images_dir)
        self.retention_days = retention_days
        self.filename_format = filename_format
        self.now = now

    def save(self, result: PipelineResult) -> Optional[Path]:
        """Save a finished dictation as a Markdown record. Returns None if blank or failed."""
        if not result.final_text or not result.final_text.strip():
            return None

        try:
            current_time = self.now()
            name = format_record_name(self.filename_format, current_time, result.mode, result.final_text)

            month_dir = self.records_dir / current_time.strftime("%Y-%m")
            month_dir.mkdir(parents=True, exist_ok=True)

            target_file = month_dir / f"{name}.md"
            if target_file.exists():
                counter = 2
                while (month_dir / f"{name}_{counter}.md").exists():
                    counter += 1
                target_file = month_dir / f"{name}_{counter}.md"

            marker_map = {marker_for(img): img for img in result.images}

            def _repl_attachment(m: re.Match) -> str:
                token = m.group(0)
                item = marker_map.get(token)
                if item is not None:
                    kind = getattr(item, "kind", "image")
                    if kind == "image":
                        return f"![圖{item.index}]({item.path.replace('\\', '/')})"
                    elif kind == "table":
                        return f"\n\n{item.text.strip()}\n\n"
                    elif kind in ("link", "text"):
                        return item.text
                    elif kind == "file":
                        return f"`{item.text}`"
                return token

            rendered_final = ANY_MARKER_RE.sub(_repl_attachment, result.final_text)

            content = (
                f"---\n"
                f"time: {current_time.isoformat(timespec='seconds')}\n"
                f"mode: {result.mode.value}\n"
                f"fallback: {str(result.fallback_triggered).lower()}\n"
                f"images: {len(result.images)}\n"
                f"---\n\n"
                f"## 結果\n\n"
                f"{rendered_final}\n\n"
                f"## 原文\n\n"
                f"{result.raw_text}\n"
            )

            target_file.write_text(content, encoding="utf-8")
            return target_file
        except Exception as e:
            logger.warning("RecordStore.save failed: %s: %s", type(e).__name__, e)
            return None

    def cleanup(self) -> int:
        """Delete records and image folders older than retention_days. Returns count of items removed."""
        if self.retention_days <= 0:
            return 0

        removed_count = 0
        try:
            cutoff_ts = (self.now() - timedelta(days=self.retention_days)).timestamp()
            records_dir = self.records_dir.resolve()
            images_dir = self.images_dir.resolve()

            # Only things this tool created are ever deleted, so pointing records_dir or
            # images_dir at a folder that also holds the user's own files is safe:
            #   records: *.md inside month folders named YYYY-MM
            #   images:  capture folders named YYYYMMDD-HHMMSS-xxxxxx
            if records_dir.exists():
                for month in list(records_dir.iterdir()):
                    if not (month.is_dir() and _MONTH_DIR_RE.match(month.name)):
                        continue
                    for md_file in list(month.glob("*.md")):
                        try:
                            if md_file.is_file() and md_file.stat().st_mtime < cutoff_ts:
                                md_file.unlink()
                                removed_count += 1
                        except Exception as e:
                            logger.debug("Failed to delete record %s: %s", md_file, e)
                    try:
                        if not any(month.iterdir()):
                            month.rmdir()
                            removed_count += 1
                    except Exception as e:
                        logger.debug("Failed to remove empty month dir %s: %s", month, e)

            if images_dir.exists():
                for item in list(images_dir.iterdir()):
                    try:
                        if (item.is_dir() and _IMAGE_DIR_RE.match(item.name)
                                and item.stat().st_mtime < cutoff_ts):
                            shutil.rmtree(item)
                            removed_count += 1
                    except Exception as e:
                        logger.debug("Failed to delete image entry %s: %s", item, e)

        except Exception as e:
            logger.warning("RecordStore.cleanup failed: %s: %s", type(e).__name__, e)

        return removed_count


def cleanup_cache_leftovers(cache_dir: Path, max_age_hours: float = 24.0,
                            now: Callable[[], datetime] = datetime.now) -> int:
    """Remove stale temporary audio a crash or failed job can leave in the cache folder:
    `chunks/*.wav` and `tmp_*.wav` older than max_age_hours. Nothing else is touched
    (last recording, history, logs and teach samples stay). Never raises; returns the count."""
    removed = 0
    try:
        cache_dir = Path(cache_dir)
        cutoff = (now() - timedelta(hours=max_age_hours)).timestamp()
        candidates = list(cache_dir.glob("tmp_*.wav")) + list((cache_dir / "chunks").glob("*.wav"))
        for f in candidates:
            try:
                if f.is_file() and f.stat().st_mtime < cutoff:
                    f.unlink()
                    removed += 1
            except OSError as e:
                logger.debug("Failed to delete leftover %s: %s", f, e)
    except Exception as e:
        logger.warning("cleanup_cache_leftovers failed: %s: %s", type(e).__name__, e)
    return removed
