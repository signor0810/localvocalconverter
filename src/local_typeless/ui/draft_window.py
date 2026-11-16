"""Draft window (Tk Toplevel) for dictation, editing, and references.

Integrates DraftModel with Tk Text widget, FileDrop, and clipboard actions.
"""
from __future__ import annotations

import json
import logging
import re
import time
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from tkinter import ttk

from local_typeless.capture.clipboard_images import Win32ClipboardBackend
from local_typeless.output.clipboard_out import copy_with_retry
from local_typeless.types import DraftPiece, Mode
from local_typeless.ui import theme
from local_typeless.ui.draft_model import DraftModel, TextBuffer
from local_typeless.ui.file_drop import enable_file_drop

logger = logging.getLogger(__name__)

FONT_FAMILY = "Microsoft JhengHei UI"
TICK_MS = 150
LINE_GUTTER_PX = 34
TABS_FILE = "draft_tabs.json"
RESULT_TITLE_EMPTY = "整理結果（按「重新整理」後出現）"


@dataclass
class DraftActions:
    """Everything the draft window needs from the application."""

    toggle_record: Callable[[], None]
    refine: Callable[[str, Mode, Callable[[Optional[str], str], None]], None]
    save_draft: Callable[[str, str], None]
    paste_to_previous: Callable[[str], bool]


class TkTextBuffer:
    """Adapts a tk.Text widget to the TextBuffer protocol used by DraftModel."""

    def __init__(self, text_widget: tk.Text) -> None:
        self.text = text_widget

    def _offset_to_index(self, offset: int) -> str:
        return f"1.0 + {max(0, offset)} chars"

    def _index_to_offset(self, index: Any) -> int:
        return len(self.text.get("1.0", index))

    def get_text(self, start: Optional[int] = None, end: Optional[int] = None) -> str:
        # Tk Text always adds a trailing \n; slice without it
        s_idx = self._offset_to_index(start) if start is not None else "1.0"
        e_idx = self._offset_to_index(end) if end is not None else "end - 1 chars"
        return self.text.get(s_idx, e_idx)

    def set_text(self, text: str) -> None:
        self.text.delete("1.0", "end")
        self.text.insert("1.0", text)

    def clear(self) -> None:
        self.text.delete("1.0", "end")

    def get_cursor(self) -> int:
        return self._index_to_offset("insert")

    def set_cursor(self, pos: int) -> None:
        self.text.mark_set("insert", self._offset_to_index(pos))

    def set_mark(self, name: str, pos: Any, gravity: str = "right") -> None:
        idx = self._offset_to_index(pos) if isinstance(pos, int) else pos
        self.text.mark_set(name, idx)
        self.text.mark_gravity(name, gravity)

    def get_mark(self, name: str) -> Optional[int]:
        try:
            if name in self.text.mark_names():
                return self._index_to_offset(name)
        except Exception:
            pass
        return None

    def delete_mark(self, name: str) -> None:
        try:
            self.text.mark_unset(name)
        except Exception:
            pass

    def insert(self, pos: Any, text: str, tags: Sequence[str] = ()) -> None:
        idx = self._offset_to_index(pos) if isinstance(pos, int) else pos
        self.text.insert(idx, text, tuple(tags))

    def delete(self, start: int, end: int) -> None:
        s_idx = self._offset_to_index(start)
        e_idx = self._offset_to_index(end)
        self.text.delete(s_idx, e_idx)

    def add_tag(self, tag: str, start: int, end: int) -> None:
        s_idx = self._offset_to_index(start)
        e_idx = self._offset_to_index(end)
        self.text.tag_add(tag, s_idx, e_idx)

    def get_tag_ranges(self, tag: str) -> List[Tuple[int, int]]:
        raw_ranges = self.text.tag_ranges(tag)
        out: List[Tuple[int, int]] = []
        for i in range(0, len(raw_ranges), 2):
            s = self._index_to_offset(raw_ranges[i])
            e = self._index_to_offset(raw_ranges[i + 1])
            out.append((s, e))
        return out

    def clear_tag(self, tag: str) -> None:
        self.text.tag_remove(tag, "1.0", "end")


class DraftWindow:
    """The draft editor: an editable draft on top, the refined result below.

    Dictation and pasted references go into the draft. "重新整理" never touches the
    draft; its output lands in the result box, so the draft can be edited and refined
    again as often as needed.
    """

    def __init__(
        self,
        master: tk.Misc,
        model_factory: Any,
        *,
        actions: DraftActions,
        clipboard_backend: Any = None,
        on_close: Optional[Callable[[], None]] = None,
        menu_items: Sequence[Tuple[str, Callable[[], None]]] = (),
        cancel_record: Optional[Callable[[], None]] = None,
    ) -> None:
        self.actions = actions
        self._on_close = on_close          # set => this is the main window: closing it quits
        self._menu_items = list(menu_items)
        self._cancel_record = cancel_record
        self._clipboard_backend = (
            clipboard_backend if clipboard_backend is not None else Win32ClipboardBackend()
        )
        self.top = tk.Toplevel(master)
        self.top.title("Local Typeless")
        self.top.geometry("820x680")
        self.top.minsize(520, 420)

        self._alive = True
        self._busy = False
        self._recording = False
        self._raw_visible = False
        self._record_hint = ""
        self._local_msg = ""
        self._local_msg_until = 0.0
        self._line_rows: List[Tuple[int, int]] = []
        # Draft tabs. The one on screen lives in the model; the others wait here as data.
        self._tabs: List[Dict[str, Any]] = [{"name": "草稿1"}]
        self._active = 0
        self._result_warn = False

        self._build_ui()

        # Connect buffer & model
        self._buffer = TkTextBuffer(self.text)
        if callable(model_factory):
            self.model: DraftModel = model_factory(self._buffer)
        else:
            self.model = model_factory
            self.model.buffer = self._buffer

        if getattr(self.model, "_autosave", False):
            self.model.load()
            self._load_tabs()
            self._update_raw_box()
        self._render_tabs()

        # Setup file drag and drop
        self._file_drop = enable_file_drop(self.top)

        # Window events
        self.top.protocol("WM_DELETE_WINDOW", self._close_clicked)
        self.top.after(TICK_MS, self._tick)

    def lift(self) -> None:
        """Bring window to front and focus text area."""
        try:
            self.top.deiconify()
            self.top.lift()
            self.text.focus_set()
        except Exception:
            pass

    def hide(self) -> None:
        """Hide window without destroying or clearing draft."""
        try:
            self.top.withdraw()
        except Exception:
            pass

    def _close_clicked(self) -> None:
        if self._on_close is not None:
            self._on_close()
        else:
            self.hide()

    def close(self) -> None:
        """Destroy window and cleanup file drop hooks."""
        if self._alive:
            self._save_tabs()
        self._alive = False
        if self._file_drop:
            self._file_drop.remove()
            self._file_drop = None
        try:
            self.top.destroy()
        except Exception:
            pass

    # ------------------------------------------------------------- public API
    def add_piece(self, piece: DraftPiece) -> None:
        """Insert a dictation piece (Tk thread only)."""
        self.model.add_piece(piece)
        try:
            self.text.see("insert")
        except Exception:
            pass
        self._update_ui_state()

    def set_recording(self, recording: bool, elapsed_sec: int = 0) -> None:
        """Update recording status and button display."""
        if recording:
            self.record_btn.config(text=f"■ 停止（已錄 {elapsed_sec} 秒）")
        if self._recording == recording:
            return
        self._recording = recording
        theme.restyle(self.record_btn, "danger" if recording else "primary")
        if recording:
            self.refine_btn.config(state="disabled")
            self.cancel_btn.config(state="normal")
            self.model.recording_started()
        else:
            self._show_record_idle()
            self.refine_btn.config(state="normal" if not self._busy else "disabled")
            self.cancel_btn.config(state="disabled")
            self.model.recording_stopped()
        self._update_ui_state()

    def _show_record_idle(self) -> None:
        hint = f" ({self._record_hint})" if self._record_hint else ""
        self.record_btn.config(text=f"● 開始錄音{hint}")

    def set_record_hint(self, hint: str) -> None:
        """Hotkey shown on the record button, e.g. 'Alt+Q'."""
        if hint != self._record_hint:
            self._record_hint = hint
            if not self._recording:
                self._show_record_idle()

    def set_app_status(self, status: str, message: str = "") -> None:
        """Engine status (STT / LLM) and the app's current message, shown in the status bar."""
        if self.app_lbl.cget("text") != status:
            self.app_lbl.config(text=status)
        self._app_msg = message
        self._refresh_message()

    def flash(self, text: str, seconds: float = 4.0) -> None:
        """Show a message from this window; it yields to the app's message afterwards."""
        self._local_msg = text
        self._local_msg_until = time.monotonic() + seconds
        self._refresh_message()

    def _refresh_message(self) -> None:
        if self._busy:
            return
        if self._local_msg and time.monotonic() < self._local_msg_until:
            text = self._local_msg
        else:
            text = getattr(self, "_app_msg", "") or "就緒"
        if self.status_lbl.cget("text") != text:
            self.status_lbl.config(text=text)

    def paste_from_clipboard(self) -> None:
        """Paste reference or text from clipboard into draft at cursor."""
        if self._busy:
            return
        try:                        # like any editor: a paste replaces the selection
            if self.text.tag_ranges("sel"):
                self.text.delete("sel.first", "sel.last")
        except Exception:
            pass
        res = self.model.paste_clipboard(self._clipboard_backend)
        if res:
            labels = {"file": "檔案路徑", "image": "圖片路徑", "text": "文字"}
            self.flash(f"已貼上參照（{labels.get(res, res)}）")
            try:
                self.text.see("insert")
            except Exception:
                pass
        else:
            self.flash("剪貼簿沒有可貼上的內容")
        self._update_ui_state()

    def set_busy(self, busy: bool, message: str = "") -> None:
        """Set window busy/normal state for long-running actions (e.g. LLM refine)."""
        self._busy = busy
        state = "disabled" if busy else "normal"
        self.text.config(state=state)
        self.record_btn.config(state=state)
        self.paste_ref_btn.config(state=state)
        self.refine_btn.config(state="disabled" if (busy or self._recording) else "normal")
        self.finish_btn.config(state=state)
        if busy:
            self.status_lbl.config(text=message or "處理中...")
        else:
            self._refresh_message()

    # ------------------------------------------------------------- UI layout
    def _build_ui(self) -> None:
        t = self.top

        self.topmost_var = tk.BooleanVar(value=True)
        self.top.attributes("-topmost", True)

        t.configure(bg=theme.PAGE_BG)
        ttk.Style(t).configure("Draft.TPanedwindow", background=theme.PAGE_BG)

        def label(parent, text="", fg=theme.MUTED, bg=theme.PAGE_BG, size=10):
            return tk.Label(parent, text=text, fg=fg, bg=bg, font=(FONT_FAMILY, size))

        # 1. Top toolbar
        toolbar = tk.Frame(t, bg=theme.PAGE_BG, padx=8, pady=8)
        toolbar.pack(fill="x", side="top")
        self._build_toolbar_right(toolbar)

        self.record_btn = theme.button(toolbar, "● 開始錄音", self.actions.toggle_record,
                                       kind="primary")
        self.record_btn.pack(side="left", padx=2)

        self.cancel_btn = theme.button(
            toolbar, "取消", lambda: self._cancel_record() if self._cancel_record else None,
            state="disabled",
        )
        if self._cancel_record is not None:
            self.cancel_btn.pack(side="left", padx=2)

        self.paste_ref_btn = theme.button(toolbar, "貼上參照 (Alt+V)", self.paste_from_clipboard)
        self.paste_ref_btn.pack(side="left", padx=2)

        tk.Frame(toolbar, bg=theme.BORDER, width=1).pack(side="left", fill="y", padx=8, pady=2)

        self.mode_var = tk.StringVar(value="AI 提示")       # the mode the window opens with
        self.mode_combo = theme.combobox(
            toolbar,
            textvariable=self.mode_var,
            values=["整理", "AI 提示", "摘要", "投影片"],
            width=8,
        )
        self.mode_combo.pack(side="left", padx=2)

        self.refine_btn = theme.button(toolbar, "重新整理 ↓", self._on_refine_full)
        self.refine_btn.pack(side="left", padx=2)
        self._build_rest(label)

    def _build_toolbar_right(self, toolbar: tk.Frame) -> None:
        """Right-hand toolbar items. Packed first, so a narrow window clips the left ones last."""
        if self._menu_items:
            more = theme.menubutton(toolbar, "更多")
            menu = tk.Menu(more, tearoff=False)
            for item_label, command in self._menu_items:
                menu.add_command(label=item_label, command=command)
            more["menu"] = menu
            more.pack(side="right", padx=2)

        tk.Checkbutton(
            toolbar, text="置頂", variable=self.topmost_var, command=self._toggle_topmost,
            bg=theme.PAGE_BG, fg=theme.TEXT, activebackground=theme.PAGE_BG,
            activeforeground=theme.TEXT, selectcolor=theme.PAGE_BG, font=(FONT_FAMILY, 10),
        ).pack(side="right", padx=4)

    def _build_rest(self, label: Callable[..., tk.Label]) -> None:
        t = self.top

        # 2. Status bar (very bottom)
        status_bar = tk.Frame(t, bg=theme.SIDEBAR_BG, padx=10, pady=4)
        status_bar.pack(fill="x", side="bottom")

        self.status_lbl = label(status_bar, "就緒", fg=theme.TEXT, bg=theme.SIDEBAR_BG, size=9)
        self.status_lbl.pack(side="left")

        self.fallback_lbl = label(status_bar, fg=theme.DANGER, bg=theme.SIDEBAR_BG, size=9)
        self.fallback_lbl.pack(side="left", padx=12)

        self.app_lbl = label(status_bar, bg=theme.SIDEBAR_BG, size=9)
        self.app_lbl.pack(side="right")

        # 3. Bottom action row
        bottom = tk.Frame(t, bg=theme.PAGE_BG, padx=10, pady=6)
        bottom.pack(fill="x", side="bottom")

        self.finish_btn = theme.button(bottom, "完成（存檔並清空）", self._on_finish)
        self.finish_btn.pack(side="left")

        self.raw_toggle_btn = theme.button(bottom, "▶ 顯示逐字稿", self._toggle_raw)
        self.raw_toggle_btn.pack(side="left", padx=8)

        self.count_lbl = label(bottom, "0 字")
        self.count_lbl.pack(side="right")

        # 4. Collapsible raw transcript (hidden by default)
        self.raw_frame = tk.Frame(t, bg=theme.PAGE_BG, padx=10, pady=4)
        self.raw_box = theme.text_box(self.raw_frame, bg=theme.CARD_BG, size=9, height=4,
                                      wrap="word", state="disabled")
        self.raw_box.configure(fg=theme.MUTED)
        raw_scroll = ttk.Scrollbar(self.raw_frame, orient="vertical", command=self.raw_box.yview)
        self.raw_box.configure(yscrollcommand=raw_scroll.set)
        self.raw_box.pack(side="left", fill="both", expand=True)
        raw_scroll.pack(side="right", fill="y")
        self._bottom_row = bottom

        # 5. Draft (top) and result (bottom), resizable against each other
        panes = ttk.PanedWindow(t, orient="vertical", style="Draft.TPanedwindow")
        panes.pack(fill="both", expand=True, side="top")

        draft_frame = tk.Frame(panes, bg=theme.PAGE_BG, padx=10, pady=4)
        draft_head = tk.Frame(draft_frame, bg=theme.PAGE_BG)
        draft_head.pack(fill="x", pady=(0, 4))
        # the buttons are packed first so a long row of tabs cannot push them out
        self.copy_all_btn = theme.button(draft_head, "複製草稿", self._on_copy_all)
        self.copy_all_btn.pack(side="right")
        self._cleared: Optional[Tuple[str, List[Tuple[int, int]]]] = None   # for 復原清空
        self.clear_btn = theme.button(draft_head, "清空", self._on_clear)
        self.clear_btn.pack(side="right", padx=4)
        self.tab_bar = tk.Frame(draft_head, bg=theme.PAGE_BG)
        self.tab_bar.pack(side="left")
        self._tab_widgets: List[tk.Label] = []
        label(draft_head, "可直接修改；右鍵分頁可儲存或刪除", size=9).pack(side="left", padx=8)
        self.line_gutter = tk.Canvas(draft_frame, width=LINE_GUTTER_PX, bg=theme.SIDEBAR_BG,
                                     highlightthickness=0, borderwidth=0)
        self.line_gutter.pack(side="left", fill="y")
        self.text = theme.text_box(draft_frame, undo=True, wrap="word", height=10)
        text_scroll = ttk.Scrollbar(draft_frame, orient="vertical", command=self.text.yview)

        def scrolled(first: str, last: str) -> None:
            text_scroll.set(first, last)
            self._draw_line_numbers()

        self.text.configure(yscrollcommand=scrolled)
        text_scroll.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)
        self.text.bind("<Configure>", lambda _e: self.text.after_idle(self._draw_line_numbers),
                       add="+")
        panes.add(draft_frame, weight=3)

        result_frame = tk.Frame(panes, bg=theme.PAGE_BG, padx=10, pady=4)
        result_head = tk.Frame(result_frame, bg=theme.PAGE_BG)
        result_head.pack(fill="x", pady=(0, 4))
        self.result_title = label(result_head, RESULT_TITLE_EMPTY,
                                  fg=theme.ACCENT, size=12)
        self.result_title.pack(side="left")
        self.paste_back_btn = theme.button(result_head, "貼回上一個視窗", self._on_paste_back,
                                           kind="primary")
        self.paste_back_btn.pack(side="right")
        self.copy_result_btn = theme.button(result_head, "複製結果", self._on_copy_result)
        self.copy_result_btn.pack(side="right", padx=4)
        self.result = theme.text_box(result_frame, bg=theme.CARD_BG, undo=True, wrap="word",
                                     height=7)
        result_scroll = ttk.Scrollbar(result_frame, orient="vertical", command=self.result.yview)
        self.result.configure(yscrollcommand=result_scroll.set)
        result_scroll.pack(side="right", fill="y")
        self.result.pack(side="left", fill="both", expand=True)
        panes.add(result_frame, weight=2)

        # Tag for embedded items
        self.text.tag_configure("embed", background="#e0f2fe", foreground="#0369a1")
        self.text.tag_raise("sel")       # or a selected reference keeps its own background

        # Key bindings
        self.text.bind("<Control-v>", lambda _e: (self.paste_from_clipboard(), "break")[1])
        self.text.bind("<Control-V>", lambda _e: (self.paste_from_clipboard(), "break")[1])
        # Alt+V is the global hotkey (the app calls paste_from_clipboard); binding it here
        # as well would paste twice while this window has the focus.
        self.text.bind("<Button-3>", self._show_context_menu)
        self.text.bind("<<Modified>>", self._on_text_modified)

        # Context menu
        self.context_menu = tk.Menu(self.top, tearoff=False)
        self.context_menu.add_command(label="剪下 (Ctrl+X)", command=self._cut)
        self.context_menu.add_command(label="複製 (Ctrl+C)", command=self._copy)
        self.context_menu.add_command(
            label="貼上參照 (Ctrl+V)", command=self.paste_from_clipboard
        )
        self.context_menu.add_separator()
        self.context_menu.add_command(label="全選 (Ctrl+A)", command=self._select_all)

    # ------------------------------------------------------------- event handlers
    def _toggle_topmost(self) -> None:
        self.top.attributes("-topmost", self.topmost_var.get())

    def _toggle_raw(self) -> None:
        self._raw_visible = not self._raw_visible
        if self._raw_visible:
            self.raw_frame.pack(fill="x", side="bottom", after=self._bottom_row)
            self.raw_toggle_btn.config(text="▼ 隱藏逐字稿")
            self._update_raw_box()
        else:
            self.raw_frame.pack_forget()
            self.raw_toggle_btn.config(text="▶ 顯示逐字稿")

    def _show_context_menu(self, event: Any) -> None:
        try:
            self.context_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.context_menu.grab_release()

    def _cut(self) -> None:
        self.text.event_generate("<<Cut>>")

    def _copy(self) -> None:
        self.text.event_generate("<<Copy>>")

    def _select_all(self) -> None:
        self.text.tag_add("sel", "1.0", "end")

    def visible_line_numbers(self) -> List[Tuple[int, int]]:
        """(line number, y of its first row) for every draft line whose start is on screen.

        A line is what ends with Enter; a long line that wraps keeps one number.
        """
        first = int(self.text.index("@0,0").split(".")[0])
        last = int(self.text.index(f"@0,{self.text.winfo_height()}").split(".")[0])
        rows: List[Tuple[int, int]] = []
        for number in range(first, last + 1):
            info = self.text.dlineinfo(f"{number}.0")
            if info is not None:
                rows.append((number, info[1] + info[3] // 2))
        return rows

    def _draw_line_numbers(self) -> None:
        try:
            rows = self.visible_line_numbers()
        except tk.TclError:
            return
        if rows == self._line_rows:
            return
        self._line_rows = rows
        gutter = self.line_gutter
        gutter.delete("all")
        for number, y in rows:
            gutter.create_text(LINE_GUTTER_PX - 6, y, anchor="e", text=str(number),
                               fill=theme.MUTED, font=(FONT_FAMILY, 9))

    def _on_text_modified(self, _event: Any = None) -> None:
        self.text.after_idle(self._draw_line_numbers)
        if self.text.edit_modified():
            self.text.edit_modified(False)
            if getattr(self.model, "_autosave", False):
                self.model.save()
            self._update_ui_state()

    def result_text(self) -> str:
        return self.result.get("1.0", "end-1c")

    def _set_result(self, text: str) -> None:
        self.result.delete("1.0", "end")
        self.result.insert("1.0", text)

    def _set_result_title(self, text: str = "", warn: bool = False) -> None:
        """Heading of the result box; `warn` = it carries doubts about the result."""
        self._result_warn = warn
        self.result_title.config(text=text or RESULT_TITLE_EMPTY,
                                 fg=theme.DANGER if warn else theme.ACCENT)

    # ------------------------------------------------------------- draft tabs
    def tab_names(self) -> List[str]:
        return [t["name"] for t in self._tabs]

    def _can_change_tab(self) -> bool:
        if self._recording or self._busy:
            self.flash("錄音或整理進行中，等結束後再切換草稿", 5.0)
            return False
        return True

    def _snapshot(self) -> Dict[str, Any]:
        return {"name": self._tabs[self._active]["name"], "state": self.model.state(),
                "result": self.result_text(), "title": self.result_title.cget("text"),
                "warn": self._result_warn}

    def _show_tab(self, tab: Dict[str, Any]) -> None:
        self.model.set_state(tab.get("state") or {})
        self._set_result(tab.get("result") or "")
        self._set_result_title(tab.get("title") or "", bool(tab.get("warn")))
        self._cleared = None
        try:
            self.text.edit_reset()          # Ctrl+Z must not bring back another tab's text
        except tk.TclError:
            pass
        self._update_raw_box()
        self._update_ui_state()

    def _after_tab_change(self) -> None:
        self._render_tabs()
        if getattr(self.model, "_autosave", False):
            self.model.save()
        self._save_tabs()

    def switch_tab(self, index: int) -> bool:
        if index == self._active:
            return True
        if not 0 <= index < len(self._tabs) or not self._can_change_tab():
            return False
        self._tabs[self._active] = self._snapshot()
        self._active = index
        self._show_tab(self._tabs[index])
        self._after_tab_change()
        return True

    def new_tab(self) -> bool:
        if not self._can_change_tab():
            return False
        self._tabs[self._active] = self._snapshot()
        used = [int(m.group(1)) for m in (re.fullmatch(r"草稿(\d+)", n) for n in self.tab_names()) if m]
        self._tabs.append({"name": f"草稿{max(used, default=0) + 1}"})
        self._active = len(self._tabs) - 1
        self._show_tab(self._tabs[self._active])
        self._after_tab_change()
        self.text.focus_set()
        return True

    def delete_tab(self, index: int, confirm: bool = True) -> bool:
        if not 0 <= index < len(self._tabs) or not self._can_change_tab():
            return False
        self._tabs[self._active] = self._snapshot()
        tab = self._tabs[index]
        if confirm and ((tab.get("state") or {}).get("text") or "").strip():
            from tkinter import messagebox

            if not messagebox.askyesno("刪除草稿", f"「{tab['name']}」裡還有內容，確定刪除？",
                                       parent=self.top):
                return False
        if len(self._tabs) == 1:
            self._tabs = [{"name": "草稿1"}]          # there is always one draft
        else:
            del self._tabs[index]
            if index < self._active or self._active >= len(self._tabs):
                self._active -= 1
        self._show_tab(self._tabs[self._active])
        self._after_tab_change()
        self.flash(f"已刪除{tab['name']}")
        return True

    def save_tab(self, index: int) -> bool:
        """Keep a draft as a record (History) without closing or emptying it."""
        if not self.switch_tab(index):
            return False
        draft = self.model.buffer.get_text()
        if not draft.strip():
            self.flash("這份草稿是空的，沒有東西可存")
            return False
        result = self.result_text()
        if result.strip():
            final_text, source = result, draft
        else:
            final_text, source = draft, self.model.raw_transcript()
        self.actions.save_draft(final_text, source)
        self.flash(f"已把{self._tabs[index]['name']}存成紀錄（草稿還在）", 6.0)
        return True

    def _tab_menu(self, event: Any, index: int) -> None:
        menu = tk.Menu(self.top, tearoff=False)
        menu.add_command(label="儲存成紀錄", command=lambda: self.save_tab(index))
        menu.add_command(label="刪除", command=lambda: self.delete_tab(index))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _render_tabs(self) -> None:
        for w in self.tab_bar.winfo_children():
            w.destroy()
        self._tab_widgets = []
        for i, tab in enumerate(self._tabs):
            active = i == self._active
            w = tk.Label(self.tab_bar, text=tab["name"], padx=10, pady=3, cursor="hand2",
                         font=(FONT_FAMILY, 10, "bold" if active else "normal"),
                         bg=theme.ACCENT if active else theme.SIDEBAR_BG,
                         fg="#ffffff" if active else theme.TEXT)
            w.pack(side="left", padx=(0, 3))
            w.bind("<Button-1>", lambda _e, i=i: self.switch_tab(i))
            w.bind("<Button-3>", lambda e, i=i: self._tab_menu(e, i))
            self._tab_widgets.append(w)
        plus = tk.Label(self.tab_bar, text="＋", padx=8, pady=3, cursor="hand2",
                        font=(FONT_FAMILY, 10), bg=theme.SIDEBAR_BG, fg=theme.ACCENT)
        plus.pack(side="left")
        plus.bind("<Button-1>", lambda _e: self.new_tab())
        self.new_tab_btn = plus

    def _tabs_file(self) -> Path:
        return Path(self.model._cache_dir) / TABS_FILE

    def _save_tabs(self) -> None:
        """Names, order and the drafts that are not on screen (that one is the model's file)."""
        if not getattr(self.model, "_autosave", False):
            return
        try:
            tabs = list(self._tabs)
            tabs[self._active] = dict(self._snapshot(), state={})
            path = self._tabs_file()
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"active": self._active, "tabs": tabs}, ensure_ascii=False,
                                      indent=2), encoding="utf-8")
            tmp.replace(path)
        except Exception as exc:
            logger.warning("Failed to save draft tabs: %s", exc)

    def _load_tabs(self) -> None:
        path = self._tabs_file()
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            tabs = [t for t in data.get("tabs", []) if isinstance(t, dict) and t.get("name")]
            if not tabs:
                return
            self._tabs = tabs
            self._active = min(max(int(data.get("active", 0)), 0), len(tabs) - 1)
            tab = tabs[self._active]             # its draft was loaded by the model already
            self._set_result(tab.get("result") or "")
            self._set_result_title(tab.get("title") or "", bool(tab.get("warn")))
        except Exception as exc:
            logger.warning("Failed to load draft tabs: %s", exc)

    def _on_refine_full(self) -> None:
        if self._recording or self._busy:
            return
        text = self.model.buffer.get_text()
        if not text.strip():
            return

        mode_label = self.mode_var.get()
        mode_map = {"整理": Mode.CLEAN, "AI 提示": Mode.PROMPT, "摘要": Mode.SUMMARY,
                    "投影片": Mode.SLIDE}
        mode = mode_map.get(mode_label, Mode.CLEAN)

        protected, mapping = self.model.protect()
        self.set_busy(True, f"{mode_label}中…（草稿不會被改動）")

        def on_done(refined_or_none: Optional[str], err_msg: str, doubts: str = "") -> None:
            self.set_busy(False)
            if refined_or_none is None or err_msg:
                self.flash(f"整理失敗，草稿與原結果都沒有變動：{err_msg or '未知錯誤'}", 8.0)
                return
            restored = self.model.restore(refined_or_none, mapping)
            if restored is None:
                self.flash("LLM 弄丟了嵌入的參照，這次結果沒有採用；請再按一次", 8.0)
                return
            self._set_result(restored)
            if doubts:
                self._set_result_title(f"整理結果（{mode_label}）⚠ 請檢查：{doubts[:36]}", warn=True)
                self.flash(f"已更新整理結果，但有疑點請檢查：{doubts}", 10.0)
            else:
                self._set_result_title(f"整理結果（{mode_label}）")
                self.flash("已更新整理結果")
            self._update_ui_state()
            self._save_tabs()

        self.actions.refine(protected, mode, on_done)

    def _output_text(self) -> str:
        """What leaves the window: the result when there is one, otherwise the draft."""
        result = self.result_text()
        return result if result.strip() else self.model.buffer.get_text()

    def _on_copy_all(self) -> None:
        text = self.model.buffer.get_text()
        if text:
            copy_with_retry(text)
            self.flash("已複製草稿")

    def _on_clear(self) -> None:
        """Empty the draft. While it stays empty the same button puts it back."""
        buf = self.model.buffer
        text = buf.get_text()
        if text:
            self._cleared = (text, buf.get_tag_ranges("embed"))
            buf.clear()
            self.flash("已清空草稿；按「復原清空」可以拿回來", 6.0)
        elif self._cleared is not None:
            old, embeds = self._cleared
            self._cleared = None
            buf.set_text(old)
            for start, end in embeds:
                buf.add_tag("embed", start, end)
            self.flash("已復原草稿")
        self._update_ui_state()

    def _on_copy_result(self) -> None:
        text = self.result_text()
        if text.strip():
            copy_with_retry(text)
            self.flash("已複製整理結果")
        else:
            self.flash("還沒有整理結果（先按「重新整理」）")

    def _on_paste_back(self) -> None:
        text = self._output_text()
        if not text.strip():
            return
        used = "整理結果" if self.result_text().strip() else "草稿"
        if self.actions.paste_to_previous(text):
            self.flash(f"已把{used}貼回上一個視窗")
        else:
            self.flash("找不到上一個視窗；請改用複製，再到目標視窗按 Ctrl+V", 8.0)

    def _on_finish(self) -> None:
        draft = self.model.buffer.get_text()
        if not draft.strip():
            return
        result = self.result_text()
        draft_text, raw_text = self.model.finish()
        if result.strip():
            # The record keeps the result, with the draft it was made from as the source.
            final_text, source = result, draft_text
        else:
            final_text, source = draft_text, raw_text
        self.actions.save_draft(final_text, source)
        try:
            copy_with_retry(final_text)
        except Exception as exc:
            logger.debug("Failed to copy finished draft: %s", exc)
        self._set_result("")
        self._set_result_title()
        self._update_raw_box()
        self.flash("已存成紀錄並複製到剪貼簿", 6.0)
        self._update_ui_state()
        self._save_tabs()

    # ------------------------------------------------------------- polling tick
    def _tick(self) -> None:
        if not self._alive:
            return
        try:
            if self._file_drop is not None:
                drops = self._file_drop.poll()
                for paths in drops:
                    if paths:
                        self.model.insert_files(paths)
                        self.flash("已插入檔案路徑")
            self._refresh_message()
            self._update_ui_state()
            self._draw_line_numbers()        # catches re-wrapping that fires no event
        except Exception as exc:
            logger.debug("DraftWindow._tick error: %s", exc)
        finally:
            if self._alive:
                self.top.after(TICK_MS, self._tick)

    def _update_ui_state(self) -> None:
        try:
            n_chars = len(self.model.buffer.get_text())
            count = f"草稿 {n_chars} 字"
            clear = "復原清空" if not n_chars and self._cleared is not None else "清空"
            if self.clear_btn.cget("text") != clear:
                self.clear_btn.config(text=clear)
            if self.count_lbl.cget("text") != count:
                self.count_lbl.config(text=count)

            fb = ""
            if self.model.fallback_count > 0:
                fb = f"有 {self.model.fallback_count} 段未整理（LLM 連不上）"
            if self.fallback_lbl.cget("text") != fb:
                self.fallback_lbl.config(text=fb)

            if self._raw_visible:
                self._update_raw_box()
        except Exception:
            pass

    def _update_raw_box(self) -> None:
        try:
            raw = self.model.raw_transcript()
            if raw == getattr(self, "_raw_shown", None):
                return                  # unchanged: keep the scroll position
            self._raw_shown = raw
            self.raw_box.config(state="normal")
            self.raw_box.delete("1.0", "end")
            self.raw_box.insert("1.0", raw)
            self.raw_box.config(state="disabled")
        except Exception:
            pass
