"""Accept files dragged from Explorer onto a Tk window (Windows only, no extra package).

Tk has no built-in file drop, so the top-level window is marked with DragAcceptFiles and
its window procedure is wrapped to pick up WM_DROPFILES.

The wrapped procedure runs inside Tk's event loop, where calling back into Tk (even
`after`) corrupts the interpreter's thread state and kills the process. So it only
stores the paths; the owner fetches them with `FileDrop.poll()` from a normal Tk tick.
"""
from __future__ import annotations

import collections
import logging
import sys
from typing import Any, List, Optional

from local_typeless.capture._win32clip import get_shell32, hdrop_paths

logger = logging.getLogger(__name__)

WM_DROPFILES = 0x0233
GWLP_WNDPROC = -4


class FileDrop:
    """Keeps the wrapped window procedure alive; call `remove()` before the window dies."""

    def __init__(self) -> None:
        self._hwnd = 0
        self._old_proc = 0
        self._new_proc: Any = None       # must stay referenced while installed
        self._user32: Any = None
        self._pending: collections.deque = collections.deque()

    def poll(self) -> List[List[str]]:
        """Drops received since the last call (one list of paths per drop)."""
        out: List[List[str]] = []
        while self._pending:
            out.append(self._pending.popleft())
        return out

    def remove(self) -> None:
        if self._old_proc:
            try:
                self._user32.SetWindowLongPtrW(self._hwnd, GWLP_WNDPROC, self._old_proc)
            except Exception as e:
                logger.debug("FileDrop.remove failed: %s", e)
            self._old_proc = 0


def enable_file_drop(root: Any) -> Optional[FileDrop]:
    """Make `root` (a tk.Tk / Toplevel) accept dropped files. Returns None if unsupported."""
    if sys.platform != "win32":
        return None
    shell32 = get_shell32()
    if shell32 is None:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        LRESULT = ctypes.c_ssize_t
        WNDPROC = ctypes.WINFUNCTYPE(
            LRESULT, ctypes.c_void_p, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t)
        user32.GetParent.argtypes = [ctypes.c_void_p]
        user32.GetParent.restype = ctypes.c_void_p
        user32.GetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int]
        user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
        user32.SetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]
        user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
        user32.CallWindowProcW.argtypes = [
            ctypes.c_ssize_t, ctypes.c_void_p, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t]
        user32.CallWindowProcW.restype = LRESULT

        root.update_idletasks()
        hwnd = user32.GetParent(root.winfo_id()) or root.winfo_id()
        old_proc = user32.GetWindowLongPtrW(hwnd, GWLP_WNDPROC)
        if not old_proc:
            return None

        drop = FileDrop()

        def proc(h: Any, msg: int, wparam: int, lparam: int) -> int:
            if msg == WM_DROPFILES:
                try:                        # no Tk calls in here (see module docstring)
                    paths = hdrop_paths(wparam)
                    shell32.DragFinish(wparam)
                    if paths:
                        drop._pending.append(paths)
                except Exception as e:      # never let an exception cross into Win32
                    logger.warning("file drop failed: %s: %s", type(e).__name__, e)
                return 0
            return user32.CallWindowProcW(old_proc, h, msg, wparam, lparam)

        new_proc = WNDPROC(proc)
        new_addr = ctypes.cast(new_proc, ctypes.c_void_p).value
        if not user32.SetWindowLongPtrW(hwnd, GWLP_WNDPROC, new_addr):
            return None
        drop._hwnd, drop._old_proc, drop._new_proc, drop._user32 = hwnd, old_proc, new_proc, user32
        shell32.DragAcceptFiles(hwnd, True)
        return drop
    except Exception as e:
        logger.warning("enable_file_drop failed: %s: %s", type(e).__name__, e)
        return None
