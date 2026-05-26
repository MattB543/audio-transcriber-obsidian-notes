"""Tests for the ``obsidian`` package.

Covers:
    * ``writer.write_transcript`` — frontmatter, slug collisions, content
    * ``writer.sanitize_slug`` — kebab rules
    * ``daily_note.append_memo_link`` — create/append/idempotency
    * ``migrate_legacy`` — legacy name parsing + frontmatter generation
    * ``opener.build_obsidian_uri`` / ``open_in_obsidian`` — URI scheme
"""

from __future__ import annotations

import os
import re
import sys
import threading
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

# Make the notes-pipeline root importable when running tests directly.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import config  # noqa: E402
from obsidian import daily_note, opener, writer  # noqa: E402
from obsidian import migrate_legacy  # noqa: E402


# --- fixtures ----------------------------------------------------------------


@pytest.fixture
def vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Redirect config paths at a tmp vault and return the directory map."""
    vault_root = tmp_path / "vault"
    audio_dir = vault_root / "🎙 Audio"
    transcript_dir = audio_dir / "transcriptions"
    daily_dir = vault_root / "Daily Notes"
    for d in (audio_dir, transcript_dir, daily_dir):
        d.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(config, "VAULT_ROOT", vault_root)
    monkeypatch.setattr(config, "AUDIO_DIR", audio_dir)
    monkeypatch.setattr(config, "TRANSCRIPT_DIR", transcript_dir)
    monkeypatch.setattr(config, "DAILY_DIR", daily_dir)

    return {
        "vault": vault_root,
        "audio": audio_dir,
        "transcripts": transcript_dir,
        "daily": daily_dir,
    }


def _rec(timestamp: str = "2026-04-24_143208", duration_sec: float = 187.4) -> dict:
    """Return a minimal RecordingResult-shaped dict."""
    return {
        "audio_path": Path(f"{timestamp}.flac"),
        "sidecar_path": Path(f"{timestamp}.json"),
        "timestamp": timestamp,
        "duration_sec": duration_sec,
        "sample_rate": 16000,
        "channels": 1,
    }


def _tr(**overrides) -> dict:
    """Return a minimal TranscriptResult-shaped dict."""
    base = {
        "raw": "um, so I was, I was thinking we could maybe ship this",
        "cleaned": "I was thinking we could maybe ship this.",
        "slug": "ship-the-thing",
        "title": "Ship The Thing",
        "tags": ["product", "shipping"],
        "duration_sec": 187.4,
        "model_used": "gemini-3-flash-preview",
    }
    base.update(overrides)
    return base


# --- sanitize_slug -----------------------------------------------------------


class TestSanitizeSlug:
    def test_basic_lowercase_kebab(self) -> None:
        assert writer.sanitize_slug("Hello, World!") == "hello-world"

    def test_collapses_dashes_and_strips_edges(self) -> None:
        assert writer.sanitize_slug("---Foo   Bar---") == "foo-bar"

    def test_strips_unicode_and_punctuation(self) -> None:
        assert writer.sanitize_slug("Café — reflections?!") == "caf-reflections"

    def test_truncates_to_max_len(self) -> None:
        out = writer.sanitize_slug("a" * 200)
        assert len(out) <= writer.SLUG_MAX_LEN
        assert out == "a" * writer.SLUG_MAX_LEN

    def test_empty_falls_back_to_untitled(self) -> None:
        assert writer.sanitize_slug("") == "untitled"
        assert writer.sanitize_slug("!!!") == "untitled"

    def test_already_kebab_is_preserved(self) -> None:
        assert writer.sanitize_slug("ship-the-thing") == "ship-the-thing"

    def test_trims_dash_after_truncation(self) -> None:
        # Force a dash at position 50 to verify trailing-dash trimming.
        raw = ("x" * 49) + "-leftover"
        out = writer.sanitize_slug(raw)
        assert not out.endswith("-")
        assert len(out) <= writer.SLUG_MAX_LEN


# --- write_transcript --------------------------------------------------------


class TestWriteTranscript:
    def test_produces_two_files_with_expected_names(self, vault: dict) -> None:
        cleaned, raw = writer.write_transcript(_tr(), _rec())
        assert cleaned == vault["transcripts"] / "2026-04-24_143208_ship-the-thing.md"
        assert raw == vault["transcripts"] / "2026-04-24_143208_ship-the-thing.raw.md"
        assert cleaned.exists()
        assert raw.exists()

    def test_cleaned_frontmatter_contents(self, vault: dict) -> None:
        cleaned, _ = writer.write_transcript(_tr(), _rec())
        text = cleaned.read_text(encoding="utf-8")
        # Frontmatter delimiters and key lines.
        assert text.startswith("---\n")
        assert 'title: "Ship The Thing"' in text
        assert "date: 2026-04-24" in text
        assert 'time: "14:32:08"' in text
        assert 'duration: "3:07"' in text
        assert 'audio: "[[🎙 Audio/2026-04-24_143208.flac]]"' in text
        assert (
            'raw_transcript: "[[🎙 Audio/transcriptions/2026-04-24_143208_ship-the-thing.raw]]"'
            in text
        )
        assert "source: voice-memo" in text
        assert "tags: [voice-memo, product, shipping]" in text
        # Summary was removed from the pipeline; ensure it is NOT emitted.
        assert "summary:" not in text
        assert "## Summary" not in text
        assert "status: captured" in text
        assert "model: gemini-3-flash-preview" in text
        # Body: audio embed + transcript header.
        assert "![[🎙 Audio/2026-04-24_143208.flac]]" in text
        assert "## Transcript" in text
        assert "I was thinking we could maybe ship this." in text
        # LF line endings only.
        assert "\r\n" not in text

    def test_raw_frontmatter_contents(self, vault: dict) -> None:
        _, raw = writer.write_transcript(_tr(), _rec())
        text = raw.read_text(encoding="utf-8")
        assert 'title: "Ship The Thing (raw transcript)"' in text
        assert "source: voice-memo-raw" in text
        assert "tags: [voice-memo-raw]" in text
        assert (
            'cleaned_transcript: "[[🎙 Audio/transcriptions/2026-04-24_143208_ship-the-thing]]"'
            in text
        )
        assert "## Transcript (verbatim)" in text
        assert "um, so I was, I was thinking we could maybe ship this" in text

    def test_utf8_encoding_preserves_emoji(self, vault: dict) -> None:
        cleaned, _ = writer.write_transcript(_tr(), _rec())
        raw_bytes = cleaned.read_bytes()
        assert "🎙".encode("utf-8") in raw_bytes

    def test_slug_collision_appends_numeric_suffix(self, vault: dict) -> None:
        # Pre-create a file with the un-suffixed name for the same timestamp.
        first, _ = writer.write_transcript(_tr(), _rec())
        assert first.name == "2026-04-24_143208_ship-the-thing.md"

        second_cleaned, second_raw = writer.write_transcript(_tr(), _rec())
        assert second_cleaned.name == "2026-04-24_143208_ship-the-thing-2.md"
        assert second_raw.name == "2026-04-24_143208_ship-the-thing-2.raw.md"
        # Raw link in second file should reference its own raw filename.
        text = second_cleaned.read_text(encoding="utf-8")
        assert "2026-04-24_143208_ship-the-thing-2.raw" in text

        third_cleaned, _ = writer.write_transcript(_tr(), _rec())
        assert third_cleaned.name == "2026-04-24_143208_ship-the-thing-3.md"

    def test_duration_zero_rendered_as_0_00(self, vault: dict) -> None:
        cleaned, _ = writer.write_transcript(_tr(), _rec(duration_sec=0))
        assert 'duration: "0:00"' in cleaned.read_text(encoding="utf-8")

    def test_duration_rounds_to_nearest_second(self, vault: dict) -> None:
        cleaned, _ = writer.write_transcript(_tr(), _rec(duration_sec=59.6))
        assert 'duration: "1:00"' in cleaned.read_text(encoding="utf-8")

    def test_invalid_timestamp_raises(self, vault: dict) -> None:
        with pytest.raises(ValueError):
            writer.write_transcript(_tr(), _rec(timestamp="not-a-timestamp"))

    def test_accepts_typeddict_like_objects(self, vault: dict) -> None:
        # Just make sure we don't accidentally require dict-ness.
        class RecObj:
            timestamp = "2026-04-24_143208"
            duration_sec = 10.0

        class TrObj:
            raw = "verbatim"
            cleaned = "clean"
            slug = "attr-based"
            title = "Attr Based"
            tags = ["a"]
            duration_sec = 10.0
            model_used = "m"

        cleaned, _ = writer.write_transcript(TrObj(), RecObj())
        assert cleaned.exists()

    def test_write_transcript_rollback_on_raw_failure(self, vault: dict) -> None:
        """If the raw ``os.replace`` fails AFTER the cleaned one succeeded,
        the cleaned file must be rolled back so the vault never shows a
        cleaned transcript pointing at a non-existent raw file."""
        transcripts = vault["transcripts"]
        cleaned_expected = transcripts / "2026-04-24_143208_ship-the-thing.md"
        raw_expected = transcripts / "2026-04-24_143208_ship-the-thing.raw.md"

        real_replace = os.replace
        calls = {"n": 0}

        def flaky_replace(src, dst, *args, **kwargs):
            calls["n"] += 1
            # First call = cleaned swap (allow). Second call = raw swap (fail).
            if calls["n"] == 2:
                raise OSError("simulated disk-full on raw replace")
            return real_replace(src, dst, *args, **kwargs)

        with patch("obsidian.writer.os.replace", side_effect=flaky_replace):
            with pytest.raises(OSError, match="simulated disk-full"):
                writer.write_transcript(_tr(), _rec())

        # Neither destination file should survive.
        assert not cleaned_expected.exists(), "cleaned file must be rolled back"
        assert not raw_expected.exists(), "raw file must never have landed"

    def test_write_transcript_rollback_on_cleaned_failure(
        self, vault: dict
    ) -> None:
        """If the cleaned ``os.replace`` fails, neither file should exist."""
        transcripts = vault["transcripts"]
        cleaned_expected = transcripts / "2026-04-24_143208_ship-the-thing.md"
        raw_expected = transcripts / "2026-04-24_143208_ship-the-thing.raw.md"

        def failing_replace(src, dst, *args, **kwargs):
            raise OSError("simulated permission-denied on cleaned replace")

        with patch("obsidian.writer.os.replace", side_effect=failing_replace):
            with pytest.raises(OSError, match="simulated permission-denied"):
                writer.write_transcript(_tr(), _rec())

        assert not cleaned_expected.exists()
        assert not raw_expected.exists()

    def test_write_transcript_partial_tmp_cleanup(self, vault: dict) -> None:
        """On any failure, no ``*.tmp`` files should be left behind."""
        transcripts = vault["transcripts"]

        # Case 1: fail on second replace (after first succeeded).
        real_replace = os.replace
        calls = {"n": 0}

        def flaky_replace(src, dst, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("boom")
            return real_replace(src, dst, *args, **kwargs)

        with patch("obsidian.writer.os.replace", side_effect=flaky_replace):
            with pytest.raises(OSError):
                writer.write_transcript(_tr(), _rec())

        leftover_tmps = list(transcripts.glob("*.tmp"))
        assert leftover_tmps == [], f"unexpected tmp leftovers: {leftover_tmps}"

        # Case 2: fail on first replace (before anything lands).
        def always_fail_replace(src, dst, *args, **kwargs):
            raise OSError("boom")

        with patch("obsidian.writer.os.replace", side_effect=always_fail_replace):
            with pytest.raises(OSError):
                writer.write_transcript(_tr(), _rec())

        leftover_tmps = list(transcripts.glob("*.tmp"))
        assert leftover_tmps == [], f"unexpected tmp leftovers: {leftover_tmps}"

        # Case 3: fail while writing the second tmp file (before any replace).
        real_mkstemp = writer.tempfile.mkstemp
        mkstemp_calls = {"n": 0}

        def flaky_mkstemp(*args, **kwargs):
            mkstemp_calls["n"] += 1
            if mkstemp_calls["n"] == 2:
                raise OSError("simulated tmp-create failure")
            return real_mkstemp(*args, **kwargs)

        with patch("obsidian.writer.tempfile.mkstemp", side_effect=flaky_mkstemp):
            with pytest.raises(OSError):
                writer.write_transcript(_tr(), _rec())

        leftover_tmps = list(transcripts.glob("*.tmp"))
        assert leftover_tmps == [], f"unexpected tmp leftovers: {leftover_tmps}"


# --- wikilink folder is config-derived (B2 regression) -----------------------


class TestWikilinkFolderFollowsConfig:
    """The ``[[...]]`` wikilink folder MUST be derived from config so links
    point at the same vault-relative location files are actually written to.

    Previously the folder was the frozen literal ``🎙 Audio`` while the default
    config wrote files under ``Audio`` — every non-author user got broken
    links. These tests pin the derivation: change the configured audio /
    transcript subdirs and the links must follow.
    """

    @pytest.fixture
    def plain_vault(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> dict[str, Path]:
        """A vault using the *default* (non-emoji) ``Audio`` subdir layout."""
        vault_root = tmp_path / "vault"
        audio_dir = vault_root / "Audio"
        transcript_dir = audio_dir / "transcriptions"
        daily_dir = vault_root / "Daily Notes"
        for d in (audio_dir, transcript_dir, daily_dir):
            d.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(config, "VAULT_ROOT", vault_root)
        monkeypatch.setattr(config, "AUDIO_DIR", audio_dir)
        monkeypatch.setattr(config, "TRANSCRIPT_DIR", transcript_dir)
        monkeypatch.setattr(config, "DAILY_DIR", daily_dir)
        return {"transcripts": transcript_dir, "daily": daily_dir}

    def test_helpers_derive_from_config(self, plain_vault: dict) -> None:
        assert writer._audio_vault_folder() == "Audio"
        assert writer._transcriptions_vault_subfolder() == "transcriptions"

    def test_cleaned_links_use_config_folder_not_hardcoded_emoji(
        self, plain_vault: dict
    ) -> None:
        cleaned, _ = writer.write_transcript(_tr(), _rec())
        text = cleaned.read_text(encoding="utf-8")
        assert 'audio: "[[Audio/2026-04-24_143208.flac]]"' in text
        assert (
            'raw_transcript: "[[Audio/transcriptions/'
            '2026-04-24_143208_ship-the-thing.raw]]"' in text
        )
        assert "![[Audio/2026-04-24_143208.flac]]" in text
        # The old hardcoded emoji folder must NOT appear.
        assert "🎙 Audio" not in text

    def test_daily_note_link_uses_config_folder(self, plain_vault: dict) -> None:
        path = daily_note.append_memo_link(
            "2026-04-24_143208", "ship-the-thing", "Ship The Thing", 187.4
        )
        text = path.read_text(encoding="utf-8")
        assert (
            "[[Audio/transcriptions/2026-04-24_143208_ship-the-thing|"
            "Ship The Thing]]" in text
        )
        assert "🎙 Audio" not in text

    def test_custom_audio_subdir_is_reflected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A custom NOTES_AUDIO_SUBDIR (e.g. an emoji folder) flows into links."""
        vault_root = tmp_path / "vault"
        audio_dir = vault_root / "🎙 Memos"
        transcript_dir = audio_dir / "txt"
        transcript_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(config, "VAULT_ROOT", vault_root)
        monkeypatch.setattr(config, "AUDIO_DIR", audio_dir)
        monkeypatch.setattr(config, "TRANSCRIPT_DIR", transcript_dir)

        cleaned, _ = writer.write_transcript(_tr(), _rec())
        text = cleaned.read_text(encoding="utf-8")
        assert 'audio: "[[🎙 Memos/2026-04-24_143208.flac]]"' in text
        assert "🎙 Memos/txt/2026-04-24_143208_ship-the-thing.raw" in text

    def test_falls_back_when_audio_dir_outside_vault(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If AUDIO_DIR isn't under VAULT_ROOT we degrade to the fallback name
        rather than crashing on relative_to()."""
        monkeypatch.setattr(config, "VAULT_ROOT", tmp_path / "vault")
        monkeypatch.setattr(config, "AUDIO_DIR", tmp_path / "elsewhere" / "Audio")
        assert writer._audio_vault_folder() == writer._AUDIO_VAULT_FOLDER_FALLBACK


# --- write_placeholder + finalize_transcript ---------------------------------


class TestWritePlaceholder:
    def test_creates_pending_file_with_processing_content(
        self, vault: dict
    ) -> None:
        """``write_placeholder`` lands a ``<timestamp>_pending.md`` stub
        with status=transcribing YAML and a "Transcribing..." body."""
        ts = "2026-04-24_143208"
        audio = vault["audio"] / f"{ts}.flac"
        audio.write_bytes(b"fLaC\x00")

        path = writer.write_placeholder(ts, audio, duration_sec=42.5)

        assert path == vault["transcripts"] / "2026-04-24_143208_pending.md"
        assert path.exists()
        text = path.read_text(encoding="utf-8")

        # Frontmatter sanity.
        assert text.startswith("---\n")
        assert 'title: "Processing..."' in text
        assert "date: 2026-04-24" in text
        assert 'time: "14:32:08"' in text
        assert 'duration: "0:42"' in text
        assert 'audio: "[[🎙 Audio/2026-04-24_143208.flac]]"' in text
        assert "tags: [voice-memo, processing]" in text
        assert "status: transcribing" in text
        # Body cues.
        assert "## Transcribing..." in text
        assert "auto-update" in text or "auto‑update" in text
        # LF-only.
        assert "\r\n" not in text

    def test_overwrites_existing_pending_file(self, vault: dict) -> None:
        """A second call for the same timestamp overwrites the first
        placeholder (timestamps are unique per recording, but if the same
        timestamp comes around again — e.g. after a retry — we want the
        latest "Processing..." stub to win)."""
        ts = "2026-04-24_143208"
        audio = vault["audio"] / f"{ts}.flac"
        audio.write_bytes(b"fLaC\x00")
        first = writer.write_placeholder(ts, audio, duration_sec=10.0)
        first.write_text(first.read_text() + "\nLEFTOVER", encoding="utf-8")
        second = writer.write_placeholder(ts, audio, duration_sec=20.0)
        assert first == second
        text = second.read_text(encoding="utf-8")
        assert "LEFTOVER" not in text
        assert 'duration: "0:20"' in text

    def test_invalid_timestamp_raises(self, vault: dict) -> None:
        with pytest.raises(ValueError):
            writer.write_placeholder("not-a-timestamp", Path("x.flac"))


class TestFinalizeTranscript:
    def test_overwrites_placeholder_then_renames(self, vault: dict) -> None:
        """``finalize_transcript`` must:
          * remove the ``<timestamp>_pending.md`` file
          * land a ``<timestamp>_<slug>.md`` cleaned file with the same
            content shape ``write_transcript`` would have produced
          * land a ``<timestamp>_<slug>.raw.md`` raw file
        """
        ts = "2026-04-24_143208"
        audio = vault["audio"] / f"{ts}.flac"
        audio.write_bytes(b"fLaC\x00")

        placeholder = writer.write_placeholder(ts, audio, duration_sec=187.4)
        assert placeholder.exists()

        cleaned, raw = writer.finalize_transcript(placeholder, _tr(), _rec())

        # Placeholder gone (renamed onto the cleaned path).
        assert not placeholder.exists()

        # Final paths and existence.
        assert cleaned == vault["transcripts"] / "2026-04-24_143208_ship-the-thing.md"
        assert raw == vault["transcripts"] / "2026-04-24_143208_ship-the-thing.raw.md"
        assert cleaned.exists()
        assert raw.exists()

        cleaned_text = cleaned.read_text(encoding="utf-8")
        # Real transcript content (not the "Processing..." stub).
        assert 'title: "Ship The Thing"' in cleaned_text
        assert "status: captured" in cleaned_text
        assert "I was thinking we could maybe ship this." in cleaned_text
        # No leftover placeholder fields.
        assert "Transcribing..." not in cleaned_text
        assert "status: transcribing" not in cleaned_text

        # Raw file matches write_transcript's shape.
        raw_text = raw.read_text(encoding="utf-8")
        assert 'title: "Ship The Thing (raw transcript)"' in raw_text
        assert "## Transcript (verbatim)" in raw_text

    def test_handles_collision_with_existing_final_file(
        self, vault: dict
    ) -> None:
        """If ``<timestamp>_<slug>.md`` already exists (e.g. a previous
        recording happened to land with the same slug), finalize must use
        ``-2`` (and bump from there)."""
        ts = "2026-04-24_143208"
        audio = vault["audio"] / f"{ts}.flac"
        audio.write_bytes(b"fLaC\x00")

        # Pre-existing collision file with the un-suffixed name.
        pre_existing = vault["transcripts"] / "2026-04-24_143208_ship-the-thing.md"
        pre_existing.write_text("preexisting", encoding="utf-8")

        placeholder = writer.write_placeholder(ts, audio, duration_sec=10.0)
        cleaned, raw = writer.finalize_transcript(placeholder, _tr(), _rec())

        assert cleaned.name == "2026-04-24_143208_ship-the-thing-2.md"
        assert raw.name == "2026-04-24_143208_ship-the-thing-2.raw.md"
        assert cleaned.exists()
        assert raw.exists()
        # Collision file untouched.
        assert pre_existing.read_text(encoding="utf-8") == "preexisting"
        # Placeholder gone.
        assert not placeholder.exists()

    def test_falls_back_to_write_transcript_when_placeholder_missing(
        self, vault: dict
    ) -> None:
        """If the placeholder vanished (user deleted it, etc.), finalize
        should still produce the two output files via the legacy path."""
        bogus = vault["transcripts"] / "2026-04-24_143208_pending.md"
        # Don't write the placeholder; simulate the "user deleted it" case.
        cleaned, raw = writer.finalize_transcript(bogus, _tr(), _rec())
        assert cleaned.exists()
        assert raw.exists()
        assert cleaned.name == "2026-04-24_143208_ship-the-thing.md"

    def test_write_placeholder_error_overwrites_with_failure_message(
        self, vault: dict
    ) -> None:
        """``write_placeholder_error`` rewrites the placeholder so the
        user's open Obsidian tab shows the failure inline."""
        ts = "2026-04-24_143208"
        audio = vault["audio"] / f"{ts}.flac"
        audio.write_bytes(b"fLaC\x00")
        placeholder = writer.write_placeholder(ts, audio, duration_sec=12.0)

        writer.write_placeholder_error(
            placeholder,
            RuntimeError("Quota exceeded for project 'foo'"),
            timestamp=ts,
            duration_sec=12.0,
        )

        text = placeholder.read_text(encoding="utf-8")
        assert 'title: "Transcription failed"' in text
        assert "status: failed" in text
        assert "tags: [voice-memo, failed]" in text
        assert "## Transcription failed" in text
        assert "Quota exceeded for project 'foo'" in text
        # The original "Processing..." banner should be gone.
        assert "Transcribing..." not in text


# --- append_memo_link --------------------------------------------------------


class TestAppendMemoLink:
    def test_creates_daily_note_when_missing(self, vault: dict) -> None:
        path = daily_note.append_memo_link(
            "2026-04-24_143208", "ship-the-thing", "Ship The Thing", 187.4
        )
        assert path == vault["daily"] / "2026-04-24.md"
        text = path.read_text(encoding="utf-8")
        assert text.startswith("---\ndate: 2026-04-24\ntags: [daily]\n---\n")
        assert "## Voice Memos" in text
        assert (
            "- 14:32 [[🎙 Audio/transcriptions/2026-04-24_143208_ship-the-thing|Ship The Thing]] (3:07)"
            in text
        )

    def test_appends_under_existing_heading(self, vault: dict) -> None:
        # Pre-populate a daily note with an existing Voice Memos section
        # and an unrelated trailing section.
        p = vault["daily"] / "2026-04-24.md"
        p.write_text(
            "---\ndate: 2026-04-24\ntags: [daily]\n---\n\n"
            "## Voice Memos\n"
            "- 09:00 [[🎙 Audio/transcriptions/2026-04-24_090000_morning|Morning]] (1:00)\n"
            "\n"
            "## Tasks\n"
            "- buy milk\n",
            encoding="utf-8",
        )
        daily_note.append_memo_link(
            "2026-04-24_143208", "ship-the-thing", "Ship The Thing", 187.4
        )
        text = p.read_text(encoding="utf-8")
        # The new entry lives under Voice Memos, above Tasks.
        voice_idx = text.find("## Voice Memos")
        tasks_idx = text.find("## Tasks")
        assert voice_idx < text.find("Morning") < tasks_idx
        assert voice_idx < text.find("Ship The Thing") < tasks_idx
        # Tasks section is preserved.
        assert "- buy milk" in text

    def test_case_insensitive_heading_match(self, vault: dict) -> None:
        p = vault["daily"] / "2026-04-24.md"
        p.write_text(
            "---\ndate: 2026-04-24\ntags: [daily]\n---\n\n"
            "## voice memos\n"
            "- 09:00 [[🎙 Audio/transcriptions/2026-04-24_090000_m|M]] (1:00)\n",
            encoding="utf-8",
        )
        daily_note.append_memo_link(
            "2026-04-24_143208", "ship-the-thing", "Ship The Thing", 187.4
        )
        text = p.read_text(encoding="utf-8")
        # No second Voice Memos heading was added.
        assert len(re.findall(r"(?im)^##\s+voice\s+memos\s*$", text)) == 1
        assert "Ship The Thing" in text

    def test_is_idempotent(self, vault: dict) -> None:
        first = daily_note.append_memo_link(
            "2026-04-24_143208", "ship-the-thing", "Ship The Thing", 187.4
        )
        before = first.read_text(encoding="utf-8")
        second = daily_note.append_memo_link(
            "2026-04-24_143208", "ship-the-thing", "Ship The Thing (again)", 187.4
        )
        after = second.read_text(encoding="utf-8")
        assert first == second
        # File unchanged on second call.
        assert before == after
        # Only one bullet.
        assert after.count("2026-04-24_143208_ship-the-thing") == 1

    def test_appends_heading_when_absent(self, vault: dict) -> None:
        p = vault["daily"] / "2026-04-24.md"
        p.write_text(
            "---\ndate: 2026-04-24\ntags: [daily]\n---\n\n"
            "Some random earlier journaling.\n",
            encoding="utf-8",
        )
        daily_note.append_memo_link(
            "2026-04-24_143208", "ship-the-thing", "Ship The Thing", 187.4
        )
        text = p.read_text(encoding="utf-8")
        assert "Some random earlier journaling." in text
        assert "## Voice Memos" in text
        assert "Ship The Thing" in text

    def test_preserves_existing_content_verbatim(self, vault: dict) -> None:
        p = vault["daily"] / "2026-04-24.md"
        original = (
            "---\ndate: 2026-04-24\ntags: [daily]\n---\n"
            "\n"
            "## Voice Memos\n"
            "- 09:00 [[🎙 Audio/transcriptions/2026-04-24_090000_morning|Morning]] (1:00)\n"
        )
        p.write_text(original, encoding="utf-8")
        daily_note.append_memo_link(
            "2026-04-24_143208", "new-one", "New One", 60.0
        )
        text = p.read_text(encoding="utf-8")
        # Original lines must still be present, unmodified.
        for line in original.splitlines():
            assert line in text

    def test_uses_lf_line_endings(self, vault: dict) -> None:
        path = daily_note.append_memo_link(
            "2026-04-24_143208", "ship-the-thing", "Ship The Thing", 187.4
        )
        assert b"\r\n" not in path.read_bytes()

    def test_daily_note_concurrent_append_preserves_both_entries(
        self, vault: dict
    ) -> None:
        """Two threads appending to the same daily note must both land.

        Without the module-level ``_DAILY_NOTE_LOCK`` this race is flaky —
        both threads read the same file, each appends their bullet, and
        whichever ``os.replace`` lands second silently drops the other's
        entry. We add a small artificial delay inside the read step (which
        sits inside the critical section) to make any missing-lock
        regression fail reliably.
        """
        # Sanity check: if the lock ever gets removed/renamed, this test
        # should fail loudly rather than silently stop exercising the race.
        lock_obj = daily_note._DAILY_NOTE_LOCK
        assert hasattr(lock_obj, "acquire") and hasattr(lock_obj, "release")

        import time

        real_read_text = Path.read_text

        def slow_read_text(self, *args, **kwargs):
            # Only slow down reads of the daily note file itself; leaving
            # the read open for a noticeable window makes a missing-lock
            # regression fail reliably (both threads would observe the
            # same pre-write content and clobber each other).
            if self.name.endswith("2026-04-24.md"):
                time.sleep(0.1)
            return real_read_text(self, *args, **kwargs)

        errors: list[BaseException] = []

        def worker(ts: str, slug: str, title: str) -> None:
            try:
                daily_note.append_memo_link(ts, slug, title, 60.0)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        with patch.object(Path, "read_text", slow_read_text):
            t1 = threading.Thread(
                target=worker,
                args=("2026-04-24_090000", "first-memo", "First Memo"),
            )
            t2 = threading.Thread(
                target=worker,
                args=("2026-04-24_143208", "second-memo", "Second Memo"),
            )
            t1.start()
            # Start t2 almost immediately after t1 so their critical
            # sections would overlap in the absence of the lock.
            t2.start()
            t1.join(timeout=10)
            t2.join(timeout=10)

        assert not errors, f"worker raised: {errors!r}"
        assert not t1.is_alive() and not t2.is_alive()

        final_path = vault["daily"] / "2026-04-24.md"
        text = final_path.read_text(encoding="utf-8")
        # Both entries must survive the race.
        assert "2026-04-24_090000_first-memo" in text, (
            "first memo link missing — race dropped it"
        )
        assert "2026-04-24_143208_second-memo" in text, (
            "second memo link missing — race dropped it"
        )
        assert "First Memo" in text
        assert "Second Memo" in text
        # Exactly one Voice Memos heading in the final file.
        assert (
            len(re.findall(r"(?im)^##\s+voice\s+memos\s*$", text)) == 1
        ), "heading was duplicated by the racing writers"


# --- migrate_legacy ----------------------------------------------------------


class TestMigrateLegacy:
    def _seed_legacy(self, vault: dict, name: str, body: str) -> Path:
        p = vault["transcripts"] / name
        p.write_text(body, encoding="utf-8")
        return p

    def test_parses_7_8_25_timestamp(self) -> None:
        parsed = migrate_legacy._parse_legacy_name("7-8-25_22.33.md")
        assert parsed == ("2025-07-08", "22:33:00", "2025-07-08_223300")

    def test_parses_two_digit_fields(self) -> None:
        assert migrate_legacy._parse_legacy_name("10-15-25_17.54.md") == (
            "2025-10-15",
            "17:54:00",
            "2025-10-15_175400",
        )

    def test_rejects_new_style_filenames(self) -> None:
        assert migrate_legacy._parse_legacy_name("2026-04-24_143208_ship.md") is None

    def test_rejects_bad_dates(self) -> None:
        assert migrate_legacy._parse_legacy_name("13-40-25_25.99.md") is None

    def test_camel_to_kebab(self) -> None:
        assert migrate_legacy._camel_to_kebab("CamelCase") == "camel-case"
        assert migrate_legacy._camel_to_kebab("ABCFoo") == "abc-foo"
        assert migrate_legacy._camel_to_kebab("snake_case_thing") == "snake-case-thing"
        assert migrate_legacy._camel_to_kebab("already-kebab") == "already-kebab"

    def test_extract_legacy_tags(self) -> None:
        body = (
            "#Obsidian  \n"
            "#NoteTaking  \n"
            "#SyncTest  \n"
            "\n"
            "This is the transcript body.\n"
        )
        tags, remaining = migrate_legacy._extract_legacy_tags(body)
        assert tags == ["obsidian", "note-taking", "sync-test"]
        assert remaining == "This is the transcript body.\n"

    def test_plan_migration_dry_run(self, vault: dict) -> None:
        self._seed_legacy(
            vault,
            "7-8-25_22.33.md",
            "#Obsidian\n#NoteTaking\n\nHello.\n",
        )
        plan = migrate_legacy.plan_migration(vault["transcripts"], vault["audio"])
        assert len(plan) == 1
        item = plan[0]
        assert item.src_path == vault["transcripts"] / "7-8-25_22.33.md"
        assert item.dst_path == vault["transcripts"] / "2025-07-08_223300.md"
        assert item.iso_date == "2025-07-08"
        assert item.iso_time == "22:33:00"
        assert item.new_stem == "2025-07-08_223300"
        assert item.audio_src is None

    def test_plan_picks_up_matching_audio(self, vault: dict) -> None:
        self._seed_legacy(vault, "7-8-25_22.33.md", "#Tag\n\nbody\n")
        audio = vault["audio"] / "7-8-25_22.33.mp4"
        audio.write_bytes(b"\x00\x01")
        plan = migrate_legacy.plan_migration(vault["transcripts"], vault["audio"])
        item = plan[0]
        assert item.audio_src == audio
        assert item.audio_dst == vault["audio"] / "2025-07-08_223300.mp4"

    def test_apply_renames_transcript_and_audio(self, vault: dict) -> None:
        self._seed_legacy(
            vault,
            "7-8-25_22.33.md",
            "#Obsidian  \n#NoteTaking  \n\nHello world.\n",
        )
        audio = vault["audio"] / "7-8-25_22.33.mp4"
        audio.write_bytes(b"AUDIO")

        exit_code = migrate_legacy.main(
            [
                "--apply",
                "--transcripts",
                str(vault["transcripts"]),
                "--audio",
                str(vault["audio"]),
            ]
        )
        assert exit_code == 0

        # Source transcript is gone, destination exists.
        assert not (vault["transcripts"] / "7-8-25_22.33.md").exists()
        new_path = vault["transcripts"] / "2025-07-08_223300.md"
        assert new_path.exists()

        text = new_path.read_text(encoding="utf-8")
        assert text.startswith("---\n")
        assert 'title: "Legacy transcript 2025-07-08"' in text
        assert "date: 2025-07-08" in text
        assert 'time: "22:33:00"' in text
        assert "source: voice-memo-legacy" in text
        assert "migrated_from: 7-8-25_22.33.md" in text
        # Extracted tags land inline after the base ones.
        assert "voice-memo" in text
        assert "voice-memo-legacy" in text
        assert "obsidian" in text
        assert "note-taking" in text
        # Body preserved after hashtag block.
        assert "Hello world." in text

        # Audio got renamed.
        assert not audio.exists()
        assert (vault["audio"] / "2025-07-08_223300.mp4").exists()

    def test_dry_run_is_noop(self, vault: dict, capsys: pytest.CaptureFixture) -> None:
        src = self._seed_legacy(vault, "7-8-25_22.33.md", "#Tag\n\nbody\n")
        audio = vault["audio"] / "7-8-25_22.33.mp4"
        audio.write_bytes(b"A")
        before = src.read_text(encoding="utf-8")

        exit_code = migrate_legacy.main(
            [
                "--transcripts",
                str(vault["transcripts"]),
                "--audio",
                str(vault["audio"]),
            ]
        )
        assert exit_code == 0

        # Source is untouched.
        assert src.exists()
        assert src.read_text(encoding="utf-8") == before
        assert audio.exists()

        out = capsys.readouterr().out
        assert "WOULD RENAME" in out
        assert "Would rename 1 files" in out

    def test_no_legacy_files_prints_message(
        self, vault: dict, capsys: pytest.CaptureFixture
    ) -> None:
        exit_code = migrate_legacy.main(
            [
                "--transcripts",
                str(vault["transcripts"]),
                "--audio",
                str(vault["audio"]),
            ]
        )
        assert exit_code == 0
        out = capsys.readouterr().out
        assert "No legacy transcripts found" in out


# --- opener ------------------------------------------------------------------


class TestObsidianOpener:
    def test_uri_uses_vault_folder_name(self, vault: dict) -> None:
        md = vault["transcripts"] / "2026-04-24_143208_ship-the-thing.md"
        md.write_text("x", encoding="utf-8")
        uri = opener.build_obsidian_uri(md)
        # vault[vault] is a tmp_path / "vault"
        assert uri.startswith("obsidian://open?vault=vault&file=")

    def test_uri_strips_md_suffix(self, vault: dict) -> None:
        md = vault["transcripts"] / "2026-04-24_143208_ship-the-thing.md"
        md.write_text("x", encoding="utf-8")
        uri = opener.build_obsidian_uri(md)
        # The encoded file portion should NOT include `.md`.
        assert ".md" not in uri

    def test_uri_url_encodes_emoji_and_spaces(self, vault: dict) -> None:
        md = vault["transcripts"] / "2026-04-24_143208_ship-the-thing.md"
        md.write_text("x", encoding="utf-8")
        uri = opener.build_obsidian_uri(md)
        # The audio folder is "🎙 Audio" — the emoji + space must be percent-encoded.
        assert "🎙" not in uri
        assert " " not in uri  # spaces become %20
        # Forward slashes between path components must be encoded too (we used
        # safe="" via urllib.parse.quote).
        assert "/" not in uri.split("&file=", 1)[1]

    def test_uri_with_explicit_vault_root(self, tmp_path: Path) -> None:
        vault_root = tmp_path / "obsidian_backup"
        (vault_root / "subdir").mkdir(parents=True)
        md = vault_root / "subdir" / "Note.md"
        md.write_text("x", encoding="utf-8")

        uri = opener.build_obsidian_uri(md, vault_root=vault_root)
        assert uri == "obsidian://open?vault=obsidian_backup&file=subdir%2FNote"

    def test_open_in_obsidian_calls_os_startfile_with_uri(
        self, vault: dict
    ) -> None:
        md = vault["transcripts"] / "2026-04-24_143208_ship-the-thing.md"
        md.write_text("x", encoding="utf-8")

        with patch("obsidian.opener.os.startfile", create=True) as fake_start:
            opener.open_in_obsidian(md)

        fake_start.assert_called_once()
        called_uri = fake_start.call_args.args[0]
        assert called_uri.startswith("obsidian://open?vault=")
        assert "ship-the-thing" in called_uri

    def test_open_in_obsidian_swallows_errors(self, vault: dict) -> None:
        md = vault["transcripts"] / "2026-04-24_143208_ship-the-thing.md"
        md.write_text("x", encoding="utf-8")

        # Force os.startfile to fail; opener must NOT raise.
        with patch(
            "obsidian.opener.os.startfile",
            create=True,
            side_effect=OSError("no protocol handler"),
        ):
            opener.open_in_obsidian(md)  # must not raise

    def test_open_in_obsidian_swallows_outside_vault_path(
        self, tmp_path: Path
    ) -> None:
        """If md_path can't be made relative to VAULT_ROOT, log and return."""
        # Path that's not inside VAULT_ROOT — relative_to() will raise.
        outside = tmp_path / "outside.md"
        outside.write_text("x", encoding="utf-8")

        # No exception should bubble out.
        with patch("obsidian.opener.os.startfile", create=True) as fake_start:
            opener.open_in_obsidian(outside)
        # And we shouldn't try to launch anything when URI building fails.
        fake_start.assert_not_called()


# --- _yaml_escape (regression: control chars must not break frontmatter) -----


def _split_frontmatter(text: str) -> dict:
    """Parse the YAML frontmatter block from a markdown file.

    Mirrors the publisher's split logic just enough to verify the file is
    well-formed: the frontmatter must start at the first line with ``---``
    and end at the next ``---`` line. Returns the parsed mapping.
    """
    assert text.startswith("---\n"), "frontmatter must start at first line"
    end = text.find("\n---\n", 4)
    assert end != -1, "frontmatter terminator '---' not found"
    fm_block = text[4:end]
    return yaml.safe_load(fm_block)


class TestYamlEscape:
    def test_handles_newline(self) -> None:
        # Literal newline in input → escaped \n (two chars: backslash + n).
        out = writer._yaml_escape("line one\nline two")
        assert out == "line one\\nline two"
        # Round-trip via YAML must succeed and restore the original newline.
        parsed = yaml.safe_load(f'value: "{out}"')
        assert parsed == {"value": "line one\nline two"}

    def test_handles_carriage_return(self) -> None:
        out = writer._yaml_escape("a\rb")
        assert out == "a\\rb"
        parsed = yaml.safe_load(f'value: "{out}"')
        assert parsed == {"value": "a\rb"}

    def test_handles_tab(self) -> None:
        out = writer._yaml_escape("a\tb")
        assert out == "a\\tb"
        parsed = yaml.safe_load(f'value: "{out}"')
        assert parsed == {"value": "a\tb"}

    def test_strips_control_chars(self) -> None:
        # NUL and other C0 controls (other than \t \r \n) get stripped.
        out = writer._yaml_escape("hello\x00world\x01\x1f!")
        assert out == "helloworld!"

    def test_preserves_existing_backslash_and_quote_handling(self) -> None:
        # The original behaviour for backslashes and double quotes
        # must still work.
        assert writer._yaml_escape('a"b\\c') == 'a\\"b\\\\c'

    def test_combined_special_chars(self) -> None:
        out = writer._yaml_escape('quote " backslash \\ newline \n tab \t')
        # Each special is escaped exactly once.
        parsed = yaml.safe_load(f'value: "{out}"')
        assert parsed == {"value": 'quote " backslash \\ newline \n tab \t'}


class TestWriteTranscriptYamlSafety:
    def test_write_transcript_with_multiline_title_in_cleaned_produces_valid_yaml(
        self, vault: dict
    ) -> None:
        """A multiline title must not break the frontmatter (defensive: even
        though the LLM is no longer asked for titles, the YAML escape must
        still survive whatever we pass in)."""
        cleaned, raw = writer.write_transcript(
            _tr(title="line one\nline two"), _rec()
        )
        text = cleaned.read_text(encoding="utf-8")
        # The raw newline inside the YAML scalar must have been escaped.
        assert 'title: "line one\\nline two"' in text

        # The full frontmatter must parse as valid YAML.
        fm = _split_frontmatter(text)
        assert fm["title"] == "line one\nline two"
        # Other expected fields survived too.
        assert fm["status"] == "captured"

    def test_write_transcript_with_multiline_title_produces_valid_yaml(
        self, vault: dict
    ) -> None:
        """A title with \\r\\n / \\t / control char also stays well-formed."""
        cleaned, _ = writer.write_transcript(
            _tr(title="weird\r\nname\twith\x00null"), _rec()
        )
        text = cleaned.read_text(encoding="utf-8")
        fm = _split_frontmatter(text)
        # \x00 stripped, \r/\n/\t round-trip via the YAML scalar.
        assert fm["title"] == "weird\r\nname\twithnull"

    def test_migrate_legacy_uses_shared_yaml_escape(self) -> None:
        """``migrate_legacy`` should reuse ``writer._yaml_escape``."""
        assert migrate_legacy._yaml_escape is writer._yaml_escape
