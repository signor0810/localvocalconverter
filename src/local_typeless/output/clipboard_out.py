"""Clipboard Output Adapter (WP2-A, owned by flash-infra).

Implements OutputAdapter protocol defined in local_typeless.types.
Copies text to clipboard with optional image-overwrite protection, simulates Ctrl+V paste,
and retries clipboard writing against transient OS locks.
"""
from __future__ import annotations

import logging
import sys
import time
from typing import Any, Optional

import pyperclip

from local_typeless.types import Mode, OutputAdapter, OutputConfig

logger = logging.getLogger(__name__)


def _is_clipboard_image_present() -> bool:
    """Check if the clipboard currently contains image data (Windows CF_DIB / CF_BITMAP)."""
    if sys.platform != "win32":
        return False

    try:
        import ctypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        CF_BITMAP = 2
        CF_DIB = 8
        CF_DIBV5 = 17

        if user32.OpenClipboard(None):
            try:
                return bool(
                    user32.IsClipboardFormatAvailable(CF_BITMAP)
                    or user32.IsClipboardFormatAvailable(CF_DIB)
                    or user32.IsClipboardFormatAvailable(CF_DIBV5)
                )
            finally:
                user32.CloseClipboard()
    except Exception as exc:
        logger.debug("Error checking clipboard image format: %s", exc)

    return False


def _simulate_paste() -> None:
    """Simulate Ctrl+V keystroke, releasing modifiers first to prevent Ctrl+Alt+V collision."""
    try:
        from pynput.keyboard import Controller, Key

        keyboard = Controller()

        # Explicitly release any modifier keys that might still be held down by user hotkey
        modifiers = [
            Key.alt,
            Key.alt_l,
            Key.alt_r,
            Key.ctrl,
            Key.ctrl_l,
            Key.ctrl_r,
            Key.shift,
            Key.shift_l,
            Key.shift_r,
        ]
        for mod in modifiers:
            try:
                keyboard.release(mod)
            except Exception:
                pass

        time.sleep(0.05)  # 50ms pause to ensure OS processes key-up events

        keyboard.press(Key.ctrl)
        keyboard.press("v")
        keyboard.release("v")
        keyboard.release(Key.ctrl)
    except Exception as exc:
        logger.warning("Simulate paste failed: %s", exc)


def copy_with_retry(text: str) -> None:
    """Copy text to clipboard with up to 4 attempts and ~75ms sleep between retries."""
    last_err: Optional[Exception] = None
    for attempt in range(4):
        try:
            pyperclip.copy(text)
            last_err = None
            break
        except Exception as exc:
            last_err = exc
            if attempt < 3:
                time.sleep(0.075)

    if last_err is not None:
        raise last_err


class ClipboardOutput(OutputAdapter):
    """Output adapter that writes to the system clipboard and optionally simulates paste."""

    def __init__(self, cfg: OutputConfig) -> None:
        self.cfg = cfg

    def emit(self, text: str, mode: Mode) -> None:
        """Emit text to clipboard and trigger auto-paste if enabled.

        Retries pyperclip.copy up to 4 times with ~75ms sleep and re-raises on final failure.
        """
        if self.cfg.clipboard:
            if self.cfg.protect_image_clipboard and _is_clipboard_image_present():
                logger.warning(
                    "Clipboard currently holds an image. Writing text will overwrite image data."
                )

            copy_with_retry(text)

            if self.cfg.auto_paste:
                _simulate_paste()
