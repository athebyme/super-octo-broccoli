#!/bin/bash
# Скрипт для восстановления базы данных в Docker volume

set -e

if [ -z "$1" ]; then
  echo "❌ Использование: ./restore_database.sh <путь_к_файлу_бэкапа>"
  echo ""
  echo "Доступные бэкапы:"
  ls -lh backups/*.db 2>/dev/null || echo "  (бэкапов не найдено)"
  exit 1
fi

BACKUP_FILE="$1"

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

# Копируем бэкап в Docker volume
docker run --rm \
  -v super-octo-broccoli_seller_platform_data:/data \
  -v "$(pwd)/$(dirname "$BACKUP_FILE"):/backup" \
  alpine \
  cp "/backup/$(basename "$BACKUP_FILE")" /data/seller_platform.db

echo "✅ База данных восстановлена"
echo "🔄 Перезапуск контейнера..."

docker compose restart seller-platform

echo "✅ Готово!"
