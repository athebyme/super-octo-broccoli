#!/bin/bash
# Скрипт для бэкапа базы данных из Docker volume

set -e

BACKUP_DIR="./backups"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="${BACKUP_DIR}/seller_platform_${TIMESTAMP}.db"
CONTAINER_NAME="seller-platform"

# Создаем директорию для бэкапов
mkdir -p "$BACKUP_DIR"

echo "🔄 Создание бэкапа базы данных..."

# Проверяем что контейнер запущен
if ! docker ps --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
    echo "⚠️  Контейнер ${CONTAINER_NAME} не запущен"
    echo "Попытка создать бэкап из volume напрямую..."

    # Пробуем скопировать из volume напрямую
    docker run --rm \
      -v super-octo-broccoli_seller_platform_data:/data \
      -v "$(pwd)/${BACKUP_DIR}:/backup" \
      alpine \
      sh -c "if [ -f /data/seller_platform.db ]; then cp /data/seller_platform.db /backup/seller_platform_${TIMESTAMP}.db; else echo 'Database file not found in volume'; exit 1; fi"
else
    # Контейнер запущен, копируем через docker cp
    echo "📦 Копирование базы из запущенного контейнера..."
    docker cp ${CONTAINER_NAME}:/app/data/seller_platform.db "${BACKUP_FILE}"
fi

if [ -f "$BACKUP_FILE" ]; then
  SIZE=$(du -h "$BACKUP_FILE" | cut -f1)
  echo "✅ Бэкап создан: $BACKUP_FILE (размер: $SIZE)"

  # Проверяем целостность базы
  if sqlite3 "$BACKUP_FILE" "PRAGMA integrity_check;" | grep -q "ok"; then
    echo "✅ Целостность базы проверена: OK"
  else
    echo "⚠️  Предупреждение: возможны проблемы с целостностью базы"
  fi

  echo ""
  echo "Для восстановления используйте:"
  echo "  ./restore_database.sh $BACKUP_FILE"
else
  echo "❌ Ошибка создания бэкапа"
  echo ""
  echo "Проверьте что:"
  echo "  1. Контейнер seller-platform запущен: docker ps"
  echo "  2. База данных существует: docker exec seller-platform ls -lh /app/data/"
  echo "  3. Volume существует: docker volume ls | grep seller_platform"
  exit 1
fi
