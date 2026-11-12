"""Diagnostics for the windowless (pythonw) UI: file logging + hang watchdog.

pythonw has no stderr, so everything goes to <cache_dir>/ui.log. Two watchdogs cover a
stuck PROCESSING phase:
  * a Python thread that logs every thread's stack (with thread names) into ui.log;
  * faulthandler.dump_traceback_later, a C-level timer that still fires if some thread
    holds the GIL; it writes to <cache_dir>/ui_fault.log (a separate file, because
    faulthandler needs a raw file handle and an open second handle on ui.log would break
    RotatingFileHandler's rename on Windows).
"""
from __future__ import annotations

import faulthandler
import logging
import logging.handlers
import sys
import threading
import traceback
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

LOG_FORMAT = "%(asctime)s %(levelname)s [%(threadName)s] %(name)s: %(message)s"
WATCHDOG_SEC = 240.0

_fault_file: Any = None


def setup_logging(cache_dir: Path, level: int = logging.INFO) -> Path:
    """Route all logging (and uncaught exceptions) to a rotating <cache_dir>/ui.log."""
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / "ui.log"
    handler = logging.handlers.RotatingFileHandler(
        path, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(handler)
    root.setLevel(level)

    def _excepthook(exc_type, exc, tb):
        logging.getLogger("uncaught").critical("uncaught exception", exc_info=(exc_type, exc, tb))

    def _thread_hook(args):
        logging.getLogger("uncaught").critical(
            "uncaught exception in thread %s", getattr(args.thread, "name", "?"),
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = _excepthook
    threading.excepthook = _thread_hook
    return path


def enable_faulthandler(cache_dir: Path) -> Optional[Any]:
    """Leave a trace on hard crashes (and for the C-level watchdog)."""
    global _fault_file
    try:
        path = Path(cache_dir) / "ui_fault.log"
        _fault_file = open(path, "a", encoding="utf-8")
        faulthandler.enable(file=_fault_file, all_threads=True)
        return _fault_file
    except Exception:
        logger.exception("could not enable faulthandler")
        return None


def dump_all_threads() -> str:
    names = {t.ident: t.name for t in threading.enumerate()}
    parts = []
    for ident, frame in sys._current_frames().items():
        stack = "".join(traceback.format_stack(frame))
        parts.append(f"--- thread {names.get(ident, '?')} (id {ident}) ---\n{stack}")
    return "\n".join(parts)


class ProcessingWatchdog:
    """arm() when processing starts, disarm() when it ends. While armed, dumps all thread
    stacks every `interval` seconds."""

    def __init__(
        self,
        interval: float = WATCHDOG_SEC,
        fault_file: Any = None,
        dump: Callable[[], str] = dump_all_threads,
    ) -> None:
        self.interval = interval
        self._fault_file = fault_file
        self._dump = dump
        self._lock = threading.Lock()
        self._stop: Optional[threading.Event] = None

    @property
    def armed(self) -> bool:
        return self._stop is not None

    def arm(self) -> None:
        with self._lock:
            if self._stop is not None:
                return
            stop = threading.Event()
            self._stop = stop
        threading.Thread(target=self._loop, args=(stop,), daemon=True, name="watchdog").start()
        if self._fault_file is not None:
            try:
                faulthandler.dump_traceback_later(
                    self.interval, repeat=True, file=self._fault_file)
            except Exception:
                logger.debug("faulthandler timer unavailable", exc_info=True)

    def disarm(self) -> None:
        with self._lock:
            stop, self._stop = self._stop, None
        if stop is None:
            return
        stop.set()
        if self._fault_file is not None:
            try:
                faulthandler.cancel_dump_traceback_later()
            except Exception:
                pass

    def _loop(self, stop: threading.Event) -> None:
        n = 0
        while not stop.wait(self.interval):
            n += 1
            try:
                logger.error(
                    "WATCHDOG: still processing after %.0fs (dump #%d); all thread stacks:\n%s",
                    self.interval * n, n, self._dump())
            except Exception:
                logger.exception("watchdog dump failed")
