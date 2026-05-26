"""Central config for the audio-transcriber → Obsidian notes pipeline.

All paths and constants live here. Anything machine-specific is read from
environment variables (loaded from a local ``.env`` file next to this module)
so you can clone the repo and configure it without editing code.

See ``.env.example`` for every supported variable and its default.
"""

from __future__ import annotations

import os
import socket
from pathlib import Path

from dotenv import load_dotenv

# --- repo root + .env -------------------------------------------------------
# NP_ROOT is the repo root (this file lives at the top level). We load .env
# from here so the pipeline is self-contained and portable: clone the repo,
# drop a .env next to config.py, done.
NP_ROOT: Path = Path(__file__).resolve().parent
load_dotenv(NP_ROOT / ".env")


def _env_path(name: str, default: Path) -> Path:
    """Read ``name`` as an env var and return it as an expanded Path.

    Supports ``~`` and ``%VAR%`` / ``$VAR`` expansion. Falls back to
    ``default`` when the variable is unset or empty.
    """
    raw = os.environ.get(name)
    if raw and raw.strip():
        return Path(os.path.expandvars(raw.strip())).expanduser()
    return default


# --- Obsidian vault paths ---------------------------------------------------
# Point NOTES_VAULT_ROOT at your Obsidian vault. Subdirectories are created on
# demand the first time the pipeline writes to them (only if the vault root
# actually exists), so importing this module never scatters empty folders.
VAULT_ROOT: Path = _env_path("NOTES_VAULT_ROOT", Path.home() / "ObsidianVault")
AUDIO_DIR: Path = VAULT_ROOT / os.environ.get("NOTES_AUDIO_SUBDIR", "Audio")
TRANSCRIPT_DIR: Path = AUDIO_DIR / os.environ.get(
    "NOTES_TRANSCRIPT_SUBDIR", "transcriptions"
)
DAILY_DIR: Path = VAULT_ROOT / os.environ.get("NOTES_DAILY_SUBDIR", "Daily Notes")
# Obsidian web-clippings folder. Clippings tagged `#publish` are mirrored to the
# site's /consuming section (see publisher.publish.publish_clipping). Unlike the
# other vault subdirs we do NOT auto-create this on import (see the dir-creation
# guard at the bottom): not every vault has a clippings folder, and we only read
# from it -- never write -- so a phantom empty dir would be misleading.
CLIPPINGS_DIR: Path = VAULT_ROOT / os.environ.get(
    "NOTES_CLIPPINGS_VAULT_SUBDIR", "Clippings"
)

# --- personal-site publishing (OPT-IN, disabled by default) -----------------
# The "#publish" feature copies a cleaned note into a static-site repo and
# pushes it. It is DISABLED unless NOTES_SITE_ROOT is set to a real git repo:
# when unset/empty, SITE_ROOT is None, PUBLISH_ENABLED is False, and the tray
# never starts the publisher watcher. (The publish step is deterministic — no
# LLM is involved; Gemini is used only for transcript cleanup.)
_site_root_raw = os.environ.get("NOTES_SITE_ROOT")
SITE_ROOT: Path | None = (
    Path(os.path.expandvars(_site_root_raw.strip())).expanduser()
    if _site_root_raw and _site_root_raw.strip()
    else None
)
PUBLISH_ENABLED: bool = SITE_ROOT is not None
SITE_NOTES_DIR: Path | None = (
    SITE_ROOT / os.environ.get("NOTES_SITE_NOTES_SUBDIR", "src/pages/notes")
    if SITE_ROOT is not None
    else None
)
# Where mirrored web clippings are dropped inside the site repo (the
# /consuming section). None when publishing is disabled, like SITE_NOTES_DIR.
SITE_CONSUMING_DIR: Path | None = (
    SITE_ROOT / os.environ.get("NOTES_SITE_CONSUMING_SUBDIR", "src/pages/consuming")
    if SITE_ROOT is not None
    else None
)
# Name of the clipping frontmatter field holding the user's commentary. The
# clipping page renders this under a "## My commentary" heading when present.
CLIPPING_COMMENT_FIELD: str = os.environ.get("NOTES_CLIPPING_COMMENT_FIELD", "comment")
# How many characters of the mirrored clipping body to show on the published
# /consuming page before truncating to a fade-out preview + "read the full
# version at the source" call-to-action. Bodies at or under this length are
# shown in full (they're already complete). See publisher.publish._build_consuming_md.
CLIPPING_PREVIEW_CHARS: int = int(os.environ.get("NOTES_CLIPPING_PREVIEW_CHARS", "1000"))

# --- repo-local paths -------------------------------------------------------
DRAFTS_DIR: Path = NP_ROOT / "drafts"
PUBLISHED_LEDGER: Path = NP_ROOT / ".published.json"
LOG_DIR: Path = NP_ROOT / ".logs"

# --- audio ------------------------------------------------------------------
SAMPLE_RATE: int = int(os.environ.get("NOTES_SAMPLE_RATE", "16000"))
CHANNELS: int = int(os.environ.get("NOTES_CHANNELS", "1"))
AUDIO_CODEC: str = "flac"
AUDIO_SUBTYPE: str = "PCM_16"  # for soundfile

# --- hotkey -----------------------------------------------------------------
# pynput `GlobalHotKeys` combo string. `<cmd>` is the Windows / Super key.
HOTKEY_COMBO_PYNPUT: str = os.environ.get("NOTES_HOTKEY", "<cmd>+<alt>+<space>")
HOTKEY_LABEL: str = os.environ.get("NOTES_HOTKEY_LABEL", "Win+Alt+Space")

# --- LLM --------------------------------------------------------------------
GEMINI_API_KEY: str | None = os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL_PRIMARY: str = os.environ.get(
    "GEMINI_MODEL_PRIMARY", "gemini-3-flash-preview"
)
GEMINI_MODEL_FALLBACK: str = os.environ.get(
    "GEMINI_MODEL_FALLBACK", "gemini-2.5-flash"
)

# --- misc -------------------------------------------------------------------
HOSTNAME: str = socket.gethostname()
PIPELINE_VERSION: str = "1.0"
WATCHER_POLL_SECONDS: int = int(os.environ.get("NOTES_WATCHER_POLL_SECONDS", "60"))

# --- ensure dirs exist ------------------------------------------------------
# Always create repo-local working dirs. Only create vault-relative dirs when
# the vault root actually exists, so importing this module on a fresh clone
# (or in CI / during tests) doesn't create a phantom vault on disk.
for _d in (DRAFTS_DIR, LOG_DIR):
    _d.mkdir(parents=True, exist_ok=True)

if VAULT_ROOT.exists():
    for _d in (TRANSCRIPT_DIR, DAILY_DIR):
        _d.mkdir(parents=True, exist_ok=True)
