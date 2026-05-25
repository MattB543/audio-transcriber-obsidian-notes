"""Tests for publisher/{publish,watcher}.py with a tmp git repo.

NO LLM is involved in publishing now — the published note is a deterministic
transformation of the source Obsidian transcript file. We set up a bare remote
+ a local clone per test via subprocess `git init`. No real network. No real
personal_site push.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

# Ensure repo root on sys.path.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import config  # noqa: E402
from publisher import publish as publish_mod  # noqa: E402
from publisher import watcher as watcher_mod  # noqa: E402
from publisher.publish import publish_note  # noqa: E402
from publisher.watcher import (  # noqa: E402
    BlockedLedger,
    PublishedLedger,
    clear_blocked,
    run_once,
    scan_for_publishable,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _git(args: list[str], cwd: Path, *, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=check,
    )


def _init_site_repo(tmp_path: Path) -> tuple[Path, Path]:
    """Create a bare `origin` repo and a site clone wired to it. Returns (site, bare_origin)."""
    bare = tmp_path / "origin.git"
    bare.mkdir()
    _git(["init", "--bare", "-b", "main"], cwd=bare)

    site = tmp_path / "site"
    site.mkdir()
    _git(["init", "-b", "main"], cwd=site)
    _git(["config", "user.email", "test@example.com"], cwd=site)
    _git(["config", "user.name", "Test"], cwd=site)
    _git(["config", "commit.gpgsign", "false"], cwd=site)

    # Seed initial commit so we can pull --ff-only.
    notes_dir = site / "src" / "pages" / "notes"
    notes_dir.mkdir(parents=True)
    (notes_dir / "how-this-website-works.md").write_text(
        "---\ntitle: How This Website Works\nlayout: ../../layouts/Layout.astro\n"
        "created: 2026-03-30\ndescription: seed\n---\n\nSeed body.\n",
        encoding="utf-8",
    )
    _git(["add", "."], cwd=site)
    _git(["commit", "-m", "seed"], cwd=site)
    _git(["remote", "add", "origin", str(bare)], cwd=site)
    _git(["push", "-u", "origin", "main"], cwd=site)

    return site, bare


OBS_TRANSCRIPT_TEMPLATE = """\
---
title: "{title}"
date: 2026-04-24
time: "14:32:08"
duration: "3:07"
audio: "[[\U0001F399 Audio/2026-04-24_143208.flac]]"
raw_transcript: "[[\U0001F399 Audio/transcriptions/2026-04-24_143208.raw]]"
source: voice-memo
tags: [voice-memo{extra_tags}]
summary: "Notes on keeping the site small."
status: captured
model: gemini-3-flash-preview
---
![[\U0001F399 Audio/2026-04-24_143208.flac]]

## Summary
Notes on keeping the site small.

## Transcript
{transcript_body}{body_extra}
"""


_DEFAULT_TRANSCRIPT_BODY = (
    "This is a cleaned transcript about why I like static sites.\n"
    "I was talking through why I picked Astro for the site. It's simple, "
    "it just builds markdown to HTML, and there's no server to keep alive."
)


def _make_transcript(
    dir_: Path,
    name: str,
    *,
    frontmatter_publish: bool = False,
    body_publish: bool = False,
    raw: bool = False,
    title: str = "Thinking About Static Site Builds",
    transcript_body: str | None = None,
) -> Path:
    extra_tags = ", publish" if frontmatter_publish else ""
    body_extra = "\n\n#publish" if body_publish else ""
    md = OBS_TRANSCRIPT_TEMPLATE.format(
        title=title,
        extra_tags=extra_tags,
        body_extra=body_extra,
        transcript_body=transcript_body if transcript_body is not None else _DEFAULT_TRANSCRIPT_BODY,
    )
    suffix = ".raw.md" if raw else ".md"
    p = dir_ / f"{name}{suffix}"
    p.write_text(md, encoding="utf-8")
    return p


def _make_transcript_no_title(
    dir_: Path,
    name: str,
    *,
    frontmatter_publish: bool = False,
    transcript_body: str | None = None,
) -> Path:
    """Source `.md` with no `title:` field in frontmatter."""
    extra_tags = ", publish" if frontmatter_publish else ""
    body = transcript_body if transcript_body is not None else _DEFAULT_TRANSCRIPT_BODY
    md = (
        "---\n"
        "date: 2026-04-24\n"
        f"tags: [voice-memo{extra_tags}]\n"
        "source: voice-memo\n"
        "---\n"
        "\n"
        "## Transcript\n"
        f"{body}\n"
    )
    p = dir_ / f"{name}.md"
    p.write_text(md, encoding="utf-8")
    return p


def _make_transcript_empty_section(
    dir_: Path, name: str, *, frontmatter_publish: bool = False
) -> Path:
    """Source `.md` with a `## Transcript` heading but no body under it."""
    extra_tags = ", publish" if frontmatter_publish else ""
    md = (
        "---\n"
        'title: "Empty Memo"\n'
        f"tags: [voice-memo{extra_tags}]\n"
        "source: voice-memo\n"
        "---\n"
        "\n"
        "## Transcript\n"
        "\n"
    )
    p = dir_ / f"{name}.md"
    p.write_text(md, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# auto-fixtures: monkeypatch config paths to tmp
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Redirect all config paths + set up a fake site repo."""
    site, bare = _init_site_repo(tmp_path)

    transcript_dir = tmp_path / "transcripts"
    transcript_dir.mkdir()
    drafts_dir = tmp_path / "drafts"
    drafts_dir.mkdir()
    ledger_path = tmp_path / ".published.json"

    notes_dir = site / "src" / "pages" / "notes"
    ref_note = notes_dir / "how-this-website-works.md"

    # Patch config module attrs.
    monkeypatch.setattr(config, "SITE_ROOT", site)
    monkeypatch.setattr(config, "SITE_NOTES_DIR", notes_dir)
    monkeypatch.setattr(config, "SITE_REFERENCE_NOTE", ref_note)
    monkeypatch.setattr(config, "TRANSCRIPT_DIR", transcript_dir)
    monkeypatch.setattr(config, "DRAFTS_DIR", drafts_dir)
    monkeypatch.setattr(config, "PUBLISHED_LEDGER", ledger_path)

    yield types_ns(
        tmp_path=tmp_path,
        site=site,
        bare=bare,
        transcript_dir=transcript_dir,
        drafts_dir=drafts_dir,
        ledger_path=ledger_path,
        notes_dir=notes_dir,
        ref_note=ref_note,
    )


class types_ns:  # noqa: N801
    """Tiny attribute-bag so tests can do `env.site`, `env.transcript_dir`, etc."""

    def __init__(self, **kw: Any) -> None:
        self.__dict__.update(kw)


# ---------------------------------------------------------------------------
# scan_for_publishable
# ---------------------------------------------------------------------------


class TestScan:
    def test_finds_frontmatter_tag(self, tmp_env):
        _make_transcript(tmp_env.transcript_dir, "2026-04-24_143208_a", frontmatter_publish=True)
        _make_transcript(tmp_env.transcript_dir, "2026-04-24_143208_b")  # no tag

        results = scan_for_publishable(
            transcript_dir=tmp_env.transcript_dir,
            ledger=PublishedLedger(path=tmp_env.ledger_path),
        )
        names = sorted(p.name for p in results)
        assert names == ["2026-04-24_143208_a.md"]

    def test_finds_body_tag(self, tmp_env):
        _make_transcript(tmp_env.transcript_dir, "memo1", body_publish=True)
        results = scan_for_publishable(
            transcript_dir=tmp_env.transcript_dir,
            ledger=PublishedLedger(path=tmp_env.ledger_path),
        )
        assert len(results) == 1
        assert results[0].name == "memo1.md"

    def test_skips_raw_files(self, tmp_env):
        _make_transcript(tmp_env.transcript_dir, "memo1", frontmatter_publish=True, raw=True)
        results = scan_for_publishable(
            transcript_dir=tmp_env.transcript_dir,
            ledger=PublishedLedger(path=tmp_env.ledger_path),
        )
        assert results == []

    def test_skips_when_no_trigger(self, tmp_env):
        _make_transcript(tmp_env.transcript_dir, "memo1")
        results = scan_for_publishable(
            transcript_dir=tmp_env.transcript_dir,
            ledger=PublishedLedger(path=tmp_env.ledger_path),
        )
        assert results == []

    def test_ignores_word_containing_publish(self, tmp_env):
        """Make sure `#publishing` doesn't trip the body regex."""
        txt = OBS_TRANSCRIPT_TEMPLATE.format(
            title="Thinking About Static Site Builds",
            extra_tags="",
            body_extra="\n\n#publishing",
            transcript_body=_DEFAULT_TRANSCRIPT_BODY,
        )
        (tmp_env.transcript_dir / "memo1.md").write_text(txt, encoding="utf-8")
        results = scan_for_publishable(
            transcript_dir=tmp_env.transcript_dir,
            ledger=PublishedLedger(path=tmp_env.ledger_path),
        )
        assert results == []

    def test_ledger_prevents_republish(self, tmp_env):
        f = _make_transcript(tmp_env.transcript_dir, "memo1", frontmatter_publish=True)
        ledger = PublishedLedger(path=tmp_env.ledger_path)
        # Compute the source's hash so the ledger entry matches the file.
        current_hash = watcher_mod._hash_for_source(f)
        ledger.mark_published(
            "thinking-about-static-site-builds",
            site_path=tmp_env.notes_dir / "thinking-about-static-site-builds.md",
            commit_sha="deadbeef",
            source=f,
            content_hash=current_hash,
        )

        # New ledger instance reads same file.
        results = scan_for_publishable(
            transcript_dir=tmp_env.transcript_dir,
            ledger=PublishedLedger(path=tmp_env.ledger_path),
        )
        assert results == []


# ---------------------------------------------------------------------------
# scan_for_publishable: hash-aware republish behavior
# ---------------------------------------------------------------------------


class TestScanHashAware:
    def test_scan_includes_first_time_source(self, tmp_env):
        """A source with #publish that's never been published is included."""
        src = _make_transcript(
            tmp_env.transcript_dir, "fresh_memo", frontmatter_publish=True
        )
        results = scan_for_publishable(
            transcript_dir=tmp_env.transcript_dir,
            ledger=PublishedLedger(path=tmp_env.ledger_path),
        )
        assert src in results

    def test_scan_excludes_unchanged_source(self, tmp_env):
        """Publish source A; without modification, scan must NOT include it again."""
        src = _make_transcript(
            tmp_env.transcript_dir, "stable_memo", frontmatter_publish=True
        )
        result = publish_note(src)
        assert result["status"] == "published", result

        # Mark in the ledger as the watcher would.
        ledger = PublishedLedger(path=tmp_env.ledger_path)
        ledger.mark_published(
            result["slug"],
            site_path=result["site_path"],
            commit_sha=result["commit_sha"],
            source=src,
            content_hash=result["content_hash"],
        )

        results = scan_for_publishable(
            transcript_dir=tmp_env.transcript_dir,
            ledger=PublishedLedger(path=tmp_env.ledger_path),
        )
        assert src not in results, results

    def test_scan_includes_changed_source_for_republish(self, tmp_env):
        """Publish source A; modify its transcript; scan must include it again."""
        src = _make_transcript(
            tmp_env.transcript_dir, "edited_memo", frontmatter_publish=True
        )
        result = publish_note(src)
        assert result["status"] == "published", result

        ledger = PublishedLedger(path=tmp_env.ledger_path)
        ledger.mark_published(
            result["slug"],
            site_path=result["site_path"],
            commit_sha=result["commit_sha"],
            source=src,
            content_hash=result["content_hash"],
        )

        # Now edit the transcript text. The hash includes the title + transcript,
        # so this MUST change the hash. Note: publish_note also wrote
        # `published_at` into the source — re-rewrite the file fresh to keep the
        # transcript-body change clean.
        new_body = "Totally rewritten body — the user changed their mind."
        # Re-make the file with the same title but a new transcript body.
        # _make_transcript overwrites the file, including stripping `published_at`.
        _make_transcript(
            tmp_env.transcript_dir,
            "edited_memo",
            frontmatter_publish=True,
            transcript_body=new_body,
        )

        results = scan_for_publishable(
            transcript_dir=tmp_env.transcript_dir,
            ledger=PublishedLedger(path=tmp_env.ledger_path),
        )
        assert src in results, results

    def test_published_at_writeback_doesnt_trigger_republish(self, tmp_env):
        """Publish source A (which writes published_at back into the source's
        frontmatter); scan immediately after must NOT include it. We hash
        title + transcript only — frontmatter `published_at` is excluded.
        """
        src = _make_transcript(
            tmp_env.transcript_dir, "writeback_memo", frontmatter_publish=True
        )
        result = publish_note(src)
        assert result["status"] == "published", result

        # Sanity: the source now has a `published_at:` line.
        post = src.read_text(encoding="utf-8")
        assert "published_at:" in post, post

        # Mark in the ledger as the watcher would.
        ledger = PublishedLedger(path=tmp_env.ledger_path)
        ledger.mark_published(
            result["slug"],
            site_path=result["site_path"],
            commit_sha=result["commit_sha"],
            source=src,
            content_hash=result["content_hash"],
        )

        # The published_at writeback must NOT cause a re-publish.
        results = scan_for_publishable(
            transcript_dir=tmp_env.transcript_dir,
            ledger=PublishedLedger(path=tmp_env.ledger_path),
        )
        assert src not in results, (
            f"published_at writeback must not trigger republish; got {results}"
        )

    def test_old_ledger_without_hash_triggers_republish(self, tmp_env):
        """Manually create a ledger entry without `content_hash` (pre-v2);
        scan must include the source so we re-publish and populate the hash.
        """
        src = _make_transcript(
            tmp_env.transcript_dir, "legacy_memo", frontmatter_publish=True
        )
        ledger = PublishedLedger(path=tmp_env.ledger_path)
        # No content_hash kwarg (simulates old ledger).
        ledger.mark_published(
            "thinking-about-static-site-builds",
            site_path=tmp_env.notes_dir / "thinking-about-static-site-builds.md",
            commit_sha="abc123",
            source=src,
        )

        # Sanity-check: the on-disk entry has no `content_hash`.
        data = json.loads(tmp_env.ledger_path.read_text(encoding="utf-8"))
        assert "content_hash" not in data["thinking-about-static-site-builds"], data

        results = scan_for_publishable(
            transcript_dir=tmp_env.transcript_dir,
            ledger=PublishedLedger(path=tmp_env.ledger_path),
        )
        assert src in results, (
            f"old ledger entry without content_hash must trigger republish; got {results}"
        )


# ---------------------------------------------------------------------------
# PublishedLedger
# ---------------------------------------------------------------------------


class TestLedger:
    def test_roundtrip(self, tmp_path: Path):
        p = tmp_path / ".published.json"
        ledger = PublishedLedger(path=p)
        src = tmp_path / "t.md"
        src.write_text("x")
        ledger.mark_published(
            "foo", site_path=tmp_path / "foo.md", commit_sha="abc123", source=src
        )

        reloaded = PublishedLedger(path=p)
        assert reloaded.contains_slug("foo")
        assert reloaded.contains_source(src)

        data = json.loads(p.read_text(encoding="utf-8"))
        assert "foo" in data
        assert data["foo"]["commit_sha"] == "abc123"

    def test_bad_json_starts_empty(self, tmp_path: Path):
        p = tmp_path / ".published.json"
        p.write_text("{not json", encoding="utf-8")
        ledger = PublishedLedger(path=p)
        assert ledger.as_dict() == {}

    def test_non_dict_starts_empty(self, tmp_path: Path):
        p = tmp_path / ".published.json"
        p.write_text("[1,2,3]", encoding="utf-8")
        ledger = PublishedLedger(path=p)
        assert ledger.as_dict() == {}

    def test_mark_published_with_content_hash(self, tmp_path: Path):
        p = tmp_path / ".published.json"
        ledger = PublishedLedger(path=p)
        src = tmp_path / "t.md"
        src.write_text("x")
        ledger.mark_published(
            "foo",
            site_path=tmp_path / "foo.md",
            commit_sha="abc123",
            source=src,
            content_hash="cafebabe" * 8,
        )
        assert ledger.hash_for_source(src) == "cafebabe" * 8

    def test_hash_for_source_returns_none_when_missing(self, tmp_path: Path):
        p = tmp_path / ".published.json"
        ledger = PublishedLedger(path=p)
        src = tmp_path / "missing.md"
        src.write_text("x")
        assert ledger.hash_for_source(src) is None


# ---------------------------------------------------------------------------
# publish_note: happy path
# ---------------------------------------------------------------------------


class TestPublishNote:
    def test_happy_path(self, tmp_env):
        """Publish a normal source file end-to-end (commit + push to bare remote)."""
        src = _make_transcript(
            tmp_env.transcript_dir, "2026-04-24_143208_static", frontmatter_publish=True
        )

        result = publish_note(src)

        assert result["status"] == "published", result
        assert result["slug"] == "thinking-about-static-site-builds"
        assert result["commit_sha"]
        assert result["site_path"] is not None
        assert result["site_path"].exists()

        # A commit landed on main.
        log = _git(["log", "--oneline"], cwd=tmp_env.site).stdout
        assert "note: Thinking About Static Site Builds" in log

        # The bare remote got the push.
        remote_log = _git(["log", "--oneline"], cwd=tmp_env.bare).stdout
        assert "note: Thinking About Static Site Builds" in remote_log

        # The draft was staged.
        draft = tmp_env.drafts_dir / "thinking-about-static-site-builds.md"
        assert draft.exists()

    def test_publish_uses_source_title_as_title(self, tmp_env):
        """The published markdown's `title:` must come from the source frontmatter."""
        src = _make_transcript(
            tmp_env.transcript_dir,
            "title_memo",
            frontmatter_publish=True,
            title="My Voice Memo Title",
        )

        result = publish_note(src)
        assert result["status"] == "published", result
        assert result["site_path"] is not None

        published = result["site_path"].read_text(encoding="utf-8")
        # YAML scalar is double-quoted by _yaml_dump_str.
        assert 'title: "My Voice Memo Title"' in published, published

    def test_publish_description_is_first_200_chars(self, tmp_env):
        """`description` must be ~200 chars of cleaned transcript + '...'."""
        long_body = (
            "This is a deliberately very long transcript that exceeds two "
            "hundred characters so we can verify the description gets "
            "truncated. We keep going so we are well past two hundred chars "
            "in this string. More words follow here to push past the limit "
            "and well beyond it so we are confidently above three hundred "
            "characters in total length for this assertion to mean anything."
        )
        assert len(long_body) > 300, len(long_body)

        src = _make_transcript(
            tmp_env.transcript_dir,
            "long_memo",
            frontmatter_publish=True,
            transcript_body=long_body,
        )

        result = publish_note(src)
        assert result["status"] == "published", result
        published = result["site_path"].read_text(encoding="utf-8")

        # Pull the description value out of the YAML frontmatter.
        m = re.search(r'^description:\s*"(?P<d>.*)"\s*$', published, flags=re.MULTILINE)
        assert m, f"could not find description in:\n{published}"
        description = m.group("d")

        # Cap is 200 chars + "..." (3) = 203 max; word-boundary backup makes it
        # somewhat shorter. Allow a small slop on either side.
        assert len(description) <= 210, (
            f"description too long ({len(description)} chars): {description!r}"
        )
        assert description.endswith("..."), description

    def test_publish_body_is_cleaned_transcript_verbatim(self, tmp_env):
        """The body of the published file must be the cleaned transcript text."""
        custom_body = (
            "This is a very specific marker phrase that we expect to appear "
            "in the published body verbatim. Banana grommet flange 42."
        )
        src = _make_transcript(
            tmp_env.transcript_dir,
            "verbatim_memo",
            frontmatter_publish=True,
            transcript_body=custom_body,
        )

        result = publish_note(src)
        assert result["status"] == "published", result
        published = result["site_path"].read_text(encoding="utf-8")

        # Strip frontmatter and verify the body contains the exact marker.
        body_after_fm = re.split(r"^---\s*$", published, maxsplit=2, flags=re.MULTILINE)[-1]
        assert custom_body in body_after_fm, (
            f"expected custom body in published file body; got:\n{body_after_fm}"
        )

    def test_publish_writes_published_at_to_source_frontmatter(self, tmp_env):
        """After successful publish, the source `.md` must have `published_at:` set."""
        src = _make_transcript(
            tmp_env.transcript_dir, "writeback_memo", frontmatter_publish=True
        )
        # Sanity: not present before.
        assert "published_at:" not in src.read_text(encoding="utf-8")

        result = publish_note(src)
        assert result["status"] == "published", result

        post = src.read_text(encoding="utf-8")
        m = re.search(r'^published_at:\s*"(?P<ts>[^"]+)"\s*$', post, flags=re.MULTILINE)
        assert m, f"published_at not found in source after publish:\n{post}"
        # Roughly ISO-8601-shaped.
        assert re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", m.group("ts")), m.group("ts")

    def test_publish_returns_content_hash(self, tmp_env):
        """`result['content_hash']` must be a 64-char hex string and stable."""
        src = _make_transcript(
            tmp_env.transcript_dir, "hash_memo", frontmatter_publish=True
        )

        result = publish_note(src)
        assert result["status"] == "published", result
        h = result["content_hash"]
        assert isinstance(h, str)
        assert len(h) == 64, h
        assert re.fullmatch(r"[0-9a-f]{64}", h), h

        # Stability: hashing the same title + transcript again yields the same
        # value. We use the watcher's helper which mirrors what publish_note
        # hashed.
        recomputed = watcher_mod._hash_for_source(src)
        assert recomputed == h, (recomputed, h)

    def test_publish_returns_published_at(self, tmp_env):
        """`result['published_at']` must be a non-empty ISO timestamp."""
        src = _make_transcript(
            tmp_env.transcript_dir, "ts_memo", frontmatter_publish=True
        )

        result = publish_note(src)
        assert result["status"] == "published", result
        pa = result["published_at"]
        assert isinstance(pa, str) and pa, pa
        assert re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", pa), pa

    def test_publish_no_title_in_source_returns_error(self, tmp_env):
        """Source frontmatter without `title:` -> error mentioning title."""
        src = _make_transcript_no_title(
            tmp_env.transcript_dir, "untitled", frontmatter_publish=True
        )
        result = publish_note(src)
        assert result["status"] == "error", result
        assert result["error"] and "title" in result["error"].lower(), result["error"]

        # No commit landed.
        log = _git(["log", "--oneline"], cwd=tmp_env.site).stdout
        assert "note:" not in log

    def test_publish_blank_title_returns_error_not_literal_none(self, tmp_env):
        """Codex P2 regression: a YAML `title:` with no value parses to
        Python None. We must treat that as 'missing title' and error -- the
        old `str(fm.get('title', '')).strip()` formulation produced the
        literal string 'None' and would have happily published it."""
        src = tmp_env.transcript_dir / "blank_title.md"
        src.write_text(
            "---\n"
            "title:\n"          # YAML null
            "tags: [voice-memo, publish]\n"
            "---\n"
            "## Transcript\n"
            "Some body content here.\n",
            encoding="utf-8",
        )
        result = publish_note(src)
        assert result["status"] == "error", result
        assert result["error"] and "title" in result["error"].lower(), result["error"]

        # And ensure 'None' didn't sneak through as a published title.
        log = _git(["log", "--oneline"], cwd=tmp_env.site).stdout
        assert "None" not in log
        assert "note:" not in log

    def test_empty_transcript_section_skips(self, tmp_env):
        """Source with `## Transcript` but no body under it -> skipped."""
        src = _make_transcript_empty_section(
            tmp_env.transcript_dir, "empty", frontmatter_publish=True
        )

        head_before = _git(["rev-parse", "HEAD"], cwd=tmp_env.site).stdout.strip()
        remote_before = _git(["rev-parse", "HEAD"], cwd=tmp_env.bare).stdout.strip()

        result = publish_note(src)

        assert result["status"] == "skipped", result
        assert result["reason"] and "transcript" in result["reason"].lower()
        assert result["commit_sha"] is None

        assert _git(["rev-parse", "HEAD"], cwd=tmp_env.site).stdout.strip() == head_before
        assert _git(["rev-parse", "HEAD"], cwd=tmp_env.bare).stdout.strip() == remote_before

    def test_dirty_working_tree_aborts(self, tmp_env):
        src = _make_transcript(tmp_env.transcript_dir, "dirty", frontmatter_publish=True)
        # Make tree dirty.
        (tmp_env.site / "random.txt").write_text("dirty", encoding="utf-8")

        result = publish_note(src)

        assert result["status"] == "error"
        assert result["error"] and "dirty" in result["error"].lower()

        # No commit made, no push.
        log = _git(["log", "--oneline"], cwd=tmp_env.site).stdout
        assert "note:" not in log

    def test_slug_collision(self, tmp_env):
        """If a note with the same slug already exists, append -2."""
        # Put a colliding file in the site notes dir + commit it so the tree is clean.
        existing = tmp_env.notes_dir / "thinking-about-static-site-builds.md"
        existing.write_text(
            "---\ntitle: Pre-existing\nlayout: ../../layouts/Layout.astro\n"
            "created: 2026-04-01\ndescription: pre\n---\n\nPre-existing body.\n",
            encoding="utf-8",
        )
        _git(["add", "."], cwd=tmp_env.site)
        _git(["commit", "-m", "pre-existing"], cwd=tmp_env.site)
        _git(["push", "origin", "main"], cwd=tmp_env.site)

        src = _make_transcript(tmp_env.transcript_dir, "2026-04-24_memo", frontmatter_publish=True)

        result = publish_note(src)

        assert result["status"] == "published", result
        assert result["slug"] == "thinking-about-static-site-builds-2"
        assert result["site_path"] is not None
        assert result["site_path"].name == "thinking-about-static-site-builds-2.md"
        assert result["site_path"].exists()
        # Pre-existing file untouched.
        assert "Pre-existing" in existing.read_text(encoding="utf-8")

    def test_pull_runs_before_slug_resolution(self, tmp_env, tmp_path: Path):
        """Bug 1 regression: pull must happen BEFORE slug collision check.

        Set up a second clone that pushes a colliding `<slug>.md` to the bare
        remote. The local site clone has NOT yet pulled, so the file is not
        present locally when publish_note starts. The fix requires that pull
        runs FIRST, sees the colliding file from the remote, and the slug
        resolver then picks `<slug>-2.md` instead of clobbering it.
        """
        # --- Set up a second clone of the bare repo (simulates "other machine"). ---
        other = tmp_path / "other_site"
        other.mkdir()
        _git(["clone", str(tmp_env.bare), str(other)], cwd=tmp_path)
        _git(["config", "user.email", "other@example.com"], cwd=other)
        _git(["config", "user.name", "Other"], cwd=other)
        _git(["config", "commit.gpgsign", "false"], cwd=other)

        # The "other machine" publishes the same slug to bare.
        other_notes = other / "src" / "pages" / "notes"
        colliding = other_notes / "thinking-about-static-site-builds.md"
        colliding.write_text(
            "---\ntitle: Thinking About Static Site Builds\n"
            "layout: ../../layouts/Layout.astro\n"
            "created: 2026-04-23\ndescription: from other machine\n---\n\n"
            "Body from the other machine.\n",
            encoding="utf-8",
        )
        _git(["add", "."], cwd=other)
        _git(["commit", "-m", "note: from other machine"], cwd=other)
        _git(["push", "origin", "main"], cwd=other)

        # CRITICAL precondition: the local site clone does NOT yet have the file.
        # If publish_note resolved slug collision BEFORE pulling, it would not
        # see the file and would pick `thinking-about-static-site-builds.md`,
        # then pull, then clobber the freshly-pulled file with copy2.
        local_target = tmp_env.notes_dir / "thinking-about-static-site-builds.md"
        assert not local_target.exists(), (
            "precondition: file must not exist locally before publish_note runs"
        )

        src = _make_transcript(
            tmp_env.transcript_dir,
            "2026-04-24_static",
            frontmatter_publish=True,
        )

        result = publish_note(src)

        assert result["status"] == "published", result
        # The fix: slug must be `-2` because pull brought the file in BEFORE
        # collision resolution.
        assert result["slug"] == "thinking-about-static-site-builds-2", result
        assert result["site_path"] is not None
        assert result["site_path"].name == "thinking-about-static-site-builds-2.md"
        assert result["site_path"].exists()

        # The file from the "other machine" was NOT clobbered.
        assert local_target.exists()
        assert "Body from the other machine." in local_target.read_text(encoding="utf-8")

    def test_commit_failure_cleans_working_tree(self, tmp_env):
        """Bug 2A regression: commit-failure path must unlink the site file.

        If commit fails and we leave the file behind, the next watcher tick
        sees a dirty working tree and aborts forever.
        """
        src = _make_transcript(
            tmp_env.transcript_dir, "fail_commit", frontmatter_publish=True
        )

        # Wrap _git so that the `commit -m ...` invocation fails, but every
        # other git operation runs normally.
        original_git = publish_mod._git

        def fake_git(args, cwd):
            if args and args[0] == "commit":
                raise subprocess.CalledProcessError(
                    returncode=1,
                    cmd=["git", *args],
                    output="",
                    stderr="simulated commit failure",
                )
            return original_git(args, cwd)

        with patch.object(publish_mod, "_git", side_effect=fake_git):
            result = publish_note(src)

        assert result["status"] == "error"
        assert result["error"] and "commit" in result["error"].lower()

        # The fix: site_path must be unlinked so the working tree is clean
        # for the next watcher tick.
        assert result["site_path"] is not None
        assert not result["site_path"].exists(), (
            "commit-failure path must unlink site_path; left-behind file "
            "would block all future publishes"
        )

        # The working tree must be clean (no untracked file, no staged file).
        status = _git(["status", "--porcelain"], cwd=tmp_env.site).stdout
        assert status.strip() == "", (
            f"expected clean working tree after commit failure, got: {status!r}"
        )

        # No commit landed on main.
        log = _git(["log", "--oneline"], cwd=tmp_env.site).stdout
        assert "note:" not in log

    def test_push_failure_rolls_back_commit(self, tmp_env):
        """Bug 1 regression: push-failure path must roll back the local commit.

        If we leave a committed-but-unpushed change in HEAD, the next watcher
        tick sees a clean working tree, slug-collision resolution picks
        `<slug>-2.md` for the retry, and a later successful push publishes
        BOTH commits as duplicate notes.
        """
        src = _make_transcript(
            tmp_env.transcript_dir, "push_fail", frontmatter_publish=True
        )

        # Snapshot HEAD before publish so we can assert we rolled back to it.
        head_before = _git(["rev-parse", "HEAD"], cwd=tmp_env.site).stdout.strip()
        remote_before = _git(["rev-parse", "HEAD"], cwd=tmp_env.bare).stdout.strip()

        # Wrap _git_ok so that `push` fails but the rollback `reset --hard`
        # runs for real. (_git handles add + commit — we leave those alone.)
        original_git_ok = publish_mod._git_ok

        def fake_git_ok(args, cwd):
            if args and args[0] == "push":
                return subprocess.CompletedProcess(
                    args=["git", *args],
                    returncode=1,
                    stdout="",
                    stderr="simulated push failure",
                )
            return original_git_ok(args, cwd)

        with patch.object(publish_mod, "_git_ok", side_effect=fake_git_ok):
            result = publish_note(src)

        assert result["status"] == "error"
        assert result["error"] and "push" in result["error"].lower()

        # The fix: local commit rolled back so HEAD is exactly what it was
        # before publish_note ran.
        head_after = _git(["rev-parse", "HEAD"], cwd=tmp_env.site).stdout.strip()
        assert head_after == head_before, (
            f"expected HEAD rolled back to {head_before}, got {head_after}"
        )

        # The site file must not exist: reverting head_before_commit removes it.
        assert result["site_path"] is not None
        assert not result["site_path"].exists(), (
            "push-failure path must remove site_path via reset --hard"
        )

        # Working tree must be clean for the next watcher tick.
        assert publish_mod._working_tree_clean(tmp_env.site), (
            "expected clean working tree after push-failure rollback"
        )
        status = _git(["status", "--porcelain"], cwd=tmp_env.site).stdout
        assert status.strip() == "", (
            f"expected clean working tree, got: {status!r}"
        )

        # git log shows no new commit beyond the seed.
        log = _git(["log", "--oneline"], cwd=tmp_env.site).stdout
        assert "note:" not in log

        # Nothing landed on the bare remote.
        remote_after = _git(["rev-parse", "HEAD"], cwd=tmp_env.bare).stdout.strip()
        assert remote_after == remote_before

    def test_push_failure_preserves_unrelated_dirty_files(self, tmp_env):
        """P1 regression: push-failure rollback must NOT touch unrelated files.

        Between the initial `_working_tree_clean` check and the push attempt
        the user may have edited other files in `personal_site/`. A naive
        `reset --hard` would blow those away. The fix uses a soft-reset and
        a status-check to ensure only our file is cleaned up. If anything
        unrelated is dirty, we bail with a manual-recovery error and leave
        the commit + file alone.
        """
        src = _make_transcript(
            tmp_env.transcript_dir, "push_fail_unrelated", frontmatter_publish=True
        )

        unrelated_path = tmp_env.notes_dir / "some-other-note.md"
        unrelated_body = "unsaved edits that MUST NOT be deleted\n"

        # Snapshot HEAD before publish.
        head_before = _git(["rev-parse", "HEAD"], cwd=tmp_env.site).stdout.strip()
        remote_before = _git(["rev-parse", "HEAD"], cwd=tmp_env.bare).stdout.strip()

        original_git_ok = publish_mod._git_ok
        injected = {"done": False}

        def fake_git_ok(args, cwd):
            # Inject an unrelated dirty file AFTER the commit (so the initial
            # _working_tree_clean check passes) but before the push runs.
            # The first _git_ok call after commit is the push (_git handles
            # add+commit). Inject the unrelated file right before returning
            # the fake push-failure.
            if args and args[0] == "push" and not injected["done"]:
                unrelated_path.write_text(unrelated_body, encoding="utf-8")
                injected["done"] = True
                return subprocess.CompletedProcess(
                    args=["git", *args],
                    returncode=1,
                    stdout="",
                    stderr="simulated push failure",
                )
            return original_git_ok(args, cwd)

        with patch.object(publish_mod, "_git_ok", side_effect=fake_git_ok):
            result = publish_note(src)

        # The error should mention "unrelated" because the unrelated file
        # appears in `git status --porcelain` after the soft-reset. Status
        # was changed from "error" to "manual_recovery" (P2 Bug 1 fix) so
        # the watcher does not retry — leaving the local repo in this state
        # for the user to resolve.
        assert result["status"] == "manual_recovery", result
        assert result["error"] is not None
        err = result["error"].lower()
        assert "unrelated" in err, result["error"]
        assert "manual recovery" in err, result["error"]

        # CRITICAL: the unrelated file must NOT have been touched.
        assert unrelated_path.exists(), (
            "unrelated file was deleted by rollback - this is the P1 bug"
        )
        assert unrelated_path.read_text(encoding="utf-8") == unrelated_body, (
            "unrelated file contents were modified by rollback"
        )

        # Nothing was pushed to the bare remote.
        remote_after = _git(["rev-parse", "HEAD"], cwd=tmp_env.bare).stdout.strip()
        assert remote_after == remote_before

        # We deliberately do NOT assert HEAD was rolled back: the safe
        # behavior when unrelated changes are present is to leave things
        # alone and tell the user to recover manually. The HEAD-after check:
        # we left our commit in place (soft-reset undoes it, but we return
        # early before cleanup; actually we soft-reset first, so HEAD is now
        # at head_before - that's fine either way: the important invariant
        # is the unrelated file survived).
        head_after = _git(["rev-parse", "HEAD"], cwd=tmp_env.site).stdout.strip()
        # After the soft-reset step, HEAD is back at head_before.
        assert head_after == head_before, (
            f"HEAD after rollback should equal {head_before}, got {head_after}"
        )

    def test_push_failure_with_extra_commit_aborts_with_manual_recovery_error(
        self, tmp_env,
    ):
        """P1 regression: if a commit lands on top of ours before push fails,
        we must NOT soft-reset (that would un-commit BOTH our commit and the
        user's). Instead we bail with a manual-recovery error.
        """
        src = _make_transcript(
            tmp_env.transcript_dir, "push_fail_extra", frontmatter_publish=True
        )

        original_git_ok = publish_mod._git_ok
        injected = {"done": False}

        def fake_git_ok(args, cwd):
            if args and args[0] == "push" and not injected["done"]:
                # Simulate a racing commit landing on top of ours right
                # before the push attempts. This makes HEAD~1 != our
                # head_before_commit, so the rollback must abort.
                racing = tmp_env.site / "racing.txt"
                racing.write_text("racing commit", encoding="utf-8")
                _git(["add", "."], cwd=tmp_env.site)
                _git(["commit", "-m", "racing commit"], cwd=tmp_env.site)
                injected["done"] = True
                return subprocess.CompletedProcess(
                    args=["git", *args],
                    returncode=1,
                    stdout="",
                    stderr="simulated push failure",
                )
            return original_git_ok(args, cwd)

        with patch.object(publish_mod, "_git_ok", side_effect=fake_git_ok):
            result = publish_note(src)

        # Status was changed from "error" to "manual_recovery" (P2 Bug 1 fix)
        # so the watcher does not retry the source — the local repo has an
        # un-pushed commit that requires user resolution.
        assert result["status"] == "manual_recovery", result
        assert result["error"] is not None
        err = result["error"].lower()
        assert "head has moved" in err, result["error"]
        assert "manual recovery" in err, result["error"]
        # Source transcript path should be present so the user can find it.
        assert str(src) in result["error"], result["error"]

        # Our commit must still exist (we refused to roll it back).
        log = _git(["log", "--oneline"], cwd=tmp_env.site).stdout
        assert "note: Thinking About Static Site Builds" in log, (
            "our commit must not be reset when the rollback aborts to "
            "manual-recovery"
        )
        # The racing commit must also still exist.
        assert "racing commit" in log

    def test_git_add_failure_cleans_working_tree(self, tmp_env):
        """Bug 2 regression: git-add-failure path must unlink the copied file.

        If `git add` fails after `shutil.copy2` copied the draft into the
        site, the file sits in the working tree as untracked and
        `_working_tree_clean` will fail forever on the next tick.
        """
        src = _make_transcript(
            tmp_env.transcript_dir, "add_fail", frontmatter_publish=True
        )

        # Wrap _git so the `add` invocation fails; everything else is real.
        original_git = publish_mod._git

        def fake_git(args, cwd):
            if args and args[0] == "add":
                raise subprocess.CalledProcessError(
                    returncode=1,
                    cmd=["git", *args],
                    output="",
                    stderr="simulated add failure",
                )
            return original_git(args, cwd)

        with patch.object(publish_mod, "_git", side_effect=fake_git):
            result = publish_note(src)

        assert result["status"] == "error"
        assert result["error"] and "add" in result["error"].lower()

        # The fix: the copied site file must be unlinked.
        assert result["site_path"] is not None
        assert not result["site_path"].exists(), (
            "git-add-failure path must unlink site_path; left-behind file "
            "would block all future publishes"
        )

        # Working tree must be clean (no untracked leftovers).
        status = _git(["status", "--porcelain"], cwd=tmp_env.site).stdout
        assert status.strip() == "", (
            f"expected clean working tree after add failure, got: {status!r}"
        )

        # No commit landed.
        log = _git(["log", "--oneline"], cwd=tmp_env.site).stdout
        assert "note:" not in log

    def test_head_moved_unexpectedly_returns_actionable_error(self, tmp_env):
        """Bug 2B regression: HEAD-moved error must be actionable.

        If something raced us between commit and the parent-sanity-check, we
        cannot auto-recover. The error must include `manual recovery` and
        the source transcript path so the user can find what to clean up.
        """
        src = _make_transcript(
            tmp_env.transcript_dir, "racey_memo", frontmatter_publish=True
        )

        # Wrap _git so the parent rev-parse returns an unexpected sha,
        # simulating a race where another commit somehow landed in between.
        original_git = publish_mod._git

        def fake_git(args, cwd):
            # Detect the parent-sanity-check call: ["rev-parse", "<sha>^"].
            if (
                len(args) == 2
                and args[0] == "rev-parse"
                and args[1].endswith("^")
            ):
                proc = subprocess.CompletedProcess(
                    args=["git", *args],
                    returncode=0,
                    stdout="deadbeefcafebabe1234567890abcdef12345678\n",
                    stderr="",
                )
                return proc
            return original_git(args, cwd)

        # Take a HEAD snapshot of the bare remote so we can verify nothing got pushed.
        remote_before = _git(["rev-parse", "HEAD"], cwd=tmp_env.bare).stdout.strip()

        with patch.object(publish_mod, "_git", side_effect=fake_git):
            result = publish_note(src)

        # Status was changed from "error" to "manual_recovery" (P2 Bug 1 fix)
        # so the watcher does not retry — the commit is in HEAD and the user
        # must resolve it manually.
        assert result["status"] == "manual_recovery", result
        assert result["error"] is not None

        err = result["error"].lower()
        # Must say "manual recovery" so user knows it won't auto-retry.
        assert "manual recovery" in err, result["error"]
        # Must include source transcript path so user can find it.
        assert str(src) in result["error"], result["error"]
        # Sanity: error mentions HEAD moving.
        assert "head moved" in err, result["error"]

        # commit_sha is set because the commit DID succeed before the check.
        assert result["commit_sha"]
        # site_path is set because the file was written (and is now in HEAD).
        assert result["site_path"] is not None

        # Nothing was pushed to the bare remote.
        remote_after = _git(["rev-parse", "HEAD"], cwd=tmp_env.bare).stdout.strip()
        assert remote_after == remote_before, (
            "must NOT push when HEAD-sanity-check fails"
        )

    def test_push_failure_with_unstable_git_aborts_manual_recovery(self, tmp_env):
        """P2 Bug 2 regression: if a recovery git command fails (e.g. index
        lock from another git process), publish_note must NOT proceed with
        further rollback steps. It must:
          - Return status='manual_recovery' (so watcher blocks the source)
          - NOT unlink the site file
          - Mention which step failed in the error message
        """
        src = _make_transcript(
            tmp_env.transcript_dir, "unstable_git", frontmatter_publish=True
        )

        # Snapshot HEAD to reason about whether the commit was made.
        head_before = _git(["rev-parse", "HEAD"], cwd=tmp_env.site).stdout.strip()

        original_git_ok = publish_mod._git_ok
        original_try_recovery = publish_mod._try_recovery_git

        def fake_git_ok(args, cwd):
            # The push goes through _git_ok. Fail it to enter the rollback.
            if args and args[0] == "push":
                return subprocess.CompletedProcess(
                    args=["git", *args],
                    returncode=1,
                    stdout="",
                    stderr="simulated push failure",
                )
            return original_git_ok(args, cwd)

        def fake_try_recovery(args, cwd, step_name):
            # The rollback path uses _try_recovery_git. Simulate an index-lock
            # failure on the soft-reset step, mirroring what would happen if
            # another git process is mid-operation in the same repo.
            if step_name == "soft-reset":
                return False, (
                    "fatal: Unable to create '.../.git/index.lock': "
                    "File exists. Another git process seems to be running."
                )
            return original_try_recovery(args, cwd, step_name)

        with patch.object(publish_mod, "_git_ok", side_effect=fake_git_ok), \
                patch.object(
                    publish_mod, "_try_recovery_git", side_effect=fake_try_recovery
                ):
            result = publish_note(src)

        # Bug 2 fix: must escalate to manual_recovery, not "error".
        assert result["status"] == "manual_recovery", result
        assert result["error"] is not None
        err = result["error"].lower()
        # Error must mention which recovery step failed.
        assert "soft-reset" in err, result["error"]
        assert "manual recovery" in err, result["error"]

        # The site file must NOT have been unlinked: rollback aborted before
        # the unlink step, so the file (committed in HEAD) is still tracked.
        assert result["site_path"] is not None
        assert result["site_path"].exists(), (
            "unlink must NOT run when soft-reset failed; the publish commit "
            "is still in HEAD"
        )

        # Our commit is still in HEAD: HEAD != head_before because the
        # soft-reset failed so the publish commit was never undone.
        head_after = _git(["rev-parse", "HEAD"], cwd=tmp_env.site).stdout.strip()
        assert head_after != head_before, (
            "publish commit should still be in HEAD because soft-reset failed"
        )

        # The publish commit DID succeed before the rollback was attempted.
        assert result["commit_sha"]


# ---------------------------------------------------------------------------
# run_once
# ---------------------------------------------------------------------------


class TestRunOnce:
    def test_publishes_and_updates_ledger(self, tmp_env):
        src = _make_transcript(tmp_env.transcript_dir, "memo", frontmatter_publish=True)

        results = run_once()

        assert len(results) == 1
        assert results[0]["status"] == "published"

        # Ledger now has the slug.
        data = json.loads(tmp_env.ledger_path.read_text(encoding="utf-8"))
        assert "thinking-about-static-site-builds" in data
        entry = data["thinking-about-static-site-builds"]
        assert Path(entry["source"]).resolve() == src.resolve()
        assert entry["commit_sha"]
        # The watcher now records the content_hash too.
        assert "content_hash" in entry, entry
        assert len(entry["content_hash"]) == 64, entry["content_hash"]

        # Second pass: nothing new to do.
        results2 = run_once()
        assert results2 == []

    def test_skip_does_not_mark_ledger(self, tmp_env):
        """A skipped publish (empty transcript) must NOT update the ledger."""
        _make_transcript_empty_section(
            tmp_env.transcript_dir, "thin", frontmatter_publish=True
        )

        results = run_once()

        assert len(results) == 1
        assert results[0]["status"] == "skipped"
        # Ledger not updated -> file would be re-scanned next cycle.
        if tmp_env.ledger_path.exists():
            data = json.loads(tmp_env.ledger_path.read_text(encoding="utf-8"))
        else:
            data = {}
        assert data == {}

    def test_republish_overwrites_same_slug_no_duplicate_files(self, tmp_env):
        """Codex P1 regression: editing a published source must OVERWRITE the
        existing `<slug>.md` instead of bumping to `<slug>-2.md` (which would
        leave the original stale and cause an infinite republish loop)."""
        src = _make_transcript(
            tmp_env.transcript_dir, "memo", frontmatter_publish=True
        )

        # First publish.
        results1 = run_once()
        assert len(results1) == 1
        assert results1[0]["status"] == "published"
        first_slug = results1[0]["slug"]
        site_dir = config.SITE_NOTES_DIR
        assert (site_dir / f"{first_slug}.md").exists()

        # User edits the transcript body.
        _make_transcript(
            tmp_env.transcript_dir,
            "memo",
            frontmatter_publish=True,
            transcript_body="Totally different content after editing.",
        )

        # Second publish (republish).
        results2 = run_once()
        assert len(results2) == 1, results2
        assert results2[0]["status"] == "published"
        # SAME slug -- not bumped to -2.
        assert results2[0]["slug"] == first_slug

        # And there is exactly ONE site file matching that slug stem (no -2,
        # -3, etc.).
        matching = list(site_dir.glob(f"{first_slug}*.md"))
        assert len(matching) == 1, (
            f"Expected exactly one site file for slug {first_slug!r}, found: "
            f"{[p.name for p in matching]}"
        )

        # The ledger has exactly one entry for this source (not duplicate slug
        # entries piling up).
        data = json.loads(tmp_env.ledger_path.read_text(encoding="utf-8"))
        entries_for_src = [
            slug for slug, e in data.items()
            if Path(e["source"]).resolve() == src.resolve()
        ]
        assert entries_for_src == [first_slug], entries_for_src


# ---------------------------------------------------------------------------
# Manual-recovery -> blocked ledger (P2 Bug 1)
# ---------------------------------------------------------------------------


class TestManualRecoveryBlocking:
    def _make_publish_returning_manual_recovery(self, src: Path):
        """Return a fake publish_note that simulates a manual-recovery result."""

        def fake_publish_note(path: Path, *, existing_slug: str | None = None):
            return publish_mod.PublishResult(
                status="manual_recovery",
                slug="thinking-about-static-site-builds",
                reason=None,
                commit_sha="deadbeef" * 5,
                site_path=Path("/fake/site/path/thinking-about-static-site-builds.md"),
                error=(
                    "HEAD moved unexpectedly between commit and push. "
                    "Manual recovery needed."
                ),
                content_hash=None,
                published_at=None,
            )

        return fake_publish_note

    def test_manual_recovery_blocks_future_publishes(self, tmp_env):
        """P2 Bug 1: a manual_recovery result must add the source to the
        blocked ledger so the next scan does NOT re-process it (which would
        otherwise pick `<slug>-2` and risk a duplicate publish).
        """
        src = _make_transcript(
            tmp_env.transcript_dir, "blocked_memo", frontmatter_publish=True
        )

        # Sanity: before publish, the file is publishable.
        results = scan_for_publishable(
            transcript_dir=tmp_env.transcript_dir,
            ledger=PublishedLedger(path=tmp_env.ledger_path),
        )
        assert src in results

        # Run once with publish_note mocked to return manual_recovery.
        with patch.object(
            watcher_mod, "publish_note",
            side_effect=self._make_publish_returning_manual_recovery(src),
        ):
            results = run_once(
                transcript_dir=tmp_env.transcript_dir,
                ledger=PublishedLedger(path=tmp_env.ledger_path),
            )

        assert len(results) == 1
        assert results[0]["status"] == "manual_recovery"

        # The blocked ledger sidecar must exist with the source recorded.
        blocked_path = tmp_env.ledger_path.with_suffix(
            ".blocked" + tmp_env.ledger_path.suffix
        )
        assert blocked_path.exists(), (
            f"expected blocked ledger at {blocked_path}, found none"
        )
        blocked_data = json.loads(blocked_path.read_text(encoding="utf-8"))
        # Key is the resolved source path.
        key = str(src.resolve())
        assert key in blocked_data, blocked_data
        entry = blocked_data[key]
        assert entry["blocked"] is True
        assert entry["slug"] == "thinking-about-static-site-builds"
        assert entry["commit_sha"] == "deadbeef" * 5
        assert "manual recovery" in entry["error"].lower()
        assert entry["blocked_at"]  # ISO timestamp populated

        # The next scan must NOT include the still-#publish-tagged source.
        next_results = scan_for_publishable(
            transcript_dir=tmp_env.transcript_dir,
            ledger=PublishedLedger(path=tmp_env.ledger_path),
        )
        assert next_results == [], (
            f"manual-recovery source was re-scanned; would risk duplicate "
            f"publish. Results: {next_results}"
        )

        # And the watcher's run_once should also be a no-op now.
        with patch.object(
            watcher_mod, "publish_note",
            side_effect=self._make_publish_returning_manual_recovery(src),
        ):
            second_results = run_once(
                transcript_dir=tmp_env.transcript_dir,
                ledger=PublishedLedger(path=tmp_env.ledger_path),
            )
        assert second_results == [], (
            f"run_once must skip blocked sources, got: {second_results}"
        )

    def test_clear_blocked_allows_republish(self, tmp_env):
        """After the user resolves the manual-recovery situation and calls
        clear_blocked(source), the next scan picks the file up again.
        """
        src = _make_transcript(
            tmp_env.transcript_dir, "to_unblock", frontmatter_publish=True
        )

        # Block it first.
        with patch.object(
            watcher_mod, "publish_note",
            side_effect=self._make_publish_returning_manual_recovery(src),
        ):
            run_once(
                transcript_dir=tmp_env.transcript_dir,
                ledger=PublishedLedger(path=tmp_env.ledger_path),
            )

        # Confirm it's blocked.
        blocked_path = tmp_env.ledger_path.with_suffix(
            ".blocked" + tmp_env.ledger_path.suffix
        )
        assert blocked_path.exists()
        scanned = scan_for_publishable(
            transcript_dir=tmp_env.transcript_dir,
            ledger=PublishedLedger(path=tmp_env.ledger_path),
        )
        assert scanned == []

        # Clear the block.
        was_blocked = clear_blocked(
            src,
            blocked_ledger=BlockedLedger(path=blocked_path),
        )
        assert was_blocked is True

        # Now the source should be publishable again.
        scanned_again = scan_for_publishable(
            transcript_dir=tmp_env.transcript_dir,
            ledger=PublishedLedger(path=tmp_env.ledger_path),
        )
        assert src in scanned_again, (
            f"after clear_blocked, source must be publishable again. "
            f"Got: {scanned_again}"
        )

        # Calling clear_blocked on an already-cleared source returns False.
        assert clear_blocked(
            src, blocked_ledger=BlockedLedger(path=blocked_path)
        ) is False


# ---------------------------------------------------------------------------
# Smoke: imports at module level
# ---------------------------------------------------------------------------


def test_public_imports():
    from publisher.publish import publish_note as _p  # noqa: F401
    from publisher.watcher import run_once as _r, run_forever as _f  # noqa: F401
