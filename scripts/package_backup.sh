#!/usr/bin/env bash
set -euo pipefail

# Packages a clean, rebuildable, Google-Drive-ready backup of portfolio-dashboard
# (excludes .venv, caches, and logs; includes source code, docs, lockfile, and data/portfolio.db).

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
STAMP="$(date +%Y-%m-%d_%H%M)"
OUT_DIR="${1:-$(dirname "$PROJECT_ROOT")/portfolio-dashboard-backup-$STAMP}"
ZIP_PATH="${OUT_DIR}.zip"

rm -rf "$OUT_DIR" "$ZIP_PATH"
mkdir -p "$OUT_DIR"

rsync -a \
    --exclude '.venv/' \
    --exclude '.agents/' \
    --exclude '__pycache__/' \
    --exclude '.pytest_cache/' \
    --exclude '*.pyc' \
    --exclude 'data/streamlit.log' \
    --exclude 'data/demo_portfolio.db*' \
    --exclude '.DS_Store' \
    "$PROJECT_ROOT/" "$OUT_DIR/"

chmod +x "$OUT_DIR/start_dashboard.command" "$OUT_DIR/scripts/package_backup.sh"

(
    cd "$(dirname "$OUT_DIR")"
    zip -rq "$(basename "$ZIP_PATH")" "$(basename "$OUT_DIR")"
)

echo "Created portable folder: $OUT_DIR"
echo "Created portable zip:    $ZIP_PATH"
du -sh "$OUT_DIR" "$ZIP_PATH"
