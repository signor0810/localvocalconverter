"""Tests for editor protect and restore functionality."""
from __future__ import annotations

import pytest

from local_typeless.editor.protect import protect, restore


def test_protect_all_eight_patterns():
    text = (
        "1. Fenced:\n"
        "```python\n"
        "x = 1\n"
        "```\n"
        "2. Inline: `var_a + var_b`\n"
        "3. Image: ![preview](assets/2026-11/test_01.png)\n"
        '4. Quoted: "C:\\Program Files\\App\\app.exe" and \'/mnt/c/test/foo.txt\'\n'
        "5. URL: https://example.com/api/v1/test\n"
        "6. UNC: \\\\server01\\share\\doc\\notes.txt\n"
        "7. Windows: C:\\Users\\user\\Documents\\report.docx\n"
        "8. WSL: /mnt/c/Users/user/project/file.py\n"
    )

    protected, mapping = protect(text, paths=True, code=True)

    assert "```python\nx = 1\n```" not in protected
    assert "`var_a + var_b`" not in protected
    assert "![preview](assets/2026-11/test_01.png)" not in protected
    assert '"C:\\Program Files\\App\\app.exe"' not in protected
    assert "'/mnt/c/test/foo.txt'" not in protected
    assert "https://example.com/api/v1/test" not in protected
    assert "\\\\server01\\share\\doc\\notes.txt" not in protected
    assert "C:\\Users\\user\\Documents\\report.docx" not in protected
    assert "/mnt/c/Users/user/project/file.py" not in protected

    assert len(mapping) == 9
    for i in range(1, 10):
        assert f"[文字{i}]" in mapping
        assert f"[文字{i}]" in protected

    restored = restore(protected, mapping)
    assert restored == text


def test_protect_code_only():
    text = "Code: `func()` and path: C:\\test\\data.txt"
    protected, mapping = protect(text, paths=False, code=True)
    assert "[文字1]" in protected
    assert mapping["[文字1]"] == "`func()`"
    assert "C:\\test\\data.txt" in protected
    assert len(mapping) == 1
    assert restore(protected, mapping) == text


def test_protect_paths_only():
    text = "Code: `func()` and path: C:\\test\\data.txt"
    protected, mapping = protect(text, paths=True, code=False)
    assert "`func()`" in protected
    assert "[文字1]" in protected
    assert mapping["[文字1]"] == "C:\\test\\data.txt"
    assert len(mapping) == 1
    assert restore(protected, mapping) == text


def test_protect_none():
    text = "Code: `func()` and path: C:\\test\\data.txt"
    protected, mapping = protect(text, paths=False, code=False)
    assert protected == text
    assert mapping == {}
    assert restore(protected, mapping) == text


def test_chinese_punctuation_boundaries():
    text = (
        "請見網址 https://example.com/api。另外還有 "
        "D:\\workspace\\project，以及 WSL 路徑 /home/user/docs；"
        "請參考（https://example.com/help）！"
    )
    protected, mapping = protect(text, paths=True, code=True)

    assert "https://example.com/api" in mapping.values()
    assert "D:\\workspace\\project" in mapping.values()
    assert "/home/user/docs" in mapping.values()
    assert "https://example.com/help" in mapping.values()

    # Verify Chinese punctuation is not swallowed
    assert "。[文字" in protected or "[文字1]。" in protected
    assert "，" in protected
    assert "；" in protected
    assert "（" in protected
    assert "）" in protected
    assert "！" in protected

    assert restore(protected, mapping) == text


def test_restore_missing_marker_returns_none():
    mapping = {"[文字1]": "path1", "[文字2]": "path2"}
    refined = "Only [文字1] is here."
    assert restore(refined, mapping) is None


def test_restore_duplicated_marker_returns_none():
    mapping = {"[文字1]": "path1"}
    refined = "Here is [文字1] and [文字1] again."
    assert restore(refined, mapping) is None


def test_restore_hallucinated_marker_returns_none():
    mapping = {"[文字1]": "path1"}
    refined = "Here is [文字1] and [文字99]."
    assert restore(refined, mapping) is None


def test_restore_empty_mapping_with_markers_returns_none():
    mapping = {}
    refined = "LLM hallucinated a [文字1] out of nowhere."
    assert restore(refined, mapping) is None


def test_restore_single_pass_safety():
    # If [文字1] restores to a string containing [文字2], it must NOT be re-expanded
    mapping = {
        "[文字1]": "nested [文字2] value",
        "[文字2]": "original value 2",
    }
    refined = "Result: [文字1] and [文字2]"
    restored = restore(refined, mapping)
    assert restored == "Result: nested [文字2] value and original value 2"


def test_regression_colon_and_parens_boundaries():
    text1 = "請看 C:\\a\\b.png：然後再說"
    protected1, mapping1 = protect(text1, paths=True, code=True)
    assert mapping1 == {"[文字1]": "C:\\a\\b.png"}
    assert protected1 == "請看 [文字1]：然後再說"
    assert restore(protected1, mapping1) == text1

    text2 = "(見 C:\\c\\d.png)"
    protected2, mapping2 = protect(text2, paths=True, code=True)
    assert mapping2 == {"[文字1]": "C:\\c\\d.png"}
    assert protected2 == "(見 [文字1])"
    assert restore(protected2, mapping2) == text2
