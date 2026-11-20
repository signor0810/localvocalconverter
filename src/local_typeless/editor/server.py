"""Local HTTP serve process for editor integrations (Sublime Text / external workspace)."""
from __future__ import annotations

import hmac
import json
import logging
import os
import secrets
import signal
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from local_typeless import __version__
from local_typeless.capture.clipboard_images import Win32ClipboardBackend
from local_typeless.editor.config import EditorConfig, load_editor_config
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
        else:
            self._send_json(404, {"ok": False, "error": "not_found"})

    def _handle_health(self) -> None:
        editor_cfg = self.editor_server.editor_config
        active_llm = self.editor_server.app_config.active_llm_profile if self.editor_server.app_config else ""
        resp = {
            "ok": True,
            "version": __version__,
            "llm_profile": active_llm,
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

    def _handle_refine(self, body: Dict[str, Any]) -> None:
        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            self._send_json(400, {"ok": False, "error": "text is required"})
            return

        editor_cfg = self.editor_server.editor_config
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
            result = self.editor_server.pipeline.refine_only(protected_text, Mode(mode_val), guard_warns=True)
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
        png_list = backend.grab_png()
        retries = 2
        while not png_list and retries > 0:
            time.sleep(0.05)
            png_list = backend.grab_png()
            retries -= 1

        valid_pngs = [b for b in png_list if b]
        if not valid_pngs:
            self._send_json(200, {"kind": "text", "paths": []})
            return

        editor_cfg = self.editor_server.editor_config
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
                    self._send_json(400, {"ok": False, "error": "invalid paste template: image_name must vary with {n}"})
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

        self._send_json(200, {"kind": "image", "paths": saved_paths})


class EditorServer:
    def __init__(
        self,
        *,
        pipeline: Any,
        clipboard_backend: Optional[Any] = None,
        editor_config: Optional[EditorConfig] = None,
        app_config: Optional[AppConfig] = None,
        token: Optional[str] = None,
        serve_json_path: Optional[Path] = None,
        port: int = 8765,
    ) -> None:
        self.pipeline = pipeline
        self.clipboard_backend = clipboard_backend if clipboard_backend is not None else Win32ClipboardBackend()
        self.editor_config = editor_config or EditorConfig()
        self.app_config = app_config
        self.token = token or secrets.token_hex(16)
        self.serve_json_path = serve_json_path or default_serve_json_path()
        self.requested_port = port

        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), _EditorRequestHandler)
        self.httpd.editor_server = self
        self.port = self.httpd.server_address[1]

        write_serve_json(self.serve_json_path, self.port, self.token, os.getpid())
        self._thread: Optional[threading.Thread] = None
        self._serving: bool = False

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
        """Stop server and clean up serve.json."""
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
) -> int:
    """Run local-typeless serve daemon."""
    from local_typeless.config.loader import load_config
    from local_typeless.pipeline import Pipeline

    app_config = load_config(config_path, overrides=overrides)
    editor_config = load_editor_config(config_path)

    actual_port = port if port is not None else editor_config.server.port

    pipeline = Pipeline(app_config, stt=NullSTT(), outputs=[], records=False)
    clipboard_backend = Win32ClipboardBackend()

    server = EditorServer(
        pipeline=pipeline,
        clipboard_backend=clipboard_backend,
        editor_config=editor_config,
        app_config=app_config,
        port=actual_port,
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
