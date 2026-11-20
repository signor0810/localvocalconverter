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

    # Second call with same timestamp and title should produce _2.md
    expanded2 = lt_core.expand_file_layout(
        prompts_root,
        layout,
        now=fixed_time,
        title=title,
    )
    assert expanded2.endswith("2026-11-20_143045_test illegalcharsintitle_2.md")

    # Third call produces _3.md
    with open(expanded2, "w", encoding="utf-8") as f:
        f.write("second")
    expanded3 = lt_core.expand_file_layout(
        prompts_root,
        layout,
        now=fixed_time,
        title=title,
    )
    assert expanded3.endswith("2026-11-20_143045_test illegalcharsintitle_3.md")


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

    prefix_similar_dir = tmp_path / "prompts_extra"
    prefix_similar_dir.mkdir()
    prefix_similar_file = prefix_similar_dir / "test.md"
    prefix_similar_file.write_text("hello", encoding="utf-8")

    # Valid managed path
    assert lt_core.is_managed_path(str(good_file), str(root)) is True

    # Windows case insensitivity
    assert lt_core.is_managed_path(str(good_file).upper(), str(root).lower()) is True

    # Rejection of non-md extension
    assert lt_core.is_managed_path(str(txt_file), str(root)) is False

    # Rejection of files outside root
    assert lt_core.is_managed_path(str(outside_file), str(root)) is False

    # Prevention of prefix-similar directory confusion
    assert lt_core.is_managed_path(str(prefix_similar_file), str(root)) is False

    # Root itself is not a managed markdown file
    assert lt_core.is_managed_path(str(root), str(root)) is False


def test_refined_paths():
    p = r"C:\prompts\2026\11\my_prompt.md"
    assert lt_core.refined_path_for(p) == r"C:\prompts\2026\11\my_prompt.refined.md"
    assert lt_core.is_refined_path(lt_core.refined_path_for(p)) is True

    # Already refined path
    refined_p = r"C:\prompts\2026\11\my_prompt.refined.md"
    assert lt_core.refined_path_for(refined_p) == refined_p
    assert lt_core.is_refined_path(refined_p) is True

    # Normal prompt is not refined
    assert lt_core.is_refined_path(p) is False


def test_read_serve_info(tmp_path):
    # Non-existent
    non_existent = tmp_path / "serve_none.json"
    assert lt_core.read_serve_info(str(non_existent)) is None

    # Valid
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

    # Corrupted
    corrupted_serve = tmp_path / "corrupted.json"
    corrupted_serve.write_text("invalid json syntax", encoding="utf-8")
    assert lt_core.read_serve_info(str(corrupted_serve)) is None


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
    # Standard library modules available in Python 3.8
    allowed_core_modules = {
        "ast", "os", "sys", "re", "json", "datetime", "typing", "urllib",
        "urllib.request", "urllib.error", "pathlib", "math", "time"
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