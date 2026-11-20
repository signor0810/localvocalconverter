"""
Local Typeless Sublime Text Plugin.
Compatible with Python 3.8 (Sublime Build 4200) and Python 3.14 (Build 4215).
"""

import os
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import sublime
import sublime_plugin

try:
    from . import lt_core
except (ImportError, ValueError):
    import lt_core

EDITOR_CONFIG = dict(lt_core.DEFAULT_EDITOR_CONFIG)
SERVER_STATE = {
    "online": False,
    "llm_profile": "",
    "version": "",
}

VIEW_PHANTOM_SETS = {}  # type: Dict[int, sublime.PhantomSet]
VIEW_WARN_PHANTOM_SETS = {}  # type: Dict[int, sublime.PhantomSet]
VIEW_PHANTOM_OFFSETS = {}  # type: Dict[int, int]
VIEW_AUTOSAVE_SEQS = {}  # type: Dict[int, int]
LAST_SERVER_CHECK_TIME = 0.0

TOOLBAR_HTML = """
<body id="lt-toolbar">
  <style>
    body {
      margin: 0;
      padding: 3px 6px;
      font-size: 0.9em;
      border-bottom: 1px solid color(var(--accent) a(0.3));
    }
    a {
      text-decoration: none;
      color: var(--bluish);
      font-weight: bold;
    }
  </style>
  <a href="refine">✨ AI 整理</a>
</body>
"""


def _escape_html(text: Any) -> str:
    """Escapes &, <, > characters for safe minihtml rendering."""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _on_toolbar_navigate(view, href):
    if href == "refine":
        view.run_command("lt_refine")


def get_status_string():
    if SERVER_STATE["online"]:
        prof = SERVER_STATE["llm_profile"] or "線上"
        return "LT ● " + prof
    return "LT ○ 離線"


def update_view_status(view):
    if not view.is_valid():
        return
    if view.settings().get("lt_managed"):
        view.set_status("lt_status", get_status_string())
    else:
        view.erase_status("lt_status")


def update_view_phantoms(view):
    if not view.is_valid():
        return
    vid = view.id()
    fname = view.file_name()
    if not fname or not view.settings().get("lt_managed") or lt_core.is_refined_path(fname):
        if vid in VIEW_PHANTOM_SETS:
            VIEW_PHANTOM_SETS[vid].update([])
            VIEW_PHANTOM_OFFSETS.pop(vid, None)
        return

    full_text = view.substr(sublime.Region(0, view.size()))
    _, _, body_offset = lt_core.parse_frontmatter(full_text)

    # Avoid phantom flickering while typing: skip update if body_offset has not changed
    if vid in VIEW_PHANTOM_OFFSETS and VIEW_PHANTOM_OFFSETS[vid] == body_offset:
        return

    if vid not in VIEW_PHANTOM_SETS:
        VIEW_PHANTOM_SETS[vid] = sublime.PhantomSet(view, "lt_toolbar")
    pset = VIEW_PHANTOM_SETS[vid]

    region = sublime.Region(body_offset, body_offset)
    p = sublime.Phantom(
        region,
        TOOLBAR_HTML,
        sublime.LAYOUT_BLOCK,
        lambda href, v=view: _on_toolbar_navigate(v, href),
    )
    pset.update([p])
    VIEW_PHANTOM_OFFSETS[vid] = body_offset


def update_refined_phantoms(view, warnings):
    if not view.is_valid():
        return
    vid = view.id()
    if vid not in VIEW_WARN_PHANTOM_SETS:
        VIEW_WARN_PHANTOM_SETS[vid] = sublime.PhantomSet(view, "lt_warnings")
    pset = VIEW_WARN_PHANTOM_SETS[vid]

    if not warnings:
        pset.update([])
        return

    items = "".join("<li>" + _escape_html(w) + "</li>" for w in warnings)
    warn_html = (
        '<body id="lt-warnings"><style>'
        "body { margin: 0; padding: 4px 8px; background-color: color(var(--yellowish) a(0.15));"
        " border-left: 3px solid var(--yellowish); color: var(--foreground); }"
        "ul { margin: 2px 0; padding-left: 1.2rem; }"
        "</style><div>⚠️ <strong>AI 整理警告</strong>:</div>"
        "<ul>" + items + "</ul></body>"
    )
    p = sublime.Phantom(sublime.Region(0, 0), warn_html, sublime.LAYOUT_BLOCK)
    pset.update([p])


def refresh_server_and_views():
    ok, data, _ = lt_core.request_server("/health", method="GET", timeout=2.0)
    if ok and isinstance(data, dict) and data.get("ok"):
        SERVER_STATE["online"] = True
        SERVER_STATE["llm_profile"] = str(data.get("llm_profile", ""))
        SERVER_STATE["version"] = str(data.get("version", ""))
        ed = data.get("editor", {})
        if isinstance(ed, dict):
            for k, v in ed.items():
                EDITOR_CONFIG[k] = v
    else:
        SERVER_STATE["online"] = False
        SERVER_STATE["llm_profile"] = ""

    def update_all():
        prompts_root = EDITOR_CONFIG.get(
            "prompts_root", lt_core.DEFAULT_EDITOR_CONFIG["prompts_root"]
        )
        for win in sublime.windows():
            for v in win.views():
                fname = v.file_name()
                if fname and lt_core.is_managed_path(fname, prompts_root):
                    v.settings().set("lt_managed", True)
                    update_view_status(v)
                    update_view_phantoms(v)

    sublime.set_timeout(update_all, 0)


def maybe_check_server():
    global LAST_SERVER_CHECK_TIME
    if not SERVER_STATE["online"]:
        now = time.time()
        if now - LAST_SERVER_CHECK_TIME >= 2.0:
            if lt_core.read_serve_info() is not None:
                LAST_SERVER_CHECK_TIME = now
                sublime.set_timeout_async(refresh_server_and_views, 0)


def plugin_loaded():
    sublime.set_timeout_async(refresh_server_and_views, 100)


class LtNewPromptCommand(sublime_plugin.WindowCommand):
    def run(self):
        maybe_check_server()
        prompts_root = EDITOR_CONFIG.get(
            "prompts_root", lt_core.DEFAULT_EDITOR_CONFIG["prompts_root"]
        )
        file_layout = EDITOR_CONFIG.get(
            "file_layout", lt_core.DEFAULT_EDITOR_CONFIG["file_layout"]
        )
        target_path = lt_core.expand_file_layout(
            prompts_root, file_layout, title="untitled"
        )

        parent_dir = os.path.dirname(target_path)
        os.makedirs(parent_dir, exist_ok=True)

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        default_mode = EDITOR_CONFIG.get("default_mode", "clean")
        fields = {
            "created": now_str,
            "status": "draft",
            "mode": default_mode,
            "refined_at": "",
            "sent_at": "",
            "target": "",
        }
        field_order = EDITOR_CONFIG.get("frontmatter_fields")
        content = lt_core.render_frontmatter(fields, field_order=field_order)

        with open(target_path, "w", encoding="utf-8") as f:
            f.write(content)

        view = self.window.open_file(target_path)
        body_offset = len(content)

        def finish_setup(v, retries=30):
            if not v.is_valid():
                return
            if v.is_loading():
                if retries > 0:
                    sublime.set_timeout(lambda: finish_setup(v, retries - 1), 50)
                return
            v.settings().set("lt_managed", True)
            v.sel().clear()
            v.sel().add(sublime.Region(body_offset, body_offset))
            v.show_at_center(body_offset)
            update_view_status(v)
            update_view_phantoms(v)

        sublime.set_timeout(lambda: finish_setup(view), 50)


class LtEventListener(sublime_plugin.EventListener):
    def on_load(self, view):
        self._check_and_mark(view)

    def on_activated(self, view):
        maybe_check_server()
        self._check_and_mark(view)

    def on_post_save(self, view):
        self._check_and_mark(view)

    def on_close(self, view):
        vid = view.id()
        VIEW_PHANTOM_SETS.pop(vid, None)
        VIEW_WARN_PHANTOM_SETS.pop(vid, None)
        VIEW_PHANTOM_OFFSETS.pop(vid, None)
        VIEW_AUTOSAVE_SEQS.pop(vid, None)

    def _check_and_mark(self, view):
        fname = view.file_name()
        if not fname:
            return
        prompts_root = EDITOR_CONFIG.get(
            "prompts_root", lt_core.DEFAULT_EDITOR_CONFIG["prompts_root"]
        )
        if lt_core.is_managed_path(fname, prompts_root):
            view.settings().set("lt_managed", True)
            update_view_status(view)
            update_view_phantoms(view)

    def on_modified_async(self, view):
        if not view.settings().get("lt_managed"):
            return
        fname = view.file_name()
        if not fname:
            return

        sublime.set_timeout(lambda: update_view_phantoms(view), 0)

        vid = view.id()
        delay = int(EDITOR_CONFIG.get("autosave_delay_ms", 1000))
        VIEW_AUTOSAVE_SEQS[vid] = VIEW_AUTOSAVE_SEQS.get(vid, 0) + 1
        current_seq = VIEW_AUTOSAVE_SEQS[vid]

        def try_save(target_seq):
            if VIEW_AUTOSAVE_SEQS.get(vid) == target_seq:
                if view.is_valid() and view.is_dirty() and view.file_name():
                    view.run_command("save")

        sublime.set_timeout(lambda: try_save(current_seq), delay)

    def on_deactivated(self, view):
        if view.settings().get("lt_managed") and view.file_name() and view.is_dirty():
            view.run_command("save")


class LtSmartPasteCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        prompt_path = self.view.file_name() or ""
        sublime.set_timeout_async(lambda: self._async_capture(prompt_path), 0)

    def _async_capture(self, prompt_path):
        payload = {"prompt_path": prompt_path}
        ok, data, _ = lt_core.request_server(
            "/clipboard/capture",
            method="POST",
            payload=payload,
            timeout=1.5,
        )

        if ok and isinstance(data, dict):
            if not SERVER_STATE["online"]:
                sublime.set_timeout_async(refresh_server_and_views, 0)
            kind = data.get("kind", "text")
            paths = data.get("paths", [])
            if kind == "image" and paths:
                style = EDITOR_CONFIG.get("path_style", "windows")
                quote_spaces = bool(EDITOR_CONFIG.get("quote_paths_with_spaces", True))
                formatted = [
                    lt_core.format_path(p, style=style, quote_spaces=quote_spaces)
                    for p in paths
                ]
                text_to_insert = "\n".join(formatted)
                sublime.set_timeout(
                    lambda: self.view.run_command(
                        "lt_insert_text", {"text": text_to_insert}
                    ),
                    0,
                )
                return
            else:
                # Backend responded, but not image or paths empty; paste normally without false offline warning
                sublime.set_timeout(lambda: self.view.run_command("paste"), 0)
                return

        def fallback():
            self.view.set_status("lt_msg", "LT 後端離線，改用一般貼上")
            self.view.run_command("paste")
            sublime.set_timeout(lambda: self.view.erase_status("lt_msg"), 3000)

        sublime.set_timeout(fallback, 0)


class LtPasteRawCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        self.view.run_command("paste")


class LtRefineCommand(sublime_plugin.TextCommand):
    def run(self, edit, mode=None):
        sel_text = ""
        for r in self.view.sel():
            if not r.empty():
                sel_text = self.view.substr(r)
                break

        if sel_text.strip():
            text_to_refine = sel_text
        else:
            full_text = self.view.substr(sublime.Region(0, self.view.size()))
            _, body, _ = lt_core.parse_frontmatter(full_text)
            text_to_refine = body

        if not text_to_refine.strip():
            self.view.set_status("lt_msg", "內容為空，無法整理")
            sublime.set_timeout(lambda: self.view.erase_status("lt_msg"), 3000)
            return

        source_path = self.view.file_name()
        if not source_path:
            self.view.set_status("lt_msg", "請先儲存檔案再進行 AI 整理")
            sublime.set_timeout(lambda: self.view.erase_status("lt_msg"), 3000)
            return

        target_mode = mode or EDITOR_CONFIG.get("default_mode", "clean")
        self.view.set_status("lt_msg", "整理中…")

        win = self.view.window()
        sublime.set_timeout_async(
            lambda: self._async_refine(
                win, self.view, source_path, text_to_refine, target_mode
            ),
            0,
        )

    def _async_refine(self, win, source_view, source_path, text_to_refine, target_mode):
        payload = {
            "text": text_to_refine,
            "mode": target_mode,
            "protect": True,
        }
        ok, data, err = lt_core.request_server(
            "/refine",
            method="POST",
            payload=payload,
            timeout=120.0,
        )

        if ok and isinstance(data, dict) and data.get("ok"):
            if not SERVER_STATE["online"]:
                sublime.set_timeout_async(refresh_server_and_views, 0)
            result_text = data.get("text", "")
            warnings = data.get("warnings", [])
            sublime.set_timeout(
                lambda: self._apply_refine_success(
                    win, source_view, source_path, result_text, warnings
                ),
                0,
            )
        else:
            err_msg = ""
            if data and isinstance(data, dict):
                err_msg = data.get("error", "")
            if not err_msg:
                err_msg = err or "後端連線失敗"
            sublime.set_timeout(
                lambda: self._apply_refine_fail(source_view, err_msg),
                0,
            )

    def _apply_refine_fail(self, source_view, error_msg):
        if source_view.is_valid():
            source_view.set_status("lt_msg", "整理失敗: " + str(error_msg))
            sublime.set_timeout(lambda: source_view.erase_status("lt_msg"), 5000)

    def _apply_refine_success(
        self, win, source_view, source_path, result_text, warnings
    ):
        if source_view.is_valid():
            source_view.erase_status("lt_msg")

        if not win or not win.is_valid():
            win = sublime.active_window()
        if not win:
            return

        if win.num_groups() < 2:
            win.set_layout(
                {
                    "cols": [0.0, 0.5, 1.0],
                    "rows": [0.0, 1.0],
                    "cells": [[0, 0, 1, 1], [1, 0, 2, 1]],
                }
            )

        refined_path = lt_core.refined_path_for(source_path)
        if not os.path.exists(refined_path):
            with open(refined_path, "w", encoding="utf-8") as f:
                pass

        win.focus_group(1)
        right_view = win.open_file(refined_path)

        def apply_content(v, retries=30):
            if not v.is_valid():
                return
            if v.is_loading():
                if retries > 0:
                    sublime.set_timeout(lambda: apply_content(v, retries - 1), 50)
                return

            v.settings().set("lt_managed", True)
            v.run_command("lt_replace_all", {"text": result_text})
            v.run_command("save")

            if warnings:
                update_refined_phantoms(v, warnings)
                v.set_status(
                    "lt_warn", "⚠️ 警告: " + "; ".join(str(w) for w in warnings)
                )
            else:
                update_refined_phantoms(v, [])
                v.erase_status("lt_warn")

        sublime.set_timeout(lambda: apply_content(right_view), 50)


class LtInsertTextCommand(sublime_plugin.TextCommand):
    def run(self, edit, text=""):
        for r in self.view.sel():
            self.view.replace(edit, r, text)


class LtReplaceAllCommand(sublime_plugin.TextCommand):
    def run(self, edit, text=""):
        region = sublime.Region(0, self.view.size())
        self.view.replace(edit, region, text)