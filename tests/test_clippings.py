"""Tests for the clipping -> /consuming publish flow in publisher/.

NO LLM is involved — a clipping page is a deterministic transformation of the
source Obsidian clipping file. We reuse the same tmp-git-repo pattern as
``tests/test_publisher.py`` (a bare remote + a local clone, no real network /
no real personal_site push).
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Any

import pytest

# Ensure repo root on sys.path.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import config  # noqa: E402
from publisher import publish as publish_mod  # noqa: E402
from publisher import watcher as watcher_mod  # noqa: E402
from publisher.publish import (  # noqa: E402
    _consuming_slugify,
    _normalize_authors,
    _strip_wikilink,
    _update_consuming_index,
    publish_clipping,
)
from publisher.watcher import (  # noqa: E402
    PublishedLedger,
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


# The seed index mirrors the real site's consuming/index.md: an intro
# paragraph, one existing 2025/October entry, and the trailing structure
# comment. We assert these survive every update.
_SEED_INDEX = """\
---
title: What I'm Consuming
layout: ../../layouts/Layout.astro
---

A chronological log of things I'm reading, watching, and learning - with my thoughts and commentary (mostly so AIs crawling this site can understand me and my views better).

---

## 2025

### October

- **[Jim Rutt on the Win Win Podcast](/consuming/Jim-Rutt-on-Win-Win)** - Jim Rutt argues that our current socioeconomic system is self-terminating.

<!--
Structure for new entries:
- Create a new markdown file for your summary (e.g., something-i-read.md)
- Add the entry to this index in reverse chronological order
-->
"""


def _init_site_repo(tmp_path: Path) -> tuple[Path, Path]:
    """Create a bare `origin` repo and a site clone wired to it.

    Seeds BOTH src/pages/notes (so the notes flow could run) and
    src/pages/consuming (index.md + a layout placeholder). Returns
    (site, bare_origin).
    """
    bare = tmp_path / "origin.git"
    bare.mkdir()
    _git(["init", "--bare", "-b", "main"], cwd=bare)

    site = tmp_path / "site"
    site.mkdir()
    _git(["init", "-b", "main"], cwd=site)
    _git(["config", "user.email", "test@example.com"], cwd=site)
    _git(["config", "user.name", "Test"], cwd=site)
    _git(["config", "commit.gpgsign", "false"], cwd=site)

    notes_dir = site / "src" / "pages" / "notes"
    notes_dir.mkdir(parents=True)
    (notes_dir / "how-this-website-works.md").write_text(
        "---\ntitle: How This Website Works\nlayout: ../../layouts/Layout.astro\n"
        "created: 2026-03-30\ndescription: seed\n---\n\nSeed body.\n",
        encoding="utf-8",
    )

    consuming_dir = site / "src" / "pages" / "consuming"
    consuming_dir.mkdir(parents=True)
    (consuming_dir / "index.md").write_text(_SEED_INDEX, encoding="utf-8")

    _git(["add", "."], cwd=site)
    _git(["commit", "-m", "seed"], cwd=site)
    _git(["remote", "add", "origin", str(bare)], cwd=site)
    _git(["push", "-u", "origin", "main"], cwd=site)

    return site, bare


def _make_clipping(
    dir_: Path,
    name: str,
    *,
    title: str = "AI Tools for Existential Security — EA Forum",
    source: str | None = "https://forum.effectivealtruism.org/posts/abc/ai-tools",
    author: list[str] | str | None = None,
    created: str | None = "2025-11-27",
    description: str | None = "Rapid AI progress is the greatest driver of existential risk today.",
    comment: str | None = None,
    body: str = "This is the mirrored clipping body.\n\nIt has multiple paragraphs.",
    publish: bool = True,
    comment_field: str = "comment",
) -> Path:
    """Write an Obsidian-clipper-shaped clipping `.md` to ``dir_``.

    Mirrors the real clipping frontmatter: title, source (URL), author (list of
    [[wikilink]]s), published/created dates, description, tags: [clippings].
    Adds `publish` to the tags when ``publish`` is True.
    """
    dir_.mkdir(parents=True, exist_ok=True)
    if author is None:
        author = ["[[Lizka]]", "[[Owen Cotton-Barratt]]"]

    fm_lines = ["---", f'title: "{title}"']
    if source is not None:
        fm_lines.append(f'source: "{source}"')
    # author as a YAML list
    if isinstance(author, list):
        fm_lines.append("author:")
        for a in author:
            fm_lines.append(f'  - "{a}"')
    elif author:
        fm_lines.append(f'author: "{author}"')
    if created is not None:
        fm_lines.append(f"created: {created}")
    if description is not None:
        fm_lines.append(f'description: "{description}"')
    if comment is not None:
        fm_lines.append(f'{comment_field}: "{comment}"')
    tags = ["clippings"]
    if publish:
        tags.append("publish")
    fm_lines.append(f"tags: [{', '.join(tags)}]")
    fm_lines.append("---")

    md = "\n".join(fm_lines) + "\n" + body + "\n"
    p = dir_ / f"{name}.md"
    p.write_text(md, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# fixture: monkeypatch config paths to tmp (clipping-aware)
# ---------------------------------------------------------------------------


class types_ns:  # noqa: N801
    def __init__(self, **kw: Any) -> None:
        self.__dict__.update(kw)


@pytest.fixture
def tmp_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    site, bare = _init_site_repo(tmp_path)

    transcript_dir = tmp_path / "transcripts"
    transcript_dir.mkdir()
    clippings_dir = tmp_path / "clippings"  # created here so the scan sees it
    clippings_dir.mkdir()
    drafts_dir = tmp_path / "drafts"
    drafts_dir.mkdir()
    ledger_path = tmp_path / ".published.json"

    notes_dir = site / "src" / "pages" / "notes"
    consuming_dir = site / "src" / "pages" / "consuming"

    monkeypatch.setattr(config, "SITE_ROOT", site)
    monkeypatch.setattr(config, "SITE_NOTES_DIR", notes_dir)
    monkeypatch.setattr(config, "SITE_CONSUMING_DIR", consuming_dir)
    monkeypatch.setattr(config, "CLIPPINGS_DIR", clippings_dir)
    monkeypatch.setattr(config, "CLIPPING_COMMENT_FIELD", "comment")
    monkeypatch.setattr(config, "PUBLISH_ENABLED", True)
    monkeypatch.setattr(config, "TRANSCRIPT_DIR", transcript_dir)
    monkeypatch.setattr(config, "DRAFTS_DIR", drafts_dir)
    monkeypatch.setattr(config, "PUBLISHED_LEDGER", ledger_path)

    yield types_ns(
        tmp_path=tmp_path,
        site=site,
        bare=bare,
        transcript_dir=transcript_dir,
        clippings_dir=clippings_dir,
        drafts_dir=drafts_dir,
        ledger_path=ledger_path,
        notes_dir=notes_dir,
        consuming_dir=consuming_dir,
    )


# ---------------------------------------------------------------------------
# Pure helpers: slugify, wikilink, authors
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_consuming_slugify_title_case_hyphen(self):
        assert (
            _consuming_slugify("AI Tools for Existential Security — EA Forum")
            == "AI-Tools-for-Existential-Security-EA-Forum"
        )

    def test_consuming_slugify_preserves_case(self):
        assert _consuming_slugify("Jim Rutt on Win-Win") == "Jim-Rutt-on-Win-Win"

    def test_consuming_slugify_strips_apostrophes(self):
        assert _consuming_slugify("What I'm Consuming") == "What-Im-Consuming"

    def test_consuming_slugify_collapses_punct_runs(self):
        assert _consuming_slugify("Foo: Bar -- Baz!!!") == "Foo-Bar-Baz"

    def test_consuming_slugify_empty_fallback(self):
        assert _consuming_slugify("   ") == "clipping"
        assert _consuming_slugify("———") == "clipping"

    def test_strip_wikilink_plain(self):
        assert _strip_wikilink("[[Lizka]]") == "Lizka"

    def test_strip_wikilink_alias(self):
        assert _strip_wikilink("[[target|Display Name]]") == "Display Name"

    def test_normalize_authors_list_of_wikilinks(self):
        assert (
            _normalize_authors(["[[Lizka]]", "[[Owen Cotton-Barratt]]", "[[Forethought]]"])
            == "Lizka, Owen Cotton-Barratt, Forethought"
        )

    def test_normalize_authors_single_string(self):
        assert _normalize_authors("[[Solo Author]]") == "Solo Author"

    def test_normalize_authors_none(self):
        assert _normalize_authors(None) == ""


# ---------------------------------------------------------------------------
# publish_clipping: page rendering
# ---------------------------------------------------------------------------


class TestPublishClippingRendering:
    def test_with_comment_renders_my_commentary(self, tmp_env):
        src = _make_clipping(
            tmp_env.clippings_dir,
            "clip_with_comment",
            comment="I think this framing of differential acceleration is exactly right.",
        )
        result = publish_clipping(src)
        assert result["status"] == "published", result

        page = result["site_path"].read_text(encoding="utf-8")
        assert "## My commentary" in page, page
        assert "differential acceleration is exactly right" in page
        assert "source: clipping" in page
        assert "layout: ../../layouts/Layout.astro" in page
        assert "backText: Consuming" in page

    def test_without_comment_omits_commentary_section(self, tmp_env):
        src = _make_clipping(
            tmp_env.clippings_dir, "clip_no_comment", comment=None
        )
        result = publish_clipping(src)
        assert result["status"] == "published", result

        page = result["site_path"].read_text(encoding="utf-8")
        assert "## My commentary" not in page, page
        # The body after frontmatter should start with the Source line (no
        # leading commentary divider).
        body_after_fm = page.split("---\n", 2)[-1].lstrip("\n")
        assert body_after_fm.startswith("**Source:**"), body_after_fm[:120]

    def test_author_wikilink_stripping_in_byline(self, tmp_env):
        src = _make_clipping(
            tmp_env.clippings_dir,
            "clip_authors",
            author=["[[Lizka]]", "[[Owen Cotton-Barratt]]"],
            comment="x",
        )
        result = publish_clipping(src)
        assert result["status"] == "published", result
        page = result["site_path"].read_text(encoding="utf-8")
        assert "By Lizka, Owen Cotton-Barratt" in page, page
        assert "[[" not in page, "wikilink brackets leaked into the page"

    def test_source_link_and_disclaimer_present(self, tmp_env):
        url = "https://forum.effectivealtruism.org/posts/abc/ai-tools"
        src = _make_clipping(
            tmp_env.clippings_dir, "clip_src", source=url, comment="x"
        )
        result = publish_clipping(src)
        assert result["status"] == "published", result
        page = result["site_path"].read_text(encoding="utf-8")

        # Pretty source is the host (path is short enough to keep here).
        assert f"]({url})" in page, page
        assert "forum.effectivealtruism.org" in page
        # Disclaimer blockquote now describes a short preview pointing at the
        # source "above".
        assert "Mirrored as a short preview" in page
        assert "the source above" in page

    def test_no_source_url_renders_not_recorded(self, tmp_env):
        src = _make_clipping(
            tmp_env.clippings_dir, "clip_nosrc", source=None, comment="x"
        )
        result = publish_clipping(src)
        assert result["status"] == "published", result
        page = result["site_path"].read_text(encoding="utf-8")
        assert "**Source:** _(original URL not recorded)_" in page, page
        # Disclaimer must NOT claim there's an original "above".
        assert "the source above" not in page, page
        assert "original source URL was not recorded" in page

    def test_no_authors_omits_by(self, tmp_env):
        src = _make_clipping(
            tmp_env.clippings_dir, "clip_noauthor", author=[], comment="x"
        )
        result = publish_clipping(src)
        assert result["status"] == "published", result
        page = result["site_path"].read_text(encoding="utf-8")
        # Byline still has "clipped <date>" but no "By ".
        assert "clipped 2025-11-27" in page, page
        assert re.search(r"^By ", page, flags=re.MULTILINE) is None, page

    def test_body_mirrored_verbatim(self, tmp_env):
        marker = "UNIQUE-MARKER-PHRASE-banana-grommet-42"
        src = _make_clipping(
            tmp_env.clippings_dir, "clip_body", body=f"Intro.\n\n{marker}\n\nOutro."
        )
        result = publish_clipping(src)
        assert result["status"] == "published", result
        page = result["site_path"].read_text(encoding="utf-8")
        assert marker in page

    def test_description_prefers_comment_excerpt(self, tmp_env):
        src = _make_clipping(
            tmp_env.clippings_dir,
            "clip_desc",
            comment="My sharp one-line take on this piece.",
            description="The clip's own description that should be overridden.",
        )
        result = publish_clipping(src)
        assert result["status"] == "published", result
        page = result["site_path"].read_text(encoding="utf-8")
        m = re.search(r'^description:\s*"(?P<d>.*)"\s*$', page, flags=re.MULTILINE)
        assert m, page
        assert "My sharp one-line take" in m.group("d")

    def test_slug_style(self, tmp_env):
        src = _make_clipping(
            tmp_env.clippings_dir,
            "clip_slug",
            title="AI Tools for Existential Security — EA Forum",
        )
        result = publish_clipping(src)
        assert result["status"] == "published", result
        assert result["slug"] == "AI-Tools-for-Existential-Security-EA-Forum"
        assert result["site_path"].name == (
            "AI-Tools-for-Existential-Security-EA-Forum.md"
        )


# ---------------------------------------------------------------------------
# publish_clipping: body preview / fade-out + CTA
# ---------------------------------------------------------------------------

# A pinch of stable, sentence-y filler so truncation has clean boundaries.
_FADE_DIV_SNIPPET = 'background:linear-gradient(to bottom, transparent, var(--color-bg)'
_CTA_LINK_TEXT = "Read the full version at the source →"


class TestPublishClippingPreview:
    def test_long_body_truncated_with_fade_and_cta(self, tmp_env, monkeypatch):
        # Small limit so we don't need a giant fixture.
        monkeypatch.setattr(config, "CLIPPING_PREVIEW_CHARS", 120)
        url = "https://example.com/the-original-post"
        opening = (
            "This opening paragraph is the part readers should see. "
            "It comfortably exceeds the tiny preview limit on its own."
        )
        tail = "SECRET-TAIL-MARKER-only-after-the-cut should never be published."
        body = f"{opening}\n\n{tail}"
        src = _make_clipping(
            tmp_env.clippings_dir,
            "clip_long",
            source=url,
            body=body,
            comment="x",
        )
        result = publish_clipping(src)
        assert result["status"] == "published", result
        page = result["site_path"].read_text(encoding="utf-8")

        # Truncated opening is present...
        assert "This opening paragraph is the part readers should see." in page
        # ...but the text AFTER the cut is NOT.
        assert "SECRET-TAIL-MARKER-only-after-the-cut" not in page, page
        # Fade <div> block + CTA link to the source.
        assert _FADE_DIV_SNIPPET in page, page
        assert "<div style=" in page
        assert f'<a href="{url}">{_CTA_LINK_TEXT}</a>' in page, page

    def test_short_body_full_no_fade(self, tmp_env, monkeypatch):
        monkeypatch.setattr(config, "CLIPPING_PREVIEW_CHARS", 1000)
        body = "Intro.\n\nA short and complete body well under the limit.\n\nOutro."
        src = _make_clipping(
            tmp_env.clippings_dir, "clip_short", body=body, comment="x"
        )
        result = publish_clipping(src)
        assert result["status"] == "published", result
        page = result["site_path"].read_text(encoding="utf-8")

        # Full body present.
        assert "A short and complete body well under the limit." in page
        assert "Outro." in page
        # No fade / CTA.
        assert _FADE_DIV_SNIPPET not in page, page
        assert _CTA_LINK_TEXT not in page, page

    def test_truncation_does_not_cut_midword(self, tmp_env, monkeypatch):
        monkeypatch.setattr(config, "CLIPPING_PREVIEW_CHARS", 60)
        # No paragraph/sentence break before the limit, so it must fall back to
        # a whitespace boundary -- never mid-word.
        body = (
            "alpha bravo charlie delta echo foxtrot golf hotel india juliet "
            "kilo lima mike november oscar papa quebec romeo sierra tango"
        )
        src = _make_clipping(
            tmp_env.clippings_dir, "clip_midword", body=body, comment="x"
        )
        result = publish_clipping(src)
        assert result["status"] == "published", result
        page = result["site_path"].read_text(encoding="utf-8")

        # Pull out the preview text (between the last `---` divider and the fade).
        before_fade = page.split("<div style=", 1)[0]
        preview = before_fade.rsplit("---\n", 1)[-1].strip()
        assert preview, page
        # The preview must end on a complete word that exists in the body.
        words = body.split()
        assert preview.split()[-1] in words
        # And the very next word after the preview's last word must NOT already
        # be glued on (i.e. we cut at a space, not inside a token).
        assert not preview.endswith("-"), preview
        # Every word in the preview is a whole word from the source body.
        for w in preview.split():
            assert w in words, (w, preview)

    def test_truncated_no_source_url_uses_note_not_link(self, tmp_env, monkeypatch):
        monkeypatch.setattr(config, "CLIPPING_PREVIEW_CHARS", 80)
        opening = "This opening easily exceeds the small preview limit set for this test."
        body = f"{opening}\n\nHidden remainder paragraph that is past the cut point."
        src = _make_clipping(
            tmp_env.clippings_dir,
            "clip_trunc_nosrc",
            source=None,
            body=body,
            comment="x",
        )
        result = publish_clipping(src)
        assert result["status"] == "published", result
        page = result["site_path"].read_text(encoding="utf-8")

        # Fade block still present.
        assert _FADE_DIV_SNIPPET in page, page
        assert "<div style=" in page
        # The "not mirrored" note, no link / no broken href.
        assert "Full text not mirrored — original source not recorded." in page, page
        assert "<a href=" not in page.split("<div style=", 1)[1], page
        assert _CTA_LINK_TEXT not in page, page


# ---------------------------------------------------------------------------
# publish_clipping: git + ledger + dedup behavior
# ---------------------------------------------------------------------------


class TestPublishClippingGit:
    def test_happy_path_commits_and_pushes(self, tmp_env):
        src = _make_clipping(tmp_env.clippings_dir, "clip_happy", comment="x")
        result = publish_clipping(src)
        assert result["status"] == "published", result

        # Commit landed locally and on the bare remote.
        log = _git(["log", "--oneline"], cwd=tmp_env.site).stdout
        assert "consuming: AI Tools for Existential Security" in log
        remote_log = _git(["log", "--oneline"], cwd=tmp_env.bare).stdout
        assert "consuming: AI Tools for Existential Security" in remote_log

        # The commit included BOTH the page and the index.
        files = _git(
            ["show", "--name-only", "--pretty=format:", "HEAD"], cwd=tmp_env.site
        ).stdout
        assert "src/pages/consuming/AI-Tools-for-Existential-Security-EA-Forum.md" in files
        assert "src/pages/consuming/index.md" in files

    def test_index_updated_with_bullet(self, tmp_env):
        src = _make_clipping(
            tmp_env.clippings_dir,
            "clip_idx",
            created="2025-11-27",
            comment="commentary",
        )
        result = publish_clipping(src)
        assert result["status"] == "published", result

        index = (tmp_env.consuming_dir / "index.md").read_text(encoding="utf-8")
        # November 2025 section created (months descending: Nov before Oct).
        assert "### November" in index
        slug = result["slug"]
        assert f"](/consuming/{slug})" in index

    def test_writes_published_at_to_source(self, tmp_env):
        src = _make_clipping(tmp_env.clippings_dir, "clip_wb", comment="x")
        assert "published_at:" not in src.read_text(encoding="utf-8")
        result = publish_clipping(src)
        assert result["status"] == "published", result
        post = src.read_text(encoding="utf-8")
        assert re.search(r'^published_at:\s*"', post, flags=re.MULTILINE), post

    def test_content_hash_is_64_hex(self, tmp_env):
        src = _make_clipping(tmp_env.clippings_dir, "clip_hash", comment="x")
        result = publish_clipping(src)
        assert result["status"] == "published", result
        h = result["content_hash"]
        assert isinstance(h, str) and re.fullmatch(r"[0-9a-f]{64}", h), h

    def test_slug_collision_first_time(self, tmp_env):
        # Pre-create a colliding page and commit it.
        existing = tmp_env.consuming_dir / "AI-Tools-for-Existential-Security-EA-Forum.md"
        existing.write_text(
            "---\ntitle: pre\nlayout: ../../layouts/Layout.astro\n---\n\npre\n",
            encoding="utf-8",
        )
        _git(["add", "."], cwd=tmp_env.site)
        _git(["commit", "-m", "pre-existing consuming page"], cwd=tmp_env.site)
        _git(["push", "origin", "main"], cwd=tmp_env.site)

        src = _make_clipping(tmp_env.clippings_dir, "clip_collide", comment="x")
        result = publish_clipping(src)
        assert result["status"] == "published", result
        assert result["slug"] == "AI-Tools-for-Existential-Security-EA-Forum-2"
        # Pre-existing untouched.
        assert "pre" in existing.read_text(encoding="utf-8")

    def test_republish_via_existing_slug_overwrites(self, tmp_env):
        src = _make_clipping(
            tmp_env.clippings_dir, "clip_repub", comment="first take"
        )
        first = publish_clipping(src)
        assert first["status"] == "published", first
        slug = first["slug"]

        # Republish with the SAME slug (as the watcher would) after editing.
        _make_clipping(
            tmp_env.clippings_dir, "clip_repub", comment="REVISED take"
        )
        second = publish_clipping(src, existing_slug=slug)
        assert second["status"] == "published", second
        assert second["slug"] == slug, "must reuse slug, not bump to -2"

        # Exactly one page file for this slug stem.
        matching = list(tmp_env.consuming_dir.glob(f"{slug}*.md"))
        assert len(matching) == 1, [p.name for p in matching]
        assert "REVISED take" in matching[0].read_text(encoding="utf-8")

        # Index has exactly one bullet for the slug (idempotent).
        index = (tmp_env.consuming_dir / "index.md").read_text(encoding="utf-8")
        assert index.count(f"](/consuming/{slug})") == 1, index

    def test_disabled_guard_site_root_none(self, tmp_path: Path, monkeypatch):
        clippings_dir = tmp_path / "clippings"
        clippings_dir.mkdir()
        monkeypatch.setattr(config, "SITE_ROOT", None)
        monkeypatch.setattr(config, "SITE_CONSUMING_DIR", None)
        monkeypatch.setattr(config, "PUBLISH_ENABLED", False)
        monkeypatch.setattr(config, "CLIPPINGS_DIR", clippings_dir)

        src = _make_clipping(clippings_dir, "clip_disabled", comment="x")
        result = publish_clipping(src)
        assert result["status"] == "error", result
        assert result["error"] and "NOTES_SITE_ROOT" in result["error"]

    def test_no_title_errors(self, tmp_env):
        # Write a clipping with a blank title.
        p = tmp_env.clippings_dir / "clip_notitle.md"
        p.write_text(
            "---\ntitle:\nsource: \"https://x.com\"\ntags: [clippings, publish]\n---\nbody\n",
            encoding="utf-8",
        )
        result = publish_clipping(p)
        assert result["status"] == "error", result
        assert result["error"] and "title" in result["error"].lower()


# ---------------------------------------------------------------------------
# Content-hash dedup (edit comment OR body -> republish)
# ---------------------------------------------------------------------------


class TestClippingDedup:
    def _publish_and_mark(self, tmp_env, src):
        result = publish_clipping(src)
        assert result["status"] == "published", result
        ledger = PublishedLedger(path=tmp_env.ledger_path)
        ledger.mark_published(
            result["slug"],
            site_path=result["site_path"],
            commit_sha=result["commit_sha"],
            source=src,
            content_hash=result["content_hash"],
        )
        return result

    def test_unchanged_clipping_not_rescanned(self, tmp_env):
        src = _make_clipping(tmp_env.clippings_dir, "clip_stable", comment="stable")
        self._publish_and_mark(tmp_env, src)
        results = scan_for_publishable(
            transcript_dir=tmp_env.transcript_dir,
            clippings_dir=tmp_env.clippings_dir,
            ledger=PublishedLedger(path=tmp_env.ledger_path),
        )
        assert src not in results, results

    def test_editing_comment_triggers_republish(self, tmp_env):
        src = _make_clipping(tmp_env.clippings_dir, "clip_editcom", comment="v1")
        self._publish_and_mark(tmp_env, src)
        # Re-write with a changed comment (also strips published_at).
        _make_clipping(tmp_env.clippings_dir, "clip_editcom", comment="v2 changed")
        results = scan_for_publishable(
            transcript_dir=tmp_env.transcript_dir,
            clippings_dir=tmp_env.clippings_dir,
            ledger=PublishedLedger(path=tmp_env.ledger_path),
        )
        assert src in results, results

    def test_editing_body_triggers_republish(self, tmp_env):
        src = _make_clipping(
            tmp_env.clippings_dir, "clip_editbody", comment="same", body="body v1"
        )
        self._publish_and_mark(tmp_env, src)
        _make_clipping(
            tmp_env.clippings_dir, "clip_editbody", comment="same", body="body v2 DIFFERENT"
        )
        results = scan_for_publishable(
            transcript_dir=tmp_env.transcript_dir,
            clippings_dir=tmp_env.clippings_dir,
            ledger=PublishedLedger(path=tmp_env.ledger_path),
        )
        assert src in results, results

    def test_published_at_writeback_doesnt_trigger_republish(self, tmp_env):
        src = _make_clipping(tmp_env.clippings_dir, "clip_wbnorepub", comment="x")
        self._publish_and_mark(tmp_env, src)
        # publish_clipping wrote published_at back; that must not re-trigger.
        assert "published_at:" in src.read_text(encoding="utf-8")
        results = scan_for_publishable(
            transcript_dir=tmp_env.transcript_dir,
            clippings_dir=tmp_env.clippings_dir,
            ledger=PublishedLedger(path=tmp_env.ledger_path),
        )
        assert src not in results, results


# ---------------------------------------------------------------------------
# Watcher integration: notes + clippings in one pass, routing
# ---------------------------------------------------------------------------


class TestWatcherRouting:
    def test_is_clipping_source(self, tmp_env):
        clip = tmp_env.clippings_dir / "a.md"
        clip.write_text("x", encoding="utf-8")
        note = tmp_env.transcript_dir / "b.md"
        note.write_text("y", encoding="utf-8")
        assert watcher_mod._is_clipping_source(clip) is True
        assert watcher_mod._is_clipping_source(note) is False

    def test_run_once_publishes_clipping_and_marks_ledger(self, tmp_env):
        src = _make_clipping(tmp_env.clippings_dir, "clip_runonce", comment="hello")
        results = run_once()
        assert len(results) == 1, results
        assert results[0]["status"] == "published"

        data = json.loads(tmp_env.ledger_path.read_text(encoding="utf-8"))
        slug = results[0]["slug"]
        assert slug in data
        assert Path(data[slug]["source"]).resolve() == src.resolve()
        assert len(data[slug]["content_hash"]) == 64

        # Second pass: nothing to do.
        assert run_once() == []

    def test_run_once_handles_both_note_and_clipping(self, tmp_env):
        # A transcript note with #publish.
        note = tmp_env.transcript_dir / "memo.md"
        note.write_text(
            '---\ntitle: "My Memo"\ntags: [voice-memo, publish]\nsource: voice-memo\n'
            "---\n\n## Transcript\nThis is the cleaned transcript body.\n",
            encoding="utf-8",
        )
        # A clipping with #publish.
        _make_clipping(tmp_env.clippings_dir, "clip_both", comment="take")

        results = run_once()
        statuses = sorted(r["status"] for r in results)
        assert statuses == ["published", "published"], results

        # One note page + one consuming page exist.
        assert any(tmp_env.notes_dir.glob("*.md"))
        consuming_pages = [
            p for p in tmp_env.consuming_dir.glob("*.md") if p.name != "index.md"
        ]
        assert len(consuming_pages) == 1, [p.name for p in consuming_pages]

    def test_hash_for_source_routes_by_kind(self, tmp_env):
        clip = _make_clipping(
            tmp_env.clippings_dir, "clip_hashroute", comment="c", body="b"
        )
        # The clipping hash must equal _compute_clipping_content_hash(title, comment, body)
        expected = publish_mod._compute_clipping_content_hash(
            "AI Tools for Existential Security — EA Forum", "c", "b\n"
        )
        assert watcher_mod._hash_for_source(clip) == expected


# ---------------------------------------------------------------------------
# _update_consuming_index: structural correctness
# ---------------------------------------------------------------------------


class TestConsumingIndex:
    def _seed(self, tmp_path: Path) -> Path:
        p = tmp_path / "index.md"
        p.write_text(_SEED_INDEX, encoding="utf-8")
        return p

    def test_existing_month_inserts_at_top(self, tmp_path: Path):
        idx = self._seed(tmp_path)
        _update_consuming_index(
            idx,
            title="New October Thing",
            slug="New-October-Thing",
            description="desc",
            entry_date=date(2025, 10, 15),
        )
        text = idx.read_text(encoding="utf-8")
        # New bullet is ABOVE the existing Jim Rutt bullet within ### October.
        new_pos = text.index("](/consuming/New-October-Thing)")
        jim_pos = text.index("](/consuming/Jim-Rutt-on-Win-Win)")
        assert new_pos < jim_pos, text

    def test_new_month_in_existing_year_descending(self, tmp_path: Path):
        idx = self._seed(tmp_path)
        _update_consuming_index(
            idx,
            title="A November Read",
            slug="A-November-Read",
            description="desc",
            entry_date=date(2025, 11, 3),
        )
        text = idx.read_text(encoding="utf-8")
        assert "### November" in text
        # November (newer) must appear before October within 2025.
        nov_pos = text.index("### November")
        oct_pos = text.index("### October")
        assert nov_pos < oct_pos, text

    def test_older_month_inserts_after_newer(self, tmp_path: Path):
        idx = self._seed(tmp_path)
        _update_consuming_index(
            idx,
            title="A September Read",
            slug="A-September-Read",
            description="desc",
            entry_date=date(2025, 9, 3),
        )
        text = idx.read_text(encoding="utf-8")
        sep_pos = text.index("### September")
        oct_pos = text.index("### October")
        assert oct_pos < sep_pos, "older month must come after newer"

    def test_new_year_descending(self, tmp_path: Path):
        idx = self._seed(tmp_path)
        _update_consuming_index(
            idx,
            title="A 2026 Read",
            slug="A-2026-Read",
            description="desc",
            entry_date=date(2026, 1, 5),
        )
        text = idx.read_text(encoding="utf-8")
        assert "## 2026" in text
        y2026 = text.index("## 2026")
        y2025 = text.index("## 2025")
        assert y2026 < y2025, "newer year must come first"

    def test_older_year_inserts_after(self, tmp_path: Path):
        idx = self._seed(tmp_path)
        _update_consuming_index(
            idx,
            title="A 2024 Read",
            slug="A-2024-Read",
            description="desc",
            entry_date=date(2024, 6, 5),
        )
        text = idx.read_text(encoding="utf-8")
        y2024 = text.index("## 2024")
        y2025 = text.index("## 2025")
        assert y2025 < y2024, "older year must come after newer"

    def test_idempotent_republish_updates_in_place(self, tmp_path: Path):
        idx = self._seed(tmp_path)
        for desc in ("first desc", "second desc", "third desc"):
            _update_consuming_index(
                idx,
                title="Repeated Read",
                slug="Repeated-Read",
                description=desc,
                entry_date=date(2025, 11, 1),
            )
        text = idx.read_text(encoding="utf-8")
        # Exactly one bullet, with the latest description.
        assert text.count("](/consuming/Repeated-Read)") == 1, text
        assert "third desc" in text
        assert "first desc" not in text

    def test_preserves_intro_and_trailing_comment(self, tmp_path: Path):
        idx = self._seed(tmp_path)
        _update_consuming_index(
            idx,
            title="Whatever",
            slug="Whatever",
            description="desc",
            entry_date=date(2025, 11, 1),
        )
        text = idx.read_text(encoding="utf-8")
        # Intro paragraph preserved.
        assert "A chronological log of things I'm reading" in text
        # Trailing structure comment preserved and still at the very bottom.
        assert "<!--" in text
        assert "Structure for new entries" in text
        # Nothing was inserted below the trailing comment.
        comment_pos = text.index("<!--")
        assert "](/consuming/Whatever)" in text
        assert text.index("](/consuming/Whatever)") < comment_pos, (
            "bullet must be above the trailing structure comment"
        )

    def test_frontmatter_h2_not_treated_as_year(self, tmp_path: Path):
        # Sanity: the index frontmatter has no `## YYYY`, but make sure a
        # heading-like intro line doesn't break year detection. Insert into a
        # brand-new year to force the "no matching year" path.
        idx = self._seed(tmp_path)
        _update_consuming_index(
            idx,
            title="Future Read",
            slug="Future-Read",
            description="desc",
            entry_date=date(2099, 12, 1),
        )
        text = idx.read_text(encoding="utf-8")
        assert "## 2099" in text
        # Must be the first year section (descending), before 2025.
        assert text.index("## 2099") < text.index("## 2025")
