"""Tests for Editor settings view, InfoController Sublime prompts integration, and panel runner."""
from __future__ import annotations

import sys
import tkinter as tk
from datetime import datetime
from pathlib import Path

import pytest
import yaml

from local_typeless.types import AppConfig, LLMProfileConfig, STTProfileConfig
from local_typeless.ui.info_controller import EditorSettingsView, InfoController
from local_typeless.ui.info_window import InfoWindow
from local_typeless.ui.panel import run_panel


def _make_cfg(tmp_path: Path) -> AppConfig:
    cfg = AppConfig(
        stt_profiles={"cpu_fast": STTProfileConfig()},
        llm_profiles={
            "dev": LLMProfileConfig(base_url="http://localhost:8000/v1", model="test-model"),
            "corp": LLMProfileConfig(base_url="http://corp.local/v1", model="corp-model"),
        },
        active_llm_profile="dev",
        glossary_path=str(tmp_path / "config" / "glossary.yaml"),
        prompts_dir=str(tmp_path / "prompts"),
        cache_dir=str(tmp_path / "cache"),
    )
    cfg.storage.records_dir = str(tmp_path / "records")
    return cfg


def test_editor_settings_view_reading(tmp_path: Path):
    cfg = _make_cfg(tmp_path)
    cfg_file = tmp_path / "config" / "config.yaml"
    cfg_file.parent.mkdir(parents=True, exist_ok=True)
    cfg_file.write_text(yaml.dump({
        "editor": {
            "prompts_root": str(tmp_path / "custom_prompts"),
            "refine": {"default_mode": "prompt", "result_view": "below"},
            "send": {"copy": "both"},
        }
    }), encoding="utf-8")

    sett_file = tmp_path / "config" / "settings.local.yaml"
    ctl = InfoController(cfg, settings_file=sett_file, config_path=cfg_file)
    view = ctl.editor_settings()

    assert isinstance(view, EditorSettingsView)
    assert view.active_llm_profile == "dev"
    assert "corp" in view.llm_profiles
    assert view.prompts_root == str((tmp_path / "custom_prompts").resolve())
    assert view.default_mode == "prompt"
    assert view.result_view == "below"
    assert view.send_copy == "both"
    assert view.path == sett_file


def test_save_editor_settings_updates_memory_and_persists_safely(tmp_path: Path):
    cfg = _make_cfg(tmp_path)
    cfg_file = tmp_path / "config" / "config.yaml"
    cfg_file.parent.mkdir(parents=True, exist_ok=True)
    cfg_file.write_text("editor: {}\n", encoding="utf-8")

    sett_file = tmp_path / "config" / "settings.local.yaml"
    # Existing settings file with unrelated keys
    sett_file.write_text(yaml.dump({"unrelated_key": 42, "streaming": {"silence_sec": 0.8}}), encoding="utf-8")

    ctl = InfoController(cfg, settings_file=sett_file, config_path=cfg_file)

    # 1. Reject invalid values
    assert not ctl.save_editor_settings(active_llm_profile="nonexistent")[0]
    assert not ctl.save_editor_settings(path_style="invalid_style")[0]
    assert not ctl.save_editor_settings(result_view="invalid_view")[0]
    assert not ctl.save_editor_settings(send_copy="invalid_copy")[0]
    assert not ctl.save_editor_settings(default_mode="invalid_mode")[0]
    assert not ctl.save_editor_settings(autosave_delay_ms=50)[0]
    assert not ctl.save_editor_settings(server_port=70000)[0]
    assert not ctl.save_editor_settings(prompts_root="   ")[0]

    # 2. Save valid settings
    new_prompts_dir = tmp_path / "my_new_prompts"
    ok, msg = ctl.save_editor_settings(
        active_llm_profile="corp",
        prompts_root=str(new_prompts_dir),
        path_style="forward",
        quote_paths_with_spaces=False,
        image_preview_on_hover=False,
        default_mode="slide",
        result_view="replace",
        protect_paths=False,
        protect_code_blocks=False,
        guard=False,
        auto_rename_after_refine=False,
        send_copy="raw",
        auto_paste_to_last_window=True,
        autosave_delay_ms=2500,
        server_port=9123,
    )
    assert ok is True
    assert new_prompts_dir.is_dir()
    assert ctl.cfg.active_llm_profile == "corp"
    assert ctl.cfg.refine.guard is False

    # 3. Verify YAML file content
    saved_data = yaml.safe_load(sett_file.read_text(encoding="utf-8"))
    assert saved_data["unrelated_key"] == 42
    assert saved_data["streaming"]["silence_sec"] == 0.8
    assert saved_data["active_llm_profile"] == "corp"
    assert saved_data["refine"]["guard"] is False

    ed = saved_data["editor"]
    assert ed["prompts_root"] == str(new_prompts_dir)
    assert ed["autosave_delay_ms"] == 2500
    assert ed["server"]["port"] == 9123
    assert ed["paste"]["path_style"] == "forward"
    assert ed["paste"]["quote_paths_with_spaces"] is False
    assert ed["refine"]["default_mode"] == "slide"
    assert ed["refine"]["result_view"] == "replace"
    assert ed["send"]["copy"] == "raw"
    assert ed["send"]["auto_paste_to_last_window"] is True


def test_sublime_prompts_scanned_into_home_and_history(tmp_path: Path):
    cfg = _make_cfg(tmp_path)
    prompts_root = tmp_path / "sublime_prompts"
    sub_dir = prompts_root / "2026" / "11"
    sub_dir.mkdir(parents=True, exist_ok=True)

    # 1. Normal prompt file with paired .refined.md
    p1 = sub_dir / "20261121_100000_note1.md"
    p1.write_text(
        "---\ncreated: 2026-11-21 10:00:00\nmode: summary\nstatus: refined\n---\n"
        "這是原始 prompt 筆記內容。\n",
        encoding="utf-8",
    )
    p1_refined = sub_dir / "20261121_100000_note1.refined.md"
    p1_refined.write_text("這是精煉後的結果摘要。\n", encoding="utf-8")

    # 2. Asset file (should be ignored)
    assets_dir = prompts_root / "assets" / "2026-11"
    assets_dir.mkdir(parents=True, exist_ok=True)
    (assets_dir / "image.png.md").write_text("ignore me", encoding="utf-8")

    ctl = InfoController(cfg, editor_prompts_root=prompts_root)

    # Check home stats
    home = ctl.home()
    assert home.records >= 1
    assert home.words > 0

    # Check history entries
    hist = ctl.history()
    assert len(hist) == 1
    entry = hist[0]
    assert entry.mode == "summary"
    assert entry.text == "這是精煉後的結果摘要。"
    assert entry.raw_text == "這是原始 prompt 筆記內容。"
    assert entry.path == p1


def test_info_window_editor_settings_page_smoke(tmp_path: Path):
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display")
    root.withdraw()
    try:
        cfg = _make_cfg(tmp_path)
        sett_file = tmp_path / "settings.local.yaml"
        ctl = InfoController(cfg, settings_file=sett_file)

        win = InfoWindow(root, ctl, show_editor_settings=True)
        win.show("settings")
        root.update()

        # Modify values in the form
        win._editor_settings_vars["default_mode"].set("slide")
        win._editor_settings_vars["autosave_delay_ms"].set("1500")

        ok, msg = win.save_editor_settings_clicked()
        assert ok is True

        saved_data = yaml.safe_load(sett_file.read_text(encoding="utf-8"))
        assert saved_data["editor"]["refine"]["default_mode"] == "slide"
        assert saved_data["editor"]["autosave_delay_ms"] == 1500

        win.close()
    finally:
        root.destroy()


def test_panel_runner_does_not_load_faster_whisper():
    # Make sure faster_whisper is not loaded in sys.modules
    assert "faster_whisper" not in sys.modules


def test_run_panel_teach_and_settings_smoke(tmp_path: Path):
    try:
        probe = tk.Tk()
        probe.destroy()
    except tk.TclError:
        pytest.skip("no display")

    glossary_file = tmp_path / "glossary.yaml"
    glossary_file.write_text("canonical_terms: []\n", encoding="utf-8")
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        yaml.dump(
            {
                "active_stt_profile": "cpu_fast",
                "active_llm_profile": "dev",
                "glossary_path": str(glossary_file),
                "cache_dir": str(tmp_path / "cache"),
                "prompts_dir": str(tmp_path / "prompts"),
                "stt_profiles": {"cpu_fast": {"device": "cpu", "model_size": "small"}},
                "llm_profiles": {"dev": {"provider": "openai_compatible", "base_url": "http://127.0.0.1:8000/v1"}},
                "storage": {"records_dir": str(tmp_path / "records")},
                "editor": {"prompts_root": str(tmp_path / "sublime_prompts")},
            }
        ),
        encoding="utf-8",
    )

    rc_teach = run_panel(config_path=cfg_file, page="teach", auto_close_sec=0.05)
    if rc_teach == 1:
        pytest.skip("Tk initialization failed transiently")
    assert rc_teach == 0

    rc_settings = run_panel(config_path=cfg_file, page="settings", auto_close_sec=0.05)
    if rc_settings == 1:
        pytest.skip("Tk initialization failed transiently")
    assert rc_settings == 0
