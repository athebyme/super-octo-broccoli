#!/bin/bash
# Скрипт для восстановления PostgreSQL базы данных из бэкапа

set -e

if [ -z "$1" ]; then
    echo "❌ Использование: ./scripts/restore_database.sh <путь_к_файлу_бэкапа>"
    echo ""
    echo "Доступные бэкапы:"
    ls -lh backups/*.dump 2>/dev/null || echo "  (бэкапов не найдено)"
    exit 1
fi

BACKUP_FILE="$1"
CONTAINER_NAME="seller-postgres"
POSTGRES_USER="${POSTGRES_USER:-seller}"
POSTGRES_DB="${POSTGRES_DB:-seller_platform}"

if [ ! -f "$BACKUP_FILE" ]; then
    echo "❌ Файл не найден: $BACKUP_FILE"
    exit 1
fi

echo "⚠️  ВНИМАНИЕ: Это перезапишет текущую базу данных!"
echo "   Файл: $BACKUP_FILE"
read -p "Продолжить? (yes/no): " confirm

if [ "$confirm" != "yes" ]; then
    echo "Отменено"
    exit 0
fi

echo "🔄 Восстановление базы данных..."

# Остановить приложение чтобы не было подключений
echo "⏸️  Остановка seller-platform..."
docker compose stop seller-platform 2>/dev/null || true

# Пересоздать базу и восстановить из дампа
echo "📦 Восстановление из дампа..."
docker exec -i ${CONTAINER_NAME} sh -c "
    dropdb -U ${POSTGRES_USER} --if-exists ${POSTGRES_DB} &&
    createdb -U ${POSTGRES_USER} ${POSTGRES_DB}
" && cat "${BACKUP_FILE}" | docker exec -i ${CONTAINER_NAME} pg_restore \
    -U "${POSTGRES_USER}" \
    -d "${POSTGRES_DB}" \
    --no-owner \
    --no-privileges \
    --clean \
    --if-exists 2>/dev/null || true

echo "✅ База данных восстановлена"
echo "🔄 Перезапуск seller-platform..."

docker compose up -d seller-platform

echo "✅ Готово!"
