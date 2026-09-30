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
if [ "${1:-}" = "--run-database-migrations" ]; then
  echo "🚀 Инициализация seller-platform..."
fi

python scripts/startup_migrations.py --database-path /app/data/seller_platform.db
# Credentials can change independently of schema/migration code. Synchronize
# the administrator on every boot after the database is ready.
SKIP_SCHEDULER=1 python - <<'PYADMIN'
from seller_platform import app, _ensure_admin_from_env
with app.app_context():
    _ensure_admin_from_env()
PYADMIN

if [ "${1:-}" = "--run-database-migrations" ]; then
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
