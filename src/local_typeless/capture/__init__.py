"""Clipboard image capture module."""
from __future__ import annotations

from local_typeless.capture.clipboard_images import (
    ClipboardBackend,
    ClipboardImageWatcher,
    Win32ClipboardBackend,
)

__all__ = [
    "ClipboardBackend",
    "ClipboardImageWatcher",
    "Win32ClipboardBackend",
]
