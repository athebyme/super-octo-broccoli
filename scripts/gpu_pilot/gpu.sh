#!/usr/bin/env bash
# Хелпер подключения к GPU-серверу пилота. Конфиг: .env.gpu в корне репо
# (см. scripts/gpu_pilot/gpu_server.env.example).
#
#   scripts/gpu_pilot/gpu.sh ssh [команда]   — зайти/выполнить команду
#   scripts/gpu_pilot/gpu.sh push <файлы..>  — закинуть файлы в ~ сервера
#   scripts/gpu_pilot/gpu.sh pull <путь> <куда>  — забрать с сервера
#   scripts/gpu_pilot/gpu.sh status          — очередь воркеров и GPU
#   scripts/gpu_pilot/gpu.sh server <cmd>    — openstack (stop/start/show...)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENV_FILE="${GPU_ENV_FILE:-$ROOT/.env.gpu}"
if [[ ! -f "$ENV_FILE" ]]; then
    echo "Нет $ENV_FILE — скопируйте scripts/gpu_pilot/gpu_server.env.example" >&2
    exit 1
fi
set -a; . "$ENV_FILE"; set +a
: "${GPU_SERVER_HOST:?заполните GPU_SERVER_HOST в .env.gpu}"
KEY="${GPU_SSH_KEY/#\~/$HOME}"
SSH=(ssh -i "$KEY" -o StrictHostKeyChecking=accept-new "${GPU_SERVER_USER:-ubuntu}@$GPU_SERVER_HOST")

cmd="${1:-ssh}"; shift || true
case "$cmd" in
    ssh)    exec "${SSH[@]}" "$@" ;;
    push)   exec scp -i "$KEY" -o StrictHostKeyChecking=accept-new -r "$@" \
                 "${GPU_SERVER_USER:-ubuntu}@$GPU_SERVER_HOST:~/" ;;
    pull)   src="$1"; dst="$2"
            exec scp -i "$KEY" -o StrictHostKeyChecking=accept-new -r \
                 "${GPU_SERVER_USER:-ubuntu}@$GPU_SERVER_HOST:$src" "$dst" ;;
    status) exec "${SSH[@]}" '
        echo "--- очередь ---"
        ls ~/jobs/ 2>/dev/null || echo "(пусто)"
        echo "--- процессы ---"
        pgrep -af "[q]wen_worker|[r]un_qwen_pilot" || echo "(нет)"
        echo "--- GPU ---"
        nvidia-smi --query-gpu=index,memory.used,utilization.gpu --format=csv,noheader' ;;
    server) exec openstack "$@" ;;   # нужен python-openstackclient и OS_* из env
    *) echo "Неизвестная команда: $cmd" >&2; exit 1 ;;
esac
