# -*- coding: utf-8 -*-
"""Пилот «Фотостудии» (Phase 0): матрица генераций для выбора модели и режима.

Спека: docs/superpowers/specs/2026-07-14-infographics-design.md (§3, Phase 0).

Использование:
    SKIP_SCHEDULER=1 GEN_API_KEY=... AITUNNEL_API_KEY=... \\
    python scripts/infographic_pilot.py \\
        --seller-id 1 --limit 20 --preset boudoir \\
        --variants gen_api:flux-2:B,gen_api:nano-banana:A,aitunnel:seedream-4.5:A \\
        --budget-rub 800 [--products 1,2,3] [--dry-run] \\
        [--export-gpu-bundle] [--extra-results path/to/gpu/results.jsonl]

Пишет data/infographic_pilot/run_<id>/: PNG-файлы, results.jsonl, report.html.
"""

import argparse
import html
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

os.environ.setdefault("SKIP_SCHEDULER", "1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

KNOWN_PROVIDERS = ("gen_api", "aitunnel")

# Цены за 1 генерацию, ₽ (июль 2026, Gen-API/AITunnel; спека §2.1)
PRICE_TABLE_RUB = {
    "gen_api:flux-2": 3.3,
    "gen_api:flux-kontext-pro": 8.0,
    "gen_api:seedream-4-5": 10.0,
    "gen_api:nano-banana": 9.75,
    "aitunnel:seedream-4.5": 6.8,
    "aitunnel:gpt-image-2": 1.53,
    "gen_api:midjourney": 10.0,  # оценка — уточнить по факту в ЛК Gen-API
}
DEFAULT_PRICE_RUB = 10.0  # незнакомая модель — консервативная оценка

# t2i-only модели: не умеют product-preserving edit, допустим только режим B.
T2I_ONLY_MODELS = ("midjourney",)


@dataclass
class Variant:
    provider: str
    model: str
    mode: str  # "A" — i2i edit вокруг товара, "B" — пустой фон (t2i)

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.model}"

    @property
    def slug(self) -> str:
        return f"{self.provider}_{self.model}_{self.mode}".replace(".", "-")


def parse_variants(spec):
    """'gen_api:flux-2:B,aitunnel:seedream-4.5:A' -> [Variant, ...]"""
    variants = []
    for chunk in (spec or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = chunk.split(":")
        if len(parts) != 3:
            raise ValueError(f"Вариант '{chunk}': ожидается provider:model:mode")
        provider, model, mode = parts[0].strip(), parts[1].strip(), parts[2].strip().upper()
        if provider not in KNOWN_PROVIDERS:
            raise ValueError(f"Неизвестный провайдер '{provider}' (можно: {KNOWN_PROVIDERS})")
        if mode not in ("A", "B"):
            raise ValueError(f"Режим '{mode}' — допустимы только A или B")
        if mode == "A" and any(t in model.lower() for t in T2I_ONLY_MODELS):
            raise ValueError(
                f"Модель '{model}' не сохраняет товар (t2i-only) — "
                f"для неё допустим только режим B")
        variants.append(Variant(provider=provider, model=model, mode=mode))
    if not variants:
        raise ValueError("Пустой список вариантов")
    return variants


def estimate_cost_rub(variants, n_products):
    """Оценка стоимости прогона всей матрицы, ₽."""
    return sum(PRICE_TABLE_RUB.get(v.key, DEFAULT_PRICE_RUB) for v in variants) * n_products


def first_photo_url(photo_urls_json):
    """Первый URL из JSON-поля ImportedProduct.photo_urls, иначе None."""
    if not photo_urls_json:
        return None
    if isinstance(photo_urls_json, list):
        urls = photo_urls_json
    else:
        try:
            urls = json.loads(photo_urls_json)
        except (ValueError, TypeError):
            return None
    if not isinstance(urls, list) or not urls:
        return None
    first = urls[0]
    if isinstance(first, str) and first.strip():
        return first.strip()
    if isinstance(first, dict):
        # прод-формат ImportedProduct.photo_urls: [{"original": "https://..."}]
        for key in ("original", "url", "processed"):
            value = first.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _make_service(variant):
    """Создаёт ImageGenerationService под вариант (ключи из env)."""
    from services.image_generation_service import (
        ImageGenerationConfig,
        ImageGenerationService,
        ImageProvider,
    )

    provider = ImageProvider(variant.provider)
    config = ImageGenerationConfig.from_env(provider)
    if config is None:
        raise SystemExit(
            f"Нет API-ключа для {variant.provider}: задайте "
            f"{'GEN_API_KEY' if variant.provider == 'gen_api' else 'AITUNNEL_API_KEY'}"
        )
    # Midjourney стабильно дольше 120с — расширяем окно поллинга
    config.timeout = 300 if "midjourney" in variant.model.lower() else 120
    if provider == ImageProvider.GEN_API:
        if variant.mode == "A":
            config.gen_api_edit_model = variant.model
        else:
            config.gen_api_model = variant.model
    else:
        if variant.mode == "A":
            config.aitunnel_edit_model = variant.model
        else:
            config.aitunnel_model = variant.model
    return ImageGenerationService(config)


def load_products_json(path):
    """Товары из JSON-файла [{id, title, photo}] — режим без БД (снапшот из контейнера)."""
    items = json.loads(Path(path).read_text(encoding="utf-8"))
    products = []
    for it in items:
        pid, photo = it.get("id"), (it.get("photo") or "").strip()
        if isinstance(pid, int) and pid > 0 and photo:
            products.append((pid, it.get("title") or f"product-{pid}", photo))
    return products


def select_products(seller_id, product_ids, limit):
    """Товары с фото, tenant-scoped (id + seller_id). Возвращает [(id, title, photo_url)]."""
    from models import ImportedProduct

    query = ImportedProduct.query.filter(ImportedProduct.seller_id == seller_id)
    if product_ids:
        query = query.filter(ImportedProduct.id.in_(product_ids))
    rows = query.limit(500).all()
    selected = []
    for p in rows:
        url = first_photo_url(getattr(p, "photo_urls", None))
        if url:
            selected.append((p.id, (p.title or f"product-{p.id}"), url))
        if len(selected) >= limit:
            break
    return selected


def run_matrix(products, variants, preset, run_dir, budget_rub):
    """Прогоняет матрицу товары × варианты. Возвращает список строк results.jsonl."""
    from services.image_generation_service import is_censorship_refusal
    from services.infographic_prompts import build_background_prompt, build_edit_prompt
    from services.infographic_quality import (
        ImageQualityError,
        canonicalize_image,
        evaluate_background_text,
        evaluate_final_image,
    )

    results = []
    spent = 0.0
    results_path = run_dir / "results.jsonl"
    with open(results_path, "a", encoding="utf-8") as sink:
        for variant in variants:
            service = _make_service(variant)
            price = PRICE_TABLE_RUB.get(variant.key, DEFAULT_PRICE_RUB)
            prompt = (build_edit_prompt(preset) if variant.mode == "A"
                      else build_background_prompt(preset))
            for product_id, title, photo_url in products:
                if spent + price > budget_rub:
                    print(f"⛔ Бюджет {budget_rub}₽ исчерпан (потрачено ~{spent:.0f}₽) — стоп.")
                    return results
                started = time.monotonic()
                if variant.mode == "A":
                    ok, image, err = service.edit_image(
                        prompt=prompt, source_image_url=photo_url)
                else:
                    ok, image, err = service.generate_from_prompt(
                        prompt=prompt, width=900, height=1200)
                latency = round(time.monotonic() - started, 1)
                spent += price
                out_name = f"{product_id}_{variant.slug}.png"
                artifact_name = None
                quality = None
                composite_metadata = None
                if ok and image:
                    if variant.mode == "B":
                        artifact_name = f"{product_id}_{variant.slug}_background.png"
                        (run_dir / artifact_name).write_bytes(canonicalize_image(image))
                        composite = try_compose_mode_b_result(image, photo_url)
                        if composite is not None:
                            comp_name = f"{product_id}_{variant.slug}_comp.png"
                            (run_dir / comp_name).write_bytes(composite.image_bytes)
                            out_name = comp_name
                            composite_metadata = composite.metadata
                            quality = evaluate_final_image(
                                composite.image_bytes,
                                identity_mode="pixel_preserved_composite",
                                text_mode="none",
                                claims_pass=True,
                                composite_metadata=composite.metadata,
                                background_text_check=evaluate_background_text(image),
                            )
                            status = quality["status"]
                        else:
                            status = "rejected"
                            out_name = None
                            err = "Не собран identity-preserving foreground-композит"
                    else:
                        try:
                            normalized = canonicalize_image(image)
                            (run_dir / out_name).write_bytes(normalized)
                            quality = evaluate_final_image(
                                normalized,
                                identity_mode="generative_edit",
                                text_mode="none",
                                claims_pass=True,
                            )
                            status = quality["status"]
                        except ImageQualityError as exc:
                            status = "rejected"
                            out_name = None
                            err = str(exc)
                else:
                    out_name = None
                    status = ("nsfw" if (err or "").startswith("NSFW:")
                              or is_censorship_refusal(err) else "error")
                row = {
                    "product_id": product_id,
                    "title": title,
                    "variant": variant.key,
                    "mode": variant.mode,
                    "status": status,
                    "latency_s": latency,
                    "cost_rub": price,
                    "output": out_name,
                    "artifact": artifact_name,
                    "error": (err or "")[:300],
                    "quality": quality,
                    "composite_metadata": composite_metadata,
                }
                results.append(row)
                sink.write(json.dumps(row, ensure_ascii=False) + "\n")
                sink.flush()
                print(f"[{status:5s}] {variant.key} mode={variant.mode} "
                      f"product={product_id} {latency}s ~{price}₽")
    return results


def build_gpu_manifest(products, presets, text_files):
    """Манифест input-бандла для GPU-скрипта (scripts/gpu_pilot/)."""
    from services.infographic_prompts import (
        SHORT_TEXT_SAMPLES,
        build_background_prompt,
        build_edit_prompt,
    )

    return {
        "production_policy": "background_only_pixel_preserved_composite",
        "products": [{"id": pid, "photo": f"photos/{pid}.png"}
                     for pid, _title, _url in products],
        "presets": {
            key: {
                "edit_prompt": build_edit_prompt(key),
                "background_prompt": build_background_prompt(key),
            }
            for key in presets
        },
        "text_samples": [{"file": rel, "phrase": phrase}
                         for rel, phrase in text_files],
        "short_texts": list(SHORT_TEXT_SAMPLES),
        "text_benchmark_policy": "research_only_never_publish",
    }


_TEXT_SAMPLE_HTML = """<!doctype html><meta charset="utf-8">
<style>body{{margin:0;width:900px;height:300px;display:flex;align-items:center;
justify-content:center;background:#ffffff}}
div{{font-family:'Inter',Arial,sans-serif;font-weight:800;font-size:64px;
color:#111;text-align:center;padding:0 40px}}</style>
<body><div>{phrase}</div></body>"""


def _render_text_samples(text_dir):
    """Рендерит фразы уровня 2 в PNG через Playwright (глифы задаём мы)."""
    from playwright.sync_api import sync_playwright

    from services.infographic_prompts import TEXT_SAMPLE_PHRASES

    text_dir.mkdir(parents=True, exist_ok=True)
    rendered = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 900, "height": 300})
        for idx, phrase in enumerate(TEXT_SAMPLE_PHRASES, start=1):
            page.set_content(_TEXT_SAMPLE_HTML.format(phrase=phrase))
            rel = f"text/phrase_{idx:02d}.png"
            page.screenshot(path=str(text_dir / f"phrase_{idx:02d}.png"))
            rendered.append((rel, phrase))
        browser.close()
    return rendered


def export_gpu_bundle(products, bundle_dir):
    """Собирает input-бандл для GPU-ветки: фото + текст-сэмплы + manifest.json."""
    import requests as _requests

    photos_dir = bundle_dir / "photos"
    photos_dir.mkdir(parents=True, exist_ok=True)
    exported = []
    for pid, title, url in products:
        try:
            resp = _requests.get(url, timeout=60)
            if resp.status_code == 200 and resp.content:
                (photos_dir / f"{pid}.png").write_bytes(resp.content)
                exported.append((pid, title, url))
            else:
                print(f"⚠️ Фото товара {pid}: HTTP {resp.status_code} — пропуск")
        except Exception as e:
            print(f"⚠️ Фото товара {pid}: {e} — пропуск")

    text_files = _render_text_samples(bundle_dir / "text")
    manifest = build_gpu_manifest(
        exported, presets=list(("boudoir", "neon", "luxury", "spa")),
        text_files=text_files)
    (bundle_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✅ GPU-бандл: {bundle_dir} (фото: {len(exported)}, "
          f"текстов: {len(text_files)})")
    return bundle_dir


def compose_product_on_background(background_bytes, product_bytes, cutout=None):
    """Compatibility wrapper around the production identity compositor."""
    from services.infographic_quality import compose_identity_preserving

    return compose_identity_preserving(
        background_bytes, product_bytes, cutout=cutout
    ).image_bytes


def try_compose_mode_b_result(background_bytes, product_photo_url):
    """Return an audited composite, or None; never fall back to model editing."""
    try:
        import requests as _requests

        from services.infographic_quality import compose_identity_preserving

        photo = _requests.get(product_photo_url, timeout=60)
        if photo.status_code != 200 or not photo.content:
            return None
        return compose_identity_preserving(background_bytes, photo.content)
    except Exception as exc:
        print(f"⚠️ Композит режима B отклонён: {exc}")
        return None


def try_compose_mode_b(background_bytes, product_photo_url):
    """Legacy bytes-only wrapper used by existing scripts/tests."""
    result = try_compose_mode_b_result(background_bytes, product_photo_url)
    return result.image_bytes if result is not None else None


_BADGE_COLORS = {
    "auto_pass": "#1a7f37",
    "review_required": "#8250df",
    "rejected": "#c0392b",
    "background_only": "#3d6b8e",
    "research_only": "#777",
    "nsfw": "#b35900",
    "error": "#c0392b",
}


def _normalize_result_row(row):
    normalized = dict(row)
    if normalized.get("status") == "ok":
        normalized["status"] = "review_required"
        normalized.setdefault("quality_note", "legacy result had no quality gates")
    return normalized


def _load_extra_results(path):
    """Строки results.jsonl соседнего прогона; пути картинок делаем относительными
    отчёту: results.jsonl лежит рядом со своими PNG, поэтому префиксуем имя папки."""
    rows = []
    if not path:
        return rows
    base = Path(path).resolve().parent.name
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = _normalize_result_row(json.loads(line))
            if row.get("output"):
                row["output"] = f"{base}/{row['output']}"
            rows.append(row)
    return rows


def build_report_html(results, extra_results_path=None, originals=None):
    """Самодостаточный HTML-отчёт: сводка по вариантам + матрица товар × вариант.

    originals: {product_id: относительный путь к исходному фото} — добавляет
    колонку «Оригинал» в матрицу.
    """
    rows = [_normalize_result_row(row) for row in results]
    rows += _load_extra_results(extra_results_path)
    originals = originals or {}
    variants = sorted({r["variant"] + ":" + r["mode"] for r in rows})
    products = {}
    for r in rows:
        products.setdefault(r["product_id"], {"title": r.get("title", ""), "cells": {}})
        products[r["product_id"]]["cells"][r["variant"] + ":" + r["mode"]] = r

    summary = {}
    for r in rows:
        s = summary.setdefault(r["variant"] + ":" + r["mode"],
                               {"auto_pass": 0, "review_required": 0,
                                "rejected": 0, "nsfw": 0, "error": 0,
                                "background_only": 0, "research_only": 0,
                                "latency": [], "cost": 0.0})
        s[r["status"]] = s.get(r["status"], 0) + 1
        s["latency"].append(r.get("latency_s") or 0)
        s["cost"] += r.get("cost_rub") or 0

    parts = [
        "<!doctype html><meta charset='utf-8'><title>Infographic pilot report</title>",
        "<style>body{font-family:Inter,Arial,sans-serif;margin:24px;background:#faf7f2}",
        "table{border-collapse:collapse}td,th{border:1px solid #ddd;padding:6px;",
        "vertical-align:top;text-align:center}img{max-width:180px;display:block}",
        ".badge{display:inline-block;padding:2px 8px;border-radius:6px;color:#fff;",
        "font-size:12px}</style>",
        "<h1>Пилот «Фотостудии»</h1><h2>Сводка по вариантам</h2>",
        "<table><tr><th>Вариант</th><th>auto pass</th><th>review</th>",
        "<th>rejected</th><th>background</th><th>research</th>"
        "<th>nsfw</th><th>error</th><th>publish yield</th>",
        "<th>сред. время, с</th><th>₽/попытку</th><th>₽/auto pass</th>",
        "<th>потрачено, ₽</th></tr>",
    ]
    per_unit = {}
    for key in variants:
        s = summary[key]
        n = sum(s[name] for name in (
            "auto_pass", "review_required", "rejected", "nsfw", "error"))
        n += s["background_only"] + s["research_only"]
        avg = round(sum(s["latency"]) / max(len(s["latency"]), 1), 1)
        unit = round(s["cost"] / n, 2) if n else 0.0
        accepted_unit = (round(s["cost"] / s["auto_pass"], 2)
                         if s["auto_pass"] else "—")
        publish_yield = round(100 * s["auto_pass"] / n, 1) if n else 0.0
        per_unit[key] = unit
        parts.append(
            f"<tr><td>{html.escape(key)}</td><td>{s['auto_pass']}</td>"
            f"<td>{s['review_required']}</td><td>{s['rejected']}</td>"
            f"<td>{s['background_only']}</td><td>{s['research_only']}</td>"
            f"<td>{s['nsfw']}</td><td>{s['error']}</td>"
            f"<td>{publish_yield}%</td><td>{avg}</td><td>{unit}</td>"
            f"<td>{accepted_unit}</td>"
            f"<td>{round(s['cost'], 2)}</td></tr>")
    # Прогноз месячной стоимости по тарифу варианта (+20% на перегенерации)
    parts.append(
        "</table><h2>Прогноз на месяц (по тарифу, +20% на перегенерации)</h2>"
        "<table><tr><th>Вариант</th><th>500 карточек, hero</th>"
        "<th>500 карточек, hero+2 доп.</th><th>1000 карточек, hero+2 доп.</th></tr>")
    for key in variants:
        unit = per_unit.get(key) or 0.0
        parts.append(
            f"<tr><td>{key}</td>"
            f"<td>{round(unit * 500 * 1.2)}₽</td>"
            f"<td>{round(unit * 1500 * 1.2)}₽</td>"
            f"<td>{round(unit * 3000 * 1.2)}₽</td></tr>")
    parts.append("</table><h2>Матрица</h2><table><tr><th>Товар</th>")
    if originals:
        parts.append("<th>Оригинал</th>")
    parts.extend(f"<th>{html.escape(key)}</th>" for key in variants)
    parts.append("</tr>")
    for product_id, info in sorted(products.items()):
        parts.append(
            f"<tr><td><b>{int(product_id)}</b><br>"
            f"{html.escape(str(info['title'])[:60])}</td>")
        if originals:
            orig = originals.get(product_id)
            parts.append(
                f"<td><img src='{html.escape(str(orig), quote=True)}' loading='lazy'></td>"
                if orig else "<td>—</td>")
        for key in variants:
            cell = info["cells"].get(key)
            if not cell:
                parts.append("<td>—</td>")
                continue
            color = _BADGE_COLORS.get(cell["status"], "#777")
            badge = (f"<span class='badge' style='background:{color}'>"
                     f"{cell['status'].upper()}</span>")
            img = (f"<img src='{html.escape(str(cell['output']), quote=True)}' loading='lazy'>"
                   if cell.get("output") else "")
            parts.append(
                f"<td>{img}{badge}<br><small>{cell['latency_s']}с · "
                f"{cell['cost_rub']}₽</small><br>"
                f"<small>{html.escape(str(cell.get('error') or '')[:80])}</small></td>")
        parts.append("</tr>")
    parts.append("</table>")
    return "".join(parts)


def main():
    parser = argparse.ArgumentParser(description="Пилот генерации инфографики")
    parser.add_argument("--seller-id", type=int,
                        help="Обязателен, если товары читаются из БД")
    parser.add_argument("--products", help="Явные ID через запятую (иначе --limit выборка)")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--preset", default="boudoir",
                        choices=["boudoir", "neon", "luxury", "spa"])
    parser.add_argument("--variants", required=True,
                        help="provider:model:mode через запятую, напр. gen_api:flux-2:B")
    parser.add_argument("--budget-rub", type=float, default=500.0)
    parser.add_argument("--dry-run", action="store_true",
                        help="Только оценка стоимости, без API-вызовов")
    parser.add_argument("--export-gpu-bundle", action="store_true",
                        help="Собрать input-бандл для GPU-ветки")
    parser.add_argument("--extra-results",
                        help="results.jsonl GPU-прогона для объединённого отчёта")
    parser.add_argument("--products-json",
                        help="JSON-файл [{id,title,photo}] вместо чтения БД")
    args = parser.parse_args()

    variants = parse_variants(args.variants)

    if args.products_json:
        products = load_products_json(args.products_json)[: args.limit]
    else:
        if not args.seller_id:
            parser.error("--seller-id обязателен без --products-json")
        product_ids = None
        if args.products:
            product_ids = [int(x) for x in args.products.split(",") if x.strip()]

        from seller_platform import app  # noqa: WPS433 — паттерн one-off скриптов репо

        with app.app_context():
            try:
                products = select_products(args.seller_id, product_ids, args.limit)
            except Exception as e:
                print(f"❌ БД недоступна или не инициализирована: {str(e)[:200]}")
                print("   Подсказка: DATABASE_URL существующей базы или --products-json.")
                sys.exit(1)

    if not products:
        print("❌ Не найдено товаров с фото")
        sys.exit(1)

    estimate = estimate_cost_rub(variants, len(products))
    print(f"Товаров: {len(products)}, вариантов: {len(variants)}, "
          f"оценка: ~{estimate:.0f}₽ (бюджет {args.budget_rub:.0f}₽)")
    if args.dry_run:
        return

    run_dir = Path("data/infographic_pilot") / time.strftime("run_%Y%m%d_%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)

    if args.export_gpu_bundle:
        try:
            export_gpu_bundle(products, run_dir / "gpu_bundle")
        except Exception as e:
            print(f"⚠️ GPU-бандл не собрался ({str(e)[:150]}) — "
                  f"продолжаю API-прогон, бандл можно собрать позже")

    results = run_matrix(products, variants, args.preset, run_dir, args.budget_rub)
    report = build_report_html(results, extra_results_path=args.extra_results)
    (run_dir / "report.html").write_text(report, encoding="utf-8")
    print(f"✅ Готово: {run_dir}/report.html")


if __name__ == "__main__":
    main()
