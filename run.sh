#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
PYTHON_BIN="${PYTHON_BIN:-python3.12}"
command -v "$PYTHON_BIN" >/dev/null 2>&1 || PYTHON_BIN=python3
[ -x .venv/bin/python ] || "$PYTHON_BIN" -m venv .venv
source .venv/bin/activate
pip install -q -r requirements-dev.txt
printf '\nOpen http://127.0.0.1:8000\n\n'
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
