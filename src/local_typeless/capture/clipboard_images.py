"""Clipboard image watcher and backend implementation (Task 4)."""
from __future__ import annotations

import hashlib
import io
import logging
import shutil
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, List, Optional, Protocol, runtime_checkable

from local_typeless.types import CapturedImage

logger = logging.getLogger(__name__)

GRAB_RETRIES = 3   # extra polls to retry an empty grab after a clipboard change


@runtime_checkable
class ClipboardBackend(Protocol):
    """Protocol for clipboard monitoring backends."""

    def sequence(self) -> int:
        """Returns sequence number that increments when clipboard changes."""
        ...

    def grab_png(self) -> List[bytes]:
        """Returns PNG bytes of the image(s) on the clipboard now; [] if none."""
        ...


class Win32ClipboardBackend:
    """Real Win32 implementation using ctypes and Pillow."""

    def sequence(self) -> int:
        if sys.platform != "win32":
            return 0
        try:
            import ctypes

            return int(ctypes.windll.user32.GetClipboardSequenceNumber())
        except Exception as e:
            logger.debug("Win32ClipboardBackend.sequence failed: %s", e)
            return 0

    def grab_png(self) -> List[bytes]:
        try:
            from PIL import Image, ImageGrab

            result = ImageGrab.grabclipboard()
            if result is None:
                return []

            if isinstance(result, Image.Image):
                buf = io.BytesIO()
                img = result
                if img.mode not in ("RGB", "RGBA", "L"):
                    img = img.convert("RGBA" if "A" in img.mode else "RGB")
                img.save(buf, format="PNG")
                return [buf.getvalue()]

            if isinstance(result, list):
                valid_exts = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp"}
                out: List[bytes] = []
                for item in result:
                    try:
                        p = Path(item)
                        if p.suffix.lower() in valid_exts and p.is_file():
                            with Image.open(p) as img:
                                buf = io.BytesIO()
                                if img.mode not in ("RGB", "RGBA", "L"):
                                    img = img.convert(
                                        "RGBA" if "A" in img.mode else "RGB"
                                    )
                                img.save(buf, format="PNG")
                                out.append(buf.getvalue())
                    except Exception as e:
                        logger.debug("Failed to read image path %r: %s", item, e)
                return out

            return []
        except Exception as e:
            logger.debug("Win32ClipboardBackend.grab_png failed: %s", e)
            return []


class ClipboardImageWatcher:
    """Monitors clipboard sequence for image captures during dictation."""

    def __init__(
        self,
        sessions_dir: Path,
        backend: Optional[ClipboardBackend] = None,
        *,
        poll_sec: float = 0.15,
        clock: Callable[[], float] = time.monotonic,
        on_image: Optional[Callable[[int], None]] = None,
        keep_sessions: Optional[int] = None,
        trigger: str = "paste",
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._sessions_dir = Path(sessions_dir)
        self._backend = backend if backend is not None else Win32ClipboardBackend()
        self._poll_sec = poll_sec
        self._clock = clock
        self._on_image = on_image
        self._keep_sessions = keep_sessions
        self._trigger = trigger
        self._sleep = sleep

        self._lock = threading.RLock()
        self._images: List[CapturedImage] = []
        self._is_running: bool = False
        self._session_dir: Optional[Path] = None
        self._last_seq: int = 0
        self._grab_retries: int = 0
        self._last_captured_sha: Optional[str] = None
        self._t0: float = 0.0
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        with self._lock:
            if self._is_running:
                logger.warning("ClipboardImageWatcher is already running")
                return

            self._prune_sessions()
            self._images = []
            self._session_dir = None
            self._last_captured_sha = None
            self._last_seq = self._backend.sequence()
            self._grab_retries = 0
            self._t0 = self._clock()
            self._is_running = True
            self._stop_event.clear()

            if self._trigger == "auto":
                self._thread = threading.Thread(
                    target=self._run_loop,
                    name="ClipboardImageWatcherThread",
                    daemon=True,
                )
                self._thread.start()

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            self.poll_once()
            self._stop_event.wait(self._poll_sec)

    def poll_once(self) -> None:
        if self._trigger == "paste":
            return
        with self._lock:
            try:
                seq = self._backend.sequence()
                if seq != self._last_seq:
                    self._last_seq = seq
                    self._grab_retries = GRAB_RETRIES + 1
                elif self._grab_retries <= 0:
                    return
                # The writer (e.g. the snipping tool) may still hold the clipboard right
                # after the sequence changes, so an empty grab is retried on later polls.
                self._grab_retries -= 1          # before grabbing: a raising grab also counts
                png_list = self._backend.grab_png()
                if png_list:
                    self._grab_retries = 0
                for png_bytes in png_list:
                    if not png_bytes:
                        continue
                    sha = hashlib.sha1(png_bytes).hexdigest()
                    if sha == self._last_captured_sha:
                        continue

                    if self._session_dir is None:
                        session_name = (
                            f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
                        )
                        self._session_dir = self._sessions_dir / session_name
                        self._session_dir.mkdir(parents=True, exist_ok=True)

                    index = len(self._images) + 1
                    img_path = self._session_dir / f"img_{index}.png"
                    img_path.write_bytes(png_bytes)

                    dt = self._clock() - self._t0
                    captured = CapturedImage(
                        index=index,
                        t_sec=dt,
                        path=str(img_path.resolve()),
                    )
                    self._images.append(captured)
                    self._last_captured_sha = sha

                    if self._on_image is not None:
                        try:
                            self._on_image(len(self._images))
                        except Exception as e:
                            logger.debug("on_image callback failed: %s", e)
            except Exception as e:
                logger.debug("poll_once encountered an error: %s", e)

    def capture_now(self) -> int:
        """Grab the clipboard image now and save it into the current session."""
        try:
            with self._lock:
                if not self._is_running:
                    return 0
                t0 = self._t0

            png_list = self._backend.grab_png()
            retries = 3
            while not png_list and retries > 0:
                self._sleep(0.05)
                retries -= 1
                with self._lock:
                    if not self._is_running:
                        return 0
                png_list = self._backend.grab_png()

            if not png_list:
                return 0

            valid_pngs = [b for b in png_list if b]
            if not valid_pngs:
                return 0

            added = 0
            with self._lock:
                if not self._is_running:
                    return 0
                if self._session_dir is None:
                    session_name = (
                        f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
                    )
                    self._session_dir = self._sessions_dir / session_name
                    self._session_dir.mkdir(parents=True, exist_ok=True)

                dt = self._clock() - t0
                for png_bytes in valid_pngs:
                    index = len(self._images) + 1
                    img_path = self._session_dir / f"img_{index}.png"
                    img_path.write_bytes(png_bytes)
                    captured = CapturedImage(
                        index=index,
                        t_sec=dt,
                        path=str(img_path.resolve()),
                    )
                    self._images.append(captured)
                    added += 1
                total_count = len(self._images)

            if added > 0 and self._on_image is not None:
                try:
                    self._on_image(total_count)
                except Exception as e:
                    logger.debug("on_image callback failed: %s", e)

            return added
        except Exception as e:
            logger.debug("capture_now encountered an error: %s", e)
            return 0

    def stop(self) -> List[CapturedImage]:
        if self._trigger == "paste":
            with self._lock:
                if not self._is_running:
                    return []
                self._is_running = False
                return list(self._images)

        with self._lock:
            if not self._is_running:
                return []
            self._stop_event.set()

        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)

        with self._lock:
            self._thread = None
            self.poll_once()
            self._is_running = False
            return list(self._images)

    def cancel(self) -> None:
        with self._lock:
            if self._is_running:
                self._stop_event.set()

        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)

        with self._lock:
            self._thread = None
            self._is_running = False
            if self._session_dir is not None and self._session_dir.exists():
                shutil.rmtree(self._session_dir, ignore_errors=True)
            self._session_dir = None
            self._images.clear()
            self._last_captured_sha = None

    def _prune_sessions(self) -> None:
        if self._keep_sessions is None or not self._sessions_dir.exists():
            return
        try:
            dirs = sorted(
                [p for p in self._sessions_dir.iterdir() if p.is_dir()],
                key=lambda p: p.name,
            )
            if len(dirs) > self._keep_sessions:
                excess = len(dirs) - self._keep_sessions
                for old_dir in dirs[:excess]:
                    shutil.rmtree(old_dir, ignore_errors=True)
        except Exception as e:
            logger.debug("Pruning session directories failed: %s", e)

    @property
    def images(self) -> List[CapturedImage]:
        with self._lock:
            return list(self._images)

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._images)

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._is_running
