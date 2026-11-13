"""Rich paste adapter: interleaves text and clipboard image paste operations."""
from __future__ import annotations

import io
import logging
import re
import sys
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Union

from local_typeless.capture._win32clip import (
    CF_DIB,
    CF_UNICODETEXT,
    GMEM_MOVEABLE,
    get_foreground_app,
    get_win32_apis,
)
from local_typeless.output.clipboard_out import _simulate_paste, copy_with_retry
from local_typeless.textblocks import ANY_MARKER_RE, marker, marker_for, split_rich
from local_typeless.types import CapturedImage, Mode, OutputConfig

logger = logging.getLogger(__name__)


def set_clipboard_image(path: Union[str, Path]) -> None:
    """Load an image and place it onto the Windows clipboard as CF_DIB.

    Raises OSError on any failure (or on non-Windows platforms).
    """
    if sys.platform != "win32":
        raise OSError("image clipboard is only supported on Windows")

    kernel32, user32 = get_win32_apis()
    if kernel32 is None or user32 is None:
        raise OSError("Win32 APIs not available")

    p = Path(path)
    if not p.is_file():
        raise OSError(f"Image file does not exist: {path}")

    try:
        from PIL import Image

        with Image.open(p) as img:
            rgb_img = img.convert("RGB")
            buf = io.BytesIO()
            rgb_img.save(buf, format="BMP")
            bmp_bytes = buf.getvalue()
    except Exception as exc:
        raise OSError(f"Failed to read/convert image {path}: {exc}") from exc

    # Drop 14-byte BITMAPFILEHEADER to get device-independent bitmap (DIB)
    dib_bytes = bmp_bytes[14:]

    import ctypes

    handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(dib_bytes))
    if not handle:
        raise OSError("GlobalAlloc failed to allocate memory for DIB")

    ptr = kernel32.GlobalLock(handle)
    if not ptr:
        kernel32.GlobalFree(handle)
        raise OSError("GlobalLock failed")

    ctypes.memmove(ptr, dib_bytes, len(dib_bytes))
    kernel32.GlobalUnlock(handle)

    opened = False
    for _ in range(5):
        if user32.OpenClipboard(None):
            opened = True
            break
        time.sleep(0.05)

    if not opened:
        kernel32.GlobalFree(handle)
        raise OSError("Could not open Windows clipboard after 5 retries")

    try:
        user32.EmptyClipboard()
        res = user32.SetClipboardData(CF_DIB, handle)
        if not res:
            kernel32.GlobalFree(handle)
            raise OSError("SetClipboardData(CF_DIB) failed")
    finally:
        user32.CloseClipboard()


def set_clipboard_table(item: CapturedImage, fmt: str = "html") -> None:
    """Set table content on the Windows clipboard (CF_HTML + CF_UNICODETEXT).

    Raises OSError on failure or non-Windows platforms.
    """
    if sys.platform != "win32":
        raise OSError("clipboard table operations are only supported on Windows")

    kernel32, user32 = get_win32_apis()
    if kernel32 is None or user32 is None:
        raise OSError("Win32 APIs not available")

    from local_typeless.capture.tables import build_cf_html

    html_bytes = b""
    if item.html:
        html_bytes = build_cf_html(item.html)

    import ctypes

    opened = False
    for _ in range(5):
        if user32.OpenClipboard(None):
            opened = True
            break
        time.sleep(0.05)

    if not opened:
        raise OSError("Could not open Windows clipboard after 5 retries")

    items_placed = 0
    try:
        user32.EmptyClipboard()

        # 1. Place CF_HTML if html_bytes present
        if html_bytes:
            cf_html = user32.RegisterClipboardFormatW("HTML Format")
            if cf_html:
                h_mem = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(html_bytes) + 1)
                if not h_mem:
                    raise OSError("GlobalAlloc failed for CF_HTML")
                ptr = kernel32.GlobalLock(h_mem)
                if not ptr:
                    kernel32.GlobalFree(h_mem)
                    raise OSError("GlobalLock failed for CF_HTML")
                ctypes.memmove(ptr, html_bytes, len(html_bytes))
                ctypes.memset(ctypes.c_void_p(ptr + len(html_bytes)), 0, 1)
                kernel32.GlobalUnlock(h_mem)
                res = user32.SetClipboardData(cf_html, h_mem)
                if not res:
                    kernel32.GlobalFree(h_mem)
                    raise OSError("SetClipboardData(CF_HTML) failed")
                items_placed += 1

        # 2. Place CF_UNICODETEXT if item.text present
        if item.text:
            text_bytes = (item.text + "\0").encode("utf-16-le")
            h_text = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(text_bytes))
            if not h_text:
                raise OSError("GlobalAlloc failed for CF_UNICODETEXT")
            ptr = kernel32.GlobalLock(h_text)
            if not ptr:
                kernel32.GlobalFree(h_text)
                raise OSError("GlobalLock failed for CF_UNICODETEXT")
            ctypes.memmove(ptr, text_bytes, len(text_bytes))
            kernel32.GlobalUnlock(h_text)
            res = user32.SetClipboardData(CF_UNICODETEXT, h_text)
            if not res:
                kernel32.GlobalFree(h_text)
                raise OSError("SetClipboardData(CF_UNICODETEXT) failed")
            items_placed += 1

        if items_placed == 0:
            raise OSError("No data placed on clipboard for table")
    finally:
        user32.CloseClipboard()


def format_path(path: str) -> str:
    """Absolute path as pasted into a chat box; quoted when it contains whitespace."""
    p = str(path).strip()
    if any(c.isspace() for c in p) and not (p.startswith('"') and p.endswith('"')):
        return f'"{p}"'
    return p


class RichPaster:
    """Sequentially pastes text chunks, tables, and images with inter-step delays."""

    def __init__(
        self,
        cfg: OutputConfig,
        *,
        copy_text: Callable[[str], None] = copy_with_retry,
        set_image: Callable[[Union[str, Path]], None] = set_clipboard_image,
        set_table: Optional[Callable[[CapturedImage, str], None]] = None,
        foreground_app: Callable[[], str] = get_foreground_app,
        paste: Callable[[], None] = _simulate_paste,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._cfg = cfg
        self._copy_text = copy_text
        self._set_image = set_image
        self._set_table = set_table if set_table is not None else set_clipboard_table
        self._foreground_app = foreground_app
        self._paste = paste
        self._sleep = sleep

    def emit(self, text: str, mode: Optional[Mode] = None) -> None:
        """Same as emit_rich(text, [])."""
        self.emit_rich(text, [])

    def _resolve_table_format(self) -> str:
        cfg_fmt = getattr(self._cfg, "table_paste_format", "auto").lower()
        if cfg_fmt == "html":
            return "html"
        if cfg_fmt == "markdown":
            return "markdown"

        # auto mode: check foreground application
        app = (self._foreground_app() or "").strip().lower()
        allowed_apps = {
            a.strip().lower()
            for a in getattr(self._cfg, "table_html_apps", [])
        }
        if app and app in allowed_apps:
            return "html"
        return "markdown"

    def _resolve_image_mode(self) -> str:
        """'path' pastes an image as its absolute file path, 'image' pastes the bitmap."""
        mode = str(getattr(self._cfg, "image_paste_mode", "auto")).lower()
        if mode in ("image", "path"):
            return mode
        # auto: terminals (where local agents run) cannot take a bitmap but can read a path.
        app = (self._foreground_app() or "").strip().lower()
        path_apps = {a.strip().lower() for a in getattr(self._cfg, "path_paste_apps", [])}
        return "path" if app and app in path_apps else "image"

    def emit_rich(self, text: str, images: Sequence[CapturedImage]) -> None:
        """Split text and media items, sequentially copying and pasting with delays."""
        tables = [img for img in images if getattr(img, "kind", "image") == "table"]
        pictures = [img for img in images if getattr(img, "kind", "image") == "image"]
        # Resolved once per insertion: the paste target does not change in between.
        table_fmt = self._resolve_table_format() if tables else "markdown"
        image_mode = self._resolve_image_mode() if pictures else "image"

        # Items that become part of the text itself: links, snippets, Markdown tables and paths.
        inline_map: Dict[str, str] = {}
        path_markers: set = set()
        media_items: List[CapturedImage] = []
        for img in images:
            kind = getattr(img, "kind", "image")
            if kind in ("link", "text"):
                inline_map[marker_for(img)] = img.text
            elif kind == "file" or (kind == "image" and image_mode == "path" and img.path):
                inline_map[marker_for(img)] = format_path(img.text if kind == "file" else img.path)
                path_markers.add(marker_for(img))
            elif kind == "table" and table_fmt == "markdown":
                # A Markdown table only renders when it starts on its own line.
                inline_map[marker_for(img)] = "\n\n" + img.text.strip() + "\n\n"
            else:
                media_items.append(img)

        def _inline(s: str) -> str:
            if not inline_map:
                return s
            pieces: List[str] = []
            last = 0
            for m in ANY_MARKER_RE.finditer(s):
                token = m.group(0)
                if token not in inline_map:
                    continue
                pieces.append(s[last:m.start()])
                value = inline_map[token]
                if token in path_markers:
                    # A path glued to the neighbouring words cannot be told apart from them.
                    before = "".join(pieces)
                    if before and not before[-1].isspace():
                        value = " " + value
                    if m.end() < len(s) and not s[m.end()].isspace():
                        value = value + " "
                pieces.append(value)
                last = m.end()
            pieces.append(s[last:])
            out = re.sub(r"\n{3,}", "\n\n", "".join(pieces))
            # A table at the very start must not push blank lines in front of the text.
            return out if s.startswith("\n") else out.lstrip("\n")

        parts = split_rich(text, media_items)
        has_media = any(isinstance(part, CapturedImage) for part in parts)

        if not has_media:
            self._copy_text(_inline(text))
            self._paste()
            return

        delay = self._cfg.paste_step_delay_ms / 1000.0

        for part in parts:
            if isinstance(part, str):
                substituted = _inline(part)
                if substituted:
                    self._copy_text(substituted)
                    self._paste()
                    self._sleep(delay)
            elif getattr(part, "kind", "image") == "table":
                try:
                    self._set_table(part, "html")
                    self._paste()
                    self._sleep(delay)
                except Exception as exc:
                    logger.warning(
                        "Failed to set clipboard table %s: %s; pasting Markdown text instead",
                        part.index, exc,
                    )
                    self._copy_text("\n" + part.text.strip() + "\n")
                    self._paste()
                    self._sleep(delay)
            else:
                try:
                    self._set_image(part.path)
                    self._paste()
                    self._sleep(delay)
                except Exception as exc:
                    logger.warning(
                        "Failed to set clipboard image %s: %s; pasting marker text instead",
                        part.path, exc,
                    )
                    self._copy_text(marker_for(part))
                    self._paste()
                    self._sleep(delay)
