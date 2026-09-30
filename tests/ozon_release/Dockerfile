FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 SKIP_SCHEDULER=1
WORKDIR /app
RUN groupadd --gid 1000 check && useradd --uid 1000 --gid 1000 --create-home check
COPY requirements.txt requirements-ci.txt ./
RUN pip install --no-cache-dir -r requirements-ci.txt
RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium nodejs fonts-dejavu-core && rm -rf /var/lib/apt/lists/*

# Explicit source list excludes working data, credentials and private release artifacts.
COPY --chown=check:check *.py *.md *.txt *.yml *.sh ./
COPY --chown=check:check .env.example .dockerignore Dockerfile Dockerfile.agents ./
COPY --chown=check:check agents/ agents/
COPY --chown=check:check routes/ routes/
COPY --chown=check:check services/ services/
COPY --chown=check:check migrations/ migrations/
COPY --chown=check:check static/ static/
COPY --chown=check:check templates/ templates/
COPY --chown=check:check tests/ tests/
COPY --chown=check:check scripts/ scripts/
COPY --chown=check:check docs/ docs/
RUN mkdir -p /app/data /app/logs /app/uploads /app/processed /artifacts \
    && chown -R check:check /app /artifacts
USER check
ENTRYPOINT ["python", "scripts/check_ozon_release.py"]
