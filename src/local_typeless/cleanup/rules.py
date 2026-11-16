"""Cleanup that needs no model and never changes meaning.

Used as the fallback text when the LLM is unreachable or its output fails the guard.
Only removes what is certainly noise: standalone fillers and stammered repeats.
Self-corrections ("三點，不對，四點") are left alone: without word boundaries a rule
cannot know how far back the speaker took their words, and a wrong guess loses content.
"""
from __future__ import annotations

import re
from typing import Sequence

from local_typeless.types import DEFAULT_FILLERS

_CJK = r"一-鿿"
# Spoken numbers repeat legitimately ("三三三" is 333).
_NUMERALS = "零〇一二兩三四五六七八九十百千萬億"
_SOFT_PUNCT = "，,、"

# "我我我" -> "我": one character needs three copies ("看看", "謝謝" are real words).
_SINGLE_REPEAT = re.compile(rf"([{_CJK}])\1{{2,}}")
# "這個這個", "我覺得 我覺得" -> one copy: units of 2-4 characters.
_UNIT_REPEAT = re.compile(rf"([{_CJK}]{{2,4}}?)(?:[ \t]*\1)+")
# "the the" -> "the"
_WORD_REPEAT = re.compile(r"\b([A-Za-z]+)(?:\s+\1\b)+", re.IGNORECASE)


def _filler_pattern(fillers: Sequence[str]) -> "re.Pattern[str] | None":
    cjk = [re.escape(f) for f in fillers if f and not f.isascii()]
    latin = [re.escape(f) for f in fillers if f and f.isascii()]
    parts = []
    if cjk:
        parts.append("(?:" + "|".join(sorted(cjk, key=len, reverse=True)) + ")")
    if latin:
        parts.append(
            "(?<![A-Za-z0-9])(?:" + "|".join(sorted(latin, key=len, reverse=True)) + ")(?![A-Za-z0-9])"
        )
    if not parts:
        return None
    one = "(?:" + "|".join(parts) + ")"
    # A filler, anything it drags along (more fillers, the comma it carried, spaces).
    return re.compile(rf"(?:{one}[{_SOFT_PUNCT}\s…]*)+", re.IGNORECASE)


def _has_numeral(s: str) -> bool:
    return any(c in _NUMERALS for c in s)


def _collapse_repeats(text: str) -> str:
    text = _SINGLE_REPEAT.sub(lambda m: m.group(0) if _has_numeral(m.group(1)) else m.group(1), text)
    text = _UNIT_REPEAT.sub(lambda m: m.group(0) if _has_numeral(m.group(1)) else m.group(1), text)
    return _WORD_REPEAT.sub(lambda m: m.group(1), text)


def _clean_line(line: str, pattern: "re.Pattern[str] | None") -> str:
    if pattern is not None:
        def drop(m: "re.Match[str]") -> str:
            # Between two Latin words a removed filler must still leave a space.
            before = line[m.start() - 1] if m.start() > 0 else ""
            after = line[m.end()] if m.end() < len(line) else ""
            return " " if before.isascii() and before.isalnum() and after.isascii() and after.isalnum() else ""

        line = pattern.sub(drop, line)
    line = _collapse_repeats(line)
    # Removed fillers can leave "，，", "，。" or a leading comma behind.
    line = re.sub(rf"[{_SOFT_PUNCT}]\s*([。！？!?])", r"\1", line)
    line = re.sub(rf"([{_SOFT_PUNCT}])(?:\s*[{_SOFT_PUNCT}])+", r"\1", line)
    line = re.sub(rf"。\s*[。{_SOFT_PUNCT}]+", "。", line)
    line = re.sub(r"[ \t]{2,}", " ", line)
    return line.lstrip(_SOFT_PUNCT + " \t").rstrip()


def clean(text: str, fillers: Sequence[str] = DEFAULT_FILLERS) -> str:
    """Remove standalone fillers and stammered repeats; keep everything else as spoken."""
    if not text:
        return ""
    pattern = _filler_pattern(fillers)
    return "\n".join(_clean_line(line, pattern) for line in text.split("\n")).strip()
