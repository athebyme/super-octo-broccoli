#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"
PYTHON_BIN="$PROJECT_DIR/venv/bin/python"
[ -x "$PYTHON_BIN" ] || PYTHON_BIN=python3

echo "========================================="
echo "🔧 Скрипт исправления проблемы с базой данных"
echo "========================================="
echo ""

# Цвета для вывода
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo "Шаг 1: Проверка текущего состояния"
echo "-----------------------------------"

if [ ! -f .env ]; then
    echo -e "${RED}✗ Файл .env не найден!${NC}"
    if [ -f .env.example ]; then
        echo -e "${YELLOW}Копирую .env.example в .env...${NC}"
        cp .env.example .env
        echo -e "${GREEN}✓ .env создан${NC}"
    else
        echo -e "${RED}✗ .env.example тоже не найден!${NC}"
        exit 1
    fi
else
    echo -e "${GREEN}✓ Файл .env существует${NC}"
fi

echo ""
echo "Проверка DATABASE_URL в .env:"
grep DATABASE_URL .env || echo -e "${RED}DATABASE_URL не найден!${NC}"

echo ""
echo "Содержимое ./data/ ДО пересборки:"
ls -lh ./data/

echo ""
echo "Шаг 2: Остановка контейнеров"
echo "-----------------------------------"
echo -e "${YELLOW}Пересборка выполняется с ожиданием завершения активных native Flash вызовов; volume базы данных сохраняется.${NC}"
"$PYTHON_BIN" "$SCRIPT_DIR/deploy_safety.py" \
    --project-dir "$PROJECT_DIR" --no-cache --compose-down
echo -e "${GREEN}✓ Контейнеры безопасно пересозданы${NC}"

echo ""
echo "Шаг 5: Ожидание инициализации (10 секунд)"
echo "-----------------------------------"
for i in {10..1}; do
    echo -n "$i "
    sleep 1
done
echo ""

echo ""
echo "Шаг 6: Проверка логов инициализации"
echo "-----------------------------------"
    docker compose logs seller-platform | grep -E "Используется база данных|администратор|Базовая структура БД"

echo ""
echo "Шаг 7: Проверка создания базы данных"
echo "-----------------------------------"
if [ -f ./data/seller_platform.db ]; then
    DB_SIZE=$(du -h ./data/seller_platform.db | cut -f1)
    echo -e "${GREEN}✓ База данных создана!${NC}"
    echo "  Путь: ./data/seller_platform.db"
    echo "  Размер: $DB_SIZE"

    echo ""
    echo "Содержимое ./data/ ПОСЛЕ пересборки:"
    ls -lh ./data/

    echo ""
    echo -e "${GREEN}=========================================${NC}"
    echo -e "${GREEN}✓ УСПЕШНО! База данных создана правильно${NC}"
    echo -e "${GREEN}=========================================${NC}"
    echo ""
    echo "Теперь можете войти в систему:"
    echo "  URL: http://ваш-сервер:5001"
    echo "  Логин: admin"
    echo "  Пароль: admin123"
else
    echo -e "${RED}✗ ОШИБКА! База данных НЕ создана в ./data/${NC}"
    echo ""
    echo "Содержимое ./data/ ПОСЛЕ пересборки:"
    ls -lh ./data/

    echo ""
    echo "Полные логи контейнера:"
    docker-compose logs seller-platform | tail -50

    echo ""
    echo -e "${RED}=========================================${NC}"
    echo -e "${RED}Необходима дополнительная диагностика${NC}"
    echo -e "${RED}=========================================${NC}"
    exit 1
fi
