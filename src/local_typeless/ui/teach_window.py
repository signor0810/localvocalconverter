"""Hidden teach window (Tk Toplevel). Logic lives in ui.teach_controller.TeachController.

Separate focusable window: the main panel is WS_EX_NOACTIVATE and can't host a text box.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import simpledialog, ttk
from typing import Any, Optional

from local_typeless.types import Mode
from local_typeless.ui.controller import MODE_CHOICES, mode_label
from local_typeless.ui.teach_controller import TeachController, TeachView

FONT = "Microsoft JhengHei UI"
TICK_MS = 250
GROUPS = (("term", "詞庫新詞"), ("alias", "聽錯對照"), ("rule", "整理規則"))
PHASE_TEXT = {"analyzing": "分析中…", "applying": "套用並評分中…"}


class TeachWindow:
    def __init__(self, master: tk.Misc, ctl: TeachController, get_selection) -> None:
        """get_selection() -> (raw_text, output_text, Mode|None) of the panel's selected history."""
        self.ctl = ctl
        self._get_selection = get_selection
        self.top = tk.Toplevel(master)
        self.top.title("教學模式")
        self.top.geometry("640x760")
        self._alive = True
        self._last_items_sig: Any = None
        self._iid_to_index: dict = {}
        self._build()
        self.refresh_selection()
        self.top.protocol("WM_DELETE_WINDOW", self.close)
        self.top.after(TICK_MS, self._tick)

    # ----------------------------------------------------------------- UI
    def _build(self) -> None:
        t = self.top
        pad = {"padx": 8, "pady": 3}
        head = ttk.Frame(t)
        head.pack(fill="x", **pad)
        ttk.Label(head, text="模式").pack(side="left")
        self.mode_var = tk.StringVar()
        self.mode_box = ttk.Combobox(head, textvariable=self.mode_var, state="readonly", width=8,
                                     values=[l for l, _ in MODE_CHOICES])
        self.mode_box.pack(side="left", padx=6)
        self.mode_box.bind("<<ComboboxSelected>>", self._mode_changed)
        ttk.Button(head, text="用目前這筆", command=self.refresh_selection).pack(side="left")

        ttk.Label(t, text="原文（唯讀）").pack(anchor="w", padx=8)
        self.raw_box = tk.Text(t, height=4, wrap="word", state="disabled", font=(FONT, 10))
        self.raw_box.pack(fill="x", **pad)
        ttk.Label(t, text="目前結果（唯讀）").pack(anchor="w", padx=8)
        self.out_box = tk.Text(t, height=4, wrap="word", state="disabled", font=(FONT, 10))
        self.out_box.pack(fill="x", **pad)

        ttk.Label(t, text="正確答案（貼上你真正想要的結果）").pack(anchor="w", padx=8)
        self.exp_box = tk.Text(t, height=5, wrap="word", font=(FONT, 10))
        self.exp_box.pack(fill="x", **pad)
        row = ttk.Frame(t)
        row.pack(fill="x", **pad)
        ttk.Button(row, text="加入樣本", command=self._add).pack(side="left")
        self.count_lbl = ttk.Label(row, text="")
        self.count_lbl.pack(side="left", padx=8)
        ttk.Button(row, text="刪除選取樣本", command=self._delete).pack(side="right")

        self.sample_list = tk.Listbox(t, height=4, exportselection=False, activestyle="none")
        self.sample_list.pack(fill="x", **pad)

        row2 = ttk.Frame(t)
        row2.pack(fill="x", **pad)
        self.analyze_btn = ttk.Button(row2, text="分析並提出建議", command=self.ctl.analyze)
        self.analyze_btn.pack(side="left")
        self.status_lbl = ttk.Label(row2, text="")
        self.status_lbl.pack(side="left", padx=8)

        ttk.Label(t, text="建議（點方框勾選／取消，雙擊修改文字）").pack(anchor="w", padx=8)
        self.tree = ttk.Treeview(t, columns=("chk", "text", "src"), show="tree headings", height=9)
        self.tree.heading("#0", text="")
        self.tree.column("#0", width=90, stretch=False)
        self.tree.heading("chk", text="")
        self.tree.column("chk", width=30, stretch=False, anchor="center")
        self.tree.heading("text", text="內容")
        self.tree.column("text", width=400)
        self.tree.heading("src", text="來源")
        self.tree.column("src", width=60, stretch=False)
        self.tree.pack(fill="both", expand=True, **pad)
        self.tree.bind("<Button-1>", self._tree_click)
        self.tree.bind("<Double-1>", self._tree_edit)

        row3 = ttk.Frame(t)
        row3.pack(fill="x", **pad)
        self.apply_btn = ttk.Button(row3, text="套用勾選項目", command=self.ctl.apply_selected)
        self.apply_btn.pack(side="left")
        self.revert_btn = ttk.Button(row3, text="復原上一次套用", command=self.ctl.revert)
        self.revert_btn.pack(side="left", padx=6)
        self.score_lbl = ttk.Label(t, text="", justify="left", wraplength=600)
        self.score_lbl.pack(fill="x", **pad)

    # ------------------------------------------------------------- events
    def _mode_from_label(self) -> Optional[Mode]:
        for label, mode in MODE_CHOICES:
            if label == self.mode_var.get():
                return mode
        return None

    def _mode_changed(self, _e: Any = None) -> None:
        m = self._mode_from_label()
        if m:
            self.ctl.set_mode(m.value)

    def refresh_selection(self) -> None:
        raw, out, mode = self._get_selection()
        self.ctl.set_selection(raw, out, mode)

    def _add(self) -> None:
        if self.ctl.add_sample(self.exp_box.get("1.0", "end")):
            self.exp_box.delete("1.0", "end")

    def _delete(self) -> None:
        sel = self.sample_list.curselection()
        samples = [s for s in self.ctl.view.samples if s.mode == self.ctl.view.mode]
        if sel and sel[0] < len(samples):
            self.ctl.delete_sample(samples[sel[0]].id)

    def _tree_click(self, e: Any) -> None:
        if self.tree.identify_column(e.x) != "#1":
            return
        index = self._iid_to_index.get(self.tree.identify_row(e.y))
        if index is not None:
            self.ctl.set_item_selected(index, not self.ctl.view.items[index].selected)

    def _tree_edit(self, e: Any) -> None:
        index = self._iid_to_index.get(self.tree.identify_row(e.y))
        if index is None or self.tree.identify_column(e.x) == "#1":
            return
        it = self.ctl.view.items[index]
        if it.kind == "rule":
            new = simpledialog.askstring("修改規則", "規則：", initialvalue=it.key, parent=self.top)
            if new is not None:
                self.ctl.edit_item(index, key=new)
        else:
            label = "聽錯的字" if it.kind == "alias" else "術語"
            second = "正確詞" if it.kind == "alias" else "定義"
            k = simpledialog.askstring("修改", f"{label}：", initialvalue=it.key, parent=self.top)
            if k is None:
                return
            v = simpledialog.askstring("修改", f"{second}：", initialvalue=it.value, parent=self.top)
            self.ctl.edit_item(index, key=k, value=v if v is not None else it.value)

    # ------------------------------------------------------------- render
    @staticmethod
    def _set_ro(widget: tk.Text, value: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", value)
        widget.configure(state="disabled")

    @staticmethod
    def _item_text(it) -> str:
        if it.kind == "alias":
            return f"「{it.key}」→ {it.value}"
        if it.kind == "term":
            return f"{it.key}：{it.value}" if it.value else it.key
        return it.key

    def render(self, v: TeachView) -> None:
        self.mode_var.set(mode_label(Mode(v.mode)))
        self._set_ro(self.raw_box, v.raw_text)
        self._set_ro(self.out_box, v.output_text)
        self.count_lbl.configure(text=v.count_label)
        self.sample_list.delete(0, "end")
        for s in (x for x in v.samples if x.mode == v.mode):
            self.sample_list.insert("end", f"{s.created_at[5:16]}  {s.expected_text.replace(chr(10), ' ')[:40]}")
        busy = v.phase != "idle"
        self.status_lbl.configure(text=PHASE_TEXT.get(v.phase, "") or v.message)
        has = any(s.mode == v.mode for s in v.samples)
        self.analyze_btn.configure(state="normal" if (has and not busy) else "disabled")
        self.apply_btn.configure(
            state="normal" if (not busy and any(i.selected and i.key.strip() for i in v.items)) else "disabled")
        self.revert_btn.configure(state="normal" if (v.can_revert and not busy) else "disabled")

        sig = [(i.kind, i.key, i.value, i.source, i.selected) for i in v.items]
        if sig != self._last_items_sig:
            self._last_items_sig = sig
            self.tree.delete(*self.tree.get_children())
            self._iid_to_index = {}
            for kind, title in GROUPS:
                members = [(n, i) for n, i in enumerate(v.items) if i.kind == kind]
                if not members:
                    continue
                parent = self.tree.insert("", "end", text=title, open=True)
                for n, it in members:
                    iid = self.tree.insert(parent, "end", values=(
                        "☑" if it.selected else "☐", self._item_text(it), it.source))
                    self._iid_to_index[iid] = n
        lines = []
        by_id = {s.id: s for s in v.samples}
        for sid, b, a, _new in v.scores:
            s = by_id.get(sid)
            lines.append(f"{(s.expected_text[:18] if s else sid)}…  {b:.0%} → {a:.0%}")
        if v.avg_after is not None:
            lines.append(f"平均：{v.avg_before:.0%} → {v.avg_after:.0%}")
        self.score_lbl.configure(text="\n".join(lines) if lines else v.message)

    def _tick(self) -> None:
        if not self._alive:
            return
        try:
            v = self.ctl.poll()
            if v is not None:
                self.render(v)
        finally:
            self.top.after(TICK_MS, self._tick)

    def close(self) -> None:
        self._alive = False
        self.top.destroy()

    def lift(self) -> None:
        self.top.deiconify()
        self.top.lift()
        self.top.focus_force()
