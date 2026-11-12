"""Config and Prompt Loader (WP1-A, owned by flash-infra).

Implements load_config and load_prompt per local_typeless.types contract.
Validates configurations internally with Pydantic, returning the dataclass AppConfig.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional, Union

import yaml
from pydantic import BaseModel, Field, ValidationError, model_validator

from local_typeless.types import (
    AppConfig,
    HotkeyConfig,
    ImagesConfig,
    LLMProfileConfig,
    Mode,
    OutputConfig,
    RecorderConfig,
    RefineConfig,
    STTProfileConfig,
    StorageConfig,
    StreamingConfig,
)


# --- Internal Pydantic Validation Schemas ---


class _PydanticSTTProfile(BaseModel):
    device: str = "cpu"
    model_size: str = "small"
    model_path: Optional[str] = None
    compute_type: str = "int8"
    cpu_threads: int = 6
    beam_size: int = 2
    vad_filter: bool = True
    language: Optional[str] = None


class _PydanticLLMProfile(BaseModel):
    provider: str = "openai_compatible"
    base_url: str = "http://localhost:8000/v1"
    api_key_env: str = "DGX_API_KEY"
    model: str = "Qwen/Qwen2.5-32B-Instruct"
    timeout_sec: float = 15.0
    temperature: float = 0.2
    max_tokens: int = 2048
    extra_headers: Dict[str, str] = Field(default_factory=dict)
    extra_body: Dict[str, Any] = Field(default_factory=dict)
    api_key_header: str = "Authorization"
    api_key_prefix: str = "Bearer "
    verify_tls: bool = True
    ca_bundle: Optional[str] = None
    use_env_proxy: bool = True
    proxy: Optional[str] = None
    chat_path: str = "/chat/completions"
    models_path: str = "/models"

    @model_validator(mode="after")
    def _validate_gateway_fields(self) -> _PydanticLLMProfile:
        if not self.api_key_header.strip():
            raise ValueError("api_key_header must not be empty")
        if not self.chat_path.strip():
            raise ValueError("chat_path must not be empty")
        return self


class _PydanticRecorder(BaseModel):
    sample_rate: int = 16000
    channels: int = 1
    beep: bool = True


class _PydanticOutput(BaseModel):
    clipboard: bool = True
    auto_paste: bool = False
    protect_image_clipboard: bool = True
    obsidian_note_path: Optional[str] = None
    paste_step_delay_ms: int = Field(default=300, ge=0)


class _PydanticHotkeys(BaseModel):
    clean: str = "<alt>+q"
    slide: str = "<alt>+s"
    summary: str = "<alt>+w"
    cancel: str = "<esc>"
    paste_image: str = "<alt>+v"
    mode: str = "toggle"


class _PydanticImages(BaseModel):
    enabled: bool = True
    poll_ms: int = Field(default=150, ge=20)
    trigger: str = "paste"

    @model_validator(mode="after")
    def _validate_trigger(self) -> _PydanticImages:
        if self.trigger not in ("paste", "auto"):
            raise ValueError(f"images.trigger must be 'paste' or 'auto', got {self.trigger!r}")
        return self


class _PydanticStreaming(BaseModel):
    enabled: bool = True
    min_chunk_sec: float = Field(default=12.0, gt=0)
    max_chunk_sec: float = Field(default=28.0, gt=0)
    silence_sec: float = Field(default=0.5, gt=0)

    @model_validator(mode="after")
    def _validate_chunk_bounds(self) -> _PydanticStreaming:
        if self.max_chunk_sec <= self.min_chunk_sec:
            raise ValueError(
                f"max_chunk_sec ({self.max_chunk_sec}) must be greater than min_chunk_sec ({self.min_chunk_sec})"
            )
        return self


class _PydanticRefine(BaseModel):
    block_target_chars: int = Field(default=700, gt=0)
    block_max_chars: int = Field(default=1000, gt=0)
    context_chars: int = Field(default=150, ge=0)

    @model_validator(mode="after")
    def _validate_block_bounds(self) -> _PydanticRefine:
        if self.block_max_chars < self.block_target_chars:
            raise ValueError(
                f"block_max_chars ({self.block_max_chars}) must be >= block_target_chars ({self.block_target_chars})"
            )
        return self


_ALLOWED_FILENAME_PLACEHOLDERS = {"{date}", "{time}", "{mode}", "{title}"}


class _PydanticStorage(BaseModel):
    records_dir: str = "records"
    images_dir: str = "records/images"
    retention_days: int = Field(default=90, ge=0)
    filename_format: str = "{date}-{time}_{mode}_{title}"

    @model_validator(mode="after")
    def _validate_filename_format(self) -> _PydanticStorage:
        import re

        matches = re.findall(r"\{[^{}]*\}", self.filename_format)
        for m in matches:
            if m not in _ALLOWED_FILENAME_PLACEHOLDERS:
                raise ValueError(
                    f"Invalid placeholder {m} in filename_format; allowed are {_ALLOWED_FILENAME_PLACEHOLDERS}"
                )
        return self


class _PydanticAppConfig(BaseModel):
    stt_profiles: Dict[str, _PydanticSTTProfile] = Field(default_factory=dict)
    active_stt_profile: str = "cpu_fast"
    llm_profiles: Dict[str, _PydanticLLMProfile] = Field(default_factory=dict)
    active_llm_profile: str = "dev"
    glossary_path: str = "config/glossary.yaml"
    prompts_dir: str = "prompts"
    cache_dir: str = "records/cache"
    history_enabled: bool = True
    history_max: int = 20
    recorder: _PydanticRecorder = Field(default_factory=_PydanticRecorder)
    output: _PydanticOutput = Field(default_factory=_PydanticOutput)
    hotkeys: _PydanticHotkeys = Field(default_factory=_PydanticHotkeys)
    images: _PydanticImages = Field(default_factory=_PydanticImages)
    streaming: _PydanticStreaming = Field(default_factory=_PydanticStreaming)
    refine: _PydanticRefine = Field(default_factory=_PydanticRefine)
    storage: _PydanticStorage = Field(default_factory=_PydanticStorage)


def _resolve_project_path(path_str: Optional[str], project_root: Path) -> Optional[str]:
    """Resolve a path relative to the project root when relative, expanding ~."""
    if path_str is None:
        return None
    p = Path(path_str).expanduser()
    if not p.is_absolute():
        p = (project_root / p).resolve()
    return str(p)


def _convert_to_dataclass(validated: _PydanticAppConfig, project_root: Path) -> AppConfig:
    stt_profiles = {
        name: STTProfileConfig(
            device=p.device,
            model_size=p.model_size,
            model_path=p.model_path,
            compute_type=p.compute_type,
            cpu_threads=p.cpu_threads,
            beam_size=p.beam_size,
            vad_filter=p.vad_filter,
            language=p.language,
        )
        for name, p in validated.stt_profiles.items()
    }

    llm_profiles = {
        name: LLMProfileConfig(
            provider=p.provider,
            base_url=p.base_url,
            api_key_env=p.api_key_env,
            model=p.model,
            timeout_sec=p.timeout_sec,
            temperature=p.temperature,
            max_tokens=p.max_tokens,
            extra_headers=dict(p.extra_headers),
            extra_body=dict(p.extra_body),
            api_key_header=p.api_key_header.strip(),
            api_key_prefix=p.api_key_prefix,
            verify_tls=p.verify_tls,
            ca_bundle=_resolve_project_path(p.ca_bundle, project_root),
            use_env_proxy=p.use_env_proxy,
            proxy=(p.proxy or "").strip() or None,
            chat_path=p.chat_path.strip(),
            models_path=(p.models_path or "").strip(),
        )
        for name, p in validated.llm_profiles.items()
    }

    recorder = RecorderConfig(
        sample_rate=validated.recorder.sample_rate,
        channels=validated.recorder.channels,
        beep=validated.recorder.beep,
    )

    resolved_obsidian_path = _resolve_project_path(
        validated.output.obsidian_note_path, project_root
    )

    output = OutputConfig(
        clipboard=validated.output.clipboard,
        auto_paste=validated.output.auto_paste,
        protect_image_clipboard=validated.output.protect_image_clipboard,
        obsidian_note_path=resolved_obsidian_path,
        paste_step_delay_ms=validated.output.paste_step_delay_ms,
    )

    hotkeys = HotkeyConfig(
        clean=validated.hotkeys.clean,
        slide=validated.hotkeys.slide,
        summary=validated.hotkeys.summary,
        cancel=validated.hotkeys.cancel,
        paste_image=validated.hotkeys.paste_image,
        mode=validated.hotkeys.mode,
    )

    images = ImagesConfig(
        enabled=validated.images.enabled,
        poll_ms=validated.images.poll_ms,
        trigger=validated.images.trigger,
    )

    streaming = StreamingConfig(
        enabled=validated.streaming.enabled,
        min_chunk_sec=validated.streaming.min_chunk_sec,
        max_chunk_sec=validated.streaming.max_chunk_sec,
        silence_sec=validated.streaming.silence_sec,
    )

    refine = RefineConfig(
        block_target_chars=validated.refine.block_target_chars,
        block_max_chars=validated.refine.block_max_chars,
        context_chars=validated.refine.context_chars,
    )

    resolved_records = _resolve_project_path(validated.storage.records_dir, project_root) or "records"
    resolved_images_storage = _resolve_project_path(validated.storage.images_dir, project_root) or "records/images"
    storage = StorageConfig(
        records_dir=resolved_records,
        images_dir=resolved_images_storage,
        retention_days=validated.storage.retention_days,
        filename_format=validated.storage.filename_format,
    )

    resolved_glossary = _resolve_project_path(validated.glossary_path, project_root) or ""
    resolved_prompts = _resolve_project_path(validated.prompts_dir, project_root) or ""
    resolved_cache = _resolve_project_path(validated.cache_dir, project_root) or ""

    return AppConfig(
        stt_profiles=stt_profiles,
        active_stt_profile=validated.active_stt_profile,
        llm_profiles=llm_profiles,
        active_llm_profile=validated.active_llm_profile,
        glossary_path=resolved_glossary,
        prompts_dir=resolved_prompts,
        cache_dir=resolved_cache,
        history_enabled=validated.history_enabled,
        history_max=validated.history_max,
        recorder=recorder,
        output=output,
        hotkeys=hotkeys,
        images=images,
        streaming=streaming,
        refine=refine,
        storage=storage,
    )


def load_config(
    path: Optional[Union[Path, str]] = None,
    overrides: Optional[Dict[str, Any]] = None,
) -> AppConfig:
    """Load and validate configuration from YAML, returning the dataclass AppConfig.

    - path None -> config/config.yaml if exists else config/config.example.yaml
    - overrides keys: "stt_profile", "llm_profile", "device", "model", "model_path"
      ("device"/"model"/"model_path" patch the *active* STT profile)
    - "${ENV}" style values are NOT expanded; secrets come only via api_key_env.
    """
    if path is None:
        candidate = Path("config/config.yaml")
        fallback = Path("config/config.example.yaml")
        if candidate.exists():
            resolved_path = candidate
        elif fallback.exists():
            resolved_path = fallback
        else:
            raise FileNotFoundError(
                "Neither config/config.yaml nor config/config.example.yaml found."
            )
    else:
        resolved_path = Path(path)
        if not resolved_path.exists():
            raise FileNotFoundError(f"Configuration file not found: {resolved_path}")

    resolved_path = resolved_path.resolve()
    # Determine project root (parent of config/ directory when config is inside config/)
    if resolved_path.parent.name == "config":
        project_root = resolved_path.parent.parent
    else:
        project_root = resolved_path.parent

    with resolved_path.open("r", encoding="utf-8") as f:
        raw_data = yaml.safe_load(f) or {}

    if not isinstance(raw_data, dict):
        raw_data = {}

    # Validate with Pydantic internally
    validated_model = _PydanticAppConfig.model_validate(raw_data)
    app_config = _convert_to_dataclass(validated_model, project_root)

    # Validate default active profiles
    if app_config.active_stt_profile not in app_config.stt_profiles:
        avail = sorted(list(app_config.stt_profiles.keys()))
        raise ValueError(
            f"Unknown active STT profile '{app_config.active_stt_profile}'. "
            f"Available STT profiles: {avail}"
        )

    if app_config.active_llm_profile not in app_config.llm_profiles:
        avail = sorted(list(app_config.llm_profiles.keys()))
        raise ValueError(
            f"Unknown active LLM profile '{app_config.active_llm_profile}'. "
            f"Available LLM profiles: {avail}"
        )

    # Apply overrides
    if overrides:
        if "stt_profile" in overrides and overrides["stt_profile"] is not None:
            new_stt = str(overrides["stt_profile"])
            if new_stt not in app_config.stt_profiles:
                avail = sorted(list(app_config.stt_profiles.keys()))
                raise ValueError(
                    f"Unknown STT profile '{new_stt}'. Available STT profiles: {avail}"
                )
            app_config.active_stt_profile = new_stt

        if "llm_profile" in overrides and overrides["llm_profile"] is not None:
            new_llm = str(overrides["llm_profile"])
            if new_llm not in app_config.llm_profiles:
                avail = sorted(list(app_config.llm_profiles.keys()))
                raise ValueError(
                    f"Unknown LLM profile '{new_llm}'. Available LLM profiles: {avail}"
                )
            app_config.active_llm_profile = new_llm

        active_stt = app_config.stt_profiles[app_config.active_stt_profile]

        # Device override: resetting device to 'cpu' resets compute_type to 'int8'
        # unless compute_type is also explicitly overridden
        device_override = overrides.get("device")
        compute_type_override = overrides.get("compute_type")
        if device_override is not None:
            active_stt.device = str(device_override)
            if active_stt.device.lower() == "cpu" and compute_type_override is None:
                active_stt.compute_type = "int8"

        if compute_type_override is not None:
            active_stt.compute_type = str(compute_type_override)

        # Model override: overriding model without model_path clears model_path
        has_model = "model" in overrides and overrides["model"] is not None
        has_model_size = "model_size" in overrides and overrides["model_size"] is not None
        has_model_path = "model_path" in overrides

        if has_model or has_model_size:
            active_stt.model_size = str(overrides.get("model") or overrides.get("model_size"))
            if not has_model_path:
                active_stt.model_path = None

        if has_model_path:
            active_stt.model_path = overrides["model_path"]

        # Cache dir override
        if "cache_dir" in overrides and overrides["cache_dir"] is not None:
            app_config.cache_dir = _resolve_project_path(str(overrides["cache_dir"]), project_root) or ""

    return app_config


def user_prompt_path(
    mode: Union[Mode, str],
    prompts_dir: Union[Path, str],
) -> Path:
    """Return the user overlay prompt file path for a given mode.

    E.g. prompts/clean.md -> prompts/clean.user.md
    """
    p_dir = Path(prompts_dir)
    mode_str = mode.value if isinstance(mode, Mode) else str(mode)
    return p_dir / f"{mode_str}.user.md"


def load_prompt(
    mode: Union[Mode, str],
    prompts_dir: Union[Path, str],
    glossary_section: str = "",
) -> str:
    """Read prompts/<mode>.md and substitute the literal '{glossary}' placeholder.

    If prompts/<mode>.user.md exists and is non-empty, appends:
        \n\n## 使用者校正規則（教學模式產生，優先遵守）\n<file content stripped>
    before performing the '{glossary}' substitution on the combined text.
    """
    p_dir = Path(prompts_dir)
    mode_str = mode.value if isinstance(mode, Mode) else str(mode)
    prompt_file = p_dir / f"{mode_str}.md"

    if not prompt_file.exists():
        raise FileNotFoundError(f"Prompt template file not found: {prompt_file}")

    content = prompt_file.read_text(encoding="utf-8")

    user_file = user_prompt_path(mode, prompts_dir)
    if user_file.exists():
        user_content = user_file.read_text(encoding="utf-8").strip()
        if user_content:
            content = (
                f"{content}\n\n## 使用者校正規則（教學模式產生，優先遵守）\n{user_content}"
            )

    return content.replace("{glossary}", glossary_section)
