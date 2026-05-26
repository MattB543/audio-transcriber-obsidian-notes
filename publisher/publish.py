"""Publish pipeline: Obsidian cleaned transcript -> Astro note -> git push.

NO LLM IS USED HERE. The published note is a deterministic transformation of
the source Obsidian file:

    - title       <-  source frontmatter `title`
    - description <-  first 200 chars of the cleaned transcript + "..."
    - body        <-  cleaned transcript text (no rewrite)

Re-publish is supported: each successful publish records a content hash of
the source (title + cleaned transcript). The watcher re-publishes whenever
that hash changes -- so editing the title or body in Obsidian and waiting
~60 seconds republishes the website with the updated content.

After a successful push we also write a ``published_at`` field back into the
source Obsidian file's YAML so the user can see when the web version was
last refreshed.

Key entry point: ``publish_note(transcript_md_path) -> PublishResult``.
"""

from __future__ import annotations

import hashlib
import html
import logging
import re
import shutil
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Literal, TypedDict

import yaml

import config

logger = logging.getLogger(__name__)

# When the parent process is pythonw.exe (no console attached), spawning a
# console subprocess like git.exe causes Windows to allocate a fresh console
# window. CREATE_NO_WINDOW suppresses that. No-op on non-Windows.
_NO_WINDOW_FLAGS = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


class PublishResult(TypedDict):
    """Outcome of a single `publish_note` call.

    Status values:
      - ``published`` -- successful end-to-end publish + push.
      - ``skipped``   -- intentionally not published (e.g. transcript section
        empty, source missing a title); no side effects.
      - ``error``     -- recoverable failure (dirty tree, network blip).
        The watcher will retry the source on the next tick.
      - ``manual_recovery`` -- the local repo is in an unsafe-to-auto-touch
        state (e.g. an unpushed commit was made and rollback was unsafe or
        a recovery git command failed). The watcher MUST NOT retry this
        source until the user clears the block.
    """

    status: Literal["published", "skipped", "error", "manual_recovery"]
    slug: str | None
    reason: str | None
    commit_sha: str | None
    site_path: Path | None
    error: str | None
    # Hash of (title + cleaned transcript) at the moment of publish. The
    # watcher stores this in its ledger and republishes when it changes.
    content_hash: str | None
    # ISO-8601 timestamp written to the source Obsidian file's frontmatter
    # as ``published_at`` so the user can see when the web version was last
    # refreshed. None on non-``published`` outcomes.
    published_at: str | None


def _result(
    *,
    status: Literal["published", "skipped", "error", "manual_recovery"],
    slug: str | None = None,
    reason: str | None = None,
    commit_sha: str | None = None,
    site_path: Path | None = None,
    error: str | None = None,
    content_hash: str | None = None,
    published_at: str | None = None,
) -> PublishResult:
    return PublishResult(
        status=status,
        slug=slug,
        reason=reason,
        commit_sha=commit_sha,
        site_path=site_path,
        error=error,
        content_hash=content_hash,
        published_at=published_at,
    )


# ---------------------------------------------------------------------------
# Deterministic publish-doc builders (no LLM)
# ---------------------------------------------------------------------------

# How many characters of the cleaned transcript to use as the description
# (preview text on the personal-site notes index). Truncated cleanly at the
# nearest word boundary <= this length, then "..." appended.
_DESCRIPTION_MAX_CHARS = 200

# YAML frontmatter field we write back into the SOURCE Obsidian file after a
# successful publish. The user sees this in Obsidian; the watcher uses the
# content hash (NOT this field) to decide whether to republish, so writing
# it back doesn't trigger an unnecessary republish loop.
_SOURCE_PUBLISHED_AT_FIELD = "published_at"


def _build_description(transcript_text: str) -> str:
    """First N chars of cleaned transcript + '...' (or full text if shorter).

    Truncates at the nearest word boundary so we don't cut mid-word, then
    collapses internal whitespace so the description reads as one line.
    """
    text = re.sub(r"\s+", " ", (transcript_text or "").strip())
    if not text:
        return ""
    if len(text) <= _DESCRIPTION_MAX_CHARS:
        return text
    cut = text[:_DESCRIPTION_MAX_CHARS].rstrip()
    # Back up to the last whitespace if we landed inside a word.
    space = cut.rfind(" ")
    if space > _DESCRIPTION_MAX_CHARS // 2:  # avoid producing a useless 2-char fragment
        cut = cut[:space].rstrip()
    return cut + "..."


def _compute_content_hash(title: str, transcript_text: str) -> str:
    """SHA256 of (title + transcript). Used by the watcher to detect edits.

    We deliberately exclude the source's `published_at` field, the audio
    embed line, frontmatter `tags`, etc. from the hash so cosmetic changes
    don't trigger needless republishes; only meaningful edits to the title
    or the actual transcript body do.
    """
    payload = f"{(title or '').strip()}\n---\n{(transcript_text or '').strip()}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _compute_clipping_content_hash(
    title: str, comment: str, body: str
) -> str:
    """SHA256 of (title + comment + body) for a web clipping.

    Kept SEPARATE from `_compute_content_hash` (which hashes title + transcript)
    because clippings have a different content shape: editing EITHER the user's
    commentary OR the mirrored clipping body should trigger a republish. The
    watcher routes to this hash for sources under ``config.CLIPPINGS_DIR``.
    """
    payload = (
        f"{(title or '').strip()}\n"
        f"---comment---\n{(comment or '').strip()}\n"
        f"---body---\n{(body or '').strip()}"
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _yaml_dump_str(value: str) -> str:
    """Format a single string as a safe YAML scalar (always double-quoted)."""
    escaped = (
        value.replace("\\", "\\\\")
             .replace('"', '\\"')
             .replace("\n", "\\n")
             .replace("\r", "\\r")
             .replace("\t", "\\t")
    )
    return f'"{escaped}"'


def _build_published_md(
    *,
    title: str,
    description: str,
    transcript_text: str,
    today_iso: str,
) -> str:
    """Build the deterministic Astro-compatible markdown for the site."""
    fm_lines = [
        "---",
        f"title: {_yaml_dump_str(title)}",
        f"description: {_yaml_dump_str(description)}",
        f"created: {today_iso}",
        f"updated: {today_iso}",
        "layout: ../../layouts/Layout.astro",
        "backLink: ./",
        "backText: Notes",
        "source: voice-memo",
        "---",
        "",
        transcript_text.strip(),
        "",
    ]
    return "\n".join(fm_lines)


# ---------------------------------------------------------------------------
# Clipping -> /consuming page builder (no LLM)
# ---------------------------------------------------------------------------

# How many characters of the comment to use as the index/meta description when
# we excerpt the user's commentary. Same word-boundary truncation as
# `_build_description` so the two read consistently.
_COMMENT_EXCERPT_MAX_CHARS = 200

# Obsidian wikilink wrapper: `[[Lizka]]` or `[[Owen Cotton-Barratt]]`. Clipping
# `author` fields use this syntax; we strip the brackets for display.
_WIKILINK_RE = re.compile(r"\[\[(?P<inner>.+?)\]\]")


def _strip_wikilink(value: str) -> str:
    """Turn `[[Owen Cotton-Barratt]]` into `Owen Cotton-Barratt`.

    Also handles the `[[target|alias]]` form by keeping the alias (display
    text). Leaves plain strings untouched.
    """
    def _repl(m: re.Match[str]) -> str:
        inner = m.group("inner")
        # `[[target|alias]]` -> show the alias.
        if "|" in inner:
            inner = inner.split("|", 1)[1]
        return inner.strip()

    return _WIKILINK_RE.sub(_repl, value).strip()


def _normalize_authors(raw_author: object) -> str:
    """Normalize the clipping `author` frontmatter into a display string.

    Accepts a list (the common Obsidian-clipper shape) or a single string,
    strips `[[wikilink]]` brackets from each entry, drops blanks, and joins
    with ", ". Returns "" when there are no authors.
    """
    if raw_author is None:
        return ""
    items: list[str]
    if isinstance(raw_author, list):
        items = [str(a) for a in raw_author]
    else:
        items = [str(raw_author)]
    cleaned = [_strip_wikilink(a).strip() for a in items]
    cleaned = [a for a in cleaned if a]
    return ", ".join(cleaned)


def _pretty_source(url: str) -> str:
    """Produce friendly link text for a source URL.

    Prefers the bare host (e.g. "forum.effectivealtruism.org"); appends the
    path when it's short and meaningful. Falls back to the full URL if it can't
    be parsed. Never raises.
    """
    url = (url or "").strip()
    if not url:
        return ""
    try:
        from urllib.parse import urlsplit

        parts = urlsplit(url)
    except (ValueError, ImportError):  # pragma: no cover — defensive
        return url
    host = parts.netloc
    if not host:
        # Not a recognizable absolute URL; show it verbatim.
        return url
    path = (parts.path or "").rstrip("/")
    # Keep a short path for context, but don't dump a giant slug into the link
    # text. Anything past ~40 chars of path we drop down to just the host.
    if path and path != "" and len(path) <= 40:
        return f"{host}{path}"
    return host


def _comment_excerpt(comment: str) -> str:
    """First N chars of the comment as a one-line meta/index description.

    Collapses whitespace and truncates at a word boundary + "..." just like
    `_build_description`, so the /consuming index reads as a single tidy line.
    """
    text = re.sub(r"\s+", " ", (comment or "").strip())
    if not text:
        return ""
    if len(text) <= _COMMENT_EXCERPT_MAX_CHARS:
        return text
    cut = text[:_COMMENT_EXCERPT_MAX_CHARS].rstrip()
    space = cut.rfind(" ")
    if space > _COMMENT_EXCERPT_MAX_CHARS // 2:
        cut = cut[:space].rstrip()
    return cut + "..."


# Sentence-ending punctuation we treat as a clean truncation boundary. We look
# for one of these followed by whitespace so "e.g." style abbreviations near the
# limit are less likely to be chosen as the cut point.
_SENTENCE_END_RE = re.compile(r"[.!?][\"'’”)\]]?\s")


def _truncate_preview(body: str, limit: int) -> str:
    """Truncate ``body`` to about ``limit`` chars at a CLEAN boundary.

    Preference order, all measured within the first ``limit`` characters:
      1. the last paragraph break (``\\n\\n``) before the limit,
      2. else the last sentence end (``.``/``!``/``?`` + whitespace),
      3. else the last whitespace run,
      4. else (a single giant word) a hard cut at ``limit``.

    Never cuts mid-word for cases 1-3. The returned text is right-stripped so the
    fade block that follows starts cleanly. Callers only invoke this when
    ``len(body) > limit`` (a shorter body is shown verbatim), but it is safe to
    call on any input.
    """
    if limit <= 0:
        return ""
    if len(body) <= limit:
        return body.rstrip()

    window = body[:limit]

    # 1. last paragraph break before the limit.
    para = window.rfind("\n\n")
    if para > 0:
        return window[:para].rstrip()

    # 2. last sentence end before the limit. Search the window plus one trailing
    #    char so a sentence ending exactly at the boundary (punct then the next
    #    char being whitespace) still counts.
    probe = body[: limit + 1]
    last_sentence = -1
    for m in _SENTENCE_END_RE.finditer(probe):
        # The boundary is just after the punctuation; keep the punctuation.
        end = m.start() + 1
        if end <= limit:
            last_sentence = end
    if last_sentence > limit // 2:
        return body[:last_sentence].rstrip()

    # 3. last whitespace run -> don't cut mid-word.
    space = window.rstrip().rfind(" ")
    nl = window.rstrip().rfind("\n")
    boundary = max(space, nl)
    if boundary > 0:
        return window[:boundary].rstrip()

    # 4. a single oversized word with no boundary: hard cut.
    return window.rstrip()


def _build_consuming_md(
    *,
    title: str,
    description: str,
    comment: str,
    source_url: str,
    authors: str,
    created_iso: str,
    clipping_body: str,
) -> str:
    """Build the deterministic /consuming page markdown for a web clipping.

    SECTIONED template (approved):

      - frontmatter (title, layout, backLink/backText, source: clipping,
        created, description)
      - "## My commentary" + the comment, then a `---` divider (OMITTED
        entirely when there is no comment)
      - a "**Source:**" line + "By <authors> · clipped <date>" byline + a
        disclaimer blockquote, then a `---` divider
      - the mirrored clipping body as a TRUNCATED PREVIEW (see below)

    Body preview behaviour (``config.CLIPPING_PREVIEW_CHARS``):
      - If the body is longer than the preview limit, only the opening is shown
        (truncated at a clean paragraph/sentence/word boundary, rendered as
        normal markdown), followed by a fade-out + "Read the full version at the
        source →" call-to-action linking back to the original URL.
      - If the body is at or under the limit, the FULL body is shown with no
        fade block / CTA (it's already complete).
      - If truncated but there's no source URL, the fade block stays but the CTA
        becomes a plain "Full text not mirrored" note (no link).

    The fade block is a self-contained raw ``<div>`` with inline styles only.
    Per CommonMark, markdown inside a block-level ``<div>`` is NOT re-parsed, so
    we keep all markdown OUTSIDE the wrapper and only put a plain HTML link
    inside it.

    Rules for the optional bits:
      - No comment -> omit the commentary heading/block AND its trailing divider
        (the page then starts with "**Source:**").
      - No source URL -> render "**Source:** _(original URL not recorded)_" and
        word the disclaimer so it does not say "above".
      - No authors -> omit the "By <authors>" part of the byline.
      - No created date -> omit "· clipped <date>".
    """
    lines: list[str] = [
        "---",
        f"title: {_yaml_dump_str(title)}",
        "layout: ../../layouts/Layout.astro",
        "backLink: ./",
        "backText: Consuming",
        "source: clipping",
        f"created: {created_iso}",
        f"description: {_yaml_dump_str(description)}",
        "---",
        "",
    ]

    comment_text = (comment or "").strip()
    if comment_text:
        lines.append("## My commentary")
        lines.append("")
        lines.append(comment_text)
        lines.append("")
        lines.append("---")
        lines.append("")

    # Source line.
    source_url = (source_url or "").strip()
    if source_url:
        pretty = _pretty_source(source_url)
        lines.append(f"**Source:** [{pretty}]({source_url})")
    else:
        lines.append("**Source:** _(original URL not recorded)_")

    # Byline: "By <authors> · clipped <date>". Each half is optional.
    byline_bits: list[str] = []
    authors = (authors or "").strip()
    if authors:
        byline_bits.append(f"By {authors}")
    created = (created_iso or "").strip()
    if created:
        byline_bits.append(f"clipped {created}")
    if byline_bits:
        lines.append(" · ".join(byline_bits))

    lines.append("")
    # Disclaimer blockquote. We now publish a short PREVIEW of the body, so the
    # wording points the reader at the source for the full thing. When there's
    # no source URL we can't point "above", so we soften it.
    body_text = (clipping_body or "").strip()
    preview_limit = config.CLIPPING_PREVIEW_CHARS
    truncated = len(body_text) > preview_limit
    if source_url:
        lines.append(
            "> Mirrored as a short preview from a web clipping saved in my "
            "Obsidian —"
        )
        lines.append(
            "> only the opening is shown here. Read the full thing at the "
            "source above"
        )
        lines.append("> (if it's still up).")
    else:
        lines.append(
            "> Mirrored as a short preview from a web clipping saved in my "
            "Obsidian —"
        )
        lines.append(
            "> only the opening is shown here. The original source URL was not "
            "recorded,"
        )
        lines.append("> so the full version may live elsewhere.")
    lines.append("")
    lines.append("---")
    lines.append("")

    if not truncated:
        # Short body: show it in full, no fade / CTA (it's already complete).
        lines.append(body_text)
        lines.append("")
        return "\n".join(lines)

    # Long body: show a truncated opening rendered as normal markdown, then a
    # self-contained fade-out + CTA block. Keep markdown OUTSIDE the wrapper div
    # (CommonMark won't re-parse markdown inside a block-level <div>); only a
    # plain HTML link goes inside it.
    preview = _truncate_preview(body_text, preview_limit)
    lines.append(preview)
    lines.append("")
    lines.append(
        '<div style="position:relative; margin-top:-5rem; padding-top:5rem; '
        "background:linear-gradient(to bottom, transparent, var(--color-bg) "
        '70%); text-align:center;">'
    )
    if source_url:
        href = html.escape(source_url, quote=True)
        lines.append(
            f'  <p style="margin:0;"><a href="{href}">Read the full version at '
            "the source →</a></p>"
        )
    else:
        lines.append(
            '  <p style="margin:0;"><em>Full text not mirrored — original '
            "source not recorded.</em></p>"
        )
    lines.append("</div>")
    lines.append("")
    return "\n".join(lines)


def _read_source_frontmatter_field(source_path: Path, field: str) -> str | None:
    """Look up a single frontmatter field from a source file. Returns None if
    missing or unparseable."""
    try:
        md = source_path.read_text(encoding="utf-8")
    except OSError:
        return None
    fm, _body = _split_frontmatter(md)
    val = fm.get(field)
    return None if val is None else str(val)


def _write_source_published_at(source_path: Path, iso_timestamp: str) -> bool:
    """Update (or insert) the ``published_at`` field in the source's
    frontmatter, atomically. Returns True on success.

    Race-safe against Obsidian/sync re-saving the file: we capture the
    file's ``mtime_ns`` before reading, and refuse to write if the file
    has been touched since then. Otherwise we'd silently overwrite the
    user's intervening edit with our stale snapshot.

    If the source has no frontmatter (shouldn't happen -- our writer
    always emits one) we silently no-op rather than corrupt the file.
    """
    try:
        before_stat = source_path.stat()
    except OSError as exc:
        logger.warning("Cannot stat %s to update published_at: %s", source_path, exc)
        return False
    mtime_before = before_stat.st_mtime_ns

    try:
        original = source_path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("Cannot read %s to update published_at: %s", source_path, exc)
        return False

    m = _FRONTMATTER_RE.match(original)
    if not m:
        logger.warning(
            "Source %s has no YAML frontmatter; not writing published_at",
            source_path,
        )
        return False

    fm_text = m.group("fm")
    body = m.group("body")
    new_field_line = f'{_SOURCE_PUBLISHED_AT_FIELD}: "{iso_timestamp}"'

    # Replace existing line if present, else append before the closing ---.
    field_pattern = re.compile(
        rf"^{_SOURCE_PUBLISHED_AT_FIELD}\s*:\s*.*$", re.MULTILINE
    )
    if field_pattern.search(fm_text):
        new_fm = field_pattern.sub(new_field_line, fm_text)
    else:
        # Append at the end of the existing frontmatter block.
        new_fm = fm_text.rstrip("\n") + "\n" + new_field_line

    new_doc = f"---\n{new_fm}\n---\n{body}"

    # Re-check mtime just before swapping. If something (Obsidian autosave,
    # cloud sync, the user) saved the file between our read and now, our
    # `new_doc` would clobber that change. Skip the writeback with a clear
    # warning -- the website publish itself already succeeded; missing the
    # `published_at` write is a soft failure.
    try:
        recheck_stat = source_path.stat()
    except OSError as exc:
        logger.warning(
            "Source %s disappeared during published_at writeback: %s",
            source_path, exc,
        )
        return False
    if recheck_stat.st_mtime_ns != mtime_before:
        logger.warning(
            "Source %s was modified between read and writeback "
            "(mtime %d -> %d); skipping published_at update to avoid "
            "clobbering the concurrent edit. The website publish itself "
            "already succeeded.",
            source_path, mtime_before, recheck_stat.st_mtime_ns,
        )
        return False

    try:
        # Atomic-ish: write to .tmp + replace. Same-dir keeps it on the
        # same volume so os.replace is atomic on NTFS.
        tmp = source_path.with_suffix(source_path.suffix + ".tmp")
        tmp.write_text(new_doc, encoding="utf-8")
        tmp.replace(source_path)
        return True
    except OSError as exc:
        logger.warning("Failed to write published_at to %s: %s", source_path, exc)
        return False


# ---------------------------------------------------------------------------
# Frontmatter parsing / slugify
# ---------------------------------------------------------------------------


_FRONTMATTER_RE = re.compile(
    r"\A---\s*\n(?P<fm>.*?)\n---\s*\n?(?P<body>.*)\Z",
    re.DOTALL,
)


def _split_frontmatter(md: str) -> tuple[dict, str]:
    """Split a markdown file into (frontmatter_dict, body_str).

    Returns ({}, md) if no frontmatter present.
    """
    m = _FRONTMATTER_RE.match(md)
    if not m:
        return {}, md
    fm_raw = m.group("fm")
    body = m.group("body")
    try:
        fm = yaml.safe_load(fm_raw) or {}
        if not isinstance(fm, dict):
            fm = {}
    except yaml.YAMLError as exc:
        logger.warning("Failed to parse frontmatter: %s", exc)
        fm = {}
    return fm, body


def _extract_transcript_section(body: str) -> str:
    """Pull just the `## Transcript` section body out (the cleaned transcript)."""
    # Find `## Transcript` heading, take everything after it until the next
    # top-level-ish heading or end of file.
    pattern = re.compile(
        r"^##\s+Transcript\b[^\n]*\n(?P<body>.*?)(?=^##\s+|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    m = pattern.search(body)
    if not m:
        # Fall back to the whole body if there's no explicit Transcript section.
        return body.strip()
    return m.group("body").strip()


_SLUG_STRIP_RE = re.compile(r"[^a-z0-9\-]+")
_SLUG_COLLAPSE_RE = re.compile(r"-+")


def _slugify(s: str) -> str:
    s = s.lower().strip()
    s = s.replace("'", "").replace("’", "")
    s = re.sub(r"\s+", "-", s)
    s = _SLUG_STRIP_RE.sub("-", s)
    s = _SLUG_COLLAPSE_RE.sub("-", s).strip("-")
    return s or "note"


def _resolve_slug_collision(slug: str, *dirs: Path) -> str:
    """Append `-2`, `-3` etc. if `{slug}.md` exists in any of the given dirs.

    Checks every directory (typically site_notes_dir AND drafts_dir) so the
    same slug can't collide with a freshly-pulled note OR a stale draft.
    """
    candidate = slug
    suffix = 2
    while any((d / f"{candidate}.md").exists() for d in dirs):
        candidate = f"{slug}-{suffix}"
        suffix += 1
    return candidate


# Anything that is NOT an ASCII alphanumeric becomes a slug separator. We keep
# the original case (the /consuming convention is Title-Case-hyphen, e.g.
# "Jim-Rutt-on-Win-Win") and collapse runs of spaces, punctuation, and
# em/en-dashes into a single hyphen.
_CONSUMING_SLUG_SEP_RE = re.compile(r"[^A-Za-z0-9]+")


def _consuming_slugify(title: str) -> str:
    """Produce a Title-Case-hyphen slug matching the existing /consuming convention.

    Preserves the original case (unlike `_slugify`, which lowercases for the
    notes section). Drops apostrophes entirely so "Matt's" -> "Matts" rather
    than "Matt-s", then replaces every run of non-alphanumeric characters
    (spaces, punctuation, em-dashes) with a single hyphen and strips leading /
    trailing hyphens.

    e.g. "AI Tools for Existential Security — EA Forum"
          -> "AI-Tools-for-Existential-Security-EA-Forum"
    """
    s = (title or "").strip()
    # Drop apostrophes so possessives collapse cleanly (Matt's -> Matts).
    s = s.replace("'", "").replace("’", "")
    s = _CONSUMING_SLUG_SEP_RE.sub("-", s)
    s = _SLUG_COLLAPSE_RE.sub("-", s).strip("-")
    return s or "clipping"


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run a git command with capture_output + check, returning the completed proc.

    We explicitly avoid shell=True and never append --force/--no-verify here.
    `creationflags=_NO_WINDOW_FLAGS` keeps git.exe from popping a console
    window when the parent is pythonw.exe.
    """
    cmd = ["git", *args]
    logger.debug("git %s (cwd=%s)", " ".join(args), cwd)
    return subprocess.run(
        cmd,
        capture_output=True,
        check=True,
        cwd=str(cwd),
        text=True,
        creationflags=_NO_WINDOW_FLAGS,
    )


def _git_ok(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run git without check=True; caller inspects returncode."""
    cmd = ["git", *args]
    logger.debug("git %s (cwd=%s)", " ".join(args), cwd)
    return subprocess.run(
        cmd,
        capture_output=True,
        check=False,
        cwd=str(cwd),
        text=True,
        creationflags=_NO_WINDOW_FLAGS,
    )


def _try_recovery_git(
    args: list[str], cwd: Path, step_name: str
) -> tuple[bool, str]:
    """Run a git command in the rollback/recovery path.

    Returns ``(success, output_or_stderr)``. On rc != 0 logs the failure with
    step_name so the caller can build an actionable error message. Never
    raises — exceptions (e.g. git binary missing) are caught and returned as
    failure with the exception text.
    """
    cmd = ["git", *args]
    logger.debug("recovery git %s (cwd=%s, step=%s)", " ".join(args), cwd, step_name)
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            check=False,
            cwd=str(cwd),
            text=True,
            creationflags=_NO_WINDOW_FLAGS,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Rollback step %r raised", step_name)
        return False, str(exc)
    if result.returncode != 0:
        logger.error(
            "Rollback step %r failed: rc=%d stderr=%s",
            step_name,
            result.returncode,
            (result.stderr or "").strip(),
        )
        return False, (result.stderr or "").strip()
    return True, result.stdout or ""


def _working_tree_clean(cwd: Path) -> bool:
    """True iff `git status --porcelain` is empty."""
    proc = _git(["status", "--porcelain"], cwd=cwd)
    return proc.stdout.strip() == ""


def _current_head(cwd: Path) -> str:
    proc = _git(["rev-parse", "HEAD"], cwd=cwd)
    return proc.stdout.strip()


# ---------------------------------------------------------------------------
# Consuming index auto-update
# ---------------------------------------------------------------------------

_MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]

# Heading matchers. `## 2025` is a year section; `### October` is a month
# section. We deliberately match on the heading level so a stray `## My
# commentary`-style heading inside a paragraph never trips us.
_YEAR_HEADING_RE = re.compile(r"^##\s+(?P<year>\d{4})\s*$")
_MONTH_HEADING_RE = re.compile(r"^###\s+(?P<month>[A-Za-z]+)\s*$")


def _parse_iso_date(value: str) -> date | None:
    """Parse a ``YYYY-MM-DD`` (optionally with a time suffix) into a date.

    Returns None if the leading 10 chars aren't a valid ISO date. Used for the
    clipping's ``created`` field, which Obsidian writes as a bare date but the
    user might hand-edit.
    """
    text = (value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _consuming_index_bullet(title: str, slug: str, description: str) -> str:
    """Build the single index bullet line for a clipping."""
    desc = re.sub(r"\s+", " ", (description or "").strip())
    return f"- **[{title}](/consuming/{slug})** - {desc}"


def _update_consuming_index(
    index_path: Path,
    *,
    title: str,
    slug: str,
    description: str,
    entry_date: date,
) -> bool:
    """Insert (or update in place) a clipping bullet in the /consuming index.

    The index is grouped ``## <YYYY>`` -> ``### <MonthName>`` with bullets like
    ``- **[Title](/consuming/Slug)** - description``. We:

      1. Remove any existing bullet line that links to ``/consuming/<slug>`` so
         a republish updates in place (idempotent — no duplicate bullets).
      2. Ensure a ``## <year>`` section exists (years descending) and a
         ``### <month>`` subsection within it (months descending), creating
         either heading if missing.
      3. Insert the fresh bullet at the TOP of that month's bullet list
         (reverse-chronological within the month).

    The intro paragraph(s) above the first year heading and the trailing
    ``<!-- Structure... -->`` comment are preserved by operating on the line
    list, never a brittle whole-file regex.

    Returns True if the file was written (always, on success). Raises OSError on
    read/write failure (the caller treats that as a publish error).
    """
    year = entry_date.year
    month_name = _MONTH_NAMES[entry_date.month - 1]
    bullet = _consuming_index_bullet(title, slug, description)
    link_marker = f"](/consuming/{slug})"

    original = index_path.read_text(encoding="utf-8")
    # Preserve trailing-newline style: splitlines() drops it; we re-add below.
    lines = original.splitlines()

    # --- 1. drop any existing bullet for this slug (idempotency). ---
    lines = [ln for ln in lines if link_marker not in ln]

    # --- locate the trailing HTML comment block so we never insert past it. ---
    # Everything from the first `<!--` after the last content line is treated as
    # the trailing structure comment and kept at the bottom.
    trailing_start = len(lines)
    for i, ln in enumerate(lines):
        if ln.lstrip().startswith("<!--"):
            trailing_start = i
            break
    head = lines[:trailing_start]
    trailing = lines[trailing_start:]

    # --- 2/3. find or create the year + month section within `head`. ---
    # Collect indices of all year headings.
    year_positions: list[tuple[int, int]] = []  # (line_index, year_value)
    for i, ln in enumerate(head):
        m = _YEAR_HEADING_RE.match(ln)
        if m:
            year_positions.append((i, int(m.group("year"))))

    def _section_bounds(start_idx: int, heading_re: re.Pattern[str]) -> int:
        """Return the end index (exclusive) of the section starting at
        ``start_idx`` (whose heading is at start_idx). The section ends at the
        next heading of the SAME level, or end of ``head``."""
        for j in range(start_idx + 1, len(head)):
            if heading_re.match(head[j]):
                return j
        return len(head)

    target_year_idx: int | None = None
    for idx, yr in year_positions:
        if yr == year:
            target_year_idx = idx
            break

    if target_year_idx is None:
        # Create a new `## <year>` section. Insert so years stay DESCENDING.
        # Find the first existing year that is smaller than ours; insert before
        # its heading. If none smaller (or no years at all), append after the
        # intro (i.e. at the end of `head`).
        insert_at = len(head)
        for idx, yr in year_positions:
            if yr < year:
                insert_at = idx
                break
        new_block = [f"## {year}", "", f"### {month_name}", "", bullet, ""]
        # Ensure a blank line separates the new block from preceding content.
        if insert_at > 0 and head[insert_at - 1].strip() != "":
            new_block = [""] + new_block
        head[insert_at:insert_at] = new_block
        _write_index(index_path, head, trailing, original)
        return True

    # Year section exists. Find its bounds, then locate the month within it.
    year_end = _section_bounds(target_year_idx, _YEAR_HEADING_RE)
    month_positions: list[tuple[int, str]] = []  # (line_index, month_name)
    for j in range(target_year_idx + 1, year_end):
        m = _MONTH_HEADING_RE.match(head[j])
        if m:
            month_positions.append((j, m.group("month")))

    target_month_idx: int | None = None
    for idx, mn in month_positions:
        if mn.lower() == month_name.lower():
            target_month_idx = idx
            break

    target_month_num = entry_date.month

    def _month_num(name: str) -> int:
        try:
            return _MONTH_NAMES.index(name.capitalize()) + 1
        except ValueError:
            return 0

    if target_month_idx is None:
        # Create a new `### <month>` subsection within this year. Months
        # DESCENDING: insert before the FIRST existing month smaller than ours.
        # If ours is the newest, the first existing month is already smaller, so
        # we land right after the `## <year>` heading (top of year). If ours is
        # the oldest, no existing month is smaller, so the default `year_end`
        # appends it after the last month in this year's section.
        insert_at = year_end
        for idx, mn in month_positions:
            if _month_num(mn) < target_month_num:
                insert_at = idx
                break
        new_block = [f"### {month_name}", "", bullet, ""]
        if insert_at > 0 and head[insert_at - 1].strip() != "":
            new_block = [""] + new_block
        head[insert_at:insert_at] = new_block
        _write_index(index_path, head, trailing, original)
        return True

    # Month subsection exists: insert the bullet at the TOP of its list (just
    # after the `### <month>` heading and any blank line).
    insert_at = target_month_idx + 1
    while insert_at < year_end and head[insert_at].strip() == "":
        insert_at += 1
    head[insert_at:insert_at] = [bullet]
    _write_index(index_path, head, trailing, original)
    return True


def _write_index(
    index_path: Path,
    head: list[str],
    trailing: list[str],
    original: str,
) -> None:
    """Join head + trailing back into a document and write it, preserving the
    original file's trailing-newline style."""
    new_lines = head + trailing
    new_doc = "\n".join(new_lines)
    if original.endswith("\n"):
        new_doc += "\n"
    index_path.write_text(new_doc, encoding="utf-8")


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def publish_note(
    transcript_md_path: Path,
    *,
    existing_slug: str | None = None,
) -> PublishResult:
    """Publish an Obsidian voice-memo transcript to the personal site.

    Deterministic -- NO LLM call. The published note's title is the source's
    YAML title, the description is the first ~200 chars of the cleaned
    transcript, and the body is the cleaned transcript verbatim.

    Parameters
    ----------
    transcript_md_path:
        Source Obsidian `.md` file (cleaned transcript).
    existing_slug:
        If this source has been published before, the slug it was published
        under. When provided, we OVERWRITE that file in `personal_site` and
        skip slug-collision resolution -- otherwise an edited source would
        be republished as `<slug>-2.md` and the old `.md` would stay live,
        creating duplicates and a republish-every-tick loop. The watcher
        passes this from `PublishedLedger.slug_for_source(source)`.

    On a successful push we also write `published_at: <iso>` back into the
    source Obsidian file's frontmatter so the user can see when the web
    version was last refreshed.

    Returns a `PublishResult`. Never raises.
    """
    transcript_md_path = Path(transcript_md_path)
    logger.info("[publish] START %s (existing_slug=%s)",
                transcript_md_path, existing_slug)

    # Publishing is opt-in. If NOTES_SITE_ROOT was never configured,
    # config.SITE_ROOT / config.SITE_NOTES_DIR are None and every downstream
    # path operation would crash on a None. Fail fast with a clear, actionable
    # error instead of an opaque AttributeError/TypeError mid-flow.
    if config.SITE_ROOT is None or config.SITE_NOTES_DIR is None:
        msg = (
            "Publishing is disabled: NOTES_SITE_ROOT is not set, so there is "
            "no site repo to publish to. Set NOTES_SITE_ROOT in your .env to "
            "enable the #publish feature."
        )
        logger.error(msg)
        return _result(status="error", error=msg)

    try:
        md = transcript_md_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        return _result(status="error", error=f"Transcript file not found: {exc}")
    except OSError as exc:
        return _result(status="error", error=f"Failed to read transcript: {exc}")

    # --- 1. parse Obsidian frontmatter + body ---
    fm, body = _split_frontmatter(md)
    raw_title = fm.get("title")
    # Treat YAML null / missing key both as "no title". A line like `title:`
    # with no value parses as None, and `str(None).strip()` would otherwise
    # become the literal string "None" and get published as the article title.
    if raw_title is None:
        source_title = ""
    else:
        source_title = str(raw_title).strip()
    if not source_title:
        msg = (
            f"Source {transcript_md_path.name} has no `title:` in frontmatter "
            "(or it's blank); refusing to publish without a title."
        )
        logger.error(msg)
        return _result(status="error", error=msg)

    # --- 2. extract cleaned transcript text ---
    cleaned_transcript = _extract_transcript_section(body)
    if not cleaned_transcript.strip():
        return _result(
            status="skipped",
            reason="Transcript section is empty.",
            slug=None,
        )

    # --- 3. build deterministic published markdown ---
    today_iso = date.today().isoformat()
    description = _build_description(cleaned_transcript)
    published_md = _build_published_md(
        title=source_title,
        description=description,
        transcript_text=cleaned_transcript,
        today_iso=today_iso,
    )
    content_hash = _compute_content_hash(source_title, cleaned_transcript)
    logger.info(
        "[publish] built doc title=%r description_len=%d body_len=%d hash=%s",
        source_title, len(description), len(cleaned_transcript), content_hash[:12],
    )

    # Used by the rest of the flow (git, slug, etc.) under the names that
    # the existing rollback code expects.
    llm_output = published_md
    out_fm = {"title": source_title}

    # --- 4. git status check (must happen before pull) ---
    site_root = config.SITE_ROOT
    try:
        if not _working_tree_clean(site_root):
            msg = (
                f"personal_site working tree is dirty at {site_root}; "
                "commit or stash before publishing."
            )
            logger.error(msg)
            return _result(status="error", error=msg)
    except subprocess.CalledProcessError as exc:
        return _result(
            status="error",
            error=f"git status failed: {exc.stderr.strip() or exc}",
        )
    except FileNotFoundError as exc:
        return _result(status="error", error=f"git not found: {exc}")

    # --- 7. pull --ff-only FIRST, before any slug resolution ---
    # CRITICAL: We must pull before resolving the slug so that if another
    # machine published the same slug, our collision-resolution sees their
    # file and picks `<slug>-2.md` instead of clobbering it.
    try:
        _git(["pull", "--ff-only", "origin", "main"], cwd=site_root)
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        msg = f"git pull --ff-only failed: {stderr or exc}"
        logger.error(msg)
        return _result(status="error", error=msg)

    # Capture HEAD *after* pull so we can detect unexpected moves before push.
    head_before_commit = _current_head(site_root)

    # --- 8. slug + collision resolution (against post-pull state) ---
    # Check both SITE_NOTES_DIR (in case another machine published the same
    # slug) and DRAFTS_DIR (in case a stale draft sits there from a prior
    # failed run). Both must be uncontested before we pick the slug.
    slug_source = str(out_fm.get("slug", "")).strip() or str(out_fm["title"])
    slug_base = _slugify(slug_source)
    if existing_slug:
        # Republish path: reuse the slug the source was originally published
        # under. We OVERWRITE the existing `<slug>.md` in personal_site so the
        # URL stays stable and we don't create `<slug>-2.md` duplicates with
        # every edit cycle.
        slug = existing_slug
        if slug != slug_base:
            logger.info(
                "[publish] reusing existing slug %r (title-derived would be %r)",
                slug, slug_base,
            )
    else:
        # First-time publish: avoid collisions with any pre-existing notes
        # (e.g. another machine published the same slug, or a stale draft).
        slug = _resolve_slug_collision(
            slug_base, config.SITE_NOTES_DIR, config.DRAFTS_DIR
        )

    # --- 9. write staging draft ---
    draft_path = config.DRAFTS_DIR / f"{slug}.md"
    try:
        config.DRAFTS_DIR.mkdir(parents=True, exist_ok=True)
        draft_path.write_text(llm_output, encoding="utf-8")
    except OSError as exc:
        return _result(status="error", slug=slug, error=f"Failed to write draft: {exc}")

    # --- 10. copy draft into site ---
    site_path = config.SITE_NOTES_DIR / f"{slug}.md"
    try:
        config.SITE_NOTES_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(draft_path, site_path)
    except OSError as exc:
        return _result(
            status="error", slug=slug, error=f"Failed to copy to site: {exc}"
        )

    # --- 11/12. git add + commit ---
    rel_site_path = site_path.relative_to(site_root).as_posix()
    title = str(out_fm["title"])
    commit_message = f"note: {title}"

    try:
        _git(["add", "--", rel_site_path], cwd=site_root)
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        # Recover the site tree: the file was copied in but never staged, so
        # it sits in the working tree as an untracked file. Leaving it there
        # makes the next watcher tick's `_working_tree_clean` check fail
        # forever. Mirror the commit-failure branch's cleanup.
        try:
            site_path.unlink(missing_ok=True)
        except OSError as unlink_exc:
            logger.warning(
                "Could not unlink %s after git add failure: %s",
                site_path,
                unlink_exc,
            )
        return _result(
            status="error",
            slug=slug,
            site_path=site_path,
            error=f"git add failed: {stderr or exc}",
        )

    try:
        _git(["commit", "-m", commit_message], cwd=site_root)
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        # Recover the site tree: unstage AND remove the file. If we leave the
        # file behind, the next watcher tick sees a "dirty tree" and aborts
        # forever until the user manually cleans up.
        _git_ok(["reset", "HEAD", "--", rel_site_path], cwd=site_root)
        try:
            site_path.unlink(missing_ok=True)
        except OSError as unlink_exc:
            logger.warning(
                "Could not unlink %s after commit failure: %s",
                site_path,
                unlink_exc,
            )
        return _result(
            status="error",
            slug=slug,
            site_path=site_path,
            error=f"git commit failed: {stderr or exc}",
        )

    commit_sha = _current_head(site_root)

    # Sanity-check: HEAD should be exactly one commit ahead of head_before_commit.
    # If it isn't, something raced us (another commit landed between our pull
    # and our commit). We DO NOT auto-recover — that would risk destroying
    # whatever else landed. The commit succeeded, so the file is in HEAD; we
    # also can't safely unlink it because it's tracked. The user must
    # manually decide what to do.
    try:
        parent = _git(["rev-parse", f"{commit_sha}^"], cwd=site_root).stdout.strip()
    except subprocess.CalledProcessError:
        parent = ""
    if parent != head_before_commit:
        msg = (
            "HEAD moved unexpectedly between commit and push "
            f"(expected parent={head_before_commit}, got parent={parent}); "
            "aborting push. Manual recovery needed: inspect git log in "
            f"{site_root}, then either `git reset --soft {head_before_commit}` "
            f"and `rm {site_path}` to undo, or `git push origin main` to "
            "accept the commit. After recovery, remove the #publish tag from "
            f"{transcript_md_path} and re-tag to retry."
        )
        logger.error(msg)
        return _result(
            status="manual_recovery",
            slug=slug,
            site_path=site_path,
            commit_sha=commit_sha,
            error=msg,
        )

    # --- 13. push ---
    push_proc = _git_ok(["push", "origin", "main"], cwd=site_root)
    if push_proc.returncode != 0:
        stderr = (push_proc.stderr or "").strip()
        msg = f"git push failed (returncode={push_proc.returncode}): {stderr}"
        logger.error(msg)

        # Rollback strategy (P1-safe):
        # A naive `reset --hard <head_before_commit>` would be whole-tree
        # destructive: between our initial `_working_tree_clean` check and
        # this push attempt (potentially many seconds later) the user or
        # another tool may have edited unrelated files in site_root (e.g.
        # another note they're mid-draft). We refuse to nuke those.
        #
        # Steps:
        #   1) Sanity-check HEAD layout: our commit must be EXACTLY one
        #      commit on top of head_before_commit. If the user committed
        #      their own work on top of ours (e.g. from another terminal),
        #      HEAD~1 != head_before_commit and we bail to manual recovery.
        #   2) `reset --soft head_before_commit` to undo just our commit
        #      (keeps our file staged, keeps any unrelated dirty files
        #      exactly as the user had them).
        #   3) Inspect `git status --porcelain`. If anything other than
        #      rel_site_path shows up, the user has unrelated changes we
        #      must not touch -- bail to manual recovery.
        #   4) Otherwise do a targeted cleanup: unstage rel_site_path and
        #      unlink the file on disk.

        # Step 1: HEAD layout sanity-check.
        ok_parent, parent_out = _try_recovery_git(
            ["rev-parse", "HEAD~1"], site_root, "parent-sanity"
        )
        if not ok_parent:
            recovery_msg = (
                f"Push failed AND rollback step 'parent-sanity' (git rev-parse "
                f"HEAD~1) failed: {parent_out}. Refusing to proceed with "
                f"rollback. The local commit {commit_sha} is still in HEAD. "
                f"Manual recovery needed in {site_root}. Source transcript: "
                f"{transcript_md_path}. Original push error: {stderr}"
            )
            logger.error(recovery_msg)
            return _result(
                status="manual_recovery",
                slug=slug,
                site_path=site_path,
                commit_sha=commit_sha,
                error=recovery_msg,
            )
        parent_of_head = parent_out.strip()
        if parent_of_head != head_before_commit:
            recovery_msg = (
                f"Push failed AND HEAD has moved unexpectedly since our commit. "
                f"Local commit was {commit_sha} on top of {head_before_commit}, "
                f"but HEAD~1 is now {parent_of_head!r}. Manual recovery needed: "
                f"check `git log` in {site_root} and decide whether to keep or "
                f"drop the local commit. Source transcript: {transcript_md_path}. "
                f"Original push error: {stderr}"
            )
            logger.error(recovery_msg)
            return _result(
                status="manual_recovery",
                slug=slug,
                site_path=site_path,
                commit_sha=commit_sha,
                error=recovery_msg,
            )

        # Step 2: soft-reset to undo just our commit (keeps file in index
        # and leaves any unrelated dirty files untouched). If THIS fails
        # (e.g. index lock from another git process, repo corruption), we
        # MUST NOT proceed — the next steps would otherwise run against a
        # still-committed tree, see no unrelated changes, and unlink the
        # file while the publish commit is still HEAD.
        ok_reset, reset_out = _try_recovery_git(
            ["reset", "--soft", head_before_commit], site_root, "soft-reset"
        )
        if not ok_reset:
            recovery_msg = (
                f"Push failed AND rollback step 'soft-reset' "
                f"(git reset --soft {head_before_commit}) failed: {reset_out}. "
                f"The local commit {commit_sha} is still in HEAD and the file "
                f"{site_path} is still tracked. Refusing to proceed with "
                f"further rollback steps to avoid data loss. Manual recovery "
                f"needed in {site_root}. Source transcript: "
                f"{transcript_md_path}. Original push error: {stderr}"
            )
            logger.error(recovery_msg)
            return _result(
                status="manual_recovery",
                slug=slug,
                site_path=site_path,
                commit_sha=commit_sha,
                error=recovery_msg,
            )

        # Step 3: inspect the tree. Nothing other than our file should be
        # staged, modified, or untracked.
        ok_status, status_out = _try_recovery_git(
            ["status", "--porcelain"], site_root, "status-after-reset"
        )
        if not ok_status:
            recovery_msg = (
                f"Push failed AND rollback step 'status-after-reset' "
                f"(git status --porcelain) failed: {status_out}. The "
                f"soft-reset succeeded but we cannot verify the working tree "
                f"is safe to clean up. Refusing to unlink {site_path} blind. "
                f"Manual recovery needed in {site_root}. Source transcript: "
                f"{transcript_md_path}. Original push error: {stderr}"
            )
            logger.error(recovery_msg)
            return _result(
                status="manual_recovery",
                slug=slug,
                site_path=site_path,
                commit_sha=commit_sha,
                error=recovery_msg,
            )
        status_lines = [
            line for line in status_out.splitlines() if line.strip()
        ]

        def _path_from_porcelain(line: str) -> str:
            # Porcelain format: "XY path" or "XY orig -> path" for renames.
            body = line[3:] if len(line) >= 3 else line
            if " -> " in body:
                body = body.split(" -> ", 1)[1]
            return body.strip().strip('"')

        unrelated = [
            line for line in status_lines
            if _path_from_porcelain(line) != rel_site_path
        ]
        if unrelated:
            recovery_msg = (
                f"Push failed AND {site_root} has unrelated uncommitted changes. "
                f"Refusing to auto-rollback to avoid data loss. Unrelated status "
                f"lines: {unrelated!r}. Manual recovery: review `git status` in "
                f"{site_root}. To remove just our note: `git restore --staged "
                f"{rel_site_path} && rm {site_path}`. To keep the un-pushed "
                f"commit instead, run `git reset --soft HEAD@{{1}}`. Source "
                f"transcript: {transcript_md_path}. Original push error: {stderr}"
            )
            logger.error(recovery_msg)
            return _result(
                status="manual_recovery",
                slug=slug,
                site_path=site_path,
                commit_sha=commit_sha,
                error=recovery_msg,
            )

        # Step 4: targeted cleanup of our file only.
        ok_unstage, unstage_out = _try_recovery_git(
            ["reset", "HEAD", "--", rel_site_path], site_root, "unstage-our-file"
        )
        if not ok_unstage:
            recovery_msg = (
                f"Push failed AND rollback step 'unstage-our-file' "
                f"(git reset HEAD -- {rel_site_path}) failed: {unstage_out}. "
                f"Refusing to unlink {site_path} when we cannot guarantee the "
                f"index state. Manual recovery needed in {site_root}. Source "
                f"transcript: {transcript_md_path}. Original push error: "
                f"{stderr}"
            )
            logger.error(recovery_msg)
            return _result(
                status="manual_recovery",
                slug=slug,
                site_path=site_path,
                commit_sha=commit_sha,
                error=recovery_msg,
            )

        # Only NOW unlink site_path: every recovery git command above
        # returned rc=0.
        try:
            site_path.unlink(missing_ok=True)
        except OSError as unlink_exc:
            logger.warning(
                "Could not unlink %s after push rollback: %s",
                site_path,
                unlink_exc,
            )

        logger.warning(
            "Push failed; rolled back local commit %s back to %s and removed %s",
            commit_sha,
            head_before_commit,
            rel_site_path,
        )
        return _result(
            status="error",
            slug=slug,
            site_path=site_path,
            commit_sha=commit_sha,
            error=msg,
        )

    # --- 14. success: write `published_at` back to the source so the user
    # sees the timestamp in Obsidian, and surface the content_hash so the
    # watcher can decide whether to republish next tick.
    published_at_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    wrote_back = _write_source_published_at(transcript_md_path, published_at_iso)
    if wrote_back:
        logger.info(
            "[publish] wrote published_at=%s back to %s",
            published_at_iso, transcript_md_path.name,
        )
        # Honest reporting: only surface the timestamp if we actually
        # persisted it. Otherwise downstream callers (and the user) might
        # mistakenly believe the source frontmatter reflects this publish.
        result_published_at: str | None = published_at_iso
    else:
        logger.warning(
            "[publish] could not write published_at back to %s; "
            "publish still considered successful, but the source "
            "frontmatter was NOT updated.",
            transcript_md_path,
        )
        result_published_at = None

    logger.info(
        "[publish] DONE %s -> %s (commit=%s, hash=%s, source_updated=%s)",
        transcript_md_path.name, slug,
        commit_sha[:8] if commit_sha else "?", content_hash[:12], wrote_back,
    )
    return _result(
        status="published",
        slug=slug,
        commit_sha=commit_sha,
        site_path=site_path,
        content_hash=content_hash,
        published_at=result_published_at,
    )


def publish_clipping(
    clipping_md_path: Path,
    *,
    existing_slug: str | None = None,
) -> PublishResult:
    """Publish an Obsidian web clipping to the personal site's /consuming section.

    Deterministic -- NO LLM call. Mirrors :func:`publish_note` step-for-step
    (opt-in guard, dirty-tree check, pull --ff-only, slug-collision, write,
    commit, push, ledger writeback, recovery) but:

      - writes a ``/consuming/<Slug>`` page (Title-Case-hyphen slug) built from
        the clipping's frontmatter + body via :func:`_build_consuming_md`,
      - ALSO updates the consuming ``index.md`` and stages BOTH the new page and
        the index in the SAME commit, pushed once,
      - hashes (title + comment + body) so editing the comment OR the body
        triggers a republish (kept separate from the transcript hash).

    Parameters mirror :func:`publish_note`. ``existing_slug`` reuses the slug a
    prior publish used (republish-in-place, no ``<slug>-2`` churn).

    Returns a :class:`PublishResult`. Never raises.
    """
    clipping_md_path = Path(clipping_md_path)
    logger.info("[clipping] START %s (existing_slug=%s)",
                clipping_md_path, existing_slug)

    # Opt-in guard: clippings publish to /consuming, which needs both SITE_ROOT
    # and SITE_CONSUMING_DIR. Fail fast with an actionable message rather than
    # crash on a None path mid-flow (mirrors publish_note).
    if config.SITE_ROOT is None or config.SITE_CONSUMING_DIR is None:
        msg = (
            "Publishing is disabled: NOTES_SITE_ROOT is not set, so there is "
            "no site repo to publish clippings to. Set NOTES_SITE_ROOT in your "
            ".env to enable the #publish feature."
        )
        logger.error(msg)
        return _result(status="error", error=msg)

    try:
        md = clipping_md_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        return _result(status="error", error=f"Clipping file not found: {exc}")
    except OSError as exc:
        return _result(status="error", error=f"Failed to read clipping: {exc}")

    # --- 1. parse Obsidian frontmatter + body ---
    fm, clipping_body = _split_frontmatter(md)
    raw_title = fm.get("title")
    if raw_title is None:
        source_title = ""
    else:
        source_title = str(raw_title).strip()
    if not source_title:
        msg = (
            f"Clipping {clipping_md_path.name} has no `title:` in frontmatter "
            "(or it's blank); refusing to publish without a title."
        )
        logger.error(msg)
        return _result(status="error", error=msg)

    # --- 2. read the clipping-specific fields ---
    source_url = ""
    raw_source = fm.get("source")
    if raw_source is not None:
        source_url = str(raw_source).strip()
    authors = _normalize_authors(fm.get("author"))
    comment_raw = fm.get(config.CLIPPING_COMMENT_FIELD)
    comment = "" if comment_raw is None else str(comment_raw).strip()

    # `created` may be a date object (PyYAML parses bare dates), a string, or
    # missing. Fall back to today's date so the page + index always have one.
    raw_created = fm.get("created")
    if raw_created is None or str(raw_created).strip() == "":
        created_iso = date.today().isoformat()
        entry_date = date.today()
    else:
        created_iso = str(raw_created).strip()
        entry_date = _parse_iso_date(created_iso) or date.today()

    clipping_description = ""
    raw_desc = fm.get("description")
    if raw_desc is not None:
        clipping_description = str(raw_desc).strip()

    # --- 3. build deterministic /consuming page ---
    # Index/meta description: prefer a one-line excerpt of the comment, else the
    # clip description, else the title.
    if comment:
        description = _comment_excerpt(comment)
    elif clipping_description:
        description = re.sub(r"\s+", " ", clipping_description)
    else:
        description = source_title

    consuming_md = _build_consuming_md(
        title=source_title,
        description=description,
        comment=comment,
        source_url=source_url,
        authors=authors,
        created_iso=created_iso,
        clipping_body=clipping_body,
    )
    content_hash = _compute_clipping_content_hash(
        source_title, comment, clipping_body
    )
    logger.info(
        "[clipping] built page title=%r has_comment=%s body_len=%d hash=%s",
        source_title, bool(comment), len(clipping_body), content_hash[:12],
    )

    # --- 4. git status check (must happen before pull) ---
    site_root = config.SITE_ROOT
    consuming_dir = config.SITE_CONSUMING_DIR
    try:
        if not _working_tree_clean(site_root):
            msg = (
                f"personal_site working tree is dirty at {site_root}; "
                "commit or stash before publishing."
            )
            logger.error(msg)
            return _result(status="error", error=msg)
    except subprocess.CalledProcessError as exc:
        return _result(
            status="error",
            error=f"git status failed: {exc.stderr.strip() or exc}",
        )
    except FileNotFoundError as exc:
        return _result(status="error", error=f"git not found: {exc}")

    # --- 5. pull --ff-only FIRST, before slug resolution ---
    try:
        _git(["pull", "--ff-only", "origin", "main"], cwd=site_root)
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        msg = f"git pull --ff-only failed: {stderr or exc}"
        logger.error(msg)
        return _result(status="error", error=msg)

    head_before_commit = _current_head(site_root)

    # --- 6. slug + collision resolution (against post-pull state) ---
    slug_base = _consuming_slugify(source_title)
    if existing_slug:
        slug = existing_slug
        if slug != slug_base:
            logger.info(
                "[clipping] reusing existing slug %r (title-derived would be %r)",
                slug, slug_base,
            )
    else:
        slug = _resolve_slug_collision(slug_base, consuming_dir, config.DRAFTS_DIR)

    # --- 7. write staging draft ---
    draft_path = config.DRAFTS_DIR / f"{slug}.md"
    try:
        config.DRAFTS_DIR.mkdir(parents=True, exist_ok=True)
        draft_path.write_text(consuming_md, encoding="utf-8")
    except OSError as exc:
        return _result(status="error", slug=slug, error=f"Failed to write draft: {exc}")

    # --- 8. copy draft into the site /consuming dir ---
    site_path = consuming_dir / f"{slug}.md"
    try:
        consuming_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(draft_path, site_path)
    except OSError as exc:
        return _result(
            status="error", slug=slug, error=f"Failed to copy to site: {exc}"
        )

    # --- 9. update the consuming index.md (idempotent; in place on republish) ---
    index_path = consuming_dir / "index.md"
    index_existed = index_path.exists()
    if index_existed:
        try:
            _update_consuming_index(
                index_path,
                title=source_title,
                slug=slug,
                description=description,
                entry_date=entry_date,
            )
        except OSError as exc:
            # Recover: remove the page we copied so the tree doesn't go dirty
            # forever, then report the error.
            try:
                site_path.unlink(missing_ok=True)
            except OSError:
                pass
            return _result(
                status="error",
                slug=slug,
                site_path=site_path,
                error=f"Failed to update consuming index: {exc}",
            )
    else:
        # No index to update -- publish the page anyway, just log it. We do not
        # fabricate an index from scratch (its intro/structure is user-owned).
        logger.warning(
            "[clipping] consuming index %s does not exist; publishing the page "
            "without an index entry.", index_path,
        )

    # --- 10. git add (page + index) ---
    rel_site_path = site_path.relative_to(site_root).as_posix()
    rel_paths = [rel_site_path]
    if index_existed:
        rel_paths.append(index_path.relative_to(site_root).as_posix())
    commit_message = f"consuming: {source_title}"

    def _cleanup_page_and_index() -> None:
        """Best-effort: remove the page + revert the index so the tree is clean
        again after an add/commit failure."""
        try:
            site_path.unlink(missing_ok=True)
        except OSError as unlink_exc:
            logger.warning("Could not unlink %s: %s", site_path, unlink_exc)
        if index_existed:
            # Restore the committed version of the index (discards our edit).
            _git_ok(["checkout", "HEAD", "--", rel_paths[1]], cwd=site_root)

    try:
        _git(["add", "--", *rel_paths], cwd=site_root)
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        _cleanup_page_and_index()
        # Unstage anything that did get staged before the failure.
        _git_ok(["reset", "HEAD", "--", *rel_paths], cwd=site_root)
        return _result(
            status="error",
            slug=slug,
            site_path=site_path,
            error=f"git add failed: {stderr or exc}",
        )

    try:
        _git(["commit", "-m", commit_message], cwd=site_root)
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        _git_ok(["reset", "HEAD", "--", *rel_paths], cwd=site_root)
        _cleanup_page_and_index()
        return _result(
            status="error",
            slug=slug,
            site_path=site_path,
            error=f"git commit failed: {stderr or exc}",
        )

    commit_sha = _current_head(site_root)

    # Sanity-check: HEAD should be exactly one commit ahead of head_before_commit.
    try:
        parent = _git(["rev-parse", f"{commit_sha}^"], cwd=site_root).stdout.strip()
    except subprocess.CalledProcessError:
        parent = ""
    if parent != head_before_commit:
        msg = (
            "HEAD moved unexpectedly between commit and push "
            f"(expected parent={head_before_commit}, got parent={parent}); "
            "aborting push. Manual recovery needed: inspect git log in "
            f"{site_root}, then either `git reset --soft {head_before_commit}` "
            f"to undo, or `git push origin main` to accept the commit. After "
            f"recovery, remove the #publish tag from {clipping_md_path} and "
            "re-tag to retry."
        )
        logger.error(msg)
        return _result(
            status="manual_recovery",
            slug=slug,
            site_path=site_path,
            commit_sha=commit_sha,
            error=msg,
        )

    # --- 11. push ---
    push_proc = _git_ok(["push", "origin", "main"], cwd=site_root)
    if push_proc.returncode != 0:
        stderr = (push_proc.stderr or "").strip()
        msg = f"git push failed (returncode={push_proc.returncode}): {stderr}"
        logger.error(msg)

        # P1-safe rollback, mirroring publish_note: soft-reset our commit, make
        # sure ONLY our files are dirty, then targeted cleanup. Our commit
        # touches the page AND (maybe) the index, so the "expected dirty set"
        # is `rel_paths` rather than a single file.
        ok_parent, parent_out = _try_recovery_git(
            ["rev-parse", "HEAD~1"], site_root, "parent-sanity"
        )
        if not ok_parent:
            recovery_msg = (
                f"Push failed AND rollback step 'parent-sanity' (git rev-parse "
                f"HEAD~1) failed: {parent_out}. The local commit {commit_sha} "
                f"is still in HEAD. Manual recovery needed in {site_root}. "
                f"Source clipping: {clipping_md_path}. Original push error: "
                f"{stderr}"
            )
            logger.error(recovery_msg)
            return _result(
                status="manual_recovery", slug=slug, site_path=site_path,
                commit_sha=commit_sha, error=recovery_msg,
            )
        if parent_out.strip() != head_before_commit:
            recovery_msg = (
                f"Push failed AND HEAD has moved unexpectedly since our commit. "
                f"Local commit was {commit_sha} on top of {head_before_commit}, "
                f"but HEAD~1 is now {parent_out.strip()!r}. Manual recovery "
                f"needed: check `git log` in {site_root}. Source clipping: "
                f"{clipping_md_path}. Original push error: {stderr}"
            )
            logger.error(recovery_msg)
            return _result(
                status="manual_recovery", slug=slug, site_path=site_path,
                commit_sha=commit_sha, error=recovery_msg,
            )

        ok_reset, reset_out = _try_recovery_git(
            ["reset", "--soft", head_before_commit], site_root, "soft-reset"
        )
        if not ok_reset:
            recovery_msg = (
                f"Push failed AND rollback step 'soft-reset' "
                f"(git reset --soft {head_before_commit}) failed: {reset_out}. "
                f"The local commit {commit_sha} is still in HEAD. Refusing "
                f"further rollback to avoid data loss. Manual recovery needed "
                f"in {site_root}. Source clipping: {clipping_md_path}. Original "
                f"push error: {stderr}"
            )
            logger.error(recovery_msg)
            return _result(
                status="manual_recovery", slug=slug, site_path=site_path,
                commit_sha=commit_sha, error=recovery_msg,
            )

        ok_status, status_out = _try_recovery_git(
            ["status", "--porcelain"], site_root, "status-after-reset"
        )
        if not ok_status:
            recovery_msg = (
                f"Push failed AND rollback step 'status-after-reset' "
                f"(git status --porcelain) failed: {status_out}. The soft-reset "
                f"succeeded but we cannot verify the working tree is safe to "
                f"clean up. Manual recovery needed in {site_root}. Source "
                f"clipping: {clipping_md_path}. Original push error: {stderr}"
            )
            logger.error(recovery_msg)
            return _result(
                status="manual_recovery", slug=slug, site_path=site_path,
                commit_sha=commit_sha, error=recovery_msg,
            )

        def _path_from_porcelain(line: str) -> str:
            body = line[3:] if len(line) >= 3 else line
            if " -> " in body:
                body = body.split(" -> ", 1)[1]
            return body.strip().strip('"')

        expected = set(rel_paths)
        unrelated = [
            line for line in status_out.splitlines() if line.strip()
            and _path_from_porcelain(line) not in expected
        ]
        if unrelated:
            recovery_msg = (
                f"Push failed AND {site_root} has unrelated uncommitted changes. "
                f"Refusing to auto-rollback to avoid data loss. Unrelated status "
                f"lines: {unrelated!r}. Manual recovery: review `git status` in "
                f"{site_root}. Source clipping: {clipping_md_path}. Original "
                f"push error: {stderr}"
            )
            logger.error(recovery_msg)
            return _result(
                status="manual_recovery", slug=slug, site_path=site_path,
                commit_sha=commit_sha, error=recovery_msg,
            )

        # Targeted cleanup: unstage our files, remove the page, restore the index.
        ok_unstage, unstage_out = _try_recovery_git(
            ["reset", "HEAD", "--", *rel_paths], site_root, "unstage-our-files"
        )
        if not ok_unstage:
            recovery_msg = (
                f"Push failed AND rollback step 'unstage-our-files' "
                f"(git reset HEAD -- {rel_paths}) failed: {unstage_out}. "
                f"Refusing to clean up when we cannot guarantee the index "
                f"state. Manual recovery needed in {site_root}. Source "
                f"clipping: {clipping_md_path}. Original push error: {stderr}"
            )
            logger.error(recovery_msg)
            return _result(
                status="manual_recovery", slug=slug, site_path=site_path,
                commit_sha=commit_sha, error=recovery_msg,
            )

        try:
            site_path.unlink(missing_ok=True)
        except OSError as unlink_exc:
            logger.warning(
                "Could not unlink %s after push rollback: %s",
                site_path, unlink_exc,
            )
        if index_existed:
            _try_recovery_git(
                ["checkout", "HEAD", "--", rel_paths[1]],
                site_root, "restore-index",
            )

        logger.warning(
            "Push failed; rolled back local commit %s back to %s and removed %s",
            commit_sha, head_before_commit, rel_site_path,
        )
        return _result(
            status="error",
            slug=slug,
            site_path=site_path,
            commit_sha=commit_sha,
            error=msg,
        )

    # --- 12. success: write `published_at` back to the source clipping ---
    published_at_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    wrote_back = _write_source_published_at(clipping_md_path, published_at_iso)
    if wrote_back:
        logger.info(
            "[clipping] wrote published_at=%s back to %s",
            published_at_iso, clipping_md_path.name,
        )
        result_published_at: str | None = published_at_iso
    else:
        logger.warning(
            "[clipping] could not write published_at back to %s; publish still "
            "considered successful, but the source frontmatter was NOT updated.",
            clipping_md_path,
        )
        result_published_at = None

    logger.info(
        "[clipping] DONE %s -> %s (commit=%s, hash=%s, source_updated=%s)",
        clipping_md_path.name, slug,
        commit_sha[:8] if commit_sha else "?", content_hash[:12], wrote_back,
    )
    return _result(
        status="published",
        slug=slug,
        commit_sha=commit_sha,
        site_path=site_path,
        content_hash=content_hash,
        published_at=result_published_at,
    )
