#!/bin/sh
# Linux: makes a private venv (.venv) with the packages from requirements.txt on first run, then starts the app.
cd "$(dirname "$0")" || exit 1
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv || exit 1
  .venv/bin/pip install -q -r requirements.txt || exit 1
fi
exec .venv/bin/python -m speakstone_worker "$@"
