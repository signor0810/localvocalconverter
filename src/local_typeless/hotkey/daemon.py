"""Global hotkey daemon.

Layers (bottom-up):
  * HotkeyStateMachine - pure IDLE -> RECORDING(mode) -> PROCESSING -> IDLE logic.
  * ChordTracker       - pure key press/release tracking -> state-machine events.
  * Daemon             - queues + worker thread (recorder / pipeline) + status writer.
  * PynputBackend      - thin pynput.keyboard.Listener glue (lazy import).

Threading rule: pynput low-level hook callbacks must return immediately, otherwise
Windows silently unhooks them (LowLevelHooksTimeout). Callbacks therefore only run
the (microsecond-fast) state machine and enqueue an Action; a single worker thread
performs recorder.start/stop and pipeline.run_file.

Cancel-key tradeoff: the cancel key (<esc>) is NOT suppressed, so it still reaches
the foreground application (e.g. closes a dialog) while we cancel the recording.
Suppressing would need a win32 suppressing hook and would break Esc system-wide
whenever the daemon is running, so we deliberately do not do it.
"""
from __future__ import annotations

import logging
import queue
import threading
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Dict, FrozenSet, List, Optional, Set

from local_typeless.types import HotkeyConfig, Mode

logger = logging.getLogger(__name__)


def _wav_info(path: Any) -> str:
    """'12.3s audio' for a wav file, or an error hint; used for diagnostics only."""
    try:
        import wave

        with wave.open(str(path), "rb") as w:
            return f"{w.getnframes() / float(w.getframerate()):.1f}s audio"
    except Exception as e:
        return f"duration unknown: {type(e).__name__}"


class State(str, Enum):
    IDLE = "IDLE"
    RECORDING = "RECORDING"
    PROCESSING = "PROCESSING"


@dataclass(frozen=True)
class Event:
    kind: str                       # "press" | "release" | "cancel"
    mode: Optional[Mode] = None


@dataclass(frozen=True)
class Action:
    kind: str                       # "start" | "stop" | "cancel"
    mode: Optional[Mode] = None


class HotkeyStateMachine:
    """Pure, thread-safe decision logic. No I/O besides the notify callback."""

    def __init__(
        self,
        push_to_talk: bool = False,
        notify: Optional[Callable[[str], None]] = None,
        on_change: Optional[Callable[[State, Optional[Mode], str], None]] = None,
        background: bool = False,
    ) -> None:
        self.push_to_talk = push_to_talk
        self._notify = notify or (lambda msg: None)
        self._on_change = on_change
        self.background = background
        self._lock = threading.Lock()
        self._state = State.IDLE
        self._mode: Optional[Mode] = None

    @property
    def state(self) -> State:
        return self._state

    @property
    def mode(self) -> Optional[Mode]:
        return self._mode

    def _set(self, state: State, note: str) -> None:
        self._state = state
        self._notify(f"[{state.value}] {note}")
        if self._on_change is not None:
            try:
                self._on_change(state, self._mode, note)
            except Exception:
                logger.exception("on_change callback failed")

    def handle(self, ev: Event) -> Optional[Action]:
        with self._lock:
            if ev.kind == "cancel":
                if self._state is State.RECORDING:
                    self._set(State.IDLE, "recording cancelled")
                    return Action("cancel")
                return None  # only acts while RECORDING

            if ev.kind == "paste_image":
                if self._state is State.RECORDING:
                    return Action("paste_image")
                return None

            if ev.kind == "press":
                if self._state is State.IDLE:
                    self._mode = ev.mode
                    self._set(State.RECORDING, f"recording ({ev.mode.value})")
                    return Action("start", ev.mode)
                if self._state is State.RECORDING:
                    if self.push_to_talk:
                        return None  # key auto-repeat while held
                    if self.background:
                        mode = self._mode
                        self._set(State.IDLE, "queued")
                        return Action("stop", mode)
                    self._set(State.PROCESSING, f"processing ({self._mode.value})")
                    return Action("stop", self._mode)
                self._notify("[PROCESSING] hotkey ignored: still processing")
                return None

            if ev.kind == "release":
                if (
                    self.push_to_talk
                    and self._state is State.RECORDING
                    and ev.mode == self._mode
                ):
                    if self.background:
                        mode = self._mode
                        self._set(State.IDLE, "queued")
                        return Action("stop", mode)
                    self._set(State.PROCESSING, f"processing ({self._mode.value})")
                    return Action("stop", self._mode)
                return None
        return None

    def force_stop(self) -> Optional[Action]:
        """Shutdown while RECORDING: stop and process (never discard dictation)."""
        with self._lock:
            if self._state is State.RECORDING:
                if self.background:
                    mode = self._mode
                    self._set(State.IDLE, "queued")
                    return Action("stop", mode)
                self._set(State.PROCESSING, f"processing ({self._mode.value})")
                return Action("stop", self._mode)
        return None

    def begin_retry(self, mode: Mode) -> Optional[Action]:
        """IDLE -> PROCESSING for a retry of the last recording."""
        with self._lock:
            if self._state is not State.IDLE:
                self._notify(f"[{self._state.value}] retry ignored: busy")
                return None
            if self.background:
                self._notify(f"[{self._state.value}] retry queued")
                if self._on_change is not None:
                    try:
                        self._on_change(self._state, self._mode, "retry queued")
                    except Exception:
                        logger.exception("on_change callback failed")
                return Action("retry", mode)
            self._mode = mode
            self._set(State.PROCESSING, f"processing retry ({mode.value})")
            return Action("retry", mode)

    def notice(self, note: str) -> None:
        """Surface a message without changing state."""
        with self._lock:
            self._notify(f"[{self._state.value}] {note}")
            if self._on_change is not None:
                try:
                    self._on_change(self._state, self._mode, note)
                except Exception:
                    logger.exception("on_change callback failed")

    def finish(self, note: str = "ready") -> None:
        """Worker finished processing (or failed): back to IDLE."""
        with self._lock:
            self._set(State.IDLE, note)
            self._mode = None

    def reset(self, note: str) -> None:
        self.finish(note)


# --------------------------------------------------------------- key tracking

_MODIFIER_NAMES = {
    "alt", "alt_l", "alt_r", "alt_gr", "ctrl", "ctrl_l", "ctrl_r",
    "shift", "shift_l", "shift_r", "cmd", "cmd_l", "cmd_r",
}


def _is_modifier(key: Any) -> bool:
    return getattr(key, "name", None) in _MODIFIER_NAMES


def canonical_key(key: Any) -> Any:
    """Normalise a pynput key: lower-case chars, alt_l -> alt, vk A-Z/0-9 -> char
    (Windows can deliver char=None for letters while Alt is held)."""
    from pynput.keyboard import Key, KeyCode

    if isinstance(key, KeyCode) and key.char is None and key.vk is not None:
        # HotKey.parse("<esc>") yields KeyCode(vk=27) while listeners deliver Key.esc.
        for member in Key:
            if getattr(member.value, "vk", None) == key.vk:
                key = member
                break
    if isinstance(key, Key):
        aliases = {
            Key.alt_l: Key.alt, Key.alt_r: Key.alt, Key.alt_gr: Key.alt,
            Key.ctrl_l: Key.ctrl, Key.ctrl_r: Key.ctrl,
            Key.shift_l: Key.shift, Key.shift_r: Key.shift,
            Key.cmd_l: Key.cmd, Key.cmd_r: Key.cmd,
        }
        return aliases.get(key, key)
    if isinstance(key, KeyCode):
        if key.char is not None:
            return KeyCode.from_char(key.char.lower())
        vk = getattr(key, "vk", None)
        if vk is not None and (0x30 <= vk <= 0x39 or 0x41 <= vk <= 0x5A):
            return KeyCode.from_char(chr(vk).lower())
    return key


class ChordTracker:
    """Tracks pressed keys and turns chord edges into state-machine Events.

    chords: {"clean": [keys], "slide": [keys], "cancel": [keys]} (already parsed).
    A chord fires "press" once when all its keys are down (auto-repeat safe) and
    "release" when its non-modifier key is released.
    """

    def __init__(
        self,
        chords: Dict[str, List[Any]],
        canonical: Callable[[Any], Any] = canonical_key,
    ) -> None:
        self._canon = canonical
        self._chords: Dict[str, FrozenSet[Any]] = {
            n: frozenset(canonical(k) for k in keys) for n, keys in chords.items()
        }
        self._triggers = {
            n: {k for k in ks if not _is_modifier(k)} or set(ks)
            for n, ks in self._chords.items()
        }
        self._pressed: Set[Any] = set()
        self._active: Set[str] = set()

    @staticmethod
    def _to_event(name: str, kind: str) -> Event:
        if name == "cancel":
            return Event("cancel")
        if name == "paste_image":
            return Event("paste_image")
        return Event(kind, Mode(name))

    def on_press(self, key: Any) -> List[Event]:
        self._pressed.add(self._canon(key))
        out = []
        for name, chord in self._chords.items():
            if name not in self._active and chord <= self._pressed:
                self._active.add(name)
                out.append(self._to_event(name, "press"))
        return out

    def on_release(self, key: Any) -> List[Event]:
        k = self._canon(key)
        self._pressed.discard(k)
        out = []
        for name in list(self._active):
            if k in self._triggers[name]:
                self._active.discard(name)
                if name not in ("cancel", "paste_image"):
                    out.append(self._to_event(name, "release"))
        return out


# ------------------------------------------------------------------- daemon

class Daemon:
    """Owns the queue + worker thread. `recorder`/`pipeline` are injected."""

    _STOP = object()

    def __init__(
        self,
        hotkeys: HotkeyConfig,
        recorder: Any,
        pipeline: Any,
        *,
        out: Callable[[str], None] = print,
        backend_factory: Optional[Callable[["Daemon"], Any]] = None,
        shutdown_wait_sec: float = 60.0,
        cache_dir: str = "records/cache",
        on_state: Optional[Callable[[State, Optional[Mode], str], None]] = None,
        on_result: Optional[Callable[[Any], None]] = None,
        on_segment: Optional[Callable[[str], None]] = None,
        image_watcher: Optional[Any] = None,
        sessions: Optional[Any] = None,
        on_jobs: Optional[Callable[[int, str], None]] = None,
        on_paste: Optional[Callable[[int], None]] = None,
    ) -> None:
        """on_state(state, mode, note) fires on every transition (hook or worker thread);
        on_result(PipelineResult) fires on the worker thread right after run_file, before
        the machine returns to IDLE. Both must be quick/thread-safe (a GUI must only
        enqueue and let its own thread apply them)."""
        self.image_watcher = image_watcher if sessions is None else None
        self.sessions = sessions
        self.on_jobs = on_jobs
        self.on_paste = on_paste
        self._newest_job_id: Optional[int] = None
        self.on_result = on_result
        self.on_segment = on_segment
        self._job_lock = threading.Lock()
        self._job_seq = 0
        self._current_job: Optional[int] = None
        self._abandoned: set = set()
        self._zombie_job: Optional[int] = None
        self.hotkeys_ok = False
        self.shutdown_wait_sec = shutdown_wait_sec
        self.cache_dir = cache_dir
        self.hotkeys = hotkeys
        self.recorder = recorder
        self.pipeline = pipeline
        self._out = out
        self._backend_factory = backend_factory or (lambda d: PynputBackend(d))
        self.machine = HotkeyStateMachine(
            push_to_talk=(hotkeys.mode == "push_to_talk"),
            notify=self._status,
            on_change=on_state,
            background=(sessions is not None),
        )
        self._actions: "queue.Queue[Any]" = queue.Queue()
        self._status_q: "queue.Queue[Any]" = queue.Queue()
        self._worker: Optional[threading.Thread] = None
        self._status_thread: Optional[threading.Thread] = None
        self._backend: Any = None
        self._stopping = threading.Event()
        if self.sessions is not None:
            self.bind_sessions()

    def bind_sessions(self) -> None:
        if self.sessions is None:
            return

        def _on_seg(job: Any) -> None:
            if self._newest_job_id is None or job.id == self._newest_job_id:
                if self.on_segment is not None:
                    try:
                        self.on_segment(job.text)
                    except Exception:
                        logger.exception("on_segment failed")

        def _on_res(job: Any, result: Any) -> None:
            if self.on_result is not None:
                try:
                    self.on_result(result)
                except Exception:
                    logger.exception("on_result failed")

        def _on_j(job: Any, pending: int) -> None:
            with self._job_lock:
                if self._newest_job_id is None or job.id > self._newest_job_id:
                    self._newest_job_id = job.id

            if getattr(job, "status", None) == "cancelled":
                return  # pending count unchanged; keep the "已取消錄音" message
            if getattr(job, "status", None) == "error":
                note = f"error: {getattr(job, 'error', '')}"
            elif getattr(job, "status", None) == "abandoned":
                note = "abandoned"
            elif (
                getattr(job, "result", None)
                and getattr(job.result, "error_message", None) == "no speech detected"
            ):
                note = "no speech detected"
            else:
                note = ""

            if self.on_jobs is not None:
                try:
                    self.on_jobs(pending, note)
                except Exception:
                    logger.exception("on_jobs failed")

        self.sessions.on_segment = _on_seg
        self.sessions.on_result = _on_res
        self.sessions.on_job = _on_j

    # ---- called from hook callbacks: must be instant -------------------
    def _status(self, msg: str) -> None:
        logger.info("state: %s", msg)
        self._status_q.put(msg)

    def _refuse_if_zombie(self) -> bool:
        if self.sessions is not None:
            return False
        if self._zombie_job is not None and self.machine.state is State.IDLE:
            self.machine.notice("refused: previous job still running in background")
            return True
        return False

    def submit(self, ev: Event) -> None:
        if ev.kind == "press" and self._refuse_if_zombie():
            return
        action = self.machine.handle(ev)
        if action is not None:
            self._actions.put(action)

    def request_retry(self, mode: Mode) -> bool:
        """Re-run the last recording (UI button). False if busy/refused."""
        if self._refuse_if_zombie():
            return False
        action = self.machine.begin_retry(mode)
        if action is None:
            return False
        self._actions.put(action)
        return True

    # ---- abandon / job bookkeeping --------------------------------------
    def abandon(self) -> bool:
        """Give up on the job in PROCESSING: state returns to IDLE immediately; the worker
        keeps running it in the background and its result is ignored. New jobs are refused
        until it ends (a single worker cannot be preempted)."""
        if self.sessions is not None:
            return bool(self.sessions.abandon())
        with self._job_lock:
            job = self._current_job
            if job is None or self.machine.state is not State.PROCESSING:
                return False
            self._abandoned.add(job)
            self._zombie_job = job
            self._current_job = None
        logger.warning("job %d abandoned by user", job)
        self.machine.reset("abandoned")
        return True

    def _begin_job(self) -> int:
        with self._job_lock:
            self._job_seq += 1
            self._current_job = self._job_seq
            return self._job_seq

    def _claim(self, job: int) -> bool:
        """True if the job is still wanted; False if it was abandoned (and clears zombie)."""
        with self._job_lock:
            if job in self._abandoned:
                self._abandoned.discard(job)
                if self._zombie_job == job:
                    self._zombie_job = None
                return False
            if self._current_job == job:
                self._current_job = None
            return True

    def _segment(self, job: int, text: str) -> None:
        if job in self._abandoned or self.on_segment is None:
            return
        try:
            self.on_segment(text)
        except Exception:
            logger.exception("on_segment callback failed")

    # ---- worker --------------------------------------------------------
    def _status_loop(self) -> None:
        while True:
            msg = self._status_q.get()
            try:
                if msg is self._STOP:
                    return
                try:
                    self._out(msg)
                except Exception:
                    logger.exception("status output failed")
            finally:
                self._status_q.task_done()

    def _worker_loop(self) -> None:
        while True:
            action = self._actions.get()
            try:
                if action is self._STOP:
                    return
                self._execute(action)
            except Exception as e:  # never let the worker die
                logger.exception("worker error")
                self.machine.reset(f"error: {type(e).__name__}: {e}")
            finally:
                self._actions.task_done()

    def _watcher_start(self) -> None:
        if self.image_watcher is None:
            return
        try:
            self.image_watcher.start()
        except Exception as e:
            logger.warning("image_watcher.start failed: %s", e)

    def _watcher_cancel(self) -> None:
        if self.image_watcher is None:
            return
        try:
            self.image_watcher.cancel()
        except Exception as e:
            logger.warning("image_watcher.cancel failed: %s", e)

    def _watcher_stop(self) -> List[Any]:
        if self.image_watcher is None:
            return []
        try:
            return list(self.image_watcher.stop() or [])
        except Exception as e:
            logger.warning("image_watcher.stop failed: %s", e)
            return []

    def _execute(self, action: Action) -> None:
        if action.kind == "paste_image":
            added = 0
            try:
                if self.sessions is not None:
                    added = self.sessions.paste_image()
                elif self.image_watcher is not None:
                    added = self.image_watcher.capture_now()
            except Exception as e:
                logger.warning("paste_image failed: %s", e)
                added = 0
            if self.on_paste is not None:
                try:
                    self.on_paste(added)
                except Exception:
                    logger.exception("on_paste callback failed")
            return

        if self.sessions is not None:
            if action.kind == "start":
                try:
                    job = self.sessions.begin(action.mode)
                    with self._job_lock:
                        self._newest_job_id = getattr(job, "id", None)
                except Exception as e:
                    logger.error("sessions.begin failed: %s", e)
                    self.machine.reset(f"error: cannot start recording ({e})")
            elif action.kind == "cancel":
                self.sessions.cancel()
            elif action.kind == "stop":
                self.sessions.end()
            elif action.kind == "retry":
                job = self.sessions.retry(action.mode)
                if job is None:
                    self.machine.notice("error: no last recording")
                else:
                    with self._job_lock:
                        self._newest_job_id = getattr(job, "id", None)
            return

        if action.kind == "start":
            try:
                logger.info("recorder.start()")
                self.recorder.start()
            except Exception as e:
                logger.error("recorder.start failed: %s", e)
                self.machine.reset(f"error: cannot start recording ({e})")
                return
            self._watcher_start()
        elif action.kind == "cancel":
            self.recorder.cancel()
            self._watcher_cancel()
        elif action.kind == "stop":
            if not getattr(self.recorder, "is_recording", True):
                logger.warning("stop requested but recorder is not recording; skipping")
                self.machine.finish("nothing recorded")
                return

            def runner(cb):
                from pathlib import Path
                from local_typeless.pipeline import save_image_manifest

                logger.info("recorder.stop()")
                try:
                    path = self.recorder.stop()
                except Exception:
                    self._watcher_cancel()
                    raise
                logger.info("recorder stopped: %s (%s)", path, _wav_info(path))
                images = self._watcher_stop()
                save_image_manifest(Path(self.cache_dir), images)
                kw = {}
                if images:
                    kw["images"] = images
                return self._run_pipeline("run_file", (path, action.mode), cb, **kw)

            self._process_job(runner)
        elif action.kind == "retry":
            self._process_job(lambda cb: self._run_pipeline("retry_last", (action.mode,), cb))

    def _run_pipeline(
        self, name: str, args: tuple, cb: Callable[[str], None], **kwargs: Any
    ) -> Any:
        fn = getattr(self.pipeline, name)
        if self.on_segment is not None:
            kwargs["on_segment"] = cb
        return fn(*args, **kwargs)

    def _process_job(self, runner: Callable[[Callable[[str], None]], Any]) -> None:
        from local_typeless.pipeline import outputs_failed

        job = self._begin_job()
        try:
            result = runner(lambda text: self._segment(job, text))
        except Exception as e:
            # No dedicated error cue exists in the recorder API: log only.
            logger.exception("job %d failed", job)
            if self._claim(job):
                self.machine.finish(f"error: {type(e).__name__}: {e}")
            return
        if not self._claim(job):
            logger.warning("abandoned job %d finished; result ignored", job)
            return
        if self.on_result is not None:
            try:
                self.on_result(result)
            except Exception:
                logger.exception("on_result callback failed")
        if result.error_message == "no speech detected":
            note = "no speech detected"
        elif outputs_failed(result):
            # Nothing reached clipboard/Obsidian: show the whole text so it is not lost.
            note = (f"done - OUTPUT FAILED ({result.error_message}); "
                    f"full text follows:\n{result.final_text}")
        else:
            preview = result.final_text.replace("\n", " ")[:60]
            note = f"done{' (LLM fallback)' if result.fallback_triggered else ''}: {preview}"
        self.machine.finish(note)

    # ---- lifecycle -----------------------------------------------------
    def start_workers(self) -> None:
        self._status_thread = threading.Thread(target=self._status_loop, daemon=True)
        self._worker = threading.Thread(target=self._worker_loop, daemon=True)
        self._status_thread.start()
        self._worker.start()

    def wait_idle(self) -> None:
        """Block until queued actions and status lines are processed (tests)."""
        self._actions.join()
        self._status_q.join()

    def shutdown(self, timeout: Optional[float] = None) -> None:
        """Stop listening, then finish (not discard) any dictation in flight."""
        if self._stopping.is_set():
            return
        self._stopping.set()
        wait = self.shutdown_wait_sec if timeout is None else timeout
        if self._backend is not None:
            try:
                self._backend.stop()
            except Exception:
                logger.exception("listener stop failed")

        if self.sessions is not None:
            finished = self.sessions.shutdown(timeout=wait)
            if not finished:
                self._status(
                    f"[EXIT] unfinished dictation preserved in "
                    f"{self.cache_dir}/last_recording.wav: run retry"
                )
            self._actions.put(self._STOP)
            self._status_q.put(self._STOP)
            self._join(self._worker, 2.0)
            self._join(self._status_thread, 2.0)
            return

        action = self.machine.force_stop()   # Ctrl+C while RECORDING -> stop + process
        if action is not None:
            self._actions.put(action)
        self._actions.put(self._STOP)
        finished = self._join(self._worker, wait)
        if not finished:
            self._status(
                f"[EXIT] unfinished dictation preserved in "
                f"{self.cache_dir}/last_recording.wav: run retry"
            )
        self._status_q.put(self._STOP)
        self._join(self._status_thread, 5.0)

    @staticmethod
    def _join(thread: Optional[threading.Thread], timeout: float) -> bool:
        if thread is None:
            return True
        try:
            thread.join(timeout)
        except KeyboardInterrupt:  # second Ctrl+C: stop waiting
            return not thread.is_alive()
        return not thread.is_alive()

    def start(self, listen: bool = True) -> None:
        """Start worker threads and (optionally) the global hotkey listener; non-blocking."""
        self.start_workers()
        if self.sessions is not None:
            try:
                self.sessions.start()
            except Exception as e:
                logger.warning("sessions.start failed: %s", e)
        if not listen:
            return
        try:
            self._backend = self._backend_factory(self)
            self._backend.start()
            self.hotkeys_ok = True
        except Exception as e:
            logger.error("global hotkeys unavailable: %s: %s", type(e).__name__, e)
            self._backend = None
            self._status(f"[IDLE] global hotkeys unavailable: {type(e).__name__}: {e}")

    def run(self) -> None:
        """Start listener and block until Ctrl+C."""
        self.start()
        hk = self.hotkeys
        self._status(
            f"[IDLE] ready - {hk.clean}=clean {hk.slide}=slide "
            f"{getattr(hk, 'summary', '')}=summary "
            f"{hk.cancel}=cancel ({hk.mode}); Ctrl+C quits"
        )
        try:
            while not self._stopping.wait(0.5):
                pass
        except KeyboardInterrupt:
            pass
        finally:
            self._status("[EXIT] shutting down")
            self.shutdown()


class PynputBackend:
    """pynput.keyboard.Listener glue. Callbacks only feed ChordTracker -> submit()."""

    def __init__(self, daemon: Daemon) -> None:
        from pynput import keyboard

        hk = daemon.hotkeys
        chords = {
            "clean": keyboard.HotKey.parse(hk.clean),
            "slide": keyboard.HotKey.parse(hk.slide),
            "cancel": keyboard.HotKey.parse(hk.cancel),
        }
        if getattr(hk, "summary", None):
            chords["summary"] = keyboard.HotKey.parse(hk.summary)
        if getattr(hk, "paste_image", None):
            chords["paste_image"] = keyboard.HotKey.parse(hk.paste_image)
        self._tracker = ChordTracker(chords)
        self._daemon = daemon
        self._listener = keyboard.Listener(on_press=self._on_press, on_release=self._on_release)

    def _dispatch(self, events: List[Event]) -> None:
        for ev in events:
            self._daemon.submit(ev)

    # pynput >= 1.8 passes `injected` (LLKHF_INJECTED on Windows). Synthetic events - e.g.
    # the Ctrl+V / modifier releases ClipboardOutput simulates - are ignored so they can
    # never corrupt the tracker's pressed-key set. Backends that cannot tell always
    # report injected=False, which degrades to the old behaviour.
    def _on_press(self, key: Any, injected: bool = False) -> None:
        if injected:
            return
        try:
            self._dispatch(self._tracker.on_press(key))
        except Exception:
            logger.exception("hotkey press handler failed")

    def _on_release(self, key: Any, injected: bool = False) -> None:
        if injected:
            return
        try:
            self._dispatch(self._tracker.on_release(key))
        except Exception:
            logger.exception("hotkey release handler failed")

    def start(self) -> None:
        self._listener.start()

    def stop(self) -> None:
        self._listener.stop()
