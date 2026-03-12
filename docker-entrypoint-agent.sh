#!/bin/sh
set -e

AGENT_NAME=${AGENT_NAME:?AGENT_NAME must be set (e.g. seo-writer, category-mapper)}

export PYTHONPATH=/app:${PYTHONPATH:-}

echo "🤖 Запуск агента: ${AGENT_NAME}"
echo "   Платформа: ${PLATFORM_URL:-http://seller-platform:5001}"
echo "   LLM: ${LLM_PROVIDER:-cloudru}"
if [ -n "$FALLBACK_LLM_PROVIDER" ]; then
  echo "   Fallback LLM: ${FALLBACK_LLM_PROVIDER}"
fi

exec python -m agents.runner --agent "$AGENT_NAME" --log-level "${LOG_LEVEL:-INFO}"
