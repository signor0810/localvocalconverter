"""Tests for draft core features: Pipeline refine_piece/refine_only/save_draft,
SessionManager draft_mode, and Daemon on_paste_request."""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import List, Optional

import pytest

from local_typeless.config.loader import load_config
from local_typeless.hotkey.daemon import ChordTracker, Daemon, Event, HotkeyStateMachine, State
from local_typeless.output.record_store import RecordStore
from local_typeless.pipeline import NO_SPEECH, Pipeline
from local_typeless.sessions import Job, SessionManager
from local_typeless.types import (
    AppConfig,
    AudioChunk,
    DraftConfig,
    DraftPiece,
    GlossaryProvider,
    HotkeyConfig,
    LLMConnectionError,
    LLMProfileConfig,
    Mode,
    OutputAdapter,
    PipelineResult,
    Segment,
    TranscriptResult,
)


# ---------------------------------------------------------------- helpers / fakes


class FakeSTT:
    def __init__(self, text: str = "OPC recipe"):
        self.text = text
        self.calls: list = []
        self.fail_chunks: set = set()

    def transcribe(self, audio_path, **kwargs):
        self.calls.append((audio_path, kwargs))
        path_str = str(audio_path)
        for fc in self.fail_chunks:
            if fc in path_str:
                raise RuntimeError(f"Simulated STT error for {fc}")
        seg = Segment(start=0.0, end=1.0, text=self.text)
        return TranscriptResult(
            raw_text=self.text,
            duration_sec=1.0,
            stt_elapsed_sec=0.05,
            used_device="cpu",
            used_model="small",
            segments=[seg],
        )


class FakeLLM:
    def __init__(self, response: str = "Cleaned EUV lithography text"):
        self.response = response
        self.calls: list = []
        self.raise_exc: Optional[Exception] = None

    def refine(self, text: str, system_prompt: str) -> str:
        self.calls.append((text, system_prompt))
        if self.raise_exc:
            raise self.raise_exc
        return self.response


class FakeGlossary(GlossaryProvider):
    def build_whisper_initial_prompt(self) -> str:
        return "ILT SMO EUV"

    def build_llm_glossary_section(self) -> str:
        return "- OPC: Optical Proximity Correction"

    def normalize_terms(self, text: str) -> str:
        return text.replace("opc", "OPC")


class FakeOutput(OutputAdapter):
    def __init__(self):
        self.emitted: List[tuple] = []

    def emit(self, text: str, mode: Mode) -> None:
        self.emitted.append((text, mode))


class FakeRecorder:
    def __init__(self, tmp_path: Path):
        self.tmp_path = tmp_path
        self.on_chunk = None
        self._is_rec = False
        self.wav_path = tmp_path / "last_recording.wav"

    @property
    def is_recording(self) -> bool:
        return self._is_rec

    def start(self, on_chunk=None):
        self._is_rec = True
        self.on_chunk = on_chunk

    def stop(self):
        self._is_rec = False
        self.wav_path.write_bytes(b"RIFF\x24\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00\x80>\x00\x00\x00}\x00\x00\x02\x00\x10\x00data\x00\x00\x00\x00")
        return self.wav_path

    def cancel(self):
        self._is_rec = False


class FakeWatcher:
    def __init__(self):
        self.started = False
        self.stopped = False
        self.cancelled = False
        self.images = []

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True
        return []

    def cancel(self):
        self.cancelled = True

    def capture_now(self) -> int:
        return 1


def make_chunk(index: int, start_sec: float, duration_sec: float = 1.0, sample_rate: int = 16000) -> AudioChunk:
    pcm = b"\x00\x00" * int(sample_rate * duration_sec)
    return AudioChunk(index=index, start_sec=start_sec, pcm=pcm, sample_rate=sample_rate)


def create_test_config(tmp_path: Path) -> AppConfig:
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir(parents=True, exist_ok=True)
    (prompts_dir / "clean.md").write_text("System prompt for clean\n{glossary}", encoding="utf-8")
    (prompts_dir / "summary.md").write_text("System prompt for summary\n{glossary}", encoding="utf-8")
    (prompts_dir / "slide.md").write_text("System prompt for slide\n{glossary}", encoding="utf-8")

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    records_dir = tmp_path / "records"
    records_dir.mkdir(parents=True, exist_ok=True)

    cfg = AppConfig(
        prompts_dir=str(prompts_dir),
        cache_dir=str(cache_dir),
        active_llm_profile="dev",
        active_stt_profile="cpu_fast",
        draft=DraftConfig(enabled=True, autosave=True),
    )
    return cfg


# ---------------------------------------------------------------- Config Loader tests


def test_loader_draft_config(tmp_path: Path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        """
active_stt_profile: cpu_fast
active_llm_profile: dev
stt_profiles:
  cpu_fast:
    device: cpu
llm_profiles:
  dev:
    provider: openai_compatible
draft:
  enabled: true
  autosave: false
""",
        encoding="utf-8",
    )
    loaded = load_config(cfg_file)
    assert loaded.draft.enabled is True
    assert loaded.draft.autosave is False


def test_loader_draft_config_defaults(tmp_path: Path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        """
active_stt_profile: cpu_fast
active_llm_profile: dev
stt_profiles:
  cpu_fast:
    device: cpu
llm_profiles:
  dev:
    provider: openai_compatible
""",
        encoding="utf-8",
    )
    loaded = load_config(cfg_file)
    assert loaded.draft.enabled is True
    assert loaded.draft.autosave is True


# ---------------------------------------------------------------- Pipeline refine_piece tests


def test_refine_piece_happy_path(tmp_path: Path):
    cfg = create_test_config(tmp_path)
    llm = FakeLLM(response="Cleaned opc mask recipe")
    pipeline = Pipeline(cfg, llm=llm, stt=FakeSTT(), glossary=FakeGlossary(), outputs=[])

    text, fallback = pipeline.refine_piece("raw opc mask recipe")
    assert text == "Cleaned OPC mask recipe"  # glossary normalize_terms applied
    assert fallback is False
    assert len(llm.calls) == 1


def test_refine_piece_with_prev_raw(tmp_path: Path):
    cfg = create_test_config(tmp_path)
    cfg.refine.context_chars = 10
    llm = FakeLLM(response="EUV source power")
    pipeline = Pipeline(cfg, llm=llm, stt=FakeSTT(), glossary=FakeGlossary(), outputs=[])

    text, fallback = pipeline.refine_piece("source power", prev_raw="illumination EUV")
    assert fallback is False
    assert len(llm.calls) == 1
    sys_prompt = llm.calls[0][1]
    assert "## 前文" in sys_prompt
    assert "nation EUV" in sys_prompt  # last 10 characters of prev_raw


def test_refine_piece_empty_or_whitespace(tmp_path: Path):
    cfg = create_test_config(tmp_path)
    llm = FakeLLM()
    pipeline = Pipeline(cfg, llm=llm, stt=FakeSTT(), glossary=FakeGlossary(), outputs=[])

    text, fallback = pipeline.refine_piece("")
    assert text == ""
    assert fallback is False
    assert len(llm.calls) == 0

    text2, fallback2 = pipeline.refine_piece("   \n\t  ")
    assert text2 == ""
    assert fallback2 is False
    assert len(llm.calls) == 0


def test_refine_piece_llm_failure_fallback_no_loss(tmp_path: Path):
    cfg = create_test_config(tmp_path)
    llm = FakeLLM()
    llm.raise_exc = LLMConnectionError("Connection refused")
    pipeline = Pipeline(cfg, llm=llm, stt=FakeSTT(), glossary=FakeGlossary(), outputs=[])

    text, fallback = pipeline.refine_piece("raw opc input")
    # Must fallback to normalized raw text and not lose words
    assert text == "raw OPC input"
    assert fallback is True


def test_refine_piece_prompt_load_failure(tmp_path: Path):
    cfg = create_test_config(tmp_path)
    cfg.prompts_dir = str(tmp_path / "nonexistent_prompts")
    llm = FakeLLM()
    pipeline = Pipeline(cfg, llm=llm, stt=FakeSTT(), glossary=FakeGlossary(), outputs=[])

    text, fallback = pipeline.refine_piece("raw opc input")
    assert text == "raw OPC input"
    assert fallback is True


def test_refine_piece_degenerate_fallback(tmp_path: Path):
    cfg = create_test_config(tmp_path)
    llm = FakeLLM(response="... [?] ...")
    pipeline = Pipeline(cfg, llm=llm, stt=FakeSTT(), glossary=FakeGlossary(), outputs=[])

    text, fallback = pipeline.refine_piece("SMO optimization")
    assert text == "SMO optimization"
    assert fallback is True


# ---------------------------------------------------------------- Pipeline refine_only tests


def test_refine_only_no_history_no_records_no_emit(tmp_path: Path):
    cfg = create_test_config(tmp_path)
    records = RecordStore(tmp_path / "records", tmp_path / "images")
    output = FakeOutput()
    llm = FakeLLM(response="Refined ILT mask [文字1]")
    pipeline = Pipeline(cfg, llm=llm, stt=FakeSTT(), glossary=FakeGlossary(), outputs=[output], records=records)

    res = pipeline.refine_only("raw ILT mask [文字1]", Mode.CLEAN)
    assert res.final_text == "Refined ILT mask [文字1]"

    # No history written
    history_file = tmp_path / "cache" / "history.jsonl"
    assert not history_file.exists()

    # No record file written
    assert list((tmp_path / "records").glob("**/*.md")) == []

    # No output emit called
    assert output.emitted == []


# ---------------------------------------------------------------- Pipeline save_draft tests


def test_save_draft_empty_text(tmp_path: Path):
    cfg = create_test_config(tmp_path)
    records = RecordStore(tmp_path / "records", tmp_path / "images")
    pipeline = Pipeline(cfg, stt=FakeSTT(), llm=FakeLLM(), glossary=FakeGlossary(), records=records)

    assert pipeline.save_draft("", "raw text") is None
    assert pipeline.save_draft("   ", "raw text") is None
    assert not (tmp_path / "cache" / "history.jsonl").exists()
    assert list((tmp_path / "records").glob("**/*.md")) == []


def test_save_draft_success(tmp_path: Path):
    cfg = create_test_config(tmp_path)
    records = RecordStore(tmp_path / "records", tmp_path / "images")
    pipeline = Pipeline(cfg, stt=FakeSTT(), llm=FakeLLM(), glossary=FakeGlossary(), records=records)

    path = pipeline.save_draft("Final EUV mask pattern", "raw transcript")
    assert path is not None
    assert path.exists()
    content = path.read_text(encoding="utf-8")
    assert "Final EUV mask pattern" in content
    assert "raw transcript" in content
    assert "mode: clean" in content

    # History appended
    history_file = tmp_path / "cache" / "history.jsonl"
    assert history_file.exists()
    assert "Final EUV mask pattern" in history_file.read_text(encoding="utf-8")


# ---------------------------------------------------------------- SessionManager draft_mode tests


def test_session_manager_draft_mode_piece_order_and_timestamps(tmp_path: Path):
    cfg = create_test_config(tmp_path)
    stt = FakeSTT("EUV layer")
    llm = FakeLLM("Refined EUV layer")
    pipeline = Pipeline(cfg, stt=stt, llm=llm, glossary=FakeGlossary(), outputs=[])
    recorder = FakeRecorder(tmp_path)

    pieces: List[DraftPiece] = []
    job_events: List[tuple] = []
    mgr = SessionManager(
        pipeline,
        recorder,
        cache_dir=tmp_path / "cache",
        draft_mode=True,
        on_piece=lambda p: pieces.append(p),
        on_job=lambda j, p: job_events.append((j.id, j.status, p)),
    )
    mgr.start()

    job = mgr.begin(Mode.CLEAN)
    recorder.on_chunk(make_chunk(0, 0.0, 1.0))
    recorder.on_chunk(make_chunk(1, 1.0, 1.5))

    ended = mgr.end()
    assert ended is job
    assert mgr.wait_idle(timeout=5) is True

    assert len(pieces) == 2
    p0, p1 = pieces[0], pieces[1]
    assert p0.seq == 0
    assert p0.start_sec == 0.0
    assert p0.end_sec == 1.0
    assert p0.text == "Refined EUV layer"
    assert p0.fallback is False

    assert p1.seq == 1
    assert p1.start_sec == 1.0
    assert p1.end_sec == 2.5
    assert p1.text == "Refined EUV layer"
    assert p1.fallback is False

    assert job.status == "done"
    mgr.shutdown(timeout=5)


def test_session_manager_draft_mode_prev_raw_context(tmp_path: Path):
    cfg = create_test_config(tmp_path)
    stt = FakeSTT()
    llm = FakeLLM("Clean piece")
    pipeline = Pipeline(cfg, stt=stt, llm=llm, glossary=FakeGlossary(), outputs=[])
    recorder = FakeRecorder(tmp_path)

    pieces: List[DraftPiece] = []
    mgr = SessionManager(
        pipeline,
        recorder,
        cache_dir=tmp_path / "cache",
        draft_mode=True,
        on_piece=lambda p: pieces.append(p),
    )
    mgr.start()

    # Configure STT to return different text per call
    call_count = 0
    def transcribe_side_effect(audio_path, **kwargs):
        nonlocal call_count
        call_count += 1
        txt = f"Chunk_{call_count}"
        return TranscriptResult(
            raw_text=txt,
            duration_sec=1.0,
            stt_elapsed_sec=0.05,
            used_device="cpu",
            used_model="small",
            segments=[Segment(0.0, 1.0, txt)],
        )
    stt.transcribe = transcribe_side_effect

    mgr.begin(Mode.CLEAN)
    recorder.on_chunk(make_chunk(0, 0.0, 1.0))
    recorder.on_chunk(make_chunk(1, 1.0, 1.0))
    mgr.end()
    assert mgr.wait_idle(timeout=5) is True

    assert len(pieces) == 2
    # Verify second LLM call received chunk 1's raw text as prev_raw context
    assert len(llm.calls) == 2
    assert "Chunk_1" in llm.calls[1][1]
    mgr.shutdown(timeout=5)


def test_session_manager_draft_mode_silence_chunk_skipped(tmp_path: Path):
    cfg = create_test_config(tmp_path)
    stt = FakeSTT()
    llm = FakeLLM("Refined")
    pipeline = Pipeline(cfg, stt=stt, llm=llm, glossary=FakeGlossary(), outputs=[])
    recorder = FakeRecorder(tmp_path)

    pieces: List[DraftPiece] = []
    mgr = SessionManager(
        pipeline,
        recorder,
        cache_dir=tmp_path / "cache",
        draft_mode=True,
        on_piece=lambda p: pieces.append(p),
    )
    mgr.start()

    call_count = 0
    def transcribe_side_effect(audio_path, **kwargs):
        nonlocal call_count
        call_count += 1
        txt = "" if call_count == 2 else f"Text_{call_count}"
        return TranscriptResult(
            raw_text=txt,
            duration_sec=1.0,
            stt_elapsed_sec=0.05,
            used_device="cpu",
            used_model="small",
            segments=[Segment(0.0, 1.0, txt)] if txt else [],
        )
    stt.transcribe = transcribe_side_effect

    mgr.begin(Mode.CLEAN)
    recorder.on_chunk(make_chunk(0, 0.0, 1.0))
    recorder.on_chunk(make_chunk(1, 1.0, 1.0))
    recorder.on_chunk(make_chunk(2, 2.0, 1.0))
    mgr.end()
    assert mgr.wait_idle(timeout=5) is True

    # Silence chunk 1 skipped, only 2 pieces delivered
    assert len(pieces) == 2
    assert pieces[0].seq == 0
    assert pieces[0].raw_text == "Text_1"
    assert pieces[1].seq == 1
    assert pieces[1].raw_text == "Text_3"
    mgr.shutdown(timeout=5)


def test_session_manager_draft_mode_failed_chunk_retried_at_end(tmp_path: Path):
    cfg = create_test_config(tmp_path)
    stt = FakeSTT("OPC pattern")

    # Fail job1_1.wav once, then succeed on retry
    fail_once = "job1_1.wav"
    original_transcribe = stt.transcribe

    def transcribe_with_fail(audio_path, **kwargs):
        nonlocal fail_once
        if fail_once and fail_once in str(audio_path):
            fail_once = None
            raise RuntimeError("transient STT error")
        return original_transcribe(audio_path, **kwargs)

    stt.transcribe = transcribe_with_fail
    llm = FakeLLM("Refined OPC pattern")
    pipeline = Pipeline(cfg, stt=stt, llm=llm, glossary=FakeGlossary(), outputs=[])
    recorder = FakeRecorder(tmp_path)

    pieces: List[DraftPiece] = []
    mgr = SessionManager(
        pipeline,
        recorder,
        cache_dir=tmp_path / "cache",
        draft_mode=True,
        on_piece=lambda p: pieces.append(p),
    )
    mgr.start()

    mgr.begin(Mode.CLEAN)
    recorder.on_chunk(make_chunk(0, 0.0, 1.0))
    recorder.on_chunk(make_chunk(1, 1.0, 1.0))  # fails during recording
    recorder.on_chunk(make_chunk(2, 2.0, 1.0))  # succeeds

    # Wait until chunk 2 is processed
    deadline = time.monotonic() + 3.0
    while len(pieces) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)

    mgr.end()
    assert mgr.wait_idle(timeout=5) is True

    # All 3 chunks delivered as pieces
    assert len(pieces) == 3
    # Chunk 0: seq=0, start=0.0
    assert pieces[0].seq == 0
    assert pieces[0].start_sec == 0.0
    # Chunk 2: seq=1, start=2.0
    assert pieces[1].seq == 1
    assert pieces[1].start_sec == 2.0
    # Chunk 1 retried at end: seq=2, original start=1.0, end=2.0
    p_last = pieces[2]
    assert p_last.seq == 2
    assert p_last.start_sec == 1.0
    assert p_last.end_sec == 2.0
    mgr.shutdown(timeout=5)


def test_session_manager_draft_mode_cancel_no_more_pieces(tmp_path: Path):
    cfg = create_test_config(tmp_path)
    stt = FakeSTT("SMO recipe")
    llm = FakeLLM("Refined SMO")
    pipeline = Pipeline(cfg, stt=stt, llm=llm, glossary=FakeGlossary(), outputs=[])
    recorder = FakeRecorder(tmp_path)

    pieces: List[DraftPiece] = []
    mgr = SessionManager(
        pipeline,
        recorder,
        cache_dir=tmp_path / "cache",
        draft_mode=True,
        on_piece=lambda p: pieces.append(p),
    )
    mgr.start()

    mgr.begin(Mode.CLEAN)
    recorder.on_chunk(make_chunk(0, 0.0, 1.0))
    mgr.cancel()
    assert mgr.wait_idle(timeout=5) is True

    # No pieces should be delivered after cancel
    assert len(pieces) == 0
    mgr.shutdown(timeout=5)


def test_session_manager_draft_mode_completion_no_run_text_no_on_result(tmp_path: Path):
    cfg = create_test_config(tmp_path)
    stt = FakeSTT("ILT curve")
    llm = FakeLLM("Refined ILT curve")
    pipeline = Pipeline(cfg, stt=stt, llm=llm, glossary=FakeGlossary(), outputs=[])
    recorder = FakeRecorder(tmp_path)

    results = []
    job_events = []
    mgr = SessionManager(
        pipeline,
        recorder,
        cache_dir=tmp_path / "cache",
        draft_mode=True,
        on_result=lambda j, r: results.append((j, r)),
        on_job=lambda j, p: job_events.append((j.id, j.status, p)),
    )
    mgr.start()

    job = mgr.begin(Mode.CLEAN)
    recorder.on_chunk(make_chunk(0, 0.0, 1.0))
    mgr.end()
    assert mgr.wait_idle(timeout=5) is True

    # Must NOT call on_result
    assert results == []
    # Must transition job to done and call on_job
    assert job.status == "done"
    assert any(ev[1] == "done" for ev in job_events)
    mgr.shutdown(timeout=5)


def test_session_manager_draft_mode_watcher_unused_and_paste_image_zero(tmp_path: Path):
    cfg = create_test_config(tmp_path)
    pipeline = Pipeline(cfg, stt=FakeSTT(), llm=FakeLLM(), glossary=FakeGlossary(), outputs=[])
    recorder = FakeRecorder(tmp_path)
    watcher = FakeWatcher()

    mgr = SessionManager(
        pipeline,
        recorder,
        watcher=watcher,
        cache_dir=tmp_path / "cache",
        draft_mode=True,
    )
    mgr.start()

    mgr.begin(Mode.CLEAN)
    assert watcher.started is False  # watcher not started in draft mode
    assert mgr.paste_image() == 0    # paste_image returns 0

    mgr.end()
    assert watcher.stopped is False
    assert mgr.wait_idle(timeout=5) is True
    mgr.shutdown(timeout=5)


def test_session_manager_draft_mode_retry_last_recording(tmp_path: Path):
    cfg = create_test_config(tmp_path)
    stt = FakeSTT("Retried EUV pattern")
    llm = FakeLLM("Refined retried EUV pattern")
    pipeline = Pipeline(cfg, stt=stt, llm=llm, glossary=FakeGlossary(), outputs=[])
    recorder = FakeRecorder(tmp_path)

    # Write last_recording.wav
    wav_path = (tmp_path / "cache") / "last_recording.wav"
    wav_path.write_bytes(b"RIFF\x24\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00\x80>\x00\x00\x00}\x00\x00\x02\x00\x10\x00data\x00\x00\x00\x00")

    pieces: List[DraftPiece] = []
    mgr = SessionManager(
        pipeline,
        recorder,
        cache_dir=tmp_path / "cache",
        draft_mode=True,
        on_piece=lambda p: pieces.append(p),
    )
    mgr.start()

    job = mgr.retry(Mode.CLEAN)
    assert job is not None
    assert mgr.wait_idle(timeout=5) is True

    assert len(pieces) == 1
    p = pieces[0]
    assert p.seq == 0
    assert p.start_sec == 0.0
    assert p.text == "Refined retried EUV pattern"
    assert job.status == "done"
    mgr.shutdown(timeout=5)


def test_session_manager_draft_mode_false_regression(tmp_path: Path):
    cfg = create_test_config(tmp_path)
    pipeline = Pipeline(cfg, stt=FakeSTT("OPC rule"), llm=FakeLLM("Refined OPC rule"), glossary=FakeGlossary(), outputs=[])
    recorder = FakeRecorder(tmp_path)

    results = []
    mgr = SessionManager(
        pipeline,
        recorder,
        cache_dir=tmp_path / "cache",
        draft_mode=False,
        on_result=lambda j, r: results.append((j, r)),
    )
    mgr.start()

    mgr.begin(Mode.CLEAN)
    recorder.on_chunk(make_chunk(0, 0.0, 1.0))
    mgr.end()
    assert mgr.wait_idle(timeout=5) is True

    # When draft_mode is False, on_result must fire with PipelineResult
    assert len(results) == 1
    res_job, res_result = results[0]
    assert res_job.status == "done"
    assert "OPC rule" in res_result.final_text
    mgr.shutdown(timeout=5)


# ---------------------------------------------------------------- Daemon on_paste_request tests


def test_daemon_on_paste_request_in_idle(tmp_path: Path):
    called = []
    hotkeys = HotkeyConfig()
    recorder = FakeRecorder(tmp_path)
    pipeline = Pipeline(create_test_config(tmp_path), stt=FakeSTT(), llm=FakeLLM(), glossary=FakeGlossary(), outputs=[])

    daemon = Daemon(
        hotkeys,
        recorder,
        pipeline,
        cache_dir=str(tmp_path / "cache"),
        on_paste_request=lambda: called.append(True),
    )
    daemon._worker = threading.Thread(target=daemon._worker_loop, daemon=True)
    daemon._worker.start()

    assert daemon.machine.state == State.IDLE
    daemon.submit(Event("paste_image"))

    deadline = time.monotonic() + 2.0
    while not called and time.monotonic() < deadline:
        time.sleep(0.01)

    assert called == [True]
    daemon._actions.put(Daemon._STOP)
    daemon._worker.join(timeout=2.0)


def test_daemon_on_paste_request_in_recording(tmp_path: Path):
    called = []
    paste_counts = []
    watcher = FakeWatcher()
    hotkeys = HotkeyConfig()
    recorder = FakeRecorder(tmp_path)
    pipeline = Pipeline(create_test_config(tmp_path), stt=FakeSTT(), llm=FakeLLM(), glossary=FakeGlossary(), outputs=[])

    daemon = Daemon(
        hotkeys,
        recorder,
        pipeline,
        cache_dir=str(tmp_path / "cache"),
        image_watcher=watcher,
        on_paste=lambda n: paste_counts.append(n),
        on_paste_request=lambda: called.append("paste_request"),
    )
    daemon._worker = threading.Thread(target=daemon._worker_loop, daemon=True)
    daemon._worker.start()

    daemon.submit(Event("press", Mode.CLEAN))
    assert daemon.machine.state == State.RECORDING

    daemon.submit(Event("paste_image"))

    deadline = time.monotonic() + 2.0
    while not called and time.monotonic() < deadline:
        time.sleep(0.01)

    assert called == ["paste_request"]
    # watcher and on_paste should NOT be called
    assert paste_counts == []

    daemon.submit(Event("press", Mode.CLEAN))
    daemon._actions.put(Daemon._STOP)
    daemon._worker.join(timeout=2.0)


def test_daemon_on_paste_request_exception_handled(tmp_path: Path, caplog):
    def failing_paste_request():
        raise RuntimeError("Clipboard busy error")

    hotkeys = HotkeyConfig()
    recorder = FakeRecorder(tmp_path)
    pipeline = Pipeline(create_test_config(tmp_path), stt=FakeSTT(), llm=FakeLLM(), glossary=FakeGlossary(), outputs=[])

    daemon = Daemon(
        hotkeys,
        recorder,
        pipeline,
        cache_dir=str(tmp_path / "cache"),
        on_paste_request=failing_paste_request,
    )
    daemon._worker = threading.Thread(target=daemon._worker_loop, daemon=True)
    daemon._worker.start()

    with caplog.at_level(logging.WARNING):
        daemon.submit(Event("paste_image"))
        deadline = time.monotonic() + 2.0
        while not daemon._actions.empty() and time.monotonic() < deadline:
            time.sleep(0.01)

    daemon._actions.put(Daemon._STOP)
    daemon._worker.join(timeout=2.0)
    # Check that error was caught and logged without crashing daemon
    assert any("on_paste_request" in r.message for r in caplog.records)


def test_daemon_without_on_paste_request_idle_does_nothing(tmp_path: Path):
    paste_counts = []
    hotkeys = HotkeyConfig()
    recorder = FakeRecorder(tmp_path)
    pipeline = Pipeline(create_test_config(tmp_path), stt=FakeSTT(), llm=FakeLLM(), glossary=FakeGlossary(), outputs=[])

    daemon = Daemon(
        hotkeys,
        recorder,
        pipeline,
        cache_dir=str(tmp_path / "cache"),
        on_paste=lambda n: paste_counts.append(n),
    )
    assert daemon.machine.state == State.IDLE
    daemon.submit(Event("paste_image"))

    # In IDLE, paste_image should not produce any action
    assert daemon._actions.empty()
