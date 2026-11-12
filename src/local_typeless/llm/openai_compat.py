"""OpenAI-compatible Chat Completions client (vLLM / Ollama / SGLang / internal gateways)."""
from __future__ import annotations

import logging
import os
import re
import ssl
import sys
from typing import Any, Dict, List, Optional, Union

import httpx

from local_typeless.llm.base import LLMClient
from local_typeless.types import (
    LLMConnectionError,
    LLMProfileConfig,
    LLMResponseError,
    LLMTimeoutError,
)

logger = logging.getLogger(__name__)
_PROTECTED_KEYS = {"model", "messages"}

_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_THINK_OPEN = re.compile(r"^\s*<think>.*\Z", re.DOTALL | re.IGNORECASE)


_REG_USER = ("HKEY_CURRENT_USER", r"Environment")
_REG_MACHINE = (
    "HKEY_LOCAL_MACHINE",
    r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
)


def _read_registry_env(name: str, winreg_mod: Any = None) -> str:
    """Windows: read a persistent environment variable straight from the registry.

    A process started from Explorer (desktop shortcut, wscript -> pythonw) inherits a
    possibly stale environment, so a later `setx` is invisible to os.environ. The value is
    never logged."""
    if winreg_mod is None:
        if sys.platform != "win32":
            return ""
        import winreg as winreg_mod
    for hive_name, subkey in (_REG_USER, _REG_MACHINE):
        try:
            with winreg_mod.OpenKey(getattr(winreg_mod, hive_name), subkey) as key:
                value, _type = winreg_mod.QueryValueEx(key, name)
        except OSError:
            continue
        if isinstance(value, str) and value.strip():
            return os.path.expandvars(value).strip()
    return ""


def resolve_api_key(env_name: str, winreg_mod: Any = None) -> str:
    """os.environ first; on Windows fall back to HKCU then HKLM environment. '' if none."""
    if not env_name:
        return ""
    value = os.environ.get(env_name, "").strip()
    if value:
        return value
    try:
        return _read_registry_env(env_name, winreg_mod)
    except Exception as e:  # never let key lookup break a request
        logger.debug("registry env lookup failed for %s: %s", env_name, type(e).__name__)
        return ""


def build_ssl_context(verify_tls: bool = True, ca_bundle: Optional[str] = None) -> Union[ssl.SSLContext, bool]:
    """TLS trust for the LLM endpoint.

    httpx trusts only the certifi bundle by default, so an internal gateway signed by a
    corporate root CA fails verification even though browsers on the same PC accept it.
    Default here: certifi (or SSL_CERT_FILE / SSL_CERT_DIR) plus the operating system trust
    store, where corporate roots are normally installed. `ca_bundle` (PEM file or directory)
    replaces all of that. `verify_tls=False` turns verification off."""
    if not verify_tls:
        return False
    if ca_bundle:
        if os.path.isdir(ca_bundle):
            return ssl.create_default_context(capath=ca_bundle)
        return ssl.create_default_context(cafile=ca_bundle)  # missing/invalid file -> OSError/SSLError
    env_file, env_dir = os.environ.get("SSL_CERT_FILE"), os.environ.get("SSL_CERT_DIR")
    if env_file or env_dir:
        ctx = ssl.create_default_context(cafile=env_file or None, capath=env_dir or None)
    else:
        try:
            import certifi

            ctx = ssl.create_default_context(cafile=certifi.where())
        except Exception:  # certifi missing/broken: the OS store below still applies
            ctx = ssl.create_default_context()
    try:
        ctx.load_default_certs()  # Windows: ROOT + CA system stores
    except Exception as e:
        logger.debug("could not load OS trust store: %s", type(e).__name__)
    return ctx


def _join(base: str, path: str) -> str:
    return base.rstrip("/") + "/" + path.lstrip("/")


def strip_think(text: str) -> str:
    """Remove <think>...</think> blocks, an unterminated leading <think>,
    and a stray leading '...</think>' (template-injected opening tag)."""
    text = _THINK_BLOCK.sub("", text)
    if "</think>" in text.lower():
        text = re.split(r"</think>", text, flags=re.IGNORECASE)[-1]
    text = _THINK_OPEN.sub("", text)
    return text.strip()


class OpenAICompatibleClient(LLMClient):
    def __init__(
        self,
        profile: LLMProfileConfig,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        self._profile = profile
        self._transport = transport
        self._ssl: Union[ssl.SSLContext, bool, None] = None

    # ------------------------------------------------------------ helpers
    @property
    def _base(self) -> str:
        return self._profile.base_url.rstrip("/")

    def _headers(self) -> Dict[str, str]:
        key = resolve_api_key(self._profile.api_key_env)
        if not key or key == "EMPTY":
            key = "EMPTY"
        p = self._profile
        name = getattr(p, "api_key_header", "Authorization") or "Authorization"
        prefix = getattr(p, "api_key_prefix", "Bearer ")
        headers = {name: f"{prefix}{key}", "Content-Type": "application/json"}
        headers.update(p.extra_headers or {})
        return headers

    def _verify(self) -> Union[ssl.SSLContext, bool]:
        if self._ssl is None:
            p = self._profile
            try:
                self._ssl = build_ssl_context(getattr(p, "verify_tls", True), getattr(p, "ca_bundle", None))
            except (OSError, ssl.SSLError) as e:
                raise LLMConnectionError(f"LLM connection failed: cannot load ca_bundle ({e})") from e
            if self._ssl is False:
                logger.warning("TLS certificate verification is DISABLED for this LLM profile (verify_tls: false)")
        return self._ssl

    def _request(self, method: str, url: str, **kw: Any) -> httpx.Response:
        p = self._profile
        client_kw: Dict[str, Any] = {"timeout": p.timeout_sec, "trust_env": getattr(p, "use_env_proxy", True)}
        if self._transport is not None:
            client_kw["transport"] = self._transport
        else:
            client_kw["verify"] = self._verify()
            if getattr(p, "proxy", None):
                client_kw["proxy"] = p.proxy
        try:
            with httpx.Client(**client_kw) as c:
                resp = c.request(method, url, headers=self._headers(), **kw)
        except httpx.TimeoutException as e:
            raise LLMTimeoutError(f"LLM request timed out after {p.timeout_sec}s: {e}") from e
        except (httpx.HTTPError, httpx.InvalidURL, httpx.StreamError) as e:
            raise LLMConnectionError(f"LLM connection failed: {e}") from e
        if resp.status_code >= 400:
            raise LLMResponseError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        return resp

    @staticmethod
    def _json(resp: httpx.Response) -> Any:
        try:
            return resp.json()
        except ValueError as e:
            raise LLMResponseError(f"Invalid JSON payload: {e}") from e

    # ------------------------------------------------- overridable hooks
    def _build_payload(self, raw_text: str, system_prompt: str) -> Dict[str, Any]:
        p = self._profile
        payload: Dict[str, Any] = {
            "model": p.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": raw_text},
            ],
            "temperature": p.temperature,
            "max_tokens": p.max_tokens,
        }
        for key, value in (getattr(p, "extra_body", None) or {}).items():
            if key in _PROTECTED_KEYS:
                logger.warning("extra_body key %r is protected and was ignored", key)
                continue
            payload[key] = value
        return payload

    def _transform_request(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return payload

    def _extract_response(self, data: Any) -> str:
        try:
            choice = data["choices"][0]
            finish_reason = choice.get("finish_reason") if isinstance(choice, dict) else None
        except (KeyError, IndexError, TypeError) as e:
            raise LLMResponseError(f"Unexpected response shape: {e}") from e
        if finish_reason == "length":
            raise LLMResponseError("LLM output truncated (finish_reason=length)")
        try:
            content = choice["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise LLMResponseError(f"Unexpected response shape: {e}") from e
        if not isinstance(content, str):
            raise LLMResponseError("Response content is not a string")
        return content

    # ---------------------------------------------------------------- API
    def refine(self, raw_text: str, system_prompt: str) -> str:
        payload = self._transform_request(self._build_payload(raw_text, system_prompt))
        resp = self._request("POST", _join(self._base, getattr(self._profile, "chat_path", "") or "/chat/completions"), json=payload)
        text = strip_think(self._extract_response(self._json(resp)))
        if not text:
            raise LLMResponseError("LLM returned empty content")
        return text

    def list_models(self) -> List[str]:
        path = getattr(self._profile, "models_path", "/models")
        if not path:  # gateway without a model listing: nothing to ask
            return []
        data = self._json(self._request("GET", _join(self._base, path)))
        try:
            return [m["id"] for m in data["data"]]
        except (KeyError, TypeError) as e:
            raise LLMResponseError(f"Unexpected /models payload: {e}") from e
