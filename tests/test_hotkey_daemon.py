"""Hotkey state machine / tracker / daemon worker tests (no pynput hooks, no audio)."""
from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest
from pynput.keyboard import HotKey, Key, KeyCode

from local_typeless.hotkey.daemon import (
    Action,
    ChordTracker,
    Daemon,
    Event,
    HotkeyStateMachine,
    State,
)
from local_typeless.types import HotkeyConfig, Mode

P = lambda m: Event("press", m)      # noqa: E731
R = lambda m: Event("release", m)    # noqa: E731
CANCEL = Event("cancel")


# ------------------------------------------------------------ state machine
def test_toggle_flow_uses_starting_mode():
    sm = HotkeyStateMachine()
    assert sm.handle(P(Mode.CLEAN)) == Action("start", Mode.CLEAN)
    assert sm.state is State.RECORDING
    # pressing the *other* hotkey stops, with the mode that started it
    assert sm.handle(P(Mode.SLIDE)) == Action("stop", Mode.CLEAN)
    assert sm.state is State.PROCESSING
    sm.finish()
    assert sm.state is State.IDLE and sm.mode is None


def test_cancel_only_while_recording():
    sm = HotkeyStateMachine()
    assert sm.handle(CANCEL) is None and sm.state is State.IDLE
    sm.handle(P(Mode.SLIDE))
    assert sm.handle(CANCEL) == Action("cancel") and sm.state is State.IDLE
    sm.handle(P(Mode.SLIDE))
    sm.handle(P(Mode.SLIDE))  # -> PROCESSING
    assert sm.handle(CANCEL) is None and sm.state is State.PROCESSING


def test_presses_ignored_while_processing_with_log():
    notes = []
    sm = HotkeyStateMachine(notify=notes.append)
    sm.handle(P(Mode.CLEAN))
    sm.handle(P(Mode.CLEAN))
    assert sm.handle(P(Mode.SLIDE)) is None
    assert sm.state is State.PROCESSING
    assert "ignored" in notes[-1]


def test_push_to_talk():
    sm = HotkeyStateMachine(push_to_talk=True)
    assert sm.handle(P(Mode.CLEAN)) == Action("start", Mode.CLEAN)
    assert sm.handle(P(Mode.CLEAN)) is None          # auto-repeat
    assert sm.handle(R(Mode.SLIDE)) is None          # other chord's release
    assert sm.handle(R(Mode.CLEAN)) == Action("stop", Mode.CLEAN)
    assert sm.state is State.PROCESSING
    assert sm.handle(R(Mode.CLEAN)) is None


def test_release_ignored_in_toggle_mode():
    sm = HotkeyStateMachine()
    sm.handle(P(Mode.CLEAN))
    assert sm.handle(R(Mode.CLEAN)) is None and sm.state is State.RECORDING


# ----------------------------------------------------------- chord tracker
def make_tracker():
    return ChordTracker({
        "clean": HotKey.parse("<alt>+q"),
        "slide": HotKey.parse("<alt>+s"),
        "cancel": HotKey.parse("<esc>"),
    })


def test_tracker_press_edge_and_autorepeat():
    t = make_tracker()
    assert t.on_press(Key.alt_l) == []
    assert t.on_press(KeyCode.from_char("q")) == [P(Mode.CLEAN)]
    assert t.on_press(KeyCode.from_char("q")) == []           # repeat
    assert t.on_release(Key.alt_l) == []                      # modifier first: no release yet
    assert t.on_release(KeyCode.from_char("q")) == [R(Mode.CLEAN)]


def test_tracker_vk_fallback_and_case():
    t = make_tracker()
    t.on_press(Key.alt_l)
    assert t.on_press(KeyCode.from_vk(0x51)) == [P(Mode.CLEAN)]   # char=None, vk='Q'
    t2 = make_tracker()
    t2.on_press(Key.alt_r)
    assert t2.on_press(KeyCode.from_char("S")) == [P(Mode.SLIDE)]


def test_tracker_cancel_and_no_trigger_without_modifier():
    t = make_tracker()
    assert t.on_press(KeyCode.from_char("q")) == []
    assert t.on_press(Key.esc) == [CANCEL]
    assert t.on_release(Key.esc) == []


# ------------------------------------------------------------------ daemon
class FakeRecorder:
    def __init__(self, fail_start=False):
        self.calls = []
        self.fail_start = fail_start
        self.threads = set()
        self.is_recording = False

    def start(self):
        self.threads.add(threading.current_thread().name)
        if self.fail_start:
            raise RuntimeError("no mic")
        self.calls.append("start")
        self.is_recording = True

    def stop(self):
        self.calls.append("stop")
        self.is_recording = False
        return "x.wav"

    def cancel(self):
        self.calls.append("cancel")
        self.is_recording = False


class FakePipeline:
    def __init__(self, text="hello", exc=None, gate=None):
        self.text, self.exc, self.gate, self.calls = text, exc, gate, []

    def run_file(self, path, mode):
        self.calls.append((path, mode))
        if self.gate:
            self.gate.wait(5)
        if self.exc:
            raise self.exc
        return SimpleNamespace(final_text=self.text, fallback_triggered=False, error_message=None)


def make_daemon(rec=None, pipe=None, mode="toggle", **kw):
    lines = []
    d = Daemon(HotkeyConfig(mode=mode), rec or FakeRecorder(), pipe or FakePipeline(),
               out=lines.append, **kw)
    d.start_workers()
    return d, lines


def test_daemon_full_cycle_runs_on_worker_thread():
    rec, pipe = FakeRecorder(), FakePipeline()
    d, lines = make_daemon(rec, pipe)
    d.submit(P(Mode.SLIDE))
    d.submit(P(Mode.CLEAN))
    d.wait_idle()
    assert rec.calls == ["start", "stop"]
    assert pipe.calls == [("x.wav", Mode.SLIDE)]
    assert d.machine.state is State.IDLE
    assert rec.threads and threading.current_thread().name not in rec.threads
    assert any("recording (slide)" in l for l in lines)
    assert any("done: hello" in l for l in lines)
    d.shutdown()


def test_hotkey_during_processing_is_ignored_not_queued():
    gate = threading.Event()
    rec, pipe = FakeRecorder(), FakePipeline(gate=gate)
    d, lines = make_daemon(rec, pipe)
    d.submit(P(Mode.CLEAN))
    d.submit(P(Mode.CLEAN))                 # stop -> processing (blocked on gate)
    d.submit(P(Mode.CLEAN))                 # ignored
    assert d.machine.state is State.PROCESSING
    gate.set()
    d.wait_idle()
    assert rec.calls == ["start", "stop"] and len(pipe.calls) == 1
    assert d.machine.state is State.IDLE
    d.shutdown()


def test_cancel_discards_without_pipeline():
    rec, pipe = FakeRecorder(), FakePipeline()
    d, _ = make_daemon(rec, pipe)
    d.submit(P(Mode.CLEAN))
    d.submit(CANCEL)
    d.wait_idle()
    assert rec.calls == ["start", "cancel"] and pipe.calls == []
    assert d.machine.state is State.IDLE
    d.shutdown()


def test_pipeline_error_returns_to_idle():
    d, lines = make_daemon(pipe=FakePipeline(exc=RuntimeError("boom")))
    d.submit(P(Mode.CLEAN))
    d.submit(P(Mode.CLEAN))
    d.wait_idle()
    assert d.machine.state is State.IDLE
    assert any("error: RuntimeError: boom" in l for l in lines)
    d.shutdown()


def test_recorder_start_failure_resets():
    d, lines = make_daemon(rec=FakeRecorder(fail_start=True))
    d.submit(P(Mode.CLEAN))
    d.wait_idle()
    assert d.machine.state is State.IDLE
    assert any("cannot start recording" in l for l in lines)
    d.shutdown()


def test_push_to_talk_daemon():
    rec, pipe = FakeRecorder(), FakePipeline()
    d, _ = make_daemon(rec, pipe, mode="push_to_talk")
    d.submit(P(Mode.CLEAN))
    d.submit(R(Mode.CLEAN))
    d.wait_idle()
    assert rec.calls == ["start", "stop"] and pipe.calls[0][1] is Mode.CLEAN
    d.shutdown()


def test_shutdown_while_recording_stops_and_processes():
    rec, pipe = FakeRecorder(), FakePipeline()
    d, lines = make_daemon(rec, pipe)
    d._backend = SimpleNamespace(stop=lambda: rec.calls.append("backend_stop"))
    d.submit(P(Mode.CLEAN))
    d.wait_idle()
    d.shutdown()
    assert rec.calls == ["start", "backend_stop", "stop"]    # stop, never cancel
    assert pipe.calls == [("x.wav", Mode.CLEAN)]
    assert not d._worker.is_alive()
    d.shutdown()  # idempotent


def test_shutdown_while_processing_times_out_with_preserved_message():
    gate = threading.Event()
    d, lines = make_daemon(pipe=FakePipeline(gate=gate), shutdown_wait_sec=0.2, cache_dir=".cache")
    d.submit(P(Mode.CLEAN))
    d.submit(P(Mode.CLEAN))
    d.shutdown()
    assert any("unfinished dictation preserved in .cache/last_recording.wav: run retry" in l
               for l in lines)
    gate.set()


def test_shutdown_while_processing_waits_for_completion():
    gate = threading.Event()
    d, lines = make_daemon(pipe=FakePipeline(gate=gate), shutdown_wait_sec=5)
    d.submit(P(Mode.CLEAN))
    d.submit(P(Mode.CLEAN))
    threading.Timer(0.2, gate.set).start()
    d.shutdown()
    assert not any("unfinished" in l for l in lines)
    assert any("done: hello" in l for l in lines)


def test_stop_skipped_when_recorder_not_recording():
    rec, pipe = FakeRecorder(), FakePipeline()
    d, lines = make_daemon(rec, pipe)
    d.submit(P(Mode.CLEAN))
    d.wait_idle()
    rec.is_recording = False          # e.g. recorder already stopped elsewhere
    d.submit(P(Mode.CLEAN))
    d.wait_idle()
    assert pipe.calls == [] and "stop" not in rec.calls
    assert d.machine.state is State.IDLE
    assert any("nothing recorded" in l for l in lines)
    d.shutdown()


def test_all_outputs_failed_prints_full_text():
    long_text = "word " * 40
    res = SimpleNamespace(final_text=long_text, fallback_triggered=False,
                          error_message="all outputs failed (ClipboardOutput: OSError: x)")
    d, lines = make_daemon(pipe=FakePipeline(text=long_text))
    d.pipeline.run_file = lambda p, m: res
    d.submit(P(Mode.CLEAN))
    d.submit(P(Mode.CLEAN))
    d.wait_idle()
    assert any(long_text in l for l in lines)
    d.shutdown()


def test_backend_ignores_injected_events():
    from local_typeless.hotkey.daemon import PynputBackend

    d, _ = make_daemon()
    b = PynputBackend(d)
    b._on_press(Key.alt_l, True)
    b._on_press(KeyCode.from_char("q"), True)
    d.wait_idle()
    assert d.machine.state is State.IDLE
    b._on_press(Key.alt_l, False)
    b._on_press(KeyCode.from_char("q"), False)
    d.wait_idle()
    assert d.machine.state is State.RECORDING
    d.shutdown()


# ------------------------------------------------------------ image watcher
class FakeImageWatcher:
    def __init__(self, images=None, raise_start=False):
        self.images = images if images is not None else []
        self.raise_start = raise_start
        self.calls = []

    def start(self):
        self.calls.append("start")
        if self.raise_start:
            raise RuntimeError("watcher start error")

    def stop(self):
        self.calls.append("stop")
        return list(self.images)

    def cancel(self):
        self.calls.append("cancel")


class FakeImagePipeline(FakePipeline):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.last_kw = {}

    def run_file(self, path, mode, **kw):
        self.last_kw = kw
        return super().run_file(path, mode)


def test_daemon_image_watcher_lifecycle_and_manifest(tmp_path):
    from local_typeless.types import CapturedImage

    images = [CapturedImage(1, 1.2, str(tmp_path / "img1.png"))]
    watcher = FakeImageWatcher(images=images)
    rec, pipe = FakeRecorder(), FakeImagePipeline()
    d, _ = make_daemon(rec, pipe, cache_dir=str(tmp_path), image_watcher=watcher)

    d.submit(P(Mode.CLEAN))
    d.wait_idle()
    assert rec.calls == ["start"]
    assert watcher.calls == ["start"]

    d.submit(P(Mode.CLEAN))
    d.wait_idle()
    assert rec.calls == ["start", "stop"]
    assert watcher.calls == ["start", "stop"]
    assert pipe.calls == [("x.wav", Mode.CLEAN)]
    assert pipe.last_kw.get("images") == images

    manifest = tmp_path / "last_recording.images.json"
    assert manifest.exists()
    d.shutdown()


def test_daemon_without_images_kwarg_absent(tmp_path):
    watcher = FakeImageWatcher(images=[])
    rec, pipe = FakeRecorder(), FakeImagePipeline()
    d, _ = make_daemon(rec, pipe, cache_dir=str(tmp_path), image_watcher=watcher)

    d.submit(P(Mode.CLEAN))
    d.submit(P(Mode.CLEAN))
    d.wait_idle()
    assert "images" not in pipe.last_kw
    d.shutdown()


def test_daemon_cancel_calls_watcher_cancel(tmp_path):
    watcher = FakeImageWatcher()
    rec, pipe = FakeRecorder(), FakePipeline()
    d, _ = make_daemon(rec, pipe, cache_dir=str(tmp_path), image_watcher=watcher)

    d.submit(P(Mode.CLEAN))
    d.submit(CANCEL)
    d.wait_idle()
    assert rec.calls == ["start", "cancel"]
    assert watcher.calls == ["start", "cancel"]
    d.shutdown()


def test_daemon_watcher_start_failure_does_not_prevent_recording(tmp_path):
    watcher = FakeImageWatcher(raise_start=True)
    rec, pipe = FakeRecorder(), FakePipeline()
    d, _ = make_daemon(rec, pipe, cache_dir=str(tmp_path), image_watcher=watcher)

    d.submit(P(Mode.CLEAN))
    d.wait_idle()
    assert rec.calls == ["start"]
    assert d.machine.state is State.RECORDING
    d.shutdown()


# ------------------------------------------------------------ Task 10: streaming
def test_state_machine_background_true():
    notes = []
    sm = HotkeyStateMachine(background=True, notify=notes.append)

    # Toggle mode: press starts recording
    assert sm.handle(P(Mode.CLEAN)) == Action("start", Mode.CLEAN)
    assert sm.state is State.RECORDING

    # Toggle mode: press again stops and returns to IDLE immediately
    assert sm.handle(P(Mode.CLEAN)) == Action("stop", Mode.CLEAN)
    assert sm.state is State.IDLE
    assert "queued" in notes[-1]

    # Press right after stop starts a new recording directly
    assert sm.handle(P(Mode.SLIDE)) == Action("start", Mode.SLIDE)
    assert sm.state is State.RECORDING

    # Force stop returns to IDLE with queued note
    assert sm.force_stop() == Action("stop", Mode.SLIDE)
    assert sm.state is State.IDLE

    # Retry while IDLE emits notice and stays IDLE
    assert sm.begin_retry(Mode.SUMMARY) == Action("retry", Mode.SUMMARY)
    assert sm.state is State.IDLE
    assert any("retry queued" in n for n in notes)

    # Push to talk mode with background=True
    sm_ptt = HotkeyStateMachine(push_to_talk=True, background=True)
    assert sm_ptt.handle(P(Mode.CLEAN)) == Action("start", Mode.CLEAN)
    assert sm_ptt.state is State.RECORDING
    assert sm_ptt.handle(R(Mode.CLEAN)) == Action("stop", Mode.CLEAN)
    assert sm_ptt.state is State.IDLE


class FakeSessions:
    def __init__(self, fail_begin=False, retry_ret=None):
        self.fail_begin = fail_begin
        self.retry_ret = retry_ret
        self.calls = []
        self.on_segment = None
        self.on_result = None
        self.on_job = None
        self.next_id = 0

    def start(self):
        self.calls.append("start")

    def begin(self, mode):
        self.calls.append(("begin", mode))
        if self.fail_begin:
            raise RuntimeError("audio device busy")
        self.next_id += 1
        return SimpleNamespace(id=self.next_id, mode=mode)

    def end(self):
        self.calls.append("end")

    def cancel(self):
        self.calls.append("cancel")

    def retry(self, mode):
        self.calls.append(("retry", mode))
        if self.retry_ret is not None:
            return self.retry_ret
        self.next_id += 1
        return SimpleNamespace(id=self.next_id, mode=mode)

    def abandon(self):
        self.calls.append("abandon")
        return True

    def shutdown(self, timeout=60.0):
        self.calls.append(("shutdown", timeout))
        return True


def test_daemon_with_sessions_delegation(tmp_path):
    sess = FakeSessions()
    rec, pipe = FakeRecorder(), FakePipeline()
    on_jobs_calls = []
    d = Daemon(
        HotkeyConfig(),
        rec,
        pipe,
        cache_dir=str(tmp_path),
        sessions=sess,
        on_jobs=lambda p, n: on_jobs_calls.append((p, n)),
    )
    d.start(listen=False)
    assert "start" in sess.calls

    # Start
    d.submit(P(Mode.CLEAN))
    d.wait_idle()
    assert ("begin", Mode.CLEAN) in sess.calls

    # Stop
    d.submit(P(Mode.CLEAN))
    d.wait_idle()
    assert "end" in sess.calls

    # Cancel
    d.submit(P(Mode.CLEAN))
    d.submit(CANCEL)
    d.wait_idle()
    assert "cancel" in sess.calls

    # Retry
    d.request_retry(Mode.SUMMARY)
    d.wait_idle()
    assert ("retry", Mode.SUMMARY) in sess.calls

    # Abandon
    assert d.abandon() is True
    assert "abandon" in sess.calls

    # Shutdown
    d.shutdown(timeout=2.0)
    assert any(c[0] == "shutdown" for c in sess.calls if isinstance(c, tuple))


def test_daemon_with_sessions_begin_failure_resets():
    sess = FakeSessions(fail_begin=True)
    rec, pipe = FakeRecorder(), FakePipeline()
    d = Daemon(HotkeyConfig(), rec, pipe, sessions=sess)
    d.start_workers()

    d.submit(P(Mode.CLEAN))
    d.wait_idle()
    assert d.machine.state is State.IDLE
    d.shutdown()


def test_daemon_sessions_callbacks_and_notes():
    sess = FakeSessions()
    rec, pipe = FakeRecorder(), FakePipeline()
    seg_calls, res_calls, jobs_calls = [], [], []

    d = Daemon(
        HotkeyConfig(),
        rec,
        pipe,
        sessions=sess,
        on_segment=seg_calls.append,
        on_result=res_calls.append,
        on_jobs=lambda p, n: jobs_calls.append((p, n)),
    )
    d.start_workers()

    # on_segment only from newest job
    job1 = SimpleNamespace(id=1, text="text1")
    job2 = SimpleNamespace(id=2, text="text2")

    sess.on_job(job1, 1)
    sess.on_segment(job1)
    assert seg_calls == ["text1"]

    sess.on_job(job2, 2)
    sess.on_segment(job1)  # older job segment ignored
    sess.on_segment(job2)
    assert seg_calls == ["text1", "text2"]

    # on_result
    fake_res = SimpleNamespace(final_text="done text")
    sess.on_result(job2, fake_res)
    assert res_calls == [fake_res]

    # on_job notes mapping
    job_err = SimpleNamespace(id=3, status="error", error="boom", result=None)
    sess.on_job(job_err, 1)
    assert jobs_calls[-1] == (1, "error: boom")

    job_ab = SimpleNamespace(id=4, status="abandoned", result=None)
    sess.on_job(job_ab, 0)
    assert jobs_calls[-1] == (0, "abandoned")

    job_ns = SimpleNamespace(
        id=5,
        status="done",
        result=SimpleNamespace(error_message="no speech detected"),
    )
    sess.on_job(job_ns, 0)
    assert jobs_calls[-1] == (0, "no speech detected")

    d.shutdown()


# ------------------------------------------------------------ Task 14: paste_image
def test_state_machine_paste_image_only_while_recording():
    sm = HotkeyStateMachine()
    # In IDLE:
    assert sm.handle(Event("paste_image")) is None
    assert sm.state is State.IDLE

    # In RECORDING:
    sm.handle(P(Mode.CLEAN))
    assert sm.state is State.RECORDING
    act = sm.handle(Event("paste_image"))
    assert act == Action("paste_image")
    assert sm.state is State.RECORDING

    # In PROCESSING:
    sm.handle(P(Mode.CLEAN))
    assert sm.state is State.PROCESSING
    assert sm.handle(Event("paste_image")) is None


def test_chord_tracker_emits_paste_image():
    from pynput.keyboard import Key, KeyCode

    tracker = ChordTracker({"paste_image": [Key.alt, KeyCode.from_char("v")]})
    assert tracker.on_press(Key.alt) == []
    events = tracker.on_press(KeyCode.from_char("v"))
    assert events == [Event("paste_image")]

    # Release does not emit anything
    assert tracker.on_release(KeyCode.from_char("v")) == []


def test_daemon_routes_paste_image_legacy_and_sessions(tmp_path):
    # 1. Legacy path
    watcher = FakeImageWatcher()
    watcher.capture_now = lambda: 1
    rec, pipe = FakeRecorder(), FakePipeline()
    paste_calls = []
    d, _ = make_daemon(
        rec, pipe, cache_dir=str(tmp_path), image_watcher=watcher, on_paste=paste_calls.append
    )
    d.submit(P(Mode.CLEAN))  # start recording
    d.submit(Event("paste_image"))
    d.wait_idle()
    assert paste_calls == [1]
    d.shutdown()

    # 2. Sessions path
    sess = FakeSessions()
    sess.paste_image = lambda: 2
    sess_paste_calls = []
    d2 = Daemon(
        HotkeyConfig(),
        rec,
        pipe,
        cache_dir=str(tmp_path),
        sessions=sess,
        on_paste=sess_paste_calls.append,
    )
    d2.start_workers()
    d2.submit(P(Mode.CLEAN))
    d2.submit(Event("paste_image"))
    d2.wait_idle()
    assert sess_paste_calls == [2]
    d2.shutdown()


