#!/bin/bash
# =============================================================================
# Autodeploy - следит за текущей git-веткой и автоматически пересобирает
# при появлении новых коммитов.
#
# Использование:
#   ./scripts/autodeploy.sh              # интервал 30 сек (по умолчанию)
#   ./scripts/autodeploy.sh --interval 60  # интервал 60 сек
#   ./scripts/autodeploy.sh --once         # один раз проверить и выйти
#
# Устанавливается как systemd-сервис через:
#   sudo ./scripts/install-autodeploy.sh
# =============================================================================

set -euo pipefail

# --- Конфигурация ---
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
INTERVAL=30
ONCE=false
LOG_PREFIX="[autodeploy]"

# Telegram-уведомления (задай в .env.autodeploy или через env)
# AUTODEPLOY_TG_BOT_TOKEN=123456:ABC...
# AUTODEPLOY_TG_CHAT_ID=-100123456789
TG_BOT_TOKEN="${AUTODEPLOY_TG_BOT_TOKEN:-}"
TG_CHAT_ID="${AUTODEPLOY_TG_CHAT_ID:-}"

# --- Парсинг аргументов ---
while [[ $# -gt 0 ]]; do
    case "$1" in
        --interval) INTERVAL="$2"; shift 2 ;;
        --once) ONCE=true; shift ;;
        --project-dir) PROJECT_DIR="$2"; shift 2 ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

cd "$PROJECT_DIR"

# Загружаем .env.autodeploy если есть
if [ -f "$PROJECT_DIR/.env.autodeploy" ]; then
    set -a
    source "$PROJECT_DIR/.env.autodeploy"
    set +a
    TG_BOT_TOKEN="${AUTODEPLOY_TG_BOT_TOKEN:-$TG_BOT_TOKEN}"
    TG_CHAT_ID="${AUTODEPLOY_TG_CHAT_ID:-$TG_CHAT_ID}"
fi

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') $LOG_PREFIX $*"
}

tg_send() {
    # Отправка сообщения в Telegram (логируем ошибки, но не останавливаемся)
    local text="$1"
    if [ -n "$TG_BOT_TOKEN" ] && [ -n "$TG_CHAT_ID" ]; then
        local response
        log "Sending Telegram notification..."
        response=$(curl -s --connect-timeout 10 --max-time 30 \
            -X POST "https://api.telegram.org/bot${TG_BOT_TOKEN}/sendMessage" \
            --data-urlencode "text=$text" \
            -d "chat_id=$TG_CHAT_ID" \
            -d "parse_mode=HTML" \
            -d "disable_web_page_preview=true" 2>&1) || true
        if echo "$response" | grep -q '"ok":true'; then
            log "Telegram notification sent"
        else
            log "WARNING: Telegram send failed: $response"
        fi
    else
        log "WARNING: TG_BOT_TOKEN or TG_CHAT_ID not set, skipping notification"
    fi
}

deploy() {
    local branch="$1"
    local old_hash="$2"
    local new_hash="$3"

    log "New commits detected on '$branch': ${old_hash:0:7} -> ${new_hash:0:7}"

    # Собираем инфу о коммитах
    local commits_text
    commits_text=$(git log --oneline "$old_hash..$new_hash" 2>/dev/null | head -10)
    local commit_count
    commit_count=$(echo "$commits_text" | wc -l)
    local last_author
    last_author=$(git log -1 --format='%an' "$new_hash" 2>/dev/null || echo "unknown")
    local last_message
    last_message=$(git log -1 --format='%s' "$new_hash" 2>/dev/null || echo "")

    # Показываем что изменилось
    log "Changes:"
    echo "$commits_text" | while read -r line; do
        log "  $line"
    done

    # TG: начало деплоя
    local commits_formatted
    commits_formatted=$(echo "$commits_text" | sed 's/^/<code>/;s/$/<\/code>/' | tr '\n' '\n')
    tg_send "$(cat <<MSG
<b>🔄 Деплой начат</b>

<b>Ветка:</b> <code>$branch</code>
<b>Коммиты:</b> ${old_hash:0:7} → ${new_hash:0:7} ($commit_count шт.)
<b>Автор:</b> $last_author
<b>Последний:</b> $last_message

$commits_formatted
MSG
)"

    # Пересборка
    log "Rebuilding containers..."
    local deploy_start
    deploy_start=$(date +%s)
    if docker compose build seller-platform 2>&1 | tail -5; then
        log "Build successful, restarting..."
        docker compose up -d seller-platform 2>&1 | tail -3

        # Ждём healthcheck
        log "Waiting for healthcheck (max 60s)..."
        local i=0
        while [ $i -lt 12 ]; do
            sleep 5
            i=$((i + 1))
            local health
            health=$(docker inspect --format='{{.State.Health.Status}}' "$(docker compose ps -q seller-platform 2>/dev/null)" 2>/dev/null || echo "unknown")
            if [ "$health" = "healthy" ]; then
                log "Container is healthy!"
                break
            fi
            log "  status: $health ($((i*5))s)"
        done

        # Пересобираем агентов, если Dockerfile.agents изменился
        if git diff --name-only "$old_hash..$new_hash" | grep -qE 'Dockerfile\.agents|agents/|docker-entrypoint-agent'; then
            log "Agent files changed, rebuilding agents..."
            docker compose --profile agents build 2>&1 | tail -3
            docker compose --profile agents up -d 2>&1 | tail -3
            log "Agents restarted"
        fi

        # Чистим Docker кеш после успешной сборки
        log "Cleaning Docker build cache and dangling images..."
        local freed
        freed=$(docker system prune -f 2>&1 | grep -oP 'reclaimed .*' || echo "")
        docker builder prune -f 2>&1 | tail -1
        log "Docker cache cleaned${freed:+: $freed}"

        local deploy_end
        deploy_end=$(date +%s)
        local duration=$(( deploy_end - deploy_start ))

        log "Deploy complete!"

        # TG: успех
        tg_send "$(cat <<MSG
<b>✅ Деплой завершён</b>

<b>Ветка:</b> <code>$branch</code> → <code>${new_hash:0:7}</code>
<b>Время:</b> ${duration} сек
<b>Коммит:</b> $last_message
<b>Docker cache:</b> очищен
MSG
)"
    else
        log "ERROR: Build failed! Containers NOT restarted."

        # TG: ошибка
        tg_send "$(cat <<MSG
<b>❌ Деплой ПРОВАЛЕН</b>

<b>Ветка:</b> <code>$branch</code>
<b>Коммит:</b> ${new_hash:0:7} — $last_message
<b>Автор:</b> $last_author

Docker build failed. Контейнеры НЕ перезапущены.
Смотри логи: <code>journalctl -u seller-autodeploy -n 50</code>
MSG
)"
        return 1
    fi
}

# --- Главный цикл ---
log "Starting autodeploy watcher"
log "  Project: $PROJECT_DIR"
log "  Interval: ${INTERVAL}s"

BRANCH=$(git rev-parse --abbrev-ref HEAD)
LAST_HASH=$(git rev-parse HEAD)
log "  Branch: $BRANCH"
log "  Current: ${LAST_HASH:0:7}"

while true; do
    # Тихо проверяем remote
    if git fetch origin "$BRANCH" 2>/dev/null; then
        REMOTE_HASH=$(git rev-parse "origin/$BRANCH" 2>/dev/null || echo "")

        if [ -n "$REMOTE_HASH" ] && [ "$REMOTE_HASH" != "$LAST_HASH" ]; then
            # Есть новые коммиты — pull и деплой
            git pull --ff-only origin "$BRANCH" 2>/dev/null || {
                log "WARNING: fast-forward pull failed, trying merge..."
                git pull origin "$BRANCH" 2>/dev/null || {
                    log "ERROR: git pull failed, skipping this cycle"
                    sleep "$INTERVAL"
                    continue
                }
            }

            deploy "$BRANCH" "$LAST_HASH" "$REMOTE_HASH" || true
            LAST_HASH="$REMOTE_HASH"
        fi
    else
        log "WARNING: git fetch failed (network issue?), retrying in ${INTERVAL}s"
    fi

    if [ "$ONCE" = true ]; then
        log "Single check mode, exiting"
        exit 0
    fi

    sleep "$INTERVAL"
done


