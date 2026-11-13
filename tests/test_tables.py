"""Unit tests for capture/tables.py pure functions."""
from __future__ import annotations

import pytest

from local_typeless.capture.tables import (
    build_cf_html,
    extract_html_fragment,
    html_table_to_rows,
    parse_single_table_html,
    rows_to_html,
    rows_to_markdown,
    tsv_to_rows,
)


def test_build_and_extract_cf_html_roundtrip():
    fragment = "<table><tr><td>測試中文</td><td>123</td></tr></table>"
    cf_bytes = build_cf_html(fragment)
    assert isinstance(cf_bytes, bytes)

    # Decode and check header fields
    cf_str = cf_bytes.decode("utf-8")
    assert "StartFragment:" in cf_str
    assert "EndFragment:" in cf_str
    assert "StartHTML:" in cf_str
    assert "EndHTML:" in cf_str

    extracted = extract_html_fragment(cf_str)
    assert extracted == fragment


def test_build_cf_html_exact_byte_offsets():
    # Chinese characters take 3 bytes in UTF-8
    fragment = "<table><tr><th>項目</th><th>數值</th></tr><tr><td>微影技術</td><td>ILT</td></tr></table>"
    cf_bytes = build_cf_html(fragment)

    import re
    m_start = re.search(r"StartFragment:(\d+)", cf_bytes.decode("utf-8"))
    m_end = re.search(r"EndFragment:(\d+)", cf_bytes.decode("utf-8"))
    assert m_start and m_end
    start_offset = int(m_start.group(1))
    end_offset = int(m_end.group(1))

    sliced = cf_bytes[start_offset:end_offset].decode("utf-8")
    assert sliced == fragment


def test_extract_html_fragment_fallback_comments():
    raw = (
        "Version:1.0\r\n"
        "<html><body>\r\n"
        "<!--StartFragment--><table><tr><td>Hello</td></tr></table><!--EndFragment-->\r\n"
        "</body></html>"
    )
    extracted = extract_html_fragment(raw)
    assert extracted == "<table><tr><td>Hello</td></tr></table>"


def test_extract_html_fragment_plain():
    frag = "<table><tr><td>Direct</td></tr></table>"
    assert extract_html_fragment(frag) == frag


def test_html_table_to_rows_basic():
    html = """
    <table>
      <thead>
        <tr><th>Name</th><th>Score</th></tr>
      </thead>
      <tbody>
        <tr><td>Alice</td><td>100</td></tr>
        <tr><td>Bob</td><td>90</td></tr>
      </tbody>
    </table>
    """
    rows = html_table_to_rows(html)
    assert rows == [
        ["Name", "Score"],
        ["Alice", "100"],
        ["Bob", "90"],
    ]


def test_html_table_to_rows_nested_tags_and_br():
    html = """
    <table>
      <tr>
        <td><b>Bold</b> and <i>italic</i></td>
        <td>Line 1<br>Line 2<br/>Line 3</td>
      </tr>
    </table>
    """
    rows = html_table_to_rows(html)
    assert len(rows) == 1
    assert rows[0][0] == "Bold and italic"
    assert rows[0][1] == "Line 1\nLine 2\nLine 3"


def test_html_table_to_rows_colspan():
    html = """
    <table>
      <tr>
        <th colspan="2">Merged Header</th>
        <th>Single</th>
      </tr>
      <tr>
        <td>A</td><td>B</td><td>C</td>
      </tr>
    </table>
    """
    rows = html_table_to_rows(html)
    assert rows == [
        ["Merged Header", "", "Single"],
        ["A", "B", "C"],
    ]


def test_html_table_to_rows_entities():
    html = """
    <table>
      <tr><td>A &amp; B</td><td>&lt;Tag&gt;</td><td>&quot;Quote&quot;</td><td>Non&nbsp;breaking</td></tr>
    </table>
    """
    rows = html_table_to_rows(html)
    assert rows == [
        ["A & B", "<Tag>", '"Quote"', "Non breaking"],
    ]


def test_tsv_to_rows_valid():
    tsv = "Col1\tCol2\tCol3\r\nA\tB\tC\r\n1\t2\t3\n"
    rows = tsv_to_rows(tsv)
    assert rows == [
        ["Col1", "Col2", "Col3"],
        ["A", "B", "C"],
        ["1", "2", "3"],
    ]


def test_tsv_to_rows_invalid():
    # Only 1 row -> None
    assert tsv_to_rows("Col1\tCol2\tCol3") is None
    # Only 1 column -> None
    assert tsv_to_rows("Line1\nLine2\nLine3") is None
    # Inconsistent column counts -> None
    assert tsv_to_rows("A\tB\r\nC\tD\tE") is None
    # Empty string -> None
    assert tsv_to_rows("") is None
    assert tsv_to_rows("   \n\t  ") is None


def test_rows_to_markdown_formatting():
    rows = [
        ["項目", "說明", "備註"],
        ["A | B", "第一行\n第二行", "正常"],
        ["C", "D", "E"],
    ]
    md = rows_to_markdown(rows)
    expected = (
        "| 項目 | 說明 | 備註 |\n"
        "| --- | --- | --- |\n"
        "| A \\| B | 第一行<br>第二行 | 正常 |\n"
        "| C | D | E |"
    )
    assert md == expected


def test_rows_to_markdown_empty():
    assert rows_to_markdown([]) == ""


def test_rows_to_html_formatting():
    rows = [
        ["Col1", "Col2"],
        ["Val 1", "Line1\nLine2"],
    ]
    html = rows_to_html(rows)
    assert "<table>" in html
    assert "<th>Col1</th>" in html
    assert "<th>Col2</th>" in html
    assert "<td>Val 1</td>" in html
    assert "<td>Line1<br>Line2</td>" in html


def test_html_table_to_rows_script_style_comment():
    html = """
    <style>td { color: red; }</style>
    <table>
      <tr>
        <td><!-- comment -->Hello<script>console.log("bad");</script> World</td>
        <td>Valid</td>
      </tr>
    </table>
    """
    rows = html_table_to_rows(html)
    assert rows == [["Hello World", "Valid"]]


def test_html_table_to_rows_nested_table_flattened():
    html = """
    <table>
      <tr>
        <td>Outer 1</td>
        <td>
          <table><tr><td>Nested A</td><td>Nested B</td></tr></table>
        </td>
      </tr>
    </table>
    """
    rows = html_table_to_rows(html)
    assert len(rows) == 1
    assert len(rows[0]) == 2
    assert rows[0][0] == "Outer 1"
    assert "Nested A" in rows[0][1] and "Nested B" in rows[0][1]


def test_html_table_to_rows_uneven_rows_padded():
    html = """
    <table>
      <tr><td>A</td><td>B</td><td>C</td></tr>
      <tr><td>D</td></tr>
      <tr><td>E</td><td>F</td></tr>
    </table>
    """
    rows = html_table_to_rows(html)
    assert rows == [
        ["A", "B", "C"],
        ["D", "", ""],
        ["E", "F", ""],
    ]


def test_parse_single_table_html_detection():
    # Valid clean table
    valid_html = "<div><table><tr><td>A</td><td>B</td></tr></table></div>"
    res = parse_single_table_html(valid_html)
    assert res is not None
    table_html, rows = res
    assert table_html == "<table><tr><td>A</td><td>B</td></tr></table>"
    assert rows == [["A", "B"]]

    # Negligible outside text (<= 20 chars) -> accepted
    short_outside = "<p>Intro note</p><table><tr><td>A</td><td>B</td></tr></table>"
    res_short = parse_single_table_html(short_outside)
    assert res_short is not None
    assert res_short[0] == "<table><tr><td>A</td><td>B</td></tr></table>"

    # Too much outside text (> 20 chars) -> rejected
    long_outside = "<p>This is a long explanatory paragraph preceding the table</p><table><tr><td>A</td><td>B</td></tr></table>"
    assert parse_single_table_html(long_outside) is None

    # Multiple top-level tables -> rejected
    two_tables = "<table><tr><td>A</td><td>B</td></tr></table><table><tr><td>C</td><td>D</td></tr></table>"
    assert parse_single_table_html(two_tables) is None

    # Less than 2 cells -> rejected
    single_cell = "<table><tr><td>OnlyOne</td></tr></table>"
    assert parse_single_table_html(single_cell) is None

