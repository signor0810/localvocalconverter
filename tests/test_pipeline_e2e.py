"""End-to-End Pipeline integration tests and Eval Benchmark tests (WP3-B)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional, Tuple

import pytest

from local_typeless.config import load_config
from local_typeless.glossary import GlossaryManager
from local_typeless.llm import create_llm_client
from local_typeless.llm.base import LLMClient
from local_typeless.llm.openai_compat import OpenAICompatibleClient
from local_typeless.llm.gateway_adapter import GatewayAdapter
from local_typeless.pipeline import Pipeline
from local_typeless.stt.base import STTEngine
from local_typeless.types import (
    AppConfig,
    LLMConnectionError,
    LLMError,
    LLMTimeoutError,
    Mode,
    OutputAdapter,
    TranscriptResult,
)
from scripts.eval_benchmark import (
    EvalFileResult,
    ProfileSummary,
    compute_term_recall,
    render_markdown_report,
)


class RecordingOutputAdapter(OutputAdapter):
    """Output adapter that captures all emit calls in memory for test assertions."""

    def __init__(self) -> None:
        self.calls: List[Tuple[str, Mode]] = []

    def emit(self, text: str, mode: Mode) -> None:
        self.calls.append((text, mode))


class MockSTTEngine(STTEngine):
    """Fake STTEngine for testing without faster-whisper or audio hardware."""

    def __init__(self, transcript: str = "test audio transcript", duration: float = 2.0) -> None:
        self.transcript = transcript
        self.duration = duration
        self.calls: List[Tuple[Path, Optional[str]]] = []

    def transcribe(
        self, audio_path: Path, initial_prompt: Optional[str] = None
    ) -> TranscriptResult:
        self.calls.append((audio_path, initial_prompt))
        return TranscriptResult(
            raw_text=self.transcript,
            duration_sec=self.duration,
            stt_elapsed_sec=0.25,
            used_device="cpu",
            used_model="mock-small",
        )


class MockLLMClient(LLMClient):
    """Fake LLMClient implementing both refine and list_models abstract methods."""

    def __init__(self, response: str = "Refined text", error: Optional[Exception] = None) -> None:
        self.response = response
        self.error = error
        self.calls: List[Tuple[str, str]] = []

    def refine(self, raw_text: str, system_prompt: str) -> str:
        self.calls.append((raw_text, system_prompt))
        if self.error:
            raise self.error
        return self.response

    def list_models(self) -> List[str]:
        return ["mock-model-1", "mock-model-2"]


def _build_test_pipeline(
    tmp_path: Path,
    stt: STTEngine,
    llm: LLMClient,
    output: RecordingOutputAdapter,
) -> Pipeline:
    """Helper to construct Pipeline with real config, glossary, and prompts."""
    cfg = load_config(overrides={"cache_dir": str(tmp_path / ".cache")})
    cfg.cache_dir = str(tmp_path / ".cache")
    glossary = GlossaryManager.from_yaml(Path("config/glossary.yaml"))

    return Pipeline(
        config=cfg,
        stt=stt,
        llm=llm,
        glossary=glossary,
        outputs=[output],
    )


def test_pipeline_normal_clean_and_slide_flow(tmp_path: Path):
    """Case 1: Verify normal clean and slide flow with real prompt and glossary."""
    out = RecordingOutputAdapter()
    stt = MockSTTEngine(transcript="今天探討 ilt 與 smo 最佳化")
    llm = MockLLMClient(response="今天探討 ILT 與 SMO 最佳化。")
    pipeline = _build_test_pipeline(tmp_path, stt, llm, out)

    audio_file = tmp_path / "sample.wav"
    audio_file.write_bytes(b"RIFFdummyWAVdata")

    # 1. Clean mode
    res_clean = pipeline.run_file(audio_file, mode=Mode.CLEAN)
    assert not res_clean.fallback_triggered
    assert res_clean.final_text == "今天探討 ILT 與 SMO 最佳化。"
    assert len(out.calls) == 1
    assert out.calls[0] == ("今天探討 ILT 與 SMO 最佳化。", Mode.CLEAN)

    # 2. Slide mode
    llm.response = "## ILT & SMO 最佳化\n- 最佳化流程分析"
    res_slide = pipeline.run_file(audio_file, mode=Mode.SLIDE)
    assert not res_slide.fallback_triggered
    assert "## ILT & SMO 最佳化" in res_slide.final_text
    assert len(out.calls) == 2
    assert out.calls[1][1] == Mode.SLIDE


@pytest.mark.parametrize("exc", [
    LLMTimeoutError("DGX Gateway timeout after 15s"),
    LLMConnectionError("Connection refused to DGX Qwen"),
])
def test_pipeline_llm_failure_fallback_to_normalized_raw(tmp_path: Path, exc: LLMError):
    """Case 2: LLM failure triggers fallback_triggered=True and emits normalized raw text."""
    out = RecordingOutputAdapter()
    stt = MockSTTEngine(transcript="use ilt and smo for curvilinear mask optimization")
    llm = MockLLMClient(error=exc)
    pipeline = _build_test_pipeline(tmp_path, stt, llm, out)

    audio_file = tmp_path / "sample.wav"
    audio_file.write_bytes(b"RIFFdummyWAVdata")

    res = pipeline.run_file(audio_file, mode=Mode.CLEAN)
    assert res.fallback_triggered is True
    assert res.error_message == str(exc)
    # The raw text 'use ilt and smo...' must be normalized to uppercase 'use ILT and SMO...'
    assert res.final_text == "use ILT and SMO for curvilinear mask optimization"
    assert len(out.calls) == 1
    assert out.calls[0][0] == "use ILT and SMO for curvilinear mask optimization"


def test_pipeline_retry_last_reruns_wav(tmp_path: Path):
    """Case 3: retry_last re-runs STT + LLM from last_recording.wav."""
    out = RecordingOutputAdapter()
    stt = MockSTTEngine(transcript="retry audio transcript")
    llm = MockLLMClient(response="Refined retry transcript")
    pipeline = _build_test_pipeline(tmp_path, stt, llm, out)

    cache_dir = Path(pipeline.cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    last_wav = cache_dir / "last_recording.wav"
    last_wav.write_bytes(b"RIFFwavdata")

    res = pipeline.retry_last(mode=Mode.CLEAN)
    assert res.final_text == "Refined retry transcript"
    assert len(stt.calls) == 1
    assert stt.calls[0][0] == last_wav


def test_pipeline_retry_last_llm_only_uses_history(tmp_path: Path):
    """Case 4: retry_last(llm_only=True) reuses raw_text from history without STT."""
    out = RecordingOutputAdapter()
    stt = MockSTTEngine(transcript="Initial speech")
    llm = MockLLMClient(response="Refined version 1")
    pipeline = _build_test_pipeline(tmp_path, stt, llm, out)

    # First run to populate history
    pipeline.run_text("Recorded high-na test", mode=Mode.CLEAN)
    assert len(stt.calls) == 0  # run_text skips STT

    # LLM-only retry with a new mode/response
    llm.response = "## High NA Test\n- Bullet point"
    retry_res = pipeline.retry_last(mode=Mode.SLIDE, llm_only=True)
    assert retry_res.final_text == "## High NA Test\n- Bullet point"
    assert retry_res.raw_text == "Recorded high-na test"
    # Still no STT call made
    assert len(stt.calls) == 0


def test_dev_corp_profile_switch_zero_code_change():
    """Case 5: dev <-> corp switch via load_config overrides selects gateway vs openai client."""
    # Dev profile -> OpenAICompatibleClient
    cfg_dev = load_config(overrides={"llm_profile": "dev"})
    assert cfg_dev.active_llm_profile == "dev"
    client_dev = create_llm_client(cfg_dev.llm)
    assert isinstance(client_dev, OpenAICompatibleClient)

    # Corp profile -> GatewayAdapter
    cfg_corp = load_config(overrides={"llm_profile": "corp"})
    assert cfg_corp.active_llm_profile == "corp"
    client_corp = create_llm_client(cfg_corp.llm)
    assert isinstance(client_corp, GatewayAdapter)


def test_pipeline_empty_transcript_nothing_emitted(tmp_path: Path):
    """Case 6: Empty or whitespace-only transcript emits nothing."""
    out = RecordingOutputAdapter()
    stt = MockSTTEngine(transcript="   \n\t  ")
    llm = MockLLMClient(response="Should not be reached")
    pipeline = _build_test_pipeline(tmp_path, stt, llm, out)

    audio_file = tmp_path / "empty.wav"
    audio_file.write_bytes(b"RIFFemptyWAVdata")

    res = pipeline.run_file(audio_file, mode=Mode.CLEAN)
    assert res.final_text == ""
    assert res.error_message == "no speech detected"
    # Nothing was emitted to outputs
    assert len(out.calls) == 0
    assert len(llm.calls) == 0


def test_eval_benchmark_recall_and_report():
    """Unit test for eval_benchmark compute_term_recall and render_markdown_report."""
    text = "We evaluate ILT mask optimization under High NA EUV conditions."
    expected = ["ILT", "High NA", "EUV", "SMO"]
    recall, hits, total = compute_term_recall(text, expected)
    assert recall == 0.75
    assert hits == 3
    assert total == 4

    # Empty expected terms
    assert compute_term_recall(text, []) == (1.0, 0, 0)

    # Markdown table rendering
    res = [
        EvalFileResult(
            filename="test1.wav",
            profile="cpu_fast",
            duration_sec=5.0,
            stt_sec=1.0,
            llm_sec=0.5,
            total_sec=1.5,
            recall=0.75,
            hit_count=3,
            total_terms=4,
            used_device="cpu",
            rtf=0.2,
        )
    ]
    summaries = [
        ProfileSummary(
            profile="cpu_fast",
            file_count=1,
            mean_stt_sec=1.0,
            mean_total_sec=1.5,
            mean_recall=0.75,
            mean_rtf=0.2,
            error_count=0,
        )
    ]

    report = render_markdown_report(res, summaries, with_llm=True)
    assert "# Benchmark Evaluation Report" in report
    assert "cpu_fast" in report
    assert "75.0%" in report
    assert "test1.wav" in report
