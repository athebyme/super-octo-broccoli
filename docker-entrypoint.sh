#!/bin/sh
set -e

APP_MODULE=${APP_MODULE:-seller_platform:app}
PORT=${PORT:-5001}

# Ensure all working directories exist so uploads and reports survive volume mounts.
mkdir -p uploads processed data

# Run lightweight initialization depending on the application we serve.
if [ "$APP_MODULE" = "seller_platform:app" ]; then
echo "🚀 Инициализация seller-platform..."

# Сначала создаем базовую структуру БД через Flask/SQLAlchemy
echo "📦 Создание базовой структуры базы данных..."
python - <<'PYCODE'
import os

# DATABASE_URL уже установлен из docker-compose.yml
# Проверяем что пришло
db_url_from_env = os.environ.get('DATABASE_URL', 'NOT_SET')
print(f"🔍 DATABASE_URL from environment: {db_url_from_env}")

from seller_platform import app, db, ensure_storage_roots
from models import User

print(f"🗄️  Используется база данных: {app.config['SQLALCHEMY_DATABASE_URI']}")

ensure_storage_roots()
with app.app_context():
    # create_all() безопасно - создает только отсутствующие таблицы
    db.create_all()
    print("✅ Базовая структура БД создана")

    # Включаем WAL mode для лучшей поддержки конкурентного доступа
    try:
        db.session.execute(db.text("PRAGMA journal_mode=WAL;"))
        db.session.execute(db.text("PRAGMA synchronous=NORMAL;"))
        db.session.execute(db.text("PRAGMA busy_timeout=30000;"))  # 30 секунд
        db.session.commit()
        print("✅ SQLite настроен: WAL mode включен, busy_timeout=30s")
    except Exception as e:
        print(f"⚠️  Не удалось настроить SQLite WAL mode: {e}")

    # Проверяем, есть ли администратор
    admin_exists = User.query.filter_by(is_admin=True).first()

    if not admin_exists:
        # Создаем дефолтного администратора
        username = os.environ.get('ADMIN_USERNAME', 'admin')
        email = os.environ.get('ADMIN_EMAIL', 'admin@example.com')
        password = os.environ.get('ADMIN_PASSWORD', 'admin123')

        admin = User(
            username=username,
            email=email,
            is_admin=True,
            is_active=True
        )
        admin.set_password(password)

        db.session.add(admin)
        db.session.commit()

        print(f"✅ Создан администратор: {username}")
        print(f"   Email: {email}")
        print(f"   Пароль: {password}")
        print(f"   ⚠️  ВАЖНО: Смените пароль после первого входа!")
    else:
        print(f"✅ Администратор уже существует: {admin_exists.username}")
PYCODE

# Теперь применяем миграции для добавления новых колонок
echo "📦 Применение миграций базы данных..."
python migrate_db.py --db-path /app/data/seller_platform.db
python migrate_add_characteristics.py /app/data/seller_platform.db
python migrate_add_history_and_logging.py --db-path /app/data/seller_platform.db
python migrate_add_subject_id.py /app/data/seller_platform.db
python migrate_add_price_monitoring.py || echo "⚠️ Price monitoring migration skipped (already applied or error)"
python migrate_add_product_sync_settings.py || echo "⚠️ Product sync settings migration skipped (already applied or error)"
python migrate_add_admin_features.py || echo "⚠️ Admin features migration skipped (already applied or error)"
python migrate_add_card_merge_history.py --db-path /app/data/seller_platform.db || echo "⚠️ Card merge history migration skipped (already applied or error)"
python migrate_add_supplier_price.py || echo "⚠️ Supplier price migration skipped (already applied or error)"
python migrate_add_safe_price_change.py || echo "⚠️ Safe price change migration skipped (already applied or error)"
python migrate_add_unlimited_batch.py || echo "⚠️ Unlimited batch migration skipped (already applied or error)"
python migrate_add_blocked_cards.py || echo "⚠️ Blocked cards migration skipped (already applied or error)"

echo "✅ Инициализация seller-platform завершена"
else
echo "🚀 Инициализация wb-calculator..."
python - <<'PYCODE'
from app import ensure_directories

ensure_directories()
print("✅ Инициализация wb-calculator завершена")
PYCODE
fi

echo "🌐 Запуск gunicorn на порту ${PORT}..."
exec gunicorn \
  --bind 0.0.0.0:${PORT} \
  --timeout 600 \
  --workers 2 \
  --threads 2 \
  --worker-class gthread \
  --access-logfile - \
  --error-logfile - \
  ${APP_MODULE}
