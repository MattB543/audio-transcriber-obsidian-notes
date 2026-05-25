"""Unit tests for :mod:`recorder.recorder`.

These tests do NOT touch a real microphone — :class:`sounddevice.InputStream`
is monkey-patched with a fake that calls the user-supplied callback with a
sine wave buffer.
"""

from __future__ import annotations

import json
import re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import soundfile as sf

# Make the notes-pipeline root importable for `config` + `recorder`.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from recorder.recorder import AudioRecorder, RecorderError  # noqa: E402
import recorder.recorder as recorder_module  # noqa: E402


# -------------------------------------------------------------- fakes / helpers


class _FakeInputStream:
    """Drop-in stand-in for :class:`sounddevice.InputStream` in tests.

    On :meth:`start` it spawns a short-lived thread that calls the user-supplied
    callback a few times with a synthetic sine-wave buffer, so the recorder
    writes real audio frames to the FLAC file.
    """

    # Class-level knobs tests can tweak. Default produces ~0.256s of audio.
    num_callbacks: int = 4
    frequency_hz: float = 440.0

    def __init__(
        self,
        *,
        samplerate: int,
        channels: int,
        dtype: str,
        blocksize: int,
        callback: Any,
    ) -> None:
        self.samplerate = samplerate
        self.channels = channels
        self.dtype = dtype
        self.blocksize = blocksize
        self.callback = callback
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    def start(self) -> None:
        def _run() -> None:
            # Precompute one block of a sine wave.
            t = np.arange(self.blocksize) / float(self.samplerate)
            wave = np.sin(2.0 * np.pi * self.frequency_hz * t)
            scaled = np.clip(wave * 0.5 * 32767.0, -32768, 32767).astype(np.int16)
            if self.channels == 1:
                block = scaled.reshape(-1, 1)
            else:  # pragma: no cover — we only run mono in tests
                block = np.tile(scaled[:, None], (1, self.channels))

            for _ in range(self.num_callbacks):
                if self._stop_event.is_set():
                    return
                # sounddevice shares the buffer across calls; mimic that so
                # the recorder's copy-on-enqueue behavior gets exercised.
                self.callback(block, self.blocksize, None, 0)
                # Tiny sleep to more realistically mimic real-time audio.
                time.sleep(0.005)

        self._thread = threading.Thread(target=_run, name="fake-input-stream", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)

    def close(self) -> None:
        self.stop()


def _fake_query_devices(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
    return {"name": "Fake Test Microphone"}


@pytest.fixture
def patched_sounddevice(monkeypatch: pytest.MonkeyPatch) -> type[_FakeInputStream]:
    """Patch sounddevice.InputStream + query_devices on the recorder module."""
    monkeypatch.setattr(recorder_module.sd, "InputStream", _FakeInputStream)
    monkeypatch.setattr(recorder_module.sd, "query_devices", _fake_query_devices)
    return _FakeInputStream


@pytest.fixture
def audio_dir(tmp_path: Path) -> Path:
    d = tmp_path / "audio"
    d.mkdir()
    return d


# --------------------------------------------------------------------- tests


def test_make_timestamp_format() -> None:
    """Timestamp must be `YYYY-MM-DD_HHMMSS`."""
    ts = AudioRecorder.make_timestamp(datetime(2026, 4, 24, 14, 32, 8))
    assert ts == "2026-04-24_143208"


def test_make_timestamp_matches_regex() -> None:
    """A live timestamp must match the documented pattern."""
    ts = AudioRecorder.make_timestamp()
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}_\d{6}", ts), ts


def test_start_stop_produces_valid_flac(
    patched_sounddevice: type[_FakeInputStream],
    audio_dir: Path,
) -> None:
    """A full start→stop cycle should leave a playable FLAC on disk."""
    recorder = AudioRecorder(audio_dir=audio_dir)
    ts = "2026-04-24_143208"

    recorder.start(ts)
    # Let the fake stream pump a few buffers.
    time.sleep(0.15)
    result = recorder.stop()

    # Result contract.
    assert result["timestamp"] == ts
    assert result["sample_rate"] == 16000
    assert result["channels"] == 1
    assert result["duration_sec"] > 0.0

    # FLAC on disk.
    flac_path = result["audio_path"]
    assert flac_path.exists()
    assert flac_path.name == f"{ts}.flac"

    # Valid FLAC header: "fLaC" magic bytes at offset 0.
    assert flac_path.read_bytes()[:4] == b"fLaC"

    # soundfile should be able to read the data back.
    data, sr = sf.read(str(flac_path), dtype="int16")
    assert sr == 16000
    assert len(data) > 0


def test_sidecar_json_has_all_schema_fields(
    patched_sounddevice: type[_FakeInputStream],
    audio_dir: Path,
) -> None:
    """The sidecar JSON must contain every field documented in SPEC.md."""
    recorder = AudioRecorder(audio_dir=audio_dir)
    ts = "2026-04-24_143208"

    recorder.start(ts)
    time.sleep(0.15)
    result = recorder.stop()

    sidecar_path = result["sidecar_path"]
    assert sidecar_path.exists()
    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))

    expected_keys = {
        "filename",
        "recorded_at",
        "duration_sec",
        "sample_rate",
        "channels",
        "codec",
        "device",
        "hostname",
        "hotkey",
        "pipeline_version",
    }
    assert expected_keys <= set(sidecar.keys()), (
        f"Missing keys: {expected_keys - set(sidecar.keys())}"
    )

    # Spot-check critical values.
    assert sidecar["filename"] == f"{ts}.flac"
    assert sidecar["sample_rate"] == 16000
    assert sidecar["channels"] == 1
    assert sidecar["codec"] == "flac"
    assert sidecar["device"] == "Fake Test Microphone"
    assert sidecar["hotkey"] == "win+alt+space"
    assert sidecar["duration_sec"] > 0.0

    # `recorded_at` must be a parseable ISO 8601 timestamp with timezone.
    parsed = datetime.fromisoformat(sidecar["recorded_at"])
    assert parsed.tzinfo is not None


def test_stop_without_start_raises(
    patched_sounddevice: type[_FakeInputStream],
    audio_dir: Path,
) -> None:
    recorder = AudioRecorder(audio_dir=audio_dir)
    with pytest.raises(RecorderError):
        recorder.stop()


def test_double_start_raises(
    patched_sounddevice: type[_FakeInputStream],
    audio_dir: Path,
) -> None:
    recorder = AudioRecorder(audio_dir=audio_dir)
    try:
        recorder.start("2026-04-24_143208")
        with pytest.raises(RecorderError):
            recorder.start("2026-04-24_143209")
    finally:
        # Always clean up so we don't leak threads into other tests.
        if recorder.is_recording:
            recorder.stop()


def test_mic_init_failure_raises_recorder_error(
    monkeypatch: pytest.MonkeyPatch, audio_dir: Path
) -> None:
    """If sounddevice.InputStream raises, start() should wrap it in RecorderError."""

    class _BrokenInputStream:
        def __init__(self, **_kwargs: Any) -> None:
            raise OSError("no microphone available")

    monkeypatch.setattr(recorder_module.sd, "InputStream", _BrokenInputStream)
    monkeypatch.setattr(recorder_module.sd, "query_devices", _fake_query_devices)

    recorder = AudioRecorder(audio_dir=audio_dir)
    with pytest.raises(RecorderError, match="microphone"):
        recorder.start("2026-04-24_143208")
    assert not recorder.is_recording


def test_reusable_after_stop(
    patched_sounddevice: type[_FakeInputStream],
    audio_dir: Path,
) -> None:
    """The recorder instance must be reusable for a second recording."""
    recorder = AudioRecorder(audio_dir=audio_dir)

    recorder.start("2026-04-24_143208")
    time.sleep(0.1)
    r1 = recorder.stop()

    recorder.start("2026-04-24_143209")
    time.sleep(0.1)
    r2 = recorder.stop()

    assert r1["audio_path"] != r2["audio_path"]
    assert r1["audio_path"].exists()
    assert r2["audio_path"].exists()


# ---------------------------------------------------------------------------
# AudioRecorder.get_waveform / get_level (live indicator data accessors)
# ---------------------------------------------------------------------------


def test_get_waveform_returns_empty_when_not_recording(audio_dir: Path) -> None:
    """A fresh recorder (or one between sessions) reports an empty waveform."""
    recorder = AudioRecorder(audio_dir=audio_dir)
    assert recorder.get_waveform() == []
    assert recorder.get_waveform(0) == []
    assert recorder.get_level() == 0.0


def test_get_waveform_returns_recent_samples_after_callback(
    patched_sounddevice: type[_FakeInputStream],
    audio_dir: Path,
) -> None:
    """After the fake input stream pumps a few buffers, get_waveform should
    return ~n_samples normalized floats in [-1, 1]."""
    recorder = AudioRecorder(audio_dir=audio_dir)
    recorder.start("2026-04-24_143208")
    try:
        # Let the fake stream pump several buffers.
        time.sleep(0.15)
        wave = recorder.get_waveform(100)
        assert isinstance(wave, list)
        assert len(wave) > 0
        # Should be close to (but not necessarily exactly) 100 samples; the
        # downsample stride may produce one fewer/more depending on rounding.
        assert 80 <= len(wave) <= 100, f"unexpected waveform length: {len(wave)}"
        # All samples must be normalized into the unit interval (the recorder
        # captures int16 input but get_waveform divides by 32768).
        assert all(-1.0 <= s <= 1.0 for s in wave), wave
        # And not all zero — the synthetic sine wave is at 0.5 amplitude.
        assert any(abs(s) > 0.05 for s in wave)
    finally:
        if recorder.is_recording:
            recorder.stop()


def test_get_level_returns_expected_rms(
    patched_sounddevice: type[_FakeInputStream],
    audio_dir: Path,
) -> None:
    """A 0.5-amplitude sine wave should yield an RMS near 0.5/sqrt(2) ≈ 0.354."""
    recorder = AudioRecorder(audio_dir=audio_dir)
    recorder.start("2026-04-24_143208")
    try:
        time.sleep(0.15)
        level = recorder.get_level()
        # Theoretical RMS for sine of amplitude 0.5 is ~0.3536. Allow a
        # generous margin because the int16 rounding + sample window cut
        # introduce small deviations.
        assert 0.20 <= level <= 0.50, f"unexpected RMS level: {level}"
    finally:
        if recorder.is_recording:
            recorder.stop()


def test_reset_state_clears_waveform_buffer(
    patched_sounddevice: type[_FakeInputStream],
    audio_dir: Path,
) -> None:
    """After stop() (which calls _reset_state) the waveform buffer must be empty."""
    recorder = AudioRecorder(audio_dir=audio_dir)
    recorder.start("2026-04-24_143208")
    time.sleep(0.15)
    # Confirm samples present BEFORE stop.
    pre_stop = recorder.get_waveform(50)
    assert len(pre_stop) > 0
    recorder.stop()
    # AFTER stop, the buffer is cleared.
    assert recorder.get_waveform() == []
    assert recorder.get_level() == 0.0


def test_get_waveform_handles_short_buffer(
    patched_sounddevice: type[_FakeInputStream],
    audio_dir: Path,
) -> None:
    """When fewer samples have been buffered than requested, return them all
    (no IndexError, no padding) — important when the indicator polls right
    after recording starts."""
    recorder = AudioRecorder(audio_dir=audio_dir)
    # Reduce callbacks dramatically so very little audio is buffered.
    monkey_orig = patched_sounddevice.num_callbacks
    try:
        patched_sounddevice.num_callbacks = 1
        recorder.start("2026-04-24_143208")
        time.sleep(0.05)
        wave = recorder.get_waveform(10000)  # ask for way more than exists
        # Must not crash and must return normalized floats.
        assert isinstance(wave, list)
        assert all(-1.0 <= s <= 1.0 for s in wave)
    finally:
        patched_sounddevice.num_callbacks = monkey_orig
        if recorder.is_recording:
            recorder.stop()


# ---------------------------------------------------------------------------
# retry_with_backoff (recorder._retry)
# ---------------------------------------------------------------------------


from recorder._retry import retry_with_backoff  # noqa: E402


class TestRetryWithBackoff:
    def test_returns_value_on_first_success(self) -> None:
        out = retry_with_backoff(lambda: 42, sleep_fn=lambda _s: None)
        assert out == 42

    def test_retries_until_success(self) -> None:
        attempts: list[int] = []
        sleeps: list[float] = []

        def thunk() -> str:
            attempts.append(1)
            if len(attempts) < 3:
                raise RuntimeError("transient")
            return "done"

        out = retry_with_backoff(
            thunk,
            max_attempts=4,
            base_delay_sec=2.0,
            sleep_fn=sleeps.append,
        )
        assert out == "done"
        assert len(attempts) == 3
        # 2 retries → 2 sleep calls of 2.0 and 4.0.
        assert sleeps == [2.0, 4.0]

    def test_raises_after_max_attempts(self) -> None:
        attempts: list[int] = []
        sleeps: list[float] = []

        def thunk() -> None:
            attempts.append(1)
            raise RuntimeError("never works")

        with pytest.raises(RuntimeError, match="never works"):
            retry_with_backoff(
                thunk,
                max_attempts=3,
                base_delay_sec=1.0,
                sleep_fn=sleeps.append,
            )
        assert len(attempts) == 3
        # We sleep BETWEEN attempts only (not after the last one).
        assert sleeps == [1.0, 2.0]

    def test_invokes_on_attempt_failed_callback(self) -> None:
        seen: list[tuple[int, str]] = []

        def thunk() -> None:
            raise ValueError("nope")

        with pytest.raises(ValueError):
            retry_with_backoff(
                thunk,
                max_attempts=3,
                base_delay_sec=0.1,
                on_attempt_failed=lambda n, e: seen.append((n, str(e))),
                sleep_fn=lambda _s: None,
            )
        assert [n for n, _ in seen] == [1, 2, 3]
        assert all(msg == "nope" for _, msg in seen)

    def test_non_retryable_predicate_short_circuits(self) -> None:
        attempts = {"n": 0}

        def thunk() -> None:
            attempts["n"] += 1
            raise RuntimeError("GEMINI_API_KEY missing — please renew")

        with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
            retry_with_backoff(
                thunk,
                max_attempts=4,
                non_retryable_predicates=[
                    lambda e: "GEMINI_API_KEY" in str(e),
                ],
                sleep_fn=lambda _s: None,
            )
        assert attempts["n"] == 1  # No retries.

    def test_only_retries_listed_exception_classes(self) -> None:
        def thunk() -> None:
            raise KeyError("nope")

        with pytest.raises(KeyError):
            retry_with_backoff(
                thunk,
                max_attempts=4,
                retryable_exceptions=(ValueError,),
                sleep_fn=lambda _s: None,
            )

    def test_max_attempts_zero_raises(self) -> None:
        with pytest.raises(ValueError):
            retry_with_backoff(lambda: 1, max_attempts=0)


# ---------------------------------------------------------------------------
# TrayApp._process_recording — pipeline retry / opener / mark_failed wiring
# ---------------------------------------------------------------------------


import json  # noqa: E402
import types  # noqa: E402

from recorder import retry_queue  # noqa: E402
import recorder.tray as tray_mod  # noqa: E402


@pytest.fixture
def tray_pipeline_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Path]:
    """Set up tmp paths + an unconfigured TrayApp instance for pipeline tests."""
    import config

    audio = tmp_path / "audio"
    transcripts = tmp_path / "audio" / "transcriptions"
    daily = tmp_path / "daily"
    vault = tmp_path / "vault"
    audio.mkdir()
    transcripts.mkdir(parents=True)
    daily.mkdir()
    vault.mkdir()

    monkeypatch.setattr(config, "AUDIO_DIR", audio)
    monkeypatch.setattr(config, "TRANSCRIPT_DIR", transcripts)
    monkeypatch.setattr(config, "DAILY_DIR", daily)
    monkeypatch.setattr(config, "VAULT_ROOT", vault)

    return {"audio": audio, "transcripts": transcripts, "daily": daily, "vault": vault}


def _build_tray_for_test(
    monkeypatch: pytest.MonkeyPatch,
) -> tray_mod.TrayApp:
    """Return a TrayApp instance with the pystray icon stubbed out."""

    class _FakeIcon:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            self.title = ""
            self.icon = None

        def update_menu(self) -> None:
            return None

        def notify(self, *_args: Any, **_kwargs: Any) -> None:
            return None

        def stop(self) -> None:
            return None

        def run(self) -> None:
            return None

    monkeypatch.setattr(tray_mod.pystray, "Icon", _FakeIcon)
    return tray_mod.TrayApp()


def _fake_recording_result(audio_dir: Path) -> dict[str, Any]:
    """A minimal RecordingResult-shaped dict pointing at a fake on-disk audio file."""
    audio = audio_dir / "2026-04-24_143208.flac"
    audio.write_bytes(b"\x00\x01")
    sidecar = audio_dir / "2026-04-24_143208.json"
    sidecar.write_text("{}", encoding="utf-8")
    return {
        "audio_path": audio,
        "sidecar_path": sidecar,
        "timestamp": "2026-04-24_143208",
        "duration_sec": 12.5,
        "sample_rate": 16000,
        "channels": 1,
    }


class TestProcessRecordingPipeline:
    def test_retry_then_succeed_runs_full_pipeline(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tray_pipeline_env: dict[str, Path],
    ) -> None:
        """If the first transcribe call fails but the second succeeds, the
        pipeline (write_transcript + append_memo_link + open_in_obsidian)
        must still run and the failed marker must NOT be created."""
        result = _fake_recording_result(tray_pipeline_env["audio"])

        # 1st call raises, 2nd returns the canned transcript dict.
        transcript_payload = {
            "raw": "raw",
            "cleaned": "cleaned",
            "slug": "test-slug",
            "title": "Test Slug",
            "summary": "summary",
            "tags": ["t"],
            "duration_sec": 12.5,
            "model_used": "fake",
        }

        attempts = {"n": 0}

        def fake_transcribe(_path: Path) -> dict[str, Any]:
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise RuntimeError("transient: server returned 503")
            return transcript_payload

        # Inject our own writer / daily_note / opener / transcribe modules
        # before _process_recording is called by patching the imports it does.
        cleaned_md = tray_pipeline_env["transcripts"] / "2026-04-24_143208_test-slug.md"
        raw_md = tray_pipeline_env["transcripts"] / "2026-04-24_143208_test-slug.raw.md"

        write_calls: list[tuple[Any, Any]] = []
        opener_calls: list[Path] = []
        daily_calls: list[dict[str, Any]] = []

        def fake_write_transcript(tr: Any, rec: Any) -> tuple[Path, Path]:
            write_calls.append((tr, rec))
            cleaned_md.write_text("md body", encoding="utf-8")
            raw_md.write_text("md body", encoding="utf-8")
            return cleaned_md, raw_md

        def fake_append_memo_link(**kwargs: Any) -> None:
            daily_calls.append(kwargs)

        def fake_open_in_obsidian(p: Path) -> None:
            opener_calls.append(p)

        # Build module stand-ins matching the lazy `from ... import` calls.
        fake_transcribe_mod = types.ModuleType("transcribe.transcribe")
        fake_transcribe_mod.transcribe_audio = fake_transcribe  # type: ignore[attr-defined]
        fake_writer_mod = types.ModuleType("obsidian.writer")
        fake_writer_mod.write_transcript = fake_write_transcript  # type: ignore[attr-defined]
        # New placeholder API — exercised below via finalize_transcript.
        # `write_placeholder` returns the placeholder path so the tray can
        # open it in Obsidian; `finalize_transcript` then takes that path
        # and produces the final cleaned/raw pair.
        fake_writer_mod.write_placeholder = (  # type: ignore[attr-defined]
            lambda *, timestamp, audio_path, duration_sec=0.0: (
                tray_pipeline_env["transcripts"] / f"{timestamp}_pending.md"
            )
        )
        fake_writer_mod.finalize_transcript = (  # type: ignore[attr-defined]
            lambda placeholder_path, tr, rec: fake_write_transcript(tr, rec)
        )
        fake_writer_mod.write_placeholder_error = (  # type: ignore[attr-defined]
            lambda *args, **kwargs: None
        )
        fake_daily_mod = types.ModuleType("obsidian.daily_note")
        fake_daily_mod.append_memo_link = fake_append_memo_link  # type: ignore[attr-defined]
        fake_opener_mod = types.ModuleType("obsidian.opener")
        fake_opener_mod.open_in_obsidian = fake_open_in_obsidian  # type: ignore[attr-defined]

        monkeypatch.setitem(sys.modules, "transcribe.transcribe", fake_transcribe_mod)
        monkeypatch.setitem(sys.modules, "obsidian.writer", fake_writer_mod)
        monkeypatch.setitem(sys.modules, "obsidian.daily_note", fake_daily_mod)
        monkeypatch.setitem(sys.modules, "obsidian.opener", fake_opener_mod)

        # Restore retry_with_backoff to the real fn but with sleep stubbed.
        monkeypatch.setattr(
            tray_mod,
            "retry_with_backoff",
            lambda func, **kw: _real_retry_no_sleep(func, **kw),
        )

        # Pre-create the placeholder file on disk so the tray's
        # ``placeholder_path.exists()`` check fires the finalize branch.
        placeholder_md = (
            tray_pipeline_env["transcripts"] / "2026-04-24_143208_pending.md"
        )
        placeholder_md.write_text("placeholder", encoding="utf-8")

        tray = _build_tray_for_test(monkeypatch)
        tray._process_recording(result)  # type: ignore[arg-type]

        # The transcribe callable was retried.
        assert attempts["n"] == 2

        # Pipeline ran on the second-attempt result.
        assert len(write_calls) == 1
        assert daily_calls == [
            {
                "timestamp": "2026-04-24_143208",
                "slug": "test-slug",
                "title": "Test Slug",
                "duration_sec": 12.5,
            }
        ]
        # Obsidian opener was called twice: once with the placeholder
        # (immediate UX), once with the final cleaned path (post-finalize).
        assert placeholder_md in opener_calls
        assert opener_calls[-1] == cleaned_md

        # No failed-transcription marker was left behind.
        marker = retry_queue.marker_path_for(result["audio_path"])
        assert not marker.exists()

    def test_auth_error_no_retry_marker_created(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tray_pipeline_env: dict[str, Path],
    ) -> None:
        """When the GEMINI_API_KEY predicate fires, NO retries are attempted
        and the marker IS created."""
        result = _fake_recording_result(tray_pipeline_env["audio"])

        attempts = {"n": 0}

        def fake_transcribe(_path: Path) -> dict[str, Any]:
            attempts["n"] += 1
            raise RuntimeError(
                "GEMINI_API_KEY rejected — please renew at aistudio.google.com"
            )

        opener_calls: list[Path] = []
        placeholder_error_calls: list[tuple[Path, BaseException]] = []

        # Stub modules.
        fake_transcribe_mod = types.ModuleType("transcribe.transcribe")
        fake_transcribe_mod.transcribe_audio = fake_transcribe  # type: ignore[attr-defined]
        fake_writer_mod = types.ModuleType("obsidian.writer")
        fake_writer_mod.write_transcript = lambda *a, **kw: (Path(), Path())  # type: ignore[attr-defined]
        fake_writer_mod.write_placeholder = (  # type: ignore[attr-defined]
            lambda *, timestamp, audio_path, duration_sec=0.0: (
                tray_pipeline_env["transcripts"] / f"{timestamp}_pending.md"
            )
        )
        fake_writer_mod.finalize_transcript = (  # type: ignore[attr-defined]
            lambda placeholder_path, tr, rec: (Path(), Path())
        )
        fake_writer_mod.write_placeholder_error = (  # type: ignore[attr-defined]
            lambda placeholder_path, exc, **kw: placeholder_error_calls.append(
                (placeholder_path, exc)
            )
        )
        fake_daily_mod = types.ModuleType("obsidian.daily_note")
        fake_daily_mod.append_memo_link = lambda **kw: None  # type: ignore[attr-defined]
        fake_opener_mod = types.ModuleType("obsidian.opener")
        fake_opener_mod.open_in_obsidian = lambda p: opener_calls.append(p)  # type: ignore[attr-defined]

        monkeypatch.setitem(sys.modules, "transcribe.transcribe", fake_transcribe_mod)
        monkeypatch.setitem(sys.modules, "obsidian.writer", fake_writer_mod)
        monkeypatch.setitem(sys.modules, "obsidian.daily_note", fake_daily_mod)
        monkeypatch.setitem(sys.modules, "obsidian.opener", fake_opener_mod)

        # Stub retry_with_backoff to the real impl but no real sleeps.
        monkeypatch.setattr(
            tray_mod,
            "retry_with_backoff",
            lambda func, **kw: _real_retry_no_sleep(func, **kw),
        )

        # Pre-create the placeholder file so the tray's exists() check
        # treats it as a real on-disk placeholder (and feeds it to
        # ``write_placeholder_error`` once transcription fails).
        placeholder_md = (
            tray_pipeline_env["transcripts"] / "2026-04-24_143208_pending.md"
        )
        placeholder_md.write_text("placeholder", encoding="utf-8")

        tray = _build_tray_for_test(monkeypatch)
        tray._process_recording(result)  # type: ignore[arg-type]

        # Auth predicate stops retries after the first attempt.
        assert attempts["n"] == 1

        # The opener WAS called once for the placeholder (immediate UX
        # before transcription). It must NOT be called for a final cleaned
        # path, because we never wrote one.
        assert opener_calls == [placeholder_md]

        # And we updated the placeholder with the error message.
        assert len(placeholder_error_calls) == 1
        assert placeholder_error_calls[0][0] == placeholder_md
        assert "GEMINI_API_KEY" in str(placeholder_error_calls[0][1])

        # Failed-transcription marker WAS created.
        marker = retry_queue.marker_path_for(result["audio_path"])
        assert marker.exists()
        data = json.loads(marker.read_text(encoding="utf-8"))
        assert data["attempts"] == 1
        assert "GEMINI_API_KEY" in data["last_error"]

    def test_rate_limit_short_circuits_outer_retry(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tray_pipeline_env: dict[str, Path],
    ) -> None:
        """A `RateLimitError` from `transcribe_audio` must NOT trigger 4 retries.

        Verifies Bug B fix: rate-limit errors short-circuit the outer
        retry_with_backoff loop, calling `transcribe_audio` exactly once
        and creating a retry marker so the user can re-trigger via the menu
        once the quota window has passed.
        """
        from transcribe.gemini_client import RateLimitError  # noqa: WPS433

        result = _fake_recording_result(tray_pipeline_env["audio"])

        attempts = {"n": 0}

        def fake_transcribe(_path: Path) -> dict[str, Any]:
            attempts["n"] += 1
            raise RateLimitError(
                "RESOURCE_EXHAUSTED: Quota exceeded for project 'foo'"
            )

        opener_calls: list[Path] = []

        fake_transcribe_mod = types.ModuleType("transcribe.transcribe")
        fake_transcribe_mod.transcribe_audio = fake_transcribe  # type: ignore[attr-defined]
        fake_writer_mod = types.ModuleType("obsidian.writer")
        fake_writer_mod.write_transcript = lambda *a, **kw: (Path(), Path())  # type: ignore[attr-defined]
        # Placeholder API stubs — the placeholder file is NOT pre-created
        # below, so the tray's exists() check will fall through and
        # ``write_placeholder_error`` should not fire.
        fake_writer_mod.write_placeholder = (  # type: ignore[attr-defined]
            lambda *, timestamp, audio_path, duration_sec=0.0: (
                tray_pipeline_env["transcripts"] / f"{timestamp}_pending.md"
            )
        )
        fake_writer_mod.finalize_transcript = (  # type: ignore[attr-defined]
            lambda placeholder_path, tr, rec: (Path(), Path())
        )
        fake_writer_mod.write_placeholder_error = (  # type: ignore[attr-defined]
            lambda *args, **kwargs: None
        )
        fake_daily_mod = types.ModuleType("obsidian.daily_note")
        fake_daily_mod.append_memo_link = lambda **kw: None  # type: ignore[attr-defined]
        fake_opener_mod = types.ModuleType("obsidian.opener")
        fake_opener_mod.open_in_obsidian = lambda p: opener_calls.append(p)  # type: ignore[attr-defined]

        monkeypatch.setitem(sys.modules, "transcribe.transcribe", fake_transcribe_mod)
        monkeypatch.setitem(sys.modules, "obsidian.writer", fake_writer_mod)
        monkeypatch.setitem(sys.modules, "obsidian.daily_note", fake_daily_mod)
        monkeypatch.setitem(sys.modules, "obsidian.opener", fake_opener_mod)

        monkeypatch.setattr(
            tray_mod,
            "retry_with_backoff",
            lambda func, **kw: _real_retry_no_sleep(func, **kw),
        )

        tray = _build_tray_for_test(monkeypatch)
        tray._process_recording(result)  # type: ignore[arg-type]

        # Outer retry was short-circuited: only 1 attempt (not 4).
        assert attempts["n"] == 1, (
            f"Rate-limit error should short-circuit, got {attempts['n']} attempts"
        )

        # Marker created so user can re-trigger via the tray menu.
        marker = retry_queue.marker_path_for(result["audio_path"])
        assert marker.exists()
        data = json.loads(marker.read_text(encoding="utf-8"))
        assert data["attempts"] == 1
        assert "RESOURCE_EXHAUSTED" in data["last_error"]

        # Opener WAS called once for the immediate placeholder open. The
        # placeholder file was never written to disk in this test (we use
        # a no-op fake) so the exists() check after transcription failure
        # returns False — meaning the tab will simply stay on a freshly
        # written placeholder if Obsidian had one. Either way, no FINAL
        # cleaned-file open occurred.
        expected_placeholder = (
            tray_pipeline_env["transcripts"] / "2026-04-24_143208_pending.md"
        )
        assert opener_calls == [expected_placeholder]

    def test_rate_limit_429_string_short_circuits_outer_retry(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tray_pipeline_env: dict[str, Path],
    ) -> None:
        """Plain RuntimeError with ``429`` in its message also short-circuits.

        This guards the case where the SDK raises something other than our
        custom `RateLimitError` but the message still indicates rate limiting.
        """
        result = _fake_recording_result(tray_pipeline_env["audio"])

        attempts = {"n": 0}

        def fake_transcribe(_path: Path) -> dict[str, Any]:
            attempts["n"] += 1
            raise RuntimeError("HTTP 429 Too Many Requests")

        fake_transcribe_mod = types.ModuleType("transcribe.transcribe")
        fake_transcribe_mod.transcribe_audio = fake_transcribe  # type: ignore[attr-defined]
        fake_writer_mod = types.ModuleType("obsidian.writer")
        fake_writer_mod.write_transcript = lambda *a, **kw: (Path(), Path())  # type: ignore[attr-defined]
        fake_writer_mod.write_placeholder = (  # type: ignore[attr-defined]
            lambda *, timestamp, audio_path, duration_sec=0.0: (
                tray_pipeline_env["transcripts"] / f"{timestamp}_pending.md"
            )
        )
        fake_writer_mod.finalize_transcript = (  # type: ignore[attr-defined]
            lambda placeholder_path, tr, rec: (Path(), Path())
        )
        fake_writer_mod.write_placeholder_error = (  # type: ignore[attr-defined]
            lambda *args, **kwargs: None
        )
        fake_daily_mod = types.ModuleType("obsidian.daily_note")
        fake_daily_mod.append_memo_link = lambda **kw: None  # type: ignore[attr-defined]
        fake_opener_mod = types.ModuleType("obsidian.opener")
        fake_opener_mod.open_in_obsidian = lambda p: None  # type: ignore[attr-defined]

        monkeypatch.setitem(sys.modules, "transcribe.transcribe", fake_transcribe_mod)
        monkeypatch.setitem(sys.modules, "obsidian.writer", fake_writer_mod)
        monkeypatch.setitem(sys.modules, "obsidian.daily_note", fake_daily_mod)
        monkeypatch.setitem(sys.modules, "obsidian.opener", fake_opener_mod)

        monkeypatch.setattr(
            tray_mod,
            "retry_with_backoff",
            lambda func, **kw: _real_retry_no_sleep(func, **kw),
        )

        tray = _build_tray_for_test(monkeypatch)
        tray._process_recording(result)  # type: ignore[arg-type]

        assert attempts["n"] == 1
        marker = retry_queue.marker_path_for(result["audio_path"])
        assert marker.exists()
        data = json.loads(marker.read_text(encoding="utf-8"))
        assert "429" in data["last_error"]


# Helpers used inside the pipeline tests above. We import the real retry
# helper but inject a no-op sleep so the suite stays fast.
def _real_retry_no_sleep(func: Any, **kwargs: Any) -> Any:
    from recorder._retry import retry_with_backoff as _real

    kwargs.setdefault("sleep_fn", lambda _s: None)
    return _real(func, **kwargs)


# ---------------------------------------------------------------------------
# P2 robustness fixes — regression tests
# ---------------------------------------------------------------------------


def test_start_failure_resets_stream_state(
    monkeypatch: pytest.MonkeyPatch, audio_dir: Path
) -> None:
    """Bug 1: if ``InputStream(...)`` constructs but ``.start()`` raises, the
    recorder must fully return to idle so a subsequent ``start()`` works.
    Previously ``_stream`` stayed set and ``is_recording`` got stuck at True,
    forcing a tray restart.
    """

    class _StartRaises(_FakeInputStream):
        """InputStream whose constructor succeeds but whose .start() raises."""

        raise_on_start: bool = True

        def start(self) -> None:  # type: ignore[override]
            if type(self).raise_on_start:
                # Flip the class-level flag so a second instance starts cleanly.
                type(self).raise_on_start = False
                raise OSError("mic permission denied")
            super().start()

    monkeypatch.setattr(recorder_module.sd, "InputStream", _StartRaises)
    monkeypatch.setattr(recorder_module.sd, "query_devices", _fake_query_devices)

    recorder = AudioRecorder(audio_dir=audio_dir)
    with pytest.raises(RecorderError, match="microphone"):
        recorder.start("2026-04-24_143208")

    # The whole point: state must be idle, not stuck with _stream set.
    assert recorder.is_recording is False
    assert recorder._stream is None  # noqa: SLF001 — invariant check
    assert recorder._soundfile is None  # noqa: SLF001
    assert recorder._timestamp is None  # noqa: SLF001
    assert recorder._audio_path is None  # noqa: SLF001

    # A subsequent start() must succeed (the class-level flag flipped off).
    recorder.start("2026-04-24_143209")
    try:
        time.sleep(0.1)
        r2 = recorder.stop()
    finally:
        if recorder.is_recording:
            recorder.stop()
    assert r2["timestamp"] == "2026-04-24_143209"
    assert r2["audio_path"].exists()


def test_writer_failure_stops_queue_growth(
    patched_sounddevice: type[_FakeInputStream],
    audio_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug 2: once the writer thread exits due to a write error, the audio
    callback must NOT keep appending buffers to the queue — otherwise a long
    recording after an early disk-full error leaks memory unboundedly.
    """

    class _ExplodingSoundFile:
        """Pretends to be a ``soundfile.SoundFile``; raises on first ``write``."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self._path = args[0] if args else kwargs.get("file", "")
            # Touch the path so the .flac file actually exists on disk —
            # the real soundfile would leave a header. That lets downstream
            # assertions like ``flac_path.exists()`` behave consistently.
            Path(self._path).write_bytes(b"fLaC")

        def write(self, _buffer: Any) -> None:
            raise OSError("disk full")

        def flush(self) -> None:
            return None

        def close(self) -> None:
            return None

    monkeypatch.setattr(recorder_module.sf, "SoundFile", _ExplodingSoundFile)

    # Crank the fake input stream to pump many buffers so we'd see unbounded
    # queue growth if the fix weren't in place.
    monkeypatch.setattr(patched_sounddevice, "num_callbacks", 200)

    recorder = AudioRecorder(audio_dir=audio_dir)
    recorder.start("2026-04-24_143208")

    # Wait for the writer to see the first buffer, explode, and set the
    # event. Give it generous slack for CI.
    for _ in range(100):
        if recorder._writer_stopped.is_set():  # noqa: SLF001
            break
        time.sleep(0.01)
    assert recorder._writer_stopped.is_set(), (  # noqa: SLF001
        "writer thread should have exited after write error"
    )

    # Let the fake stream keep firing callbacks. The queue must stay small
    # because the callback drops buffers when the writer is gone.
    time.sleep(0.2)
    qsize_after = recorder._queue.qsize()  # noqa: SLF001
    assert qsize_after <= 5, (
        f"queue grew unboundedly after writer failure: qsize={qsize_after}"
    )

    # stop() should surface the callback_error.
    with pytest.raises(RecorderError, match="disk full"):
        recorder.stop()

    # Post-stop: back to idle so the instance is reusable.
    assert recorder.is_recording is False


def test_stop_failure_marks_recording_as_failed(
    monkeypatch: pytest.MonkeyPatch,
    tray_pipeline_env: dict[str, Path],
) -> None:
    """Bug 3: if ``AudioRecorder.stop()`` raises in the tray handler, the
    partial audio file must be pushed into the retry queue — otherwise the
    user silently loses the recording.
    """
    audio_dir = tray_pipeline_env["audio"]

    # Simulate: partial audio file on disk (recorder left it behind before
    # stop() raised) and recorder state that points at it.
    timestamp = "2026-04-24_143208"
    partial_audio = audio_dir / f"{timestamp}.flac"
    partial_audio.write_bytes(b"fLaC\x00\x00\x00")
    partial_sidecar = audio_dir / f"{timestamp}.json"

    # Build a tray with the real recorder-state accessors stubbed out.
    tray = _build_tray_for_test(monkeypatch)

    # Point ``mark_failed`` at the temp audio dir so markers land there.
    from recorder import retry_queue as rq
    import config

    monkeypatch.setattr(config, "AUDIO_DIR", audio_dir)
    monkeypatch.setattr(tray_mod, "AUDIO_DIR", audio_dir)

    # Fake the recorder: is_recording True, stop() raises, and last_* props
    # report the partial file.
    class _FakeRecorder:
        def __init__(self) -> None:
            self._running = True

        @property
        def is_recording(self) -> bool:
            return self._running

        def start(self, _ts: str) -> None:
            self._running = True

        def stop(self) -> Any:
            self._running = False
            raise RecorderError("disk full during close")

        @property
        def last_audio_path(self) -> Path:
            return partial_audio

        @property
        def last_sidecar_path(self) -> Path:
            return partial_sidecar

        @property
        def last_timestamp(self) -> str:
            return timestamp

    tray._recorder = _FakeRecorder()  # type: ignore[assignment]
    tray._is_recording = True
    tray._last_timestamp = timestamp

    # Invoke the tray's stop path. It must NOT raise, and it must mark the
    # partial recording as failed.
    tray._stop_recording()

    marker = rq.marker_path_for(partial_audio)
    assert marker.exists(), "stop() failure should have produced a retry marker"
    data = json.loads(marker.read_text(encoding="utf-8"))
    assert data["audio_path"] == str(partial_audio)
    assert "disk full" in data["last_error"]
    assert data["attempts"] == 1


def test_retry_scan_serialized_by_lock(
    monkeypatch: pytest.MonkeyPatch,
    tray_pipeline_env: dict[str, Path],
) -> None:
    """Bug 4: two concurrent calls to ``_scan_and_retry_pending`` must NOT
    both pick up the same marker. The retry lock serializes them so a
    marker is processed by exactly one scan.
    """
    audio_dir = tray_pipeline_env["audio"]

    from recorder import retry_queue as rq
    import config

    monkeypatch.setattr(config, "AUDIO_DIR", audio_dir)

    # Drop a single failed marker so both scans see it.
    audio_path = audio_dir / "2026-04-24_143208.flac"
    sidecar_path = audio_dir / "2026-04-24_143208.json"
    audio_path.write_bytes(b"\x00\x01")
    sidecar_path.write_text("{}", encoding="utf-8")
    rq.mark_failed(audio_path, sidecar_path, "original failure")

    tray = _build_tray_for_test(monkeypatch)

    # Replace the inner pipeline step with a counter + deliberate slow work
    # so the two threads would definitely overlap without the lock. The
    # "process" here also clears the marker (simulating a successful retry).
    process_calls: list[Path] = []
    process_lock = threading.Lock()

    def _fake_process(result: dict[str, Any]) -> None:
        time.sleep(0.15)  # ensure threads would race without the lock
        with process_lock:
            process_calls.append(result["audio_path"])
        rq.clear_failed(result["audio_path"])

    monkeypatch.setattr(tray, "_process_recording", _fake_process)

    results: list[int] = []
    barrier = threading.Barrier(2)

    def _run() -> None:
        barrier.wait()  # line up both threads at the call site
        results.append(tray._scan_and_retry_pending())

    t1 = threading.Thread(target=_run, name="scan-1")
    t2 = threading.Thread(target=_run, name="scan-2")
    t1.start()
    t2.start()
    t1.join(timeout=5.0)
    t2.join(timeout=5.0)

    # Exactly ONE scan should have processed the marker — the other should
    # have found the marker gone (because the lock forced serialization and
    # the first scan already cleared it).
    assert len(process_calls) == 1, (
        f"marker was processed {len(process_calls)} times; "
        f"expected exactly 1. Lock is not serializing scans."
    )


def test_daily_note_uses_resolved_slug_on_collision(
    monkeypatch: pytest.MonkeyPatch,
    tray_pipeline_env: dict[str, Path],
) -> None:
    """Bug 5: if ``write_transcript`` has to append a collision suffix (e.g.
    ``-2``) to the slug, the daily-note link must use the RESOLVED slug, not
    the original. Otherwise the link points at the wrong (or nonexistent)
    file.
    """
    audio_dir = tray_pipeline_env["audio"]
    transcripts = tray_pipeline_env["transcripts"]

    result = _fake_recording_result(audio_dir)

    transcript_payload = {
        "raw": "raw",
        "cleaned": "cleaned",
        "slug": "test-slug",  # ORIGINAL slug
        "title": "Test Slug",
        "summary": "summary",
        "tags": ["t"],
        "duration_sec": 12.5,
        "model_used": "fake",
    }

    # Simulate a collision: write_transcript returns a `-2` suffixed slug
    # in the filename, as it would after detecting a pre-existing file.
    resolved_stem = "2026-04-24_143208_test-slug-2"
    cleaned_md = transcripts / f"{resolved_stem}.md"
    raw_md = transcripts / f"{resolved_stem}.raw.md"

    def fake_transcribe(_path: Path) -> dict[str, Any]:
        return transcript_payload

    def fake_write_transcript(_tr: Any, _rec: Any) -> tuple[Path, Path]:
        cleaned_md.write_text("md body", encoding="utf-8")
        raw_md.write_text("md body", encoding="utf-8")
        return cleaned_md, raw_md

    daily_calls: list[dict[str, Any]] = []

    def fake_append_memo_link(**kwargs: Any) -> None:
        daily_calls.append(kwargs)

    fake_transcribe_mod = types.ModuleType("transcribe.transcribe")
    fake_transcribe_mod.transcribe_audio = fake_transcribe  # type: ignore[attr-defined]
    fake_writer_mod = types.ModuleType("obsidian.writer")
    fake_writer_mod.write_transcript = fake_write_transcript  # type: ignore[attr-defined]
    # Placeholder API: the test focuses on slug-collision handling on the
    # finalize path, so route ``finalize_transcript`` through the same
    # ``fake_write_transcript`` that produces the ``-2`` suffixed stem.
    fake_writer_mod.write_placeholder = (  # type: ignore[attr-defined]
        lambda *, timestamp, audio_path, duration_sec=0.0: (
            tray_pipeline_env["transcripts"] / f"{timestamp}_pending.md"
        )
    )
    fake_writer_mod.finalize_transcript = (  # type: ignore[attr-defined]
        lambda placeholder_path, tr, rec: fake_write_transcript(tr, rec)
    )
    fake_writer_mod.write_placeholder_error = (  # type: ignore[attr-defined]
        lambda *args, **kwargs: None
    )
    fake_daily_mod = types.ModuleType("obsidian.daily_note")
    fake_daily_mod.append_memo_link = fake_append_memo_link  # type: ignore[attr-defined]
    fake_opener_mod = types.ModuleType("obsidian.opener")
    fake_opener_mod.open_in_obsidian = lambda _p: None  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "transcribe.transcribe", fake_transcribe_mod)
    monkeypatch.setitem(sys.modules, "obsidian.writer", fake_writer_mod)
    monkeypatch.setitem(sys.modules, "obsidian.daily_note", fake_daily_mod)
    monkeypatch.setitem(sys.modules, "obsidian.opener", fake_opener_mod)

    monkeypatch.setattr(
        tray_mod,
        "retry_with_backoff",
        lambda func, **kw: _real_retry_no_sleep(func, **kw),
    )

    # Pre-create the placeholder file so finalize_transcript is exercised
    # (otherwise the tray falls back to write_transcript on a missing
    # placeholder, which is also valid but bypasses the new code path).
    placeholder_md = transcripts / "2026-04-24_143208_pending.md"
    placeholder_md.write_text("placeholder", encoding="utf-8")

    tray = _build_tray_for_test(monkeypatch)
    tray._process_recording(result)  # type: ignore[arg-type]

    assert len(daily_calls) == 1, daily_calls
    # CRITICAL: the slug passed to append_memo_link must be the RESOLVED one
    # derived from the filename, not the original ``tr["slug"]``.
    assert daily_calls[0]["slug"] == "test-slug-2", (
        f"append_memo_link got slug={daily_calls[0]['slug']!r}; "
        f"expected 'test-slug-2' (the resolved, collision-suffixed slug)."
    )
    assert daily_calls[0]["title"] == "Test Slug"
    assert daily_calls[0]["timestamp"] == "2026-04-24_143208"


# ---------------------------------------------------------------------------
# Bug 1 (P1): indicator stop button must never START a new recording.
# Reuses the _build_tray_for_test + tray_pipeline_env helpers above.
# ---------------------------------------------------------------------------


class _StopIndicatorFakeRecorder:
    """Tracks start/stop/is_recording calls so we can assert on the tray side."""

    def __init__(self, *, running: bool = False) -> None:
        self._running = running
        self.start_calls: list[str] = []
        self.stop_calls: int = 0

    @property
    def is_recording(self) -> bool:
        return self._running

    def start(self, timestamp: str) -> None:
        self.start_calls.append(timestamp)
        self._running = True

    def stop(self) -> dict[str, Any]:
        self.stop_calls += 1
        self._running = False
        # Mimic a minimal RecordingResult so _stop_recording can proceed.
        return {
            "audio_path": Path("nonexistent.flac"),
            "sidecar_path": Path("nonexistent.json"),
            "timestamp": "2026-04-24_143208",
            "duration_sec": 0.1,
            "sample_rate": 16_000,
            "channels": 1,
        }

    # Minimum accessors the retry/partial path inspects on error. Not used
    # by the tests below (stop() succeeds), but kept for completeness.
    @property
    def last_audio_path(self) -> Path | None:
        return None

    @property
    def last_sidecar_path(self) -> Path | None:
        return None

    @property
    def last_timestamp(self) -> str | None:
        return None


def test_indicator_stop_clicked_when_not_recording_is_noop(
    monkeypatch: pytest.MonkeyPatch,
    tray_pipeline_env: dict[str, Path],
) -> None:
    """Bug 1 regression: clicking the indicator's stop button while the tray
    is **idle** (e.g. the user already stopped via the hotkey and an orphan
    indicator lingered) must NOT start a new recording.

    The prior implementation called ``_on_hotkey()`` which toggles — so a
    late click could silently START recording. This is a privacy bug. Fix
    makes the handler stop-only.
    """
    tray = _build_tray_for_test(monkeypatch)

    fake_rec = _StopIndicatorFakeRecorder(running=False)
    tray._recorder = fake_rec  # type: ignore[assignment]
    tray._is_recording = False

    # Also stub out _start_recording / _stop_recording directly so we can
    # assert neither was invoked (defence-in-depth vs. just checking mic calls).
    start_calls: list[int] = []
    stop_calls: list[int] = []
    monkeypatch.setattr(
        tray, "_start_recording", lambda: start_calls.append(1)
    )
    monkeypatch.setattr(
        tray, "_stop_recording", lambda: stop_calls.append(1)
    )

    tray._indicator_stop_clicked()

    # The critical assertion: no recording was started.
    assert fake_rec.start_calls == [], (
        f"recorder.start was called when tray was idle: {fake_rec.start_calls}. "
        f"Indicator stop must NEVER start a new recording."
    )
    assert start_calls == [], "_start_recording must not fire when idle"
    # And stop_recording also not called (nothing to stop).
    assert stop_calls == []


def test_indicator_stop_clicked_calls_stop_recording_when_recording(
    monkeypatch: pytest.MonkeyPatch,
    tray_pipeline_env: dict[str, Path],
) -> None:
    """Bug 1 positive path: while actively recording, the indicator's stop
    button must route to ``_stop_recording`` (not ``_start_recording``)."""
    tray = _build_tray_for_test(monkeypatch)

    fake_rec = _StopIndicatorFakeRecorder(running=True)
    tray._recorder = fake_rec  # type: ignore[assignment]
    tray._is_recording = True
    tray._record_start_monotonic = time.monotonic()
    tray._last_timestamp = "2026-04-24_143208"

    start_calls: list[int] = []
    stop_calls: list[int] = []

    # Wrap both so we can tell which was invoked. Keep the real _stop_recording
    # OFF the path — we just want to verify dispatch.
    monkeypatch.setattr(
        tray, "_start_recording", lambda: start_calls.append(1)
    )
    monkeypatch.setattr(
        tray, "_stop_recording", lambda: stop_calls.append(1)
    )

    tray._indicator_stop_clicked()

    # Since the handler dispatches onto a worker thread, wait briefly.
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and not stop_calls:
        time.sleep(0.01)

    assert stop_calls == [1], (
        f"_stop_recording should have been invoked; got stop_calls={stop_calls}"
    )
    assert start_calls == [], (
        f"_start_recording must not fire from the indicator; "
        f"got start_calls={start_calls}"
    )
