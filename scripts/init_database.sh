#!/bin/bash
# Скрипт для проверки инициализации PostgreSQL базы данных

set -e

echo "🔧 Проверка базы данных PostgreSQL..."

if ! command -v docker &> /dev/null; then
    echo "❌ Docker команда не найдена"
    exit 1
fi

PG_CONTAINER="seller-postgres"
APP_CONTAINER="seller-platform"

# Проверяем что PostgreSQL контейнер запущен
if ! docker ps --format '{{.Names}}' | grep -q "^${PG_CONTAINER}$"; then
    echo "❌ Контейнер ${PG_CONTAINER} не запущен"
    echo "Запустите: docker compose up -d postgres"
    exit 1
fi

# Проверяем что PostgreSQL отвечает
echo "📋 Проверка подключения к PostgreSQL..."
if docker exec ${PG_CONTAINER} pg_isready -U "${POSTGRES_USER:-seller}" > /dev/null 2>&1; then
    echo "✅ PostgreSQL доступен"
else
    echo "❌ PostgreSQL не отвечает"
    exit 1
fi

# Проверяем что база существует
POSTGRES_USER="${POSTGRES_USER:-seller}"
POSTGRES_DB="${POSTGRES_DB:-seller_platform}"

DB_EXISTS=$(docker exec ${PG_CONTAINER} psql -U "${POSTGRES_USER}" -lqt 2>/dev/null | grep -c "${POSTGRES_DB}" || echo "0")

if [ "$DB_EXISTS" -gt "0" ]; then
    # Считаем таблицы
    TABLE_COUNT=$(docker exec ${PG_CONTAINER} psql -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" -tAc \
        "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public';" 2>/dev/null || echo "0")
    echo "✅ База данных '${POSTGRES_DB}' существует (таблиц: ${TABLE_COUNT})"
else
    echo "⚠️  База данных '${POSTGRES_DB}' не найдена"
    echo "   Она будет создана при запуске seller-platform (flask db upgrade)"
fi

echo ""
echo "✅ Проверка завершена!"
