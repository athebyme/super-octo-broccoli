#!/bin/bash
# Агрессивная очистка и пересборка

set -e

echo "🧹 Полная очистка Docker..."

# 1. Останавливаем всё
docker-compose down -v 2>/dev/null || true

# 2. Удаляем образы этого проекта
docker rmi super-octo-broccoli_seller-platform 2>/dev/null || true
docker rmi super-octo-broccoli_wb-calculator 2>/dev/null || true

# 3. Принудительно обновляем timestamp файлов Python
touch seller_platform.py
touch app.py

# 4. Пересборка с нуля
echo "🏗️  Пересборка образов с нуля..."
docker-compose build --no-cache --pull

# 5. Запуск
echo "🚀 Запуск контейнеров..."
docker-compose up -d

# 6. Ожидание
echo "⏳ Ожидание инициализации (15 секунд)..."
sleep 15

# 7. Проверка
echo ""
echo "📋 Проверка DATABASE_URL в контейнере:"
docker-compose exec -T seller-platform env | grep DATABASE || echo "Переменная не найдена"

echo ""
echo "📋 Проверка из логов:"
docker-compose logs seller-platform | grep "Используется база данных" | tail -1

echo ""
echo "✅ Готово! Теперь запустите: ./init_database.sh"
