"""Info panel (Tk Toplevel): a sidebar with Home, History and System prompt pages.

Home and History only display. The System prompt page edits the prompt file of a mode;
"載入" saves it, and the next refine uses it. The data comes from
ui.info_controller.InfoController; a page is rebuilt from the files each time it is shown.
"""
from __future__ import annotations

import os
import tkinter as tk
from datetime import date, timedelta
from pathlib import Path
from tkinter import ttk
from typing import Callable, Dict, List, Optional, Tuple

from local_typeless.types import Mode
from local_typeless.ui import theme
from local_typeless.ui.info_controller import (
    MODE_LABELS,
    PROMPT_MODES,
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
    ("prompt", "System prompt"),
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
    ) -> None:
        self.ctl = ctl
        self._mode_getter = mode_getter
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
        {"home": self._home_page, "history": self._history_page, "prompt": self._prompt_page}[page]()

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
