"""Small always-on-top recording indicator with live waveform + stop button.

Spawns a borderless tkinter window that polls :meth:`AudioRecorder.get_waveform`
and :meth:`AudioRecorder.get_level` to render a scrolling cyan waveform plus a
red "REC" pulse and an elapsed-time readout. Clicking the stop button invokes
the user-supplied ``stop_callback`` — typically the tray's hotkey toggle — so
the user can end a recording without reaching for the keyboard.

Threading
---------
Tkinter must run on the thread that created its :class:`tkinter.Tk` instance.
We therefore expose two paths:

* :meth:`RecordingIndicator.show` — builds and runs the UI on the **current**
  thread (blocks until the window closes).
* :func:`run_indicator_thread` — convenience helper that spins up a daemon
  thread and calls ``show`` there, returning a ``(handle, thread)`` tuple so
  the caller can close the window later by calling ``handle.hide()`` from any
  thread.

``hide()`` schedules the actual ``destroy()`` call via ``after(0, ...)`` so
it's safe to invoke from a non-Tk thread.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    import tkinter as tk

    from recorder.recorder import AudioRecorder

logger = logging.getLogger(__name__)


# --- visual constants ------------------------------------------------------

_WIN_WIDTH: int = 360
_WIN_HEIGHT: int = 88
_WIN_MARGIN: int = 24
_BG_COLOR: str = "#1a1a1a"
_FG_COLOR: str = "#e5e5e5"
_REC_COLOR: str = "#ef4444"  # red
_WAVE_COLOR: str = "#38bdf8"  # cyan
_STOP_HOVER_COLOR: str = "#f87171"  # lighter red on hover
_FONT_FAMILY: str = "Segoe UI"
_FONT_SIZE: int = 10
_ALPHA: float = 0.92

_REC_DOT_DIAMETER: int = 14
_STOP_BUTTON_DIAMETER: int = 26
_STOP_SQUARE_SIZE: int = 10
_WAVEFORM_WIDTH: int = 180
_WAVEFORM_HEIGHT: int = 60
_WAVEFORM_COLUMNS: int = 180

# Update cadences. Time/REC dot tick at 100 ms; waveform at ~50 ms (≈20 fps).
_TIME_TICK_MS: int = 100
_WAVEFORM_TICK_MS: int = 50
_PULSE_PERIOD_SEC: float = 1.2  # one pulse every 1.2 s


def _format_elapsed(seconds: float) -> str:
    """Format a non-negative duration as ``M:SS`` (no zero-padding on minutes)."""
    if seconds < 0 or seconds != seconds:  # NaN guard
        seconds = 0.0
    total = int(seconds)
    return f"{total // 60}:{total % 60:02d}"


class RecordingIndicator:
    """Borderless overlay showing live waveform + stop button while recording.

    The instance is constructed cheaply (no GUI side effects); the actual Tk
    root is built inside :meth:`show`, which **must** run on the thread that
    will own the window. Use :func:`run_indicator_thread` for the common case
    where the caller wants the indicator to live on its own daemon thread.

    Parameters
    ----------
    recorder:
        Anything exposing :meth:`get_waveform(n)` and :meth:`get_level()`
        accessors with the same semantics as :class:`AudioRecorder`.
    stop_callback:
        Zero-arg callable invoked when the user clicks the stop button. The
        callback should be cheap / non-blocking; if it does heavy work,
        dispatch onto another thread itself.
    """

    def __init__(
        self,
        recorder: "AudioRecorder",
        stop_callback: Callable[[], None],
    ) -> None:
        self._recorder = recorder
        self._stop_callback = stop_callback

        # Built lazily inside show() so the constructor is safe to call from
        # any thread.
        self._root: "tk.Tk | None" = None
        self._waveform_canvas: "tk.Canvas | None" = None
        self._stop_canvas: "tk.Canvas | None" = None
        self._rec_dot_canvas: "tk.Canvas | None" = None
        self._rec_dot_id: int | None = None
        self._time_label: "tk.Label | None" = None
        self._stop_circle_id: int | None = None
        self._waveform_line_ids: list[int] = []

        self._start_monotonic: float | None = None
        self._stop_clicked = False
        self._destroyed = threading.Event()
        # Protects ``self._root`` assignment and the ``_destroyed`` check
        # against the ``show()`` <-> ``hide()`` startup race (P2): without this
        # lock, hide() can read ``_root`` as None (because show() hasn't
        # assigned it yet) and silently no-op, leaving an orphan window when
        # show() subsequently finishes building the UI and enters mainloop.
        self._root_lock = threading.Lock()

        # Drag-to-move state.
        self._drag_offset_x: int = 0
        self._drag_offset_y: int = 0

    # ------------------------------------------------------------- public

    def is_alive(self) -> bool:
        """Whether the underlying Tk root currently exists.

        Returns ``False`` both before :meth:`show` is called and after
        :meth:`hide` (or a user close) has destroyed the window.
        """
        if self._destroyed.is_set():
            return False
        root = self._root
        if root is None:
            return False
        try:
            # ``winfo_exists`` returns 1/0 (or raises if the interpreter is
            # gone); cast to bool for clarity.
            return bool(root.winfo_exists())
        except Exception:
            return False

    def show(self) -> None:
        """Build the Tk UI and run its mainloop on the **current** thread.

        Blocks until the window closes (either via :meth:`hide`, the user
        clicking the stop button, or an OS-level close). Safe to call once
        per instance — calling again after destroy is a no-op.
        """
        if self._destroyed.is_set():
            logger.debug("show() called on already-destroyed indicator; ignoring")
            return

        import tkinter as tk  # local import — keeps tray startup snappy

        try:
            root = tk.Tk()
        except Exception:
            logger.exception("Failed to create Tk root for indicator window")
            self._destroyed.set()
            return

        # Re-check under the lock: hide() may have been called while we were
        # inside ``tk.Tk()``. If so, destroy the freshly created root and bail
        # out — otherwise we'd leave an orphan borderless window alive after
        # the recording has already stopped (P2 race).
        with self._root_lock:
            if self._destroyed.is_set():
                try:
                    root.destroy()
                except Exception:
                    logger.debug(
                        "destroy on raced-hide root failed", exc_info=True
                    )
                return
            self._root = root

        self._start_monotonic = time.monotonic()

        try:
            self._build_ui(root)
        except Exception:
            logger.exception("Failed to build indicator UI")
            try:
                root.destroy()
            finally:
                self._root = None
                self._destroyed.set()
            return

        # Start the polling loops.
        try:
            root.after(_TIME_TICK_MS, self._tick_time)
            root.after(_WAVEFORM_TICK_MS, self._tick_waveform)
        except Exception:
            logger.exception("Failed to schedule indicator polling")

        try:
            root.mainloop()
        except Exception:
            logger.exception("Indicator mainloop crashed")
        finally:
            self._destroyed.set()
            self._root = None

    def hide(self) -> None:
        """Close the indicator window from any thread.

        Schedules ``root.destroy()`` via ``after(0, ...)`` so the destroy
        runs on the Tk thread (the only thread allowed to touch widgets).
        Safe to call multiple times.

        Uses :attr:`_root_lock` to serialize with :meth:`show`: if hide runs
        BEFORE show has assigned ``self._root = root``, setting ``_destroyed``
        under the lock ensures show will see it on re-check and destroy the
        freshly created root immediately — preventing an orphan window.
        """
        if self._destroyed.is_set():
            return
        with self._root_lock:
            if self._destroyed.is_set():
                return
            self._destroyed.set()
            root = self._root
        if root is None:
            # show() hasn't reached its root assignment yet. Because we set
            # ``_destroyed`` under the lock, show() will observe it on its
            # post-``tk.Tk()`` re-check and destroy the root itself.
            return
        try:
            root.after(0, self._safe_destroy)
        except Exception:
            # Tk may already be torn down (rare race); _destroyed is already
            # set above, so nothing more to do.
            logger.debug("hide() could not schedule destroy", exc_info=True)

    # -------------------------------------------------------------- internal

    def _safe_destroy(self) -> None:
        """Tk-thread destroy. Idempotent.

        Called via ``root.after(0, ...)`` from :meth:`hide`, which sets
        ``_destroyed`` BEFORE scheduling this callback (to synchronize with
        :meth:`show`). We therefore cannot gate on ``_destroyed`` here — we
        pop the root under the lock instead so repeated invocations of
        this callback are harmless.
        """
        with self._root_lock:
            root = self._root
            self._root = None
        if root is None:
            return
        try:
            root.destroy()
        except Exception:
            logger.debug("destroy raised", exc_info=True)

    def _build_ui(self, root: "tk.Tk") -> None:
        """Create the borderless overlay window and lay out its widgets."""
        import tkinter as tk

        root.title("Voice Notes — Recording")
        root.overrideredirect(True)
        try:
            root.wm_attributes("-topmost", True)
            root.wm_attributes("-alpha", _ALPHA)
        except tk.TclError:
            logger.debug(
                "wm_attributes for topmost/alpha not supported on this platform",
                exc_info=True,
            )

        root.configure(bg=_BG_COLOR)

        # Position bottom-right of the primary monitor.
        try:
            screen_w = root.winfo_screenwidth()
            screen_h = root.winfo_screenheight()
        except Exception:
            screen_w, screen_h = 1920, 1080
        x = max(0, screen_w - _WIN_WIDTH - _WIN_MARGIN)
        y = max(0, screen_h - _WIN_HEIGHT - _WIN_MARGIN)
        root.geometry(f"{_WIN_WIDTH}x{_WIN_HEIGHT}+{x}+{y}")

        # Outer container — dragging anywhere on this moves the window.
        outer = tk.Frame(root, bg=_BG_COLOR)
        outer.pack(fill="both", expand=True, padx=10, pady=10)

        # --- left: REC dot + elapsed time ----------------------------------
        left = tk.Frame(outer, bg=_BG_COLOR)
        left.pack(side="left", padx=(0, 10))

        self._rec_dot_canvas = tk.Canvas(
            left,
            width=_REC_DOT_DIAMETER,
            height=_REC_DOT_DIAMETER,
            bg=_BG_COLOR,
            highlightthickness=0,
            bd=0,
        )
        self._rec_dot_canvas.pack(side="left", padx=(0, 6))
        self._rec_dot_id = self._rec_dot_canvas.create_oval(
            1,
            1,
            _REC_DOT_DIAMETER - 1,
            _REC_DOT_DIAMETER - 1,
            fill=_REC_COLOR,
            outline="",
        )

        self._time_label = tk.Label(
            left,
            text="0:00",
            bg=_BG_COLOR,
            fg=_FG_COLOR,
            font=(_FONT_FAMILY, _FONT_SIZE),
        )
        self._time_label.pack(side="left")

        # --- middle: waveform canvas ---------------------------------------
        self._waveform_canvas = tk.Canvas(
            outer,
            width=_WAVEFORM_WIDTH,
            height=_WAVEFORM_HEIGHT,
            bg=_BG_COLOR,
            highlightthickness=0,
            bd=0,
        )
        self._waveform_canvas.pack(side="left", padx=(0, 10))

        # --- right: stop button --------------------------------------------
        self._stop_canvas = tk.Canvas(
            outer,
            width=_STOP_BUTTON_DIAMETER,
            height=_STOP_BUTTON_DIAMETER,
            bg=_BG_COLOR,
            highlightthickness=0,
            bd=0,
            cursor="hand2",
        )
        self._stop_canvas.pack(side="right")

        self._stop_circle_id = self._stop_canvas.create_oval(
            0,
            0,
            _STOP_BUTTON_DIAMETER,
            _STOP_BUTTON_DIAMETER,
            fill=_REC_COLOR,
            outline="",
        )
        sq_x0 = (_STOP_BUTTON_DIAMETER - _STOP_SQUARE_SIZE) // 2
        sq_y0 = (_STOP_BUTTON_DIAMETER - _STOP_SQUARE_SIZE) // 2
        self._stop_canvas.create_rectangle(
            sq_x0,
            sq_y0,
            sq_x0 + _STOP_SQUARE_SIZE,
            sq_y0 + _STOP_SQUARE_SIZE,
            fill="#ffffff",
            outline="",
        )

        # Click + hover bindings.
        self._stop_canvas.bind("<Button-1>", self._on_stop_clicked)
        self._stop_canvas.bind("<Enter>", self._on_stop_hover_enter)
        self._stop_canvas.bind("<Leave>", self._on_stop_hover_leave)

        # Drag-to-move bindings on every "background" widget.
        for widget in (root, outer, left, self._waveform_canvas, self._time_label):
            widget.bind("<ButtonPress-1>", self._on_drag_start)
            widget.bind("<B1-Motion>", self._on_drag_motion)

        # Window-close (Alt+F4 etc.) → treat as stop click for safety.
        try:
            root.protocol("WM_DELETE_WINDOW", self._on_stop_clicked)
        except Exception:
            logger.debug("WM_DELETE_WINDOW protocol unavailable", exc_info=True)

    # ----------------------------------------------------------------- ticks

    def _tick_time(self) -> None:
        """Update the elapsed-time label and pulse the REC dot."""
        if self._destroyed.is_set() or self._root is None:
            return
        try:
            elapsed = (
                time.monotonic() - self._start_monotonic
                if self._start_monotonic is not None
                else 0.0
            )
            if self._time_label is not None:
                self._time_label.config(text=_format_elapsed(elapsed))

            self._pulse_rec_dot(elapsed)
        except Exception:
            logger.debug("time tick failed", exc_info=True)
        finally:
            try:
                self._root.after(_TIME_TICK_MS, self._tick_time)
            except Exception:
                # Root has been destroyed; stop polling.
                pass

    def _pulse_rec_dot(self, elapsed_sec: float) -> None:
        """Gently fade the REC dot by recoloring between two reds."""
        if self._rec_dot_canvas is None or self._rec_dot_id is None:
            return
        # Sine-wave LFO between 0.5 and 1.0 over one period.
        import math

        phase = (elapsed_sec % _PULSE_PERIOD_SEC) / _PULSE_PERIOD_SEC
        # 0..1 sine clamp:
        scale = 0.5 + 0.5 * math.sin(2.0 * math.pi * phase - math.pi / 2.0)
        # Lerp red brightness — keep R high, vary G/B slightly so the dot
        # looks like it's pulsing rather than fading to black.
        red = 0xEF
        gb = int(round(0x44 + (1.0 - scale) * 0x40))  # 0x44..0x84
        gb = max(0x44, min(0xCC, gb))
        color = f"#{red:02x}{gb:02x}{gb:02x}"
        try:
            self._rec_dot_canvas.itemconfig(self._rec_dot_id, fill=color)
        except Exception:
            logger.debug("rec dot recolor failed", exc_info=True)

    def _tick_waveform(self) -> None:
        """Pull a fresh waveform snapshot and redraw the canvas."""
        if self._destroyed.is_set() or self._root is None:
            return
        try:
            samples = self._recorder.get_waveform(_WAVEFORM_COLUMNS)
            self._draw_waveform(samples)
        except Exception:
            logger.debug("waveform tick failed", exc_info=True)
        finally:
            try:
                self._root.after(_WAVEFORM_TICK_MS, self._tick_waveform)
            except Exception:
                pass

    def _draw_waveform(self, samples: list[float]) -> None:
        """Render ``samples`` as vertical lines mirrored around the midline."""
        canvas = self._waveform_canvas
        if canvas is None:
            return
        try:
            canvas.delete("waveform")
        except Exception:
            return

        if not samples:
            return

        mid_y = _WAVEFORM_HEIGHT / 2.0
        max_amplitude = mid_y - 1.0  # 1px breathing room top/bottom

        n = len(samples)
        # Map sample index → x coordinate evenly across the canvas.
        for i, s in enumerate(samples):
            # Symmetric vertical line: |s| controls height, mirrored around midline.
            magnitude = abs(s)
            if magnitude < 0.01:
                # Floor to 1 px so a totally silent input still draws a baseline.
                half_h = 1.0
            else:
                half_h = max(1.0, magnitude * max_amplitude)
            x = (
                int(round(i * (_WAVEFORM_WIDTH - 1) / max(1, n - 1)))
                if n > 1
                else _WAVEFORM_WIDTH // 2
            )
            try:
                canvas.create_line(
                    x,
                    mid_y - half_h,
                    x,
                    mid_y + half_h,
                    fill=_WAVE_COLOR,
                    width=1,
                    tags=("waveform",),
                )
            except Exception:
                # Canvas was destroyed mid-redraw.
                return

    # ----------------------------------------------------------------- input

    def _on_stop_clicked(self, _event: Any = None) -> None:
        """Invoke the user's stop callback once, then close the window."""
        if self._stop_clicked:
            return
        self._stop_clicked = True
        try:
            self._stop_callback()
        except Exception:
            logger.exception("stop_callback raised")
        # Close the window — the caller is also expected to call hide(),
        # but doing it here makes the click feel responsive.
        self.hide()

    def _on_stop_hover_enter(self, _event: Any = None) -> None:
        if self._stop_canvas is None or self._stop_circle_id is None:
            return
        try:
            self._stop_canvas.itemconfig(self._stop_circle_id, fill=_STOP_HOVER_COLOR)
        except Exception:
            logger.debug("hover-enter recolor failed", exc_info=True)

    def _on_stop_hover_leave(self, _event: Any = None) -> None:
        if self._stop_canvas is None or self._stop_circle_id is None:
            return
        try:
            self._stop_canvas.itemconfig(self._stop_circle_id, fill=_REC_COLOR)
        except Exception:
            logger.debug("hover-leave recolor failed", exc_info=True)

    def _on_drag_start(self, event: Any) -> None:
        """Record the click offset so motion can compute the new window pos."""
        self._drag_offset_x = int(event.x_root) - self._safe_root_x()
        self._drag_offset_y = int(event.y_root) - self._safe_root_y()

    def _on_drag_motion(self, event: Any) -> None:
        """Move the window by the offset from the original click point."""
        if self._destroyed.is_set() or self._root is None:
            return
        try:
            new_x = int(event.x_root) - self._drag_offset_x
            new_y = int(event.y_root) - self._drag_offset_y
            self._root.geometry(f"+{new_x}+{new_y}")
        except Exception:
            logger.debug("drag motion failed", exc_info=True)

    def _safe_root_x(self) -> int:
        if self._root is None:
            return 0
        try:
            return int(self._root.winfo_rootx())
        except Exception:
            return 0

    def _safe_root_y(self) -> int:
        if self._root is None:
            return 0
        try:
            return int(self._root.winfo_rooty())
        except Exception:
            return 0


# --- factory helper ---------------------------------------------------------


def run_indicator_thread(
    recorder: "AudioRecorder",
    stop_callback: Callable[[], None],
) -> tuple[RecordingIndicator, threading.Thread]:
    """Spawn a daemon thread that hosts the indicator window.

    Returns the ``(handle, thread)`` pair so the caller can ``handle.hide()``
    from elsewhere when recording stops. The thread is daemonized so an
    untorn-down indicator never holds the process open at exit.
    """
    handle = RecordingIndicator(recorder=recorder, stop_callback=stop_callback)

    def _runner() -> None:
        try:
            handle.show()
        except Exception:
            logger.exception("Indicator thread crashed")

    thread = threading.Thread(
        target=_runner, name="recording-indicator", daemon=True
    )
    thread.start()
    return handle, thread
