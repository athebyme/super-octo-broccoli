#!/bin/bash
# =============================================================================
# setup-coolify.sh — Установка и настройка Coolify для проекта
#
# Запускать на сервере от root:
#   curl -fsSL <url>/setup-coolify.sh | bash
#   или
#   ./setup-coolify.sh
#
# После установки:
#   1. Открыть http://<server-ip>:8000
#   2. Создать аккаунт администратора
#   3. Добавить GitHub-репозиторий как источник
#   4. Создать ресурс типа "Docker Compose" и указать docker-compose.prod.yml
# =============================================================================

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

log_info()  { echo -e "${CYAN}[INFO]${NC}  $1"; }
log_ok()    { echo -e "${GREEN}[OK]${NC}    $1"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC}  $1"; }
log_err()   { echo -e "${RED}[ERR]${NC}   $1"; }

echo ""
echo -e "${CYAN}═══════════════════════════════════════════════${NC}"
echo -e "${CYAN}  Установка Coolify для WB Seller Platform${NC}"
echo -e "${CYAN}═══════════════════════════════════════════════${NC}"
echo ""

# ─── Проверки ───

if [ "$(id -u)" -ne 0 ]; then
    log_err "Запустите от root: sudo ./setup-coolify.sh"
    exit 1
fi

# ─── 1. Docker ───

if ! command -v docker &>/dev/null; then
    log_info "Установка Docker..."
    curl -fsSL https://get.docker.com | sh
    systemctl enable docker
    systemctl start docker
    log_ok "Docker установлен"
else
    log_ok "Docker уже установлен: $(docker --version)"
fi

# ─── 2. Coolify ───

if docker ps --format '{{.Names}}' 2>/dev/null | grep -q coolify; then
    log_ok "Coolify уже запущен"
else
    log_info "Установка Coolify..."
    curl -fsSL https://cdn.coollabs.io/coolify/install.sh | bash
    log_ok "Coolify установлен"
fi

# ─── 3. Docker prune cron ───

CRON_JOB="0 4 * * * docker system prune -af --filter 'until=72h' >> /var/log/docker-prune.log 2>&1"
if crontab -l 2>/dev/null | grep -q "docker system prune"; then
    log_ok "Cron очистки Docker уже настроен"
else
    log_info "Добавление cron-задачи очистки Docker (ежедневно в 4:00)..."
    (crontab -l 2>/dev/null; echo "$CRON_JOB") | crontab -
    log_ok "Cron очистки Docker добавлен"
fi

# ─── Готово ───

SERVER_IP=$(hostname -I | awk '{print $1}')

echo ""
echo -e "${GREEN}═══════════════════════════════════════════════${NC}"
echo -e "${GREEN}  Coolify установлен!${NC}"
echo -e "${GREEN}═══════════════════════════════════════════════${NC}"
echo ""
echo -e "  Панель:  ${CYAN}http://${SERVER_IP}:8000${NC}"
echo ""
echo -e "  ${YELLOW}Следующие шаги:${NC}"
echo ""
echo "  1. Откройте панель и создайте аккаунт администратора"
echo "  2. Добавьте сервер (localhost уже добавлен)"
echo "  3. Создайте новый проект"
echo "  4. Добавьте ресурс:"
echo "     - Тип: Docker Compose"
echo "     - Источник: GitHub репозиторий"
echo "     - Docker Compose файл: docker-compose.prod.yml"
echo "  5. В настройках ресурса задайте переменные окружения"
echo "     (скопируйте из .env.example)"
echo "  6. Настройте Webhook для автодеплоя на push"
echo ""
echo -e "  ${CYAN}Управление ветками:${NC}"
echo "  В настройках ресурса можно указать ветку для деплоя."
echo "  Для preview-деплоев веток включите 'Preview Deployments'"
echo "  в настройках проекта."
echo ""
echo -e "  ${CYAN}Очистка Docker:${NC}"
echo "  Coolify: Settings → Advanced → Docker Cleanup"
echo "  Cron: ежедневно в 4:00 (docker system prune)"
echo ""
