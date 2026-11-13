"""Tests for Feature B: Review dictation records -> Personal dictionary."""
from __future__ import annotations

import json
from pathlib import Path
import pytest

from local_typeless.review.advisor import Answer, Question, build_questions, to_items
from local_typeless.review.detect import Candidate, find_candidates
from local_typeless.review.records import Record, load_records
from local_typeless.review.state import ReviewState
from local_typeless.teach import apply as teach_apply
from local_typeless.types import AppConfig, LLMConnectionError, Mode
from local_typeless.ui.review_controller import ReviewController, ReviewView


# ----------------------------------------------------------------- records
def test_load_records_parses_valid_and_skips_bad(tmp_path):
    records_dir = tmp_path / "records"
    month_dir = records_dir / "2026-11"
    month_dir.mkdir(parents=True)

    # Valid record
    rec1 = month_dir / "20261112-120000_整理_ILT優化.md"
    rec1.write_text(
        "---\ntime: 2026-11-12T12:00:00\nmode: clean\nfallback: false\nimages: 0\n---\n\n"
        "## 結果\n\n我們在ILT計算中使用curvilinear mask以提高解析度。\n\n"
        "## 原文\n\n我們在ILT計算中使用curve linear mask以提高解析度。\n",
        encoding="utf-8",
    )

    # Valid record with another mode
    rec2 = month_dir / "20261112-130000_摘要_SMO模擬.md"
    rec2.write_text(
        "---\ntime: 2026-11-12T13:00:00\nmode: summary\n---\n\n"
        "## 結果\n\nSMO與OPC協同優化。\n\n"
        "## 原文\n\nSMO與歐匹西協同優化。\n",
        encoding="utf-8",
    )

    # Corrupt records:
    # 1. Missing sections
    bad1 = month_dir / "bad1.md"
    bad1.write_text("Just some random notes without sections\n", encoding="utf-8")

    # 2. Empty file
    bad2 = month_dir / "bad2.md"
    bad2.write_text("", encoding="utf-8")

    # 3. Non-md file in month dir
    (month_dir / "notes.txt").write_text("notes", encoding="utf-8")

    # 4. Md file outside month dirs
    (records_dir / "other.md").write_text(
        "## 結果\n\nOut\n\n## 原文\n\nOut\n", encoding="utf-8"
    )

    records = load_records(records_dir)
    assert len(records) == 2
    paths = {r.path.name for r in records}
    assert paths == {"20261112-120000_整理_ILT優化.md", "20261112-130000_摘要_SMO模擬.md"}

    r1 = next(r for r in records if r.path.name == "20261112-120000_整理_ILT優化.md")
    assert r1.mode == "clean"
    assert "curvilinear mask" in r1.result_text
    assert "curve linear mask" in r1.raw_text
    assert r1.mtime > 0


# ----------------------------------------------------------------- state
def test_state_roundtrip_incremental_and_recovery(tmp_path):
    state_file = tmp_path / "review" / "state.json"

    # 1. Non-existent -> empty state
    s = ReviewState.load(state_file)
    assert s.files == {}
    assert s.ignored == set()

    # 2. Add files and ignored terms
    file1 = tmp_path / "rec1.md"
    file1.write_text("dummy", encoding="utf-8")
    mtime1 = file1.stat().st_mtime

    assert s.is_file_changed(file1, mtime1)
    s.update_file(file1, mtime1)
    assert not s.is_file_changed(file1, mtime1)
    assert s.is_file_changed(file1, mtime1 + 10.0)

    s.add_ignored("CommonWord")
    assert s.is_ignored("commonword")
    assert s.is_ignored("COMMONWORD")
    assert not s.is_ignored("ILT")

    # 3. Save and reload
    s.save(state_file)
    s2 = ReviewState.load(state_file)
    assert not s2.is_file_changed(file1, mtime1)
    assert s2.is_ignored("commonword")

    # 4. Reset files
    s2.reset_files()
    assert s2.is_file_changed(file1, mtime1)
    assert s2.is_ignored("commonword")  # ignored preserved

    # 5. Corrupt file -> returns empty state gracefully
    state_file.write_text("{broken json", encoding="utf-8")
    s_corrupt = ReviewState.load(state_file)
    assert s_corrupt.files == {}
    assert s_corrupt.ignored == set()


# ----------------------------------------------------------------- detect
def test_detect_unsure_candidates():
    records = [
        Record(
            path=Path("2026-11/r1.md"),
            mtime=1.0,
            raw_text="ILT模擬",
            result_text="這是 ILT[?] 模擬與 SMO[?] 測試。",
        ),
        Record(
            path=Path("2026-11/r2.md"),
            mtime=2.0,
            raw_text="OPC優化",
            result_text="還有 [?]OPC 與 SRAF[?] 測試。",
        ),
    ]
    # SRAF is already known, OPC is ignored
    candidates = find_candidates(
        records,
        known_terms=["SRAF"],
        known_aliases={},
        ignored={"OPC"},
    )
    terms = {c.term for c in candidates if c.kind == "unsure"}
    assert "ILT" in terms
    assert "SMO" in terms
    assert "SRAF" not in terms
    assert "OPC" not in terms


def test_detect_variant_candidates():
    records = [
        Record(
            path=Path("2026-11/r1.md"),
            mtime=1.0,
            raw_text="run curvilinear mask",
            result_text="We run curvilinear mask simulation today.",
        ),
        Record(
            path=Path("2026-11/r2.md"),
            mtime=2.0,
            raw_text="run curve linear mask",
            result_text="We run curve linear mask simulation tomorrow.",
        ),
    ]
    candidates = find_candidates(
        records,
        known_terms=[],
        known_aliases={},
        ignored=[],
    )
    variants = [c for c in candidates if c.kind == "variant"]
    assert len(variants) >= 1
    v = variants[0]
    var_set = {x.lower() for x in v.variants}
    assert "curvilinear" in var_set or "curvilinear mask" in var_set
    assert len(v.contexts) >= 1
    assert any("r1.md" in src or "r2.md" in src for src in v.sources)


def test_detect_corrected_requires_threshold_and_skips_known():
    records = [
        # Record 1: 歐匹西 -> OPC
        Record(
            path=Path("2026-11/r1.md"),
            mtime=1.0,
            raw_text="我們用歐匹西模擬",
            result_text="我們用OPC模擬",
        ),
        # Record 2: 歐匹西 -> OPC (count = 2 -> should qualify)
        Record(
            path=Path("2026-11/r2.md"),
            mtime=2.0,
            raw_text="繼續做歐匹西優化",
            result_text="繼續做OPC優化",
        ),
        # Record 3: 艾爾特 -> ILT (count = 1 -> should NOT qualify because < 2)
        Record(
            path=Path("2026-11/r3.md"),
            mtime=3.0,
            raw_text="執行艾爾特運算",
            result_text="執行ILT運算",
        ),
    ]
    candidates = find_candidates(
        records,
        known_terms=[],
        known_aliases={},
        ignored=[],
    )
    corrected = [c for c in candidates if c.kind == "corrected"]
    corr_terms = {c.term for c in corrected}
    # OPC / 歐匹西 should qualify
    assert any("OPC" in c.term or "歐匹西" in c.term or c.alias_target == "OPC" for c in corrected)
    # ILT / 艾爾特 only appeared once, must not appear
    assert not any("ILT" in c.term or "艾爾特" in c.term or c.alias_target == "ILT" for c in corrected)

    # If OPC is in known_terms, it is skipped
    candidates_known = find_candidates(
        records,
        known_terms=["OPC"],
        known_aliases={},
        ignored=[],
    )
    corr_known = [c for c in candidates_known if c.kind == "corrected"]
    assert not any("OPC" in c.term or c.alias_target == "OPC" for c in corr_known)


# -------------------------------------------------------- fixes for round 2
def test_variant_result_text_only():
    records = [
        Record(
            path=Path("2026-11/r1.md"),
            mtime=1.0,
            raw_text="we test ilt today",
            result_text="We test ILT today.",
        ),
        Record(
            path=Path("2026-11/r2.md"),
            mtime=2.0,
            raw_text="we run smo today",
            result_text="We run SMO today.",
        ),
    ]
    candidates = find_candidates(records)
    variants = [c for c in candidates if c.kind == "variant"]
    # Case difference between raw and result (ilt vs ILT) must not produce a variant candidate
    assert not any("ilt" in [x.lower() for x in c.variants] for c in variants)


def test_variant_no_ngram_echoes():
    records = [
        Record(
            path=Path("2026-11/r1.md"),
            mtime=1.0,
            raw_text="test curvilinear",
            result_text="We evaluate curvilinear mask and ILT.",
        ),
        Record(
            path=Path("2026-11/r2.md"),
            mtime=2.0,
            raw_text="test curve linear",
            result_text="We evaluate curve linear mask and ILT.",
        ),
        Record(
            path=Path("2026-11/r3.md"),
            mtime=3.0,
            raw_text="test high na",
            result_text="High NA lithography test.",
        ),
        Record(
            path=Path("2026-11/r4.md"),
            mtime=4.0,
            raw_text="test high-na",
            result_text="high-na lithography test.",
        ),
    ]
    candidates = find_candidates(records)
    var_candidates = [c for c in candidates if c.kind == "variant"]

    # Genuine multi-word / 1-gram variants should exist
    assert any("curvilinear" in c.variants or "curvilinear mask" in c.variants for c in var_candidates)
    assert any("High NA" in c.variants or "high-na" in c.variants for c in var_candidates)

    # N-gram echoes like "mask and ILT" or "evaluate curvilinear" must not exist
    for c in var_candidates:
        for v in c.variants:
            assert "mask and ILT" not in v
            assert "evaluate curvilinear" not in v


def test_variant_ignores_inflections():
    records = [
        Record(
            path=Path("2026-11/r1.md"),
            mtime=1.0,
            raw_text="mask pattern optimize",
            result_text="We design mask pattern and optimize OPC.",
        ),
        Record(
            path=Path("2026-11/r2.md"),
            mtime=2.0,
            raw_text="masks patterns optimized",
            result_text="We design masks patterns and optimized OPC.",
        ),
    ]
    candidates = find_candidates(records)
    variants = [c for c in candidates if c.kind == "variant"]
    # mask/masks, pattern/patterns, optimize/optimized must be ignored
    for c in variants:
        v_low = {x.lower() for x in c.variants}
        assert not ({"mask", "masks"}.issubset(v_low))
        assert not ({"pattern", "patterns"}.issubset(v_low))
        assert not ({"optimize", "optimized"}.issubset(v_low))


def test_corrected_bounded_terms():
    records = [
        Record(
            path=Path("2026-11/r1.md"),
            mtime=1.0,
            raw_text="這是一段非常非常長超過四十個字元而且超過四個單字的完整句子描述內容",
            result_text="This is a very very long sentence that exceeds four words and forty characters",
        ),
        Record(
            path=Path("2026-11/r2.md"),
            mtime=2.0,
            raw_text="這是一段非常非常長超過四十個字元而且超過四個單字的完整句子描述內容",
            result_text="This is a very very long sentence that exceeds four words and forty characters",
        ),
        Record(
            path=Path("2026-11/r3.md"),
            mtime=3.0,
            raw_text="用歐匹西模擬",
            result_text="用OPC模擬",
        ),
        Record(
            path=Path("2026-11/r4.md"),
            mtime=4.0,
            raw_text="用歐匹西模擬",
            result_text="用OPC模擬",
        ),
    ]
    candidates = find_candidates(records)
    corrected = [c for c in candidates if c.kind == "corrected"]

    # Long sentence must not be a candidate
    assert not any(len(c.term) > 40 or len(c.term.split()) > 4 for c in corrected)
    # Short term OPC/歐匹西 must be accepted
    assert any("OPC" in c.variants or "歐匹西" in c.variants for c in corrected)


def test_corrected_context_is_snippet():
    long_prefix = "前文" * 50  # 100 chars
    long_suffix = "後文" * 50  # 100 chars
    records = [
        Record(
            path=Path("2026-11/r1.md"),
            mtime=1.0,
            raw_text=f"{long_prefix}使用歐匹西進行模擬{long_suffix}",
            result_text=f"{long_prefix}使用OPC進行模擬{long_suffix}",
        ),
        Record(
            path=Path("2026-11/r2.md"),
            mtime=2.0,
            raw_text=f"{long_prefix}使用歐匹西進行計算{long_suffix}",
            result_text=f"{long_prefix}使用OPC進行計算{long_suffix}",
        ),
    ]
    candidates = find_candidates(records)
    corr = [c for c in candidates if c.kind == "corrected"]
    assert len(corr) >= 1
    c = corr[0]
    for ctx in c.contexts:
        # Context must be a snippet around window 30, not the full ~200 character text
        assert len(ctx) < 120
        assert "歐匹西" in ctx or "OPC" in ctx


def test_cross_kind_deduplication():
    # Record has unsure, variant, and corrected all relating to OPC
    records = [
        Record(
            path=Path("2026-11/r1.md"),
            mtime=1.0,
            raw_text="我們用歐匹西跑模擬",
            result_text="我們用OPC[?]跑模擬，還有OP-C技術。",
        ),
        Record(
            path=Path("2026-11/r2.md"),
            mtime=2.0,
            raw_text="我們用歐匹西跑優化",
            result_text="我們用OPC[?]跑優化，還有OP-C技術。",
        ),
    ]
    candidates = find_candidates(records)
    # Filter candidates with normalized key "opc"
    opc_candidates = [c for c in candidates if "opc" in c.term.lower() or c.alias_target.lower() == "opc"]
    # Must only have ONE candidate for OPC due to deduplication
    assert len(opc_candidates) == 1
    # Priority unsure > corrected-with-alias > variant > corrected-term
    assert opc_candidates[0].kind == "unsure"
    assert opc_candidates[0].count >= 2


def test_detect_all_records_filter_only_sources():
    rec1 = Record(
        path=Path("2026-11/r1.md"),
        mtime=1.0,
        raw_text="ILT and curvilinear",
        result_text="ILT and curvilinear mask.",
    )
    rec2 = Record(
        path=Path("2026-11/r2.md"),
        mtime=2.0,
        raw_text="ILT and curve linear",
        result_text="ILT and curve linear mask.",
    )
    rec3 = Record(
        path=Path("2026-11/r3.md"),
        mtime=3.0,
        raw_text="SMO test",
        result_text="SMO[?] only in r3.",
    )

    # When only r2 is new/changed:
    # curvilinear/curve linear appeared in r1 and r2 -> matches r2 -> returned!
    # SMO only appeared in r3 -> not in r2 -> filtered out!
    candidates = find_candidates([rec1, rec2, rec3], only_sources={"r2.md"})
    terms = {c.term.lower() for c in candidates}
    assert any("curvilinear" in t for t in terms)
    assert not any("smo" in t for t in terms)

    # Verify sources tracking: sources has both r1.md and r2.md
    c = next(c for c in candidates if "curvilinear" in c.term.lower())
    assert "r1.md" in c.sources
    assert "r2.md" in c.sources


def test_candidates_capped_at_forty():
    # Generate 50 records with distinct unsure words
    records = [
        Record(
            path=Path(f"2026-11/r{i}.md"),
            mtime=float(i),
            raw_text=f"term{i}",
            result_text=f"測試 term{i}[?] 內容",
        )
        for i in range(50)
    ]
    candidates = find_candidates(records)
    assert len(candidates) == 40


# ----------------------------------------------------------------- advisor
class MockLLM:
    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    def refine(self, raw_text: str, system_prompt: str) -> str:
        self.calls.append((raw_text, system_prompt))
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def test_build_questions_llm_success():
    cand = Candidate(
        kind="variant",
        term="curvilinear",
        count=3,
        variants=["curvilinear", "curve linear"],
        contexts=["...we use curvilinear mask..."],
        sources=["r1.md"],
    )
    llm_json = json.dumps({
        "items": [
            {
                "term": "curvilinear",
                "canonical": "Curvilinear",
                "definition": "曲面光罩技術",
                "is_proper_noun": True,
            }
        ]
    })
    llm = MockLLM(llm_json)
    questions, llm_ok = build_questions([cand], glossary=None, llm=llm)

    assert llm_ok is True
    assert len(questions) == 1
    q = questions[0]
    assert q.suggested_term == "Curvilinear"
    assert q.definition == "曲面光罩技術"
    assert "Curvilinear" in q.options
    assert "curvilinear" in q.options
    assert "curve linear" in q.options


def test_build_questions_llm_timeout_falls_back():
    cand = Candidate(
        kind="unsure",
        term="ILT",
        count=2,
        variants=["ILT"],
        contexts=["...ILT模擬..."],
        sources=["r1.md"],
    )
    llm = MockLLM(LLMConnectionError("Timeout"))
    questions, llm_ok = build_questions([cand], glossary=None, llm=llm)

    assert llm_ok is False
    assert len(questions) == 1
    q = questions[0]
    assert q.suggested_term == ""
    assert "ILT" in q.options


def test_build_questions_llm_non_json_falls_back():
    cand = Candidate(
        kind="variant",
        term="SMO",
        count=2,
        variants=["SMO", "smo"],
        contexts=["...SMO優化..."],
        sources=["r1.md"],
    )
    llm = MockLLM("Here is my analysis: SMO stands for Source Mask Optimization.")
    questions, llm_ok = build_questions([cand], glossary=None, llm=llm)

    assert llm_ok is False
    assert len(questions) == 1
    q = questions[0]
    assert "SMO" in q.options


def test_to_items_and_apply(tmp_path):
    cand1 = Candidate(
        kind="variant",
        term="curvilinear",
        variants=["curvilinear", "curve linear"],
        count=3,
    )
    q1 = Question(
        candidate=cand1,
        question_text="你說的是哪一個？",
        contexts=["ctx"],
        options=["Curvilinear", "curve linear"],
        suggested_term="Curvilinear",
        definition="曲面光罩技術",
    )

    cand2 = Candidate(
        kind="unsure",
        term="not_a_term",
        variants=["not_a_term"],
        count=1,
    )
    q2 = Question(
        candidate=cand2,
        question_text="你說的是哪一個？",
        contexts=["ctx2"],
        options=["not_a_term"],
    )

    cand3 = Candidate(
        kind="unsure",
        term="skip_me",
        variants=["skip_me"],
        count=1,
    )
    q3 = Question(
        candidate=cand3,
        question_text="你說的是哪一個？",
        contexts=["ctx3"],
        options=["skip_me"],
    )

    answers = {
        0: Answer(choice="Curvilinear"),
        1: Answer(choice="not_proper_noun"),
        2: Answer(choice="skip"),
    }

    items = to_items(answers, [q1, q2, q3])
    # q1 produces term Curvilinear and alias curve linear -> Curvilinear
    kinds = {i.kind for i in items}
    assert "term" in kinds
    assert "alias" in kinds

    term_items = [i for i in items if i.kind == "term"]
    assert any(i.key == "Curvilinear" for i in term_items)

    alias_items = [i for i in items if i.kind == "alias"]
    assert any(i.key == "curve linear" and i.value == "Curvilinear" for i in alias_items)

    # Neither q2 nor q3 produced items
    assert not any(i.key == "not_a_term" for i in items)
    assert not any(i.key == "skip_me" for i in items)

    # Now test writing to overlay via teach.apply.apply
    gloss_file = tmp_path / "glossary.yaml"
    gloss_file.write_text("canonical_terms: []\nphonetic_aliases: {}\n", encoding="utf-8")
    cfg = AppConfig(
        glossary_path=str(gloss_file),
        prompts_dir=str(tmp_path / "prompts"),
    )
    result = teach_apply.apply(items, Mode.CLEAN, cfg)
    assert result.glossary_changed is True
    overlay = tmp_path / "glossary.user.yaml"
    assert overlay.exists()
    content = overlay.read_text(encoding="utf-8")
    assert "Curvilinear" in content
    assert "curve linear" in content


# --------------------------------------------------------------- controller
def test_controller_default_skip_and_empty_apply(tmp_path):
    records_dir = tmp_path / "records"
    month_dir = records_dir / "2026-11"
    month_dir.mkdir(parents=True)

    rec = month_dir / "rec.md"
    rec.write_text(
        "---\ntime: 2026-11-12T12:00:00\nmode: clean\n---\n\n"
        "## 結果\n\nILT[?] 模擬。\n\n"
        "## 原文\n\nILT 模擬。\n",
        encoding="utf-8",
    )

    gloss_file = tmp_path / "glossary.yaml"
    gloss_file.write_text("canonical_terms: []\nphonetic_aliases: {}\n", encoding="utf-8")

    from types import SimpleNamespace
    cfg = SimpleNamespace(
        storage=SimpleNamespace(records_dir=str(records_dir)),
        cache_dir=str(tmp_path / "cache"),
        glossary_path=str(gloss_file),
        prompts_dir=str(tmp_path / "prompts"),
        llm=SimpleNamespace(),
    )

    ctl = ReviewController(cfg, lambda: MockLLM("{}"), threaded=False)
    ctl.scan(recheck_all=True)
    view = ctl.poll()
    assert view.phase == "ready"
    assert len(view.questions) == 1

    # Default answer MUST be "skip"
    assert view.answers[0].choice == "skip"

    # Calling apply with only skip answers must write nothing to glossary overlay
    ctl.apply()
    view = ctl.poll()
    assert view.phase == "done"
    overlay = tmp_path / "glossary.user.yaml"
    assert not overlay.exists()


def test_controller_scan_apply_and_revert_flow(tmp_path):
    records_dir = tmp_path / "records"
    month_dir = records_dir / "2026-11"
    month_dir.mkdir(parents=True)

    rec1 = month_dir / "rec1.md"
    rec1.write_text(
        "---\ntime: 2026-11-12T12:00:00\nmode: clean\n---\n\n"
        "## 結果\n\n我們在ILT計算中使用curvilinear mask優化。\n\n"
        "## 原文\n\n我們在ILT計算中使用curve linear mask優化。\n",
        encoding="utf-8",
    )

    rec2 = month_dir / "rec2.md"
    rec2.write_text(
        "---\ntime: 2026-11-12T12:01:00\nmode: clean\n---\n\n"
        "## 結果\n\n這是一個 ordinaryword[?]。\n\n"
        "## 原文\n\n這是一個 ordinaryword。\n",
        encoding="utf-8",
    )

    rec3 = month_dir / "rec3.md"
    rec3.write_text(
        "---\ntime: 2026-11-12T12:02:00\nmode: clean\n---\n\n"
        "## 結果\n\n模擬使用curvilinear mask優化。\n\n"
        "## 原文\n\n模擬使用curve linear mask優化。\n",
        encoding="utf-8",
    )

    gloss_file = tmp_path / "glossary.yaml"
    gloss_file.write_text("canonical_terms: []\nphonetic_aliases: {}\n", encoding="utf-8")

    from types import SimpleNamespace
    cfg = SimpleNamespace(
        storage=SimpleNamespace(records_dir=str(records_dir)),
        cache_dir=str(tmp_path / "cache"),
        glossary_path=str(gloss_file),
        prompts_dir=str(tmp_path / "prompts"),
        llm=SimpleNamespace(),
    )

    llm_json = json.dumps({
        "items": [
            {
                "term": "curvilinear",
                "canonical": "Curvilinear",
                "definition": "曲面光罩技術",
                "is_proper_noun": True,
            },
            {
                "term": "ordinaryword",
                "canonical": "ordinaryword",
                "definition": "日常詞彙",
                "is_proper_noun": False,
            },
        ]
    })
    llm = MockLLM(llm_json)

    ctl = ReviewController(cfg, lambda: llm, threaded=False)
    view = ctl.poll()
    assert view.phase == "idle"

    # 1. First scan: incremental
    ctl.scan(recheck_all=False)
    view = ctl.poll()
    assert view.phase == "ready"
    assert len(view.questions) >= 1
    assert view.llm_ok is True

    # Find the curvilinear and ordinaryword questions
    curv_idx = next(
        i for i, q in enumerate(view.questions)
        if "curvilinear" in q.candidate.term.lower() or "curvilinear" in q.candidate.alias_target.lower()
    )
    ctl.set_answer(curv_idx, choice="Curvilinear")

    ord_idx = next((i for i, q in enumerate(view.questions) if "ordinaryword" in q.candidate.term.lower()), None)
    if ord_idx is not None:
        ctl.set_answer(ord_idx, choice="not_proper_noun")

    # 2. Apply
    ctl.apply()
    view = ctl.poll()
    assert view.phase == "done"
    assert "套用完成" in view.message

    overlay = tmp_path / "glossary.user.yaml"
    assert overlay.exists()
    content = overlay.read_text(encoding="utf-8")
    assert "Curvilinear" in content

    # 3. Check state persistence
    state = ctl.state
    assert not state.is_file_changed(rec1, rec1.stat().st_mtime)
    if ord_idx is not None:
        assert state.is_ignored("ordinaryword")

    # 4. Incremental re-scan should find 0 candidates since files haven't changed
    ctl.scan(recheck_all=False)
    view = ctl.poll()
    assert view.phase == "ready"
    assert len(view.questions) == 0

    # 5. Full re-scan with recheck_all=True
    ctl.scan(recheck_all=True)
    view = ctl.poll()
    assert view.phase == "ready"
    # Ordinaryword was marked ignored, so it won't appear
    assert not any("ordinaryword" in q.candidate.term.lower() for q in view.questions)

    # 6. Revert
    assert ctl.can_revert()
    ctl.revert()
    view = ctl.poll()
    assert "已復原" in view.message


def test_review_window_lifecycle(tmp_path):
    import tkinter as tk
    try:
        root = tk.Tk()
        root.withdraw()
    except Exception:
        pytest.skip("Tkinter display not available")

    try:
        from types import SimpleNamespace
        from local_typeless.ui.review_window import ReviewWindow

        cfg = SimpleNamespace(
            storage=SimpleNamespace(records_dir=str(tmp_path)),
            cache_dir=str(tmp_path / "cache"),
            glossary_path=str(tmp_path / "g.yaml"),
            prompts_dir=str(tmp_path),
        )
        ctl = ReviewController(cfg, lambda: None, threaded=False)
        win = ReviewWindow(root, ctl)
        assert win._alive is True
        win.lift()
        win.close()
        assert win._alive is False
    finally:
        try:
            root.destroy()
        except Exception:
            pass


def test_llm_suggestion_is_never_paired_by_position():
    from local_typeless.review.advisor import build_questions
    from local_typeless.review.detect import Candidate

    class FakeLLM:
        def refine(self, raw_text, system_prompt):
            # The LLM answered only for the second candidate.
            return '{"items":[{"term":"SRAF","canonical":"SRAF","definition":"輔助圖形","is_proper_noun":true}]}'

    cands = [
        Candidate(kind="corrected", term="MRC", count=2, variants=["MRC"]),
        Candidate(kind="unsure", term="SRAF", count=1, variants=["SRAF"]),
    ]
    questions, ok = build_questions(cands, llm=FakeLLM())
    assert ok
    assert questions[0].suggested_term == "" and questions[0].options == ["MRC"]
    assert questions[1].suggested_term == "SRAF"


def test_variant_contexts_cover_every_spelling(tmp_path):
    from pathlib import Path

    from local_typeless.review.detect import find_candidates
    from local_typeless.review.records import Record

    def rec(n, text):
        return Record(path=Path(f"r{n}.md"), mtime=float(n), result_text=text, raw_text="")

    records = [rec(i, f"第 {i} 次用 curvilinear mask 做實驗。") for i in range(1, 5)]
    records.append(rec(9, "curve linear mask 的規則比較難。"))
    cands = [c for c in find_candidates(records) if c.kind == "variant"]
    assert cands and any("curve linear" in ctx for ctx in cands[0].contexts)
