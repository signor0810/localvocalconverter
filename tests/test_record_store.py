"""Tests for RecordStore and record name formatting (Task 13)."""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

import pytest

from local_typeless.output.record_store import RecordStore, format_record_name
from local_typeless.types import CapturedImage, Mode, PipelineResult


def test_format_record_name_modes():
    t = datetime(2026, 11, 12, 14, 30, 5)
    fmt = "{date}-{time}_{mode}_{title}"

    assert format_record_name(fmt, t, Mode.CLEAN, "技術討論記錄") == "20261112-143005_整理_技術討論記錄"
    assert format_record_name(fmt, t, Mode.SUMMARY, "總結會議") == "20261112-143005_摘要_總結會議"
    assert format_record_name(fmt, t, Mode.SLIDE, "架構設計") == "20261112-143005_投影片_架構設計"
    assert format_record_name(fmt, t, Mode.TRANSFORM, "轉換測試") == "20261112-143005_轉換_轉換測試"


def test_format_record_name_cleaning():
    t = datetime(2026, 11, 12, 14, 30, 5)
    fmt = "{date}_{mode}_{title}"

    # Markers stripped, [?] stripped, whitespace collapsed
    text1 = "重點 [圖1] 以及 [圖2] 還有 [?]   多餘  空白"
    assert format_record_name(fmt, t, Mode.CLEAN, text1) == "20261112_整理_重點 以及 還有 多餘 空白"

    # Illegal Windows characters removed
    text2 = r'路徑 / 和 \ 還有 : * ? " < > | 符號'
    assert format_record_name(fmt, t, Mode.CLEAN, text2) == "20261112_整理_路徑 和 還有 符號"

    # Leading and trailing dots and spaces removed
    text3 = " ... 標題內容 ... "
    assert format_record_name(fmt, t, Mode.CLEAN, text3) == "20261112_整理_標題內容"

    # Empty title -> 無標題
    text4 = "[圖1] [?] / : * "
    assert format_record_name(fmt, t, Mode.CLEAN, text4) == "20261112_整理_無標題"

    # Truncated to 20 characters
    text5 = "這是一段超過二十個中文字的超長口述標題用來驗證截斷長度"
    assert format_record_name(fmt, t, Mode.CLEAN, text5) == "20261112_整理_這是一段超過二十個中文字的超長口述標題用"


def test_format_record_name_custom_format():
    t = datetime(2026, 11, 12, 14, 30, 5)
    fmt = "rec_{date}_{time}"
    assert format_record_name(fmt, t, Mode.CLEAN, "任意文字") == "rec_20261112_143005"


def test_record_store_save_blank_text(tmp_path: Path):
    store = RecordStore(tmp_path / "records", tmp_path / "images")
    res = PipelineResult(
        raw_text="",
        final_text="   ",
        mode=Mode.CLEAN,
        stt_profile_name="cpu",
        llm_profile_name="dev",
        stt_elapsed_sec=0.1,
        llm_elapsed_sec=0.1,
    )
    assert store.save(res) is None
    assert not (tmp_path / "records").exists()


def test_record_store_save_content_and_month_folder(tmp_path: Path):
    rec_dir = tmp_path / "records"
    img_dir = tmp_path / "images"
    fixed_time = datetime(2026, 11, 12, 14, 30, 5)
    store = RecordStore(rec_dir, img_dir, now=lambda: fixed_time)

    img1 = CapturedImage(index=1, t_sec=1.5, path="D:/data/img1.png")
    res = PipelineResult(
        raw_text="原文包含 [圖1] 標記",
        final_text="結果包含 [圖1] 以及說明",
        mode=Mode.CLEAN,
        stt_profile_name="cpu",
        llm_profile_name="dev",
        stt_elapsed_sec=0.2,
        llm_elapsed_sec=0.3,
        fallback_triggered=False,
        images=[img1],
    )

    saved_path = store.save(res)
    assert saved_path is not None
    assert saved_path.exists()
    assert saved_path.parent == rec_dir / "2026-11"
    assert saved_path.name == "20261112-143005_整理_結果包含 以及說明.md"

    content = saved_path.read_text(encoding="utf-8")
    assert "time: 2026-11-12T14:30:05\n" in content
    assert "mode: clean\n" in content
    assert "fallback: false\n" in content
    assert "images: 1\n" in content
    assert "## 結果\n\n結果包含 ![圖1](D:/data/img1.png) 以及說明\n\n" in content
    assert "## 原文\n\n原文包含 [圖1] 標記\n" in content


def test_record_store_save_name_collision(tmp_path: Path):
    rec_dir = tmp_path / "records"
    img_dir = tmp_path / "images"
    fixed_time = datetime(2026, 11, 12, 14, 30, 5)
    store = RecordStore(rec_dir, img_dir, now=lambda: fixed_time)

    res = PipelineResult(
        raw_text="同名口述",
        final_text="同名結果",
        mode=Mode.CLEAN,
        stt_profile_name="cpu",
        llm_profile_name="dev",
        stt_elapsed_sec=0.1,
        llm_elapsed_sec=0.1,
    )

    path1 = store.save(res)
    path2 = store.save(res)
    path3 = store.save(res)

    assert path1 is not None and path1.name.endswith(".md") and "_2" not in path1.name
    assert path2 is not None and path2.name.endswith("_2.md")
    assert path3 is not None and path3.name.endswith("_3.md")


def test_record_store_save_swallows_os_error(tmp_path: Path, monkeypatch):
    rec_dir = tmp_path / "records"
    img_dir = tmp_path / "images"
    store = RecordStore(rec_dir, img_dir)

    def boom(*a, **kw):
        raise OSError("disk write error")

    monkeypatch.setattr(Path, "mkdir", boom)

    res = PipelineResult(
        raw_text="測試",
        final_text="測試結果",
        mode=Mode.CLEAN,
        stt_profile_name="cpu",
        llm_profile_name="dev",
        stt_elapsed_sec=0.1,
        llm_elapsed_sec=0.1,
    )
    assert store.save(res) is None


def test_record_store_cleanup_retention(tmp_path: Path):
    rec_dir = tmp_path / "records"
    img_dir = rec_dir / "images"  # Nested inside records_dir!
    img_dir.mkdir(parents=True)

    now = datetime(2026, 11, 12, 12, 0, 0)
    store = RecordStore(rec_dir, img_dir, retention_days=90, now=lambda: now)

    # 1. Old record (> 90 days)
    old_month = rec_dir / "2026-06"
    old_month.mkdir(parents=True)
    old_file = old_month / "old_record.md"
    old_file.write_text("old", encoding="utf-8")
    old_time = now.timestamp() - (95 * 86400)
    os.utime(old_file, (old_time, old_time))

    # 2. Recent record (< 90 days)
    new_month = rec_dir / "2026-10"
    new_month.mkdir(parents=True)
    new_file = new_month / "recent_record.md"
    new_file.write_text("recent", encoding="utf-8")
    recent_time = now.timestamp() - (10 * 86400)
    os.utime(new_file, (recent_time, recent_time))

    # 3. Old image directory under images_dir
    old_img_session = img_dir / "20260601-101010-abc123"
    old_img_session.mkdir(parents=True)
    (old_img_session / "img_1.png").write_bytes(b"png")
    os.utime(old_img_session, (old_time, old_time))

    # 4. Recent image directory under images_dir
    new_img_session = img_dir / "session_recent"
    new_img_session.mkdir(parents=True)
    (new_img_session / "img_1.png").write_bytes(b"png")
    os.utime(new_img_session, (recent_time, recent_time))

    # Run cleanup
    removed = store.cleanup()
    assert removed >= 2  # old_file, old_img_session, and old_month removed

    assert not old_file.exists()
    assert not old_month.exists()  # Empty month folder removed!
    assert new_file.exists()
    assert new_month.exists()

    assert not old_img_session.exists()
    assert new_img_session.exists()
    assert img_dir.exists()  # Nested images_dir must survive!


def test_record_store_cleanup_retention_zero_is_noop(tmp_path: Path):
    rec_dir = tmp_path / "records"
    img_dir = tmp_path / "images"
    rec_dir.mkdir(parents=True)
    img_dir.mkdir(parents=True)

    old_file = rec_dir / "old.md"
    old_file.write_text("old")
    old_time = 0.0
    os.utime(old_file, (old_time, old_time))

    store = RecordStore(rec_dir, img_dir, retention_days=0)
    assert store.cleanup() == 0
    assert old_file.exists()


def test_cleanup_never_deletes_files_the_tool_did_not_create(tmp_path: Path):
    """records_dir / images_dir may be folders that also hold the user's own files."""
    rec_dir, img_dir = tmp_path / "docs", tmp_path / "pictures"
    (rec_dir / "notes").mkdir(parents=True)
    img_dir.mkdir()
    now = datetime(2026, 11, 12, 12, 0, 0)
    old = now.timestamp() - 400 * 86400
    keep = [rec_dir / "my_note.md", rec_dir / "notes" / "old.md",
            img_dir / "holiday.png", img_dir / "album" / "a.png"]
    (img_dir / "album").mkdir()
    for f in keep:
        f.write_text("mine", encoding="utf-8")
        os.utime(f, (old, old))
    os.utime(img_dir / "album", (old, old))

    store = RecordStore(rec_dir, img_dir, retention_days=90, now=lambda: now)
    assert store.cleanup() == 0
    assert all(f.exists() for f in keep)


def test_cleanup_cache_leftovers_removes_only_stale_temp_audio(tmp_path):
    import os
    import time

    from local_typeless.output.record_store import cleanup_cache_leftovers

    cache = tmp_path / "cache"
    (cache / "chunks").mkdir(parents=True)
    (cache / "teach").mkdir()
    old = time.time() - 3 * 86400
    stale = [cache / "tmp_abc.wav", cache / "chunks" / "job1_0.wav"]
    keep_old = [cache / "last_recording.wav", cache / "history.jsonl", cache / "ui.log",
                cache / "teach" / "samples.jsonl"]
    fresh = [cache / "tmp_new.wav", cache / "chunks" / "job2_0.wav"]
    for f in stale + keep_old + fresh:
        f.write_bytes(b"x")
    for f in stale + keep_old:
        os.utime(f, (old, old))

    assert cleanup_cache_leftovers(cache) == 2
    assert not any(f.exists() for f in stale)
    assert all(f.exists() for f in keep_old + fresh)
    assert cleanup_cache_leftovers(tmp_path / "missing") == 0


def test_default_cache_dir_is_under_records():
    from local_typeless.types import AppConfig

    assert AppConfig().cache_dir == "records/cache"
