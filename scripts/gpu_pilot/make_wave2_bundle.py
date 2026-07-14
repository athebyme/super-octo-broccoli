# -*- coding: utf-8 -*-
"""Сборка GPU-бандла «волна 2»: категорийные промпты + промо-постеры.

Вход: JSON [{id, group, title, photo}] (см. data/pilot_products_wave2.json).
Выход: бандл (photos/ + manifest.json) для scripts/gpu_pilot/run_qwen_pilot.py:
  - у каждого товара свои edit_prompt (доп-фото, режим A) и background_prompt
    (фон, режим B) по сцене его категории;
  - short_texts — готовые промо-промпты (dict {"prompt": ...}) с русским
    текстом, отрисовываемым самой Qwen-Image (режим text3).

Запуск: python scripts/gpu_pilot/make_wave2_bundle.py \
    --products data/pilot_products_wave2.json \
    --out data/infographic_pilot/gpu_bundle_wave2
"""

import argparse
import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from services.infographic_prompts import (  # noqa: E402
    ATMOSPHERE_PRESETS,
    build_background_prompt,
    build_background_prompt_for_scene,
    build_edit_prompt,
    build_edit_prompt_for_scene,
)

# Сцены категорий: эмоции через свет/фактуры, без людей и текста в кадре.
CATEGORY_SCENES = {
    "куклы": (
        "a neutral charcoal studio backdrop, soft rim lighting, discreet "
        "elegant product staging, premium catalog mood"
    ),
    "БДСМ": (
        "a dramatic dark scene: black leather texture, matte metal chain "
        "decor, deep red accent light, smoky atmosphere"
    ),
    "вибраторы": (
        "flowing silk waves in deep rose and plum tones, soft studio light, "
        "gentle gradient backdrop, premium beauty-editorial style"
    ),
    "лубриканты": (
        "a fresh aqua scene: clear water droplets on glass, light blue "
        "gradient, crisp clean studio light"
    ),
    "анальные": (
        "dark velvet drapery, warm amber candlelight, intimate premium mood"
    ),
    "мастурбаторы": (
        "a modern graphite tech-studio backdrop, subtle blue rim light, "
        "minimal composition"
    ),
    "насадки/кольца": (
        "a macro still-life on black stone with delicate gold accents, "
        "single dramatic spotlight"
    ),
    "крема/спреи": (
        "a serene spa scene: light stone, eucalyptus leaves, soft daylight, "
        "gentle steam"
    ),
    "фаллоимитаторы": (
        "a dark silk backdrop with warm bronze lighting, sculptural "
        "still-life staging"
    ),
    "вибро в трусики": (
        "delicate lace texture and blush pink silk, soft boudoir lighting"
    ),
    "бельё: трусики": (
        "an elegant boudoir: rumpled silk bedsheets, warm bedside lamp glow, "
        "soft shadows"
    ),
    "бельё: игровые костюмы": (
        "a playful boudoir scene: velvet chaise lounge, warm theatrical "
        "lighting, rich burgundy drapes"
    ),
    "бельё: чулки": (
        "a dark velvet chaise with sheer fabric flowing, warm intimate light"
    ),
    "бельё: пояса": (
        "a boudoir vanity scene: silk, pearls and warm mirror lights"
    ),
    "БДСМ: одежда": (
        "a dark leather and smoke scene, dramatic contrast lighting, premium "
        "fashion-editorial mood"
    ),
}

PROMO_TEXTS = ("НОВИНКА", "ХИТ ПРОДАЖ")

_PROMO_TEMPLATE = (
    "Vertical 3:4 e-commerce promo poster: {scene}. Large bold Russian text "
    '"{text}" in elegant modern typography as the focal point, high '
    "contrast, premium minimal composition. No people, no watermarks, "
    "no other text."
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--products", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--max-side", type=int, default=1280)
    args = parser.parse_args()

    import requests
    from PIL import Image

    items = json.loads(Path(args.products).read_text(encoding="utf-8"))
    out = Path(args.out)
    (out / "photos").mkdir(parents=True, exist_ok=True)

    products, failed = [], 0
    for it in items:
        scene = CATEGORY_SCENES.get(it["group"])
        if not scene:
            print(f"⚠️ нет сцены для группы {it['group']} — пропуск {it['id']}")
            continue
        try:
            r = requests.get(it["photo"], timeout=45)
            r.raise_for_status()
            img = Image.open(io.BytesIO(r.content)).convert("RGB")
            img.thumbnail((args.max_side, args.max_side))
            name = f"photos/{it['id']}.png"
            img.save(out / name)
        except Exception as e:
            print(f"⚠️ фото {it['id']}: {str(e)[:100]} — пропуск")
            failed += 1
            continue
        products.append({
            "id": it["id"],
            "photo": name,
            "group": it["group"],
            "edit_prompt": build_edit_prompt_for_scene(scene),
            "background_prompt": build_background_prompt_for_scene(scene),
        })

    short_texts = [
        {"prompt": _PROMO_TEMPLATE.format(scene=scene, text=text),
         "group": group, "text": text}
        for group, scene in CATEGORY_SCENES.items()
        for text in PROMO_TEXTS
    ]

    presets = {
        key: {
            "edit_prompt": build_edit_prompt(key),
            "background_prompt": build_background_prompt(key),
        }
        for key in ATMOSPHERE_PRESETS
    }

    manifest = {
        "products": products,
        "presets": presets,
        "text_samples": [],
        "short_texts": short_texts,
    }
    (out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"✅ бандл: {out} — товаров {len(products)}, "
          f"промо-постеров {len(short_texts)}, ошибок фото {failed}")


if __name__ == "__main__":
    main()
