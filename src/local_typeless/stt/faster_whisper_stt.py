"""faster-whisper backed STT engine with lazy loading and CUDA->CPU fallback."""
from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable, List, Optional

from local_typeless.stt.base import STTEngine
from local_typeless.types import Segment, STTProfileConfig, TranscriptResult, Word

logger = logging.getLogger(__name__)


def _enforce_offline() -> None:
    """Runtime must never contact third parties: no HF Hub requests, no telemetry.

    setdefault, so `download-model` (which sets HF_HUB_OFFLINE=0 first) can opt out."""
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")


_enforce_offline()   # before faster_whisper / huggingface_hub are ever imported

_CUDA_DEVICES = ("cuda", "auto")
# Decode settings measured on a 79 s music clip (pathological) and the M0 speech set:
#   default fallback temps: 161 s;  these settings: 20.7 s, same text on normal speech.
TEMPERATURES = (0.0,)          # no temperature fallback (it retries each 30 s window)
REPETITION_PENALTY = 1.1
NO_REPEAT_NGRAM_SIZE = 6       # breaks "啊 啊 啊..." loops; 6 avoids blocking repeated terms


class ModelNotAvailableError(Exception):
    """STT weights are not on disk. Deliberately NOT a RuntimeError/OSError/ValueError so
    the CUDA->CPU fallback (which catches those) never mistakes it for a device error."""


def _download_hint(profile_name: str, model: str) -> str:
    target = f"--stt-profile {profile_name}" if profile_name else f"--model {model}"
    return (
        "STT 模型尚未下載：請執行 "
        f"python -m local_typeless.cli download-model {target}"
        "（只在初次安裝時連線下載模型權重，不會傳送任何音訊）"
    )


def _resolve_model(source: str) -> str:
    """Local path of the model weights; never touches the network. Raises if absent."""
    _enforce_offline()
    if Path(source).is_dir():
        return str(source)
    from faster_whisper.utils import download_model

    return download_model(source, local_files_only=True)


def is_model_available(source: str) -> bool:
    try:
        _resolve_model(source)
        return True
    except Exception:
        return False


def download_model(source: str, output_dir: Optional[str] = None) -> str:
    """Explicitly fetch weights (the only network use; no audio is ever sent).

    Honour HF_ENDPOINT to point at an internal mirror. Offline mode is restored after."""
    previous = os.environ.get("HF_HUB_OFFLINE")
    os.environ["HF_HUB_OFFLINE"] = "0"
    constants = None
    try:
        import huggingface_hub.constants as constants  # type: ignore

        constants.HF_HUB_OFFLINE = False
    except Exception:
        constants = None
    try:
        _register_nvidia_dll_dirs()
        from faster_whisper.utils import download_model as _dl

        return _dl(source, output_dir=output_dir, local_files_only=False)
    finally:
        if previous is None:
            os.environ["HF_HUB_OFFLINE"] = "1"
        else:
            os.environ["HF_HUB_OFFLINE"] = previous
        if constants is not None:
            constants.HF_HUB_OFFLINE = os.environ["HF_HUB_OFFLINE"] == "1"


_DLL_HANDLES: list = []
_DLL_REGISTERED = False


def _register_nvidia_dll_dirs() -> List[str]:
    """Windows: make pip-installed NVIDIA wheels (cublas/cudnn/cuda_runtime) findable.

    Adds <sys.prefix>/Lib/site-packages/nvidia/{cublas,cudnn,cuda_runtime}/bin to the DLL
    search path (os.add_dll_directory, used by ctypes/extension imports) and to PATH
    (native code inside ctranslate2 loads cuBLAS with plain LoadLibrary, which only
    honours PATH). No-op off Windows or when the directories do not exist.
    Returns the directories registered.
    """
    global _DLL_REGISTERED
    if sys.platform != "win32":
        return []
    base = Path(sys.prefix) / "Lib" / "site-packages" / "nvidia"
    found = [str(base / n / "bin") for n in ("cublas", "cudnn", "cuda_runtime")
             if (base / n / "bin").is_dir()]
    if _DLL_REGISTERED:
        return found
    _DLL_REGISTERED = True
    for d in found:
        try:
            _DLL_HANDLES.append(os.add_dll_directory(d))
        except OSError as e:
            logger.debug("add_dll_directory(%s) failed: %s", d, e)
    if found:
        os.environ["PATH"] = os.pathsep.join(found) + os.pathsep + os.environ.get("PATH", "")
    return found


def _get_whisper_model_class() -> Any:
    """Import lazily so `check` stays fast and tests can patch this."""
    _enforce_offline()
    _register_nvidia_dll_dirs()
    from faster_whisper import WhisperModel

    return WhisperModel


class FasterWhisperSTT(STTEngine):
    def __init__(self, profile: STTProfileConfig, profile_name: str = "") -> None:
        self._profile = profile
        self._profile_name = profile_name
        self._device = profile.device
        self._compute_type = profile.compute_type
        self._fallback_active = False
        self._model: Any = None

    # ------------------------------------------------------------ helpers
    @property
    def _model_source(self) -> str:
        p = self._profile
        if p.model_path and Path(p.model_path).is_dir():
            return str(p.model_path)
        return p.model_size

    @property
    def used_device(self) -> str:
        return "cpu (fallback)" if self._fallback_active else self._device

    def _ensure_model(self) -> Any:
        if self._model is None:
            try:
                local_path = _resolve_model(self._model_source)
            except Exception as e:
                logger.error("STT model %r not available locally: %s: %s",
                             self._model_source, type(e).__name__, e)
                raise ModelNotAvailableError(
                    _download_hint(self._profile_name, self._model_source)) from e
            cls = _get_whisper_model_class()
            self._model = cls(
                local_path,
                device=self._device,
                compute_type=self._compute_type,
                cpu_threads=self._profile.cpu_threads,
                local_files_only=True,
            )
        return self._model

    def _fall_back_to_cpu(self, err: BaseException) -> None:
        logger.warning(
            "CUDA STT failed (%s: %s); falling back to cpu/int8",
            type(err).__name__,
            err,
        )
        self._device = "cpu"
        self._compute_type = "int8"
        self._fallback_active = True
        self._model = None

    def _run(
        self,
        audio_path: Path,
        initial_prompt: Optional[str],
        on_segment: Optional[Callable[[str], None]] = None,
        word_timestamps: bool = False,
    ) -> tuple[str, float, List[Segment]]:
        model = self._ensure_model()
        p = self._profile
        kwargs: dict = dict(
            beam_size=p.beam_size,
            language=p.language,
            vad_filter=p.vad_filter,
            initial_prompt=initial_prompt,
            temperature=list(TEMPERATURES),
            repetition_penalty=REPETITION_PENALTY,
            no_repeat_ngram_size=NO_REPEAT_NGRAM_SIZE,
        )
        if word_timestamps:
            kwargs["word_timestamps"] = True
        if p.vad_filter:
            kwargs["vad_parameters"] = dict(min_silence_duration_ms=500)
        segments, info = model.transcribe(str(audio_path), **kwargs)
        # Segments are a lazy generator: consume here so DLL/CUDA errors surface.
        parts: List[str] = []
        decoded_segments: List[Segment] = []
        for seg in segments:
            parts.append(seg.text)
            words_tuple = tuple(
                Word(
                    float(getattr(w, "start", 0.0)),
                    float(getattr(w, "end", 0.0)),
                    getattr(w, "word", getattr(w, "text", "")),
                )
                for w in (getattr(seg, "words", None) or [])
            )
            decoded_segments.append(
                Segment(
                    float(getattr(seg, "start", 0.0)),
                    float(getattr(seg, "end", 0.0)),
                    seg.text,
                    words_tuple,
                )
            )
            if on_segment is not None:
                try:
                    on_segment("".join(parts).strip())
                except Exception:  # progress reporting must never abort decoding
                    logger.debug("on_segment callback failed", exc_info=True)
        text = "".join(parts).strip()
        return text, float(getattr(info, "duration", 0.0) or 0.0), decoded_segments

    # ---------------------------------------------------------------- API
    def transcribe(
        self,
        audio_path: Path,
        initial_prompt: Optional[str] = None,
        on_segment: Optional[Callable[[str], None]] = None,
        word_timestamps: bool = False,
    ) -> TranscriptResult:
        start = time.perf_counter()
        try:
            text, duration, segments = self._run(
                audio_path, initial_prompt, on_segment, word_timestamps=word_timestamps
            )
        except ModelNotAvailableError:
            raise  # not a device problem: never fall back to CPU for it
        except (RuntimeError, OSError, ValueError) as err:
            if self._device not in _CUDA_DEVICES or self._fallback_active:
                raise
            self._fall_back_to_cpu(err)
            text, duration, segments = self._run(
                audio_path, initial_prompt, on_segment, word_timestamps=word_timestamps
            )
        return TranscriptResult(
            raw_text=text,
            duration_sec=duration,
            stt_elapsed_sec=time.perf_counter() - start,
            used_device=self.used_device,
            used_model=self._model_source,
            segments=segments,
        )
