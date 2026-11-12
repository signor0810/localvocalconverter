"""Rich paste adapter: interleaves text and clipboard image paste operations."""
from __future__ import annotations

import io
import logging
import sys
import time
from pathlib import Path
from typing import Callable, Optional, Sequence, Union

from local_typeless.output.clipboard_out import _simulate_paste, copy_with_retry
from local_typeless.textblocks import marker, split_rich
from local_typeless.types import CapturedImage, Mode, OutputConfig

logger = logging.getLogger(__name__)


def set_clipboard_image(path: Union[str, Path]) -> None:
    """Load an image and place it onto the Windows clipboard as CF_DIB.

    Raises OSError on any failure (or on non-Windows platforms).
    """
    if sys.platform != "win32":
        raise OSError("image clipboard is only supported on Windows")

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
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32 = ctypes.WinDLL("user32", use_last_error=True)

    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = ctypes.c_void_p

    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p

    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.restype = wintypes.BOOL

    kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
    kernel32.GlobalFree.restype = ctypes.c_void_p

    user32.OpenClipboard.argtypes = [ctypes.c_void_p]
    user32.OpenClipboard.restype = wintypes.BOOL

    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = wintypes.BOOL

    user32.EmptyClipboard.argtypes = []
    user32.EmptyClipboard.restype = wintypes.BOOL

    user32.SetClipboardData.argtypes = [wintypes.UINT, ctypes.c_void_p]
    user32.SetClipboardData.restype = ctypes.c_void_p

    # GMEM_MOVEABLE = 0x0002
    handle = kernel32.GlobalAlloc(0x0002, len(dib_bytes))
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
        # CF_DIB = 8
        res = user32.SetClipboardData(8, handle)
        if not res:
            kernel32.GlobalFree(handle)
            raise OSError("SetClipboardData(CF_DIB) failed")
    finally:
        user32.CloseClipboard()


class RichPaster:
    """Sequentially pastes text chunks and images with inter-step delays."""

    def __init__(
        self,
        cfg: OutputConfig,
        *,
        copy_text: Callable[[str], None] = copy_with_retry,
        set_image: Callable[[Union[str, Path]], None] = set_clipboard_image,
        paste: Callable[[], None] = _simulate_paste,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._cfg = cfg
        self._copy_text = copy_text
        self._set_image = set_image
        self._paste = paste
        self._sleep = sleep

    def emit(self, text: str, mode: Optional[Mode] = None) -> None:
        """Same as emit_rich(text, [])."""
        self.emit_rich(text, [])

    def emit_rich(self, text: str, images: Sequence[CapturedImage]) -> None:
        """Split text and images, sequentially copying and pasting with delays."""
        parts = split_rich(text, images)
        has_image = any(isinstance(part, CapturedImage) for part in parts)

        if not has_image:
            self._copy_text(text)
            self._paste()
            return

        delay = self._cfg.paste_step_delay_ms / 1000.0

        for part in parts:
            if isinstance(part, str):
                self._copy_text(part)
                self._paste()
                self._sleep(delay)
            elif isinstance(part, CapturedImage):
                try:
                    self._set_image(part.path)
                    self._paste()
                    self._sleep(delay)
                except Exception as exc:
                    logger.warning(
                        "Failed to set clipboard image %s: %s; pasting marker text instead",
                        part.path,
                        exc,
                    )
                    self._copy_text(marker(part.index))
                    self._paste()
                    self._sleep(delay)
