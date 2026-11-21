"""
Local Typeless Sublime Text Plugin.
Compatible with Python 3.8 (Sublime Build 4200) and Python 3.14 (Build 4215).
"""

import os
import subprocess
import sys
import threading
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
    "llm_model": "",
    "version": "",
    "config_path": "",
    "config_errors": [],
    "modes": ["clean", "prompt", "slide", "summary", "transform"],
}

VIEW_PHANTOM_SETS = {}  # type: Dict[int, sublime.PhantomSet]
VIEW_WARN_PHANTOM_SETS = {}  # type: Dict[int, sublime.PhantomSet]
VIEW_PHANTOM_OFFSETS = {}  # type: Dict[int, Any]
VIEW_AUTOSAVE_SEQS = {}  # type: Dict[int, int]

LAST_SERVER_CHECK_TIME = 0.0
SERVER_START_LOCK = threading.Lock()

MODE_NAMES = {
    "clean": "Clean（去除贅字校正）",
    "prompt": "Prompt（改寫為 AI 提示詞）",
    "summary": "Summary（重點摘要）",
    "slide": "Slide（單頁簡報架構）",
    "transform": "Transform（客製轉換）",
}


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
    elif href == "mode":
        modes = list(EDITOR_CONFIG.get("modes_in_palette", ["clean", "prompt", "slide", "summary", "transform"]))
        items = [MODE_NAMES.get(m, m) for m in modes]

        def on_mode_select(idx):
            if idx >= 0 and idx < len(modes):
                view.run_command("lt_refine", {"mode": modes[idx]})

        win = view.window() or sublime.active_window()
        if win:
            win.show_quick_panel(items, on_mode_select)
    elif href == "send":
        view.run_command("lt_send")
    elif href == "paste_prev":
        view.run_command("lt_paste_to_previous")
    elif href == "history":
        win = view.window() or sublime.active_window()
        if win:
            win.run_command("lt_history")
    elif href == "settings":
        win = view.window() or sublime.active_window()
        if win:
            win.run_command("lt_open_settings_gui", {"page": "settings"})
    elif href == "status":
        if not SERVER_STATE["online"]:
            view.set_status("lt_msg", "正在啟動後端服務…")
            sublime.set_timeout_async(lambda: ensure_server_online(wait_sec=4.0), 0)
        else:
            win = view.window() or sublime.active_window()
            if win:
                win.run_command("lt_check_status")


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


def _build_toolbar_html(default_mode: str, online: bool, profile: str) -> str:
    esc_mode = _escape_html(default_mode)
    esc_profile = _escape_html(profile or "線上")
    if online:
        status_part = f'<span style="color: var(--greenish); font-weight: bold;">● {esc_profile}</span>'
    else:
        status_part = '<a href="status" style="color: var(--yellowish);">○ 離線（點此啟動）</a>'

    return f"""
<body id="lt-toolbar">
  <style>
    body {{
      margin: 0;
      padding: 3px 6px;
      font-size: 0.9em;
      border-bottom: 1px solid color(var(--accent) a(0.3));
    }}
    a {{
      text-decoration: none;
      color: var(--bluish);
      font-weight: bold;
    }}
  </style>
  <a href="refine">✨ AI 整理</a> ｜
  <a href="mode">模式：{esc_mode} ▾</a> ｜
  <a href="send">📋 送出</a> ｜
  <a href="paste_prev">📤 貼回視窗</a> ｜
  <a href="history">🕘 歷史</a> ｜
  <a href="settings">⚙️ 設定</a> ｜
  {status_part}
</body>
"""


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

    def_mode = EDITOR_CONFIG.get("default_mode", "clean")
    toolbar_html = _build_toolbar_html(def_mode, SERVER_STATE["online"], SERVER_STATE["llm_profile"])
    cache_key = (body_offset, toolbar_html)

    # Avoid phantom flickering while typing: skip update if body_offset & HTML have not changed
    if vid in VIEW_PHANTOM_OFFSETS and VIEW_PHANTOM_OFFSETS[vid] == cache_key:
        return

    if vid not in VIEW_PHANTOM_SETS:
        VIEW_PHANTOM_SETS[vid] = sublime.PhantomSet(view, "lt_toolbar")
    pset = VIEW_PHANTOM_SETS[vid]

    region = sublime.Region(body_offset, body_offset)
    p = sublime.Phantom(
        region,
        toolbar_html,
        sublime.LAYOUT_BLOCK,
        lambda href, v=view: _on_toolbar_navigate(v, href),
    )
    pset.update([p])
    VIEW_PHANTOM_OFFSETS[vid] = cache_key


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
    # 1. Prefer /config for full metadata & hot-reloaded settings
    ok, data, _ = lt_core.request_server("/config", method="GET", timeout=2.0)
    if ok and isinstance(data, dict) and data.get("ok"):
        SERVER_STATE["online"] = True
        SERVER_STATE["active_llm_profile"] = str(data.get("active_llm_profile", ""))
        SERVER_STATE["llm_profile"] = str(data.get("active_llm_profile", ""))
        SERVER_STATE["config_path"] = str(data.get("config_path", ""))
        SERVER_STATE["config_errors"] = list(data.get("config_errors", []))
        if "modes" in data:
            SERVER_STATE["modes"] = list(data["modes"])
        ed = data.get("editor", {})
        if isinstance(ed, dict):
            lt_core.merge_editor_config(ed, base=EDITOR_CONFIG)
    else:
        # Fallback to /health
        ok_h, data_h, _ = lt_core.request_server("/health", method="GET", timeout=2.0)
        if ok_h and isinstance(data_h, dict) and data_h.get("ok"):
            SERVER_STATE["online"] = True
            SERVER_STATE["llm_profile"] = str(data_h.get("llm_profile", ""))
            SERVER_STATE["llm_model"] = str(data_h.get("llm_model", ""))
            SERVER_STATE["version"] = str(data_h.get("version", ""))
            SERVER_STATE["config_errors"] = list(data_h.get("config_errors", []))
            ed = data_h.get("editor", {})
            if isinstance(ed, dict):
                lt_core.merge_editor_config(ed, base=EDITOR_CONFIG)
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


def ensure_server_online(wait_sec: float = 6.0) -> bool:
    """
    Checks if local server is online. If offline, attempts to auto-launch
    the background serve daemon and polls until online or wait_sec expires.
    """
    with SERVER_START_LOCK:
        # Check if already online
        ok, data, _ = lt_core.request_server("/health", method="GET", timeout=1.0)
        if ok and isinstance(data, dict) and data.get("ok"):
            refresh_server_and_views()
            return True

        # Check autostart configuration
        configured_cmd = str(EDITOR_CONFIG.get("autostart_cmd", ""))
        cmd_list, repo_root = lt_core.find_repo_autostart_cmd(__file__, configured_cmd=configured_cmd)
        if not cmd_list:
            return False

        try:
            creationflags = 0x08000000 if sys.platform.startswith("win") else 0
            subprocess.Popen(
                cmd_list,
                cwd=repo_root,
                creationflags=creationflags,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            return False

        # Poll until online
        deadline = time.time() + wait_sec
        while time.time() < deadline:
            time.sleep(0.2)
            if lt_core.read_serve_info() is not None:
                ok, data, _ = lt_core.request_server("/health", method="GET", timeout=0.5)
                if ok and isinstance(data, dict) and data.get("ok"):
                    refresh_server_and_views()
                    return True

        return False


def maybe_check_server():
    global LAST_SERVER_CHECK_TIME
    now = time.time()
    if SERVER_STATE["online"]:
        if now - LAST_SERVER_CHECK_TIME >= 5.0:
            LAST_SERVER_CHECK_TIME = now
            sublime.set_timeout_async(refresh_server_and_views, 0)
        return

    if now - LAST_SERVER_CHECK_TIME >= 2.0:
        LAST_SERVER_CHECK_TIME = now
        if lt_core.read_serve_info() is not None:
            sublime.set_timeout_async(refresh_server_and_views, 0)
        else:
            sublime.set_timeout_async(lambda: ensure_server_online(wait_sec=3.0), 0)


def plugin_loaded():
    sublime.set_timeout_async(lambda: ensure_server_online(wait_sec=3.0), 100)


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

    def on_hover(self, view, point, hover_zone):
        if hover_zone != sublime.HOVER_TEXT:
            return
        if not view.settings().get("lt_managed"):
            return
        if not EDITOR_CONFIG.get("image_preview_on_hover", True):
            return

        line_region = view.line(point)
        line_str = view.substr(line_region)
        col = point - line_region.begin()

        fname = view.file_name() or ""
        base_dir = os.path.dirname(fname)
        img_path = lt_core.extract_image_path_at(line_str, col, base_dir=base_dir)
        if not img_path:
            return

        def _async_load():
            data_uri, disp_w, disp_h, err = lt_core.read_image_data_uri(
                img_path, max_bytes=1048576, max_width=400
            )

            def _show_ui():
                if not view.is_valid():
                    return
                if err:
                    html = f'<body style="padding: 4px 8px;"><span style="color: var(--yellowish);">{_escape_html(err)}</span></body>'
                    view.show_popup(html, flags=sublime.HIDE_ON_MOUSE_MOVE_AWAY, location=point, max_width=450)
                elif data_uri:
                    html = f'<body style="margin: 0; padding: 4px;"><img src="{data_uri}" width="{disp_w}" height="{disp_h}"></body>'
                    view.show_popup(html, flags=sublime.HIDE_ON_MOUSE_MOVE_AWAY, location=point, max_width=450)

            sublime.set_timeout(_show_ui, 0)

        sublime.set_timeout_async(_async_load, 0)


class LtSmartPasteCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        prompt_path = self.view.file_name() or ""
        sublime.set_timeout_async(lambda: self._async_capture(prompt_path), 0)

    def _async_capture(self, prompt_path):
        if not SERVER_STATE["online"]:
            ensure_server_online(wait_sec=5.0)

        payload = {"prompt_path": prompt_path}
        ok, data, _ = lt_core.request_server(
            "/clipboard/capture",
            method="POST",
            payload=payload,
            timeout=2.0,
        )

        if not ok:
            # Detect connection failure (including stale serve.json causing request_server to fail)
            if ensure_server_online(wait_sec=5.0):
                ok, data, _ = lt_core.request_server(
                    "/clipboard/capture",
                    method="POST",
                    payload=payload,
                    timeout=2.0,
                )

        if ok and isinstance(data, dict):
            if not SERVER_STATE["online"]:
                sublime.set_timeout_async(refresh_server_and_views, 0)
            kind = data.get("kind", "text")
            paths = data.get("paths", [])
            if kind in ("image", "files") and paths:
                style = data.get("path_style") or EDITOR_CONFIG.get("path_style", "windows")
                quote_spaces = (
                    data["quote_paths_with_spaces"]
                    if "quote_paths_with_spaces" in data
                    else bool(EDITOR_CONFIG.get("quote_paths_with_spaces", True))
                )
                formatted = [
                    lt_core.format_path(p, style=style, quote_spaces=quote_spaces)
                    for p in paths
                ]
                text_to_insert = "\n".join(formatted)
                sublime.set_timeout(
                    lambda: self.view.run_command(
                        "lt_insert_text_smart", {"text": text_to_insert}
                    ),
                    0,
                )
                return
            else:
                # Backend responded with text or empty paths; normal paste without warning
                sublime.set_timeout(lambda: self.view.run_command("paste"), 0)
                return

        # Offline fallback: Try CF_HDROP via ctypes
        win32_files = lt_core.grab_clipboard_files_win32()
        if win32_files:
            style = EDITOR_CONFIG.get("path_style", "windows")
            quote_spaces = bool(EDITOR_CONFIG.get("quote_paths_with_spaces", True))
            formatted = [
                lt_core.format_path(p, style=style, quote_spaces=quote_spaces)
                for p in win32_files
            ]
            text_to_insert = "\n".join(formatted)
            sublime.set_timeout(
                lambda: self.view.run_command(
                    "lt_insert_text_smart", {"text": text_to_insert}
                ),
                0,
            )
            return

        def fallback():
            self.view.set_status("lt_msg", "LT 後端離線，改用一般貼上")
            self.view.run_command("paste")
            sublime.set_timeout(lambda: self.view.erase_status("lt_msg"), 3000)

        sublime.set_timeout(fallback, 0)


class LtPasteRawCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        self.view.run_command("paste")


def _update_frontmatter_in_view(view, updates):
    """Updates frontmatter in view by replacing only 0..body_offset region."""
    if not view.is_valid():
        return
    src_text = view.substr(sublime.Region(0, view.size()))
    fields, _, body_offset = lt_core.parse_frontmatter(src_text)
    fields = dict(fields)
    for k, v in updates.items():
        fields[k] = "" if v is None else str(v)
    rendered_fm = lt_core.render_frontmatter(
        fields, field_order=EDITOR_CONFIG.get("frontmatter_fields")
    )
    view.run_command(
        "lt_replace_region", {"start": 0, "end": body_offset, "text": rendered_fm}
    )
    view.run_command("save")


def _do_auto_rename(win, source_view, source_path, refined_path, result_text, right_view=None):
    if not bool(EDITOR_CONFIG.get("auto_rename_after_refine", True)):
        return
    if "_untitled" not in os.path.basename(source_path).lower():
        return

    slug = lt_core.extract_title_slug(result_text, max_len=20)
    renamed = lt_core.renamed_paths_for(source_path, refined_path, slug)
    if not renamed:
        return

    new_src, new_ref = renamed
    try:
        if os.path.exists(source_path):
            os.replace(source_path, new_src)
            try:
                source_view.retarget(new_src)
            except Exception:
                pass

        if os.path.exists(refined_path):
            os.replace(refined_path, new_ref)
            if right_view and right_view.is_valid():
                try:
                    right_view.retarget(new_ref)
                except Exception:
                    try:
                        right_view.close()
                        if win and win.is_valid():
                            right_view = win.open_file(new_ref)
                    except Exception:
                        pass

        if source_view and source_view.is_valid():
            VIEW_PHANTOM_OFFSETS.pop(source_view.id(), None)
            update_view_status(source_view)
            update_view_phantoms(source_view)

        if right_view and right_view.is_valid():
            VIEW_PHANTOM_OFFSETS.pop(right_view.id(), None)
            update_view_status(right_view)
            update_view_phantoms(right_view)
    except Exception:
        pass


class LtRefineCommand(sublime_plugin.TextCommand):
    def run(self, edit, mode=None):
        sel_text = ""
        has_selection = False
        for r in self.view.sel():
            if not r.empty():
                sel_text = self.view.substr(r)
                has_selection = True
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
                win, self.view, source_path, text_to_refine, target_mode, has_selection
            ),
            0,
        )

    def _async_refine(self, win, source_view, source_path, text_to_refine, target_mode, has_selection):
        if not SERVER_STATE["online"]:
            ensure_server_online(wait_sec=2.5)

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

        if not ok and not SERVER_STATE["online"]:
            if ensure_server_online(wait_sec=2.0):
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
                    win, source_view, source_path, result_text, warnings, target_mode, has_selection
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
        self, win, source_view, source_path, result_text, warnings, target_mode, has_selection
    ):
        if source_view.is_valid():
            source_view.erase_status("lt_msg")

        # 1. Update frontmatter in source view (only 0..body_offset)
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        _update_frontmatter_in_view(
            source_view,
            {"status": "refined", "mode": target_mode, "refined_at": now_str},
        )

        if not win or not win.is_valid():
            win = sublime.active_window()
        if not win:
            return

        result_view_mode = str(EDITOR_CONFIG.get("result_view", "split_right")).lower()
        refined_path = lt_core.refined_path_for(source_path)

        if result_view_mode == "replace":
            # Write backup to .refined.md
            with open(refined_path, "w", encoding="utf-8") as f:
                f.write(result_text)

            # In-place replace body of source view (pass result_text directly, no "\n" +)
            src_text = source_view.substr(sublime.Region(0, source_view.size()))
            _, _, body_offset = lt_core.parse_frontmatter(src_text)
            source_view.run_command(
                "lt_replace_region",
                {"start": body_offset, "end": source_view.size(), "text": result_text},
            )
            source_view.run_command("save")
            if warnings:
                update_refined_phantoms(source_view, warnings)
                source_view.set_status("lt_warn", "⚠️ 警告: " + "; ".join(str(w) for w in warnings))
            else:
                update_refined_phantoms(source_view, [])
                source_view.erase_status("lt_warn")

            if not has_selection:
                _do_auto_rename(win, source_view, source_path, refined_path, result_text, right_view=None)
        else:
            # Layout setup
            if result_view_mode == "below":
                if win.num_groups() < 2:
                    win.set_layout({
                        "cols": [0.0, 1.0],
                        "rows": [0.0, 0.5, 1.0],
                        "cells": [[0, 0, 1, 1], [0, 1, 1, 2]],
                    })
            else:
                # Default: split_right
                if win.num_groups() < 2:
                    win.set_layout({
                        "cols": [0.0, 0.5, 1.0],
                        "rows": [0.0, 1.0],
                        "cells": [[0, 0, 1, 1], [1, 0, 2, 1]],
                    })

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

                # Trigger auto_rename only after write and save are complete
                if not has_selection:
                    _do_auto_rename(win, source_view, source_path, refined_path, result_text, right_view=v)

            sublime.set_timeout(lambda: apply_content(right_view), 50)


class LtSendCommand(sublime_plugin.TextCommand):
    """
    Copies refined/raw content to clipboard, updates status to sent,
    and optionally auto-pastes to previous window.
    """
    def run(self, edit):
        source_path = self.view.file_name()
        if not source_path:
            self.view.set_status("lt_msg", "請先儲存檔案")
            return

        source_text = self.view.substr(sublime.Region(0, self.view.size()))
        _, raw_body, _ = lt_core.parse_frontmatter(source_text)

        copy_mode = str(EDITOR_CONFIG.get("copy", "refined")).lower()
        refined_path = lt_core.refined_path_for(source_path)
        refined_body = ""
        if os.path.isfile(refined_path):
            try:
                with open(refined_path, "r", encoding="utf-8") as f:
                    ref_text = f.read()
                _, ref_b, _ = lt_core.parse_frontmatter(ref_text)
                refined_body = ref_b.strip() if ref_b else ref_text.strip()
            except Exception:
                refined_body = ""

        if copy_mode == "raw":
            final_clip = raw_body.strip()
        elif copy_mode == "both":
            ref_part = refined_body if refined_body else raw_body.strip()
            final_clip = f"{raw_body.strip()}\n\n---\n\n{ref_part}".strip()
        else:
            # Default: refined
            final_clip = refined_body if refined_body else raw_body.strip()

        if not final_clip:
            self.view.set_status("lt_msg", "內容為空，未複製")
            return

        sublime.set_clipboard(final_clip)
        self.view.set_status("lt_msg", "已複製到剪貼簿")
        sublime.set_timeout(lambda: self.view.erase_status("lt_msg"), 3000)

        # Mark sent in frontmatter
        if bool(EDITOR_CONFIG.get("mark_sent_in_frontmatter", True)):
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
            _update_frontmatter_in_view(
                self.view,
                {"status": "sent", "sent_at": now_str},
            )

        # Auto paste to previous window if enabled
        if bool(EDITOR_CONFIG.get("auto_paste_to_last_window", False)):
            sublime.set_timeout_async(
                lambda: self._async_paste_to_previous(final_clip), 0
            )

    def _async_paste_to_previous(self, text):
        lt_core.request_server(
            "/paste_to_previous",
            method="POST",
            payload={"text": text},
            timeout=2.0,
        )


class LtPasteToPreviousCommand(sublime_plugin.TextCommand):
    """Pastes prompt text directly to the last active window."""
    def run(self, edit):
        source_path = self.view.file_name()
        if not source_path:
            return

        source_text = self.view.substr(sublime.Region(0, self.view.size()))
        _, raw_body, _ = lt_core.parse_frontmatter(source_text)

        refined_path = lt_core.refined_path_for(source_path)
        final_text = ""
        if os.path.isfile(refined_path):
            try:
                with open(refined_path, "r", encoding="utf-8") as f:
                    ref_text = f.read()
                _, ref_b, _ = lt_core.parse_frontmatter(ref_text)
                final_text = ref_b.strip() if ref_b else ref_text.strip()
            except Exception:
                final_text = ""

        if not final_text:
            final_text = raw_body.strip()

        if not final_text:
            self.view.set_status("lt_msg", "內容為空，無法貼回")
            return

        self.view.set_status("lt_msg", "正在貼回上一個視窗…")
        sublime.set_timeout_async(lambda: self._async_send(final_text), 0)

    def _async_send(self, text):
        if not SERVER_STATE["online"]:
            ensure_server_online(wait_sec=2.0)

        ok, data, err = lt_core.request_server(
            "/paste_to_previous",
            method="POST",
            payload={"text": text},
            timeout=3.0,
        )

        def report():
            if ok and isinstance(data, dict) and data.get("ok"):
                self.view.set_status("lt_msg", "已貼回上一個視窗")
            else:
                err_msg = data.get("error") if isinstance(data, dict) else err
                self.view.set_status("lt_msg", f"貼回失敗: {err_msg}")
            sublime.set_timeout(lambda: self.view.erase_status("lt_msg"), 3000)

        sublime.set_timeout(report, 0)


class LtHistoryCommand(sublime_plugin.WindowCommand):
    """Displays quick panel with recent prompts in prompts_root."""
    def run(self):
        prompts_root = EDITOR_CONFIG.get(
            "prompts_root", lt_core.DEFAULT_EDITOR_CONFIG["prompts_root"]
        )
        history = lt_core.list_prompt_history(prompts_root, limit=200)
        if not history:
            sublime.message_dialog("沒有找到任何歷史 Prompt 檔案。")
            return

        items = [
            [
                f"[{item['status'].upper()}] {item['mtime_str']} · 模式: {item['mode']}",
                f"{item['snippet']} ({os.path.basename(item['path'])})",
            ]
            for item in history
        ]

        def on_select(idx):
            if idx >= 0 and idx < len(history):
                self.window.open_file(history[idx]["path"])

        self.window.show_quick_panel(items, on_select)


class LtOpenSettingsGuiCommand(sublime_plugin.WindowCommand):
    """Opens Tk settings / dictionary GUI panel."""
    def run(self, page="settings"):
        sublime.set_timeout_async(lambda: self._async_open(page), 0)

    def _async_open(self, page):
        ok, data, _ = lt_core.request_server(
            "/ui/open",
            method="POST",
            payload={"page": page},
            timeout=2.0,
        )
        if not (ok and isinstance(data, dict) and data.get("ok")):
            # Fallback to direct python execution via find_repo_panel_cmd
            cmd_list, repo_root = lt_core.find_repo_panel_cmd(__file__, page=page)
            try:
                creationflags = 0x08000000 if sys.platform.startswith("win") else 0
                subprocess.Popen(
                    cmd_list,
                    cwd=repo_root,
                    creationflags=creationflags,
                )
            except Exception as e:
                sublime.error_message(f"無法開啟設定面板: {e}")


class LtOpenSettingsCommand(sublime_plugin.WindowCommand):
    """Opens config.yaml directly in Sublime Text."""
    def run(self):
        sublime.set_timeout_async(self._async_open, 0)

    def _async_open(self):
        cfg_path = SERVER_STATE.get("config_path")
        if not cfg_path:
            ok, data, _ = lt_core.request_server("/config", method="GET", timeout=1.5)
            if ok and isinstance(data, dict):
                cfg_path = data.get("config_path")

        if not cfg_path or not os.path.isfile(cfg_path):
            plugin_real = os.path.realpath(__file__)
            repo_root = os.path.dirname(os.path.dirname(os.path.dirname(plugin_real)))
            cfg_path = os.path.join(repo_root, "config", "config.yaml")

        if os.path.isfile(cfg_path):
            sublime.set_timeout(lambda: self.window.open_file(cfg_path), 0)
        else:
            sublime.error_message(f"找不到設定檔: {cfg_path}")


class LtCheckStatusCommand(sublime_plugin.WindowCommand):
    """Displays current Local Typeless server and LLM connection status."""
    def run(self):
        sublime.set_timeout_async(self._async_check, 0)

    def _async_check(self):
        refresh_server_and_views()
        serve_info = lt_core.read_serve_info()

        if SERVER_STATE["online"]:
            pid = serve_info.get("pid", "未知") if serve_info else "未知"
            port = serve_info.get("port", "未知") if serve_info else "未知"
            profile = SERVER_STATE.get("active_llm_profile") or SERVER_STATE.get("llm_profile") or "預設"
            errors = SERVER_STATE.get("config_errors", [])
            err_str = ("\n\n設定警告:\n" + "\n".join(f"• {e}" for e in errors)) if errors else ""
            msg = (
                f"Local Typeless 後端服務正常運行中 (線上)\n\n"
                f"• Port: {port}\n"
                f"• PID: {pid}\n"
                f"• Active LLM: {profile}"
                f"{err_str}"
            )
            sublime.message_dialog(msg)
        else:
            res = sublime.ok_cancel_dialog(
                "Local Typeless 後端服務目前處於「離線」狀態。\n\n是否立即在背景啟動服務？",
                "立即啟動"
            )
            if res:
                started = ensure_server_online(wait_sec=4.0)
                if started:
                    sublime.message_dialog("Local Typeless 後端服務已成功啟動！")
                else:
                    sublime.error_message("啟動失敗，請檢查 Python 環境或執行 scripts/install.ps1。")


class LtInsertTextCommand(sublime_plugin.TextCommand):
    def run(self, edit, text=""):
        for r in self.view.sel():
            self.view.replace(edit, r, text)


class LtInsertTextSmartCommand(sublime_plugin.TextCommand):
    """Inserts text with automatic boundary whitespace padding."""
    def run(self, edit, text=""):
        for r in self.view.sel():
            left_space = False
            right_space = False
            if r.begin() > 0:
                char_before = self.view.substr(r.begin() - 1)
                if not char_before.isspace() and char_before not in ("\n", "\r"):
                    left_space = True
            if r.end() < self.view.size():
                char_after = self.view.substr(r.end())
                if not char_after.isspace() and char_after not in ("\n", "\r"):
                    right_space = True

            insert_s = (" " if left_space else "") + text + (" " if right_space else "")
            self.view.replace(edit, r, insert_s)


class LtReplaceAllCommand(sublime_plugin.TextCommand):
    def run(self, edit, text=""):
        region = sublime.Region(0, self.view.size())
        self.view.replace(edit, region, text)


class LtReplaceRegionCommand(sublime_plugin.TextCommand):
    def run(self, edit, start=0, end=0, text=""):
        region = sublime.Region(start, end)
        self.view.replace(edit, region, text)
