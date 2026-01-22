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
    DB_SIZE=$(docker exec ${CONTAINER_NAME} du -h /app/data/seller_platform.db | cut -f1)
    echo "✅ База данных существует (размер: ${DB_SIZE})"

    # Проверяем количество таблиц
    TABLE_COUNT=$(docker exec ${CONTAINER_NAME} sqlite3 /app/data/seller_platform.db "SELECT COUNT(*) FROM sqlite_master WHERE type='table';" 2>/dev/null || echo "0")
    echo "📊 Количество таблиц: ${TABLE_COUNT}"

    if [ "$TABLE_COUNT" -gt 0 ]; then
        echo "✅ База данных инициализирована корректно"

        # Показываем некоторую статистику
        echo ""
        echo "📈 Статистика базы данных:"
        docker exec ${CONTAINER_NAME} sqlite3 /app/data/seller_platform.db <<SQL
.mode column
SELECT
    name as 'Таблица',
    (SELECT COUNT(*) FROM sqlite_master sm WHERE sm.tbl_name = m.name AND sm.type != 'table') as 'Индексы'
FROM sqlite_master m
WHERE type='table' AND name NOT LIKE 'sqlite_%'
ORDER BY name;
SQL
    else
        echo "⚠️  База существует но не содержит таблиц"
        echo "🔄 Требуется перезапуск для инициализации..."
        docker-compose restart seller-platform
        sleep 10

        # Повторная проверка
        TABLE_COUNT=$(docker exec ${CONTAINER_NAME} sqlite3 /app/data/seller_platform.db "SELECT COUNT(*) FROM sqlite_master WHERE type='table';" 2>/dev/null || echo "0")
        if [ "$TABLE_COUNT" -gt 0 ]; then
            echo "✅ База инициализирована после перезапуска"
        else
            echo "❌ База не инициализирована. Проверьте логи:"
            echo "   docker-compose logs seller-platform"
            exit 1
        fi
    fi
else
    echo "⚠️  База данных не найдена"
    echo "🔄 Перезапускаю контейнер для создания базы..."

    docker-compose restart seller-platform
    sleep 10

    # Проверка после перезапуска
    DB_EXISTS=$(docker exec ${CONTAINER_NAME} test -f /app/data/seller_platform.db && echo "yes" || echo "no")
    if [ "$DB_EXISTS" = "yes" ]; then
        echo "✅ База данных создана"
    else
        echo "❌ Не удалось создать базу данных"
        echo ""
        echo "📋 Последние логи контейнера:"
        docker-compose logs --tail=50 seller-platform
        exit 1
    fi
fi

echo ""
echo "✅ Инициализация завершена успешно!"
