"""Rule-based cleanup, the faithfulness guard, and how the pipeline uses them.

Sample sentences use public computational-lithography terms only.
"""
from pathlib import Path

import pytest

from local_typeless.cleanup import guard_check, rules_clean
from local_typeless.cleanup.guard import glossary_terms
from local_typeless.pipeline import GUARD_REJECTED, Pipeline
from local_typeless.types import AppConfig, LLMConnectionError, Mode, STTProfileConfig


# ------------------------------------------------------------------ rules
@pytest.mark.parametrize(
    "raw, expected",
    [
        ("嗯，請看這個檔案", "請看這個檔案"),
        ("我我我想要，呃，這個這個 OPC 的 the the result", "我想要，這個 OPC 的 the result"),
        ("請看，嗯，這個。嗯。然後 um run it", "請看，這個。然後 run it"),
        ("run um it", "run it"),
        ("這個 model 我覺得 我覺得有點問題", "這個 model 我覺得有點問題"),
        ("A，嗯。B", "A。B"),
    ],
)
def test_rules_remove_fillers_and_stammers(raw, expected):
    assert rules_clean(raw) == expected


@pytest.mark.parametrize(
    "text",
    [
        "看看這個結果，謝謝",            # doubled characters are real words
        "dose 是三三三",                 # spoken digits repeat
        "請看 [檔案1] 跟 [圖2]",          # attachment markers
        "summary 跟 number 都要",        # "um" inside a word is not a filler
        "三點，不對，四點",               # self-corrections are left to the LLM
    ],
)
def test_rules_leave_content_alone(text):
    assert rules_clean(text) == text


def test_rules_use_the_configured_fillers_and_keep_lines():
    assert rules_clean("那個 第一行\n嗯 第二行", fillers=["那個"]) == "第一行\n嗯 第二行"
    assert rules_clean("") == ""


# ------------------------------------------------------------------ guard
@pytest.mark.parametrize(
    "raw, out, mode",
    [
        ("嗯那個我想問一下 ILT 的 cost function 要怎麼設", "我想問一下 ILT 的 cost function 要怎麼設？", "clean"),
        ("dose 是百分之十，focus 三點五", "Dose 是 10%，focus 3.5。", "clean"),
        ("二零二六年要做兩三個實驗，大概三百二十片", "2026 年要做 2-3 個實驗，大概 320 片。", "clean"),
        ("比較 OPC 跟 I L T 在 high na 的差異", "比較 OPC 跟 ILT 在 High NA 的差異。", "clean"),
        ("用 OPC 不對用 SMO 來做", "用 SMO 來做。", "clean"),          # taken back by the speaker
        ("請看 [檔案1] 然後分析 OPC", "請看 [檔案1]，然後分析 OPC。", "clean"),
        ("focus 是負三十奈米", "Focus 是 -30 nm。", "clean"),
        ("這個這個 OPC 的 model 我覺得 我覺得有點問題 dose 用百分之十 不對 百分之十二 然後 focus 是負三十奈米",
         "這個 OPC 的 model 我覺得有點問題。dose 用 12%，focus 是 -30 nm。", "clean"),
        ("二零二六年第三季要做完", "2026 Q3 要做完。", "clean"),
        ("幫我做幾件事 第一 跑 OPC 第二 檢查 SRAF 第三 寫報告",
         "請完成以下工作：\n1. 跑 OPC。\n2. 檢查 SRAF。\n3. 寫報告。", "prompt"),
        ("這頁講 EUV 的 resist 模型 這裡放圖一", "## EUV resist 模型\n- resist 模型\n> [!todo] 🖼️ 待補圖：一", "slide"),
        ("", "anything", "clean"),
    ],
)
def test_guard_accepts_faithful_rewrites(raw, out, mode):
    assert guard_check(raw, out, mode) is None


@pytest.mark.parametrize(
    "raw, out, mode, word",
    [
        # answered instead of cleaned, with new terms
        ("我想問一下 ILT 的 cost function 要怎麼設",
         "ILT 的 cost function 通常包含 EPE 與 PV band，建議先用 MSE。", "clean", "英文詞"),
        # answered in pure Chinese: caught by length
        ("我想問一下這個參數要怎麼設",
         "這個參數通常要依照製程條件來設定，建議先從預設值開始，再逐步調整並觀察結果的變化。", "clean", "長"),
        ("dose 是百分之十", "Dose 是 15%。", "clean", "數字"),
        ("focus 是負三十", "Focus 是 -30 nm。", "clean", "英文詞"),
        ("用 OPC 跟 SMO 還有 SRAF 來做 mask 最佳化", "來做最佳化。", "clean", "少了"),
        ("今天先把光罩的資料整理好然後再跑一次模擬最後把結果寄給大家確認一下有沒有問題", "整理資料。", "clean", "短"),
        ("比較這兩個方法", "比較 OPC 與 ILT。", "prompt", "英文詞"),
        ("寫一個函式算平均", "```python\ndef f(): pass\n```", "prompt", "程式碼"),
        ("今天講 EUV", "摘要：EUV 與 DUV 的比較。", "summary", "英文詞"),
        ("今天講 EUV", "   ", "summary", "空"),
    ],
)
def test_guard_rejects_inventions_and_answers(raw, out, mode, word):
    reason = guard_check(raw, out, mode)
    assert reason is not None and word in reason


def test_guard_allows_corrections_towards_glossary_terms():
    raw, out = "這個 OBC 的結果", "這個 OPC 的結果。"
    assert guard_check(raw, out, "clean") is not None
    allowed = glossary_terms('- "OPC": Optical Proximity Correction')
    assert "opc" in allowed
    assert guard_check(raw, out, "clean", allowed_terms=allowed) is None


def test_guard_summary_may_be_short_and_drop_terms():
    raw = "今天講了 OPC 跟 SMO 還有 SRAF 的設定，然後也討論 EUV 的 resist 模型跟後續的實驗規劃"
    assert guard_check(raw, "討論了 OPC 與 EUV。", "summary") is None


# --------------------------------------------------------------- pipeline
class _LLM:
    def __init__(self, reply=None, exc=None):
        self.reply, self.exc = reply, exc

    def refine(self, raw_text, system_prompt):
        if self.exc is not None:
            raise self.exc
        return self.reply

    def ping(self):
        return True


class _Glossary:
    def build_whisper_initial_prompt(self):
        return ""

    def build_llm_glossary_section(self):
        return ""

    def normalize_terms(self, text):
        return text


def _pipeline(tmp_path: Path, llm, **refine) -> Pipeline:
    prompts = tmp_path / "prompts"
    prompts.mkdir(exist_ok=True)
    for name in ("clean", "prompt", "summary", "slide"):
        (prompts / f"{name}.md").write_text("{glossary}", encoding="utf-8")
    cfg = AppConfig(stt_profiles={"cpu_fast": STTProfileConfig()}, prompts_dir=str(prompts),
                    cache_dir=str(tmp_path / "cache"), history_enabled=False)
    for key, value in refine.items():
        setattr(cfg.refine, key, value)
    return Pipeline(cfg, stt=object(), llm=llm, glossary=_Glossary(), outputs=[], records=False)


RAW = "嗯，我想問一下這個參數要怎麼設"
ANSWER = "這個參數通常要依照製程條件來設定，建議先從預設值開始，再逐步調整並觀察結果的變化。"


@pytest.mark.guard
def test_pipeline_rejected_output_falls_back_to_rule_cleaned_text(tmp_path):
    p = _pipeline(tmp_path, _LLM(ANSWER))
    res = p.refine_only(RAW, Mode.CLEAN)
    assert res.fallback_triggered and GUARD_REJECTED in res.error_message
    assert res.final_text == "我想問一下這個參數要怎麼設"
    assert p.refine_piece(RAW) == ("我想問一下這個參數要怎麼設", True)


@pytest.mark.guard
def test_pipeline_accepts_faithful_output(tmp_path):
    p = _pipeline(tmp_path, _LLM("我想問一下這個參數要怎麼設？"))
    res = p.refine_only(RAW, Mode.CLEAN)
    assert not res.fallback_triggered and res.final_text == "我想問一下這個參數要怎麼設？"
    assert p.refine_piece(RAW) == ("我想問一下這個參數要怎麼設？", False)


@pytest.mark.guard
def test_pipeline_guard_can_be_switched_off(tmp_path):
    p = _pipeline(tmp_path, _LLM(ANSWER), guard=False)
    res = p.refine_only(RAW, Mode.CLEAN)
    assert not res.fallback_triggered and res.final_text == ANSWER


@pytest.mark.guard
def test_pipeline_prompt_mode_is_guarded_too(tmp_path):
    raw = "幫我比較這兩個方法然後寫成表格"
    ok = _pipeline(tmp_path, _LLM("比較這兩個方法，並寫成表格。")).refine_only(raw, Mode.PROMPT)
    assert not ok.fallback_triggered and ok.mode == Mode.PROMPT
    bad = _pipeline(tmp_path, _LLM("比較 OPC 與 ILT，並寫成表格。")).refine_only(raw, Mode.PROMPT)
    assert bad.fallback_triggered and bad.final_text == raw


def test_llm_outage_falls_back_to_rule_cleaned_text(tmp_path):
    p = _pipeline(tmp_path, _LLM(exc=LLMConnectionError("down")), fillers=["嗯", "那個"])
    res = p.refine_only("嗯，那個，請看這個這個結果", Mode.CLEAN)
    assert res.fallback_triggered and res.final_text == "請看這個結果"


def test_prompt_mode_guard_leaves_room_for_restructuring():
    raw = "嗯幫我比較一下 OPC 跟 ILT 的差異 然後 mask 的資料量大概 2千 筆 結果列出來"
    # a lead-in line, list wording and a word or two of its own are fine in prompt mode
    out = ("請完成以下工作，並把結果整理給我：\n1. 比較 OPC 與 ILT 的差異，逐項說明。\n"
           "2. mask 的資料量大約 2000 筆。\n3. 把結果列出來，使用 AI 產生的 summary。")
    assert guard_check(raw, out, "prompt") is None
    assert guard_check(raw, out, "clean") is not None            # clean stays strict
    # an answer is still refused: far longer, or full of terms nobody said
    assert guard_check(raw, out + "OPC 是光學鄰近修正，" * 12, "prompt") is not None
    assert guard_check(raw, "用 Python、pandas、numpy 寫 script 比較 OPC 跟 ILT", "prompt") is not None
    assert guard_check(raw, "比較 OPC 跟 ILT，資料量 5000 筆", "prompt") is not None


@pytest.mark.guard
def test_refine_only_can_keep_a_doubted_result_for_review(tmp_path):
    raw = "比較這兩個方法 然後寫成表格"
    p = _pipeline(tmp_path, _LLM("比較 OPC 與 ILT，並寫成表格。"))
    strict = p.refine_only(raw, Mode.PROMPT)
    assert strict.fallback_triggered and not strict.guard_warnings
    kept = p.refine_only(raw, Mode.PROMPT, guard_warns=True)
    assert not kept.fallback_triggered and kept.final_text.startswith("比較 OPC")
    assert kept.guard_warnings and "英文詞" in kept.guard_warnings[0]
