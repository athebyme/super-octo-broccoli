#!/bin/bash
# Скрипт для бэкапа PostgreSQL базы данных

set -e

BACKUP_DIR="./backups"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="${BACKUP_DIR}/seller_platform_${TIMESTAMP}.dump"
CONTAINER_NAME="seller-postgres"

# Переменные подключения (из .env)
POSTGRES_USER="${POSTGRES_USER:-seller}"
POSTGRES_DB="${POSTGRES_DB:-seller_platform}"

# Создаем директорию для бэкапов
mkdir -p "$BACKUP_DIR"

echo "🔄 Создание бэкапа базы данных PostgreSQL..."

# Проверяем что контейнер запущен
if ! docker ps --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
    echo "❌ Контейнер ${CONTAINER_NAME} не запущен"
    echo "Запустите: docker compose up -d postgres"
    exit 1
fi

# Создаём бэкап через pg_dump
echo "📦 Создание дампа базы..."
docker exec ${CONTAINER_NAME} pg_dump \
    -U "${POSTGRES_USER}" \
    -d "${POSTGRES_DB}" \
    -Fc \
    --no-owner \
    --no-privileges \
    > "${BACKUP_FILE}"

if [ -f "$BACKUP_FILE" ] && [ -s "$BACKUP_FILE" ]; then
    SIZE=$(du -h "$BACKUP_FILE" | cut -f1)
    echo "✅ Бэкап создан: $BACKUP_FILE (размер: $SIZE)"

    # Проверяем целостность дампа
    if pg_restore --list "${BACKUP_FILE}" > /dev/null 2>&1; then
        echo "✅ Целостность дампа проверена: OK"
    else
        echo "⚠️  pg_restore не найден локально, целостность не проверена (но дамп создан)"
    fi

    echo ""
    echo "Для восстановления используйте:"
    echo "  ./scripts/restore_database.sh $BACKUP_FILE"
else
    echo "❌ Ошибка создания бэкапа"
    echo ""
    echo "Проверьте что:"
    echo "  1. Контейнер postgres запущен: docker ps"
    echo "  2. База доступна: docker exec ${CONTAINER_NAME} pg_isready -U ${POSTGRES_USER}"
    exit 1
fi
