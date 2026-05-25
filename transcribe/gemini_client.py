"""Thin wrapper over the `google-genai` SDK.

Handles:
  * Singleton client construction (keyed off `config.GEMINI_API_KEY`).
  * Model resolution: tries `GEMINI_MODEL_PRIMARY`, falls back to
    `GEMINI_MODEL_FALLBACK` if the primary model id isn't listed by the API.
  * Audio upload via `client.files.upload`.
  * `generate_content` wrapper with simple retry on transient errors.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from google import genai
from google.genai import types as genai_types

import config

logger = logging.getLogger(__name__)

# --- module-level singletons ---
_client: genai.Client | None = None
_resolved_model: str | None = None

# --- error text used by both the runtime and tests ---
EXPIRED_KEY_ERROR = (
    "GEMINI_API_KEY missing/expired — renew at https://aistudio.google.com/apikey"
)


class RateLimitError(RuntimeError):
    """Raised when Gemini reports a rate-limit / quota / 429 error.

    These errors are NOT retried internally by ``generate()`` — burning more
    quota on a backoff loop is counterproductive. The outer pipeline retry
    treats this as non-retryable and surfaces it to the user immediately so
    they can re-trigger the recording later (after the rate-limit window).
    """


# Sentinels for classifying auth errors from the SDK.
_AUTH_ERROR_MARKERS = (
    "API key not valid",
    "API_KEY_INVALID",
    "invalid api key",
    "expired",
    "unauthenticated",
    "permission denied",
    "401",
    "403",
)

# Sentinels for classifying rate-limit / quota errors from the SDK. Match is
# case-insensitive substring on str(exception).
_RATE_LIMIT_MARKERS = (
    "429",
    "RESOURCE_EXHAUSTED",
    "quota",
    "rate limit",
)


def is_auth_error(err: BaseException) -> bool:
    """Heuristic: does this exception look like an expired/invalid API key?"""
    msg = str(err).lower()
    return any(marker.lower() in msg for marker in _AUTH_ERROR_MARKERS)


def is_rate_limit_error(err: BaseException) -> bool:
    """Heuristic: does this exception look like a rate-limit / quota error?

    Matches case-insensitive substrings of ``str(err)``: ``"429"``,
    ``"RESOURCE_EXHAUSTED"``, ``"quota"``, ``"rate limit"``. Also returns True
    for any :class:`RateLimitError` instance.
    """
    if isinstance(err, RateLimitError):
        return True
    msg = str(err).lower()
    return any(marker.lower() in msg for marker in _RATE_LIMIT_MARKERS)


# Backwards-compatible alias for the old private name.
_looks_like_auth_error = is_auth_error


def get_client() -> genai.Client:
    """Return a cached `genai.Client` built from `config.GEMINI_API_KEY`.

    Raises:
        RuntimeError: if the API key is missing. (We can't cheaply detect an
            expired key here without doing an API call — downstream code will
            convert auth-looking errors into the same `RuntimeError`.)
    """
    global _client
    if _client is not None:
        return _client

    key = config.GEMINI_API_KEY
    if not key:
        raise RuntimeError(EXPIRED_KEY_ERROR)

    try:
        _client = genai.Client(api_key=key)
    except Exception as exc:  # noqa: BLE001
        if is_auth_error(exc):
            raise RuntimeError(EXPIRED_KEY_ERROR) from exc
        raise
    return _client


def _strip_models_prefix(name: str) -> str:
    """`client.models.list()` returns names like `models/gemini-2.5-flash`."""
    return name[len("models/") :] if name.startswith("models/") else name


def resolve_model() -> str:
    """Return the model id to use, cached module-level.

    Tries `config.GEMINI_MODEL_PRIMARY`. If it isn't in the account's model
    list, logs a warning and falls back to `config.GEMINI_MODEL_FALLBACK`.
    Match is a case-insensitive substring check on the model name (since the
    API may prefix with `models/` and append version suffixes).
    """
    global _resolved_model
    if _resolved_model is not None:
        return _resolved_model

    primary = config.GEMINI_MODEL_PRIMARY
    fallback = config.GEMINI_MODEL_FALLBACK

    try:
        client = get_client()
        available: list[str] = []
        for m in client.models.list():
            name = getattr(m, "name", "") or ""
            available.append(_strip_models_prefix(name))
    except RuntimeError:
        raise
    except Exception as exc:  # noqa: BLE001
        if is_auth_error(exc):
            raise RuntimeError(EXPIRED_KEY_ERROR) from exc
        logger.warning(
            "Could not list models (%s); assuming primary=%r is available.",
            exc,
            primary,
        )
        _resolved_model = primary
        return primary

    primary_lc = primary.lower()
    if any(primary_lc in a.lower() for a in available):
        logger.info("Resolved Gemini model: %s (primary)", primary)
        _resolved_model = primary
        return primary

    logger.warning(
        "Primary model %r not in account model list — falling back to %r.",
        primary,
        fallback,
    )
    _resolved_model = fallback
    return fallback


def upload_audio(path: Path) -> Any:
    """Upload an audio file and return the `File` handle.

    Raises:
        FileNotFoundError: if `path` doesn't exist.
        RuntimeError: on auth errors (expired/invalid key).
    """
    if not path.exists():
        raise FileNotFoundError(f"Audio file not found: {path}")

    client = get_client()
    try:
        handle = client.files.upload(file=str(path))
    except Exception as exc:  # noqa: BLE001
        if is_auth_error(exc):
            raise RuntimeError(EXPIRED_KEY_ERROR) from exc
        if is_rate_limit_error(exc):
            raise RateLimitError(str(exc)) from exc
        raise
    logger.debug("Uploaded audio %s -> %s", path, getattr(handle, "name", "?"))
    return handle


def delete_uploaded(handle: Any) -> None:
    """Best-effort delete of an uploaded file. Never raises."""
    name = getattr(handle, "name", None)
    if not name:
        return
    try:
        client = get_client()
        client.files.delete(name=name)
        logger.debug("Deleted uploaded file: %s", name)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to delete uploaded file %s: %s", name, exc)


def generate(
    model: str,
    parts: list[Any],
    *,
    config_obj: genai_types.GenerateContentConfig | None = None,
    attempts: int = 2,
    backoff_sec: float = 1.5,
) -> str:
    """Invoke `generate_content` with a tiny retry loop.

    Args:
        model: Gemini model id.
        parts: list of content parts (strings, File handles, etc.).
        config_obj: optional `GenerateContentConfig` (e.g. for structured output).
        attempts: total attempts (default 2 — one initial, one retry).
        backoff_sec: seconds to sleep between attempts.

    Returns:
        The `.text` attribute of the response.
    """
    client = get_client()
    last_exc: BaseException | None = None

    for attempt in range(1, attempts + 1):
        try:
            kwargs: dict[str, Any] = {"model": model, "contents": parts}
            if config_obj is not None:
                kwargs["config"] = config_obj
            resp = client.models.generate_content(**kwargs)
            text = getattr(resp, "text", None)
            if text is None:
                # Some model responses stash text on candidates; be defensive.
                candidates = getattr(resp, "candidates", None) or []
                for c in candidates:
                    content = getattr(c, "content", None)
                    if content is None:
                        continue
                    for p in getattr(content, "parts", []) or []:
                        t = getattr(p, "text", None)
                        if t:
                            return t
                raise RuntimeError("Gemini response had no text")
            return text
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if is_auth_error(exc):
                raise RuntimeError(EXPIRED_KEY_ERROR) from exc
            # Rate-limit errors short-circuit the internal retry: burning more
            # quota on a backoff loop just makes things worse. The outer
            # pipeline retry also treats RateLimitError as non-retryable.
            if is_rate_limit_error(exc):
                logger.warning(
                    "Gemini generate hit rate limit (%s); not retrying internally",
                    exc,
                )
                raise RateLimitError(str(exc)) from exc
            if attempt >= attempts:
                break
            logger.warning(
                "Gemini generate attempt %d/%d failed (%s); retrying in %.1fs",
                attempt,
                attempts,
                exc,
                backoff_sec,
            )
            time.sleep(backoff_sec)

    assert last_exc is not None
    raise last_exc


# --- test hook ---
def _reset_for_tests() -> None:
    """Clear cached client + model. Used by tests only."""
    global _client, _resolved_model
    _client = None
    _resolved_model = None
