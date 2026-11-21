"""Tests for editor configuration loading and defaulting."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from local_typeless.config.loader import load_config
from local_typeless.editor.config import EditorConfig, load_editor_config


def test_editor_config_defaults():
    cfg = EditorConfig()
    assert cfg.prompts_root == "~/Documents/prompts"
    assert cfg.file_layout == "{yyyy}/{mm}/{date}_{time}_{title}.md"
    assert cfg.auto_rename_after_refine is True
    assert cfg.autosave_delay_ms == 1000

    assert cfg.server.port == 8765
    assert cfg.server.autostart_cmd == ""

    assert cfg.paste.image_dir == "assets/{yyyy}-{mm}"
    assert cfg.paste.image_name == "{prompt}_{n:02}.png"
    assert cfg.paste.path_style == "windows"
    assert cfg.paste.quote_paths_with_spaces is True
    assert cfg.paste.image_preview_on_hover is True

    assert cfg.refine.default_mode == "clean"
    assert cfg.refine.modes_in_palette == ["clean", "prompt", "slide", "summary", "transform"]
    assert cfg.refine.result_view == "split_right"
    assert cfg.refine.protect_paths is True
    assert cfg.refine.protect_code_blocks is True

    assert cfg.send.copy == "refined"
    assert cfg.send.auto_paste_to_last_window is False
    assert cfg.send.mark_sent_in_frontmatter is True

    assert cfg.frontmatter.fields == ["created", "status", "mode", "refined_at", "sent_at", "target"]


def test_load_editor_config_from_example():
    cfg = load_editor_config("config/config.example.yaml")
    assert cfg.server.port == 8765
    assert Path(cfg.prompts_root).is_absolute()
    assert cfg.refine.default_mode == "clean"


def test_existing_load_config_unaffected_by_editor_section():
    app_cfg = load_config("config/config.example.yaml")
    assert app_cfg.active_stt_profile == "cpu_fast"
    assert app_cfg.active_llm_profile == "dev"


def test_load_editor_config_with_settings_override(tmp_path: Path):
    cfg_file = tmp_path / "config.yaml"
    cfg_data = {
        "active_stt_profile": "cpu_fast",
        "active_llm_profile": "dev",
        "stt_profiles": {"cpu_fast": {"device": "cpu", "model_size": "small"}},
        "llm_profiles": {"dev": {"provider": "openai_compatible", "base_url": "http://127.0.0.1:8000/v1"}},
        "editor": {
            "server": {"port": 8888},
            "refine": {"default_mode": "prompt"},
        },
    }
    cfg_file.write_text(yaml.dump(cfg_data), encoding="utf-8")

    settings_file = tmp_path / "settings.local.yaml"
    settings_data = {
        "editor": {
            "server": {"port": 9090},
        }
    }
    settings_file.write_text(yaml.dump(settings_data), encoding="utf-8")

    loaded = load_editor_config(cfg_file)
    assert loaded.server.port == 9090
    assert loaded.refine.default_mode == "prompt"


def test_load_editor_config_with_errors_field_fault_tolerance(tmp_path: Path):
    from local_typeless.editor.config import load_editor_config_with_errors

    cfg_file = tmp_path / "config.yaml"
    cfg_data = {
        "active_llm_profile": "dev",
        "editor": {
            "prompts_root": str(tmp_path / "my_prompts"),
            "autosave_delay_ms": 50,  # Invalid: < 100
            "server": {
                "port": 99999,  # Invalid: > 65535
            },
            "paste": {
                "path_style": "invalid_style",  # Invalid
                "quote_paths_with_spaces": False,  # Valid
            },
            "refine": {
                "default_mode": "nonexistent_mode",  # Invalid
                "result_view": "floating",  # Invalid
                "modes_in_palette": ["clean", "bogus", "summary"],  # Partial invalid
                "protect_paths": False,  # Valid
            },
            "send": {
                "copy": "everything",  # Invalid
                "auto_paste_to_last_window": True,  # Valid
            },
        },
    }
    cfg_file.write_text(yaml.dump(cfg_data), encoding="utf-8")

    cfg, errors, resolved_cfg, resolved_sett = load_editor_config_with_errors(cfg_file)

    # Valid fields are preserved
    assert cfg.prompts_root == str((tmp_path / "my_prompts").resolve())
    assert cfg.paste.quote_paths_with_spaces is False
    assert cfg.refine.protect_paths is False
    assert cfg.send.auto_paste_to_last_window is True

    # Invalid fields fell back to default
    assert cfg.autosave_delay_ms == 1000
    assert cfg.server.port == 8765
    assert cfg.paste.path_style == "windows"
    assert cfg.refine.default_mode == "clean"
    assert cfg.refine.result_view == "split_right"
    assert cfg.refine.modes_in_palette == ["clean", "summary"]
    assert cfg.send.copy == "refined"

    # Errors recorded
    assert len(errors) >= 7
    assert any("autosave_delay_ms" in e for e in errors)
    assert any("server.port" in e for e in errors)
    assert any("paste.path_style" in e for e in errors)
    assert any("refine.default_mode" in e for e in errors)
    assert any("refine.result_view" in e for e in errors)
    assert any("modes_in_palette" in e for e in errors)
    assert any("send.copy" in e for e in errors)
    assert resolved_cfg == cfg_file.resolve()

