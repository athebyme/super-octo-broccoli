#!/usr/bin/env bash
set -euo pipefail
task_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$task_root"
if [[ $# -ne 1 || -e "$1" ]]; then
  echo 'Usage: bash scripts/check_ozon_release.sh /absolute/path/to/new-report-directory' >&2
  exit 2
fi
case "$1" in /*) ;; *) echo 'Report path must be absolute.' >&2; exit 2 ;; esac
task_output=$1
mkdir -p -- "$task_output"
task_image="seller-hub-ozon-check:local"
docker build -f tests/ozon_release/Dockerfile -t "$task_image" .
task_container=$(docker create --network=none --cap-drop=ALL --security-opt=no-new-privileges \
  --pids-limit=512 --memory=4g --cpus=2 --shm-size=512m "$task_image")
cleanup() {
  local task_cleanup_status=$?
  if ! docker cp "$task_container:/artifacts/." "$task_output/"; then
    task_cleanup_status=1
  fi
  if ! docker rm -f "$task_container" >/dev/null; then
    task_cleanup_status=1
  fi
  trap - EXIT
  exit "$task_cleanup_status"
}
trap cleanup EXIT
set +e
docker start -a "$task_container"
task_status=$?
set -e
echo "Ozon check artifacts: $task_output"
exit "$task_status"
