"""Unit tests for DraftModel and MemoryBuffer (no Tkinter, no network, tmp_path only)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional

import pytest

from local_typeless.types import DraftPiece
from local_typeless.ui.draft_model import DraftModel, MemoryBuffer


class FakeClipboardBackend:
    def __init__(
        self,
        files: Optional[List[str]] = None,
        png_list: Optional[List[bytes]] = None,
        text: str = "",
    ) -> None:
        self._files = files or []
        self._png_list = png_list or []
        self._text = text

    def grab_files(self) -> List[str]:
        return list(self._files)

    def grab_png(self) -> List[bytes]:
        return list(self._png_list)

    def grab_text(self) -> str:
        return self._text


class FakeClock:
    def __init__(self, start: float = 100.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, dt: float) -> None:
        self.now += dt


@pytest.fixture
def fake_clock():
    return FakeClock(100.0)


@pytest.fixture
def model(tmp_path: Path, fake_clock: FakeClock):
    images_dir = tmp_path / "images"
    cache_dir = tmp_path / "cache"
    buf = MemoryBuffer()
    return DraftModel(
        buf,
        images_dir=images_dir,
        cache_dir=cache_dir,
        clock=fake_clock,
        autosave=True,
    )


# ------------------------------------------------------------- Basic & Dictation
def test_dictation_point_advances_with_pieces(model: DraftModel):
    model.recording_started()
    piece1 = DraftPiece(job_id=1, seq=0, start_sec=0.0, end_sec=1.0, raw_text="OPC", text="OPC")
    model.add_piece(piece1)
    assert model.buffer.get_text() == "OPC"

    piece2 = DraftPiece(job_id=1, seq=1, start_sec=1.0, end_sec=2.0, raw_text="model", text="model")
    model.add_piece(piece2)
    # Alphanumeric pieces should be separated by a space
    assert model.buffer.get_text() == "OPC model"
    assert model.raw_transcript() == "OPC model"


def test_edit_elsewhere_does_not_affect_dictation_point(model: DraftModel):
    model.recording_started()
    piece1 = DraftPiece(job_id=1, seq=0, start_sec=0.0, end_sec=1.0, raw_text="model", text="model")
    model.add_piece(piece1)
    assert model.buffer.get_text() == "model"

    # User moves cursor to position 0 and types "ILT "
    model.buffer.set_cursor(0)
    model.buffer.insert(0, "ILT ")
    assert model.buffer.get_text() == "ILT model"

    # Next piece should arrive at the dictation point (after "model"), not at cursor 0
    piece2 = DraftPiece(job_id=1, seq=1, start_sec=1.0, end_sec=2.0, raw_text="tuning", text="tuning")
    model.add_piece(piece2)
    assert model.buffer.get_text() == "ILT model tuning"


def test_piece_spacing_rules(model: DraftModel):
    model.recording_started()
    # English + English -> space
    model.add_piece(DraftPiece(1, 0, 0.0, 1.0, "EUV", "EUV"))
    model.add_piece(DraftPiece(1, 1, 1.0, 2.0, "source", "source"))
    assert model.buffer.get_text() == "EUV source"

    # Chinese + Chinese -> no space
    model.finish()
    model.recording_started()
    model.add_piece(DraftPiece(2, 0, 0.0, 1.0, "計算", "計算"))
    model.add_piece(DraftPiece(2, 1, 1.0, 2.0, "微影", "微影"))
    assert model.buffer.get_text() == "計算微影"

    # Chinese + English -> no extra space
    model.add_piece(DraftPiece(2, 2, 2.0, 3.0, "OPC", "OPC"))
    assert model.buffer.get_text() == "計算微影OPC"

    # Empty text piece is ignored
    model.add_piece(DraftPiece(2, 3, 3.0, 4.0, "", ""))
    assert model.buffer.get_text() == "計算微影OPC"


def test_piece_when_not_recording(model: DraftModel):
    # Stopped recording: piece belonging to the recording still goes to dictation point
    model.recording_started()
    model.buffer.insert(0, "prefix ")
    model.recording_stopped()
    piece = DraftPiece(1, 0, 0.0, 1.0, "chunk", "chunk")
    model.add_piece(piece)
    assert "chunk" in model.buffer.get_text()

    # Retry when completely idle (no prior recording started) inserts at cursor
    model.finish()
    model.buffer.set_text("before after")
    model.buffer.set_cursor(7)  # between "before " and "after"
    retry_piece = DraftPiece(99, 0, 0.0, 1.0, "middle", "middle")
    model.add_piece(retry_piece)
    assert model.buffer.get_text() == "before middle after"


# ------------------------------------------------------------- Embedding
def test_embedding_priority(model: DraftModel, tmp_path: Path):
    f = tmp_path / "test.txt"
    f.write_text("dummy", encoding="utf-8")

    # Priority 1: Files
    backend_files = FakeClipboardBackend(
        files=[str(f)],
        png_list=[b"\x89PNG\r\n\x1a\nfake"],
        text="plain text",
    )
    res = model.paste_clipboard(backend_files)
    assert res == "file"
    assert str(f) in model.buffer.get_text()

    # Priority 2: Image
    model.finish()
    backend_img = FakeClipboardBackend(
        png_list=[b"\x89PNG\r\n\x1a\nfake"],
        text="plain text",
    )
    res = model.paste_clipboard(backend_img)
    assert res == "image"
    assert ".png" in model.buffer.get_text()

    # Priority 3: Text
    model.finish()
    backend_txt = FakeClipboardBackend(text="hello world")
    res = model.paste_clipboard(backend_txt)
    assert res == "text"
    assert model.buffer.get_text() == "hello world"

    # Nothing usable
    model.finish()
    backend_empty = FakeClipboardBackend()
    res = model.paste_clipboard(backend_empty)
    assert res == ""
    assert model.buffer.get_text() == ""


def test_file_embedding_formats_paths_and_spacing(model: DraftModel, tmp_path: Path):
    file_with_space = tmp_path / "my doc.txt"
    file_with_space.write_text("x", encoding="utf-8")

    # Insert between characters without spaces -> pads with spaces
    model.buffer.set_text("AB")
    model.buffer.set_cursor(1)  # between A and B
    model.insert_files([str(file_with_space)])

    text = model.buffer.get_text()
    expected_path = f'"{file_with_space.absolute()}"'
    assert text == f"A {expected_path} B"

    # Embedded ranges should be tagged
    ranges = model.buffer.get_tag_ranges("embed")
    assert len(ranges) == 1
    start, end = ranges[0]
    assert model.buffer.get_text()[start:end] == expected_path


def test_image_embedding_saves_png_and_inserts_path(model: DraftModel, tmp_path: Path):
    png_bytes = b"\x89PNG\r\n\x1a\nimage_payload"
    backend = FakeClipboardBackend(png_list=[png_bytes])

    res = model.paste_clipboard(backend)
    assert res == "image"
    text = model.buffer.get_text()
    saved_file = Path(text.strip('"'))
    assert saved_file.exists()
    assert saved_file.read_bytes() == png_bytes
    assert saved_file.name.startswith("draft-")
    assert saved_file.name.endswith(".png")


# ------------------------------------------------------------- Time Ordering (Rule 3)
def test_time_ordering_with_embedding_and_late_pieces(model: DraftModel, fake_clock: FakeClock, tmp_path: Path):
    doc_file = tmp_path / "recipe.txt"
    doc_file.write_text("recipe", encoding="utf-8")

    # t = 100.0: Start recording
    model.recording_started()

    # t = 102.5: User embeds file at dictation point (rel t = 2.5s)
    fake_clock.advance(2.5)
    model.paste_clipboard(FakeClipboardBackend(files=[str(doc_file)]))

    # Late piece 1 arrives: spoken at rel t = 0.0s (start_sec = 0.0 < 2.5s)
    p1 = DraftPiece(1, 0, 0.0, 2.0, "請看這個檔案", "請看這個檔案")
    model.add_piece(p1)

    # Late piece 2 arrives: spoken at rel t = 3.0s (start_sec = 3.0 >= 2.5s)
    p2 = DraftPiece(1, 1, 3.0, 4.5, "幫我分析", "幫我分析")
    model.add_piece(p2)

    expected_path = str(doc_file.absolute())
    assert model.buffer.get_text() == f"請看這個檔案 {expected_path} 幫我分析"


def test_time_ordering_pieces_arriving_out_of_order(model: DraftModel, fake_clock: FakeClock, tmp_path: Path):
    doc_file = tmp_path / "data.csv"
    doc_file.write_text("col", encoding="utf-8")

    model.recording_started()
    fake_clock.advance(3.0)
    model.paste_clipboard(FakeClipboardBackend(files=[str(doc_file)]))

    # Piece 2 (after embedding) arrives BEFORE Piece 1 (before embedding)
    p2 = DraftPiece(1, 1, 3.5, 5.0, "幫我分析", "幫我分析")
    model.add_piece(p2)

    p1 = DraftPiece(1, 0, 0.5, 2.0, "請看這個檔案", "請看這個檔案")
    model.add_piece(p1)

    expected_path = str(doc_file.absolute())
    assert model.buffer.get_text() == f"請看這個檔案 {expected_path} 幫我分析"


def test_embedding_not_at_dictation_point_does_not_order(model: DraftModel, fake_clock: FakeClock, tmp_path: Path):
    doc_file = tmp_path / "foo.txt"
    doc_file.write_text("foo", encoding="utf-8")

    model.recording_started()
    # User types or has text before
    p1 = DraftPiece(1, 0, 0.0, 1.0, "first", "first")
    model.add_piece(p1)

    # User deliberately moves cursor to start of buffer (not dictation point)
    model.buffer.set_cursor(0)
    model.paste_clipboard(FakeClipboardBackend(files=[str(doc_file)]))

    # Next piece arrives at dictation point (after "first")
    p2 = DraftPiece(1, 1, 2.0, 3.0, "second", "second")
    model.add_piece(p2)

    expected_path = str(doc_file.absolute())
    assert model.buffer.get_text() == f"{expected_path} first second"


# ------------------------------------------------------------- Whole-Draft Refine
def test_protect_and_restore(model: DraftModel, tmp_path: Path):
    doc1 = tmp_path / "doc1.txt"
    doc2 = tmp_path / "doc2.txt"
    doc1.write_text("1", encoding="utf-8")
    doc2.write_text("2", encoding="utf-8")

    model.buffer.insert(0, "請比較 ")
    model.paste_clipboard(FakeClipboardBackend(files=[str(doc1)]))
    model.buffer.insert(model.buffer.get_cursor(), " 與 ")
    model.paste_clipboard(FakeClipboardBackend(files=[str(doc2)]))
    model.buffer.insert(model.buffer.get_cursor(), " 的差異。")

    protected, mapping = model.protect()
    assert "[文字1]" in protected
    assert "[文字2]" in protected
    assert mapping["[文字1]"] == str(doc1.absolute())
    assert mapping["[文字2]"] == str(doc2.absolute())

    # Simulated LLM output
    llm_output = "請仔細對照 [文字1] 與 [文字2] 兩份文件的差異說明。"
    restored = model.restore(llm_output, mapping)
    assert restored is not None
    assert str(doc1.absolute()) in restored
    assert str(doc2.absolute()) in restored
    assert "[文字1]" not in restored

    # Apply refined replaces draft
    model.apply_refined(restored)
    assert model.buffer.get_text() == restored
    # Check that embedded ranges are still tagged
    ranges = model.buffer.get_tag_ranges("embed")
    assert len(ranges) == 2

    # Undo
    assert model.undo_refine() is True
    assert "請比較 " in model.buffer.get_text()
    assert model.undo_refine() is False


def test_restore_fails_when_marker_missing_or_duplicated(model: DraftModel):
    mapping = {"[文字1]": "val1", "[文字2]": "val2"}

    # Missing [文字2]
    bad1 = "LLM output with only [文字1]."
    assert model.restore(bad1, mapping) is None

    # Duplicated [文字1]
    bad2 = "LLM output [文字1] and [文字1] and [文字2]."
    assert model.restore(bad2, mapping) is None

    # Hallucinated extra marker [文字3]
    bad3 = "LLM output [文字1] and [文字2] and [文字3]."
    assert model.restore(bad3, mapping) is None


# ------------------------------------------------------------- Lifecycle & Autosave
def test_autosave_and_load(tmp_path: Path, fake_clock: FakeClock):
    cache_dir = tmp_path / "cache"
    images_dir = tmp_path / "images"

    buf1 = MemoryBuffer()
    m1 = DraftModel(buf1, images_dir=images_dir, cache_dir=cache_dir, clock=fake_clock, autosave=True)

    m1.recording_started()
    m1.add_piece(DraftPiece(1, 0, 0.0, 1.0, "raw1", "clean1", fallback=False))
    m1.add_piece(DraftPiece(1, 1, 1.0, 2.0, "raw2", "clean2", fallback=True))

    doc = tmp_path / "doc.txt"
    doc.write_text("d", encoding="utf-8")
    m1.insert_files([str(doc)])

    # Check that draft.json was written
    save_file = cache_dir / "draft.json"
    assert save_file.exists()
    data = json.loads(save_file.read_text(encoding="utf-8"))
    assert "clean1" in data["text"]
    assert len(data["embed_ranges"]) == 1
    assert data["fallback_count"] == 1

    # Load in new instance
    buf2 = MemoryBuffer()
    m2 = DraftModel(buf2, images_dir=images_dir, cache_dir=cache_dir, clock=fake_clock, autosave=True)
    m2.load()

    assert m2.buffer.get_text() == m1.buffer.get_text()
    assert len(m2.buffer.get_tag_ranges("embed")) == 1
    assert m2.fallback_count == 1
    assert m2.raw_transcript() == "raw1 raw2"


def test_finish_clears_draft_and_deletes_autosave(tmp_path: Path, fake_clock: FakeClock):
    cache_dir = tmp_path / "cache"
    images_dir = tmp_path / "images"

    buf = MemoryBuffer()
    model = DraftModel(buf, images_dir=images_dir, cache_dir=cache_dir, clock=fake_clock, autosave=True)

    model.recording_started()
    model.add_piece(DraftPiece(1, 0, 0.0, 1.0, "raw text", "clean text"))

    final_text, raw_text = model.finish()
    assert final_text == "clean text"
    assert raw_text == "raw text"

    assert model.buffer.get_text() == ""
    assert model.raw_transcript() == ""
    assert not (cache_dir / "draft.json").exists()
    assert model.fallback_count == 0


# ------------------------------------------------------------- Tk Integration (No Visible Windows)
def test_tk_text_buffer_adapter(tmp_path: Path):
    import tkinter as tk
    try:
        root = tk.Tk()
        root.withdraw()
    except Exception:
        pytest.skip("Tkinter display not available")

    try:
        from local_typeless.ui.draft_window import TkTextBuffer

        text_widget = tk.Text(root)
        buf = TkTextBuffer(text_widget)

        buf.set_text("hello world")
        assert buf.get_text() == "hello world"

        buf.insert(5, " beautiful")
        assert buf.get_text() == "hello beautiful world"

        buf.add_tag("embed", 6, 15)
        ranges = buf.get_tag_ranges("embed")
        assert len(ranges) == 1
        assert ranges[0] == (6, 15)

        buf.clear_tag("embed")
        assert len(buf.get_tag_ranges("embed")) == 0

        buf.delete(5, 15)
        assert buf.get_text() == "hello world"
    finally:
        root.destroy()


def test_draft_window_lifecycle(tmp_path: Path, fake_clock: FakeClock):
    import tkinter as tk
    try:
        root = tk.Tk()
        root.withdraw()
    except Exception:
        pytest.skip("Tkinter display not available")

    try:
        from local_typeless.types import Mode
        from local_typeless.ui.draft_window import DraftActions, DraftWindow

        saved = []
        pasted = []

        actions = DraftActions(
            toggle_record=lambda: None,
            refine=lambda text, mode, done: done("refined " + text, ""),
            save_draft=lambda final, raw: saved.append((final, raw)),
            paste_to_previous=lambda text: pasted.append(text) or True,
        )

        win = DraftWindow(
            root,
            lambda buf: DraftModel(
                buf,
                images_dir=tmp_path / "images",
                cache_dir=tmp_path / "cache",
                clock=fake_clock,
                autosave=False,
            ),
            actions=actions,
            clipboard_backend=FakeClipboardBackend(text="pasted clip"),
        )
        win.hide()

        # Add piece
        win.add_piece(DraftPiece(1, 0, 0.0, 1.0, "raw text", "cleaned text"))
        assert "cleaned text" in win.model.buffer.get_text()

        # Recording state
        win.set_recording(True)
        assert win._recording is True
        win.set_recording(False)
        assert win._recording is False

        # Paste from clipboard
        win.paste_from_clipboard()
        assert "pasted clip" in win.model.buffer.get_text()

        # Set busy
        win.set_busy(True, "busy")
        assert win._busy is True
        win.set_busy(False)
        assert win._busy is False

        # Finish
        win._on_finish()
        assert len(saved) == 1
        assert win.model.buffer.get_text() == ""

        win.close()
    finally:
        root.destroy()

