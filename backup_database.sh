#!/bin/bash
# Скрипт для бэкапа базы данных из Docker volume

set -e

BACKUP_DIR="./backups"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="${BACKUP_DIR}/seller_platform_${TIMESTAMP}.db"

# Создаем директорию для бэкапов
mkdir -p "$BACKUP_DIR"

echo "🔄 Создание бэкапа базы данных..."

# Копируем базу из Docker volume
docker run --rm \
  -v super-octo-broccoli_seller_platform_data:/data \
  -v "$(pwd)/${BACKUP_DIR}:/backup" \
  alpine \
  cp /data/seller_platform.db "/backup/seller_platform_${TIMESTAMP}.db"

if [ -f "$BACKUP_FILE" ]; then
  SIZE=$(du -h "$BACKUP_FILE" | cut -f1)
  echo "✅ Бэкап создан: $BACKUP_FILE (размер: $SIZE)"
  echo ""
  echo "Для восстановления используйте:"
  echo "  ./restore_database.sh $BACKUP_FILE"
else
  echo "❌ Ошибка создания бэкапа"
  exit 1
fi
