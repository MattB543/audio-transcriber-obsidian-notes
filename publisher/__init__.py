"""Publisher: polls for tagged transcripts and publishes them to personal_site.

Public API (import from the submodules):
    from publisher.publish import publish_note, PublishResult
    from publisher.watcher import (
        PublishedLedger, scan_for_publishable, run_once, run_forever,
    )

This ``__init__`` deliberately re-exports *lazily* via ``__getattr__`` so that
running ``python -m publisher.watcher`` does not double-import ``watcher``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

__all__ = [
    "PublishResult",
    "PublishedLedger",
    "publish_note",
    "run_forever",
    "run_once",
    "scan_for_publishable",
]

if TYPE_CHECKING:  # pragma: no cover
    from publisher.publish import (  # noqa: F401
        PublishResult,
        publish_note,
    )
    from publisher.watcher import (  # noqa: F401
        PublishedLedger,
        run_forever,
        run_once,
        scan_for_publishable,
    )


_PUBLISH_EXPORTS = {"PublishResult", "publish_note"}
_WATCHER_EXPORTS = {
    "PublishedLedger",
    "run_forever",
    "run_once",
    "scan_for_publishable",
}


def __getattr__(name: str) -> Any:
    if name in _PUBLISH_EXPORTS:
        from publisher import publish as _m
        return getattr(_m, name)
    if name in _WATCHER_EXPORTS:
        from publisher import watcher as _m
        return getattr(_m, name)
    raise AttributeError(f"module 'publisher' has no attribute {name!r}")
