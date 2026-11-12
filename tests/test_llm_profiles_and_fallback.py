"""LLM client tests (httpx.MockTransport only; no network)."""
from __future__ import annotations

import json

import httpx
import pytest

from local_typeless.llm import create_llm_client
from local_typeless.llm.openai_compat import OpenAICompatibleClient, strip_think
from local_typeless.llm.gateway_adapter import GatewayAdapter
from local_typeless.types import (
    LLMConnectionError,
    LLMError,
    LLMProfileConfig,
    LLMResponseError,
    LLMTimeoutError,
)


def ok(content):
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def client(handler, cls=OpenAICompatibleClient, **prof):
    return cls(LLMProfileConfig(**prof), transport=httpx.MockTransport(handler))


def test_factory_dispatch_and_unknown():
    assert type(create_llm_client(LLMProfileConfig())) is OpenAICompatibleClient
    assert type(create_llm_client(LLMProfileConfig(provider="gateway"))) is GatewayAdapter
    with pytest.raises(ValueError):
        create_llm_client(LLMProfileConfig(provider="nope"))


def test_request_shape_headers_and_url(monkeypatch):
    monkeypatch.setenv("MY_KEY", "secret")
    seen = {}

    def h(req):
        seen["url"] = str(req.url)
        seen["auth"] = req.headers["authorization"]
        seen["x"] = req.headers["x-team"]
        seen["body"] = json.loads(req.content)
        return ok("done")

    c = client(h, base_url="http://h/v1/", api_key_env="MY_KEY", extra_headers={"X-Team": "a"},
               max_tokens=77, model="m", temperature=0.1)
    assert c.refine("raw", "sys") == "done"
    assert seen["url"] == "http://h/v1/chat/completions"
    assert seen["auth"] == "Bearer secret"
    assert seen["x"] == "a"
    b = seen["body"]
    assert b["max_tokens"] == 77 and b["model"] == "m" and b["temperature"] == 0.1
    assert b["messages"] == [{"role": "system", "content": "sys"}, {"role": "user", "content": "raw"}]


@pytest.mark.parametrize("val", [None, "", "EMPTY"])
def test_bearer_empty(monkeypatch, val):
    monkeypatch.delenv("K_X", raising=False)
    if val is not None:
        monkeypatch.setenv("K_X", val)
    seen = {}

    def h(req):
        seen["auth"] = req.headers["authorization"]
        return ok("x")

    client(h, api_key_env="K_X").refine("a", "b")
    assert seen["auth"] == "Bearer EMPTY"


@pytest.mark.parametrize("raw,want", [
    ("<think>plan\nmore</think>\nFinal", "Final"),
    ("<think>a</think>X<think>b</think>Y", "XY"),
    ("reasoning...</think>Answer", "Answer"),
    ("<think>never closed", ""),
    ("plain", "plain"),
])
def test_strip_think(raw, want):
    assert strip_think(raw) == want


def test_refine_strips_think():
    assert client(lambda r: ok("<think>x</think>Hi")).refine("a", "b") == "Hi"


def test_empty_after_strip_raises():
    with pytest.raises(LLMResponseError):
        client(lambda r: ok("<think>unterminated")).refine("a", "b")
    with pytest.raises(LLMResponseError):
        client(lambda r: ok("")).refine("a", "b")


def test_timeout_raises():
    def h(req):
        raise httpx.ReadTimeout("slow")

    with pytest.raises(LLMTimeoutError):
        client(h).refine("a", "b")


def test_connection_error_raises():
    def h(req):
        raise httpx.ConnectError("refused")

    with pytest.raises(LLMConnectionError):
        client(h).refine("a", "b")


def test_http_500_raises():
    with pytest.raises(LLMResponseError):
        client(lambda r: httpx.Response(500, text="boom")).refine("a", "b")


@pytest.mark.parametrize("resp", [
    httpx.Response(200, text="not json"),
    httpx.Response(200, json={"choices": []}),
    httpx.Response(200, json={"nope": 1}),
])
def test_bad_payload_raises(resp):
    with pytest.raises(LLMResponseError):
        client(lambda r: resp).refine("a", "b")


def test_all_errors_are_llmerror():
    with pytest.raises(LLMError):
        client(lambda r: httpx.Response(503)).refine("a", "b")


def test_list_models_ok_and_fail():
    seen = {}

    def h(req):
        seen["url"] = str(req.url)
        return httpx.Response(200, json={"data": [{"id": "a"}, {"id": "b"}]})

    assert client(h, base_url="http://h/v1").list_models() == ["a", "b"]
    assert seen["url"] == "http://h/v1/models"
    with pytest.raises(LLMError):
        client(lambda r: httpx.Response(401)).list_models()

    def boom(req):
        raise httpx.ConnectError("x")

    with pytest.raises(LLMConnectionError):
        client(boom).list_models()


def test_gateway_hooks_used():
    class Custom(GatewayAdapter):
        def _transform_request(self, payload):
            return {"q": payload["messages"][-1]["content"]}

        def _extract_response(self, data):
            return data["answer"]

    seen = {}

    def h(req):
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"answer": "ok"})

    assert client(h, cls=Custom, provider="gateway").refine("hello", "s") == "ok"
    assert seen["body"] == {"q": "hello"}


def test_gateway_default_is_openai_format():
    c = client(lambda r: ok("hi"), cls=GatewayAdapter, provider="gateway")
    assert c.refine("a", "b") == "hi"


def test_invalid_url_maps_to_connection_error():
    with pytest.raises(LLMConnectionError):
        client(lambda r: ok("x"), base_url="http://\x00bad").refine("a", "b")


def test_unsupported_protocol_and_other_http_errors_map_to_connection_error():
    for exc in (httpx.UnsupportedProtocol("ftp"), httpx.DecodingError("zip"),
                httpx.TooManyRedirects("loop")):
        def h(req, exc=exc):
            raise exc

        with pytest.raises(LLMConnectionError):
            client(h).refine("a", "b")


def test_extra_body_merged_but_cannot_override_model_or_messages(caplog):
    import logging

    seen = {}

    def h(req):
        seen["body"] = json.loads(req.content)
        return ok("x")

    extra = {"chat_template_kwargs": {"enable_thinking": False}, "top_p": 0.9,
             "model": "evil", "messages": []}
    with caplog.at_level(logging.WARNING):
        client(h, model="m", extra_body=extra).refine("raw", "sys")
    body = seen["body"]
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert body["top_p"] == 0.9
    assert body["model"] == "m" and len(body["messages"]) == 2
    assert sum("protected" in r.message for r in caplog.records) == 2


def test_extra_body_reaches_gateway_before_transform():
    seen = {}

    class Custom(GatewayAdapter):
        def _transform_request(self, payload):
            seen["in"] = dict(payload)
            return payload

    client(lambda r: ok("x"), cls=Custom, provider="gateway", extra_body={"top_k": 5}).refine("a", "b")
    assert seen["in"]["top_k"] == 5


def test_blank_lines_around_content_are_stripped():
    assert client(lambda r: ok("\n\n  Answer text \n\n")).refine("a", "b") == "Answer text"
    assert client(lambda r: ok("\n\n<think>r</think>\n\nFinal\n")).refine("a", "b") == "Final"


# ------------------------------------------------------- registry env fallback
class FakeWinreg:
    HKEY_CURRENT_USER = "HKCU"
    HKEY_LOCAL_MACHINE = "HKLM"

    def __init__(self, data):
        self.data = data   # {(hive, subkey): {name: value}}

    def OpenKey(self, hive, subkey):
        if (hive, subkey) not in self.data:
            raise FileNotFoundError(subkey)
        outer = self

        class Key:
            def __enter__(s):
                return (hive, subkey)

            def __exit__(s, *a):
                return False

        return Key()

    def QueryValueEx(self, key, name):
        vals = self.data[key]
        if name not in vals:
            raise FileNotFoundError(name)
        return vals[name], 1


HKCU = ("HKCU", "Environment")
HKLM = ("HKLM", r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment")


def test_resolve_key_prefers_process_env(monkeypatch):
    from local_typeless.llm.openai_compat import resolve_api_key

    monkeypatch.setenv("K_R", "from-env")
    reg = FakeWinreg({HKCU: {"K_R": "from-reg"}})
    assert resolve_api_key("K_R", reg) == "from-env"


def test_resolve_key_falls_back_to_hkcu_then_hklm(monkeypatch):
    from local_typeless.llm.openai_compat import resolve_api_key

    monkeypatch.delenv("K_R", raising=False)
    both = FakeWinreg({HKCU: {"K_R": "user-val"}, HKLM: {"K_R": "machine-val"}})
    assert resolve_api_key("K_R", both) == "user-val"
    only_machine = FakeWinreg({HKLM: {"K_R": "machine-val"}})
    assert resolve_api_key("K_R", only_machine) == "machine-val"
    assert resolve_api_key("K_R", FakeWinreg({})) == ""
    assert resolve_api_key("", both) == ""


def test_empty_env_var_also_falls_back(monkeypatch):
    from local_typeless.llm.openai_compat import resolve_api_key

    monkeypatch.setenv("K_R", "  ")
    assert resolve_api_key("K_R", FakeWinreg({HKCU: {"K_R": "reg"}})) == "reg"


def test_registry_key_used_in_header_and_never_logged(monkeypatch, caplog):
    import logging

    import local_typeless.llm.openai_compat as m

    monkeypatch.delenv("K_R", raising=False)
    reg = FakeWinreg({HKCU: {"K_R": "sup3r-secret"}})
    real = m._read_registry_env
    monkeypatch.setattr(m, "_read_registry_env", lambda name, mod=None: real(name, reg))
    seen = {}

    def h(req):
        seen["auth"] = req.headers["authorization"]
        return httpx.Response(401, text="no")

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(LLMResponseError):
            client(h, api_key_env="K_R").refine("a", "b")
    assert seen["auth"] == "Bearer sup3r-secret"
    assert "sup3r-secret" not in caplog.text


def test_registry_errors_never_break_requests(monkeypatch):
    import local_typeless.llm.openai_compat as m

    monkeypatch.delenv("K_R", raising=False)

    def boom(name, mod=None):
        raise PermissionError("denied")

    monkeypatch.setattr(m, "_read_registry_env", boom)
    seen = {}

    def h(req):
        seen["auth"] = req.headers["authorization"]
        return ok("x")

    client(h, api_key_env="K_R").refine("a", "b")
    assert seen["auth"] == "Bearer EMPTY"


def test_finish_reason_length_raises_truncated():
    def h_len(req):
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": "partially generated..."},
                        "finish_reason": "length",
                    }
                ]
            },
        )

    with pytest.raises(LLMResponseError, match="truncated"):
        client(h_len).refine("a", "b")

    def h_stop(req):
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": "full text"},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    assert client(h_stop).refine("a", "b") == "full text"

    def h_none(req):
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "full text"}}]},
        )

    assert client(h_none).refine("a", "b") == "full text"



# ---- gateway compatibility fields (first-connection knobs for an internal gateway) ----

def test_custom_key_header_prefix_and_paths(monkeypatch):
    monkeypatch.setenv("GW_KEY", "k123")
    seen = {}

    def h(req):
        seen.setdefault("urls", []).append(str(req.url))
        seen["headers"] = dict(req.headers)
        if req.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "m1"}]})
        return ok("done")

    c = client(h, cls=GatewayAdapter, provider="gateway", base_url="https://gw/api/", api_key_env="GW_KEY",
               api_key_header="api-key", api_key_prefix="", chat_path="llm/chat", models_path="/llm/models")
    assert c.refine("a", "b") == "done"
    assert c.list_models() == ["m1"]
    assert seen["urls"] == ["https://gw/api/llm/chat", "https://gw/api/llm/models"]
    assert seen["headers"]["api-key"] == "k123"
    assert "authorization" not in seen["headers"]


def test_empty_models_path_skips_request():
    def h(req):
        raise AssertionError("no request expected")

    assert client(h, models_path="").list_models() == []


def test_build_ssl_context_modes(tmp_path, monkeypatch):
    import ssl

    import certifi

    from local_typeless.llm.openai_compat import build_ssl_context

    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    assert build_ssl_context(False) is False
    ctx = build_ssl_context(True)
    assert isinstance(ctx, ssl.SSLContext) and ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname
    assert isinstance(build_ssl_context(True, certifi.where()), ssl.SSLContext)
    assert isinstance(build_ssl_context(True, str(tmp_path)), ssl.SSLContext)  # directory form
    with pytest.raises((OSError, ssl.SSLError)):
        build_ssl_context(True, str(tmp_path / "missing.pem"))


def test_bad_ca_bundle_is_a_connection_error_and_client_flags(tmp_path, monkeypatch):
    from local_typeless.llm import openai_compat as oc
    from local_typeless.types import LLMConnectionError as ConnErr

    c = OpenAICompatibleClient(LLMProfileConfig(base_url="https://gw/v1", ca_bundle=str(tmp_path / "nope.pem")))
    with pytest.raises(ConnErr, match="ca_bundle"):
        c.refine("a", "b")

    seen = {}

    class FakeClient:
        def __init__(self, **kw):
            seen.update(kw)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def request(self, method, url, **kw):
            return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}]})

    monkeypatch.setattr(oc.httpx, "Client", FakeClient)
    c = OpenAICompatibleClient(LLMProfileConfig(base_url="https://gw/v1", use_env_proxy=False, verify_tls=False))
    assert c.refine("a", "b") == "hi"
    assert seen["trust_env"] is False and seen["verify"] is False and "proxy" not in seen
    c = OpenAICompatibleClient(LLMProfileConfig(base_url="https://gw/v1", proxy="http://px:8080"))
    assert c.refine("a", "b") == "hi"
    assert seen["proxy"] == "http://px:8080" and seen["trust_env"] is True
