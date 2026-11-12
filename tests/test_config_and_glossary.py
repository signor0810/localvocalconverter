"""Unit tests for WP1-A: Config loader, prompts, and Glossary manager."""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from local_typeless.config import load_config, load_prompt, user_prompt_path
from local_typeless.glossary import GlossaryManager, user_overlay_path
from local_typeless.types import (
    AppConfig,
    GlossaryProvider,
    HotkeyConfig,
    ImagesConfig,
    LLMProfileConfig,
    Mode,
    OutputConfig,
    RecorderConfig,
    RefineConfig,
    STTProfileConfig,
    StreamingConfig,
)


def test_load_config_defaults():
    """Verify loading default config.example.yaml returns valid AppConfig dataclass."""
    cfg = load_config()
    assert isinstance(cfg, AppConfig)
    assert cfg.active_stt_profile == "cpu_fast"
    assert cfg.active_llm_profile == "dev"

    # Properties
    assert isinstance(cfg.stt, STTProfileConfig)
    assert isinstance(cfg.llm, LLMProfileConfig)
    assert cfg.stt.device == "cpu"
    assert cfg.stt.model_size == "small"
    assert cfg.stt.compute_type == "int8"
    assert cfg.stt.beam_size == 2
    assert cfg.stt.vad_filter is True

    # STT profiles
    assert "cpu_fast" in cfg.stt_profiles
    assert "cpu_accurate" in cfg.stt_profiles
    assert "gpu_local" in cfg.stt_profiles

    gpu = cfg.stt_profiles["gpu_local"]
    assert gpu.device == "cuda"
    assert gpu.model_size == "large-v3-turbo"
    assert gpu.compute_type == "int8_float16"
    assert gpu.beam_size == 5

    # LLM profiles
    assert "dev" in cfg.llm_profiles
    assert "corp" in cfg.llm_profiles
    assert cfg.llm_profiles["dev"].provider == "openai_compatible"
    assert cfg.llm_profiles["corp"].provider == "gateway"
    assert cfg.llm_profiles["dev"].extra_body == {
        "chat_template_kwargs": {"enable_thinking": False}
    }
    assert cfg.llm_profiles["corp"].extra_body == {}

    # Sub-configs
    assert isinstance(cfg.recorder, RecorderConfig)
    assert isinstance(cfg.output, OutputConfig)
    assert isinstance(cfg.hotkeys, HotkeyConfig)
    assert cfg.hotkeys.clean == "<alt>+q"
    assert cfg.hotkeys.slide == "<alt>+s"
    assert cfg.hotkeys.summary == "<alt>+w"
    assert cfg.hotkeys.cancel == "<esc>"


def test_load_config_overrides():
    """Verify overrides for active profiles and active STT attributes."""
    overrides = {
        "stt_profile": "gpu_local",
        "llm_profile": "corp",
        "device": "cpu",
        "model": "medium",
        "model_path": "/opt/models/whisper-medium",
    }
    cfg = load_config(overrides=overrides)
    assert cfg.active_stt_profile == "gpu_local"
    assert cfg.active_llm_profile == "corp"
    active = cfg.stt
    assert active.device == "cpu"
    assert active.model_size == "medium"
    assert active.model_path == "/opt/models/whisper-medium"


def test_load_config_missing_file():
    """Verify FileNotFoundError on nonexistent path."""
    with pytest.raises(FileNotFoundError):
        load_config(path=Path("nonexistent/config.yaml"))


def test_load_config_custom_yaml(tmp_path: Path):
    """Verify loading custom YAML file."""
    yaml_content = """
active_stt_profile: custom
active_llm_profile: dev
stt_profiles:
  custom:
    device: cpu
    model_size: tiny
    compute_type: int8
llm_profiles:
  dev:
    provider: openai_compatible
    base_url: http://localhost:8000/v1
    model: qwen
"""
    custom_file = tmp_path / "custom.yaml"
    custom_file.write_text(yaml_content, encoding="utf-8")

    cfg = load_config(path=custom_file)
    assert cfg.active_stt_profile == "custom"
    assert cfg.stt.model_size == "tiny"


def test_load_config_llm_extra_body_nested(tmp_path: Path):
    """Verify extra_body with nested dictionaries and values loads cleanly."""
    yaml_content = """
active_stt_profile: cpu_fast
active_llm_profile: dev
stt_profiles:
  cpu_fast:
    device: cpu
llm_profiles:
  dev:
    provider: openai_compatible
    base_url: http://localhost:8000/v1
    extra_body:
      chat_template_kwargs:
        enable_thinking: false
      custom_nested:
        top_k: 40
        flags: [true, false]
"""
    f = tmp_path / "extra_body.yaml"
    f.write_text(yaml_content, encoding="utf-8")

    cfg = load_config(path=f)
    dev_body = cfg.llm.extra_body
    assert dev_body["chat_template_kwargs"]["enable_thinking"] is False
    assert dev_body["custom_nested"]["top_k"] == 40
    assert dev_body["custom_nested"]["flags"] == [True, False]


def test_load_config_pydantic_validation_error(tmp_path: Path):
    """Verify invalid types trigger Pydantic ValidationError."""
    bad_yaml = """
stt_profiles:
  bad:
    beam_size: "not-an-int"
"""
    bad_file = tmp_path / "bad.yaml"
    bad_file.write_text(bad_yaml, encoding="utf-8")

    with pytest.raises(ValidationError):
        load_config(path=bad_file)


# --- R4 Regression Tests ---


def test_load_config_unknown_profile_raises():
    """(R4) Unknown STT or LLM profile override raises ValueError listing available."""
    with pytest.raises(ValueError, match="Unknown STT profile 'ghost_stt'. Available STT profiles:"):
        load_config(overrides={"stt_profile": "ghost_stt"})

    with pytest.raises(ValueError, match="Unknown LLM profile 'ghost_llm'. Available LLM profiles:"):
        load_config(overrides={"llm_profile": "ghost_llm"})


def test_load_config_yaml_unknown_active_profile_raises(tmp_path: Path):
    """(R4) YAML with unknown active profile raises ValueError."""
    bad_yaml = """
active_stt_profile: not_in_profiles
stt_profiles:
  valid_stt:
    device: cpu
llm_profiles:
  dev:
    provider: openai_compatible
"""
    f = tmp_path / "bad_active.yaml"
    f.write_text(bad_yaml, encoding="utf-8")

    with pytest.raises(ValueError, match="Unknown active STT profile 'not_in_profiles'"):
        load_config(path=f)


def test_load_config_device_cpu_resets_compute_type():
    """(R4) Overriding device to 'cpu' resets compute_type to 'int8' unless overridden."""
    # gpu_local default is int8_float16
    cfg = load_config(overrides={"stt_profile": "gpu_local", "device": "cpu"})
    assert cfg.stt.device == "cpu"
    assert cfg.stt.compute_type == "int8"

    # Explicit compute_type override is preserved
    cfg_explicit = load_config(
        overrides={"stt_profile": "gpu_local", "device": "cpu", "compute_type": "float32"}
    )
    assert cfg_explicit.stt.device == "cpu"
    assert cfg_explicit.stt.compute_type == "float32"


def test_load_config_model_override_clears_model_path(tmp_path: Path):
    """(R4) Overriding model without model_path clears model_path."""
    yaml_content = """
active_stt_profile: custom
active_llm_profile: dev
stt_profiles:
  custom:
    device: cpu
    model_size: small
    model_path: /custom/offline/dir
llm_profiles:
  dev:
    provider: openai_compatible
"""
    f = tmp_path / "custom.yaml"
    f.write_text(yaml_content, encoding="utf-8")

    # Override model without model_path -> model_path cleared
    cfg1 = load_config(path=f, overrides={"model": "large-v3-turbo"})
    assert cfg1.stt.model_size == "large-v3-turbo"
    assert cfg1.stt.model_path is None

    # Override model WITH model_path -> model_path set
    cfg2 = load_config(
        path=f, overrides={"model": "large-v3-turbo", "model_path": "/new/path"}
    )
    assert cfg2.stt.model_size == "large-v3-turbo"
    assert cfg2.stt.model_path == "/new/path"


def test_load_config_hotkey_summary_custom(tmp_path: Path):
    """Verify hotkeys.summary loads correctly from YAML."""
    f = tmp_path / "custom_hotkeys.yaml"
    f.write_text(
        """
hotkeys:
  clean: "<alt>+1"
  slide: "<alt>+2"
  summary: "<alt>+3"
  cancel: "<esc>"
stt_profiles:
  cpu_fast:
    device: cpu
llm_profiles:
  dev:
    provider: openai_compatible
""",
        encoding="utf-8",
    )
    cfg = load_config(path=f)
    assert cfg.hotkeys.summary == "<alt>+3"


def test_load_config_resolves_project_paths_and_expands_tilde(tmp_path: Path):
    """(R4) Resolve paths relative to project root and expand ~."""
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)
    cfg_file = config_dir / "config.yaml"

    yaml_content = """
active_stt_profile: cpu_fast
active_llm_profile: dev
glossary_path: my_glossary.yaml
prompts_dir: my_prompts
cache_dir: .my_cache
output:
  obsidian_note_path: ~/vault/slide.md
stt_profiles:
  cpu_fast:
    device: cpu
llm_profiles:
  dev:
    provider: openai_compatible
"""
    cfg_file.write_text(yaml_content, encoding="utf-8")

    cfg = load_config(path=cfg_file)

    # Relative paths should be resolved against project_root (parent of config/)
    expected_root = tmp_path.resolve()
    assert Path(cfg.glossary_path) == expected_root / "my_glossary.yaml"
    assert Path(cfg.prompts_dir) == expected_root / "my_prompts"
    assert Path(cfg.cache_dir) == expected_root / ".my_cache"

    # Tilde path must be expanded
    assert cfg.output.obsidian_note_path is not None
    assert not cfg.output.obsidian_note_path.startswith("~")
    assert Path(cfg.output.obsidian_note_path).is_absolute()


# --- Prompts & Glossary Tests ---


def test_load_prompt_all_modes():
    """Verify all Mode templates load and substitute {glossary}."""
    prompts_dir = Path("prompts")
    glossary_sample = "### Glossary Test Block"

    for mode in [Mode.CLEAN, Mode.SLIDE, Mode.TRANSFORM, Mode.SUMMARY]:
        content = load_prompt(mode, prompts_dir, glossary_section=glossary_sample)
        assert glossary_sample in content
        assert "{glossary}" not in content
        assert "零幻覺保證" in content

    # Test string input as mode
    slide_content = load_prompt("slide", prompts_dir, glossary_section="[GLOSSARY]")
    assert "[GLOSSARY]" in slide_content
    assert "> [!todo] 🖼️ 待補圖：" in slide_content


def test_load_prompt_missing():
    """Verify FileNotFoundError when prompt file does not exist."""
    with pytest.raises(FileNotFoundError):
        load_prompt("unknown_mode", Path("prompts"))


def test_prompt_content_rules():
    """Verify prompt guidelines for content preservation, Traditional Chinese, and no meta-commentary."""
    prompts_dir = Path("prompts")

    for mode in [Mode.CLEAN, Mode.SLIDE, Mode.TRANSFORM, Mode.SUMMARY]:
        raw_text = (prompts_dir / f"{mode.value}.md").read_text(encoding="utf-8")
        assert "{glossary}" in raw_text
        assert "零幻覺保證" in raw_text
        assert "繁體中文" in raw_text

    # (1) clean.md must never drop sentences / test remarks
    clean_text = (prompts_dir / "clean.md").read_text(encoding="utf-8")
    assert "絕不刪除實質語句" in clean_text or "禁止刪除任何完整句子" in clean_text

    # (2) summary.md must forbid meta-commentary and diagnose messages
    summary_text = (prompts_dir / "summary.md").read_text(encoding="utf-8")
    assert "絕不輸出元評論" in summary_text
    assert "直接摘要實際口述內容" in summary_text

    # (4) slide.md must include callout format
    slide_text = (prompts_dir / "slide.md").read_text(encoding="utf-8")
    assert "> [!todo] 🖼️ 待補圖：" in slide_text


def test_glossary_manager_from_yaml():
    """Verify loading glossary.yaml and protocol compliance."""
    glossary = GlossaryManager.from_yaml(Path("config/glossary.yaml"))
    assert isinstance(glossary, GlossaryProvider)

    # Whisper initial prompt
    initial_prompt = glossary.build_whisper_initial_prompt()
    assert "ILT 與 SMO" in initial_prompt
    assert "High NA EUV" in initial_prompt
    assert len(initial_prompt) < 200

    # LLM glossary section
    llm_section = glossary.build_llm_glossary_section()
    assert "### 專業術語與詞庫對照表" in llm_section
    assert "ILT" in llm_section
    assert "SMO" in llm_section
    assert "High NA" in llm_section
    assert "艾爾T" in llm_section


def test_glossary_normalization_regression():
    """Verify the 4 specific regression cases requested in review:
    1. '我在上海那邊開會' -> unchanged
    2. '用ilt做優化' -> '用ILT做優化'
    3. 'Focus is key. High NA EUV' -> unchanged ('Focus is key. High NA EUV')
    4. 'smo跟opc' -> 'SMO跟OPC'
    """
    glossary = GlossaryManager.from_yaml(Path("config/glossary.yaml"))

    # (a) Chinese phonetic aliases must NOT be substring-replaced deterministically
    assert glossary.normalize_terms("我在上海那邊開會") == "我在上海那邊開會"

    # (b) CJK adjacent English terms normalized using lookarounds
    assert glossary.normalize_terms("用ilt做優化") == "用ILT做優化"
    assert glossary.normalize_terms("smo跟opc") == "SMO跟OPC"

    # (c) Lowercase generic terms not touched when capitalized in sentences
    assert (
        glossary.normalize_terms("Focus is key. High NA EUV")
        == "Focus is key. High NA EUV"
    )


def test_glossary_missing_file():
    """Verify FileNotFoundError on nonexistent glossary file."""
    with pytest.raises(FileNotFoundError):
        GlossaryManager.from_yaml(Path("nonexistent/glossary.yaml"))


def test_user_overlay_path_helper():
    """Verify user_overlay_path naming convention (<dir>/<stem>.user.yaml)."""
    assert user_overlay_path("config/glossary.yaml") == Path("config/glossary.user.yaml")
    assert user_overlay_path(Path("custom/my_terms.yml")) == Path("custom/my_terms.user.yaml")
    assert user_overlay_path(Path("glossary.yaml")) == Path("glossary.user.yaml")


def test_glossary_overlay_merge_and_dedup(tmp_path: Path):
    """Verify glossary overlay merging:
    - Canonical terms appended, de-duplicated by term (case-sensitive; base wins on duplicate).
    - Aliases: overlay keys override base keys.
    """
    base_file = tmp_path / "glossary.yaml"
    base_file.write_text(
        """canonical_terms:
  - term: ILT
    definition: Inverse Lithography Technology (base)
  - term: SMO
    definition: Source Mask Optimization
phonetic_aliases:
  "il t": "ILT"
  "s m o": "SMO"
""",
        encoding="utf-8",
    )

    overlay_file = tmp_path / "glossary.user.yaml"
    overlay_file.write_text(
        """canonical_terms:
  - term: ILT
    definition: Overridden ILT definition (should be ignored; base wins)
  - term: Proteus
    definition: Optical Proximity Correction platform
phonetic_aliases:
  "s m o": "Proteus"
  "pro te us": "Proteus"
""",
        encoding="utf-8",
    )

    gm = GlossaryManager.from_yaml(base_file)

    # 1. Canonical terms check
    terms_dict = {item["term"]: item["definition"] for item in gm.canonical_terms}
    assert len(gm.canonical_terms) == 3
    # Base definition wins on duplicate term
    assert terms_dict["ILT"] == "Inverse Lithography Technology (base)"
    assert terms_dict["SMO"] == "Source Mask Optimization"
    # Overlay term appended
    assert terms_dict["Proteus"] == "Optical Proximity Correction platform"

    # 2. Phonetic aliases check
    # Base alias preserved
    assert gm.phonetic_aliases["il t"] == "ILT"
    # Overlay alias overrides base alias
    assert gm.phonetic_aliases["s m o"] == "Proteus"
    # New overlay alias added
    assert gm.phonetic_aliases["pro te us"] == "Proteus"

    # 3. Normalization with merged terms
    assert gm.normalize_terms("用s m o測試") == "用Proteus測試"
    assert gm.normalize_terms("pro te us run") == "Proteus run"
    assert gm.normalize_terms("proteus opc") == "Proteus OPC" or gm.normalize_terms("proteus opc") == "Proteus opc"


def test_glossary_overlay_missing(tmp_path: Path):
    """Verify missing overlay file loads base terms only without error."""
    base_file = tmp_path / "glossary.yaml"
    base_file.write_text(
        """canonical_terms:
  - term: EUV
    definition: Extreme Ultraviolet
phonetic_aliases:
  "e u v": "EUV"
""",
        encoding="utf-8",
    )

    gm = GlossaryManager.from_yaml(base_file)
    assert len(gm.canonical_terms) == 1
    assert gm.canonical_terms[0]["term"] == "EUV"
    assert gm.phonetic_aliases == {"e u v": "EUV"}


def test_glossary_overlay_corrupt_or_empty(tmp_path: Path, caplog: pytest.LogCaptureFixture):
    """Verify corrupt or empty overlay logs warning and loads base only."""
    base_file = tmp_path / "glossary.yaml"
    base_file.write_text(
        """canonical_terms:
  - term: SRAF
    definition: Sub-resolution assist feature
phonetic_aliases:
  "s r a f": "SRAF"
""",
        encoding="utf-8",
    )

    overlay_file = tmp_path / "glossary.user.yaml"

    # Case A: Empty overlay file (0 bytes)
    overlay_file.write_text("", encoding="utf-8")
    with caplog.at_level("WARNING"):
        gm_empty = GlossaryManager.from_yaml(base_file)
    assert len(gm_empty.canonical_terms) == 1
    assert gm_empty.canonical_terms[0]["term"] == "SRAF"
    assert any("empty or not a mapping" in record.message for record in caplog.records)

    caplog.clear()

    # Case B: Corrupt YAML syntax
    overlay_file.write_text("canonical_terms: [broken: {yaml", encoding="utf-8")
    with caplog.at_level("WARNING"):
        gm_corrupt = GlossaryManager.from_yaml(base_file)
    assert len(gm_corrupt.canonical_terms) == 1
    assert gm_corrupt.canonical_terms[0]["term"] == "SRAF"
    assert any("Failed to load glossary overlay" in record.message for record in caplog.records)

    caplog.clear()

    # Case C: Non-mapping overlay (e.g. YAML list)
    overlay_file.write_text("- item1\n- item2\n", encoding="utf-8")
    with caplog.at_level("WARNING"):
        gm_list = GlossaryManager.from_yaml(base_file)
    assert len(gm_list.canonical_terms) == 1
    assert gm_list.canonical_terms[0]["term"] == "SRAF"
    assert any("empty or not a mapping" in record.message for record in caplog.records)


def test_user_prompt_path_helper():
    """Verify user_prompt_path naming convention (<prompts_dir>/<mode>.user.md)."""
    assert user_prompt_path(Mode.CLEAN, "prompts") == Path("prompts/clean.user.md")
    assert user_prompt_path("summary", Path("my_prompts")) == Path("my_prompts/summary.user.md")


def test_prompt_overlay_appended_and_glossary_substituted(tmp_path: Path):
    """Verify prompt overlay appended and {glossary} substituted on combined text."""
    base_prompt = tmp_path / "clean.md"
    base_prompt.write_text(
        "# Clean Mode Base\nInstructions here.\n{glossary}\nBase rules end.",
        encoding="utf-8",
    )

    user_overlay = tmp_path / "clean.user.md"
    user_overlay.write_text(
        "- 當提到 Proteus 時，請確保拼寫正確。\n- 嚴禁修改代號字串。\n",
        encoding="utf-8",
    )

    result = load_prompt(
        Mode.CLEAN,
        tmp_path,
        glossary_section="### Injected Glossary ###",
    )

    assert "### Injected Glossary ###" in result
    assert "{glossary}" not in result
    assert "## 使用者校正規則（教學模式產生，優先遵守）" in result
    assert "- 當提到 Proteus 時，請確保拼寫正確。" in result
    assert "- 嚴禁修改代號字串。" in result
    # Ensure correct structure: base prompt before user correction header
    base_idx = result.find("# Clean Mode Base")
    header_idx = result.find("## 使用者校正規則（教學模式產生，優先遵守）")
    assert base_idx < header_idx


def test_prompt_overlay_empty_or_missing_ignored(tmp_path: Path):
    """Verify missing, empty, or whitespace-only overlay file is ignored."""
    base_prompt = tmp_path / "summary.md"
    base_prompt.write_text(
        "# Summary Mode Base\n{glossary}\nEnd.",
        encoding="utf-8",
    )

    # Case A: Missing user overlay file
    out_missing = load_prompt("summary", tmp_path, glossary_section="[G]")
    assert out_missing == "# Summary Mode Base\n[G]\nEnd."
    assert "使用者校正規則" not in out_missing

    # Case B: Empty user overlay file (0 bytes)
    user_overlay = tmp_path / "summary.user.md"
    user_overlay.write_text("", encoding="utf-8")
    out_empty = load_prompt("summary", tmp_path, glossary_section="[G]")
    assert out_empty == "# Summary Mode Base\n[G]\nEnd."
    assert "使用者校正規則" not in out_empty

    # Case C: Whitespace-only user overlay file
    user_overlay.write_text("   \n\t  \n  ", encoding="utf-8")
    out_ws = load_prompt("summary", tmp_path, glossary_section="[G]")
    assert out_ws == "# Summary Mode Base\n[G]\nEnd."
    assert "使用者校正規則" not in out_ws


def test_task2_config_defaults_when_sections_absent(tmp_path: Path):
    """When output.paste_step_delay_ms and images/streaming/refine are absent, defaults apply."""
    minimal_yaml = """
active_stt_profile: cpu_fast
active_llm_profile: dev
stt_profiles:
  cpu_fast:
    device: cpu
llm_profiles:
  dev:
    provider: openai_compatible
    base_url: http://localhost:8000/v1
"""
    f = tmp_path / "minimal.yaml"
    f.write_text(minimal_yaml, encoding="utf-8")

    cfg = load_config(path=f)
    assert cfg.output.paste_step_delay_ms == 300
    assert isinstance(cfg.images, ImagesConfig)
    assert cfg.images.enabled is True
    assert cfg.images.poll_ms == 150
    assert isinstance(cfg.streaming, StreamingConfig)
    assert cfg.streaming.enabled is True
    assert cfg.streaming.min_chunk_sec == 12.0
    assert cfg.streaming.max_chunk_sec == 28.0
    assert cfg.streaming.silence_sec == 0.5
    assert isinstance(cfg.refine, RefineConfig)
    assert cfg.refine.block_target_chars == 700
    assert cfg.refine.block_max_chars == 1000
    assert cfg.refine.context_chars == 150


def test_task2_config_values_from_yaml(tmp_path: Path):
    """Verify custom values for paste_step_delay_ms and images/streaming/refine sections."""
    custom_yaml = """
active_stt_profile: cpu_fast
active_llm_profile: dev
stt_profiles:
  cpu_fast:
    device: cpu
llm_profiles:
  dev:
    provider: openai_compatible
    base_url: http://localhost:8000/v1
output:
  paste_step_delay_ms: 450
images:
  enabled: false
  poll_ms: 80
streaming:
  enabled: false
  min_chunk_sec: 10.0
  max_chunk_sec: 30.0
  silence_sec: 1.0
refine:
  block_target_chars: 600
  block_max_chars: 900
  context_chars: 200
"""
    f = tmp_path / "custom_task2.yaml"
    f.write_text(custom_yaml, encoding="utf-8")

    cfg = load_config(path=f)
    assert cfg.output.paste_step_delay_ms == 450
    assert cfg.images.enabled is False
    assert cfg.images.poll_ms == 80
    assert cfg.streaming.enabled is False
    assert cfg.streaming.min_chunk_sec == 10.0
    assert cfg.streaming.max_chunk_sec == 30.0
    assert cfg.streaming.silence_sec == 1.0
    assert cfg.refine.block_target_chars == 600
    assert cfg.refine.block_max_chars == 900
    assert cfg.refine.context_chars == 200


@pytest.mark.parametrize(
    "patch_yaml",
    [
        "output:\n  paste_step_delay_ms: -1\n",
        "images:\n  poll_ms: 19\n",
        "streaming:\n  min_chunk_sec: 0.0\n",
        "streaming:\n  min_chunk_sec: -2.0\n",
        "streaming:\n  silence_sec: 0.0\n",
        "streaming:\n  silence_sec: -0.5\n",
        "streaming:\n  min_chunk_sec: 15.0\n  max_chunk_sec: 10.0\n",
        "streaming:\n  min_chunk_sec: 15.0\n  max_chunk_sec: 15.0\n",
        "refine:\n  block_target_chars: 0\n",
        "refine:\n  block_target_chars: -5\n",
        "refine:\n  context_chars: -1\n",
        "refine:\n  block_target_chars: 800\n  block_max_chars: 500\n",
    ],
)
def test_task2_config_invalid_cases_raise_value_error(tmp_path: Path, patch_yaml: str):
    """Each invalid constraint in output/images/streaming/refine raises ValueError."""
    base_yaml = """
active_stt_profile: cpu_fast
active_llm_profile: dev
stt_profiles:
  cpu_fast:
    device: cpu
llm_profiles:
  dev:
    provider: openai_compatible
    base_url: http://localhost:8000/v1
"""
    f = tmp_path / "invalid.yaml"
    f.write_text(base_yaml + patch_yaml, encoding="utf-8")

    with pytest.raises(ValueError):
        load_config(path=f)


def test_task2_load_prompt_contains_image_marker():
    """Verify load_prompt for clean, slide, summary contains '[圖1]'."""
    prompts_dir = Path("prompts")
    for mode in [Mode.CLEAN, Mode.SLIDE, Mode.SUMMARY]:
        content = load_prompt(mode, prompts_dir)
        assert "[圖1]" in content


# ------------------------------------------------------------ Task 13 storage config tests
def test_storage_config_defaults():
    cfg = load_config()
    assert cfg.storage.retention_days == 90
    assert cfg.storage.filename_format == "{date}-{time}_{mode}_{title}"
    assert cfg.images.trigger == "paste"
    assert cfg.hotkeys.paste_image == "<alt>+v"


@pytest.mark.parametrize(
    "patch_yaml",
    [
        "storage:\n  retention_days: -5\n",
        "storage:\n  filename_format: '{date}_{time}_{unknown}'\n",
        "images:\n  trigger: invalid_trigger\n",
    ],
)
def test_storage_config_invalid_cases_raise_value_error(tmp_path: Path, patch_yaml: str):
    base_yaml = """
active_stt_profile: cpu_fast
active_llm_profile: dev
stt_profiles:
  cpu_fast:
    device: cpu
llm_profiles:
  dev:
    provider: openai_compatible
    base_url: http://localhost:8000/v1
"""
    f = tmp_path / "invalid_storage.yaml"
    f.write_text(base_yaml + patch_yaml, encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(path=f)




def test_llm_gateway_fields_defaults_values_and_validation(tmp_path):
    import pytest as _pytest
    import yaml as _yaml

    from local_typeless.config.loader import load_config as _load

    def write(profile):
        f = tmp_path / "c.yaml"
        f.write_text(_yaml.safe_dump({
            "stt_profiles": {"cpu_fast": {}}, "llm_profiles": {"dev": profile},
        }), encoding="utf-8")
        return f

    p = _load(write({})).llm
    assert (p.api_key_header, p.api_key_prefix, p.verify_tls, p.ca_bundle, p.use_env_proxy,
            p.chat_path, p.models_path) == ("Authorization", "Bearer ", True, None, True,
                                            "/chat/completions", "/models")
    p = _load(write({"api_key_header": "api-key", "api_key_prefix": "", "verify_tls": False,
                     "ca_bundle": "certs/root.pem", "use_env_proxy": False,
                     "chat_path": "/x/chat", "models_path": ""})).llm
    assert p.api_key_header == "api-key" and p.api_key_prefix == "" and p.verify_tls is False
    assert p.use_env_proxy is False and p.chat_path == "/x/chat" and p.models_path == ""
    assert p.ca_bundle == str((tmp_path / "certs" / "root.pem").resolve())
    for bad in ({"api_key_header": " "}, {"chat_path": ""}):
        with _pytest.raises(ValueError):
            _load(write(bad))
