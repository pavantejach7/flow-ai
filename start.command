#!/bin/zsh
cd "${0:A:h}" || exit 1
if ! .venv/bin/python dictate.py --check; then
  .venv/bin/python dictate.py --request-permissions
  print 'Grant the listed permissions to Terminal, then quit/reopen Terminal and run this again.'
  read '?Press Return to close.'
  exit 1
fi
exec .venv/bin/python dictate.py "$@"
