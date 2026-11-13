"""Glue between the worker threads and the draft window.

Worker threads (STT/LLM workers, the hotkey daemon) only put items on a queue; the Tk
thread drains it in `tick()`, so the draft window is never touched from another thread.
"""
from __future__ import annotations

import logging
import os
import queue
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, List, Optional, Sequence

from local_typeless.types import DraftPiece, Mode

logger = logging.getLogger(__name__)


class PreviousWindow:
    """Remembers the last foreground window that belongs to another process."""

    def __init__(self) -> None:
        self._hwnd = 0
        self._user32: Any = None
        if sys.platform == "win32":
            try:
                import ctypes
                from ctypes import wintypes

                u = ctypes.WinDLL("user32", use_last_error=True)
                u.GetForegroundWindow.argtypes = []
                u.GetForegroundWindow.restype = ctypes.c_void_p
                u.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
                u.GetWindowThreadProcessId.restype = wintypes.DWORD
                u.SetForegroundWindow.argtypes = [ctypes.c_void_p]
                u.SetForegroundWindow.restype = wintypes.BOOL
                u.IsWindow.argtypes = [ctypes.c_void_p]
                u.IsWindow.restype = wintypes.BOOL
                self._user32 = u
            except Exception as e:
                logger.debug("PreviousWindow unavailable: %s", e)

    def track(self) -> None:
        u = self._user32
        if u is None:
            return
        try:
            import ctypes
            from ctypes import wintypes

            hwnd = u.GetForegroundWindow()
            if not hwnd:
                return
            pid = wintypes.DWORD(0)
            u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value and pid.value != os.getpid():
                self._hwnd = hwnd
        except Exception as e:
            logger.debug("PreviousWindow.track failed: %s", e)

    def activate(self) -> bool:
        u = self._user32
        if u is None or not self._hwnd:
            return False
        try:
            if not u.IsWindow(self._hwnd):
                return False
            return bool(u.SetForegroundWindow(self._hwnd))
        except Exception as e:
            logger.debug("PreviousWindow.activate failed: %s", e)
            return False


class DraftBridge:
    def __init__(
        self,
        pipeline: Any,
        *,
        images_dir: Path,
        cache_dir: Path,
        autosave: bool = True,
        copy_text: Optional[Callable[[str], None]] = None,
        simulate_paste: Optional[Callable[[], None]] = None,
        run_async: Optional[Callable[[Callable[[], None]], None]] = None,
        previous: Optional[Any] = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._pipeline = pipeline
        self._images_dir = Path(images_dir)
        self._cache_dir = Path(cache_dir)
        self._autosave = autosave
        self._copy_text = copy_text
        self._simulate_paste = simulate_paste
        self._run_async = run_async or (
            lambda fn: threading.Thread(target=fn, daemon=True, name="draft-bg").start())
        self._previous = previous if previous is not None else PreviousWindow()
        self._sleep = sleep
        self._q: "queue.Queue[Any]" = queue.Queue()
        self.window: Any = None
        self.toggle_record: Callable[[], None] = lambda: None   # set by the app
        self.cancel_record: Optional[Callable[[], None]] = None

    # ------------------------------------------------ any thread: just enqueue
    def on_piece(self, piece: DraftPiece) -> None:
        self._q.put(("piece", piece))

    def on_paste_request(self) -> None:
        self._q.put(("paste", None))

    def _on_tk(self, fn: Callable[[], None]) -> None:
        self._q.put(("call", fn))

    # ------------------------------------------------------------ Tk thread
    def open(self, master: Any, *, on_close: Optional[Callable[[], None]] = None,
             menu_items: Sequence[Any] = ()) -> Any:
        from local_typeless.ui.draft_model import DraftModel
        from local_typeless.ui.draft_window import DraftActions, DraftWindow

        def model_factory(buffer: Any) -> Any:
            return DraftModel(buffer, images_dir=self._images_dir, cache_dir=self._cache_dir,
                              autosave=self._autosave)

        actions = DraftActions(
            toggle_record=lambda: self.toggle_record(),
            refine=self.refine,
            save_draft=self.save_draft,
            paste_to_previous=self.paste_to_previous,
        )
        self.window = DraftWindow(
            master, model_factory, actions=actions, on_close=on_close, menu_items=menu_items,
            cancel_record=(lambda: self.cancel_record()) if self.cancel_record else None,
        )
        return self.window

    def show(self) -> None:
        if self.window is not None:
            self.window.lift()

    def insert_files(self, paths: Sequence[str]) -> None:
        if self.window is not None:
            self.window.model.insert_files(list(paths))
            self.window.top.deiconify()

    def tick(self, recording: bool, *, elapsed_sec: int = 0, status: str = "",
             message: str = "", record_hint: str = "") -> None:
        """Drain queued work and mirror the app state. Tk thread only."""
        self._previous.track()
        win = self.window
        if win is None:
            return
        if recording and not getattr(win, "_recording", False):
            try:
                win.top.deiconify()      # make the draft visible; focus stays where it is
            except Exception:
                pass
        win.set_recording(recording, elapsed_sec)
        win.set_record_hint(record_hint)
        win.set_app_status(status, message)
        while True:
            try:
                kind, payload = self._q.get_nowait()
            except queue.Empty:
                break
            try:
                if kind == "piece":
                    win.add_piece(payload)
                elif kind == "paste":
                    win.paste_from_clipboard()
                elif kind == "call":
                    payload()
            except Exception as e:
                logger.warning("draft %s failed: %s: %s", kind, type(e).__name__, e)

    def close(self) -> None:
        if self.window is not None:
            try:
                self.window.model.save()
            except Exception:
                pass
            self.window.close()
            self.window = None

    # ------------------------------------------------------------- actions
    def refine(self, text: str, mode: Mode, done: Callable[[Optional[str], str], None]) -> None:
        def work() -> None:
            try:
                res = self._pipeline.refine_only(text, mode)
                if res.fallback_triggered:
                    out, err = None, (res.error_message or "LLM 沒有回應")
                else:
                    out, err = res.final_text, ""
            except Exception as e:
                out, err = None, f"{type(e).__name__}: {e}"
            self._on_tk(lambda: done(out, err))

        self._run_async(work)

    def save_draft(self, final_text: str, raw_text: str) -> None:
        self._run_async(lambda: self._pipeline.save_draft(final_text, raw_text))

    def paste_to_previous(self, text: str) -> bool:
        """Copy the draft and paste it into the window that was in front before ours."""
        if not self._previous.activate():
            return False
        copy_text, paste = self._copy_text, self._simulate_paste
        if copy_text is None or paste is None:
            from local_typeless.output.clipboard_out import _simulate_paste, copy_with_retry

            copy_text = copy_text or copy_with_retry
            paste = paste or _simulate_paste

        def work() -> None:
            try:
                self._sleep(0.25)        # let the other window take the focus first
                copy_text(text)
                paste()
            except Exception as e:
                logger.warning("paste_to_previous failed: %s: %s", type(e).__name__, e)

        self._run_async(work)
        return True
