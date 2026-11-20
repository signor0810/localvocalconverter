"""Pre-LLM protection and restoration for paths, URLs and code blocks."""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

# Chinese and fullwidth punctuation marks that should not be swallowed at the end of paths/URLs
CHINESE_PUNCT = "，。；、！？（）【】「」『』《》〈〉：～…—"


def protect(
    text: str,
    *,
    paths: bool = True,
    code: bool = True,
) -> Tuple[str, Dict[str, str]]:
    """Single-pass protect code blocks, URLs, and paths before sending to LLM."""
    patterns: List[str] = []
    if code:
        # (1) fenced code blocks
        patterns.append(r"```[\s\S]*?```")
        # (2) inline code
        patterns.append(r"`[^`\r\n]+`")
    if paths:
        # (3) Markdown images: ![alt](target)
        patterns.append(r"!\[[^\]\r\n]*\]\([^)\r\n]+\)")
        # (4) Quoted paths: ("C:\...", "\\server\...", "/mnt/...")
        patterns.append(
            r'(?:"(?:[A-Za-z]:[\\/]|\\\\|/(?:mnt|home|Users|tmp|var|etc|opt|usr|srv|data)/)[^"\r\n]+"'
            r'|\'(?:[A-Za-z]:[\\/]|\\\\|/(?:mnt|home|Users|tmp|var|etc|opt|usr|srv|data)/)[^\'\r\n]+\')'
        )
        # (5) URLs: https?://...
        patterns.append(
            rf"https?://[^\s\"'<>|{CHINESE_PUNCT}]+(?<![.,;:!?)\]])"
        )
        # (6) UNC paths: \\server\share\...
        patterns.append(
            rf"\\\\[a-zA-Z0-9_.\-]+(?:\\[^\s\"'<>|{CHINESE_PUNCT}]+)+(?<![.,;:!?)\]])"
        )
        # (7) Windows absolute paths: [A-Za-z]:[\\/]...
        patterns.append(
            rf"[A-Za-z]:[\\/][^\s\"'<>|{CHINESE_PUNCT}]*(?<![.,;:!?)\]])"
        )
        # (8) WSL/POSIX absolute paths: /(?:mnt|home|Users|tmp|var|etc|opt|usr|srv|data)/...
        patterns.append(
            rf"/(?:mnt|home|Users|tmp|var|etc|opt|usr|srv|data)/[^\s\"'<>|{CHINESE_PUNCT}]*(?<![.,;:!?)\]])"
        )

    if not patterns:
        return text, {}

    combined = re.compile("|".join(f"(?:{p})" for p in patterns))
    pieces: List[str] = []
    mapping: Dict[str, str] = {}
    last = 0
    idx = 1
    for m in combined.finditer(text):
        matched_str = m.group(0)
        if not matched_str:
            continue
        pieces.append(text[last:m.start()])
        marker = f"[文字{idx}]"
        mapping[marker] = matched_str
        pieces.append(marker)
        last = m.end()
        idx += 1
    pieces.append(text[last:])
    return "".join(pieces), mapping


def restore(refined: str, mapping: Dict[str, str]) -> Optional[str]:
    """Restore [文字N] markers in refined text. Return None if markers are lost or corrupted."""
    if not mapping:
        if re.search(r"\[文字\d+\]", refined):
            return None
        return refined

    # Each mapped marker must appear exactly once in refined text
    for marker in mapping:
        if refined.count(marker) != 1:
            return None

    # No extra/hallucinated markers allowed
    all_markers = re.findall(r"\[文字\d+\]", refined)
    if len(all_markers) != len(mapping):
        return None

    # Single-pass regex replacement using keys sorted by length descending
    pattern = re.compile("|".join(re.escape(k) for k in sorted(mapping.keys(), key=len, reverse=True)))
    pieces: List[str] = []
    last = 0
    for m in pattern.finditer(refined):
        pieces.append(refined[last:m.start()])
        pieces.append(mapping[m.group(0)])
        last = m.end()
    pieces.append(refined[last:])
    return "".join(pieces)
