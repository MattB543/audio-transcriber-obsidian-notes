"""Tests for the transcribe/* module with a mocked Gemini client.

These tests avoid any real network / API calls. We patch the module-level
helpers exposed by `transcribe.gemini_client` so that the orchestration code
in `transcribe.transcribe` runs against canned responses.
"""

from __future__ import annotations

import json
import re
import sys
import types
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

# Make sure the repo root is on sys.path so `transcribe.*` imports work when
# running `pytest` from anywhere.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from transcribe import gemini_client, transcribe as transcribe_mod  # noqa: E402


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


RAW_SAMPLE = (
    "Um, so I was, I was thinking about, like, the new project and uh, "
    "I-I think we should ship it on Friday."
)
CLEANED_SAMPLE = (
    "So I was thinking about the new project and I think we should ship it on Friday."
)


@pytest.fixture(autouse=True)
def _reset_client_cache():
    """Clear module-level caches between tests."""
    gemini_client._reset_for_tests()
    yield
    gemini_client._reset_for_tests()


@pytest.fixture
def fake_audio_file(tmp_path: Path) -> Path:
    """Create a minimal real FLAC file so `soundfile.info` succeeds."""
    import numpy as np
    import soundfile as sf

    p = tmp_path / "2026-04-24_143208.flac"
    # 0.5 seconds of silence @ 16 kHz mono.
    data = np.zeros(int(0.5 * 16_000), dtype="int16")
    sf.write(str(p), data, 16_000, subtype="PCM_16", format="FLAC")
    return p


class _FakeHandle:
    """Stand-in for a `google.genai` File handle."""

    def __init__(self, name: str = "files/fake-upload-123") -> None:
        self.name = name


def _install_fake_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    verbatim: str = RAW_SAMPLE,
    metadata: dict[str, Any] | str | None = None,
    available_models: list[str] | None = None,
    upload_raises: BaseException | None = None,
    generate_side_effect: Any = None,
) -> dict[str, Any]:
    """Monkeypatch the helpers used by `transcribe_audio`.

    Returns a dict you can inspect afterward to see what was called with what.
    """
    state: dict[str, Any] = {
        "uploaded": [],
        "deleted": [],
        "generate_calls": [],
    }

    if metadata is None:
        metadata = {
            "cleaned": CLEANED_SAMPLE,
            "title": "Ship New Project Friday",
            "tags": ["project", "shipping"],
        }
    metadata_text = (
        metadata if isinstance(metadata, str) else json.dumps(metadata)
    )

    # `resolve_model`: either use the real one (with a fake model list) or
    # just pin a value. We'll simulate via a fake client.models.list.
    if available_models is None:
        available_models = ["models/gemini-3-flash-preview", "models/gemini-2.5-flash"]

    fake_models = [types.SimpleNamespace(name=n) for n in available_models]

    fake_client = types.SimpleNamespace(
        models=types.SimpleNamespace(list=lambda: iter(fake_models)),
        files=types.SimpleNamespace(
            upload=lambda file: state["uploaded"].append(file) or _FakeHandle(),
            delete=lambda name: state["deleted"].append(name),
        ),
    )

    def _get_client() -> Any:
        return fake_client

    monkeypatch.setattr(gemini_client, "get_client", _get_client)

    def _upload(path: Path) -> Any:
        if upload_raises is not None:
            raise upload_raises
        state["uploaded"].append(str(path))
        return _FakeHandle()

    monkeypatch.setattr(gemini_client, "upload_audio", _upload)
    monkeypatch.setattr(
        gemini_client, "delete_uploaded", lambda h: state["deleted"].append(h.name)
    )

    # The `generate` helper: first call returns verbatim, second returns metadata.
    if generate_side_effect is None:
        replies = [verbatim, metadata_text]

        def _generate(model: str, parts: list[Any], **kwargs: Any) -> str:
            state["generate_calls"].append(
                {"model": model, "parts": parts, "kwargs": kwargs}
            )
            return replies[min(len(state["generate_calls"]) - 1, len(replies) - 1)]

        monkeypatch.setattr(gemini_client, "generate", _generate)
    else:
        monkeypatch.setattr(gemini_client, "generate", generate_side_effect)

    return state


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------


def test_transcribe_audio_returns_all_required_keys(
    monkeypatch: pytest.MonkeyPatch, fake_audio_file: Path
) -> None:
    _install_fake_client(monkeypatch)
    result = transcribe_mod.transcribe_audio(fake_audio_file)

    for key in (
        "raw",
        "cleaned",
        "slug",
        "title",
        "tags",
        "duration_sec",
        "model_used",
    ):
        assert key in result, f"missing key: {key}"

    # Summary was removed; verify it's NOT in the result.
    assert "summary" not in result

    assert isinstance(result["tags"], list)
    assert isinstance(result["duration_sec"], float)
    assert result["duration_sec"] > 0  # probed from the real FLAC


def test_verbatim_transcript_preserved_in_raw(
    monkeypatch: pytest.MonkeyPatch, fake_audio_file: Path
) -> None:
    _install_fake_client(monkeypatch)
    result = transcribe_mod.transcribe_audio(fake_audio_file)

    # Every filler word from RAW_SAMPLE must remain in `raw`.
    assert "Um" in result["raw"]
    assert "uh" in result["raw"]
    assert "like" in result["raw"]
    assert "I-I" in result["raw"]
    assert result["raw"] == RAW_SAMPLE


def test_cleanup_prompt_removes_um(
    monkeypatch: pytest.MonkeyPatch, fake_audio_file: Path
) -> None:
    _install_fake_client(monkeypatch)
    result = transcribe_mod.transcribe_audio(fake_audio_file)

    # The mocked cleanup response should have dropped "um" / "uh" / "like".
    assert " um " not in (" " + result["cleaned"].lower() + " ")
    assert " uh " not in (" " + result["cleaned"].lower() + " ")
    assert "I-I" not in result["cleaned"]
    # And the cleaned text must mention the content words.
    assert "project" in result["cleaned"].lower()


def test_slug_title_tags_formatting(
    monkeypatch: pytest.MonkeyPatch, fake_audio_file: Path
) -> None:
    # Title comes from the LLM (used for headline + filename); slug is
    # derived locally from title. Tags are normalized.
    _install_fake_client(
        monkeypatch,
        metadata={
            "cleaned": CLEANED_SAMPLE,
            "title": "ship new project friday",  # lowercase -> normalized to Title Case
            "tags": ["#Project", "  Shipping  ", "Project"],  # dup + leading '#'
        },
    )
    result = transcribe_mod.transcribe_audio(fake_audio_file)

    # Slug: kebab-case derived from title, lowercase, no spaces or punctuation.
    assert re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", result["slug"]), result["slug"]
    assert result["slug"] == "ship-new-project-friday"

    # Title: starts with uppercase words, no trailing whitespace, <=60 chars.
    assert result["title"][0].isupper()
    assert len(result["title"]) <= 60
    assert result["title"] != result["title"].lower()

    # Tags: lowercase, no '#', deduped, 2-4 entries.
    assert all(t == t.lower() for t in result["tags"])
    assert all(not t.startswith("#") for t in result["tags"])
    assert len(result["tags"]) == len(set(result["tags"]))
    assert 1 <= len(result["tags"]) <= 4


def test_falls_back_to_secondary_model_when_primary_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Only the fallback is "available".
    fake_models = [types.SimpleNamespace(name="models/gemini-2.5-flash")]
    fake_client = types.SimpleNamespace(
        models=types.SimpleNamespace(list=lambda: iter(fake_models)),
        files=types.SimpleNamespace(
            upload=lambda file: _FakeHandle(), delete=lambda name: None
        ),
    )
    monkeypatch.setattr(gemini_client, "get_client", lambda: fake_client)
    # Ensure the api key looks present so get_client-internal check wouldn't fire.
    import config

    monkeypatch.setattr(config, "GEMINI_API_KEY", "fake-key-for-tests")

    model = gemini_client.resolve_model()
    assert model == "gemini-2.5-flash"


def test_primary_model_selected_when_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_models = [
        types.SimpleNamespace(name="models/gemini-3-flash-preview"),
        types.SimpleNamespace(name="models/gemini-2.5-flash"),
    ]
    fake_client = types.SimpleNamespace(
        models=types.SimpleNamespace(list=lambda: iter(fake_models)),
        files=types.SimpleNamespace(
            upload=lambda file: _FakeHandle(), delete=lambda name: None
        ),
    )
    monkeypatch.setattr(gemini_client, "get_client", lambda: fake_client)
    import config

    monkeypatch.setattr(config, "GEMINI_API_KEY", "fake-key-for-tests")

    model = gemini_client.resolve_model()
    assert model == "gemini-3-flash-preview"


def test_expired_or_missing_key_raises_clear_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Simulate missing key.
    import config

    monkeypatch.setattr(config, "GEMINI_API_KEY", None)

    with pytest.raises(RuntimeError) as excinfo:
        gemini_client.get_client()

    msg = str(excinfo.value)
    assert "GEMINI_API_KEY" in msg
    assert "renew" in msg.lower()
    assert "aistudio.google.com" in msg


def test_expired_key_error_surfaced_from_auth_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the SDK raises an API-key error mid-call, the real `generate` wrapper
    re-raises it as `RuntimeError` with the canonical renew message.
    """
    import config

    monkeypatch.setattr(config, "GEMINI_API_KEY", "fake-key-for-tests")

    def _bad_generate_content(**kwargs: Any) -> Any:
        raise Exception(
            "API_KEY_INVALID: API key not valid. Please pass a valid API key."
        )

    fake_client = types.SimpleNamespace(
        models=types.SimpleNamespace(
            list=lambda: iter([]),
            generate_content=_bad_generate_content,
        ),
        files=types.SimpleNamespace(
            upload=lambda file: _FakeHandle(),
            delete=lambda name: None,
        ),
    )
    monkeypatch.setattr(gemini_client, "get_client", lambda: fake_client)

    with pytest.raises(RuntimeError) as excinfo:
        gemini_client.generate("gemini-2.5-flash", ["hi"])

    msg = str(excinfo.value).lower()
    assert "renew" in msg
    assert "aistudio.google.com" in msg


def test_cleanup_transcript_standalone(monkeypatch: pytest.MonkeyPatch) -> None:
    """`cleanup_transcript` should work without re-uploading audio."""
    _install_fake_client(monkeypatch, verbatim="unused", metadata="unused")

    # Redirect generate to return a cleaned string.
    monkeypatch.setattr(
        gemini_client, "generate", lambda model, parts, **kw: CLEANED_SAMPLE
    )

    from transcribe.cleanup import CLEANUP_PROMPT, cleanup_transcript

    out = cleanup_transcript(RAW_SAMPLE)
    assert "project" in out.lower()
    assert "um" not in out.lower().split()  # no bare-word "um"
    # Sanity: the prompt constant is exported.
    assert "CLEANED version" in CLEANUP_PROMPT


def test_metadata_fallback_on_malformed_json(
    monkeypatch: pytest.MonkeyPatch, fake_audio_file: Path
) -> None:
    """If metadata comes back non-JSON twice, we fall back to heuristics."""
    state = {"n": 0}

    def _gen(model: str, parts: list[Any], **kw: Any) -> str:
        state["n"] += 1
        if state["n"] == 1:
            return RAW_SAMPLE  # verbatim pass
        return "this is definitely not json {{{"  # both metadata attempts

    _install_fake_client(monkeypatch, generate_side_effect=_gen)
    result = transcribe_mod.transcribe_audio(fake_audio_file)

    # Heuristic fallback uses a voice-memo tag and derives slug/title from the cleaned text.
    assert "voice-memo" in result["tags"]
    assert result["slug"]  # non-empty
    assert result["title"]


def test_upload_missing_file_raises_file_not_found(tmp_path: Path) -> None:
    missing = tmp_path / "nope.flac"
    with pytest.raises(FileNotFoundError):
        transcribe_mod.transcribe_audio(missing)


# ---------------------------------------------------------------------------
# reserved trigger tag filtering (Bug 1 fix)
# ---------------------------------------------------------------------------


def test_publish_tag_filtered_from_auto_generated_metadata(
    monkeypatch: pytest.MonkeyPatch, fake_audio_file: Path
) -> None:
    """Gemini must not be able to inject the reserved ``publish`` tag.

    Regression test for the auto-publish bug: when a voice memo is ABOUT
    publishing/deployment, the model may helpfully tag it ``publish``. The
    publisher watcher treats that tag as a user trigger — auto-publishing
    without the user's consent. The normalizer must strip it.
    """
    _install_fake_client(
        monkeypatch,
        metadata={
            "cleaned": CLEANED_SAMPLE,
            "title": "Ship New Project Friday",
            # Multiple variations + a legitimate neighbour tag.
            "tags": ["publish", "Publish", "PUBLISH", "something-else"],
        },
    )
    result = transcribe_mod.transcribe_audio(fake_audio_file)

    # The reserved trigger tag must be stripped in every case form.
    assert "publish" not in result["tags"], result["tags"]
    assert "Publish" not in result["tags"], result["tags"]
    assert "PUBLISH" not in result["tags"], result["tags"]
    # Legitimate content tags survive.
    assert "something-else" in result["tags"]


def test_fallback_metadata_does_not_generate_publish_tag() -> None:
    """The heuristic fallback must never emit the reserved ``publish`` tag.

    The fallback always tags ``voice-memo``; this guards against a future
    refactor that derives tags from the transcript content.
    """
    meta = transcribe_mod._fallback_metadata(
        "We should publish this release before Friday."
    )
    assert "publish" not in meta.tags
    assert "voice-memo" in meta.tags


def test_reserved_trigger_tags_constant_contains_publish() -> None:
    """Sanity check: the reserved-tags constant is exported and contains ``publish``."""
    assert "publish" in transcribe_mod.RESERVED_TRIGGER_TAGS


# ---------------------------------------------------------------------------
# rate-limit detection (Bug B fix)
# ---------------------------------------------------------------------------


def test_rate_limit_raises_immediately(monkeypatch: pytest.MonkeyPatch) -> None:
    """A RESOURCE_EXHAUSTED error must NOT trigger the inner 2-attempt retry.

    `generate()` sees a rate-limit error and re-raises it as `RateLimitError`
    after exactly one call to `client.models.generate_content`.
    """
    import config

    monkeypatch.setattr(config, "GEMINI_API_KEY", "fake-key-for-tests")

    call_count = {"n": 0}

    def _rate_limited_generate_content(**kwargs: Any) -> Any:
        call_count["n"] += 1
        raise Exception(
            "RESOURCE_EXHAUSTED: Quota exceeded for project 'foo' "
            "(quota metric requests-per-minute)."
        )

    fake_client = types.SimpleNamespace(
        models=types.SimpleNamespace(
            list=lambda: iter([]),
            generate_content=_rate_limited_generate_content,
        ),
        files=types.SimpleNamespace(
            upload=lambda file: _FakeHandle(),
            delete=lambda name: None,
        ),
    )
    monkeypatch.setattr(gemini_client, "get_client", lambda: fake_client)

    sleeps: list[float] = []
    monkeypatch.setattr(gemini_client.time, "sleep", lambda s: sleeps.append(s))

    with pytest.raises(gemini_client.RateLimitError) as excinfo:
        gemini_client.generate("gemini-2.5-flash", ["hi"])

    # Exactly one call to the SDK — internal retry was short-circuited.
    assert call_count["n"] == 1
    # No sleeps happened (the inner retry would have slept 1.5s before retrying).
    assert sleeps == []
    # The error message preserves the original detail.
    assert "RESOURCE_EXHAUSTED" in str(excinfo.value)


def test_429_in_error_message_treated_as_rate_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bare ``429`` substring in the error should also short-circuit."""
    import config

    monkeypatch.setattr(config, "GEMINI_API_KEY", "fake-key-for-tests")

    call_count = {"n": 0}

    def _rate_limited_generate_content(**kwargs: Any) -> Any:
        call_count["n"] += 1
        raise Exception("HTTP 429 Too Many Requests")

    fake_client = types.SimpleNamespace(
        models=types.SimpleNamespace(
            list=lambda: iter([]),
            generate_content=_rate_limited_generate_content,
        ),
        files=types.SimpleNamespace(
            upload=lambda file: _FakeHandle(),
            delete=lambda name: None,
        ),
    )
    monkeypatch.setattr(gemini_client, "get_client", lambda: fake_client)
    monkeypatch.setattr(gemini_client.time, "sleep", lambda s: None)

    with pytest.raises(gemini_client.RateLimitError):
        gemini_client.generate("gemini-2.5-flash", ["hi"])

    assert call_count["n"] == 1


def test_is_rate_limit_error_helper() -> None:
    """`is_rate_limit_error` should detect all documented markers."""
    assert gemini_client.is_rate_limit_error(Exception("got 429 from server"))
    assert gemini_client.is_rate_limit_error(Exception("RESOURCE_EXHAUSTED"))
    assert gemini_client.is_rate_limit_error(Exception("daily quota exceeded"))
    assert gemini_client.is_rate_limit_error(Exception("Rate Limit Hit"))
    assert gemini_client.is_rate_limit_error(gemini_client.RateLimitError("boom"))

    # Negative cases.
    assert not gemini_client.is_rate_limit_error(Exception("transient 503"))
    assert not gemini_client.is_rate_limit_error(Exception("API_KEY_INVALID"))


def test_is_auth_error_helper() -> None:
    """`is_auth_error` should detect auth/key markers (consolidated logic)."""
    assert gemini_client.is_auth_error(Exception("API_KEY_INVALID"))
    assert gemini_client.is_auth_error(Exception("API key not valid"))
    assert gemini_client.is_auth_error(Exception("HTTP 401 unauthorized"))
    assert gemini_client.is_auth_error(Exception("permission denied"))
    assert gemini_client.is_auth_error(Exception("expired"))

    # Rate-limit errors are NOT auth errors.
    assert not gemini_client.is_auth_error(Exception("429 rate limit"))
    assert not gemini_client.is_auth_error(Exception("RESOURCE_EXHAUSTED"))


def test_transient_error_still_uses_internal_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-rate-limit, non-auth transient error should still trigger 1 retry."""
    import config

    monkeypatch.setattr(config, "GEMINI_API_KEY", "fake-key-for-tests")

    call_count = {"n": 0}

    class _OkResp:
        text = "ok"

    def _flaky_generate_content(**kwargs: Any) -> Any:
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise Exception("HTTP 503 transient backend error")
        return _OkResp()

    fake_client = types.SimpleNamespace(
        models=types.SimpleNamespace(
            list=lambda: iter([]),
            generate_content=_flaky_generate_content,
        ),
        files=types.SimpleNamespace(
            upload=lambda file: _FakeHandle(),
            delete=lambda name: None,
        ),
    )
    monkeypatch.setattr(gemini_client, "get_client", lambda: fake_client)
    monkeypatch.setattr(gemini_client.time, "sleep", lambda s: None)

    out = gemini_client.generate("gemini-2.5-flash", ["hi"])
    assert out == "ok"
    # Two calls = one initial + one retry — internal retry kept for genuine transients.
    assert call_count["n"] == 2
