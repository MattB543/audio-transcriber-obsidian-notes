"""One-shot migration of your existing pre-pipeline voice-memo transcripts.

Legacy filenames look like ``<M>-<D>-<YY>_<HH>.<MM>.md`` (e.g.
``7-8-25_22.33.md``). Their bodies start with a block of inline hashtags
(``#Camelcased`` one per line), then a blank line, then the verbatim
transcript. This script:

1. Scans ``TRANSCRIPT_DIR`` for legacy filenames
2. Parses the date/time from each filename (two-digit year → 2000s)
3. Extracts the leading hashtag block, converts each tag to lowercase
   kebab-case, and promotes them to YAML ``tags``
4. Writes the body (minus the hashtag block) under a freshly-added YAML
   frontmatter at ``<YYYY-MM-DD_HHMMSS>.md``
5. Renames the matching audio file in ``AUDIO_DIR`` if present

Default mode is DRY-RUN; pass ``--apply`` to actually modify the vault.

Usage:
    python -m obsidian.migrate_legacy [--apply] [--transcripts DIR] [--audio DIR]
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import config
from obsidian.writer import _yaml_escape

logger = logging.getLogger(__name__)

# --- patterns ----------------------------------------------------------------

# Matches legacy transcript names: 7-8-25_22.33.md  (any 1-2 digit M / D / HH / MM).
LEGACY_TRANSCRIPT_RE = re.compile(
    r"^(?P<m>\d{1,2})-(?P<d>\d{1,2})-(?P<yy>\d{2})_(?P<hh>\d{1,2})\.(?P<mm>\d{1,2})\.md$"
)
LEGACY_AUDIO_RE = re.compile(
    r"^(?P<m>\d{1,2})-(?P<d>\d{1,2})-(?P<yy>\d{2})_(?P<hh>\d{1,2})\.(?P<mm>\d{1,2})\.(?P<ext>mp4|mp3|m4a|flac|wav)$"
)

# Leading hashtag lines (word chars / camelcase allowed). Trailing whitespace OK.
_HASHTAG_LINE_RE = re.compile(r"^#([A-Za-z0-9_]+)\s*$")

# CamelCase -> kebab-case. Splits "CamelCase" / "ABCFoo" correctly.
_CAMEL_SPLIT_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


# --- data --------------------------------------------------------------------


@dataclass(frozen=True)
class LegacyFile:
    """One transcript scheduled for migration."""

    src_path: Path
    dst_path: Path
    iso_date: str  # "YYYY-MM-DD"
    iso_time: str  # "HH:MM:SS"
    new_stem: str  # "YYYY-MM-DD_HHMMSS"
    audio_src: Path | None
    audio_dst: Path | None


# --- helpers -----------------------------------------------------------------


def _camel_to_kebab(word: str) -> str:
    """Convert a CamelCase / snake_case / noisy token to lowercase kebab-case."""
    if not word:
        return ""
    # snake_case → dashes
    word = word.replace("_", "-")
    # Split camelCase on case transitions.
    parts = _CAMEL_SPLIT_RE.split(word)
    kebab = "-".join(p for p in parts if p).lower()
    # Collapse multiple dashes, strip ends.
    kebab = re.sub(r"-{2,}", "-", kebab).strip("-")
    return kebab


def _parse_legacy_name(name: str) -> tuple[str, str, str] | None:
    """Return ``(iso_date, iso_time, new_stem)`` or ``None`` if not legacy."""
    m = LEGACY_TRANSCRIPT_RE.match(name)
    if not m:
        return None
    year = 2000 + int(m["yy"])
    month = int(m["m"])
    day = int(m["d"])
    hour = int(m["hh"])
    minute = int(m["mm"])
    if not (1 <= month <= 12 and 1 <= day <= 31 and 0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    iso_date = f"{year:04d}-{month:02d}-{day:02d}"
    iso_time = f"{hour:02d}:{minute:02d}:00"
    new_stem = f"{iso_date}_{hour:02d}{minute:02d}00"
    return iso_date, iso_time, new_stem


def _extract_legacy_tags(body: str) -> tuple[list[str], str]:
    """Split the leading hashtag block from ``body``.

    Returns a ``(tags, remaining_body)`` tuple where ``tags`` is the list of
    kebab-case tags extracted (duplicates preserved in first-seen order,
    empty strings dropped) and ``remaining_body`` is the transcript text
    with the hashtag block and its trailing blank line(s) removed.
    """
    lines = body.splitlines(keepends=True)
    tags: list[str] = []
    idx = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            # Allow blank lines inside the leading block? No. First blank
            # line after at least one hashtag ends the block.
            if tags:
                idx = i + 1
                break
            # Blank line before any hashtag — skip and continue looking.
            idx = i + 1
            continue
        m = _HASHTAG_LINE_RE.match(stripped)
        if m:
            raw_tag = m.group(1)
            kebab = _camel_to_kebab(raw_tag)
            if kebab and kebab not in tags:
                tags.append(kebab)
            idx = i + 1
            continue
        # Non-tag, non-blank line → end of hashtag block.
        idx = i
        break
    else:
        # Ran through the whole file without breaking — that means every
        # line was either a hashtag or blank. The remaining body is empty.
        idx = len(lines)

    # Skip any additional blank lines after the block so we don't emit
    # extra leading whitespace in the migrated body.
    while idx < len(lines) and not lines[idx].strip():
        idx += 1

    remaining = "".join(lines[idx:])
    return tags, remaining


def _yaml_list_inline(items: list[str]) -> str:
    """Render a flow-style YAML list safe for Obsidian Properties.

    Each entry is emitted bare (unquoted) provided it matches a
    conservative plain-scalar pattern; otherwise it's double-quoted.
    """
    parts: list[str] = []
    for item in items:
        s = "" if item is None else str(item)
        if re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*$", s) and s.lower() not in {
            "y", "n", "yes", "no", "true", "false", "on", "off", "null", "~",
        }:
            parts.append(s)
        else:
            parts.append('"' + _yaml_escape(s) + '"')
    return "[" + ", ".join(parts) + "]"


def _build_frontmatter(
    *, iso_date: str, iso_time: str, extra_tags: list[str], original_name: str
) -> str:
    """Build the YAML frontmatter block for a migrated legacy transcript."""
    base_tags = ["voice-memo", "voice-memo-legacy"]
    seen = set(base_tags)
    merged = list(base_tags)
    for t in extra_tags:
        if t and t not in seen:
            merged.append(t)
            seen.add(t)
    title = f"Legacy transcript {iso_date}"
    lines = [
        "---",
        f'title: "{_yaml_escape(title)}"',
        f"date: {iso_date}",
        f'time: "{iso_time}"',
        "source: voice-memo-legacy",
        f"tags: {_yaml_list_inline(merged)}",
        f"migrated_from: {original_name}",
        "---",
    ]
    return "\n".join(lines)


def _atomic_write_text(path: Path, content: str) -> None:
    """Atomic UTF-8 / LF write via a sibling tmpfile."""
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


def _find_legacy_audio(audio_dir: Path, name: str) -> Path | None:
    """Find the audio file matching legacy transcript ``name``.

    The transcript's base (sans ``.md``) should prefix the audio name; we
    check common extensions in priority order.
    """
    if not audio_dir.exists():
        return None
    stem = name[:-3] if name.endswith(".md") else name
    for ext in ("mp4", "m4a", "mp3", "flac", "wav"):
        candidate = audio_dir / f"{stem}.{ext}"
        if candidate.exists():
            return candidate
    return None


# --- scan + plan -------------------------------------------------------------


def plan_migration(transcript_dir: Path, audio_dir: Path) -> list[LegacyFile]:
    """Scan ``transcript_dir`` and return a planned migration list."""
    if not transcript_dir.exists():
        return []
    plan: list[LegacyFile] = []
    for entry in sorted(transcript_dir.iterdir()):
        if not entry.is_file():
            continue
        parsed = _parse_legacy_name(entry.name)
        if parsed is None:
            continue
        iso_date, iso_time, new_stem = parsed
        dst_path = transcript_dir / f"{new_stem}.md"
        audio_src = _find_legacy_audio(audio_dir, entry.name)
        audio_dst: Path | None = None
        if audio_src is not None:
            audio_dst = audio_src.parent / f"{new_stem}{audio_src.suffix}"
        plan.append(
            LegacyFile(
                src_path=entry,
                dst_path=dst_path,
                iso_date=iso_date,
                iso_time=iso_time,
                new_stem=new_stem,
                audio_src=audio_src,
                audio_dst=audio_dst,
            )
        )
    return plan


def _migrate_one(item: LegacyFile, *, apply: bool) -> None:
    """Migrate a single legacy file, respecting dry-run vs apply."""
    src = item.src_path
    body = src.read_text(encoding="utf-8")
    tags, remaining = _extract_legacy_tags(body)
    frontmatter = _build_frontmatter(
        iso_date=item.iso_date,
        iso_time=item.iso_time,
        extra_tags=tags,
        original_name=src.name,
    )
    # Exactly one blank line between frontmatter and body for readability.
    remaining_trimmed = remaining.lstrip("\n")
    if remaining_trimmed:
        new_content = f"{frontmatter}\n\n{remaining_trimmed}"
    else:
        new_content = f"{frontmatter}\n"
    if not new_content.endswith("\n"):
        new_content += "\n"

    if not apply:
        return

    # Refuse to overwrite an existing destination (could happen if someone
    # has manually migrated some already).
    if item.dst_path.exists() and item.dst_path != src:
        raise FileExistsError(
            f"Destination {item.dst_path} already exists; refusing to overwrite"
        )

    _atomic_write_text(item.dst_path, new_content)
    if item.dst_path != src:
        try:
            src.unlink()
        except OSError as exc:  # pragma: no cover
            logger.warning("Could not remove legacy source %s: %s", src, exc)

    if item.audio_src is not None and item.audio_dst is not None:
        if item.audio_src == item.audio_dst:
            return
        if item.audio_dst.exists():
            logger.warning(
                "Audio destination %s already exists; skipping audio rename for %s",
                item.audio_dst,
                item.audio_src,
            )
            return
        os.replace(item.audio_src, item.audio_dst)


# --- CLI ---------------------------------------------------------------------


def _format_plan_line(item: LegacyFile, *, apply: bool) -> str:
    verb = "RENAME" if apply else "WOULD RENAME"
    audio_bit = ""
    if item.audio_src is not None and item.audio_dst is not None:
        audio_bit = f"\n    audio: {item.audio_src.name} -> {item.audio_dst.name}"
    return (
        f"[{verb}] {item.src_path.name} -> {item.dst_path.name}"
        f"{audio_bit}"
    )


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint. Returns process exit code."""
    parser = argparse.ArgumentParser(
        prog="python -m obsidian.migrate_legacy",
        description=(
            "Migrate legacy voice-memo transcripts in TRANSCRIPT_DIR to the new "
            "ISO-timestamped naming scheme with YAML frontmatter. Dry-run by default."
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually perform the renames (default is dry-run).",
    )
    parser.add_argument(
        "--transcripts",
        type=Path,
        default=Path(config.TRANSCRIPT_DIR),
        help="Transcripts directory to scan (default: config.TRANSCRIPT_DIR).",
    )
    parser.add_argument(
        "--audio",
        type=Path,
        default=Path(config.AUDIO_DIR),
        help="Audio directory to rename matching audio files (default: config.AUDIO_DIR).",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Emit migration log messages to stderr.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    plan = plan_migration(args.transcripts, args.audio)
    if not plan:
        print(f"No legacy transcripts found in {args.transcripts}")
        return 0

    for item in plan:
        print(_format_plan_line(item, apply=args.apply))

    count = len(plan)
    if args.apply:
        errors: list[tuple[Path, Exception]] = []
        for item in plan:
            try:
                _migrate_one(item, apply=True)
            except Exception as exc:  # pragma: no cover - surfaced to user
                errors.append((item.src_path, exc))
                print(f"ERROR migrating {item.src_path.name}: {exc}", file=sys.stderr)
        ok = count - len(errors)
        print(f"Renamed {ok} files" + (f" ({len(errors)} errors)" if errors else ""))
        return 1 if errors else 0

    print(f"Would rename {count} files (dry-run). Pass --apply to execute.")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI passthrough
    raise SystemExit(main())
