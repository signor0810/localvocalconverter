"""Deterministic alias mining (no LLM): what did the recogniser hear vs what the user wanted."""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from typing import Iterable, List, Tuple

MAX_RAW_CHUNK = 12
# spoken fillers the diff often glues onto the misheard term; trimmed from both ends of the raw chunk
FILLER_TOKENS = ("就是說", "那個", "這個", "就是", "然後", "嗯", "啊", "呃", "對", "欸")
_FILLERS_LONGEST_FIRST = sorted(FILLER_TOKENS, key=len, reverse=True)
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-]*|[^\sA-Za-z0-9]")
_LATIN_PHRASE = re.compile(r"[A-Za-z][A-Za-z0-9\-]*(?: [A-Za-z0-9\-]+)*")
_HAS_LATIN = re.compile(r"[A-Za-z]")


@dataclass(frozen=True)
class AlignItem:
    kind: str       # "alias" | "term"
    key: str        # alias: what was heard;  term: the canonical term
    value: str      # alias: the correct term; term: definition ("" if unknown)


def _tokens(text: str) -> List[Tuple[str, int, int]]:
    return [(m.group(), m.start(), m.end()) for m in _TOKEN.finditer(text)]


def _is_punct(tok: str) -> bool:
    return not any(c.isalnum() for c in tok)


def trim_fillers(chunk: str) -> str:
    chunk = chunk.strip()
    changed = True
    while changed and chunk:
        changed = False
        for f in _FILLERS_LONGEST_FIRST:
            if chunk.startswith(f):
                chunk, changed = chunk[len(f):].strip(), True
                break
            if chunk.endswith(f):
                chunk, changed = chunk[:-len(f)].strip(), True
                break
    return chunk


def mine_aliases(raw_text: str, expected_text: str) -> List[Tuple[str, str]]:
    """[(heard, correct)] for replaced spans whose expected side is one Latin term."""
    rt, et = _tokens(raw_text), _tokens(expected_text)
    sm = difflib.SequenceMatcher(None, [t[0].lower() for t in rt], [t[0].lower() for t in et],
                                 autojunk=False)
    found: List[Tuple[str, str]] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag != "replace":
            continue
        raw_chunk = trim_fillers(raw_text[rt[i1][1]:rt[i2 - 1][2]])
        exp_chunk = expected_text[et[j1][1]:et[j2 - 1][2]].strip()
        if not raw_chunk or not exp_chunk or len(raw_chunk) > MAX_RAW_CHUNK:
            continue
        if all(_is_punct(t[0]) for t in rt[i1:i2]) or all(_is_punct(t[0]) for t in et[j1:j2]):
            continue                                   # punctuation / whitespace-only diff
        if not _HAS_LATIN.search(exp_chunk):
            continue
        phrases = _LATIN_PHRASE.findall(exp_chunk)
        if len(phrases) != 1 or phrases[0] != exp_chunk:
            continue
        if raw_chunk.lower() == exp_chunk.lower():
            continue                                   # case-only: normalisation handles it
        found.append((raw_chunk, exp_chunk))
    return found


def mine(raw_text: str, expected_text: str, known_terms: Iterable[str] = ()) -> List[AlignItem]:
    known = {t.lower() for t in known_terms}
    items: List[AlignItem] = []
    seen = set()

    def add(item: AlignItem) -> None:
        k = (item.kind, item.key.lower(), item.value.lower() if item.kind == "alias" else "")
        if k not in seen:
            seen.add(k)
            items.append(item)

    aliases = mine_aliases(raw_text, expected_text)
    for heard, correct in aliases:
        add(AlignItem("alias", heard, correct))
    targets = {c for _, c in aliases}
    for phrase in _LATIN_PHRASE.findall(expected_text):
        if phrase.lower() in known or len(phrase) < 2:
            continue
        # acronyms / proper nouns only (an uppercase letter), and only if the recogniser missed it
        if phrase in targets or (any(c.isupper() for c in phrase) and phrase not in raw_text):
            add(AlignItem("term", phrase, ""))
    return items
