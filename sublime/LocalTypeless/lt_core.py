"""
Local Typeless core logic for Sublime Text plugin.
Pure Python 3.8 standard library. No sublime or third-party dependencies.
"""

import base64
import ctypes
import json
import os
import re
import shlex
import struct
import sys
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_EDITOR_CONFIG = {
    "prompts_root": "~/Documents/prompts",
    "file_layout": "{yyyy}/{mm}/{date}_{time}_{title}.md",
    "auto_rename_after_refine": True,
    "autosave_delay_ms": 1000,
    "server": {
        "port": 8765,
        "autostart_cmd": "",
    },
    "paste": {
        "image_dir": "assets/{yyyy}-{mm}",
        "image_name": "{prompt}_{n:02}.png",
        "path_style": "windows",
        "quote_paths_with_spaces": True,
        "image_preview_on_hover": True,
    },
    "refine": {
        "default_mode": "clean",
        "modes_in_palette": ["clean", "prompt", "slide", "summary", "transform"],
        "result_view": "split_right",
        "protect_paths": True,
        "protect_code_blocks": True,
    },
    "send": {
        "copy": "refined",
        "auto_paste_to_last_window": False,
        "mark_sent_in_frontmatter": True,
    },
    "frontmatter": {
        "fields": [
            "created",
            "status",
            "mode",
            "refined_at",
            "sent_at",
            "target",
        ],
    },
    # Convenience flat keys for backward compatibility
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
    "autostart_cmd": "",
    "image_preview_on_hover": True,
    "copy": "refined",
    "auto_paste_to_last_window": False,
    "mark_sent_in_frontmatter": True,
    "modes_in_palette": ["clean", "prompt", "slide", "summary", "transform"],
}


def merge_editor_config(
    raw_editor_dict: Optional[Dict[str, Any]],
    base: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Merges raw editor config (nested from /config or flat from /health)
    into a standardized dictionary containing both nested and flat keys.
    """
    res = dict(base if base is not None else DEFAULT_EDITOR_CONFIG)
    # Ensure nested sub-dicts exist
    res["server"] = dict(res.get("server", DEFAULT_EDITOR_CONFIG["server"]))
    res["paste"] = dict(res.get("paste", DEFAULT_EDITOR_CONFIG["paste"]))
    res["refine"] = dict(res.get("refine", DEFAULT_EDITOR_CONFIG["refine"]))
    res["send"] = dict(res.get("send", DEFAULT_EDITOR_CONFIG["send"]))
    res["frontmatter"] = dict(res.get("frontmatter", DEFAULT_EDITOR_CONFIG["frontmatter"]))

    if not raw_editor_dict or not isinstance(raw_editor_dict, dict):
        return res

    # 1. Top-level direct keys
    for k in ("prompts_root", "file_layout", "auto_rename_after_refine", "autosave_delay_ms"):
        if k in raw_editor_dict:
            res[k] = raw_editor_dict[k]

    # 2. Nested sub-dictionaries
    if isinstance(raw_editor_dict.get("server"), dict):
        res["server"].update(raw_editor_dict["server"])
    if isinstance(raw_editor_dict.get("paste"), dict):
        res["paste"].update(raw_editor_dict["paste"])
    if isinstance(raw_editor_dict.get("refine"), dict):
        res["refine"].update(raw_editor_dict["refine"])
    if isinstance(raw_editor_dict.get("send"), dict):
        res["send"].update(raw_editor_dict["send"])
    if isinstance(raw_editor_dict.get("frontmatter"), dict):
        res["frontmatter"].update(raw_editor_dict["frontmatter"])

    # 3. Handle flat inputs (e.g. from /health)
    if "autostart_cmd" in raw_editor_dict:
        res["server"]["autostart_cmd"] = raw_editor_dict["autostart_cmd"]
    if "path_style" in raw_editor_dict:
        res["paste"]["path_style"] = raw_editor_dict["path_style"]
    if "quote_paths_with_spaces" in raw_editor_dict:
        res["paste"]["quote_paths_with_spaces"] = bool(raw_editor_dict["quote_paths_with_spaces"])
    if "image_preview_on_hover" in raw_editor_dict:
        res["paste"]["image_preview_on_hover"] = bool(raw_editor_dict["image_preview_on_hover"])
    if "default_mode" in raw_editor_dict:
        res["refine"]["default_mode"] = raw_editor_dict["default_mode"]
    if "result_view" in raw_editor_dict:
        res["refine"]["result_view"] = raw_editor_dict["result_view"]
    if "modes_in_palette" in raw_editor_dict:
        res["refine"]["modes_in_palette"] = list(raw_editor_dict["modes_in_palette"])
    if "copy" in raw_editor_dict:
        res["send"]["copy"] = raw_editor_dict["copy"]
    if "auto_paste_to_last_window" in raw_editor_dict:
        res["send"]["auto_paste_to_last_window"] = bool(raw_editor_dict["auto_paste_to_last_window"])
    if "mark_sent_in_frontmatter" in raw_editor_dict:
        res["send"]["mark_sent_in_frontmatter"] = bool(raw_editor_dict["mark_sent_in_frontmatter"])
    if "frontmatter_fields" in raw_editor_dict:
        res["frontmatter"]["fields"] = list(raw_editor_dict["frontmatter_fields"])

    # 4. Synchronize convenience flat keys with nested values
    res["autostart_cmd"] = res["server"].get("autostart_cmd", "")
    res["path_style"] = res["paste"].get("path_style", "windows")
    res["quote_paths_with_spaces"] = bool(res["paste"].get("quote_paths_with_spaces", True))
    res["image_preview_on_hover"] = bool(res["paste"].get("image_preview_on_hover", True))
    res["default_mode"] = res["refine"].get("default_mode", "clean")
    res["result_view"] = res["refine"].get("result_view", "split_right")
    res["modes_in_palette"] = list(res["refine"].get("modes_in_palette", DEFAULT_EDITOR_CONFIG["refine"]["modes_in_palette"]))
    res["copy"] = res["send"].get("copy", "refined")
    res["auto_paste_to_last_window"] = bool(res["send"].get("auto_paste_to_last_window", False))
    res["mark_sent_in_frontmatter"] = bool(res["send"].get("mark_sent_in_frontmatter", True))
    res["frontmatter_fields"] = list(res["frontmatter"].get("fields", DEFAULT_EDITOR_CONFIG["frontmatter"]["fields"]))

    return res


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
    pattern = re.compile(r"^---[ \t]*\r?$", re.MULTILINE)
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


def update_frontmatter(
    text: str,
    updates: Dict[str, str],
    field_order: Optional[List[str]] = None,
) -> str:
    """
    Updates or inserts frontmatter fields without modifying the body text.
    """
    fields, body, body_offset = parse_frontmatter(text)
    fields = dict(fields)
    for k, v in updates.items():
        fields[k] = "" if v is None else str(v)

    rendered_fm = render_frontmatter(fields, field_order=field_order)
    if body_offset == 0:
        return rendered_fm + text

    return rendered_fm + text[body_offset:]


def sanitize_title(title: str) -> str:
    """Removes illegal filename characters."""
    illegal_chars = r'[\\/:*?"<>|\r\n\t]'
    cleaned = re.sub(illegal_chars, "", title).strip()
    return cleaned if cleaned else "untitled"


def extract_title_slug(text: str, max_len: int = 20) -> str:
    """
    Extracts a title slug from refined text (first Markdown heading or first non-empty line).
    Truncates to max_len, cleans Windows illegal filename characters.
    """
    if not text:
        return "untitled"

    candidate = ""
    lines = text.splitlines()

    # 1. Look for first markdown heading (# ...)
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#"):
            candidate = re.sub(r"^#+\s*", "", stripped).strip()
            if candidate:
                break

    # 2. Look for first non-empty line if no heading
    if not candidate:
        for line in lines:
            stripped = line.strip()
            if stripped:
                # Split at first sentence delimiter
                parts = re.split(r"[。！？!?.]", stripped)
                candidate = parts[0].strip() if parts else stripped
                if candidate:
                    break

    if not candidate:
        return "untitled"

    # Remove illegal filename characters
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]', "", candidate)
    # Compress whitespace
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        return "untitled"

    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len].strip()

    return cleaned if cleaned else "untitled"


def renamed_paths_for(
    source_path: str,
    refined_path: str,
    slug: str,
) -> Optional[Tuple[str, str]]:
    """
    If source filename contains '_untitled' (case-insensitive), replaces it with '_<slug>'.
    Ensures collision avoidance for both source and refined companions (_2, _3).
    Returns (new_source_path, new_refined_path) or None if not applicable.
    """
    if not source_path or not refined_path or not slug:
        return None
    if slug.lower() == "untitled":
        return None

    src_dir = os.path.dirname(source_path)
    ref_dir = os.path.dirname(refined_path)
    src_name = os.path.basename(source_path)
    ref_name = os.path.basename(refined_path)

    pattern = re.compile(r"_untitled", re.IGNORECASE)
    if not pattern.search(src_name):
        return None

    # Replace _untitled with _<slug>
    new_src_base = pattern.sub(f"_{slug}", src_name)
    new_ref_base = pattern.sub(f"_{slug}", ref_name)

    # Collision check
    cand_src = os.path.normpath(os.path.join(src_dir, new_src_base))
    cand_ref = os.path.normpath(os.path.join(ref_dir, new_ref_base))

    # Strip .refined.md and .md to find root stem
    if not os.path.exists(cand_src) and not os.path.exists(cand_ref):
        return cand_src, cand_ref

    # Suffix increment
    src_stem, src_ext = os.path.splitext(cand_src)  # .md
    ref_stem = cand_ref
    if ref_stem.lower().endswith(".refined.md"):
        ref_stem = ref_stem[:-len(".refined.md")]
        ref_ext = ".refined.md"
    else:
        ref_stem, ref_ext = os.path.splitext(cand_ref)

    counter = 2
    while True:
        try_src = f"{src_stem}_{counter}{src_ext}"
        try_ref = f"{ref_stem}_{counter}{ref_ext}"
        if not os.path.exists(try_src) and not os.path.exists(try_ref):
            return try_src, try_ref
        counter += 1


def list_prompt_history(prompts_root: str, limit: int = 200) -> List[Dict[str, str]]:
    """
    Scans prompts_root for .md files (excluding .refined.md and assets directories).
    Sorted by modification time (newest first), returns up to limit items.
    """
    root = os.path.abspath(os.path.expanduser(prompts_root))
    if not os.path.isdir(root):
        return []

    collected = []
    for dirpath, dirnames, filenames in os.walk(root):
        # Exclude assets directories
        dirnames[:] = [d for d in dirnames if d.lower() != "assets"]
        for f in filenames:
            fl = f.lower()
            if fl.endswith(".md") and not fl.endswith(".refined.md"):
                fp = os.path.join(dirpath, f)
                try:
                    mtime = os.path.getmtime(fp)
                    collected.append((mtime, fp))
                except OSError:
                    continue

    collected.sort(key=lambda item: item[0], reverse=True)
    results = []
    for mtime, fp in collected[:limit]:
        mtime_str = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
        status = "draft"
        mode = "clean"
        snippet = ""
        try:
            with open(fp, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            fields, body, _ = parse_frontmatter(content)
            status = fields.get("status", "draft")
            mode = fields.get("mode", "clean")
            for line in body.splitlines():
                st = line.strip()
                if st:
                    snippet = st[:60]
                    break
        except Exception:
            pass

        results.append({
            "path": fp,
            "mtime_str": mtime_str,
            "status": status,
            "mode": mode,
            "snippet": snippet,
        })
    return results


def extract_image_path_at(line: str, col: int, base_dir: str = "") -> Optional[str]:
    """
    Extracts an image file path at cursor position (line, col).
    Supports quoted paths, unquoted paths, WSL /mnt paths, Markdown ![](path), and relative paths.
    """
    if not line:
        return None

    img_ext_pattern = r"\.(?:png|jpe?g|gif|bmp|webp)"

    # Patterns to match potential image path substrings
    patterns = [
        # Markdown image syntax: ![](...)
        r"!\[.*?\]\((?P<p1>[^)]+)\)",
        # Quoted string
        r'["\'](?P<p2>[^"\']+' + img_ext_pattern + r')["\']',
        # Absolute or relative path without quotes
        r"(?P<p3>(?:[a-zA-Z]:[\\/]|/mnt/[a-zA-Z]/|[a-zA-Z0-9_.-]+[\\/])[^\s\"'<>|]+" + img_ext_pattern + r")",
        # Simple file name
        r"(?P<p4>[a-zA-Z0-9_.-]+" + img_ext_pattern + r")",
    ]

    for pat in patterns:
        for m in re.finditer(pat, line, re.IGNORECASE):
            if m.start() <= col <= m.end():
                # Extract path group
                raw_path = next((v for v in m.groupdict().values() if v is not None), None)
                if not raw_path:
                    continue
                raw_path = raw_path.strip().strip("<>\"'")

                # WSL translation: /mnt/c/a/b -> c:\a\b
                if raw_path.startswith("/mnt/") and len(raw_path) > 7:
                    drive = raw_path[5]
                    rest = raw_path[6:].replace("/", "\\")
                    raw_path = f"{drive}:{rest}"

                # Normalize slashes
                raw_path = raw_path.replace("/", "\\")

                if base_dir and not os.path.isabs(raw_path):
                    cand = os.path.normpath(os.path.join(base_dir, raw_path))
                else:
                    cand = os.path.normpath(raw_path)

                return cand

    return None


def _parse_image_dimensions(data: bytes) -> Tuple[int, int]:
    """Parses PNG, JPEG, GIF image width and height using pure stdlib."""
    # PNG: 8-byte signature + IHDR chunk
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        w, h = struct.unpack(">II", data[16:24])
        return w, h

    # GIF: GIF87a or GIF89a
    if (data.startswith(b"GIF87a") or data.startswith(b"GIF89a")) and len(data) >= 10:
        w, h = struct.unpack("<HH", data[6:10])
        return w, h

    # JPEG: SOI marker
    if data.startswith(b"\xff\xd8"):
        idx = 2
        length_data = len(data)
        while idx < length_data - 9:
            if data[idx] != 0xFF:
                idx += 1
                continue
            marker = data[idx + 1]
            # Baseline / Progressive SOF markers
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                h, w = struct.unpack(">HH", data[idx + 5 : idx + 9])
                return w, h
            if idx + 4 > length_data:
                break
            seg_len = struct.unpack(">H", data[idx + 2 : idx + 4])[0]
            idx += 2 + seg_len

    return 400, 300


def read_image_data_uri(
    path: str,
    max_bytes: int = 1048576,
    max_width: int = 400,
) -> Tuple[Optional[str], int, int, str]:
    """
    Reads image file, encodes to data URI, and calculates display width and height.
    Rejects files larger than max_bytes (default 1 MB).
    """
    if not os.path.isfile(path):
        return None, 0, 0, f"圖片檔案不存在: {path}"

    try:
        size = os.path.getsize(path)
        if size > max_bytes:
            return None, 0, 0, "圖片超過 1 MB，略過預覽"

        with open(path, "rb") as f:
            data = f.read()

        b64 = base64.b64encode(data).decode("ascii")
        ext = os.path.splitext(path)[1].lower().lstrip(".")
        mime = "jpeg" if ext in ("jpg", "jpeg") else ext
        data_uri = f"data:image/{mime};base64,{b64}"

        orig_w, orig_h = _parse_image_dimensions(data)
        if orig_w > 0 and orig_h > 0:
            if orig_w > max_width:
                disp_w = max_width
                disp_h = max(1, int(orig_h * max_width / orig_w))
            else:
                disp_w = orig_w
                disp_h = orig_h
        else:
            disp_w = max_width
            disp_h = max_width

        return data_uri, disp_w, disp_h, ""
    except Exception as e:
        return None, 0, 0, f"讀取圖片失敗: {e}"


def grab_clipboard_files_win32() -> List[str]:
    """Reads file paths from Windows clipboard (CF_HDROP) using pure stdlib ctypes."""
    if not sys.platform.startswith("win"):
        return []

    try:
        user32 = ctypes.windll.user32
        shell32 = ctypes.windll.shell32
        if not user32.OpenClipboard(None):
            return []
        try:
            h_drop = user32.GetClipboardData(15)  # CF_HDROP = 15
            if not h_drop:
                return []
            count = shell32.DragQueryFileW(h_drop, 0xFFFFFFFF, None, 0)
            files = []
            buf = ctypes.create_unicode_buffer(1024)
            for i in range(count):
                length = shell32.DragQueryFileW(h_drop, i, buf, 1024)
                if length > 0:
                    files.append(buf.value)
            return files
        finally:
            user32.CloseClipboard()
    except Exception:
        return []


def find_repo_autostart_cmd(
    plugin_file: str,
    configured_cmd: str = "",
) -> Tuple[Optional[List[str]], Optional[str]]:
    """
    Resolves autostart command and repo root directory.
    - If configured_cmd is 'off'/'false'/'none': returns (None, None).
    - If configured_cmd is provided: parses with shlex.
    - If empty (default): locates <repo>/.venv/Scripts/pythonw.exe (or python.exe).
    """
    cmd_lower = configured_cmd.strip().lower()
    if cmd_lower in ("off", "false", "none"):
        return None, None

    plugin_real = os.path.realpath(plugin_file)
    # plugin_real is <repo>/sublime/LocalTypeless/lt_plugin.py
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(plugin_real)))

    if configured_cmd.strip():
        cmd_list = shlex.split(configured_cmd, posix=False)
        return cmd_list, repo_root

    # Default auto-detection
    pythonw = os.path.join(repo_root, ".venv", "Scripts", "pythonw.exe")
    python = os.path.join(repo_root, ".venv", "Scripts", "python.exe")
    if os.path.isfile(pythonw):
        return [pythonw, "-m", "local_typeless.cli", "serve"], repo_root
    elif os.path.isfile(python):
        return [python, "-m", "local_typeless.cli", "serve"], repo_root

    return None, repo_root


def find_repo_panel_cmd(
    plugin_file: str,
    page: str = "settings",
) -> Tuple[Optional[List[str]], Optional[str]]:
    """
    Resolves the command to launch the Tk settings panel directly.
    """
    plugin_real = os.path.realpath(plugin_file)
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(plugin_real)))

    pythonw = os.path.join(repo_root, ".venv", "Scripts", "pythonw.exe")
    python = os.path.join(repo_root, ".venv", "Scripts", "python.exe")
    py_exe = pythonw if os.path.isfile(pythonw) else (python if os.path.isfile(python) else "python")

    return [py_exe, "-m", "local_typeless.cli", "panel", "--page", page], repo_root


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