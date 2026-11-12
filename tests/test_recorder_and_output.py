"""Unit tests for WP2-A: AudioRecorder, ClipboardOutput, and ObsidianOutput."""
from __future__ import annotations

import sys
import wave
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from local_typeless.output import ClipboardOutput, ObsidianOutput, append_to_file
from local_typeless.output.clipboard_out import _is_clipboard_image_present
from local_typeless.recorder import AudioRecorder
from local_typeless.types import Mode, OutputAdapter, OutputConfig, Recorder, RecorderConfig


class FakeInputStream:
    """Mock sounddevice.InputStream for testing without sound hardware."""

    def __init__(self, samplerate, channels, dtype, callback):
        self.samplerate = samplerate
        self.channels = channels
        self.dtype = dtype
        self.callback = callback
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True
        # Simulate feeding 1 chunk of 16-bit PCM samples
        dummy_pcm = b"\x01\x00\x02\x00" * 800  # 800 samples = 50ms at 16kHz
        self.callback(dummy_pcm, 800, None, None)

    def stop(self):
        self.stopped = True

    def close(self):
        pass


def test_audio_recorder_protocol_and_lifecycle(tmp_path: Path):
    """Verify AudioRecorder implements Recorder and produces valid 16kHz mono WAV."""
    cfg = RecorderConfig(sample_rate=16000, channels=1, beep=False)
    cache_dir = tmp_path / ".cache"
    recorder = AudioRecorder(cfg, cache_dir)

    assert isinstance(recorder, Recorder)
    assert not recorder.is_recording

    fake_sd = MagicMock()
    fake_sd.InputStream = FakeInputStream

    with patch("local_typeless.recorder.audio_recorder._get_sounddevice", return_value=fake_sd):
        recorder.start()
        assert recorder.is_recording

        wav_path = recorder.stop()
        assert not recorder.is_recording
        assert wav_path == cache_dir / "last_recording.wav"
        assert wav_path.exists()

        # Validate WAV structure
        with wave.open(str(wav_path), "rb") as wf:
            assert wf.getnchannels() == 1
            assert wf.getsampwidth() == 2
            assert wf.getframerate() == 16000
            assert wf.getnframes() > 0


def test_audio_recorder_cancel_does_not_modify_existing(tmp_path: Path):
    """Verify cancel() discards buffer and preserves existing last_recording.wav."""
    cfg = RecorderConfig(sample_rate=16000, channels=1, beep=False)
    cache_dir = tmp_path / ".cache"
    cache_dir.mkdir(parents=True)
    target = cache_dir / "last_recording.wav"
    target.write_bytes(b"ORIGINAL_AUDIO_CONTENT")

    recorder = AudioRecorder(cfg, cache_dir)
    fake_sd = MagicMock()
    fake_sd.InputStream = FakeInputStream

    with patch("local_typeless.recorder.audio_recorder._get_sounddevice", return_value=fake_sd):
        recorder.start()
        assert recorder.is_recording

        recorder.cancel()
        assert not recorder.is_recording

        # Ensure original file was untouched
        assert target.exists()
        assert target.read_bytes() == b"ORIGINAL_AUDIO_CONTENT"


def test_audio_recorder_beep_no_crash(tmp_path: Path):
    """Verify winsound beep cues don't crash start/stop/cancel even if beep=True."""
    cfg = RecorderConfig(sample_rate=16000, channels=1, beep=True)
    recorder = AudioRecorder(cfg, tmp_path)
    fake_sd = MagicMock()
    fake_sd.InputStream = FakeInputStream

    with patch("local_typeless.recorder.audio_recorder._get_sounddevice", return_value=fake_sd):
        with patch("local_typeless.recorder.audio_recorder._play_beep") as mock_beep:
            recorder.start()
            assert mock_beep.called

            recorder.stop()
            assert mock_beep.call_count == 2


# --- R1 & R2 Regression Tests ---


def test_audio_recorder_start_failure_cleanup(tmp_path: Path):
    """(R1) Verify on stream startup exception, state is cleaned and error re-raised."""
    cfg = RecorderConfig(sample_rate=16000, channels=1, beep=True)
    recorder = AudioRecorder(cfg, tmp_path)

    fake_stream = MagicMock()
    fake_stream.start.side_effect = RuntimeError("Microphone in exclusive use")
    fake_sd = MagicMock()
    fake_sd.InputStream.return_value = fake_stream

    with patch("local_typeless.recorder.audio_recorder._get_sounddevice", return_value=fake_sd):
        with patch("local_typeless.recorder.audio_recorder._play_beep") as mock_beep:
            with pytest.raises(RuntimeError, match="exclusive use"):
                recorder.start()

            assert not recorder.is_recording
            assert recorder._stream is None
            assert len(recorder._frames) == 0
            # Beep should NOT be played if start failed
            mock_beep.assert_not_called()
            fake_stream.close.assert_called()


def test_audio_recorder_stop_not_recording_raises(tmp_path: Path):
    """(R2) stop() when not recording must raise RuntimeError and never return stale file."""
    cfg = RecorderConfig(sample_rate=16000, channels=1, beep=False)
    cache_dir = tmp_path / ".cache"
    cache_dir.mkdir(parents=True)
    stale_wav = cache_dir / "last_recording.wav"
    stale_wav.write_bytes(b"STALE_FILE")

    recorder = AudioRecorder(cfg, cache_dir)
    assert not recorder.is_recording

    with pytest.raises(RuntimeError, match="AudioRecorder was not recording"):
        recorder.stop()

    # Stale file remains untouched
    assert stale_wav.read_bytes() == b"STALE_FILE"


def test_audio_recorder_stop_no_frames_raises_and_preserves_target(tmp_path: Path):
    """(R2) If no frames captured, stop() raises RuntimeError and does not overwrite target."""
    cfg = RecorderConfig(sample_rate=16000, channels=1, beep=False)
    cache_dir = tmp_path / ".cache"
    cache_dir.mkdir(parents=True)
    target = cache_dir / "last_recording.wav"
    target.write_bytes(b"EXISTING_VALID_WAV")

    recorder = AudioRecorder(cfg, cache_dir)

    class EmptyInputStream:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass  # Feeds 0 audio callbacks

        def stop(self):
            pass

        def close(self):
            pass

    fake_sd = MagicMock()
    fake_sd.InputStream = EmptyInputStream

    with patch("local_typeless.recorder.audio_recorder._get_sounddevice", return_value=fake_sd):
        recorder.start()
        assert recorder.is_recording

        with pytest.raises(RuntimeError, match="No audio frames were captured"):
            recorder.stop()

        assert not recorder.is_recording
        assert target.read_bytes() == b"EXISTING_VALID_WAV"


def test_audio_recorder_stop_atomic_replace_retry(tmp_path: Path):
    """(R2) os.replace retries on Windows PermissionError before succeeding."""
    cfg = RecorderConfig(sample_rate=16000, channels=1, beep=False)
    recorder = AudioRecorder(cfg, tmp_path)
    fake_sd = MagicMock()
    fake_sd.InputStream = FakeInputStream

    replace_attempts = [0]
    real_replace = wave.open

    with patch("local_typeless.recorder.audio_recorder._get_sounddevice", return_value=fake_sd):
        recorder.start()

        def flaky_replace(src, dst):
            replace_attempts[0] += 1
            if replace_attempts[0] < 3:
                raise PermissionError("Access denied by AV")
            # Successful replace on 3rd attempt
            Path(dst).write_bytes(Path(src).read_bytes())
            Path(src).unlink()

        with patch("os.replace", side_effect=flaky_replace):
            res_path = recorder.stop()
            assert res_path.exists()
            assert replace_attempts[0] == 3


# --- Clipboard & Output Tests (including R3) ---


def test_clipboard_output_protocol_and_emit():
    """Verify ClipboardOutput copies text using pyperclip."""
    cfg = OutputConfig(clipboard=True, auto_paste=False, protect_image_clipboard=False)
    adapter = ClipboardOutput(cfg)
    assert isinstance(adapter, OutputAdapter)

    with patch("pyperclip.copy") as mock_copy:
        adapter.emit("Hello Typeless", Mode.CLEAN)
        mock_copy.assert_called_once_with("Hello Typeless")


def test_clipboard_output_retry_success():
    """(R3) pyperclip.copy retries on transient errors and succeeds."""
    cfg = OutputConfig(clipboard=True, auto_paste=False, protect_image_clipboard=False)
    adapter = ClipboardOutput(cfg)

    attempts = [0]

    def flaky_copy(text):
        attempts[0] += 1
        if attempts[0] < 3:
            raise OSError("Clipboard busy")

    with patch("pyperclip.copy", side_effect=flaky_copy):
        with patch("time.sleep") as mock_sleep:
            adapter.emit("Retry copy", Mode.CLEAN)
            assert attempts[0] == 3
            assert mock_sleep.call_count == 2


def test_clipboard_output_retry_re_raises_on_all_fails():
    """(R3) pyperclip.copy re-raises if all 4 attempts fail."""
    cfg = OutputConfig(clipboard=True, auto_paste=False, protect_image_clipboard=False)
    adapter = ClipboardOutput(cfg)

    with patch("pyperclip.copy", side_effect=OSError("Clipboard locked permanently")):
        with patch("time.sleep"):
            with pytest.raises(OSError, match="Clipboard locked permanently"):
                adapter.emit("Fail copy", Mode.CLEAN)


def test_clipboard_image_check_try_finally_close():
    """(R3) WinDLL user32 CloseClipboard is invoked in finally."""
    if sys.platform != "win32":
        pytest.skip("Windows only ctypes test")

    mock_user32 = MagicMock()
    mock_user32.OpenClipboard.return_value = 1
    mock_user32.IsClipboardFormatAvailable.side_effect = RuntimeError("Query error")

    with patch("ctypes.WinDLL", return_value=mock_user32):
        has_img = _is_clipboard_image_present()
        assert not has_img
        mock_user32.CloseClipboard.assert_called_once()


def test_clipboard_output_image_protection(caplog):
    """Verify image presence triggers warning but does NOT drop output."""
    cfg = OutputConfig(clipboard=True, auto_paste=False, protect_image_clipboard=True)
    adapter = ClipboardOutput(cfg)

    with patch("local_typeless.output.clipboard_out._is_clipboard_image_present", return_value=True):
        with patch("pyperclip.copy") as mock_copy:
            with caplog.at_level("WARNING"):
                adapter.emit("Transcription text", Mode.CLEAN)

            assert "Clipboard currently holds an image" in caplog.text
            mock_copy.assert_called_once_with("Transcription text")


def test_clipboard_output_auto_paste():
    """Verify auto_paste triggers _simulate_paste."""
    cfg = OutputConfig(clipboard=True, auto_paste=True, protect_image_clipboard=False)
    adapter = ClipboardOutput(cfg)

    with patch("pyperclip.copy"):
        with patch("local_typeless.output.clipboard_out._simulate_paste") as mock_paste:
            adapter.emit("Paste this", Mode.CLEAN)
            mock_paste.assert_called_once()


def test_obsidian_output_append(tmp_path: Path):
    """Verify ObsidianOutput and append_to_file create file and append \\n\\n---\\n text."""
    note = tmp_path / "vault" / "slides.md"
    adapter = ObsidianOutput(note)
    assert isinstance(adapter, OutputAdapter)

    # First slide
    adapter.emit("## Slide 1\n- Point 1", Mode.SLIDE)
    assert note.exists()
    content1 = note.read_text(encoding="utf-8")
    assert content1 == "\n\n---\n## Slide 1\n- Point 1"

    # Second slide
    adapter.emit("## Slide 2\n- Point 2", Mode.SLIDE)
    content2 = note.read_text(encoding="utf-8")
    assert content2 == "\n\n---\n## Slide 1\n- Point 1\n\n---\n## Slide 2\n- Point 2"


def test_clipboard_output_disabled():
    """Verify ClipboardOutput does not copy when clipboard=False."""
    cfg = OutputConfig(clipboard=False, auto_paste=False, protect_image_clipboard=False)
    adapter = ClipboardOutput(cfg)

    with patch("pyperclip.copy") as mock_copy:
        adapter.emit("Do not copy this", Mode.CLEAN)
        mock_copy.assert_not_called()


# ------------------------------------------------------------ streaming recorder tests
def test_audio_recorder_streaming(tmp_path: Path):
    cfg = RecorderConfig(sample_rate=16000, channels=1, beep=False)
    recorder = AudioRecorder(cfg, tmp_path)
    assert recorder.supports_streaming is True

    fake_sd = MagicMock()
    fake_sd.InputStream = FakeInputStream

    chunks = []
    with patch("local_typeless.recorder.audio_recorder._get_sounddevice", return_value=fake_sd):
        recorder.start(on_chunk=chunks.append)
        wav_path = recorder.stop()
        assert wav_path.exists()
        assert len(chunks) == 1
        assert chunks[0].index == 0
        assert len(chunks[0].pcm) == 3200


def test_audio_recorder_streaming_cancel(tmp_path: Path):
    cfg = RecorderConfig(sample_rate=16000, channels=1, beep=False)
    recorder = AudioRecorder(cfg, tmp_path)

    fake_sd = MagicMock()
    fake_sd.InputStream = FakeInputStream

    chunks = []
    with patch("local_typeless.recorder.audio_recorder._get_sounddevice", return_value=fake_sd):
        recorder.start(on_chunk=chunks.append)
        recorder.cancel()
        assert chunks == []


def test_audio_recorder_streaming_on_chunk_error_does_not_break_stop(tmp_path: Path):
    cfg = RecorderConfig(sample_rate=16000, channels=1, beep=False)
    recorder = AudioRecorder(cfg, tmp_path)

    fake_sd = MagicMock()
    fake_sd.InputStream = FakeInputStream

    def exploding_cb(chunk):
        raise RuntimeError("cb exploded")

    with patch("local_typeless.recorder.audio_recorder._get_sounddevice", return_value=fake_sd):
        recorder.start(on_chunk=exploding_cb)
        wav_path = recorder.stop()
        assert wav_path.exists()


def test_audio_recorder_start_without_on_chunk(tmp_path: Path):
    cfg = RecorderConfig(sample_rate=16000, channels=1, beep=False)
    recorder = AudioRecorder(cfg, tmp_path)

    fake_sd = MagicMock()
    fake_sd.InputStream = FakeInputStream

    with patch("local_typeless.recorder.audio_recorder._get_sounddevice", return_value=fake_sd):
        recorder.start()
        wav_path = recorder.stop()
        assert wav_path.exists()


def test_audio_recorder_stereo_does_not_support_streaming(tmp_path: Path):
    cfg = RecorderConfig(sample_rate=16000, channels=2, beep=False)
    recorder = AudioRecorder(cfg, tmp_path)
    assert recorder.supports_streaming is False

