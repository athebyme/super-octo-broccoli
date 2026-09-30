#!/bin/bash
# Stage a verified recovery in a new directory; never overwrite/restart production.
set -euo pipefail

if [ "$#" -lt 2 ]; then
  echo 'Usage: bash scripts/restore_database.sh <manifest.json> <new-directory/seller_platform.db> [limits]' >&2
  echo 'Paths are inside Docker. Existing directories are rejected. No production cutover is performed.' >&2
  exit 2
fi
manifest_path=$1
destination_path=$2
shift 2
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
container_name=${SELLER_BACKUP_CONTAINER:-seller-platform}
if [ "$(docker inspect --format '{{.State.Running}}' "$container_name")" != true ]; then
  echo 'Restore refused: container is not running. Run the stdlib helper in an isolated recovery environment.' >&2
  exit 1
fi
exec docker exec --interactive --user app "$container_name" python - \
  --restore-manifest "$manifest_path" --destination "$destination_path" "$@" \
  < "$script_dir/verified_sqlite_backup.py"
