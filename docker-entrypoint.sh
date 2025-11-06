#!/bin/sh
set -e

APP_MODULE=${APP_MODULE:-seller_platform:app}
PORT=${PORT:-5001}

# Ensure all working directories exist so uploads and reports survive volume mounts.
mkdir -p uploads processed data

# Run lightweight initialization depending on the application we serve.
if [ "$APP_MODULE" = "seller_platform:app" ]; then
echo "🚀 Инициализация seller-platform..."

# Запуск миграций БД
echo "📦 Применение миграций базы данных..."
python migrate_db.py --db-path data/seller_platform.db
python migrate_add_characteristics.py data/seller_platform.db
python migrate_add_history_and_logging.py --db-path data/seller_platform.db

# Дополнительная инициализация через Flask
python - <<'PYCODE'
from seller_platform import app, db, ensure_storage_roots

ensure_storage_roots()
with app.app_context():
    # create_all() безопасно - создает только отсутствующие таблицы
    db.create_all()
    print("✅ Инициализация seller-platform завершена")
PYCODE
else
echo "🚀 Инициализация wb-calculator..."
python - <<'PYCODE'
from app import ensure_directories

ensure_directories()
print("✅ Инициализация wb-calculator завершена")
PYCODE
fi

echo "🌐 Запуск gunicorn на порту ${PORT}..."
exec gunicorn --bind 0.0.0.0:${PORT} ${APP_MODULE}
