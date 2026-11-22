"""Floating recording indicator for Flow (hold-to-talk dictation).

Part (a): IndicatorState — pure Python, no AppKit, headlessly testable.
Part (b): OverlayPanel   — PyObjC AppKit panel; AppKit imported lazily so
          Part (a) is usable in tests without a running NSApplication.
"""
from __future__ import annotations

import collections
import re

# ---------------------------------------------------------------------------
# State constants
# ---------------------------------------------------------------------------
IDLE = "idle"
RECORDING = "recording"
PROCESSING = "processing"
ERROR = "error"


# ---------------------------------------------------------------------------
# Part (a) — pure model, no AppKit
# ---------------------------------------------------------------------------

def pill_origin(screen_x, screen_y, screen_width, pill_width, bottom_margin=72.0):
    """Bottom-center origin for a pill on a screen (pure, testable)."""
    return (screen_x + (screen_width - pill_width) / 2.0, screen_y + bottom_margin)


def screen_under_point(screens, point):
    """Return the screen whose frame contains `point`, or None.

    `screens` are NSScreen-like objects exposing frame().origin/size; `point` has x/y.
    """
    for screen in screens:
        f = screen.frame()
        ox, oy = f.origin.x, f.origin.y
        if ox <= point.x < ox + f.size.width and oy <= point.y < oy + f.size.height:
            return screen
    return None


def parse_hex(s: str) -> tuple[float, float, float]:
    """Parse a 6-digit hex color into (r, g, b) floats in [0, 1].

    Accepts with or without a leading '#'.  Raises ValueError otherwise.
    """
    s = s.strip()
    if s.startswith("#"):
        s = s[1:]
    if not re.fullmatch(r"[0-9A-Fa-f]{6}", s):
        raise ValueError(f"Expected 6-digit hex color, got {s!r}")
    return int(s[0:2], 16) / 255.0, int(s[2:4], 16) / 255.0, int(s[4:6], 16) / 255.0


class IndicatorState:
    """Model for the overlay.  push_level() is the only hot-path method;
    it only appends a float to a deque — no AppKit, no locks held long."""

    def __init__(self, gain: float = 6.0, capacity: int = 32):
        self.gain = gain
        self.levels: collections.deque = collections.deque(maxlen=capacity)
        self.state: str = IDLE
        self.mode: str = "cleanup"
        self.latency_text: str = ""
        self.error_text: str = ""

    # Hot path — audio callback
    def push_level(self, rms: float) -> None:
        self.levels.append(min(1.0, max(0.0, rms * self.gain)))

    # State transitions
    def set_recording(self) -> None:
        self.state = RECORDING

    def set_processing(self) -> None:
        self.state = PROCESSING

    def set_idle(self) -> None:
        self.state = IDLE

    def set_error(self, text: str = "") -> None:
        self.error_text = text
        self.state = ERROR

    def set_mode(self, raw: bool) -> None:
        self.mode = "raw" if raw else "cleanup"

    def set_latency(self, text: str) -> None:
        self.latency_text = text


# ---------------------------------------------------------------------------
# Part (b) — OverlayPanel (AppKit; imported lazily)
# ---------------------------------------------------------------------------

class OverlayPanel:
    """Floating pill that shows waveform/processing/error states.

    Every AppKit mutation is marshalled to the main thread via
    AppHelper.callAfter().  level() skips marshalling (audio-callback path).
    """

    _gen = 0  # Show generation; see recording()/_hide_after_error().

    def __init__(self, accent_rgb: tuple, gain: float = 6.0, capacity: int = 32):
        # Deferred imports keep Part (a) import-clean and tests patchable.
        import AppKit
        import objc
        from PyObjCTools import AppHelper

        self._AppKit = AppKit
        self._AppHelper = AppHelper
        self._objc = objc

        self.state = IndicatorState(gain=gain, capacity=capacity)
        self.accent_rgb = accent_rgb
        self._panel = None
        self._timer = None
        self._frame_counter = 0

        self._build_panel()
        self._visible = False
        self._gen = 0  # Bumped on every show; delayed hides check it so they never hide a newer recording.
        self._observe_screen_changes()

    # ------------------------------------------------------------------
    # Display changes: rebuild the panel so a window created under an old
    # display layout (monitor unplugged, clamshell) never lingers invisible.
    # ------------------------------------------------------------------

    def _observe_screen_changes(self):
        AppKit, objc = self._AppKit, self._objc
        try:
            ObserverClass = _make_screen_observer_class(AppKit, objc)
            self._screen_observer = ObserverClass.alloc().init()
            self._screen_observer.indicator = self
            AppKit.NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(
                self._screen_observer, b"screensChanged:",
                AppKit.NSApplicationDidChangeScreenParametersNotification, None)
        except Exception as exc:  # Never let the indicator take Flow down.
            self._screen_observer = None
            import logging
            logging.getLogger("flow").warning("Screen-change observer unavailable: %s", exc)

    def _rebuild_panel(self):
        """Main thread. Drop the current NSPanel and create a fresh one in the new layout."""
        was_visible = self._visible
        self._stop_timer()
        old = self._panel
        try:
            old.orderOut_(None)
            old.close()
        except Exception:
            pass
        try:
            self._build_panel()
        except Exception as exc:  # Keep the old (possibly stale) panel rather than crash the main thread.
            import logging
            logging.getLogger("flow").error("Indicator panel rebuild failed: %s", exc)
            self._panel = old
            return
        if was_visible:
            self._show()

    def flash(self, seconds: float = 2.0):
        """Show the pill in recording state for `seconds`, then hide (self-test; any thread).

        Ignored while a real recording or processing is under way so it never disturbs one.
        """
        if self.state.state != IDLE:
            return
        self.recording()
        gen = self._gen
        for i in range(1, int(seconds * 10)):
            self._AppHelper.callLater(i / 10.0, self._flash_level, gen, 0.02 + 0.03 * ((i % 7) / 7.0))
        self._AppHelper.callLater(seconds, self._idle_if_gen, gen)

    def _flash_level(self, gen, rms):
        if gen == self._gen:
            self.state.push_level(rms)

    def _idle_if_gen(self, gen):
        if gen == self._gen:
            self.idle()

    # ------------------------------------------------------------------
    # Panel construction
    # ------------------------------------------------------------------

    def _build_panel(self):
        AppKit = self._AppKit
        objc = self._objc

        style = (
            AppKit.NSWindowStyleMaskBorderless
            | AppKit.NSWindowStyleMaskNonactivatingPanel
        )

        x, y, w, h = self._target_frame()
        frame = AppKit.NSMakeRect(x, y, w, h)

        panel = AppKit.NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            frame, style, AppKit.NSBackingStoreBuffered, False
        )
        panel.setLevel_(AppKit.NSFloatingWindowLevel)
        panel.setHasShadow_(False)
        panel.setOpaque_(False)
        panel.setBackgroundColor_(AppKit.NSColor.clearColor())
        panel.setIgnoresMouseEvents_(True)
        panel.setCollectionBehavior_(
            AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
            | AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary
            | AppKit.NSWindowCollectionBehaviorStationary
        )
        panel.setReleasedWhenClosed_(False)

        PillViewClass = _make_pill_view_class(AppKit, objc)
        pill = PillViewClass.alloc().initWithFrame_(frame)
        pill.indicator = self
        panel.setContentView_(pill)

        self._panel = panel
        self._pill = pill

    def _target_frame(self):
        """Bottom-center of the display under the mouse (fallback: main screen).

        Recomputed on every show so the pill follows display changes (external monitor
        unplugged, lid closed, mouse on another screen) instead of staying where it was
        when Flow started.
        """
        AppKit = self._AppKit
        w, h = 200.0, 48.0
        screen = screen_under_point(AppKit.NSScreen.screens(), AppKit.NSEvent.mouseLocation())
        if screen is None:
            screen = AppKit.NSScreen.mainScreen() or (AppKit.NSScreen.screens() or [None])[0]
        if screen is None:
            return 0.0, 72.0, w, h
        sf = screen.frame()
        return pill_origin(sf.origin.x, sf.origin.y, sf.size.width, w) + (w, h)

    def _reposition(self):
        x, y, w, h = self._target_frame()
        self._panel.setFrame_display_(self._AppKit.NSMakeRect(x, y, w, h), False)

    # ------------------------------------------------------------------
    # Timer (must run on main thread)
    # ------------------------------------------------------------------

    def _start_timer(self):
        if self._timer is not None:
            return
        self._timer = self._AppKit.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            1.0 / 30.0, self._pill, b"timerFired:", None, True
        )

    def _stop_timer(self):
        if self._timer is not None:
            self._timer.invalidate()
            self._timer = None

    # ------------------------------------------------------------------
    # Show/hide (called on main thread via callAfter)
    # ------------------------------------------------------------------

    def _show(self):
        self._visible = True
        self._reposition()
        self._start_timer()
        self._panel.orderFrontRegardless()

    def _hide(self):
        self._visible = False
        self._stop_timer()
        self._panel.orderOut_(None)

    def _hide_after_error(self, gen):
        if gen != self._gen:  # A newer recording/processing started; leave it alone.
            return
        self.state.set_idle()
        self._hide()

    # ------------------------------------------------------------------
    # Public interface (called from any thread)
    # ------------------------------------------------------------------

    def recording(self):
        self._gen += 1
        self.state.set_recording()
        self._AppHelper.callAfter(self._show)

    def level(self, rms: float) -> None:
        # Hot path: audio callback — no AppKit, no marshalling.
        self.state.push_level(rms)

    def processing(self):
        self._gen += 1
        self.state.set_processing()
        self._AppHelper.callAfter(self._show)  # Also covers a limit-clip whose pill was already showing.

    def idle(self):
        self.state.set_idle()
        self._AppHelper.callAfter(self._hide)

    def error(self, text: str = ""):
        self._gen += 1
        self.state.set_error(text)
        self._AppHelper.callAfter(self._show)  # The error may arrive while the pill is hidden.
        self._AppHelper.callLater(1.2, self._hide_after_error, self._gen)

    def mode(self, raw: bool):
        self.state.set_mode(raw)
        self._AppHelper.callAfter(self._pill.setNeedsDisplay_, True)

    def latency(self, text: str):
        self.state.set_latency(text)
        self._AppHelper.callAfter(self._pill.setNeedsDisplay_, True)


# ---------------------------------------------------------------------------
# PillView factory (created once per process, lazily)
# ---------------------------------------------------------------------------

_pill_view_class_cache = {}
_screen_observer_class_cache = {}


def _make_screen_observer_class(AppKit, objc):
    key = id(AppKit)
    if key in _screen_observer_class_cache:
        return _screen_observer_class_cache[key]

    class FlowScreenObserver(AppKit.NSObject):
        indicator = objc.ivar()

        def screensChanged_(self, note):
            ind = self.indicator
            if ind is not None:
                import logging
                logging.getLogger("flow").info("Display layout changed; rebuilding indicator panel")
                ind._rebuild_panel()

    _screen_observer_class_cache[key] = FlowScreenObserver
    return FlowScreenObserver


def _make_pill_view_class(AppKit, objc):
    key = id(AppKit)
    if key in _pill_view_class_cache:
        return _pill_view_class_cache[key]

    NSView = AppKit.NSView
    NSColor = AppKit.NSColor
    NSBezierPath = AppKit.NSBezierPath
    NSMakeRect = AppKit.NSMakeRect

    class PillView(NSView):
        indicator = objc.ivar()

        def timerFired_(self, _timer):
            self.setNeedsDisplay_(True)

        def drawRect_(self, rect):
            ind = self.indicator
            if ind is None:
                return
            st = ind.state
            ar, ag, ab = ind.accent_rgb
            ind._frame_counter += 1
            fc = ind._frame_counter

            bounds = self.bounds()
            w = bounds.size.width
            h = bounds.size.height
            radius = h / 2.0

            # Pill background
            pill_path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                NSMakeRect(0, 0, w, h), radius, radius
            )
            if st.state == ERROR:
                NSColor.colorWithCalibratedRed_green_blue_alpha_(0.07, 0.04, 0.04, 0.92).setFill()
            else:
                NSColor.colorWithCalibratedRed_green_blue_alpha_(18/255, 18/255, 22/255, 0.92).setFill()
            pill_path.fill()

            # Border
            if st.state == ERROR:
                NSColor.colorWithCalibratedRed_green_blue_alpha_(1.0, 0.25, 0.25, 0.35).setStroke()
            else:
                NSColor.colorWithCalibratedRed_green_blue_alpha_(ar, ag, ab, 0.35).setStroke()
            pill_path.setLineWidth_(1.0)
            pill_path.stroke()

            # Content
            if st.state == RECORDING:
                _draw_recording(st, w, h, ar, ag, ab, NSColor, NSBezierPath, NSMakeRect)
            elif st.state == PROCESSING:
                _draw_pulsing_dots(fc, w, h, ar, ag, ab, 1.0, NSColor, NSBezierPath, NSMakeRect)
            elif st.state == ERROR:
                _draw_pulsing_dots(fc, w, h, 1.0, 0.25, 0.25, 1.0, NSColor, NSBezierPath, NSMakeRect)

    _pill_view_class_cache[key] = PillView
    return PillView


def _draw_recording(st, w, h, ar, ag, ab, NSColor, NSBezierPath, NSMakeRect):
    levels = list(st.levels)
    n_bars = 24
    pad_x = 20.0
    pad_y = 6.0
    bar_area_w = w - pad_x * 2
    bar_w = max(2.0, bar_area_w / n_bars - 1)
    bar_step = bar_area_w / n_bars
    max_bar_h = h - pad_y * 2

    while len(levels) < n_bars:
        levels.insert(0, 0.0)
    levels = levels[-n_bars:]

    NSColor.colorWithCalibratedRed_green_blue_alpha_(ar, ag, ab, 1.0).setFill()
    for i, lv in enumerate(levels):
        bar_h = max(2.0, lv * max_bar_h)
        x = pad_x + i * bar_step
        y = (h - bar_h) / 2.0
        NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            NSMakeRect(x, y, bar_w, bar_h), bar_w / 2, bar_w / 2
        ).fill()

    # Red live dot
    dot_r = 4.0
    NSColor.colorWithCalibratedRed_green_blue_alpha_(1.0, 0.2, 0.2, 1.0).setFill()
    NSBezierPath.bezierPathWithOvalInRect_(
        NSMakeRect(8.0, h / 2.0 - dot_r, dot_r * 2, dot_r * 2)
    ).fill()


def _draw_pulsing_dots(fc, w, h, r, g, b, base_alpha, NSColor, NSBezierPath, NSMakeRect):
    n_dots = 3
    dot_r = 5.0
    spacing = 18.0
    total = n_dots * dot_r * 2 + (n_dots - 1) * (spacing - dot_r * 2)
    start_x = (w - total) / 2.0
    phase = (fc // 10) % n_dots

    for i in range(n_dots):
        alpha = base_alpha if i == phase else 0.35
        NSColor.colorWithCalibratedRed_green_blue_alpha_(r, g, b, alpha).setFill()
        x = start_x + i * spacing
        y = h / 2.0 - dot_r
        NSBezierPath.bezierPathWithOvalInRect_(NSMakeRect(x, y, dot_r * 2, dot_r * 2)).fill()
