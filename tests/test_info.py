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


def _controller(
    tmp_path: Path,
    glossary: str = "",
    on_glossary_changed=None,
) -> InfoController:
    prompts = tmp_path / "prompts"
    prompts.mkdir(exist_ok=True)
    cfg = AppConfig(
        stt_profiles={"cpu_fast": STTProfileConfig()},
        llm_profiles={"dev": LLMProfileConfig(base_url="http://localhost:8000/v1", model="test-model")},
        glossary_path=str(tmp_path / "config" / "glossary.yaml"),
        prompts_dir=str(prompts),
        cache_dir=str(tmp_path / "cache"),
    )
    cfg.storage.records_dir = str(tmp_path / "records")
    return InfoController(cfg, lambda: glossary, now=lambda: NOW, on_glossary_changed=on_glossary_changed)


def _write_base_glossary(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "config" / "glossary.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


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


def test_dictionary_lists_base_then_user_terms(tmp_path):
    ctl = _controller(tmp_path)
    _write_base_glossary(
        tmp_path,
        """canonical_terms:
  - term: OPC
    definition: Optical Proximity Correction
  - term: EUV
    definition: Extreme Ultraviolet Lithography
phonetic_aliases:
  欧皮西: OPC
  埃爾特: ILT
""",
    )
    user_p = tmp_path / "config" / "glossary.user.yaml"
    user_p.write_text(
        """canonical_terms:
  - term: SMO
    definition: Source Mask Optimization
phonetic_aliases:
  斯莫: SMO
  歐皮西: OPC
""",
        encoding="utf-8",
    )

    view = ctl.dictionary()
    assert view.error == ""
    assert [e.term for e in view.entries] == ["OPC", "EUV", "ILT", "SMO"]
    opc, euv, ilt, smo = view.entries
    assert opc.builtin and opc.definition == "Optical Proximity Correction"
    assert set(opc.aliases) == {"欧皮西", "歐皮西"}
    assert euv.builtin and euv.aliases == ()
    assert ilt.builtin and ilt.definition == "" and ilt.aliases == ("埃爾特",)
    assert not smo.builtin and smo.definition == "Source Mask Optimization"
    assert smo.aliases == ("斯莫",)


def test_save_term_user_term_and_backups_and_callback(tmp_path):
    called = []
    ctl = _controller(tmp_path, on_glossary_changed=lambda: called.append(True))
    _write_base_glossary(
        tmp_path,
        "canonical_terms:\n  - term: OPC\n    definition: Optical Proximity Correction\n",
    )

    # 1st save: adds user term with aliases
    ok, msg = ctl.save_term("SRAF", "Sub-Resolution Assist Feature", ["助記條", "輔助圖形"])
    assert ok and "SRAF" in msg
    assert len(called) == 1
    entries = {e.term: e for e in ctl.dictionary().entries}
    assert not entries["SRAF"].builtin
    assert set(entries["SRAF"].aliases) == {"助記條", "輔助圖形"}

    # No backup on first write because there was no prior file
    backups = list((tmp_path / "cache" / "glossary_backups").glob("*.yaml"))
    assert len(backups) == 0

    # 2nd save of same term replaces its aliases
    ok, msg = ctl.save_term("SRAF", "Sub-Resolution Assist Feature", ["輔助圖形", "sraf條"])
    assert ok
    assert len(called) == 2
    entries2 = {e.term: e for e in ctl.dictionary().entries}
    assert set(entries2["SRAF"].aliases) == {"輔助圖形", "sraf條"}

    # Backup file appears from the second write on
    backups = list((tmp_path / "cache" / "glossary_backups").glob("*.yaml"))
    assert len(backups) == 1
    assert "助記條" in backups[0].read_text(encoding="utf-8")


def test_rename_and_delete_user_term_and_refuse_builtin(tmp_path):
    ctl = _controller(tmp_path)
    _write_base_glossary(
        tmp_path,
        "canonical_terms:\n  - term: OPC\n    definition: Optical Proximity Correction\n",
    )
    ctl.save_term("curvilinear mask", "曲線光罩", ["曲面光罩"])

    # Rename via original
    ok, msg = ctl.save_term(
        "curvilinear mask v2", "曲線光罩第二版", ["曲面光罩"], original="curvilinear mask"
    )
    assert ok
    terms = [e.term for e in ctl.dictionary().entries]
    assert "curvilinear mask v2" in terms and "curvilinear mask" not in terms

    # Delete user term
    ok, msg = ctl.delete_term("curvilinear mask v2")
    assert ok
    terms = [e.term for e in ctl.dictionary().entries]
    assert "curvilinear mask v2" not in terms

    # Refuse to delete built-in term
    ok, msg = ctl.delete_term("OPC")
    assert not ok and msg == "內建詞不能在這裡刪，請改 glossary.yaml"


def test_builtin_term_saves_only_new_alias(tmp_path):
    import yaml

    ctl = _controller(tmp_path)
    _write_base_glossary(
        tmp_path,
        """canonical_terms:
  - term: EUV
    definition: Extreme Ultraviolet
phonetic_aliases:
  極紫外: EUV
""",
    )
    ok, msg = ctl.save_term("EUV", "不同定義", ["極紫外", "極紫外光"])
    assert ok and "以內建" in msg

    user_p = tmp_path / "config" / "glossary.user.yaml"
    u_data = yaml.safe_load(user_p.read_text(encoding="utf-8"))

    # Canonical terms must NOT have EUV (definition not saved)
    u_terms = [
        item.get("term") if isinstance(item, dict) else item
        for item in u_data.get("canonical_terms", [])
    ]
    assert "EUV" not in u_terms

    # Only alias not in base file reaches user file
    assert u_data["phonetic_aliases"] == {"極紫外光": "EUV"}

    # Base definition wins
    view = ctl.dictionary()
    euv = next(e for e in view.entries if e.term == "EUV")
    assert euv.definition == "Extreme Ultraviolet"
    assert set(euv.aliases) == {"極紫外", "極紫外光"}


def test_alias_pointing_to_another_term_and_empty_term_refused(tmp_path):
    ctl = _controller(tmp_path)
    _write_base_glossary(
        tmp_path,
        """canonical_terms:
  - term: OPC
    definition: Optical Proximity Correction
phonetic_aliases:
  歐皮西: OPC
""",
    )
    # Alias points to OPC already
    ok, msg = ctl.save_term("ILT", "Inverse Lithography", ["歐皮西"])
    assert not ok and "OPC" in msg and "歐皮西" in msg

    # Empty term
    ok, msg = ctl.save_term("   ", "Definition", ["alias"])
    assert not ok and "空的" in msg


def test_broken_user_file_handling(tmp_path):
    ctl = _controller(tmp_path)
    _write_base_glossary(
        tmp_path,
        "canonical_terms:\n  - term: cost function\n    definition: 成本函數\n",
    )
    user_p = tmp_path / "config" / "glossary.user.yaml"
    user_p.parent.mkdir(parents=True, exist_ok=True)
    broken_content = "[unclosed list"
    user_p.write_text(broken_content, encoding="utf-8")

    # dictionary still returns base terms with error set
    view = ctl.dictionary()
    assert view.error != ""
    assert [e.term for e in view.entries] == ["cost function"]

    # save_term refuses without changing the file
    ok, msg = ctl.save_term("SMO", "Source Mask Optimization", [])
    assert not ok and ("損毀" in msg or "錯誤" in msg)
    assert user_p.read_text(encoding="utf-8") == broken_content


def test_glossary_manager_sees_saved_term(tmp_path):
    from local_typeless.glossary.manager import GlossaryManager

    ctl = _controller(tmp_path)
    _write_base_glossary(
        tmp_path,
        "canonical_terms:\n  - term: OPC\n    definition: Optical Proximity Correction\n",
    )
    ok, msg = ctl.save_term("ILT", "Inverse Lithography Technology", ["埃爾特"])
    assert ok

    mgr = GlossaryManager.from_yaml(ctl.cfg.glossary_path)
    assert any(t["term"] == "ILT" for t in mgr.canonical_terms)
    assert mgr.phonetic_aliases.get("埃爾特") == "ILT"


def test_dictionary_missing_base_file_does_not_crash(tmp_path):
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display")
    root.withdraw()
    try:
        from local_typeless.ui.info_window import InfoWindow

        ctl = _controller(tmp_path)
        view = ctl.dictionary()
        assert view.error != "" and view.entries == []

        win = InfoWindow(root, ctl)
        win.show("dictionary")
        root.update()
        assert win.dictionary_rows() == []
        win.close()
    finally:
        root.destroy()


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
        _write_base_glossary(
            tmp_path,
            "canonical_terms:\n  - term: OPC\n    definition: 光學鄰近修正\nphonetic_aliases:\n  歐皮西: OPC\n",
        )
        (tmp_path / "prompts" / "summary.md").write_text("摘要口述。{glossary}", encoding="utf-8")
        _record(tmp_path / "records", "2026-11-14T09:00:00", "clean", "比較 OPC 跟 ILT。")
        win = InfoWindow(root, ctl, mode_getter=lambda: Mode.SUMMARY)
        for page in ("home", "history", "dictionary", "prompt"):
            win.show(page)
            root.update()
        assert win._prompt_mode == Mode.SUMMARY
        assert win.prompt_editor_text() == "摘要口述。{glossary}"

        # check dictionary_rows and save_term_clicked
        win.show("dictionary")
        root.update()
        assert win.dictionary_rows() == ["OPC"]

        win._dict_term_var.set("ILT")
        win._dict_def_var.set("反向光學微影技術")
        win._dict_aliases_var.set("埃爾特")
        ok, _msg = win.save_term_clicked()
        assert ok
        root.update()
        assert win.dictionary_rows() == ["OPC", "ILT"]

        # edit, browse away and back: the edit is still there; "載入" writes the file
        win.show("prompt")
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


def _settings_controller(tmp_path: Path):
    ctl = _controller(tmp_path)
    ctl._settings_file = tmp_path / "config" / "settings.local.yaml"
    return ctl


def test_settings_are_saved_and_used_at_once(tmp_path):
    ctl = _settings_controller(tmp_path)
    view = ctl.settings()
    assert (view.language, view.min_chunk_sec, view.silence_sec) == ("", 12.0, 0.5)

    ok, message = ctl.save_settings("zh", "6", "0.4", "20", view.cache_dir, view.records_dir)
    assert ok and "重開" not in message
    assert ctl.cfg.stt.language == "zh"                       # the live objects the app reads
    assert (ctl.cfg.streaming.min_chunk_sec, ctl.cfg.streaming.max_chunk_sec) == (6.0, 20.0)

    ok, _message = ctl.save_settings("", "6", "0.4", "20", view.cache_dir, view.records_dir)
    assert ok and ctl.cfg.stt.language is None                # back to auto-detect

    new_records = tmp_path / "elsewhere" / "records"
    ok, message = ctl.save_settings("", "6", "0.4", "20", view.cache_dir, str(new_records))
    assert ok and "重開" in message and new_records.is_dir()


@pytest.mark.parametrize(
    "args",
    [("xx", "6", "0.5", "20"), ("zh", "abc", "0.5", "20"), ("zh", "1", "0.5", "20"),
     ("zh", "6", "9", "20"), ("zh", "20", "0.5", "20")],
)
def test_settings_refuse_bad_values(tmp_path, args):
    ctl = _settings_controller(tmp_path)
    view = ctl.settings()
    ok, _message = ctl.save_settings(*args, view.cache_dir, view.records_dir)
    assert not ok and not ctl._settings_file.exists()
    assert ctl.cfg.streaming.min_chunk_sec == 12.0


def test_settings_file_overrides_the_config_at_the_next_start(tmp_path):
    import yaml

    from local_typeless.config.loader import load_config, settings_path

    config = tmp_path / "config" / "config.yaml"
    config.parent.mkdir()
    config.write_text(yaml.safe_dump({
        "stt_profiles": {"cpu_fast": {"beam_size": 2}},
        "llm_profiles": {"dev": {"base_url": "http://localhost:8000/v1", "model": "test-model"}},
    }), encoding="utf-8")
    cfg = load_config(config)
    assert cfg.stt.language is None and cfg.streaming.min_chunk_sec == 12.0

    ctl = InfoController(cfg, settings_file=settings_path(config))
    records = tmp_path / "my records"
    assert ctl.save_settings("zh", 6, 0.5, 20, cfg.cache_dir, str(records))[0]

    cfg = load_config(config)
    assert cfg.stt.language == "zh" and cfg.stt.beam_size == 2      # merged, not replaced
    assert (cfg.streaming.min_chunk_sec, cfg.streaming.max_chunk_sec) == (6.0, 20.0)
    assert Path(cfg.storage.records_dir) == records

    settings_path(config).write_text("- broken", encoding="utf-8")   # ignored, not fatal
    assert load_config(config).streaming.min_chunk_sec == 12.0


def test_settings_page_saves_from_the_form(tmp_path):
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display")
    root.withdraw()
    try:
        from local_typeless.ui.info_window import InfoWindow

        ctl = _settings_controller(tmp_path)
        win = InfoWindow(root, ctl)
        win.show("settings")
        root.update()
        win._settings_vars["language"].set("中文為主（比較快）")
        win._settings_vars["min_chunk_sec"].set("8")
        ok, _message = win.save_settings_clicked()
        assert ok and ctl.cfg.stt.language == "zh" and ctl.cfg.streaming.min_chunk_sec == 8.0
        win._settings_vars["min_chunk_sec"].set("many")
        assert not win.save_settings_clicked()[0]
        win.close()
    finally:
        root.destroy()


def test_settings_choose_the_recognition_model_for_the_next_start(tmp_path):
    import yaml

    import copy

    ctl = _settings_controller(tmp_path)
    bigger = copy.copy(ctl.cfg.stt)
    bigger.model_size = "large-v3-turbo"
    ctl.cfg.stt_profiles["cpu_accurate"] = bigger
    view = ctl.settings()
    names = [name for name, _label in view.stt_profiles]
    assert view.stt_profile == ctl.cfg.active_stt_profile and len(names) >= 2
    other = next(n for n in names if n != view.stt_profile)
    args = ("", "12", "0.5", "28", view.cache_dir, view.records_dir)

    assert not ctl.save_settings(*args, stt_profile="nope")[0]
    ok, message = ctl.save_settings(*args, stt_profile=other)
    assert ok and "辨識模型要重開" in message
    assert ctl.cfg.active_stt_profile == view.stt_profile      # the running engine is untouched
    assert ctl.settings().stt_profile == other                 # the page shows what was chosen
    saved = yaml.safe_load(ctl._settings_file.read_text(encoding="utf-8"))
    assert saved["active_stt_profile"] == other

    ok, message = ctl.save_settings(*args, stt_profile=other)  # same again: nothing to restart
    assert ok and "重開" not in message
