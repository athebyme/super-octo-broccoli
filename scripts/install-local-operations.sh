#!/usr/bin/env bash
# Host-only services: never restart the app or deploy an image.
set -euo pipefail
if [[ $(id -u) -ne 0 || -z ${SUDO_USER:-} || ${SUDO_USER:-} == root ]]; then
  echo 'Run from deployment user: sudo bash scripts/install-local-operations.sh' >&2
  exit 1
fi
task_project=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
task_user=$SUDO_USER
task_group=$(id -gn "$task_user")
task_python="$task_project/venv/bin/python"
[[ -x "$task_python" && -f "$task_project/scripts/local_operations.py" ]]
task_state=$(runuser -u "$task_user" -- "$task_python" -c 'from pathlib import Path; print(Path.home()/".local/share/seller-hub/local-operations")')
task_telegram=$(runuser -u "$task_user" -- "$task_python" -c \
  'import sys; from pathlib import Path; sys.path.insert(0,sys.argv[1]); from scripts.deploy_telegram import configuration,KEYS; print(configuration(Path(sys.argv[1])/".env.autodeploy")[KEYS[3]])' "$task_project")
install -d -m 700 -o "$task_user" -g "$task_group" "$task_state"
[[ -d "$task_telegram" ]]
# Explicitly refuse characters which require additional systemd specifier escaping.
for task_path in "$task_project" "$task_state" "$task_telegram"; do
  if [[ "$task_path" == *'%'* || "$task_path" == *'"'* || "$task_path" == *$'\n'* || "$task_path" == *'\'* ]]; then
    echo 'Unsupported unit path.' >&2
    exit 1
  fi
done
for task_kind in observer backup; do
  task_action=observe
  task_timeout=110
  [[ $task_kind != backup ]] || { task_action=backup; task_timeout=2050; }
  cat > "/etc/systemd/system/seller-local-${task_kind}.service" <<EOF
[Unit]
Description=Seller Hub local $task_kind
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=oneshot
User=$task_user
Group=$task_group
WorkingDirectory=$task_project
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart="$task_python" "$task_project/scripts/local_operations.py" $task_action --state-dir "$task_state"
TimeoutStartSec=$task_timeout
TimeoutStopSec=15
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths="$task_state" "$task_telegram"
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
MemoryMax=256M
TasksMax=32
StandardOutput=journal
StandardError=journal
EOF
done
cat > /etc/systemd/system/seller-local-observer.timer <<'EOF'
[Unit]
Description=Observe Seller Hub independently of its container
[Timer]
OnBootSec=90s
OnUnitInactiveSec=60s
AccuracySec=5s
Unit=seller-local-observer.service
[Install]
WantedBy=timers.target
EOF
cat > /etc/systemd/system/seller-local-backup.timer <<'EOF'
[Unit]
Description=Daily verified Seller Hub SQLite backup (03:15 Moscow)
[Timer]
OnCalendar=*-*-* 03:15:00 Europe/Moscow
RandomizedDelaySec=5m
AccuracySec=30s
Persistent=true
Unit=seller-local-backup.service
[Install]
WantedBy=timers.target
EOF
systemd-analyze verify /etc/systemd/system/seller-local-{observer,backup}.{service,timer}
systemctl daemon-reload
systemctl enable --now seller-local-observer.timer seller-local-backup.timer
echo 'Local operations timers installed. Check actual backup completion separately.'
