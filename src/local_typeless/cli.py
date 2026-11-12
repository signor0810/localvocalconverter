"""Command line interface: python -m local_typeless.cli <command>."""
from __future__ import annotations

import argparse
import dataclasses
import os
import sys
from pathlib import Path
from typing import List, Optional

from local_typeless.types import Mode, PipelineResult

MODES = [m.value for m in Mode]


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="local-typeless", description="Local voice dictation tool")
    p.add_argument("--config", type=Path, default=None)
    p.add_argument("--stt-profile", default=None)
    p.add_argument("--llm-profile", default=None)
    p.add_argument("--device", default=None)
    p.add_argument("--model", default=None)
    p.add_argument("--model-path", default=None)
    sub = p.add_subparsers(dest="command", required=True)

    f = sub.add_parser("file", help="transcribe + refine an audio file")
    f.add_argument("wav", type=Path)
    f.add_argument("--mode", choices=MODES, default="clean")

    r = sub.add_parser("record", help="record from microphone (Enter start/stop)")
    r.add_argument("--mode", choices=MODES, default="clean")

    t = sub.add_parser("retry", help="retry the last recording")
    t.add_argument("--mode", choices=MODES, default="clean")
    t.add_argument("--llm-only", action="store_true")

    sub.add_parser("transform", help="transform clipboard text")
    sub.add_parser("daemon", help="run the global hotkey daemon")
    d = sub.add_parser(
        "download-model",
        help="download the STT model weights (the only network use; no audio is sent)",
        description="Fetch faster-whisper weights into the local cache (or the profile's "
                    "model_path). Set HF_ENDPOINT to use an internal mirror.",
    )
    d.add_argument("--stt-profile", dest="dl_stt_profile", default=None)
    d.add_argument("--model", dest="dl_model", default=None)
    sub.add_parser("check", help="show STT config, CUDA devices and LLM connectivity")
    return p


def _overrides(args: argparse.Namespace) -> dict:
    o = {
        "stt_profile": getattr(args, "dl_stt_profile", None) or args.stt_profile,
        "llm_profile": args.llm_profile,
        "device": args.device,
        "model": getattr(args, "dl_model", None) or args.model,
    }
    o = {k: v for k, v in o.items() if v is not None}
    if args.model_path is not None:
        o["model_path"] = args.model_path
    return o


def _report(result: PipelineResult) -> None:
    if result.error_message == "no speech detected":
        print("(no speech detected)", file=sys.stderr)
        return
    if result.fallback_triggered:
        print(
            f"[fallback] LLM failed ({result.error_message}); "
            "output is the normalized raw transcript.",
            file=sys.stderr,
        )
    if result.error_message and "all outputs failed" in result.error_message:
        print(f"[output failed] {result.error_message}", file=sys.stderr)
    print(result.final_text)


def _record(pipeline, mode: Mode) -> Optional[PipelineResult]:
    from local_typeless.recorder.audio_recorder import AudioRecorder

    cfg = pipeline.config
    rec = AudioRecorder(cfg.recorder, Path(cfg.cache_dir))
    try:
        input("Press Enter to start recording (Ctrl+C to quit)... ")
        rec.start()
        print("Recording... Enter = stop, 'c' + Enter = cancel", file=sys.stderr)
        line = input()
    except (KeyboardInterrupt, EOFError):
        if rec.is_recording:
            rec.cancel()
        print("Cancelled.", file=sys.stderr)
        return None
    if line.strip().lower() == "c":
        rec.cancel()
        print("Cancelled.", file=sys.stderr)
        return None
    return pipeline.run_file(rec.stop(), mode)


def probe_cuda_usable(device_count: int, loader=None) -> "tuple[bool, str]":
    """Can CUDA actually be used? A device being present is not enough: the cuBLAS DLL
    must also load (the usual failure is 'Library cublas64_12.dll is not found')."""
    if device_count <= 0:
        return False, "no CUDA device"
    if sys.platform != "win32" and loader is None:
        return True, "device present (DLL probe only implemented on Windows)"
    try:
        if loader is None:
            import ctypes

            loader = ctypes.WinDLL
        loader("cublas64_12.dll")
        return True, "cublas64_12.dll loaded"
    except OSError as e:
        return False, f"cublas64_12.dll not loadable: {e}"


def _model_source(cfg) -> str:
    p = cfg.stt
    return str(p.model_path) if p.model_path and Path(p.model_path).is_dir() else p.model_size


def _download_model(cfg) -> int:
    from local_typeless.stt import faster_whisper_stt as stt

    p = cfg.stt
    if p.model_path and Path(p.model_path).is_dir():
        print(f"Model already present at model_path: {p.model_path}")
        return 0
    print(f"Downloading STT model '{p.model_size}' (network is used only for the weights; "
          "no audio is sent). Use HF_ENDPOINT to point at an internal mirror.")
    try:
        path = stt.download_model(p.model_size, output_dir=p.model_path)
    except Exception as e:
        print(f"Download failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    print(f"Model ready at: {path}")
    return 0


def _check(cfg) -> None:
    from local_typeless.stt import faster_whisper_stt as stt

    offline = os.environ.get("HF_HUB_OFFLINE") == "1"
    print(f"Offline mode: {'on' if offline else 'OFF (HF_HUB_OFFLINE != 1)'}")
    print(f"Active STT profile: {cfg.active_stt_profile}")
    for k, v in dataclasses.asdict(cfg.stt).items():
        print(f"  {k}: {v}")
    src = _model_source(cfg)
    if stt.is_model_available(src):
        print(f"STT model available locally: yes ({src})")
    else:
        print("STT model available locally: NO - run "
              f"python -m local_typeless.cli download-model --stt-profile {cfg.active_stt_profile}")
    count = 0
    try:
        from local_typeless.stt.faster_whisper_stt import _register_nvidia_dll_dirs

        _register_nvidia_dll_dirs()
        import ctranslate2

        count = ctranslate2.get_cuda_device_count()
        print(f"CUDA devices (ctranslate2): {count}")
    except Exception as e:
        print(f"CUDA devices: unavailable ({type(e).__name__}: {e})")
    ok, reason = probe_cuda_usable(count)
    print(f"CUDA usable: {'yes' if ok else 'no'} ({reason})")
    print(f"Active LLM profile: {cfg.active_llm_profile} "
          f"({cfg.llm.provider} @ {cfg.llm.base_url})")
    _check_llm(cfg)


def _llm_hint(exc: BaseException) -> str:
    """One actionable line for the most common first-connection failures."""
    text = str(exc)
    if "CERTIFICATE_VERIFY_FAILED" in text or "certificate verify failed" in text:
        return ("TLS: the server certificate is not trusted. Set ca_bundle in this profile to the "
                "root CA (PEM) of your organisation.")
    if "ca_bundle" in text:
        return "TLS: the ca_bundle path of this profile is missing or not a PEM file."
    if "HTTP 401" in text or "HTTP 403" in text:
        return "Auth: check the key variable, api_key_header and api_key_prefix of this profile."
    if "HTTP 404" in text or "HTTP 405" in text:
        return 'Path: check base_url, chat_path and models_path (set models_path: "" if there is no model list).'
    if "HTTP 407" in text or "proxy" in text.lower():
        return "Proxy: set NO_PROXY for this host, or use_env_proxy: false to connect directly."
    if "timed out" in text:
        return "Timeout: check network/VPN, or raise timeout_sec."
    if "connection failed" in text:
        return "Network: check base_url, DNS/VPN and proxy settings (use_env_proxy)."
    return ""


def _check_llm(cfg) -> None:
    """Connectivity report for the active LLM profile. Never prints the key itself."""
    import time

    from local_typeless.llm import create_llm_client
    from local_typeless.llm.openai_compat import resolve_api_key

    p = cfg.llm
    key_state = "set" if resolve_api_key(p.api_key_env) else "NOT set (sending the placeholder EMPTY)"
    print(f"  model: {p.model}")
    print(f"  key variable: {p.api_key_env or '(none)'} -> {key_state}")
    print(f"  key header: {p.api_key_header}: {p.api_key_prefix}<key>")
    tls = "DISABLED (verify_tls: false)" if not p.verify_tls else (p.ca_bundle or "certifi + OS trust store")
    print(f"  TLS trust: {tls}")
    proxies = [k for k in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY")
               if os.environ.get(k) or os.environ.get(k.lower())]
    if p.proxy:
        proxy_state = "explicit proxy set in this profile"
    elif not p.use_env_proxy:
        proxy_state = "ignored (use_env_proxy: false), direct connection"
    else:
        proxy_state = "system settings; variables set: " + (", ".join(proxies) or "none")
    print(f"  proxy: {proxy_state}")

    def fail(label: str, e: BaseException) -> None:
        print(f"{label} failed: {type(e).__name__}: {e}")
        hint = _llm_hint(e)
        if hint:
            print(f"  hint: {hint}")

    try:
        client = create_llm_client(p)
    except Exception as e:
        fail("LLM client", e)
        return
    if not p.models_path:
        print("LLM model list: skipped (models_path is empty)")
    else:
        try:
            models = client.list_models()
            print(f"LLM reachable. Models: {', '.join(models) if models else '(none)'}")
            if models and p.model not in models:
                print(f"  warning: configured model '{p.model}' is not in that list")
        except Exception as e:
            fail("LLM ping", e)
    try:
        t0 = time.monotonic()
        reply = client.refine("ping", "Reply with the single word: pong")
        print(f"LLM chat test: OK in {time.monotonic() - t0:.1f}s ({len(reply)} chars returned)")
    except Exception as e:
        fail("LLM chat test", e)


def main(argv: Optional[List[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    args = _build_parser().parse_args(argv)

    from local_typeless.config.loader import load_config

    try:
        cfg = load_config(args.config, _overrides(args))
    except Exception as e:
        print(f"Config error: {e}", file=sys.stderr)
        return 2

    if args.command == "check":
        _check(cfg)
        return 0
    if args.command == "download-model":
        return _download_model(cfg)

    from local_typeless.pipeline import Pipeline

    try:
        pipeline = Pipeline(cfg)
        if args.command == "file":
            _report(pipeline.run_file(args.wav, Mode(args.mode)))
        elif args.command == "record":
            result = _record(pipeline, Mode(args.mode))
            if result is not None:
                _report(result)
        elif args.command == "retry":
            _report(pipeline.retry_last(Mode(args.mode), llm_only=args.llm_only))
        elif args.command == "daemon":
            from local_typeless.hotkey.daemon import Daemon
            from local_typeless.recorder.audio_recorder import AudioRecorder

            recorder = AudioRecorder(cfg.recorder, Path(cfg.cache_dir))
            Daemon(
                cfg.hotkeys, recorder, pipeline,
                out=lambda m: print(m, flush=True),
                shutdown_wait_sec=cfg.llm.timeout_sec + 30,
                cache_dir=cfg.cache_dir,
            ).run()
        elif args.command == "transform":
            import pyperclip

            text = pyperclip.paste()
            result = pipeline.run_text(text, Mode.TRANSFORM)
            if result.final_text and not cfg.output.clipboard:
                pyperclip.copy(result.final_text)
            _report(result)
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    except Exception as e:
        print(f"Error: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
