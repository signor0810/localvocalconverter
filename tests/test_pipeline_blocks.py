"""Tests for Pipeline block-wise refine, marker validation, and image plumbing (Task 3)."""
from __future__ import annotations

from pathlib import Path

import pytest

from local_typeless.pipeline import (
    IMAGE_MANIFEST,
    Pipeline,
    load_image_manifest,
    save_image_manifest,
)
from local_typeless.types import (
    AppConfig,
    CapturedImage,
    LLMConnectionError,
    LLMProfileConfig,
    LLMResponseError,
    Mode,
    STTProfileConfig,
    Segment,
    TranscriptResult,
    Word,
)

PROMPTS = Path(__file__).resolve().parent.parent / "prompts"


class FakeSTT:
    def __init__(self, text="raw text", segments=None):
        self.text = text
        self.segments = segments or []
        self.calls: list = []

    def transcribe(self, audio_path, **kwargs):
        self.calls.append((audio_path, kwargs))
        return TranscriptResult(
            raw_text=self.text,
            duration_sec=2.0,
            stt_elapsed_sec=0.1,
            used_device="cpu",
            used_model="small",
            segments=self.segments,
        )


class FakeLLM:
    def __init__(self, refine_fn=None, out="refined", exc=None):
        self.refine_fn = refine_fn
        self.out = out
        self.exc = exc
        self.calls: list = []

    def refine(self, raw_text, system_prompt):
        self.calls.append((raw_text, system_prompt))
        if self.refine_fn:
            return self.refine_fn(raw_text, system_prompt)
        if self.exc:
            raise self.exc
        return self.out


class FakeGlossary:
    def build_whisper_initial_prompt(self):
        return "PROMPT"

    def build_llm_glossary_section(self):
        return "GLOSS"

    def normalize_terms(self, text):
        return text


def make_pipeline(tmp_path, stt=None, llm=None, **cfg_kw):
    cfg = AppConfig(
        stt_profiles={"cpu_fast": STTProfileConfig()},
        llm_profiles={"dev": LLMProfileConfig()},
        prompts_dir=str(PROMPTS),
        cache_dir=str(tmp_path / "cache"),
        **cfg_kw,
    )
    return Pipeline(
        cfg,
        stt=stt or FakeSTT(),
        llm=llm or FakeLLM(),
        glossary=FakeGlossary(),
        outputs=[],
    )


# 1. Long clean text (≈2500 chars with 。)
def test_long_clean_text_blocks(tmp_path):
    s40 = "零一二三四五六七八九" * 3 + "一二三四五六七八九。"
    raw_text = s40 * 63  # 2520 chars

    def refine_cb(text, system):
        return f"REFINED_{len(text)}。"

    llm = FakeLLM(refine_fn=refine_cb)
    pipeline = make_pipeline(tmp_path, llm=llm)
    res = pipeline.run_text(raw_text, Mode.CLEAN)

    assert res.total_blocks > 1
    assert len(llm.calls) == res.total_blocks
    assert res.fallback_blocks == 0
    assert not res.fallback_triggered

    # Call 0 has no "前文"
    assert "前文" not in llm.calls[0][1]

    # Call 2+ system prompt contains "前文"
    for i in range(1, len(llm.calls)):
        assert "## 前文" in llm.calls[i][1]


# 2. Middle block raises LLMResponseError
def test_middle_block_fallback(tmp_path):
    s40 = "零一二三四五六七八九" * 3 + "一二三四五六七八九。"
    raw_text = s40 * 63  # 3 blocks

    call_count = 0

    def refine_cb(text, system):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise LLMResponseError("bad json")
        return f"OK_{call_count}。"

    llm = FakeLLM(refine_fn=refine_cb)
    pipeline = make_pipeline(tmp_path, llm=llm)
    res = pipeline.run_text(raw_text, Mode.CLEAN)

    assert res.fallback_triggered is True
    assert res.fallback_blocks == 1
    assert res.error_message is not None
    assert res.error_message.startswith("block 2/")


# 3. First block raises LLMConnectionError -> LLM called exactly once; every block is raw
def test_first_block_connection_error(tmp_path):
    s40 = "零一二三四五六七八九" * 3 + "一二三四五六七八九。"
    raw_text = s40 * 63  # multiple blocks

    llm = FakeLLM(exc=LLMConnectionError("server offline"))
    pipeline = make_pipeline(tmp_path, llm=llm)
    res = pipeline.run_text(raw_text, Mode.CLEAN)

    assert len(llm.calls) == 1
    assert res.fallback_triggered is True
    assert res.fallback_blocks == res.total_blocks
    assert "server offline" in (res.error_message or "")


# 4. LLM drops a marker in a block -> block falls back and still contains marker
def test_llm_drops_marker_falls_back(tmp_path):
    raw_text = "這裡有一張圖片 [圖1] 請參考說明。"
    llm = FakeLLM(out="這裡有一張圖片，請參考說明。")  # dropped [圖1]
    pipeline = make_pipeline(tmp_path, llm=llm)
    res = pipeline.run_text(raw_text, Mode.CLEAN)

    assert res.fallback_triggered is True
    assert "[圖1]" in res.final_text


# 5. LLM returns [图1] -> accepted and canonicalised to [圖1]
def test_llm_variant_marker_canonicalised(tmp_path):
    raw_text = "這裡有一張圖片 [圖1] 請參考。"
    llm = FakeLLM(out="這裡有一張圖片 [图1] 請參考。")
    pipeline = make_pipeline(tmp_path, llm=llm)
    res = pipeline.run_text(raw_text, Mode.CLEAN)

    assert not res.fallback_triggered
    assert "[圖1]" in res.final_text
    assert "[图1]" not in res.final_text


# 6. Summary mode: LLM drops [圖1] -> final_text ends with a new line containing [圖1]
def test_summary_mode_appends_missing_marker(tmp_path):
    raw_text = "今天討論了架構設計 [圖1] 以及下一步行動。"
    llm = FakeLLM(out="- 討論了架構設計\n- 規劃了下一步行動")
    pipeline = make_pipeline(tmp_path, llm=llm)
    res = pipeline.run_text(raw_text, Mode.SUMMARY)

    assert not res.fallback_triggered
    assert res.final_text.endswith("\n[圖1]")


# 7. Markers-only raw text -> no LLM call, final_text == raw, not a fallback
def test_markers_only_no_llm_call(tmp_path):
    raw_text = "[圖1] [圖2]"
    llm = FakeLLM()
    pipeline = make_pipeline(tmp_path, llm=llm)
    res = pipeline.run_text(raw_text, Mode.CLEAN)

    assert len(llm.calls) == 0
    assert not res.fallback_triggered
    assert res.final_text == raw_text


# 8. run_file with images and fake STT returning segments with words
def test_run_file_with_images_word_timestamps(tmp_path):
    img_path = tmp_path / "img1.png"
    img_path.write_bytes(b"fake png")
    img = CapturedImage(index=1, t_sec=0.5, path=str(img_path))

    w1 = Word(start=0.0, end=0.4, text="hello ")
    w2 = Word(start=0.6, end=1.0, text="world")
    seg = Segment(start=0.0, end=1.0, text="hello world", words=(w1, w2))

    stt = FakeSTT(text="hello world", segments=[seg])
    llm = FakeLLM(out="hello [圖1]world")
    pipeline = make_pipeline(tmp_path, stt=stt, llm=llm)

    wav = tmp_path / "audio.wav"
    wav.write_bytes(b"RIFF...")
    res = pipeline.run_file(wav, Mode.CLEAN, images=[img])

    assert stt.calls[0][1].get("word_timestamps") is True
    assert res.raw_text == "hello [圖1]world"
    assert res.images == [img]

    # Without images: word_timestamps kwarg absent
    stt2 = FakeSTT(text="hello world", segments=[seg])
    pipeline2 = make_pipeline(tmp_path, stt=stt2, llm=llm)
    pipeline2.run_file(wav, Mode.CLEAN)
    assert "word_timestamps" not in stt2.calls[0][1]


# 9. Manifest round trip; empty list deletes; missing image skipped; corrupt JSON -> []
def test_image_manifest_operations(tmp_path):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True)

    img1_file = tmp_path / "img1.png"
    img1_file.write_bytes(b"1")
    img2_file = tmp_path / "img2.png"
    img2_file.write_bytes(b"2")

    img1 = CapturedImage(index=1, t_sec=1.0, path=str(img1_file))
    img2 = CapturedImage(index=2, t_sec=2.0, path=str(img2_file))

    # Save and load
    save_image_manifest(cache_dir, [img1, img2])
    loaded = load_image_manifest(cache_dir)
    assert len(loaded) == 2
    assert loaded[0] == img1
    assert loaded[1] == img2

    # Missing image file skipped
    img1_file.unlink()
    loaded_after_delete = load_image_manifest(cache_dir)
    assert len(loaded_after_delete) == 1
    assert loaded_after_delete[0] == img2

    # Empty list deletes manifest
    save_image_manifest(cache_dir, [])
    assert not (cache_dir / IMAGE_MANIFEST).exists()
    assert load_image_manifest(cache_dir) == []

    # Corrupt JSON -> []
    manifest_file = cache_dir / IMAGE_MANIFEST
    manifest_file.write_text("{corrupt json", encoding="utf-8")
    assert load_image_manifest(cache_dir) == []


# 10. retry_last picks up manifest; history contains images; retry_last(llm_only=True) restores them
def test_retry_last_images(tmp_path):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True)
    wav = cache_dir / "last_recording.wav"
    wav.write_bytes(b"RIFF...")

    img_file = tmp_path / "img.png"
    img_file.write_bytes(b"img")
    img = CapturedImage(index=1, t_sec=0.5, path=str(img_file))
    save_image_manifest(cache_dir, [img])

    stt = FakeSTT(text="說話內容", segments=[Segment(0.0, 1.0, "說話內容")])
    llm = FakeLLM(out="整理內容 [圖1]")
    pipeline = make_pipeline(tmp_path, stt=stt, llm=llm)

    res = pipeline.retry_last(Mode.CLEAN)
    assert res.images == [img]

    # History row contains images
    rows = pipeline._read_history()
    assert len(rows) == 1
    assert "images" in rows[0]
    assert len(rows[0]["images"]) == 1
    assert rows[0]["images"][0]["index"] == 1

    # retry_last(llm_only=True) restores them
    res_llm = pipeline.retry_last(Mode.CLEAN, llm_only=True)
    assert res_llm.images == [img]


# 11. Short clean text behaves exactly as before: one LLM call, system prompt has no 前文
def test_short_clean_text_unchanged(tmp_path):
    raw_text = "這是一段簡短的文字。"
    llm = FakeLLM(out="這是一段簡短的文字。")
    pipeline = make_pipeline(tmp_path, llm=llm)
    res = pipeline.run_text(raw_text, Mode.CLEAN)

    assert len(llm.calls) == 1
    assert "前文" not in llm.calls[0][1]
    assert res.total_blocks == 1
    assert res.fallback_blocks == 0
    assert not res.fallback_triggered


# 12. Pipeline record saving via RecordStore
def test_pipeline_record_saving(tmp_path):
    raw_text = "測試紀錄儲存"
    llm = FakeLLM(out="整理完成的文字")
    pipeline = make_pipeline(tmp_path, llm=llm)
    res = pipeline.run_text(raw_text, Mode.CLEAN)

    assert pipeline.records is not None
    records_dir = Path(pipeline.records.records_dir)
    md_files = list(records_dir.rglob("*.md"))
    assert len(md_files) == 1
    content = md_files[0].read_text(encoding="utf-8")
    assert "---" in content
    assert "mode: clean" in content
    assert "整理完成的文字" in content


def test_pipeline_record_saving_disabled(tmp_path):
    # record_history=False on run_text
    raw_text = "不儲存紀錄"
    llm = FakeLLM(out="文字")
    p1 = make_pipeline(tmp_path, llm=llm)
    p1.run_text(raw_text, Mode.CLEAN, record_history=False)
    records_dir = Path(p1.records.records_dir)
    assert not records_dir.exists() or list(records_dir.rglob("*.md")) == []

    # records=False on Pipeline constructor
    cfg = AppConfig(
        stt_profiles={"cpu_fast": STTProfileConfig()},
        llm_profiles={"dev": LLMProfileConfig()},
        prompts_dir=str(PROMPTS),
        cache_dir=str(tmp_path / "cache"),
    )
    p2 = Pipeline(
        cfg,
        stt=FakeSTT(),
        llm=llm,
        glossary=FakeGlossary(),
        outputs=[],
        records=False,
    )
    assert p2.records is None

