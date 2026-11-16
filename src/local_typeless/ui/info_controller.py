"""Tk-free data for the info panel: Home (usage numbers), History, System prompt.

Everything is read from files that already exist (the record folder and the prompt
files). The only thing written is a prompt file, when the user loads an edited one.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from local_typeless.config.loader import load_prompt, user_prompt_path
from local_typeless.review.records import Record, load_records
from local_typeless.textblocks import ANY_MARKER_RE
from local_typeless.types import Mode
from local_typeless.ui.controller import format_hotkey

MODE_LABELS: Dict[str, str] = {
    Mode.CLEAN.value: "整理",
    Mode.PROMPT.value: "AI 提示",
    Mode.SUMMARY.value: "摘要",
    Mode.SLIDE.value: "投影片",
    Mode.TRANSFORM.value: "轉換",
}
# Modes that have a prompt file of their own, in the order the draft window lists them.
PROMPT_MODES: Tuple[Mode, ...] = (Mode.CLEAN, Mode.PROMPT, Mode.SUMMARY, Mode.SLIDE)

_UNIT = re.compile(r"[一-鿿]|[A-Za-z0-9]+(?:\.\d+)?")


def count_units(text: str) -> int:
    """Size of a text: Chinese characters plus English words and numbers."""
    return len(_UNIT.findall(ANY_MARKER_RE.sub(" ", text or "")))


def streaks(days: Sequence[date], today: date) -> Tuple[int, int]:
    """(current, longest) run of consecutive days. The current run may end yesterday."""
    have = set(days)
    longest = run = 0
    prev: Optional[date] = None
    for d in sorted(have):
        run = run + 1 if prev is not None and d - prev == timedelta(days=1) else 1
        longest = max(longest, run)
        prev = d
    start = today if today in have else today - timedelta(days=1)
    current = 0
    while start in have:
        current += 1
        start -= timedelta(days=1)
    return current, longest


@dataclass
class HomeStats:
    records: int = 0
    words: int = 0
    fallbacks: int = 0
    active_days: int = 0
    current_streak: int = 0
    longest_streak: int = 0
    day_counts: Dict[date, int] = field(default_factory=dict)
    hotkeys: List[Tuple[str, str]] = field(default_factory=list)      # (what it does, key)
    engine: List[Tuple[str, str]] = field(default_factory=list)       # (name, value)
    retention_days: int = 0
    records_dir: str = ""


@dataclass
class HistoryEntry:
    when: datetime
    mode: str
    mode_label: str
    text: str
    raw_text: str
    fallback: bool
    path: Path


@dataclass
class PromptView:
    mode: Mode
    text: str = ""
    base_path: Optional[Path] = None
    user_path: Optional[Path] = None
    has_user_rules: bool = False
    error: str = ""


def _when(rec: Record) -> datetime:
    when = rec.when if rec.when is not None else datetime.fromtimestamp(rec.mtime)
    if when.tzinfo is not None:      # a hand-written record: compare as local time
        when = when.astimezone().replace(tzinfo=None)
    return when


def _read_prompt_file(path: Path) -> str:
    """Prompt file as text with \\n line ends; odd bytes are shown rather than hidden."""
    return path.read_bytes().decode("utf-8-sig", errors="replace").replace("\r\n", "\n")


class InfoController:
    def __init__(
        self,
        cfg: Any,
        glossary_section: Optional[Callable[[], str]] = None,
        now: Callable[[], datetime] = datetime.now,
    ) -> None:
        self.cfg = cfg
        self._glossary_section = glossary_section
        self._now = now

    # ---------------------------------------------------------------- records
    @property
    def records_dir(self) -> Path:
        return Path(getattr(self.cfg.storage, "records_dir", "records"))

    def _records(self) -> List[Record]:
        return load_records(self.records_dir)

    def home(self) -> HomeStats:
        cfg = self.cfg
        records = self._records()
        day_counts: Dict[date, int] = {}
        words = fallbacks = 0
        for rec in records:
            day = _when(rec).date()
            day_counts[day] = day_counts.get(day, 0) + 1
            words += count_units(rec.result_text)
            fallbacks += 1 if rec.fallback else 0
        current, longest = streaks(list(day_counts), self._now().date())

        hk = cfg.hotkeys
        hotkeys = [
            ("開始／停止錄音", format_hotkey(hk.clean)),
            ("貼上參照", format_hotkey(hk.paste_image)),
            ("取消錄音", format_hotkey(hk.cancel)),
        ]
        stt = cfg.stt
        engine = [
            ("語音辨識", f"{cfg.active_stt_profile}（{stt.model_size}，{stt.device}）"),
            ("LLM", f"{cfg.active_llm_profile}（{cfg.llm.model}）"),
            ("防呆檢查", "開" if cfg.refine.guard else "關"),
        ]
        return HomeStats(
            records=len(records), words=words, fallbacks=fallbacks,
            active_days=len(day_counts), current_streak=current, longest_streak=longest,
            day_counts=day_counts, hotkeys=hotkeys, engine=engine,
            retention_days=int(getattr(cfg.storage, "retention_days", 0) or 0),
            records_dir=str(self.records_dir),
        )

    def history(self, mode: Optional[str] = None, limit: int = 300) -> List[HistoryEntry]:
        """Finished dictations, newest first. `mode` is a Mode value; None means all."""
        entries = [
            HistoryEntry(
                when=_when(rec), mode=rec.mode, mode_label=MODE_LABELS.get(rec.mode, rec.mode),
                text=rec.result_text, raw_text=rec.raw_text, fallback=rec.fallback, path=rec.path,
            )
            for rec in self._records()
            if mode is None or rec.mode == mode
        ]
        entries.sort(key=lambda e: e.when, reverse=True)
        return entries[:limit]

    # ----------------------------------------------------------------- prompt
    def prompt(self, mode: Mode) -> PromptView:
        """The system prompt exactly as it is sent to the LLM for this mode."""
        prompts_dir = Path(self.cfg.prompts_dir)
        base = prompts_dir / f"{mode.value}.md"
        user = Path(user_prompt_path(mode, prompts_dir))
        view = PromptView(mode=mode, base_path=base, user_path=user)
        try:
            view.has_user_rules = user.exists() and bool(user.read_text(encoding="utf-8").strip())
            glossary = self._glossary_section() if self._glossary_section is not None else ""
            view.text = load_prompt(mode, prompts_dir, glossary)
        except Exception as e:
            view.error = f"讀不到 prompt：{type(e).__name__}: {e}"
        return view

    def prompt_source(self, mode: Mode) -> str:
        """The editable prompt file of this mode, as it is on disk ("" when unreadable)."""
        try:
            return _read_prompt_file(Path(self.cfg.prompts_dir) / f"{mode.value}.md")
        except Exception:
            return ""

    def save_prompt(self, mode: Mode, text: str) -> Tuple[bool, str]:
        """Write the prompt file; the next refine uses it. Returns (saved, message).

        The version being replaced is kept in <cache_dir>/prompt_backups.
        """
        text = text.strip("\n")
        if not text.strip():
            return False, "prompt 是空的，沒有存檔"
        path = Path(self.cfg.prompts_dir) / f"{mode.value}.md"
        try:
            old_bytes = path.read_bytes() if path.exists() else b""
            old = _read_prompt_file(path) if old_bytes else ""
            if old.strip("\n") == text:
                return True, "內容沒有變，目前用的就是這一份"
            if old_bytes.strip():
                backups = Path(self.cfg.cache_dir) / "prompt_backups"
                backups.mkdir(parents=True, exist_ok=True)
                stamp = self._now().strftime("%Y%m%d-%H%M%S")
                backup = backups / f"{mode.value}-{stamp}.md"
                n = 1
                while backup.exists():       # two loads within the same second
                    n += 1
                    backup = backups / f"{mode.value}-{stamp}-{n}.md"
                backup.write_bytes(old_bytes)            # the old file, byte for byte
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes((text + "\n").encode("utf-8"))      # \n line ends on every OS
        except Exception as e:
            return False, f"存檔失敗：{type(e).__name__}: {e}"
        message = "已載入，下一次整理就會用這一份"
        if "{glossary}" not in text:
            message += "。注意：內容裡沒有 {glossary}，詞庫不會送給 LLM"
        return True, message
