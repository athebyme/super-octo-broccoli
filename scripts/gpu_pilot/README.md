# GPU-ветка «Фотостудии»

Production GPU делает только пустые фоны через Qwen-Image-2512. Фото товара и
текст остаются на Seller Hub. Edit-2511 и text benchmarks — только
`research_only`, их output нельзя считать publishable.

## Окружение GPU

- A100/H100 80 ГБ, Python 3.11, CUDA 12.x;
- `torch`, `diffusers`, `transformers`, `accelerate`, `safetensors`, `peft`,
  `pillow`, `sentencepiece`;
- веса `Qwen/Qwen-Image-2512` и Lightning LoRA.

```bash
python3 -m venv ~/venv && source ~/venv/bin/activate
pip install torch torchvision --index-url https://download.pytorch.org/whl/cu121
pip install diffusers transformers accelerate safetensors peft pillow sentencepiece
```

## Постоянный worker + web tunnel

```bash
mkdir -p ~/jobs ~/image_bridge

nohup ~/venv/bin/python qwen_worker.py \
  --device cuda:0 --watch ~/jobs --start t2i \
  > ~/qwen-worker.log 2>&1 &

export GPU_BRIDGE_TOKEN='<не менее 32 случайных символов>'
nohup ~/venv/bin/python http_bridge.py \
  --host 127.0.0.1 --port 8787 --queue ~/jobs --root ~/image_bridge \
  > ~/gpu-bridge.log 2>&1 &
```

Bridge по умолчанию слушает localhost. Наружу его выводят через HTTPS reverse
proxy с IP allowlist либо через SSH/WireGuard tunnel. На Seller Hub:

```bash
GPU_IMAGE_SERVER_URL=https://gpu.example
GPU_IMAGE_SERVER_TOKEN='<тот же GPU_BRIDGE_TOKEN>'
GPU_IMAGE_STEPS=4
GPU_IMAGE_TRUE_CFG=1.0
```

Для локального tunnel при запуске web без Docker:

```bash
ssh -N -L 8787:127.0.0.1:8787 ubuntu@gpu-host
# Seller Hub env:
GPU_IMAGE_SERVER_URL=http://127.0.0.1:8787
GPU_IMAGE_ALLOW_HTTP=1
```

Если Seller Hub работает в Compose, tunnel на Linux-хосте должен слушать адрес,
доступный из Docker bridge (и быть закрыт firewall от внешней сети), например:

```bash
ssh -N -L 0.0.0.0:8787:127.0.0.1:8787 ubuntu@gpu-host
# .env Compose; host.docker.internal добавлен через host-gateway
GPU_IMAGE_SERVER_URL=http://host.docker.internal:8787
GPU_IMAGE_ALLOW_HTTP=1
```

Для постоянной production-связи предпочтительнее HTTPS/WireGuard, а не
публично слушающий SSH forward.

Health: `curl http://127.0.0.1:8787/healthz`. Job endpoints требуют
`Authorization: Bearer $GPU_BRIDGE_TOKEN`.

Остановка worker: `touch ~/jobs/STOP-cuda0`. После тестов обязательно
остановите/заморозьте облачную машину.

## Offline bundle

```bash
SKIP_SCHEDULER=1 python scripts/infographic_pilot.py \
  --seller-id 1 --limit 20 --variants gen_api:flux-2:B \
  --budget-rub 0 --export-gpu-bundle

python scripts/gpu_pilot/run_qwen_pilot.py \
  --bundle data/infographic_pilot/run_*/gpu_bundle \
  --out data/infographic_pilot/gpu_out \
  --mode b --lightning

python scripts/gpu_pilot/finalize_backgrounds.py \
  --bundle data/infographic_pilot/run_*/gpu_bundle \
  --gpu-out data/infographic_pilot/gpu_out
```

Raw worker output имеет `status=background_only`. После локального foreground
composite `results_final.jsonl` содержит `review_required`, пока автоматическая
alpha-mask не подтверждена отдельно; только verified mask вместе с остальными
quality gates может дать `auto_pass`.

`make_flow_bundle.py` создаёт только `background_prompts` cat/lux/neon.
Исторические `edit_prompts`, `info` и модельные русские плашки из production
flow удалены. Для намеренного edit/text benchmark передавайте
`--research-only`; worker queue jobs требуют `research_only=true`.

Lightning defaults: t2i=4 шага, edit=8 шагов, `true_cfg_scale=1.0`.
Edit-2511 при исследовании грузится только через
`QwenImageEditPlusPipeline`; старый pipeline перерисовывает товар.

Секреты, SSH/OpenStack credentials, `.env.gpu`, веса и generated artifacts в
git не добавлять.
