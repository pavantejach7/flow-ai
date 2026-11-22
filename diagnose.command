#!/bin/zsh
cd "${0:A:h}" || exit 1
mkdir -p .run
.venv/bin/python -u dictate.py --hotkey-test 2>&1 | tee .run/hotkey-test.log
print 'Diagnostic finished. This window can be closed.'
read '?Press Return to close.'
