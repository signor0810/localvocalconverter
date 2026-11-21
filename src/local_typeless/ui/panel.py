"""Standalone UI panel runner (Settings / Info / Review / Teach) without starting STT."""
from __future__ import annotations

import logging
import sys
import tkinter as tk
from pathlib import Path
from typing import Any, Dict, Optional

from local_typeless.config.loader import load_config, resolve_config_path, settings_path
from local_typeless.llm import create_llm_client
from local_typeless.ui.info_controller import InfoController
from local_typeless.ui.info_window import InfoWindow

logger = logging.getLogger(__name__)


class _NoSTT:
    """Placeholder STT avoiding faster_whisper import."""

    def transcribe(self, *a, **k):
        raise RuntimeError("STT is not available in standalone panel")


def run_panel(
    config_path: Optional[Path] = None,
    page: str = "settings",
    overrides: Optional[Dict[str, Any]] = None,
    auto_close_sec: Optional[float] = None,
) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    try:
        cfg = load_config(config_path, overrides=overrides)
    except Exception as e:
        print(f"Config error: {e}", file=sys.stderr)
        return 2

    res_cfg_path = resolve_config_path(config_path)
    sett_file = settings_path(res_cfg_path)

    try:
        root = tk.Tk()
    except tk.TclError as e:
        print(f"Tk error (no display?): {e}", file=sys.stderr)
        return 1

    root.withdraw()

    def glossary_section() -> str:
        try:
            from local_typeless.glossary.manager import GlossaryManager
            gm = GlossaryManager.from_yaml(Path(cfg.glossary_path))
            return gm.build_llm_glossary_section()
        except Exception:
            return ""

    ctl = InfoController(
        cfg,
        glossary_section=glossary_section,
        settings_file=sett_file,
        config_path=res_cfg_path,
    )

    review_win: Optional[Any] = None
    teach_win: Optional[Any] = None
    info_win: Optional[InfoWindow] = None

    def close_all():
        try:
            root.destroy()
        except Exception:
            pass

    def open_review():
        nonlocal review_win
        if review_win is not None and getattr(review_win, "_alive", False):
            review_win.lift()
            return
        from local_typeless.ui.review_controller import ReviewController
        from local_typeless.ui.review_window import ReviewWindow

        rev_ctl = ReviewController(cfg, lambda: create_llm_client(cfg.llm))
        review_win = ReviewWindow(root, rev_ctl)
        review_win.lift()

    def open_teach():
        nonlocal teach_win
        if teach_win is not None and getattr(teach_win, "_alive", False):
            teach_win.lift()
            return
        from local_typeless.pipeline import Pipeline
        from local_typeless.ui.teach_controller import TeachController
        from local_typeless.ui.teach_window import TeachWindow

        def make_pipeline():
            return Pipeline(cfg, stt=_NoSTT(), outputs=[], records=False)

        tch_ctl = TeachController(cfg, lambda: create_llm_client(cfg.llm), make_pipeline)
        teach_win = TeachWindow(root, tch_ctl, lambda: ("", "", None))
        teach_win.lift()

    page_lower = (page or "settings").lower().strip()
    if page_lower == "review":
        open_review()
        if review_win is not None:
            review_win.top.protocol("WM_DELETE_WINDOW", close_all)
    elif page_lower == "teach":
        open_teach()
        if teach_win is not None:
            teach_win.top.protocol("WM_DELETE_WINDOW", close_all)
    else:
        info_win = InfoWindow(
            root,
            ctl,
            open_review=open_review,
            open_teach=open_teach,
            show_editor_settings=True,
        )
        info_win.top.protocol("WM_DELETE_WINDOW", close_all)
        info_win.show(page_lower)
        info_win.lift()

    if auto_close_sec is not None and auto_close_sec > 0:
        root.after(int(auto_close_sec * 1000), close_all)

    try:
        root.mainloop()
    except KeyboardInterrupt:
        close_all()
    return 0
