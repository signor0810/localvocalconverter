"""Pipeline/CLI unit tests with fakes (no network, mic, GPU)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from local_typeless import cli
from local_typeless.pipeline import NoHistoryError, Pipeline
from local_typeless.types import (
    AppConfig,
    LLMConnectionError,
    LLMProfileConfig,
    Mode,
    STTProfileConfig,
    TranscriptResult,
)

PROMPTS = Path(__file__).resolve().parent.parent / "prompts"


class FakeSTT:
    def __init__(self, text="raw text"):
        self.text = text
        self.calls = []

    def transcribe(self, audio_path, initial_prompt=None):
        self.calls.append((audio_path, initial_prompt))
        return TranscriptResult(self.text, 1.0, 0.1, "cpu", "small")


class FakeLLM:
    def __init__(self, out="refined", exc=None):
        self.out, self.exc, self.calls = out, exc, []

    def refine(self, raw_text, system_prompt):
        self.calls.append((raw_text, system_prompt))
        if self.exc:
            raise self.exc
        return self.out

    def list_models(self):
        return ["m"]


class FakeGlossary:
    def build_whisper_initial_prompt(self):
        return "PROMPT"

    def build_llm_glossary_section(self):
        return "GLOSS"

    def normalize_terms(self, text):
        return text.replace("high-na", "High NA")


class Out:
    def __init__(self, fail=False):
        self.fail, self.got = fail, []

    def emit(self, text, mode):
        if self.fail:
            raise RuntimeError("boom")
        self.got.append((text, mode))


def make(tmp_path, stt=None, llm=None, outputs=None, **cfg_kw):
    cfg = AppConfig(
        stt_profiles={"cpu_fast": STTProfileConfig()},
        llm_profiles={"dev": LLMProfileConfig()},
        prompts_dir=str(PROMPTS),
        cache_dir=str(tmp_path / "cache"),
        **cfg_kw,
    )
    return Pipeline(cfg, stt=stt or FakeSTT(), llm=llm or FakeLLM(),
                    glossary=FakeGlossary(), outputs=[] if outputs is None else outputs)


def test_happy_path(tmp_path):
    stt, llm, out = FakeSTT("um high-na"), FakeLLM("clean high-na"), Out()
    p = make(tmp_path, stt, llm, [out])
    r = p.run_file(tmp_path / "a.wav", Mode.CLEAN)
    assert stt.calls[0][1] == "PROMPT"
    assert "GLOSS" in llm.calls[0][1]
    assert r.final_text == "clean High NA"  # normalize applied to LLM output
    assert not r.fallback_triggered and r.error_message is None
    assert out.got == [("clean High NA", Mode.CLEAN)]


def test_llm_failure_falls_back(tmp_path):
    out = Out()
    p = make(tmp_path, FakeSTT("say high-na"), FakeLLM(exc=LLMConnectionError("down")), [out])
    r = p.run_file(tmp_path / "a.wav", Mode.CLEAN)
    assert r.fallback_triggered and "down" in r.error_message
    assert r.final_text == "say High NA"
    assert out.got[0][0] == "say High NA"


@pytest.mark.parametrize("text", ["", "   \n"])
def test_empty_transcript_skips_llm(tmp_path, text):
    llm, out = FakeLLM(), Out()
    p = make(tmp_path, FakeSTT(text), llm, [out])
    r = p.run_file(tmp_path / "a.wav", Mode.CLEAN)
    assert r.error_message == "no speech detected"
    assert llm.calls == [] and out.got == []
    assert not p.history_path.exists()


def test_failing_output_isolated(tmp_path):
    good = Out()
    p = make(tmp_path, outputs=[Out(fail=True), good])
    r = p.run_file(tmp_path / "a.wav", Mode.CLEAN)
    assert r.final_text == "refined" and good.got


def test_history_trim_and_disable(tmp_path):
    p = make(tmp_path, history_max=3)
    for i in range(5):
        p.run_text(f"t{i}", Mode.CLEAN)
    rows = [json.loads(l) for l in p.history_path.read_text(encoding="utf-8").splitlines()]
    assert [r["raw_text"] for r in rows] == ["t2", "t3", "t4"]

    p2 = make(tmp_path / "x", history_enabled=False)
    p2.run_text("hi", Mode.CLEAN)
    assert not p2.history_path.exists()


def test_retry_llm_only(tmp_path):
    llm = FakeLLM()
    p = make(tmp_path, FakeSTT(), llm)
    with pytest.raises(NoHistoryError):
        p.retry_last(Mode.CLEAN, llm_only=True)
    p.run_text("original raw", Mode.CLEAN)
    r = p.retry_last(Mode.SLIDE, llm_only=True)
    assert r.raw_text == "original raw" and r.mode is Mode.SLIDE


def test_retry_audio(tmp_path):
    stt = FakeSTT()
    p = make(tmp_path, stt)
    with pytest.raises(FileNotFoundError):
        p.retry_last(Mode.CLEAN)
    (tmp_path / "cache").mkdir()
    (tmp_path / "cache" / "last_recording.wav").write_bytes(b"x")
    p.retry_last(Mode.CLEAN)
    assert stt.calls[0][0].name == "last_recording.wav"


def test_run_text_transform(tmp_path):
    llm = FakeLLM()
    r = make(tmp_path, llm=llm).run_text("some text", Mode.TRANSFORM)
    assert r.mode is Mode.TRANSFORM and r.stt_elapsed_sec == 0.0


def test_cli_parser_and_overrides():
    a = cli._build_parser().parse_args(
        ["--stt-profile", "gpu_local", "--device", "cpu", "--model-path", "D:/m",
         "retry", "--mode", "slide", "--llm-only"]
    )
    assert cli._overrides(a) == {"stt_profile": "gpu_local", "device": "cpu", "model_path": "D:/m"}
    assert a.llm_only and a.mode == "slide"


def test_cli_check_nonfatal(monkeypatch, capsys):
    import local_typeless.llm as llm_mod

    class Bad:
        def list_models(self):
            raise LLMConnectionError("nope")

    monkeypatch.setattr(llm_mod, "create_llm_client", lambda p: Bad())
    cfg = AppConfig(stt_profiles={"cpu_fast": STTProfileConfig()},
                    llm_profiles={"dev": LLMProfileConfig()})
    cli._check(cfg)
    out = capsys.readouterr().out
    assert "cpu_fast" in out and "LLM ping failed" in out


def test_missing_prompt_triggers_fallback(tmp_path):
    p = make(tmp_path, FakeSTT("say high-na"))
    p.config.prompts_dir = str(tmp_path / "no_prompts")
    r = p.run_text("say high-na", Mode.CLEAN)
    assert r.fallback_triggered and "prompt load failed" in r.error_message
    assert r.final_text == "say High NA"


def test_any_llm_stage_exception_falls_back(tmp_path):
    class Bad(FakeLLM):
        def refine(self, raw_text, system_prompt):
            raise KeyError("bug")

    r = make(tmp_path, FakeSTT(), llm=Bad()).run_text("say high-na", Mode.CLEAN)
    assert r.fallback_triggered and "KeyError" in r.error_message
    assert r.final_text == "say High NA"


def test_normalize_failure_emits_bare_raw(tmp_path):
    class BadGloss(FakeGlossary):
        def normalize_terms(self, text):
            raise ValueError("glossary broken")

    out = Out()
    p = make(tmp_path, outputs=[out])
    p.glossary = BadGloss()
    r = p.run_text("raw words", Mode.CLEAN)
    assert r.fallback_triggered and r.final_text == "raw words"
    assert out.got[0][0] == "raw words"


def test_all_outputs_failed_sets_error(tmp_path):
    from local_typeless.pipeline import outputs_failed

    p = make(tmp_path, outputs=[Out(fail=True), Out(fail=True)])
    r = p.run_text("hi", Mode.CLEAN)
    assert outputs_failed(r) and r.final_text == "refined"
    ok = make(tmp_path / "y", outputs=[Out(fail=True), Out()]).run_text("hi", Mode.CLEAN)
    assert not outputs_failed(ok) and ok.error_message is None


def test_history_written_before_emit(tmp_path):
    seen = {}

    class Spy(Out):
        def emit(self, text, mode):
            seen["history_exists"] = p.history_path.exists()
            raise RuntimeError("hang then fail")

    p = make(tmp_path, outputs=[Spy()])
    p.run_text("hello", Mode.CLEAN)
    assert seen["history_exists"] is True


def test_history_append_only_until_over_limit_and_tolerates_garbage(tmp_path):
    p = make(tmp_path, history_max=3)
    p.run_text("t0", Mode.CLEAN)
    p.run_text("t1", Mode.CLEAN)
    before = p.history_path.read_bytes()
    p.run_text("t2", Mode.CLEAN)
    assert p.history_path.read_bytes().startswith(before)       # pure append
    with p.history_path.open("ab") as f:
        f.write(b"\xff\xfe not json\n")                       # invalid utf-8 + garbage
    p.run_text("t3", Mode.CLEAN)
    rows = p._read_history()
    assert [r["raw_text"] for r in rows] == ["t1", "t2", "t3"]
    assert not list(p.cache_dir.glob("*.tmp"))


def test_history_failure_never_breaks_dictation(tmp_path):
    out = Out()
    p = make(tmp_path, outputs=[out])
    p.cache_dir = tmp_path / "file_not_dir"
    p.cache_dir.write_text("x")                                 # mkdir/open will fail
    r = p.run_text("hello", Mode.CLEAN)
    assert r.final_text == "refined" and out.got


def test_probe_cuda_usable():
    assert cli.probe_cuda_usable(0)[0] is False

    def ok_loader(name):
        assert name == "cublas64_12.dll"

    def bad_loader(name):
        raise OSError("not found")

    assert cli.probe_cuda_usable(1, ok_loader)[0] is True
    usable, reason = cli.probe_cuda_usable(1, bad_loader)
    assert usable is False and "not found" in reason


def test_cli_check_reports_offline_and_model_availability(monkeypatch, capsys):
    import local_typeless.llm as llm_mod
    from local_typeless.stt import faster_whisper_stt as stt

    class Down:
        def list_models(self):
            raise LLMConnectionError("nope")

    monkeypatch.setattr(llm_mod, "create_llm_client", lambda p: Down())
    cfg = AppConfig(stt_profiles={"cpu_fast": STTProfileConfig()},
                    llm_profiles={"dev": LLMProfileConfig()})
    monkeypatch.setattr(stt, "is_model_available", lambda src: True)
    cli._check(cfg)
    out = capsys.readouterr().out
    assert "Offline mode: on" in out and "STT model available locally: yes" in out
    monkeypatch.setattr(stt, "is_model_available", lambda src: False)
    cli._check(cfg)
    out = capsys.readouterr().out
    assert "STT model available locally: NO" in out and "download-model --stt-profile cpu_fast" in out


def test_cli_download_model_subcommand(monkeypatch, capsys):
    from local_typeless.stt import faster_whisper_stt as stt

    a = cli._build_parser().parse_args(["download-model", "--stt-profile", "cpu_accurate"])
    assert a.command == "download-model"
    assert cli._overrides(a)["stt_profile"] == "cpu_accurate"
    b = cli._build_parser().parse_args(["--model", "tiny", "download-model"])
    assert cli._overrides(b)["model"] == "tiny"

    cfg = AppConfig(stt_profiles={"cpu_fast": STTProfileConfig(model_size="small")},
                    llm_profiles={"dev": LLMProfileConfig()})
    seen = {}
    monkeypatch.setattr(stt, "download_model",
                        lambda size, output_dir=None: seen.update(size=size, out=output_dir) or "C:/hf/small")
    assert cli._download_model(cfg) == 0
    out = capsys.readouterr().out
    assert "Model ready at: C:/hf/small" in out and "HF_ENDPOINT" in out
    assert seen == {"size": "small", "out": None}

    def fail(size, output_dir=None):
        raise OSError("network down")

    monkeypatch.setattr(stt, "download_model", fail)
    assert cli._download_model(cfg) == 1
