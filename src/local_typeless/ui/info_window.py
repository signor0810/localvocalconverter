"""Info panel (Tk Toplevel): a sidebar with Home, History and System prompt pages.

Home and History only display. The System prompt page edits the prompt file of a mode;
"載入" saves it, and the next refine uses it. The data comes from
ui.info_controller.InfoController; a page is rebuilt from the files each time it is shown.
"""
from __future__ import annotations

import os
import re
import tkinter as tk
from datetime import date, timedelta
from pathlib import Path
from tkinter import ttk
from typing import Any, Callable, Dict, List, Optional, Tuple

from local_typeless.types import Mode
from local_typeless.ui import theme
from local_typeless.ui.info_controller import (
    LANGUAGE_CHOICES,
    MODE_LABELS,
    PROMPT_MODES,
    DictEntry,
    DictionaryView,
    HistoryEntry,
    HomeStats,
    InfoController,
)
from local_typeless.ui.theme import CARD_BG, FONT, MUTED, PAGE_BG, SIDEBAR_BG, TEXT

HEAT_WEEKS = 18
WEEKDAYS = "一二三四五六日"

PAGES: Tuple[Tuple[str, str], ...] = (
    ("home", "Home"),
    ("history", "History"),
    ("dictionary", "Dictionary"),
    ("prompt", "System prompt"),
    ("settings", "System setting"),
)


def heat_level(count: int) -> int:
    """Colour step of a heat-map day: 0 (nothing) .. 3 (busy)."""
    if count <= 0:
        return 0
    if count <= 2:
        return 1
    if count <= 5:
        return 2
    return 3


def format_time(entry: HistoryEntry) -> str:
    return entry.when.strftime("%H:%M")


def format_day(day: date) -> str:
    return f"{day.year} 年 {day.month} 月 {day.day} 日（{WEEKDAYS[day.weekday()]}）"


def open_path(path: Path) -> bool:
    """Open a file or folder with the system's default program."""
    try:
        os.startfile(str(path))  # type: ignore[attr-defined]
        return True
    except Exception:
        return False


class InfoWindow:
    def __init__(
        self,
        master: tk.Misc,
        ctl: InfoController,
        mode_getter: Optional[Callable[[], Optional[Mode]]] = None,
        open_review: Optional[Callable[[], None]] = None,
        open_teach: Optional[Callable[[], None]] = None,
        show_editor_settings: bool = True,
    ) -> None:
        self.ctl = ctl
        self._mode_getter = mode_getter
        self._open_review = open_review
        self._open_teach = open_teach
        self._show_editor_settings = show_editor_settings
        self.top = tk.Toplevel(master)
        self.top.title("Local Typeless")
        self.top.geometry("900x680")
        self.top.minsize(680, 460)
        self.top.configure(bg=PAGE_BG)
        self.top.attributes("-topmost", True)    # the draft window is topmost too
        self._alive = True
        self._page = ""
        self._nav: Dict[str, tk.Label] = {}
        self._history_mode: Optional[str] = None
        self._prompt_mode: Mode = PROMPT_MODES[0]
        self._prompt_mode_chosen = False         # True once picked here; then the draft's mode is ignored
        self._prompt_edits: Dict[Mode, str] = {}  # edits not loaded yet, kept while browsing
        self._prompt_box: Optional[tk.Text] = None
        self._prompt_preview = False
        self._dict_filter_text = ""
        self._dict_selected_term: Optional[str] = None
        self._dict_tree: Optional[ttk.Treeview] = None
        self._dict_entries: List[DictEntry] = []
        self._settings_vars: Dict[str, tk.StringVar] = {}
        self._editor_settings_vars: Dict[str, Any] = {}
        self._build()
        self.top.protocol("WM_DELETE_WINDOW", self.close)
        self.show("home")

    # ----------------------------------------------------------- window
    def lift(self) -> None:
        try:
            self.top.deiconify()
            self.top.lift()
        except Exception:
            pass

    def close(self) -> None:
        self._alive = False
        try:
            self.top.destroy()
        except Exception:
            pass

    def _build(self) -> None:
        side = tk.Frame(self.top, bg=SIDEBAR_BG)
        side.pack(side="left", fill="y")
        tk.Label(side, text="Local Typeless", bg=SIDEBAR_BG, fg=theme.ACCENT, anchor="w",
                 font=(FONT, 13, "bold")).pack(fill="x", padx=(16, 28), pady=(18, 14))
        for key, label in PAGES:
            item = tk.Label(side, text=label, bg=SIDEBAR_BG, fg=TEXT, anchor="w",
                            font=(FONT, 11), padx=14, pady=8, cursor="hand2")
            item.pack(fill="x", padx=8, pady=1)
            item.bind("<Button-1>", lambda _e, k=key: self.show(k))
            self._nav[key] = item

        if self._open_review is not None or self._open_teach is not None:
            side_bottom = tk.Frame(side, bg=SIDEBAR_BG)
            side_bottom.pack(side="bottom", fill="x", pady=10)
            if self._open_review is not None:
                rev_btn = tk.Label(side_bottom, text="檢查口述紀錄…", bg=SIDEBAR_BG, fg=TEXT, anchor="w",
                                   font=(FONT, 10), padx=14, pady=6, cursor="hand2")
                rev_btn.pack(fill="x", padx=8, pady=2)
                rev_btn.bind("<Button-1>", lambda _e: self._open_review())
            if self._open_teach is not None:
                tch_btn = tk.Label(side_bottom, text="教學模式…", bg=SIDEBAR_BG, fg=TEXT, anchor="w",
                                   font=(FONT, 10), padx=14, pady=6, cursor="hand2")
                tch_btn.pack(fill="x", padx=8, pady=2)
                tch_btn.bind("<Button-1>", lambda _e: self._open_teach())

        self.body = tk.Frame(self.top, bg=PAGE_BG)
        self.body.pack(side="left", fill="both", expand=True)

    def show(self, page: str) -> None:
        """Switch to a page and rebuild it from the current files."""
        self._keep_prompt_edit()
        self._page = page
        for key, item in self._nav.items():
            selected = key == page
            item.configure(bg=theme.SIDEBAR_SELECTED if selected else SIDEBAR_BG,
                           fg=theme.ACCENT if selected else TEXT,
                           font=(FONT, 11, "bold" if selected else "normal"))
        for child in self.body.winfo_children():
            child.destroy()
        self._prompt_box = None
        self._dict_tree = None
        {
            "home": self._home_page,
            "history": self._history_page,
            "dictionary": self._dictionary_page,
            "prompt": self._prompt_page,
            "settings": self._settings_page,
        }[page]()

    # ----------------------------------------------------------- helpers
    def _title(self, text: str) -> tk.Frame:
        head = tk.Frame(self.body, bg=PAGE_BG)
        head.pack(fill="x", padx=24, pady=(18, 10))
        tk.Label(head, text=text, bg=PAGE_BG, fg=theme.ACCENT, font=(FONT, 22, "bold")).pack(side="left")
        return head

    def _card(self, parent: tk.Misc) -> tk.Frame:
        return tk.Frame(parent, bg=CARD_BG, padx=16, pady=12)

    def _number(self, parent: tk.Misc, value: str, unit: str, caption: str) -> tk.Frame:
        box = tk.Frame(parent, bg=CARD_BG)
        row = tk.Frame(box, bg=CARD_BG)
        row.pack(anchor="w")
        tk.Label(row, text=value, bg=CARD_BG, fg=theme.ACCENT, font=(FONT, 20, "bold")).pack(side="left")
        tk.Label(row, text=" " + unit, bg=CARD_BG, fg=TEXT, font=(FONT, 10)).pack(side="left", anchor="s", pady=(0, 5))
        tk.Label(box, text=caption, bg=CARD_BG, fg=MUTED, font=(FONT, 10)).pack(anchor="w")
        return box

    def _flash(self, label: tk.Label, text: str, colour: str = MUTED, seconds: float = 2.5) -> None:
        label.configure(text=text, fg=colour)
        self.top.after(int(seconds * 1000),
                       lambda: label.winfo_exists() and label.cget("text") == text
                       and label.configure(text=""))

    def _copy(self, text: str) -> None:
        self.top.clipboard_clear()
        self.top.clipboard_append(text)

    # -------------------------------------------------------------- home
    def _home_page(self) -> None:
        stats = self.ctl.home()
        self._title("Home")
        cols = tk.Frame(self.body, bg=PAGE_BG)
        cols.pack(fill="both", expand=True, padx=24, pady=(0, 16))
        left = tk.Frame(cols, bg=PAGE_BG)
        left.pack(side="left", fill="both", expand=True)
        right = tk.Frame(cols, bg=PAGE_BG, width=230)
        right.pack(side="left", fill="y", padx=(16, 0))
        right.pack_propagate(False)

        top = tk.Frame(left, bg=PAGE_BG)
        top.pack(fill="x")
        top.columnconfigure(0, weight=1, uniform="c")
        top.columnconfigure(1, weight=1, uniform="c")
        for col, (value, unit, caption) in enumerate([
            (f"{stats.words:,}", "字", "口述字數（整理後）"),
            (f"{stats.records:,}", "筆", "完成的口述"),
        ]):
            card = self._card(top)
            card.grid(row=0, column=col, sticky="nsew", padx=(0, 10) if col == 0 else 0)
            self._number(card, value, unit, caption).pack(anchor="w")

        days = self._card(left)
        days.pack(fill="x", pady=(10, 0))
        row = tk.Frame(days, bg=CARD_BG)
        row.pack(fill="x")
        for value, caption in [
            (stats.active_days, "使用天數"),
            (stats.current_streak, "目前連續"),
            (stats.longest_streak, "最長連續"),
        ]:
            self._number(row, str(value), "天", caption).pack(side="left", expand=True, anchor="w")
        self._heatmap(days, stats).pack(anchor="w", pady=(14, 0))

        notes = []
        if stats.fallbacks:
            notes.append(f"其中 {stats.fallbacks} 筆沒有經過 LLM（連不上或防呆未通過，改用規則整理）。")
        keep = f"保留 {stats.retention_days} 天" if stats.retention_days > 0 else "永久保留"
        notes.append(f"統計範圍：紀錄資料夾裡現有的紀錄（{keep}）。")
        tk.Label(left, text="\n".join(notes), bg=PAGE_BG, fg=MUTED, font=(FONT, 9),
                 justify="left", anchor="w").pack(fill="x", pady=(10, 0))

        keys = self._card(right)
        keys.pack(fill="x")
        tk.Label(keys, text="快捷鍵", bg=CARD_BG, fg=theme.ACCENT, font=(FONT, 11, "bold")).pack(anchor="w")
        for what, key in stats.hotkeys:
            tk.Label(keys, text=what, bg=CARD_BG, fg=MUTED, font=(FONT, 10)).pack(anchor="w", pady=(8, 2))
            tk.Label(keys, text=key or "（未設定）", bg=PAGE_BG, fg=theme.ACCENT, font=(FONT, 10, "bold"),
                     padx=8, pady=2, highlightthickness=1,
                     highlightbackground=theme.BORDER).pack(anchor="w")

        engine = self._card(right)
        engine.pack(fill="x", pady=(10, 0))
        tk.Label(engine, text="目前設定", bg=CARD_BG, fg=theme.ACCENT, font=(FONT, 11, "bold")).pack(anchor="w")
        for name, value in stats.engine:
            tk.Label(engine, text=name, bg=CARD_BG, fg=MUTED, font=(FONT, 10)).pack(anchor="w", pady=(8, 0))
            tk.Label(engine, text=value, bg=CARD_BG, fg=TEXT, font=(FONT, 10), wraplength=190,
                     justify="left").pack(anchor="w")

    def _heatmap(self, parent: tk.Misc, stats: HomeStats) -> tk.Canvas:
        cell, gap, left_pad, top_pad = 14, 4, 22, 4
        width = left_pad + HEAT_WEEKS * (cell + gap)
        height = top_pad + 7 * (cell + gap) + 18
        canvas = tk.Canvas(parent, width=width, height=height, bg=CARD_BG, highlightthickness=0)
        today = date.today()
        first = today - timedelta(days=today.weekday() + 7 * (HEAT_WEEKS - 1))   # a Monday
        for r in range(7):
            canvas.create_text(8, top_pad + r * (cell + gap) + cell / 2, text=WEEKDAYS[r],
                               fill=MUTED, font=(FONT, 8))
        last_month = 0
        for w in range(HEAT_WEEKS):
            monday = first + timedelta(days=7 * w)
            x = left_pad + w * (cell + gap)
            if monday.month != last_month:
                last_month = monday.month
                canvas.create_text(x, top_pad + 7 * (cell + gap) + 8, text=f"{monday.month}月",
                                   fill=MUTED, font=(FONT, 8), anchor="w")
            for r in range(7):
                day = monday + timedelta(days=r)
                if day > today:
                    continue
                y = top_pad + r * (cell + gap)
                colour = theme.HEAT_COLORS[heat_level(stats.day_counts.get(day, 0))]
                canvas.create_rectangle(x, y, x + cell, y + cell, fill=colour, outline="")
        return canvas

    # ----------------------------------------------------------- history
    def _history_page(self) -> None:
        head = self._title("History")
        msg = tk.Label(head, text="", bg=PAGE_BG, fg=MUTED, font=(FONT, 9))
        theme.button(head, "開啟紀錄資料夾",
                     lambda: open_path(self.ctl.records_dir)
                     or self._flash(msg, "找不到紀錄資料夾")).pack(side="right")
        msg.pack(side="right", padx=8)

        info = self._card(self.body)
        info.pack(fill="x", padx=24)
        days = int(getattr(self.ctl.cfg.storage, "retention_days", 0) or 0)
        keep = f"保留 {days} 天，過期的會自動刪除" if days > 0 else "永久保留"
        tk.Label(info, text="紀錄只存在這台電腦", bg=CARD_BG, fg=theme.ACCENT,
                 font=(FONT, 10, "bold")).pack(anchor="w")
        tk.Label(info, text=f"每次按「完成」會存成一個 Markdown 檔（{keep}）。\n位置：{self.ctl.records_dir}",
                 bg=CARD_BG, fg=MUTED, font=(FONT, 9), justify="left").pack(anchor="w")

        bar = tk.Frame(self.body, bg=PAGE_BG)
        bar.pack(fill="x", padx=24, pady=(10, 4))
        choices: List[Tuple[str, Optional[str]]] = [("全部", None)] + [
            (MODE_LABELS[m.value], m.value) for m in PROMPT_MODES]
        current = next((label for label, value in choices if value == self._history_mode), "全部")
        var = tk.StringVar(value=current)
        box = theme.combobox(bar, textvariable=var, width=8,
                             values=[label for label, _ in choices])
        box.pack(side="left")
        count = tk.Label(bar, text="", bg=PAGE_BG, fg=MUTED, font=(FONT, 9))
        count.pack(side="left", padx=10)

        def changed(_e: object = None) -> None:
            self._history_mode = dict(choices)[var.get()]
            self.show("history")

        box.bind("<<ComboboxSelected>>", changed)

        frame = tk.Frame(self.body, bg=PAGE_BG)
        frame.pack(fill="both", expand=True, padx=(24, 12), pady=(0, 14))
        text = tk.Text(frame, wrap="word", font=(FONT, 10), bg=PAGE_BG, fg=theme.INK, relief="flat",
                       borderwidth=0, padx=4, spacing1=2, spacing3=2, cursor="arrow")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        text.pack(side="left", fill="both", expand=True)
        text.tag_configure("day", foreground=theme.ACCENT, font=(FONT, 9, "bold"), spacing1=14, spacing3=6)
        text.tag_configure("time", foreground=MUTED, font=(FONT, 9))
        text.tag_configure("flag", foreground=theme.WARN, font=(FONT, 9))
        text.tag_configure("body", lmargin1=64, lmargin2=64, spacing3=10)

        entries = self.ctl.history(self._history_mode)
        count.configure(text=f"{len(entries)} 筆" if entries else "")
        if not entries:
            text.insert("end", "還沒有紀錄。在草稿視窗按「完成」之後，那次口述會出現在這裡。", "time")
        last_day: Optional[date] = None
        for entry in entries:
            day = entry.when.date()
            if day != last_day:
                last_day = day
                text.insert("end", format_day(day) + "\n", "day")
            text.insert("end", f"{format_time(entry)}   {entry.mode_label}", "time")
            if entry.fallback:
                text.insert("end", "   未經 LLM", "flag")
            text.insert("end", "\n")
            text.insert("end", entry.text.strip() + "\n", "body")
        text.configure(state="disabled")

    # -------------------------------------------------------- dictionary
    def _populate_dictionary_tree(self) -> None:
        if not hasattr(self, "_dict_tree") or self._dict_tree is None:
            return
        self._dict_tree.delete(*self._dict_tree.get_children())
        query = getattr(self, "_dict_filter_text", "").strip().lower()
        entries = getattr(self, "_dict_entries", [])
        for e in entries:
            aliases_str = ", ".join(e.aliases)
            source_str = "內建" if e.builtin else "自訂"
            if query:
                matched = (
                    query in e.term.lower()
                    or query in e.definition.lower()
                    or query in aliases_str.lower()
                )
                if not matched:
                    continue
            item_id = self._dict_tree.insert(
                "", "end",
                values=(e.term, e.definition, aliases_str, source_str)
            )
            if getattr(self, "_dict_selected_term", None) == e.term:
                self._dict_tree.selection_set(item_id)

    def _dict_clear(self) -> None:
        if hasattr(self, "_dict_term_var"):
            self._dict_term_var.set("")
        if hasattr(self, "_dict_def_var"):
            self._dict_def_var.set("")
        if hasattr(self, "_dict_aliases_var"):
            self._dict_aliases_var.set("")
        self._dict_selected_term = None
        if hasattr(self, "_dict_tree") and self._dict_tree:
            for item in self._dict_tree.selection():
                self._dict_tree.selection_remove(item)

    def _dict_delete(self) -> None:
        term = getattr(self, "_dict_selected_term", None) or (
            self._dict_term_var.get().strip() if hasattr(self, "_dict_term_var") else ""
        )
        if not term:
            return
        ok, message = self.ctl.delete_term(term)
        if ok:
            self._dict_clear()
            self.show("dictionary")
            if hasattr(self, "_dict_msg"):
                self._flash(self._dict_msg, message, theme.ACCENT)
        else:
            if hasattr(self, "_dict_msg"):
                self._flash(self._dict_msg, message, theme.DANGER)

    def dictionary_rows(self) -> List[str]:
        if not hasattr(self, "_dict_tree") or self._dict_tree is None:
            return []
        rows: List[str] = []
        for item_id in self._dict_tree.get_children():
            vals = self._dict_tree.item(item_id, "values")
            if vals:
                rows.append(str(vals[0]))
        return rows

    def save_term_clicked(self) -> Tuple[bool, str]:
        term = self._dict_term_var.get() if hasattr(self, "_dict_term_var") else ""
        definition = self._dict_def_var.get() if hasattr(self, "_dict_def_var") else ""
        aliases_raw = self._dict_aliases_var.get() if hasattr(self, "_dict_aliases_var") else ""
        aliases = [a.strip() for a in re.split(r"[,，、]", aliases_raw) if a.strip()]
        original = getattr(self, "_dict_selected_term", None)
        ok, message = self.ctl.save_term(term, definition, aliases, original=original)
        if ok:
            self._dict_selected_term = term.strip()
            self.show("dictionary")
            if hasattr(self, "_dict_msg"):
                self._flash(self._dict_msg, message, theme.ACCENT)
        else:
            if hasattr(self, "_dict_msg"):
                self._flash(self._dict_msg, message, theme.DANGER)
        return ok, message

    def _dictionary_page(self) -> None:
        view = self.ctl.dictionary()
        self._dict_entries = view.entries

        head = self._title("Dictionary")
        total = len(view.entries)
        custom = sum(1 for e in view.entries if not e.builtin)
        tk.Label(head, text=f"共 {total} 個詞（自訂 {custom}）", bg=PAGE_BG, fg=MUTED,
                 font=(FONT, 10)).pack(side="right")

        hint = tk.Label(
            self.body,
            text="領域常用詞。語音辨識、辨識後的校正和每個模式的 System prompt 都會用到；"
                 "存檔後下一次錄音和整理就生效。",
            bg=PAGE_BG, fg=MUTED, font=(FONT, 9), justify="left", anchor="w"
        )
        hint.pack(fill="x", padx=24)
        hint.bind("<Configure>", lambda e: hint.configure(wraplength=max(200, e.width - 4)))

        if view.error and not view.entries:
            err_box = tk.Frame(self.body, bg=PAGE_BG)
            err_box.pack(fill="both", expand=True, padx=24, pady=20)
            tk.Label(err_box, text=view.error, bg=PAGE_BG, fg=theme.DANGER,
                     font=(FONT, 11), justify="left", anchor="w").pack(anchor="w")
            return

        if view.error:
            tk.Label(self.body, text=view.error, bg=PAGE_BG, fg=theme.DANGER,
                     font=(FONT, 9), justify="left", anchor="w").pack(fill="x", padx=24, pady=(2, 0))

        bar = tk.Frame(self.body, bg=PAGE_BG)
        bar.pack(fill="x", padx=24, pady=(8, 4))
        tk.Label(bar, text="篩選", bg=PAGE_BG, fg=MUTED, font=(FONT, 10)).pack(side="left", padx=(0, 6))
        self._dict_filter_var = tk.StringVar(value=getattr(self, "_dict_filter_text", ""))
        filter_entry = ttk.Entry(bar, textvariable=self._dict_filter_var, font=(FONT, 10), width=24)
        filter_entry.pack(side="left")

        def on_filter(*_args: Any) -> None:
            self._dict_filter_text = self._dict_filter_var.get()
            self._populate_dictionary_tree()

        self._dict_filter_var.trace_add("write", on_filter)

        style = ttk.Style(self.top)
        style.configure("Dictionary.Treeview", font=(FONT, 10), foreground=theme.INK, rowheight=24)
        style.configure("Dictionary.Treeview.Heading", font=(FONT, 10, "bold"), foreground=theme.ACCENT)
        style.map("Dictionary.Treeview",
                  background=[("selected", theme.ACCENT_SOFT_HOVER)],
                  foreground=[("selected", theme.INK)])

        tree_frame = tk.Frame(self.body, bg=PAGE_BG)
        tree_frame.pack(fill="both", expand=True, padx=(24, 12), pady=(4, 8))
        columns = ("term", "definition", "aliases", "source")
        tree = ttk.Treeview(tree_frame, columns=columns, show="headings",
                            style="Dictionary.Treeview", selectmode="browse")
        tree.heading("term", text="詞")
        tree.heading("definition", text="說明")
        tree.heading("aliases", text="常被聽成")
        tree.heading("source", text="來源")
        # small base widths: the two wide columns take whatever room the window has
        tree.column("term", width=130, minwidth=80, stretch=False, anchor="w")
        tree.column("definition", width=200, minwidth=100, stretch=True, anchor="w")
        tree.column("aliases", width=140, minwidth=80, stretch=True, anchor="w")
        tree.column("source", width=52, minwidth=52, stretch=False, anchor="center")

        scroll = ttk.Scrollbar(tree_frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        tree.pack(side="left", fill="both", expand=True)
        self._dict_tree = tree

        edit_box = tk.Frame(self.body, bg=CARD_BG, padx=14, pady=10)
        edit_box.pack(fill="x", padx=24, pady=(0, 14))

        inputs = tk.Frame(edit_box, bg=CARD_BG)
        inputs.pack(fill="x", pady=(0, 8))
        inputs.columnconfigure(1, weight=1)
        self._dict_term_var = tk.StringVar()
        self._dict_def_var = tk.StringVar()
        self._dict_aliases_var = tk.StringVar()
        fields = (("詞", self._dict_term_var), ("說明", self._dict_def_var),
                  ("常被聽成（用逗號分開）", self._dict_aliases_var))
        for row, (caption, var) in enumerate(fields):
            tk.Label(inputs, text=caption, bg=CARD_BG, fg=TEXT, font=(FONT, 10), anchor="w").grid(
                row=row, column=0, sticky="w", padx=(0, 8), pady=2)
            ttk.Entry(inputs, textvariable=var, font=(FONT, 10)).grid(
                row=row, column=1, sticky="ew", pady=2)

        btn_row = tk.Frame(edit_box, bg=CARD_BG)
        btn_row.pack(fill="x")

        theme.button(btn_row, "儲存", self.save_term_clicked, kind="primary").pack(side="left")
        theme.button(btn_row, "刪除", self._dict_delete).pack(side="left", padx=6)
        theme.button(btn_row, "清空", self._dict_clear).pack(side="left")

        target_file = view.user_path if view.user_path.exists() else view.base_path
        theme.button(btn_row, "開啟檔案", lambda: open_path(target_file)).pack(side="left", padx=6)

        self._dict_msg = tk.Label(btn_row, text="", bg=CARD_BG, fg=MUTED, font=(FONT, 9))
        self._dict_msg.pack(side="left", padx=8)

        def on_tree_select(_event: Any = None) -> None:
            if not self._dict_tree:
                return
            selected = self._dict_tree.selection()
            if not selected:
                return
            item = self._dict_tree.item(selected[0])
            vals = item["values"]
            if vals and len(vals) >= 3:
                self._dict_term_var.set(str(vals[0]))
                self._dict_def_var.set(str(vals[1]))
                self._dict_aliases_var.set(str(vals[2]))
                self._dict_selected_term = str(vals[0])

        tree.bind("<<TreeviewSelect>>", on_tree_select)
        self._populate_dictionary_tree()

    # ---------------------------------------------------------- settings
    def save_settings_clicked(self) -> Tuple[bool, str]:
        """What the 儲存 button of the System setting page does."""
        v = self._settings_vars
        labels = {label: key for key, label in LANGUAGE_CHOICES}
        ok, message = self.ctl.save_settings(
            labels.get(v["language"].get(), ""), v["min_chunk_sec"].get(), v["silence_sec"].get(),
            v["max_chunk_sec"].get(), v["cache_dir"].get(), v["records_dir"].get(),
            stt_profile=self._settings_profiles.get(v["stt_profile"].get()))
        self._flash(self._settings_msg, message, theme.ACCENT if ok else theme.DANGER, seconds=6)
        return ok, message

    def save_editor_settings_clicked(self) -> Tuple[bool, str]:
        """What the 儲存工作區設定 button does."""
        v = self._editor_settings_vars
        ok, message = self.ctl.save_editor_settings(
            active_llm_profile=v["active_llm_profile"].get(),
            prompts_root=v["prompts_root"].get(),
            path_style=v["path_style"].get(),
            quote_paths_with_spaces=v["quote_paths_with_spaces"].get(),
            image_preview_on_hover=v["image_preview_on_hover"].get(),
            default_mode=v["default_mode"].get(),
            result_view=v["result_view"].get(),
            protect_paths=v["protect_paths"].get(),
            protect_code_blocks=v["protect_code_blocks"].get(),
            guard=v["guard"].get(),
            auto_rename_after_refine=v["auto_rename_after_refine"].get(),
            send_copy=v["send_copy"].get(),
            auto_paste_to_last_window=v["auto_paste_to_last_window"].get(),
            autosave_delay_ms=v["autosave_delay_ms"].get(),
        )
        self._flash(self._editor_settings_msg, message, theme.ACCENT if ok else theme.DANGER, seconds=6)
        return ok, message

    def test_llm_clicked(self) -> Tuple[bool, str]:
        """Test current LLM connection."""
        profile = self._editor_settings_vars["active_llm_profile"].get()
        if profile in getattr(self.ctl.cfg, "llm_profiles", {}):
            self.ctl.cfg.active_llm_profile = profile
        ok, message = self.ctl.test_llm_connection()
        self._flash(self._editor_settings_msg, message, theme.ACCENT if ok else theme.DANGER, seconds=6)
        return ok, message

    def _settings_page(self) -> None:
        self._title("System setting")

        container = tk.Frame(self.body, bg=PAGE_BG)
        container.pack(fill="both", expand=True)

        canvas = tk.Canvas(container, bg=PAGE_BG, highlightthickness=0)
        vscroll = ttk.Scrollbar(container, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vscroll.set)

        vscroll.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        content = tk.Frame(canvas, bg=PAGE_BG)
        win_id = canvas.create_window((0, 0), window=content, anchor="nw")

        def _on_configure(_e: Any = None) -> None:
            canvas.configure(scrollregion=canvas.bbox("all"))

        def _on_canvas_configure(e: Any) -> None:
            canvas.itemconfig(win_id, width=e.width)

        content.bind("<Configure>", _on_configure)
        canvas.bind("<Configure>", _on_canvas_configure)

        def _on_mousewheel(e: Any) -> None:
            if canvas.winfo_exists():
                canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")

        canvas.bind_all("<MouseWheel>", _on_mousewheel)

        # ----------------- Top Card: Sublime 工作區與 LLM 設定 -----------------
        if self._show_editor_settings:
            ed_view = self.ctl.editor_settings()

            head_frame = tk.Frame(content, bg=PAGE_BG)
            head_frame.pack(fill="x", padx=24, pady=(6, 2))
            tk.Label(head_frame, text="Sublime 工作區與 LLM 設定", bg=PAGE_BG, fg=TEXT,
                     font=(FONT, 11, "bold")).pack(side="left")

            ed_card = self._card(content)
            ed_card.pack(fill="x", padx=24, pady=(4, 12))
            ed_card.columnconfigure(1, weight=1)

            self._editor_settings_vars = {
                "active_llm_profile": tk.StringVar(value=ed_view.active_llm_profile),
                "prompts_root": tk.StringVar(value=ed_view.prompts_root),
                "path_style": tk.StringVar(value=ed_view.path_style),
                "quote_paths_with_spaces": tk.BooleanVar(value=ed_view.quote_paths_with_spaces),
                "image_preview_on_hover": tk.BooleanVar(value=ed_view.image_preview_on_hover),
                "default_mode": tk.StringVar(value=ed_view.default_mode),
                "result_view": tk.StringVar(value=ed_view.result_view),
                "protect_paths": tk.BooleanVar(value=ed_view.protect_paths),
                "protect_code_blocks": tk.BooleanVar(value=ed_view.protect_code_blocks),
                "guard": tk.BooleanVar(value=ed_view.guard),
                "auto_rename_after_refine": tk.BooleanVar(value=ed_view.auto_rename_after_refine),
                "send_copy": tk.StringVar(value=ed_view.send_copy),
                "auto_paste_to_last_window": tk.BooleanVar(value=ed_view.auto_paste_to_last_window),
                "autosave_delay_ms": tk.StringVar(value=str(ed_view.autosave_delay_ms)),
            }
            ev = self._editor_settings_vars
            ed_row = [0]

            def ed_line(caption: str, note: str = "") -> tk.Frame:
                tk.Label(ed_card, text=caption, bg=CARD_BG, fg=TEXT, font=(FONT, 10), anchor="w").grid(
                    row=ed_row[0], column=0, sticky="nw", padx=(0, 14), pady=(6, 0))
                cell = tk.Frame(ed_card, bg=CARD_BG)
                cell.grid(row=ed_row[0], column=1, sticky="ew", pady=(6, 0))
                if note:
                    tk.Label(ed_card, text=note, bg=CARD_BG, fg=MUTED, font=(FONT, 9), anchor="w",
                             justify="left", wraplength=470).grid(row=ed_row[0] + 1, column=1, sticky="w")
                ed_row[0] += 2
                return cell

            # 1. LLM Profile
            cell = ed_line("LLM Profile", "選擇使用的語言模型設定檔。")
            theme.combobox(cell, textvariable=ev["active_llm_profile"], width=24,
                           values=list(ed_view.llm_profiles)).pack(side="left")
            theme.button(cell, "測試連線", self.test_llm_clicked).pack(side="left", padx=8)

            # 2. Prompts Root
            cell = ed_line("Prompt 資料夾", "Sublime 工作區存放 prompt Markdown 筆記的根目錄。")
            def browse_prompts() -> None:
                from tkinter import filedialog
                chosen = filedialog.askdirectory(parent=self.top, initialdir=ev["prompts_root"].get() or ".")
                if chosen:
                    ev["prompts_root"].set(str(Path(chosen)))

            theme.button(cell, "開啟", lambda: open_path(Path(ev["prompts_root"].get()))).pack(side="right")
            theme.button(cell, "瀏覽…", browse_prompts).pack(side="right", padx=6)
            ttk.Entry(cell, textvariable=ev["prompts_root"], font=(FONT, 10)).pack(side="left", fill="x", expand=True)

            # 3. Path style & quote spaces
            cell = ed_line("貼上路徑格式", "貼入檔案路徑時採用的格式風格。")
            theme.combobox(cell, textvariable=ev["path_style"], width=16,
                           values=["windows", "forward", "wsl", "markdown"]).pack(side="left")
            tk.Checkbutton(cell, text="含空白路徑加引號", variable=ev["quote_paths_with_spaces"],
                           bg=CARD_BG, fg=TEXT, activebackground=CARD_BG, selectcolor=CARD_BG,
                           font=(FONT, 9)).pack(side="left", padx=(12, 0))
            tk.Checkbutton(cell, text="懸停圖片預覽", variable=ev["image_preview_on_hover"],
                           bg=CARD_BG, fg=TEXT, activebackground=CARD_BG, selectcolor=CARD_BG,
                           font=(FONT, 9)).pack(side="left", padx=(12, 0))

            # 4. Refine mode & result view
            cell = ed_line("整理設定", "預設整理模式與結果分割視窗方式。")
            tk.Label(cell, text="預設模式:", bg=CARD_BG, fg=MUTED, font=(FONT, 9)).pack(side="left")
            theme.combobox(cell, textvariable=ev["default_mode"], width=12,
                           values=["clean", "prompt", "slide", "summary", "transform"]).pack(side="left", padx=(4, 12))
            tk.Label(cell, text="顯示方式:", bg=CARD_BG, fg=MUTED, font=(FONT, 9)).pack(side="left")
            theme.combobox(cell, textvariable=ev["result_view"], width=14,
                           values=["split_right", "below", "replace"]).pack(side="left", padx=(4, 0))

            # 5. Protection & Guard
            cell = ed_line("保護與檢查", "防止 LLM 整理時誤改特殊內容或產生幻覺。")
            tk.Checkbutton(cell, text="保護路徑", variable=ev["protect_paths"],
                           bg=CARD_BG, fg=TEXT, activebackground=CARD_BG, selectcolor=CARD_BG,
                           font=(FONT, 9)).pack(side="left")
            tk.Checkbutton(cell, text="保護程式碼區塊", variable=ev["protect_code_blocks"],
                           bg=CARD_BG, fg=TEXT, activebackground=CARD_BG, selectcolor=CARD_BG,
                           font=(FONT, 9)).pack(side="left", padx=(10, 0))
            tk.Checkbutton(cell, text="防呆檢查", variable=ev["guard"],
                           bg=CARD_BG, fg=TEXT, activebackground=CARD_BG, selectcolor=CARD_BG,
                           font=(FONT, 9)).pack(side="left", padx=(10, 0))
            tk.Checkbutton(cell, text="首次整理自動改名", variable=ev["auto_rename_after_refine"],
                           bg=CARD_BG, fg=TEXT, activebackground=CARD_BG, selectcolor=CARD_BG,
                           font=(FONT, 9)).pack(side="left", padx=(10, 0))

            # 6. Send & Paste
            cell = ed_line("送出與貼回", "送出時複製的內容與是否自動貼回上一個使用中視窗。")
            tk.Label(cell, text="複製內容:", bg=CARD_BG, fg=MUTED, font=(FONT, 9)).pack(side="left")
            theme.combobox(cell, textvariable=ev["send_copy"], width=12,
                           values=["refined", "raw", "both"]).pack(side="left", padx=(4, 12))
            tk.Checkbutton(cell, text="自動貼回上一個視窗", variable=ev["auto_paste_to_last_window"],
                           bg=CARD_BG, fg=TEXT, activebackground=CARD_BG, selectcolor=CARD_BG,
                           font=(FONT, 9)).pack(side="left")

            # 7. Autosave delay
            cell = ed_line("自動存檔延遲", "編輯停止後多少毫秒自動存檔（至少 100 毫秒）。")
            ttk.Entry(cell, textvariable=ev["autosave_delay_ms"], font=(FONT, 10), width=8).pack(side="left")
            tk.Label(cell, text="毫秒", bg=CARD_BG, fg=TEXT, font=(FONT, 10)).pack(side="left", padx=6)

            # Editor settings save button bar
            ed_bar = tk.Frame(content, bg=PAGE_BG)
            ed_bar.pack(fill="x", padx=24, pady=(2, 14))
            theme.button(ed_bar, "儲存工作區設定", self.save_editor_settings_clicked, kind="primary").pack(side="left")
            self._editor_settings_msg = tk.Label(ed_bar, text="", bg=PAGE_BG, fg=MUTED, font=(FONT, 9),
                                                 justify="left", anchor="w")
            self._editor_settings_msg.pack(side="left", padx=10)

        # ----------------- Bottom Card: STT 語音設定 -----------------
        view = self.ctl.settings()
        stt_head = tk.Frame(content, bg=PAGE_BG)
        stt_head.pack(fill="x", padx=24, pady=(6, 2))
        tk.Label(stt_head, text="語音輸入與辨識設定", bg=PAGE_BG, fg=TEXT,
                 font=(FONT, 11, "bold")).pack(side="left")

        hint = tk.Label(content, bg=PAGE_BG, fg=MUTED, font=(FONT, 9), justify="left", anchor="w",
                        text="語言和秒數存檔後，下一次錄音就開始用。辨識模型和資料夾要重開程式才會換，"
                             "資料夾裡原本的檔案不會搬過去。")
        hint.pack(fill="x", padx=24)
        hint.bind("<Configure>", lambda e: hint.configure(wraplength=max(200, e.width - 4)))

        card = self._card(content)
        card.pack(fill="x", padx=24, pady=(6, 8))
        card.columnconfigure(1, weight=1)
        fmt = lambda x: f"{x:g}"
        self._settings_vars = {
            "stt_profile": tk.StringVar(value=dict(view.stt_profiles).get(view.stt_profile, "")),
            "language": tk.StringVar(value=dict(LANGUAGE_CHOICES)[view.language]),
            "min_chunk_sec": tk.StringVar(value=fmt(view.min_chunk_sec)),
            "silence_sec": tk.StringVar(value=fmt(view.silence_sec)),
            "max_chunk_sec": tk.StringVar(value=fmt(view.max_chunk_sec)),
            "cache_dir": tk.StringVar(value=view.cache_dir),
            "records_dir": tk.StringVar(value=view.records_dir),
        }
        v = self._settings_vars
        row = [0]

        def line(caption: str, note: str = "") -> tk.Frame:
            """One setting: caption on the left, a frame for its widgets, a note under it."""
            tk.Label(card, text=caption, bg=CARD_BG, fg=TEXT, font=(FONT, 10), anchor="w").grid(
                row=row[0], column=0, sticky="nw", padx=(0, 14), pady=(6, 0))
            cell = tk.Frame(card, bg=CARD_BG)
            cell.grid(row=row[0], column=1, sticky="ew", pady=(6, 0))
            if note:
                tk.Label(card, text=note, bg=CARD_BG, fg=MUTED, font=(FONT, 9), anchor="w",
                         justify="left", wraplength=470).grid(row=row[0] + 1, column=1, sticky="w")
            row[0] += 2
            return cell

        self._settings_profiles = {label: name for name, label in view.stt_profiles}
        cell = line("辨識模型", "模型越大，同音字和專有名詞越準，但越慢。換了之後要重開程式。")
        theme.combobox(cell, textvariable=v["stt_profile"], width=34,
                       values=[label for _name, label in view.stt_profiles]).pack(side="left")

        cell = line("辨識語言", "自動會先花時間判斷語言；固定成中文或英文比較快，但另一種語言的整段話會變差。")
        theme.combobox(cell, textvariable=v["language"], width=22,
                       values=[label for _key, label in LANGUAGE_CHOICES]).pack(side="left")

        def seconds(caption: str, key: str, note: str) -> None:
            cell = line(caption, note)
            ttk.Entry(cell, textvariable=v[key], font=(FONT, 10), width=8).pack(side="left")
            tk.Label(cell, text="秒", bg=CARD_BG, fg=TEXT, font=(FONT, 10)).pack(side="left", padx=6)

        seconds("每段至少", "min_chunk_sec", "講滿這麼久之後，遇到停頓就送去辨識。調小，字比較快出現，但太短容易聽錯。")
        seconds("停頓", "silence_sec", "安靜這麼久才算一次停頓。")
        seconds("每段最長", "max_chunk_sec", "一直沒有停頓時，到這裡強制切開。")

        def folder(caption: str, key: str, note: str) -> None:
            cell = line(caption, note)

            def browse() -> None:
                from tkinter import filedialog

                chosen = filedialog.askdirectory(parent=self.top, initialdir=v[key].get() or ".")
                if chosen:
                    v[key].set(str(Path(chosen)))

            theme.button(cell, "開啟", lambda: open_path(Path(v[key].get()))).pack(side="right")
            theme.button(cell, "瀏覽…", browse).pack(side="right", padx=6)
            ttk.Entry(cell, textvariable=v[key], font=(FONT, 10)).pack(side="left", fill="x", expand=True)

        folder("暫存檔資料夾", "cache_dir", "錄音暫存、草稿自動保存、prompt 和詞庫的備份。")
        folder("歷史紀錄資料夾", "records_dir", "按「完成」存下來的每一筆口述，也是 Home 和 History 讀的地方。")

        bar = tk.Frame(content, bg=PAGE_BG)
        bar.pack(fill="x", padx=24, pady=(4, 20))
        theme.button(bar, "儲存", self.save_settings_clicked, kind="primary").pack(side="left")
        if view.path is not None:
            where = tk.Label(bar, text=f"存在 {view.path}", bg=PAGE_BG, fg=MUTED, font=(FONT, 9))
            where.pack(side="right")
        self._settings_msg = tk.Label(bar, text="", bg=PAGE_BG, fg=MUTED, font=(FONT, 9),
                                      justify="left", anchor="w")
        self._settings_msg.pack(side="left", padx=10)

    # ------------------------------------------------------------ prompt
    def _keep_prompt_edit(self) -> None:
        """Remember what is in the editor, so browsing away does not lose unloaded edits."""
        box = self._prompt_box
        if box is None or self._prompt_preview:
            return
        try:
            edited = box.get("1.0", "end-1c")
        except tk.TclError:
            return
        if edited.strip("\n") != self.ctl.prompt_source(self._prompt_mode).strip("\n"):
            self._prompt_edits[self._prompt_mode] = edited
        else:
            self._prompt_edits.pop(self._prompt_mode, None)

    def prompt_editor_text(self) -> str:
        return self._prompt_box.get("1.0", "end-1c") if self._prompt_box is not None else ""

    def load_prompt_clicked(self) -> Tuple[bool, str]:
        """"載入": save the editor's text as this mode's prompt file."""
        ok, message = self.ctl.save_prompt(self._prompt_mode, self.prompt_editor_text())
        if ok:
            self._prompt_edits.pop(self._prompt_mode, None)
        return ok, message

    def _prompt_page(self) -> None:
        if self._mode_getter is not None and not self._prompt_mode_chosen:
            try:
                mode = self._mode_getter()
            except Exception:
                mode = None
            if mode in PROMPT_MODES:
                self._prompt_mode = mode
        mode = self._prompt_mode
        preview = self._prompt_preview

        head = self._title("System prompt")
        labels = {MODE_LABELS[m.value]: m for m in PROMPT_MODES}
        var = tk.StringVar(value=MODE_LABELS[mode.value])
        box = theme.combobox(head, textvariable=var, width=8, values=list(labels))
        box.pack(side="right")
        tk.Label(head, text="模式", bg=PAGE_BG, fg=MUTED, font=(FONT, 10)).pack(side="right", padx=6)

        def changed(_e: object = None) -> None:
            self._keep_prompt_edit()
            self._prompt_box = None
            self._prompt_mode = labels[var.get()]
            self._prompt_mode_chosen = True
            self.show("prompt")

        box.bind("<<ComboboxSelected>>", changed)

        note_text = ("預覽：實際送給 LLM 的完整內容（prompt 檔＋教學規則＋詞庫），這裡不能改。"
                     if preview else
                     "直接在下面修改，按「載入」就存檔上線，下一次整理開始使用。"
                     "請保留{glossary}，詞庫會放進這個位置。")
        note = tk.Label(self.body, text=note_text, bg=PAGE_BG, fg=MUTED, font=(FONT, 9),
                        justify="left", anchor="w")
        note.pack(fill="x", padx=24)
        note.bind("<Configure>", lambda e: note.configure(wraplength=max(200, e.width - 4)))
        link = tk.Label(self.body, text="{glossary} = Dictionary 的內容 →", bg=PAGE_BG,
                        fg=theme.ACCENT, font=(FONT, 9), cursor="hand2", anchor="w")
        link.pack(fill="x", padx=24)
        link.bind("<Button-1>", lambda _e: self.show("dictionary"))

        view = self.ctl.prompt(mode)
        bar = tk.Frame(self.body, bg=PAGE_BG)
        bar.pack(fill="x", side="bottom", padx=24, pady=(6, 14))
        msg = tk.Label(self.body, text="", bg=PAGE_BG, fg=MUTED, font=(FONT, 9), anchor="w",
                       justify="left")
        msg.pack(fill="x", side="bottom", padx=24)
        msg.bind("<Configure>", lambda e: msg.configure(wraplength=max(200, e.width - 4)))

        source = f"檔案：{view.base_path}"
        source += (f"\n教學規則：{view.user_path}" if view.has_user_rules
                   else "\n教學規則：這個模式目前沒有")
        tk.Label(self.body, text=source, bg=PAGE_BG, fg=MUTED, font=(FONT, 9), justify="left",
                 anchor="w").pack(fill="x", side="bottom", padx=24)

        frame = tk.Frame(self.body, bg=PAGE_BG)
        frame.pack(fill="both", expand=True, padx=(24, 12), pady=(8, 4))
        # "char": a prompt is mostly Chinese with no spaces, where word wrapping breaks badly
        text = theme.text_box(frame, bg=CARD_BG if preview else PAGE_BG, size=10, wrap="char",
                              undo=True)
        scroll = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        text.pack(side="left", fill="both", expand=True)

        if preview:
            text.insert("end", view.error or view.text)
            text.configure(state="disabled")
        else:
            saved = self.ctl.prompt_source(mode)
            text.insert("end", self._prompt_edits.get(mode, saved).strip("\n"))
            text.edit_reset()
            self._prompt_box = text
            if mode in self._prompt_edits:
                msg.configure(text="有還沒載入的修改", fg=theme.WARN)
            elif view.error:
                msg.configure(text=view.error, fg=theme.DANGER)

        def load() -> None:
            ok, message = self.load_prompt_clicked()
            self._flash(msg, message, theme.ACCENT if ok else theme.DANGER, 6.0)

        def revert() -> None:
            self._prompt_edits.pop(mode, None)
            self._prompt_box = None
            self.show("prompt")

        def toggle_preview() -> None:
            self._keep_prompt_edit()
            self._prompt_box = None
            self._prompt_preview = not preview
            self.show("prompt")

        def copy() -> None:
            self._copy(text.get("1.0", "end-1c"))
            self._flash(msg, "已複製")

        if not preview:
            theme.button(bar, "載入（存檔上線）", load, kind="primary").pack(side="left")
            theme.button(bar, "還原成檔案內容", revert).pack(side="left", padx=6)
        theme.button(bar, "回到編輯" if preview else "預覽完整內容", toggle_preview).pack(side="left")
        theme.button(bar, "複製", copy).pack(side="left", padx=6)
