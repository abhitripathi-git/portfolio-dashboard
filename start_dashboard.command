#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

if [ ! -x ".venv/bin/streamlit" ]; then
    echo "Virtual environment not found — rebuilding .venv..."
    if command -v uv >/dev/null 2>&1; then
        uv sync
    else
        python3 -m venv .venv
        .venv/bin/pip install --upgrade pip
        .venv/bin/pip install -e ".[dev]"
    fi
fi

echo "Starting Personal Portfolio Dashboard at http://localhost:8501 ..."
exec ".venv/bin/streamlit" run app.py
