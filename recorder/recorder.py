"""Streaming FLAC audio recorder.

Uses :mod:`sounddevice` in callback mode to stream 16 kHz mono PCM frames into a
:class:`soundfile.SoundFile` opened in write mode. This lets us record multi-hour
sessions without holding the audio in RAM — each callback writes directly to disk,
so a mid-recording crash still leaves a playable FLAC file on disk.

A JSON sidecar is written alongside every recording; its schema is defined in
``SPEC.md`` and matches :class:`RecordingResult`.
"""

from __future__ import annotations

import collections
import json
import logging
import math
import queue
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict

import sounddevice as sd
import soundfile as sf

from config import (
    AUDIO_CODEC,
    AUDIO_DIR,
    AUDIO_SUBTYPE,
    CHANNELS,
    HOSTNAME,
    HOTKEY_LABEL,
    PIPELINE_VERSION,
    SAMPLE_RATE,
)

if TYPE_CHECKING:
    import numpy as np

logger = logging.getLogger(__name__)

# Block size for the InputStream callback. 1024 frames at 16 kHz = 64 ms latency,
# which is plenty responsive for toggle-start/stop while keeping callback overhead low.
_BLOCKSIZE: int = 1024

# Number of seconds of recent samples kept in :attr:`AudioRecorder._waveform_buffer`
# for the visual indicator. Three seconds is plenty for a smooth scrolling waveform
# without burning RAM (3 s × 16 kHz × 8 bytes ≈ 384 KiB).
_WAVEFORM_BUFFER_SECONDS: int = 3

# How many "raw" samples to consider per output column when downsampling
# in :meth:`AudioRecorder.get_waveform`. Using a multiple greater than 1 means
# we sweep over a real chunk of audio per drawn column so quiet samples don't
# alias the displayed waveform to zero.
_WAVEFORM_SAMPLES_PER_COLUMN: int = 50

# Window length used by :meth:`AudioRecorder.get_level` for the RMS estimate.
# 1024 samples ≈ 64 ms at 16 kHz which roughly matches one input block.
_LEVEL_WINDOW_SAMPLES: int = 1024


def _normalize_to_unit(samples: list[Any]) -> list[float]:
    """Scale a list of audio samples to floats in ``[-1, 1]``.

    Detects int16 input by either type or magnitude (samples whose absolute
    value exceeds ``1.5`` cannot be normalized float32 audio) and divides by
    ``32768.0`` to bring them into the unit interval. Float-valued samples
    are returned as ``float`` unchanged.

    Returns an empty list for an empty input.
    """
    if not samples:
        return []
    first = samples[0]
    if isinstance(first, (int,)):
        return [float(s) / 32768.0 for s in samples]
    # Defensive: if anyone fed us float samples that are still in int16
    # range, scale them down too.
    try:
        peak = max(abs(float(s)) for s in samples)
    except (TypeError, ValueError):
        return [0.0 for _ in samples]
    if peak > 1.5:
        return [float(s) / 32768.0 for s in samples]
    return [float(s) for s in samples]


class RecordingResult(TypedDict):
    """Metadata about a completed recording.

    Mirrors the shape documented in ``SPEC.md``.
    """

    audio_path: Path
    sidecar_path: Path
    timestamp: str
    duration_sec: float
    sample_rate: int
    channels: int


class RecorderError(RuntimeError):
    """Raised when the recorder cannot start or stop cleanly."""


class AudioRecorder:
    """Streams microphone input into a FLAC file on disk.

    Usage::

        rec = AudioRecorder()
        rec.start("2026-04-24_143208")
        # ... user speaks ...
        result = rec.stop()

    The recorder is single-shot: after :meth:`stop` you should construct a new
    instance (or just call :meth:`start` again — internal state is reset).
    """

    def __init__(
        self,
        sample_rate: int = SAMPLE_RATE,
        channels: int = CHANNELS,
        subtype: str = AUDIO_SUBTYPE,
        audio_format: str = AUDIO_CODEC.upper(),
        audio_dir: Path = AUDIO_DIR,
        blocksize: int = _BLOCKSIZE,
    ) -> None:
        self.sample_rate = sample_rate
        self.channels = channels
        self.subtype = subtype
        self.audio_format = audio_format
        self.audio_dir = audio_dir
        self.blocksize = blocksize

        self._stream: sd.InputStream | None = None
        self._soundfile: sf.SoundFile | None = None
        self._queue: queue.Queue[Any] = queue.Queue()
        self._writer_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        # Set by the writer thread when it exits (normal or error). The audio
        # callback checks this to avoid growing the queue unboundedly after a
        # writer-side failure (e.g. disk full).
        self._writer_stopped = threading.Event()

        self._timestamp: str | None = None
        self._audio_path: Path | None = None
        self._sidecar_path: Path | None = None
        self._device_name: str = "unknown"
        self._frames_written: int = 0
        self._start_wall_time: float | None = None
        self._recorded_at_iso: str | None = None
        self._callback_error: BaseException | None = None

        # Rolling buffer of recent mono samples for the live indicator
        # waveform / level meter. Lock-protected so the UI thread can read
        # while the audio callback writes.
        self._waveform_buffer: collections.deque[float] = collections.deque(
            maxlen=int(self.sample_rate * _WAVEFORM_BUFFER_SECONDS),
        )
        self._waveform_lock = threading.Lock()

    # ------------------------------------------------------------------ public

    @property
    def is_recording(self) -> bool:
        """Whether a recording is currently in progress."""
        return self._stream is not None

    @property
    def last_timestamp(self) -> str | None:
        """Timestamp of the current/most-recent recording, if any.

        Exposed so the tray can reconstruct a best-effort
        :class:`RecordingResult` when :meth:`stop` raises mid-finalization.
        The value is cleared by :meth:`_reset_state` once the recorder
        returns to idle.
        """
        return self._timestamp

    @property
    def last_audio_path(self) -> Path | None:
        """Audio-file path of the current/most-recent recording, if any.

        Like :attr:`last_timestamp`, this is exposed so callers can fall back
        to the partial file when :meth:`stop` raises.
        """
        return self._audio_path

    @property
    def last_sidecar_path(self) -> Path | None:
        """Sidecar-JSON path of the current/most-recent recording, if any."""
        return self._sidecar_path

    def get_waveform(self, n_samples: int = 600) -> list[float]:
        """Return the most recent ``n_samples`` samples scaled to ``[-1, 1]``.

        Thread-safe — the indicator UI polls this from a non-audio thread.
        Returns ``[]`` when no audio has been buffered yet (e.g. polled
        before the first callback fires, or after :meth:`_reset_state`).

        The buffer holds raw int16 samples at the recorder's sample rate; we
        snapshot a generous tail (``n_samples * _WAVEFORM_SAMPLES_PER_COLUMN``
        samples) and stride-downsample to ``n_samples`` floats, which gives
        the indicator a stable scrolling waveform regardless of the actual
        sample rate.

        Parameters
        ----------
        n_samples:
            Desired length of the output. Must be > 0.
        """
        if n_samples <= 0:
            return []
        with self._waveform_lock:
            if not self._waveform_buffer:
                return []
            buf = list(self._waveform_buffer)

        # Take a tail wide enough to give us real material to downsample.
        # Cap at the full buffer length.
        tail_len = min(len(buf), n_samples * _WAVEFORM_SAMPLES_PER_COLUMN)
        tail = buf[-tail_len:]
        if len(tail) <= n_samples:
            return _normalize_to_unit(tail)

        stride = max(1, len(tail) // n_samples)
        # Pick every ``stride``-th sample, then trim/pad to exactly n_samples.
        sampled = [tail[i] for i in range(0, len(tail), stride)][:n_samples]
        return _normalize_to_unit(sampled)

    def get_level(self) -> float:
        """RMS level of the most recent block of samples, in ``[0, 1]``.

        Cheap to call (~1024 samples summed). Returns ``0.0`` when no audio
        has been buffered yet so callers can render a flat meter at idle.
        """
        with self._waveform_lock:
            if not self._waveform_buffer:
                return 0.0
            # Snapshot the last block-or-so worth of samples while holding
            # the lock; do the math after releasing it.
            n = min(len(self._waveform_buffer), _LEVEL_WINDOW_SAMPLES)
            tail = list(self._waveform_buffer)[-n:]

        if not tail:
            return 0.0
        normalized = _normalize_to_unit(tail)
        if not normalized:
            return 0.0
        rms = math.sqrt(sum(x * x for x in normalized) / len(normalized))
        # Clamp to [0, 1] — RMS of normalized samples can't exceed 1 in
        # theory, but float rounding occasionally pushes it a hair over.
        return max(0.0, min(1.0, rms))

    def start(self, timestamp: str) -> None:
        """Begin recording to ``<AUDIO_DIR>/<timestamp>.flac``.

        Parameters
        ----------
        timestamp:
            ISO-ish timestamp string, e.g. ``"2026-04-24_143208"``. This is used
            as the base filename.

        Raises
        ------
        RecorderError
            If the recorder is already running, or if the mic/file cannot be
            opened. The caller is expected to surface this to the user (e.g.
            via the tray icon/tooltip).
        """
        if self.is_recording:
            raise RecorderError("Recorder is already running; stop() it first.")

        self.audio_dir.mkdir(parents=True, exist_ok=True)

        self._timestamp = timestamp
        self._audio_path = self.audio_dir / f"{timestamp}.{AUDIO_CODEC}"
        self._sidecar_path = self.audio_dir / f"{timestamp}.json"
        self._frames_written = 0
        self._callback_error = None
        self._stop_event.clear()
        self._writer_stopped.clear()
        # Drain any stale items from previous runs.
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break

        # Resolve input device name up front so we can record it even if the
        # device disappears mid-recording.
        self._device_name = self._resolve_device_name()

        # Open the SoundFile first so that even the tiniest audio data is
        # flushed to a valid FLAC header on disk.
        try:
            self._soundfile = sf.SoundFile(
                str(self._audio_path),
                mode="w",
                samplerate=self.sample_rate,
                channels=self.channels,
                subtype=self.subtype,
                format=self.audio_format,
            )
        except Exception as exc:
            logger.exception("Failed to open FLAC file at %s", self._audio_path)
            self._reset_state()
            raise RecorderError(f"Failed to open audio file: {exc}") from exc

        # Kick off the writer thread before opening the stream so callbacks
        # always have a consumer.
        self._writer_thread = threading.Thread(
            target=self._writer_loop,
            name="AudioRecorder-writer",
            daemon=True,
        )
        self._writer_thread.start()

        try:
            self._stream = sd.InputStream(
                samplerate=self.sample_rate,
                channels=self.channels,
                dtype="int16",
                blocksize=self.blocksize,
                callback=self._audio_callback,
            )
            self._stream.start()
        except Exception as exc:
            logger.exception("Failed to open microphone input stream")
            # Tear down writer + file so we don't leak handles.
            self._stop_event.set()
            if self._writer_thread is not None:
                self._writer_thread.join(timeout=2.0)
            if self._soundfile is not None:
                try:
                    self._soundfile.close()
                except Exception:  # pragma: no cover — best effort
                    logger.exception("Error closing SoundFile during failed start")
                self._soundfile = None
            # If `InputStream(...)` itself succeeded but `.start()` raised, we
            # still hold a stream handle. Close it and clear the attribute so
            # ``is_recording`` (which is defined as ``_stream is not None``)
            # returns to False and the next ``start()`` isn't rejected with
            # "already running".
            if self._stream is not None:
                try:
                    self._stream.close()
                except Exception:  # pragma: no cover — best effort
                    logger.exception("Error closing InputStream during failed start")
                self._stream = None
            # Leave the (possibly empty) file in place for post-mortem; it will
            # have a valid empty FLAC header written by soundfile. But fully
            # reset the idle-state invariant so a subsequent start() works.
            self._reset_state()
            raise RecorderError(f"Failed to open microphone: {exc}") from exc

        self._start_wall_time = time.monotonic()
        self._recorded_at_iso = datetime.now().astimezone().isoformat()
        logger.info(
            "Recording started: device=%r file=%s", self._device_name, self._audio_path
        )

    def stop(self) -> RecordingResult:
        """Stop recording, flush+close the FLAC file, write JSON sidecar.

        Returns a :class:`RecordingResult` with canonical metadata. Safe to call
        even if a callback raised an error — we try hard to leave a valid file
        on disk before re-raising.
        """
        if not self.is_recording:
            raise RecorderError("Recorder is not running.")

        assert self._stream is not None
        assert self._soundfile is not None
        assert self._timestamp is not None
        assert self._audio_path is not None
        assert self._sidecar_path is not None
        assert self._start_wall_time is not None
        assert self._recorded_at_iso is not None

        # Stop/close the stream first so no more callbacks fire.
        try:
            self._stream.stop()
        except Exception:  # pragma: no cover — best effort
            logger.exception("Error stopping InputStream")
        try:
            self._stream.close()
        except Exception:  # pragma: no cover — best effort
            logger.exception("Error closing InputStream")
        self._stream = None

        # Signal writer to drain + exit.
        self._stop_event.set()
        if self._writer_thread is not None:
            self._writer_thread.join(timeout=5.0)
            if self._writer_thread.is_alive():  # pragma: no cover — shouldn't happen
                logger.warning("Writer thread did not exit within timeout")
        self._writer_thread = None

        # Flush + close file.
        try:
            self._soundfile.flush()
        except Exception:  # pragma: no cover — best effort
            logger.exception("Error flushing SoundFile")
        try:
            self._soundfile.close()
        except Exception:  # pragma: no cover — best effort
            logger.exception("Error closing SoundFile")
        self._soundfile = None

        duration_sec = self._frames_written / float(self.sample_rate)
        audio_path = self._audio_path
        sidecar_path = self._sidecar_path
        timestamp = self._timestamp
        recorded_at = self._recorded_at_iso

        # Write sidecar JSON.
        sidecar_data = {
            "filename": audio_path.name,
            "recorded_at": recorded_at,
            "duration_sec": round(duration_sec, 3),
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "codec": AUDIO_CODEC,
            "device": self._device_name,
            "hostname": HOSTNAME,
            "hotkey": HOTKEY_LABEL.lower(),
            "pipeline_version": PIPELINE_VERSION,
        }
        try:
            sidecar_path.write_text(
                json.dumps(sidecar_data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception:
            logger.exception("Failed to write sidecar JSON at %s", sidecar_path)
            # Don't raise — the audio file itself is the important artifact.

        logger.info(
            "Recording stopped: %s (%.2fs, %d frames)",
            audio_path,
            duration_sec,
            self._frames_written,
        )

        # If a callback error occurred, raise AFTER we've safely closed the file
        # so the user still has the partial recording on disk.
        if self._callback_error is not None:
            err = self._callback_error
            self._reset_state()
            raise RecorderError(f"Recording callback error: {err}") from err

        result: RecordingResult = {
            "audio_path": audio_path,
            "sidecar_path": sidecar_path,
            "timestamp": timestamp,
            "duration_sec": round(duration_sec, 3),
            "sample_rate": self.sample_rate,
            "channels": self.channels,
        }
        self._reset_state()
        return result

    # ----------------------------------------------------------------- helpers

    @staticmethod
    def make_timestamp(now: datetime | None = None) -> str:
        """Return a filename-safe timestamp string, e.g. ``2026-04-24_143208``."""
        dt = now or datetime.now()
        return dt.strftime("%Y-%m-%d_%H%M%S")

    def _resolve_device_name(self) -> str:
        """Best-effort lookup of the default input device name."""
        try:
            info = sd.query_devices(kind="input")
            if isinstance(info, dict) and "name" in info:
                return str(info["name"])
            return "unknown"
        except Exception:
            logger.exception("Could not query default input device; using 'unknown'")
            return "unknown"

    def _audio_callback(
        self,
        indata: "np.ndarray",
        frames: int,
        time_info: Any,
        status: sd.CallbackFlags,
    ) -> None:
        """sounddevice callback: push a *copy* of the buffer onto the queue.

        We copy because sounddevice reuses the underlying numpy buffer across
        callbacks — holding a reference without copying can corrupt earlier
        frames.

        If the writer thread has exited (set :attr:`_writer_stopped`) we drop
        the buffer on the floor rather than growing the queue unboundedly.
        Otherwise a long recording after an early disk-full error would chew
        through RAM until the user pressed stop.
        """
        if status:
            # Overflows/underflows are logged but don't abort the recording.
            logger.warning("InputStream status: %s", status)
        if self._writer_stopped.is_set():
            # Writer is no longer consuming — dropping the buffer is the only
            # safe option. The error that caused it to stop has already been
            # latched into ``self._callback_error`` and will be surfaced by
            # the next ``stop()`` call.
            return
        try:
            self._queue.put_nowait(indata.copy())
        except Exception as exc:  # pragma: no cover — queue.put_nowait rarely fails
            self._callback_error = exc
            logger.exception("Failed to enqueue audio buffer")
            return

        # Best-effort: stash a mono copy in the rolling waveform buffer so
        # the indicator UI can render a live mic-level / waveform. We never
        # let waveform-buffer errors abort recording — the FLAC writer is
        # the source of truth.
        try:
            mono = indata[:, 0] if indata.ndim > 1 else indata
            with self._waveform_lock:
                self._waveform_buffer.extend(mono.tolist())
        except Exception:  # pragma: no cover — defensive
            logger.debug("Failed to update waveform buffer", exc_info=True)

    def _writer_loop(self) -> None:
        """Pull buffers off the queue and write them to the FLAC file."""
        try:
            while True:
                try:
                    buffer = self._queue.get(timeout=0.1)
                except queue.Empty:
                    if self._stop_event.is_set() and self._queue.empty():
                        return
                    continue

                if self._soundfile is None:
                    return
                try:
                    self._soundfile.write(buffer)
                    self._frames_written += len(buffer)
                except Exception as exc:
                    self._callback_error = exc
                    logger.exception("Error writing audio buffer to disk")
                    return
        except Exception as exc:  # pragma: no cover — defensive
            self._callback_error = exc
            logger.exception("Writer loop crashed")
        finally:
            # Signal the audio callback to stop enqueuing. Must happen on
            # every exit path (normal drain, write failure, or unexpected
            # crash) so the queue can't grow after the writer is gone.
            self._writer_stopped.set()

    def _reset_state(self) -> None:
        """Clear per-recording state so the instance can be reused."""
        self._stream = None
        self._soundfile = None
        self._writer_thread = None
        self._timestamp = None
        self._audio_path = None
        self._sidecar_path = None
        self._device_name = "unknown"
        self._frames_written = 0
        self._start_wall_time = None
        self._recorded_at_iso = None
        self._callback_error = None
        self._stop_event.clear()
        self._writer_stopped.clear()
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
        # Drop any stale samples from the previous session so the indicator
        # opens on a fresh waveform when recording starts again.
        with self._waveform_lock:
            self._waveform_buffer.clear()
