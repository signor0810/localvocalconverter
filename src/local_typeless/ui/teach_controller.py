"""Tk-free state machine behind the hidden teach window (testable without Tk).

Background work runs in daemon threads (`threaded=True`); the Tk layer calls `poll()`.
"""
from __future__ import annotations

import copy
import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, List, Optional, Tuple

from local_typeless.teach import advisor, apply as teach_apply, evaluate as teach_eval
from local_typeless.teach.store import TeachSample, TeachStore
from local_typeless.types import Mode

logger = logging.getLogger("ui.teach")

SUGGESTED_SAMPLES = "建議 4–5 筆"


@dataclass
class TeachView:
    phase: str = "idle"                 # idle | analyzing | applying
    mode: str = Mode.SUMMARY.value
    raw_text: str = ""
    output_text: str = ""
    samples: List[TeachSample] = field(default_factory=list)
    items: List[advisor.ProposalItem] = field(default_factory=list)
    scores: List[Tuple[str, float, float, str]] = field(default_factory=list)
    avg_before: Optional[float] = None
    avg_after: Optional[float] = None
    message: str = ""
    can_revert: bool = False

    @property
    def count_label(self) -> str:
        return f"已收集 {len([s for s in self.samples if s.mode == self.mode])} 筆（{SUGGESTED_SAMPLES}）"


class TeachController:
    def __init__(
        self,
        cfg: Any,
        llm_factory: Callable[[], Any],
        pipeline_factory: Callable[[], Any],
        *,
        store: Optional[TeachStore] = None,
        threaded: bool = True,
    ) -> None:
        self.cfg = cfg
        self._llm_factory = llm_factory
        self._pipeline_factory = pipeline_factory
        self.store = store or TeachStore(cfg.cache_dir)
        self._threaded = threaded
        self._lock = threading.RLock()
        self._dirty = True
        self.view = TeachView(samples=self.store.list())

    # -------------------------------------------------------------- plumbing
    def _touch(self) -> None:
        self._dirty = True

    def poll(self) -> Optional[TeachView]:
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

    def _mode_samples(self) -> List[TeachSample]:
        return self.store.list(self.view.mode)

    def _refresh_samples(self) -> None:
        self._set(samples=self.store.list())

    # ------------------------------------------------------------ selection
    def set_selection(self, raw_text: str, output_text: str, mode: Optional[Mode] = None) -> None:
        """The panel's currently selected history item (read-only context for the next sample)."""
        with self._lock:
            self.view.raw_text, self.view.output_text = raw_text, output_text
            if mode is not None:
                self.view.mode = mode.value if isinstance(mode, Mode) else str(mode)
            self._touch()

    def set_mode(self, mode: str) -> None:
        self._set(mode=mode, items=[], scores=[], avg_before=None, avg_after=None)

    # -------------------------------------------------------------- samples
    def add_sample(self, expected_text: str) -> bool:
        v = self.view
        expected = (expected_text or "").strip()
        if not v.raw_text.strip():
            self._set(message="請先在主面板選一筆有原文的歷史紀錄。")
            return False
        if not expected:
            self._set(message="請貼上正確答案。")
            return False
        self.store.add(v.mode, v.raw_text, v.output_text, expected)
        self._refresh_samples()
        self._set(message="已加入樣本。")
        return True

    def delete_sample(self, sample_id: str) -> None:
        self.store.delete(sample_id)
        self._refresh_samples()

    # -------------------------------------------------------------- analyze
    def can_analyze(self) -> bool:
        return self.view.phase == "idle" and bool(self._mode_samples())

    def analyze(self) -> None:
        if not self.can_analyze():
            return
        self._set(phase="analyzing", message="分析中…（呼叫內部 LLM）", items=[],
                  scores=[], avg_before=None, avg_after=None)
        self._bg("teach-analyze", self._do_analyze)

    def _do_analyze(self) -> None:
        mode = self.view.mode
        try:
            from local_typeless.config.loader import load_prompt
            from local_typeless.glossary.manager import GlossaryManager

            cfg = self.cfg
            try:
                prompt = load_prompt(Mode(mode), Path(cfg.prompts_dir), "")
            except OSError:
                prompt = ""
            try:
                glossary = GlossaryManager.from_yaml(Path(cfg.glossary_path))
            except OSError:
                glossary = None
            rules = teach_apply.read_rules(teach_apply.prompt_overlay_path(mode, cfg.prompts_dir))
            proposal = advisor.propose(self._mode_samples(), mode, prompt, glossary,
                                       self._llm_factory(), existing_rules=rules)
            msg = f"提出 {len(proposal.items)} 項建議。" if proposal.items else "沒有找到需要調整的地方。"
            self._set(items=proposal.items, message=msg)
        except advisor.TeachError as e:
            self._set(message=str(e))
        except Exception as e:      # never leave the window stuck in "analyzing"
            logger.exception("teach analyze failed")
            self._set(message=f"分析失敗：{type(e).__name__}: {e}")
        finally:
            self._set(phase="idle")

    # ------------------------------------------------------ proposal editing
    def set_item_selected(self, index: int, selected: bool) -> None:
        with self._lock:
            if 0 <= index < len(self.view.items):
                self.view.items[index].selected = selected
                self._touch()

    def edit_item(self, index: int, key: Optional[str] = None, value: Optional[str] = None) -> None:
        with self._lock:
            if 0 <= index < len(self.view.items):
                it = self.view.items[index]
                if key is not None:
                    it.key = key.strip()
                if value is not None:
                    it.value = value.strip()
                self._touch()

    # ---------------------------------------------------------------- apply
    def can_apply(self) -> bool:
        return self.view.phase == "idle" and any(i.selected and i.key.strip() for i in self.view.items)

    def apply_selected(self) -> None:
        if not self.can_apply():
            return
        self._set(phase="applying", message="套用中…")
        self._bg("teach-apply", self._do_apply)

    def _do_apply(self) -> None:
        try:
            chosen = [i for i in self.view.items if i.selected and i.key.strip()]
            res = teach_apply.apply(chosen, self.view.mode, self.cfg)
            self._set(can_revert=res.glossary_changed or res.prompt_changed,
                      message=f"已套用（新詞 {res.added['term']}、對照 {res.added['alias']}、規則 {res.added['rule']}），評分中…")
            scores = teach_eval.evaluate(self._mode_samples(), self._pipeline_factory)
            b, a = teach_eval.average(scores)
            self._set(scores=scores, avg_before=b, avg_after=a,
                      message=f"平均相似度 {b:.0%} → {a:.0%}")
        except Exception as e:
            logger.exception("teach apply/evaluate failed")
            self._set(message=f"套用或評分失敗：{type(e).__name__}: {e}")
        finally:
            self._set(phase="idle")

    def revert(self) -> None:
        if self.view.phase != "idle":
            return
        try:
            restored = teach_apply.revert(self.view.mode, self.cfg)
        except Exception as e:
            self._set(message=f"復原失敗：{type(e).__name__}: {e}")
            return
        self._set(can_revert=False, scores=[], avg_before=None, avg_after=None,
                  message="已復原上一次套用。" if restored else "沒有可復原的項目。")
