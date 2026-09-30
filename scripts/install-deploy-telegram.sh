#!/usr/bin/env bash
# Install only the subscription receiver; does not run/restart deployments.
set -euo pipefail
if [[ $(id -u) -ne 0 ]]; then
  echo 'Run with sudo: bash scripts/install-deploy-telegram.sh' >&2
  exit 1
fi
task_project=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
task_user=${SUDO_USER:-}
if [[ -z "$task_user" || "$task_user" == root ]]; then
  echo 'Run sudo from the deployment user account.' >&2
  exit 1
fi
task_python="$task_project/venv/bin/python"
[[ -x "$task_python" && -f "$task_project/.env.autodeploy" ]]
task_group=$(id -gn "$task_user")
task_state=$(runuser -u "$task_user" -- "$task_python" -c \
  'import sys; from pathlib import Path; sys.path.insert(0,sys.argv[1]); from scripts.deploy_telegram import configuration,KEYS; print(configuration(Path(sys.argv[1])/".env.autodeploy")[KEYS[3]])' "$task_project")
install -d -m 700 -o "$task_user" -g "$task_group" "$task_state"
cat > /etc/systemd/system/seller-deploy-telegram.service <<EOF
[Unit]
Description=Seller Hub deployment Telegram subscriptions
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$task_user
Group=$task_group
WorkingDirectory=$task_project
ExecStart="$task_python" "$task_project/scripts/deploy_telegram_bot.py" --config "$task_project/.env.autodeploy"
Restart=on-failure
RestartSec=15
RestartPreventExitStatus=78
TimeoutStopSec=40
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths="$task_state"
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
InaccessiblePaths=-/run/docker.sock
MemoryMax=128M
TasksMax=16
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now seller-deploy-telegram.service
echo 'Deployment Telegram receiver installed; use systemctl status seller-deploy-telegram.'
