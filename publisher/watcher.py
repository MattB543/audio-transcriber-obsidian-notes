"""Background watcher that polls TRANSCRIPT_DIR for #publish-tagged notes.

Run as a module:
    python -m publisher.watcher

`scan_for_publishable()` is the stateless scan used by both `run_once` and
`run_forever`. The `PublishedLedger` helper persists the set of already-
published slugs to `config.PUBLISHED_LEDGER` so we don't republish on the
next poll cycle.

If a publish attempt returns ``status="manual_recovery"`` (the local repo
is in an unsafe-to-auto-touch state — see `publish.py`), we record the
source path in a sibling ``_blocked.json`` file alongside the published
ledger. Blocked sources are skipped by `scan_for_publishable` so the
watcher does not retry them on the next 60s tick. To unblock, the user can
either edit `_blocked.json` directly to remove the entry or call
`publisher.watcher.clear_blocked(source_path)`.
"""

from __future__ import annotations

import json
import logging
import re
import signal
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, TypedDict

import yaml

import config
from publisher.publish import (
    PublishResult,
    _compute_content_hash,
    _extract_transcript_section,
    _split_frontmatter,
    publish_note,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Hash helpers
# ---------------------------------------------------------------------------


def _hash_for_source(source_path: Path) -> str | None:
    """Compute the content hash of a source `.md` file (title + transcript).

    Returns None if the file can't be read or has no transcript section.
    Mirrors what `publish.py:publish_note` would hash so the comparison is
    apples-to-apples.
    """
    try:
        md = source_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    fm, body = _split_frontmatter(md)
    title = str(fm.get("title", "")).strip()
    transcript = _extract_transcript_section(body)
    if not title and not transcript:
        return None
    return _compute_content_hash(title, transcript)


# ---------------------------------------------------------------------------
# Published ledger
# ---------------------------------------------------------------------------


class LedgerEntry(TypedDict, total=False):
    """Single entry in `.published.json` -- one published note.

    `content_hash` was added in v2 (re-publish on edit). Older ledger entries
    won't have it; treat its absence as "definitely republish next time" so
    we re-sync the website with the latest source content on first run.
    """

    published_at: str  # ISO-8601 UTC
    site_path: str
    commit_sha: str
    source: str  # the transcript md file we published from
    content_hash: str  # SHA256 of (title + transcript) at publish time


class PublishedLedger:
    """JSON-backed set of published slugs.

    The on-disk shape is ``{<slug>: LedgerEntry}``. Reads/writes are synchronous;
    this is single-writer (the watcher loop) so we don't need locking.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path: Path = Path(path) if path is not None else config.PUBLISHED_LEDGER
        self._data: dict[str, LedgerEntry] = {}
        self.load()

    # -- persistence --------------------------------------------------------

    def load(self) -> None:
        """Load from disk. If the file is missing or malformed, start empty."""
        if not self.path.exists():
            self._data = {}
            return
        try:
            raw = self.path.read_text(encoding="utf-8").strip()
            if not raw:
                self._data = {}
                return
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                logger.warning(
                    "Ledger at %s is not a dict (%r); starting empty.",
                    self.path,
                    type(parsed),
                )
                self._data = {}
                return
            self._data = parsed
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to load ledger %s: %s; starting empty.", self.path, exc)
            self._data = {}

    def save(self) -> None:
        """Write atomically (tmp + replace)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(self._data, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        tmp.replace(self.path)

    # -- api ----------------------------------------------------------------

    def contains_source(self, source_path: Path) -> bool:
        """True iff the given transcript file has already been published."""
        return self._entry_for_source(source_path) is not None

    def _entry_for_source(self, source_path: Path) -> LedgerEntry | None:
        """Return the ledger entry whose `source` resolves to `source_path`."""
        target = str(source_path.resolve())
        for entry in self._data.values():
            try:
                if Path(entry.get("source", "")).resolve() == Path(target):
                    return entry
            except OSError:
                if entry.get("source") == target:
                    return entry
        return None

    def hash_for_source(self, source_path: Path) -> str | None:
        """Return the stored content_hash for a previously-published source,
        or None if the source isn't in the ledger or has no stored hash
        (older entry, pre-v2)."""
        entry = self._entry_for_source(source_path)
        if entry is None:
            return None
        h = entry.get("content_hash")
        return str(h) if h else None

    def slug_for_source(self, source_path: Path) -> str | None:
        """Return the slug a previously-published source was published under.

        The watcher passes this back into `publish_note(..., existing_slug=...)`
        so a republish OVERWRITES the existing `<slug>.md` instead of creating
        `<slug>-2.md` and looping.
        """
        target = str(source_path.resolve())
        for slug, entry in self._data.items():
            try:
                if Path(entry.get("source", "")).resolve() == Path(target):
                    return slug
            except OSError:
                if entry.get("source") == target:
                    return slug
        return None

    def contains_slug(self, slug: str) -> bool:
        return slug in self._data

    def mark_published(
        self,
        slug: str,
        *,
        site_path: Path,
        commit_sha: str,
        source: Path,
        content_hash: str | None = None,
    ) -> None:
        """Record a successful publish and persist."""
        entry = LedgerEntry(
            published_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            site_path=str(site_path),
            commit_sha=commit_sha,
            source=str(source.resolve()),
        )
        if content_hash:
            entry["content_hash"] = content_hash
        self._data[slug] = entry
        self.save()

    def as_dict(self) -> dict[str, LedgerEntry]:
        """Return a shallow copy of the underlying dict (for tests)."""
        return dict(self._data)


# ---------------------------------------------------------------------------
# Blocked ledger (manual-recovery cases)
# ---------------------------------------------------------------------------


class BlockedEntry(TypedDict):
    """Entry in the ``_blocked.json`` sidecar — one source path the watcher
    refuses to retry until the user clears it manually."""

    blocked_at: str        # ISO-8601 UTC
    error: str             # last error message from publish_note
    slug: str | None       # slug attempted (for cross-ref to git log)
    commit_sha: str | None  # un-pushed commit sha (so user can `git log`)
    blocked: bool          # always True; explicit flag for grep-ability


def _blocked_ledger_path(published_ledger_path: Path | None = None) -> Path:
    """Sidecar file alongside the published ledger.

    If `published_ledger_path` is e.g. `.../.published.json`, this returns
    `.../.published.blocked.json`. Centralising this keeps tests sane —
    they pass a custom `PublishedLedger.path` and the blocked file follows.
    """
    base = (
        Path(published_ledger_path)
        if published_ledger_path is not None
        else config.PUBLISHED_LEDGER
    )
    # Insert ".blocked" before the suffix so foo/.published.json -> foo/.published.blocked.json
    return base.with_suffix(".blocked" + base.suffix)


class BlockedLedger:
    """JSON-backed map of source path -> BlockedEntry.

    Sources in here are skipped by `scan_for_publishable` so the watcher
    does not loop on a repo state we cannot safely auto-recover. The on-disk
    shape is ``{<source_resolved_str>: BlockedEntry}``.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path: Path = (
            Path(path) if path is not None else _blocked_ledger_path()
        )
        self._data: dict[str, BlockedEntry] = {}
        self.load()

    # -- persistence --------------------------------------------------------

    def load(self) -> None:
        if not self.path.exists():
            self._data = {}
            return
        try:
            raw = self.path.read_text(encoding="utf-8").strip()
            if not raw:
                self._data = {}
                return
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                logger.warning(
                    "Blocked ledger at %s is not a dict (%r); starting empty.",
                    self.path,
                    type(parsed),
                )
                self._data = {}
                return
            self._data = parsed
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(
                "Failed to load blocked ledger %s: %s; starting empty.",
                self.path,
                exc,
            )
            self._data = {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(self._data, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        tmp.replace(self.path)

    # -- api ----------------------------------------------------------------

    @staticmethod
    def _key(source_path: Path) -> str:
        try:
            return str(Path(source_path).resolve())
        except OSError:
            return str(source_path)

    def contains_source(self, source_path: Path) -> bool:
        return self._key(source_path) in self._data

    def block(
        self,
        source_path: Path,
        *,
        error: str,
        slug: str | None,
        commit_sha: str | None,
    ) -> None:
        """Record a source as blocked and persist."""
        self._data[self._key(source_path)] = BlockedEntry(
            blocked_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            error=error,
            slug=slug,
            commit_sha=commit_sha,
            blocked=True,
        )
        self.save()

    def unblock(self, source_path: Path) -> bool:
        """Remove a source from the blocked set. Returns True if it existed."""
        key = self._key(source_path)
        if key in self._data:
            del self._data[key]
            self.save()
            return True
        return False

    def as_dict(self) -> dict[str, BlockedEntry]:
        return dict(self._data)


def clear_blocked(
    source_path: Path,
    *,
    blocked_ledger: BlockedLedger | None = None,
) -> bool:
    """Convenience helper for the user to unblock a source after manual
    recovery. Returns True iff the source was previously blocked."""
    ledger = blocked_ledger or BlockedLedger()
    return ledger.unblock(Path(source_path))


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------


_PUBLISH_BODY_TAG_RE = re.compile(r"(^|\s)#publish(\s|$)")


def _has_publish_tag(md: str) -> bool:
    """Check both YAML frontmatter and body for a #publish trigger."""
    fm, body = _split_frontmatter(md)

    # YAML form: `tags: [publish, ...]` or `tags:\n  - publish`
    tags_val = fm.get("tags")
    if isinstance(tags_val, list):
        if any(str(t).strip().lstrip("#").lower() == "publish" for t in tags_val):
            return True
    elif isinstance(tags_val, str):
        # Single-string form, maybe comma-separated.
        for piece in re.split(r"[,\s]+", tags_val):
            if piece.strip().lstrip("#").lower() == "publish":
                return True

    # Body form: `#publish` standalone
    if _PUBLISH_BODY_TAG_RE.search(body):
        return True

    return False


def _iter_candidate_files(transcript_dir: Path) -> Iterable[Path]:
    """Yield `*.md` files in transcript_dir, excluding `*.raw.md`."""
    if not transcript_dir.exists():
        return
    for p in sorted(transcript_dir.glob("*.md")):
        if p.name.endswith(".raw.md"):
            continue
        yield p


def scan_for_publishable(
    *,
    transcript_dir: Path | None = None,
    ledger: PublishedLedger | None = None,
    blocked_ledger: BlockedLedger | None = None,
) -> list[Path]:
    """Return transcript md files that should be (re)published right now.

    A file is included if all of:
      - it has the `#publish` tag (frontmatter or body)
      - it's NOT in the blocked ledger
      - it's either (a) not in the published ledger at all, OR (b) in the
        ledger but its current content hash differs from the recorded one
        (the user edited the title or body since the last publish).

    Blocked sources (manual-recovery cases) are skipped so the watcher does
    not retry a publish that left the local repo in an unsafe-to-auto-touch
    state. To unblock, call :func:`clear_blocked` or edit the blocked
    ledger file directly.
    """
    transcript_dir = transcript_dir or config.TRANSCRIPT_DIR
    ledger = ledger or PublishedLedger()
    blocked_ledger = blocked_ledger or BlockedLedger(
        path=_blocked_ledger_path(ledger.path)
    )

    out: list[Path] = []
    for path in _iter_candidate_files(transcript_dir):
        if blocked_ledger.contains_source(path):
            logger.debug(
                "Skipping %s: source is in blocked ledger (manual recovery "
                "required; clear via publisher.watcher.clear_blocked).",
                path,
            )
            continue
        try:
            md = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            logger.warning("Could not read %s: %s", path, exc)
            continue
        try:
            if not _has_publish_tag(md):
                continue
        except yaml.YAMLError as exc:
            logger.warning("Skipping %s: bad YAML (%s)", path, exc)
            continue

        # Has #publish. Decide first-time vs republish-on-change.
        prev_hash = ledger.hash_for_source(path)
        if prev_hash is None and ledger.contains_source(path):
            # Old ledger entry without hash -- republish once to populate it.
            logger.info(
                "Re-publishing %s: ledger entry has no content_hash (old format).",
                path.name,
            )
            out.append(path)
            continue
        if prev_hash is None:
            # Never published.
            out.append(path)
            continue
        # Already published; compare hashes.
        current_hash = _hash_for_source(path)
        if current_hash is None:
            logger.debug("Could not compute hash for %s; skipping.", path)
            continue
        if current_hash != prev_hash:
            logger.info(
                "Re-publishing %s: content changed (hash %s -> %s).",
                path.name, prev_hash[:12], current_hash[:12],
            )
            out.append(path)
        # else: content unchanged, skip.
    return out


# ---------------------------------------------------------------------------
# Loop orchestration
# ---------------------------------------------------------------------------


def _process_one(
    path: Path,
    ledger: PublishedLedger,
    blocked_ledger: BlockedLedger | None = None,
) -> PublishResult:
    """Publish a single file and update ledgers as appropriate. Never raises.

    On ``status == "manual_recovery"`` the source is added to the blocked
    ledger so future scans skip it until the user calls
    :func:`clear_blocked`.
    """
    # If we've already published this source, reuse the prior slug so the
    # republish OVERWRITES the existing `<slug>.md` instead of bumping to
    # `<slug>-2.md` (which would leave the original stale + cause a republish
    # loop on every subsequent edit).
    existing_slug = ledger.slug_for_source(path)

    try:
        result = publish_note(path, existing_slug=existing_slug)
    except Exception as exc:  # noqa: BLE001 — defensive; publish_note shouldn't raise
        logger.exception("publish_note raised unexpectedly for %s", path)
        return PublishResult(
            status="error",
            slug=None,
            reason=None,
            commit_sha=None,
            site_path=None,
            error=f"unhandled: {exc}",
        )

    status = result["status"]
    if status == "published":
        slug = result.get("slug")
        site_path = result.get("site_path")
        commit_sha = result.get("commit_sha")
        content_hash = result.get("content_hash")
        if slug and site_path and commit_sha:
            try:
                ledger.mark_published(
                    slug,
                    site_path=site_path,
                    commit_sha=commit_sha,
                    source=path,
                    content_hash=content_hash,
                )
            except OSError as exc:
                logger.error("Failed to update ledger for %s: %s", slug, exc)
    elif status == "skipped":
        logger.info("Skipped %s: %s", path, result.get("reason"))
    elif status == "manual_recovery":
        # Block this source so the watcher does not loop on it. The user
        # must call clear_blocked() (or edit the blocked ledger) after
        # manually recovering the local repo.
        if blocked_ledger is None:
            blocked_ledger = BlockedLedger(
                path=_blocked_ledger_path(ledger.path)
            )
        try:
            blocked_ledger.block(
                path,
                error=result.get("error") or "(no error message)",
                slug=result.get("slug"),
                commit_sha=result.get("commit_sha"),
            )
        except OSError as exc:
            logger.error(
                "Failed to write blocked ledger for %s: %s; the source may "
                "be retried on the next tick.",
                path,
                exc,
            )
        logger.error(
            "Manual recovery required for %s: %s. Source has been added to "
            "the blocked ledger and will not be retried until "
            "publisher.watcher.clear_blocked() is called.",
            path,
            result.get("error"),
        )
    else:
        logger.error("Error publishing %s: %s", path, result.get("error"))
    return result


def run_once(
    *,
    transcript_dir: Path | None = None,
    ledger: PublishedLedger | None = None,
    blocked_ledger: BlockedLedger | None = None,
) -> list[PublishResult]:
    """Do a single scan + publish pass. Returns all results (errors included)."""
    ledger = ledger or PublishedLedger()
    blocked_ledger = blocked_ledger or BlockedLedger(
        path=_blocked_ledger_path(ledger.path)
    )
    transcript_dir = transcript_dir or config.TRANSCRIPT_DIR
    candidates = scan_for_publishable(
        transcript_dir=transcript_dir,
        ledger=ledger,
        blocked_ledger=blocked_ledger,
    )
    if not candidates:
        logger.debug("run_once: no new publishable notes in %s", transcript_dir)
        return []

    logger.info("run_once: %d candidate(s): %s", len(candidates), [p.name for p in candidates])
    results: list[PublishResult] = []
    for path in candidates:
        results.append(_process_one(path, ledger, blocked_ledger))
    return results


# Sentinel the SIGINT handler flips.
_stop_requested = False


def _handle_stop(signum: int, _frame: object) -> None:  # pragma: no cover
    global _stop_requested
    _stop_requested = True
    logger.info("Received signal %s; will stop after current cycle.", signum)


def run_forever(
    *,
    transcript_dir: Path | None = None,
    poll_seconds: int | None = None,
) -> None:
    """Poll forever. Ctrl+C or SIGTERM causes a clean exit after the current tick."""
    poll_seconds = poll_seconds if poll_seconds is not None else config.WATCHER_POLL_SECONDS
    ledger = PublishedLedger()
    blocked_ledger = BlockedLedger(path=_blocked_ledger_path(ledger.path))

    # Install signal handlers (best effort; on Windows SIGTERM may be unavailable).
    try:
        signal.signal(signal.SIGINT, _handle_stop)
    except (ValueError, OSError):  # pragma: no cover — not on main thread
        pass
    try:
        signal.signal(signal.SIGTERM, _handle_stop)
    except (ValueError, OSError, AttributeError):  # pragma: no cover
        pass

    logger.info(
        "Publisher watcher starting (transcript_dir=%s, poll=%ss)",
        transcript_dir or config.TRANSCRIPT_DIR,
        poll_seconds,
    )
    global _stop_requested
    _stop_requested = False

    while not _stop_requested:
        try:
            run_once(
                transcript_dir=transcript_dir,
                ledger=ledger,
                blocked_ledger=blocked_ledger,
            )
        except Exception:  # noqa: BLE001
            logger.exception("Unhandled error in watcher tick; continuing.")

        # Responsive sleep so Ctrl+C doesn't have to wait up to poll_seconds.
        slept = 0.0
        step = 1.0
        while slept < poll_seconds and not _stop_requested:
            time.sleep(min(step, poll_seconds - slept))
            slept += step

    logger.info("Publisher watcher stopped cleanly.")


# ---------------------------------------------------------------------------
# Module CLI
# ---------------------------------------------------------------------------


def _configure_cli_logging() -> None:
    root = logging.getLogger()
    if any(isinstance(h, logging.StreamHandler) for h in root.handlers):
        return
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)s %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    root.addHandler(handler)
    root.setLevel(logging.INFO)


def main() -> None:  # pragma: no cover — CLI entry
    _configure_cli_logging()
    run_forever()


if __name__ == "__main__":  # pragma: no cover
    main()
