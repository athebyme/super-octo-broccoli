FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_MODULE=seller_platform:app \
    PORT=5001

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Playwright + Chromium для рендеринга инфографики
RUN pip install 'playwright==1.52.0' \
    && python -m playwright install chromium \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
       libglib2.0-0 libnss3 libnspr4 libdbus-1-3 libatk1.0-0 \
       libatk-bridge2.0-0 libcups2 libexpat1 libxcb1 libxkbcommon0 \
       libatspi2.0-0 libx11-6 libxcomposite1 libxdamage1 libxext6 \
       libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 libcairo2 libasound2 \
    && rm -rf /var/lib/apt/lists/*

COPY . .

RUN chmod +x /app/docker-entrypoint.sh

HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('https://localhost:${PORT:-5001}/login', context=__import__('ssl')._create_unverified_context())" || exit 1

ENTRYPOINT ["/app/docker-entrypoint.sh"]

