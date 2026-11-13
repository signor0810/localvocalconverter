"""Tk-free state machine behind the review window (testable without Tk).

Background work runs in daemon threads (`threaded=True`); the Tk layer calls `poll()`.
"""
from __future__ import annotations

import copy
import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from local_typeless.review.advisor import Answer, Question, build_questions, to_items
from local_typeless.review.detect import find_candidates
from local_typeless.review.records import Record, load_records
from local_typeless.review.state import ReviewState
from local_typeless.teach import apply as teach_apply
from local_typeless.types import Mode

logger = logging.getLogger("ui.review")


@dataclass
class ReviewView:
    """Read-only view state polled by the UI layer."""

    phase: str = "idle"              # idle | scanning | ready | applying | done
    questions: List[Question] = field(default_factory=list)
    answers: Dict[int, Answer] = field(default_factory=dict)
    llm_ok: bool = True
    message: str = ""
    can_revert: bool = False
    progress_text: str = ""


class ReviewController:
    """Coordinates record scanning, candidate finding, LLM advice, and user feedback."""

    def __init__(
        self,
        cfg: Any,
        llm_factory: Callable[[], Any],
        *,
        state: Optional[ReviewState] = None,
        threaded: bool = True,
    ) -> None:
        self.cfg = cfg
        self._llm_factory = llm_factory
        self._state = state
        self._threaded = threaded
        self._lock = threading.RLock()
        self._dirty = True
        self.view = ReviewView()
        self._last_scanned_records: List[Record] = []
        self._init_revert_state()

    @property
    def state_path(self) -> Path:
        cache_dir = Path(getattr(self.cfg, "cache_dir", "records/cache"))
        return cache_dir / "review" / "state.json"

    @property
    def state(self) -> ReviewState:
        if self._state is None:
            self._state = ReviewState.load(self.state_path)
        return self._state

    def _init_revert_state(self) -> None:
        try:
            gpath = teach_apply.glossary_overlay_path(self.cfg.glossary_path)
            bak = gpath.with_name(gpath.name + ".bak")
            self.view.can_revert = bak.exists()
        except Exception:
            self.view.can_revert = False

    def can_revert(self) -> bool:
        return self.view.can_revert

    # -------------------------------------------------------------- plumbing
    def _touch(self) -> None:
        self._dirty = True

    def poll(self) -> Optional[ReviewView]:
        with self._lock:
            if not self._dirty:
                return None
            self._dirty = False
            return copy.deepcopy(self.view)

    def _bg(self, name: str, fn: Callable[[], None]) -> None:
        if self._threaded:
            threading.Thread(target=fn, daemon=True, name=name).start()
        else:
            fn()

    def _set(self, **kw: Any) -> None:
        with self._lock:
            for k, v in kw.items():
                setattr(self.view, k, v)
            self._touch()

    # ------------------------------------------------------------- actions
    def scan(self, recheck_all: bool = False) -> None:
        """Scan records and build review questions."""
        self._set(phase="scanning", message="正在掃描口述紀錄…", questions=[], answers={})

        def work() -> None:
            try:
                records_dir = getattr(self.cfg.storage, "records_dir", "records")
                all_records = load_records(records_dir)

                st = self.state
                only_sources = None
                if not recheck_all:
                    changed_records = [
                        r for r in all_records if st.is_file_changed(r.path, r.mtime)
                    ]
                    only_sources = {r.path.name for r in changed_records}
                    self._last_scanned_records = list(changed_records)
                else:
                    self._last_scanned_records = list(all_records)

                # Load glossary
                known_terms: List[str] = []
                known_aliases: Dict[str, str] = {}
                gloss_obj = None
                try:
                    from local_typeless.glossary.manager import GlossaryManager

                    gloss_path = getattr(self.cfg, "glossary_path", None)
                    if gloss_path and Path(gloss_path).exists():
                        gloss_obj = GlossaryManager.from_yaml(gloss_path)
                        for t in gloss_obj.canonical_terms:
                            if isinstance(t, dict):
                                term = t.get("term", "")
                            else:
                                term = str(t)
                            if term:
                                known_terms.append(term)
                        known_aliases = dict(gloss_obj.phonetic_aliases or {})
                except Exception as e:
                    logger.debug("Failed to load glossary in review: %s", e)

                candidates = find_candidates(
                    all_records,
                    known_terms=known_terms,
                    known_aliases=known_aliases,
                    ignored=st.ignored,
                    only_sources=only_sources,
                )

                if not candidates:
                    # Update file mtimes so we don't re-scan unchanged records
                    for r in self._last_scanned_records:
                        st.update_file(r.path, r.mtime)
                    st.save(self.state_path)

                    self._set(
                        phase="ready",
                        questions=[],
                        answers={},
                        message="沒有發現需要檢查的詞彙（所有紀錄皆正常）",
                    )
                    return

                # Query LLM
                llm = None
                try:
                    llm = self._llm_factory()
                except Exception as e:
                    logger.warning("Failed to create LLM client: %s", e)

                prompt_path = None
                prompts_dir = getattr(self.cfg, "prompts_dir", None)
                if prompts_dir:
                    p = Path(prompts_dir) / "review.md"
                    if p.is_file():
                        prompt_path = p

                questions, llm_ok = build_questions(
                    candidates,
                    glossary=gloss_obj,
                    llm=llm,
                    prompt_path=prompt_path,
                )

                # Initialize default answers: ALWAYS default to skip
                answers: Dict[int, Answer] = {}
                for idx in range(len(questions)):
                    answers[idx] = Answer(choice="skip")

                msg = ""
                if not llm_ok:
                    msg = "LLM 無法使用，僅顯示規則比對結果"

                self._set(
                    phase="ready",
                    questions=questions,
                    answers=answers,
                    llm_ok=llm_ok,
                    message=msg,
                )

            except Exception as e:
                logger.exception("Review scan failed: %s", e)
                self._set(phase="ready", message=f"檢查失敗：{e}")

        self._bg("review-scan", work)

    def set_answer(self, question_idx: int, choice: str, custom_text: str = "") -> None:
        """Update answer for a specific question."""
        with self._lock:
            self.view.answers[question_idx] = Answer(
                choice=choice.strip(),
                custom_text=custom_text.strip(),
            )
            self._touch()

    def apply(self) -> None:
        """Apply selected answers to glossary overlay and save state."""
        self._set(phase="applying", message="套用中…")

        def work() -> None:
            try:
                items = to_items(self.view.answers, self.view.questions)
                st = self.state

                # Record ignored items
                for idx, ans in self.view.answers.items():
                    if ans.choice == "not_proper_noun" and idx < len(self.view.questions):
                        q = self.view.questions[idx]
                        st.add_ignored(q.candidate.term)
                        for v in q.candidate.variants:
                            st.add_ignored(v)

                # Apply items to glossary overlay only if there are items to apply
                res = None
                if items:
                    res = teach_apply.apply(items, Mode.CLEAN, self.cfg)

                # Update scanned records in state
                for r in self._last_scanned_records:
                    st.update_file(r.path, r.mtime)
                st.save(self.state_path)

                added_terms = res.added.get("term", 0) if res else 0
                added_aliases = res.added.get("alias", 0) if res else 0
                if items:
                    msg = f"套用完成：新增 {added_terms} 個術語、{added_aliases} 個別名"
                    can_rev = True
                else:
                    msg = "套用完成：未選擇任何新增項目（維持原詞庫）"
                    can_rev = self.view.can_revert

                self._set(
                    phase="done",
                    message=msg,
                    can_revert=can_rev,
                )

            except Exception as e:
                logger.exception("Review apply failed: %s", e)
                self._set(phase="ready", message=f"套用失敗：{e}")

        self._bg("review-apply", work)

    def revert(self) -> List[Path]:
        """Revert the most recent glossary overlay changes."""
        try:
            reverted = teach_apply.revert(Mode.CLEAN, self.cfg)
            self._init_revert_state()
            self._set(message="已復原上一次套用", can_revert=self.view.can_revert)
            return reverted
        except Exception as e:
            logger.warning("Review revert failed: %s", e)
            self._set(message=f"復原失敗：{e}")
            return []
