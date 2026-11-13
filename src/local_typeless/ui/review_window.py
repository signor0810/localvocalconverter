"""Review window (Tk Toplevel). Logic lives in ui.review_controller.ReviewController.

Background work runs in daemon threads; the Tk layer calls `poll()`.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Any, Dict, List, Optional

from local_typeless.review.advisor import Answer, Question
from local_typeless.ui.review_controller import ReviewController, ReviewView

FONT = "Microsoft JhengHei UI"
TICK_MS = 250

KIND_LABELS = {
    "unsure": "疑慮 [?]",
    "variant": "拼寫變體",
    "corrected": "辨識修正",
}


class ReviewWindow:
    """Toplevel window for reviewing dictation records and updating glossary."""

    def __init__(self, master: tk.Misc, ctl: ReviewController) -> None:
        self.ctl = ctl
        self.top = tk.Toplevel(master)
        self.top.title("檢查口述紀錄")
        self.top.geometry("720x760")
        self._alive = True
        self._selected_idx: Optional[int] = None
        self._last_questions_sig: Any = None
        self._cached_questions: List[Question] = []
        self._cached_answers: Dict[int, Answer] = {}

        self._build()
        self.top.protocol("WM_DELETE_WINDOW", self.close)
        self.top.after(TICK_MS, self._tick)

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

    # ----------------------------------------------------------------- UI
    def _build(self) -> None:
        t = self.top
        pad = {"padx": 8, "pady": 4}

        # Top control bar
        top_bar = ttk.Frame(t)
        top_bar.pack(fill="x", **pad)

        self.scan_btn = ttk.Button(top_bar, text="開始檢查", command=self._start_scan)
        self.scan_btn.pack(side="left")

        self.recheck_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            top_bar, text="重新檢查全部紀錄", variable=self.recheck_var
        ).pack(side="left", padx=8)

        self.status_lbl = ttk.Label(top_bar, text="", foreground="#333333")
        self.status_lbl.pack(side="left", padx=8)

        self.progress = ttk.Progressbar(t, mode="indeterminate")

        # Questions List (Treeview)
        ttk.Label(t, text="發現的詞彙問題（點選檢視與回答）：").pack(anchor="w", padx=8, pady=(4, 0))
        list_frame = ttk.Frame(t)
        list_frame.pack(fill="both", expand=True, **pad)

        self.tree = ttk.Treeview(
            list_frame,
            columns=("num", "term", "kind", "count", "choice"),
            show="headings",
            height=6,
            selectmode="browse",
        )
        self.tree.heading("num", text="#")
        self.tree.column("num", width=30, stretch=False, anchor="center")
        self.tree.heading("term", text="詞彙")
        self.tree.column("term", width=160)
        self.tree.heading("kind", text="種類")
        self.tree.column("kind", width=90, stretch=False)
        self.tree.heading("count", text="次數")
        self.tree.column("count", width=50, stretch=False, anchor="center")
        self.tree.heading("choice", text="目前選擇")
        self.tree.column("choice", width=260)

        tree_scroll = ttk.Scrollbar(list_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=tree_scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        tree_scroll.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)

        # Question Detail Frame
        self.detail_frame = ttk.LabelFrame(t, text="問題細節與選項")
        self.detail_frame.pack(fill="both", expand=True, **pad)

        self.q_title_lbl = ttk.Label(
            self.detail_frame, text="請先點擊上方列表中的項目", font=(FONT, 10, "bold")
        )
        self.q_title_lbl.pack(anchor="w", padx=8, pady=4)

        ttk.Label(self.detail_frame, text="上下文片段：").pack(anchor="w", padx=8)
        self.ctx_box = tk.Text(
            self.detail_frame, height=3, wrap="word", state="disabled", font=(FONT, 9)
        )
        self.ctx_box.pack(fill="x", padx=8, pady=2)

        ttk.Label(self.detail_frame, text="請選擇正確的處理方式：").pack(anchor="w", padx=8, pady=(4, 2))

        self.options_frame = ttk.Frame(self.detail_frame)
        self.options_frame.pack(fill="x", padx=8, pady=2)

        self.choice_var = tk.StringVar(value="skip")
        self.custom_entry_var = tk.StringVar(value="")

        # Custom entry frame (shown inside options_frame)
        self.custom_entry = ttk.Entry(self.options_frame, textvariable=self.custom_entry_var, width=24)
        self.custom_entry.bind("<KeyRelease>", self._on_custom_typed)

        # Bottom actions bar
        bottom_bar = ttk.Frame(t)
        bottom_bar.pack(fill="x", **pad)

        self.apply_btn = ttk.Button(bottom_bar, text="套用設定到詞庫", command=self.ctl.apply)
        self.apply_btn.pack(side="left")

        self.revert_btn = ttk.Button(bottom_bar, text="復原上一次套用", command=self.ctl.revert)
        self.revert_btn.pack(side="left", padx=8)

        ttk.Button(bottom_bar, text="關閉", command=self.close).pack(side="right")

        self.msg_lbl = ttk.Label(t, text="", foreground="#0055aa")
        self.msg_lbl.pack(fill="x", padx=8, pady=(0, 4))

    # ------------------------------------------------------------- events
    def _start_scan(self) -> None:
        self.ctl.scan(recheck_all=self.recheck_var.get())

    def _on_tree_select(self, _evt: Any = None) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        iid = sel[0]
        try:
            idx = int(iid)
            self._show_question(idx)
        except ValueError:
            pass

    def _show_question(self, idx: int) -> None:
        self._selected_idx = idx
        if idx >= len(self._cached_questions):
            return

        q = self._cached_questions[idx]
        ans = self._cached_answers.get(idx, Answer(choice="skip"))

        # Update title & contexts
        cand = q.candidate
        kind_name = KIND_LABELS.get(cand.kind, cand.kind)
        self.q_title_lbl.config(
            text=f"{q.question_text}  [{kind_name}]"
        )

        self.ctx_box.config(state="normal")
        self.ctx_box.delete("1.0", "end")
        if q.contexts:
            self.ctx_box.insert("1.0", "\n\n".join(q.contexts))
        else:
            self.ctx_box.insert("1.0", "（無上下文）")
        self.ctx_box.config(state="disabled")

        # Clear existing option widgets
        for w in self.options_frame.winfo_children():
            w.pack_forget()

        # Set variable
        self.choice_var.set(ans.choice)
        self.custom_entry_var.set(ans.custom_text)

        # Build radiobuttons for options
        for opt in q.options:
            label_text = opt
            if opt == q.suggested_term and q.definition:
                label_text = f"{opt}  （建議，{q.definition}）"
            elif opt == q.suggested_term:
                label_text = f"{opt}  （建議）"

            rb = ttk.Radiobutton(
                self.options_frame,
                text=label_text,
                value=opt,
                variable=self.choice_var,
                command=self._on_choice_changed,
            )
            rb.pack(anchor="w", pady=1)

        # Custom entry option
        custom_row = ttk.Frame(self.options_frame)
        custom_row.pack(anchor="w", pady=1)
        ttk.Radiobutton(
            custom_row,
            text="自行輸入：",
            value="custom",
            variable=self.choice_var,
            command=self._on_choice_changed,
        ).pack(side="left")
        self.custom_entry.pack(side="left", padx=4)

        # Not proper noun option
        ttk.Radiobutton(
            self.options_frame,
            text="這不是專有名詞（加入忽略清單，不再詢問）",
            value="not_proper_noun",
            variable=self.choice_var,
            command=self._on_choice_changed,
        ).pack(anchor="w", pady=1)

        # Skip option
        ttk.Radiobutton(
            self.options_frame,
            text="略過（本次不處理；按「重新檢查全部紀錄」時會再出現）",
            value="skip",
            variable=self.choice_var,
            command=self._on_choice_changed,
        ).pack(anchor="w", pady=1)

    def _on_choice_changed(self) -> None:
        if self._selected_idx is None:
            return
        choice = self.choice_var.get()
        custom = self.custom_entry_var.get()
        self.ctl.set_answer(self._selected_idx, choice, custom)
        self._update_tree_choice(self._selected_idx, choice, custom)

    def _on_custom_typed(self, _evt: Any = None) -> None:
        if self._selected_idx is None:
            return
        self.choice_var.set("custom")
        custom = self.custom_entry_var.get()
        self.ctl.set_answer(self._selected_idx, "custom", custom)
        self._update_tree_choice(self._selected_idx, "custom", custom)

    def _update_tree_choice(self, idx: int, choice: str, custom: str) -> None:
        iid = str(idx)
        if not self.tree.exists(iid):
            return
        label = choice
        if choice == "custom":
            label = f"自行輸入: {custom}" if custom else "自行輸入（未填）"
        elif choice == "not_proper_noun":
            label = "非專有名詞（忽略）"
        elif choice == "skip":
            label = "略過"
        self.tree.set(iid, "choice", label)

    # ------------------------------------------------------------- poll tick
    def _tick(self) -> None:
        if not self._alive:
            return
        try:
            view = self.ctl.poll()
            if view is not None:
                self._render(view)
        except Exception:
            pass
        finally:
            if self._alive:
                self.top.after(TICK_MS, self._tick)

    def _render(self, view: ReviewView) -> None:
        busy = view.phase in ("scanning", "applying")
        if busy:
            self.scan_btn.config(state="disabled")
            self.apply_btn.config(state="disabled")
            self.progress.pack(fill="x", padx=8, pady=2, before=self.tree.master)
            self.progress.start(10)
        else:
            self.scan_btn.config(state="normal")
            self.apply_btn.config(
                state="normal" if view.questions and view.phase == "ready" else "disabled"
            )
            self.progress.stop()
            self.progress.pack_forget()

        self.revert_btn.config(state="normal" if view.can_revert else "disabled")
        self.status_lbl.config(text=view.message)
        self.msg_lbl.config(text=view.message if view.phase == "done" else "")

        # Check if questions list changed
        sig = (len(view.questions), [q.candidate.term for q in view.questions])
        if sig != self._last_questions_sig:
            self._last_questions_sig = sig
            self._cached_questions = list(view.questions)
            self._cached_answers = dict(view.answers)
            self.tree.delete(*self.tree.get_children())
            for idx, q in enumerate(view.questions):
                ans = view.answers.get(idx, Answer(choice="skip"))
                choice_lbl = ans.choice
                if ans.choice == "not_proper_noun":
                    choice_lbl = "非專有名詞（忽略）"
                elif ans.choice == "skip":
                    choice_lbl = "略過"
                elif ans.choice == "custom":
                    choice_lbl = f"自行輸入: {ans.custom_text}" if ans.custom_text else "自行輸入"

                self.tree.insert(
                    "",
                    "end",
                    iid=str(idx),
                    values=(
                        idx + 1,
                        q.candidate.term,
                        KIND_LABELS.get(q.candidate.kind, q.candidate.kind),
                        q.candidate.count,
                        choice_lbl,
                    ),
                )
            if view.questions:
                self.tree.selection_set("0")
                self._show_question(0)
            else:
                self._selected_idx = None
                self.q_title_lbl.config(text="沒有需要檢查的項目")
                self.ctx_box.config(state="normal")
                self.ctx_box.delete("1.0", "end")
                self.ctx_box.config(state="disabled")
                for w in self.options_frame.winfo_children():
                    w.pack_forget()
