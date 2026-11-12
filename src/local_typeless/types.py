"""Cross-module data contracts (WP0, owned by Orchestrator).

LOCKED: workers must not edit this file. Request changes from the Orchestrator.

Module API contracts (implementations must match these names/signatures):

  config.loader      (flash-infra)
      load_config(path: Optional[Path] = None, overrides: Optional[dict] = None) -> AppConfig
          - path None -> config/config.yaml if exists else config/config.example.yaml
          - overrides keys: "stt_profile", "llm_profile", "device", "model", "model_path"
            ("device"/"model"/"model_path" patch the *active* STT profile)
          - "${ENV}" style values are NOT expanded; secrets come only via api_key_env.
      load_prompt(mode: Mode, prompts_dir: Path, glossary_section: str = "") -> str
          - reads prompts/<mode>.md, substitutes the literal "{glossary}" placeholder.

  glossary.manager   (flash-infra)
      class GlossaryManager(GlossaryProvider): GlossaryManager.from_yaml(path: Path)

  stt                (sonnet-core)
      stt.base.STTEngine (ABC) ; stt.create_stt_engine(profile: STTProfileConfig) -> STTEngine

  llm                (sonnet-core)
      llm.base.LLMClient (ABC) ; llm.create_llm_client(profile: LLMProfileConfig) -> LLMClient
      refine() MUST raise LLMError (or subclass) on timeout / connection / HTTP / bad payload.

  recorder.audio_recorder (flash-infra)
      class AudioRecorder(Recorder): AudioRecorder(cfg: RecorderConfig, cache_dir: Path)

  output             (flash-infra)
      output.clipboard_out.ClipboardOutput(cfg: OutputConfig)          implements OutputAdapter
      output.obsidian_out.ObsidianOutput(note_path: Path)               implements OutputAdapter
      output.obsidian_out.append_to_file(note_path: Path, text: str) -> None

  pipeline           (sonnet-core)
      class Pipeline:
          __init__(config: AppConfig, *, stt: Optional[STTEngine] = None,
                   llm: Optional[LLMClient] = None, glossary: Optional[GlossaryProvider] = None,
                   outputs: Optional[List[OutputAdapter]] = None)
              (None -> build from config via factories; injection used by tests)
          run_file(audio_path: Path, mode: Mode) -> PipelineResult
          run_text(raw_text: str, mode: Mode) -> PipelineResult      # S3 transform / llm-only retry
          retry_last(mode: Mode, llm_only: bool = False) -> PipelineResult
              (reads <cache_dir>/last_recording.wav; llm_only reuses last raw_text from history)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable


class Mode(str, Enum):
    CLEAN = "clean"
    SLIDE = "slide"
    TRANSFORM = "transform"
    SUMMARY = "summary"                 # condensed summary of the dictation (UI default)


@dataclass
class STTProfileConfig:
    device: str = "cpu"                 # "cpu" | "cuda" | "auto"
    model_size: str = "small"           # "small" | "medium" | "large-v3-turbo" | "large-v3"
    model_path: Optional[str] = None    # local model dir takes priority (offline/corp)
    compute_type: str = "int8"          # "int8" | "int8_float16" | "float16"
    cpu_threads: int = 6
    beam_size: int = 2
    vad_filter: bool = True
    language: Optional[str] = None      # None = auto-detect, or "zh"


@dataclass
class LLMProfileConfig:
    provider: str = "openai_compatible"  # "openai_compatible" | "gateway" (internal gateway adapter)
    base_url: str = "http://localhost:8000/v1"
    api_key_env: str = "DGX_API_KEY"
    model: str = "Qwen/Qwen2.5-32B-Instruct"
    timeout_sec: float = 15.0
    temperature: float = 0.2
    max_tokens: int = 2048
    extra_headers: Dict[str, str] = field(default_factory=dict)
    extra_body: Dict[str, Any] = field(default_factory=dict)  # merged into the request JSON (e.g. chat_template_kwargs)
    # --- gateway compatibility (all optional; defaults = plain OpenAI-compatible server) ---
    api_key_header: str = "Authorization"   # header that carries the key, e.g. "api-key" / "X-API-Key"
    api_key_prefix: str = "Bearer "         # text put before the key; "" sends the bare key
    verify_tls: bool = True                 # False disables certificate checks (diagnosis only)
    ca_bundle: Optional[str] = None         # PEM file/dir with the corporate root CA; None = certifi + OS store
    use_env_proxy: bool = True              # honour proxy env vars / Windows proxy settings; False = connect directly
    proxy: Optional[str] = None             # explicit proxy URL for this profile, e.g. "http://proxy:8080"
    chat_path: str = "/chat/completions"    # appended to base_url for refine()
    models_path: str = "/models"            # appended to base_url for the health check; "" = gateway has none


@dataclass
class RecorderConfig:
    sample_rate: int = 16000
    channels: int = 1
    beep: bool = True                   # winsound.Beep cues; no-op on non-Windows


@dataclass
class OutputConfig:
    clipboard: bool = True
    auto_paste: bool = False            # simulate Ctrl+V after copying
    protect_image_clipboard: bool = True  # don't clobber an image on the clipboard silently
    obsidian_note_path: Optional[str] = None  # if set, also append to this .md
    paste_step_delay_ms: int = 300      # pause after each Ctrl+V of a text/image sequence


@dataclass
class ImagesConfig:
    enabled: bool = True                # capture clipboard images while recording
    poll_ms: int = 150                  # clipboard sequence-number poll interval ("auto" trigger)
    trigger: str = "paste"              # "paste": the image enters the text when the user presses the
                                        # paste-image hotkey/button; "auto": on every clipboard change


@dataclass
class StorageConfig:
    records_dir: str = "records"            # one Markdown file per finished dictation
    images_dir: str = "records/images"      # images captured while recording
    retention_days: int = 90                # older records and images are deleted (0 = keep forever)
    filename_format: str = "{date}-{time}_{mode}_{title}"   # record file name without ".md"


@dataclass
class StreamingConfig:
    enabled: bool = True                # transcribe chunks while recording + background queue
    min_chunk_sec: float = 12.0         # cut at the first silence after this much audio
    max_chunk_sec: float = 28.0         # forced cut (at the quietest recent frame)
    silence_sec: float = 0.5            # silence run that qualifies as a cut point


@dataclass
class RefineConfig:
    block_target_chars: int = 700       # clean mode: aim for blocks of about this size
    block_max_chars: int = 1000         # clean mode: never send a larger block to the LLM
    context_chars: int = 150            # tail of the previous raw block shown as context


@dataclass
class HotkeyConfig:
    clean: str = "<alt>+q"              # pynput GlobalHotKeys syntax (Alt+Space avoided: Windows system menu)
    slide: str = "<alt>+s"
    summary: str = "<alt>+w"
    cancel: str = "<esc>"               # only active while recording
    paste_image: str = "<alt>+v"        # only active while recording: insert the clipboard image here
    mode: str = "toggle"                # "toggle" | "push_to_talk"


@dataclass
class AppConfig:
    stt_profiles: Dict[str, STTProfileConfig] = field(default_factory=dict)
    active_stt_profile: str = "cpu_fast"
    llm_profiles: Dict[str, LLMProfileConfig] = field(default_factory=dict)
    active_llm_profile: str = "dev"
    glossary_path: str = "config/glossary.yaml"
    prompts_dir: str = "prompts"
    cache_dir: str = "records/cache"
    history_enabled: bool = True
    history_max: int = 20
    recorder: RecorderConfig = field(default_factory=RecorderConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    hotkeys: HotkeyConfig = field(default_factory=HotkeyConfig)
    images: ImagesConfig = field(default_factory=ImagesConfig)
    streaming: StreamingConfig = field(default_factory=StreamingConfig)
    refine: RefineConfig = field(default_factory=RefineConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)

    @property
    def stt(self) -> STTProfileConfig:
        return self.stt_profiles[self.active_stt_profile]

    @property
    def llm(self) -> LLMProfileConfig:
        return self.llm_profiles[self.active_llm_profile]


@dataclass(frozen=True)
class Word:
    start: float                        # seconds from the start of the audio
    end: float
    text: str                           # exactly as decoded (may carry a leading space)


@dataclass(frozen=True)
class Segment:
    start: float
    end: float
    text: str
    words: tuple = ()                   # Tuple[Word, ...]; empty unless word_timestamps


@dataclass(frozen=True)
class CapturedImage:
    """An image the user copied/snipped while recording. Never leaves the machine."""
    index: int                          # 1-based; rendered in text as the marker "[圖<index>]"
    t_sec: float                        # seconds from the start of the recording
    path: str                           # local PNG file


@dataclass(frozen=True)
class AudioChunk:
    index: int                          # 0-based within a recording
    start_sec: float                    # offset of the chunk's first sample in the recording
    pcm: bytes                          # 16-bit little-endian mono PCM
    sample_rate: int = 16000


@dataclass
class TranscriptResult:
    raw_text: str
    duration_sec: float
    stt_elapsed_sec: float
    used_device: str                    # actual device, e.g. "cuda" or "cpu (fallback)"
    used_model: str
    segments: List[Segment] = field(default_factory=list)


@dataclass
class PipelineResult:
    raw_text: str
    final_text: str
    mode: Mode
    stt_profile_name: str
    llm_profile_name: str
    stt_elapsed_sec: float
    llm_elapsed_sec: float
    fallback_triggered: bool = False    # True if LLM failed and normalized raw text was emitted
    error_message: Optional[str] = None
    images: List[CapturedImage] = field(default_factory=list)
    total_blocks: int = 1               # clean mode sends long text to the LLM in blocks
    fallback_blocks: int = 0            # blocks emitted as normalized raw text (0 < n < total = partial)


# ---------------------------------------------------------------- errors

class LLMError(Exception):
    """Any failure of a remote LLM call. Pipeline catches this to trigger fallback."""


class LLMTimeoutError(LLMError):
    pass


class LLMConnectionError(LLMError):
    pass


class LLMResponseError(LLMError):
    """HTTP >= 400 or unparseable payload."""


# ---------------------------------------------------------------- protocols

@runtime_checkable
class GlossaryProvider(Protocol):
    def build_whisper_initial_prompt(self) -> str: ...
    def build_llm_glossary_section(self) -> str: ...
    def normalize_terms(self, text: str) -> str: ...


@runtime_checkable
class Recorder(Protocol):
    def start(self) -> None: ...
    def stop(self) -> Path: ...          # writes <cache_dir>/last_recording.wav, returns its path
    def cancel(self) -> None: ...        # discards buffer, does NOT overwrite last_recording.wav
    @property
    def is_recording(self) -> bool: ...


@runtime_checkable
class OutputAdapter(Protocol):
    def emit(self, text: str, mode: Mode) -> None: ...
