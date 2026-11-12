"""Abandon/retry/live-progress/diagnostics tests (no Tk window, no audio, no network)."""
from __future__ import annotations

import logging
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from local_typeless.hotkey.daemon import Daemon, Event, State
from local_typeless.pipeline import Pipeline, is_degenerate
from local_typeless.types import (
    AppConfig,
    HotkeyConfig,
    LLMProfileConfig,
    Mode,
    STTProfileConfig,
    TranscriptResult,
)
from local_typeless.ui import diagnostics
from local_typeless.ui.app import WidgetCache
from local_typeless.ui.controller import PanelController

P = lambda m: Event("press", m)  # noqa: E731


def res(mode=Mode.SUMMARY, text="final"):
    return SimpleNamespace(raw_text="raw", final_text=text, mode=mode, fallback_triggered=False,
                           error_message=None, stt_elapsed_sec=1.0, llm_elapsed_sec=1.0)


class Rec:
    def __init__(self):
        self.calls, self.is_recording = [], False

    def start(self):
        self.calls.append("start")
        self.is_recording = True

    def stop(self):
        self.calls.append("stop")
        self.is_recording = False
        return "x.wav"

    def cancel(self):
        self.calls.append("cancel")
        self.is_recording = False


class Pipe:
    def __init__(self, gate=None):
        self.gate, self.calls = gate, []

    def run_file(self, path, mode, on_segment=None):
        self.calls.append(("run_file", mode))
        if on_segment:
            on_segment("partial")
        if self.gate:
            self.gate.wait(5)
        if on_segment:
            on_segment("partial more")
        return res(mode)

    def retry_last(self, mode, llm_only=False, on_segment=None):
        self.calls.append(("retry_last", mode))
        if on_segment:
            on_segment("retry text")
        return res(mode, "retried")


def make(gate=None, **kw):
    rec, pipe = Rec(), Pipe(gate)
    states, results, segs = [], [], []
    d = Daemon(HotkeyConfig(), rec, pipe, out=lambda m: None,
               on_state=lambda s, m, n: states.append((s, n)),
               on_result=results.append, on_segment=segs.append, **kw)
    d.start(listen=False)
    return d, rec, pipe, states, results, segs


def record_and_stop(d):
    d.submit(P(Mode.SUMMARY))
    d.submit(P(Mode.SUMMARY))


def wait_for(cond, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


# ------------------------------------------------------------ live segments
def test_segments_forwarded_while_processing():
    d, _, _, _, results, segs = make()
    record_and_stop(d)
    d.wait_idle()
    assert segs == ["partial", "partial more"] and len(results) == 1
    d.shutdown()


# ------------------------------------------------------------------ abandon
def test_abandon_returns_to_idle_ignores_result_and_refuses_until_done():
    gate = threading.Event()
    d, rec, pipe, states, results, segs = make(gate)
    assert d.abandon() is False                       # nothing running
    record_and_stop(d)
    assert wait_for(lambda: "partial" in segs)
    assert d.machine.state is State.PROCESSING
    assert d.abandon() is True
    assert d.machine.state is State.IDLE
    assert states[-1] == (State.IDLE, "abandoned")

    d.submit(P(Mode.CLEAN))                           # refused while zombie still runs
    assert d.machine.state is State.IDLE and rec.calls == ["start", "stop"]
    assert states[-1][1].startswith("refused")
    assert d.request_retry(Mode.CLEAN) is False

    gate.set()
    assert wait_for(lambda: d._zombie_job is None)
    d.wait_idle()
    assert results == [] and "partial more" not in segs      # result + late segments ignored
    assert d.machine.state is State.IDLE

    d.submit(P(Mode.CLEAN))                           # new work accepted again
    d.wait_idle()
    assert d.machine.state is State.RECORDING
    d.shutdown()


def test_abandon_not_possible_after_result_delivered():
    d, *_ = make()
    record_and_stop(d)
    d.wait_idle()
    assert d.abandon() is False
    d.shutdown()


# -------------------------------------------------------------------- retry
def test_request_retry_runs_retry_last_with_mode():
    d, rec, pipe, states, results, segs = make()
    assert d.request_retry(Mode.SLIDE) is True
    d.wait_idle()
    assert pipe.calls == [("retry_last", Mode.SLIDE)]
    assert results[0].final_text == "retried" and segs == ["retry text"]
    assert d.machine.state is State.IDLE
    d.shutdown()


def test_retry_refused_when_busy():
    gate = threading.Event()
    d, *_ = make(gate)
    d.submit(P(Mode.SUMMARY))
    assert d.request_retry(Mode.CLEAN) is False       # RECORDING
    d.submit(P(Mode.SUMMARY))
    assert d.request_retry(Mode.CLEAN) is False       # PROCESSING
    gate.set()
    d.wait_idle()
    d.shutdown()


# --------------------------------------------------------------- controller
def ctl(**kw):
    events = []
    c = PanelController(submit=events.append, paster=SimpleNamespace(emit=lambda t, m: None),
                        copier=lambda t: None, run_async=lambda f: f(), clock=lambda: 50.0, **kw)
    return c, events


def test_live_text_clears_at_start_and_first_segment_ends_model_loading():
    c, _ = ctl()
    c.on_result(res())                                   # earlier result on screen
    c._model_loaded = False
    c.on_state(State.PROCESSING, Mode.SUMMARY, "")
    assert c.vm.raw_text == "" and c.vm.final_text == "" and c.vm.loading_model
    assert c.vm.processing_started == 50.0
    c.on_segment("hello")
    assert c.vm.raw_text == "hello" and not c.vm.loading_model
    c.on_segment("hello world")
    assert c.vm.raw_text == "hello world"
    c.on_state(State.IDLE, Mode.SUMMARY, "done: x")
    c.on_segment("late")                                 # ignored when not processing
    assert c.vm.raw_text == "hello world"


def test_abandon_and_retry_buttons():
    calls = []
    c, _ = ctl(abandon=lambda: calls.append("abandon") or True,
               retry=lambda mode: calls.append(("retry", mode)) or True, cache_dir=".cache")
    c.on_result(res(text="older"))
    c.on_state(State.PROCESSING, Mode.SUMMARY, "")
    c.retry_clicked()                                    # not while processing
    c.abandon_clicked()
    assert calls == ["abandon"]
    c.on_state(State.IDLE, None, "abandoned")
    assert c.vm.message == "已放棄；錄音保存在 .cache/last_recording.wav，可按「重試上一段」"
    assert c.vm.final_text == "older"                    # previous result restored
    c.set_mode(Mode.SLIDE)
    c.retry_clicked()
    assert calls[-1] == ("retry", Mode.SLIDE)


def test_refused_message():
    c, _ = ctl()
    c.on_state(State.IDLE, None, "refused: previous job still running in background")
    assert "背景處理" in c.vm.message


# ----------------------------------------------------------------- pipeline
def test_is_degenerate():
    for t in ["", "  ", "[?]", "[?] [?]", "...", "。", " — ", "[?]。\n"]:
        assert is_degenerate(t), t
    for t in ["a", "你好", "[?]x", "3"]:
        assert not is_degenerate(t), t


class _STT:
    def __init__(self):
        self.kw = None

    def transcribe(self, audio_path, initial_prompt=None, on_segment=None):
        self.kw = on_segment
        if on_segment:
            on_segment("live")
        return TranscriptResult("raw words", 1.0, 0.1, "cpu", "small")


class _Gloss:
    def build_whisper_initial_prompt(self):
        return ""

    def build_llm_glossary_section(self):
        return ""

    def normalize_terms(self, text):
        return text


def _pipe(tmp_path, llm_text, stt=None):
    cfg = AppConfig(stt_profiles={"cpu_fast": STTProfileConfig()},
                    llm_profiles={"dev": LLMProfileConfig()},
                    prompts_dir=str(__import__("pathlib").Path(__file__).resolve().parent.parent / "prompts"),
                    cache_dir=str(tmp_path))
    llm = SimpleNamespace(refine=lambda raw, sys_p: llm_text)
    return Pipeline(cfg, stt=stt or _STT(), llm=llm, glossary=_Gloss(), outputs=[])


@pytest.mark.parametrize("reply", ["[?]", "  ", "...", "[?] 。"])
def test_degenerate_llm_reply_falls_back_to_raw(tmp_path, reply):
    r = _pipe(tmp_path, reply).run_text("raw words", Mode.CLEAN)
    assert r.fallback_triggered and r.final_text == "raw words"
    assert "degenerate" in r.error_message


def test_run_file_forwards_on_segment_and_logs(tmp_path, caplog):
    stt = _STT()
    seen = []
    with caplog.at_level(logging.INFO):
        r = _pipe(tmp_path, "refined text", stt).run_file(tmp_path / "a.wav", Mode.CLEAN, seen.append)
    assert seen == ["live"] and not r.fallback_triggered
    text = caplog.text
    assert "STT start" in text and "STT done" in text and "LLM start" in text and "LLM done" in text


def test_retry_last_forwards_on_segment(tmp_path):
    (tmp_path / "last_recording.wav").write_bytes(b"x")
    seen = []
    _pipe(tmp_path, "ok text").retry_last(Mode.CLEAN, on_segment=seen.append)
    assert seen == ["live"]


# -------------------------------------------------------------- diagnostics
@pytest.fixture
def restore_logging():
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    hooks = (sys.excepthook, threading.excepthook)
    yield
    for h in list(root.handlers):
        h.close()
        root.removeHandler(h)
    for h in handlers:
        root.addHandler(h)
    root.setLevel(level)
    sys.excepthook, threading.excepthook = hooks


def test_setup_logging_writes_thread_names(tmp_path, restore_logging):
    path = diagnostics.setup_logging(tmp_path / "c")
    t = threading.Thread(target=lambda: logging.getLogger("x").info("from worker"),
                         name="my-worker")
    t.start()
    t.join()
    for h in logging.getLogger().handlers:
        h.flush()
    assert path.name == "ui.log"
    text = path.read_text(encoding="utf-8")
    assert "[my-worker]" in text and "from worker" in text


def test_dump_all_threads_names_threads():
    stop = threading.Event()
    t = threading.Thread(target=stop.wait, name="stuck-stt", daemon=True)
    t.start()
    try:
        assert "stuck-stt" in diagnostics.dump_all_threads()
    finally:
        stop.set()


def test_watchdog_repeats_while_armed_and_stops_on_disarm():
    dumps = []
    w = diagnostics.ProcessingWatchdog(interval=0.05, dump=lambda: dumps.append(1) or "stacks")
    w.arm()
    w.arm()                                  # idempotent
    assert wait_for(lambda: len(dumps) >= 2)
    w.disarm()
    n = len(dumps)
    time.sleep(0.2)
    assert len(dumps) in (n, n + 1) and not w.armed
    w.arm()
    w.disarm()


def test_watchdog_silent_when_disarmed_in_time():
    dumps = []
    w = diagnostics.ProcessingWatchdog(interval=0.3, dump=lambda: dumps.append(1) or "")
    w.arm()
    w.disarm()
    time.sleep(0.5)
    assert dumps == []


# -------------------------------------------------------------- widget cache
def test_widget_cache_only_touches_changed_options():
    class W:
        def __init__(self):
            self.calls = []

        def configure(self, **kw):
            self.calls.append(kw)

    w, c = W(), WidgetCache()
    assert c.configure(w, text="a", state="normal") is True
    assert c.configure(w, text="a", state="normal") is False
    assert c.configure(w, text="b", state="normal") is True
    assert w.calls == [{"text": "a", "state": "normal"}, {"text": "b"}]


# ------------------------------------------------------------ streaming e2e
def test_streaming_e2e_integration(tmp_path):
    from local_typeless.sessions import SessionManager
    from local_typeless.types import AudioChunk, PipelineResult, Segment

    class ChunkRecorder:
        def __init__(self):
            self.on_chunk = None
            self.calls = []

        def start(self, on_chunk=None):
            self.on_chunk = on_chunk
            self.calls.append("start")

        def stop(self):
            self.calls.append("stop")
            return tmp_path / "last_recording.wav"

        def cancel(self):
            self.calls.append("cancel")

    class FakeSTTEngine:
        def __init__(self):
            self.call_count = 0

        def transcribe(self, path, **kw):
            self.call_count += 1
            return SimpleNamespace(
                raw_text=f"chunk_{self.call_count}",
                segments=[Segment(0.0, 1.0, f"chunk_{self.call_count}")],
                duration_sec=1.0,
                stt_elapsed_sec=0.01,
            )

    class FakePipeline:
        def __init__(self):
            self.stt = FakeSTTEngine()
            self.glossary = SimpleNamespace(build_whisper_initial_prompt=lambda: "")

        def run_text(self, text, mode, **kw):
            return PipelineResult(
                raw_text=text,
                final_text=f"refined: {text}",
                mode=mode,
                stt_profile_name="cpu_fast",
                llm_profile_name="dev",
                stt_elapsed_sec=0.02,
                llm_elapsed_sec=0.01,
                fallback_triggered=False,
            )

    class FakePaster:
        def __init__(self):
            self.pasted = []

        def emit(self, text, mode):
            self.pasted.append(text)

        def emit_rich(self, text, images):
            self.pasted.append(text)

    rec = ChunkRecorder()
    pipe = FakePipeline()
    paster = FakePaster()
    sess = SessionManager(pipe, rec, cache_dir=tmp_path)

    daemon = None
    ctl = PanelController(
        submit=lambda ev: daemon.submit(ev),
        paster=paster,
        copier=lambda t: None,
        streaming=True,
    )
    daemon = Daemon(
        HotkeyConfig(mode="toggle"),
        rec,
        pipe,
        cache_dir=str(tmp_path),
        sessions=sess,
        on_result=ctl.on_result,
        on_segment=ctl.on_segment,
        on_jobs=ctl.on_jobs,
    )
    daemon.start(listen=False)

    try:
        # First recording: press -> push chunks -> press
        ctl.record_clicked()
        assert wait_for(lambda: rec.calls == ["start"])
        rec.on_chunk(AudioChunk(index=0, start_sec=0.0, pcm=b"\x00" * 3200))

        ctl.record_clicked()  # stop 1st recording
        assert wait_for(lambda: "stop" in rec.calls)

        # Immediately start 2nd recording: press again -> push chunks -> press
        ctl.record_clicked()  # start 2nd recording
        assert wait_for(lambda: rec.calls.count("start") == 2)
        rec.on_chunk(AudioChunk(index=0, start_sec=0.0, pcm=b"\x00" * 3200))

        ctl.record_clicked()  # stop 2nd recording
        assert wait_for(lambda: rec.calls.count("stop") == 2)

        # Wait for both jobs to finish and verify results pasted in order
        assert wait_for(lambda: len(paster.pasted) == 2, timeout=5.0)
        assert "refined: chunk_1" in paster.pasted[0]
        assert "refined: chunk_2" in paster.pasted[1]
    finally:
        daemon.shutdown(timeout=2.0)


def test_app_render_streaming_widget_states():
    from local_typeless.ui.app import PanelApp, WidgetCache
    from local_typeless.ui.controller import ViewModel

    class DummyWidget:
        def __init__(self):
            self.attrs = {}
            self.packed = False

        def configure(self, **kw):
            self.attrs.update(kw)

        def pack(self, **kw):
            self.packed = True

        def pack_forget(self):
            self.packed = False

        def start(self, *a):
            pass

        def stop(self):
            pass

        def delete(self, *a):
            pass

        def insert(self, *a):
            pass

        def selection_clear(self, *a):
            pass

        def selection_set(self, *a):
            pass

        def see(self, *a):
            pass

    app = SimpleNamespace(
        _wc=WidgetCache(),
        _cache={},
        _cached=lambda k, v: PanelApp._cached(app, k, v),
        rec_btn=DummyWidget(),
        cancel_btn=DummyWidget(),
        paste_image_btn=DummyWidget(),
        abandon_btn=DummyWidget(),
        retry_btn=DummyWidget(),
        mode_box=DummyWidget(),
        raw_ins=DummyWidget(),
        res_ins=DummyWidget(),
        progress=DummyWidget(),
        ctl_row=DummyWidget(),
        notice=DummyWidget(),
        raw_text=DummyWidget(),
        res_text=DummyWidget(),
        res_title=DummyWidget(),
        hist=DummyWidget(),
        status=DummyWidget(),
        msg_label=DummyWidget(),
        root=SimpleNamespace(winfo_width=lambda: 300),
        _set_text=lambda w, t: None,
    )

    # 1. Background job pending, idle:
    # - abandon_btn is normal
    # - progress bar is packed
    # - status text includes "處理中 2"
    # - mode_box and insert buttons are enabled ("readonly" and "normal")
    vm = ViewModel(phase="idle", pending_jobs=2)
    PanelApp.render(app, vm)

    assert app.abandon_btn.attrs["state"] == "normal"
    assert app.progress.packed is True
    assert "處理中 2" in app.status.attrs["text"]
    assert app.mode_box.attrs["state"] == "readonly"
    assert app.raw_ins.attrs["state"] == "normal"
    assert app.res_ins.attrs["state"] == "normal"

    # 2. Dropping to 0 pending, idle:
    # - abandon_btn becomes disabled
    # - progress bar is unpacked
    # - status text does not include "處理中"
    vm2 = ViewModel(phase="idle", pending_jobs=0)
    PanelApp.render(app, vm2)

    assert app.abandon_btn.attrs["state"] == "disabled"
    assert app.progress.packed is False
    assert "處理中" not in app.status.attrs["text"]

    # 3. Hotkey hints & paste_image_btn states:
    vm3 = ViewModel(
        phase="recording",
        mode=Mode.CLEAN,
        hotkey_hints={"clean": "Alt+Q", "paste_image": "Alt+V", "cancel": "Esc"},
    )
    PanelApp.render(app, vm3)
    assert app.paste_image_btn.attrs["state"] == "normal"
    assert app.paste_image_btn.attrs["text"] == "貼上圖片 (Alt+V)"
    assert app.cancel_btn.attrs["text"] == "取消 (Esc)"

    vm4 = ViewModel(
        phase="idle",
        mode=Mode.CLEAN,
        hotkey_hints={"clean": "Alt+Q", "paste_image": "Alt+V", "cancel": "Esc"},
    )
    PanelApp.render(app, vm4)
    assert app.paste_image_btn.attrs["state"] == "disabled"
    assert app.rec_btn.attrs["text"] == "🎙  開始錄音 (Alt+Q)"

