"""Shared look of the draft window and the info panel: colours and a few widget helpers."""
from __future__ import annotations

import tkinter as tk
from typing import Any, Callable, Optional

FONT = "Microsoft JhengHei UI"

PAGE_BG = "#ffffff"
SIDEBAR_BG = "#f1f5f9"
SIDEBAR_SELECTED = "#dbeafe"
CARD_BG = "#f3f7ff"
BORDER = "#cbd5e1"
ACCENT = "#1d4ed8"          # titles, numbers, the main button
ACCENT_HOVER = "#1e40af"
ACCENT_SOFT = "#dbeafe"     # ordinary buttons
ACCENT_SOFT_HOVER = "#bfdbfe"
TEXT = "#1e3a8a"            # labels
INK = "#172554"             # long text the user reads or edits
MUTED = "#64748b"
DANGER = "#dc2626"
DANGER_HOVER = "#b91c1c"
WARN = "#b45309"
DISABLED_FG = "#94a3b8"
HEAT_COLORS = ("#e2e8f0", "#bfdbfe", "#60a5fa", "#1d4ed8")

# kind -> (background, hover background, text)
_BUTTON_KINDS = {
    "plain": (ACCENT_SOFT, ACCENT_SOFT_HOVER, ACCENT),
    "primary": (ACCENT, ACCENT_HOVER, "#ffffff"),
    "danger": (DANGER, DANGER_HOVER, "#ffffff"),
}


def restyle(btn: Any, kind: str) -> None:
    """Give an existing button another kind (e.g. the record button while recording)."""
    bg, hover, fg = _BUTTON_KINDS[kind]
    btn._kind_bg, btn._kind_hover = bg, hover
    btn.configure(bg=bg, fg=fg, activebackground=hover, activeforeground=fg)


def _hover(btn: Any, inside: bool) -> None:
    try:
        if str(btn["state"]) != "disabled":
            btn.configure(bg=btn._kind_hover if inside else btn._kind_bg)
    except tk.TclError:
        pass


def _flat(btn: Any, kind: str) -> Any:
    btn.configure(relief="flat", borderwidth=0, padx=10, pady=4, cursor="hand2",
                  font=(FONT, 10), disabledforeground=DISABLED_FG, highlightthickness=0)
    restyle(btn, kind)
    btn.bind("<Enter>", lambda _e: _hover(btn, True), add="+")
    btn.bind("<Leave>", lambda _e: _hover(btn, False), add="+")
    return btn


def button(parent: Any, text: str, command: Optional[Callable[[], None]] = None,
           kind: str = "plain", **kw: Any) -> tk.Button:
    return _flat(tk.Button(parent, text=text, command=command, **kw), kind)


def menubutton(parent: Any, text: str, kind: str = "plain") -> tk.Menubutton:
    return _flat(tk.Menubutton(parent, text=text), kind)


def combobox(parent: Any, **kw: Any) -> Any:
    """A read-only drop-down whose text is blue (the native box itself stays white)."""
    from tkinter import ttk

    style = ttk.Style(parent)
    style.configure("Blue.TCombobox", foreground=ACCENT)
    style.map("Blue.TCombobox", foreground=[("readonly", ACCENT)],
              selectforeground=[("readonly", ACCENT)], selectbackground=[("readonly", PAGE_BG)])
    box = ttk.Combobox(parent, state="readonly", style="Blue.TCombobox", font=(FONT, 10), **kw)
    return box


def text_box(parent: Any, *, bg: str = PAGE_BG, size: int = 11, **kw: Any) -> tk.Text:
    """A text area with a thin border that turns blue when it has the focus."""
    return tk.Text(parent, font=(FONT, size), bg=bg, fg=INK, insertbackground=ACCENT,
                   relief="flat", borderwidth=0, highlightthickness=1,
                   highlightbackground=BORDER, highlightcolor=ACCENT,
                   selectbackground=ACCENT_SOFT_HOVER, selectforeground=INK,
                   padx=8, pady=6, **kw)
