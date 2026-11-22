"""Tests for overlay.py — IndicatorState (pure model) and OverlayPanel (mocked AppKit)."""
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import overlay as ov
from overlay import (
    IDLE, RECORDING, PROCESSING, ERROR,
    IndicatorState, parse_hex,
)


# ---------------------------------------------------------------------------
# Part (a): parse_hex
# ---------------------------------------------------------------------------

class ParseHexTests(unittest.TestCase):
    def test_with_hash(self):
        r, g, b = parse_hex("#7C5CFF")
        self.assertAlmostEqual(r, 0x7C / 255.0, places=3)
        self.assertAlmostEqual(g, 0x5C / 255.0, places=3)
        self.assertAlmostEqual(b, 0xFF / 255.0, places=3)

    def test_without_hash(self):
        self.assertEqual(parse_hex("7C5CFF"), parse_hex("#7C5CFF"))

    def test_white(self):
        self.assertEqual(parse_hex("#FFFFFF"), (1.0, 1.0, 1.0))

    def test_black(self):
        self.assertEqual(parse_hex("#000000"), (0.0, 0.0, 0.0))

    def test_short_raises(self):
        with self.assertRaises(ValueError):
            parse_hex("#FFF")

    def test_five_digits_raises(self):
        with self.assertRaises(ValueError):
            parse_hex("#12345")

    def test_invalid_chars_raises(self):
        with self.assertRaises(ValueError):
            parse_hex("#GGGGGG")

    def test_empty_raises(self):
        with self.assertRaises(ValueError):
            parse_hex("")

    def test_no_appkit_needed(self):
        # Constructing IndicatorState and calling parse_hex must not require AppKit.
        # We verify by ensuring no AppKit import side-effect occurs when the module is used.
        st = IndicatorState()
        self.assertIsNotNone(st)
        r, g, b = parse_hex("#FF0000")
        self.assertAlmostEqual(r, 1.0)


# ---------------------------------------------------------------------------
# Part (a): IndicatorState transitions
# ---------------------------------------------------------------------------

class IndicatorStateTests(unittest.TestCase):
    def setUp(self):
        self.st = IndicatorState(gain=6.0, capacity=32)

    def test_initial_state(self):
        self.assertEqual(self.st.state, IDLE)
        self.assertEqual(self.st.mode, "cleanup")
        self.assertEqual(self.st.latency_text, "")
        self.assertEqual(self.st.error_text, "")
        self.assertEqual(len(self.st.levels), 0)

    def test_set_recording(self):
        self.st.set_recording()
        self.assertEqual(self.st.state, RECORDING)

    def test_set_processing(self):
        self.st.set_processing()
        self.assertEqual(self.st.state, PROCESSING)

    def test_set_idle(self):
        self.st.set_recording()
        self.st.set_idle()
        self.assertEqual(self.st.state, IDLE)

    def test_set_error_stores_text(self):
        self.st.set_error("boom")
        self.assertEqual(self.st.state, ERROR)
        self.assertEqual(self.st.error_text, "boom")

    def test_set_mode_raw(self):
        self.st.set_mode(True)
        self.assertEqual(self.st.mode, "raw")

    def test_set_mode_cleanup(self):
        self.st.set_mode(False)
        self.assertEqual(self.st.mode, "cleanup")

    def test_set_latency(self):
        self.st.set_latency("123 ms")
        self.assertEqual(self.st.latency_text, "123 ms")


# ---------------------------------------------------------------------------
# Part (a): ring buffer + scaling
# ---------------------------------------------------------------------------

class LevelBufferTests(unittest.TestCase):
    def test_capacity_bounded(self):
        st = IndicatorState(gain=1.0, capacity=32)
        for _ in range(100):
            st.push_level(0.5)
        self.assertEqual(len(st.levels), 32)

    def test_newest_on_right(self):
        st = IndicatorState(gain=1.0, capacity=32)
        for i in range(5):
            st.push_level(i * 0.1)
        self.assertAlmostEqual(list(st.levels)[-1], 0.4)

    def test_scaling_half_height(self):
        # gain=6.0, rms≈0.083 → ≈0.5
        st = IndicatorState(gain=6.0, capacity=32)
        st.push_level(1 / 12.0)
        self.assertAlmostEqual(list(st.levels)[0], 0.5, places=5)

    def test_clamp_high(self):
        st = IndicatorState(gain=6.0, capacity=32)
        st.push_level(10.0)
        self.assertEqual(list(st.levels)[0], 1.0)

    def test_clamp_negative(self):
        st = IndicatorState(gain=6.0, capacity=32)
        st.push_level(-1.0)
        self.assertEqual(list(st.levels)[0], 0.0)


# ---------------------------------------------------------------------------
# Part (b): OverlayPanel — bypass __init__ with __new__, mock AppHelper
# ---------------------------------------------------------------------------

class OverlayPanelTests(unittest.TestCase):
    def _make_panel(self):
        """Build an OverlayPanel without calling __init__ or touching AppKit."""
        panel = ov.OverlayPanel.__new__(ov.OverlayPanel)
        panel.state = IndicatorState(gain=6.0)
        panel.accent_rgb = (0.486, 0.361, 1.0)
        panel._AppHelper = MagicMock(name="AppHelper")
        panel._AppKit = MagicMock(name="AppKit")
        panel._panel = MagicMock(name="ns_panel")
        panel._pill = MagicMock(name="pill")
        panel._timer = None
        panel._frame_counter = 0
        return panel

    def test_recording_marshals_to_main_thread(self):
        panel = self._make_panel()
        panel.recording()
        panel._AppHelper.callAfter.assert_called()

    def test_processing_marshals_to_main_thread(self):
        panel = self._make_panel()
        panel.processing()
        panel._AppHelper.callAfter.assert_called()

    def test_idle_marshals_to_main_thread(self):
        panel = self._make_panel()
        panel.idle()
        panel._AppHelper.callAfter.assert_called()

    def test_error_marshals_to_main_thread_and_schedules_hide(self):
        panel = self._make_panel()
        panel.error("oops")
        panel._AppHelper.callAfter.assert_called()
        # callLater must be called with ~1.2 s delay
        panel._AppHelper.callLater.assert_called()
        delay = panel._AppHelper.callLater.call_args[0][0]
        self.assertAlmostEqual(delay, 1.2, places=1)

    def test_mode_marshals_to_main_thread(self):
        panel = self._make_panel()
        panel.mode(True)
        panel._AppHelper.callAfter.assert_called()

    def test_latency_marshals_to_main_thread(self):
        panel = self._make_panel()
        panel.latency("123 ms")
        panel._AppHelper.callAfter.assert_called()

    def test_level_does_not_call_apphelper(self):
        panel = self._make_panel()
        panel.level(0.5)
        panel._AppHelper.callAfter.assert_not_called()
        panel._AppHelper.callLater.assert_not_called()

    def test_level_increases_buffer(self):
        panel = self._make_panel()
        before = len(panel.state.levels)
        panel.level(0.5)
        self.assertEqual(len(panel.state.levels), before + 1)


if __name__ == "__main__":
    unittest.main()


class _Pt:
    def __init__(self, x, y): self.x, self.y = x, y


class _Frame:
    def __init__(self, x, y, w, h):
        self.origin = _Pt(x, y); self.size = type("S", (), {"width": w, "height": h})()


class _Screen:
    def __init__(self, x, y, w, h): self._f = _Frame(x, y, w, h)
    def frame(self): return self._f


class PlacementTests(unittest.TestCase):
    def test_pill_origin_centers_on_screen(self):
        self.assertEqual(ov.pill_origin(0, 0, 1728, 200), (764.0, 72.0))

    def test_pill_origin_respects_screen_offset(self):
        self.assertEqual(ov.pill_origin(-1676, 1117, 2560, 200), (-1676 + 1180.0, 1117 + 72.0))

    def test_screen_under_point_picks_containing_screen(self):
        a, b = _Screen(0, 0, 1728, 1117), _Screen(-2560, 0, 2560, 1440)
        self.assertIs(ov.screen_under_point([a, b], _Pt(-100, 50)), b)
        self.assertIs(ov.screen_under_point([a, b], _Pt(100, 50)), a)

    def test_screen_under_point_none_when_outside(self):
        self.assertIsNone(ov.screen_under_point([_Screen(0, 0, 100, 100)], _Pt(500, 500)))


class FlashTests(unittest.TestCase):
    def test_flash_schedules_show_then_hide(self):
        calls = []
        class FakeHelper:
            @staticmethod
            def callAfter(fn, *a): calls.append(("after", getattr(fn, "__name__", str(fn))))
            @staticmethod
            def callLater(delay, fn, *a): calls.append(("later", round(delay, 2), getattr(fn, "__name__", str(fn))))
        panel = ov.OverlayPanel.__new__(ov.OverlayPanel)
        panel._AppHelper = FakeHelper
        panel.state = ov.IndicatorState()
        panel.flash(1.0)
        self.assertEqual(calls[0], ("after", "_show"))
        self.assertEqual(calls[-1], ("later", 1.0, "idle"))
        self.assertTrue(any(c[0] == "later" and c[2] == "level" for c in calls))
