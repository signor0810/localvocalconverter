"""Configuration schema and loader for the editor workspace."""
from __future__ import annotations

import warnings
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import yaml
from pydantic import BaseModel, Field

# Filter Pydantic warning for 'copy' field in EditorSendConfig
warnings.filterwarnings(
    "ignore",
    message='Field name "copy" in "EditorSendConfig" shadows an attribute in parent "BaseModel"',
    category=UserWarning,
)


class EditorServerConfig(BaseModel):
    port: int = 8765
    autostart_cmd: str = ""


class EditorPasteConfig(BaseModel):
    image_dir: str = "assets/{yyyy}-{mm}"
    image_name: str = "{prompt}_{n:02}.png"
    path_style: str = "windows"
    quote_paths_with_spaces: bool = True
    image_preview_on_hover: bool = True


class EditorRefineConfig(BaseModel):
    default_mode: str = "clean"
    modes_in_palette: List[str] = Field(
        default_factory=lambda: ["clean", "prompt", "slide", "summary", "transform"]
    )
    result_view: str = "split_right"
    protect_paths: bool = True
    protect_code_blocks: bool = True


class EditorSendConfig(BaseModel):
    copy: str = "refined"
    auto_paste_to_last_window: bool = False
    mark_sent_in_frontmatter: bool = True


class EditorFrontmatterConfig(BaseModel):
    fields: List[str] = Field(
        default_factory=lambda: [
            "created",
            "status",
            "mode",
            "refined_at",
            "sent_at",
            "target",
        ]
    )


class EditorConfig(BaseModel):
    prompts_root: str = "~/Documents/prompts"
    file_layout: str = "{yyyy}/{mm}/{date}_{time}_{title}.md"
    auto_rename_after_refine: bool = True
    autosave_delay_ms: int = 1000

    server: EditorServerConfig = Field(default_factory=EditorServerConfig)
    paste: EditorPasteConfig = Field(default_factory=EditorPasteConfig)
    refine: EditorRefineConfig = Field(default_factory=EditorRefineConfig)
    send: EditorSendConfig = Field(default_factory=EditorSendConfig)
    frontmatter: EditorFrontmatterConfig = Field(default_factory=EditorFrontmatterConfig)


def load_editor_config(
    config_path: Optional[Union[Path, str]] = None,
) -> EditorConfig:
    """Load editor configuration from config.yaml layered with settings.local.yaml."""
    from local_typeless.config.loader import _with_settings, resolve_config_path

    resolved_path = resolve_config_path(config_path)
    if resolved_path.parent.name == "config":
        project_root = resolved_path.parent.parent
    else:
        project_root = resolved_path.parent

    with resolved_path.open("r", encoding="utf-8") as f:
        raw_data = yaml.safe_load(f) or {}

    if not isinstance(raw_data, dict):
        raw_data = {}
    raw_data = _with_settings(raw_data, resolved_path)

    editor_data = raw_data.get("editor")
    if not isinstance(editor_data, dict):
        editor_data = {}

    editor_cfg = EditorConfig.model_validate(editor_data)

    # Resolve prompts_root: expanduser, resolve relative paths against project_root
    raw_root = editor_cfg.prompts_root
    p = Path(raw_root).expanduser()
    if not p.is_absolute():
        p = (project_root / p).resolve()
    else:
        p = p.resolve()
    editor_cfg.prompts_root = str(p)

    return editor_cfg
