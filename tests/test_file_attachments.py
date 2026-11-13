"""File attachments ([檔案N]) and pasting images as absolute paths."""
from __future__ import annotations

from pathlib import Path

import pytest

from local_typeless.capture.clipboard_images import ClipboardImageWatcher
from local_typeless.config.loader import _PydanticOutput
from local_typeless.output.record_store import RecordStore
from local_typeless.output.rich_paste import RichPaster, format_path
from local_typeless.textblocks import (
    canonicalize_markers,
    find_markers,
    marker_for,
    place_markers,
    strip_markers,
)
from local_typeless.types import CapturedImage, Mode, OutputConfig, PipelineResult, Segment


def _file(index: int, path: str, t: float = 0.0) -> CapturedImage:
    return CapturedImage(index=index, t_sec=t, path="", kind="file", text=path)


def _image(index: int, path: str) -> CapturedImage:
    return CapturedImage(index=index, t_sec=0.0, path=path, kind="image")


class Rec:
    """Records what a RichPaster copies/pastes."""

    def __init__(self, app: str = "", **cfg) -> None:
        self.steps: list = []
        self.paster = RichPaster(
            OutputConfig(paste_step_delay_ms=0, **cfg),
            copy_text=lambda t: self.steps.append(("text", t)),
            set_image=lambda p: self.steps.append(("image", str(p))),
            set_table=lambda item, fmt: self.steps.append(("table", fmt)),
            foreground_app=lambda: app,
            paste=lambda: self.steps.append(("paste",)),
            sleep=lambda s: None,
        )

    @property
    def texts(self) -> list:
        return [s[1] for s in self.steps if s[0] == "text"]

    @property
    def kinds(self) -> list:
        return [s[0] for s in self.steps if s[0] != "paste"]


# ------------------------------------------------------------------ markers
def test_file_marker_roundtrip():
    item = _file(2, r"D:\data\report.pdf")
    assert marker_for(item) == "[檔案2]"
    assert find_markers("看 [檔案2] 和 [圖1]") == ["[檔案2]", "[圖1]"]
    assert strip_markers("看[檔案2]這份") == "看這份"
    assert canonicalize_markers("【档案 ２】 [File3]") == "[檔案2] [檔案3]"


def test_place_markers_puts_file_at_its_time():
    segs = [Segment(0.0, 1.0, "請看"), Segment(2.0, 3.0, "這份報告")]
    out = place_markers(segs, [_file(1, r"D:\a.pdf", t=1.5)])
    assert out == "請看[檔案1]這份報告"


# -------------------------------------------------------------------- paste
def test_file_is_pasted_as_absolute_path_in_one_step():
    r = Rec(app="chrome.exe")
    r.paster.emit_rich("請看[檔案1]這份報告", [_file(1, r"D:\data\report.pdf")])
    assert r.kinds == ["text"]
    assert r.texts == [r"請看 D:\data\report.pdf 這份報告"]


def test_path_with_spaces_is_quoted_and_adjacent_paths_are_separated():
    r = Rec()
    r.paster.emit_rich(
        "[檔案1][檔案2]",
        [_file(1, r"D:\my docs\a b.txt"), _file(2, r"D:\x.txt")],
    )
    assert r.texts == [r'"D:\my docs\a b.txt" D:\x.txt']
    assert format_path(r"D:\x.txt") == r"D:\x.txt"


def test_no_extra_space_when_already_separated():
    r = Rec()
    r.paster.emit_rich("看 [檔案1] 這份\n[檔案2]", [_file(1, r"D:\a.txt"), _file(2, r"D:\b.txt")])
    assert r.texts == ["看 D:\\a.txt 這份\nD:\\b.txt"]


def test_image_auto_mode_pastes_path_in_terminal_and_bitmap_elsewhere():
    img = _image(1, r"D:\records\images\s1\img_1.png")

    term = Rec(app="WindowsTerminal.exe")
    term.paster.emit_rich("結果如[圖1]所示", [img])
    assert term.kinds == ["text"]
    assert term.texts == [r"結果如 D:\records\images\s1\img_1.png 所示"]

    browser = Rec(app="chrome.exe")
    browser.paster.emit_rich("結果如[圖1]所示", [img])
    assert browser.kinds == ["text", "image", "text"]


def test_image_paste_mode_can_be_forced():
    img = _image(1, r"D:\i.png")
    forced_path = Rec(app="chrome.exe", image_paste_mode="path")
    forced_path.paster.emit_rich("a[圖1]", [img])
    assert forced_path.texts == [r"a D:\i.png"]

    forced_image = Rec(app="windowsterminal.exe", image_paste_mode="image")
    forced_image.paster.emit_rich("a[圖1]", [img])
    assert forced_image.kinds == ["text", "image"]


def test_path_paste_apps_is_configurable():
    r = Rec(app="code.exe", path_paste_apps=["Code.exe"])
    r.paster.emit_rich("[圖1]", [_image(1, r"D:\i.png")])
    assert r.texts == [r"D:\i.png"]


def test_config_rejects_unknown_image_paste_mode():
    assert _PydanticOutput().image_paste_mode == "auto"
    with pytest.raises(ValueError):
        _PydanticOutput(image_paste_mode="link")


# ------------------------------------------------------------------ capture
class FakeBackend:
    def __init__(self, files=(), png=(), text="", html=""):
        self._files, self._png, self._text, self._html = list(files), list(png), text, html

    def sequence(self) -> int:
        return 1

    def grab_png(self):
        return list(self._png)

    def grab_html(self) -> str:
        return self._html

    def grab_text(self) -> str:
        return self._text

    def grab_files(self):
        return list(self._files)


def test_add_files_only_while_recording(tmp_path: Path):
    seen: list = []
    w = ClipboardImageWatcher(tmp_path, FakeBackend(), on_image=lambda n, kind: seen.append((n, kind)))
    assert w.add_files([r"D:\a.pdf"]) == 0

    w.start()
    assert w.add_files([r"D:\a.pdf", r"D:\b.png"]) == 2
    items = w.stop()
    assert [(i.index, i.kind, i.text, i.path) for i in items] == [
        (1, "file", r"D:\a.pdf", ""),
        (2, "file", r"D:\b.png", ""),      # a dropped picture stays a path too
    ]
    assert seen == [(2, "file")]
    assert not any(tmp_path.iterdir())     # nothing is copied into the session folder


def test_capture_now_takes_copied_files_as_paths(tmp_path: Path):
    w = ClipboardImageWatcher(tmp_path, FakeBackend(files=[r"D:\docs\spec.docx", r"D:\pic.png"]))
    w.start()
    assert w.capture_now() == 1            # the .png is left to the image path (grab_png)
    (item,) = w.stop()
    assert (item.kind, item.text) == ("file", r"D:\docs\spec.docx")


# ------------------------------------------------------------------- record
def test_record_file_shows_the_path(tmp_path: Path):
    store = RecordStore(tmp_path / "rec", tmp_path / "img")
    result = PipelineResult(
        raw_text="看[檔案1]",
        final_text="請看[檔案1]",
        mode=Mode.CLEAN,
        stt_profile_name="t",
        llm_profile_name="t",
        stt_elapsed_sec=0.0,
        llm_elapsed_sec=0.0,
        fallback_triggered=False,
        images=[_file(1, r"D:\data\report.pdf")],
    )
    path = store.save(result)
    assert r"請看`D:\data\report.pdf`" in Path(path).read_text(encoding="utf-8")
