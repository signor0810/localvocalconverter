"""Faithfulness guard: the LLM may delete and reformat, but must not invent.

`check(raw, out, mode)` returns None when the output is acceptable, otherwise a short
reason. What it looks at is what a program can judge reliably in mixed Chinese/English
dictation: English terms, numbers and length. It does not catch an invented sentence that
is pure Chinese and within the length limit.
"""
from __future__ import annotations

import difflib
import re
from typing import Iterable, List, Optional, Set, Tuple

from local_typeless.cleanup import rules
from local_typeless.textblocks import ANY_MARKER_RE

_LATIN = re.compile(r"[A-Za-z][A-Za-z0-9]*")
_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_CJK_CHAR = re.compile(r"[一-鿿]")
_LIST_MARKER = re.compile(r"(?m)^[ \t>]*(?:[-*+][ \t]+)?\d{1,2}[.)、．][ \t]*")
_CALLOUT = re.compile(r"\[![A-Za-z]+\]")

_CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "兩": 2, "三": 3, "四": 4, "五": 5,
              "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000}
_CN_BIG = {"萬": 10_000, "億": 100_000_000}
_CN_RUN = re.compile("[" + "".join(_CN_DIGITS) + "".join(_CN_UNITS) + "".join(_CN_BIG) + "點]+")

# English words that come and go without changing what was said.
_FUNCTION_WORDS: Set[str] = {
    "a", "an", "the", "and", "or", "but", "so", "to", "of", "in", "on", "at", "for", "with",
    "by", "from", "is", "are", "was", "were", "be", "it", "its", "this", "that", "we", "you",
    "i", "me", "my", "our", "your", "can", "could", "would", "will", "do", "does", "just",
    "also", "then", "there", "here", "as", "like", "ok", "okay", "yeah", "yes", "well",
    "please", "no", "not", "wait", "actually", "sorry", "mean", "um", "uh", "uhm", "umm",
    "er", "erm", "hmm", "hey", "hi", "hello", "thanks", "thank", "know", "let", "lets", "s",
    "im", "if", "have", "has", "want", "need", "about", "how", "what", "which",
}

# Words that mark a self-correction; terms shortly before one may legitimately vanish.
_CORRECTION_CUES: Tuple[str, ...] = (
    "不對", "不是", "我是說", "我是指", "我的意思是", "更正", "應該是", "說錯", "講錯", "改成",
    "no wait", "no actually", "actually", "i mean", "scratch that", "sorry", "wait",
)
_RETRACT_WINDOW = 40          # characters before a cue that the speaker may have taken back

# Spoken units the LLM may write as symbols ("三十奈米" -> "30 nm", "第三季" -> "Q3").
_UNIT_SYMBOLS = {
    "奈米": ("nm",), "微米": ("um", "μm"), "毫米": ("mm",), "公分": ("cm",), "公尺": ("m",),
    "埃": ("angstrom",), "毫秒": ("ms",), "微秒": ("us",), "奈秒": ("ns",), "秒": ("s", "sec"),
    "分鐘": ("min",), "小時": ("hr", "h"), "赫茲": ("hz",), "毫焦": ("mj",), "焦耳": ("j",),
    "瓦": ("w",), "伏": ("v",), "公斤": ("kg",), "公克": ("g",), "度": ("deg",),
    "季": ("q1", "q2", "q3", "q4"),
}

# mode -> (max growth, slack, min kept share, raw size from which the minimum applies)
_LENGTH_LIMITS = {
    "clean": (1.15, 6, 0.5, 20),
    "prompt": (1.6, 30, 0.25, 40),      # a lead-in line and list wording add length
}
# mode -> (unexplained dropped terms always tolerated, plus this share of the raw terms)
_DROP_LIMITS = {"clean": (1, 0.10), "prompt": (1, 0.25)}
# mode -> English words not heard in the raw text that the output may still contain
_ADDED_LIMITS = {"prompt": 1}
# words a rewritten request may use to name its reader
_MODE_WORDS = {"prompt": {"ai", "llm", "prompt"}}
# "2千" / "3 萬" / "5k": a digit with a spoken multiplier
_MIXED_NUMBER = re.compile(r"(\d+(?:\.\d+)?)\s*([十百千萬億kKmM])")
_MULTIPLIERS = {"十": 10, "百": 100, "千": 1000, "萬": 10_000, "億": 100_000_000,
                "k": 1000, "m": 1_000_000}


def _strip(text: str) -> str:
    return ANY_MARKER_RE.sub(" ", text or "").replace("[?]", " ")


def _latin_tokens(text: str) -> List[Tuple[str, int]]:
    return [(m.group(0).lower(), m.end()) for m in _LATIN.finditer(text)]


def _squash(text: str) -> str:
    return "".join(c for c in text.lower() if c.isascii() and c.isalnum())


def _units(text: str) -> int:
    """Rough size: Chinese characters plus English words and numbers.

    A spoken number counts once however it is written ("百分之十二" vs "12%").
    """
    text, spoken_numbers = _CN_RUN.subn(" ", text)
    return (len(_CJK_CHAR.findall(text)) + len(_LATIN.findall(text))
            + len(_NUMBER.findall(text)) + spoken_numbers)


def _cn_value(run: str) -> Optional[int]:
    """Positional reading of a Chinese numeral ("三百二十" -> 320); None if it isn't one."""
    total = section = 0
    digit: Optional[int] = None
    seen_unit = False
    for ch in run:
        if ch in _CN_DIGITS:
            digit = _CN_DIGITS[ch]
        elif ch in _CN_UNITS:
            seen_unit = True
            section += (1 if digit is None else digit) * _CN_UNITS[ch]
            digit = None
        elif ch in _CN_BIG:
            seen_unit = True
            section += digit or 0
            total += (section or 1) * _CN_BIG[ch]
            section, digit = 0, None
        else:
            return None
    if not seen_unit:
        return None
    return total + section + (digit or 0)


def _spoken_numbers(raw: str) -> Tuple[Set[str], str]:
    """Numbers the speaker said: (values as strings, all digits run together)."""
    values: Set[str] = set()
    digit_stream: List[str] = []
    for m in _NUMBER.finditer(raw):
        values.add(_norm_number(m.group(0)))
        digit_stream.append(m.group(0).replace(".", ""))
    for m in _MIXED_NUMBER.finditer(raw):
        product = float(m.group(1)) * _MULTIPLIERS[m.group(2).lower()]
        values.add(_norm_number(f"{product:.4f}"))
    for m in _CN_RUN.finditer(raw):
        parts = [p for p in m.group(0).split("點") if p]
        digitwise_parts = []
        for part in parts:
            value = _cn_value(part)
            if value is not None:
                values.add(str(value))
            digitwise = "".join(str(_CN_DIGITS[c]) for c in part if c in _CN_DIGITS)
            digitwise_parts.append(digitwise)
            if digitwise:
                values.add(_norm_number(digitwise))
                values.update(digitwise)            # "兩三個" -> "2-3 個"
                digit_stream.append(digitwise)
        # "三點五" -> 3.5
        if len(parts) == 2 and all(digitwise_parts):
            whole = _cn_value(parts[0])
            head = str(whole) if whole is not None else digitwise_parts[0]
            values.add(_norm_number(f"{head}.{digitwise_parts[1]}"))
    return values, "".join(digit_stream)


def _norm_number(s: str) -> str:
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    s = s.lstrip("0")
    return s if s and not s.startswith(".") else "0" + s


def _explained(token: str, raw_squashed: str, raw_tokens: Iterable[str], allowed: Set[str]) -> bool:
    if len(token) < 2 or token in _FUNCTION_WORDS or token in allowed:
        return True
    if token in raw_squashed:                       # "i l t" -> ILT, "my sql" -> MySQL
        return True
    return bool(difflib.get_close_matches(token, list(raw_tokens), n=1, cutoff=0.8))


def glossary_terms(glossary_section: str) -> Set[str]:
    """English words of the glossary text: terms the LLM is told to correct towards."""
    return {t for t, _ in _latin_tokens(glossary_section or "")}


def check(raw: str, out: str, mode: str = "clean", allowed_terms: Iterable[str] = ()) -> Optional[str]:
    """None when `out` is a faithful rewrite of `raw`; otherwise why it is not."""
    raw_s, out_s = _strip(raw), _strip(out)
    if not raw_s.strip():
        return None
    if not out_s.strip():
        return "LLM 輸出是空的"
    if mode == "prompt" and "```" in out:
        return "LLM 輸出了程式碼區塊（像是在回答而不是整理）"

    allowed = {t.lower() for t in allowed_terms} | _MODE_WORDS.get(mode, set())
    for spoken, symbols in _UNIT_SYMBOLS.items():
        if spoken in raw_s:
            allowed.update(symbols)
    raw_tokens = _latin_tokens(raw_s)
    raw_words = [t for t, _ in raw_tokens]
    raw_squashed = _squash(raw_s)
    out_body = _CALLOUT.sub(" ", _LIST_MARKER.sub("", out_s))
    out_words = [t for t, _ in _latin_tokens(out_body)]
    out_squashed = _squash(out_body)

    added = sorted({t for t in out_words if not _explained(t, raw_squashed, raw_words, allowed)})
    if len(added) > _ADDED_LIMITS.get(mode, 0):
        return "多出原文沒有的英文詞：" + "、".join(added[:5])

    values, digit_stream = _spoken_numbers(raw_s)
    new_numbers = sorted({
        n for n in (m.group(0) for m in _NUMBER.finditer(out_body))
        if _norm_number(n) not in values and n.replace(".", "") not in digit_stream
    })
    if new_numbers:
        return "多出原文沒有的數字：" + "、".join(new_numbers[:5])

    if mode in _DROP_LIMITS:
        lower = raw_s.lower()
        cue_positions = [m.start() for cue in _CORRECTION_CUES for m in re.finditer(re.escape(cue), lower)]
        dropped: Set[str] = set()
        content = {t for t in raw_words if len(t) >= 2 and t not in _FUNCTION_WORDS}
        for token, end in raw_tokens:
            if token not in content or token in out_squashed:
                continue
            if difflib.get_close_matches(token, out_words, n=1, cutoff=0.8):
                continue
            if any(0 <= pos - end <= _RETRACT_WINDOW for pos in cue_positions):
                continue                                    # taken back by the speaker
            dropped.add(token)
        free, share = _DROP_LIMITS[mode]
        if len(dropped) > free + int(share * len(content)):
            return "少了原文講過的英文詞：" + "、".join(sorted(dropped)[:5])

    if mode in _LENGTH_LIMITS:
        growth, slack, min_share, min_from = _LENGTH_LIMITS[mode]
        # Stammers and fillers are noise, not content the output has to keep.
        n_raw, n_out = _units(rules.clean(raw_s)), _units(out_body)
        if n_out > n_raw * growth + slack:
            return f"比原文長太多（{n_raw} → {n_out}，像是在回答而不是整理）"
        if n_raw >= min_from and n_out < n_raw * min_share:
            return f"比原文短太多（{n_raw} → {n_out}，可能漏了內容）"
    return None
