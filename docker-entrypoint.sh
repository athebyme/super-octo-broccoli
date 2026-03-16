#!/bin/sh
set -e

APP_MODULE=${APP_MODULE:-seller_platform:app}
PORT=${PORT:-5001}

# Ensure Python can find root-level modules when running scripts from subdirectories
export PYTHONPATH=/app:${PYTHONPATH:-}

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
    # Автоматическая миграция новых колонок
    from seller_platform import _run_startup_migrations
    _run_startup_migrations()
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
    username = os.environ.get('ADMIN_USERNAME', 'admin')
    email = os.environ.get('ADMIN_EMAIL', 'admin@example.com')
    password = os.environ.get('ADMIN_PASSWORD', '')
    if not password:
        import secrets as _s
        password = _s.token_urlsafe(16)
        print(f"⚠️  ADMIN_PASSWORD не задан! Сгенерирован случайный пароль: {password}")
        print(f"   ОБЯЗАТЕЛЬНО сохраните его и задайте ADMIN_PASSWORD в .env!")

    # Ищем по username, по email или первого админа
    admin_user = (
        User.query.filter_by(username=username).first()
        or User.query.filter_by(email=email).first()
        or User.query.filter_by(is_admin=True).first()
    )

    if not admin_user:
        # Создаем дефолтного администратора
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
        print(f"   ⚠️  ВАЖНО: Смените пароль после первого входа!")
    else:
        # Синхронизируем username, пароль и email из переменных окружения
        updated = False
        if admin_user.username != username:
            admin_user.username = username
            updated = True
        if not admin_user.check_password(password):
            admin_user.set_password(password)
            updated = True
        if email and admin_user.email != email:
            admin_user.email = email
            updated = True
        if not admin_user.is_admin:
            admin_user.is_admin = True
            updated = True
        if updated:
            db.session.commit()
            print(f"✅ Администратор '{username}' обновлён из переменных окружения")
        else:
            print(f"✅ Администратор уже существует: {admin_user.username}")
PYCODE

# Теперь применяем миграции для добавления новых колонок
echo "📦 Применение миграций базы данных..."
python migrations/migrate_db.py --db-path /app/data/seller_platform.db
python migrations/migrate_add_characteristics.py /app/data/seller_platform.db
python migrations/migrate_add_history_and_logging.py --db-path /app/data/seller_platform.db
python migrations/migrate_add_subject_id.py /app/data/seller_platform.db
python migrations/migrate_add_price_monitoring.py || echo "⚠️ Price monitoring migration skipped (already applied or error)"
python migrations/migrate_add_product_sync_settings.py || echo "⚠️ Product sync settings migration skipped (already applied or error)"
python migrations/migrate_add_admin_features.py || echo "⚠️ Admin features migration skipped (already applied or error)"
python migrations/migrate_add_card_merge_history.py --db-path /app/data/seller_platform.db || echo "⚠️ Card merge history migration skipped (already applied or error)"
python migrations/migrate_add_supplier_price.py || echo "⚠️ Supplier price migration skipped (already applied or error)"
python migrations/migrate_add_safe_price_change.py || echo "⚠️ Safe price change migration skipped (already applied or error)"
python migrations/migrate_add_unlimited_batch.py || echo "⚠️ Unlimited batch migration skipped (already applied or error)"
python migrations/migrate_add_blocked_cards.py || echo "⚠️ Blocked cards migration skipped (already applied or error)"
python migrations/migrate_add_price_stock_sync.py /app/data/seller_platform.db || echo "⚠️ Price stock sync migration skipped (already applied or error)"
python migrations/migrate_add_marketplace_tables.py || echo "⚠️ Marketplace tables migration skipped (already applied or error)"
python migrations/add_ai_job_model_field.py || echo "⚠️ AI job model field migration skipped (already applied or error)"
python migrations/add_parsing_quality_fields.py || echo "⚠️ Parsing quality fields migration skipped (already applied or error)"
python migrations/migrate_add_service_agents.py /app/data/seller_platform.db || echo "⚠️ Service agents migration skipped (already applied or error)"
python migrations/run_all_migrations.py /app/data/seller_platform.db || echo "⚠️ Comprehensive migration skipped (already applied or error)"
python migrations/migrate_add_sexopt_supplier.py /app/data/seller_platform.db || echo "⚠️ Sexopt supplier migration skipped (already applied or error)"

echo "✅ Инициализация seller-platform завершена"
fi

echo "🌐 Запуск gunicorn на порту ${PORT}..."

# ---------- HTTPS / SSL ----------
SSL_CERT="${SSL_CERT_PATH:-}"
SSL_KEY="${SSL_KEY_PATH:-}"

# Если сертификат не предоставлен — генерируем self-signed
if [ -z "$SSL_CERT" ]; then
  SSL_DIR="/app/data/ssl"
  SSL_CERT="$SSL_DIR/cert.pem"
  SSL_KEY="$SSL_DIR/key.pem"

  if [ ! -f "$SSL_CERT" ] || [ ! -f "$SSL_KEY" ]; then
    echo "🔐 Генерация self-signed SSL сертификата..."
    mkdir -p "$SSL_DIR"
    python - <<'PYSSL'
from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
import datetime, os, ipaddress

key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

subject = issuer = x509.Name([
    x509.NameAttribute(NameOID.COMMON_NAME, os.environ.get("SSL_COMMON_NAME", "seller-platform")),
    x509.NameAttribute(NameOID.ORGANIZATION_NAME, "WB Seller Platform"),
])

# SAN: localhost + seller-platform + 127.0.0.1 + пользовательские IP из SSL_SAN_IPS
san_entries = [
    x509.DNSName("localhost"),
    x509.DNSName("seller-platform"),
    x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
]

# Добавляем пользовательские IP/домены из SSL_SAN_IPS (через запятую)
extra_sans = os.environ.get("SSL_SAN_IPS", "").strip()
if extra_sans:
    for entry in extra_sans.split(","):
        entry = entry.strip()
        if not entry:
            continue
        try:
            san_entries.append(x509.IPAddress(ipaddress.ip_address(entry)))
        except ValueError:
            san_entries.append(x509.DNSName(entry))

cert = (
    x509.CertificateBuilder()
    .subject_name(subject)
    .issuer_name(issuer)
    .public_key(key.public_key())
    .serial_number(x509.random_serial_number())
    .not_valid_before(datetime.datetime.utcnow())
    .not_valid_after(datetime.datetime.utcnow() + datetime.timedelta(days=365))
    .add_extension(
        x509.SubjectAlternativeName(san_entries),
        critical=False,
    )
    .sign(key, hashes.SHA256())
)

ssl_dir = os.environ.get("SSL_DIR", "/app/data/ssl")
os.makedirs(ssl_dir, exist_ok=True)

with open(os.path.join(ssl_dir, "key.pem"), "wb") as f:
    f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()))

with open(os.path.join(ssl_dir, "cert.pem"), "wb") as f:
    f.write(cert.public_bytes(serialization.Encoding.PEM))

print("✅ Self-signed SSL сертификат создан")
PYSSL
  else
    echo "✅ SSL сертификат уже существует"
  fi
fi
# ---------- HTTP → HTTPS Redirect ----------
# Пропускаем если стоит реверс-прокси (Caddy/nginx) — он сам делает редирект
if [ "${DISABLE_HTTP_REDIRECT:-}" = "1" ] || [ "${DISABLE_HTTP_REDIRECT:-}" = "true" ]; then
  echo "⏭️  HTTP→HTTPS редирект отключён (используется внешний прокси)"
else
  HTTP_PORT="${HTTP_PORT:-80}"
  echo "🔀 Запуск HTTP→HTTPS редиректа на порту ${HTTP_PORT}..."
  python - <<'PYREDIRECT' &
import http.server, ssl, os

https_port = os.environ.get("PORT", "5001")
http_port = int(os.environ.get("HTTP_PORT", "80"))

class RedirectHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        host = self.headers.get("Host", "").split(":")[0]
        target = f"https://{host}:{https_port}{self.path}"
        self.send_response(301)
        self.send_header("Location", target)
        self.end_headers()

    do_POST = do_HEAD = do_PUT = do_DELETE = do_GET

    def log_message(self, fmt, *args):
        pass  # тихий режим

server = http.server.HTTPServer(("0.0.0.0", http_port), RedirectHandler)
print(f"✅ HTTP redirect: :{http_port} → HTTPS :{https_port}")
server.serve_forever()
PYREDIRECT
fi

exec gunicorn \
  --bind 0.0.0.0:${PORT} \
  --timeout 600 \
  --workers 2 \
  --threads 2 \
  --worker-class gthread \
  --access-logfile - \
  --error-logfile - \
  --certfile "$SSL_CERT" \
  --keyfile "$SSL_KEY" \
  ${APP_MODULE}
