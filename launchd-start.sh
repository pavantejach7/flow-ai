#!/bin/zsh
# Entry point for the local.flow.dictation LaunchAgent (login startup).
# Under launchd the permission host is Python itself, not Terminal, so on first
# run macOS must be granted Microphone, Accessibility and Input Monitoring for
# "Python" (System Settings > Privacy & Security). If permissions are missing we
# trigger the prompts once and exit 0 so launchd does not re-prompt in a loop;
# after granting, run: launchctl kickstart -k "gui/$(id -u)/local.flow.dictation"
# Any other --check failure (bad config, missing model) exits 1 so KeepAlive retries
# and the cause is visible in .run/launchd.log.
cd "${0:A:h}" || exit 1
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
mkdir -p .run
.venv/bin/python dictate.py --check >/dev/null 2>.run/check.err
rc=$?
if [[ $rc -eq 2 ]]; then
  .venv/bin/python dictate.py --request-permissions
  echo "Permissions missing; grant them to Python in System Settings, then kickstart the agent."
  exit 0
elif [[ $rc -ne 0 ]]; then
  echo "Flow --check failed (exit $rc); see .run/check.err:"
  cat .run/check.err
  exit 1
fi
exec .venv/bin/python -u dictate.py
