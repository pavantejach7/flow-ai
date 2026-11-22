#!/bin/zsh
# Entry point for the local.flow.dictation LaunchAgent (login startup).
# Under launchd the permission host is Python itself, not Terminal, so on first
# run macOS must be granted Microphone, Accessibility and Input Monitoring for
# "Python" (System Settings > Privacy & Security). If the check fails we trigger
# the prompts once and exit 0 so launchd does not re-prompt in a loop; after
# granting, run: launchctl kickstart -k "gui/$(id -u)/local.flow.dictation"
cd "${0:A:h}" || exit 1
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
if ! .venv/bin/python dictate.py --check >/dev/null 2>&1; then
  .venv/bin/python dictate.py --request-permissions
  echo "Permissions missing; grant them to Python in System Settings, then kickstart the agent."
  exit 0
fi
exec .venv/bin/python -u dictate.py
