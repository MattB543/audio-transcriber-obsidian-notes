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
