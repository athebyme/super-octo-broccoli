# -*- coding: utf-8 -*-
"""GPU-ветка пилота: Qwen-Image-Edit / Qwen-Image на арендованном H100.

Запускается НА арендованной машине (не в репо-окружении):
    python run_qwen_pilot.py --bundle ./gpu_bundle --out ./gpu_out \\
        --mode b --lightning --rub-per-hour 342

Production-режим: b (t2i фон). a/text2/text3 требуют --research-only,
потому что меняют товар или генерируют текст и не могут быть опубликованы.

VRAM: Edit-2511 и Image-2512 (обе ~20B, bf16 ~55-60 ГБ) НЕ помещаются в 80 ГБ
одновременно — при --mode all скрипт сначала выполняет edit-фазы (a, text2),
выгружает пайплайн, затем t2i-фазы (b, text3).
"""

import argparse
import gc
import json
import time
from pathlib import Path

EDIT_MODEL = "Qwen/Qwen-Image-Edit-2511"
T2I_MODEL = "Qwen/Qwen-Image-2512"
LIGHTNING_LORA_T2I = "lightx2v/Qwen-Image-Lightning"
# Официальная Lightning под Edit-2511; LoRA от 2509 несовместима
LIGHTNING_LORA_EDIT_REPO = "lightx2v/Qwen-Image-Edit-2511-Lightning"
LIGHTNING_LORA_EDIT_WEIGHT = "Qwen-Image-Edit-2511-Lightning-8steps-V1.0-bf16.safetensors"

STYLIZE_PROMPT = (
    "Turn the plain black text into glowing neon tube letters on a dark "
    "background. Keep every letter shape and every character exactly as in "
    "the source image, same spelling. Add soft neon glow and reflections."
)
SHORT_TEXT_PROMPT = (
    'A bold promotional sticker with the text "{text}" in clean modern '
    "Cyrillic-capable typography, neon glow on dark background, centered."
)


def _load_edit_pipeline(lightning, device="cuda"):
    import torch
    # Edit-2511 — серия Plus: старый QwenImageEditPipeline тихо грузит те же
    # веса, но с другим препроцессингом условия — товар перерисовывается.
    from diffusers import QwenImageEditPlusPipeline

    pipe = QwenImageEditPlusPipeline.from_pretrained(
        EDIT_MODEL, torch_dtype=torch.bfloat16).to(device)
    if lightning:
        pipe.load_lora_weights(LIGHTNING_LORA_EDIT_REPO,
                               weight_name=LIGHTNING_LORA_EDIT_WEIGHT)
    return pipe


def _load_t2i_pipeline(lightning, device="cuda"):
    import torch
    from diffusers import QwenImagePipeline

    pipe = QwenImagePipeline.from_pretrained(
        T2I_MODEL, torch_dtype=torch.bfloat16).to(device)
    if lightning:
        pipe.load_lora_weights(LIGHTNING_LORA_T2I)
    return pipe


def _free(pipe):
    """Выгружает пайплайн из VRAM перед загрузкой следующего."""
    import torch

    del pipe
    gc.collect()
    torch.cuda.empty_cache()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--mode", default="b",
                        choices=["all", "a", "b", "text2", "text3"])
    parser.add_argument("--lightning", action="store_true")
    parser.add_argument("--steps", type=int, default=None,
                        help="Шаги (Lightning: edit=8, t2i=4; full=40)")
    parser.add_argument("--true-cfg", type=float, default=1.0)
    parser.add_argument("--research-only", action="store_true",
                        help="Разрешить непубликуемые edit/text benchmarks")
    parser.add_argument("--rub-per-hour", type=float, default=342.0)
    parser.add_argument("--preset", default="boudoir")
    parser.add_argument("--device", default="cuda",
                        help="cuda, cuda:0, cuda:1 — для параллельных процессов "
                             "на multi-GPU машине (по процессу на режим)")
    args = parser.parse_args()

    from PIL import Image

    bundle = Path(args.bundle)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    edit_steps = args.steps or (8 if args.lightning else 40)
    t2i_steps = args.steps or (4 if args.lightning else 40)
    rub_per_second = args.rub_per_hour / 3600.0
    preset = manifest["presets"][args.preset]

    sink = open(out_dir / "results.jsonl", "a", encoding="utf-8")

    def record(variant, mode, product_id, latency, out_name, error="", status=None):
        sink.write(json.dumps({
            "product_id": product_id,
            "title": "",
            "variant": variant,
            "mode": mode,
            "status": status or ("background_only" if out_name else "error"),
            "latency_s": round(latency, 1),
            "cost_rub": round(latency * rub_per_second, 3),
            "output": out_name if status == "research_only" else None,
            "artifact": out_name,
            "error": error[:300],
        }, ensure_ascii=False) + "\n")
        sink.flush()

    edit_phases = args.mode in ("all", "a", "text2")
    t2i_phases = args.mode in ("all", "b", "text3")
    if (edit_phases or args.mode == "text3") and not args.research_only:
        parser.error("a/text2/text3/all требуют --research-only")

    if edit_phases:
        edit_pipe = _load_edit_pipeline(args.lightning, args.device)

        if args.mode in ("all", "a"):
            for item in manifest["products"]:
                src = Image.open(bundle / item["photo"]).convert("RGB")
                # edit_prompts: [{tag, prompt}] — несколько генераций на товар
                # (флоу-тест: разные сцены + инфографика). Иначе один промпт:
                # per-product edit_prompt либо глобальный пресет.
                jobs = item.get("edit_prompts") or [
                    {"tag": "A",
                     "prompt": item.get("edit_prompt") or preset["edit_prompt"]}]
                for job in jobs:
                    started = time.monotonic()
                    variant = ("gpu:qwen-edit" if job["tag"] == "A"
                               else f"gpu:qwen-edit-{job['tag']}")
                    try:
                        result = edit_pipe(image=[src], prompt=job["prompt"],
                                           num_inference_steps=edit_steps,
                                           true_cfg_scale=args.true_cfg).images[0]
                        name = f"{item['id']}_gpu_qwen-edit_{job['tag']}.png"
                        result.save(out_dir / name)
                        record(variant, "A", item["id"],
                               time.monotonic() - started, name,
                               status="research_only")
                    except Exception as e:
                        record(variant, "A", item["id"],
                               time.monotonic() - started, None, str(e))

        if args.mode in ("all", "text2"):
            for idx, sample in enumerate(manifest["text_samples"], start=1):
                src = Image.open(bundle / sample["file"]).convert("RGB")
                started = time.monotonic()
                try:
                    result = edit_pipe(image=[src], prompt=STYLIZE_PROMPT,
                                       num_inference_steps=edit_steps,
                                       true_cfg_scale=args.true_cfg).images[0]
                    name = f"text2_{idx:02d}.png"
                    result.save(out_dir / name)
                    record("gpu:qwen-edit-text", "T2", idx,
                           time.monotonic() - started, name,
                           status="research_only")
                except Exception as e:
                    record("gpu:qwen-edit-text", "T2", idx,
                           time.monotonic() - started, None, str(e))

        _free(edit_pipe)

    if t2i_phases:
        t2i_pipe = _load_t2i_pipeline(args.lightning, args.device)

        if args.mode in ("all", "b"):
            for item in manifest["products"]:
                tasks = item.get("background_prompts") or [{
                    "tag": "B",
                    "prompt": item.get("background_prompt") or preset["background_prompt"],
                }]
                for task in tasks:
                    started = time.monotonic()
                    tag = str(task.get("tag") or "B")
                    try:
                        result = t2i_pipe(
                            prompt=task["prompt"],
                            num_inference_steps=t2i_steps,
                            true_cfg_scale=args.true_cfg,
                            width=896,
                            height=1200,
                        ).images[0]
                        name = f"{item['id']}_gpu_qwen-t2i_{tag}_background.png"
                        result.save(out_dir / name)
                        record(f"gpu:qwen-t2i-{tag}", "B", item["id"],
                               time.monotonic() - started, name)
                    except Exception as e:
                        record(f"gpu:qwen-t2i-{tag}", "B", item["id"],
                               time.monotonic() - started, None, str(e))

        if args.mode in ("all", "text3"):
            for idx, text in enumerate(manifest["short_texts"], start=1):
                started = time.monotonic()
                try:
                    # строка — шаблонный стикер; dict {"prompt": ...} — готовый
                    # промо-промпт целиком (инфографика с русским текстом)
                    if isinstance(text, dict):
                        prompt = text["prompt"]
                    else:
                        prompt = SHORT_TEXT_PROMPT.format(text=text)
                    result = t2i_pipe(prompt=prompt,
                                      num_inference_steps=t2i_steps,
                                      true_cfg_scale=args.true_cfg,
                                      width=896, height=1200).images[0]
                    name = f"text3_{idx:02d}.png"
                    result.save(out_dir / name)
                    record("gpu:qwen-t2i-text", "T3", idx,
                           time.monotonic() - started, name,
                           status="research_only")
                except Exception as e:
                    record("gpu:qwen-t2i-text", "T3", idx,
                           time.monotonic() - started, None, str(e))

        _free(t2i_pipe)

    sink.close()
    print(f"✅ GPU-прогон завершён: {out_dir}/results.jsonl")


if __name__ == "__main__":
    main()
