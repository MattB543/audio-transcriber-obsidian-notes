"""Tiny retry-with-exponential-backoff helper used by the post-recording pipeline.

Kept deliberately simple — no decorators, no jitter, no class hierarchy. The
caller passes a thunk plus a few knobs, we run it, and we sleep ``base_delay *
2**(attempt-1)`` between failed attempts.

Public API:
    retry_with_backoff(func, *, max_attempts, base_delay_sec,
                       retryable_exceptions, non_retryable_predicates,
                       on_attempt_failed, sleep_fn)
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Iterable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

__all__ = ["retry_with_backoff"]


def retry_with_backoff(
    func: Callable[[], T],
    *,
    max_attempts: int = 4,
    base_delay_sec: float = 2.0,
    retryable_exceptions: tuple[type[BaseException], ...] = (Exception,),
    non_retryable_predicates: Iterable[Callable[[BaseException], bool]] = (),
    on_attempt_failed: Callable[[int, BaseException], None] | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> T:
    """Call ``func()`` with exponential-backoff retries on failure.

    Parameters
    ----------
    func:
        Zero-arg callable to run.
    max_attempts:
        Maximum total number of attempts (must be >= 1). For the default of
        4, delays between failures are 2s, 4s, 8s.
    base_delay_sec:
        Base delay in seconds. Delay before retry ``n`` is
        ``base_delay_sec * 2**(n-1)`` (n=1 → first retry).
    retryable_exceptions:
        Tuple of exception classes that should trigger a retry. Anything not
        an instance of one of these classes is re-raised immediately.
    non_retryable_predicates:
        Iterable of callables ``(exc) -> bool``. If any predicate returns
        ``True`` for a caught exception, that exception is re-raised
        immediately without further retries (e.g. auth failures).
    on_attempt_failed:
        Optional callback invoked as ``(attempt_number, exception)`` after
        each failed attempt (BEFORE the sleep / re-raise decision).
    sleep_fn:
        Override for :func:`time.sleep`. Tests can pass a fake.

    Returns
    -------
    T
        The return value of ``func()`` on the first successful attempt.
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")

    predicates = list(non_retryable_predicates)
    last_exc: BaseException | None = None

    for attempt in range(1, max_attempts + 1):
        try:
            return func()
        except retryable_exceptions as exc:
            last_exc = exc
            if on_attempt_failed is not None:
                try:
                    on_attempt_failed(attempt, exc)
                except Exception:  # pragma: no cover — callback errors are non-fatal
                    logger.exception("on_attempt_failed callback raised; continuing")

            # Non-retryable predicates short-circuit immediately.
            for pred in predicates:
                try:
                    if pred(exc):
                        logger.info(
                            "Retry aborted by non-retryable predicate "
                            "(attempt %d/%d): %s",
                            attempt,
                            max_attempts,
                            exc,
                        )
                        raise
                except Exception as pred_exc:
                    if pred_exc is exc:
                        raise
                    logger.exception(
                        "non_retryable_predicate raised; treating as no-match"
                    )

            if attempt >= max_attempts:
                break

            delay = base_delay_sec * (2 ** (attempt - 1))
            logger.info(
                "Retrying after %.1fs (attempt %d/%d failed: %s)",
                delay,
                attempt,
                max_attempts,
                exc,
            )
            sleep_fn(delay)

    # Exhausted retries — re-raise the last exception.
    assert last_exc is not None  # noqa: S101 — invariant: loop ran at least once
    raise last_exc
