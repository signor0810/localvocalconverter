"""Pure helper functions for HTML/TSV table parsing and formatting."""
from __future__ import annotations

import html as html_module
import re
from html.parser import HTMLParser
from typing import List, Optional


def extract_html_fragment(cf_html: str) -> str:
    """Extract HTML fragment from CF_HTML clipboard string using header offsets or comments."""
    if not cf_html:
        return ""

    m_start = re.search(r"StartFragment:(\d+)", cf_html, re.IGNORECASE)
    m_end = re.search(r"EndFragment:(\d+)", cf_html, re.IGNORECASE)
    if m_start and m_end:
        try:
            start_idx = int(m_start.group(1))
            end_idx = int(m_end.group(1))
            raw_bytes = cf_html.encode("utf-8")
            if 0 <= start_idx < end_idx <= len(raw_bytes):
                frag = raw_bytes[start_idx:end_idx].decode("utf-8", errors="replace")
                return frag.strip()
        except Exception:
            pass

    # Fallback to comment markers if byte offsets missing or invalid
    if "<!--StartFragment-->" in cf_html and "<!--EndFragment-->" in cf_html:
        s = cf_html.find("<!--StartFragment-->") + len("<!--StartFragment-->")
        e = cf_html.find("<!--EndFragment-->", s)
        if e != -1:
            return cf_html[s:e].strip()

    return cf_html.strip()


def build_cf_html(fragment: str) -> bytes:
    """Generate CF_HTML format byte string with accurate UTF-8 byte offsets."""
    header_tmpl = (
        "Version:0.9\r\n"
        "StartHTML:{start_html:010d}\r\n"
        "EndHTML:{end_html:010d}\r\n"
        "StartFragment:{start_fragment:010d}\r\n"
        "EndFragment:{end_fragment:010d}\r\n"
    )
    prefix = "<html>\r\n<body>\r\n<!--StartFragment-->"
    suffix = "<!--EndFragment-->\r\n</body>\r\n</html>"

    dummy_header = header_tmpl.format(
        start_html=0, end_html=0, start_fragment=0, end_fragment=0
    ).encode("utf-8")
    header_len = len(dummy_header)

    prefix_bytes = prefix.encode("utf-8")
    frag_bytes = fragment.encode("utf-8")
    suffix_bytes = suffix.encode("utf-8")

    start_html = header_len
    start_fragment = start_html + len(prefix_bytes)
    end_fragment = start_fragment + len(frag_bytes)
    end_html = end_fragment + len(suffix_bytes)

    header_bytes = header_tmpl.format(
        start_html=start_html,
        end_html=end_html,
        start_fragment=start_fragment,
        end_fragment=end_fragment,
    ).encode("utf-8")

    return header_bytes + prefix_bytes + frag_bytes + suffix_bytes


class _TableHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: List[List[str]] = []
        self._curr_row: List[str] = []
        self._curr_cell: List[str] = []
        self._in_cell: bool = False
        self._colspan: int = 1
        self._table_depth: int = 0
        self._ignored_depth: int = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        tag_lower = tag.lower()
        if tag_lower in ("style", "script"):
            self._ignored_depth += 1
            return

        if tag_lower == "table":
            self._table_depth += 1
        elif tag_lower in ("td", "th"):
            if self._table_depth == 1:
                self._in_cell = True
                self._curr_cell = []
                attr_dict = {k.lower(): v for k, v in attrs if v is not None}
                colspan_str = attr_dict.get("colspan", "1")
                try:
                    self._colspan = max(1, int(colspan_str))
                except ValueError:
                    self._colspan = 1
        elif tag_lower == "br" and self._in_cell and not self._ignored_depth:
            self._curr_cell.append("\n")

    def handle_endtag(self, tag: str, attrs: Optional[list[tuple[str, Optional[str]]]] = None) -> None:
        tag_lower = tag.lower()
        if tag_lower in ("style", "script"):
            self._ignored_depth = max(0, self._ignored_depth - 1)
            return

        if tag_lower in ("td", "th"):
            if self._table_depth == 1 and self._in_cell:
                cell_text = "".join(self._curr_cell).replace("\xa0", " ").strip()
                self._curr_row.append(cell_text)
                for _ in range(self._colspan - 1):
                    self._curr_row.append("")
                self._in_cell = False
                self._curr_cell = []
                self._colspan = 1
        elif tag_lower == "tr":
            if self._table_depth == 1:
                if self._curr_row:
                    self.rows.append(self._curr_row)
                self._curr_row = []
        elif tag_lower == "table":
            self._table_depth = max(0, self._table_depth - 1)

    def handle_data(self, data: str) -> None:
        if self._in_cell and not self._ignored_depth:
            self._curr_cell.append(data)


def html_table_to_rows(html_str: str) -> List[List[str]]:
    """Parse an HTML table into a 2D rectangular list of strings using standard html.parser."""
    parser = _TableHTMLParser()
    try:
        parser.feed(html_str)
        parser.close()
    except Exception:
        pass
    # If the last row was unclosed
    if parser._curr_row:
        parser.rows.append(parser._curr_row)

    # Uneven rows: pad short rows to the widest row
    max_cols = max((len(r) for r in parser.rows), default=0)
    for r in parser.rows:
        if len(r) < max_cols:
            r.extend([""] * (max_cols - len(r)))

    return parser.rows


def parse_single_table_html(html_str: str) -> Optional[tuple[str, List[List[str]]]]:
    """Check if HTML represents a table (<=20 chars outside table, 1 top-level table, >=2 cells).

    Returns (table_html, rows) where table_html is only the <table>...</table> element, or None.
    """
    if not html_str:
        return None

    frag = extract_html_fragment(html_str)
    if not frag:
        return None

    tag_re = re.compile(r"</?table\b[^>]*>", re.IGNORECASE)
    depth = 0
    tables: List[tuple[int, int]] = []
    start_pos = -1

    for m in tag_re.finditer(frag):
        tag = m.group(0).lower()
        is_close = tag.startswith("</")
        if not is_close:
            if depth == 0:
                start_pos = m.start()
            depth += 1
        else:
            if depth == 1:
                tables.append((start_pos, m.end()))
                depth = 0
            elif depth > 1:
                depth -= 1

    if len(tables) != 1:
        return None

    tbl_start, tbl_end = tables[0]
    outside = frag[:tbl_start] + frag[tbl_end:]
    outside_stripped = re.sub(r"<[^>]+>", "", outside)
    outside_clean = re.sub(r"\s+", "", outside_stripped)
    if len(outside_clean) > 20:
        return None

    table_html = frag[tbl_start:tbl_end].strip()
    rows = html_table_to_rows(table_html)
    if not rows:
        return None

    total_cells = sum(len(r) for r in rows)
    if total_cells < 2:
        return None

    return table_html, rows


def tsv_to_rows(text: str) -> Optional[List[List[str]]]:
    """Parse text as TSV if it has >=2 rows, >=2 cols per row, and consistent col counts."""
    stripped = text.strip()
    if not stripped:
        return None

    lines = [line for line in re.split(r"\r?\n", text.rstrip("\r\n")) if line.strip("\r\n")]
    if len(lines) < 2:
        return None

    rows = [line.split("\t") for line in lines]
    col_counts = [len(r) for r in rows]
    if min(col_counts) < 2:
        return None
    if min(col_counts) != max(col_counts):
        return None

    return rows


def rows_to_markdown(rows: List[List[str]]) -> str:
    """Convert a 2D list of cells into a Markdown table with escaped pipes and <br> for newlines."""
    if not rows:
        return ""

    escaped_rows: List[List[str]] = []
    for row in rows:
        escaped_row = [
            c.replace("|", r"\|").replace("\r\n", "<br>").replace("\n", "<br>").replace("\r", "<br>").strip()
            for c in row
        ]
        escaped_rows.append(escaped_row)

    col_count = len(escaped_rows[0])
    lines: List[str] = []
    lines.append("| " + " | ".join(escaped_rows[0]) + " |")
    lines.append("| " + " | ".join(["---"] * col_count) + " |")
    for row in escaped_rows[1:]:
        # Pad or trim if row length differs
        padded = (row + [""] * col_count)[:col_count]
        lines.append("| " + " | ".join(padded) + " |")

    return "\n".join(lines)


def rows_to_html(rows: List[List[str]]) -> str:
    """Generate clean HTML <table> from rows."""
    if not rows:
        return "<table></table>"

    lines = ["<table>"]
    header = rows[0]
    lines.append("  <thead>")
    th_cells = "".join(
        f"<th>{html_module.escape(c).replace(chr(10), '<br>')}</th>" for c in header
    )
    lines.append(f"    <tr>{th_cells}</tr>")
    lines.append("  </thead>")

    if len(rows) > 1:
        lines.append("  <tbody>")
        for row in rows[1:]:
            td_cells = "".join(
                f"<td>{html_module.escape(c).replace(chr(10), '<br>')}</td>" for c in row
            )
            lines.append(f"    <tr>{td_cells}</tr>")
        lines.append("  </tbody>")

    lines.append("</table>")
    return "\n".join(lines)
