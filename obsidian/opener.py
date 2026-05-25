"""Open a markdown file inside the running Obsidian app via its URI scheme.

We deliberately go through ``obsidian://open?vault=...&file=...`` rather than
calling :func:`os.startfile` directly on the ``.md`` path: the URI scheme is
handled by Obsidian itself, so the file always opens in Obsidian even when the
default ``.md`` handler on the user's machine is something else (VS Code,
Notepad, etc.).

Public API:
    open_in_obsidian(md_path: Path) -> None
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from urllib.parse import quote

import config

logger = logging.getLogger(__name__)

__all__ = ["build_obsidian_uri", "open_in_obsidian"]


def build_obsidian_uri(md_path: Path, vault_root: Path | None = None) -> str:
    """Build the ``obsidian://open?vault=...&file=...`` URI for ``md_path``.

    Parameters
    ----------
    md_path:
        Absolute path to a markdown file inside the Obsidian vault.
    vault_root:
        Override for ``config.VAULT_ROOT`` (used by tests). The vault name is
        the folder name of this path.

    Returns
    -------
    str
        A fully URL-encoded Obsidian deep link. The ``.md`` extension is
        stripped from the file portion, matching Obsidian's URI convention.
    """
    root = Path(vault_root) if vault_root is not None else config.VAULT_ROOT
    md_path = Path(md_path)

    rel = md_path.relative_to(root)
    # Drop the trailing `.md` per Obsidian URI convention.
    if rel.suffix.lower() == ".md":
        rel = rel.with_suffix("")

    rel_str = rel.as_posix()
    vault_name = root.name

    encoded_vault = quote(vault_name, safe="")
    encoded_file = quote(rel_str, safe="")
    return f"obsidian://open?vault={encoded_vault}&file={encoded_file}"


def open_in_obsidian(md_path: Path) -> None:
    """Launch Obsidian and focus ``md_path``.

    Failures are logged but never raised — the cleaned markdown file has
    already been written to disk, so being unable to launch Obsidian is a
    convenience problem, not a pipeline failure.
    """
    try:
        uri = build_obsidian_uri(Path(md_path))
    except Exception:
        logger.exception("Could not build Obsidian URI for %s", md_path)
        return

    try:
        # ``os.startfile`` is the simplest way to dispatch a URI on Windows;
        # the Shell registers ``obsidian://`` as a protocol handler when
        # Obsidian is installed.
        os.startfile(uri)  # type: ignore[attr-defined]  # Windows-only
        logger.info("Opened in Obsidian: %s", uri)
    except Exception:
        logger.exception("Failed to open %s in Obsidian (uri=%s)", md_path, uri)
