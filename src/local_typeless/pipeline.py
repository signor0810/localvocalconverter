"""Core pipeline: audio -> STT -> LLM refine -> normalize -> outputs (+history)."""
from __future__ import annotations

import json
import logging
import os
import time
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

from local_typeless.llm.base import LLMClient
from local_typeless.stt.base import STTEngine
from local_typeless.textblocks import (
    append_missing_markers,
    canonicalize_markers,
    join_blocks,
    marker,
    markers_preserved,
    place_markers,
    split_blocks,
    strip_markers,
)
from local_typeless.types import (
    AppConfig,
    CapturedImage,
    GlossaryProvider,
    LLMConnectionError,
    LLMError,
    LLMResponseError,
    LLMTimeoutError,
    Mode,
    OutputAdapter,
    PipelineResult,
)

logger = logging.getLogger(__name__)

IMAGE_MANIFEST = "last_recording.images.json"
NO_SPEECH = "no speech detected"
OUTPUTS_FAILED = "all outputs failed"


def save_image_manifest(cache_dir: Path, images: Sequence[CapturedImage]) -> None:
    """Save captured image metadata to the session manifest file, or delete it if images is empty."""
    try:
        target = Path(cache_dir) / IMAGE_MANIFEST
        if not images:
            if target.exists():
                target.unlink(missing_ok=True)
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        data = [
            {"index": img.index, "t_sec": img.t_sec, "path": img.path}
            for img in images
        ]
        target.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        logger.warning("Could not save image manifest: %s: %s", type(e).__name__, e)


def load_image_manifest(cache_dir: Path) -> List[CapturedImage]:
    """Load captured image metadata from the session manifest file; skip missing files."""
    try:
        target = Path(cache_dir) / IMAGE_MANIFEST
        if not target.exists():
            return []
        text = target.read_text(encoding="utf-8", errors="replace")
        data = json.loads(text)
        if not isinstance(data, list):
            return []
        valid: List[CapturedImage] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            path_str = item.get("path")
            if not path_str or not Path(path_str).exists():
                continue
            valid.append(
                CapturedImage(
                    index=int(item["index"]),
                    t_sec=float(item["t_sec"]),
                    path=str(path_str),
                )
            )
        return valid
    except Exception as e:
        logger.warning("Could not load image manifest: %s: %s", type(e).__name__, e)
        return []


def is_degenerate(text: str) -> bool:
    """True if nothing meaningful remains after removing '[?]' markers, whitespace,
    punctuation and symbols (e.g. an LLM reply of '...' or '[?]')."""
    stripped = text.replace("[?]", "")
    return not any(unicodedata.category(ch)[0] not in "PSZC" for ch in stripped)


def outputs_failed(result: PipelineResult) -> bool:
    """True when every output adapter failed (caller must show the text itself)."""
    return OUTPUTS_FAILED in (result.error_message or "")


class NoHistoryError(RuntimeError):
    """retry_last(llm_only=True) called but no history exists."""


class Pipeline:
    def __init__(
        self,
        config: AppConfig,
        *,
        stt: Optional[STTEngine] = None,
        llm: Optional[LLMClient] = None,
        glossary: Optional[GlossaryProvider] = None,
        outputs: Optional[List[OutputAdapter]] = None,
        records: Optional[Any] = None,
    ) -> None:
        self.config = config
        self.cache_dir = Path(config.cache_dir)
        if stt is None:
            from local_typeless.stt import create_stt_engine

            stt = create_stt_engine(config.stt, config.active_stt_profile)
        if llm is None:
            from local_typeless.llm import create_llm_client

            llm = create_llm_client(config.llm)
        if glossary is None:
            from local_typeless.glossary.manager import GlossaryManager

            glossary = GlossaryManager.from_yaml(Path(config.glossary_path))
        if outputs is None:
            outputs = self._build_outputs(config)
        self.stt = stt
        self.llm = llm
        self.glossary = glossary
        self.outputs = outputs

        if records is False:
            self.records = None
        elif records is not None:
            self.records = records
        else:
            storage = getattr(config, "storage", None)
            if storage is not None:
                from local_typeless.output.record_store import RecordStore

                r_dir = Path(storage.records_dir)
                if not r_dir.is_absolute():
                    r_dir = self.cache_dir / r_dir
                i_dir = Path(storage.images_dir)
                if not i_dir.is_absolute():
                    i_dir = self.cache_dir / i_dir
                self.records = RecordStore(
                    records_dir=r_dir,
                    images_dir=i_dir,
                    retention_days=storage.retention_days,
                    filename_format=storage.filename_format,
                )
            else:
                self.records = None

    # ------------------------------------------------------------ builders
    @staticmethod
    def _build_outputs(config: AppConfig) -> List[OutputAdapter]:
        outs: List[OutputAdapter] = []
        if config.output.clipboard:
            from local_typeless.output.clipboard_out import ClipboardOutput

            outs.append(ClipboardOutput(config.output))
        if config.output.obsidian_note_path:
            from local_typeless.output.obsidian_out import ObsidianOutput

            outs.append(ObsidianOutput(Path(config.output.obsidian_note_path)))
        return outs

    # ------------------------------------------------------------- history
    @property
    def history_path(self) -> Path:
        return self.cache_dir / "history.jsonl"

    def _read_history(self) -> List[dict]:
        if not self.history_path.exists():
            return []
        rows = []
        text = self.history_path.read_text(encoding="utf-8", errors="replace")
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                logger.warning("Skipping corrupt history line")
                continue
            if isinstance(row, dict) and isinstance(row.get("raw_text"), str):
                rows.append(row)
        return rows

    def _append_history(self, result: PipelineResult) -> None:
        if not self.config.history_enabled:
            return
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            entry: dict = {
                "ts": datetime.now().isoformat(timespec="seconds"),
                "mode": result.mode.value,
                "raw_text": result.raw_text,
                "final_text": result.final_text,
                "fallback_triggered": result.fallback_triggered,
            }
            if result.images:
                entry["images"] = [
                    {"index": img.index, "t_sec": img.t_sec, "path": img.path}
                    for img in result.images
                ]
            with self.history_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            limit = max(1, self.config.history_max)
            rows = self._read_history()
            if len(rows) > limit:  # trim only when over the limit
                tmp = self.history_path.with_suffix(".jsonl.tmp")
                tmp.write_text(
                    "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows[-limit:]),
                    encoding="utf-8",
                )
                os.replace(tmp, self.history_path)
        except Exception as e:  # history must never break dictation
            logger.warning("Could not write history: %s: %s", type(e).__name__, e)

    # ---------------------------------------------------------------- core
    def _emit(self, text: str, mode: Mode) -> List[Tuple[str, Exception]]:
        """Emit to every adapter individually; return the adapters that failed."""
        failures: List[Tuple[str, Exception]] = []
        for out in self.outputs:
            try:
                out.emit(text, mode)
            except Exception as e:  # one adapter must not block the others
                logger.warning("Output %s failed: %s: %s", type(out).__name__, type(e).__name__, e)
                failures.append((type(out).__name__, e))
        return failures

    def _normalize_safe(self, text: str) -> str:
        try:
            return self.glossary.normalize_terms(text)
        except Exception as e:
            logger.warning("normalize_terms failed (%s: %s); emitting bare text", type(e).__name__, e)
            return text

    def _finish(self, result: PipelineResult, record_history: bool) -> PipelineResult:
        """Record history, emit to every output, and note when all outputs failed."""
        if record_history:
            self._append_history(result)  # before emit: text is safe even if output hangs
            if self.records is not None:
                try:
                    self.records.save(result)
                except Exception as e:
                    logger.warning("RecordStore.save failed: %s: %s", type(e).__name__, e)
                try:
                    self.records.cleanup()
                except Exception as e:
                    logger.warning("RecordStore.cleanup failed: %s: %s", type(e).__name__, e)
        failures = self._emit(result.final_text, result.mode)
        if self.outputs and len(failures) == len(self.outputs):
            detail = "; ".join(f"{n}: {type(e).__name__}: {e}" for n, e in failures)
            msg = f"{OUTPUTS_FAILED} ({detail})"
            prior = result.error_message
            result.error_message = f"{prior}; {msg}" if prior else msg
        return result

    def _process(
        self,
        raw_text: str,
        mode: Mode,
        stt_elapsed: float,
        record_history: bool = True,
        images: Optional[Sequence[CapturedImage]] = None,
    ) -> PipelineResult:
        cfg = self.config
        img_list = list(images or [])
        base = dict(
            mode=mode,
            stt_profile_name=cfg.active_stt_profile,
            llm_profile_name=cfg.active_llm_profile,
            stt_elapsed_sec=stt_elapsed,
            images=img_list,
        )
        if not raw_text.strip():
            return PipelineResult(
                raw_text=raw_text,
                final_text="",
                llm_elapsed_sec=0.0,
                error_message=NO_SPEECH,
                total_blocks=1,
                fallback_blocks=0,
                **base,
            )

        if not strip_markers(raw_text).strip():
            final_text = raw_text.strip()
            result = PipelineResult(
                raw_text=raw_text,
                final_text=final_text,
                llm_elapsed_sec=0.0,
                fallback_triggered=False,
                error_message=None,
                total_blocks=1,
                fallback_blocks=0,
                **base,
            )
            return self._finish(result, record_history)

        from local_typeless.config.loader import load_prompt

        t0 = time.perf_counter()
        fallback_triggered = False
        error_message: Optional[str] = None

        if mode == Mode.CLEAN:
            try:
                system_prompt = load_prompt(
                    mode, Path(cfg.prompts_dir), self.glossary.build_llm_glossary_section()
                )
            except OSError as e:
                err = LLMError(f"prompt load failed ({mode.value}): {e}")
                logger.warning(
                    "LLM stage failed (%s: %s); emitting normalized raw transcript",
                    type(err).__name__,
                    err,
                )
                blocks = split_blocks(
                    raw_text, cfg.refine.block_target_chars, cfg.refine.block_max_chars
                )
                total_blocks = max(1, len(blocks))
                final_text = self._normalize_safe(raw_text)
                llm_elapsed = time.perf_counter() - t0
                result = PipelineResult(
                    raw_text=raw_text,
                    final_text=final_text,
                    llm_elapsed_sec=llm_elapsed,
                    fallback_triggered=True,
                    error_message=str(err),
                    total_blocks=total_blocks,
                    fallback_blocks=total_blocks,
                    **base,
                )
                return self._finish(result, record_history)

            blocks = split_blocks(
                raw_text, cfg.refine.block_target_chars, cfg.refine.block_max_chars
            )
            total_blocks = len(blocks)
            fallback_blocks = 0
            parts: List[str] = []
            connection_failed = False
            first_error: Optional[str] = None

            logger.info("LLM start (%s, %d chars)", mode.value, len(raw_text))

            for i, block in enumerate(blocks):
                if connection_failed:
                    parts.append(self._normalize_safe(block))
                    fallback_blocks += 1
                    continue

                if i == 0:
                    current_sys_prompt = system_prompt
                else:
                    ctx_len = cfg.refine.context_chars
                    if ctx_len > 0:
                        prev_tail = blocks[i - 1][-ctx_len:]
                        current_sys_prompt = (
                            f"{system_prompt}\n\n"
                            "## 前文（僅供理解語境，絕對不要輸出、重複或改寫前文）\n"
                            f"{prev_tail}"
                        )
                    else:
                        current_sys_prompt = system_prompt

                try:
                    logger.info("LLM block %d/%d (%d chars)", i + 1, total_blocks, len(block))
                    refined = self.llm.refine(block, current_sys_prompt)
                    canonical = canonicalize_markers(refined)
                    out = self.glossary.normalize_terms(canonical)
                    if is_degenerate(out):
                        raise LLMResponseError("LLM reply is empty or degenerate")
                    if not markers_preserved(block, out):
                        raise LLMResponseError("image markers changed by LLM")
                    parts.append(out)
                except Exception as e:
                    logger.warning(
                        "LLM block %d/%d failed (%s: %s); using raw block",
                        i + 1,
                        total_blocks,
                        type(e).__name__,
                        e,
                    )
                    if isinstance(e, (LLMConnectionError, LLMTimeoutError)):
                        connection_failed = True
                    fallback_blocks += 1
                    msg = str(e) if isinstance(e, LLMError) else f"{type(e).__name__}: {e}"
                    if first_error is None:
                        first_error = (
                            msg if total_blocks == 1 else f"block {i + 1}/{total_blocks}: {msg}"
                        )
                    parts.append(self._normalize_safe(block))

            final_text = join_blocks(parts)
            fallback_triggered = fallback_blocks > 0
            error_message = first_error
            logger.info("LLM done in %.1fs (%d chars)", time.perf_counter() - t0, len(final_text))

        else:
            total_blocks = 1
            fallback_blocks = 0
            try:
                try:
                    system_prompt = load_prompt(
                        mode, Path(cfg.prompts_dir), self.glossary.build_llm_glossary_section()
                    )
                except OSError as e:
                    raise LLMError(f"prompt load failed ({mode.value}): {e}") from e
                logger.info("LLM start (%s, %d chars)", mode.value, len(raw_text))
                refined = self.llm.refine(raw_text, system_prompt)
                logger.info("LLM done in %.1fs (%d chars)", time.perf_counter() - t0, len(refined))
                canonical = canonicalize_markers(refined)
                appended = append_missing_markers(raw_text, canonical)
                final_text = self.glossary.normalize_terms(appended)
                if is_degenerate(final_text):
                    raise LLMResponseError("LLM reply is empty or degenerate")
            except Exception as e:
                logger.warning(
                    "LLM stage failed (%s: %s); emitting normalized raw transcript",
                    type(e).__name__,
                    e,
                )
                fallback_triggered = True
                fallback_blocks = 1
                error_message = str(e) if isinstance(e, LLMError) else f"{type(e).__name__}: {e}"
                final_text = self._normalize_safe(raw_text)

        llm_elapsed = time.perf_counter() - t0
        result = PipelineResult(
            raw_text=raw_text,
            final_text=final_text,
            llm_elapsed_sec=llm_elapsed,
            fallback_triggered=fallback_triggered,
            error_message=error_message,
            total_blocks=total_blocks,
            fallback_blocks=fallback_blocks,
            **base,
        )
        return self._finish(result, record_history)

    # ----------------------------------------------------------------- API
    def run_file(
        self,
        audio_path: Path,
        mode: Mode,
        on_segment: Optional[Callable[[str], None]] = None,
        images: Optional[Sequence[CapturedImage]] = None,
    ) -> PipelineResult:
        kwargs: dict = {"initial_prompt": self.glossary.build_whisper_initial_prompt()}
        if on_segment is not None:  # keep compatibility with engines lacking the kwarg
            kwargs["on_segment"] = on_segment
        if images:
            kwargs["word_timestamps"] = True
        logger.info("STT start: %s", audio_path)
        t = self.stt.transcribe(Path(audio_path), **kwargs)
        logger.info(
            "STT done in %.1fs (audio %.1fs, %d chars, device=%s)",
            t.stt_elapsed_sec,
            t.duration_sec,
            len(t.raw_text),
            t.used_device,
        )
        if not images:
            raw_text = t.raw_text
        elif getattr(t, "segments", None):
            raw_text = place_markers(t.segments, images)
        else:
            raw_text = t.raw_text + "".join(
                marker(img.index) for img in sorted(images, key=lambda x: x.index)
            )
        return self._process(raw_text, mode, t.stt_elapsed_sec, images=images)

    def run_text(
        self,
        raw_text: str,
        mode: Mode,
        record_history: bool = True,
        images: Optional[Sequence[CapturedImage]] = None,
    ) -> PipelineResult:
        return self._process(raw_text, mode, 0.0, record_history, images=images)

    def retry_last(
        self,
        mode: Mode,
        llm_only: bool = False,
        on_segment: Optional[Callable[[str], None]] = None,
    ) -> PipelineResult:
        if llm_only:
            rows = self._read_history()
            if not rows:
                raise NoHistoryError(
                    "No history available for --llm-only retry (history empty or disabled)."
                )
            last_row = rows[-1]
            images = None
            if "images" in last_row:
                images = [
                    CapturedImage(index=it["index"], t_sec=it["t_sec"], path=it["path"])
                    for it in last_row["images"]
                ]
            return self.run_text(last_row["raw_text"], mode, images=images)
        wav = self.cache_dir / "last_recording.wav"
        if not wav.exists():
            raise FileNotFoundError(f"No last recording found at {wav}")
        images = load_image_manifest(self.cache_dir) or None
        return self.run_file(wav, mode, on_segment, images=images)
