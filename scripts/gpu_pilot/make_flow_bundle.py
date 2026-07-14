# -*- coding: utf-8 -*-
"""Production-safe GPU bundle: the model creates text-free backgrounds only.

Берёт по одному товару на категорию из готового бандла волны 2 и собирает
манифест с background_prompts (cat/lux/neon). Товар и текст никогда не
передаются Qwen; финальный foreground-композит и типографика выполняются
локально детерминированными сервисами.

Запуск: python scripts/gpu_pilot/make_flow_bundle.py \
    --wave2 data/infographic_pilot/gpu_bundle_wave2 \
    --out data/infographic_pilot/gpu_bundle_flow
"""

import argparse
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from services.infographic_prompts import (  # noqa: E402
    ATMOSPHERE_PRESETS,
    build_background_prompt_for_scene,
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--wave2", required=True,
                        help="бандл волны 2 (источник фото и категорийных промптов)")
    parser.add_argument("--out", required=True)
    parser.add_argument("--per-group", type=int, default=1)
    args = parser.parse_args()

    src = Path(args.wave2)
    out = Path(args.out)
    (out / "photos").mkdir(parents=True, exist_ok=True)
    manifest = json.loads((src / "manifest.json").read_text(encoding="utf-8"))

    lux = ATMOSPHERE_PRESETS["luxury"]["scene"]
    neon = ATMOSPHERE_PRESETS["neon"]["scene"]

    taken = {}
    products = []
    for p in manifest["products"]:
        group = p.get("group")
        if not group or taken.get(group, 0) >= args.per_group:
            continue
        photo = src / p["photo"]
        if not photo.exists():
            continue
        taken[group] = taken.get(group, 0) + 1
        shutil.copy2(photo, out / "photos" / photo.name)
        products.append({
            "id": p["id"],
            "photo": p["photo"],
            "group": group,
            "background_prompts": [
                {"tag": "cat", "prompt": p["background_prompt"]},
                {"tag": "lux", "prompt": build_background_prompt_for_scene(lux)},
                {"tag": "neon", "prompt": build_background_prompt_for_scene(neon)},
            ],
        })

    (out / "manifest.json").write_text(json.dumps({
        "production_policy": "background_only_pixel_preserved_composite",
        "products": products,
        "presets": manifest["presets"],
        "text_samples": [],
        "short_texts": [],
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"✅ флоу-бандл: {out} — товаров {len(products)}, "
          f"генераций фона {len(products) * 3}")


if __name__ == "__main__":
    main()
