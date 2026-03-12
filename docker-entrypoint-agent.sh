#!/bin/sh
set -e

AGENT_NAME=${AGENT_NAME:?AGENT_NAME must be set (e.g. seo-writer, category-mapper)}

export PYTHONPATH=/app:${PYTHONPATH:-}

# ── Pre-flight checks ──────────────────────────────────────────
has_errors=0

if [ -z "$AGENT_ID" ]; then
  echo "ERROR: AGENT_ID is not set."
  echo "  Set AGENT_<NAME>_ID in .env (get it from UI: /agents -> Activate)"
  has_errors=1
fi

if [ -z "$AGENT_API_KEY" ]; then
  echo "ERROR: AGENT_API_KEY is not set."
  echo "  Set AGENT_<NAME>_KEY in .env (generated on activation)"
  has_errors=1
fi

if [ "$LLM_PROVIDER" = "cloudru" ] && [ -z "$CLOUDRU_API_KEY" ]; then
  echo "ERROR: CLOUDRU_API_KEY is not set (required for LLM_PROVIDER=cloudru)."
  has_errors=1
fi

if [ "$has_errors" = "1" ]; then
  echo ""
  echo "Agent '${AGENT_NAME}' cannot start due to missing configuration."
  echo "See .env.example and agents/.env.example for required variables."
  exit 1
fi

echo "Starting agent: ${AGENT_NAME}"
echo "  Platform: ${PLATFORM_URL:-http://seller-platform:5001}"
echo "  LLM: ${LLM_PROVIDER:-cloudru}"
if [ -n "$FALLBACK_LLM_PROVIDER" ]; then
  echo "  Fallback LLM: ${FALLBACK_LLM_PROVIDER}"
fi

exec python -m agents.runner --agent "$AGENT_NAME" --log-level "${LOG_LEVEL:-INFO}"
