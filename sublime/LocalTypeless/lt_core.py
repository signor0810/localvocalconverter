"""
Local Typeless core logic for Sublime Text plugin.
Pure Python 3.8 standard library. No sublime or third-party dependencies.
"""

import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_EDITOR_CONFIG = {
    "prompts_root": "~/Documents/prompts",
    "file_layout": "{yyyy}/{mm}/{date}_{time}_{title}.md",
    "autosave_delay_ms": 1000,
    "path_style": "windows",
    "quote_paths_with_spaces": True,
    "default_mode": "clean",
    "result_view": "split_right",
    "frontmatter_fields": [
        "created",
        "status",
        "mode",
        "refined_at",
        "sent_at",
        "target",
    ],
}


def default_serve_json_path() -> str:
    """Returns the default path to serve.json under %LOCALAPPDATA%."""
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        local_app_data = os.path.expanduser("~")
    return os.path.join(local_app_data, "local-typeless", "serve.json")


def read_serve_info(path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    Reads port and token from serve.json.
    Returns dict {"port": int, "token": str, "pid": int} or None on error/missing.
    """
    if path is None:
        path = default_serve_json_path()
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and "port" in data and "token" in data:
            return {
                "port": int(data["port"]),
                "token": str(data["token"]),
                "pid": int(data.get("pid", 0)),
            }
    except Exception:
        return None
    return None


def format_path(path: str, style: str = "windows", quote_spaces: bool = True) -> str:
    """
    Formats a file path according to style:
    - windows: backslashes (C:\\a\\b.png)
    - forward: forward slashes (C:/a/b.png)
    - wsl: /mnt/c/a/b.png
    - markdown: ![](forward_path)
    If path contains spaces (and not markdown), wraps in double quotes if quote_spaces=True.
    """
    p = str(path).strip()
    if style == "windows":
        formatted = p.replace("/", "\\")
    elif style == "forward":
        formatted = p.replace("\\", "/")
    elif style == "wsl":
        norm = p.replace("\\", "/")
        if len(norm) >= 2 and norm[1] == ":" and norm[0].isalpha():
            drive = norm[0].lower()
            rest = norm[2:]
            if not rest.startswith("/"):
                rest = "/" + rest
            formatted = "/mnt/" + drive + rest
        else:
            formatted = norm
    elif style == "markdown":
        norm = p.replace("\\", "/")
        return "![" + "](" + norm + ")"
    else:
        formatted = p

    if " " in formatted and quote_spaces:
        return '"' + formatted + '"'
    return formatted


def parse_frontmatter(text: str) -> Tuple[Dict[str, str], str, int]:
    """
    Parses flat key: value frontmatter from text starting with ---.
    Returns (fields_dict, body_text, body_offset).
    If no frontmatter is found, returns ({}, text, 0).
    """
    if not text or not text.startswith("---"):
        return {}, text, 0

    first_nl = text.find("\n")
    if first_nl == -1:
        return {}, text, 0
    first_line = text[:first_nl].rstrip("\r")
    if first_line != "---":
        return {}, text, 0

    rest = text[first_nl + 1 :]
    pattern = re.compile(r"^---\s*$", re.MULTILINE)
    m = pattern.search(rest)
    if not m:
        return {}, text, 0

    fm_raw = rest[: m.start()]
    closing_end = first_nl + 1 + m.end()

    body_offset = closing_end
    if body_offset < len(text) and text[body_offset] == "\r":
        body_offset += 1
    if body_offset < len(text) and text[body_offset] == "\n":
        body_offset += 1

    body = text[body_offset:]

    fields = {}
    for line in fm_raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        colon_idx = line.find(":")
        if colon_idx != -1:
            key = line[:colon_idx].strip()
            val = line[colon_idx + 1 :].strip()
            fields[key] = val

    return fields, body, body_offset


def render_frontmatter(
    fields: Dict[str, str],
    field_order: Optional[List[str]] = None,
) -> str:
    """
    Renders fields into standard frontmatter block:
    ---
    key: value
    key:
    ---

    """
    if field_order is None:
        field_order = [
            "created",
            "status",
            "mode",
            "refined_at",
            "sent_at",
            "target",
        ]
    lines = ["---"]
    seen = set()
    for k in field_order:
        if k in fields:
            val = fields[k]
            if val is None or val == "":
                lines.append(k + ":")
            else:
                lines.append(k + ": " + str(val))
            seen.add(k)
    for k, val in fields.items():
        if k not in seen:
            if val is None or val == "":
                lines.append(k + ":")
            else:
                lines.append(k + ": " + str(val))
    lines.append("---")
    return "\n".join(lines) + "\n\n"


def sanitize_title(title: str) -> str:
    """Removes illegal filename characters."""
    illegal_chars = r'[\\/:*?"<>|\r\n\t]'
    cleaned = re.sub(illegal_chars, "", title).strip()
    return cleaned if cleaned else "untitled"


def expand_file_layout(
    prompts_root: str,
    file_layout: str,
    now: Optional[datetime] = None,
    title: str = "untitled",
) -> str:
    """
    Expands file_layout using now and title, prepends prompts_root.
    If the target file already exists, appends _2, _3, ... before the extension.
    """
    if now is None:
        now = datetime.now()

    clean_title = sanitize_title(title)
    yyyy = f"{now.year:04d}"
    mm = f"{now.month:02d}"
    dd = f"{now.day:02d}"
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H%M%S")

    expanded_rel = (
        file_layout.replace("{yyyy}", yyyy)
        .replace("{mm}", mm)
        .replace("{dd}", dd)
        .replace("{date}", date_str)
        .replace("{time}", time_str)
        .replace("{title}", clean_title)
    )

    expanded_root = os.path.expanduser(prompts_root)
    full_path = os.path.normpath(os.path.join(expanded_root, expanded_rel))

    if os.path.exists(full_path):
        base, ext = os.path.splitext(full_path)
        counter = 2
        candidate = f"{base}_{counter}{ext}"
        while os.path.exists(candidate):
            counter += 1
            candidate = f"{base}_{counter}{ext}"
        full_path = candidate

    return full_path


def is_managed_path(file_path: str, prompts_root: str) -> bool:
    """
    Returns True if file_path is a .md file strictly inside prompts_root.
    Performs case-insensitive normalized comparison on Windows.
    """
    if not file_path or not prompts_root:
        return False
    if not file_path.lower().endswith(".md"):
        return False
    try:
        norm_file = os.path.normcase(os.path.abspath(os.path.expanduser(file_path)))
        norm_root = os.path.normcase(os.path.abspath(os.path.expanduser(prompts_root)))
        common = os.path.commonpath([norm_file, norm_root])
        return common == norm_root and norm_file != norm_root
    except (ValueError, Exception):
        return False


def is_refined_path(file_path: str) -> bool:
    """Returns True if file ends with .refined.md (case-insensitive)."""
    if not file_path:
        return False
    return file_path.lower().endswith(".refined.md")


def refined_path_for(prompt_path: str) -> str:
    """
    Returns the refined companion path for prompt_path:
    foo.md -> foo.refined.md
    foo.refined.md -> foo.refined.md
    """
    if not prompt_path:
        return ""
    if is_refined_path(prompt_path):
        return prompt_path
    if prompt_path.lower().endswith(".md"):
        return prompt_path[:-3] + ".refined.md"
    return prompt_path + ".refined.md"


def request_server(
    endpoint: str,
    method: str = "GET",
    payload: Optional[Dict[str, Any]] = None,
    serve_path: Optional[str] = None,
    timeout: float = 2.0,
) -> Tuple[bool, Optional[Dict[str, Any]], str]:
    """
    Sends an HTTP request to local-typeless serve.
    Re-reads serve.json on every call.
    Attaches X-LT-Token, never sends Origin.
    Returns (ok, data_dict, error_message).
    """
    serve_info = read_serve_info(serve_path)
    if not serve_info:
        return False, None, "未找到 serve.json，後端可能尚未啟動"

    port = serve_info["port"]
    token = serve_info["token"]
    url = "http://127.0.0.1:" + str(port) + endpoint

    headers = {
        "X-LT-Token": token,
    }
    data_bytes = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data_bytes = json.dumps(payload).encode("utf-8")

    req = urllib.request.Request(url, data=data_bytes, headers=headers, method=method)
    if "Origin" in req.headers:
        del req.headers["Origin"]
    if "origin" in req.headers:
        del req.headers["origin"]

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp_body = resp.read().decode("utf-8")
            return True, json.loads(resp_body), ""
    except urllib.error.HTTPError as e:
        err_msg = "HTTP " + str(e.code)
        try:
            err_body = e.read().decode("utf-8")
            err_json = json.loads(err_body)
            if "error" in err_json:
                err_msg = str(err_json["error"])
        except Exception:
            pass
        return False, None, err_msg
    except Exception as e:
        return False, None, str(e)