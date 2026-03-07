#!/bin/bash
# =============================================================================
# deploy.sh — Автодеплой с переключением веток и очисткой Docker-кеша
#
# Использование:
#   ./deploy.sh                   — деплой текущей ветки
#   ./deploy.sh feature/new-ui    — переключиться на ветку и задеплоить
#   ./deploy.sh main --clean      — деплой с полной очисткой кеша
#   ./deploy.sh --status          — показать текущее состояние
#   ./deploy.sh --prune           — только очистка Docker (без деплоя)
#
# Переменные окружения:
#   DEPLOY_KEEP_IMAGES=3          — сколько последних образов оставлять (по умолч. 3)
#   DEPLOY_NO_CACHE=0             — билдить без Docker-кеша (по умолч. 0)
#   DEPLOY_SERVICES="seller-platform" — какие сервисы пересобирать (по умолч. все)
# =============================================================================

set -euo pipefail

# ─── Цвета ───
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# ─── Настройки ───
KEEP_IMAGES="${DEPLOY_KEEP_IMAGES:-3}"
NO_CACHE="${DEPLOY_NO_CACHE:-0}"
SERVICES="${DEPLOY_SERVICES:-}"
COMPOSE_FILE="docker-compose.yml"
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"

cd "$PROJECT_DIR"

# ─── Функции ───

log_info()  { echo -e "${BLUE}[INFO]${NC}  $1"; }
log_ok()    { echo -e "${GREEN}[OK]${NC}    $1"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC}  $1"; }
log_err()   { echo -e "${RED}[ERR]${NC}   $1"; }

show_status() {
    echo ""
    echo -e "${CYAN}═══════════════════════════════════════${NC}"
    echo -e "${CYAN}  Текущее состояние${NC}"
    echo -e "${CYAN}═══════════════════════════════════════${NC}"
    echo ""

    # Git
    local branch=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "n/a")
    local commit=$(git rev-parse --short HEAD 2>/dev/null || echo "n/a")
    local dirty=""
    if ! git diff --quiet 2>/dev/null; then dirty=" (есть изменения)"; fi
    echo -e "  Git ветка:    ${GREEN}${branch}${NC}${YELLOW}${dirty}${NC}"
    echo -e "  Коммит:       ${commit}"
    echo ""

    # Ветки
    echo -e "  ${BLUE}Локальные ветки:${NC}"
    git branch --format='    %(refname:short)' 2>/dev/null | while read b; do
        if [ "$(echo "$b" | xargs)" = "$branch" ]; then
            echo -e "  ${GREEN}* $(echo "$b" | xargs)${NC}"
        else
            echo "   $b"
        fi
    done
    echo ""

    # Docker
    echo -e "  ${BLUE}Docker контейнеры:${NC}"
    docker compose -f "$COMPOSE_FILE" ps --format "table {{.Name}}\t{{.Status}}\t{{.Ports}}" 2>/dev/null || echo "    (compose недоступен)"
    echo ""

    # Docker диск
    echo -e "  ${BLUE}Docker диск:${NC}"
    docker system df 2>/dev/null | sed 's/^/    /'
    echo ""

    # Dangling images
    local dangling=$(docker images -f "dangling=true" -q 2>/dev/null | wc -l)
    if [ "$dangling" -gt 0 ]; then
        echo -e "  ${YELLOW}Висячих образов: ${dangling}${NC}"
    else
        echo -e "  ${GREEN}Висячих образов: 0${NC}"
    fi
    echo ""
}

docker_prune() {
    log_info "Очистка Docker..."

    # 1. Удаляем dangling images (образы без тега — остатки от прошлых билдов)
    local dangling=$(docker images -f "dangling=true" -q 2>/dev/null)
    if [ -n "$dangling" ]; then
        local count=$(echo "$dangling" | wc -l)
        log_info "Удаление $count висячих образов..."
        echo "$dangling" | xargs docker rmi -f 2>/dev/null || true
        log_ok "Висячие образы удалены"
    fi

    # 2. Удаляем старые образы проекта, оставляя KEEP_IMAGES последних
    local project_name=$(basename "$PROJECT_DIR")
    # Ищем образы по имени проекта
    local project_images=$(docker images --format "{{.ID}} {{.Repository}} {{.CreatedAt}}" 2>/dev/null \
        | grep -i "$project_name" \
        | sort -k3 -r \
        | tail -n +$((KEEP_IMAGES + 1)) \
        | awk '{print $1}')

    if [ -n "$project_images" ]; then
        local count=$(echo "$project_images" | wc -l)
        log_info "Удаление $count старых образов проекта (оставляем последние $KEEP_IMAGES)..."
        echo "$project_images" | xargs docker rmi -f 2>/dev/null || true
        log_ok "Старые образы удалены"
    fi

    # 3. Очищаем build cache
    log_info "Очистка Docker build cache..."
    docker builder prune -f --filter "until=72h" 2>/dev/null || true

    # 4. Удаляем неиспользуемые сети
    docker network prune -f 2>/dev/null || true

    # Итого
    local freed=$(docker system df 2>/dev/null | tail -1 | awk '{print $4}')
    log_ok "Очистка завершена. Можно вернуть: ${freed:-n/a}"
}

switch_branch() {
    local target_branch="$1"
    local current_branch=$(git rev-parse --abbrev-ref HEAD 2>/dev/null)

    if [ "$current_branch" = "$target_branch" ]; then
        log_info "Уже на ветке $target_branch"
        return 0
    fi

    # Проверяем незакоммиченные изменения
    if ! git diff --quiet 2>/dev/null || ! git diff --cached --quiet 2>/dev/null; then
        log_warn "Есть незакоммиченные изменения. Сохраняю в stash..."
        git stash push -m "deploy-auto-stash-$(date +%Y%m%d-%H%M%S)"
        log_ok "Изменения сохранены в stash"
    fi

    # Пробуем переключиться
    log_info "Переключение на ветку: $target_branch"

    # Сначала fetch, чтобы знать про удалённые ветки
    git fetch origin "$target_branch" 2>/dev/null || git fetch origin 2>/dev/null || true

    if git show-ref --verify --quiet "refs/heads/$target_branch" 2>/dev/null; then
        # Локальная ветка существует
        git checkout "$target_branch"
        git pull origin "$target_branch" 2>/dev/null || log_warn "Не удалось подтянуть изменения из remote"
    elif git show-ref --verify --quiet "refs/remotes/origin/$target_branch" 2>/dev/null; then
        # Только remote ветка
        git checkout -b "$target_branch" "origin/$target_branch"
    else
        log_err "Ветка '$target_branch' не найдена ни локально, ни в remote"
        return 1
    fi

    log_ok "Переключено на ветку: $target_branch"
}

do_deploy() {
    local start_time=$(date +%s)

    echo ""
    echo -e "${CYAN}═══════════════════════════════════════${NC}"
    echo -e "${CYAN}  Деплой: $(git rev-parse --abbrev-ref HEAD)${NC}"
    echo -e "${CYAN}  Коммит: $(git rev-parse --short HEAD)${NC}"
    echo -e "${CYAN}═══════════════════════════════════════${NC}"
    echo ""

    # Собираем аргументы для docker compose build
    local build_args=""
    if [ "$NO_CACHE" = "1" ]; then
        build_args="--no-cache"
        log_info "Билд без кеша (DEPLOY_NO_CACHE=1)"
    fi

    # Определяем сервисы для билда
    local target_services=""
    if [ -n "$SERVICES" ]; then
        target_services="$SERVICES"
        log_info "Пересборка сервисов: $target_services"
    fi

    # Build
    log_info "Сборка Docker образов..."
    docker compose -f "$COMPOSE_FILE" build $build_args $target_services
    log_ok "Образы собраны"

    # Stop + Up
    log_info "Перезапуск контейнеров..."
    docker compose -f "$COMPOSE_FILE" up -d $target_services
    log_ok "Контейнеры запущены"

    # Автоочистка после билда
    docker_prune

    # Результат
    local end_time=$(date +%s)
    local duration=$((end_time - start_time))
    echo ""
    log_ok "Деплой завершён за ${duration}с"
    echo ""

    # Краткий статус
    docker compose -f "$COMPOSE_FILE" ps --format "table {{.Name}}\t{{.Status}}" 2>/dev/null
    echo ""
}

show_help() {
    echo ""
    echo -e "${CYAN}Автодеплой с управлением ветками и Docker-кешем${NC}"
    echo ""
    echo "Использование:"
    echo "  ./deploy.sh                       деплой текущей ветки"
    echo "  ./deploy.sh <branch>              переключиться на ветку и задеплоить"
    echo "  ./deploy.sh <branch> --clean      деплой с полной очисткой кеша"
    echo "  ./deploy.sh --status              показать состояние"
    echo "  ./deploy.sh --prune               только очистка Docker"
    echo "  ./deploy.sh --branches            список веток"
    echo "  ./deploy.sh --help                эта справка"
    echo ""
    echo "Переменные окружения:"
    echo "  DEPLOY_KEEP_IMAGES=3              сколько образов оставлять"
    echo "  DEPLOY_NO_CACHE=0                 билдить без Docker-кеша"
    echo "  DEPLOY_SERVICES=\"seller-platform\"  какие сервисы пересобирать"
    echo ""
}

show_branches() {
    local current=$(git rev-parse --abbrev-ref HEAD 2>/dev/null)
    echo ""
    echo -e "${CYAN}Локальные ветки:${NC}"
    git branch --format='%(refname:short)' | while read b; do
        if [ "$b" = "$current" ]; then
            echo -e "  ${GREEN}* $b${NC} (текущая)"
        else
            echo "    $b"
        fi
    done

    echo ""
    echo -e "${CYAN}Remote ветки:${NC}"
    git fetch origin --prune 2>/dev/null || true
    git branch -r --format='%(refname:short)' | grep -v HEAD | while read b; do
        echo "    $b"
    done
    echo ""
}

# ─── Разбор аргументов ───

TARGET_BRANCH=""
CLEAN_MODE=0

for arg in "$@"; do
    case "$arg" in
        --status)
            show_status
            exit 0
            ;;
        --prune)
            docker_prune
            exit 0
            ;;
        --branches)
            show_branches
            exit 0
            ;;
        --clean)
            CLEAN_MODE=1
            ;;
        --help|-h)
            show_help
            exit 0
            ;;
        *)
            if [ -z "$TARGET_BRANCH" ]; then
                TARGET_BRANCH="$arg"
            else
                log_err "Неизвестный аргумент: $arg"
                show_help
                exit 1
            fi
            ;;
    esac
done

# ─── Основной flow ───

# Полная очистка если --clean
if [ "$CLEAN_MODE" = "1" ]; then
    NO_CACHE=1
    log_info "Режим полной очистки"
fi

# Переключение ветки если указана
if [ -n "$TARGET_BRANCH" ]; then
    switch_branch "$TARGET_BRANCH"
fi

# Деплой
do_deploy
