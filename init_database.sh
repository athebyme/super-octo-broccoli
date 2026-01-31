#!/bin/bash
# Скрипт для инициализации базы данных в Docker volume

set -e

echo "🔧 Инициализация базы данных..."

# Проверяем что контейнер запущен
if ! command -v docker &> /dev/null; then
    echo "❌ Docker команда не найдена"
    exit 1
fi

CONTAINER_NAME="seller-platform"

# Проверяем что контейнер существует
if ! docker ps -a --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
    echo "❌ Контейнер ${CONTAINER_NAME} не найден"
    echo "Запустите: docker-compose up -d"
    exit 1
fi

# Проверяем что контейнер запущен
if ! docker ps --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
    echo "⚠️  Контейнер ${CONTAINER_NAME} не запущен"
    echo "Запускаю контейнер..."
    docker-compose up -d seller-platform
    sleep 5
fi

# Проверяем существует ли база в volume
echo "📋 Проверка базы данных в volume..."
DB_EXISTS=$(docker exec ${CONTAINER_NAME} test -f /app/data/seller_platform.db && echo "yes" || echo "no")

if [ "$DB_EXISTS" = "yes" ]; then
    DB_SIZE=$(docker exec ${CONTAINER_NAME} du -h /app/data/seller_platform.db 2>/dev/null | cut -f1 || echo "unknown")
    echo "✅ База данных существует (размер: ${DB_SIZE})"

    # Проверяем что это валидная SQLite база (проверяем заголовок файла)
    HEADER=$(docker exec ${CONTAINER_NAME} head -c 16 /app/data/seller_platform.db 2>/dev/null || echo "")
    if [[ "$HEADER" == *"SQLite"* ]]; then
        echo "✅ База данных валидная (SQLite формат)"
    else
        echo "⚠️  Предупреждение: файл может быть повреждён"
    fi

    echo "✅ База данных инициализирована корректно"
else
    echo "⚠️  База данных не найдена в /app/data/"
    echo "🔍 Проверяю логи контейнера для диагностики..."
    echo ""

    docker-compose logs seller-platform | grep "Используется база данных" | tail -1

    echo ""
    echo "❌ База данных НЕ найдена в volume"
    echo ""
    echo "Возможные причины:"
    echo "  1. Docker использовал старый кэшированный код"
    echo "  2. База создаётся в неправильном месте"
    echo ""
    echo "Решение:"
    echo "  ./rebuild.sh  # пересобрать БЕЗ кэша"
    exit 1
fi

echo ""
echo "✅ Инициализация завершена успешно!"
