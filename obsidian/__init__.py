"""Obsidian integration for the notes-pipeline.

Public API:
    write_transcript(tr, rec) -> tuple[Path, Path]
    append_memo_link(timestamp, slug, title, duration_sec) -> Path
    sanitize_slug(raw) -> str
    open_in_obsidian(md_path) -> None
    build_obsidian_uri(md_path, vault_root=None) -> str

The ``migrate_legacy`` module is a CLI entry point and is not re-exported
here; invoke it via ``python -m obsidian.migrate_legacy``.
"""

from __future__ import annotations

from obsidian.daily_note import append_memo_link
from obsidian.opener import build_obsidian_uri, open_in_obsidian
from obsidian.writer import sanitize_slug, write_transcript

__all__ = [
    "append_memo_link",
    "build_obsidian_uri",
    "open_in_obsidian",
    "sanitize_slug",
    "write_transcript",
]
