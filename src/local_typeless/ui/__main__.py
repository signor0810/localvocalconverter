"""python -m local_typeless.ui  - launch the side panel."""
from __future__ import annotations

import argparse
import dataclasses
import logging
import sys
from pathlib import Path
from typing import List, Optional


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="local-typeless-ui")
    p.add_argument("--config", type=Path, default=None)
    p.add_argument("--stt-profile", default=None)
    p.add_argument("--llm-profile", default=None)
    p.add_argument("--device", default=None)
    p.add_argument("--model", default=None)
    p.add_argument("--model-path", default=None)
    p.add_argument("--auto-close", type=float, default=None,
                   help="close the window after N seconds (smoke testing)")
    return p


class _NoSTT:
    """Placeholder STT for teach evaluation (text-only; transcribe is never called)."""

    def transcribe(self, *a, **k):
        raise RuntimeError("STT is not available in teach evaluation")


def main(argv: Optional[List[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    args = _parser().parse_args(argv)

    from local_typeless import cli
    from local_typeless.config.loader import load_config
    from local_typeless.hotkey.daemon import Daemon
    from local_typeless.llm import create_llm_client
    from local_typeless.output.clipboard_out import ClipboardOutput
    from local_typeless.output.rich_paste import RichPaster
    from local_typeless.pipeline import Pipeline
    from local_typeless.recorder.audio_recorder import AudioRecorder
    from local_typeless.ui.app import PanelApp
    from local_typeless.ui.controller import PanelController, format_hotkey

    try:
        cfg = load_config(args.config, cli._overrides(args))
    except Exception as e:
        print(f"Config error: {e}", file=sys.stderr)
        return 2

    from local_typeless.hotkey.daemon import State
    from local_typeless.ui import diagnostics

    # pythonw has no stderr: log to <cache_dir>/ui.log and leave crash traces on disk.
    log_path = diagnostics.setup_logging(Path(cfg.cache_dir))
    fault_file = diagnostics.enable_faulthandler(Path(cfg.cache_dir))
    watchdog = diagnostics.ProcessingWatchdog(fault_file=fault_file)
    logging.getLogger("ui").info(
        "UI starting: stt=%s llm=%s cache=%s log=%s",
        cfg.active_stt_profile, cfg.active_llm_profile, cfg.cache_dir, log_path)

    from local_typeless.output.record_store import cleanup_cache_leftovers

    cleanup_cache_leftovers(Path(cfg.cache_dir))

    # The UI performs the insertion itself, so the pipeline must not also paste/copy.
    ui_cfg = dataclasses.replace(cfg, output=dataclasses.replace(cfg.output, clipboard=False))
    pipeline = Pipeline(cfg, outputs=Pipeline._build_outputs(ui_cfg))
    recorder = AudioRecorder(cfg.recorder, Path(cfg.cache_dir))
    use_streaming = bool(cfg.streaming.enabled and getattr(recorder, "supports_streaming", False))
    if use_streaming:
        recorder = AudioRecorder(cfg.recorder, Path(cfg.cache_dir), streaming=cfg.streaming)

    paster_cfg = dataclasses.replace(
        cfg.output, clipboard=True, auto_paste=True, obsidian_note_path=None
    )
    paster = RichPaster(paster_cfg)
    copier = ClipboardOutput(dataclasses.replace(
        cfg.output, clipboard=True, auto_paste=False, obsidian_note_path=None))

    hotkey_hints = {
        "clean": format_hotkey(cfg.hotkeys.clean),
        "summary": format_hotkey(cfg.hotkeys.summary),
        "slide": format_hotkey(cfg.hotkeys.slide),
        "cancel": format_hotkey(cfg.hotkeys.cancel),
        "paste_image": format_hotkey(cfg.hotkeys.paste_image),
    }

    daemon: Optional[Daemon] = None
    controller = PanelController(
        submit=lambda ev: daemon.submit(ev),
        paster=paster,
        copier=lambda text: copier.emit(text, None),
        stt_profile=cfg.active_stt_profile,
        llm_profile=cfg.active_llm_profile,
        device_getter=lambda: getattr(pipeline.stt, "used_device", ""),
        # fresh client per ping so a newly set API key is picked up
        llm_pinger=lambda: create_llm_client(cfg.llm).list_models(),
        llm_model=cfg.llm.model,
        api_key_env=cfg.llm.api_key_env,
        abandon=lambda: daemon.abandon(),
        retry=lambda mode: daemon.request_retry(mode),
        cache_dir=cfg.cache_dir,
        streaming=use_streaming,
        hotkey_hints=hotkey_hints,
    )

    # Draft window flow: each chunk is cleaned while recording and lands in an editable
    # draft; nothing is pasted automatically. It needs the streaming recorder.
    draft_bridge = None
    if cfg.draft.enabled and use_streaming:
        from local_typeless.ui.draft_bridge import DraftBridge

        draft_bridge = DraftBridge(
            pipeline,
            images_dir=Path(cfg.storage.images_dir),
            cache_dir=Path(cfg.cache_dir),
            autosave=cfg.draft.autosave,
        )
        draft_bridge.toggle_record = controller.record_clicked

    image_watcher = None
    if draft_bridge is None and cfg.images.enabled and sys.platform == "win32":
        from local_typeless.capture.clipboard_images import ClipboardImageWatcher

        image_watcher = ClipboardImageWatcher(
            Path(cfg.storage.images_dir),
            poll_sec=cfg.images.poll_ms / 1000.0,
            on_image=controller.on_image,
            keep_sessions=None,
            trigger=cfg.images.trigger,
        )
        controller.file_adder = image_watcher.add_files

    # The panel's record button is a toggle, whatever the hotkey mode in the config is.
    hotkeys = dataclasses.replace(cfg.hotkeys, mode="toggle")

    if use_streaming:
        from local_typeless.sessions import SessionManager

        manager = SessionManager(
            pipeline, recorder, watcher=image_watcher, cache_dir=cfg.cache_dir,
            draft_mode=draft_bridge is not None,
            on_piece=draft_bridge.on_piece if draft_bridge is not None else None,
        )

        def on_jobs(pending: int, note: str) -> None:
            controller.on_jobs(pending, note)
            if pending > 0:
                watchdog.arm()
            else:
                watchdog.disarm()

        daemon = Daemon(
            hotkeys, recorder, pipeline,
            out=lambda m: None,
            on_state=controller.on_state,
            on_result=controller.on_result,
            on_segment=controller.on_segment,
            shutdown_wait_sec=cfg.llm.timeout_sec + 30,
            cache_dir=cfg.cache_dir,
            sessions=manager,
            on_jobs=on_jobs,
            on_paste=controller.on_paste,
            on_paste_request=(draft_bridge.on_paste_request if draft_bridge is not None else None),
        )
    else:
        def on_state(state, mode, note):
            controller.on_state(state, mode, note)
            if state is State.PROCESSING:
                watchdog.arm()
            else:
                watchdog.disarm()

        daemon = Daemon(
            hotkeys, recorder, pipeline,
            out=lambda m: None,
            on_state=on_state,
            on_result=controller.on_result,
            on_segment=controller.on_segment,
            shutdown_wait_sec=cfg.llm.timeout_sec + 30,
            cache_dir=cfg.cache_dir,
            image_watcher=image_watcher,
            on_paste=controller.on_paste,
        )
    daemon.start(listen=True)
    controller.set_hotkeys_ok(daemon.hotkeys_ok)
    controller.ping_llm()

    def check_stt_model():
        # offline lookup only; tells the user right away if the weights still need a download
        from local_typeless.stt import faster_whisper_stt as stt

        p = cfg.stt
        src = str(p.model_path) if p.model_path and Path(p.model_path).is_dir() else p.model_size
        if not stt.is_model_available(src):
            controller.notify(stt._download_hint(cfg.active_stt_profile, src))

    import threading

    threading.Thread(target=check_stt_model, daemon=True, name="stt-model-check").start()

    def on_close():
        watchdog.disarm()
        daemon.shutdown(timeout=5.0)

    def teach_factory():
        from local_typeless.ui.teach_controller import TeachController

        def make_pipeline():   # fresh: re-reads prompt + glossary overlays; no outputs, no STT
            return Pipeline(cfg, stt=_NoSTT(), outputs=[])

        return TeachController(cfg, lambda: create_llm_client(cfg.llm), make_pipeline)

    def review_factory():
        from local_typeless.ui.review_controller import ReviewController

        return ReviewController(cfg, lambda: create_llm_client(cfg.llm))

    app = PanelApp(
        controller,
        on_close=on_close,
        teach_factory=teach_factory,
        review_factory=review_factory,
        draft_bridge=draft_bridge,
    )
    try:
        app.run(auto_close_sec=args.auto_close)
    except KeyboardInterrupt:
        on_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
