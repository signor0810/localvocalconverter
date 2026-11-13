"""Draft editor model (pure business logic, no Tkinter).

Manages the draft text buffer, dictation pieces, attachments/embeddings,
autosave persistence, and whole-draft LLM refinement protection.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, Sequence, Tuple

from local_typeless.output.rich_paste import format_path
from local_typeless.types import DraftPiece

logger = logging.getLogger(__name__)


def is_latin_or_digit(c: str) -> bool:
    """Return True if character is an ASCII Latin letter or digit."""
    return bool(c and (("a" <= c <= "z") or ("A" <= c <= "Z") or ("0" <= c <= "9")))


class TextBuffer(Protocol):
    """Protocol for abstracting text buffer operations."""

    def get_text(self, start: Optional[int] = None, end: Optional[int] = None) -> str: ...
    def set_text(self, text: str) -> None: ...
    def clear(self) -> None: ...
    def get_cursor(self) -> int: ...
    def set_cursor(self, pos: int) -> None: ...
    def set_mark(self, name: str, pos: Any, gravity: str = "right") -> None: ...
    def get_mark(self, name: str) -> Optional[int]: ...
    def delete_mark(self, name: str) -> None: ...
    def insert(self, pos: Any, text: str, tags: Sequence[str] = ()) -> None: ...
    def delete(self, start: int, end: int) -> None: ...
    def add_tag(self, tag: str, start: int, end: int) -> None: ...
    def get_tag_ranges(self, tag: str) -> List[Tuple[int, int]]: ...
    def clear_tag(self, tag: str) -> None: ...


class MemoryBuffer:
    """Pure-Python in-memory implementation of TextBuffer for headless testing."""

    def __init__(self, initial_text: str = "") -> None:
        self._text: str = initial_text
        self._cursor: int = len(initial_text)
        self._marks: Dict[str, Tuple[int, str]] = {}  # name -> (pos, gravity)
        self._tags: Dict[str, List[Tuple[int, int]]] = {}

    def get_text(self, start: Optional[int] = None, end: Optional[int] = None) -> str:
        s = 0 if start is None else max(0, start)
        e = len(self._text) if end is None else min(len(self._text), end)
        return self._text[s:e]

    def set_text(self, text: str) -> None:
        self._text = text
        self._cursor = len(text)
        self._marks.clear()
        self._tags.clear()

    def clear(self) -> None:
        self.set_text("")

    def get_cursor(self) -> int:
        return self._cursor

    def set_cursor(self, pos: int) -> None:
        self._cursor = max(0, min(len(self._text), pos))

    def set_mark(self, name: str, pos: Any, gravity: str = "right") -> None:
        if isinstance(pos, str):
            resolved = self.get_mark(pos)
            p = resolved if resolved is not None else self._cursor
        else:
            p = max(0, min(len(self._text), int(pos)))
        self._marks[name] = (p, gravity)

    def get_mark(self, name: str) -> Optional[int]:
        if name in self._marks:
            return self._marks[name][0]
        if name == "insert":
            return self._cursor
        return None

    def delete_mark(self, name: str) -> None:
        self._marks.pop(name, None)

    def insert(self, pos: Any, text: str, tags: Sequence[str] = ()) -> None:
        if not text:
            return
        if isinstance(pos, str):
            resolved = self.get_mark(pos)
            p = resolved if resolved is not None else self._cursor
        else:
            p = max(0, min(len(self._text), int(pos)))

        self._text = self._text[:p] + text + self._text[p:]
        delta = len(text)

        # Shift marks
        for m_name, (m_pos, gravity) in list(self._marks.items()):
            if m_pos > p:
                self._marks[m_name] = (m_pos + delta, gravity)
            elif m_pos == p:
                if gravity == "right":
                    self._marks[m_name] = (m_pos + delta, gravity)

        # Update cursor if insertion was before or at cursor
        if p <= self._cursor:
            self._cursor += delta

        # Shift tags
        for t_name, ranges in list(self._tags.items()):
            new_ranges: List[Tuple[int, int]] = []
            for r_start, r_end in ranges:
                if r_start >= p:
                    new_ranges.append((r_start + delta, r_end + delta))
                elif r_start < p < r_end:
                    new_ranges.append((r_start, r_end + delta))
                else:
                    new_ranges.append((r_start, r_end))
            self._tags[t_name] = new_ranges

        # Apply new tags
        for t in tags:
            self.add_tag(t, p, p + delta)

    def delete(self, start: int, end: int) -> None:
        s = max(0, min(len(self._text), start))
        e = max(0, min(len(self._text), end))
        if s >= e:
            return
        delta = e - s
        self._text = self._text[:s] + self._text[e:]

        # Shift marks
        for m_name, (m_pos, gravity) in list(self._marks.items()):
            if m_pos >= e:
                self._marks[m_name] = (m_pos - delta, gravity)
            elif m_pos >= s:
                self._marks[m_name] = (s, gravity)

        if self._cursor >= e:
            self._cursor -= delta
        elif self._cursor >= s:
            self._cursor = s

        # Adjust tags
        for t_name, ranges in list(self._tags.items()):
            new_ranges: List[Tuple[int, int]] = []
            for r_start, r_end in ranges:
                if r_end <= s:
                    new_ranges.append((r_start, r_end))
                elif r_start >= e:
                    new_ranges.append((r_start - delta, r_end - delta))
                else:
                    # Overlapping deleted region
                    ns = min(r_start, s)
                    ne = max(s, r_end - delta)
                    if ne > ns:
                        new_ranges.append((ns, ne))
            self._tags[t_name] = new_ranges

    def add_tag(self, tag: str, start: int, end: int) -> None:
        s = max(0, min(len(self._text), start))
        e = max(0, min(len(self._text), end))
        if s >= e:
            return
        ranges = self._tags.setdefault(tag, [])
        ranges.append((s, e))
        # Merge overlapping
        ranges.sort(key=lambda r: (r[0], r[1]))
        merged: List[Tuple[int, int]] = []
        for r in ranges:
            if not merged:
                merged.append(r)
            else:
                last_s, last_e = merged[-1]
                if r[0] <= last_e:
                    merged[-1] = (last_s, max(last_e, r[1]))
                else:
                    merged.append(r)
        self._tags[tag] = merged

    def get_tag_ranges(self, tag: str) -> List[Tuple[int, int]]:
        return list(self._tags.get(tag, []))

    def clear_tag(self, tag: str) -> None:
        self._tags.pop(tag, None)


@dataclass
class _DictationEntry:
    t_sec: float
    kind: str  # "piece" or "embed"
    mark_name: str
    end_mark_name: str


class DraftModel:
    """Manages business logic and editing operations for the draft window."""

    def __init__(
        self,
        buffer: Optional[TextBuffer] = None,
        *,
        images_dir: Path,
        cache_dir: Path,
        clock=time.monotonic,
        autosave: bool = True,
    ) -> None:
        self.buffer: TextBuffer = buffer if buffer is not None else MemoryBuffer()
        self._images_dir = Path(images_dir)
        self._cache_dir = Path(cache_dir)
        self._clock = clock
        self._autosave = autosave

        self._is_recording: bool = False
        self._recording_t0: float = 0.0
        self._timeline: List[_DictationEntry] = []
        self._raw_pieces: List[DraftPiece] = []
        self._mark_seq: int = 0
        self._image_seq: int = 0
        self.fallback_count: int = 0
        self._undo_stack: List[Tuple[str, List[Tuple[int, int]]]] = []
        self._last_restored_ranges: List[Tuple[int, int]] = []

    # ------------------------------------------------------------- dictation
    def recording_started(self) -> None:
        """Called when dictation recording begins."""
        self._is_recording = True
        self._recording_t0 = self._clock()
        self._timeline.clear()
        cursor_pos = self.buffer.get_cursor()
        self.buffer.set_mark("dictation_point", cursor_pos, gravity="right")

    def recording_stopped(self) -> None:
        """Called when dictation recording stops."""
        self._is_recording = False

    def add_piece(self, piece: DraftPiece) -> None:
        """Insert a cleaned dictation chunk into the draft."""
        if not piece.text.strip():
            return

        if piece.fallback:
            self.fallback_count += 1

        self._raw_pieces.append(piece)

        # Determine target position for insertion
        target_pos: Optional[int] = None
        target_entry_idx: Optional[int] = None

        if self.buffer.get_mark("dictation_point") is not None:
            # Check timeline for any item spoken/embedded later than this piece
            for idx, item in enumerate(self._timeline):
                if item.t_sec > piece.start_sec:
                    m_pos = self.buffer.get_mark(item.mark_name)
                    if m_pos is not None:
                        target_pos = m_pos
                        target_entry_idx = idx
                        break

            if target_pos is None:
                target_pos = self.buffer.get_mark("dictation_point")

        if target_pos is None:
            target_pos = self.buffer.get_cursor()

        # Rule 2 & Rule 4: Piece spacing rules
        c_before = self.buffer.get_text(target_pos - 1, target_pos) if target_pos > 0 else ""
        c_after = (
            self.buffer.get_text(target_pos, target_pos + 1)
            if target_pos < len(self.buffer.get_text())
            else ""
        )

        embed_ranges = self.buffer.get_tag_ranges("embed")
        is_right_of_embed = any(end == target_pos for _, end in embed_ranges)
        is_left_of_embed = any(start == target_pos for start, _ in embed_ranges)

        p_text = piece.text
        if c_before and not p_text.startswith(" "):
            if is_right_of_embed and not c_before.isspace():
                p_text = " " + p_text
            elif is_latin_or_digit(c_before) and is_latin_or_digit(p_text[0]):
                p_text = " " + p_text

        if c_after and not p_text.endswith(" "):
            if is_left_of_embed and not c_after.isspace():
                p_text = p_text + " "
            elif is_latin_or_digit(p_text[-1]) and is_latin_or_digit(c_after):
                p_text = p_text + " "

        self._mark_seq += 1
        m_start = f"piece_{self._mark_seq}_start"
        m_end = f"piece_{self._mark_seq}_end"

        self.buffer.insert(target_pos, p_text)
        self.buffer.set_mark(m_start, target_pos, gravity="right")
        self.buffer.set_mark(m_end, target_pos + len(p_text), gravity="right")

        entry = _DictationEntry(
            t_sec=piece.start_sec,
            kind="piece",
            mark_name=m_start,
            end_mark_name=m_end,
        )

        if target_entry_idx is not None:
            self._timeline.insert(target_entry_idx, entry)
        else:
            self._timeline.append(entry)

        # If inserted at or after dictation point, advance dictation point
        dp = self.buffer.get_mark("dictation_point")
        if dp is not None and (target_pos + len(p_text)) >= dp:
            self.buffer.set_mark("dictation_point", target_pos + len(p_text), gravity="right")

        if self._autosave:
            self.save()

    # ------------------------------------------------------------- embedding
    def paste_clipboard(self, backend: Any) -> str:
        """Paste clipboard content: files > bitmap > text. Returns inserted kind."""
        grab_files_fn = getattr(backend, "grab_files", lambda: [])
        grab_png_fn = getattr(backend, "grab_png", lambda: [])
        grab_text_fn = getattr(backend, "grab_text", lambda: "")

        # 1. File list
        files = grab_files_fn()
        if files:
            formatted = [format_path(str(Path(f).absolute())) for f in files if str(f).strip()]
            if formatted:
                self._insert_embedded_content("\n".join(formatted), is_path=True)
                return "file"

        # 2. Bitmap image
        png_list = grab_png_fn()
        valid_pngs = [b for b in png_list if b]
        if valid_pngs:
            self._images_dir.mkdir(parents=True, exist_ok=True)
            self._image_seq += 1
            ts = time.strftime("%Y%m%d-%H%M%S")
            img_path = self._images_dir / f"draft-{ts}-{self._image_seq}.png"
            img_path.write_bytes(valid_pngs[0])
            formatted_path = format_path(str(img_path.absolute()))
            self._insert_embedded_content(formatted_path, is_path=True)
            return "image"

        # 3. Plain text
        raw_text = grab_text_fn()
        if raw_text:
            self._insert_embedded_content(raw_text, is_path=False)
            return "text"

        return ""

    def insert_files(self, paths: Sequence[str]) -> None:
        """Insert a list of file paths (e.g. from drag-and-drop) at cursor."""
        cleaned = [format_path(str(Path(p).absolute())) for p in paths if p and str(p).strip()]
        if not cleaned:
            return
        self._insert_embedded_content("\n".join(cleaned), is_path=True)

    def _insert_embedded_content(self, content: str, is_path: bool) -> None:
        cursor_pos = self.buffer.get_cursor()
        dp = self.buffer.get_mark("dictation_point")
        is_at_dp = self._is_recording and (dp is not None) and (dp == cursor_pos)

        # Rule 4: Surround paths with spaces if glued to non-whitespace
        lead_space = ""
        trail_space = ""
        if is_path:
            c_before = self.buffer.get_text(cursor_pos - 1, cursor_pos) if cursor_pos > 0 else ""
            c_after = (
                self.buffer.get_text(cursor_pos, cursor_pos + 1)
                if cursor_pos < len(self.buffer.get_text())
                else ""
            )
            if c_before and not c_before.isspace():
                lead_space = " "
            if c_after and not c_after.isspace():
                trail_space = " "

        to_insert = lead_space + content + trail_space
        self.buffer.insert(cursor_pos, to_insert)

        tag_start = cursor_pos + len(lead_space)
        tag_end = tag_start + len(content)
        self.buffer.add_tag("embed", tag_start, tag_end)

        new_cursor = cursor_pos + len(to_insert)
        self.buffer.set_cursor(new_cursor)

        if is_at_dp:
            rel_t = self._clock() - self._recording_t0
            self._mark_seq += 1
            m_start = f"embed_{self._mark_seq}_start"
            m_end = f"embed_{self._mark_seq}_end"

            self.buffer.set_mark(m_start, cursor_pos, gravity="right")
            self.buffer.set_mark(m_end, new_cursor, gravity="right")
            self.buffer.set_mark("dictation_point", new_cursor, gravity="right")

            self._timeline.append(
                _DictationEntry(
                    t_sec=rel_t,
                    kind="embed",
                    mark_name=m_start,
                    end_mark_name=m_end,
                )
            )

        if self._autosave:
            self.save()

    # ------------------------------------------------------------- whole-draft refine
    def protect(self) -> Tuple[str, Dict[str, str]]:
        """Replace embedded ranges with [文字N] markers; return protected text and mapping."""
        ranges = sorted(self.buffer.get_tag_ranges("embed"), key=lambda r: (r[0], r[1]))
        full_text = self.buffer.get_text()
        if not ranges:
            return full_text, {}

        pieces: List[str] = []
        mapping: Dict[str, str] = {}
        last = 0
        for idx, (start, end) in enumerate(ranges, start=1):
            if start < last:
                continue
            pieces.append(full_text[last:start])
            marker = f"[文字{idx}]"
            embed_text = full_text[start:end]
            mapping[marker] = embed_text
            pieces.append(marker)
            last = end
        pieces.append(full_text[last:])
        return "".join(pieces), mapping

    def restore(self, refined: str, mapping: Dict[str, str]) -> Optional[str]:
        """Restore markers in refined text. Return None if any marker is missing or duplicated."""
        if not mapping:
            if re.search(r"\[文字\d+\]", refined):
                return None
            return refined

        # Check marker counts
        for marker in mapping:
            if refined.count(marker) != 1:
                return None

        # Check for hallucinated/unexpected markers
        found_markers = re.findall(r"\[文字\d+\]", refined)
        if len(found_markers) != len(mapping):
            return None

        # Replace markers and record restored embed ranges
        pattern = re.compile("|".join(re.escape(k) for k in sorted(mapping.keys(), key=len, reverse=True)))
        pieces: List[str] = []
        last = 0
        restored_ranges: List[Tuple[int, int]] = []
        cur_len = 0

        for m in pattern.finditer(refined):
            pieces.append(refined[last:m.start()])
            cur_len += m.start() - last
            rep = mapping[m.group(0)]
            restored_ranges.append((cur_len, cur_len + len(rep)))
            pieces.append(rep)
            cur_len += len(rep)
            last = m.end()

        pieces.append(refined[last:])
        restored = "".join(pieces)
        self._last_restored_ranges = restored_ranges
        return restored

    def apply_refined(self, text: str) -> None:
        """Replace draft with refined text; save undo state."""
        current_text = self.buffer.get_text()
        current_embeds = self.buffer.get_tag_ranges("embed")
        self._undo_stack.append((current_text, current_embeds))

        self.buffer.set_text(text)
        self.buffer.clear_tag("embed")

        if self._last_restored_ranges:
            for s, e in self._last_restored_ranges:
                if e <= len(text):
                    self.buffer.add_tag("embed", s, e)
            self._last_restored_ranges = []

        if self._autosave:
            self.save()

    def undo_refine(self) -> bool:
        """Revert draft to state before the last apply_refined."""
        if not self._undo_stack:
            return False

        prev_text, prev_embeds = self._undo_stack.pop()
        self.buffer.set_text(prev_text)
        self.buffer.clear_tag("embed")
        for s, e in prev_embeds:
            if e <= len(prev_text):
                self.buffer.add_tag("embed", s, e)

        if self._autosave:
            self.save()
        return True

    # ------------------------------------------------------------- lifecycle
    def raw_transcript(self) -> str:
        """Return all raw pieces of this draft, in time order."""
        sorted_pieces = sorted(self._raw_pieces, key=lambda p: (p.job_id, p.start_sec, p.seq))
        res: List[str] = []
        for p in sorted_pieces:
            raw = p.raw_text
            if not raw:
                continue
            if res and is_latin_or_digit(res[-1][-1]) and is_latin_or_digit(raw[0]):
                res.append(" ")
            res.append(raw)
        return "".join(res)

    def finish(self) -> Tuple[str, str]:
        """Return (final_text, raw_text), clear buffer, and delete autosave file."""
        final_text = self.buffer.get_text()
        raw_text = self.raw_transcript()

        self.buffer.clear()
        self.buffer.clear_tag("embed")
        self._raw_pieces.clear()
        self._timeline.clear()
        self._undo_stack.clear()
        self.fallback_count = 0
        self._is_recording = False
        self.buffer.delete_mark("dictation_point")

        save_file = self._cache_dir / "draft.json"
        try:
            if save_file.exists():
                save_file.unlink()
        except Exception as exc:
            logger.warning("Failed to delete draft autosave: %s", exc)

        return final_text, raw_text

    def save(self) -> None:
        """Write current draft state to <cache_dir>/draft.json."""
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            data = {
                "text": self.buffer.get_text(),
                "embed_ranges": self.buffer.get_tag_ranges("embed"),
                "raw_pieces": [
                    {
                        "job_id": p.job_id,
                        "seq": p.seq,
                        "start_sec": p.start_sec,
                        "end_sec": p.end_sec,
                        "raw_text": p.raw_text,
                        "text": p.text,
                        "fallback": p.fallback,
                    }
                    for p in self._raw_pieces
                ],
                "fallback_count": self.fallback_count,
            }
            tmp_file = self._cache_dir / f"draft.tmp.{os.getpid()}"
            tmp_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            target_file = self._cache_dir / "draft.json"
            tmp_file.replace(target_file)
        except Exception as exc:
            logger.warning("Failed to save draft autosave: %s", exc)

    def load(self) -> None:
        """Load draft state from <cache_dir>/draft.json if it exists."""
        save_file = self._cache_dir / "draft.json"
        if not save_file.exists():
            return
        try:
            data = json.loads(save_file.read_text(encoding="utf-8"))
            text = data.get("text", "")
            self.buffer.set_text(text)
            self.buffer.clear_tag("embed")
            for s, e in data.get("embed_ranges", []):
                if e <= len(text):
                    self.buffer.add_tag("embed", s, e)
            self._raw_pieces = [
                DraftPiece(
                    job_id=p["job_id"],
                    seq=p["seq"],
                    start_sec=p["start_sec"],
                    end_sec=p["end_sec"],
                    raw_text=p["raw_text"],
                    text=p["text"],
                    fallback=p.get("fallback", False),
                )
                for p in data.get("raw_pieces", [])
            ]
            self.fallback_count = data.get("fallback_count", 0)
        except Exception as exc:
            logger.warning("Failed to load draft autosave: %s", exc)
