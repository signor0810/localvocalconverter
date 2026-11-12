"""Tk-free UI logic: a thread-safe view-model fed by the hotkey daemon's callbacks.

Worker/hook threads call `on_state` / `on_result` (they only mutate the view-model under
a lock). The Tk thread calls `poll()` on a timer and renders the snapshot it returns, so
Tk is never touched from another thread.
"""
from __future__ import annotations

import copy
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional, Sequence

from local_typeless.hotkey.daemon import Event, State
from local_typeless.textblocks import strip_markers
from local_typeless.types import Mode

logger = logging.getLogger(__name__)

HISTORY_MAX = 10

# (label shown in the dropdown, Mode); first entry is the default.
MODE_CHOICES = [("整理", Mode.CLEAN), ("摘要", Mode.SUMMARY), ("投影片", Mode.SLIDE)]
RESULT_LABELS = {
    Mode.SUMMARY: "摘要結果",
    Mode.CLEAN: "整理結果",
    Mode.SLIDE: "投影片結果",
    Mode.TRANSFORM: "轉換結果",
}
FALLBACK_NOTICE = "LLM 未連線，顯示原文"


def format_hotkey(hotkey: str) -> str:
    """Format pynput hotkey string, e.g. '<alt>+q' -> 'Alt+Q'."""
    if not hotkey:
        return ""
    known = {
        "alt": "Alt",
        "ctrl": "Ctrl",
        "shift": "Shift",
        "cmd": "Win",
        "esc": "Esc",
        "space": "Space",
    }
    parts = hotkey.split("+")
    out = []
    for part in parts:
        p = part.strip()
        if p.startswith("<") and p.endswith(">"):
            tag = p[1:-1].lower()
            out.append(known.get(tag, tag.capitalize()))
        elif len(p) == 1:
            out.append(p.upper())
        else:
            out.append(p.capitalize())
    return "+".join(out)


def describe_llm_error(exc: BaseException, api_key_env: str = "") -> str:
    """Short zh-TW reason for the status bar."""
    from local_typeless.types import LLMConnectionError, LLMTimeoutError

    text = str(exc)
    m = re.search(r"HTTP (\d{3})", text)
    if m:
        code = int(m.group(1))
        if code in (401, 403):
            suffix = f" ({api_key_env})" if api_key_env else ""
            return f"金鑰錯誤或未設定{suffix}"
        if code == 404:
            return "找不到服務路徑 (HTTP 404)"
        return f"伺服器錯誤 (HTTP {code})"
    if "CERTIFICATE_VERIFY_FAILED" in text or "ca_bundle" in text:
        return "憑證驗證失敗（請設定 ca_bundle）"
    if isinstance(exc, LLMTimeoutError):
        return "連線逾時"
    if isinstance(exc, LLMConnectionError):
        return "無法連線"
    return (text.splitlines() or [type(exc).__name__])[0][:40]


def mode_label(mode: Mode) -> str:
    for label, m in MODE_CHOICES:
        if m is mode:
            return label
    return mode.value


def compose_insert(raw: str, final: str, include_raw: bool) -> str:
    """Text pasted into the document; always ends with a blank line so dictations stack."""
    if include_raw:
        raw_part = strip_markers(raw).strip()
        body = f"原文：{raw_part}\n摘要：{final.strip()}"
    else:
        body = final.strip()
    return body + "\n\n"


@dataclass
class HistoryItem:
    mode: Mode
    raw_text: str
    final_text: str
    fallback: bool
    stamp: str
    images: list = field(default_factory=list)
    total_blocks: int = 1
    fallback_blocks: int = 0


@dataclass
class ViewModel:
    phase: str = "idle"                     # idle | recording | processing
    mode: Mode = Mode.CLEAN                 # dropdown selection (next recording)
    recording_mode: Optional[Mode] = None
    record_started: Optional[float] = None  # time.monotonic()
    processing_started: Optional[float] = None
    loading_model: bool = False
    raw_text: str = ""
    final_text: str = ""
    result_mode: Mode = Mode.CLEAN
    fallback: bool = False
    message: str = ""
    history: List[HistoryItem] = field(default_factory=list)
    selected: Optional[int] = None          # index into history
    images: list = field(default_factory=list)
    image_count: int = 0
    pending_jobs: int = 0
    total_blocks: int = 1
    fallback_blocks: int = 0
    stt_profile: str = ""
    llm_profile: str = ""
    device_note: str = ""
    stt_sec: Optional[float] = None
    llm_sec: Optional[float] = None
    auto_paste: bool = True
    include_raw: bool = False
    hotkeys_ok: bool = True
    llm_status: str = "檢查中…"
    hotkey_hints: Dict[str, str] = field(default_factory=dict)

    @property
    def result_label(self) -> str:
        return RESULT_LABELS.get(self.result_mode, "結果")

    @property
    def notice(self) -> str:
        if not self.fallback:
            return ""
        if 0 < self.fallback_blocks < self.total_blocks:
            return f"部分段落 LLM 失敗（{self.fallback_blocks}/{self.total_blocks}），該段顯示原文"
        return FALLBACK_NOTICE


def _run_in_thread(fn: Callable[[], None]) -> None:
    threading.Thread(target=fn, daemon=True).start()


class PanelController:
    def __init__(
        self,
        *,
        submit: Callable[[Event], None],
        paster: Any,                       # OutputAdapter that copies + pastes
        copier: Callable[[str], None],     # copy-only
        stt_profile: str = "",
        llm_profile: str = "",
        device_getter: Optional[Callable[[], str]] = None,
        llm_pinger: Optional[Callable[[], List[str]]] = None,
        llm_model: str = "",
        api_key_env: str = "",
        abandon: Optional[Callable[[], bool]] = None,
        retry: Optional[Callable[[Mode], bool]] = None,
        cache_dir: str = "records/cache",
        run_async: Callable[[Callable[[], None]], None] = _run_in_thread,
        clock: Callable[[], float] = time.monotonic,
        streaming: bool = False,
        hotkey_hints: Optional[Dict[str, str]] = None,
    ) -> None:
        self.streaming = streaming
        self._submit = submit
        self._paster = paster
        self._copier = copier
        self._device_getter = device_getter
        self._llm_pinger = llm_pinger
        self._llm_model = llm_model
        self._api_key_env = api_key_env
        self._ping_lock = threading.Lock()
        self._abandon = abandon
        self._retry = retry
        self._cache_dir = cache_dir
        self._run_async = run_async
        self._clock = clock
        self._lock = threading.Lock()
        self._dirty = True
        self._model_loaded = False
        self.vm = ViewModel(stt_profile=stt_profile, llm_profile=llm_profile)
        if hotkey_hints:
            self.vm.hotkey_hints = dict(hotkey_hints)

    # ----------------------------------------------------------- LLM health
    def ping_llm(self) -> None:
        """Check the LLM off the UI thread; the outcome lands in vm.llm_status."""
        if self._llm_pinger is None:
            return
        with self._lock:
            self.vm.llm_status = "檢查中…"
            self._touch()
        self._run_async(self._do_ping)

    def _do_ping(self) -> None:
        if not self._ping_lock.acquire(blocking=False):
            return  # a check is already running
        try:
            try:
                models = self._llm_pinger() or []
                name = self._llm_model or (models[0] if models else "")
                if models and self._llm_model and self._llm_model not in models:
                    status = f"已連線，但伺服器沒有模型 {self._llm_model}"
                else:
                    status = f"已連線 ({name})" if name else "已連線"
            except Exception as e:
                logger.warning("LLM ping failed: %s: %s", type(e).__name__, e)
                status = f"未連線 — {describe_llm_error(e, self._api_key_env)}"
        finally:
            self._ping_lock.release()
        with self._lock:
            self.vm.llm_status = status
            self._touch()

    # ------------------------------------------------------------ plumbing
    def _touch(self) -> None:
        self._dirty = True

    def poll(self) -> Optional[ViewModel]:
        """Tk thread: returns a snapshot if anything changed since the last poll."""
        with self._lock:
            if not self._dirty:
                return None
            self._dirty = False
            return copy.deepcopy(self.vm)

    # -------------------------------------------- daemon callbacks (any thread)
    def on_image(self, count: int) -> None:
        """Daemon thread: update captured image counter."""
        with self._lock:
            self.vm.image_count = count
            self.vm.message = f"已擷取圖片 {count}"
            self._touch()

    def on_paste(self, added: int) -> None:
        """Daemon callback: number of images added by paste_image action."""
        with self._lock:
            if added == 0:
                self.vm.message = "剪貼簿沒有圖片（先截圖或複製圖片，再按貼上）"
                self._touch()

    def _translate_note(self, note: str) -> str:
        if note == "abandoned":
            return (f"已放棄；錄音保存在 {self._cache_dir}/last_recording.wav，"
                    "可按「重試上一段」")
        if note == "queued":
            return "已送出，背景處理中…"
        if note.startswith("error: ModelNotAvailableError: "):
            return note[len("error: ModelNotAvailableError: "):]
        if note.startswith("refused"):
            return "上一段仍在背景處理，請稍候再試"
        if note.startswith("done"):
            return "完成"
        if note == "recording cancelled":
            return "已取消錄音"
        if note == "no speech detected":
            return "沒有偵測到語音"
        return note

    def on_state(self, state: State, mode: Optional[Mode], note: str) -> None:
        with self._lock:
            vm = self.vm
            if state is State.RECORDING:
                vm.phase = "recording"
                vm.recording_mode = mode
                vm.record_started = self._clock()
                vm.image_count = 0
                if self.streaming:
                    vm.raw_text = ""
                    vm.final_text = ""
                vm.message = "錄音中…"
            elif state is State.PROCESSING:
                vm.phase = "processing"
                vm.record_started = None
                vm.processing_started = self._clock()
                vm.loading_model = not self._model_loaded
                vm.raw_text = ""          # filled live by on_segment
                vm.final_text = ""
                vm.fallback = False
                vm.message = "載入模型…" if vm.loading_model else "分析中…"
            else:
                was_processing = vm.phase == "processing"
                vm.phase = "idle"
                vm.record_started = None
                vm.processing_started = None
                vm.recording_mode = None
                vm.loading_model = False
                if was_processing and note != "abandoned":
                    self._model_loaded = True
                if note == "abandoned":
                    self._restore_selected()
                if note.startswith("done"):
                    # keep an insert-failure warning raised by on_result
                    if not vm.message.startswith("插入失敗"):
                        vm.message = "完成"
                else:
                    vm.message = self._translate_note(note)
            self._touch()

    def on_segment(self, text: str) -> None:
        """Worker thread: live partial transcript while the STT decodes."""
        with self._lock:
            if self.streaming:
                if self.vm.phase != "recording" and self.vm.pending_jobs <= 0:
                    return
            else:
                if self.vm.phase != "processing":
                    return
            self.vm.raw_text = text
            if self.vm.loading_model:     # first segment => the model is loaded
                self.vm.loading_model = False
                self._model_loaded = True
            self._touch()

    def on_jobs(self, pending: int, note: str) -> None:
        """Sessions callback: update pending job count and status message."""
        with self._lock:
            self.vm.pending_jobs = pending
            if note:
                if note == "abandoned":
                    self._restore_selected()
                if not (note.startswith("done") and self.vm.message.startswith("插入失敗")):
                    self.vm.message = self._translate_note(note)
            elif pending > 0:
                self.vm.message = f"背景處理中（{pending}）"
            else:
                if not self.vm.message.startswith("插入失敗"):
                    self.vm.message = "完成"
            self._touch()

    def _restore_selected(self) -> None:
        vm = self.vm
        if vm.selected is not None and 0 <= vm.selected < len(vm.history):
            self._show(vm.history[vm.selected])
        else:
            vm.raw_text = vm.final_text = ""

    def on_result(self, result: Any) -> None:
        """Worker thread: record the result, then insert it into the document."""
        self._model_loaded = True
        if not (result.final_text or "").strip():
            return  # "no speech detected": on_state shows the note
        device = ""
        if self._device_getter is not None:
            try:
                device = self._device_getter() or ""
            except Exception:
                device = ""
        images = list(getattr(result, "images", None) or [])
        item = HistoryItem(
            mode=result.mode,
            raw_text=result.raw_text,
            final_text=result.final_text,
            fallback=bool(result.fallback_triggered),
            stamp=time.strftime("%H:%M:%S"),
            images=images,
            total_blocks=getattr(result, "total_blocks", 1) or 1,
            fallback_blocks=getattr(result, "fallback_blocks", 0) or 0,
        )
        with self._lock:
            vm = self.vm
            vm.history.insert(0, item)
            del vm.history[HISTORY_MAX:]
            vm.selected = 0
            self._show(item)
            vm.stt_sec = result.stt_elapsed_sec
            vm.llm_sec = result.llm_elapsed_sec
            vm.device_note = "CUDA 失敗，已改用 CPU" if "fallback" in device else ""
            auto, include_raw = vm.auto_paste, vm.include_raw
            self._touch()
        if item.fallback:
            self.ping_llm()  # reflect the outage (or recovery) in the status bar
        if auto:
            self._insert_text(compose_insert(item.raw_text, item.final_text, include_raw), images=item.images)

    def _show(self, item: HistoryItem) -> None:
        vm = self.vm
        vm.raw_text, vm.final_text = item.raw_text, item.final_text
        vm.result_mode, vm.fallback = item.mode, item.fallback
        vm.images = list(item.images)
        vm.total_blocks, vm.fallback_blocks = item.total_blocks, item.fallback_blocks

    # ------------------------------------------------ user actions (Tk thread)
    def set_mode(self, mode: Mode) -> None:
        with self._lock:
            self.vm.mode = mode
            self._touch()

    def set_auto_paste(self, value: bool) -> None:
        with self._lock:
            self.vm.auto_paste = bool(value)
            self._touch()

    def set_include_raw(self, value: bool) -> None:
        with self._lock:
            self.vm.include_raw = bool(value)
            self._touch()

    def set_hotkeys_ok(self, ok: bool) -> None:
        with self._lock:
            self.vm.hotkeys_ok = ok
            self._touch()

    def record_clicked(self) -> None:
        """Same event as the hotkey: press starts, press again stops."""
        with self._lock:
            phase, mode, rec_mode = self.vm.phase, self.vm.mode, self.vm.recording_mode
        if phase == "processing":
            return
        self._submit(Event("press", rec_mode if phase == "recording" and rec_mode else mode))

    def cancel_clicked(self) -> None:
        self._submit(Event("cancel"))

    def paste_image_clicked(self) -> None:
        """User clicked 'Paste Image' button in UI."""
        with self._lock:
            phase = self.vm.phase
        if phase == "recording":
            self._submit(Event("paste_image"))

    def abandon_clicked(self) -> None:
        if self._abandon is not None:
            self._abandon()

    def retry_clicked(self) -> None:
        """Re-run the last recording with the currently selected mode (idle only)."""
        with self._lock:
            if self.vm.phase != "idle":
                return
            mode = self.vm.mode
        if self._retry is not None:
            self._retry(mode)

    def select_history(self, index: int) -> None:
        with self._lock:
            if 0 <= index < len(self.vm.history):
                self.vm.selected = index
                self._show(self.vm.history[index])
                self._touch()

    def copy_text(self, which: str) -> None:
        text = self._text_for(which)
        if not text:
            return
        try:
            self._copier(text)
            self._flash("已複製")
        except Exception as e:
            logger.warning("copy failed: %s", e)
            self._flash(f"複製失敗：{e}")

    def insert_clicked(self, which: str) -> None:
        text = self._text_for(which)
        with self._lock:
            images = list(self.vm.images)
        if text:
            self._run_async(lambda: self._insert_text(text.strip() + "\n\n", images=images))

    # ------------------------------------------------------------- helpers
    def _text_for(self, which: str) -> str:
        with self._lock:
            return self.vm.raw_text if which == "raw" else self.vm.final_text

    def notify(self, msg: str) -> None:
        """Show a message in the panel (thread-safe)."""
        self._flash(msg)

    def _flash(self, msg: str) -> None:
        with self._lock:
            self.vm.message = msg
            self._touch()

    def _insert_text(self, text: str, images: Sequence[Any] = ()) -> None:
        try:
            if images and hasattr(self._paster, "emit_rich"):
                self._paster.emit_rich(text, list(images))
            else:
                self._paster.emit(text, Mode.CLEAN)
        except Exception as e:
            logger.warning("insert failed: %s: %s", type(e).__name__, e)
            self._flash(f"插入失敗（文字仍在記錄中，可按「插入」重試）：{e}")
