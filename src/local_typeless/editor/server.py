"""Local HTTP serve process for editor integrations (Sublime Text / external workspace)."""
from __future__ import annotations

import hmac
import json
import logging
import os
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from local_typeless import __version__
from local_typeless.capture.clipboard_images import Win32ClipboardBackend
from local_typeless.editor.config import EditorConfig, load_editor_config_with_errors
from local_typeless.editor.protect import protect, restore
from local_typeless.stt.base import STTEngine
from local_typeless.types import AppConfig, Mode, TranscriptResult

logger = logging.getLogger(__name__)

MAX_BODY_BYTES = 2 * 1024 * 1024  # 2 MB limit


class NullSTT(STTEngine):
    """Null STT implementation so serve does not import faster_whisper."""

    def transcribe(
        self,
        audio_path: Path,
        initial_prompt: Optional[str] = None,
        on_segment: Optional[Any] = None,
        word_timestamps: bool = False,
    ) -> TranscriptResult:
        raise RuntimeError("NullSTT cannot transcribe")


def default_serve_json_path() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if base:
        p = Path(base)
    else:
        p = Path.home() / "AppData" / "Local"
    return p / "local-typeless" / "serve.json"


def write_serve_json(path: Path, port: int, token: str, pid: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f"{path.name}.tmp.{pid}.{secrets.token_hex(4)}")
    data = {"port": port, "token": token, "pid": pid}
    temp_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(temp_path, path)


def remove_serve_json(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except Exception as e:
        logger.warning("Could not remove %s: %s", path, e)


def scan_modes(prompts_dir: Path) -> List[str]:
    """Scan prompts_dir for existing *.md files that correspond to Mode values."""
    valid_order = [m.value for m in Mode]
    if not prompts_dir.exists():
        return list(valid_order)
    found = set()
    for p in prompts_dir.glob("*.md"):
        if p.name.endswith(".user.md") or p.name == "review.md":
            continue
        stem = p.stem.lower()
        if stem in valid_order:
            found.add(stem)
    return [m for m in valid_order if m in found] or list(valid_order)


class PreviousWindowTracker:
    """Tracks the last active window that does not belong to self or Sublime Text."""

    def __init__(self) -> None:
        self._hwnd: int = 0
        self._running: bool = False
        self._thread: Optional[threading.Thread] = None

    def get_process_name(self, pid: int) -> str:
        if sys.platform != "win32":
            return ""
        try:
            import ctypes
            from ctypes import wintypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return ""
            try:
                buf = ctypes.create_unicode_buffer(1024)
                size = wintypes.DWORD(1024)
                if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                    return Path(buf.value).name.lower()
                return ""
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return ""

    def track(self) -> None:
        if sys.platform != "win32":
            return
        try:
            import ctypes
            from ctypes import wintypes

            u = ctypes.WinDLL("user32", use_last_error=True)
            hwnd = u.GetForegroundWindow()
            if not hwnd:
                return
            pid = wintypes.DWORD(0)
            u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if not pid.value or pid.value == os.getpid():
                return
            name = self.get_process_name(pid.value)
            if name.startswith("sublime") or name.startswith("plugin_host"):
                return
            self._hwnd = int(hwnd)
        except Exception as exc:
            logger.debug("PreviousWindowTracker.track failed: %s", exc)

    def activate(self) -> bool:
        if sys.platform != "win32" or not self._hwnd:
            return False
        try:
            import ctypes

            u = ctypes.WinDLL("user32", use_last_error=True)
            if not u.IsWindow(self._hwnd):
                return False
            return bool(u.SetForegroundWindow(self._hwnd))
        except Exception as exc:
            logger.debug("PreviousWindowTracker.activate failed: %s", exc)
            return False

    def start(self) -> None:
        if self._running or sys.platform != "win32":
            return
        self._running = True

        def _loop() -> None:
            while self._running:
                self.track()
                time.sleep(0.25)

        self._thread = threading.Thread(target=_loop, daemon=True, name="PreviousWindowTracker")
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=1.0)


class ConfigReloader:
    """Watches config, settings, and glossary file mtimes and reloads before requests."""

    def __init__(
        self,
        config_path: Optional[Path],
        app_config: Optional[AppConfig] = None,
        editor_config: Optional[EditorConfig] = None,
        pipeline: Optional[Any] = None,
        config_errors: Optional[List[str]] = None,
    ) -> None:
        self.config_path = config_path
        if config_path and (app_config is None or pipeline is None or editor_config is None):
            try:
                from local_typeless.config.loader import load_config
                from local_typeless.pipeline import Pipeline
                if app_config is None:
                    app_config = load_config(config_path)
                if editor_config is None:
                    editor_config, errs, _, _ = load_editor_config_with_errors(config_path)
                    if config_errors is None:
                        config_errors = errs
                if pipeline is None:
                    pipeline = Pipeline(app_config, stt=NullSTT(), outputs=[], records=False)
            except Exception as e:
                if config_errors is None:
                    config_errors = [f"Initial load error: {e}"]
        self.app_config = app_config
        self.editor_config = editor_config or EditorConfig()
        self.pipeline = pipeline
        self.config_errors = list(config_errors or [])
        self._snapshot = self._take_snapshot()

    def get_pipeline(self) -> Any:
        return self.pipeline

    def reload_if_changed(self) -> bool:
        return self.check_and_reload()

    def _take_snapshot(self) -> Dict[Path, Optional[Tuple[int, int]]]:
        if not self.config_path:
            return {}
        from local_typeless.config.loader import settings_path
        from local_typeless.glossary.manager import user_overlay_path

        files = [self.config_path, settings_path(self.config_path)]
        if self.app_config and getattr(self.app_config, "glossary_path", None):
            g_path = Path(self.app_config.glossary_path)
            files.append(g_path)
            files.append(user_overlay_path(g_path))

        snap: Dict[Path, Optional[Tuple[int, int]]] = {}
        for f in files:
            try:
                if f.exists():
                    st = f.stat()
                    snap[f] = (st.st_mtime_ns, st.st_size)
                else:
                    snap[f] = None
            except Exception:
                snap[f] = None
        return snap

    def check_and_reload(self) -> bool:
        if not self.config_path:
            return False
        new_snap = self._take_snapshot()
        if new_snap == self._snapshot:
            return False

        try:
            from local_typeless.config.loader import load_config
            from local_typeless.pipeline import Pipeline

            new_app_cfg = load_config(self.config_path)
            new_ed_cfg, new_errors, _, _ = load_editor_config_with_errors(self.config_path)
            new_pipe = Pipeline(new_app_cfg, stt=NullSTT(), outputs=[], records=False)

            self.app_config = new_app_cfg
            self.editor_config = new_ed_cfg
            self.pipeline = new_pipe
            self.config_errors = new_errors
            self._snapshot = new_snap
            logger.info("Configuration hot-reloaded successfully")
            return True
        except Exception as exc:
            logger.warning("Hot reload failed; retaining prior configuration: %s", exc)
            self.config_errors = [f"Hot reload failed: {type(exc).__name__}: {exc}"]
            self._snapshot = new_snap
            return False


class _EditorRequestHandler(BaseHTTPRequestHandler):
    @property
    def editor_server(self) -> "EditorServer":
        return getattr(self.server, "editor_server")

    def log_message(self, format: str, *args: Any) -> None:
        logger.debug("%s - - [%s] %s", self.address_string(), self.log_date_time_string(), format % args)

    def _send_json(self, status_code: int, data: Dict[str, Any]) -> None:
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _verify_request(self) -> bool:
        # Check Origin header: reject immediately if present
        if self.headers.get("Origin") is not None:
            self._send_json(403, {"ok": False, "error": "origin_forbidden"})
            return False

        # Verify X-LT-Token using hmac.compare_digest
        token = self.headers.get("X-LT-Token")
        if not token or not hmac.compare_digest(token, self.editor_server.token):
            self._send_json(401, {"ok": False, "error": "unauthorized"})
            return False

        # Check mtime hot-reloading after authentication succeeds
        if self.editor_server.reloader is not None:
            self.editor_server.reloader.check_and_reload()

        return True

    def _read_body_json(self) -> Optional[Dict[str, Any]]:
        content_length_str = self.headers.get("Content-Length")
        if not content_length_str:
            return {}
        try:
            length = int(content_length_str)
            if length < 0:
                self._send_json(400, {"ok": False, "error": "invalid_content_length"})
                return None
        except ValueError:
            self._send_json(400, {"ok": False, "error": "invalid_content_length"})
            return None

        if length > MAX_BODY_BYTES:
            self.close_connection = True
            self.send_response(413)
            self.send_header("Connection", "close")
            self.send_header("Content-Type", "application/json; charset=utf-8")
            payload = json.dumps({"ok": False, "error": "payload_too_large"}).encode("utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return None

        raw_body = self.rfile.read(length)
        if len(raw_body) > MAX_BODY_BYTES:
            self.close_connection = True
            self.send_response(413)
            self.send_header("Connection", "close")
            self.send_header("Content-Type", "application/json; charset=utf-8")
            payload = json.dumps({"ok": False, "error": "payload_too_large"}).encode("utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return None

        if not raw_body:
            return {}

        try:
            parsed = json.loads(raw_body.decode("utf-8"))
            if not isinstance(parsed, dict):
                self._send_json(400, {"ok": False, "error": "json_must_be_object"})
                return None
            return parsed
        except Exception:
            self._send_json(400, {"ok": False, "error": "invalid_json"})
            return None

    def do_GET(self) -> None:
        if not self._verify_request():
            return

        clean_path = self.path.split("?", 1)[0]
        if clean_path == "/health":
            self._handle_health()
        elif clean_path == "/config":
            self._handle_config()
        else:
            self._send_json(404, {"ok": False, "error": "not_found"})

    def do_POST(self) -> None:
        if not self._verify_request():
            return

        body = self._read_body_json()
        if body is None:
            return

        clean_path = self.path.split("?", 1)[0]
        if clean_path == "/refine":
            self._handle_refine(body)
        elif clean_path == "/clipboard/capture":
            self._handle_clipboard_capture(body)
        elif clean_path == "/paste_to_previous":
            self._handle_paste_to_previous(body)
        elif clean_path == "/ui/open":
            self._handle_ui_open(body)
        else:
            self._send_json(404, {"ok": False, "error": "not_found"})

    def _handle_health(self) -> None:
        editor_cfg = self.editor_server.current_editor_config
        app_cfg = self.editor_server.current_app_config
        active_llm = app_cfg.active_llm_profile if app_cfg else ""
        llm_model = getattr(app_cfg.llm, "model", "") if app_cfg else ""
        config_errors = self.editor_server.get_config_errors()
        resp = {
            "ok": True,
            "version": __version__,
            "llm_profile": active_llm,
            "llm_model": llm_model,
            "config_errors": config_errors,
            "editor": {
                "prompts_root": editor_cfg.prompts_root,
                "file_layout": editor_cfg.file_layout,
                "autosave_delay_ms": editor_cfg.autosave_delay_ms,
                "path_style": editor_cfg.paste.path_style,
                "quote_paths_with_spaces": editor_cfg.paste.quote_paths_with_spaces,
                "default_mode": editor_cfg.refine.default_mode,
                "result_view": editor_cfg.refine.result_view,
                "frontmatter_fields": editor_cfg.frontmatter.fields,
            },
        }
        self._send_json(200, resp)

    def _handle_config(self) -> None:
        from local_typeless.config.loader import settings_path

        editor_cfg = self.editor_server.current_editor_config
        app_cfg = self.editor_server.current_app_config
        active_llm = app_cfg.active_llm_profile if app_cfg else ""
        llm_profiles = list(app_cfg.llm_profiles.keys()) if app_cfg else []
        config_errors = self.editor_server.get_config_errors()

        cfg_path_str = str(self.editor_server.config_path.resolve()) if self.editor_server.config_path else ""
        set_path_str = str(settings_path(self.editor_server.config_path).resolve()) if self.editor_server.config_path else ""

        prompts_dir = Path(app_cfg.prompts_dir) if app_cfg else Path("prompts")
        modes = scan_modes(prompts_dir)

        resp = {
            "ok": True,
            "config_path": cfg_path_str,
            "settings_path": set_path_str,
            "active_llm_profile": active_llm,
            "llm_profiles": llm_profiles,
            "modes": modes,
            "config_errors": config_errors,
            "editor": {
                "prompts_root": editor_cfg.prompts_root,
                "file_layout": editor_cfg.file_layout,
                "auto_rename_after_refine": editor_cfg.auto_rename_after_refine,
                "autosave_delay_ms": editor_cfg.autosave_delay_ms,
                "server": {
                    "port": editor_cfg.server.port,
                    "autostart_cmd": editor_cfg.server.autostart_cmd,
                },
                "paste": {
                    "image_dir": editor_cfg.paste.image_dir,
                    "image_name": editor_cfg.paste.image_name,
                    "path_style": editor_cfg.paste.path_style,
                    "quote_paths_with_spaces": editor_cfg.paste.quote_paths_with_spaces,
                    "image_preview_on_hover": editor_cfg.paste.image_preview_on_hover,
                },
                "refine": {
                    "default_mode": editor_cfg.refine.default_mode,
                    "modes_in_palette": editor_cfg.refine.modes_in_palette,
                    "result_view": editor_cfg.refine.result_view,
                    "protect_paths": editor_cfg.refine.protect_paths,
                    "protect_code_blocks": editor_cfg.refine.protect_code_blocks,
                },
                "send": {
                    "copy": editor_cfg.send.copy,
                    "auto_paste_to_last_window": editor_cfg.send.auto_paste_to_last_window,
                    "mark_sent_in_frontmatter": editor_cfg.send.mark_sent_in_frontmatter,
                },
                "frontmatter": {
                    "fields": editor_cfg.frontmatter.fields,
                },
            },
        }
        self._send_json(200, resp)

    def _handle_refine(self, body: Dict[str, Any]) -> None:
        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            self._send_json(400, {"ok": False, "error": "text is required"})
            return

        editor_cfg = self.editor_server.current_editor_config
        mode_val = body.get("mode", editor_cfg.refine.default_mode)
        valid_modes = {m.value for m in Mode}
        if mode_val not in valid_modes:
            self._send_json(400, {"ok": False, "error": f"invalid mode: {mode_val}"})
            return

        protect_enabled = bool(body.get("protect", True))
        if protect_enabled:
            protected_text, mapping = protect(
                text,
                paths=editor_cfg.refine.protect_paths,
                code=editor_cfg.refine.protect_code_blocks,
            )
        else:
            protected_text, mapping = text, {}

        try:
            result = self.editor_server.current_pipeline.refine_only(
                protected_text, Mode(mode_val), guard_warns=True
            )
        except Exception as exc:
            self._send_json(
                500,
                {
                    "ok": False,
                    "text": "",
                    "warnings": [],
                    "fallback": False,
                    "error": f"{type(exc).__name__}: {exc}",
                    "elapsed_sec": 0.0,
                },
            )
            return

        if result.fallback_triggered:
            self._send_json(
                200,
                {
                    "ok": False,
                    "text": "",
                    "warnings": [],
                    "fallback": True,
                    "error": result.error_message or "LLM fallback triggered",
                    "elapsed_sec": result.llm_elapsed_sec,
                },
            )
            return

        if protect_enabled:
            restored = restore(result.final_text, mapping)
            if restored is None:
                self._send_json(
                    200,
                    {
                        "ok": False,
                        "text": "",
                        "warnings": result.guard_warnings,
                        "fallback": False,
                        "error": "整理結果遺失了路徑或程式碼標記，已保留原文",
                        "elapsed_sec": result.llm_elapsed_sec,
                    },
                )
                return
            final_text = restored
        else:
            final_text = result.final_text

        self._send_json(
            200,
            {
                "ok": True,
                "text": final_text,
                "warnings": result.guard_warnings,
                "fallback": False,
                "error": "",
                "elapsed_sec": result.llm_elapsed_sec,
            },
        )

    def _handle_clipboard_capture(self, body: Dict[str, Any]) -> None:
        backend = self.editor_server.clipboard_backend
        editor_cfg = self.editor_server.current_editor_config

        # Priority 1: Files
        grab_files_fn = getattr(backend, "grab_files", None)
        if callable(grab_files_fn):
            try:
                files = grab_files_fn()
                if files:
                    clean_paths = [str(Path(f).resolve()) for f in files if str(f).strip()]
                    if clean_paths:
                        self._send_json(
                            200,
                            {
                                "kind": "files",
                                "paths": clean_paths,
                                "path_style": editor_cfg.paste.path_style,
                                "quote_paths_with_spaces": editor_cfg.paste.quote_paths_with_spaces,
                            },
                        )
                        return
            except Exception as exc:
                logger.debug("grab_files error: %s", exc)

        # Priority 2: PNG
        png_list = backend.grab_png()
        retries = 2
        while not png_list and retries > 0:
            time.sleep(0.05)
            png_list = backend.grab_png()
            retries -= 1

        valid_pngs = [b for b in png_list if b]
        if not valid_pngs:
            self._send_json(
                200,
                {
                    "kind": "text",
                    "paths": [],
                    "path_style": editor_cfg.paste.path_style,
                    "quote_paths_with_spaces": editor_cfg.paste.quote_paths_with_spaces,
                },
            )
            return

        now = datetime.now()
        try:
            image_dir_rel = editor_cfg.paste.image_dir.format(
                yyyy=now.strftime("%Y"),
                mm=now.strftime("%m"),
            )
        except Exception as exc:
            self._send_json(400, {"ok": False, "error": f"invalid paste template: {exc}"})
            return

        prompts_root = Path(editor_cfg.prompts_root)
        target_dir = prompts_root / image_dir_rel
        target_dir.mkdir(parents=True, exist_ok=True)

        prompt_path = body.get("prompt_path")
        if not prompt_path or not str(prompt_path).strip():
            stem = "untitled"
        else:
            p_name = Path(prompt_path).name
            if p_name.endswith(".refined.md"):
                stem = p_name[:-len(".refined.md")]
            else:
                stem = Path(prompt_path).stem
            if not stem:
                stem = "untitled"

        image_name_tmpl = editor_cfg.paste.image_name
        saved_paths: List[str] = []
        n = 1
        for png_bytes in valid_pngs:
            prev_filename: Optional[str] = None
            while n <= 10000:
                try:
                    filename = image_name_tmpl.format(prompt=stem, n=n)
                except Exception as exc:
                    self._send_json(400, {"ok": False, "error": f"invalid paste template: {exc}"})
                    return
                if prev_filename is not None and filename == prev_filename:
                    self._send_json(
                        400,
                        {"ok": False, "error": "invalid paste template: image_name must vary with {n}"},
                    )
                    return
                file_path = target_dir / filename
                if not file_path.exists():
                    break
                prev_filename = filename
                n += 1
            else:
                self._send_json(400, {"ok": False, "error": "too many colliding image files"})
                return
            file_path.write_bytes(png_bytes)
            saved_paths.append(str(file_path.resolve()))
            n += 1

        self._send_json(
            200,
            {
                "kind": "image",
                "paths": saved_paths,
                "path_style": editor_cfg.paste.path_style,
                "quote_paths_with_spaces": editor_cfg.paste.quote_paths_with_spaces,
            },
        )

    def _handle_paste_to_previous(self, body: Dict[str, Any]) -> None:
        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            self._send_json(400, {"ok": False, "error": "text is required"})
            return

        copy_fn = self.editor_server.copy_fn
        try:
            copy_fn(text)
        except Exception as exc:
            self._send_json(500, {"ok": False, "error": f"copy failed: {exc}"})
            return

        tracker = self.editor_server.previous_tracker
        activated = tracker.activate() if tracker is not None else False
        if activated:
            self.editor_server.sleep_fn(0.25)
            try:
                self.editor_server.paste_fn()
            except Exception as exc:
                logger.warning("paste_fn failed: %s", exc)
            self._send_json(200, {"ok": True, "error": ""})
        else:
            self._send_json(
                200,
                {
                    "ok": False,
                    "error": "找不到上一個視窗；內容已複製到剪貼簿，請切到目標視窗按 Ctrl+V",
                },
            )

    def _handle_ui_open(self, body: Dict[str, Any]) -> None:
        page = body.get("page", "settings")
        valid_pages = {"home", "history", "dictionary", "prompt", "settings", "review", "teach"}
        if page not in valid_pages:
            self._send_json(400, {"ok": False, "error": f"invalid page: {page}"})
            return

        launcher = self.editor_server.ui_launcher
        try:
            launcher(page)
            self._send_json(200, {"ok": True, "page": page})
        except Exception as exc:
            self._send_json(500, {"ok": False, "error": f"failed to launch UI: {exc}"})


class _ExclusiveThreadingHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer that prevents multiple processes from stealing the same port on Windows."""

    allow_reuse_address = False

    def server_bind(self) -> None:
        if sys.platform == "win32" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def _is_serve_json_healthy(serve_json_path: Path, timeout: float = 0.5) -> bool:
    if not serve_json_path.is_file():
        return False
    try:
        info = json.loads(serve_json_path.read_text(encoding="utf-8"))
        port = int(info["port"])
        token = str(info["token"])
    except Exception:
        return False
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/health",
        headers={"X-LT-Token": token},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode("utf-8"))
                return bool(data.get("ok"))
    except Exception:
        return False
    return False


def _try_acquire_serve_lock(lock_path: Path) -> Optional[Any]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "a+", encoding="utf-8")
    try:
        if sys.platform == "win32":
            import msvcrt

            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fh
    except Exception:
        try:
            fh.close()
        except Exception:
            pass
        return None


def _release_serve_lock(fh: Any) -> None:
    if fh is None:
        return
    try:
        if sys.platform == "win32":
            import msvcrt

            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        fh.close()
    except Exception:
        pass


def _build_ui_panel_cmd(
    exe: str, page: str, config_path: Optional[Path] = None
) -> List[str]:
    """Build CLI argv with top-level --config before the panel subcommand."""
    cmd = [exe, "-m", "local_typeless.cli"]
    if config_path:
        cmd.extend(["--config", str(config_path)])
    cmd.extend(["panel", "--page", page])
    return cmd


class EditorServer:
    def __init__(
        self,
        *,
        pipeline: Any = None,
        clipboard_backend: Optional[Any] = None,
        editor_config: Optional[EditorConfig] = None,
        app_config: Optional[AppConfig] = None,
        token: Optional[str] = None,
        serve_json_path: Optional[Path] = None,
        port: int = 8765,
        config_path: Optional[Union[Path, str]] = None,
        config_errors: Optional[List[str]] = None,
        previous_tracker: Optional[Any] = None,
        copy_fn: Optional[Callable[[str], None]] = None,
        paste_fn: Optional[Callable[[], None]] = None,
        sleep_fn: Optional[Callable[[float], None]] = None,
        ui_launcher: Optional[Callable[[str], None]] = None,
        track_windows: bool = False,
    ) -> None:
        self.config_path = Path(config_path).resolve() if config_path else None
        self.app_config = app_config
        self.editor_config = editor_config or EditorConfig()
        self.pipeline = pipeline
        self.config_errors: List[str] = list(config_errors or [])

        if self.config_path is not None:
            self.reloader: Optional[ConfigReloader] = ConfigReloader(
                self.config_path,
                self.app_config,
                self.editor_config,
                self.pipeline,
                config_errors=self.config_errors,
            )
        else:
            self.reloader = None

        self.clipboard_backend = (
            clipboard_backend if clipboard_backend is not None else Win32ClipboardBackend()
        )
        self.token = token or secrets.token_hex(16)
        self.serve_json_path = serve_json_path or default_serve_json_path()
        self.requested_port = port

        # Previous window tracker
        if previous_tracker is not None:
            self.previous_tracker = previous_tracker
        else:
            self.previous_tracker = PreviousWindowTracker()

        # Copy & paste functions
        if copy_fn is not None:
            self.copy_fn = copy_fn
        else:
            from local_typeless.output.clipboard_out import copy_with_retry
            self.copy_fn = copy_with_retry

        if paste_fn is not None:
            self.paste_fn = paste_fn
        else:
            from local_typeless.output.clipboard_out import _simulate_paste
            self.paste_fn = _simulate_paste

        self.sleep_fn = sleep_fn if sleep_fn is not None else time.sleep

        # UI launcher
        if ui_launcher is not None:
            self.ui_launcher = ui_launcher
        else:
            self.ui_launcher = self._default_ui_launcher

        self.track_windows = track_windows
        if self.track_windows and hasattr(self.previous_tracker, "start"):
            self.previous_tracker.start()

        try:
            self.httpd = _ExclusiveThreadingHTTPServer(("127.0.0.1", port), _EditorRequestHandler)
        except OSError:
            if port == 0:
                raise
            logger.warning("Port %s is already in use; falling back to an ephemeral port", port)
            self.httpd = _ExclusiveThreadingHTTPServer(("127.0.0.1", 0), _EditorRequestHandler)
        self.httpd.editor_server = self
        self.port = self.httpd.server_address[1]

        write_serve_json(self.serve_json_path, self.port, self.token, os.getpid())
        self._thread: Optional[threading.Thread] = None
        self._serving: bool = False

    def _default_ui_launcher(self, page: str) -> None:
        pythonw = Path(sys.executable).parent / "pythonw.exe"
        exe = str(pythonw) if pythonw.exists() else sys.executable
        cmd = _build_ui_panel_cmd(exe, page, self.config_path)
        creationflags = 0
        if sys.platform == "win32":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        subprocess.Popen(cmd, creationflags=creationflags)

    @property
    def current_app_config(self) -> Optional[AppConfig]:
        return self.reloader.app_config if self.reloader else self.app_config

    @property
    def current_editor_config(self) -> EditorConfig:
        return self.reloader.editor_config if self.reloader else self.editor_config

    @property
    def current_pipeline(self) -> Any:
        return self.reloader.pipeline if self.reloader else self.pipeline

    def get_config_errors(self) -> List[str]:
        if self.reloader:
            return list(self.reloader.config_errors)
        return list(self.config_errors)

    def start(self) -> None:
        """Start server in background thread."""

        def _run() -> None:
            self._serving = True
            try:
                self.httpd.serve_forever()
            finally:
                self._serving = False

        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()

    def serve_forever(self) -> None:
        """Run server synchronously until stopped."""
        self._serving = True
        try:
            self.httpd.serve_forever()
        finally:
            self._serving = False
            self.cleanup()

    def stop(self) -> None:
        """Stop server, tracker and clean up serve.json."""
        if hasattr(self.previous_tracker, "stop"):
            try:
                self.previous_tracker.stop()
            except Exception:
                pass
        try:
            if getattr(self, "_serving", False):
                self.httpd.shutdown()
        finally:
            self.cleanup()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def cleanup(self) -> None:
        try:
            self.httpd.server_close()
        except Exception:
            pass
        remove_serve_json(self.serve_json_path)


def run_serve(
    config_path: Optional[Union[Path, str]] = None,
    port: Optional[int] = None,
    overrides: Optional[dict] = None,
    serve_json_path: Optional[Path] = None,
) -> int:
    """Run local-typeless serve daemon."""
    from local_typeless.config.loader import load_config
    from local_typeless.pipeline import Pipeline

    target_serve_json = serve_json_path or default_serve_json_path()
    lock_fh: Optional[Any] = None
    if port is None:
        lock_fh = _try_acquire_serve_lock(target_serve_json.with_suffix(".lock"))
        if lock_fh is None:
            logger.info("Another local-typeless serve instance is already running; exiting")
            return 0
        if _is_serve_json_healthy(target_serve_json):
            logger.info("Existing local-typeless serve is already healthy; exiting")
            _release_serve_lock(lock_fh)
            return 0

    try:
        app_config = load_config(config_path, overrides=overrides)
        editor_config, errors, resolved_cfg_path, _ = load_editor_config_with_errors(config_path)

        actual_port = port if port is not None else editor_config.server.port

        pipeline = Pipeline(app_config, stt=NullSTT(), outputs=[], records=False)
        clipboard_backend = Win32ClipboardBackend()

        server = EditorServer(
            pipeline=pipeline,
            clipboard_backend=clipboard_backend,
            editor_config=editor_config,
            app_config=app_config,
            serve_json_path=target_serve_json,
            port=actual_port,
            config_path=resolved_cfg_path,
            config_errors=errors,
            track_windows=(sys.platform == "win32"),
        )

        if hasattr(signal, "SIGTERM"):

            def _sigterm_handler(signum: int, frame: Any) -> None:
                threading.Thread(target=server.stop, daemon=True).start()

            try:
                signal.signal(signal.SIGTERM, _sigterm_handler)
            except Exception:
                pass

        try:
            server.serve_forever()
        except (KeyboardInterrupt, SystemExit):
            server.stop()

        return 0
    finally:
        _release_serve_lock(lock_fh)
