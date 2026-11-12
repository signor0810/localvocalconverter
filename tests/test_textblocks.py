"""Tests for textblocks pure text and marker utilities (Task 1)."""
from __future__ import annotations

import pytest

from local_typeless.textblocks import (
    MARKER_RE,
    append_missing_markers,
    canonicalize_markers,
    find_markers,
    join_blocks,
    marker,
    markers_preserved,
    place_markers,
    split_blocks,
    split_rich,
    strip_markers,
)
from local_typeless.types import CapturedImage, Segment, Word


# 1. marker/find_markers/strip_markers round trip
def test_marker_find_strip_round_trip():
    assert marker(1) == "[圖1]"
    assert marker(12) == "[圖12]"

    text = "前言 [圖1] 中間 [圖12] 結尾 [?] 與 [圖]"
    assert find_markers(text) == ["[圖1]", "[圖12]"]

    stripped = strip_markers(text)
    assert "[圖1]" not in stripped
    assert "[圖12]" not in stripped
    assert "[?]" in stripped
    assert "[圖]" in stripped
    assert stripped == "前言  中間  結尾 [?] 與 [圖]"

    single = marker(3)
    assert find_markers(single) == ["[圖3]"]
    assert strip_markers(single) == ""


# 2. canonicalize_markers table
@pytest.mark.parametrize(
    "raw, expected",
    [
        ("[图1]", "[圖1]"),
        ("【圖1】", "[圖1]"),
        ("［圖１］", "[圖1]"),
        ("[圖 1]", "[圖1]"),
        ("[圖片1]", "[圖1]"),
        ("[圖01]", "[圖1]"),
        ("【图 ０2】", "[圖2]"),
        ("[?]", "[?]"),
        ("[圖]", "[圖]"),
        ("[圖a]", "[圖a]"),
    ],
)
def test_canonicalize_markers_table(raw: str, expected: str):
    assert canonicalize_markers(raw) == expected


def test_canonicalize_markers_mixed_sentence():
    src = "看這裡【图 ０2】還有［圖片 003］，但是[?]和[圖]不變。"
    expected = "看這裡[圖2]還有[圖3]，但是[?]和[圖]不變。"
    assert canonicalize_markers(src) == expected


# 3. markers_preserved
def test_markers_preserved():
    src = "說明 [圖1] 步驟 [圖2]"
    # Equal order
    assert markers_preserved(src, "改寫 [圖1] 結果 [圖2]") is True
    # Swapped order
    assert markers_preserved(src, "改寫 [圖2] 結果 [圖1]") is False
    # One missing
    assert markers_preserved(src, "改寫 [圖1] 結果") is False
    # Duplicate added
    assert markers_preserved(src, "改寫 [圖1] 結果 [圖2] [圖2]") is False


# 4. append_missing_markers
def test_append_missing_markers():
    src = "段落 A [圖1] 段落 B [圖2] 段落 C [圖3]"
    out = "整理後的文字 [圖1] 與 [圖3]"

    # Missing [圖2] appended on new line
    res = append_missing_markers(src, out)
    assert res == "整理後的文字 [圖1] 與 [圖3]\n[圖2]"

    # Multiple missing appended in source order
    out_none = "沒有任何圖片的文字"
    res_none = append_missing_markers(src, out_none)
    assert res_none == "沒有任何圖片的文字\n[圖1][圖2][圖3]"

    # Nothing missing -> identical object value
    all_present = "包含 [圖1] 以及 [圖2] 還有 [圖3]"
    res_same = append_missing_markers(src, all_present)
    assert res_same is all_present


# 5. split_blocks
def test_split_blocks_short_and_empty():
    assert split_blocks("") == []
    short = "這是一段簡短的文字。"
    assert split_blocks(short) == [short]
    exact_max = "x" * 1000
    assert split_blocks(exact_max, target_chars=700, max_chars=1000) == [exact_max]


def test_split_blocks_chinese_strong_boundaries():
    # 3000-char Chinese text with '。' every 40 chars (75 sentences)
    s40 = "零一二三四五六七八九" * 3 + "一二三四五六七八九。"
    assert len(s40) == 40 and s40.endswith("。")
    text = s40 * 75  # 3000 chars

    target = 700
    max_c = 1000
    blocks = split_blocks(text, target_chars=target, max_chars=max_c)

    # Invariants
    assert "".join(blocks) == text
    assert all(len(b) <= max_c for b in blocks)
    assert all(len(b) > 0 for b in blocks)

    # Blocks end with '。' and lengths within [target, max] except possibly the last
    for i, b in enumerate(blocks):
        assert b.endswith("。")
        if i < len(blocks) - 1:
            assert target <= len(b) <= max_c


def test_split_blocks_no_punctuation_hard_cuts():
    # 3000-char text with no punctuation -> hard cuts at max_chars
    text = "永" * 3000
    blocks = split_blocks(text, target_chars=700, max_chars=1000)
    assert "".join(blocks) == text
    assert len(blocks) == 3
    assert [len(b) for b in blocks] == [1000, 1000, 1000]


def test_split_blocks_weak_cuts():
    # Text with only '，'
    s40 = "零一二三四五六七八九" * 3 + "一二三四五六七八九，"
    assert len(s40) == 40 and s40.endswith("，")
    text = s40 * 75  # 3000 chars

    target = 700
    max_c = 1000
    blocks = split_blocks(text, target_chars=target, max_chars=max_c)
    assert "".join(blocks) == text
    assert all(len(b) <= max_c for b in blocks)
    for b in blocks[:-1]:
        assert b.endswith("，")


def test_split_blocks_marker_straddling():
    # Marker straddling the 1000 cut point
    # 998 chars + "[圖1]" (5 chars, span 998..1003) + 500 chars
    text = ("a" * 998) + "[圖1]" + ("b" * 500)
    blocks = split_blocks(text, target_chars=700, max_chars=1000)
    assert "".join(blocks) == text
    assert all(len(b) <= 1000 for b in blocks)
    # The first block must cut before [圖1], so it does not split [圖1]
    assert blocks[0] == "a" * 998
    assert blocks[1].startswith("[圖1]")


def test_split_blocks_invariants_property_style():
    # Mixed English, Chinese, markers, various punctuations
    samples = [
        "Hello world! This is a test. Another sentence here? Yes; indeed.\nNew line here.",
        ("Words and sentences. " * 80),
        ("中文句子測試！問句呢？還有分號；以及換行\n" * 30),
        ("沒有任何標點符號的超長文字測試段落" * 100),
        ("混合模式 [圖1] with English text and Chinese 標點。 " * 50),
    ]
    for sample in samples:
        for target, max_c in [(100, 150), (300, 500), (700, 1000)]:
            blocks = split_blocks(sample, target_chars=target, max_chars=max_c)
            if not sample:
                assert blocks == []
                continue
            assert "".join(blocks) == sample
            assert all(len(b) > 0 for b in blocks)
            assert all(len(b) <= max_c for b in blocks)
            # Never cut inside a marker
            for b in blocks:
                # If marker is present, it must be intact (no trailing '[' or leading ']')
                for m in MARKER_RE.finditer(b):
                    assert m.group(0).startswith("[圖") and m.group(0).endswith("]")


# 6. join_blocks
def test_join_blocks():
    assert join_blocks(["你好。", "世界"]) == "你好。世界"
    assert join_blocks(["hello.", "world"]) == "hello. world"
    assert join_blocks(["end", "中文"]) == "end中文"
    assert join_blocks(["", "  ", "hello", "  ", "world", ""]) == "hello world"
    assert join_blocks(["中文", "start"]) == "中文start"
    assert join_blocks([]) == ""


# 7. place_markers
def test_place_markers_words():
    # Words -> marker lands mid-sentence between the right words
    words = (
        Word(start=0.0, end=1.0, text="hello "),
        Word(start=1.0, end=2.0, text="world"),
    )
    seg = Segment(start=0.0, end=2.0, text="hello world", words=words)
    img = CapturedImage(index=1, t_sec=0.8, path="/fake/1.png")
    # w1 mid=0.5, w2 mid=1.5. img t_sec=0.8 -> before w2
    assert place_markers([seg], [img]) == "hello [圖1]world"


def test_place_markers_segments_only():
    seg1 = Segment(start=0.0, end=1.0, text="你好。")
    seg2 = Segment(start=2.0, end=3.0, text="世界！")
    img = CapturedImage(index=1, t_sec=1.5, path="/fake/1.png")
    # seg1 mid=0.5, seg2 mid=2.5. img t_sec=1.5 -> between seg1 and seg2
    assert place_markers([seg1, seg2], [img]) == "你好。[圖1]世界！"


def test_place_markers_boundaries():
    seg = Segment(start=1.0, end=3.0, text="測試文本")
    # Before all tokens
    img_before = CapturedImage(index=1, t_sec=0.5, path="/fake/1.png")
    assert place_markers([seg], [img_before]) == "[圖1]測試文本"

    # After all tokens
    img_after = CapturedImage(index=2, t_sec=5.0, path="/fake/2.png")
    assert place_markers([seg], [img_after]) == "測試文本[圖2]"

    # Two images at same position keep sorted order
    img_a = CapturedImage(index=2, t_sec=0.2, path="/fake/2.png")
    img_b = CapturedImage(index=1, t_sec=0.2, path="/fake/1.png")
    # sorted by (t_sec, index) -> img_b (index 1) then img_a (index 2)
    assert place_markers([seg], [img_a, img_b]) == "[圖1][圖2]測試文本"


def test_place_markers_no_tokens_or_no_images():
    img1 = CapturedImage(index=1, t_sec=0.1, path="/fake/1.png")
    img2 = CapturedImage(index=2, t_sec=0.2, path="/fake/2.png")
    # No tokens
    assert place_markers([], [img2, img1]) == "[圖1][圖2]"

    # No images
    seg = Segment(start=0.0, end=1.0, text="只有文字")
    assert place_markers([seg], []) == "只有文字"


# 8. split_rich
def test_split_rich():
    img1 = CapturedImage(index=1, t_sec=1.0, path="/fake/1.png")
    img2 = CapturedImage(index=2, t_sec=2.0, path="/fake/2.png")

    # Spec example
    assert split_rich("按照 [圖1] 的風格", [img1]) == ["按照 ", img1, " 的風格"]

    # Unknown marker stays text
    assert split_rich("按照 [圖2] 的風格", [img1]) == ["按照 [圖2] 的風格"]

    # Image first / last
    assert split_rich("[圖1] 的風格", [img1]) == [img1, " 的風格"]
    assert split_rich("按照 [圖1]", [img1]) == ["按照 ", img1]

    # Two adjacent images with space between -> no string between them
    assert split_rich("[圖1] [圖2]", [img1, img2]) == [img1, img2]

    # No images
    assert split_rich("一般純文字", []) == ["一般純文字"]
    assert split_rich("   ", []) == []
    assert split_rich("", []) == []
