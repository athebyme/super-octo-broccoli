# -*- coding: utf-8 -*-
"""Постоянный GPU-воркер с общей очередью и перехватом задач.

Модель грузится один раз; воркер забирает задания из общей папки атомарным
rename. Если заданий под загруженную модель нет, а под другую — есть,
воркер переключает модель (edit <-> t2i) и продолжает.

Задание — JSON-файл в папке очереди:
  {"kind": "t2i",
   "bundle": "~/gpu_bundle_x", "out": "~/gpu_out_x",
   "steps": 30, "lightning": false}
kind=t2i — production: products c background_prompt/background_prompts.
edit/posters доступны только с research_only=true и никогда не получают
publishable status.

Остановка: файл STOP-<device> (например STOP-cuda1) в папке очереди.

Запуск: python qwen_worker.py --device cuda:0 --watch ~/jobs --start edit

Lightning: t2i — lightx2v/Qwen-Image-Lightning; edit — официальная
lightx2v/Qwen-Image-Edit-2511-Lightning (8 шагов). LoRA от 2509 к 2511
несовместима (ломает следование промпту) — не подставлять.

Edit-2511 обязан грузиться через QwenImageEditPlusPipeline (model_index.json
модели объявляет именно его). Старый QwenImageEditPipeline загружается без
ошибки, но товар перерисовывается и сцена галлюцинируется.
"""

import argparse
import gc
import json
import time
from pathlib import Path

EDIT_MODEL = "Qwen/Qwen-Image-Edit-2511"
T2I_MODEL = "Qwen/Qwen-Image-2512"
LIGHTNING_LORA_T2I = "lightx2v/Qwen-Image-Lightning"
# Официальная Lightning под Edit-2511 (LoRA от 2509 несовместима — проверено)
LIGHTNING_LORA_EDIT_REPO = "lightx2v/Qwen-Image-Edit-2511-Lightning"
LIGHTNING_LORA_EDIT_WEIGHT = "Qwen-Image-Edit-2511-Lightning-8steps-V1.0-bf16.safetensors"

KIND_MODEL = {"edit": "edit", "t2i": "t2i", "posters": "t2i"}


def load_pipeline(which, device, lightning):
    import torch

    if which == "edit":
        # Edit-2511 — серия Plus: старый QwenImageEditPipeline тихо грузит
        # те же веса, но с другим препроцессингом условия — модель
        # перерисовывает товар и галлюцинирует сцену.
        from diffusers import QwenImageEditPlusPipeline

        pipe = QwenImageEditPlusPipeline.from_pretrained(
            EDIT_MODEL, torch_dtype=torch.bfloat16).to(device)
        if lightning:
            pipe.load_lora_weights(LIGHTNING_LORA_EDIT_REPO,
                                   weight_name=LIGHTNING_LORA_EDIT_WEIGHT)
    else:
        from diffusers import QwenImagePipeline

        pipe = QwenImagePipeline.from_pretrained(
            T2I_MODEL, torch_dtype=torch.bfloat16).to(device)
        if lightning:
            pipe.load_lora_weights(LIGHTNING_LORA_T2I)
    return pipe


def free_pipeline(pipe):
    import torch

    del pipe
    gc.collect()
    torch.cuda.empty_cache()


def claim_job(watch, loaded_kind):
    """Сначала задания под загруженную модель, затем чужие (перехват)."""
    pending = sorted(watch.glob("*.json"))
    ordered = ([p for p in pending
                if KIND_MODEL.get(_peek_kind(p)) == loaded_kind]
               + [p for p in pending
                  if KIND_MODEL.get(_peek_kind(p)) != loaded_kind])
    for path in ordered:
        claimed = path.with_suffix(".claim")
        try:
            path.rename(claimed)
            return claimed
        except OSError:
            continue  # забрал другой воркер
    return None


def _peek_kind(path):
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("kind", "edit")
    except Exception:  # noqa: BLE001
        return "edit"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", default="cuda")
    parser.add_argument("--watch", required=True)
    parser.add_argument("--start", default="edit", choices=["edit", "t2i"])
    parser.add_argument("--rub-per-hour", type=float, default=342.0)
    args = parser.parse_args()

    from PIL import Image

    watch = Path(args.watch).expanduser()
    watch.mkdir(parents=True, exist_ok=True)
    stop_marker = watch / f"STOP-{args.device.replace(':', '')}"
    rub_per_second = args.rub_per_hour / 3600.0

    loaded_kind = args.start
    print(f"[{args.device}] загрузка {loaded_kind}...", flush=True)
    pipe = load_pipeline(loaded_kind, args.device, lightning=True)
    print(f"[{args.device}] готов", flush=True)

    while True:
        if stop_marker.exists():
            stop_marker.unlink()
            print(f"[{args.device}] STOP — выхожу", flush=True)
            return
        job_file = claim_job(watch, loaded_kind)
        if job_file is None:
            time.sleep(5)
            continue
        try:
            job = json.loads(job_file.read_text(encoding="utf-8"))
            kind = job.get("kind", "edit")
            if kind not in KIND_MODEL:
                raise ValueError(f"unknown job kind: {kind}")
            if kind != "t2i" and job.get("research_only") is not True:
                raise ValueError("edit/posters require research_only=true")
            need = KIND_MODEL[kind]
            if need != loaded_kind:
                print(f"[{args.device}] перехват: {loaded_kind} -> {need}",
                      flush=True)
                free_pipeline(pipe)
                pipe = load_pipeline(need, args.device, lightning=True)
                loaded_kind = need
            bundle = Path(job["bundle"]).expanduser()
            out_dir = Path(job["out"]).expanduser()
            out_dir.mkdir(parents=True, exist_ok=True)
            steps = int(job.get("steps") or (4 if need == "t2i" else 8))
            width = max(256, min(int(job.get("width") or 896), 2048))
            height = max(256, min(int(job.get("height") or 1200), 2048))
            width -= width % 16
            height -= height % 16
            manifest = json.loads(
                (bundle / "manifest.json").read_text(encoding="utf-8"))
            sink = open(out_dir / "results.jsonl", "a", encoding="utf-8")

            def record(variant, mode, pid, lat, name, err="", status=None):
                sink.write(json.dumps({
                    "product_id": pid, "title": "", "variant": variant,
                    "mode": mode,
                    "status": status or ("background_only" if name else "error"),
                    "latency_s": round(lat, 1),
                    "cost_rub": round(lat * rub_per_second, 3),
                    "output": name if status == "research_only" else None,
                    "artifact": name,
                    "error": err[:300],
                }, ensure_ascii=False) + "\n")
                sink.flush()

            # Lightning-LoRA требуют true_cfg_scale=1.0 (без CFG).
            extra = {"true_cfg_scale": float(job.get("true_cfg", 1.0))}
            if kind == "edit":
                for item in manifest["products"]:
                    src = Image.open(bundle / item["photo"]).convert("RGB")
                    tasks = item.get("edit_prompts") or [
                        {"tag": "A", "prompt": item["edit_prompt"]}]
                    for t in tasks:
                        started = time.monotonic()
                        variant = ("gpu:qwen-edit" if t["tag"] == "A"
                                   else f"gpu:qwen-edit-{t['tag']}")
                        try:
                            img = pipe(image=[src], prompt=t["prompt"],
                                       num_inference_steps=steps,
                                       **extra).images[0]
                            name = f"{item['id']}_gpu_qwen-edit_{t['tag']}.png"
                            img.save(out_dir / name)
                            record(variant, "A", item["id"],
                                   time.monotonic() - started, name,
                                   status="research_only")
                        except Exception as e:  # noqa: BLE001
                            record(variant, "A", item["id"],
                                   time.monotonic() - started, None, str(e))
                        print(f"[{args.device}] {item['id']}/{t['tag']}",
                              flush=True)
            elif kind == "t2i":
                for item in manifest["products"]:
                    tasks = item.get("background_prompts") or [{
                        "tag": "B", "prompt": item["background_prompt"]}]
                    for task in tasks:
                        started = time.monotonic()
                        tag = str(task.get("tag") or "B")
                        try:
                            img = pipe(prompt=task["prompt"],
                                       num_inference_steps=steps,
                                       width=width, height=height,
                                       **extra).images[0]
                            name = f"{item['id']}_gpu_qwen-t2i_{tag}_background.png"
                            img.save(out_dir / name)
                            record(f"gpu:qwen-t2i-{tag}", "B", item["id"],
                                   time.monotonic() - started, name)
                        except Exception as e:  # noqa: BLE001
                            record(f"gpu:qwen-t2i-{tag}", "B", item["id"],
                                   time.monotonic() - started, None, str(e))
            else:  # posters
                for idx, st in enumerate(manifest["short_texts"], start=1):
                    started = time.monotonic()
                    try:
                        img = pipe(prompt=st["prompt"],
                                   num_inference_steps=steps,
                                   width=896, height=1152).images[0]
                        name = f"text3_{idx:02d}.png"
                        img.save(out_dir / name)
                        record("gpu:qwen-t2i-text", "T3", idx,
                               time.monotonic() - started, name,
                               status="research_only")
                    except Exception as e:  # noqa: BLE001
                        record("gpu:qwen-t2i-text", "T3", idx,
                               time.monotonic() - started, None, str(e))
            sink.close()
            job_file.rename(job_file.with_suffix(".done"))
            print(f"[{args.device}] job {job_file.stem} готов", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[{args.device}] job {job_file.name} упал: {e}", flush=True)
            job_file.rename(job_file.with_suffix(".failed"))


if __name__ == "__main__":
    main()
