#!/bin/sh
# Healthcheck для агентов: проверяет, что процесс Python жив
# и что liveness-файл обновлялся не более 120 секунд назад.

LIVENESS_FILE="/tmp/agent-alive"

if [ ! -f "$LIVENESS_FILE" ]; then
  echo "FAIL: liveness file not found"
  exit 1
fi

# Проверяем возраст файла (секунды с последнего обновления)
if [ "$(uname)" = "Linux" ]; then
  LAST_MOD=$(stat -c %Y "$LIVENESS_FILE" 2>/dev/null || echo 0)
  NOW=$(date +%s)
  AGE=$(( NOW - LAST_MOD ))
  if [ "$AGE" -gt 120 ]; then
    echo "FAIL: liveness stale (${AGE}s ago)"
    exit 1
  fi
fi

echo "OK"
exit 0
