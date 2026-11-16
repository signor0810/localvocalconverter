"""Info panel: Home numbers, History and System prompt (data layer + a window smoke test).

Sample sentences use public computational-lithography terms only.
"""
from datetime import date, datetime
from pathlib import Path

import pytest

from local_typeless.types import AppConfig, LLMProfileConfig, Mode, STTProfileConfig
from local_typeless.ui.info_controller import InfoController, count_units, streaks
from local_typeless.ui.info_window import format_day, heat_level

NOW = datetime(2026, 11, 15, 10, 0, 0)


def _record(root: Path, when: str, mode: str, result: str, raw: str = "原文", fallback: bool = False) -> None:
    month = root / when[:7]
    month.mkdir(parents=True, exist_ok=True)
    name = when.replace(":", "").replace("-", "").replace("T", "-")
    (month / f"{name}_{mode}.md").write_text(
        f"---\ntime: {when}\nmode: {mode}\nfallback: {str(fallback).lower()}\nimages: 0\n---\n\n"
        f"## 結果\n\n{result}\n\n## 原文\n\n{raw}\n", encoding="utf-8")


def _controller(tmp_path: Path, glossary: str = "") -> InfoController:
    prompts = tmp_path / "prompts"
    prompts.mkdir(exist_ok=True)
    cfg = AppConfig(stt_profiles={"cpu_fast": STTProfileConfig()},
                    llm_profiles={"dev": LLMProfileConfig(base_url="http://localhost:8000/v1", model="test-model")},
                    prompts_dir=str(prompts),
                    cache_dir=str(tmp_path / "cache"))
    cfg.storage.records_dir = str(tmp_path / "records")
    return InfoController(cfg, lambda: glossary, now=lambda: NOW)


def test_count_units_counts_chinese_characters_and_english_words():
    assert count_units("OPC 的 cost function 是 3.5") == 6
    assert count_units("請看 [檔案1] 跟 [圖2]") == 3
    assert count_units("") == 0


@pytest.mark.parametrize(
    "days, expected",
    [
        ([], (0, 0)),
        ([date(2026, 11, 15)], (1, 1)),
        ([date(2026, 11, 13), date(2026, 11, 14)], (2, 2)),                    # may end yesterday
        ([date(2026, 11, 1), date(2026, 11, 2), date(2026, 11, 3), date(2026, 11, 15)], (1, 3)),
        ([date(2026, 11, 10), date(2026, 11, 11)], (0, 2)),                    # broken off
    ],
)
def test_streaks(days, expected):
    assert streaks(days, NOW.date()) == expected


def test_home_numbers_come_from_the_record_folder(tmp_path):
    ctl = _controller(tmp_path)
    rec = tmp_path / "records"
    _record(rec, "2026-11-13T09:00:00", "clean", "比較 OPC 跟 ILT 的差異。")
    _record(rec, "2026-11-14T09:00:00", "summary", "討論 EUV。")
    _record(rec, "2026-11-14T15:30:00", "clean", "請看結果", fallback=True)
    (rec / "notes.md").write_text("not a record", encoding="utf-8")       # outside a month folder

    stats = ctl.home()
    assert (stats.records, stats.fallbacks, stats.active_days) == (3, 1, 2)
    assert stats.words == count_units("比較 OPC 跟 ILT 的差異。") + count_units("討論 EUV。") + 4
    assert (stats.current_streak, stats.longest_streak) == (2, 2)
    assert stats.day_counts == {date(2026, 11, 13): 1, date(2026, 11, 14): 2}
    assert ("開始／停止錄音", "Alt+Q") in stats.hotkeys
    assert dict(stats.engine)["防呆檢查"] == "開"


def test_home_with_no_records(tmp_path):
    stats = _controller(tmp_path).home()
    assert (stats.records, stats.words, stats.active_days, stats.current_streak) == (0, 0, 0, 0)


def test_history_is_newest_first_and_filters_by_mode(tmp_path):
    ctl = _controller(tmp_path)
    rec = tmp_path / "records"
    _record(rec, "2026-10-30T09:00:00", "clean", "第一筆")
    _record(rec, "2026-11-14T09:00:00", "prompt", "第二筆", fallback=True)
    _record(rec, "2026-11-14T15:30:00", "clean", "第三筆")

    entries = ctl.history()
    assert [e.text for e in entries] == ["第三筆", "第二筆", "第一筆"]
    assert entries[1].mode_label == "AI 提示" and entries[1].fallback
    assert entries[0].when == datetime(2026, 11, 14, 15, 30)
    assert [e.text for e in ctl.history(Mode.CLEAN.value)] == ["第三筆", "第一筆"]
    assert [e.text for e in ctl.history(limit=1)] == ["第三筆"]


def test_prompt_page_shows_what_the_llm_receives(tmp_path):
    ctl = _controller(tmp_path, glossary='- "OPC": Optical Proximity Correction')
    prompts = tmp_path / "prompts"
    (prompts / "clean.md").write_text("整理口述。\n{glossary}", encoding="utf-8")

    view = ctl.prompt(Mode.CLEAN)
    assert view.error == "" and not view.has_user_rules
    assert view.text == '整理口述。\n- "OPC": Optical Proximity Correction'
    assert view.base_path == prompts / "clean.md"

    (prompts / "clean.user.md").write_text("- EUV 一律大寫", encoding="utf-8")
    view = ctl.prompt(Mode.CLEAN)
    assert view.has_user_rules and "EUV 一律大寫" in view.text and "Optical Proximity" in view.text


def test_prompt_page_reports_a_missing_file(tmp_path):
    view = _controller(tmp_path).prompt(Mode.SLIDE)
    assert view.text == "" and "讀不到" in view.error


def test_save_prompt_goes_live_and_keeps_the_old_version(tmp_path):
    ctl = _controller(tmp_path, glossary="- OPC")
    path = tmp_path / "prompts" / "clean.md"
    path.write_text("舊的指示。\n{glossary}\n", encoding="utf-8")
    assert ctl.prompt_source(Mode.CLEAN) == "舊的指示。\n{glossary}\n"

    ok, message = ctl.save_prompt(Mode.CLEAN, "新的指示。\n{glossary}")
    assert ok and "注意" not in message
    assert ctl.prompt(Mode.CLEAN).text == "新的指示。\n- OPC\n"       # what the next refine sends
    backups = list((tmp_path / "cache" / "prompt_backups").glob("clean-*.md"))
    assert len(backups) == 1 and backups[0].read_text(encoding="utf-8") == "舊的指示。\n{glossary}\n"

    ok, message = ctl.save_prompt(Mode.CLEAN, "新的指示。\n{glossary}")      # unchanged: no new backup
    assert ok and "沒有變" in message
    assert len(list((tmp_path / "cache" / "prompt_backups").glob("*.md"))) == 1


def test_save_prompt_refuses_empty_text_and_warns_without_glossary(tmp_path):
    ctl = _controller(tmp_path)
    path = tmp_path / "prompts" / "clean.md"
    path.write_text("指示。{glossary}", encoding="utf-8")
    ok, message = ctl.save_prompt(Mode.CLEAN, "  \n")
    assert not ok and path.read_text(encoding="utf-8") == "指示。{glossary}"
    ok, message = ctl.save_prompt(Mode.CLEAN, "只有指示。")
    assert ok and "{glossary}" in message and "注意" in message


def test_save_prompt_line_ends_backups_and_odd_files(tmp_path):
    clock = [datetime(2026, 11, 15, 10, 0, 0)]
    ctl = _controller(tmp_path)
    ctl._now = lambda: clock[0]
    path = tmp_path / "prompts" / "clean.md"
    path.write_bytes("第一版。\r\n{glossary}\r\n".encode("utf-8"))
    assert ctl.prompt_source(Mode.CLEAN) == "第一版。\n{glossary}\n"

    assert ctl.save_prompt(Mode.CLEAN, "第二版。\n{glossary}")[0]
    assert path.read_bytes() == "第二版。\n{glossary}\n".encode("utf-8")      # no \r\n on Windows
    assert ctl.save_prompt(Mode.CLEAN, "第三版。\n{glossary}")[0]             # same second
    backups = sorted(p.read_bytes() for p in (tmp_path / "cache" / "prompt_backups").glob("*.md"))
    assert backups == sorted(["第一版。\r\n{glossary}\r\n".encode("utf-8"),
                              "第二版。\n{glossary}\n".encode("utf-8")])

    path.write_bytes("舊編碼的指示".encode("cp950"))                          # not UTF-8
    assert ctl.prompt_source(Mode.CLEAN) != ""
    ok, _message = ctl.save_prompt(Mode.CLEAN, "修好的指示。{glossary}")
    assert ok and path.read_text(encoding="utf-8") == "修好的指示。{glossary}\n"


def test_history_accepts_a_record_with_a_timezone(tmp_path):
    ctl = _controller(tmp_path)
    rec = tmp_path / "records"
    _record(rec, "2026-11-14T09:00:00", "clean", "沒有時區")
    _record(rec, "2026-11-14T15:30:00", "clean", "placeholder")
    aware = next((rec / "2026-11").glob("*1530*"))
    aware.write_text(aware.read_text(encoding="utf-8").replace(
        "time: 2026-11-14T15:30:00", "time: 2026-11-14T15:30:00+08:00").replace("placeholder", "有時區"),
        encoding="utf-8")
    assert {e.text for e in ctl.history()} == {"沒有時區", "有時區"}
    assert ctl.home().records == 2


def test_heat_levels_and_day_format():
    assert [heat_level(n) for n in (0, 1, 2, 3, 5, 6)] == [0, 1, 1, 2, 2, 3]
    assert format_day(date(2026, 11, 15)) == "2026 年 11 月 15 日（日）"


def test_window_builds_every_page(tmp_path):
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display")
    root.withdraw()
    try:
        from local_typeless.ui.info_window import InfoWindow

        ctl = _controller(tmp_path)
        (tmp_path / "prompts" / "summary.md").write_text("摘要口述。{glossary}", encoding="utf-8")
        _record(tmp_path / "records", "2026-11-14T09:00:00", "clean", "比較 OPC 跟 ILT。")
        win = InfoWindow(root, ctl, mode_getter=lambda: Mode.SUMMARY)
        for page in ("home", "history", "prompt"):
            win.show(page)
            root.update()
        assert win._prompt_mode == Mode.SUMMARY
        assert win.prompt_editor_text() == "摘要口述。{glossary}"

        # edit, browse away and back: the edit is still there; "載入" writes the file
        win._prompt_box.insert("end", "\n- 只列重點")
        win.show("home")
        win.show("prompt")
        root.update()
        assert win.prompt_editor_text().endswith("- 只列重點")
        ok, _message = win.load_prompt_clicked()
        assert ok and ctl.prompt(Mode.SUMMARY).text == "摘要口述。\n- 只列重點\n"
        assert win._prompt_edits == {}
        win.close()
    finally:
        root.destroy()
