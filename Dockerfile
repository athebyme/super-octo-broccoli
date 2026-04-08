FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_MODULE=seller_platform:app \
    PORT=5001

WORKDIR /app

# Создаём непривилегированного пользователя заранее, чтобы playwright положил
# браузеры в его HOME и не пришлось копировать кэш потом
RUN groupadd --system --gid 1000 app \
    && useradd --system --uid 1000 --gid 1000 --home-dir /home/app --create-home --shell /usr/sbin/nologin app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Playwright + Chromium для рендеринга инфографики
RUN pip install 'playwright==1.52.0' \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
       libglib2.0-0 libnss3 libnspr4 libdbus-1-3 libatk1.0-0 \
       libatk-bridge2.0-0 libcups2 libexpat1 libxcb1 libxkbcommon0 \
       libatspi2.0-0 libx11-6 libxcomposite1 libxdamage1 libxext6 \
       libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 libcairo2 libasound2 \
    && rm -rf /var/lib/apt/lists/*

# Ставим chromium от имени app, чтобы кэш оказался в /home/app/.cache
ENV PLAYWRIGHT_BROWSERS_PATH=/home/app/.cache/ms-playwright
RUN mkdir -p /home/app/.cache && chown -R app:app /home/app/.cache \
    && su app -s /bin/sh -c "python -m playwright install chromium"

COPY --chown=app:app . .

RUN chmod +x /app/docker-entrypoint.sh \
    && chown -R app:app /app

USER app

HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request, ssl; ctx=ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE; urllib.request.urlopen('https://localhost:${PORT:-5001}/login', context=ctx)" || exit 1

ENTRYPOINT ["/app/docker-entrypoint.sh"]

