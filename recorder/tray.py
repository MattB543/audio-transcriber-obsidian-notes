"""System-tray front-end for the voice-note recorder.

Launches a :class:`pystray.Icon` in the Windows notification area with two
states (idle = gray circle, recording = red circle), registers a global
``Win+Alt+Space`` hotkey via :mod:`pynput`, and orchestrates the background
transcription/Obsidian/publish pipeline on stop.

Entry point: ``python -m recorder.tray``.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import subprocess
import sys
import threading
import time
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from PIL import Image, ImageDraw
from pynput import keyboard

# pystray needs to be imported via its backend; on Windows this is win32.
import pystray

from config import (
    AUDIO_CODEC,
    AUDIO_DIR,
    CHANNELS,
    HOTKEY_COMBO_PYNPUT,
    HOTKEY_LABEL,
    LOG_DIR,
    SAMPLE_RATE,
    TRANSCRIPT_DIR,
)
from recorder._retry import retry_with_backoff
from recorder.recorder import AudioRecorder, RecorderError, RecordingResult
from recorder.retry_queue import (
    clear_failed,
    find_pending_retries,
    mark_failed,
)

if TYPE_CHECKING:
    from pystray import MenuItem

    from recorder.indicator import RecordingIndicator

logger = logging.getLogger(__name__)

_TRAY_LOG_FILENAME = "tray.log"
_ICON_SIZE = 64
_STATUS_TICK_SECONDS = 0.5


# ----------------------------------------------------------------- logging


def _configure_logging() -> None:
    """Install a rotating-file + stderr log handler, idempotent."""
    root = logging.getLogger()
    if getattr(root, "_notes_pipeline_configured", False):
        return

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / _TRAY_LOG_FILENAME

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = logging.handlers.RotatingFileHandler(
        log_path,
        maxBytes=1_000_000,  # 1 MB
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.INFO)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    stream_handler.setLevel(logging.INFO)

    root.setLevel(logging.INFO)
    root.addHandler(file_handler)
    root.addHandler(stream_handler)
    root._notes_pipeline_configured = True  # type: ignore[attr-defined]


# ----------------------------------------------------------------- icons


def _make_circle_icon(color: tuple[int, int, int, int]) -> Image.Image:
    """Draw a filled circle on a transparent 64×64 RGBA canvas."""
    img = Image.new("RGBA", (_ICON_SIZE, _ICON_SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    margin = 6
    draw.ellipse(
        (margin, margin, _ICON_SIZE - margin, _ICON_SIZE - margin),
        fill=color,
    )
    return img


def _idle_icon() -> Image.Image:
    return _make_circle_icon((120, 120, 120, 255))  # gray


def _recording_icon() -> Image.Image:
    return _make_circle_icon((220, 40, 40, 255))  # red


# ----------------------------------------------------------------- tray app


class TrayApp:
    """Glue object that ties together the tray icon, hotkey, and recorder."""

    def __init__(self) -> None:
        self._recorder = AudioRecorder()
        self._is_recording = False
        self._record_start_monotonic: float | None = None
        self._last_timestamp: str | None = None
        self._state_lock = threading.Lock()
        # Serializes retry scans (startup scan + manual menu click). Without
        # this, two concurrent enumerations can both see the same
        # ``.failed.json`` marker before either clears it, causing the same
        # audio to be transcribed twice.
        self._retry_lock = threading.Lock()

        self._idle_image = _idle_icon()
        self._recording_image = _recording_icon()

        self._icon = pystray.Icon(
            name="notes-pipeline",
            icon=self._idle_image,
            title="Voice Notes (idle)",
            menu=self._build_menu(),
        )

        self._hotkey_listener: keyboard.GlobalHotKeys | None = None
        self._status_tick_stop = threading.Event()
        self._status_tick_thread: threading.Thread | None = None

        # Visual recording indicator (live waveform + stop button). Created
        # lazily on _start_recording and torn down on _stop_recording. We
        # keep both the handle (for .hide()) and the thread (for joining).
        self._indicator_handle: "RecordingIndicator | None" = None
        self._indicator_thread: threading.Thread | None = None

    # ------------------------------------------------------------- lifecycle

    def run(self) -> None:
        """Start all background threads and block on the tray icon."""
        logger.info("Starting tray app. Hotkey=%s", HOTKEY_LABEL)

        self._start_hotkey_listener()
        self._start_publisher_watcher()
        self._start_pending_retry_scan()

        # pystray.Icon.run() blocks until .stop() is called.
        try:
            self._icon.run()
        finally:
            self._shutdown()

    def _shutdown(self) -> None:
        """Best-effort cleanup of background threads + recorder."""
        logger.info("Tray app shutting down")
        self._status_tick_stop.set()
        if self._status_tick_thread is not None:
            self._status_tick_thread.join(timeout=1.0)
        if self._hotkey_listener is not None:
            try:
                self._hotkey_listener.stop()
            except Exception:  # pragma: no cover — best effort
                logger.exception("Error stopping hotkey listener")
        if self._recorder.is_recording:
            try:
                self._recorder.stop()
            except Exception:  # pragma: no cover — best effort
                logger.exception("Error stopping recorder during shutdown")

    # ------------------------------------------------------------- hotkey

    def _start_hotkey_listener(self) -> None:
        """Register the global Win+Alt+Space hotkey in a background thread."""
        try:
            listener = keyboard.GlobalHotKeys(
                {HOTKEY_COMBO_PYNPUT: self._on_hotkey}
            )
            listener.start()
            self._hotkey_listener = listener
            logger.info("Global hotkey listener registered: %s", HOTKEY_LABEL)
        except Exception:
            logger.exception(
                "Failed to register global hotkey %s — recorder usable only via menu",
                HOTKEY_LABEL,
            )
            self._notify_error("Could not register global hotkey")

    def _on_hotkey(self) -> None:
        """Hotkey callback — toggles between start/stop."""
        # pynput fires this on its listener thread; offload to a worker so we
        # never block the keyboard hook (which would make the hotkey sluggish).
        threading.Thread(
            target=self._toggle_recording, name="hotkey-toggle", daemon=True
        ).start()

    def _toggle_recording(self) -> None:
        with self._state_lock:
            should_stop = self._is_recording

        if should_stop:
            self._stop_recording()
        else:
            self._start_recording()

    # ------------------------------------------------------------- recording

    def _start_recording(self) -> None:
        timestamp = AudioRecorder.make_timestamp()
        with self._state_lock:
            if self._is_recording:
                logger.debug("start_recording called while already recording; ignoring")
                return
            try:
                self._recorder.start(timestamp)
            except RecorderError as exc:
                logger.error("Failed to start recording: %s", exc)
                self._notify_error(f"Mic error: {exc}")
                return

            self._is_recording = True
            self._record_start_monotonic = time.monotonic()
            self._last_timestamp = timestamp

        self._play_beep(start=True)
        self._set_icon_state(recording=True)
        self._start_status_ticker()
        self._start_indicator()
        logger.info("Recording started: %s", timestamp)

    def _stop_recording(self) -> None:
        # Tear the indicator down up front so the user sees immediate feedback
        # even if the recorder takes a moment to flush. Tolerant of failures —
        # we never want a UI cleanup error to block stopping the recording.
        self._stop_indicator()

        with self._state_lock:
            if not self._is_recording:
                logger.debug("stop_recording called while idle; ignoring")
                return

            try:
                result = self._recorder.stop()
            except RecorderError as exc:
                logger.exception("Recorder stop failed")
                # Best-effort: if a partial FLAC landed on disk, push it
                # into the retry queue so the user doesn't silently lose
                # the recording. See Bug 3 in the review.
                self._enqueue_partial_recording(exc)
                self._notify_error(f"Stop error: {exc}")
                self._is_recording = False
                self._record_start_monotonic = None
                self._stop_status_ticker()
                self._set_icon_state(recording=False)
                return

            self._is_recording = False
            self._record_start_monotonic = None

        self._play_beep(start=False)
        self._set_icon_state(recording=False)
        self._stop_status_ticker()

        logger.info(
            "Recording finished: %s (%.2fs)",
            result["audio_path"],
            result["duration_sec"],
        )

        # Kick off downstream pipeline in the background so the tray stays responsive.
        threading.Thread(
            target=self._process_recording,
            args=(result,),
            name="post-recording-pipeline",
            daemon=True,
        ).start()

    def _enqueue_partial_recording(self, exc: BaseException) -> None:
        """Best-effort: drop a ``.failed.json`` marker for a partial recording.

        Called when :meth:`AudioRecorder.stop` itself raises (e.g. disk full
        after a successful start). By the time ``stop()`` raises, a partial
        FLAC may already be on disk; the sidecar may or may not have been
        written. Without this hook, the recording would be silently lost —
        the retry-scan path only looks for ``.failed.json`` markers.

        We source the audio/sidecar paths from the recorder's exposed
        ``last_*`` accessors. If we can't get a usable audio path (or the
        file doesn't exist on disk), we log and give up — there's nothing
        to retry.
        """
        audio_path = self._recorder.last_audio_path
        sidecar_path = self._recorder.last_sidecar_path
        timestamp = self._recorder.last_timestamp

        # Fall back to ``_last_timestamp`` which the tray itself has been
        # tracking since _start_recording. This handles the case where the
        # recorder's internal state was reset before stop() raised.
        if audio_path is None and timestamp is None:
            timestamp = self._last_timestamp
        if audio_path is None and timestamp is not None:
            audio_path = AUDIO_DIR / f"{timestamp}.{AUDIO_CODEC}"
        if sidecar_path is None and timestamp is not None:
            sidecar_path = AUDIO_DIR / f"{timestamp}.json"

        if audio_path is None or not audio_path.exists():
            logger.warning(
                "No partial audio file on disk for failed stop; "
                "nothing to retry (timestamp=%s)",
                timestamp,
            )
            return

        # Sidecar path is optional — pass something sensible even if the
        # file wasn't written yet (mark_failed records it for later use
        # but doesn't require existence).
        if sidecar_path is None:
            sidecar_path = audio_path.with_suffix(".json")

        try:
            mark_failed(audio_path, sidecar_path, f"stop failed: {exc}")
            logger.info(
                "Partial recording %s enqueued for retry after stop failure",
                audio_path.name,
            )
        except Exception:  # pragma: no cover — best effort
            logger.exception(
                "Could not mark partial recording %s as failed after stop error",
                audio_path,
            )

    def _process_recording(self, result: RecordingResult) -> None:
        """Runs in a background thread after recording stops.

        Two-phase flow:
          1. **Immediate** (within ~100 ms of stop): write a
             ``<timestamp>_pending.md`` placeholder and open it in Obsidian
             so the user sees a "Processing..." note right away.
          2. **Slow**: transcribe via Gemini, then ``finalize_transcript``
             overwrites the placeholder in-place and renames it onto the
             final ``<timestamp>_<slug>.md`` stem. Obsidian's open tab
             follows the rename, so the user's editor refreshes without
             flickering.

        Imports are done lazily so (a) the tray launches even if the other
        pipeline modules aren't implemented yet, and (b) heavy ML deps don't
        slow down tray startup. The transcription call is wrapped in
        :func:`retry_with_backoff`; on permanent failure we drop a
        ``.failed.json`` marker next to the audio so the recording can be
        retried later — and if a placeholder was opened, we overwrite it
        with an in-Obsidian error message.
        """
        audio_path: Path = result["audio_path"]
        sidecar_path: Path = result["sidecar_path"]
        timestamp: str = result["timestamp"]
        duration_sec: float = float(result.get("duration_sec", 0.0))

        pipeline_t0 = time.monotonic()
        logger.info(
            "==== PIPELINE START ts=%s audio=%s duration=%.1fs ====",
            timestamp, audio_path.name, duration_sec,
        )
        try:
            try:
                from transcribe.transcribe import transcribe_audio  # type: ignore
                from obsidian.writer import (  # type: ignore
                    finalize_transcript,
                    write_placeholder,
                    write_placeholder_error,
                    write_transcript,
                )
                from obsidian.daily_note import append_memo_link  # type: ignore
                from obsidian.opener import open_in_obsidian  # type: ignore
            except ImportError as exc:
                logger.warning(
                    "Post-recording pipeline unavailable (module not yet implemented): %s",
                    exc,
                )
                self._notify(f"Recording saved: {audio_path.name}")
                return

            # Lazy import here so the module isn't required at tray startup.
            from transcribe.gemini_client import RateLimitError  # type: ignore

            # ---------- IMMEDIATE: placeholder + open Obsidian ----------
            placeholder_path: Path | None = None
            try:
                placeholder_path = write_placeholder(
                    timestamp=timestamp,
                    audio_path=audio_path,
                    duration_sec=duration_sec,
                )
                open_in_obsidian(placeholder_path)
                logger.info(
                    "Placeholder written and opened in Obsidian: %s",
                    placeholder_path,
                )
            except Exception:  # noqa: BLE001 — UX nicety, never fatal
                logger.exception(
                    "Failed to write placeholder / open Obsidian; "
                    "continuing with transcription"
                )
                placeholder_path = None

            # ---------- SLOW: transcribe ----------
            logger.info("Transcribing %s...", audio_path)
            tr = retry_with_backoff(
                lambda: transcribe_audio(audio_path),
                max_attempts=4,
                base_delay_sec=2.0,
                retryable_exceptions=(Exception,),
                non_retryable_predicates=[
                    # Auth / config errors need user action, not retries.
                    lambda e: "GEMINI_API_KEY" in str(e),
                    lambda e: isinstance(e, FileNotFoundError),
                    # Rate-limit / quota errors: short-circuit immediately so
                    # we don't burn the user's daily budget on a 4× retry
                    # storm. The marker lets the user re-trigger via the menu
                    # once the rate-limit window has passed.
                    lambda e: (
                        isinstance(e, RateLimitError)
                        or "RESOURCE_EXHAUSTED" in str(e)
                        or "429" in str(e)
                        or "rate limit" in str(e).lower()
                        or "quota" in str(e).lower()
                    ),
                ],
                on_attempt_failed=lambda attempt, exc: logger.warning(
                    "Transcription attempt %d failed: %s", attempt, exc
                ),
            )

            # If this audio had a stale failed marker from a previous run,
            # clear it now that we've succeeded.
            try:
                clear_failed(audio_path)
            except Exception:  # pragma: no cover — best effort
                logger.exception("Could not clear failed marker for %s", audio_path)

            # ---------- FINALIZE: overwrite placeholder + rename + raw ----------
            logger.info("Writing Obsidian notes for slug=%s...", tr["slug"])
            if placeholder_path is not None and placeholder_path.exists():
                cleaned_path, raw_path = finalize_transcript(
                    placeholder_path, tr, result
                )
            else:
                # Fallback: placeholder write or Obsidian-open failed
                # earlier (or the user manually deleted the placeholder
                # before transcription finished). Use the legacy
                # all-or-nothing two-file write.
                cleaned_path, raw_path = write_transcript(tr, result)

            # Both ``finalize_transcript`` and ``write_transcript`` may
            # append ``-2`` (etc.) to the slug on collision. The daily-note
            # link must point at the resolved filename, not the original
            # ``tr["slug"]``. Derive it from the actual stem on disk.
            prefix = f"{timestamp}_"
            cleaned_stem = cleaned_path.stem
            if cleaned_stem.startswith(prefix):
                resolved_slug = cleaned_stem[len(prefix):]
            else:
                resolved_slug = tr["slug"]

            append_memo_link(
                timestamp=timestamp,
                slug=resolved_slug,
                title=tr["title"],
                duration_sec=duration_sec,
            )
            logger.info("Done. Created: %s, %s", cleaned_path, raw_path)

            # Re-open the FINAL path. If we overwrote-then-renamed, this is
            # mostly a no-op safety call (Obsidian's tab already followed
            # the rename via its file-watcher). When we took the fallback
            # ``write_transcript`` path, this is the first time Obsidian
            # is asked to open the cleaned file.
            try:
                open_in_obsidian(cleaned_path)
            except Exception:  # pragma: no cover — opener already swallows
                logger.exception("open_in_obsidian raised unexpectedly")

            logger.info(
                "==== PIPELINE DONE ts=%s in %.2fs (title=%r) ====",
                timestamp, time.monotonic() - pipeline_t0, tr["title"],
            )
            self._notify(f"Transcribed: {tr['title']}")
        except Exception as exc:  # noqa: BLE001 — we surface all errors to user
            logger.exception(
                "==== PIPELINE FAILED ts=%s in %.2fs for %s ====",
                timestamp, time.monotonic() - pipeline_t0, audio_path.name,
            )
            # If a placeholder is still open in Obsidian, replace its
            # contents with an error message so the user sees the failure
            # in-place instead of a stuck "Processing..." spinner.
            placeholder_local = locals().get("placeholder_path")
            if (
                placeholder_local is not None
                and Path(placeholder_local).exists()
            ):
                try:
                    # write_placeholder_error was imported alongside the
                    # other obsidian.writer helpers above; re-import here
                    # so we don't rely on names from the inner try block
                    # if that import failed.
                    from obsidian.writer import (  # type: ignore
                        write_placeholder_error as _wpe,
                    )

                    _wpe(
                        placeholder_local,
                        exc,
                        timestamp=timestamp,
                        duration_sec=duration_sec,
                    )
                except Exception:  # pragma: no cover — best effort
                    logger.exception(
                        "Could not update placeholder with error message"
                    )
            try:
                mark_failed(audio_path, sidecar_path, str(exc))
            except Exception:  # pragma: no cover — best effort
                logger.exception(
                    "Could not write failed-transcription marker for %s", audio_path
                )
            self._notify_error(
                f"Transcription failed (saved to retry queue): {exc}"
            )

    # ------------------------------------------------------------- retry queue

    def _start_pending_retry_scan(self) -> None:
        """On startup, retry any failed transcriptions from previous sessions."""
        threading.Thread(
            target=self._scan_and_retry_pending,
            name="startup-retry-scan",
            daemon=True,
        ).start()

    def _scan_and_retry_pending(self, *, notify_when_zero: bool = False) -> int:
        """Scan ``AUDIO_DIR`` for failed-transcription markers and retry each.

        Returns the number of recordings that were successfully retried (i.e.
        completed the full pipeline). Always logs each attempt; surfaces a
        tray notification only when ``notify_when_zero`` is False (startup
        path) and at least one was processed.

        Serialized via :attr:`_retry_lock` so that a manual "Retry Pending"
        click cannot race with the startup scan — otherwise both enumerations
        could see the same marker before either cleared it, producing
        duplicate Obsidian files.
        """
        with self._retry_lock:
            try:
                pending = find_pending_retries()
            except Exception:
                logger.exception("Could not enumerate pending retries")
                return 0

            if not pending:
                logger.info("No pending failed transcriptions to retry")
                if notify_when_zero:
                    self._notify("No pending transcriptions to retry")
                return 0

            logger.info("Retrying %d pending transcription(s)", len(pending))
            succeeded = 0
            for entry in pending:
                audio_path: Path = entry["audio_path"]
                sidecar_path: Path = entry["sidecar_path"]
                try:
                    result = self._build_result_from_sidecar(audio_path, sidecar_path)
                except Exception:
                    logger.exception(
                        "Could not reconstruct RecordingResult for %s; leaving marker in place",
                        audio_path,
                    )
                    continue

                logger.info(
                    "Retrying transcription for %s (prior attempts: %s)",
                    audio_path.name,
                    entry.get("attempts"),
                )
                try:
                    self._process_recording(result)
                except Exception:
                    # _process_recording handles its own errors, but be defensive.
                    logger.exception(
                        "Retry pipeline crashed for %s", audio_path
                    )
                    continue

                # If the marker is gone, transcription succeeded.
                from recorder.retry_queue import marker_path_for  # local import

                if not marker_path_for(audio_path).exists():
                    succeeded += 1

            if succeeded:
                self._notify(f"Retried {succeeded} pending transcription(s)")
            elif notify_when_zero:
                self._notify("Retry scan finished; no successes")

            return succeeded

    @staticmethod
    def _build_result_from_sidecar(
        audio_path: Path, sidecar_path: Path
    ) -> RecordingResult:
        """Reconstruct a :class:`RecordingResult` dict from a sidecar JSON file.

        Falls back to filesystem-derived defaults for any missing fields so
        that a partial sidecar still lets us retry.
        """
        import json as _json

        sidecar_data: dict[str, object] = {}
        if sidecar_path.exists():
            try:
                sidecar_data = _json.loads(sidecar_path.read_text(encoding="utf-8"))
            except Exception:
                logger.exception(
                    "Could not parse sidecar %s; falling back to defaults",
                    sidecar_path,
                )

        # Derive timestamp from filename if the sidecar doesn't carry one.
        timestamp = audio_path.stem  # "<YYYY-MM-DD_HHMMSS>"

        duration_sec_raw = sidecar_data.get("duration_sec", 0.0)
        try:
            duration_sec = float(duration_sec_raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            duration_sec = 0.0

        sample_rate_raw = sidecar_data.get("sample_rate", 16_000)
        try:
            sample_rate = int(sample_rate_raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            sample_rate = 16_000

        channels_raw = sidecar_data.get("channels", 1)
        try:
            channels = int(channels_raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            channels = 1

        result: RecordingResult = {
            "audio_path": audio_path,
            "sidecar_path": sidecar_path,
            "timestamp": timestamp,
            "duration_sec": duration_sec,
            "sample_rate": sample_rate,
            "channels": channels,
        }
        return result

    # ------------------------------------------------------------- publisher

    def _start_publisher_watcher(self) -> None:
        """Spawn the optional publisher watcher thread, if the module exists."""
        def _runner() -> None:
            try:
                from publisher.watcher import run_forever  # type: ignore
            except ImportError:
                logger.info("Publisher watcher not yet implemented; skipping.")
                return
            try:
                logger.info("Publisher watcher starting")
                run_forever()
            except Exception:
                logger.exception("Publisher watcher crashed")

        threading.Thread(target=_runner, name="publisher-watcher", daemon=True).start()

    # ------------------------------------------------------------- icon/menu

    def _set_icon_state(self, *, recording: bool) -> None:
        if recording:
            self._icon.icon = self._recording_image
            self._icon.title = "Voice Notes (recording)"
        else:
            self._icon.icon = self._idle_image
            self._icon.title = "Voice Notes (idle)"
        # Force pystray to refresh the menu labels as well.
        try:
            self._icon.update_menu()
        except Exception:  # pragma: no cover — pystray internals
            logger.debug("update_menu failed", exc_info=True)

    def _status_label(self) -> str:
        if self._is_recording and self._record_start_monotonic is not None:
            elapsed = int(time.monotonic() - self._record_start_monotonic)
            return f"Status: Recording ({elapsed // 60}:{elapsed % 60:02d})"
        return "Status: Idle"

    def _build_menu(self) -> pystray.Menu:
        return pystray.Menu(
            pystray.MenuItem(lambda _item: self._status_label(), None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Recordings folder", self._open_audio_dir),
            pystray.MenuItem("Transcripts folder", self._open_transcript_dir),
            pystray.MenuItem("Show log", self._open_log),
            pystray.MenuItem(
                "Retry Pending Transcriptions", self._retry_pending_clicked
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._quit),
        )

    # ------------------------------------------------------------- ticker

    def _start_status_ticker(self) -> None:
        """Update the tray menu's status label every 500ms during recording."""
        self._status_tick_stop.clear()

        def _tick() -> None:
            while not self._status_tick_stop.wait(_STATUS_TICK_SECONDS):
                if not self._is_recording:
                    break
                try:
                    self._icon.update_menu()
                    # Also refresh the tooltip with the elapsed time.
                    if self._record_start_monotonic is not None:
                        elapsed = int(time.monotonic() - self._record_start_monotonic)
                        self._icon.title = (
                            f"Voice Notes (recording {elapsed // 60}:{elapsed % 60:02d})"
                        )
                except Exception:  # pragma: no cover — pystray internals
                    logger.debug("status ticker update failed", exc_info=True)

        self._status_tick_thread = threading.Thread(
            target=_tick, name="status-ticker", daemon=True
        )
        self._status_tick_thread.start()

    def _stop_status_ticker(self) -> None:
        self._status_tick_stop.set()

    # ------------------------------------------------------------- indicator

    def _start_indicator(self) -> None:
        """Spawn the live recording-indicator window in a daemon thread.

        Failures here are non-fatal: the recording itself is the important
        thing, so we log and continue if tkinter is unavailable, the display
        is missing, etc.
        """
        # Lazy import — keeps tkinter out of the hot startup path and means
        # the tray still loads on systems without Tk.
        try:
            from recorder.indicator import run_indicator_thread  # noqa: WPS433
        except Exception:
            logger.exception("Could not import indicator module; skipping UI overlay")
            self._indicator_handle = None
            self._indicator_thread = None
            return

        try:
            handle, thread = run_indicator_thread(
                self._recorder, stop_callback=self._indicator_stop_clicked
            )
            self._indicator_handle = handle
            self._indicator_thread = thread
        except Exception:
            logger.exception(
                "Failed to start indicator window; recording continues without it"
            )
            self._indicator_handle = None
            self._indicator_thread = None

    def _stop_indicator(self) -> None:
        """Close the indicator window (if any) without blocking the caller."""
        handle = getattr(self, "_indicator_handle", None)
        if handle is not None:
            try:
                handle.hide()
            except Exception:
                logger.debug("Indicator hide() failed", exc_info=True)
        self._indicator_handle = None
        self._indicator_thread = None

    def _indicator_stop_clicked(self) -> None:
        """Stop-button callback for the indicator window. **Stop-only.**

        Never starts a new recording, even if the indicator is clicked after
        the hotkey has already stopped the recording (orphan/late-click case).
        Prior to this guard, the callback reused ``_on_hotkey`` which is a
        toggle — a late click could secretly START a recording, which is a
        privacy risk.

        Runs the stop on a worker thread so the indicator's Tk thread isn't
        blocked while the recorder finalizes the file.
        """
        if not self._is_recording:
            logger.debug(
                "Indicator stop clicked but no recording active; ignoring"
            )
            return
        logger.info("Stop clicked from indicator window")
        threading.Thread(
            target=self._stop_recording,
            name="indicator-stop",
            daemon=True,
        ).start()

    # ------------------------------------------------------------- menu actions

    def _open_audio_dir(self, _icon: pystray.Icon, _item: "MenuItem") -> None:
        _open_in_explorer(AUDIO_DIR)

    def _open_transcript_dir(self, _icon: pystray.Icon, _item: "MenuItem") -> None:
        _open_in_explorer(TRANSCRIPT_DIR)

    def _open_log(self, _icon: pystray.Icon, _item: "MenuItem") -> None:
        _open_in_explorer(LOG_DIR / _TRAY_LOG_FILENAME)

    def _retry_pending_clicked(
        self, _icon: pystray.Icon, _item: "MenuItem"
    ) -> None:
        """Menu handler: kick off a retry scan in a background thread."""
        threading.Thread(
            target=self._scan_and_retry_pending,
            kwargs={"notify_when_zero": True},
            name="manual-retry-scan",
            daemon=True,
        ).start()

    def _quit(self, _icon: pystray.Icon, _item: "MenuItem") -> None:
        logger.info("Quit requested from tray menu")
        self._icon.stop()

    # ------------------------------------------------------------- feedback

    def _play_beep(self, *, start: bool) -> None:
        """No-op (beeps removed by user request).

        Kept as a method so existing call sites in start/stop don't need to be
        edited if the user later wants audible feedback back.
        """
        return

    def _notify(self, message: str) -> None:
        """Non-error pop-up via pystray's notify()."""
        try:
            self._icon.notify(message, "Voice Notes")
        except Exception:  # pragma: no cover — pystray backends vary
            logger.debug("Notify failed: %s", message, exc_info=True)

    def _notify_error(self, message: str) -> None:
        """Error feedback: change tooltip + notify."""
        try:
            self._icon.title = f"Voice Notes (ERROR: {message})"
            self._icon.notify(message, "Voice Notes — Error")
        except Exception:  # pragma: no cover — pystray backends vary
            logger.debug("Error notify failed", exc_info=True)


# ----------------------------------------------------------------- helpers


def _open_in_explorer(path: Path) -> None:
    """Open a folder (or a file's containing folder) in Windows Explorer.

    explorer.exe is a GUI process so it does not normally attach a console,
    but we pass CREATE_NO_WINDOW defensively in case Windows ever decides
    to allocate one when launched from pythonw.exe.
    """
    no_window = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    try:
        if path.is_file():
            subprocess.Popen(
                ["explorer", "/select,", str(path)],  # noqa: S603,S607
                creationflags=no_window,
            )
        else:
            path.mkdir(parents=True, exist_ok=True)
            os.startfile(str(path))  # type: ignore[attr-defined]  # Windows-only
    except Exception:
        logger.exception("Could not open %s in Explorer", path)


# ----------------------------------------------------------------- entry


def main() -> None:
    """Configure logging and launch the tray app. Blocks until Quit."""
    _configure_logging()
    app = TrayApp()
    app.run()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Interrupted; exiting.", file=sys.stderr)
        sys.exit(0)
