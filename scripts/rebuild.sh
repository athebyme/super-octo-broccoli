#!/bin/bash
# Rebuild seller-platform and start the default Compose services. The shared
# deployment helper drains durable native Flash reservations before stopping it.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
PYTHON_BIN="$PROJECT_DIR/venv/bin/python"
[ -x "$PYTHON_BIN" ] || PYTHON_BIN=python3

exec "$PYTHON_BIN" "$SCRIPT_DIR/deploy_safety.py" \
    --project-dir "$PROJECT_DIR" --no-cache --up-all
