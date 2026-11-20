"""Tests for local-typeless serve HTTP server."""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from local_typeless.cli import _build_parser
from local_typeless.editor.config import EditorConfig
from local_typeless.editor.server import EditorServer, NullSTT
from local_typeless.types import Mode, PipelineResult


class FakePipeline:
    def __init__(self, fallback: bool = False, return_text: str = "refined output", error_msg: str = "") -> None:
        self.fallback = fallback
        self.return_text = return_text
        self.error_msg = error_msg
        self.last_text = ""
        self.last_mode = None

    def refine_only(self, text: str, mode: Mode, guard_warns: bool = False) -> PipelineResult:
        self.last_text = text
        self.last_mode = mode
        return PipelineResult(
            raw_text=text,
            final_text=self.return_text if not self.fallback else text,
            mode=mode,
            stt_profile_name="test_stt",
            llm_profile_name="test_llm",
            stt_elapsed_sec=0.0,
            llm_elapsed_sec=0.15,
            fallback_triggered=self.fallback,
            error_message=self.error_msg,
            guard_warnings=["warning 1"] if guard_warns else [],
        )


class FakeClipboardBackend:
    def __init__(self, png_bytes: Optional[List[bytes]] = None, text: str = "") -> None:
        self._png_bytes = png_bytes or []
        self._text = text

    def grab_png(self) -> List[bytes]:
        return list(self._png_bytes)

    def grab_text(self) -> str:
        return self._text


@pytest.fixture
def running_server(tmp_path: Path):
    serve_json_path = tmp_path / "serve.json"
    prompts_root = tmp_path / "prompts"
    prompts_root.mkdir()

    editor_cfg = EditorConfig(prompts_root=str(prompts_root))
    pipeline = FakePipeline()
    clipboard = FakeClipboardBackend()

    server = EditorServer(
        pipeline=pipeline,
        clipboard_backend=clipboard,
        editor_config=editor_cfg,
        token="test_secret_token",
        serve_json_path=serve_json_path,
        port=0,
    )
    server.start()

    yield server, pipeline, clipboard, serve_json_path, prompts_root

    server.stop()


def _make_request(
    server: EditorServer,
    path: str,
    method: str = "GET",
    token: Optional[str] = "test_secret_token",
    origin: Optional[str] = None,
    json_data: Optional[Dict[str, Any]] = None,
    raw_body: Optional[bytes] = None,
) -> tuple[int, dict, dict]:
    url = f"http://127.0.0.1:{server.port}{path}"
    headers = {}
    if token is not None:
        headers["X-LT-Token"] = token
    if origin is not None:
        headers["Origin"] = origin

    data_bytes = None
    if json_data is not None:
        data_bytes = json.dumps(json_data).encode("utf-8")
        headers["Content-Type"] = "application/json"
    elif raw_body is not None:
        data_bytes = raw_body

    req = urllib.request.Request(url, data=data_bytes, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            resp_headers = dict(resp.headers)
            body = json.loads(resp.read().decode("utf-8"))
            return resp.status, body, resp_headers
    except urllib.error.HTTPError as e:
        resp_headers = dict(e.headers)
        body = json.loads(e.read().decode("utf-8"))
        return e.code, body, resp_headers


def test_health_endpoint(running_server):
    server, _, _, _, prompts_root = running_server
    code, body, _ = _make_request(server, "/health", method="GET")
    assert code == 200
    assert body["ok"] is True
    assert body["version"] == "1.0.0"
    assert "editor" in body
    assert body["editor"]["prompts_root"] == str(prompts_root)
    assert body["editor"]["default_mode"] == "clean"


def test_unauthorized_missing_token(running_server):
    server, _, _, _, _ = running_server
    code, body, _ = _make_request(server, "/health", method="GET", token=None)
    assert code == 401
    assert body["ok"] is False
    assert body["error"] == "unauthorized"


def test_unauthorized_wrong_token(running_server):
    server, _, _, _, _ = running_server
    code, body, _ = _make_request(server, "/health", method="GET", token="wrong_token")
    assert code == 401
    assert body["ok"] is False
    assert body["error"] == "unauthorized"


def test_origin_header_forbidden(running_server):
    server, _, _, _, _ = running_server
    code, body, headers = _make_request(
        server,
        "/health",
        method="GET",
        origin="http://evil.example.com",
    )
    assert code == 403
    assert body["ok"] is False
    assert body["error"] == "origin_forbidden"
    assert "Access-Control-Allow-Origin" not in headers


def test_payload_too_large(running_server):
    import http.client

    server, _, _, _, _ = running_server
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5.0)
    headers = {
        "X-LT-Token": "test_secret_token",
        "Content-Type": "application/json",
        "Content-Length": str(2 * 1024 * 1024 + 10),
    }
    conn.request("POST", "/refine", headers=headers)
    resp = conn.getresponse()
    assert resp.status == 413
    body = json.loads(resp.read().decode("utf-8"))
    assert body["ok"] is False
    assert body["error"] == "payload_too_large"
    conn.close()


def test_refine_endpoint_success_with_protect(running_server):
    server, pipeline, _, _, _ = running_server
    pipeline.return_text = "這裡請參考 [文字1] 處理完畢"

    req_data = {
        "text": "這裡請參考 C:\\data\\file.txt 處理完畢",
        "mode": "clean",
        "protect": True,
    }
    code, body, _ = _make_request(server, "/refine", method="POST", json_data=req_data)
    assert code == 200
    assert body["ok"] is True
    assert body["text"] == "這裡請參考 C:\\data\\file.txt 處理完畢"
    assert body["fallback"] is False
    assert "warnings" in body
    assert pipeline.last_mode == Mode.CLEAN


def test_refine_endpoint_invalid_mode(running_server):
    server, _, _, _, _ = running_server
    req_data = {"text": "hello", "mode": "invalid_mode"}
    code, body, _ = _make_request(server, "/refine", method="POST", json_data=req_data)
    assert code == 400
    assert body["ok"] is False
    assert "invalid mode" in body["error"]


def test_refine_endpoint_empty_text(running_server):
    server, _, _, _, _ = running_server
    req_data = {"text": "   ", "mode": "clean"}
    code, body, _ = _make_request(server, "/refine", method="POST", json_data=req_data)
    assert code == 400
    assert body["ok"] is False


def test_refine_endpoint_fallback(running_server):
    server, pipeline, _, _, _ = running_server
    pipeline.fallback = True
    pipeline.error_msg = "connection timed out"

    req_data = {"text": "some input", "mode": "clean"}
    code, body, _ = _make_request(server, "/refine", method="POST", json_data=req_data)
    assert code == 200
    assert body["ok"] is False
    assert body["fallback"] is True
    assert "timed out" in body["error"]


def test_refine_endpoint_marker_lost_error(running_server):
    server, pipeline, _, _, _ = running_server
    pipeline.return_text = "LLM dropped the marker completely"

    req_data = {
        "text": "Look at C:\\important\\path.txt here",
        "mode": "clean",
        "protect": True,
    }
    code, body, _ = _make_request(server, "/refine", method="POST", json_data=req_data)
    assert code == 200
    assert body["ok"] is False
    assert body["fallback"] is False
    assert "遺失" in body["error"]


def test_clipboard_capture_with_image(running_server):
    server, _, clipboard, _, prompts_root = running_server
    clipboard._png_bytes = [b"\x89PNG\r\n\x1a\nfakeimagebytes1", b"\x89PNG\r\n\x1a\nfakeimagebytes2"]

    req_data = {"prompt_path": "my_test_prompt.refined.md"}
    code, body, _ = _make_request(server, "/clipboard/capture", method="POST", json_data=req_data)

    assert code == 200
    assert body["kind"] == "image"
    assert len(body["paths"]) == 2

    p1 = Path(body["paths"][0])
    p2 = Path(body["paths"][1])
    assert p1.exists()
    assert p2.exists()
    assert p1.name == "my_test_prompt_01.png"
    assert p2.name == "my_test_prompt_02.png"

    # Capture again to verify numbering increments
    clipboard._png_bytes = [b"\x89PNG\r\n\x1a\nfakeimagebytes3"]
    code2, body2, _ = _make_request(server, "/clipboard/capture", method="POST", json_data=req_data)
    assert code2 == 200
    p3 = Path(body2["paths"][0])
    assert p3.name == "my_test_prompt_03.png"


def test_clipboard_capture_no_image(running_server):
    server, _, clipboard, _, _ = running_server
    clipboard._png_bytes = []
    clipboard._text = "just text"

    code, body, _ = _make_request(server, "/clipboard/capture", method="POST", json_data={})
    assert code == 200
    assert body["kind"] == "text"
    assert body["paths"] == []


def test_serve_json_lifecycle(tmp_path: Path):
    serve_json_path = tmp_path / "serve.json"
    server = EditorServer(
        pipeline=FakePipeline(),
        token="tok123",
        serve_json_path=serve_json_path,
        port=0,
    )
    assert serve_json_path.exists()
    data = json.loads(serve_json_path.read_text(encoding="utf-8"))
    assert data["port"] == server.port
    assert data["token"] == "tok123"
    assert data["pid"] == os.getpid()

    server.stop()
    assert not serve_json_path.exists()


def test_null_stt_cannot_transcribe():
    stt = NullSTT()
    with pytest.raises(RuntimeError, match="NullSTT cannot transcribe"):
        stt.transcribe(Path("test.wav"))


def test_cli_parser_serve_command():
    parser = _build_parser()
    args = parser.parse_args(["serve", "--port", "9999"])
    assert args.command == "serve"
    assert args.port == 9999


def test_serve_does_not_load_faster_whisper():
    # Verify faster_whisper has not been imported into sys.modules
    assert "faster_whisper" not in sys.modules


def test_negative_content_length(running_server):
    import http.client

    server, _, _, _, _ = running_server
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5.0)
    headers = {
        "X-LT-Token": "test_secret_token",
        "Content-Type": "application/json",
        "Content-Length": "-1",
    }
    conn.request("POST", "/refine", headers=headers)
    resp = conn.getresponse()
    assert resp.status == 400
    body = json.loads(resp.read().decode("utf-8"))
    assert body["ok"] is False
    assert body["error"] == "invalid_content_length"
    conn.close()


def test_refine_endpoint_internal_error_500(running_server):
    server, pipeline, _, _, _ = running_server

    def raise_exc(*args, **kwargs):
        raise RuntimeError("simulated unexpected crash")

    pipeline.refine_only = raise_exc

    req_data = {"text": "some input", "mode": "clean"}
    code, body, _ = _make_request(server, "/refine", method="POST", json_data=req_data)
    assert code == 500
    assert body["ok"] is False
    assert "RuntimeError: simulated unexpected crash" in body["error"]


def test_clipboard_capture_fixed_image_name_returns_400(running_server):
    server, _, clipboard, _, _ = running_server
    server.editor_config.paste.image_name = "fixed.png"
    clipboard._png_bytes = [b"\x89PNG\r\n\x1a\nfirst"]

    code1, body1, _ = _make_request(server, "/clipboard/capture", method="POST", json_data={})
    assert code1 == 200
    assert body1["kind"] == "image"

    clipboard._png_bytes = [b"\x89PNG\r\n\x1a\nsecond"]
    code2, body2, _ = _make_request(server, "/clipboard/capture", method="POST", json_data={})
    assert code2 == 400
    assert body2["ok"] is False
    assert "{n}" in body2["error"]
