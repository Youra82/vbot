#!/bin/bash
set -e

echo "--- vbot Tests werden ausgefuehrt ---"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PYTHON=".venv/bin/python3"
if [ ! -f "$PYTHON" ]; then
    PYTHON=".venv/Scripts/python.exe"
fi
if [ ! -f "$PYTHON" ]; then
    PYTHON="python3"
fi

PYTHONPATH="$SCRIPT_DIR/src" $PYTHON -m pytest tests/ -v "$@"
echo "Tests abgeschlossen."
