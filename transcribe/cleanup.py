"""Cleanup pass: convert a verbatim transcript to a cleaned transcript.

`CLEANUP_PROMPT` is the exact prompt from SPEC.md — do not paraphrase; other
modules import it directly. `cleanup_transcript` applies it via Gemini.
"""

from __future__ import annotations

import logging

from transcribe import gemini_client

logger = logging.getLogger(__name__)


# NOTE: This text is load-bearing and copied verbatim from SPEC.md. If the spec
# changes, update both places. Other modules (e.g. obsidian.migrate_legacy)
# import this constant directly.
CLEANUP_PROMPT = """You will receive a verbatim voice-to-text transcript. Produce a CLEANED version with these strict rules:

REMOVE:
- Filler words used as vocal tics: "um", "uh", "like" (only when used as filler, not meaningful)
- Stutters: "I-I-I was going" → "I was going"
- Word repetitions from mispeaking: "I was, I was saying" → "I was saying"
- Abandoned false starts that were immediately restarted with different words: "I went to— I drove to the store" → "I drove to the store"

ADD:
- Punctuation (commas, periods, question marks, quotation marks)
- Capitalization at sentence starts
- Paragraph breaks at natural topic shifts or long pauses

DO NOT:
- Rephrase or "improve" wording
- Correct casual grammar that is intentional ("gonna", "kinda", "I seen")
- Summarize or shorten
- Reorder sentences or ideas
- Add interpretation, headers, bullet points
- Change vocabulary
- Fix factual errors the speaker made

The output should be the same words the speaker intended, just without disfluencies. Preserve the speaker's exact voice, tone, and meaning.

Return ONLY the cleaned transcript text. No headers, no commentary."""


def cleanup_transcript(raw: str) -> str:
    """Apply `CLEANUP_PROMPT` to `raw` via Gemini and return the cleaned text.

    Standalone — doesn't require re-uploading audio. Usable for migrations or
    any text-only cleanup. Returns the stripped response text.
    """
    if not raw or not raw.strip():
        return ""

    model = gemini_client.resolve_model()
    parts = [CLEANUP_PROMPT, "\n\nTranscript:\n", raw]
    logger.debug("Running cleanup pass with model=%s (%d raw chars)", model, len(raw))
    cleaned = gemini_client.generate(model, parts).strip()
    return cleaned
