#!/bin/sh
set -e

APP_MODULE=${APP_MODULE:-seller_platform:app}
PORT=${PORT:-5001}

# Ensure Python can find root-level modules when running scripts from subdirectories
export PYTHONPATH=/app:${PYTHONPATH:-}

# ── Fix-permissions & drop-privileges ────────────────────────────────
# Docker volumes могут быть созданы от root. Исправляем владельца,
# а затем перезапускаем ВЕСЬ entrypoint от пользователя app через gosu.
if [ "$(id -u)" = "0" ]; then
  mkdir -p /app/data /app/uploads /app/processed /app/data/ssl
  chown -R app:app /app/data /app/uploads /app/processed
  marketplace_lock_dir=/tmp/seller-hub-marketplace-publication-locks
  if [ -L "$marketplace_lock_dir" ] || {
    [ -e "$marketplace_lock_dir" ] && [ ! -d "$marketplace_lock_dir" ]
  }; then
    echo "Unsafe marketplace operation lock path: $marketplace_lock_dir" >&2
    exit 1
  fi
  mkdir -p "$marketplace_lock_dir"
  chown app:app "$marketplace_lock_dir"
  chmod 0700 "$marketplace_lock_dir"
  exec gosu app "$0" "$@"
fi
# ─────────────────────────────────────────────────────────────────────
# С этого момента всё работает от пользователя app (uid 1000).

# Ensure all working directories exist so uploads and reports survive volume mounts.
mkdir -p uploads processed data

# Run lightweight initialization depending on the application we serve.
if [ "$APP_MODULE" = "seller_platform:app" ]; then
python scripts/validate_runtime_config.py
if [ -z "${ADMIN_PASSWORD:-}" ]; then
  echo "ADMIN_PASSWORD must be configured before seller-platform startup" >&2
  exit 1
fi
export ADMIN_USERNAME=${ADMIN_USERNAME:-admin}
if [ "${1:-}" != "--run-database-migrations" ]; then
python scripts/startup_migrations.py --database-path /app/data/seller_platform.db
# Credentials can change independently of schema/migration code. Synchronize
# the administrator on every boot, including when the bundle is already current.
SKIP_SCHEDULER=1 python - <<'PYADMIN'
from seller_platform import app, _ensure_admin_from_env
with app.app_context():
    _ensure_admin_from_env()
PYADMIN
else
echo "🚀 Инициализация seller-platform..."

# Сначала создаем базовую структуру БД через Flask/SQLAlchemy
echo "📦 Создание базовой структуры базы данных..."
SKIP_SCHEDULER=1 python - <<'PYCODE'
import os

from seller_platform import app, db, ensure_storage_roots
from models import User

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
        raise RuntimeError('ADMIN_PASSWORD must be configured before startup')

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
# SKIP_SCHEDULER=1 чтобы APScheduler не запускался и не зависал
echo "📦 Применение миграций базы данных..."
export SKIP_SCHEDULER=1
python migrations/migrate_db.py --db-path /app/data/seller_platform.db
python migrations/migrate_add_characteristics.py /app/data/seller_platform.db
python migrations/migrate_add_history_and_logging.py --db-path /app/data/seller_platform.db
python migrations/migrate_add_subject_id.py /app/data/seller_platform.db
python migrations/migrate_add_price_monitoring.py
python migrations/migrate_add_product_sync_settings.py
python migrations/migrate_add_admin_features.py
python migrations/migrate_add_card_merge_history.py --db-path /app/data/seller_platform.db
python migrations/migrate_add_supplier_price.py
python migrations/migrate_add_safe_price_change.py
python migrations/migrate_add_unlimited_batch.py
python migrations/migrate_add_blocked_cards.py
python migrations/migrate_add_price_stock_sync.py /app/data/seller_platform.db
python migrations/migrate_add_marketplace_tables.py
python -m migrations.run_scoped_batch /app/data/seller_platform.db \
  migrations/migrate_add_marketplace_accounts.py \
  migrations/migrate_add_marketplace_credential_notices.py \
  migrations/migrate_add_marketplace_account_events.py \
  migrations/migrate_add_ozon_references.py \
  migrations/migrate_add_ozon_product_type_visibility.py \
  migrations/migrate_add_ozon_reference_reviews.py \
  migrations/migrate_add_ozon_compliance_defaults.py \
  migrations/migrate_add_marketplace_reference_freshness.py \
  migrations/migrate_add_brand_category_external_id.py \
  migrations/migrate_add_wb_dictionary_provenance.py \
  migrations/migrate_add_marketplace_listings.py \
  migrations/migrate_add_ozon_catalog_checkpoints.py \
  migrations/migrate_add_marketplace_product_links.py \
  migrations/migrate_add_marketplace_canonical_content.py \
  migrations/migrate_add_marketplace_rollout.py \
  migrations/migrate_add_marketplace_drafts.py \
  migrations/migrate_add_marketplace_draft_attribute_removals.py \
  migrations/migrate_add_marketplace_operations.py \
  migrations/migrate_add_ozon_upload_queue.py \
  migrations/migrate_add_ozon_draft_ai_completion.py \
  migrations/migrate_add_marketplace_commercial.py \
  migrations/migrate_add_ozon_warehouse_reads.py \
  migrations/migrate_add_marketplace_product_updates.py \
  migrations/migrate_add_marketplace_auto_publish.py \
  migrations/migrate_add_marketplace_quality_analytics.py \
  migrations/migrate_add_marketplace_fulfillment.py \
  migrations/migrate_add_marketplace_finance.py \
  migrations/migrate_add_marketplace_inbox.py \
  migrations/migrate_add_marketplace_read_schedules.py \
  migrations/migrate_add_marketplace_read_requests.py \
  migrations/migrate_add_inbox_read_queue.py \
  migrations/migrate_add_marketplace_read_credential_identity.py
python migrations/add_ai_job_model_field.py
python migrations/add_ai_job_heartbeat.py
python migrations/add_parsing_quality_fields.py
python migrations/migrate_add_supplier_catalog_enrichment.py /app/data/seller_platform.db
python migrations/migrate_add_service_agents.py /app/data/seller_platform.db
python migrations/migrate_add_card_quality_v2.py /app/data/seller_platform.db
python migrations/migrate_add_agent_chat.py /app/data/seller_platform.db
python migrations/migrate_add_agent_knowledge.py /app/data/seller_platform.db
python migrations/run_all_migrations.py /app/data/seller_platform.db --base-only
python migrations/migrate_add_imported_wb_nm_id.py /app/data/seller_platform.db
python migrations/migrate_add_image_generation_lab.py /app/data/seller_platform.db
python migrations/migrate_add_image_lab_reference_watermark.py /app/data/seller_platform.db
python migrations/migrate_add_image_lab_angle_synthesis.py /app/data/seller_platform.db
python migrations/migrate_add_image_lab_marketplace_target.py /app/data/seller_platform.db
python -m migrations.run_scoped_batch /app/data/seller_platform.db \
  migrations/migrate_add_infographic_campaigns.py \
  migrations/migrate_add_marketplace_media_publications.py \
  migrations/migrate_add_marketplace_write_quarantine.py \
  migrations/migrate_add_bestseller_image_recommendations.py \
  migrations/migrate_add_content_factory_marketplace_scope.py \
  migrations/migrate_add_social_account_publish_health.py
python migrations/migrate_add_sexopt_supplier.py /app/data/seller_platform.db
# Fail-fast: добавляет колонки, которые ORM читает сразу после старта
python migrations/migrate_andrey_feed_full_ingest.py /app/data/seller_platform.db
# Fail-fast: rebuild CHECK mode обогащения + items.inference_json
python migrations/migrate_add_enrichment_inference.py /app/data/seller_platform.db
# Fail-fast: габариты упаковки из characteristics_json переезжают в dimensions_json
python migrations/migrate_clean_characteristic_dimensions.py /app/data/seller_platform.db
# Fail-fast: ORM читает колонки WB-ревизии сразу после старта
python migrations/migrate_add_wb_card_audit.py /app/data/seller_platform.db
# Fail-fast: история карточки хранит bounded решения smart enrichment merge
python migrations/migrate_add_enrichment_merge_audit.py /app/data/seller_platform.db
# Fail-fast: durable bulk cursor + asynchronous WB reconciliation state
python migrations/migrate_enrichment_reliability_v2.py /app/data/seller_platform.db
# Fail-fast: мониторинг конкурентов v2 (интервалы, честные наблюдения) + чистка v1-мусора снимков
python migrations/migrate_competitor_monitor_v2.py /app/data/seller_platform.db
python migrations/migrate_add_competitor_matching.py /app/data/seller_platform.db
# Fail-fast: comparison reads public basic/final prices from dedicated columns.
python migrations/migrate_add_competitor_price_lanes.py /app/data/seller_platform.db
# Fail-fast: legacy imports regain exact supplier provenance by the unique
# (supplier_id, external_id) source key; no title/AI/fuzzy matching.
python migrations/migrate_backfill_imported_supplier_links.py /app/data/seller_platform.db
python migrations/migrate_compact_competitor_snapshots.py /app/data/seller_platform.db
unset SKIP_SCHEDULER

echo "✅ Инициализация seller-platform завершена"
exit 0
fi
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

# 2 workers x 8 threads = 16 слотов. 2x2=4 слота забивались медленными
# фото-прокси запросами и платформа висела целиком (инцидент 2026-07-20).
# Число процессов не меняем: advisory lock планировщика рассчитан на них.
exec gunicorn \
  --bind 0.0.0.0:${PORT} \
  --timeout 600 \
  --workers "${GUNICORN_WORKERS:-2}" \
  --threads "${GUNICORN_THREADS:-8}" \
  --worker-class gthread \
  --access-logfile - \
  --error-logfile - \
  --certfile "$SSL_CERT" \
  --keyfile "$SSL_KEY" \
  ${APP_MODULE}
