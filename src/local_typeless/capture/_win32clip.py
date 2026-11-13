"""Shared 64-bit Win32 clipboard and process API declarations and helpers."""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# GMEM_MOVEABLE = 0x0002
GMEM_MOVEABLE = 0x0002
# CF_UNICODETEXT = 13, CF_DIB = 8
CF_DIB = 8
CF_UNICODETEXT = 13
# PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

_initialized = False
_kernel32 = None
_user32 = None


def get_win32_apis() -> Tuple[Optional[object], Optional[object]]:
    """Return configured (kernel32, user32) WinDLL instances with full 64-bit prototypes."""
    global _initialized, _kernel32, _user32
    if sys.platform != "win32":
        return None, None

    if _initialized:
        return _kernel32, _user32

    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        user32 = ctypes.WinDLL("user32", use_last_error=True)

        # GlobalAlloc / GlobalLock / GlobalUnlock / GlobalFree / GlobalSize
        kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
        kernel32.GlobalAlloc.restype = ctypes.c_void_p

        kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
        kernel32.GlobalLock.restype = ctypes.c_void_p

        kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
        kernel32.GlobalUnlock.restype = wintypes.BOOL

        kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
        kernel32.GlobalFree.restype = ctypes.c_void_p

        kernel32.GlobalSize.argtypes = [ctypes.c_void_p]
        kernel32.GlobalSize.restype = ctypes.c_size_t

        # Clipboard APIs
        user32.OpenClipboard.argtypes = [ctypes.c_void_p]
        user32.OpenClipboard.restype = wintypes.BOOL

        user32.CloseClipboard.argtypes = []
        user32.CloseClipboard.restype = wintypes.BOOL

        user32.EmptyClipboard.argtypes = []
        user32.EmptyClipboard.restype = wintypes.BOOL

        user32.GetClipboardData.argtypes = [wintypes.UINT]
        user32.GetClipboardData.restype = ctypes.c_void_p

        user32.SetClipboardData.argtypes = [wintypes.UINT, ctypes.c_void_p]
        user32.SetClipboardData.restype = ctypes.c_void_p

        user32.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
        user32.RegisterClipboardFormatW.restype = wintypes.UINT

        user32.GetClipboardSequenceNumber.argtypes = []
        user32.GetClipboardSequenceNumber.restype = wintypes.DWORD

        # Window & Process APIs for foreground window detection
        user32.GetForegroundWindow.argtypes = []
        user32.GetForegroundWindow.restype = ctypes.c_void_p

        user32.GetWindowThreadProcessId.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(wintypes.DWORD),
        ]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD

        kernel32.OpenProcess.argtypes = [
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        kernel32.OpenProcess.restype = ctypes.c_void_p

        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = wintypes.BOOL

        kernel32.QueryFullProcessImageNameW.argtypes = [
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL

        _kernel32 = kernel32
        _user32 = user32
        _initialized = True
        return _kernel32, _user32
    except Exception as exc:
        logger.debug("Failed to initialize Win32 ctypes APIs: %s", exc)
        return None, None


CF_HDROP = 15
_shell32 = None


def get_shell32() -> Optional[object]:
    """Return shell32 with the drag-and-drop prototypes set (None off Windows)."""
    global _shell32
    if sys.platform != "win32":
        return None
    if _shell32 is not None:
        return _shell32
    try:
        import ctypes
        from ctypes import wintypes

        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        shell32.DragQueryFileW.argtypes = [
            ctypes.c_void_p, wintypes.UINT, wintypes.LPWSTR, wintypes.UINT,
        ]
        shell32.DragQueryFileW.restype = wintypes.UINT
        shell32.DragAcceptFiles.argtypes = [ctypes.c_void_p, wintypes.BOOL]
        shell32.DragAcceptFiles.restype = None
        shell32.DragFinish.argtypes = [ctypes.c_void_p]
        shell32.DragFinish.restype = None
        _shell32 = shell32
        return _shell32
    except Exception as exc:
        logger.debug("Failed to initialize shell32: %s", exc)
        return None


def hdrop_paths(hdrop: object) -> list:
    """File paths held by an HDROP handle (clipboard CF_HDROP or a WM_DROPFILES wParam)."""
    shell32 = get_shell32()
    if shell32 is None or not hdrop:
        return []
    import ctypes

    count = shell32.DragQueryFileW(hdrop, 0xFFFFFFFF, None, 0)
    paths = []
    for i in range(count):
        length = shell32.DragQueryFileW(hdrop, i, None, 0)
        buf = ctypes.create_unicode_buffer(length + 1)
        if shell32.DragQueryFileW(hdrop, i, buf, length + 1):
            paths.append(buf.value)
    return paths


def get_foreground_app() -> str:
    """Return the lowercase executable name of the current foreground window, or '' on failure."""
    if sys.platform != "win32":
        return ""

    kernel32, user32 = get_win32_apis()
    if kernel32 is None or user32 is None:
        return ""

    try:
        import ctypes
        from ctypes import wintypes

        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return ""

        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return ""

        h_proc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not h_proc:
            return ""

        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(1024)
            if kernel32.QueryFullProcessImageNameW(h_proc, 0, buf, ctypes.byref(size)):
                return Path(buf.value).name.lower()
            return ""
        finally:
            kernel32.CloseHandle(h_proc)
    except Exception as exc:
        logger.debug("get_foreground_app failed: %s", exc)
        return ""
