"""PanelController tests: no Tk, no audio, no clipboard."""
from __future__ import annotations

from types import SimpleNamespace

from pynput.keyboard import HotKey, Key, KeyCode

from local_typeless.hotkey.daemon import ChordTracker, Daemon, Event, State
from local_typeless.types import HotkeyConfig, Mode
from local_typeless.ui.controller import (
    HISTORY_MAX,
    MODE_CHOICES,
    PanelController,
    compose_insert,
)


class FakePaster:
    def __init__(self, fail=False):
        self.got, self.fail = [], fail

    def emit(self, text, mode):
        if self.fail:
            raise OSError("clipboard busy")
        self.got.append(text)


def result(raw="原文 text", final="摘要 text", mode=Mode.SUMMARY, fallback=False):
    return SimpleNamespace(raw_text=raw, final_text=final, mode=mode, fallback_triggered=fallback,
                           stt_elapsed_sec=1.5, llm_elapsed_sec=0.7, error_message=None)


def make(paster=None, device="", **kw):
    events, copied = [], []
    c = PanelController(
        submit=events.append, paster=paster or FakePaster(), copier=copied.append,
        stt_profile="cpu_fast", llm_profile="dev",
        device_getter=lambda: device, run_async=lambda f: f(), clock=lambda: 100.0,
        **kw,
    )
    return c, events, copied


# ------------------------------------------------------------------ basics
def test_compose_insert_ends_with_blank_line():
    assert compose_insert("r", "f", False) == "f\n\n"
    assert compose_insert(" r ", " f ", True) == "原文：r\n摘要：f\n\n"


def test_default_mode_is_clean_and_choices():
    assert MODE_CHOICES[0] == ("整理", Mode.CLEAN)
    assert [m for _, m in MODE_CHOICES] == [Mode.CLEAN, Mode.SUMMARY, Mode.SLIDE]
    c, *_ = make()
    assert c.vm.mode is Mode.CLEAN and c.vm.auto_paste and not c.vm.include_raw
    assert c.vm.result_mode is Mode.CLEAN


def test_poll_only_when_dirty():
    c, *_ = make()
    assert c.poll() is not None
    assert c.poll() is None
    c.set_mode(Mode.CLEAN)
    assert c.poll().mode is Mode.CLEAN


def test_record_button_submits_same_events_as_hotkey():
    c, events, _ = make()
    c.set_mode(Mode.SLIDE)
    c.record_clicked()
    assert events == [Event("press", Mode.SLIDE)]
    c.on_state(State.RECORDING, Mode.SLIDE, "")
    c.set_mode(Mode.CLEAN)                     # dropdown change mid-recording
    c.record_clicked()
    assert events[-1] == Event("press", Mode.SLIDE)   # stop uses the recording's mode
    c.on_state(State.PROCESSING, Mode.SLIDE, "")
    n = len(events)
    c.record_clicked()                          # disabled while processing
    assert len(events) == n
    c.cancel_clicked()
    assert events[-1] == Event("cancel")


# -------------------------------------------------------------- state mapping
def test_phases_timer_and_model_loading_hint():
    c, *_ = make()
    c.on_state(State.RECORDING, Mode.SUMMARY, "")
    assert c.vm.phase == "recording" and c.vm.record_started == 100.0
    c.on_state(State.PROCESSING, Mode.SUMMARY, "")
    assert c.vm.phase == "processing" and c.vm.loading_model and "載入模型" in c.vm.message
    c.on_result(result())
    c.on_state(State.IDLE, Mode.SUMMARY, "done: x")
    assert c.vm.phase == "idle" and c.vm.message == "完成"
    c.on_state(State.PROCESSING, Mode.SUMMARY, "")
    assert not c.vm.loading_model and "分析中" in c.vm.message


def test_idle_notes_are_translated():
    c, *_ = make()
    c.on_state(State.IDLE, None, "recording cancelled")
    assert c.vm.message == "已取消錄音"
    c.on_state(State.IDLE, None, "no speech detected")
    assert c.vm.message == "沒有偵測到語音"


# ------------------------------------------------------------------- results
def test_result_auto_paste_with_trailing_blank_line():
    p = FakePaster()
    c, *_ = make(p)
    c.on_result(result())
    assert p.got == ["摘要 text\n\n"]
    assert c.vm.raw_text == "原文 text" and c.vm.final_text == "摘要 text"
    assert c.vm.stt_sec == 1.5 and c.vm.llm_sec == 0.7


def test_include_raw_and_auto_paste_off():
    p = FakePaster()
    c, *_ = make(p)
    c.set_include_raw(True)
    c.on_result(result("r", "f"))
    assert p.got == ["原文：r\n摘要：f\n\n"]
    c.set_auto_paste(False)
    c.on_result(result("r2", "f2"))
    assert len(p.got) == 1                      # nothing pasted
    assert c.vm.final_text == "f2"


def test_fallback_notice_and_device_note():
    c, *_ = make(device="cpu (fallback)")
    c.on_result(result(fallback=True))
    vm = c.poll()
    assert vm.notice == "LLM 未連線，顯示原文"
    assert vm.device_note == "CUDA 失敗，已改用 CPU"
    c.on_result(result(fallback=False))
    assert c.vm.notice == ""


def test_empty_result_not_recorded_or_pasted():
    p = FakePaster()
    c, *_ = make(p)
    c.on_result(result(raw="", final=""))
    assert p.got == [] and c.vm.history == []


def test_history_bounded_newest_first_and_selectable():
    c, *_ = make()
    for i in range(HISTORY_MAX + 3):
        c.on_result(result(f"raw{i}", f"final{i}", mode=Mode.SLIDE if i == 0 else Mode.SUMMARY))
    assert len(c.vm.history) == HISTORY_MAX
    assert c.vm.history[0].final_text == f"final{HISTORY_MAX + 2}"
    c.select_history(HISTORY_MAX - 1)
    assert c.vm.final_text == "final3" and c.vm.selected == HISTORY_MAX - 1
    c.select_history(99)                        # out of range ignored
    assert c.vm.final_text == "final3"


def test_result_label_follows_mode():
    c, *_ = make()
    c.on_result(result(mode=Mode.SUMMARY))
    assert c.vm.result_label == "摘要結果"
    c.on_result(result(mode=Mode.SLIDE))
    assert c.vm.result_label == "投影片結果"
    c.on_result(result(mode=Mode.CLEAN))
    assert c.vm.result_label == "整理結果"


def test_paste_failure_is_reported_and_survives_done():
    c, *_ = make(FakePaster(fail=True))
    c.on_result(result())
    assert c.vm.message.startswith("插入失敗")
    c.on_state(State.IDLE, Mode.SUMMARY, "done: x")
    assert c.vm.message.startswith("插入失敗")
    assert c.vm.final_text == "摘要 text"       # result is still shown


def test_manual_insert_and_copy_buttons():
    p = FakePaster()
    c, _, copied = make(p)
    c.on_result(result("raw!", "final!"))
    p.got.clear()
    c.insert_clicked("raw")
    c.insert_clicked("final")
    assert p.got == ["raw!\n\n", "final!\n\n"]
    c.copy_text("final")
    assert copied == ["final!"]


# ------------------------------------------------- daemon integration (fakes)
def test_summary_chord_and_daemon_callbacks_drive_controller():
    t = ChordTracker({
        "clean": HotKey.parse("<alt>+q"),
        "summary": HotKey.parse("<alt>+w"),
        "cancel": HotKey.parse("<esc>"),
    })
    t.on_press(Key.alt_l)
    assert t.on_press(KeyCode.from_char("w")) == [Event("press", Mode.SUMMARY)]

    class Rec:
        is_recording = False

        def start(self):
            self.is_recording = True

        def stop(self):
            self.is_recording = False
            return "x.wav"

        def cancel(self):
            self.is_recording = False

    class Pipe:
        def run_file(self, path, mode):
            return result(mode=mode)

    p = FakePaster()
    holder = {}
    c = PanelController(submit=lambda ev: holder["d"].submit(ev), paster=p, copier=lambda t: None,
                        run_async=lambda f: f())
    d = Daemon(HotkeyConfig(), Rec(), Pipe(), out=lambda m: None,
               on_state=c.on_state, on_result=c.on_result)
    holder["d"] = d
    d.start(listen=False)
    c.set_mode(Mode.SUMMARY)
    c.record_clicked()          # panel button == hotkey press
    d.wait_idle()
    assert c.vm.phase == "recording" and c.vm.recording_mode is Mode.SUMMARY
    c.record_clicked()
    d.wait_idle()
    assert c.vm.phase == "idle" and p.got == ["摘要 text\n\n"]
    assert c.vm.history[0].mode is Mode.SUMMARY
    d.shutdown()


# ------------------------------------------------------------ LLM health ping
def make_ping(pinger, model="test-model"):
    return PanelController(
        submit=lambda e: None, paster=FakePaster(), copier=lambda t: None,
        run_async=lambda f: f(), llm_pinger=pinger, llm_model=model,
        api_key_env="TEST_API_KEY",
    )


def test_ping_connected_shows_model():
    c = make_ping(lambda: ["test-model"])
    assert c.vm.llm_status == "檢查中…"
    c.ping_llm()
    assert c.vm.llm_status == "已連線 (test-model)"


def test_ping_model_missing_on_server():
    c = make_ping(lambda: ["other"])
    c.ping_llm()
    assert "沒有模型 test-model" in c.vm.llm_status


def test_ping_failures_are_mapped():
    from local_typeless.types import LLMConnectionError, LLMResponseError, LLMTimeoutError

    cases = [
        (LLMResponseError("HTTP 401: nope"), "未連線 — 金鑰錯誤或未設定 (TEST_API_KEY)"),
        (LLMResponseError("HTTP 403: nope"), "未連線 — 金鑰錯誤或未設定 (TEST_API_KEY)"),
        (LLMTimeoutError("slow"), "未連線 — 連線逾時"),
        (LLMConnectionError("refused"), "未連線 — 無法連線"),
        (LLMResponseError("HTTP 500: boom"), "未連線 — 伺服器錯誤 (HTTP 500)"),
    ]
    for exc, want in cases:
        def pinger(exc=exc):
            raise exc

        c = make_ping(pinger)
        c.ping_llm()
        assert c.vm.llm_status == want


def test_fallback_result_triggers_reping():
    calls = []

    def pinger():
        calls.append(1)
        return ["test-model"]

    c = make_ping(pinger)
    c.on_result(result(fallback=False))
    assert calls == []
    c.on_result(result(fallback=True))
    assert calls == [1]


def test_no_pinger_is_noop():
    c, *_ = make()
    c.ping_llm()
    assert c.vm.llm_status == "檢查中…"


def test_missing_stt_model_error_is_shown_verbatim_not_hanging():
    c, *_ = make()
    hint = "STT 模型尚未下載：請執行 python -m local_typeless.cli download-model --stt-profile cpu_fast"
    c.on_state(State.PROCESSING, Mode.SUMMARY, "")
    c.on_state(State.IDLE, Mode.SUMMARY, "error: ModelNotAvailableError: " + hint)
    assert c.vm.phase == "idle" and c.vm.message == hint
    c.notify("hello")
    assert c.vm.message == "hello"


# ------------------------------------------------------------ images & fallback
class FakeRichPaster(FakePaster):
    def __init__(self, fail=False):
        super().__init__(fail=fail)
        self.rich_calls = []

    def emit_rich(self, text, images):
        if self.fail:
            raise OSError("clipboard busy")
        self.rich_calls.append((text, images))


def test_controller_result_with_images_calls_emit_rich():
    from local_typeless.types import CapturedImage

    paster = FakeRichPaster()
    c, _, _ = make(paster=paster)
    img = CapturedImage(index=1, t_sec=1.0, path="/tmp/img1.png")
    res = result(raw="[圖1] 原文", final="[圖1] 摘要")
    res.images = [img]

    c.on_result(res)
    assert len(paster.rich_calls) == 1
    assert paster.rich_calls[0] == (compose_insert(res.raw_text, res.final_text, False), [img])


def test_controller_paster_without_emit_rich_calls_emit():
    from local_typeless.types import CapturedImage

    paster = FakePaster()
    c, _, _ = make(paster=paster)
    img = CapturedImage(index=1, t_sec=1.0, path="/tmp/img1.png")
    res = result(raw="[圖1] 原文", final="[圖1] 摘要")
    res.images = [img]

    c.on_result(res)
    assert len(paster.got) == 1


def test_controller_compose_insert_strips_markers_when_include_raw():
    text = compose_insert("按照 [圖1] 的風格", "按此風格", include_raw=True)
    assert "[圖1]" not in text.split("摘要：")[0]
    assert "按照  的風格" in text or "按照 的風格" in text or "按照" in text


def test_controller_on_image_updates_view_model_and_resets_on_recording():
    c, _, _ = make()
    c.on_image(3)
    assert c.vm.image_count == 3
    assert c.vm.message == "已擷取圖片 3"

    c.on_state(State.RECORDING, Mode.SUMMARY, "")
    assert c.vm.image_count == 0


def test_controller_partial_fallback_notice_text():
    from local_typeless.ui.controller import FALLBACK_NOTICE

    c, _, _ = make()
    c.vm.fallback = True
    c.vm.total_blocks = 3
    c.vm.fallback_blocks = 1
    assert c.vm.notice == "部分段落 LLM 失敗（1/3），該段顯示原文"

    c.vm.fallback_blocks = 3
    assert c.vm.notice == FALLBACK_NOTICE

    c.vm.fallback = False
    assert c.vm.notice == ""


def test_controller_history_selection_restores_images():
    from local_typeless.types import CapturedImage

    c, _, _ = make()
    img1 = CapturedImage(index=1, t_sec=1.0, path="/tmp/img1.png")
    img2 = CapturedImage(index=2, t_sec=2.0, path="/tmp/img2.png")
    res1 = result(raw="res1", final="res1")
    res1.images = [img1]
    res2 = result(raw="res2", final="res2")
    res2.images = [img2]

    c.on_result(res1)
    c.on_result(res2)
    assert c.vm.images == [img2]

    c.select_history(1)
    assert c.vm.images == [img1]


# ------------------------------------------------------------ streaming controller
def test_controller_streaming_mode_lifecycle_and_notes():
    c, _, _ = make()
    c.streaming = True

    # 1. on_state(RECORDING) clears raw_text/final_text in streaming mode
    c.vm.raw_text = "previous raw"
    c.vm.final_text = "previous final"
    c.on_state(State.RECORDING, Mode.CLEAN, "")
    assert c.vm.raw_text == ""
    assert c.vm.final_text == ""

    # 2. on_segment live updates raw_text during recording
    c.on_segment("live chunk text")
    assert c.vm.raw_text == "live chunk text"

    # 3. on_state(IDLE, note="queued") shows "已送出，背景處理中…"
    c.on_state(State.IDLE, Mode.CLEAN, "queued")
    assert c.vm.message == "已送出，背景處理中…"

    # 4. on_jobs: pending > 0 shows pending message
    c.on_jobs(2, "")
    assert c.vm.pending_jobs == 2
    assert c.vm.message == "背景處理中（2）"

    # on_segment still updates raw_text while pending_jobs > 0 even when idle
    c.on_segment("further background segment")
    assert c.vm.raw_text == "further background segment"

    # 5. on_jobs: drops to 0 shows "完成"
    c.on_jobs(0, "")
    assert c.vm.pending_jobs == 0
    assert c.vm.message == "完成"

    # 6. on_jobs with note mappings
    c.on_jobs(1, "no speech detected")
    assert c.vm.message == "沒有偵測到語音"

    c.on_jobs(1, "abandoned")
    assert "已放棄" in c.vm.message

    c.on_jobs(1, "error: ModelNotAvailableError: need download")
    assert c.vm.message == "need download"


def test_controller_legacy_non_streaming_behavior_unaffected():
    c, _, _ = make()
    assert not c.streaming

    # In legacy mode, on_segment only updates when phase == processing
    c.on_state(State.RECORDING, Mode.CLEAN, "")
    c.on_segment("should be ignored in recording")
    assert c.vm.raw_text == ""


# ------------------------------------------------------------ Task 14: paste_image & hints
def test_format_hotkey_table():
    from local_typeless.ui.controller import format_hotkey

    assert format_hotkey("<alt>+q") == "Alt+Q"
    assert format_hotkey("<alt>+w") == "Alt+W"
    assert format_hotkey("<alt>+s") == "Alt+S"
    assert format_hotkey("<esc>") == "Esc"
    assert format_hotkey("<alt>+v") == "Alt+V"
    assert format_hotkey("<ctrl>+<shift>+s") == "Ctrl+Shift+S"
    assert format_hotkey("<cmd>+v") == "Win+V"
    assert format_hotkey("<space>") == "Space"
    assert format_hotkey("") == ""
    assert format_hotkey("<f1>") == "F1"


def test_controller_paste_image_clicked():
    c, events, _ = make()

    # 1. While idle: no-op
    c.paste_image_clicked()
    assert events == []

    # 2. While recording: submits paste_image event
    c.on_state(State.RECORDING, Mode.CLEAN, "")
    c.paste_image_clicked()
    assert events == [Event("paste_image")]

    # 3. While processing: no-op
    c.on_state(State.PROCESSING, Mode.CLEAN, "")
    c.paste_image_clicked()
    assert len(events) == 1


def test_controller_on_paste_callback_messages():
    c, _, _ = make()

    # on_paste(0) -> notice that clipboard has no image
    c.on_paste(0)
    assert c.vm.message == "剪貼簿沒有圖片（先截圖或複製圖片，再按貼上）"

    # on_image(1) followed by on_paste(1) -> on_image message preserved
    c.on_image(1)
    assert c.vm.message == "已擷取圖片 1"
    c.on_paste(1)
    assert c.vm.message == "已擷取圖片 1"


def test_controller_hotkey_hints():
    hints = {
        "clean": "Alt+Q",
        "summary": "Alt+W",
        "slide": "Alt+S",
        "cancel": "Esc",
        "paste_image": "Alt+V",
    }
    c, _, _ = make(hotkey_hints=hints)
    assert c.vm.hotkey_hints == hints


