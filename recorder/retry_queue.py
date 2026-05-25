"""Persistent retry queue for failed transcriptions.

The transcription stage of the pipeline can fail for transient reasons (network
hiccups, the Gemini service being briefly down, an expired API key) while the
underlying audio file has already been safely written to disk. We don't want
to lose those recordings — instead we drop a small JSON marker next to each
audio file so the tray app can pick them up on the next launch (or on demand
from the menu).

Marker schema (``<audio>.failed.json``)::

    {
      "audio_path": "<absolute path to .flac>",
      "sidecar_path": "<absolute path to .json sidecar>",
      "first_failed_at": "2026-04-24T17:28:33-04:00",
      "last_attempted_at": "2026-04-24T17:30:00-04:00",
      "attempts": 3,
      "last_error": "<exception message>"
    }

Public API:
    mark_failed(audio_path, sidecar_path, error) -> Path
    clear_failed(audio_path) -> None
    find_pending_retries() -> list[dict]
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

import config

logger = logging.getLogger(__name__)

__all__ = [
    "FAILED_SUFFIX",
    "clear_failed",
    "find_pending_retries",
    "marker_path_for",
    "mark_failed",
]

#: Filename suffix appended to an audio path to produce its retry marker.
FAILED_SUFFIX: str = ".failed.json"


# ----------------------------------------------------------------- helpers


def marker_path_for(audio_path: Path) -> Path:
    """Return the ``.failed.json`` marker path that lives next to ``audio_path``.

    The marker name is the audio filename plus :data:`FAILED_SUFFIX`. We do
    NOT replace the suffix — keeping the original ``.flac`` in the marker
    name makes the relationship obvious in Explorer.
    """
    audio_path = Path(audio_path)
    return audio_path.with_name(audio_path.name + FAILED_SUFFIX)


def _now_iso() -> str:
    """Local-time ISO 8601 timestamp with timezone offset."""
    return datetime.now().astimezone().isoformat()


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write ``payload`` as JSON atomically: tempfile then :func:`os.replace`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except Exception:
        # Best-effort cleanup of the temp file on failure.
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _read_marker(path: Path) -> dict[str, Any] | None:
    """Read+parse a marker file; return ``None`` if it can't be read or is malformed."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("Cannot read retry marker %s: %s", path, exc)
        return None
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        logger.warning("Retry marker %s is not valid JSON: %s", path, exc)
        return None
    if not isinstance(obj, dict):
        logger.warning("Retry marker %s does not contain a JSON object", path)
        return None
    return obj


# ----------------------------------------------------------------- public API


def mark_failed(audio_path: Path, sidecar_path: Path, error: str) -> Path:
    """Create or update a ``.failed.json`` marker for the given recording.

    If a marker already exists, ``attempts`` is incremented and
    ``last_attempted_at`` / ``last_error`` are refreshed. ``first_failed_at``
    is preserved on update.

    Parameters
    ----------
    audio_path:
        Absolute path to the FLAC audio file.
    sidecar_path:
        Absolute path to the JSON sidecar.
    error:
        Stringified exception from the failed transcription attempt.

    Returns
    -------
    Path
        The marker file path.
    """
    audio_path = Path(audio_path)
    sidecar_path = Path(sidecar_path)
    marker = marker_path_for(audio_path)

    now = _now_iso()
    existing = _read_marker(marker) if marker.exists() else None

    attempts = 1
    first_failed_at = now
    if existing is not None:
        try:
            attempts = int(existing.get("attempts", 0)) + 1
        except (TypeError, ValueError):
            attempts = 2
        prior_first = existing.get("first_failed_at")
        if isinstance(prior_first, str) and prior_first:
            first_failed_at = prior_first

    payload: dict[str, Any] = {
        "audio_path": str(audio_path),
        "sidecar_path": str(sidecar_path),
        "first_failed_at": first_failed_at,
        "last_attempted_at": now,
        "attempts": attempts,
        "last_error": str(error),
    }

    _atomic_write_json(marker, payload)
    logger.info(
        "Failed-transcription marker %s (attempt %d) written: %s",
        "updated" if existing is not None else "created",
        attempts,
        marker,
    )
    return marker


def clear_failed(audio_path: Path) -> None:
    """Delete the marker for ``audio_path`` if one exists. No-op otherwise."""
    marker = marker_path_for(Path(audio_path))
    try:
        marker.unlink()
    except FileNotFoundError:
        return
    except OSError:
        logger.exception("Could not delete retry marker %s", marker)
        return
    logger.info("Cleared failed-transcription marker: %s", marker)


def find_pending_retries(audio_dir: Path | None = None) -> list[dict[str, Any]]:
    """Scan ``audio_dir`` (default :data:`config.AUDIO_DIR`) for ``*.failed.json``.

    Returns a list of parsed marker dicts. ``audio_path`` and ``sidecar_path``
    are normalized to absolute :class:`Path` instances; the marker's own path
    is added under the ``marker_path`` key.

    Markers without a corresponding audio file on disk are skipped (and logged)
    so we never try to retry a recording whose audio has been deleted.
    """
    directory = Path(audio_dir) if audio_dir is not None else config.AUDIO_DIR

    if not directory.exists():
        return []

    pending: list[dict[str, Any]] = []
    for marker in sorted(directory.glob(f"*{FAILED_SUFFIX}")):
        data = _read_marker(marker)
        if data is None:
            continue

        audio_str = data.get("audio_path")
        sidecar_str = data.get("sidecar_path")
        if not isinstance(audio_str, str) or not isinstance(sidecar_str, str):
            logger.warning("Marker %s missing audio/sidecar paths; skipping", marker)
            continue

        audio_path = Path(audio_str)
        sidecar_path = Path(sidecar_str)
        if not audio_path.exists():
            logger.warning(
                "Marker %s references missing audio file %s; skipping",
                marker,
                audio_path,
            )
            continue

        entry = dict(data)
        entry["audio_path"] = audio_path
        entry["sidecar_path"] = sidecar_path
        entry["marker_path"] = marker
        pending.append(entry)

    return pending
