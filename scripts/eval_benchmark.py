"""Evaluation and benchmark script (WP3-B, owned by flash-infra).

Evaluates STT profiles (and optionally full LLM pipeline) across a dataset of *.wav
files and sidecar *.json metadata, computing latency, Real-Time Factor (RTF),
and domain term recall. Outputs Markdown summary reports.
"""
from __future__ import annotations

import argparse
import json
import logging
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class EvalFileResult:
    """Evaluation metrics for a single audio file on a specific profile."""

    filename: str
    profile: str
    duration_sec: float
    stt_sec: float
    llm_sec: float
    total_sec: float
    recall: float
    hit_count: int
    total_terms: int
    used_device: str
    rtf: float = 0.0
    error: Optional[str] = None


@dataclass
class ProfileSummary:
    """Aggregated benchmark metrics for an execution profile."""

    profile: str
    file_count: int
    mean_stt_sec: float
    mean_total_sec: float
    mean_recall: float
    mean_rtf: float
    error_count: int = 0


def get_audio_duration(wav_path: Path) -> float:
    """Read audio duration in seconds from WAV header."""
    try:
        with wave.open(str(wav_path), "rb") as wf:
            frames = wf.getnframes()
            rate = wf.getframerate()
            return frames / float(rate) if rate > 0 else 0.0
    except Exception as exc:
        logger.debug("Failed to read audio duration for %s: %s", wav_path, exc)
        return 0.0


def compute_term_recall(
    normalized_text: str, expected_terms: List[str]
) -> Tuple[float, int, int]:
    """Compute case-insensitive substring recall for expected domain terms.

    Returns:
        (recall_rate, hit_count, total_count)
    """
    if not expected_terms:
        return 1.0, 0, 0

    lower_text = normalized_text.lower()
    hits = sum(1 for term in expected_terms if term.lower() in lower_text)
    total = len(expected_terms)
    recall = hits / total if total > 0 else 1.0
    return recall, hits, total


def render_markdown_report(
    results: List[EvalFileResult],
    summaries: List[ProfileSummary],
    with_llm: bool = False,
) -> str:
    """Format evaluation results and profile summaries into a Markdown report."""
    lines: List[str] = [
        "# Benchmark Evaluation Report",
        "",
        "## Summary by Profile",
        "",
    ]

    # Summary table header
    if with_llm:
        lines.extend([
            "| Profile | Files | Mean STT (s) | Mean Total (s) | Mean Recall | Mean RTF | Errors |",
            "|---|---|---|---|---|---|---|",
        ])
        for s in summaries:
            lines.append(
                f"| {s.profile} | {s.file_count} | {s.mean_stt_sec:.3f} | "
                f"{s.mean_total_sec:.3f} | {s.mean_recall * 100:.1f}% | {s.mean_rtf:.3f} | {s.error_count} |"
            )
    else:
        lines.extend([
            "| Profile | Files | Mean STT (s) | Mean Recall | Mean RTF | Errors |",
            "|---|---|---|---|---|---|",
        ])
        for s in summaries:
            lines.append(
                f"| {s.profile} | {s.file_count} | {s.mean_stt_sec:.3f} | "
                f"{s.mean_recall * 100:.1f}% | {s.mean_rtf:.3f} | {s.error_count} |"
            )

    lines.extend([
        "",
        "## Detailed Results per Audio File",
        "",
    ])

    if with_llm:
        lines.extend([
            "| File | Profile | Duration (s) | STT (s) | LLM (s) | Total (s) | Recall (Hits/Total) | Device | Status |",
            "|---|---|---|---|---|---|---|---|---|",
        ])
        for r in results:
            status = "OK" if not r.error else f"FAIL ({r.error})"
            lines.append(
                f"| {r.filename} | {r.profile} | {r.duration_sec:.2f} | {r.stt_sec:.3f} | "
                f"{r.llm_sec:.3f} | {r.total_sec:.3f} | {r.recall * 100:.1f}% ({r.hit_count}/{r.total_terms}) | "
                f"{r.used_device} | {status} |"
            )
    else:
        lines.extend([
            "| File | Profile | Duration (s) | STT (s) | RTF | Recall (Hits/Total) | Device | Status |",
            "|---|---|---|---|---|---|---|---|",
        ])
        for r in results:
            status = "OK" if not r.error else f"FAIL ({r.error})"
            lines.append(
                f"| {r.filename} | {r.profile} | {r.duration_sec:.2f} | {r.stt_sec:.3f} | "
                f"{r.rtf:.3f} | {r.recall * 100:.1f}% ({r.hit_count}/{r.total_terms}) | "
                f"{r.used_device} | {status} |"
            )

    return "\n".join(lines)


def run_benchmark(
    data_dir: Path,
    stt_profiles: List[str],
    config_path: Optional[Path] = None,
    with_llm: bool = False,
) -> Tuple[List[EvalFileResult], List[ProfileSummary]]:
    """Execute benchmarks across audio files and profiles without halting on individual errors."""
    from local_typeless.config.loader import load_config
    from local_typeless.glossary.manager import GlossaryManager

    cfg = load_config(config_path)
    glossary = GlossaryManager.from_yaml(Path(cfg.glossary_path))
    wav_files = sorted(Path(data_dir).glob("*.wav"))

    results: List[EvalFileResult] = []

    for wav_file in wav_files:
        duration = get_audio_duration(wav_file)
        sidecar_json = wav_file.with_suffix(".json")
        expected_terms: List[str] = []
        if sidecar_json.exists():
            try:
                meta = json.loads(sidecar_json.read_text(encoding="utf-8"))
                expected_terms = meta.get("expected_terms", [])
            except Exception as exc:
                logger.warning("Failed to parse sidecar %s: %s", sidecar_json, exc)

        for profile in stt_profiles:
            if with_llm:
                # Run full pipeline with LLM
                try:
                    from local_typeless.pipeline import Pipeline
                    from local_typeless.types import Mode

                    profile_cfg = load_config(
                        config_path, overrides={"stt_profile": profile}
                    )
                    pipeline = Pipeline(profile_cfg, glossary=glossary)
                    t0 = time.perf_counter()
                    res = pipeline.run_file(wav_file, mode=Mode.CLEAN)
                    total_elapsed = time.perf_counter() - t0

                    norm_text = res.final_text
                    recall, hits, total_terms = compute_term_recall(
                        norm_text, expected_terms
                    )
                    rtf = res.stt_elapsed_sec / duration if duration > 0 else 0.0

                    results.append(
                        EvalFileResult(
                            filename=wav_file.name,
                            profile=profile,
                            duration_sec=duration,
                            stt_sec=res.stt_elapsed_sec,
                            llm_sec=res.llm_elapsed_sec,
                            total_sec=total_elapsed,
                            recall=recall,
                            hit_count=hits,
                            total_terms=total_terms,
                            used_device="auto",
                            rtf=rtf,
                            error=res.error_message if res.fallback_triggered else None,
                        )
                    )
                except Exception as exc:
                    logger.error("Error evaluating %s on %s: %s", wav_file.name, profile, exc)
                    results.append(
                        EvalFileResult(
                            filename=wav_file.name,
                            profile=profile,
                            duration_sec=duration,
                            stt_sec=0.0,
                            llm_sec=0.0,
                            total_sec=0.0,
                            recall=0.0,
                            hit_count=0,
                            total_terms=len(expected_terms),
                            used_device="unknown",
                            rtf=0.0,
                            error=str(exc),
                        )
                    )
            else:
                # STT-only evaluation
                try:
                    from local_typeless.stt import create_stt_engine

                    if profile not in cfg.stt_profiles:
                        raise ValueError(f"Profile {profile} not found in config.")

                    stt_cfg = cfg.stt_profiles[profile]
                    engine = create_stt_engine(stt_cfg)
                    initial_prompt = glossary.build_whisper_initial_prompt()

                    t0 = time.perf_counter()
                    trans_res = engine.transcribe(
                        wav_file, initial_prompt=initial_prompt
                    )
                    total_sec = time.perf_counter() - t0

                    norm_text = glossary.normalize_terms(trans_res.raw_text)
                    recall, hits, total_terms = compute_term_recall(
                        norm_text, expected_terms
                    )
                    rtf = trans_res.stt_elapsed_sec / duration if duration > 0 else 0.0

                    results.append(
                        EvalFileResult(
                            filename=wav_file.name,
                            profile=profile,
                            duration_sec=duration,
                            stt_sec=trans_res.stt_elapsed_sec,
                            llm_sec=0.0,
                            total_sec=total_sec,
                            recall=recall,
                            hit_count=hits,
                            total_terms=total_terms,
                            used_device=trans_res.used_device,
                            rtf=rtf,
                            error=None,
                        )
                    )
                except Exception as exc:
                    logger.error("Error transcribing %s on %s: %s", wav_file.name, profile, exc)
                    results.append(
                        EvalFileResult(
                            filename=wav_file.name,
                            profile=profile,
                            duration_sec=duration,
                            stt_sec=0.0,
                            llm_sec=0.0,
                            total_sec=0.0,
                            recall=0.0,
                            hit_count=0,
                            total_terms=len(expected_terms),
                            used_device="unknown",
                            rtf=0.0,
                            error=str(exc),
                        )
                    )

    # Compute summaries
    summaries: List[ProfileSummary] = []
    for profile in stt_profiles:
        profile_results = [r for r in results if r.profile == profile]
        file_count = len(profile_results)
        valid = [r for r in profile_results if not r.error]

        if valid:
            mean_stt = sum(r.stt_sec for r in valid) / len(valid)
            mean_total = sum(r.total_sec for r in valid) / len(valid)
            mean_recall = sum(r.recall for r in valid) / len(valid)
            mean_rtf = sum(r.rtf for r in valid) / len(valid)
        else:
            mean_stt = mean_total = mean_recall = mean_rtf = 0.0

        errors = file_count - len(valid)
        summaries.append(
            ProfileSummary(
                profile=profile,
                file_count=file_count,
                mean_stt_sec=mean_stt,
                mean_total_sec=mean_total,
                mean_recall=mean_recall,
                mean_rtf=mean_rtf,
                error_count=errors,
            )
        )

    return results, summaries


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate Local Typeless benchmark.")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path("data"),
        help="Directory containing *.wav and sidecar *.json files",
    )
    parser.add_argument(
        "--stt-profiles",
        type=str,
        default="cpu_fast,cpu_accurate",
        help="Comma-separated STT profiles to evaluate (e.g. cpu_fast,cpu_accurate,gpu_local)",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Optional path to config YAML",
    )
    parser.add_argument(
        "--with-llm",
        action="store_true",
        help="Also execute LLM refinement via Pipeline.run_file",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Path to write the output Markdown report",
    )
    args = parser.parse_args()

    profiles = [p.strip() for p in args.stt_profiles.split(",") if p.strip()]
    results, summaries = run_benchmark(
        data_dir=args.data_dir,
        stt_profiles=profiles,
        config_path=args.config,
        with_llm=args.with_llm,
    )

    report_md = render_markdown_report(results, summaries, with_llm=args.with_llm)
    print(report_md)

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(report_md, encoding="utf-8")
        logger.info("Report written to %s", args.out)


if __name__ == "__main__":
    main()
