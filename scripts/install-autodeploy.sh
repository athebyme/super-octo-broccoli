#!/bin/bash
# =============================================================================
# Установщик systemd-сервиса для автодеплоя
#
# Запуск:
#   sudo ./scripts/install-autodeploy.sh
#
# Управление:
#   sudo systemctl status seller-autodeploy    # статус
#   sudo journalctl -u seller-autodeploy -f    # логи в реальном времени
#   sudo systemctl stop seller-autodeploy      # остановить
#   sudo systemctl disable seller-autodeploy   # отключить автозапуск
# =============================================================================

set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "Run as root: sudo $0"
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
AUTODEPLOY_SCRIPT="$SCRIPT_DIR/autodeploy.sh"

# Определяем пользователя, от которого запускать
DEPLOY_USER="${SUDO_USER:-root}"

echo "Installing autodeploy service..."
echo "  Project: $PROJECT_DIR"
echo "  Script:  $AUTODEPLOY_SCRIPT"
echo "  User:    $DEPLOY_USER"

# Делаем скрипт исполняемым
chmod +x "$AUTODEPLOY_SCRIPT"

# Создаём systemd unit
cat > /etc/systemd/system/seller-autodeploy.service << EOF
[Unit]
Description=Seller Platform Autodeploy (git watcher)
After=network-online.target docker.service
Wants=network-online.target
Requires=docker.service

[Service]
Type=simple
User=$DEPLOY_USER
Group=docker
WorkingDirectory=$PROJECT_DIR
ExecStart=$AUTODEPLOY_SCRIPT --interval 30
Restart=always
RestartSec=10

# Логирование
StandardOutput=journal
StandardError=journal
SyslogIdentifier=seller-autodeploy

# Безопасность
NoNewPrivileges=false
ProtectSystem=false

# Env
Environment=HOME=/home/$DEPLOY_USER
Environment=PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

[Install]
WantedBy=multi-user.target
EOF

# Активируем
systemctl daemon-reload
systemctl enable seller-autodeploy
systemctl start seller-autodeploy

echo ""
echo "Autodeploy service installed and started!"
echo ""
echo "Commands:"
echo "  sudo journalctl -u seller-autodeploy -f     # live logs"
echo "  sudo systemctl status seller-autodeploy      # status"
echo "  sudo systemctl restart seller-autodeploy     # restart"
echo "  sudo systemctl stop seller-autodeploy        # stop"
echo ""
