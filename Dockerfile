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

# Системный Chromium для Playwright-рендера. Он находится через
# services.infographic_renderer._find_chromium и не зависит от Playwright CDN.
# gosu — для безопасного drop-privileges в entrypoint (fix-permissions pattern)
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       gosu chromium \
       libglib2.0-0 libnss3 libnspr4 libdbus-1-3 libatk1.0-0 \
       libatk-bridge2.0-0 libcups2 libexpat1 libxcb1 libxkbcommon0 \
       libatspi2.0-0 libx11-6 libxcomposite1 libxdamage1 libxext6 \
       libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 libcairo2 libasound2 \
       tesseract-ocr tesseract-ocr-eng tesseract-ocr-rus fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

# Прогреваем rembg-модель от имени runtime-пользователя: первый request не
# должен зависеть от загрузки весов из внешней сети.
RUN mkdir -p /home/app/.cache && chown -R app:app /home/app/.cache \
    && su app -s /bin/sh -c "python -c 'from rembg import new_session; new_session()'"

COPY --chown=app:app . .

RUN chmod +x /app/docker-entrypoint.sh \
    && chown -R app:app /app

# USER app — НЕ ставим здесь: entrypoint стартует как root,
# исправляет владельца на смонтированных volumes, затем делает
# gosu app для запуска приложения (fix-permissions pattern).

HEALTHCHECK --interval=15s --timeout=5s --start-period=600s --retries=3 \
  CMD python -c "import urllib.request, ssl; ctx=ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE; response=urllib.request.urlopen('https://localhost:${PORT:-5001}/login', context=ctx, timeout=3); response.close()" || exit 1

ENTRYPOINT ["/app/docker-entrypoint.sh"]
