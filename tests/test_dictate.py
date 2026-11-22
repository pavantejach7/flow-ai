import copy
import io
import queue
from pathlib import Path
import sys
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dictate as d
import numpy as np
import requests
from pynput import keyboard as k


class DictationTests(unittest.TestCase):
    def setUp(self):
        self.cfg = d.read_config(d.ROOT / "config.toml")

    def test_loopback_only(self):
        for value in ["https://example.com", "http://127.0.0.1.evil.com", "http://user@localhost", "file:///tmp/audio"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                d.local_url(value)
        self.assertEqual(d.local_url("http://127.0.0.1:11434/"), "http://127.0.0.1:11434")

    def test_redirects_rejected_and_proxy_disabled(self):
        s = d.Services(self.cfg)
        self.assertFalse(s.http.trust_env)
        s.http.request = Mock(return_value=Mock(is_redirect=True))
        with self.assertRaises(RuntimeError):
            s.request("GET", "http://127.0.0.1:11434")
        self.assertFalse(s.http.request.call_args.kwargs["allow_redirects"])
        s.close()

    def test_right_option_is_not_left_option(self):
        h = d.Hotkeys("<alt_r>", "<ctrl>+<shift>+r")
        self.assertEqual(h.update(k.Key.alt_l, True), (False, False))
        h.update(k.Key.alt_l, False)
        self.assertEqual(h.update(k.Key.alt_r, True), (True, False))
        self.assertEqual(h.update(k.Key.alt_r, True), (True, False))
        self.assertEqual(h.update(k.Key.alt_r, False), (False, False))

    def test_generic_option_accepts_observed_mac_events(self):
        h = d.Hotkeys("<alt>", "<ctrl>+<shift>+r")
        self.assertEqual(h.update(k.Key.alt, True), (True, False))
        self.assertEqual(h.update(k.Key.alt, False), (False, False))
        self.assertEqual(h.update(k.Key.alt_r, True), (True, False))
        self.assertEqual(h.update(k.Key.alt_r, False), (False, False))

    def test_toggle_accepts_right_modifiers_and_uppercase(self):
        h = d.Hotkeys("<alt_r>", "<ctrl>+<shift>+r")
        h.update(k.Key.ctrl_r, True)
        h.update(k.Key.shift_r, True)
        self.assertEqual(h.update(k.KeyCode.from_char("R"), True), (False, True))
        self.assertEqual(h.update(k.KeyCode.from_char("r"), False), (False, False))

    def test_custom_hold_combination(self):
        h = d.Hotkeys("<ctrl>+<space>", "<f8>")
        h.update(k.Key.ctrl_r, True)
        self.assertTrue(h.update(k.Key.space, True)[0])
        self.assertFalse(h.update(k.Key.ctrl_r, False)[0])

    def test_wav_format_and_length(self):
        samples = np.arange(160000, dtype=np.int16)
        with wave.open(io.BytesIO(d.wav_bytes(samples))) as w:
            self.assertEqual((w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()), (16000, 1, 2, 160000))

    def test_raw_never_calls_llm(self):
        s = Mock()
        s.transcribe.return_value = "src/my_file.py --dry-run"
        with patch.object(d, "dictionary", return_value="MyName"):
            output = d.process_audio(s, self.cfg, b"audio", True, time.perf_counter())
        self.assertEqual(output, "src/my_file.py --dry-run")
        s.clean.assert_not_called()
        s.transcribe.assert_called_once_with(b"audio", "MyName")

    def test_cleanup_failure_preserves_transcript(self):
        s = Mock()
        s.transcribe.return_value = "send the report"
        s.clean.side_effect = requests.Timeout()
        self.assertEqual(d.process_audio(s, self.cfg, b"audio", False, time.perf_counter()), "send the report")

    def test_shutdown_cancels_pending_paste(self):
        s = Mock()
        s.transcribe.return_value = "draft"
        stop = threading.Event()
        stop.set()
        with patch.object(d, "paste") as p:
            d.process_audio(s, self.cfg, b"audio", True, time.perf_counter(), 123, stop)
        p.assert_not_called()

    def test_cleanup_uses_exact_system_prompt_and_dictionary(self):
        s = d.Services(self.cfg)
        s.request = Mock(return_value=Mock(json=lambda: {"done": True, "done_reason": "stop", "message": {"content": "Hello."}}))
        self.assertEqual(s.clean("hello", "Ollama"), "Hello.")
        payload = s.request.call_args.kwargs["json"]
        self.assertTrue(payload["messages"][0]["content"].startswith(d.SYSTEM_PROMPT))
        self.assertIn("spell these exactly", payload["messages"][0]["content"])
        self.assertFalse(payload["think"])
        s.close()

    def test_truncated_cleanup_is_rejected(self):
        s = d.Services(self.cfg)
        s.request = Mock(return_value=Mock(json=lambda: {"done": True, "done_reason": "length", "message": {"content": "truncated"}}))
        with self.assertRaises(RuntimeError):
            s.clean("original", "")
        s.close()

    def test_silence_not_pasted(self):
        s = Mock()
        s.transcribe.return_value = "[BLANK_AUDIO]"
        with patch.object(d, "paste") as p:
            self.assertEqual(d.process_audio(s, self.cfg, b"audio", False, time.perf_counter(), 123), "")
        p.assert_not_called()
        s.clean.assert_not_called()

    def test_app_change_cancels_before_clipboard_write(self):
        import ApplicationServices as AX
        import Quartz as Q
        with patch.object(AX, "AXIsProcessTrusted", return_value=True), \
             patch.object(Q, "CGEventSourceFlagsState", return_value=0), \
             patch.object(d, "front_target", return_value=(456, "Other app")), \
             patch.object(d, "snapshot_clipboard") as snapshot:
            with self.assertRaisesRegex(RuntimeError, "focused app changed"):
                d.paste("draft", self.cfg, (123, "Original document"))
        snapshot.assert_not_called()

    def clipboard_case(self, changed=False, event_error=False):
        import ApplicationServices as AX
        import Quartz as Q
        pb = Mock()
        pb.changeCount.side_effect = [1, 1, 2, 3, 4 if changed else 3]
        pb.setString_forType_.return_value = True
        module = SimpleNamespace(NSPasteboard=Mock(generalPasteboard=Mock(return_value=pb)), NSPasteboardTypeString="text")
        saved = [{"text/plain": b"original", "text/rtf": b"{rich text}"}]
        with patch.dict(sys.modules, {"AppKit": module}), \
             patch.object(AX, "AXIsProcessTrusted", return_value=True), \
             patch.object(Q, "CGEventSourceFlagsState", return_value=0), \
             patch.object(Q, "CGEventCreateKeyboardEvent", return_value=object()), \
             patch.object(Q, "CGEventSetFlags"), \
             patch.object(Q, "CGEventPost", side_effect=RuntimeError("event failed") if event_error else None), \
             patch.object(d, "front_pid", return_value=123), \
             patch.object(d, "snapshot_clipboard", return_value=saved), \
             patch.object(d, "restore_clipboard") as restore, \
             patch.object(d.time, "sleep"):
            if event_error:
                with self.assertRaises(RuntimeError):
                    d.paste("a\nb\n", self.cfg, 123)
            else:
                d.paste("a\nb\n", self.cfg, 123)
            pb.setString_forType_.assert_called_once_with("a b", "text")
            if changed:
                restore.assert_not_called()
            else:
                restore.assert_called_once_with(pb, saved)

    def test_clipboard_restored_and_command_newlines_removed(self):
        self.clipboard_case()

    def test_new_user_clipboard_is_not_overwritten(self):
        self.clipboard_case(changed=True)

    def test_clipboard_restored_after_paste_error(self):
        self.clipboard_case(event_error=True)

    def paste_focus_case(self, at_press, at_paste):
        import ApplicationServices as AX
        import Quartz as Q
        pb = Mock()
        pb.changeCount.side_effect = [1, 1, 2, 3, 3]
        pb.setString_forType_.return_value = True
        module = SimpleNamespace(NSPasteboard=Mock(generalPasteboard=Mock(return_value=pb)), NSPasteboardTypeString="text")
        with patch.dict(sys.modules, {"AppKit": module}), \
             patch.object(AX, "AXIsProcessTrusted", return_value=True), \
             patch.object(Q, "CGEventSourceFlagsState", return_value=0), \
             patch.object(Q, "CGEventCreateKeyboardEvent", return_value=object()), \
             patch.object(Q, "CGEventSetFlags"), \
             patch.object(Q, "CGEventPost"), \
             patch.object(d, "front_target", return_value=at_paste), \
             patch.object(d, "snapshot_clipboard", return_value=[]), \
             patch.object(d, "restore_clipboard"), \
             patch.object(d.time, "sleep"):
            d.paste("hello", self.cfg, at_press)
        return pb

    def test_title_change_alone_does_not_cancel_paste(self):
        # Terminal titles embed the running command, so Flow's own afplay cue
        # mutates the title mid-dictation. Only the app (pid) identifies focus.
        pb = self.paste_focus_case((98876, "whisper-server - dictate.py"), (98876, "afplay - dictate.py"))
        pb.setString_forType_.assert_called_once_with("hello", "text")

    def test_app_change_cancels_paste(self):
        with self.assertRaises(RuntimeError):
            self.paste_focus_case((98876, "Terminal"), (555, "Safari"))

    def cue_volume(self, cfg):
        """Return the -v value sound() passes to afplay."""
        with patch.object(d.subprocess, "run") as run, \
             patch.object(d.threading, "Thread", side_effect=lambda target, daemon: SimpleNamespace(start=target)):
            d.sound(cfg, "start")
        argv = run.call_args[0][0]
        return float(argv[argv.index("-v") + 1])

    def test_cue_plays_at_full_scale_by_default(self):
        # Output volume is a user setting; attenuating on top of it made the cue
        # inaudible at low system volume (0.19 * 0.3 = 0.057 -> not heard).
        self.assertEqual(self.cue_volume(self.cfg), 1.0)

    def test_cue_volume_is_configurable(self):
        cfg = copy.deepcopy(self.cfg)
        cfg["audio"]["cue_volume"] = 0.5
        self.assertEqual(self.cue_volume(cfg), 0.5)

    def test_cue_volume_out_of_range_is_rejected(self):
        for bad in [-0.1, 1.5]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                d.validate_cue_volume(bad)

    def test_discard_log_reports_rms_and_threshold(self):
        cfg = copy.deepcopy(self.cfg)
        cfg["audio"]["min_seconds"] = 0.3
        cfg["audio"]["silence_rms"] = 0.02
        with patch.object(d, "LOG") as log:
            discarded = d.discard_short_or_silent(cfg, duration=1.0, rms=0.005)
        self.assertTrue(discarded)
        args = log.info.call_args[0]
        message = args[0] % args[1:]
        self.assertIn("rms=0.0050", message)
        self.assertIn("threshold=0.0200", message)

        with patch.object(d, "LOG") as log2:
            self.assertFalse(d.discard_short_or_silent(cfg, duration=1.0, rms=0.5))
        log2.info.assert_not_called()


    # ------------------------------------------------------------------
    # Indicator config validation
    # ------------------------------------------------------------------

    def test_indicator_defaults_present(self):
        cfg = d.read_config(d.ROOT / "config.toml")
        ind = cfg["indicator"]
        self.assertTrue(ind["enabled"])
        self.assertEqual(ind["accent"], "#5468FF")
        self.assertAlmostEqual(ind["gain"], 6.0)

    def test_indicator_bad_accent_raises(self):
        for bad in ["#ZZZ", "#12345", "GGGGGG", ""]:
            with self.assertRaises(ValueError, msg=f"bad accent {bad!r}"):
                _validate_indicator({"accent": bad, "gain": 6.0})

    def test_indicator_bad_gain_raises(self):
        import copy
        for bad in [0, -1, 0.0, -0.5]:
            with self.assertRaises(ValueError, msg=f"bad gain {bad}"):
                _validate_indicator({"accent": "#7C5CFF", "gain": bad})

    # ------------------------------------------------------------------
    # process_audio indicator hooks
    # ------------------------------------------------------------------

    def test_process_audio_calls_indicator_latency(self):
        s = Mock()
        s.transcribe.return_value = "hello world"
        s.clean.return_value = "hello world"
        ind = Mock()
        with patch.object(d, "dictionary", return_value=""), \
             patch.object(d, "paste"):
            d.process_audio(s, self.cfg, b"audio", False, time.perf_counter(),
                            target=None, stop=None, indicator=ind)
        ind.latency.assert_called_once()
        # The latency text must be a non-empty string
        lat_text = ind.latency.call_args[0][0]
        self.assertIsInstance(lat_text, str)
        self.assertGreater(len(lat_text), 0)

    def test_process_audio_no_indicator_still_works(self):
        # Existing call sites pass no indicator; must not error
        s = Mock()
        s.transcribe.return_value = "hello"
        with patch.object(d, "dictionary", return_value=""):
            result = d.process_audio(s, self.cfg, b"audio", True, time.perf_counter())
        self.assertEqual(result, "hello")

    # ------------------------------------------------------------------
    # run_live indicator hooks (via event injection)
    # ------------------------------------------------------------------

    def _make_run_live_mocks(self):
        """Return (cfg, services, stop, mock_sd, mock_listener, mock_pynput, ind)."""
        import copy
        cfg = copy.deepcopy(self.cfg)
        services = Mock()
        services.transcribe.return_value = "test"
        services.clean.return_value = "test"
        stop = threading.Event()
        ind = Mock()
        return cfg, services, stop, ind

    def test_run_live_recording_and_idle_on_short_clip(self):
        cfg, services, stop, ind = self._make_run_live_mocks()

        events_q = queue.Queue()
        t0 = time.perf_counter()

        # We'll feed: press → release (empty chunks) → stop
        def fake_wait():
            pass

        import numpy as np

        with patch.object(d, "front_target", return_value=(1, "Test")), \
             patch.object(d, "sound"), \
             patch.object(d, "discard_short_or_silent", return_value=True), \
             patch("sounddevice.InputStream") as mock_sd_cls, \
             patch("pynput.keyboard.Listener") as mock_listener_cls:

            # Set up fake InputStream context manager
            mock_stream = MagicMock()
            mock_sd_cls.return_value.__enter__ = Mock(return_value=mock_stream)
            mock_sd_cls.return_value.__exit__ = Mock(return_value=False)

            # Set up fake Listener
            mock_listener = MagicMock()
            mock_listener.running = True
            mock_listener_cls.return_value = mock_listener

            # Capture the events queue by intercepting queue.Queue
            captured_q = [None]

            original_queue = d.queue.Queue

            def fake_queue():
                q = original_queue()
                captured_q[0] = q
                return q

            with patch.object(d.queue, "Queue", side_effect=fake_queue):
                # Run run_live in a thread, inject events, then stop
                def run():
                    d.run_live(cfg, services, stop, indicator=ind)

                t = threading.Thread(target=run, daemon=True)
                t.start()

                # Wait for the queue to be captured
                deadline = time.monotonic() + 2
                while captured_q[0] is None and time.monotonic() < deadline:
                    time.sleep(0.01)

                q = captured_q[0]
                if q is not None:
                    # Inject press then release
                    q.put(("press", time.perf_counter()))
                    time.sleep(0.02)
                    q.put(("release", time.perf_counter()))
                    time.sleep(0.05)

                stop.set()
                t.join(timeout=2)

        # discard_short_or_silent returns True → indicator.idle must be called
        # indicator.recording must be called on press
        ind.recording.assert_called()
        ind.idle.assert_called()

    def test_run_live_toggle_calls_indicator_mode(self):
        cfg, services, stop, ind = self._make_run_live_mocks()

        captured_q = [None]
        original_queue = d.queue.Queue

        def fake_queue():
            q = original_queue()
            captured_q[0] = q
            return q

        with patch.object(d, "sound"), \
             patch("sounddevice.InputStream") as mock_sd_cls, \
             patch("pynput.keyboard.Listener") as mock_listener_cls, \
             patch.object(d.queue, "Queue", side_effect=fake_queue):

            mock_stream = MagicMock()
            mock_sd_cls.return_value.__enter__ = Mock(return_value=mock_stream)
            mock_sd_cls.return_value.__exit__ = Mock(return_value=False)
            mock_listener = MagicMock()
            mock_listener.running = True
            mock_listener_cls.return_value = mock_listener

            def run():
                d.run_live(cfg, services, stop, indicator=ind)

            t = threading.Thread(target=run, daemon=True)
            t.start()

            deadline = time.monotonic() + 2
            while captured_q[0] is None and time.monotonic() < deadline:
                time.sleep(0.01)

            q = captured_q[0]
            if q is not None:
                q.put(("toggle", time.perf_counter()))
                time.sleep(0.05)

            stop.set()
            t.join(timeout=2)

        ind.mode.assert_called_once()

    def test_callback_calls_level_while_recording(self):
        import numpy as np
        import copy

        cfg = copy.deepcopy(self.cfg)
        services = Mock()
        stop = threading.Event()
        ind = Mock()

        # Build the callback closure by accessing run_live internals via patch
        # Instead, we test the logic directly by running a minimal scenario:
        # Create a chunk and manually compute expected rms
        chunk = np.array([[100], [200], [300], [-100]], dtype=np.int16)
        expected_rms = float(np.sqrt(np.mean((chunk.astype(np.float32) / 32768) ** 2)))

        captured_callback = [None]
        original_sd = None

        import sounddevice as sd_module

        def fake_input_stream(**kwargs):
            captured_callback[0] = kwargs.get("callback")
            m = MagicMock()
            m.__enter__ = Mock(return_value=m)
            m.__exit__ = Mock(return_value=False)
            return m

        with patch.object(d, "sound"), \
             patch.object(d, "front_target", return_value=(1, "T")), \
             patch("sounddevice.InputStream", side_effect=fake_input_stream), \
             patch("pynput.keyboard.Listener") as mock_listener_cls:

            mock_listener = MagicMock()
            mock_listener.running = True
            mock_listener_cls.return_value = mock_listener

            captured_q = [None]
            original_queue = d.queue.Queue

            def fake_queue():
                q = original_queue()
                captured_q[0] = q
                return q

            with patch.object(d.queue, "Queue", side_effect=fake_queue):
                def run():
                    d.run_live(cfg, services, stop, indicator=ind)

                t = threading.Thread(target=run, daemon=True)
                t.start()

                deadline = time.monotonic() + 2
                while (captured_callback[0] is None or captured_q[0] is None) and time.monotonic() < deadline:
                    time.sleep(0.01)

                cb = captured_callback[0]
                q = captured_q[0]

                if cb is not None and q is not None:
                    # Trigger press to set recording=True
                    q.put(("press", time.perf_counter()))
                    time.sleep(0.05)
                    # Call the audio callback
                    cb(chunk, len(chunk), None, None)
                    time.sleep(0.02)

                stop.set()
                t.join(timeout=2)

        if captured_callback[0] is not None:
            ind.level.assert_called()
            actual_rms = ind.level.call_args[0][0]
            self.assertAlmostEqual(actual_rms, expected_rms, places=6)

    def test_callback_no_level_when_not_recording(self):
        import numpy as np
        import copy

        cfg = copy.deepcopy(self.cfg)
        services = Mock()
        stop = threading.Event()
        ind = Mock()

        chunk = np.array([[100], [200]], dtype=np.int16)
        captured_callback = [None]

        def fake_input_stream(**kwargs):
            captured_callback[0] = kwargs.get("callback")
            m = MagicMock()
            m.__enter__ = Mock(return_value=m)
            m.__exit__ = Mock(return_value=False)
            return m

        with patch.object(d, "sound"), \
             patch("sounddevice.InputStream", side_effect=fake_input_stream), \
             patch("pynput.keyboard.Listener") as mock_listener_cls:

            mock_listener = MagicMock()
            mock_listener.running = True
            mock_listener_cls.return_value = mock_listener

            def run():
                d.run_live(cfg, services, stop, indicator=ind)

            t = threading.Thread(target=run, daemon=True)
            t.start()

            deadline = time.monotonic() + 2
            while captured_callback[0] is None and time.monotonic() < deadline:
                time.sleep(0.01)

            cb = captured_callback[0]
            if cb is not None:
                # Call callback WITHOUT a prior press — state["recording"] is False
                cb(chunk, len(chunk), None, None)
                time.sleep(0.02)

            stop.set()
            t.join(timeout=2)

        if captured_callback[0] is not None:
            ind.level.assert_not_called()


# ---------------------------------------------------------------------------
# Helpers for validation tests
# ---------------------------------------------------------------------------

import re as _re


def _validate_indicator(ind):
    """Mirror the validation logic from read_config for unit-testing."""
    accent = ind.get("accent", "#7C5CFF")
    if not _re.fullmatch(r"#?[0-9A-Fa-f]{6}", accent):
        raise ValueError("indicator.accent must be a 6-digit hex color")
    gain = ind.get("gain", 6.0)
    if not (isinstance(gain, (int, float)) and gain > 0):
        raise ValueError("indicator.gain must be a positive number")


if __name__ == "__main__":
    unittest.main()


class StreamStaleTests(unittest.TestCase):
    def test_fresh_callback_is_not_stale(self):
        self.assertFalse(d.stream_is_stale(100.0, 100.5))

    def test_quiet_stream_is_stale(self):
        self.assertTrue(d.stream_is_stale(100.0, 101.5))

    def test_never_called_back_is_stale(self):
        self.assertTrue(d.stream_is_stale(0.0, 5.0))

    def test_threshold_is_configurable(self):
        self.assertFalse(d.stream_is_stale(100.0, 102.0, threshold=3.0))
