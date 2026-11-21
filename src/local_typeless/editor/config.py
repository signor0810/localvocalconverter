"""Configuration schema and loader for the editor workspace."""
from __future__ import annotations

import warnings
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import yaml
from pydantic import BaseModel, Field

from local_typeless.types import Mode

# Filter Pydantic warning for 'copy' field in EditorSendConfig
warnings.filterwarnings(
    "ignore",
    message='Field name "copy" in "EditorSendConfig" shadows an attribute in parent "BaseModel"',
    category=UserWarning,
)

VALID_PATH_STYLES = ("windows", "forward", "wsl", "markdown")
VALID_RESULT_VIEWS = ("split_right", "below", "replace")
VALID_SEND_COPIES = ("refined", "raw", "both")
VALID_MODES = tuple(m.value for m in Mode)


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


def load_editor_config_with_errors(
    config_path: Optional[Union[Path, str]] = None,
) -> Tuple[EditorConfig, List[str], Path, Path]:
    """Load editor configuration with field-level fault tolerance.

    Returns (editor_cfg, config_errors, resolved_config_path, resolved_settings_path).
    If a field fails validation, only that field falls back to default while keeping
    other valid fields intact.
    """
    from local_typeless.config.loader import _with_settings, resolve_config_path, settings_path

    resolved_path = resolve_config_path(config_path)
    if resolved_path.parent.name == "config":
        project_root = resolved_path.parent.parent
    else:
        project_root = resolved_path.parent
    s_path = settings_path(resolved_path)

    raw_data: Dict[str, Any] = {}
    config_errors: List[str] = []

    try:
        with resolved_path.open("r", encoding="utf-8") as f:
            raw_data = yaml.safe_load(f) or {}
        if not isinstance(raw_data, dict):
            config_errors.append(f"{resolved_path.name}: not a mapping, using default config")
            raw_data = {}
    except Exception as exc:
        config_errors.append(f"{resolved_path.name} read error: {exc}")
        raw_data = {}

    try:
        raw_data = _with_settings(raw_data, resolved_path)
    except Exception as exc:
        config_errors.append(f"{s_path.name} overlay error: {exc}")

    raw_editor = raw_data.get("editor")
    if raw_editor is None:
        raw_editor = {}
    elif not isinstance(raw_editor, dict):
        config_errors.append("editor section is not a mapping, using default editor config")
        raw_editor = {}

    defaults = EditorConfig()

    # --- Field-level validation and fallback ---
    prompts_root = defaults.prompts_root
    if "prompts_root" in raw_editor:
        val = raw_editor["prompts_root"]
        if isinstance(val, str) and val.strip():
            prompts_root = val.strip()
        else:
            config_errors.append(f"editor.prompts_root invalid ({val!r}); using default {prompts_root!r}")

    file_layout = defaults.file_layout
    if "file_layout" in raw_editor:
        val = raw_editor["file_layout"]
        if isinstance(val, str) and val.strip():
            file_layout = val.strip()
        else:
            config_errors.append(f"editor.file_layout invalid ({val!r}); using default {file_layout!r}")

    auto_rename = defaults.auto_rename_after_refine
    if "auto_rename_after_refine" in raw_editor:
        val = raw_editor["auto_rename_after_refine"]
        if isinstance(val, bool):
            auto_rename = val
        else:
            config_errors.append(f"editor.auto_rename_after_refine must be boolean; got {val!r}")

    autosave_delay_ms = defaults.autosave_delay_ms
    if "autosave_delay_ms" in raw_editor:
        val = raw_editor["autosave_delay_ms"]
        try:
            val_int = int(val)
            if val_int >= 100:
                autosave_delay_ms = val_int
            else:
                config_errors.append(f"editor.autosave_delay_ms must be >= 100; got {val_int}")
        except (ValueError, TypeError):
            config_errors.append(f"editor.autosave_delay_ms invalid ({val!r}); using default {autosave_delay_ms}")

    # Server config
    raw_server = raw_editor.get("server") or {}
    server_port = defaults.server.port
    server_cmd = defaults.server.autostart_cmd
    if isinstance(raw_server, dict):
        if "port" in raw_server:
            val = raw_server["port"]
            try:
                val_int = int(val)
                if 1 <= val_int <= 65535:
                    server_port = val_int
                else:
                    config_errors.append(f"editor.server.port out of range 1-65535 ({val_int})")
            except (ValueError, TypeError):
                config_errors.append(f"editor.server.port invalid ({val!r}); using default {server_port}")
        if "autostart_cmd" in raw_server:
            val = raw_server["autostart_cmd"]
            if isinstance(val, str):
                server_cmd = val
            else:
                config_errors.append(f"editor.server.autostart_cmd must be string; got {val!r}")
    elif "server" in raw_editor:
        config_errors.append("editor.server must be a mapping")

    # Paste config
    raw_paste = raw_editor.get("paste") or {}
    image_dir = defaults.paste.image_dir
    image_name = defaults.paste.image_name
    path_style = defaults.paste.path_style
    quote_spaces = defaults.paste.quote_paths_with_spaces
    hover_preview = defaults.paste.image_preview_on_hover
    if isinstance(raw_paste, dict):
        if "image_dir" in raw_paste:
            val = raw_paste["image_dir"]
            if isinstance(val, str) and val.strip():
                image_dir = val.strip()
            else:
                config_errors.append(f"editor.paste.image_dir invalid ({val!r})")
        if "image_name" in raw_paste:
            val = raw_paste["image_name"]
            if isinstance(val, str) and val.strip():
                image_name = val.strip()
            else:
                config_errors.append(f"editor.paste.image_name invalid ({val!r})")
        if "path_style" in raw_paste:
            val = str(raw_paste["path_style"]).lower()
            if val in VALID_PATH_STYLES:
                path_style = val
            else:
                config_errors.append(f"editor.paste.path_style must be one of {VALID_PATH_STYLES}; got {val!r}")
        if "quote_paths_with_spaces" in raw_paste:
            val = raw_paste["quote_paths_with_spaces"]
            if isinstance(val, bool):
                quote_spaces = val
            else:
                config_errors.append(f"editor.paste.quote_paths_with_spaces must be boolean; got {val!r}")
        if "image_preview_on_hover" in raw_paste:
            val = raw_paste["image_preview_on_hover"]
            if isinstance(val, bool):
                hover_preview = val
            else:
                config_errors.append(f"editor.paste.image_preview_on_hover must be boolean; got {val!r}")
    elif "paste" in raw_editor:
        config_errors.append("editor.paste must be a mapping")

    # Refine config
    raw_refine = raw_editor.get("refine") or {}
    default_mode = defaults.refine.default_mode
    modes_in_palette = list(defaults.refine.modes_in_palette)
    result_view = defaults.refine.result_view
    protect_paths = defaults.refine.protect_paths
    protect_code = defaults.refine.protect_code_blocks
    if isinstance(raw_refine, dict):
        if "default_mode" in raw_refine:
            val = str(raw_refine["default_mode"]).lower()
            if val in VALID_MODES:
                default_mode = val
            else:
                config_errors.append(f"editor.refine.default_mode must be one of {VALID_MODES}; got {val!r}")
        if "modes_in_palette" in raw_refine:
            val = raw_refine["modes_in_palette"]
            if isinstance(val, list):
                valid_items = [str(x).lower() for x in val if str(x).lower() in VALID_MODES]
                invalid_items = [str(x) for x in val if str(x).lower() not in VALID_MODES]
                if invalid_items:
                    config_errors.append(f"editor.refine.modes_in_palette contains invalid modes {invalid_items}; excluded")
                if valid_items:
                    modes_in_palette = valid_items
                else:
                    config_errors.append("editor.refine.modes_in_palette has no valid modes; using default")
            else:
                config_errors.append("editor.refine.modes_in_palette must be a list")
        if "result_view" in raw_refine:
            val = str(raw_refine["result_view"]).lower()
            if val in VALID_RESULT_VIEWS:
                result_view = val
            else:
                config_errors.append(f"editor.refine.result_view must be one of {VALID_RESULT_VIEWS}; got {val!r}")
        if "protect_paths" in raw_refine:
            val = raw_refine["protect_paths"]
            if isinstance(val, bool):
                protect_paths = val
            else:
                config_errors.append(f"editor.refine.protect_paths must be boolean; got {val!r}")
        if "protect_code_blocks" in raw_refine:
            val = raw_refine["protect_code_blocks"]
            if isinstance(val, bool):
                protect_code = val
            else:
                config_errors.append(f"editor.refine.protect_code_blocks must be boolean; got {val!r}")
    elif "refine" in raw_editor:
        config_errors.append("editor.refine must be a mapping")

    # Send config
    raw_send = raw_editor.get("send") or {}
    send_copy = defaults.send.copy
    auto_paste_last = defaults.send.auto_paste_to_last_window
    mark_sent = defaults.send.mark_sent_in_frontmatter
    if isinstance(raw_send, dict):
        if "copy" in raw_send:
            val = str(raw_send["copy"]).lower()
            if val in VALID_SEND_COPIES:
                send_copy = val
            else:
                config_errors.append(f"editor.send.copy must be one of {VALID_SEND_COPIES}; got {val!r}")
        if "auto_paste_to_last_window" in raw_send:
            val = raw_send["auto_paste_to_last_window"]
            if isinstance(val, bool):
                auto_paste_last = val
            else:
                config_errors.append(f"editor.send.auto_paste_to_last_window must be boolean; got {val!r}")
        if "mark_sent_in_frontmatter" in raw_send:
            val = raw_send["mark_sent_in_frontmatter"]
            if isinstance(val, bool):
                mark_sent = val
            else:
                config_errors.append(f"editor.send.mark_sent_in_frontmatter must be boolean; got {val!r}")
    elif "send" in raw_editor:
        config_errors.append("editor.send must be a mapping")

    # Frontmatter config
    raw_fm = raw_editor.get("frontmatter") or {}
    fm_fields = list(defaults.frontmatter.fields)
    if isinstance(raw_fm, dict):
        if "fields" in raw_fm:
            val = raw_fm["fields"]
            if isinstance(val, list) and all(isinstance(x, str) for x in val):
                fm_fields = [str(x).strip() for x in val if str(x).strip()]
            else:
                config_errors.append("editor.frontmatter.fields must be a list of strings")
    elif "frontmatter" in raw_editor:
        config_errors.append("editor.frontmatter must be a mapping")

    editor_cfg = EditorConfig(
        prompts_root=prompts_root,
        file_layout=file_layout,
        auto_rename_after_refine=auto_rename,
        autosave_delay_ms=autosave_delay_ms,
        server=EditorServerConfig(port=server_port, autostart_cmd=server_cmd),
        paste=EditorPasteConfig(
            image_dir=image_dir,
            image_name=image_name,
            path_style=path_style,
            quote_paths_with_spaces=quote_spaces,
            image_preview_on_hover=hover_preview,
        ),
        refine=EditorRefineConfig(
            default_mode=default_mode,
            modes_in_palette=modes_in_palette,
            result_view=result_view,
            protect_paths=protect_paths,
            protect_code_blocks=protect_code,
        ),
        send=EditorSendConfig(
            copy=send_copy,
            auto_paste_to_last_window=auto_paste_last,
            mark_sent_in_frontmatter=mark_sent,
        ),
        frontmatter=EditorFrontmatterConfig(fields=fm_fields),
    )

    # Resolve prompts_root: expanduser, resolve relative paths against project_root
    raw_root = editor_cfg.prompts_root
    p = Path(raw_root).expanduser()
    if not p.is_absolute():
        p = (project_root / p).resolve()
    else:
        p = p.resolve()
    editor_cfg.prompts_root = str(p)

    return editor_cfg, config_errors, resolved_path, s_path


def load_editor_config(
    config_path: Optional[Union[Path, str]] = None,
) -> EditorConfig:
    """Load editor configuration from config.yaml layered with settings.local.yaml."""
    cfg, _errors, _c_path, _s_path = load_editor_config_with_errors(config_path)
    return cfg
