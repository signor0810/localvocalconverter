"""Tkinter side panel. Thin: all logic lives in ui.controller.PanelController.

Threading: this module only runs on the Tk thread. Worker/hook threads talk to the
controller, and `_tick` polls it with root.after - Tk is never touched from other threads.
"""
from __future__ import annotations

import sys
import time
import tkinter as tk
from tkinter import ttk
from typing import Any, Callable, Optional, Tuple

from local_typeless.ui.controller import MODE_CHOICES, PanelController, ViewModel, mode_label

FONT = "Microsoft JhengHei UI"
PANEL_WIDTH_PX = 360          # logical px; scaled by DPI
TICK_MS = 200                 # poll rate; idle ticks are a cheap flag check
TIMER_RENDER_SEC = 0.5        # timer/pulse refresh (2 Hz)

GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
SWP_FLAGS = 0x0001 | 0x0002 | 0x0004 | 0x0010 | 0x0020  # NOSIZE|NOMOVE|NOZORDER|NOACTIVATE|FRAMECHANGED


# ----------------------------------------------------------------- win32 glue
def enable_dpi_awareness() -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def make_no_activate(root: tk.Tk) -> bool:
    """WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW so clicks never take focus from the document."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        user32.GetParent.argtypes = [wintypes.HWND]
        user32.GetParent.restype = wintypes.HWND
        get = user32.GetWindowLongPtrW
        get.argtypes = [wintypes.HWND, ctypes.c_int]
        get.restype = ctypes.c_ssize_t
        setl = user32.SetWindowLongPtrW
        setl.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
        setl.restype = ctypes.c_ssize_t
        user32.SetWindowPos.argtypes = [
            wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, ctypes.c_int, wintypes.UINT,
        ]
        root.update_idletasks()
        hwnd = user32.GetParent(root.winfo_id()) or root.winfo_id()
        style = get(hwnd, GWL_EXSTYLE)
        setl(hwnd, GWL_EXSTYLE, style | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)
        user32.SetWindowPos(hwnd, None, 0, 0, 0, 0, SWP_FLAGS)
        return True
    except Exception:
        return False


def work_area(root: tk.Tk) -> Tuple[int, int, int, int]:
    """(left, top, right, bottom) of the primary monitor's work area (excl. taskbar)."""
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            rect = wintypes.RECT()
            if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):
                return rect.left, rect.top, rect.right, rect.bottom
        except Exception:
            pass
    return 0, 0, root.winfo_screenwidth(), root.winfo_screenheight()


_MISSING = object()


class WidgetCache:
    """Apply widget options only when they differ from the last applied values, so an
    unchanged UI costs no Tk work (reconfiguring every widget every tick burned CPU)."""

    def __init__(self) -> None:
        self._last: dict = {}

    def configure(self, widget: Any, **opts: Any) -> bool:
        cur = self._last.setdefault(id(widget), {})
        changed = {k: v for k, v in opts.items() if cur.get(k, _MISSING) != v}
        if not changed:
            return False
        widget.configure(**changed)
        cur.update(changed)
        return True


# ------------------------------------------------------------------------ app
class PanelApp:
    def __init__(
        self,
        controller: PanelController,
        *,
        on_close: Optional[Callable[[], None]] = None,
        teach_factory: Optional[Callable[[], Any]] = None,
        review_factory: Optional[Callable[[], Any]] = None,
        draft_bridge: Any = None,
    ) -> None:
        enable_dpi_awareness()
        self.ctl = controller
        self._on_close = on_close
        self._teach_factory = teach_factory      # -> TeachController; None disables teach mode
        self._teach_win: Any = None
        self._review_factory = review_factory    # -> ReviewController; None disables review mode
        self._review_win: Any = None
        self._draft = draft_bridge               # DraftBridge; None = classic paste-when-done flow
        self.root = tk.Tk()
        self.root.title("Local Typeless")
        self.root.attributes("-topmost", True)
        self.root.option_add("*Font", (FONT, 10))
        style = ttk.Style(self.root)
        style.configure(".", font=(FONT, 10))
        self._cache: dict = {}
        self._wc = WidgetCache()
        self._last_vm: Optional[ViewModel] = None
        self._last_timed = 0.0
        self._build()
        self._dock()
        self.no_activate = make_no_activate(self.root)
        from local_typeless.ui.file_drop import enable_file_drop

        self._file_drop = enable_file_drop(self.root)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self._bind_teach()
        if self._draft is not None:
            # Single-window mode: the draft editor is the app; the side panel stays hidden
            # and only keeps driving the controller.
            items = []
            if self._review_factory is not None:
                items.append(("檢查口述紀錄…", self.open_review))
            if self._teach_factory is not None:
                items.append(("教學模式…", self.open_teach))
            self._draft.cancel_record = self.ctl.cancel_clicked
            self._draft.open(self.root, on_close=self.close, menu_items=items)
            self.root.withdraw()
        self.root.after(TICK_MS, self._tick)

    # ------------------------------------------------------------- layout
    def _dock(self) -> None:
        self.root.update_idletasks()
        scale = max(1.0, self.root.winfo_fpixels("1i") / 96.0)
        width = int(PANEL_WIDTH_PX * scale)
        left, top, right, bottom = work_area(self.root)
        self.root.geometry(f"{width}x{bottom - top}+{right - width}+{top}")
        self.root.minsize(int(280 * scale), 400)

    def _build(self) -> None:
        r = self.root
        pad = {"padx": 8, "pady": 3}

        # top: mode + checkboxes
        top = ttk.Frame(r)
        top.pack(fill="x", **pad)
        ttk.Label(top, text="模式").pack(side="left")
        self.mode_var = tk.StringVar(value=MODE_CHOICES[0][0])
        self.mode_box = ttk.Combobox(
            top, textvariable=self.mode_var, state="readonly", width=8,
            values=[label for label, _ in MODE_CHOICES],
        )
        self.mode_box.pack(side="left", padx=6)
        self.mode_box.bind("<<ComboboxSelected>>", self._mode_changed)

        opts = ttk.Frame(r)
        opts.pack(fill="x", **pad)
        self.auto_var = tk.BooleanVar(value=True)
        self.raw_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="自動貼到文件", variable=self.auto_var,
                        command=lambda: self.ctl.set_auto_paste(self.auto_var.get())).pack(anchor="w")
        ttk.Checkbutton(opts, text="插入原文＋結果", variable=self.raw_var,
                        command=lambda: self.ctl.set_include_raw(self.raw_var.get())).pack(anchor="w")

        # record + cancel
        self.rec_btn = tk.Button(r, text="🎙  開始錄音", font=(FONT, 16, "bold"), height=2,
                                 command=self.ctl.record_clicked, relief="raised")
        self.rec_btn.pack(fill="x", padx=8, pady=6)
        self.ctl_row = ttk.Frame(r)
        self.ctl_row.pack(fill="x", padx=8)
        self.cancel_btn = ttk.Button(self.ctl_row, text="取消 (Esc)", command=self.ctl.cancel_clicked)
        self.cancel_btn.pack(side="left", expand=True, fill="x")
        self.paste_image_btn = ttk.Button(self.ctl_row, text="貼上附件", command=self.ctl.paste_image_clicked)
        self.paste_image_btn.pack(side="left", expand=True, fill="x", padx=(4, 0))
        self.abandon_btn = ttk.Button(self.ctl_row, text="放棄", command=self.ctl.abandon_clicked)
        self.abandon_btn.pack(side="left", expand=True, fill="x", padx=4)
        self.retry_btn = ttk.Button(self.ctl_row, text="重試上一段", command=self.ctl.retry_clicked)
        self.retry_btn.pack(side="left", expand=True, fill="x")
        self.progress = ttk.Progressbar(r, mode="indeterminate")
        self.notice = tk.Label(r, text="", bg="#fff3b0", fg="#5a4a00", anchor="w")

        # text areas
        self.raw_text, self.raw_copy, self.raw_ins, self.raw_title = self._text_block(
            "原文", "raw")
        self.res_text, self.res_copy, self.res_ins, self.res_title = self._text_block(
            "摘要結果", "final")

        # history
        ttk.Label(r, text="歷史（最近 10 筆）").pack(anchor="w", padx=8)
        self.hist = tk.Listbox(r, height=5, exportselection=False, activestyle="none")
        self.hist.pack(fill="x", padx=8, pady=3)
        self.hist.bind("<<ListboxSelect>>", self._history_selected)

        # status
        self.msg_label = ttk.Label(r, text="", anchor="w")
        self.msg_label.pack(side="bottom", fill="x", padx=8)
        self.status = ttk.Label(r, text="", anchor="w", relief="sunken")
        self.status.pack(side="bottom", fill="x")

    def _text_block(self, title: str, which: str):
        frame = ttk.Frame(self.root)
        frame.pack(fill="both", expand=True, padx=8, pady=3)
        head = ttk.Frame(frame)
        head.pack(fill="x")
        title_lbl = ttk.Label(head, text=title)
        title_lbl.pack(side="left")
        ins = ttk.Button(head, text="插入", width=5,
                         command=lambda: self.ctl.insert_clicked(which))
        ins.pack(side="right")
        copy_btn = ttk.Button(head, text="複製", width=5,
                              command=lambda: self.ctl.copy_text(which))
        copy_btn.pack(side="right", padx=4)
        text = tk.Text(frame, height=6, wrap="word", state="disabled", font=(FONT, 10))
        text.pack(fill="both", expand=True)
        return text, copy_btn, ins, title_lbl

    # ------------------------------------------------- hidden teach mode & review
    def _bind_teach(self) -> None:
        if self._teach_factory is None and self._review_factory is None and self._draft is None:
            return
        if self._teach_factory is not None:
            for seq in ("<Control-Shift-T>", "<Control-Shift-t>"):
                self.root.bind(seq, lambda _e: self.open_teach())
        menu = tk.Menu(self.root, tearoff=0)
        if self._draft is not None:
            menu.add_command(label="草稿視窗", command=self._draft.show)
        if self._teach_factory is not None:
            menu.add_command(label="教學模式…", command=self.open_teach)
        if self._review_factory is not None:
            menu.add_command(label="檢查口述紀錄…", command=self.open_review)

        def popup(e: Any) -> None:
            menu.tk_popup(e.x_root, e.y_root)

        for w in (self.status, self.msg_label):
            w.bind("<Button-3>", popup)

    def _selection(self):
        vm = self._last_vm
        if vm is not None and vm.selected is not None and 0 <= vm.selected < len(vm.history):
            h = vm.history[vm.selected]
            return h.raw_text, h.final_text, h.mode
        return "", "", vm.mode if vm is not None else None

    def open_teach(self) -> None:
        if self._teach_factory is None:
            return
        if self._teach_win is not None and self._teach_win._alive:
            self._teach_win.refresh_selection()
            self._teach_win.lift()
            return
        from local_typeless.ui.teach_window import TeachWindow

        self._teach_win = TeachWindow(self.root, self._teach_factory(), self._selection)
        self._teach_win.lift()

    def open_review(self) -> None:
        if self._review_factory is None:
            return
        if self._review_win is not None and self._review_win._alive:
            self._review_win.lift()
            return
        from local_typeless.ui.review_window import ReviewWindow

        self._review_win = ReviewWindow(self.root, self._review_factory())
        self._review_win.lift()

    # ------------------------------------------------------------- events
    def _mode_changed(self, _evt: Any = None) -> None:
        label = self.mode_var.get()
        for lbl, mode in MODE_CHOICES:
            if lbl == label:
                self.ctl.set_mode(mode)

    def _history_selected(self, _evt: Any = None) -> None:
        sel = self.hist.curselection()
        if sel:
            self.ctl.select_history(sel[0])

    # ------------------------------------------------------------- render
    @staticmethod
    def _set_text(widget: tk.Text, value: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", value)
        widget.configure(state="disabled")

    def _cached(self, key: str, value: Any) -> bool:
        """True if unchanged; otherwise remembers the new value and returns False."""
        if self._cache.get(key) == value:
            return True
        self._cache[key] = value
        return False

    def render(self, vm: ViewModel) -> None:
        """Idempotent: touches only widgets whose value changed (see WidgetCache)."""
        wc = self._wc
        busy = vm.phase == "processing"
        now = time.monotonic()
        if vm.phase == "recording":
            elapsed = int(now - (vm.record_started or now))
            pulse_on = int(now * 2) % 2 == 0
            img_suffix = f"  附件×{vm.image_count}" if getattr(vm, "image_count", 0) > 0 else ""
            wc.configure(
                self.rec_btn, text=f"●  錄音中  {elapsed} 秒（再按一次結束）{img_suffix}",
                fg="#e60000" if pulse_on else "#7a0000", bg="#ffe9e9", state="normal")
        elif busy:
            elapsed = int(now - (vm.processing_started or now))
            label = "載入模型…" if vm.loading_model else "分析中…"
            wc.configure(self.rec_btn, text=f"{label} {elapsed} 秒",
                         fg="#333333", bg="#f0f0f0", state="disabled")
        else:
            mode_hint = vm.hotkey_hints.get(vm.mode.value) if getattr(vm, "hotkey_hints", None) else None
            rec_text = f"🎙  開始錄音 ({mode_hint})" if mode_hint else "🎙  開始錄音"
            wc.configure(self.rec_btn, text=rec_text, fg="#000000", bg="#f0f0f0",
                         state="normal")
        cancel_hint = vm.hotkey_hints.get("cancel") if getattr(vm, "hotkey_hints", None) else None
        cancel_text = f"取消 ({cancel_hint})" if cancel_hint else "取消 (Esc)"
        wc.configure(self.cancel_btn, text=cancel_text, state="normal" if vm.phase == "recording" else "disabled")
        paste_hint = vm.hotkey_hints.get("paste_image") if getattr(vm, "hotkey_hints", None) else None
        paste_text = f"貼上附件 ({paste_hint})" if paste_hint else "貼上附件"
        wc.configure(self.paste_image_btn, text=paste_text, state="normal" if vm.phase == "recording" else "disabled")
        wc.configure(self.abandon_btn, state="normal" if (busy or vm.pending_jobs > 0) else "disabled")
        wc.configure(self.retry_btn, state="normal" if vm.phase == "idle" else "disabled")
        wc.configure(self.mode_box, state="disabled" if busy else "readonly")
        for b in (self.raw_ins, self.res_ins):
            wc.configure(b, state="disabled" if busy else "normal")

        progress_active = busy or vm.pending_jobs > 0
        if not self._cached("progress_active", progress_active):
            if progress_active:
                self.progress.pack(fill="x", padx=8, pady=3, after=self.ctl_row)
                self.progress.start(100)
            else:
                self.progress.stop()
                self.progress.pack_forget()

        if not self._cached("notice", vm.notice):
            if vm.notice:
                self.notice.configure(text=vm.notice)
                self.notice.pack(fill="x", padx=8, pady=3, after=self.ctl_row)
            else:
                self.notice.pack_forget()

        if not self._cached("raw", vm.raw_text):
            self._set_text(self.raw_text, vm.raw_text)
            self.raw_text.see("end")        # follow the live transcript
        if not self._cached("final", vm.final_text):
            self._set_text(self.res_text, vm.final_text)
        wc.configure(self.res_title, text=vm.result_label)

        items = [(h.stamp, h.final_text) for h in vm.history]
        if not self._cached("hist", items):
            self.hist.delete(0, "end")
            for h in vm.history:
                snippet = h.final_text.replace("\n", " ")[:22]
                self.hist.insert("end", f"{h.stamp} [{mode_label(h.mode)}] {snippet}")
        if vm.selected is not None and not self._cached("sel", vm.selected):
            self.hist.selection_clear(0, "end")
            self.hist.selection_set(vm.selected)

        times = ""
        if vm.stt_sec is not None:
            times = f"  |  STT {vm.stt_sec:.1f}s / LLM {(vm.llm_sec or 0):.1f}s"
        note = f"  |  {vm.device_note}" if vm.device_note else ""
        pending_note = f"  |  處理中 {vm.pending_jobs}" if vm.pending_jobs > 0 else ""
        wc.configure(
            self.status,
            text=f" STT: {vm.stt_profile}  LLM: {vm.llm_status or vm.llm_profile}{times}{note}{pending_note}")
        msg = vm.message
        if not vm.hotkeys_ok:
            msg = (msg + "  " if msg else "") + "（全域快捷鍵無法使用）"
        wc.configure(self.msg_label, text=msg,
                     wraplength=max(200, self.root.winfo_width() - 24))

    def _tick(self) -> None:
        try:
            if self._file_drop is not None:
                for paths in self._file_drop.poll():
                    if self._draft is not None:
                        self._draft.insert_files(paths)
                    else:
                        self.ctl.files_dropped(paths)
            vm = self.ctl.poll()
            now = time.monotonic()
            if vm is not None:
                self._last_vm = vm
                self.render(vm)
                self._last_timed = now
            elif (self._last_vm is not None
                  and self._last_vm.phase in ("recording", "processing")
                  and now - self._last_timed >= TIMER_RENDER_SEC):
                self.render(self._last_vm)     # drives the timer + pulse at 2 Hz
                self._last_timed = now
            if self._draft is not None:
                self._tick_draft()
        finally:
            self.root.after(TICK_MS, self._tick)

    def _tick_draft(self) -> None:
        vm = self._last_vm
        if vm is None:
            self._draft.tick(False)
            return
        recording = vm.phase == "recording"
        elapsed = int(time.monotonic() - (vm.record_started or time.monotonic())) if recording else 0
        status = f"STT: {vm.stt_profile}   LLM: {vm.llm_status or vm.llm_profile}"
        if vm.pending_jobs > 0:
            status += f"   整理中 {vm.pending_jobs}"
        message = vm.notice or vm.message
        if not vm.hotkeys_ok:
            message = (message + "  " if message else "") + "（全域快捷鍵無法使用）"
        hint = vm.hotkey_hints.get(vm.mode.value, "") if getattr(vm, "hotkey_hints", None) else ""
        self._draft.tick(recording, elapsed_sec=elapsed, status=status, message=message,
                         record_hint=hint)

    # ------------------------------------------------------------ lifecycle
    def close(self) -> None:
        if self._on_close is not None:
            try:
                self._on_close()
            except Exception:
                pass
        if self._file_drop is not None:
            self._file_drop.remove()
        if self._draft is not None:
            self._draft.close()
        self.root.destroy()

    def run(self, auto_close_sec: Optional[float] = None) -> None:
        if auto_close_sec:
            self.root.after(int(auto_close_sec * 1000), self.close)
        self.root.mainloop()
