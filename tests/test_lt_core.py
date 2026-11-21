"""
Unit tests for Sublime Text plugin core logic (lt_core.py) and AST compliance.
"""

import ast
import json
import os
import sys
from datetime import datetime
from pathlib import Path

# Add sublime/LocalTypeless to sys.path so lt_core can be imported directly
REPO_ROOT = Path(__file__).resolve().parent.parent
SUBLIME_PKG_DIR = REPO_ROOT / "sublime" / "LocalTypeless"
if str(SUBLIME_PKG_DIR) not in sys.path:
    sys.path.insert(0, str(SUBLIME_PKG_DIR))

import lt_core


def test_path_styles_and_spaces():
    # 1. Normal paths without spaces
    test_p = r"C:\Users\tester\images\test.png"
    assert lt_core.format_path(test_p, style="windows") == r"C:\Users\tester\images\test.png"
    assert lt_core.format_path(test_p, style="forward") == "C:/Users/tester/images/test.png"
    assert lt_core.format_path(test_p, style="wsl") == "/mnt/c/Users/tester/images/test.png"
    assert lt_core.format_path(test_p, style="markdown") == "![](C:/Users/tester/images/test.png)"

    # 2. Paths with spaces and quote_spaces=True (default)
    space_p = r"C:\Users\tester\My Images\test photo.png"
    assert lt_core.format_path(space_p, style="windows") == r'"C:\Users\tester\My Images\test photo.png"'
    assert lt_core.format_path(space_p, style="forward") == '"C:/Users/tester/My Images/test photo.png"'
    assert lt_core.format_path(space_p, style="wsl") == '"/mnt/c/Users/tester/My Images/test photo.png"'
    # Markdown style should NOT wrap outer double quotes
    assert lt_core.format_path(space_p, style="markdown") == "![](C:/Users/tester/My Images/test photo.png)"

    # 3. Paths with spaces and quote_spaces=False
    assert lt_core.format_path(space_p, style="windows", quote_spaces=False) == r"C:\Users\tester\My Images\test photo.png"
    assert lt_core.format_path(space_p, style="forward", quote_spaces=False) == "C:/Users/tester/My Images/test photo.png"
    assert lt_core.format_path(space_p, style="wsl", quote_spaces=False) == "/mnt/c/Users/tester/My Images/test photo.png"


def test_frontmatter_parse_and_render_roundtrip():
    original_text = (
        "---\n"
        "created: 2026-11-20 14:32\n"
        "status: draft\n"
        "mode: clean\n"
        "refined_at:\n"
        "sent_at:\n"
        "target:\n"
        "---\n\n"
        "# My Heading\n\nSome body text here.\n"
    )

    fields, body, body_offset = lt_core.parse_frontmatter(original_text)
    assert fields["created"] == "2026-11-20 14:32"
    assert fields["status"] == "draft"
    assert fields["mode"] == "clean"
    assert fields["refined_at"] == ""
    assert fields["sent_at"] == ""
    assert fields["target"] == ""
    assert body == "# My Heading\n\nSome body text here.\n"
    assert original_text[body_offset:] == body

    rendered = lt_core.render_frontmatter(fields)
    assert rendered.startswith("---\ncreated: 2026-11-20 14:32\n")
    assert "refined_at:\n" in rendered
    assert rendered.endswith("---\n\n")

    # Round trip full document reconstruction
    reconstructed = rendered + body
    fields2, body2, _ = lt_core.parse_frontmatter(reconstructed)
    assert fields2 == fields
    assert body2 == body

    # Custom field_order verification
    custom_order = ["mode", "status", "created", "target", "sent_at", "refined_at"]
    rendered_custom = lt_core.render_frontmatter(fields, field_order=custom_order)
    assert rendered_custom.startswith("---\nmode: clean\nstatus: draft\ncreated: 2026-11-20 14:32\n")


def test_frontmatter_crlf_and_edge_cases():
    crlf_text = (
        "---\r\n"
        "created: 2026-11-20\r\n"
        "status: draft\r\n"
        "---\r\n\r\n"
        "CRLF Body Text\r\n"
    )
    fields, body, body_offset = lt_core.parse_frontmatter(crlf_text)
    assert fields["created"] == "2026-11-20"
    assert fields["status"] == "draft"
    assert body == "CRLF Body Text\r\n"
    assert crlf_text[body_offset:] == body

    # Missing frontmatter
    no_fm = "Just normal markdown\nSecond line"
    fields, body, body_offset = lt_core.parse_frontmatter(no_fm)
    assert fields == {}
    assert body == no_fm
    assert body_offset == 0

    # Unclosed frontmatter
    unclosed = "---\ncreated: 2026-11-20\nNo closing marker"
    fields, body, body_offset = lt_core.parse_frontmatter(unclosed)
    assert fields == {}
    assert body == unclosed
    assert body_offset == 0


def test_update_frontmatter():
    text = (
        "---\n"
        "created: 2026-11-20 10:00\n"
        "status: draft\n"
        "mode: clean\n"
        "---\n\n"
        "# Heading\n\nParagraph 1.\nParagraph 2."
    )
    updated = lt_core.update_frontmatter(
        text,
        {"status": "refined", "refined_at": "2026-11-20 10:05"},
    )
    fields, body, _ = lt_core.parse_frontmatter(updated)
    assert fields["status"] == "refined"
    assert fields["refined_at"] == "2026-11-20 10:05"
    assert fields["created"] == "2026-11-20 10:00"
    assert body.strip() == "# Heading\n\nParagraph 1.\nParagraph 2."

    # When no frontmatter originally exists
    raw_doc = "# Title without frontmatter\n\nContent here."
    added_fm = lt_core.update_frontmatter(raw_doc, {"status": "draft", "created": "2026-11-20"})
    fields_add, body_add, _ = lt_core.parse_frontmatter(added_fm)
    assert fields_add["status"] == "draft"
    assert fields_add["created"] == "2026-11-20"
    assert body_add == raw_doc

    # Preserves multiline body and exact trailing newlines
    multiline_body = "Line 1\nLine 2 with special chars: 100% #1\n\nLine 4\n\n\n"
    doc_with_fm = f"---\nstatus: draft\nmode: clean\n---\n\n{multiline_body}"
    updated_doc = lt_core.update_frontmatter(
        doc_with_fm,
        {"status": "refined", "refined_at": "2026-11-20 15:00"},
    )
    f_ml, b_ml, off_ml = lt_core.parse_frontmatter(updated_doc)
    assert f_ml["status"] == "refined"
    assert f_ml["mode"] == "clean"
    assert f_ml["refined_at"] == "2026-11-20 15:00"
    assert b_ml == multiline_body
    assert updated_doc[off_ml:] == multiline_body

    # Preserves multiline body and trailing newlines on doc without frontmatter
    doc_no_fm = "First line without fm.\nSecond line.\n\nEnd.\n\n"
    added_no_fm = lt_core.update_frontmatter(
        doc_no_fm,
        {"status": "draft", "mode": "prompt"},
    )
    f_n, b_n, off_n = lt_core.parse_frontmatter(added_no_fm)
    assert f_n["status"] == "draft"
    assert f_n["mode"] == "prompt"
    assert b_n == doc_no_fm
    assert added_no_fm[off_n:] == doc_no_fm


def test_merge_editor_config():
    # 1. Merging nested config (from /config)
    nested = {
        "prompts_root": "D:/MyPrompts",
        "paste": {"path_style": "forward", "quote_paths_with_spaces": False},
        "refine": {"default_mode": "prompt", "result_view": "below"},
        "send": {"copy": "both", "auto_paste_to_last_window": True},
        "server": {"port": 9999, "autostart_cmd": "pythonw -m test"},
    }
    cfg = lt_core.merge_editor_config(nested)
    assert cfg["prompts_root"] == "D:/MyPrompts"
    assert cfg["path_style"] == "forward"
    assert cfg["paste"]["path_style"] == "forward"
    assert cfg["quote_paths_with_spaces"] is False
    assert cfg["default_mode"] == "prompt"
    assert cfg["result_view"] == "below"
    assert cfg["copy"] == "both"
    assert cfg["auto_paste_to_last_window"] is True
    assert cfg["server"]["port"] == 9999
    assert cfg["autostart_cmd"] == "pythonw -m test"

    # 2. Merging flat config (from /health)
    flat = {
        "prompts_root": "C:/FlatPrompts",
        "path_style": "wsl",
        "default_mode": "summary",
        "result_view": "replace",
    }
    cfg2 = lt_core.merge_editor_config(flat)
    assert cfg2["prompts_root"] == "C:/FlatPrompts"
    assert cfg2["path_style"] == "wsl"
    assert cfg2["paste"]["path_style"] == "wsl"
    assert cfg2["default_mode"] == "summary"
    assert cfg2["result_view"] == "replace"


def test_extract_title_slug():
    # 1. From Markdown heading
    text_with_h1 = "# 計算微影基本概念介紹\n\n今天想跟大家聊聊..."
    assert lt_core.extract_title_slug(text_with_h1) == "計算微影基本概念介紹"

    # 2. From first sentence
    text_plain = "這是一個沒有標題的文件。但是第一句話很重要！後面還有第二句。"
    assert lt_core.extract_title_slug(text_plain) == "這是一個沒有標題的文件"

    # 3. Windows illegal characters and max_len truncation
    text_illegal = "### 專案分析: 關於 *?<>| 的架構設計細節說明與規劃報告"
    slug = lt_core.extract_title_slug(text_illegal, max_len=15)
    assert len(slug) <= 15
    assert not any(c in slug for c in r'\/:*?"<>|')

    # 4. Empty text
    assert lt_core.extract_title_slug("") == "untitled"


def test_renamed_paths_for(tmp_path):
    src = str(tmp_path / "2026-11-20_143000_untitled.md")
    ref = str(tmp_path / "2026-11-20_143000_untitled.refined.md")
    slug = "計算微影"

    # Normal rename
    renamed = lt_core.renamed_paths_for(src, ref, slug)
    assert renamed is not None
    new_src, new_ref = renamed
    assert new_src.endswith("2026-11-20_143000_計算微影.md")
    assert new_ref.endswith("2026-11-20_143000_計算微影.refined.md")

    # If target already exists, auto-increments
    Path(new_src).write_text("dummy", encoding="utf-8")
    renamed2 = lt_core.renamed_paths_for(src, ref, slug)
    assert renamed2 is not None
    new_src2, new_ref2 = renamed2
    assert new_src2.endswith("2026-11-20_143000_計算微影_2.md")
    assert new_ref2.endswith("2026-11-20_143000_計算微影_2.refined.md")

    # Non-untitled file should return None
    fixed_src = str(tmp_path / "2026-11-20_143000_custom_name.md")
    fixed_ref = str(tmp_path / "2026-11-20_143000_custom_name.refined.md")
    assert lt_core.renamed_paths_for(fixed_src, fixed_ref, slug) is None


def test_list_prompt_history(tmp_path):
    root = tmp_path / "prompts"
    root.mkdir()

    # Create normal prompt files
    f1 = root / "2026-11-20_100000_p1.md"
    f1.write_text("---\nstatus: draft\nmode: clean\n---\n\nFirst prompt content here", encoding="utf-8")

    f2 = root / "2026-11-20_110000_p2.md"
    f2.write_text("---\nstatus: refined\nmode: prompt\n---\n\nSecond prompt content here", encoding="utf-8")

    # Create .refined.md (must be ignored)
    f2_ref = root / "2026-11-20_110000_p2.refined.md"
    f2_ref.write_text("Refined content", encoding="utf-8")

    # Create assets dir (must be ignored)
    assets_dir = root / "assets"
    assets_dir.mkdir()
    (assets_dir / "ignored.md").write_text("ignored", encoding="utf-8")

    history = lt_core.list_prompt_history(str(root), limit=10)
    assert len(history) == 2
    paths = [item["path"] for item in history]
    assert str(f2) in paths
    assert str(f1) in paths
    assert str(f2_ref) not in paths
    assert str(assets_dir / "ignored.md") not in paths


def test_extract_image_path_at(tmp_path):
    base = str(tmp_path)
    # 1. Quoted path
    line1 = r'Here is an image: "C:\images\sample photo.png" in the line.'
    col1 = line1.find("sample photo")
    extracted1 = lt_core.extract_image_path_at(line1, col1, base_dir=base)
    assert extracted1 == r"C:\images\sample photo.png"

    # 2. Markdown image
    line2 = r"Check ![](assets/2026-11/test.png) for diagram."
    col2 = line2.find("test.png")
    extracted2 = lt_core.extract_image_path_at(line2, col2, base_dir=base)
    assert extracted2 == os.path.normpath(os.path.join(base, "assets/2026-11/test.png"))

    # 3. WSL path
    line3 = r"Path is /mnt/d/projects/img.webp here"
    col3 = line3.find("img.webp")
    extracted3 = lt_core.extract_image_path_at(line3, col3, base_dir=base)
    assert extracted3 == r"d:\projects\img.webp"

    # 4. Col outside image path bounds
    assert lt_core.extract_image_path_at(line1, 0, base_dir=base) is None


def test_read_image_data_uri(tmp_path):
    # 1. Real 1x1 PNG image
    png_bytes = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x06\x00\x00\x00\x1f\x15c4\x00\x00\x00\rIDATx\x9cc`\x00\x00\x00"
        b"\x02\x00\x01H\xaf\xa4q\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    png_file = tmp_path / "tiny.png"
    png_file.write_bytes(png_bytes)

    data_uri, w, h, err = lt_core.read_image_data_uri(str(png_file), max_width=400)
    assert not err
    assert data_uri is not None and data_uri.startswith("data:image/png;base64,")
    assert w == 1 and h == 1

    # 2. File exceeding 1 MB
    large_file = tmp_path / "large.png"
    large_file.write_bytes(b"\x00" * (1024 * 1024 + 10))
    data_uri_large, _, _, err_large = lt_core.read_image_data_uri(str(large_file), max_bytes=1024 * 1024)
    assert data_uri_large is None
    assert "超過 1 MB" in err_large


def test_find_repo_autostart_and_panel_cmd():
    fake_plugin = str(SUBLIME_PKG_DIR / "lt_plugin.py")

    # Disabled
    assert lt_core.find_repo_autostart_cmd(fake_plugin, configured_cmd="off") == (None, None)
    assert lt_core.find_repo_autostart_cmd(fake_plugin, configured_cmd="none") == (None, None)

    # Custom command
    custom_cmd, root = lt_core.find_repo_autostart_cmd(fake_plugin, configured_cmd="python -m my_serve --port 8765")
    assert custom_cmd == ["python", "-m", "my_serve", "--port", "8765"]
    assert root is not None and os.path.isdir(root)

    # Default command locates .venv pythonw / python
    def_cmd, root_def = lt_core.find_repo_autostart_cmd(fake_plugin, configured_cmd="")
    assert def_cmd is not None
    assert "serve" in def_cmd

    # Panel command
    panel_cmd, _ = lt_core.find_repo_panel_cmd(fake_plugin, page="settings")
    assert panel_cmd is not None
    assert "panel" in panel_cmd
    assert "--page" in panel_cmd


def test_menu_and_commands_json_validity():
    """Validates that all .sublime-keymap, .sublime-menu, and .sublime-commands files are strictly valid JSON."""
    keymap_p = SUBLIME_PKG_DIR / "Default (Windows).sublime-keymap"
    context_p = SUBLIME_PKG_DIR / "Context.sublime-menu"
    main_p = SUBLIME_PKG_DIR / "Main.sublime-menu"
    commands_p = SUBLIME_PKG_DIR / "Default.sublime-commands"

    for p in (keymap_p, context_p, main_p, commands_p):
        assert p.is_file(), f"File {p.name} must exist"
        data = json.loads(p.read_text(encoding="utf-8"))
        assert isinstance(data, list)


def test_expand_file_layout_and_collision(tmp_path):
    fixed_time = datetime(2026, 11, 20, 14, 30, 45)
    layout = "{yyyy}/{mm}/{date}_{time}_{title}.md"
    prompts_root = str(tmp_path / "prompts")

    # Title with illegal characters
    title = 'test: illegal/chars*in?title"<>|'
    expanded = lt_core.expand_file_layout(
        prompts_root,
        layout,
        now=fixed_time,
        title=title,
    )
    expected_rel = os.path.normpath("2026/11/2026-11-20_143045_test illegalcharsintitle.md")
    assert expanded.endswith(expected_rel)

    # Collision test
    os.makedirs(os.path.dirname(expanded), exist_ok=True)
    with open(expanded, "w", encoding="utf-8") as f:
        f.write("first")

    expanded2 = lt_core.expand_file_layout(
        prompts_root,
        layout,
        now=fixed_time,
        title=title,
    )
    assert expanded2.endswith("2026-11-20_143045_test illegalcharsintitle_2.md")


def test_is_managed_path(tmp_path):
    root = tmp_path / "prompts"
    root.mkdir()
    sub_dir = root / "2026" / "11"
    sub_dir.mkdir(parents=True)

    good_file = sub_dir / "test.md"
    good_file.write_text("hello", encoding="utf-8")

    txt_file = sub_dir / "test.txt"
    txt_file.write_text("hello", encoding="utf-8")

    outside_dir = tmp_path / "other"
    outside_dir.mkdir()
    outside_file = outside_dir / "test.md"
    outside_file.write_text("hello", encoding="utf-8")

    # Prefix-similar directory (e.g. prompts_extra alongside prompts) must NOT be managed
    extra_dir = tmp_path / "prompts_extra"
    extra_dir.mkdir()
    extra_file = extra_dir / "test.md"
    extra_file.write_text("hello", encoding="utf-8")

    assert lt_core.is_managed_path(str(good_file), str(root)) is True
    assert lt_core.is_managed_path(str(txt_file), str(root)) is False
    assert lt_core.is_managed_path(str(outside_file), str(root)) is False
    assert lt_core.is_managed_path(str(extra_file), str(root)) is False

    # Windows case-insensitivity
    if sys.platform.startswith("win"):
        upper_file = str(good_file).upper()
        lower_root = str(root).lower()
        assert lt_core.is_managed_path(upper_file, lower_root) is True


def test_refined_paths():
    p = r"C:\prompts\2026\11\my_prompt.md"
    assert lt_core.refined_path_for(p) == r"C:\prompts\2026\11\my_prompt.refined.md"
    assert lt_core.is_refined_path(lt_core.refined_path_for(p)) is True
    assert lt_core.is_refined_path(p) is False


def test_read_serve_info(tmp_path):
    valid_serve = tmp_path / "serve.json"
    valid_serve.write_text(
        json.dumps({"port": 8765, "token": "abc123hex", "pid": 4567}),
        encoding="utf-8",
    )
    info = lt_core.read_serve_info(str(valid_serve))
    assert info is not None
    assert info["port"] == 8765
    assert info["token"] == "abc123hex"
    assert info["pid"] == 4567


def test_python_38_ast_and_dependencies():
    """
    Verifies that lt_core.py and lt_plugin.py parse under Python 3.8 AST rules
    and do not import unauthorized dependencies.
    """
    lt_core_path = SUBLIME_PKG_DIR / "lt_core.py"
    lt_plugin_path = SUBLIME_PKG_DIR / "lt_plugin.py"

    assert lt_core_path.is_file(), "lt_core.py must exist"
    assert lt_plugin_path.is_file(), "lt_plugin.py must exist"

    # 1. Parse with Python 3.8 feature version
    core_code = lt_core_path.read_text(encoding="utf-8")
    plugin_code = lt_plugin_path.read_text(encoding="utf-8")

    tree_core = ast.parse(core_code, filename="lt_core.py", feature_version=(3, 8))
    tree_plugin = ast.parse(plugin_code, filename="lt_plugin.py", feature_version=(3, 8))

    assert tree_core is not None
    assert tree_plugin is not None

    # 2. Check stdlib modules in Python 3.8
    allowed_core_modules = {
        "ast", "os", "sys", "re", "json", "datetime", "typing", "urllib",
        "urllib.request", "urllib.error", "pathlib", "math", "time",
        "base64", "ctypes", "shlex", "struct", "subprocess", "threading"
    }

    for node in ast.walk(tree_core):
        if isinstance(node, ast.Import):
            for alias in node.names:
                mod = alias.name.split(".")[0]
                assert mod in allowed_core_modules, f"Disallowed import in lt_core.py: {alias.name}"
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                mod = node.module.split(".")[0]
                assert mod in allowed_core_modules, f"Disallowed from-import in lt_core.py: {node.module}"

    # 3. Check allowed modules for lt_plugin.py
    allowed_plugin_modules = allowed_core_modules | {
        "sublime", "sublime_plugin", "lt_core"
    }

    for node in ast.walk(tree_plugin):
        if isinstance(node, ast.Import):
            for alias in node.names:
                mod = alias.name.split(".")[0]
                assert mod in allowed_plugin_modules, f"Disallowed import in lt_plugin.py: {alias.name}"
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                mod = node.module.split(".")[0]
                assert mod in allowed_plugin_modules, f"Disallowed from-import in lt_plugin.py: {node.module}"
