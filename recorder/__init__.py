"""Recorder package: tray app + audio capture for notes-pipeline.

Exposes the :class:`AudioRecorder` and the :data:`RecordingResult` TypedDict so
other components of the pipeline can consume recording metadata.
"""

from __future__ import annotations

from recorder.recorder import AudioRecorder, RecordingResult

__all__ = ["AudioRecorder", "RecordingResult"]
