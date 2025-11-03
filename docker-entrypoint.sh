#!/bin/sh
set -e

APP_MODULE=${APP_MODULE:-seller_platform:app}
PORT=${PORT:-5001}

# Ensure all working directories exist so uploads and reports survive volume mounts.
mkdir -p uploads processed data

# Run lightweight initialization depending on the application we serve.
if [ "$APP_MODULE" = "seller_platform:app" ]; then
python - <<'PYCODE'
from seller_platform import app, db, ensure_storage_roots

ensure_storage_roots()
with app.app_context():
    db.create_all()
PYCODE
else
python - <<'PYCODE'
from app import ensure_directories

ensure_directories()
PYCODE
fi

exec gunicorn --bind 0.0.0.0:${PORT} ${APP_MODULE}
