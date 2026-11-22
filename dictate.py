#!/usr/bin/env python3
"""Local hold-to-talk dictation for macOS. See README.md for setup and permissions."""
from __future__ import annotations

import argparse
import fcntl
import io
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import queue
import re
import signal
import subprocess
import sys
import threading
import time
import tomllib
from urllib.parse import urlparse
import wave

import numpy as np
import requests

ROOT = Path(__file__).resolve().parent
SYSTEM_PROMPT = (
    "Clean up this dictated text: fix punctuation and capitalization, remove filler "
    "words (um, uh, like), apply self-corrections ('scratch that', 'I mean'). Keep my "
    "wording and meaning. Do NOT add content. Output only the cleaned text."
)
LOG = logging.getLogger("flow")


def local_url(value):
    p = urlparse(value)
    if p.scheme != "http" or p.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("Only loopback HTTP endpoints are allowed")
    if p.username or p.password or p.query or p.fragment or p.path not in ("", "/"):
        raise ValueError("Expected a plain loopback server URL")
    return value.rstrip("/")


def path(value):
    p = Path(value).expanduser()
    return p if p.is_absolute() else ROOT / p


def validate_cue_volume(value):
    if not 0 <= value <= 1:
        raise ValueError("cue_volume must be between 0 and 1")
    return value


def read_config(filename):
    with open(filename, "rb") as f:
        cfg = tomllib.load(f)
    cfg["ollama"]["url"] = local_url(cfg["ollama"]["url"])
    if cfg["whisper"]["host"] != "127.0.0.1":
        raise ValueError("Whisper must bind to 127.0.0.1")
    if "cloud" in cfg["ollama"]["model"].lower():
        raise ValueError("Cloud models are not allowed")
    if cfg["audio"]["sample_rate"] != 16000:
        raise ValueError("Whisper input must be 16000 Hz")
    if not 0.1 <= cfg["dictation"]["clipboard_restore_seconds"] <= 5:
        raise ValueError("clipboard_restore_seconds must be between 0.1 and 5")
    if not 1 <= cfg["audio"]["max_seconds"] <= 600:
        raise ValueError("max_seconds must be between 1 and 600")
    validate_cue_volume(cfg["audio"].setdefault("cue_volume", 1.0))
    ind = cfg.get("indicator", {})
    accent = ind.get("accent", "#7C5CFF")
    if not re.fullmatch(r"#?[0-9A-Fa-f]{6}", accent):
        raise ValueError("indicator.accent must be a 6-digit hex color")
    gain = ind.get("gain", 6.0)
    if not (isinstance(gain, (int, float)) and gain > 0):
        raise ValueError("indicator.gain must be a positive number")
    return cfg


def configure_log(cfg):
    handler = RotatingFileHandler(path(cfg["dictation"]["log"]), maxBytes=2_000_000, backupCount=2)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    handler.setFormatter(fmt)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    LOG.setLevel(logging.INFO)
    LOG.addHandler(handler)
    LOG.addHandler(console)


def dictionary(cfg):
    terms = [s.strip() for s in path(cfg["dictation"]["dictionary"]).read_text().splitlines()
             if s.strip() and not s.lstrip().startswith("#")]
    # Whisper has a small initial-prompt budget. Fail visibly instead of silently truncating.
    text = ", ".join(terms)
    if len(text) > 600:
        raise ValueError("Keep dictionary.txt under 600 characters of active terms")
    return text


def permissions(request=False):
    import ApplicationServices as AX
    import AVFoundation as AV
    import Quartz
    if request:
        AX.AXIsProcessTrustedWithOptions({AX.kAXTrustedCheckOptionPrompt: True})
        Quartz.CGRequestListenEventAccess()
        if AV.AVCaptureDevice.authorizationStatusForMediaType_(AV.AVMediaTypeAudio) == 0:
            done = threading.Event()
            AV.AVCaptureDevice.requestAccessForMediaType_completionHandler_(
                AV.AVMediaTypeAudio, lambda granted: done.set())
            done.wait(30)
    return {
        "microphone": AV.AVCaptureDevice.authorizationStatusForMediaType_(AV.AVMediaTypeAudio) == 3,
        "accessibility": bool(AX.AXIsProcessTrusted()),
        "input_monitoring": bool(Quartz.CGPreflightListenEventAccess()),
    }


class Services:
    """Own only processes started by this instance; preserve the user's existing Ollama."""
    def __init__(self, cfg):
        self.cfg = cfg
        self.http = requests.Session()
        self.http.trust_env = False  # No proxies, netrc, or remote fallback.
        self.children = []
        self.handles = []
        self.whisper = f"http://127.0.0.1:{cfg['whisper']['port']}"

    def request(self, method, url, **kwargs):
        r = self.http.request(method, url, allow_redirects=False, **kwargs)
        if r.is_redirect:
            raise RuntimeError("Local service tried to redirect a request")
        r.raise_for_status()
        return r

    def ready(self, url):
        try:
            return self.request("GET", url, timeout=1).status_code == 200
        except requests.RequestException:
            return False

    def spawn(self, command, name, env=None):
        f = open(ROOT / ".run" / f"{name}.log", "a")
        self.handles.append(f)
        p = subprocess.Popen(command, stdout=f, stderr=f, stdin=subprocess.DEVNULL, env=env)
        self.children.append(p)
        return p

    def wait_ready(self, url, child):
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if child.poll() is not None:
                raise RuntimeError("Local service exited; inspect .run service logs")
            if self.ready(url):
                return
            time.sleep(0.15)
        raise TimeoutError("Local service startup timed out")

    def start(self, cleanup=True):
        w = self.cfg["whisper"]
        if not path(w["model"]).is_file():
            raise FileNotFoundError(path(w["model"]))
        if self.ready(self.whisper + "/health"):
            raise RuntimeError("Whisper port already in use; stop that server or change the port")
        child = self.spawn([w["binary"], "-m", str(path(w["model"])), "--host", "127.0.0.1",
                            "--port", str(w["port"]), "-t", str(w["threads"]), "-bo", "1",
                            "-bs", "1", "-nf", "-l", w["language"]], "whisper-server")
        self.wait_ready(self.whisper + "/health", child)
        # Warm the encoder and Metal kernels without storing microphone audio.
        self.transcribe(wav_bytes(np.zeros(16000, dtype=np.int16)), "")
        if cleanup:
            o = self.cfg["ollama"]
            if not self.ready(o["url"] + "/api/tags"):
                env = dict(os.environ, OLLAMA_HOST="127.0.0.1:11434", OLLAMA_NO_CLOUD="1")
                if urlparse(o["url"]).port != 11434:
                    raise RuntimeError("Start the custom Ollama endpoint yourself")
                child = self.spawn([o["binary"], "serve"], "ollama", env)
                self.wait_ready(o["url"] + "/api/tags", child)
            info = self.request("POST", o["url"] + "/api/show", json={"model": o["model"]}, timeout=10).json()
            if info.get("remote_model") or info.get("remote_host"):
                raise RuntimeError("Ollama model is remote; local weights are required")
            self.clean("hello", dictionary(self.cfg))
        LOG.info("Models ready: whisper=%s ollama=%s", Path(w["model"]).name, self.cfg["ollama"]["model"])

    def transcribe(self, audio, terms):
        r = self.request("POST", self.whisper + "/inference", timeout=(3, 90),
                         files={"file": ("dictation.wav", audio, "audio/wav")},
                         data={"response_format": "json", "language": self.cfg["whisper"]["language"],
                               "prompt": terms, "temperature": "0", "temperature_inc": "0",
                               "best_of": "1", "beam_size": "1"})
        return r.json()["text"].strip()

    def clean(self, text, terms):
        o = self.cfg["ollama"]
        system = SYSTEM_PROMPT
        if terms:
            system += "\nPersonal dictionary: spell these exactly when mentioned: " + terms
        r = self.request("POST", o["url"] + "/api/chat", timeout=(3, o["timeout_seconds"]), json={
            "model": o["model"], "stream": False, "think": False, "keep_alive": o["keep_alive"],
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": text}],
            "options": {"temperature": 0, "num_ctx": o["num_ctx"], "num_predict": o["num_predict"]},
        }).json()
        if not r.get("done") or r.get("done_reason") == "length":
            raise RuntimeError("Cleanup was truncated")
        output = r["message"]["content"].strip()
        if not output or "<think>" in output or len(output) > max(150, len(text) * 3):
            raise RuntimeError("Cleanup returned an empty or unexpectedly expanded result")
        return output

    def close(self):
        for p in reversed(self.children):
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait()
        for f in self.handles:
            f.close()
        self.http.close()


def wav_bytes(samples):
    b = io.BytesIO()
    with wave.open(b, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(samples.astype("<i2", copy=False).tobytes())
    return b.getvalue()


def front_pid():
    from AppKit import NSWorkspace
    return NSWorkspace.sharedWorkspace().frontmostApplication().processIdentifier()


def front_target():
    import ApplicationServices as AX
    pid = front_pid()
    app = AX.AXUIElementCreateApplication(pid)
    error, window = AX.AXUIElementCopyAttributeValue(app, AX.kAXFocusedWindowAttribute, None)
    if error or window is None:
        return pid, None
    error, title = AX.AXUIElementCopyAttributeValue(window, AX.kAXTitleAttribute, None)
    return pid, None if error else str(title)


def snapshot_clipboard(pb):
    result = []
    for item in pb.pasteboardItems() or []:
        values = {}
        for kind in item.types():
            data = item.dataForType_(kind)
            if data is None:
                raise RuntimeError("Clipboard has an unavailable promised item; leaving it unchanged")
            values[str(kind)] = bytes(data)
        result.append(values)
    return result


def restore_clipboard(pb, saved):
    from AppKit import NSPasteboardItem
    from Foundation import NSData
    items = []
    for values in saved:
        item = NSPasteboardItem.alloc().init()
        for kind, data in values.items():
            item.setData_forType_(NSData.dataWithBytes_length_(data, len(data)), kind)
        items.append(item)
    pb.clearContents()
    if items and not pb.writeObjects_(items):
        raise RuntimeError("Could not restore clipboard items")


def paste(text, cfg, expected_pid):
    from AppKit import NSPasteboard, NSPasteboardTypeString
    import Quartz as Q
    import ApplicationServices as AX
    if not AX.AXIsProcessTrusted():
        raise PermissionError("Accessibility permission is required to paste")
    # A held modifier can turn Cmd+V into a different shortcut. Give it time to release.
    deadline = time.monotonic() + 2
    mask = Q.kCGEventFlagMaskShift | Q.kCGEventFlagMaskControl | Q.kCGEventFlagMaskAlternate | Q.kCGEventFlagMaskCommand
    while Q.CGEventSourceFlagsState(Q.kCGEventSourceStateCombinedSessionState) & mask:
        if time.monotonic() > deadline:
            raise RuntimeError("Paste cancelled: release modifier keys")
        time.sleep(0.01)
    current = front_target() if isinstance(expected_pid, tuple) else front_pid()
    if cfg["dictation"]["cancel_if_app_changes"]:
        # Compare app identity (pid) only. Window titles are unstable mid-dictation:
        # Terminal titles embed the running command (including our own afplay cue),
        # browsers retitle on page changes, editors add dirty markers.
        expected_app = expected_pid[0] if isinstance(expected_pid, tuple) else expected_pid
        current_app = current[0] if isinstance(current, tuple) else current
        if current_app != expected_app:
            raise RuntimeError(
                f"Paste cancelled: focused app changed during transcription "
                f"(expected {expected_pid!r}, found {current!r})")
        if current != expected_pid:
            LOG.info("Window title changed during transcription (%r -> %r); pasting anyway",
                     expected_pid, current)
    pb = NSPasteboard.generalPasteboard()
    before = pb.changeCount()
    saved = snapshot_clipboard(pb)
    if pb.changeCount() != before:
        raise RuntimeError("Clipboard changed while saving; paste cancelled")
    # No trailing newline or Return event: never auto-submit a Terminal/Claude prompt.
    text = text.strip().replace("\x00", "")
    if cfg["dictation"].get("single_line", True):
        text = " ".join(text.splitlines())
    pb.clearContents()
    own = pb.changeCount()
    try:
        if not pb.setString_forType_(text, NSPasteboardTypeString):
            raise RuntimeError("Could not place text on clipboard")
        own = pb.changeCount()
        for down in (True, False):
            event = Q.CGEventCreateKeyboardEvent(None, 9, down)  # macOS virtual key V
            Q.CGEventSetFlags(event, Q.kCGEventFlagMaskCommand)
            Q.CGEventPost(Q.kCGHIDEventTap, event)
        time.sleep(cfg["dictation"]["clipboard_restore_seconds"])
    finally:
        # Preserve a new copy the user made while paste was in flight.
        if pb.changeCount() == own:
            restore_clipboard(pb, saved)
        else:
            LOG.info("Clipboard changed externally; retaining the newer clipboard")


def sound(cfg, kind):
    if cfg["audio"]["sounds"]:
        filename = {"start": "Pop", "stop": "Tink", "raw": "Pop", "clean": "Tink", "error": "Basso"}[kind]
        # Play at full scale: afplay is already scaled by the system output volume,
        # so attenuating here too made the cue inaudible at low output volume.
        volume = cfg["audio"].get("cue_volume", 1.0)
        # Reap promptly without blocking audio capture or key callbacks.
        threading.Thread(target=lambda: subprocess.run(
            ["/usr/bin/afplay", "-v", str(volume), f"/System/Library/Sounds/{filename}.aiff"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL), daemon=True).start()


def process_audio(services, cfg, audio, raw, released, target=None, stop=None, indicator=None):
    if indicator is None:
        indicator = NOOP_INDICATOR
    t = time.perf_counter()
    terms = dictionary(cfg)  # Reload on every dictation.
    text = services.transcribe(audio, terms)
    stt = time.perf_counter() - t
    cleanup = 0.0
    if not text or text.lower() in ("[blank_audio]", "[silence]", "(silence)"):
        LOG.info("No speech returned; nothing pasted")
        return ""
    if not raw:
        t = time.perf_counter()
        try:
            text = services.clean(text, terms)
        except (requests.RequestException, RuntimeError, KeyError, ValueError) as exc:
            LOG.warning("Cleanup failed (%s); using original transcript", type(exc).__name__)
        cleanup = time.perf_counter() - t
    t = time.perf_counter()
    if target is not None and not (stop and stop.is_set()):
        paste(text, cfg, target)
    paste_time = time.perf_counter() - t
    lat_msg = "latency mode=%s whisper=%.3fs cleanup=%.3fs paste_restore=%.3fs release_to_complete=%.3fs"
    lat_args = ("raw" if raw else "cleanup", stt, cleanup, paste_time, time.perf_counter() - released)
    LOG.info(lat_msg, *lat_args)
    indicator.latency(lat_msg % lat_args)
    return text


class Hotkeys:
    """Track side-specific modifiers; pynput's canonical() would merge Right/Left Option."""
    def __init__(self, hold, toggle):
        from pynput import keyboard
        self.keyboard = keyboard
        self.hold = keyboard.HotKey.parse(hold)
        self.toggle = keyboard.HotKey.parse(toggle)
        if self.hold == self.toggle:
            raise ValueError("Hold and toggle shortcuts must differ")
        self.pressed = set()

    def active(self, combination):
        k = self.keyboard.Key
        groups = {k.ctrl: {k.ctrl, k.ctrl_l, k.ctrl_r}, k.alt: {k.alt, k.alt_l, k.alt_r},
                  k.shift: {k.shift, k.shift_l, k.shift_r}, k.cmd: {k.cmd, k.cmd_l, k.cmd_r}}
        def matches(key):
            if key in groups:
                return bool(groups[key] & self.pressed)
            vk = getattr(key, "vk", None)
            if vk is not None:
                return any(getattr(getattr(p, "value", p), "vk", None) == vk for p in self.pressed)
            return key in self.pressed
        return all(matches(key) for key in combination)

    def update(self, key, down):
        if getattr(key, "char", None):
            key = self.keyboard.KeyCode.from_char(key.char.lower())
        if down:
            self.pressed.add(key)
        else:
            self.pressed.discard(key)
        return self.active(self.hold), self.active(self.toggle)


def test_hotkeys(cfg):
    """Report modifiers and shortcut matches only; never print ordinary typed text."""
    from pynput import keyboard
    access = permissions()
    if not access["accessibility"] or not access["input_monitoring"]:
        raise PermissionError(f"Host-app keyboard permissions missing: {access}")
    keys = Hotkeys(cfg["hotkeys"]["hold"], cfg["hotkeys"]["toggle_raw"])
    previous = [False, False]
    count = [0]

    def receive(key, down):
        count[0] += 1
        vk = getattr(getattr(key, "value", key), "vk", None)
        if vk in (54, 55, 56, 58, 59, 60, 61, 62, 63):
            print(f"Modifier: {key} {'DOWN' if down else 'UP'}", flush=True)
        state = keys.update(key, down)
        if tuple(previous) != state:
            print(f"Shortcut state: hold={state[0]}, toggle={state[1]}", flush=True)
        previous[:] = state

    with keyboard.Listener(on_press=lambda key: receive(key, True),
                           on_release=lambda key: receive(key, False)) as listener:
        listener.wait()
        print("For 60 seconds: press/release Right Option, Left Option, then Left Shift. No audio or paste.", flush=True)
        time.sleep(60)
    print(f"Received {count[0]} keyboard events; ordinary key text was not logged.")


class _NoIndicator:
    """Drop-in no-op for all indicator methods."""
    def recording(self): pass
    def level(self, rms): pass
    def processing(self): pass
    def idle(self): pass
    def error(self, text=""): pass
    def mode(self, raw): pass
    def latency(self, text): pass


NOOP_INDICATOR = _NoIndicator()


def discard_short_or_silent(cfg, duration, rms):
    threshold = cfg["audio"]["silence_rms"]
    if duration < cfg["audio"]["min_seconds"] or rms < threshold:
        LOG.info("Short/silent clip discarded: %.2fs rms=%.4f threshold=%.4f",
                 duration, rms, threshold)
        return True
    return False


STREAM_STALE_SECONDS = 1.0
STREAM_WATCHDOG_SECONDS = 2.0   # Idle-loop check interval for a dead microphone stream.
STREAM_RETRY_SECONDS = 15       # Back off this long after a failed reopen (mic truly unavailable).


def stream_is_stale(last_callback, now, threshold=STREAM_STALE_SECONDS):
    """True when the audio callback has not fired for `threshold` seconds.

    PortAudio keeps calling back continuously while the stream is healthy, so a gap
    means Core Audio dropped the device (sleep/wake, headphones plugged in or out).
    """
    return (now - last_callback) > threshold


def run_live(cfg, services, stop, indicator=None):
    if indicator is None:
        indicator = NOOP_INDICATOR
    import sounddevice as sd
    from pynput import keyboard
    keys = Hotkeys(cfg["hotkeys"]["hold"], cfg["hotkeys"]["toggle_raw"])
    events = queue.Queue()
    audio_lock = threading.Lock()
    state = {"recording": False, "chunks": [], "count": 0, "overflow": False, "limit": False,
             "last_callback": 0.0}
    previous = [False, False]
    raw = not cfg["dictation"]["cleanup"]
    busy = False
    worker = None
    target = None
    clip_raw = raw
    max_samples = int(cfg["audio"]["max_seconds"] * 16000)

    def key_event(key, down):
        hold, toggle = keys.update(key, down)
        if toggle and not previous[1]:
            events.put(("toggle", time.perf_counter()))
        if hold != previous[0]:
            events.put(("press" if hold else "release", time.perf_counter()))
        previous[:] = [hold, toggle]

    def callback(indata, frames, timing, status):
        with audio_lock:
            state["last_callback"] = time.monotonic()
            if state["recording"]:
                state["overflow"] |= bool(status)
                count = min(frames, max_samples - state["count"])
                chunk = indata[:count]
                state["chunks"].append(chunk.copy())
                state["count"] += count
                chunk_rms = float(np.sqrt(np.mean((chunk.astype(np.float32) / 32768) ** 2)))
                indicator.level(chunk_rms)
                if state["count"] >= max_samples and not state["limit"]:
                    state["limit"] = True
                    state["recording"] = False
                    events.put(("limit", time.perf_counter()))

    def work(samples, mode, released, pid):
        try:
            process_audio(services, cfg, wav_bytes(samples), mode, released, pid, stop, indicator)
            indicator.idle()
        except Exception as exc:
            LOG.error("Dictation failed: %s", str(exc))
            sound(cfg, "error")
            indicator.error(str(exc))
        finally:
            events.put(("done", time.perf_counter()))

    listener = keyboard.Listener(on_press=lambda k: key_event(k, True), on_release=lambda k: key_event(k, False))
    stream = [None]

    def open_stream():
        """(Re)open the microphone stream; Core Audio drops it after sleep or device changes."""
        if stream[0] is not None:
            try:
                stream[0].close()
            except Exception as exc:  # The old stream is already dead; nothing to salvage.
                LOG.debug("Closing stale stream failed: %s", exc)
            stream[0] = None
        stream[0] = sd.InputStream(samplerate=16000, channels=1, dtype="int16", blocksize=320,
                                   device=cfg["audio"]["device"] or None, callback=callback)
        stream[0].start()
        with audio_lock:
            state["last_callback"] = time.monotonic()

    retry_after = [0.0]  # Monotonic time before which we do not retry a failed reopen.

    def ensure_stream(background=False):
        """Reopen the stream when the callback has gone quiet. Returns False if it cannot.

        Called on every Option press and, with background=True, from the idle loop every
        STREAM_WATCHDOG_SECONDS so a dead stream is usually healed before the next press.
        """
        with audio_lock:
            stale = stream_is_stale(state["last_callback"], time.monotonic())
        if not stale:
            return True
        if time.monotonic() < retry_after[0]:
            return False
        LOG.warning("Audio input went quiet (device changed or Mac slept); reopening microphone stream%s",
                    " (watchdog)" if background else "")
        try:
            open_stream()
        except Exception as exc:
            retry_after[0] = time.monotonic() + STREAM_RETRY_SECONDS
            LOG.error("Could not reopen microphone: %s; retrying in %ds", exc, STREAM_RETRY_SECONDS)
            return False
        if background:
            # A reopen can "succeed" while the Mac is asleep or the mic is suspended and still deliver
            # no audio; do not churn every watchdog tick. A real press still retries immediately.
            retry_after[0] = time.monotonic() + STREAM_RETRY_SECONDS
        LOG.info("Microphone stream reopened")
        return True

    last_watchdog = time.monotonic()
    try:
        open_stream()
        if True:
            listener.start()
            listener.wait()
            LOG.info("READY mode=%s hold=%s toggle=%s; Ctrl+C stops", "raw" if raw else "cleanup",
                     cfg["hotkeys"]["hold"], cfg["hotkeys"]["toggle_raw"])
            while not stop.is_set():
                if not listener.running:
                    listener.join()  # Propagate event-tap errors.
                    raise RuntimeError("Keyboard listener stopped; check Input Monitoring")
                try:
                    event, timestamp = events.get(timeout=0.2)
                except queue.Empty:
                    now = time.monotonic()
                    if not state["recording"] and now - last_watchdog >= STREAM_WATCHDOG_SECONDS:
                        last_watchdog = now
                        ensure_stream(background=True)
                    continue
                if event == "done":
                    busy = False
                elif event == "toggle":
                    raw = not raw
                    LOG.info("Mode: %s (applies to next recording)", "RAW / CODE" if raw else "CLEANUP")
                    sound(cfg, "raw" if raw else "clean")
                    indicator.mode(raw)
                elif event == "press" and not busy:
                    retry_after[0] = min(retry_after[0], time.monotonic())  # A press always gets one try.
                    if not ensure_stream():
                        sound(cfg, "error")
                        indicator.error("microphone unavailable")
                        continue
                    target = front_target()
                    clip_raw = raw
                    with audio_lock:
                        state.update(recording=True, chunks=[], count=0, overflow=False, limit=False)
                    sound(cfg, "start")
                    LOG.info("Recording mode=%s", "raw" if clip_raw else "cleanup")
                    indicator.recording()
                elif event in ("release", "limit"):
                    with audio_lock:
                        if not state["recording"] and event != "limit":
                            continue
                        state["recording"] = False
                        chunks, overflow = state["chunks"], state["overflow"]
                        state["chunks"] = []
                    sound(cfg, "stop")
                    if event == "limit":
                        LOG.warning("Recording reached max_seconds")
                    if not chunks:
                        LOG.warning("No audio received during hold; microphone stream will be reopened on next press")
                        with audio_lock:
                            state["last_callback"] = 0.0  # Force a reopen next time.
                        indicator.idle()
                        continue
                    samples = np.concatenate(chunks).reshape(-1)
                    duration = samples.size / 16000
                    rms = float(np.sqrt(np.mean((samples.astype(np.float32) / 32768) ** 2)))
                    LOG.info("Captured %.2fs rms=%.4f", duration, rms)
                    if overflow:
                        LOG.error("Audio overflow; discarded clip. Try another input device.")
                        indicator.idle()
                        continue
                    if discard_short_or_silent(cfg, duration, rms):
                        indicator.idle()
                        continue
                    busy = True
                    indicator.processing()
                    worker = threading.Thread(target=work, args=(samples, clip_raw, timestamp, target))
                    worker.start()
                elif event == "press" and busy:
                    LOG.info("Still processing; wait for latency log before recording again")
    finally:
        listener.stop()
        if stream[0] is not None:
            try:
                stream[0].close()
            except Exception:
                pass
        if worker:
            worker.join()


def _run_live_with_overlay(cfg, services, stop):
    """Run run_live in a daemon thread and hand the main thread to AppKit."""
    import AppKit
    from PyObjCTools import AppHelper
    from overlay import OverlayPanel, parse_hex

    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)

    ind_cfg = cfg.get("indicator", {})
    accent = parse_hex(ind_cfg.get("accent", "#7C5CFF"))
    gain = ind_cfg.get("gain", 6.0)
    panel = OverlayPanel(accent, gain)

    live_thread = threading.Thread(
        target=run_live, args=(cfg, services, stop), kwargs={"indicator": panel}, daemon=True
    )
    live_thread.start()

    # When run_live finishes on its own, stop the event loop.
    def _watch_thread():
        live_thread.join()
        stop.set()
        AppHelper.callAfter(AppHelper.stopEventLoop)

    threading.Thread(target=_watch_thread, daemon=True).start()

    # Poll stop (set by SIGTERM/SIGINT) so the event loop exits cleanly.
    def _poll_stop():
        if stop.is_set():
            AppHelper.stopEventLoop()
        else:
            AppHelper.callLater(0.2, _poll_stop)

    # SIGUSR1 flashes the pill for 2 s: `kill -USR1 $(cat .run/dictate.pid)` checks the
    # indicator without speaking. Python runs the handler on the main thread the next time
    # Python code executes there, which _poll_stop guarantees within 0.2 s.
    signal.signal(signal.SIGUSR1, lambda *_: panel.flash(2.0))

    AppHelper.callLater(0.2, _poll_stop)
    AppHelper.runEventLoop()
    live_thread.join()


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "config.toml")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--request-permissions", action="store_true")
    parser.add_argument("--devices", action="store_true")
    parser.add_argument("--hotkey-test", action="store_true", help="60-second modifier-only keyboard diagnostic")
    parser.add_argument("--stop", action="store_true", help="Stop the running Flow instance")
    parser.add_argument("--transcribe", type=Path, help="16 kHz mono PCM16 WAV; prints without pasting")
    parser.add_argument("--raw", action="store_true", help="Start in raw mode / skip cleanup for file test")
    parser.add_argument("--base", action="store_true", help="Use configured fallback Whisper model")
    parser.add_argument("--paste-test", help="Paste harmless text after five seconds; never presses Return")
    args = parser.parse_args()
    cfg = read_config(args.config)
    if args.base:
        cfg["whisper"]["model"] = cfg["whisper"]["fallback_model"]
    if args.raw:
        cfg["dictation"]["cleanup"] = False
    configure_log(cfg)
    if args.stop:
        pidfile = ROOT / ".run" / "dictate.pid"
        if not pidfile.exists():
            print("Flow is not running")
            return
        pid = int(pidfile.read_text())
        command = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True).stdout
        if "dictate.py" not in command or pid == os.getpid():
            raise RuntimeError("Stale PID file; no process was stopped")
        os.kill(pid, signal.SIGTERM)
        print("Stop requested; check dictate.log for Stopped")
        return
    if args.devices:
        import sounddevice as sd
        print(sd.query_devices())
        return
    if args.hotkey_test:
        test_hotkeys(cfg)
        return
    if args.check or args.request_permissions:
        result = permissions(args.request_permissions)
        print(json.dumps(result, indent=2))
        print("Whisper model exists:", path(cfg["whisper"]["model"]).is_file())
        print("Dictionary:", len(dictionary(cfg)), "characters")
        sys.exit(0 if all(result.values()) else 2)
    if args.paste_test:
        print("Focus an empty test document within five seconds.", flush=True)
        time.sleep(5)
        paste(args.paste_test, cfg, front_target())
        return
    if not args.transcribe:
        access = permissions()
        if not all(access.values()):
            raise PermissionError(f"Missing permissions: {access}. Run --request-permissions from Terminal.")
    runtime = ROOT / ".run"
    runtime.mkdir(exist_ok=True, mode=0o700)
    lock = open(runtime / "dictate.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise RuntimeError("Flow is already running") from None
    (runtime / "dictate.pid").write_text(str(os.getpid()))
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    services = Services(cfg)
    try:
        # Even when starting in raw mode, warm cleanup so toggling stays fast.
        services.start(cleanup=not args.transcribe or not args.raw)
        if args.transcribe:
            with wave.open(str(args.transcribe)) as w:
                if (w.getframerate(), w.getnchannels(), w.getsampwidth()) != (16000, 1, 2):
                    raise ValueError("File must be 16 kHz mono PCM16 WAV")
            print(process_audio(services, cfg, args.transcribe.read_bytes(),
                                not cfg["dictation"]["cleanup"], time.perf_counter()))
        elif not stop.is_set():
            if cfg.get("indicator", {}).get("enabled"):
                _run_live_with_overlay(cfg, services, stop)
            else:
                run_live(cfg, services, stop)
    finally:
        services.close()
        (runtime / "dictate.pid").unlink(missing_ok=True)
        lock.close()
        LOG.info("Stopped")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        LOG.error("%s: %s", type(exc).__name__, exc)
        sys.exit(1)
