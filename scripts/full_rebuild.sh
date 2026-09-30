#!/bin/bash
# Rebuild every default Compose image without cache. Keep persistent data
# volumes; the deployment helper drains native Flash calls before stopping app.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
PYTHON_BIN="$PROJECT_DIR/venv/bin/python"
[ -x "$PYTHON_BIN" ] || PYTHON_BIN=python3

exec "$PYTHON_BIN" "$SCRIPT_DIR/deploy_safety.py" \
    --project-dir "$PROJECT_DIR" --build-all --no-cache --pull \
    --compose-down --up-all
