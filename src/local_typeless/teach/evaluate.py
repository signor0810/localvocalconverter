"""Re-run samples through a fresh Pipeline (new overlays) and score against the user's answer."""
from __future__ import annotations

import difflib
from typing import Callable, List, Tuple

from local_typeless.types import Mode


def similarity(a: str, b: str) -> float:
    na, nb = "".join(a.split()), "".join(b.split())
    return difflib.SequenceMatcher(None, na, nb, autojunk=False).ratio()


def evaluate(samples, pipeline_factory: Callable[[], object]) -> List[Tuple[str, float, float, str]]:
    """-> [(sample id, before_score, after_score, new_text)].

    pipeline_factory must return a NEW pipeline (so prompt + glossary are re-read) built with
    outputs=[]; history is not written (record_history=False).
    """
    pipeline = pipeline_factory()
    try:
        pipeline.outputs = []          # never touch the clipboard while scoring
    except Exception:
        pass
    results = []
    for s in samples:
        res = pipeline.run_text(s.raw_text, Mode(s.mode), record_history=False)
        results.append((s.id, similarity(s.output_text, s.expected_text),
                        similarity(res.final_text, s.expected_text), res.final_text))
    return results


def average(scores: List[Tuple[str, float, float, str]]) -> Tuple[float, float]:
    if not scores:
        return 0.0, 0.0
    n = len(scores)
    return sum(s[1] for s in scores) / n, sum(s[2] for s in scores) / n
