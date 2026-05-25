"""Tests for :mod:`recorder.retry_queue`."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Make repo root importable regardless of where pytest is invoked.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import config  # noqa: E402
from recorder import retry_queue  # noqa: E402


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_audio_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect ``config.AUDIO_DIR`` at a temp directory."""
    audio = tmp_path / "audio"
    audio.mkdir()
    monkeypatch.setattr(config, "AUDIO_DIR", audio)
    return audio


def _make_recording(audio_dir: Path, stem: str = "2026-04-24_143208") -> tuple[Path, Path]:
    """Create a fake .flac + .json sidecar pair on disk and return (audio, sidecar)."""
    audio = audio_dir / f"{stem}.flac"
    audio.write_bytes(b"\x00\x01")  # minimal contents — retry_queue doesn't read the audio
    sidecar = audio_dir / f"{stem}.json"
    sidecar.write_text(
        json.dumps(
            {
                "filename": audio.name,
                "duration_sec": 12.5,
                "sample_rate": 16000,
                "channels": 1,
            }
        ),
        encoding="utf-8",
    )
    return audio, sidecar


# ---------------------------------------------------------------------------
# marker_path_for
# ---------------------------------------------------------------------------


class TestMarkerPathFor:
    def test_appends_failed_json_suffix(self, tmp_path: Path) -> None:
        audio = tmp_path / "2026-04-24_143208.flac"
        marker = retry_queue.marker_path_for(audio)
        assert marker.name == "2026-04-24_143208.flac.failed.json"
        assert marker.parent == audio.parent

    def test_handles_unicode_audio_dir(self, tmp_path: Path) -> None:
        d = tmp_path / "🎙 Audio"
        d.mkdir()
        audio = d / "2026-04-24_143208.flac"
        marker = retry_queue.marker_path_for(audio)
        assert marker.parent == d


# ---------------------------------------------------------------------------
# mark_failed
# ---------------------------------------------------------------------------


class TestMarkFailed:
    def test_creates_marker_with_expected_fields(self, tmp_audio_dir: Path) -> None:
        audio, sidecar = _make_recording(tmp_audio_dir)

        marker = retry_queue.mark_failed(audio, sidecar, "boom: api went sideways")

        assert marker.exists()
        data = json.loads(marker.read_text(encoding="utf-8"))
        assert data["audio_path"] == str(audio)
        assert data["sidecar_path"] == str(sidecar)
        assert data["attempts"] == 1
        assert data["last_error"] == "boom: api went sideways"
        assert data["first_failed_at"] == data["last_attempted_at"]
        # ISO 8601 with timezone — datetime.fromisoformat must parse it.
        from datetime import datetime as _dt

        parsed = _dt.fromisoformat(data["first_failed_at"])
        assert parsed.tzinfo is not None

    def test_increments_attempts_on_existing_marker(self, tmp_audio_dir: Path) -> None:
        audio, sidecar = _make_recording(tmp_audio_dir)

        retry_queue.mark_failed(audio, sidecar, "first")
        first = json.loads(retry_queue.marker_path_for(audio).read_text(encoding="utf-8"))

        retry_queue.mark_failed(audio, sidecar, "second")
        second = json.loads(retry_queue.marker_path_for(audio).read_text(encoding="utf-8"))

        retry_queue.mark_failed(audio, sidecar, "third")
        third = json.loads(retry_queue.marker_path_for(audio).read_text(encoding="utf-8"))

        assert first["attempts"] == 1
        assert second["attempts"] == 2
        assert third["attempts"] == 3

        # `first_failed_at` is preserved across updates.
        assert second["first_failed_at"] == first["first_failed_at"]
        assert third["first_failed_at"] == first["first_failed_at"]

        # last_error tracks the most-recent message.
        assert third["last_error"] == "third"
        # last_attempted_at advanced (or at least didn't regress).
        assert third["last_attempted_at"] >= first["last_attempted_at"]

    def test_returns_marker_path(self, tmp_audio_dir: Path) -> None:
        audio, sidecar = _make_recording(tmp_audio_dir)
        marker = retry_queue.mark_failed(audio, sidecar, "x")
        assert marker == retry_queue.marker_path_for(audio)

    def test_atomic_write_no_temp_files_left_behind(
        self, tmp_audio_dir: Path
    ) -> None:
        audio, sidecar = _make_recording(tmp_audio_dir)
        retry_queue.mark_failed(audio, sidecar, "x")
        leftover = list(tmp_audio_dir.glob("*.tmp"))
        assert leftover == []

    def test_recovers_from_corrupt_existing_marker(
        self, tmp_audio_dir: Path
    ) -> None:
        """If the existing marker is malformed JSON, treat it as attempt #1."""
        audio, sidecar = _make_recording(tmp_audio_dir)
        marker = retry_queue.marker_path_for(audio)
        marker.write_text("{not json", encoding="utf-8")

        retry_queue.mark_failed(audio, sidecar, "fresh")
        data = json.loads(marker.read_text(encoding="utf-8"))
        # The corrupt prior marker is unreadable, so we restart at attempt 1.
        assert data["attempts"] == 1
        assert data["last_error"] == "fresh"


# ---------------------------------------------------------------------------
# clear_failed
# ---------------------------------------------------------------------------


class TestClearFailed:
    def test_removes_marker(self, tmp_audio_dir: Path) -> None:
        audio, sidecar = _make_recording(tmp_audio_dir)
        retry_queue.mark_failed(audio, sidecar, "x")
        marker = retry_queue.marker_path_for(audio)
        assert marker.exists()

        retry_queue.clear_failed(audio)
        assert not marker.exists()

    def test_noop_when_marker_missing(self, tmp_audio_dir: Path) -> None:
        audio, _ = _make_recording(tmp_audio_dir)
        # Should not raise even when no marker exists.
        retry_queue.clear_failed(audio)


# ---------------------------------------------------------------------------
# find_pending_retries
# ---------------------------------------------------------------------------


class TestFindPendingRetries:
    def test_empty_dir_returns_empty_list(self, tmp_audio_dir: Path) -> None:
        assert retry_queue.find_pending_retries() == []

    def test_returns_parsed_entries_with_path_objects(
        self, tmp_audio_dir: Path
    ) -> None:
        audio, sidecar = _make_recording(tmp_audio_dir)
        retry_queue.mark_failed(audio, sidecar, "transient")

        results = retry_queue.find_pending_retries()
        assert len(results) == 1
        entry = results[0]
        assert isinstance(entry["audio_path"], Path)
        assert isinstance(entry["sidecar_path"], Path)
        assert isinstance(entry["marker_path"], Path)
        assert entry["audio_path"] == audio
        assert entry["sidecar_path"] == sidecar
        assert entry["attempts"] == 1
        assert entry["last_error"] == "transient"

    def test_skips_markers_referencing_missing_audio(
        self, tmp_audio_dir: Path
    ) -> None:
        audio, sidecar = _make_recording(tmp_audio_dir, stem="2026-04-24_143208")
        retry_queue.mark_failed(audio, sidecar, "x")

        # Delete the audio after marking; the marker should now be skipped.
        audio.unlink()
        results = retry_queue.find_pending_retries()
        assert results == []

    def test_skips_unparseable_marker(self, tmp_audio_dir: Path) -> None:
        # Drop a malformed marker into the directory.
        bad = tmp_audio_dir / "garbage.flac.failed.json"
        bad.write_text("{not json", encoding="utf-8")
        assert retry_queue.find_pending_retries() == []

    def test_returns_multiple_markers_sorted(self, tmp_audio_dir: Path) -> None:
        a, sa = _make_recording(tmp_audio_dir, stem="2026-04-24_120000")
        b, sb = _make_recording(tmp_audio_dir, stem="2026-04-24_130000")
        retry_queue.mark_failed(a, sa, "first")
        retry_queue.mark_failed(b, sb, "second")

        results = retry_queue.find_pending_retries()
        assert len(results) == 2
        # Sorted by glob() — alphabetical — so "120000" comes first.
        assert results[0]["audio_path"] == a
        assert results[1]["audio_path"] == b

    def test_accepts_explicit_audio_dir_override(self, tmp_path: Path) -> None:
        # No monkeypatching of config — pass the dir explicitly.
        d = tmp_path / "alt"
        d.mkdir()
        audio = d / "2026-04-24_140000.flac"
        audio.write_bytes(b"\x00")
        sidecar = d / "2026-04-24_140000.json"
        sidecar.write_text("{}", encoding="utf-8")
        retry_queue.mark_failed(audio, sidecar, "x")

        results = retry_queue.find_pending_retries(audio_dir=d)
        assert len(results) == 1
        assert results[0]["audio_path"] == audio
