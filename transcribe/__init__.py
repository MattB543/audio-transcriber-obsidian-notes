"""Transcription + cleanup module for the notes-pipeline.

Public API:
    transcribe_audio(audio_path: Path) -> TranscriptResult
    cleanup_transcript(raw: str) -> str
    CLEANUP_PROMPT
"""

from __future__ import annotations

from transcribe.cleanup import CLEANUP_PROMPT, cleanup_transcript
from transcribe.transcribe import TranscriptResult, transcribe_audio

__all__ = [
    "CLEANUP_PROMPT",
    "TranscriptResult",
    "cleanup_transcript",
    "transcribe_audio",
]
