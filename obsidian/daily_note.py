"""Append voice memo links to the matching Obsidian Daily Note.

The daily note lives at ``DAILY_DIR / 'YYYY-MM-DD.md'`` (derived from the
voice-memo timestamp). If the file does not exist, a minimal stub is
created. The bullet is inserted under the ``## Voice Memos`` heading; if
that heading is missing it is appended to the end of the file.

All writes are UTF-8 with LF line endings via an atomic ``tempfile +
os.replace`` dance so concurrent invocations can't leave a half-written
file.

Public API:
    append_memo_link(timestamp, slug, title, duration_sec) -> Path
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
import threading
from pathlib import Path

import config
from obsidian.writer import (
    _audio_vault_folder,
    _format_duration,
    _parse_timestamp,
    _transcriptions_vault_subfolder,
)

logger = logging.getLogger(__name__)

#: Canonical heading text we insert / recognize.
VOICE_MEMOS_HEADING: str = "## Voice Memos"

#: Serializes the read -> idempotency-check -> insert -> atomic-write
#: sequence in ``append_memo_link``. Without this, two transcription
#: threads finishing on the same day can race between the initial read
#: and the final ``os.replace``, causing the later writer to clobber the
#: earlier one's bullet entry.
#:
#: Not multi-process safe. If future use cases involve concurrent
#: processes writing to the same daily note, swap for a filelock-based
#: cross-process lock (e.g. ``msvcrt.locking`` or the ``filelock``
#: package).
_DAILY_NOTE_LOCK: threading.RLock = threading.RLock()

# Match any level-2 heading whose text equals "voice memos" (case-insensitive,
# trimmed). Anchored at start of line.
_VOICE_HEADING_RE = re.compile(
    r"^##[ \t]+voice[ \t]+memos[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)

_LEVEL2_HEADING_RE = re.compile(r"^##[ \t]+\S", re.MULTILINE)


# --- helpers -----------------------------------------------------------------


def _daily_note_path(date: str) -> Path:
    """Return the path to ``Daily Notes/<date>.md``."""
    return Path(config.DAILY_DIR) / f"{date}.md"


def _minimal_daily_frontmatter(date: str) -> str:
    """Return the stub frontmatter block for a freshly-created daily note."""
    return (
        "---\n"
        f"date: {date}\n"
        "tags: [daily]\n"
        "---\n"
    )


def _build_entry(
    *, time_hhmm: str, timestamp: str, slug: str, title: str, duration: str
) -> str:
    """Render the bullet entry text (no trailing newline).

    The link folder names are derived from config (via ``writer``) so the
    daily-note wikilinks always point at the same vault-relative location the
    transcript files are actually written to.
    """
    stem = f"{timestamp}_{slug}" if slug else timestamp
    link_target = (
        f"{_audio_vault_folder()}/{_transcriptions_vault_subfolder()}/{stem}"
    )
    return f"- {time_hhmm} [[{link_target}|{title}]] ({duration})"


def _atomic_write_text(path: Path, content: str) -> None:
    """Atomically write ``content`` to ``path`` as UTF-8 / LF."""
    path.parent.mkdir(parents=True, exist_ok=True)
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
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:  # pragma: no cover
            pass
        raise


def _ensure_trailing_newline(text: str) -> str:
    if not text:
        return ""
    return text if text.endswith("\n") else text + "\n"


def _insert_under_heading(content: str, entry: str) -> str:
    """Insert ``entry`` under the existing ``## Voice Memos`` heading.

    The entry is placed immediately after the last existing bullet/line
    inside the Voice Memos section (i.e. before the next level-2 heading,
    or at end of file if this is the last section). Blank lines before
    the next heading are preserved.
    """
    match = _VOICE_HEADING_RE.search(content)
    if match is None:
        raise ValueError("Voice Memos heading not found in content")

    heading_end = match.end()
    # Move past the newline that terminates the heading line.
    if heading_end < len(content) and content[heading_end] == "\n":
        heading_end += 1

    # Find the next level-2 heading after this one.
    next_match = _LEVEL2_HEADING_RE.search(content, pos=heading_end)
    if next_match is None:
        section = content[heading_end:]
        tail = ""
    else:
        section = content[heading_end:next_match.start()]
        tail = content[next_match.start():]

    # Strip trailing whitespace from the section but keep at least one newline
    # separating bullets from the next heading / EOF. Re-apply one blank line
    # between entries and the next heading for readability.
    section_rstripped = section.rstrip("\n")

    if section_rstripped:
        new_section = section_rstripped + "\n" + entry + "\n"
    else:
        new_section = entry + "\n"

    if tail:
        # Ensure exactly one blank line between our entry and the next heading.
        new_section = new_section + "\n"
        return content[:heading_end] + new_section + tail
    # End-of-file case: ensure file ends with a single newline.
    return content[:heading_end] + new_section


def _append_heading_and_entry(content: str, entry: str) -> str:
    """Append ``## Voice Memos`` heading + ``entry`` at end of file."""
    base = _ensure_trailing_newline(content)
    # Make sure we have a blank line before the new heading when the file
    # already has meaningful content.
    if base.rstrip("\n"):
        if not base.endswith("\n\n"):
            base = base.rstrip("\n") + "\n\n"
    return base + f"{VOICE_MEMOS_HEADING}\n{entry}\n"


# --- public API --------------------------------------------------------------


def append_memo_link(
    timestamp: str,
    slug: str,
    title: str,
    duration_sec: float,
) -> Path:
    """Append a voice-memo link to today's daily note.

    Parameters
    ----------
    timestamp:
        ``YYYY-MM-DD_HHMMSS`` string identifying the recording.
    slug:
        Kebab-case slug (matching the transcript filename). May be empty
        if slug generation failed — in that case the link points at
        the bare timestamp.
    title:
        Human-readable title to display in the link.
    duration_sec:
        Recording duration in seconds; rendered as ``M:SS``.

    Returns
    -------
    Path
        The daily-note path that was (possibly created and) updated.

    Notes
    -----
    * Idempotent: if an entry linking to the same transcript already
      exists in the file, the file is left untouched.
    * Writes are atomic (``tempfile + os.replace``) so concurrent callers
      cannot observe a half-written file.
    """
    date, time = _parse_timestamp(timestamp)
    time_hhmm = time[:5]  # "HH:MM:SS" -> "HH:MM"
    duration = _format_duration(duration_sec)
    entry = _build_entry(
        time_hhmm=time_hhmm,
        timestamp=timestamp,
        slug=slug,
        title=title,
        duration=duration,
    )

    path = _daily_note_path(date)
    # Serialize the full read -> check -> insert -> write sequence.
    # Without this lock two threads can both read the same file, each
    # append their own bullet, and race on the final ``os.replace`` —
    # whichever write lands last silently drops the other's entry.
    with _DAILY_NOTE_LOCK:
        if path.exists():
            content = path.read_text(encoding="utf-8")
        else:
            content = _minimal_daily_frontmatter(date) + "\n"

        # Idempotency: if the exact transcript link target is already
        # present, skip. We compare on the stem (timestamp_slug) so title
        # tweaks don't cause duplicates.
        stem = f"{timestamp}_{slug}" if slug else timestamp
        idempotency_needle = (
            f"[[{_audio_vault_folder()}/{_transcriptions_vault_subfolder()}/{stem}"
        )
        if idempotency_needle in content:
            logger.info("Daily note %s already contains entry for %s; skipping", path, stem)
            # Still make sure the file exists on disk (create it if we only
            # just synthesised the stub content).
            if not path.exists():
                _atomic_write_text(path, content)
            return path

        if _VOICE_HEADING_RE.search(content):
            new_content = _insert_under_heading(content, entry)
        else:
            new_content = _append_heading_and_entry(content, entry)

        _atomic_write_text(path, new_content)
        logger.info("Appended voice memo entry to %s", path)
        return path
