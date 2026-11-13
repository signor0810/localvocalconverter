"""Rule-based candidate detector for review (no LLM required)."""
from __future__ import annotations

import difflib
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from local_typeless.review.records import Record
from local_typeless.teach import align

logger = logging.getLogger(__name__)

MAX_CANDIDATES = 40
_LATIN_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9\-]*")
_STOP_WORDS = {
    "a", "an", "the", "in", "on", "at", "to", "for", "of", "with", "by", "from", "as",
    "is", "are", "was", "were", "be", "been", "have", "has", "had", "do", "does", "did",
    "and", "or", "but", "not", "so", "we", "i", "you", "he", "she", "it", "they",
    "this", "that", "these", "those", "run", "runs", "running", "today", "tomorrow", "yesterday",
    "test", "tests", "use", "uses", "used", "using", "evaluate", "evaluates", "evaluating",
    "design", "designs", "designed", "designing",
}
_INFLECTIONS = ("s", "es", "ed", "d", "ing", "ly")

_UNSURE_MATCH_RE = re.compile(
    r"(?P<before>[A-Za-z0-9][A-Za-z0-9\-_]*(?:\s+[A-Za-z0-9\-_]+)*|[\u4e00-\u9fff]{1,10})?\s*\[\?\]\s*(?P<after>[A-Za-z0-9][A-Za-z0-9\-_]*(?:\s+[A-Za-z0-9\-_]+)*|[\u4e00-\u9fff]{1,10})?"
)


@dataclass
class Candidate:
    """Candidate term extracted from dictation records."""

    kind: str             # "unsure" | "variant" | "corrected"
    term: str             # primary term or heard phrase
    count: int = 1
    variants: List[str] = field(default_factory=list)
    contexts: List[str] = field(default_factory=list)
    sources: List[str] = field(default_factory=list)
    alias_target: str = ""


def extract_context(text: str, start: int, end: int, window: int = 30) -> str:
    """Extract snippet around a match with window characters before and after."""
    s = max(0, start - window)
    e = min(len(text), end + window)
    snippet = text[s:e].replace("\r", " ").replace("\n", " ").strip()
    prefix = "..." if s > 0 else ""
    suffix = "..." if e < len(text) else ""
    return f"{prefix}{snippet}{suffix}"


def _get_term_snippet(text: str, term: str, window: int = 30) -> str:
    """Extract snippet around first occurrence of term, or first 60 chars."""
    if not text:
        return ""
    idx = text.lower().find(term.lower())
    if idx != -1:
        return extract_context(text, idx, idx + len(term), window=window)
    snippet = text[:60].replace("\r", " ").replace("\n", " ").strip()
    return f"{snippet}..." if len(text) > 60 else snippet


def _norm_key(s: str) -> str:
    return re.sub(r"[\s\-_]+", "", s.lower())


def _is_inflection(k1: str, k2: str) -> bool:
    """Return True if one normalized key is the other plus an inflection suffix."""
    s1, s2 = (k1, k2) if len(k1) <= len(k2) else (k2, k1)
    if not s1 or s1 == s2:
        return False
    for suff in _INFLECTIONS:
        if s2 == s1 + suff:
            return True
    return False


def _differing_subphrase(p1: str, p2: str) -> Tuple[str, str]:
    """Find differing subphrase between two multi-word strings by stripping common prefix/suffix."""
    w1 = p1.split()
    w2 = p2.split()
    # strip common prefix
    i = 0
    while i < len(w1) and i < len(w2) and w1[i].lower() == w2[i].lower():
        i += 1
    # strip common suffix
    j1 = len(w1)
    j2 = len(w2)
    while j1 > i and j2 > i and w1[j1 - 1].lower() == w2[j2 - 1].lower():
        j1 -= 1
        j2 -= 1
    sub1 = " ".join(w1[i:j1])
    sub2 = " ".join(w2[i:j2])
    return sub1, sub2


def find_candidates(
    records: Sequence[Record],
    known_terms: Iterable[str] = (),
    known_aliases: Mapping[str, str] | Iterable[str] = (),
    ignored: Iterable[str] = (),
    only_sources: Optional[Set[str] | Sequence[str]] = None,
) -> List[Candidate]:
    """Find unsure, variant, and corrected candidates across records without LLM."""
    known_terms_set = {t.strip().lower() for t in known_terms if t and t.strip()}
    if isinstance(known_aliases, Mapping):
        known_aliases_set = {k.strip().lower() for k in known_aliases if k and k.strip()}
    else:
        known_aliases_set = {k.strip().lower() for k in known_aliases if k and k.strip()}
    ignored_set = {i.strip().lower() for i in ignored if i and i.strip()}

    def is_known_or_ignored(phrase: str) -> bool:
        norm = phrase.strip().lower()
        if not norm or len(norm) < 2:
            return True
        return (
            norm in known_terms_set
            or norm in known_aliases_set
            or norm in ignored_set
        )

    candidates: List[Candidate] = []

    # ------------------------------------------------------------- 1. Unsure
    unsure_dict: Dict[str, Candidate] = {}
    for r in records:
        text = r.result_text
        src = r.path.name

        for m in _UNSURE_MATCH_RE.finditer(text):
            word_before = (m.group("before") or "").strip()
            word_after = (m.group("after") or "").strip()

            # Prefer word before [?] if present; only take word after if before is missing
            word = ""
            if word_before:
                word = word_before
            elif word_after:
                word = word_after

            if not word or is_known_or_ignored(word):
                continue

            norm = word.lower()
            ctx = extract_context(text, m.start(), m.end())
            if norm not in unsure_dict:
                unsure_dict[norm] = Candidate(
                    kind="unsure",
                    term=word,
                    count=0,
                    variants=[],
                    contexts=[],
                    sources=[],
                )
            c = unsure_dict[norm]
            c.count += 1
            if word not in c.variants:
                c.variants.append(word)
            if len(c.contexts) < 3 and ctx not in c.contexts:
                c.contexts.append(ctx)
            if src not in c.sources:
                c.sources.append(src)

    candidates.extend(unsure_dict.values())

    # ------------------------------------------------------------ 2. Variant
    # Collect Latin 1-gram to 3-gram phrases from result_text ONLY
    phrase_records: Dict[str, List[str]] = {}       # phrase -> list of contexts (max 3)
    phrase_sources: Dict[str, Set[str]] = {}        # phrase -> all sources
    phrase_counts: Dict[str, int] = {}

    for r in records:
        text = r.result_text
        if not text:
            continue
        src = r.path.name

        matches = list(_LATIN_WORD_RE.finditer(text))
        n_matches = len(matches)
        for i in range(n_matches):
            for n in range(1, 4):  # 1 to 3 words
                if i + n > n_matches:
                    break
                valid_phrase = True
                for k in range(i, i + n - 1):
                    between = text[matches[k].end():matches[k + 1].start()]
                    if not between or not between.isspace():
                        valid_phrase = False
                        break
                if not valid_phrase:
                    continue

                start_idx = matches[i].start()
                end_idx = matches[i + n - 1].end()
                p = text[start_idx:end_idx].strip()
                words = p.lower().split()
                if not words:
                    continue
                if words[0] in _STOP_WORDS or words[-1] in _STOP_WORDS:
                    continue
                if is_known_or_ignored(p):
                    continue

                phrase_counts[p] = phrase_counts.get(p, 0) + 1
                ctx = extract_context(text, start_idx, end_idx)
                if p not in phrase_records:
                    phrase_records[p] = []
                    phrase_sources[p] = set()
                if len(phrase_records[p]) < 3:
                    phrase_records[p].append(ctx)
                phrase_sources[p].add(src)

    # Process clusters from 1-gram upward to prevent n-gram echoes
    all_phrases = sorted(phrase_records.keys(), key=lambda p: (len(p.split()), -phrase_counts[p], p))
    assigned: Set[str] = set()
    accepted_pairs: Set[Tuple[str, str]] = set()

    for i, p1 in enumerate(all_phrases):
        if p1 in assigned:
            continue
        cluster = [p1]
        k1 = _norm_key(p1)
        for p2 in all_phrases[i + 1:]:
            if p2 in assigned:
                continue
            k2 = _norm_key(p2)

            # Ignore inflections (e.g. mask/masks, optimize/optimized)
            if _is_inflection(k1, k2):
                continue

            is_var = False
            if k1 == k2:
                # Differ by case, whitespace, or hyphen
                is_var = True
            elif len(k1) >= 6 and len(k2) >= 6 and abs(len(k1) - len(k2)) <= 3:
                sim = difflib.SequenceMatcher(None, k1, k2).ratio()
                if sim >= 0.85:
                    is_var = True

            if is_var:
                cluster.append(p2)

        if len(cluster) >= 2:
            # Check n-gram echoes: if multi-word, does an accepted smaller cluster explain the difference?
            is_echo = False
            if len(p1.split()) > 1:
                for p2 in cluster[1:]:
                    sub1, sub2 = _differing_subphrase(p1, p2)
                    if sub1 and sub2:
                        k_sub1, k_sub2 = _norm_key(sub1), _norm_key(sub2)
                        if (
                            (k_sub1, k_sub2) in accepted_pairs
                            or (k_sub2, k_sub1) in accepted_pairs
                            or k_sub1 == k_sub2
                        ):
                            is_echo = True
                            break

            if is_echo:
                for p in cluster:
                    assigned.add(p)
                continue

            # Accept this cluster
            for p in cluster:
                assigned.add(p)

            # Record accepted pairs
            for a in cluster:
                for b in cluster:
                    if a != b:
                        accepted_pairs.add((_norm_key(a), _norm_key(b)))

            tot_count = sum(phrase_counts[p] for p in cluster)
            comb_contexts: List[str] = []
            comb_sources: Set[str] = set()
            # Round-robin so every spelling shows at least one context.
            for depth in range(3):
                for p in cluster:
                    ctxs = phrase_records[p]
                    if depth < len(ctxs) and ctxs[depth] not in comb_contexts and len(comb_contexts) < 3:
                        comb_contexts.append(ctxs[depth])
            for p in cluster:
                comb_sources.update(phrase_sources[p])

            candidates.append(
                Candidate(
                    kind="variant",
                    term=cluster[0],
                    count=tot_count,
                    variants=cluster,
                    contexts=comb_contexts,
                    sources=sorted(list(comb_sources)),
                )
            )

    # ---------------------------------------------------------- 3. Corrected
    corr_dict: Dict[Tuple[str, str], Candidate] = {}
    for r in records:
        src = r.path.name
        items = align.mine(r.raw_text, r.result_text, known_terms)
        for item in items:
            if item.kind == "alias":
                heard = item.key.strip()
                correct = item.value.strip()

                # Bounded terms: at most 4 words and 40 characters
                if (
                    len(heard.split()) > 4
                    or len(heard) > 40
                    or len(correct.split()) > 4
                    or len(correct) > 40
                ):
                    continue

                if is_known_or_ignored(heard) or is_known_or_ignored(correct):
                    continue

                k = (heard.lower(), correct.lower())
                ctx = _get_term_snippet(r.raw_text, heard) or _get_term_snippet(r.result_text, correct)
                if k not in corr_dict:
                    corr_dict[k] = Candidate(
                        kind="corrected",
                        term=heard,
                        count=0,
                        variants=[heard, correct],
                        contexts=[],
                        sources=[],
                        alias_target=correct,
                    )
                c = corr_dict[k]
                c.count += 1
                if len(c.contexts) < 3 and ctx and ctx not in c.contexts:
                    c.contexts.append(ctx)
                if src not in c.sources:
                    c.sources.append(src)

            elif item.kind == "term":
                term = item.key.strip()

                # Bounded terms: at most 4 words and 40 characters
                if len(term.split()) > 4 or len(term) > 40:
                    continue

                if is_known_or_ignored(term):
                    continue

                k = ("term", term.lower())
                ctx = _get_term_snippet(r.result_text, term) or _get_term_snippet(r.raw_text, term)
                if k not in corr_dict:
                    corr_dict[k] = Candidate(
                        kind="corrected",
                        term=term,
                        count=0,
                        variants=[term],
                        contexts=[],
                        sources=[],
                        alias_target="",
                    )
                c = corr_dict[k]
                c.count += 1
                if len(c.contexts) < 3 and ctx and ctx not in c.contexts:
                    c.contexts.append(ctx)
                if src not in c.sources:
                    c.sources.append(src)

    # Threshold: count >= 2
    for c in corr_dict.values():
        if c.count >= 2:
            candidates.append(c)

    # ------------------------------------------------ 4. Cross-kind deduplication
    # Priority: unsure > corrected-with-alias > variant > corrected-term
    def kind_priority(cand: Candidate) -> int:
        if cand.kind == "unsure":
            return 4
        if cand.kind == "corrected" and cand.alias_target:
            return 3
        if cand.kind == "variant":
            return 2
        return 1  # corrected-term

    def cand_key(cand: Candidate) -> str:
        if cand.alias_target:
            return _norm_key(cand.alias_target)
        return _norm_key(cand.term)

    grouped_cands: Dict[str, List[Candidate]] = {}
    for c in candidates:
        key = cand_key(c)
        if key not in grouped_cands:
            grouped_cands[key] = []
        grouped_cands[key].append(c)

    deduped_candidates: List[Candidate] = []
    for group in grouped_cands.values():
        group.sort(key=lambda x: -kind_priority(x))
        winner = group[0]
        # Merge count (max), contexts (max 3), sources, variants
        for other in group[1:]:
            winner.count = max(winner.count, other.count)
            for ctx in other.contexts:
                if ctx not in winner.contexts and len(winner.contexts) < 3:
                    winner.contexts.append(ctx)
            for src in other.sources:
                if src not in winner.sources:
                    winner.sources.append(src)
            for v in other.variants:
                if v not in winner.variants:
                    winner.variants.append(v)
        deduped_candidates.append(winner)

    # ------------------------------------------- 5. Filter by only_sources (if given)
    if only_sources is not None:
        src_set = set()
        for s in only_sources:
            src_set.add(str(s))
            src_set.add(Path(s).name)
            try:
                src_set.add(str(Path(s).resolve()))
            except Exception:
                pass
        deduped_candidates = [
            c for c in deduped_candidates
            if any(s in src_set or Path(s).name in src_set for s in c.sources)
        ]

    # Sort by count desc, then kind, then term
    deduped_candidates.sort(key=lambda c: (-c.count, c.kind, c.term.lower()))
    return deduped_candidates[:MAX_CANDIDATES]
