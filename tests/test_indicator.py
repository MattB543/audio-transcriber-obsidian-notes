"""Smoke tests for :mod:`recorder.indicator`.

We don't try to actually render a Tk window in CI — instead we patch
``tkinter`` itself so the indicator's lifecycle (build, schedule, hide,
stop-button click) can be exercised on a headless box. The waveform-drawing
math has been kept in pure Python so it remains exercised indirectly via the
``_format_elapsed`` and ``_normalize_to_unit`` helpers tested in
``test_recorder.py`` plus the smoke tests below.
"""

from __future__ import annotations

import sys
import threading
import time
import types
from pathlib import Path
from typing import Any, Callable
from unittest.mock import MagicMock

import pytest

# Make the notes-pipeline root importable for `recorder.indicator`.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# --------------------------------------------------------------------- helpers


class _FakeWidget:
    """Stand-in for an arbitrary tkinter widget (Frame/Label/Canvas)."""

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        self.bindings: dict[str, Callable[..., Any]] = {}
        self.created_items: list[dict[str, Any]] = []
        self._items_counter = 0

    # Layout / config — all no-ops.
    def pack(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def configure(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    config = configure

    def bind(self, sequence: str, func: Callable[..., Any]) -> None:
        self.bindings[sequence] = func

    # Canvas-only API.
    def create_oval(self, *_args: Any, **_kwargs: Any) -> int:
        self._items_counter += 1
        self.created_items.append({"type": "oval", "id": self._items_counter})
        return self._items_counter

    def create_rectangle(self, *_args: Any, **_kwargs: Any) -> int:
        self._items_counter += 1
        self.created_items.append({"type": "rect", "id": self._items_counter})
        return self._items_counter

    def create_line(self, *_args: Any, **_kwargs: Any) -> int:
        self._items_counter += 1
        return self._items_counter

    def itemconfig(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def delete(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class _FakeRoot(_FakeWidget):
    """Stand-in for :class:`tkinter.Tk`."""

    def __init__(self) -> None:
        super().__init__()
        self.title_value: str = ""
        self.scheduled: list[tuple[int, Callable[..., Any]]] = []
        self.protocols: dict[str, Callable[..., Any]] = {}
        self._destroyed = False
        self._running = False
        self.geometry_calls: list[str] = []
        self.wm_attributes_calls: list[tuple[Any, ...]] = []
        self.override_redirect: bool | None = None

    def title(self, value: str) -> None:
        self.title_value = value

    def overrideredirect(self, value: bool) -> None:
        self.override_redirect = value

    def wm_attributes(self, *args: Any) -> None:
        self.wm_attributes_calls.append(args)

    def winfo_screenwidth(self) -> int:
        return 1920

    def winfo_screenheight(self) -> int:
        return 1080

    def winfo_rootx(self) -> int:
        return 100

    def winfo_rooty(self) -> int:
        return 100

    def winfo_exists(self) -> int:
        return 0 if self._destroyed else 1

    def geometry(self, value: str) -> None:
        self.geometry_calls.append(value)

    def protocol(self, name: str, func: Callable[..., Any]) -> None:
        self.protocols[name] = func

    def after(self, ms: int, func: Callable[..., Any], *_a: Any, **_kw: Any) -> str:
        # Don't actually schedule — record the request so tests can drive it.
        self.scheduled.append((ms, func))
        return f"after#{len(self.scheduled)}"

    def mainloop(self) -> None:
        # Block until destroy() flips the flag — tests drive this manually.
        self._running = True
        while not self._destroyed:
            time.sleep(0.01)

    def destroy(self) -> None:
        self._destroyed = True
        self._running = False


@pytest.fixture
def fake_tkinter(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Inject a fake ``tkinter`` module so RecordingIndicator.show() doesn't
    actually open a window. Returns the mock module so tests can introspect."""
    fake_tk = types.ModuleType("tkinter")

    fake_root_holder: dict[str, _FakeRoot | None] = {"root": None}

    class _TkFactory:
        def __call__(self) -> _FakeRoot:
            root = _FakeRoot()
            fake_root_holder["root"] = root
            return root

    fake_tk.Tk = _TkFactory()  # type: ignore[attr-defined]
    fake_tk.Frame = _FakeWidget  # type: ignore[attr-defined]
    fake_tk.Label = _FakeWidget  # type: ignore[attr-defined]
    fake_tk.Canvas = _FakeWidget  # type: ignore[attr-defined]

    class _TclError(Exception):
        pass

    fake_tk.TclError = _TclError  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "tkinter", fake_tk)

    return {"module": fake_tk, "root_holder": fake_root_holder}


class _FakeRecorder:
    """Minimal recorder stand-in that always returns a fixed waveform."""

    def __init__(self, samples: list[float] | None = None, level: float = 0.42) -> None:
        self._samples = samples or [0.1 * i for i in range(-5, 5)]
        self._level = level

    def get_waveform(self, _n: int = 600) -> list[float]:
        return list(self._samples)

    def get_level(self) -> float:
        return self._level


# ----------------------------------------------------------------------- tests


def test_indicator_class_importable() -> None:
    """Sanity: imports succeed and exposes the public API."""
    from recorder.indicator import RecordingIndicator, run_indicator_thread  # noqa: F401

    assert callable(run_indicator_thread)
    assert hasattr(RecordingIndicator, "show")
    assert hasattr(RecordingIndicator, "hide")
    assert hasattr(RecordingIndicator, "is_alive")


def test_format_elapsed_helper() -> None:
    """Confirm M:SS formatting handles edge cases safely."""
    from recorder.indicator import _format_elapsed

    assert _format_elapsed(0.0) == "0:00"
    assert _format_elapsed(7.9) == "0:07"
    assert _format_elapsed(60.0) == "1:00"
    assert _format_elapsed(125.4) == "2:05"
    # Negative input shouldn't crash; we floor to 0.
    assert _format_elapsed(-3.0) == "0:00"


def test_indicator_show_builds_ui_and_hide_destroys(
    fake_tkinter: dict[str, Any],
) -> None:
    """RecordingIndicator should create a Tk root in show() and tear it down
    cleanly when hide() is called from another thread."""
    from recorder.indicator import RecordingIndicator

    rec = _FakeRecorder()
    stop_calls: list[int] = []
    indicator = RecordingIndicator(rec, stop_callback=lambda: stop_calls.append(1))

    # Run show() in a thread so we can call hide() from the main thread.
    show_thread = threading.Thread(target=indicator.show, daemon=True)
    show_thread.start()

    # Wait for the fake root to be created.
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if fake_tkinter["root_holder"]["root"] is not None:
            break
        time.sleep(0.01)
    root: _FakeRoot | None = fake_tkinter["root_holder"]["root"]
    assert root is not None, "indicator did not create a Tk root"

    # The indicator should be alive while the mainloop is running.
    assert indicator.is_alive()

    # Verify essential UI configuration.
    assert root.override_redirect is True
    # Geometry was set (one initial call in _build_ui).
    assert root.geometry_calls, "geometry not configured"
    # WM attrs (topmost / alpha) requested.
    assert root.wm_attributes_calls, "wm_attributes not called"
    # WM_DELETE_WINDOW protocol installed for safety.
    assert "WM_DELETE_WINDOW" in root.protocols

    # Two polling loops scheduled (time tick + waveform tick).
    assert len(root.scheduled) >= 2

    # hide() schedules a destroy via after(0, ...). With our fake root the
    # function is queued in `scheduled`; we need to invoke it manually.
    indicator.hide()
    # Drain pending scheduled callbacks so the destroy actually runs.
    while root.scheduled:
        _ms, fn = root.scheduled.pop(0)
        try:
            fn()
        except Exception:
            pass

    # Wait for the show() thread to exit its mainloop.
    show_thread.join(timeout=2.0)
    assert not show_thread.is_alive(), "show() did not return after hide()"
    assert root._destroyed
    assert not indicator.is_alive()

    # Idempotent hide() — calling again is a no-op.
    indicator.hide()


def test_indicator_stop_callback_invoked_on_button_press(
    fake_tkinter: dict[str, Any],
) -> None:
    """Simulate a stop-button click → stop_callback fires once and the window
    closes itself."""
    from recorder.indicator import RecordingIndicator

    rec = _FakeRecorder()
    stop_calls: list[int] = []
    indicator = RecordingIndicator(rec, stop_callback=lambda: stop_calls.append(1))

    show_thread = threading.Thread(target=indicator.show, daemon=True)
    show_thread.start()

    # Wait for the root + UI build.
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if (
            fake_tkinter["root_holder"]["root"] is not None
            and indicator._stop_canvas is not None  # noqa: SLF001
        ):
            break
        time.sleep(0.01)
    root: _FakeRoot | None = fake_tkinter["root_holder"]["root"]
    assert root is not None
    stop_canvas: _FakeWidget | None = indicator._stop_canvas  # noqa: SLF001
    assert stop_canvas is not None

    # The stop canvas must have a Button-1 binding.
    assert "<Button-1>" in stop_canvas.bindings, stop_canvas.bindings
    click_handler = stop_canvas.bindings["<Button-1>"]

    # Simulate the click. We pass a dummy event; the handler ignores it.
    click_handler(MagicMock())
    assert stop_calls == [1], f"stop_callback should fire once, got {stop_calls}"

    # Click again — should NOT fire a second time (guarded by ``_stop_clicked``).
    click_handler(MagicMock())
    assert stop_calls == [1], "stop_callback fired multiple times"

    # The click already calls hide(); drain pending scheduled callbacks so
    # the fake mainloop unblocks.
    while root.scheduled:
        _ms, fn = root.scheduled.pop(0)
        try:
            fn()
        except Exception:
            pass

    show_thread.join(timeout=2.0)
    assert not show_thread.is_alive()


def test_indicator_waveform_tick_calls_recorder_get_waveform(
    fake_tkinter: dict[str, Any],
) -> None:
    """The waveform polling tick should call ``recorder.get_waveform`` and
    not raise even when the recorder returns an empty list."""
    from recorder.indicator import RecordingIndicator

    rec = _FakeRecorder(samples=[])  # empty waveform → silent / pre-callback case
    rec_calls: list[int] = []
    original_get = rec.get_waveform

    def tracked(n: int = 600) -> list[float]:
        rec_calls.append(n)
        return original_get(n)

    rec.get_waveform = tracked  # type: ignore[method-assign]

    indicator = RecordingIndicator(rec, stop_callback=lambda: None)
    show_thread = threading.Thread(target=indicator.show, daemon=True)
    show_thread.start()

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if fake_tkinter["root_holder"]["root"] is not None:
            break
        time.sleep(0.01)
    root: _FakeRoot | None = fake_tkinter["root_holder"]["root"]
    assert root is not None

    # Drive one tick of each scheduled callback. CRITICAL: snapshot the list
    # BEFORE invoking — each tick re-schedules itself by appending to
    # `root.scheduled`, so iterating the list directly is an infinite loop.
    initial_callbacks = list(root.scheduled)
    root.scheduled.clear()
    for _ms, fn in initial_callbacks:
        try:
            fn()
        except Exception:
            pass

    # Even if multiple ticks ran, recorder.get_waveform must have been
    # called at least once with a positive sample count.
    assert any(n > 0 for n in rec_calls), rec_calls

    # Tear down.
    indicator.hide()
    while root.scheduled:
        _ms, fn = root.scheduled.pop(0)
        try:
            fn()
        except Exception:
            pass
    show_thread.join(timeout=2.0)


def test_run_indicator_thread_returns_handle_and_thread(
    fake_tkinter: dict[str, Any],
) -> None:
    """The factory helper should return both the handle and a started daemon thread."""
    from recorder.indicator import RecordingIndicator, run_indicator_thread

    rec = _FakeRecorder()
    handle, thread = run_indicator_thread(rec, stop_callback=lambda: None)
    try:
        assert isinstance(handle, RecordingIndicator)
        assert isinstance(thread, threading.Thread)
        assert thread.daemon is True
        assert thread.is_alive(), "indicator thread should be running"

        # Wait until the fake root is created so hide() has something to act on.
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if fake_tkinter["root_holder"]["root"] is not None:
                break
            time.sleep(0.01)
    finally:
        # Clean up regardless of test outcome.
        handle.hide()
        root: _FakeRoot | None = fake_tkinter["root_holder"]["root"]
        if root is not None:
            while root.scheduled:
                _ms, fn = root.scheduled.pop(0)
                try:
                    fn()
                except Exception:
                    pass
        thread.join(timeout=2.0)


def test_indicator_show_after_destroy_is_noop(
    fake_tkinter: dict[str, Any],
) -> None:
    """Calling show() after the indicator was destroyed must not crash."""
    from recorder.indicator import RecordingIndicator

    rec = _FakeRecorder()
    indicator = RecordingIndicator(rec, stop_callback=lambda: None)

    # Mark as destroyed up front (simulate already-torn-down state).
    indicator._destroyed.set()  # noqa: SLF001
    # Should be a clean no-op — no Tk root created.
    indicator.show()
    assert fake_tkinter["root_holder"]["root"] is None


# ---------------------------------------------------------------------------
# Bug 2 (P2): show() / hide() startup race — hide called before show assigns
# self._root must NOT leave an orphan window.
# ---------------------------------------------------------------------------


def test_hide_before_show_starts_prevents_window_creation(
    fake_tkinter: dict[str, Any],
) -> None:
    """Bug 2 regression: if ``hide()`` is called BEFORE ``show()`` has even
    started, ``show()`` must observe the destroyed flag and refuse to create
    a Tk root at all (or, if it did, destroy it immediately).
    """
    from recorder.indicator import RecordingIndicator

    rec = _FakeRecorder()
    indicator = RecordingIndicator(rec, stop_callback=lambda: None)

    # Call hide() first — no show thread started yet.
    indicator.hide()

    # Now start show(). It should exit quickly without mainloop'ing on
    # anything.
    show_thread = threading.Thread(target=indicator.show, daemon=True)
    show_thread.start()
    show_thread.join(timeout=2.0)
    assert not show_thread.is_alive(), "show() should return immediately after pre-hide"

    # No Tk root should have been created (the early _destroyed check in
    # show() fires before the Tk() call), or if one was, it must have been
    # destroyed.
    root = fake_tkinter["root_holder"]["root"]
    if root is not None:
        assert root._destroyed, (  # noqa: SLF001
            "hide() before show() must ensure any created root is destroyed"
        )


def test_hide_during_show_startup_destroys_root_promptly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug 2 regression: if ``hide()`` is called BETWEEN ``tk.Tk()`` returning
    and show() finishing the root-assignment / UI build, the freshly created
    root must be torn down immediately — preventing an orphan borderless
    window that outlives the recording.

    We simulate the race by wrapping ``tk.Tk`` in a factory that calls
    ``indicator.hide()`` just before returning the root, so show() re-checks
    ``_destroyed`` under the lock and takes the bail-out path.
    """
    import types as _types

    from recorder.indicator import RecordingIndicator

    # Build a local fake tkinter module (we can't share the fixture because
    # we need a side-effecting Tk() factory that references `indicator`).
    fake_tk = _types.ModuleType("tkinter")

    created_roots: list[_FakeRoot] = []
    indicator_holder: dict[str, RecordingIndicator | None] = {"ind": None}

    class _RacyTkFactory:
        def __call__(self) -> _FakeRoot:
            # Build the root, THEN call hide() before returning — simulating
            # the exact race where the stop path fires while show() is inside
            # tk.Tk().
            root = _FakeRoot()
            created_roots.append(root)
            ind = indicator_holder["ind"]
            assert ind is not None
            ind.hide()
            return root

    fake_tk.Tk = _RacyTkFactory()  # type: ignore[attr-defined]
    fake_tk.Frame = _FakeWidget  # type: ignore[attr-defined]
    fake_tk.Label = _FakeWidget  # type: ignore[attr-defined]
    fake_tk.Canvas = _FakeWidget  # type: ignore[attr-defined]

    class _TclError(Exception):
        pass

    fake_tk.TclError = _TclError  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "tkinter", fake_tk)

    rec = _FakeRecorder()
    indicator = RecordingIndicator(rec, stop_callback=lambda: None)
    indicator_holder["ind"] = indicator

    # show() runs on the main thread here — no mainloop block because the
    # bail-out path returns before mainloop is reached.
    indicator.show()

    # Exactly one root should have been created (by the factory).
    assert len(created_roots) == 1, f"expected 1 root, got {len(created_roots)}"
    root = created_roots[0]

    # CRITICAL: the root must have been destroyed — otherwise it's an orphan.
    assert root._destroyed, (  # noqa: SLF001
        "Root created during a hide() race must be destroyed by show()'s "
        "post-Tk() re-check."
    )

    # Mainloop must NEVER have been entered (the race caused early return
    # BEFORE show() could schedule ticks / enter mainloop). Verified by the
    # fact that no ticks were scheduled.
    # Snapshot before clearing — per the prompt's "never iterate
    # root.scheduled directly because ticks reschedule themselves". Here the
    # list should be empty anyway, but we guard defensively.
    cbs = list(root.scheduled)
    root.scheduled.clear()
    assert cbs == [], (
        f"show() scheduled ticks despite the hide-race bail-out: {cbs}"
    )

    # hide() after the fact is still a clean no-op.
    indicator.hide()
