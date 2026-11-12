"""Unit tests for Task 4: Clipboard image capture."""
from __future__ import annotations

import io
import time
from pathlib import Path
from typing import List, Optional

import pytest
from PIL import Image

from local_typeless.capture.clipboard_images import (
    ClipboardBackend,
    ClipboardImageWatcher,
    Win32ClipboardBackend,
)
from local_typeless.types import CapturedImage


def make_png_bytes(color: str = "red", size: tuple[int, int] = (10, 10)) -> bytes:
    img = Image.new("RGB", size, color=color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class FakeClipboardBackend:
    def __init__(self, seq: int = 100, pngs: Optional[List[bytes]] = None) -> None:
        self.seq = seq
        self.pngs = pngs if pngs is not None else []
        self.raise_on_grab = False

    def sequence(self) -> int:
        return self.seq

    def grab_png(self) -> List[bytes]:
        if self.raise_on_grab:
            raise RuntimeError("simulated clipboard error")
        return list(self.pngs)


class FakeClock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


# 1. Image present before start() with unchanged sequence -> nothing captured
def test_image_present_before_start_unchanged_sequence(tmp_path: Path):
    png = make_png_bytes("red")
    backend = FakeClipboardBackend(seq=100, pngs=[png])
    clock = FakeClock()
    watcher = ClipboardImageWatcher(tmp_path, backend, clock=clock, trigger="auto")

    watcher.start()
    assert watcher.is_running
    # Poll directly without seq change
    watcher.poll_once()
    assert watcher.count == 0
    assert watcher.images == []
    imgs = watcher.stop()
    assert imgs == []


# 2. Sequence change with an image -> one file written, index == 1, t_sec equals fake clock delta, on_image(1) called
def test_sequence_change_with_image(tmp_path: Path):
    png = make_png_bytes("red")
    backend = FakeClipboardBackend(seq=100, pngs=[])
    clock = FakeClock()
    on_image_calls = []

    watcher = ClipboardImageWatcher(
        tmp_path, backend, clock=clock, on_image=lambda c: on_image_calls.append(c), trigger="auto"
    )
    watcher.start()

    clock.advance(3.5)
    backend.seq = 101
    backend.pngs = [png]
    watcher.poll_once()

    assert watcher.count == 1
    assert on_image_calls == [1]
    captured = watcher.images
    assert len(captured) == 1
    img = captured[0]
    assert img.index == 1
    assert img.t_sec == pytest.approx(3.5)
    p = Path(img.path)
    assert p.is_file()
    assert p.read_bytes() == png
    assert p.name == "img_1.png"

    imgs = watcher.stop()
    assert len(imgs) == 1
    assert imgs[0] == img


# 3. Second different image -> index == 2; same bytes again (new sequence) -> deduplicated
def test_second_different_image_and_deduplication(tmp_path: Path):
    png1 = make_png_bytes("red")
    png2 = make_png_bytes("blue")
    backend = FakeClipboardBackend(seq=100, pngs=[])
    clock = FakeClock()
    watcher = ClipboardImageWatcher(tmp_path, backend, clock=clock, trigger="auto")
    watcher.start()

    # Capture first image
    clock.advance(1.0)
    backend.seq = 101
    backend.pngs = [png1]
    watcher.poll_once()
    assert watcher.count == 1

    # Capture second distinct image
    clock.advance(2.0)
    backend.seq = 102
    backend.pngs = [png2]
    watcher.poll_once()
    assert watcher.count == 2
    assert watcher.images[1].index == 2
    assert watcher.images[1].t_sec == pytest.approx(3.0)

    # Sequence changes again, but with the same png2 bytes (snipping tool duplicate)
    clock.advance(0.5)
    backend.seq = 103
    backend.pngs = [png2]
    watcher.poll_once()
    assert watcher.count == 2  # Deduplicated

    watcher.stop()


# 4. Sequence change with no image (text copied) -> nothing captured, and a later image is still captured
def test_sequence_change_no_image_text_copied(tmp_path: Path):
    png = make_png_bytes("green")
    backend = FakeClipboardBackend(seq=100, pngs=[])
    clock = FakeClock()
    watcher = ClipboardImageWatcher(tmp_path, backend, clock=clock, trigger="auto")
    watcher.start()

    # Text copied (seq changed, but grab_png returns [])
    backend.seq = 101
    backend.pngs = []
    watcher.poll_once()
    assert watcher.count == 0

    # Later image copied
    clock.advance(2.0)
    backend.seq = 102
    backend.pngs = [png]
    watcher.poll_once()
    assert watcher.count == 1
    assert watcher.images[0].index == 1

    watcher.stop()


# 5. stop() performs a final poll and returns images; second stop() -> []
def test_stop_performs_final_poll_and_second_stop_empty(tmp_path: Path):
    png = make_png_bytes("yellow")
    backend = FakeClipboardBackend(seq=100, pngs=[])
    clock = FakeClock()
    watcher = ClipboardImageWatcher(tmp_path, backend, clock=clock, trigger="auto")
    watcher.start()

    # Change seq and png right before stop without calling poll_once
    clock.advance(1.5)
    backend.seq = 101
    backend.pngs = [png]

    imgs = watcher.stop()
    assert not watcher.is_running
    assert len(imgs) == 1
    assert imgs[0].index == 1

    # Second stop
    second_stop = watcher.stop()
    assert second_stop == []


# 6. cancel() removes the session directory and files
def test_cancel_removes_session_directory_and_clears_list(tmp_path: Path):
    png = make_png_bytes("purple")
    backend = FakeClipboardBackend(seq=100, pngs=[])
    clock = FakeClock()
    watcher = ClipboardImageWatcher(tmp_path, backend, clock=clock, trigger="auto")
    watcher.start()

    backend.seq = 101
    backend.pngs = [png]
    watcher.poll_once()
    assert watcher.count == 1
    img_path = Path(watcher.images[0].path)
    session_dir = img_path.parent
    assert session_dir.is_dir()
    assert img_path.is_file()

    watcher.cancel()
    assert not watcher.is_running
    assert watcher.count == 0
    assert watcher.images == []
    assert not session_dir.exists()


# 7. Session pruning keeps only keep_sessions newest directories
def test_session_pruning_keeps_newest_directories(tmp_path: Path):
    # Pre-populate dummy session directories in tmp_path
    sessions = [
        "20261111-100001-aaaaaa",
        "20261111-100002-bbbbbb",
        "20261111-100003-cccccc",
        "20261111-100004-dddddd",
        "20261111-100005-eeeeee",
    ]
    for s in sessions:
        d = tmp_path / s
        d.mkdir()
        (d / "dummy.png").write_bytes(b"data")

    backend = FakeClipboardBackend(seq=100)
    # keep_sessions=3
    watcher = ClipboardImageWatcher(tmp_path, backend, keep_sessions=3)
    watcher.start()
    watcher.stop()

    remaining = sorted([d.name for d in tmp_path.iterdir() if d.is_dir()])
    assert remaining == [
        "20261111-100003-cccccc",
        "20261111-100004-dddddd",
        "20261111-100005-eeeeee",
    ]


# 8. Backend grab_png raising -> poll_once does not raise; on_image raising -> capture still recorded
def test_backend_raising_and_on_image_raising_handled_gracefully(tmp_path: Path):
    png = make_png_bytes("orange")
    backend = FakeClipboardBackend(seq=100, pngs=[])
    clock = FakeClock()

    def bad_callback(c: int) -> None:
        raise RuntimeError("callback failure")

    watcher = ClipboardImageWatcher(tmp_path, backend, clock=clock, on_image=bad_callback, trigger="auto")
    watcher.start()

    # 1. backend grab_png raises
    backend.seq = 101
    backend.raise_on_grab = True
    # Must not raise
    watcher.poll_once()
    assert watcher.count == 0

    # 2. backend grab_png succeeds, but on_image callback raises
    backend.raise_on_grab = False
    backend.seq = 102
    backend.pngs = [png]
    watcher.poll_once()
    assert watcher.count == 1
    assert watcher.images[0].index == 1

    watcher.stop()


# 9. One threaded smoke test: start(), bump the fake sequence, wait for count == 1, stop()
def test_threaded_smoke_test(tmp_path: Path):
    png = make_png_bytes("cyan")
    backend = FakeClipboardBackend(seq=100, pngs=[])
    watcher = ClipboardImageWatcher(tmp_path, backend, poll_sec=0.02, trigger="auto")
    watcher.start()
    assert watcher.is_running

    backend.seq = 101
    backend.pngs = [png]

    deadline = time.monotonic() + 2.0
    while watcher.count < 1 and time.monotonic() < deadline:
        time.sleep(0.01)

    assert watcher.count == 1
    imgs = watcher.stop()
    assert not watcher.is_running
    assert len(imgs) == 1
    assert imgs[0].index == 1


# 10. Win32ClipboardBackend.grab_png with PIL.ImageGrab.grabclipboard monkeypatched
def test_win32_clipboard_backend_grab_png(tmp_path: Path, monkeypatch):
    import PIL.ImageGrab as imagegrab

    backend = Win32ClipboardBackend()

    # Case A: returns PIL.Image.Image
    img_obj = Image.new("RGB", (20, 20), color="pink")
    monkeypatch.setattr(imagegrab, "grabclipboard", lambda: img_obj)
    res_img = backend.grab_png()
    assert len(res_img) == 1
    # Verify it is valid PNG bytes
    parsed = Image.open(io.BytesIO(res_img[0]))
    assert parsed.format == "PNG"

    # Case B: returns list of files (valid image file + non-image file + missing file)
    valid_file = tmp_path / "test.jpg"
    img_obj.save(valid_file, format="JPEG")
    text_file = tmp_path / "notes.txt"
    text_file.write_text("hello", encoding="utf-8")
    missing_file = tmp_path / "nonexistent.png"

    monkeypatch.setattr(
        imagegrab,
        "grabclipboard",
        lambda: [str(valid_file), str(text_file), str(missing_file)],
    )
    res_files = backend.grab_png()
    assert len(res_files) == 1
    parsed_file = Image.open(io.BytesIO(res_files[0]))
    assert parsed_file.format == "PNG"

    # Case C: returns None
    monkeypatch.setattr(imagegrab, "grabclipboard", lambda: None)
    assert backend.grab_png() == []

    # Case D: grabclipboard raises
    def crash():
        raise OSError("clipboard locked")

    monkeypatch.setattr(imagegrab, "grabclipboard", crash)
    assert backend.grab_png() == []


# 11. Clipboard still locked right after the change: an empty grab is retried on later polls
def test_empty_grab_after_change_is_retried(tmp_path: Path):
    from local_typeless.capture.clipboard_images import GRAB_RETRIES

    backend = FakeClipboardBackend(seq=100)
    watcher = ClipboardImageWatcher(tmp_path, backend, clock=FakeClock(), trigger="auto")
    watcher.start()
    watcher._stop_event.set()          # drive polls by hand
    watcher._thread.join(timeout=1.0)

    backend.seq = 101                  # changed, but the writer still holds the clipboard
    watcher.poll_once()
    assert watcher.count == 0
    backend.pngs = [make_png_bytes("blue")]
    watcher.poll_once()                # same sequence: retried
    assert watcher.count == 1
    watcher.poll_once()
    assert watcher.count == 1          # not captured twice

    backend.pngs = []                  # text copy: retries are bounded
    backend.seq = 102
    calls = []
    backend.grab_png = lambda: calls.append(1) or []
    for _ in range(GRAB_RETRIES + 5):
        watcher.poll_once()
    assert len(calls) == GRAB_RETRIES + 1
    watcher.cancel()


# ------------------------------------------------------------ paste mode tests
def test_paste_mode_no_thread_and_poll_once_noop(tmp_path: Path):
    backend = FakeClipboardBackend(seq=100, pngs=[make_png_bytes("red")])
    watcher = ClipboardImageWatcher(tmp_path, backend)  # default trigger="paste"
    assert getattr(watcher, "_trigger", "paste") == "paste"
    watcher.start()
    assert watcher.is_running
    assert getattr(watcher, "_thread", None) is None

    # poll_once is a no-op
    watcher.poll_once()
    assert watcher.count == 0
    imgs = watcher.stop()
    assert imgs == []
    assert not watcher.is_running


def test_paste_mode_pre_existing_image_captured_by_capture_now(tmp_path: Path):
    png = make_png_bytes("green")
    backend = FakeClipboardBackend(seq=100, pngs=[png])
    clock = FakeClock(1000.0)
    calls = []
    watcher = ClipboardImageWatcher(
        tmp_path, backend, clock=clock, on_image=lambda c: calls.append(c)
    )
    watcher.start()
    clock.advance(2.5)

    added = watcher.capture_now()
    assert added == 1
    assert watcher.count == 1
    assert calls == [1]
    assert len(watcher.images) == 1
    img = watcher.images[0]
    assert img.index == 1
    assert img.t_sec == pytest.approx(2.5)
    assert Path(img.path).is_file()
    assert Path(img.path).read_bytes() == png


def test_paste_mode_same_image_twice_yields_two_entries(tmp_path: Path):
    png = make_png_bytes("blue")
    backend = FakeClipboardBackend(seq=100, pngs=[png])
    clock = FakeClock(1000.0)
    calls = []
    watcher = ClipboardImageWatcher(
        tmp_path, backend, clock=clock, on_image=lambda c: calls.append(c)
    )
    watcher.start()

    clock.advance(1.0)
    added1 = watcher.capture_now()
    assert added1 == 1

    clock.advance(1.5)
    added2 = watcher.capture_now()
    assert added2 == 1

    assert watcher.count == 2
    assert calls == [1, 2]
    assert watcher.images[0].index == 1
    assert watcher.images[0].t_sec == pytest.approx(1.0)
    assert watcher.images[1].index == 2
    assert watcher.images[1].t_sec == pytest.approx(2.5)


def test_paste_mode_empty_clipboard_retries(tmp_path: Path):
    backend = FakeClipboardBackend(seq=100, pngs=[])
    sleep_calls = []
    watcher = ClipboardImageWatcher(
        tmp_path, backend, sleep=lambda s: sleep_calls.append(s)
    )
    watcher.start()

    added = watcher.capture_now()
    assert added == 0
    assert watcher.count == 0
    assert sleep_calls == [0.05, 0.05, 0.05]


def test_paste_mode_not_running_returns_zero(tmp_path: Path):
    backend = FakeClipboardBackend(seq=100, pngs=[make_png_bytes("red")])
    watcher = ClipboardImageWatcher(tmp_path, backend)
    # Not started
    assert watcher.capture_now() == 0
