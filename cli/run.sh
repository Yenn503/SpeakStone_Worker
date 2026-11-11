#!/bin/sh
# Linux: needs Python 3.9+ and `pip install rich` (or use: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt)
cd "$(dirname "$0")" || exit 1
if [ -x .venv/bin/python ]; then PY=.venv/bin/python; else PY=python3; fi
exec "$PY" -m speakstone_worker "$@"
