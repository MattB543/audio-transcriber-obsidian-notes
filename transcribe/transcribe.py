"""High-level transcription orchestration.

Flow for `transcribe_audio(audio_path)`:
    1. Upload the audio to Gemini.
    2. Pass 1: verbatim transcript (keeps every um/uh/stutter).
    3. Pass 2: cleaned transcript + title + tags via JSON (text-only; no reupload).
    4. Derive slug locally from the LLM-generated title (kebab-case it). The
       title is the LLM's job because we need a real publishable headline for
       the personal-site pipeline; the slug is just `sanitize(title)` so file
       name and display name stay in sync.
    5. Probe duration from the FLAC file.
    6. Return a `TranscriptResult` dict.

If metadata generation fails (malformed JSON twice), we fall back to a
heuristic title built from the first words of the cleaned transcript.
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any, TypedDict

import soundfile as sf
from google.genai import types as genai_types
from pydantic import BaseModel, Field, ValidationError

from transcribe import gemini_client
from transcribe.cleanup import CLEANUP_PROMPT

logger = logging.getLogger(__name__)


# Reserved tags that act as user-driven pipeline controls (e.g. `#publish`
# triggers auto-publishing in publisher/watcher.py). Auto-generated metadata
# must NEVER produce these — otherwise Gemini transcribing a memo about
# "publishing" would accidentally trigger the publisher. Keep lowercase
# kebab-case to match post-normalization tag form.
RESERVED_TRIGGER_TAGS = frozenset({"publish"})


# --- public types ---
class TranscriptResult(TypedDict):
    raw: str
    cleaned: str
    slug: str         # kebab-cased first-5-words of `cleaned`
    title: str        # Title Case first-5-words of `cleaned`
    tags: list[str]   # 2-4 content tags from Gemini
    duration_sec: float
    model_used: str


class TranscriptMetadata(BaseModel):
    """Structured metadata produced alongside the cleanup pass.

    LLM is responsible for `cleaned`, `title`, and `tags`. Slug is derived
    locally from `title` (see `_slug_from_title`).
    """

    cleaned: str = Field(..., description="Cleaned transcript text.")
    title: str = Field(..., description="Title Case, <60 chars, human-readable")
    tags: list[str] = Field(
        ..., description="2-4 content tags, lowercase kebab-case, no '#'"
    )


# --- prompts ---
VERBATIM_PROMPT = (
    "Transcribe this audio verbatim. Include every filler word (um, uh, like), "
    "every stutter, every false start, every word-level repetition. Do NOT "
    "clean up or correct anything. Return ONLY the transcript text, no "
    "commentary, no speaker labels unless multiple speakers are clearly distinct."
)

METADATA_INSTRUCTIONS = """You will receive a VERBATIM voice transcript. Produce a JSON object with these three fields:

- cleaned: the transcript with disfluencies removed per the CLEANUP rules below. Preserve the speaker's voice exactly — no rephrasing.
- title: a concise, descriptive title in Title Case, under 60 characters, no trailing punctuation. This may end up as a published article title, so make it real and headline-quality (not just the first words of the transcript).
- tags: 2-4 content-specific tags, each lowercase kebab-case, no '#' prefix.

Return ONLY the JSON object — no markdown fences, no commentary.

CLEANUP RULES (for the `cleaned` field):
""" + CLEANUP_PROMPT


# --- duration probe ---
def _probe_duration_sec(audio_path: Path) -> float:
    try:
        info = sf.info(str(audio_path))
        return float(info.duration)
    except Exception as exc:  # noqa: BLE001
        logger.warning("soundfile.info failed on %s: %s", audio_path, exc)
        return 0.0


# --- slug / title helpers (fallback heuristics) ---
_SLUG_STRIP = re.compile(r"[^a-z0-9\s-]+")
_WS = re.compile(r"\s+")


def _kebab_words(text: str, n_min: int = 3, n_max: int = 5) -> str:
    words = _WS.sub(" ", _SLUG_STRIP.sub("", text.lower())).strip().split(" ")
    words = [w for w in words if w]
    if not words:
        return "voice-memo"
    picked = words[:n_max] if len(words) >= n_max else words
    if len(picked) < n_min:
        picked = (picked + ["note"] * n_min)[:n_min]
    return "-".join(picked)


def _title_case(text: str, max_len: int = 60) -> str:
    cleaned = _WS.sub(" ", text).strip()
    if not cleaned:
        return "Voice Memo"
    titled = " ".join(w.capitalize() if w.islower() else w for w in cleaned.split(" "))
    if len(titled) > max_len:
        titled = titled[: max_len - 1].rstrip() + "…"
    return titled


_SLUG_TITLE_RE = re.compile(r"[^a-z0-9]+")
_SLUG_MAX_LEN = 60


def _slug_from_title(title: str) -> str:
    """Convert an LLM-generated title into a filesystem-safe kebab-case slug.

    Empty / pathological titles fall back to ``voice-memo``. Capped at
    ``_SLUG_MAX_LEN`` chars so we don't blow Windows MAX_PATH on long titles.
    """
    base = (title or "").strip().lower()
    if not base:
        return "voice-memo"
    s = _SLUG_TITLE_RE.sub("-", base).strip("-")
    if not s:
        return "voice-memo"
    if len(s) > _SLUG_MAX_LEN:
        s = s[:_SLUG_MAX_LEN].rstrip("-")
    return s


def _heuristic_title(cleaned: str, n: int = 5) -> str:
    """Title-Case rendering of the first N words of `cleaned`. Used as a
    last-resort fallback when the LLM metadata pass fails."""
    base = (cleaned or "").strip() or "Voice Memo"
    first_words = " ".join(base.split()[:n])
    return _title_case(first_words)


def _fallback_metadata(cleaned: str) -> TranscriptMetadata:
    """Heuristic metadata when the LLM's structured response is unusable.

    The fallback title is derived from the first words of the cleaned
    transcript -- not great, but better than ``Untitled`` for grep/search.
    """
    return TranscriptMetadata(
        cleaned=cleaned,
        title=_heuristic_title(cleaned),
        tags=["voice-memo"],
    )


# --- metadata pass ---
def _strip_json_fences(raw: str) -> str:
    """Remove ```json ... ``` code fences if the model emitted them."""
    s = raw.strip()
    if s.startswith("```"):
        # drop leading fence (optionally labeled)
        s = re.sub(r"^```[a-zA-Z0-9]*\s*\n?", "", s)
        if s.endswith("```"):
            s = s[: -len("```")]
        s = s.strip()
    return s


def _build_metadata_config() -> genai_types.GenerateContentConfig:
    """Ask for JSON object output (no schema — more resilient across models)."""
    return genai_types.GenerateContentConfig(response_mime_type="application/json")


def _parse_metadata(text: str, raw_fallback: str) -> TranscriptMetadata:
    """Parse a JSON metadata blob, validating via Pydantic."""
    try:
        obj = json.loads(_strip_json_fences(text))
    except json.JSONDecodeError as exc:
        raise ValueError(f"metadata JSON parse error: {exc}") from exc

    try:
        return TranscriptMetadata.model_validate(obj)
    except ValidationError as exc:
        raise ValueError(f"metadata shape invalid: {exc}") from exc


def _run_metadata_pass(model: str, raw_transcript: str) -> TranscriptMetadata:
    """Run the cleanup+metadata pass with one retry; fall back on repeated failure."""
    parts = [METADATA_INSTRUCTIONS, "\n\nVerbatim transcript:\n", raw_transcript]
    cfg = _build_metadata_config()

    errors: list[str] = []
    for attempt in (1, 2):
        try:
            text = gemini_client.generate(model, parts, config_obj=cfg)
            meta = _parse_metadata(text, raw_fallback=raw_transcript)
            _normalize_metadata_inplace(meta)
            return meta
        except ValueError as exc:
            errors.append(str(exc))
            logger.warning(
                "Metadata pass attempt %d failed (%s); %s",
                attempt,
                exc,
                "retrying" if attempt == 1 else "falling back to heuristics",
            )

    logger.warning("Metadata fallback engaged. Errors: %s", "; ".join(errors))
    return _fallback_metadata(raw_transcript)


def _normalize_metadata_inplace(meta: TranscriptMetadata) -> None:
    """Normalize title + tags to the documented shapes. Slug is derived from
    the (post-normalization) title in ``transcribe_audio``."""
    # title → Title Case, clamped to 60 chars; fall back if blank
    meta.title = _title_case(meta.title) if (meta.title or "").strip() else _heuristic_title(meta.cleaned)

    # tags → cleaned, lowercase, kebab, no '#', unique, 2-4 entries
    seen: set[str] = set()
    clean_tags: list[str] = []
    for t in meta.tags or []:
        if not isinstance(t, str):
            continue
        tag = t.strip().lstrip("#").lower()
        tag = _SLUG_STRIP.sub("", tag)
        tag = _WS.sub("-", tag).strip("-")
        # Filter reserved trigger tags (e.g. `publish`) AFTER kebab-case
        # normalization so variations like "Publish" / "PUBLISH" are caught.
        if tag in RESERVED_TRIGGER_TAGS:
            logger.debug("Dropping reserved trigger tag from auto metadata: %s", tag)
            continue
        if tag and tag not in seen:
            seen.add(tag)
            clean_tags.append(tag)
    if not clean_tags:
        clean_tags = ["voice-memo"]
    meta.tags = clean_tags[:4]


# --- verbatim pass ---
def _run_verbatim_pass(model: str, audio_handle: Any) -> str:
    parts = [VERBATIM_PROMPT, audio_handle]
    text = gemini_client.generate(model, parts)
    return text.strip()


# --- public entry point ---
def transcribe_audio(audio_path: Path) -> TranscriptResult:
    """Transcribe + clean a FLAC audio file; return the full TranscriptResult.

    Raises:
        FileNotFoundError: if the audio file doesn't exist.
        RuntimeError: if the Gemini API key is missing or expired.
    """
    audio_path = Path(audio_path)
    if not audio_path.exists():
        raise FileNotFoundError(f"Audio file not found: {audio_path}")

    file_size = audio_path.stat().st_size
    audio_duration = _probe_duration_sec(audio_path)
    model = gemini_client.resolve_model()
    logger.info(
        "[transcribe] START %s (%.1fs audio, %d bytes) model=%s",
        audio_path.name, audio_duration, file_size, model,
    )

    t_total = time.monotonic()
    logger.info("[transcribe] uploading audio to Gemini ...")
    t = time.monotonic()
    handle = gemini_client.upload_audio(audio_path)
    logger.info("[transcribe] upload complete in %.2fs (handle=%s)",
                time.monotonic() - t, getattr(handle, "name", "?"))
    try:
        # Pass 1: verbatim (keeps every um/uh)
        logger.info("[transcribe] PASS 1 (verbatim) starting ...")
        t = time.monotonic()
        raw = _run_verbatim_pass(model, handle)
        logger.info(
            "[transcribe] PASS 1 done in %.2fs (raw=%d chars / %d words)",
            time.monotonic() - t, len(raw), len(raw.split()),
        )

        # Pass 2: cleanup + metadata (text-only; no re-upload)
        logger.info("[transcribe] PASS 2 (cleanup + title + tags) starting ...")
        t = time.monotonic()
        meta = _run_metadata_pass(model, raw)
        logger.info(
            "[transcribe] PASS 2 done in %.2fs (cleaned=%d chars, title=%r, tags=%s)",
            time.monotonic() - t, len(meta.cleaned), meta.title, meta.tags,
        )
    finally:
        try:
            gemini_client.delete_uploaded(handle)
            logger.debug("[transcribe] deleted uploaded file from Gemini")
        except Exception:  # noqa: BLE001
            logger.warning("[transcribe] failed to delete uploaded file from Gemini",
                           exc_info=True)

    cleaned = meta.cleaned.strip()
    slug = _slug_from_title(meta.title)
    logger.info(
        "[transcribe] DONE in %.2fs total. title=%r slug=%r tags=%s",
        time.monotonic() - t_total, meta.title, slug, meta.tags,
    )

    return TranscriptResult(
        raw=raw,
        cleaned=cleaned,
        # Title comes from the LLM (publishable headline). Slug is derived
        # locally from the title so file name and display name stay in sync.
        title=meta.title,
        slug=slug,
        tags=meta.tags,
        duration_sec=audio_duration,
        model_used=model,
    )
