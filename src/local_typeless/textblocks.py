"""Text and marker utilities for long dictations and clipboard image insertion."""
from __future__ import annotations

import logging
import re
import unicodedata
from typing import Dict, List, Sequence, Tuple, Union

from local_typeless.types import CapturedImage, Segment

logger = logging.getLogger(__name__)

MARKER_RE: re.Pattern = re.compile(r"\[圖(\d+)\]")
ANY_MARKER_RE: re.Pattern = re.compile(r"\[(圖|表|連結|文字|檔案)(\d+)\]")

_CANONICALIZE_RE = re.compile(
    r"[\[【［]\s*(圖|图|圖片|图片|表|表格|連結|链接|網址|网址|link|文字|文本|文字片段|檔案|档案|file)\s*([0-9０-９]+)\s*[\]】］]",
    re.IGNORECASE,
)
_STRONG_PUNCT = set("。！？!?；;\n")
_WEAK_PUNCT = ("，", ",", "、", " ")


def marker(index: int) -> str:
    """Return the canonical text marker for an image index, e.g. '[圖3]'."""
    return f"[圖{index}]"


def marker_for(item: CapturedImage) -> str:
    """Return the canonical text marker for a captured attachment item."""
    kind = getattr(item, "kind", "image")
    if kind == "table":
        return f"[表{item.index}]"
    elif kind == "link":
        return f"[連結{item.index}]"
    elif kind == "text":
        return f"[文字{item.index}]"
    elif kind == "file":
        return f"[檔案{item.index}]"
    return f"[圖{item.index}]"


def find_markers(text: str) -> List[str]:
    """Return all canonical markers in order of appearance."""
    return [m.group(0) for m in ANY_MARKER_RE.finditer(text)]


def strip_markers(text: str) -> str:
    """Remove every canonical marker from text."""
    return ANY_MARKER_RE.sub("", text)


def canonicalize_markers(text: str) -> str:
    """Rewrite LLM variants to the canonical form ([圖N], [表N], [連結N], [文字N], [檔案N])."""
    def _repl(m: re.Match) -> str:
        tag = m.group(1).lower()
        digits = unicodedata.normalize("NFKC", m.group(2))
        num = int(digits)
        if tag in ("圖", "图", "圖片", "图片"):
            prefix = "圖"
        elif tag in ("表", "表格"):
            prefix = "表"
        elif tag in ("連結", "链接", "網址", "网址", "link"):
            prefix = "連結"
        elif tag in ("文字", "文本", "文字片段"):
            prefix = "文字"
        elif tag in ("檔案", "档案", "file"):
            prefix = "檔案"
        else:
            prefix = "圖"
        return f"[{prefix}{num}]"

    return _CANONICALIZE_RE.sub(_repl, text)


def markers_preserved(src: str, out: str) -> bool:
    """Return True if out contains the exact same markers in the same order as src."""
    return find_markers(src) == find_markers(out)


def append_missing_markers(src: str, out: str) -> str:
    """Append any markers from src missing in out on a new final line."""
    src_markers = find_markers(src)
    out_markers = set(find_markers(out))
    missing: List[str] = []
    seen = set()
    for m in src_markers:
        if m not in out_markers and m not in seen:
            seen.add(m)
            missing.append(m)

    if not missing:
        return out

    return out.rstrip() + "\n" + "".join(missing)


def _is_strong_boundary(text: str, i: int) -> bool:
    c = text[i]
    if c in _STRONG_PUNCT:
        return True
    if c == ".":
        if i + 1 == len(text) or text[i + 1].isspace():
            return True
    return False


def _adjust_marker_cut(text: str, cut: int, start: int) -> int:
    for m in ANY_MARKER_RE.finditer(text):
        if m.start() < cut < m.end():
            if m.start() > start:
                return m.start()
            return m.end()
    return cut


def split_blocks(text: str, target_chars: int = 700, max_chars: int = 1000) -> List[str]:
    """Split long text into coherent blocks for LLM refinement."""
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    n = len(text)
    blocks: List[str] = []
    start = 0

    while start < n:
        target_pos = start + target_chars
        max_pos = min(start + max_chars, n)

        cut = -1
        # Look for the first strong boundary with cut index >= target_pos
        for i in range(max(0, target_pos - 1), max_pos):
            if _is_strong_boundary(text, i):
                cut = i + 1
                break

        if cut != -1:
            cut = _adjust_marker_cut(text, cut, start)
        else:
            if n - start <= max_chars:
                cut = n
            else:
                mid_pos = start + max_chars // 2
                weak_cut = -1
                for i in range(start + max_chars - 1, mid_pos - 1, -1):
                    if text[i] in _WEAK_PUNCT:
                        weak_cut = i + 1
                        break
                if weak_cut != -1:
                    cut = weak_cut
                else:
                    cut = start + max_chars
                cut = _adjust_marker_cut(text, cut, start)

        blocks.append(text[start:cut])
        start = cut

    if len(blocks) >= 2:
        if len(blocks[-1]) < target_chars // 4 and len(blocks[-2]) + len(blocks[-1]) <= max_chars:
            blocks[-2] = blocks[-2] + blocks[-1]
            blocks.pop()

    return blocks


def _is_ascii_non_newline(c: str) -> bool:
    return ord(c) < 128 and c not in ("\n", "\r")


def join_blocks(parts: Sequence[str]) -> str:
    """Join block strings, handling English word-spacing without breaking Chinese."""
    stripped_parts = [p.strip() for p in parts if p.strip()]
    if not stripped_parts:
        return ""

    result = stripped_parts[0]
    for right in stripped_parts[1:]:
        if _is_ascii_non_newline(result[-1]) and _is_ascii_non_newline(right[0]):
            result += " " + right
        else:
            result += right

    return result


def place_markers(segments: Sequence[Segment], images: Sequence[CapturedImage]) -> str:
    """Insert attachment markers ([圖N], [表N], [連結N], [文字N]) into transcript text based on audio timestamps."""
    tokens: List[Tuple[float, float, str]] = []
    for seg in segments:
        words = getattr(seg, "words", None)
        if words:
            for w in words:
                tokens.append((float(w.start), float(w.end), w.text))
        else:
            tokens.append((float(seg.start), float(seg.end), seg.text))

    if not images:
        return "".join(t[2] for t in tokens).strip()

    sorted_imgs = sorted(images, key=lambda img: (img.t_sec, img.index))

    if not tokens:
        return "".join(marker_for(img) for img in sorted_imgs).strip()

    midpoints = [(t[0] + t[1]) / 2.0 for t in tokens]
    img_slots: Dict[int, List[CapturedImage]] = {i: [] for i in range(len(tokens) + 1)}
    for img in sorted_imgs:
        target_idx = len(tokens)
        for i, mid in enumerate(midpoints):
            if mid > img.t_sec:
                target_idx = i
                break
        img_slots[target_idx].append(img)

    out: List[str] = []
    for i, tok in enumerate(tokens):
        for img in img_slots[i]:
            out.append(marker_for(img))
        out.append(tok[2])
    for img in img_slots[len(tokens)]:
        out.append(marker_for(img))

    return "".join(out).strip()


def split_rich(text: str, images: Sequence[CapturedImage]) -> List[Union[str, CapturedImage]]:
    """Split text into a sequence of strings and CapturedImage objects for pasting."""
    if not images:
        return [text] if text.strip() else []

    marker_map: Dict[str, CapturedImage] = {marker_for(img): img for img in images}
    raw_parts: List[Union[str, CapturedImage]] = []
    last_end = 0

    for m in ANY_MARKER_RE.finditer(text):
        token = m.group(0)
        matched = marker_map.get(token)
        if matched is not None:
            if m.start() > last_end:
                raw_parts.append(text[last_end:m.start()])
            raw_parts.append(matched)
            last_end = m.end()

    if last_end < len(text):
        raw_parts.append(text[last_end:])

    result: List[Union[str, CapturedImage]] = []
    for part in raw_parts:
        if isinstance(part, str):
            if not part.strip():
                continue
            if result and isinstance(result[-1], str):
                result[-1] = result[-1] + part
            else:
                result.append(part)
        else:
            result.append(part)

    return result
