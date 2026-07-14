# -*- coding: utf-8 -*-
"""Сборка отчёта run5 — Qwen edit с исправленным пайплайном.

Читает results.jsonl всех прогонов из data/infographic_pilot/gpu_fix/,
обогащает title из pilot-JSON, складывает картинки в плоский каталог run5/
(+ orig/ с исходниками, + t/ с JPEG-превью ~30 КБ — полноразмерные PNG
открываются кликом), строит report.html через build_report_html(originals=...).

Запуск из корня репо (нужен python с Pillow):
    python scripts/gpu_pilot/build_run5_report.py
Публикация:
    docker cp data/infographic_pilot/run5 seller-platform:/app/static/infographic_pilot/run5
"""
import importlib.util
import json
import re
import shutil
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "data/infographic_pilot/gpu_fix"
OUT = REPO / "data/infographic_pilot/run5"

spec = importlib.util.spec_from_file_location(
    "infographic_pilot", REPO / "scripts/infographic_pilot.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

titles = {}
for f in ["pilot_products_wave2.json", "pilot_products_diverse2.json",
          "pilot_products.json", "pilot_products_diverse.json"]:
    p = REPO / "data" / f
    if p.exists():
        for item in json.load(open(p)):
            titles.setdefault(item["id"], item.get("title") or "")

OUT.mkdir(parents=True, exist_ok=True)
(OUT / "orig").mkdir(exist_ok=True)

rows = []
for run in sorted(SRC.iterdir()):
    rj = run / "results.jsonl"
    if not rj.exists():
        continue
    for line in open(rj, encoding="utf-8"):
        r = json.loads(line)
        r["title"] = titles.get(r["product_id"], "")
        rows.append(r)
        if r.get("output"):
            src_img = run / r["output"]
            if src_img.exists():
                shutil.copy(src_img, OUT / r["output"])

originals = {}
for photo_dir in sorted(SRC.glob("photos_*")):
    for img in photo_dir.glob("*.png"):
        pid = int(img.stem)
        if pid not in originals:
            shutil.copy(img, OUT / "orig" / img.name)
            originals[pid] = f"orig/{img.name}"

html = m.build_report_html(rows, originals=originals)
html = html.replace(
    "<title>Infographic pilot report</title>",
    "<title>run5 — Qwen edit (исправленный пайплайн)</title>", 1)
html = html.replace(
    "<h1>Пилот «Фотостудии»</h1>",
    "<h1>run5 — self-host Qwen edit</h1>"
    "<p>51 товар × 4 сцены (lux/info/neon/cat) + категорийные сцены. "
    "Qwen-Image-Edit-2511 через QwenImageEditPlusPipeline, Lightning 8 шагов, "
    "true_cfg=1.0, 2×A100. Сцены: lux — чёрный мрамор/золото, info — промо-плашка "
    "с русским текстом, neon — неон, cat — категорийная сцена.</p>", 1)

from PIL import Image as PILImage  # noqa: E402 — Pillow нужен только для превью

tdir = OUT / "t"
tdir.mkdir(exist_ok=True)


def _thumb(rel):
    tname = rel.replace("/", "_") + ".jpg"
    tp = tdir / tname
    if not tp.exists():
        im = PILImage.open(OUT / rel).convert("RGB")
        im.thumbnail((360, 360))
        im.save(tp, "JPEG", quality=82)
    return f"t/{tname}"


html = re.sub(
    r"<img src='([^']+)' loading='lazy'>",
    lambda mt: (f"<a href='{mt.group(1)}' target='_blank'>"
                f"<img src='{_thumb(mt.group(1))}' loading='lazy'></a>"),
    html)

(OUT / "report.html").write_text(html, encoding="utf-8")
ok = sum(1 for r in rows if r["status"] == "ok")
print(f"строк: {len(rows)} | ok: {ok} | оригиналов: {len(originals)}")
print(f"отчёт: {OUT}/report.html")
