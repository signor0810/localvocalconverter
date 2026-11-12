"""Teach mode: store, alignment, advisor (mock LLM), apply/revert, evaluate, controller. Offline."""
from __future__ import annotations

import dataclasses
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from local_typeless.teach import advisor, align, apply as tapply, evaluate as teval
from local_typeless.teach.store import TeachStore
from local_typeless.types import AppConfig, LLMConnectionError, Mode, PipelineResult
from local_typeless.ui.teach_controller import TeachController


# ----------------------------------------------------------------- store
def test_store_round_trip_delete_clear(tmp_path):
    st = TeachStore(tmp_path)
    a = st.add("summary", "原文一", "結果一", "期望一")
    b = st.add("clean", "原文二", "結果二", "期望二")
    st2 = TeachStore(tmp_path)                                  # fresh instance reads from disk
    assert [s.id for s in st2.list()] == [a.id, b.id]
    assert [s.id for s in st2.list("clean")] == [b.id]
    assert st2.list()[0].expected_text == "期望一"
    assert st2.delete(a.id) and not st2.delete(a.id)
    assert st2.clear("clean") == 1 and st2.list() == []
    (tmp_path / "teach" / "samples.jsonl").write_text("garbage\n", encoding="utf-8")
    assert st2.list() == []                                     # corrupt rows skipped


# ----------------------------------------------------------------- align
def test_align_chinese_homophone_to_latin_term():
    items = align.mine("我們用普羅透斯來跑模擬", "我們用Proteus來跑模擬", [])
    assert align.AlignItem("alias", "普羅透斯", "Proteus") in items
    assert align.AlignItem("term", "Proteus", "") in items


def test_align_latin_misheard_phrase():
    items = align.mine("we run Pro tears today", "we run Proteus today", [])
    assert align.AlignItem("alias", "Pro tears", "Proteus") in items


def test_align_ignores_punctuation_whitespace_and_known_terms():
    assert align.mine("你好 世界", "你好，世界。", []) == []
    assert align.mine("跑 SRAF 優化", "跑 SRAF 優化", ["SRAF"]) == []
    # case-only differences are left to normalisation
    assert align.mine_aliases("用 sraf 優化", "用 SRAF 優化") == []


def test_align_trims_leading_trailing_fillers():
    assert align.mine_aliases("嗯那個普羅特斯跑", "Proteus跑") == [("普羅特斯", "Proteus")]
    assert align.mine_aliases("就是說pro tears啊", "Proteus") == [("pro tears", "Proteus")]
    assert align.trim_fillers("然後就是說") == ""


def test_advisor_keeps_shorter_alias_when_one_contains_the_other(tmp_path):
    s = _sample(TeachStore(tmp_path))
    llm = FakeLLM(['{"phonetic_aliases":{"嗯那個普羅特斯":"Proteus","普羅特斯":"Proteus","別的":"Other"}}'])
    p = advisor.propose([s], "summary", "", GLOSS, llm)
    keys = {i.key for i in p.of("alias")}
    assert "普羅特斯" in keys and "嗯那個普羅特斯" not in keys and "別的" in keys


def test_revert_deletes_overlay_created_by_first_apply(tmp_path):
    cfg = _cfg(tmp_path)
    r = tapply.apply([advisor.ProposalItem("rule", "規則A")], Mode.CLEAN, cfg)
    assert r.prompt_path.exists()
    assert tapply.revert(Mode.CLEAN, cfg) == [r.prompt_path] and not r.prompt_path.exists()


def test_align_skips_long_raw_chunks():
    raw = "這是一段非常非常長的聽錯內容"
    assert align.mine_aliases(f"用{raw}跑", "用Proteus跑") == []


# --------------------------------------------------------------- advisor
class FakeLLM:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def refine(self, raw_text, system_prompt):
        self.calls.append((raw_text, system_prompt))
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


GLOSS = SimpleNamespace(canonical_terms=[{"term": "OPC", "definition": "x"}],
                        phonetic_aliases={"歐匹西": "OPC"})
JSON = ('```json\n{"glossary_terms":[{"term":"Proteus","definition":"模擬器"},'
        '{"term":"opc","definition":"dup"}],'
        '"phonetic_aliases":{"普羅透斯":"Proteus","歐匹西":"OPC"},'
        '"prompt_rules":["保留英文術語原樣","保留英文術語原樣","數字用阿拉伯數字"]}\n```')


def _sample(st, raw="用普羅透斯跑", out="用普羅透斯跑", exp="用Proteus跑", mode="summary"):
    return st.add(mode, raw, out, exp)


def test_advisor_parses_fenced_json_merges_and_dedupes(tmp_path):
    st = TeachStore(tmp_path)
    s = _sample(st)
    llm = FakeLLM([JSON])
    p = advisor.propose([s], "summary", "目前提示詞：保留英文術語原樣", GLOSS, llm, existing_rules=["數字用阿拉伯數字"])
    kinds = {(i.kind, i.key): i for i in p.items}
    assert ("alias", "普羅透斯") in kinds and kinds[("alias", "普羅透斯")].source == "比對"  # align wins, deduped
    assert ("term", "Proteus") in kinds
    assert ("term", "opc") not in kinds and ("alias", "歐匹西") not in kinds     # already in glossary
    assert p.of("rule") == []                  # both rules already known (prompt / existing overlay)
    # system prompt carries current prompt + glossary; samples block carries the three fields
    assert "OPC" in llm.calls[0][1] and "目前提示詞" in llm.calls[0][1]
    assert "原文：用普羅透斯跑" in llm.calls[0][0] and "使用者期望：用Proteus跑" in llm.calls[0][0]


def test_advisor_rules_are_capped_and_sourced(tmp_path):
    s = _sample(TeachStore(tmp_path))
    rules = [f"規則{n}" for n in range(12)]
    import json
    llm = FakeLLM([json.dumps({"prompt_rules": rules})])
    p = advisor.propose([s], "summary", "", GLOSS, llm)
    assert len(p.of("rule")) == advisor.MAX_RULES and all(r.source == "LLM" for r in p.of("rule"))


def test_advisor_retries_once_then_succeeds(tmp_path):
    s = _sample(TeachStore(tmp_path))
    llm = FakeLLM(["抱歉，這是說明文字", '{"prompt_rules":["新規則"]}'])
    p = advisor.propose([s], "summary", "", GLOSS, llm)
    assert [r.key for r in p.of("rule")] == ["新規則"] and len(llm.calls) == 2
    assert "JSON" in llm.calls[1][1] and llm.calls[1][1] != llm.calls[0][1]


def test_advisor_gives_up_after_one_retry(tmp_path):
    s = _sample(TeachStore(tmp_path))
    llm = FakeLLM(["nope", "still nope"])
    with pytest.raises(advisor.TeachError, match="JSON"):
        advisor.propose([s], "summary", "", GLOSS, llm)
    assert len(llm.calls) == 2


def test_advisor_llm_error_becomes_teach_error(tmp_path):
    s = _sample(TeachStore(tmp_path))
    with pytest.raises(advisor.TeachError, match="內部 LLM"):
        advisor.propose([s], "summary", "", GLOSS, FakeLLM([LLMConnectionError("down")]))
    with pytest.raises(advisor.TeachError):
        advisor.propose([], "summary", "", GLOSS, FakeLLM([]))


# ----------------------------------------------------------------- apply
def _cfg(tmp_path):
    (tmp_path / "prompts").mkdir()
    (tmp_path / "glossary.yaml").write_text("canonical_terms: []\n", encoding="utf-8")
    return AppConfig(glossary_path=str(tmp_path / "glossary.yaml"),
                     prompts_dir=str(tmp_path / "prompts"), cache_dir=str(tmp_path / "cache"))


def test_apply_merges_backs_up_and_reverts(tmp_path):
    cfg = _cfg(tmp_path)
    I = advisor.ProposalItem
    r1 = tapply.apply([I("term", "Proteus", "模擬器"), I("alias", "普羅透斯", "Proteus"),
                       I("rule", "保留英文術語原樣")], Mode.SUMMARY, cfg)
    assert r1.glossary_changed and r1.prompt_changed
    g, p = r1.glossary_path, r1.prompt_path
    assert g.name == "glossary.user.yaml" and p.name == "summary.user.md"
    assert tapply._bak(g).read_text(encoding="utf-8") == "" and tapply._bak(p).read_text(encoding="utf-8") == ""

    first_g, first_p = g.read_text(encoding="utf-8"), p.read_text(encoding="utf-8")
    r2 = tapply.apply([I("term", "SRAF", ""), I("term", "proteus", "dup"), I("alias", "撒夫", "SRAF"),
                       I("rule", "保留英文術語原樣"), I("rule", "數字用阿拉伯數字")], Mode.SUMMARY, cfg)
    data = yaml.safe_load(g.read_text(encoding="utf-8"))
    assert [t["term"] for t in data["canonical_terms"]] == ["Proteus", "SRAF"]      # earlier kept, no dup
    assert data["phonetic_aliases"] == {"普羅透斯": "Proteus", "撒夫": "SRAF"}
    assert r2.added == {"term": 1, "alias": 1, "rule": 1}
    assert p.read_text(encoding="utf-8").splitlines() == ["- 保留英文術語原樣", "- 數字用阿拉伯數字"]
    assert tapply._bak(g).read_text(encoding="utf-8") == first_g              # backup = previous version

    assert set(tapply.revert(Mode.SUMMARY, cfg)) == {g, p}
    assert g.read_text(encoding="utf-8") == first_g and p.read_text(encoding="utf-8") == first_p
    assert tapply.revert(Mode.SUMMARY, cfg) == []                               # single-level undo
    assert not list(tmp_path.rglob("*.tmp"))


def test_apply_only_touches_needed_overlay(tmp_path):
    cfg = _cfg(tmp_path)
    r = tapply.apply([advisor.ProposalItem("rule", "規則A")], Mode.CLEAN, cfg)
    assert r.prompt_changed and not r.glossary_changed and not r.glossary_path.exists()


# -------------------------------------------------------------- evaluate
class FakePipeline:
    def __init__(self):
        self.outputs = ["clipboard!"]
        self.seen = []

    def run_text(self, raw, mode, record_history=True):
        self.seen.append((raw, mode, record_history))
        return PipelineResult(raw_text=raw, final_text=raw.replace("普羅透斯", "Proteus"), mode=mode,
                              stt_profile_name="", llm_profile_name="", stt_elapsed_sec=0, llm_elapsed_sec=0)


def test_evaluate_scores_improve_and_no_side_effects(tmp_path):
    st = TeachStore(tmp_path)
    s = _sample(st)
    fp = FakePipeline()
    out = teval.evaluate([s], lambda: fp)
    sid, before, after, new = out[0]
    assert sid == s.id and new == "用Proteus跑" and after == 1.0 and before < after
    assert fp.outputs == [] and fp.seen == [("用普羅透斯跑", Mode.SUMMARY, False)]
    assert teval.average(out) == (before, after)
    assert teval.similarity("a b\n c", "abc") == 1.0


def test_real_pipeline_run_text_can_skip_history(tmp_path):
    from local_typeless.pipeline import Pipeline

    cfg = _cfg(tmp_path)
    (tmp_path / "prompts" / "summary.md").write_text("sys {glossary}", encoding="utf-8")

    class LLM:
        def refine(self, raw_text, system_prompt):
            return raw_text + "!"

        def list_models(self):
            return []

    gl = SimpleNamespace(build_llm_glossary_section=lambda: "", normalize_terms=lambda t: t,
                         build_whisper_initial_prompt=lambda: "")
    p = Pipeline(cfg, stt=object(), llm=LLM(), glossary=gl, outputs=[])
    assert p.run_text("嗨", Mode.SUMMARY, record_history=False).final_text == "嗨!"
    assert not p.history_path.exists()
    p.run_text("嗨", Mode.SUMMARY)
    assert p.history_path.exists()


# ------------------------------------------------------------ controller
def _controller(tmp_path, llm_reply=JSON):
    cfg = dataclasses.replace(_cfg(tmp_path))
    (tmp_path / "prompts" / "summary.md").write_text("基礎提示詞 {glossary}", encoding="utf-8")
    return TeachController(cfg, lambda: FakeLLM([llm_reply]), FakePipeline, threaded=False)


def test_controller_flow(tmp_path):
    c = _controller(tmp_path)
    assert c.poll() is not None and c.poll() is None                  # dirty flag semantics
    assert not c.add_sample("用Proteus跑") and "歷史" in c.view.message   # no selection yet
    c.set_selection("用普羅透斯跑", "用普羅透斯跑", Mode.SUMMARY)
    assert not c.add_sample("   ")
    assert not c.can_analyze()
    assert c.add_sample("用Proteus跑") and "1 筆" in c.view.count_label and "4–5" in c.view.count_label
    assert c.can_analyze()

    c.analyze()
    assert c.view.phase == "idle" and c.view.items and "建議" in c.view.message
    assert {i.kind for i in c.view.items} >= {"alias", "term"}
    assert all(i.selected for i in c.view.items)
    c.edit_item(0, key="  編輯後 ")
    assert c.view.items[0].key == "編輯後"
    for n in range(len(c.view.items)):
        c.set_item_selected(n, False)
    assert not c.can_apply()
    alias_i = next(n for n, i in enumerate(c.view.items) if i.kind == "alias")
    c.view.items[alias_i].key = "普羅透斯"
    c.set_item_selected(alias_i, True)
    c.apply_selected()
    assert c.view.phase == "idle" and c.view.can_revert
    assert c.view.avg_after == 1.0 and c.view.avg_before < 1.0 and "→" in c.view.message
    assert tapply.glossary_overlay_path(c.cfg.glossary_path).exists()
    c.revert()
    assert not c.view.can_revert and c.view.scores == []
    assert not tapply.glossary_overlay_path(c.cfg.glossary_path).exists()   # first apply undone -> removed

    sid = c.view.samples[0].id
    c.delete_sample(sid)
    assert c.view.samples == [] and not c.can_analyze()


def test_controller_analyze_error_is_reported_not_stuck(tmp_path):
    cfg = _cfg(tmp_path)
    (tmp_path / "prompts" / "summary.md").write_text("p", encoding="utf-8")
    c = TeachController(cfg, lambda: FakeLLM([LLMConnectionError("down")]), FakePipeline, threaded=False)
    c.set_selection("原文", "結果", Mode.SUMMARY)
    c.add_sample("答案")
    c.analyze()
    assert c.view.phase == "idle" and "內部 LLM" in c.view.message and c.view.items == []


def test_controller_threaded_analyze_finishes(tmp_path):
    import time

    cfg = _cfg(tmp_path)
    (tmp_path / "prompts" / "summary.md").write_text("p", encoding="utf-8")
    c = TeachController(cfg, lambda: FakeLLM([JSON]), FakePipeline)
    c.set_selection("用普羅透斯跑", "x", Mode.SUMMARY)
    c.add_sample("用Proteus跑")
    c.analyze()
    for _ in range(100):
        if c.view.phase == "idle" and c.view.items:
            break
        time.sleep(0.05)
    assert c.view.items


def test_overlay_paths_follow_naming_convention(tmp_path):
    assert tapply.glossary_overlay_path(tmp_path / "g.yaml") == tmp_path / "g.user.yaml"
    assert tapply.prompt_overlay_path(Mode.SLIDE, tmp_path) == tmp_path / "slide.user.md"
