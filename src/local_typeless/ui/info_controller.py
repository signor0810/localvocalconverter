"""Tk-free data for the info panel: Home (usage numbers), History, System prompt.

Everything is read from files that already exist (the record folder and the prompt
files). The only things written are a prompt file, the user glossary and the settings file,
and only when the user saves one.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import yaml

from local_typeless.config.loader import load_prompt, user_prompt_path
from local_typeless.glossary.manager import user_overlay_path
from local_typeless.review.records import Record, load_records
from local_typeless.textblocks import ANY_MARKER_RE
from local_typeless.types import Mode
from local_typeless.ui.controller import format_hotkey

logger = logging.getLogger(__name__)

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


# what Whisper is told about the language; "" = let it detect (best for mixed speech)
LANGUAGE_CHOICES: Tuple[Tuple[str, str], ...] = (
    ("", "自動（中英混雜）"),
    ("zh", "中文為主（比較快）"),
    ("en", "英文為主（比較快）"),
)


@dataclass(frozen=True)
class SettingsView:
    language: str                # a key of LANGUAGE_CHOICES
    min_chunk_sec: float         # a piece is at least this long
    silence_sec: float           # a pause this long ends a piece
    max_chunk_sec: float         # a piece is cut here even without a pause
    cache_dir: str
    records_dir: str
    path: Optional[Path]         # the settings file; None = settings cannot be saved
    stt_profile: str = ""        # the chosen recognition profile (used from the next start)
    stt_profiles: Tuple[Tuple[str, str], ...] = ()      # (profile name, label)


@dataclass(frozen=True)
class DictEntry:
    term: str
    definition: str
    aliases: Tuple[str, ...]     # words that are often heard instead of the term
    builtin: bool                # True = comes from the base file


@dataclass(frozen=True)
class DictionaryView:
    entries: List[DictEntry]
    base_path: Path
    user_path: Path
    error: str = ""              # e.g. the base file cannot be read


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
        on_glossary_changed: Optional[Callable[[], None]] = None,
        settings_file: Optional[Path] = None,
    ) -> None:
        self.cfg = cfg
        self._settings_file = settings_file
        self._chosen_stt_profile: Optional[str] = None      # saved, waiting for a restart
        self._glossary_section = glossary_section
        self._now = now
        self._on_glossary_changed = on_glossary_changed

    def _write_backup(
        self, folder_name: str, stem: str, ext: str, old_bytes: bytes
    ) -> Optional[Path]:
        if not old_bytes.strip():
            return None
        backups = Path(self.cfg.cache_dir) / folder_name
        backups.mkdir(parents=True, exist_ok=True)
        stamp = self._now().strftime("%Y%m%d-%H%M%S")
        backup = backups / f"{stem}-{stamp}{ext}"
        n = 1
        while backup.exists():       # two loads within the same second
            n += 1
            backup = backups / f"{stem}-{stamp}-{n}{ext}"
        backup.write_bytes(old_bytes)
        return backup

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
                self._write_backup("prompt_backups", mode.value, ".md", old_bytes)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes((text + "\n").encode("utf-8"))      # \n line ends on every OS
        except Exception as e:
            return False, f"存檔失敗：{type(e).__name__}: {e}"
        message = "已載入，下一次整理就會用這一份"
        if "{glossary}" not in text:
            message += "。注意：內容裡沒有 {glossary}，詞庫不會送給 LLM"
        return True, message

    # -------------------------------------------------------------- settings
    def settings(self) -> SettingsView:
        cfg = self.cfg
        st = cfg.streaming
        language = cfg.stt.language or ""
        if language not in dict(LANGUAGE_CHOICES):
            language = ""
        return SettingsView(
            language=language, min_chunk_sec=st.min_chunk_sec, silence_sec=st.silence_sec,
            max_chunk_sec=st.max_chunk_sec, cache_dir=str(cfg.cache_dir),
            records_dir=str(self.records_dir), path=self._settings_file,
            stt_profile=self._chosen_stt_profile or cfg.active_stt_profile,
            stt_profiles=tuple((name, self._stt_profile_label(name)) for name in cfg.stt_profiles),
        )

    _STT_PROFILE_HINTS = {"cpu_fast": "快", "cpu_accurate": "準，比較慢", "gpu_local": "準，要顯示卡"}

    def _stt_profile_label(self, name: str) -> str:
        p = self.cfg.stt_profiles[name]
        device = "顯示卡" if p.device == "cuda" else "CPU"
        hint = self._STT_PROFILE_HINTS.get(name, name)
        return f"{p.model_size}・{device}（{hint}）"

    def _stt_model_missing(self, name: str) -> bool:
        p = self.cfg.stt_profiles[name]
        try:
            from local_typeless.stt.faster_whisper_stt import is_model_available

            src = str(p.model_path) if p.model_path and Path(p.model_path).is_dir() else p.model_size
            return not is_model_available(src)
        except Exception as e:                # cannot tell: say nothing
            logger.debug("model check failed: %s", e)
            return False

    def save_settings(self, language: str, min_chunk_sec: Any, silence_sec: Any,
                      max_chunk_sec: Any, cache_dir: str, records_dir: str,
                      stt_profile: Optional[str] = None) -> Tuple[bool, str]:
        """Write the settings file. Language and seconds apply from the next recording;
        the model and the folders apply after a restart (files already there are not moved)."""
        path = self._settings_file
        if path is None:
            return False, "找不到設定檔的位置，無法儲存"
        if language not in dict(LANGUAGE_CHOICES):
            return False, "語言選項不對"
        if stt_profile is not None and stt_profile not in self.cfg.stt_profiles:
            return False, "辨識模型選項不對"
        try:
            low, pause, high = float(min_chunk_sec), float(silence_sec), float(max_chunk_sec)
        except (TypeError, ValueError):
            return False, "秒數要填數字"
        if not 2 <= low <= 60:
            return False, "「每段至少」要在 2 到 60 秒之間"
        if not 0.2 <= pause <= 5:
            return False, "「停頓」要在 0.2 到 5 秒之間"
        if not low < high <= 120:
            return False, "「每段最長」要比「每段至少」大，最多 120 秒"
        cache_dir, records_dir = cache_dir.strip(), records_dir.strip()
        if not cache_dir or not records_dir:
            return False, "資料夾不能是空的"
        old = self.settings()
        folders_changed = (cache_dir, records_dir) != (old.cache_dir, old.records_dir)
        model_changed = stt_profile is not None and stt_profile != old.stt_profile
        try:
            for folder in (cache_dir, records_dir):
                Path(folder).mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return False, f"資料夾建不起來：{e}"

        data: Dict[str, Any] = {}
        try:
            if path.exists():
                loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data = loaded
        except Exception as e:           # written by this page only: start again
            logger.warning("settings file %s unreadable, rewriting: %s", path, e)
        profiles = data.get("stt_profiles") if isinstance(data.get("stt_profiles"), dict) else {}
        for name in self.cfg.stt_profiles:           # the choice is the user's, not a profile's
            entry = profiles.get(name) if isinstance(profiles.get(name), dict) else {}
            profiles[name] = dict(entry, language=language or None)
        data["stt_profiles"] = profiles
        streaming = data.get("streaming") if isinstance(data.get("streaming"), dict) else {}
        data["streaming"] = dict(streaming, min_chunk_sec=low, silence_sec=pause, max_chunk_sec=high)
        if model_changed or (stt_profile is not None and "active_stt_profile" in data):
            data["active_stt_profile"] = stt_profile
        if cache_dir != old.cache_dir or "cache_dir" in data:
            data["cache_dir"] = cache_dir
        storage = data.get("storage") if isinstance(data.get("storage"), dict) else {}
        if records_dir != old.records_dir or "records_dir" in storage:
            storage = dict(storage, records_dir=records_dir)
        if storage:
            data["storage"] = storage
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            dumped = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
            path.write_bytes(dumped.encode("utf-8"))
        except OSError as e:
            return False, f"設定檔寫不進去：{e}"

        for profile in self.cfg.stt_profiles.values():        # live objects the app reads
            profile.language = language or None
        st = self.cfg.streaming
        st.min_chunk_sec, st.silence_sec, st.max_chunk_sec = low, pause, high
        message = "已儲存，下一次錄音開始用"
        if model_changed:
            self._chosen_stt_profile = stt_profile
            message += "；辨識模型要重開程式才會換"
            if self._stt_model_missing(stt_profile):
                message += (f"。這個模型還沒下載，先執行 python -m local_typeless.cli "
                            f"download-model --stt-profile {stt_profile}")
        if folders_changed:
            message += "；資料夾要重開程式才會換，原本的檔案不會搬過去"
        return True, message

    # ------------------------------------------------------------ dictionary
    def dictionary(self) -> DictionaryView:
        base_path = Path(getattr(self.cfg, "glossary_path", "config/glossary.yaml"))
        user_path = user_overlay_path(base_path)

        if not base_path.exists():
            return DictionaryView(
                entries=[], base_path=base_path, user_path=user_path,
                error=f"詞庫檔案不存在：{base_path}",
            )

        try:
            with base_path.open("r", encoding="utf-8") as f:
                base_data = yaml.safe_load(f)
        except Exception as e:
            return DictionaryView(
                entries=[], base_path=base_path, user_path=user_path,
                error=f"詞庫檔案讀取失敗：{type(e).__name__}: {e}",
            )

        if not isinstance(base_data, dict):
            return DictionaryView(
                entries=[], base_path=base_path, user_path=user_path,
                error=f"詞庫檔案格式錯誤（非 mapping）：{base_path}",
            )

        raw_base_canonical = base_data.get("canonical_terms", []) or []
        base_terms: List[Tuple[str, str]] = []
        if isinstance(raw_base_canonical, list):
            for item in raw_base_canonical:
                if isinstance(item, dict):
                    t = str(item.get("term", "")).strip()
                    d = str(item.get("definition", "")).strip()
                    if t:
                        base_terms.append((t, d))
                elif isinstance(item, str) and item.strip():
                    base_terms.append((item.strip(), ""))

        raw_base_aliases = base_data.get("phonetic_aliases", {}) or {}
        base_aliases: Dict[str, str] = {}
        if isinstance(raw_base_aliases, dict):
            base_aliases = {
                str(k).strip(): str(v).strip()
                for k, v in raw_base_aliases.items()
                if str(k).strip() and str(v).strip()
            }

        user_error = ""
        user_terms: List[Tuple[str, str]] = []
        user_aliases: Dict[str, str] = {}
        if user_path.exists():
            try:
                with user_path.open("r", encoding="utf-8") as f:
                    u_data = yaml.safe_load(f)
                if u_data is None:
                    u_data = {}
                elif not isinstance(u_data, dict):
                    user_error = f"使用者詞庫格式錯誤（非 mapping）：{user_path}"
                else:
                    raw_user_canonical = u_data.get("canonical_terms", []) or []
                    if isinstance(raw_user_canonical, list):
                        for item in raw_user_canonical:
                            if isinstance(item, dict):
                                t = str(item.get("term", "")).strip()
                                d = str(item.get("definition", "")).strip()
                                if t:
                                    user_terms.append((t, d))
                            elif isinstance(item, str) and item.strip():
                                user_terms.append((item.strip(), ""))
                    raw_u_aliases = u_data.get("phonetic_aliases", {}) or {}
                    if isinstance(raw_u_aliases, dict):
                        user_aliases = {
                            str(k).strip(): str(v).strip()
                            for k, v in raw_u_aliases.items()
                            if str(k).strip() and str(v).strip()
                        }
            except Exception as e:
                user_error = f"使用者詞庫讀取失敗：{type(e).__name__}: {e}"

        merged_aliases: Dict[str, str] = dict(base_aliases)
        merged_aliases.update(user_aliases)

        entries: List[DictEntry] = []
        seen_terms = set()

        # 1. Base terms in file order (builtin=True)
        for term, defn in base_terms:
            if term not in seen_terms:
                aliases = tuple(k for k, v in merged_aliases.items() if v == term)
                entries.append(DictEntry(term=term, definition=defn, aliases=aliases, builtin=True))
                seen_terms.add(term)

        # Base alias targets not in canonical terms (builtin=True)
        for _alias, target in base_aliases.items():
            if target not in seen_terms:
                aliases = tuple(k for k, v in merged_aliases.items() if v == target)
                entries.append(DictEntry(term=target, definition="", aliases=aliases, builtin=True))
                seen_terms.add(target)

        # 2. User terms in file order (builtin=False)
        for term, defn in user_terms:
            if term not in seen_terms:
                aliases = tuple(k for k, v in merged_aliases.items() if v == term)
                entries.append(DictEntry(term=term, definition=defn, aliases=aliases, builtin=False))
                seen_terms.add(term)

        # 3. User alias targets not in any canonical term list (builtin=False)
        for _alias, target in user_aliases.items():
            if target not in seen_terms:
                aliases = tuple(k for k, v in merged_aliases.items() if v == target)
                entries.append(DictEntry(term=target, definition="", aliases=aliases, builtin=False))
                seen_terms.add(target)

        return DictionaryView(
            entries=entries, base_path=base_path, user_path=user_path, error=user_error
        )

    def save_term(
        self,
        term: str,
        definition: str,
        aliases: Sequence[str],
        original: Optional[str] = None,
    ) -> Tuple[bool, str]:
        term = term.strip()
        definition = definition.strip()
        if not term:
            return False, "詞不能是空的"

        clean_aliases: List[str] = []
        seen_aliases = set()
        for a in aliases:
            a_clean = str(a).strip()
            if not a_clean or a_clean == term or a_clean in seen_aliases:
                continue
            seen_aliases.add(a_clean)
            clean_aliases.append(a_clean)

        dict_view = self.dictionary()
        if dict_view.error and not dict_view.entries:
            return False, f"無法讀取基底詞庫：{dict_view.error}"

        user_path = dict_view.user_path
        old_bytes = b""
        user_data: Dict[str, Any] = {}
        if user_path.exists():
            try:
                old_bytes = user_path.read_bytes()
                loaded = yaml.safe_load(old_bytes)
                if loaded is None:
                    user_data = {}
                elif not isinstance(loaded, dict):
                    return False, "使用者詞庫格式錯誤（非 mapping），未免覆寫已取消"
                else:
                    user_data = loaded
            except Exception as e:
                return False, f"使用者詞庫損毀（YAML 錯誤）：{e}，未免覆寫已取消"

        orig_clean = str(original).strip() if original is not None else None
        merged_alias_map: Dict[str, str] = {}
        for entry in dict_view.entries:
            for al in entry.aliases:
                merged_alias_map[al] = entry.term

        for a in clean_aliases:
            if a in merged_alias_map:
                existing_target = merged_alias_map[a]
                if existing_target != term and existing_target != orig_clean:
                    return False, f"別名「{a}」已經指向「{existing_target}」"

        builtin_terms = {e.term for e in dict_view.entries if e.builtin}
        term_is_builtin = term in builtin_terms

        base_aliases_for_term = set()
        base_path = dict_view.base_path
        if base_path.exists():
            try:
                with base_path.open("r", encoding="utf-8") as f:
                    b_data = yaml.safe_load(f) or {}
                if isinstance(b_data, dict):
                    for k, v in (b_data.get("phonetic_aliases") or {}).items():
                        if str(v).strip() == term:
                            base_aliases_for_term.add(str(k).strip())
            except Exception:
                pass

        if "canonical_terms" not in user_data or not isinstance(user_data["canonical_terms"], list):
            user_data["canonical_terms"] = []
        if "phonetic_aliases" not in user_data or not isinstance(user_data["phonetic_aliases"], dict):
            user_data["phonetic_aliases"] = {}

        u_canonical: List[Any] = user_data["canonical_terms"]
        u_aliases: Dict[str, Any] = user_data["phonetic_aliases"]

        if orig_clean and orig_clean != term and orig_clean not in builtin_terms:
            user_data["canonical_terms"] = [
                item for item in u_canonical
                if not (isinstance(item, dict) and item.get("term") == orig_clean or item == orig_clean)
            ]
            u_canonical = user_data["canonical_terms"]
            keys_to_del = [k for k, v in u_aliases.items() if str(v) == orig_clean]
            for k in keys_to_del:
                del u_aliases[k]

        if term_is_builtin:
            user_data["canonical_terms"] = [
                item for item in u_canonical
                if not (isinstance(item, dict) and item.get("term") == term or item == term)
            ]
            keys_to_del = [k for k, v in u_aliases.items() if str(v) == term]
            for k in keys_to_del:
                del u_aliases[k]
            for a in clean_aliases:
                if a not in base_aliases_for_term:
                    u_aliases[a] = term
            message = f"已儲存「{term}」的別名（內建詞說明以內建檔案為準）"
        else:
            found = False
            for idx, item in enumerate(u_canonical):
                if isinstance(item, dict) and item.get("term") == term:
                    u_canonical[idx] = {"term": term, "definition": definition}
                    found = True
                    break
                elif item == term:
                    u_canonical[idx] = {"term": term, "definition": definition}
                    found = True
                    break
            if not found:
                u_canonical.append({"term": term, "definition": definition})

            keys_to_del = [k for k, v in u_aliases.items() if str(v) == term]
            for k in keys_to_del:
                del u_aliases[k]
            for a in clean_aliases:
                u_aliases[a] = term
            message = f"已儲存「{term}」"

        return self._write_user_glossary(user_path, user_data, old_bytes, message)

    def _write_user_glossary(self, user_path: Path, user_data: Dict[str, Any], old_bytes: bytes,
                             message: str) -> Tuple[bool, str]:
        """Back up the old user file, write the new one, then tell the app to reload."""
        try:
            self._write_backup("glossary_backups", "glossary.user", ".yaml", old_bytes)
            user_path.parent.mkdir(parents=True, exist_ok=True)
            dumped = yaml.safe_dump(user_data, allow_unicode=True, sort_keys=False)
            user_path.write_bytes(dumped.replace("\r\n", "\n").encode("utf-8"))
        except Exception as e:
            return False, f"寫入使用者詞庫失敗：{type(e).__name__}: {e}"
        if self._on_glossary_changed is not None:
            try:
                self._on_glossary_changed()
            except Exception as e:
                logger.warning("on_glossary_changed failed: %s", e)
        return True, message

    def delete_term(self, term: str) -> Tuple[bool, str]:
        term = term.strip()
        if not term:
            return False, "詞不能是空的"

        dict_view = self.dictionary()
        builtin_terms = {e.term for e in dict_view.entries if e.builtin}
        if term in builtin_terms:
            return False, "內建詞不能在這裡刪，請改 glossary.yaml"
        if term not in {e.term for e in dict_view.entries}:
            return False, f"找不到「{term}」"

        user_path = dict_view.user_path
        if not user_path.exists():
            return False, f"找不到「{term}」"

        old_bytes = b""
        user_data: Dict[str, Any] = {}
        try:
            old_bytes = user_path.read_bytes()
            loaded = yaml.safe_load(old_bytes)
            if not isinstance(loaded, dict):
                return False, "使用者詞庫格式錯誤（非 mapping），無法刪除"
            user_data = loaded
        except Exception as e:
            return False, f"使用者詞庫損毀（YAML 錯誤）：{e}，無法刪除"

        u_canonical = user_data.get("canonical_terms")
        if isinstance(u_canonical, list):
            user_data["canonical_terms"] = [
                item for item in u_canonical
                if not (isinstance(item, dict) and item.get("term") == term or item == term)
            ]

        u_aliases = user_data.get("phonetic_aliases")
        if isinstance(u_aliases, dict):
            keys_to_del = [k for k, v in u_aliases.items() if str(v) == term]
            for k in keys_to_del:
                del u_aliases[k]

        return self._write_user_glossary(user_path, user_data, old_bytes, f"已刪除「{term}」")
