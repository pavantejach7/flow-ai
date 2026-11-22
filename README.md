# Flow — local Mac dictation

Hold **Option (either side)** to record; release to transcribe, clean up, and paste.
This Mac's physical-key diagnostic reports both Option presses as `Key.alt`, so the local configuration
uses `<alt>`. To require Right Option on a keyboard that reports sides distinctly, set `hotkeys.hold = "<alt_r>"`.
**Control+Shift+R** toggles raw/code mode. The terminal announces the mode.
Raw mode skips the LLM; Whisper can still misrecognize code, paths, and commands.
Nothing presses Return or submits a message. Review terminal commands before executing them.
Pastes default to a single line so ASR line wrapping cannot introduce command newlines.
Set `dictation.single_line = false` only if you want multiline pastes and the destination handles them safely.

## Architecture

Flow is one Python process (`dictate.py`, launched by the `local.flow.dictation` LaunchAgent or
`start.command`) that owns two local services and talks to the OS through PyObjC. Nothing leaves the Mac.

```
             hold Option                      release
  keyboard ───────────────► Hotkeys (pynput) ──────────► run_live()
                                                           │
  microphone ── sounddevice 16 kHz mono PCM16 ─────────────┤  clip + rms
                                                           ▼
                                    discard_short_or_silent()  (min_seconds / silence_rms gate)
                                                           │
                                                           ▼  worker thread
                             whisper-server  ◄── HTTP ── process_audio()
                             (whisper.cpp, Metal,          │   text
                              127.0.0.1:8178,              ▼
                              ggml-large-v3-turbo)   cleanup?  ── HTTP ──►  Ollama 127.0.0.1:11434
                                                           │               (qwen3:8b, punctuation,
                                                           │  cleaned text   dictionary.txt hints)
                                                           ▼
                                     paste(): app-identity guard → snapshot clipboard →
                                     pbcopy → Cmd+V via Quartz → restore clipboard
                                                           │
                                                           ▼
                                                 focused app receives text
```

| Layer | Component | Role |
| --- | --- | --- |
| Input | `Hotkeys` (pynput) | Global hold-to-talk on Option; Ctrl+Shift+R toggles raw/cleanup mode |
| Capture | `sounddevice` | 16 kHz mono stream while the key is held; rms + duration computed per clip |
| Gate | `discard_short_or_silent` | Drops clips under `audio.min_seconds` or below `audio.silence_rms`, logs the numbers |
| ASR | `whisper-server` (Homebrew whisper.cpp) | Started and owned by Flow; large-v3-turbo on Metal, base.en fallback |
| Cleanup | Ollama `qwen3:8b` | Optional LLM pass for punctuation, casing, dictionary spellings; skipped in raw mode |
| Output | `paste()` (AppKit + Quartz) | Cancels if the frontmost app changed since key-down; clipboard saved and restored |
| Feedback | `sound()` (`afplay`) | Press/release/error cues at `cues.cue_volume` |
| Ops | `Services`, `.run/`, `dictate.log` | Health checks, pid/lock files, latency line per dictation |

`Services` only starts what is missing and only stops what it started: an existing Ollama is reused.
All HTTP goes to loopback only (`local_url` rejects anything else), redirects and proxies are disabled.
Latency on an M5 is about 0.7–1.0 s from key release to pasted text.

## Recording indicator

While Option is held, a dark pill (200×48 pt) appears at the bottom center of the screen with a red live dot and
a waveform of Infyva-blue bars that follow the mic level; after release it shows three pulsing dots until the
text is pasted, flashes red on an error, and hides when idle. It is a non-activating floating `NSPanel`
(`overlay.py`) that ignores the mouse, joins every Space and sits over full-screen apps. Configure it in
`config.toml`:

```toml
[indicator]
enabled = true      # false restores the original headless behavior
accent = "#5468FF"  # 6-digit hex; Infyva blue for dark surfaces (brand primary #0115E8)
gain = 6.0          # mic-level-to-bar gain; raise it if quiet speech barely moves the bars
```

With the indicator enabled, `dictate.py` runs the dictation loop in a thread and gives the main thread to AppKit;
all window updates are marshalled to the main thread. Each time it shows, the pill re-centers itself on the
display under the mouse pointer (falling back to the main screen), so unplugging a monitor or moving to
another display never leaves it stranded off-screen. When macOS reports a display-layout change (monitor
plugged or unplugged, clamshell), Flow discards the panel and builds a fresh one, since a window created
under an old layout can stay invisible. To check the pill without speaking, flash it for two seconds:

```bash
kill -USR1 "$(cat .run/dictate.pid)"
```

The flash is ignored while a real recording is in progress, and the signal is a no-op when the indicator is
disabled. Errors (for example "microphone unavailable") show the pill in red for 1.2 s even when it was hidden.

## Start and stop

In Terminal:

```sh
cd ~/develop/flow
./start.command
```

Wait for `READY`. Keep the terminal open; switch to the destination app and dictate.
Stop with **Ctrl+C in the running terminal**, or from another terminal:

```sh
~/develop/flow/stop.command
```

The app starts and warms its own Metal-accelerated `whisper-server` on `127.0.0.1:8178`.
It uses your existing Ollama on `127.0.0.1:11434`, starting a local-only instance if needed.
It stops only services it started. Your preexisting Ollama stays running.
Qwen stays loaded; release its RAM separately with `ollama stop qwen3:8b`.
Only one Flow instance can run at a time. Stop an instance launched from T3 before starting in Terminal.

## Permissions

Run `.venv/bin/python dictate.py --request-permissions` from the intended host app.
Open **System Settings → Privacy & Security**:

- **Microphone:** allow Terminal (or T3 Code) to capture speech. The entry appears after a request.
- **Accessibility:** click **+**, add **Applications → Utilities → Terminal**, and enable it. This sends Cmd+V.
- **Input Monitoring:** add and enable the same app. This detects global hotkeys.

For T3, choose **Applications → T3 Code (Beta)** instead. Enable Python too if macOS lists it separately.
Authenticate in System Settings yourself if requested. Quit and reopen the host app when macOS asks.
Verify with `.venv/bin/python dictate.py --check`; all three results should be `true`.
Terminal permissions and T3 permissions are separate. Secure Keyboard Entry may prevent global hotkeys;
if that happens, use the app in a host where hotkeys are available or deliberately change that Terminal setting yourself.

If pressing Option does nothing, stop Flow and run this **from the same Terminal**:

```sh
.venv/bin/python dictate.py --hotkey-test
```

During its 60-second window, press/release Right Option and then Left Option. The configured key should show
`hold=True`. With `<alt>`, either `Key.alt` or `Key.alt_r` activates it; `<alt_r>` requires a distinct right-side event.
On the built-in MacBook keyboard, Right Option (⌥) is after the Command (⌘) key to the right of Space.
The diagnostic prints only modifier names/shortcut states, never ordinary typed text, and records no audio.

## Configure

Edit `config.toml`, then restart. Hotkeys use pynput syntax, such as `<alt_r>`, `<f8>`, or `<ctrl>+<space>`.
Avoid shortcuts already used by your apps and Option+letter combinations that produce alternate characters.
`dictation.cleanup = false` starts in raw mode; the toggle can still enable cleanup.

- Default Whisper: `models/ggml-large-v3-turbo.bin`.
- Faster fallback: run `./start.command --base`, or change `whisper.model` to `models/ggml-base.en.bin`.
- Cleanup: `ollama.model = "qwen3:8b"`. Pull a different **local** model explicitly before selecting it.
  The client sends `think: false`; choose a model supporting that option, or adapt the client for other models.
- Audio input: `.venv/bin/python dictate.py --devices`; set `audio.device` to a listed name.
- Clipboard: increase `clipboard_restore_seconds` for an app that consumes clipboard data slowly.
- Sounds: set `audio.sounds = false` if cues are audible in recordings.

Add names/terms to `dictionary.txt`, one per line; `#` starts a comment. Changes apply on the next recording.
Keep active entries under 600 characters (and preferably under ~100 words) to fit Whisper's prompt budget.
The dictionary is a spelling hint, not a guaranteed correction engine.

## Privacy and behavior

Downloads require internet once; audio and text processing use only loopback HTTP afterward, without paid APIs.
Requests ignore proxy environment variables, reject redirects, and reject cloud-model settings.
Qwen3 8B is instruction tuned, used with thinking disabled, and Apache 2.0 licensed for commercial use:
[Ollama listing](https://ollama.com/library/qwen3:8b) · [upstream license](https://huggingface.co/Qwen/Qwen3-8B/blob/main/LICENSE).
The downloaded license is in `tests/Qwen3-8B-LICENSE.txt`.

The microphone stream remains open while Flow runs to avoid startup lag; only held-key audio is retained.
If Core Audio drops that stream (Mac slept, headphones or AirPods connected or removed; the launchd log
shows a `PaMacCore ... err='-50'` line), Flow notices on the next Option press that the audio callback has
been quiet for over a second, logs `reopening microphone stream`, and reopens it before recording. A hold
that captured nothing logs `No audio received during hold` and forces a reopen next time.
While idle, a watchdog runs the same check every 2 s, so after a wake the stream is usually healed before
you press Option (`Microphone stream reopened` in the log). If the microphone is truly unavailable the
reopen is retried every 15 s. A crash inside the dictation loop exits non-zero, with the overlay on or off, so the
LaunchAgent (KeepAlive) restarts Flow; a clean `--stop` is the only exit it does not restart. A single failing
event (for example no frontmost app while the screen is locked) is logged and skipped rather than ending the loop.
Live recordings stay in memory. `dictate.log` records timing/status, not dictated text.
Service diagnostics are in `.run/`; test recordings/results are in `tests/`.
Logs rotate after 2 MB. Stop Flow when you do not need the microphone.

The clipboard's materialized formats (including rich text/images/files) are saved and restored.
If another app/user copies something during paste, that newer clipboard is preserved.
Unavailable promised clipboard items cause paste to cancel. Clipboard managers may still retain pasted text.
Changing the focused app/window during transcription cancels the paste. Stay in the same field until insertion completes.
Very short/silent clips and audio-overflow clips are discarded; max recording length is 120 seconds. A clip that
hits the limit while Option is still held is kept and processed when you release the key, because pasting while a
modifier is down would be cancelled.

The clipboard is restored `clipboard_restore_seconds` (default 0.45 s) after Cmd+V is sent. A target app that
services the paste later than that receives your previous clipboard instead; raise the value if that ever happens.
Wait for processing to finish before holding the hotkey again. Cleanup errors fall back to the original transcript.

## Tests and latency

```sh
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python dictate.py --transcribe tests/audio/mic-5s.wav
.venv/bin/python dictate.py --transcribe tests/audio/mic-5s.wav --raw --base
```

Stop the live app before file tests/benchmarks because they own the same Whisper port.
For an exactly 10-second test clip, `tests/benchmark.py FILE.wav` times both downloaded models without pasting.
Input must be 16 kHz, mono, PCM16 WAV.

Initial measured results on this M5 Pro: microphone clip transcribed as “Hey, what's up?”;
CLI cold start took 1.709 s, including 0.518 s model load; the warm server request took 0.215 s.
Qwen cleanup sample took 4.791 s cold and 0.273 s warm.
A generated 10-second fixture took 0.735–0.862 s for large-v3-turbo plus cleanup, excluding paste.
Synthetic speech had recognition errors; these timings are not proof of live-dictation accuracy or latency.
Actual per-step measurements appear as `whisper`, `cleanup`, `paste_restore`, and `release_to_complete` in `dictate.log`.
The last value includes clipboard restoration, so it is a conservative completion time rather than exact visible insertion time.

If live 10-second clips exceed ~1.5 s: keep models warm, try `--base`, use raw mode, reduce competing GPU work,
and inspect the individual timing fields. Do not shorten clipboard restoration until the target app is verified.

Application checklist (use harmless text; never submit it):

1. TextEdit: new document → hold/release Option → verify text; repeat in raw mode.
2. Browser: open `tests/browser-test.html` → click textarea → repeat both modes.
3. VS Code: new untitled text file → repeat both modes.
4. Terminal: launch your existing Claude Code in a test directory → repeat in its input box, without Return.

Both model downloads were SHA-256 verified against the official `ggerganov/whisper.cpp` repository:

```text
large-v3-turbo  1fc70f774d38eb169993ac391eea357ef47c88757ef72ee5943879b7e8e2bc69
base.en         a03779c86df3323075f5e796cb2ce5029f00ec8869eee3fdfb897afe36c6d002
```

## Login startup (optional)

A LaunchAgent keeps Flow running from login and restarts it if it crashes. Label: `local.flow.dictation`.
Files: `~/Library/LaunchAgents/local.flow.dictation.plist` (points at `launchd-start.sh` in this folder).
Create it from the template in this repo, substituting this folder's absolute path:

```sh
sed "s#__FLOW_DIR__#$PWD#g" local.flow.dictation.plist.example > ~/Library/LaunchAgents/local.flow.dictation.plist
```
Only one Flow instance may run, so stop any Terminal-launched Flow first (`./stop.command`).

```sh
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/local.flow.dictation.plist   # install + start
launchctl kickstart -k "gui/$(id -u)/local.flow.dictation"                              # restart now
launchctl bootout "gui/$(id -u)/local.flow.dictation"                                   # stop + unload
rm ~/Library/LaunchAgents/local.flow.dictation.plist                                    # uninstall
```

**Permissions under launchd are separate from Terminal's.** The host process is Python itself, so macOS
lists it as **Python** (the binary under `/opt/homebrew/Cellar/python@3.12/.../Python.framework`). On the first
start `launchd-start.sh` runs `--check`; if anything is missing it triggers the prompts once and exits 0 so you are
not re-prompted in a loop. Then, in **System Settings → Privacy & Security**:

- **Accessibility** and **Input Monitoring:** enable **Python** if it is listed; otherwise click **+**, press
  Cmd+Shift+G and paste the path printed by `readlink -f .venv/bin/python`, and enable it.
- **Microphone:** usually granted automatically from the prompt.

Then `launchctl kickstart -k "gui/$(id -u)/local.flow.dictation"` and check `.run/launchd.log` and `dictate.log`
for `READY`. The plist restarts Flow on any non-zero exit (30 s throttle); a clean `--stop` is not restarted.
`ProcessType=Interactive` keeps it responsive under App Nap.

## Testing without speaking

Every stage except the Option hold and the microphone can be exercised from a shell:

```sh
curl -s -F file=@tests/audio/mic-5s.wav -F response_format=json http://127.0.0.1:8178/inference   # whisper, live server
.venv/bin/python -m unittest discover -s tests -v                                                  # paste guard, cues, discard log
.venv/bin/python dictate.py --transcribe tests/audio/mic-5s.wav     # whisper + cleanup; Flow must be stopped
.venv/bin/python dictate.py --paste-test "hello"                    # paste path; run from Terminal, focus a BLANK doc
```

Flow pastes into whichever app was focused when Option went down, so always focus a blank document before a
paste or dictation test. Discarded clips log `duration`, `rms` and the `threshold` they failed against, so
"nothing happened" is diagnosable from `dictate.log` alone.

## Verified changes with looprail

`looprail.yaml` in this folder runs a verified agent loop for code changes: an opus planner, a sonnet executor,
the real unittest suite as a tester, and an opus critic on a different model than the worker. Rails:
4 iterations, $8, 25 minutes; `scope:` limits edits to `dictate.py` and `tests/test_dictate.py`;
`no_weaker_tests` fails any iteration that drops assertions. Edit `goal:` for a new change, then:

```sh
npm install -g looprail           # once; needs Node 20+ and a logged-in `claude` CLI
looprail lint looprail.yaml
looprail run --dry-run            # plan, models, budget; spends nothing
looprail run --ui                 # run with the live dashboard; `looprail why <runId>` explains a halt
```

Flow is not a git repo, so `blind: true` diff review is unavailable; copy the loopfile shape rather than the
example repo's git-dependent ones. See https://github.com/saimeda32/looprail for the reference and examples.

## Full uninstall of this setup

The existing Xcode tools, Homebrew, ffmpeg, Python 3.12, Ollama app, and llama3.2:3b were reused.
New Homebrew formulae were **whisper.cpp, llama.cpp, ggml, libomp**; Python packages are isolated in `.venv`.
The new Ollama model is **qwen3:8b**. See `installation.json` and `requirements.lock`.

Stop Flow first. If login startup was subsequently installed, unload/remove it using the commands above.
Then run these commands only when you want to remove the setup:

```sh
~/develop/flow/stop.command
ollama stop qwen3:8b
ollama rm qwen3:8b
brew uninstall whisper.cpp llama.cpp ggml libomp
rm -rf ~/develop/flow
```

Back up your dictionary/config first if desired. Homebrew may refuse to remove a dependency subsequently
needed by other software; keep it in that case. The directory removal includes both Whisper models,
virtualenv, scripts, logs, and test audio. Remove the host app's permission switches in System Settings
only if you no longer need them for other tools. The preexisting software remains installed.
