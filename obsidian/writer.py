"""Write cleaned + raw transcript markdown files into the Obsidian vault.

The two files are cross-linked via their YAML Properties so Obsidian shows
bidirectional references. Filenames follow ``<timestamp>_<slug>.md`` and
``<timestamp>_<slug>.raw.md``. Both files are written as UTF-8 with LF
line endings using an atomic ``write-temp-then-replace`` strategy so that
a partially-written file never surfaces in the vault.

For the "open Obsidian immediately" UX flow, a two-step write is exposed:
    1. ``write_placeholder`` — emits a ``<timestamp>_pending.md`` stub that
       can be opened in Obsidian *before* transcription completes, so the
       user sees a "Processing..." note within ~100 ms of stopping a memo.
    2. ``finalize_transcript`` — once transcription returns, overwrites the
       placeholder file in-place with the real cleaned content, then
       atomically renames it to ``<timestamp>_<slug>.md``. Obsidian's
       open-tab follows the rename gracefully (file watchers update the
       tab title without closing the editor). The raw verbatim file is
       written as a sibling.

Public API:
    write_transcript(tr, rec) -> tuple[Path, Path]
    write_placeholder(timestamp, audio_path, duration_sec) -> Path
    finalize_transcript(placeholder_path, tr, rec) -> tuple[Path, Path]
    write_placeholder_error(placeholder_path, exc) -> None
    sanitize_slug(raw) -> str
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

import config

if TYPE_CHECKING:  # pragma: no cover - typing only
    # These live in sibling packages. We don't import them at runtime so
    # that the obsidian package remains usable even if those modules
    # haven't been implemented yet (and to avoid circular imports).
    from recorder.recorder import RecordingResult
    from transcribe.transcribe import TranscriptResult

logger = logging.getLogger(__name__)

# --- constants ---------------------------------------------------------------

#: Maximum length of a slug after sanitization.
SLUG_MAX_LEN: int = 50

#: Folder name inside the vault that holds audio files. Kept as a module
#: constant so tests / callers can see the exact unicode used.
AUDIO_VAULT_FOLDER: str = "🎙 Audio"

#: Folder inside ``AUDIO_VAULT_FOLDER`` that holds transcripts.
TRANSCRIPTIONS_VAULT_SUBFOLDER: str = "transcriptions"

_SLUG_ALLOWED_RE = re.compile(r"[^a-z0-9-]+")
_SLUG_MULTIDASH_RE = re.compile(r"-{2,}")
_TIMESTAMP_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_(\d{2})(\d{2})(\d{2})$")


# --- helpers -----------------------------------------------------------------


def sanitize_slug(raw: str) -> str:
    """Return a filesystem-safe, kebab-case slug.

    Rules:
      * lowercase
      * replace any run of non ``[a-z0-9-]`` characters with a single ``-``
      * collapse multiple dashes
      * strip leading/trailing dashes
      * truncate to ``SLUG_MAX_LEN`` characters (trimming trailing dashes)

    Empty or pathological inputs return ``"untitled"``.
    """
    if raw is None:
        return "untitled"
    s = raw.strip().lower()
    # Replace anything that isn't [a-z0-9-] with a dash.
    s = _SLUG_ALLOWED_RE.sub("-", s)
    # Collapse runs of dashes.
    s = _SLUG_MULTIDASH_RE.sub("-", s)
    s = s.strip("-")
    if len(s) > SLUG_MAX_LEN:
        s = s[:SLUG_MAX_LEN].rstrip("-")
    return s or "untitled"


def _parse_timestamp(timestamp: str) -> tuple[str, str]:
    """Split ``2026-04-24_143208`` into (``2026-04-24``, ``14:32:08``).

    Raises ``ValueError`` if the input doesn't match the expected pattern.
    """
    m = _TIMESTAMP_RE.match(timestamp)
    if not m:
        raise ValueError(
            f"Invalid timestamp {timestamp!r}; expected 'YYYY-MM-DD_HHMMSS'"
        )
    date_part, hh, mm, ss = m.group(1), m.group(2), m.group(3), m.group(4)
    return date_part, f"{hh}:{mm}:{ss}"


def _format_duration(duration_sec: float) -> str:
    """Render a duration in ``M:SS`` form (zero-padded seconds, no cap on M)."""
    total = int(round(float(duration_sec)))
    if total < 0:
        total = 0
    minutes, seconds = divmod(total, 60)
    return f"{minutes}:{seconds:02d}"


def _resolve_slug_filenames(
    transcript_dir: Path, timestamp: str, base_slug: str
) -> tuple[str, str, str]:
    """Resolve a non-colliding (stem, cleaned_name, raw_name) for the slug.

    The stem returned is ``<timestamp>_<slug>`` — with ``-2``, ``-3``, ...
    suffixes appended to the slug until BOTH the cleaned and raw filenames
    are free.
    """
    slug = base_slug or "untitled"
    suffix = 1
    while True:
        candidate_slug = slug if suffix == 1 else f"{slug}-{suffix}"
        stem = f"{timestamp}_{candidate_slug}"
        cleaned = f"{stem}.md"
        raw = f"{stem}.raw.md"
        if not (transcript_dir / cleaned).exists() and not (
            transcript_dir / raw
        ).exists():
            return stem, cleaned, raw
        suffix += 1
        if suffix > 9999:  # pragma: no cover - absurd defensive cap
            raise RuntimeError(
                f"Could not resolve a free slug for {timestamp}_{slug} "
                "after 9999 attempts"
            )


def _yaml_escape(value: str) -> str:
    """Escape a string for inclusion inside a double-quoted YAML scalar.

    Handles backslashes, double quotes, and the C0 control characters
    ``\\n``, ``\\r``, ``\\t`` (which would otherwise terminate the YAML
    scalar mid-string and break frontmatter parsing). Other C0 control
    characters (0x00-0x1F) are stripped — they're rare in LLM output and
    have no useful representation in a single-line frontmatter field.
    """
    # Strip C0 control chars except \t \r \n (those get escaped below).
    cleaned = "".join(c for c in value if ord(c) >= 0x20 or c in "\t\r\n")
    return (
        cleaned.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )


def _yaml_list_inline(items: list[str]) -> str:
    """Render a flow-style YAML list of strings without quoting simple entries.

    Obsidian's Properties UI prefers unquoted tag values like
    ``[voice-memo, foo]``. We only quote items that aren't safe as plain
    YAML scalars (i.e. contain whitespace, flow-indicators, or start with
    a character that would confuse the parser).
    """
    rendered: list[str] = []
    for item in items:
        s = "" if item is None else str(item)
        if _is_plain_scalar_safe(s):
            rendered.append(s)
        else:
            rendered.append(f'"{_yaml_escape(s)}"')
    return "[" + ", ".join(rendered) + "]"


_PLAIN_SAFE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _is_plain_scalar_safe(s: str) -> bool:
    """Return True if ``s`` can be written as an unquoted YAML scalar."""
    if not s:
        return False
    if not _PLAIN_SAFE_RE.match(s):
        return False
    # Avoid YAML 1.1 bool/null ambiguity.
    lowered = s.lower()
    if lowered in {"y", "n", "yes", "no", "true", "false", "on", "off", "null", "~"}:
        return False
    return True


def _atomic_write_text(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` atomically (tmp + os.replace).

    Uses UTF-8 with LF line endings regardless of host OS.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # NamedTemporaryFile on Windows can't be reopened, so handle manually.
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(path.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(content)
        os.replace(tmp_path, path)
    except Exception:
        # Best-effort cleanup; re-raise the original error.
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:  # pragma: no cover
            pass
        raise


def _write_tmp_text(directory: Path, prefix: str, content: str) -> Path:
    """Write ``content`` to a fresh ``<prefix>*.md.tmp`` file in ``directory``.

    Returns the path to the created temp file. On any write error the temp
    file is removed before the exception propagates, so callers never have
    to worry about leftover partial temps from this helper itself.
    """
    directory.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=prefix,
        suffix=".md.tmp",
        dir=str(directory),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(content)
    except Exception:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:  # pragma: no cover
            pass
        raise
    return tmp_path


def _atomic_write_pair(
    cleaned_path: Path,
    cleaned_content: str,
    raw_path: Path,
    raw_content: str,
    *,
    tmp_prefix: str,
) -> None:
    """Write the ``(cleaned, raw)`` pair all-or-nothing.

    Both payloads are first staged to sibling temp files in the transcript
    directory (``os.replace`` is atomic on NTFS within the same directory).
    After both temps land on disk we atomically rename them into place.

    Failure handling:
      * If writing either temp file fails, both temps are deleted and the
        original error propagates.
      * If the first ``os.replace`` succeeds but the second fails, we try
        to revert by removing the just-created cleaned file so the vault
        isn't left with a dangling ``raw_transcript:`` frontmatter link.
        The remaining raw temp is cleaned up as well.

    The tiny race window between the two ``os.replace`` calls is the price
    of doing this on a POSIX-style filesystem API — best-effort rollback
    keeps us correct in the common failure modes (disk full, permission
    error on the raw write).
    """
    directory = cleaned_path.parent
    tmp_cleaned: Path | None = None
    tmp_raw: Path | None = None
    try:
        tmp_cleaned = _write_tmp_text(directory, tmp_prefix, cleaned_content)
        tmp_raw = _write_tmp_text(directory, tmp_prefix, raw_content)
    except Exception:
        # One of the temp writes failed. Clean up whichever temp made it
        # to disk and re-raise.
        for tmp in (tmp_cleaned, tmp_raw):
            if tmp is not None:
                try:
                    if tmp.exists():
                        tmp.unlink()
                except OSError:  # pragma: no cover
                    pass
        raise

    # At this point both temp files exist on disk. Swap them into place.
    try:
        os.replace(tmp_cleaned, cleaned_path)
    except Exception:
        # First replace failed — neither destination has been touched.
        for tmp in (tmp_cleaned, tmp_raw):
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:  # pragma: no cover
                pass
        raise

    try:
        os.replace(tmp_raw, raw_path)
    except Exception:
        # Second replace failed after the first succeeded. Revert the
        # cleaned file so the vault doesn't end up with a dangling link.
        try:
            if cleaned_path.exists():
                cleaned_path.unlink()
        except OSError:  # pragma: no cover
            pass
        try:
            if tmp_raw.exists():
                tmp_raw.unlink()
        except OSError:  # pragma: no cover
            pass
        raise


# --- frontmatter builders ----------------------------------------------------


def _build_cleaned_frontmatter(
    *,
    title: str,
    slug: str,
    date: str,
    time: str,
    duration: str,
    timestamp: str,
    stem: str,
    tags: list[str],
    model: str,
) -> str:
    """Build the YAML frontmatter string for the cleaned transcript."""
    tags_line = _yaml_list_inline(["voice-memo", *tags])
    aliases_line = _yaml_list_inline([slug])
    audio_wikilink = f"[[{AUDIO_VAULT_FOLDER}/{timestamp}.flac]]"
    raw_wikilink = (
        f"[[{AUDIO_VAULT_FOLDER}/{TRANSCRIPTIONS_VAULT_SUBFOLDER}/{stem}.raw]]"
    )
    lines = [
        "---",
        f'title: "{_yaml_escape(title)}"',
        f"aliases: {aliases_line}",
        f"date: {date}",
        f'time: "{time}"',
        f'duration: "{duration}"',
        f'audio: "{_yaml_escape(audio_wikilink)}"',
        f'raw_transcript: "{_yaml_escape(raw_wikilink)}"',
        "source: voice-memo",
        f"tags: {tags_line}",
        "status: captured",
        f"model: {model}" if _is_plain_scalar_safe(model) else f'model: "{_yaml_escape(model)}"',
        "---",
    ]
    return "\n".join(lines)


def _build_raw_frontmatter(
    *,
    title: str,
    date: str,
    time: str,
    duration: str,
    timestamp: str,
    stem: str,
) -> str:
    """Build the YAML frontmatter string for the raw transcript."""
    audio_wikilink = f"[[{AUDIO_VAULT_FOLDER}/{timestamp}.flac]]"
    cleaned_wikilink = (
        f"[[{AUDIO_VAULT_FOLDER}/{TRANSCRIPTIONS_VAULT_SUBFOLDER}/{stem}]]"
    )
    raw_title = f"{title} (raw transcript)"
    lines = [
        "---",
        f'title: "{_yaml_escape(raw_title)}"',
        f"date: {date}",
        f'time: "{time}"',
        f'duration: "{duration}"',
        f'audio: "{_yaml_escape(audio_wikilink)}"',
        f'cleaned_transcript: "{_yaml_escape(cleaned_wikilink)}"',
        "source: voice-memo-raw",
        f"tags: {_yaml_list_inline(['voice-memo-raw'])}",
        "---",
    ]
    return "\n".join(lines)


# --- accessors for duck-typed TypedDicts -------------------------------------


def _get(
    obj: Mapping[str, Any] | Any,
    key: str,
    default: Any = None,
) -> Any:
    """Fetch ``key`` from either a Mapping or an object with attributes."""
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


# --- placeholder builders ----------------------------------------------------


#: Suffix used for the placeholder file written before transcription. The
#: ``<timestamp>_pending.md`` form is unique by construction (timestamps are
#: generated per-recording and embed seconds), so it never collides with a
#: real transcript filename.
PLACEHOLDER_SUFFIX: str = "_pending"


def _placeholder_filename(timestamp: str) -> str:
    """Return the filename used for the pre-transcription placeholder."""
    return f"{timestamp}{PLACEHOLDER_SUFFIX}.md"


def _build_placeholder_frontmatter(
    *,
    date: str,
    time: str,
    duration: str,
    timestamp: str,
) -> str:
    """YAML frontmatter for the "Processing..." placeholder file.

    Marked ``status: transcribing`` and tagged ``processing`` so a future
    "find stuck placeholders" sweep can pick them up trivially.
    """
    audio_wikilink = f"[[{AUDIO_VAULT_FOLDER}/{timestamp}.flac]]"
    tags_line = _yaml_list_inline(["voice-memo", "processing"])
    lines = [
        "---",
        'title: "Processing..."',
        f"date: {date}",
        f'time: "{time}"',
        f'duration: "{duration}"',
        f'audio: "{_yaml_escape(audio_wikilink)}"',
        "source: voice-memo",
        f"tags: {tags_line}",
        "status: transcribing",
        "---",
    ]
    return "\n".join(lines)


def _build_placeholder_doc(
    *,
    timestamp: str,
    duration_sec: float,
) -> str:
    """Render the full placeholder markdown body (frontmatter + audio + msg)."""
    date, time_str = _parse_timestamp(timestamp)
    duration = _format_duration(duration_sec)
    fm = _build_placeholder_frontmatter(
        date=date, time=time_str, duration=duration, timestamp=timestamp
    )
    audio_embed = f"![[{AUDIO_VAULT_FOLDER}/{timestamp}.flac]]"
    return (
        f"{fm}\n"
        f"{audio_embed}\n"
        f"\n"
        f"## Transcribing...\n"
        f"\n"
        f"> The transcript will appear here in a few seconds. "
        f"Don't close this file -- it'll auto-update.\n"
    )


def _build_placeholder_error_doc(
    *,
    timestamp: str,
    duration_sec: float,
    exc: BaseException,
) -> str:
    """Render an error-state placeholder so the user sees the failure.

    Used when transcription permanently fails *after* a placeholder was
    already opened in Obsidian — overwrites the "Processing..." text with
    the exception message instead of leaving a stuck spinner.
    """
    date, time_str = _parse_timestamp(timestamp)
    duration = _format_duration(duration_sec)
    audio_wikilink = f"[[{AUDIO_VAULT_FOLDER}/{timestamp}.flac]]"
    tags_line = _yaml_list_inline(["voice-memo", "failed"])
    fm_lines = [
        "---",
        'title: "Transcription failed"',
        f"date: {date}",
        f'time: "{time_str}"',
        f'duration: "{duration}"',
        f'audio: "{_yaml_escape(audio_wikilink)}"',
        "source: voice-memo",
        f"tags: {tags_line}",
        "status: failed",
        "---",
    ]
    fm = "\n".join(fm_lines)
    audio_embed = f"![[{AUDIO_VAULT_FOLDER}/{timestamp}.flac]]"
    # Render the exception text inside a fenced code block so backticks /
    # markdown special chars in the message can't break Obsidian rendering.
    exc_text = str(exc) or exc.__class__.__name__
    return (
        f"{fm}\n"
        f"{audio_embed}\n"
        f"\n"
        f"## Transcription failed\n"
        f"\n"
        f"```\n"
        f"{exc_text}\n"
        f"```\n"
        f"\n"
        f"Audio file is still saved -- retry from the tray menu.\n"
    )


# --- transcript content builder ---------------------------------------------


def _build_transcript_docs(
    *,
    tr: "TranscriptResult",
    rec: "RecordingResult",
    stem: str,
    resolved_slug: str,
) -> tuple[str, str]:
    """Render the (cleaned, raw) markdown documents for a finished transcript.

    Shared by :func:`write_transcript` and :func:`finalize_transcript` so
    they emit byte-identical output for the same inputs.
    """
    timestamp: str = str(_get(rec, "timestamp"))
    duration_sec: float = float(_get(rec, "duration_sec", 0.0))

    title: str = str(_get(tr, "title", "Untitled"))
    raw_body: str = str(_get(tr, "raw", ""))
    cleaned_body: str = str(_get(tr, "cleaned", ""))
    model_used: str = str(_get(tr, "model_used", ""))

    raw_tags = _get(tr, "tags", []) or []
    tags_list: list[str] = [str(t) for t in raw_tags]

    date, time_str = _parse_timestamp(timestamp)
    duration = _format_duration(duration_sec)

    cleaned_fm = _build_cleaned_frontmatter(
        title=title,
        slug=resolved_slug,
        date=date,
        time=time_str,
        duration=duration,
        timestamp=timestamp,
        stem=stem,
        tags=tags_list,
        model=model_used,
    )
    audio_embed = f"![[{AUDIO_VAULT_FOLDER}/{timestamp}.flac]]"
    cleaned_doc = (
        f"{cleaned_fm}\n"
        f"{audio_embed}\n"
        f"\n"
        f"## Transcript\n"
        f"{cleaned_body}\n"
    )

    raw_fm = _build_raw_frontmatter(
        title=title,
        date=date,
        time=time_str,
        duration=duration,
        timestamp=timestamp,
        stem=stem,
    )
    raw_doc = (
        f"{raw_fm}\n"
        f"## Transcript (verbatim)\n"
        f"{raw_body}\n"
    )
    return cleaned_doc, raw_doc


# --- public API --------------------------------------------------------------


def write_placeholder(
    timestamp: str,
    audio_path: Path,
    duration_sec: float = 0.0,
) -> Path:
    """Write a "Processing..." placeholder markdown file.

    The placeholder lands at ``TRANSCRIPT_DIR / <timestamp>_pending.md`` and
    is meant to be opened in Obsidian *immediately* after a recording stops,
    before transcription has run. It contains an audio embed (so the user
    can play the recording while waiting) and a short banner explaining
    that the transcript will appear shortly.

    Parameters
    ----------
    timestamp:
        ``YYYY-MM-DD_HHMMSS`` recording timestamp.
    audio_path:
        Path to the FLAC audio file. Currently unused except to validate
        the timestamp shape (kept on the signature for future enrichment
        like duration probing).
    duration_sec:
        Recording duration in seconds; rendered as ``M:SS`` in the YAML.
        Pass 0 if not yet known — the file gets overwritten on finalize
        anyway.

    Returns
    -------
    Path
        The placeholder file path.
    """
    # Validate the timestamp early — same behaviour as ``write_transcript``.
    _parse_timestamp(timestamp)

    transcript_dir: Path = Path(config.TRANSCRIPT_DIR)
    transcript_dir.mkdir(parents=True, exist_ok=True)
    placeholder_path = transcript_dir / _placeholder_filename(timestamp)

    content = _build_placeholder_doc(
        timestamp=timestamp,
        duration_sec=float(duration_sec or 0.0),
    )
    _atomic_write_text(placeholder_path, content)
    logger.info(
        "Wrote placeholder: %s (audio=%s)",
        placeholder_path,
        audio_path,
    )
    return placeholder_path


def write_placeholder_error(
    placeholder_path: Path,
    exc: BaseException,
    *,
    timestamp: str | None = None,
    duration_sec: float = 0.0,
) -> None:
    """Overwrite the placeholder with a "Transcription failed" message.

    Best-effort UX hook for the case where transcription permanently fails
    after the placeholder was already opened in Obsidian. The user's open
    tab refreshes to show the error in place, instead of staring at
    "Processing..." forever.

    Parameters
    ----------
    placeholder_path:
        The path returned from a prior :func:`write_placeholder` call.
    exc:
        The exception that caused the failure. ``str(exc)`` is rendered
        inside a fenced code block.
    timestamp:
        Override for deriving the date/time YAML fields. If omitted, the
        timestamp is parsed from the placeholder filename
        (``<timestamp>_pending.md`` → ``<timestamp>``).
    duration_sec:
        Optional duration; defaults to 0.
    """
    placeholder_path = Path(placeholder_path)
    if timestamp is None:
        stem = placeholder_path.stem
        if stem.endswith(PLACEHOLDER_SUFFIX):
            timestamp = stem[: -len(PLACEHOLDER_SUFFIX)]
        else:  # pragma: no cover - defensive
            timestamp = stem

    # Validate; if the timestamp is malformed we just give up rather than
    # overwriting the placeholder with garbage.
    _parse_timestamp(timestamp)

    content = _build_placeholder_error_doc(
        timestamp=timestamp,
        duration_sec=float(duration_sec or 0.0),
        exc=exc,
    )
    _atomic_write_text(placeholder_path, content)
    logger.info(
        "Wrote placeholder-error: %s (%s)", placeholder_path, exc
    )


def finalize_transcript(
    placeholder_path: Path,
    tr: "TranscriptResult",
    rec: "RecordingResult",
) -> tuple[Path, Path]:
    """Overwrite a placeholder with the real transcript, then rename.

    The "overwrite then rename" dance is intentional: Obsidian's open
    editor tab is currently pointing at ``<timestamp>_pending.md``. If we
    instead wrote a *new* file and deleted the placeholder, the user's
    open tab would flicker to "deleted". By rewriting the placeholder
    contents in place and then renaming it to ``<timestamp>_<slug>.md``,
    Obsidian's file watcher follows the rename and the tab simply updates
    its title.

    Parameters
    ----------
    placeholder_path:
        The path returned from a prior :func:`write_placeholder` call.
        Must exist on disk; if it doesn't, this falls back to the standard
        :func:`write_transcript` flow.
    tr:
        ``TranscriptResult`` with the same fields :func:`write_transcript`
        consumes.
    rec:
        ``RecordingResult`` with ``timestamp`` and ``duration_sec``.

    Returns
    -------
    tuple[Path, Path]
        ``(cleaned_md_path, raw_md_path)`` — same shape as
        :func:`write_transcript`.
    """
    placeholder_path = Path(placeholder_path)
    if not placeholder_path.exists():
        # Fallback path: caller's earlier ``write_placeholder`` somehow
        # vanished (manual deletion in Obsidian, etc.). Defer to the
        # legacy two-file write so we still produce output.
        logger.warning(
            "finalize_transcript: placeholder %s missing; falling back to write_transcript",
            placeholder_path,
        )
        return write_transcript(tr, rec)

    timestamp: str = str(_get(rec, "timestamp"))
    base_slug = sanitize_slug(str(_get(tr, "slug", "") or ""))

    transcript_dir: Path = Path(config.TRANSCRIPT_DIR)
    transcript_dir.mkdir(parents=True, exist_ok=True)

    # Resolve a non-colliding final stem. The placeholder doesn't count
    # because we're about to rename it onto whatever stem we pick.
    stem, cleaned_name, raw_name = _resolve_slug_filenames(
        transcript_dir, timestamp, base_slug
    )
    resolved_slug = (
        stem[len(timestamp) + 1 :]
        if stem.startswith(f"{timestamp}_")
        else base_slug
    )
    cleaned_path = transcript_dir / cleaned_name
    raw_path = transcript_dir / raw_name

    cleaned_doc, raw_doc = _build_transcript_docs(
        tr=tr, rec=rec, stem=stem, resolved_slug=resolved_slug
    )

    # 1. Overwrite the placeholder file in-place with the cleaned content.
    #    Obsidian's open tab refreshes to the new content (status:
    #    transcribing → captured) without losing the editor focus.
    _atomic_write_text(placeholder_path, cleaned_doc)

    # 2. Write the raw file at the FINAL raw path (no placeholder needed
    #    since it's never opened in Obsidian).
    _atomic_write_text(raw_path, raw_doc)

    # 3. Rename the (now-cleaned-content) placeholder onto its final stem.
    #    ``os.replace`` is atomic on NTFS within the same directory and
    #    Obsidian's file-watcher follows the rename — the open tab keeps
    #    the same buffer, just under the new name.
    try:
        os.replace(placeholder_path, cleaned_path)
    except Exception:
        # Best-effort rollback: if the rename fails, drop the raw file we
        # just wrote so the vault doesn't end up with a dangling raw
        # pointing at a never-renamed placeholder.
        try:
            if raw_path.exists():
                raw_path.unlink()
        except OSError:  # pragma: no cover
            pass
        raise

    logger.info(
        "Finalized transcript: cleaned=%s raw=%s (slug=%s, from placeholder=%s)",
        cleaned_path,
        raw_path,
        resolved_slug,
        placeholder_path.name,
    )
    return cleaned_path, raw_path


def write_transcript(
    tr: "TranscriptResult",
    rec: "RecordingResult",
) -> tuple[Path, Path]:
    """Write cleaned + raw markdown files to ``TRANSCRIPT_DIR``.

    Parameters
    ----------
    tr:
        ``TranscriptResult`` TypedDict from ``transcribe.transcribe``. The
        fields we consume are ``raw``, ``cleaned``, ``slug``, ``title``,
        ``tags`` and ``model_used``.
    rec:
        ``RecordingResult`` TypedDict from ``recorder.recorder``. We
        consume ``timestamp`` and ``duration_sec``.

    Returns
    -------
    tuple[Path, Path]
        ``(cleaned_md_path, raw_md_path)``.
    """
    timestamp: str = str(_get(rec, "timestamp"))
    base_slug = sanitize_slug(str(_get(tr, "slug", "") or ""))

    transcript_dir: Path = Path(config.TRANSCRIPT_DIR)
    transcript_dir.mkdir(parents=True, exist_ok=True)

    stem, cleaned_name, raw_name = _resolve_slug_filenames(
        transcript_dir, timestamp, base_slug
    )
    # The resolved slug may differ from base_slug (collision suffix). Extract
    # the slug used so the frontmatter `aliases` / raw link agree with the
    # filename.
    resolved_slug = stem[len(timestamp) + 1 :] if stem.startswith(f"{timestamp}_") else base_slug

    cleaned_path = transcript_dir / cleaned_name
    raw_path = transcript_dir / raw_name

    cleaned_doc, raw_doc = _build_transcript_docs(
        tr=tr, rec=rec, stem=stem, resolved_slug=resolved_slug
    )

    # Write both files as an all-or-nothing pair. See ``_atomic_write_pair``
    # for the rollback semantics — this guards against the case where the
    # cleaned file lands but the raw write fails (disk full / permission
    # error), which would otherwise leave the vault with a dangling
    # ``raw_transcript:`` link.
    _atomic_write_pair(
        cleaned_path,
        cleaned_doc,
        raw_path,
        raw_doc,
        tmp_prefix=f"{stem}_",
    )
    logger.info(
        "Wrote transcripts: cleaned=%s raw=%s (slug=%s)",
        cleaned_path,
        raw_path,
        resolved_slug,
    )
    return cleaned_path, raw_path
