#!/bin/bash
# Verified online SQLite snapshot. Output defaults to /app/data/backups in Docker.
set -euo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
container_name=${SELLER_BACKUP_CONTAINER:-seller-platform}

if [ "$(docker inspect --format '{{.State.Running}}' "$container_name")" != true ]; then
  echo 'Backup refused: container is not running; no raw file-copy fallback.' >&2
  exit 1
fi

# Use the reviewed local helper without requiring an application restart.
exec docker exec --interactive --user app "$container_name" python - "$@" \
  < "$script_dir/verified_sqlite_backup.py"
