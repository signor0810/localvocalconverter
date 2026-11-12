"""STT engine tests (all mocked: no GPU, model download or network)."""
from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from local_typeless.stt import STTEngine, create_stt_engine
from local_typeless.types import STTProfileConfig

MOD = "local_typeless.stt.faster_whisper_stt"


def _segs(*texts):
    return (SimpleNamespace(text=t) for t in texts)


class FakeModel:
    instances: list = []

    def __init__(self, source, **kw):
        self.source = source
        self.kw = kw
        self.calls: list = []
        FakeModel.instances.append(self)

    def transcribe(self, path, **kw):
        self.calls.append(kw)
        return _segs(" hello ", "world "), SimpleNamespace(duration=2.5)


class CudaLazyFailModel(FakeModel):
    """Constructs fine; fails while consuming the lazy generator."""

    def transcribe(self, path, **kw):
        self.calls.append(kw)
        if self.kw["device"] == "cuda":
            def gen():
                raise RuntimeError("Library cublas64_12.dll is not found")
                yield  # pragma: no cover
            return gen(), SimpleNamespace(duration=1.0)
        return super().transcribe(path, **kw)


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    FakeModel.instances = []
    # no HF cache lookups in unit tests: resolution is the identity unless a test overrides it
    monkeypatch.setattr(f"{MOD}._resolve_model", lambda source: source)


def test_factory_returns_engine_and_is_lazy():
    with patch(f"{MOD}._get_whisper_model_class", return_value=FakeModel):
        eng = create_stt_engine(STTProfileConfig())
        assert isinstance(eng, STTEngine)
        assert FakeModel.instances == []


def test_cpu_params_and_prompt(tmp_path):
    with patch(f"{MOD}._get_whisper_model_class", return_value=FakeModel):
        eng = create_stt_engine(
            STTProfileConfig(device="cpu", model_size="small", beam_size=2, cpu_threads=4)
        )
        res = eng.transcribe(tmp_path / "a.wav", initial_prompt="ILT SMO")
    m = FakeModel.instances[0]
    assert m.source == "small"
    assert m.kw == dict(device="cpu", compute_type="int8", cpu_threads=4,
                        local_files_only=True)
    call = m.calls[0]
    assert call["beam_size"] == 2
    assert call["initial_prompt"] == "ILT SMO"
    assert call["vad_filter"] is True
    assert call["vad_parameters"] == dict(min_silence_duration_ms=500)
    assert res.raw_text == "hello world"
    assert res.duration_sec == 2.5
    assert res.used_device == "cpu"
    assert res.used_model == "small"


def test_vad_off_omits_vad_parameters(tmp_path):
    with patch(f"{MOD}._get_whisper_model_class", return_value=FakeModel):
        create_stt_engine(STTProfileConfig(vad_filter=False)).transcribe(tmp_path / "a.wav")
    call = FakeModel.instances[0].calls[0]
    assert call["vad_filter"] is False
    assert "vad_parameters" not in call


def test_model_path_priority(tmp_path):
    d = tmp_path / "model"
    d.mkdir()
    with patch(f"{MOD}._get_whisper_model_class", return_value=FakeModel):
        res = create_stt_engine(STTProfileConfig(model_path=str(d))).transcribe(tmp_path / "a.wav")
    assert FakeModel.instances[0].source == str(d)
    assert res.used_model == str(d)


def test_missing_model_path_uses_size(tmp_path):
    with patch(f"{MOD}._get_whisper_model_class", return_value=FakeModel):
        create_stt_engine(
            STTProfileConfig(model_path=str(tmp_path / "nope"), model_size="medium")
        ).transcribe(tmp_path / "a.wav")
    assert FakeModel.instances[0].source == "medium"


def test_gpu_params_passed(tmp_path):
    with patch(f"{MOD}._get_whisper_model_class", return_value=FakeModel):
        res = create_stt_engine(
            STTProfileConfig(device="cuda", compute_type="int8_float16", model_size="large-v3-turbo")
        ).transcribe(tmp_path / "a.wav")
    assert FakeModel.instances[0].kw["device"] == "cuda"
    assert FakeModel.instances[0].kw["compute_type"] == "int8_float16"
    assert res.used_device == "cuda"


def test_cuda_fallback_on_lazy_generator_error(tmp_path, caplog):
    with patch(f"{MOD}._get_whisper_model_class", return_value=CudaLazyFailModel):
        eng = create_stt_engine(STTProfileConfig(device="cuda", compute_type="int8_float16"))
        with caplog.at_level(logging.WARNING):
            res = eng.transcribe(tmp_path / "a.wav")
        assert res.raw_text == "hello world"
        assert res.used_device == "cpu (fallback)"
        assert any("falling back" in r.message for r in caplog.records)
        rebuilt = FakeModel.instances[-1]
        assert rebuilt.kw["device"] == "cpu" and rebuilt.kw["compute_type"] == "int8"
        # stays on cpu afterwards: no further cuda model construction
        n = len(FakeModel.instances)
        res2 = eng.transcribe(tmp_path / "b.wav")
        assert res2.used_device == "cpu (fallback)"
        assert len(FakeModel.instances) == n


@pytest.mark.parametrize("exc", [RuntimeError("cuda init"), OSError("dll missing")])
def test_cuda_fallback_on_construction_error(tmp_path, exc):
    class Boom(FakeModel):
        def __init__(self, source, **kw):
            if kw["device"] != "cpu":
                raise exc
            super().__init__(source, **kw)

    with patch(f"{MOD}._get_whisper_model_class", return_value=Boom):
        res = create_stt_engine(STTProfileConfig(device="auto")).transcribe(tmp_path / "a.wav")
    assert res.used_device == "cpu (fallback)"
    assert FakeModel.instances[-1].kw["device"] == "cpu"


def test_cpu_error_is_not_swallowed(tmp_path):
    class Boom(FakeModel):
        def transcribe(self, path, **kw):
            raise RuntimeError("real failure")

    with patch(f"{MOD}._get_whisper_model_class", return_value=Boom):
        with pytest.raises(RuntimeError):
            create_stt_engine(STTProfileConfig(device="cpu")).transcribe(tmp_path / "a.wav")


def test_cuda_fallback_on_value_error(tmp_path):
    class Boom(FakeModel):
        def __init__(self, source, **kw):
            if kw["device"] != "cpu":
                raise ValueError("Requested float16 compute type unsupported")
            super().__init__(source, **kw)

    with patch(f"{MOD}._get_whisper_model_class", return_value=Boom):
        res = create_stt_engine(STTProfileConfig(device="cuda")).transcribe(tmp_path / "a.wav")
    assert res.used_device == "cpu (fallback)"


def test_register_nvidia_dll_dirs(tmp_path, monkeypatch):
    import os
    import sys
    from pathlib import Path

    import local_typeless.stt.faster_whisper_stt as m

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "prefix", str(tmp_path))
    monkeypatch.setattr(m, "_DLL_REGISTERED", False)
    monkeypatch.setattr(m, "_DLL_HANDLES", [])
    added = []
    monkeypatch.setattr(m.os, "add_dll_directory", lambda d: added.append(d) or object(),
                        raising=False)
    monkeypatch.setenv("PATH", "ORIG")
    assert m._register_nvidia_dll_dirs() == []      # nothing installed -> no-op
    assert added == []
    monkeypatch.setattr(m, "_DLL_REGISTERED", False)
    base = tmp_path / "Lib" / "site-packages" / "nvidia"
    for n in ("cublas", "cudnn"):
        (base / n / "bin").mkdir(parents=True)
    found = m._register_nvidia_dll_dirs()
    assert [Path(f).parent.name for f in found] == ["cublas", "cudnn"]
    assert added == found
    assert os.environ["PATH"].startswith(found[0]) and os.environ["PATH"].endswith("ORIG")


def test_temperature_list_passed(tmp_path):
    with patch(f"{MOD}._get_whisper_model_class", return_value=FakeModel):
        create_stt_engine(STTProfileConfig()).transcribe(tmp_path / "a.wav")
    call = FakeModel.instances[0].calls[0]
    assert call["temperature"] == [0.0]
    assert call["repetition_penalty"] == 1.1
    assert call["no_repeat_ngram_size"] == 6
    assert "condition_on_previous_text" not in call      # keep faster-whisper default (True)


def test_on_segment_gets_cumulative_text_and_errors_are_ignored(tmp_path):
    seen = []

    def cb(text):
        seen.append(text)
        raise RuntimeError("ui exploded")

    with patch(f"{MOD}._get_whisper_model_class", return_value=FakeModel):
        res = create_stt_engine(STTProfileConfig()).transcribe(tmp_path / "a.wav", on_segment=cb)
    assert seen == ["hello", "hello world"]
    assert res.raw_text == "hello world"


# ------------------------------------------------------------ offline runtime
def test_runtime_is_offline_by_default():
    import os

    import local_typeless.stt.faster_whisper_stt  # noqa: F401  (import enforces it)

    assert os.environ["HF_HUB_OFFLINE"] == "1"
    assert os.environ["HF_HUB_DISABLE_TELEMETRY"] == "1"


def test_enforce_offline_respects_explicit_override(monkeypatch):
    import os

    import local_typeless.stt.faster_whisper_stt as m

    monkeypatch.setenv("HF_HUB_OFFLINE", "0")
    monkeypatch.delenv("HF_HUB_DISABLE_TELEMETRY", raising=False)
    m._enforce_offline()
    assert os.environ["HF_HUB_OFFLINE"] == "0"        # setdefault: download-model can opt out
    assert os.environ["HF_HUB_DISABLE_TELEMETRY"] == "1"


def test_model_gets_resolved_local_path_and_local_files_only(tmp_path, monkeypatch):
    monkeypatch.setattr(f"{MOD}._resolve_model", lambda source: f"/cache/{source}")
    with patch(f"{MOD}._get_whisper_model_class", return_value=FakeModel):
        res = create_stt_engine(STTProfileConfig(model_size="small")).transcribe(tmp_path / "a.wav")
    m = FakeModel.instances[0]
    assert m.source == "/cache/small" and m.kw["local_files_only"] is True
    assert res.used_model == "small"


@pytest.mark.parametrize("device", ["cpu", "cuda", "auto"])
def test_missing_model_raises_clear_error_without_cpu_fallback(tmp_path, monkeypatch, device):
    from local_typeless.stt.faster_whisper_stt import ModelNotAvailableError

    def missing(source):
        raise FileNotFoundError("LocalEntryNotFoundError: not in cache")   # an OSError/ValueError

    monkeypatch.setattr(f"{MOD}._resolve_model", missing)
    with patch(f"{MOD}._get_whisper_model_class", return_value=FakeModel):
        eng = create_stt_engine(STTProfileConfig(device=device), "cpu_fast")
        with pytest.raises(ModelNotAvailableError) as ei:
            eng.transcribe(tmp_path / "a.wav")
        msg = str(ei.value)
        assert "STT 模型尚未下載" in msg
        assert "python -m local_typeless.cli download-model --stt-profile cpu_fast" in msg
        assert "不會傳送任何音訊" in msg
        assert eng.used_device == device                    # no "(fallback)"
        assert FakeModel.instances == []                    # never constructed, never rebuilt


def test_missing_model_hint_without_profile_name_uses_model_flag(tmp_path, monkeypatch):
    from local_typeless.stt.faster_whisper_stt import ModelNotAvailableError

    monkeypatch.setattr(f"{MOD}._resolve_model", lambda s: (_ for _ in ()).throw(ValueError("x")))
    with patch(f"{MOD}._get_whisper_model_class", return_value=FakeModel):
        with pytest.raises(ModelNotAvailableError, match="download-model --model large-v3-turbo"):
            create_stt_engine(STTProfileConfig(model_size="large-v3-turbo")).transcribe(
                tmp_path / "a.wav")


def _fake_utils(monkeypatch, fn):
    import sys
    import types

    mod = types.ModuleType("faster_whisper.utils")
    mod.download_model = fn
    monkeypatch.setitem(sys.modules, "faster_whisper.utils", mod)


def test_resolve_is_local_only_and_is_model_available(monkeypatch, tmp_path):
    import local_typeless.stt.faster_whisper_stt as m

    monkeypatch.undo()                                       # drop the identity patch
    seen = {}

    def fake_dl(source, output_dir=None, local_files_only=False, **kw):
        seen.update(source=source, local=local_files_only)
        if source == "absent":
            raise FileNotFoundError("nope")
        return f"/cache/{source}"

    _fake_utils(monkeypatch, fake_dl)
    assert m._resolve_model("small") == "/cache/small" and seen["local"] is True
    assert m.is_model_available("small") is True
    assert m.is_model_available("absent") is False
    d = tmp_path / "mdl"
    d.mkdir()
    assert m._resolve_model(str(d)) == str(d)                # local dir: no lookup at all


def test_download_model_allows_network_only_while_downloading(monkeypatch):
    import os

    import local_typeless.stt.faster_whisper_stt as m

    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    seen = {}

    def fake_dl(source, output_dir=None, local_files_only=False, **kw):
        seen.update(env=os.environ["HF_HUB_OFFLINE"], local=local_files_only, out=output_dir)
        return "/downloaded/small"

    _fake_utils(monkeypatch, fake_dl)
    assert m.download_model("small", output_dir="D:/models/small") == "/downloaded/small"
    assert seen == {"env": "0", "local": False, "out": "D:/models/small"}
    assert os.environ["HF_HUB_OFFLINE"] == "1"               # offline restored


def test_download_model_restores_offline_on_failure(monkeypatch):
    import os

    import local_typeless.stt.faster_whisper_stt as m

    monkeypatch.setenv("HF_HUB_OFFLINE", "1")

    def boom(*a, **k):
        raise OSError("network down")

    _fake_utils(monkeypatch, boom)
    with pytest.raises(OSError):
        m.download_model("small")
    assert os.environ["HF_HUB_OFFLINE"] == "1"


# ------------------------------------------------------------ timestamps & segments
def test_segments_returned_with_start_end_text(tmp_path):
    class SegModel(FakeModel):
        def transcribe(self, path, **kw):
            self.calls.append(kw)
            segs = [
                SimpleNamespace(start=0.5, end=1.5, text=" hello "),
                SimpleNamespace(start=1.5, end=2.5, text="world "),
            ]
            return segs, SimpleNamespace(duration=2.5)

    with patch(f"{MOD}._get_whisper_model_class", return_value=SegModel):
        eng = create_stt_engine(STTProfileConfig())
        res = eng.transcribe(tmp_path / "a.wav")
    assert len(res.segments) == 2
    assert res.segments[0].start == 0.5
    assert res.segments[0].end == 1.5
    assert res.segments[0].text == " hello "
    assert res.segments[0].words == ()
    assert res.segments[1].start == 1.5
    assert res.segments[1].end == 2.5
    assert res.segments[1].text == "world "


def test_word_timestamps_passed_and_words_mapped(tmp_path):
    class WordModel(FakeModel):
        def transcribe(self, path, **kw):
            self.calls.append(kw)
            words = [
                SimpleNamespace(start=0.5, end=1.0, word=" hello"),
                SimpleNamespace(start=1.0, end=1.5, word=" world"),
            ]
            seg = SimpleNamespace(start=0.5, end=1.5, text=" hello world", words=words)
            return [seg], SimpleNamespace(duration=1.5)

    with patch(f"{MOD}._get_whisper_model_class", return_value=WordModel):
        eng = create_stt_engine(STTProfileConfig())
        res = eng.transcribe(tmp_path / "a.wav", word_timestamps=True)
    assert FakeModel.instances[0].calls[0]["word_timestamps"] is True
    assert len(res.segments) == 1
    assert len(res.segments[0].words) == 2
    w1, w2 = res.segments[0].words
    assert w1.start == 0.5 and w1.end == 1.0 and w1.text == " hello"
    assert w2.start == 1.0 and w2.end == 1.5 and w2.text == " world"


def test_default_call_does_not_pass_word_timestamps(tmp_path):
    with patch(f"{MOD}._get_whisper_model_class", return_value=FakeModel):
        eng = create_stt_engine(STTProfileConfig())
        eng.transcribe(tmp_path / "a.wav")
    assert "word_timestamps" not in FakeModel.instances[0].calls[0]


def test_cuda_fallback_path_still_returns_segments(tmp_path):
    with patch(f"{MOD}._get_whisper_model_class", return_value=CudaLazyFailModel):
        eng = create_stt_engine(STTProfileConfig(device="cuda", compute_type="int8_float16"))
        res = eng.transcribe(tmp_path / "a.wav")
    assert res.used_device == "cpu (fallback)"
    assert len(res.segments) == 2
    assert res.segments[0].text == " hello "
    assert res.segments[1].text == "world "

