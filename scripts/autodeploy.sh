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

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') $LOG_PREFIX $*"
}

deploy() {
    local branch="$1"
    local old_hash="$2"
    local new_hash="$3"

    log "New commits detected on '$branch': ${old_hash:0:7} -> ${new_hash:0:7}"

    # Показываем что изменилось
    log "Changes:"
    git log --oneline "$old_hash..$new_hash" 2>/dev/null | head -10 | while read -r line; do
        log "  $line"
    done

    # Пересборка
    log "Rebuilding containers..."
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

        log "Deploy complete!"
    else
        log "ERROR: Build failed! Containers NOT restarted."
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
