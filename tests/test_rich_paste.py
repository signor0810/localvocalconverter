"""Unit tests for Task 6: Rich paste (text -> image -> text)."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional, Union
from unittest.mock import patch

import pytest

from local_typeless.output.clipboard_out import ClipboardOutput, copy_with_retry
from local_typeless.output.rich_paste import RichPaster, set_clipboard_image
from local_typeless.types import CapturedImage, Mode, OutputConfig


def make_harness(
    cfg: Optional[OutputConfig] = None,
    *,
    set_table=None,
    foreground_app: Optional[Callable[[], str]] = None,
):
    calls = []

    def fake_copy(t: str) -> None:
        calls.append(("copy", t))

    def fake_image(p: Union[str, Path]) -> None:
        calls.append(("image", str(p)))

    def fake_table_default(item: CapturedImage, fmt: str) -> None:
        calls.append(("table", item, fmt))

    def fake_paste() -> None:
        calls.append(("paste",))

    def fake_sleep(s: float) -> None:
        calls.append(("sleep", s))

    paster = RichPaster(
        cfg or OutputConfig(),
        copy_text=fake_copy,
        set_image=fake_image,
        set_table=set_table or fake_table_default,
        foreground_app=foreground_app or (lambda: "winword.exe"),
        paste=fake_paste,
        sleep=fake_sleep,
    )
    return paster, calls


# 1. Text without images -> [("copy", text), ("paste",)], no sleep
def test_text_without_images():
    paster, calls = make_harness()
    paster.emit("hello world", Mode.CLEAN)
    assert calls == [("copy", "hello world"), ("paste",)]

    calls.clear()
    paster.emit_rich("plain text", [])
    assert calls == [("copy", "plain text"), ("paste",)]


# 2. "按照 [圖1] 的風格" + image 1 -> copy "按照 ", paste, sleep, image, paste, sleep, copy " 的風格", paste, sleep (0.3s)
def test_text_with_image_in_middle():
    paster, calls = make_harness()
    img = CapturedImage(index=1, t_sec=1.0, path="/path/to/img_1.png")
    paster.emit_rich("按照 [圖1] 的風格", [img])
    assert calls == [
        ("copy", "按照 "),
        ("paste",),
        ("sleep", 0.3),
        ("image", "/path/to/img_1.png"),
        ("paste",),
        ("sleep", 0.3),
        ("copy", " 的風格"),
        ("paste",),
        ("sleep", 0.3),
    ]


# 3. Image first and image last; two adjacent images
def test_image_first_image_last_and_adjacent_images():
    # Case A: Image first
    paster, calls = make_harness()
    img1 = CapturedImage(index=1, t_sec=1.0, path="/img_1.png")
    paster.emit_rich("[圖1] 開頭", [img1])
    assert calls == [
        ("image", "/img_1.png"),
        ("paste",),
        ("sleep", 0.3),
        ("copy", " 開頭"),
        ("paste",),
        ("sleep", 0.3),
    ]

    # Case B: Image last
    calls.clear()
    paster.emit_rich("結尾 [圖1]", [img1])
    assert calls == [
        ("copy", "結尾 "),
        ("paste",),
        ("sleep", 0.3),
        ("image", "/img_1.png"),
        ("paste",),
        ("sleep", 0.3),
    ]

    # Case C: Two adjacent images
    calls.clear()
    img2 = CapturedImage(index=2, t_sec=2.0, path="/img_2.png")
    paster.emit_rich("[圖1] [圖2]", [img1, img2])
    assert calls == [
        ("image", "/img_1.png"),
        ("paste",),
        ("sleep", 0.3),
        ("image", "/img_2.png"),
        ("paste",),
        ("sleep", 0.3),
    ]


# 4. set_image raising OSError -> marker text pasted instead, remaining parts still pasted
def test_set_image_raising_oserror_falls_back_to_marker():
    calls = []

    def bad_image(p: Union[str, Path]) -> None:
        calls.append(("image_failed", str(p)))
        raise OSError("failed to set clipboard image")

    paster = RichPaster(
        OutputConfig(),
        copy_text=lambda t: calls.append(("copy", t)),
        set_image=bad_image,
        foreground_app=lambda: "",       # not the terminal the tests happen to run in
        paste=lambda: calls.append(("paste",)),
        sleep=lambda s: calls.append(("sleep", s)),
    )
    img = CapturedImage(index=1, t_sec=1.0, path="/bad.png")
    paster.emit_rich("前 [圖1] 後", [img])
    assert calls == [
        ("copy", "前 "),
        ("paste",),
        ("sleep", 0.3),
        ("image_failed", "/bad.png"),
        ("copy", "[圖1]"),
        ("paste",),
        ("sleep", 0.3),
        ("copy", " 後"),
        ("paste",),
        ("sleep", 0.3),
    ]


# 5. copy_text raising -> propagates
def test_copy_text_raising_propagates():
    def boom_copy(t: str) -> None:
        raise RuntimeError("clipboard copy error")

    paster = RichPaster(
        OutputConfig(),
        copy_text=boom_copy,
        paste=lambda: None,
        sleep=lambda s: None,
    )
    with pytest.raises(RuntimeError, match="clipboard copy error"):
        paster.emit_rich("hello", [])

    img = CapturedImage(index=1, t_sec=1.0, path="/p.png")
    with pytest.raises(RuntimeError, match="clipboard copy error"):
        paster.emit_rich("前 [圖1]", [img])


# 6. set_clipboard_image on a missing file raises OSError; real round-trip on Windows
def test_set_clipboard_image_missing_file_raises():
    with pytest.raises(OSError):
        set_clipboard_image("nonexistent_image_12345.png")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
def test_set_clipboard_image_real_windows(tmp_path: Path):
    import ctypes
    from PIL import Image

    test_img = tmp_path / "test4x4.png"
    Image.new("RGB", (4, 4), color="blue").save(test_img, format="PNG")

    set_clipboard_image(test_img)

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.IsClipboardFormatAvailable.argtypes = [ctypes.c_uint]
    user32.IsClipboardFormatAvailable.restype = ctypes.c_int
    assert bool(user32.IsClipboardFormatAvailable(8))


# 7. ClipboardOutput.emit still passes after copy_with_retry extraction
def test_clipboard_output_emit_with_copy_with_retry():
    copied = []
    with patch(
        "local_typeless.output.clipboard_out.pyperclip.copy",
        side_effect=copied.append,
    ):
        out = ClipboardOutput(OutputConfig(clipboard=True, auto_paste=False))
        out.emit("test extraction", Mode.CLEAN)
    assert copied == ["test extraction"]


def test_link_and_text_inlined_without_extra_paste():
    paster, calls = make_harness()
    link_item = CapturedImage(index=1, t_sec=1.0, path="", kind="link", text="https://example.com")
    text_item = CapturedImage(index=2, t_sec=2.0, path="", kind="text", text="一段補充文字")

    # Only link and text: single copy + single paste, no sleep
    paster.emit_rich("參考 [連結1] 以及 [文字2]", [link_item, text_item])
    assert calls == [
        ("copy", "參考 https://example.com 以及 一段補充文字"),
        ("paste",),
    ]


def test_table_paste_formats():
    tab = CapturedImage(index=1, t_sec=1.0, path="/tab.md", kind="table", text="| A | B |\n| --- | --- |\n| 1 | 2 |", html="<table>...</table>")

    # auto format: foreground app is in table_html_apps -> "html"
    paster_word, calls_word = make_harness(
        OutputConfig(table_paste_format="auto"),
        foreground_app=lambda: "winword.exe",
    )
    paster_word.emit_rich("前 [表1] 後", [tab])
    assert calls_word == [
        ("copy", "前 "),
        ("paste",),
        ("sleep", 0.3),
        ("table", tab, "html"),
        ("paste",),
        ("sleep", 0.3),
        ("copy", " 後"),
        ("paste",),
        ("sleep", 0.3),
    ]

    # auto format: foreground app NOT in table_html_apps (e.g. Chrome/Notepad) -> "markdown" via copy_text
    paster_chrome, calls_chrome = make_harness(
        OutputConfig(table_paste_format="auto"),
        foreground_app=lambda: "chrome.exe",
    )
    paster_chrome.emit_rich("[表1]", [tab])
    assert calls_chrome == [
        ("copy", "| A | B |\n| --- | --- |\n| 1 | 2 |\n\n"),
        ("paste",),
    ]

    # markdown format: always via copy_text
    paster_md, calls_md = make_harness(OutputConfig(table_paste_format="markdown"))
    paster_md.emit_rich("[表1]", [tab])
    assert calls_md == [
        ("copy", "| A | B |\n| --- | --- |\n| 1 | 2 |\n\n"),
        ("paste",),
    ]

    # html format: always calls set_table with "html"
    paster_html, calls_html = make_harness(
        OutputConfig(table_paste_format="html"),
        foreground_app=lambda: "chrome.exe",
    )
    paster_html.emit_rich("[表1]", [tab])
    assert calls_html == [
        ("table", tab, "html"),
        ("paste",),
        ("sleep", 0.3),
    ]


def test_single_pass_substitution_no_recursive_expansion():
    paster, calls = make_harness()
    txt = CapturedImage(index=1, t_sec=1.0, path="", kind="text", text="文字包含 [圖2] 標記")
    paster.emit_rich("前言 [文字1] 結尾", [txt])
    assert calls == [
        ("copy", "前言 文字包含 [圖2] 標記 結尾"),
        ("paste",),
    ]



def test_table_paste_failure_falls_back_to_markdown():
    def bad_table(item: CapturedImage, fmt: str) -> None:
        raise OSError("failed to set clipboard table")

    paster, calls = make_harness(OutputConfig(table_paste_format="html"), set_table=bad_table)
    tab = CapturedImage(index=1, t_sec=1.0, path="/tab.md", kind="table", text="| A |\n| - |\n| 1 |")
    paster.emit_rich("前 [表1]", [tab])
    assert calls == [
        ("copy", "前 "),
        ("paste",),
        ("sleep", 0.3),
        ("copy", "\n| A |\n| - |\n| 1 |\n"),
        ("paste",),
        ("sleep", 0.3),
    ]



def test_markdown_table_is_inlined_on_its_own_lines():
    tab = CapturedImage(index=1, t_sec=1.0, path="/tab.md", kind="table",
                        text="| A | B |\n| --- | --- |\n| 1 | 2 |")
    paster, calls = make_harness(OutputConfig(table_paste_format="auto"), foreground_app=lambda: "chrome.exe")
    paster.emit_rich("結果如下[表1]以上。", [tab])
    assert calls == [
        ("copy", "結果如下\n\n| A | B |\n| --- | --- |\n| 1 | 2 |\n\n以上。"),
        ("paste",),
    ]
