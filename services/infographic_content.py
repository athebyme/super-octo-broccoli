# -*- coding: utf-8 -*-
"""Fact-bound copy for product infographics.

Production slides are extractive: every visible product statement points to a
stored fact and is rendered verbatim.  The image model never writes product
copy and a language model is not allowed to invent missing slides.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Dict, List, Mapping, Optional, Tuple

MAX_FACTS = 24
MAX_FACT_VALUE_CHARS = 240

_UNVERIFIED_PROMO_RE = re.compile(
    r"\b(хит(?:\s+продаж)?|топ|бестселлер|лучш(?:ий|ая|ее)|"
    r"премиум\s+качество|новинка(?:\s+сезона)?)\b",
    re.IGNORECASE,
)


def _clean_text(value: Any, max_chars: int = MAX_FACT_VALUE_CHARS) -> str:
    if value is None or isinstance(value, (bool, dict)):
        return ""
    if isinstance(value, (list, tuple)):
        items = [_clean_text(item, 80) for item in value]
        text = ", ".join(item for item in items if item)
    else:
        text = str(value)
    text = " ".join(text.split()).strip()
    return text[:max_chars]


def _fact_id(kind: str, label: str) -> str:
    suffix = hashlib.sha256(f"{kind}\0{label}".encode("utf-8")).hexdigest()[:12]
    return f"{kind}_{suffix}"


def _semantic_text(value: Any) -> str:
    clean = _clean_text(value, MAX_FACT_VALUE_CHARS).casefold().replace("ё", "е")
    return re.sub(r"[^a-zа-я0-9]+", " ", clean).strip()


def _semantic_label(kind: str, label: str) -> str:
    if kind in {"title", "brand", "category"}:
        return kind
    normalized = _semantic_text(label)
    aliases = {
        "brand": {"бренд", "марка", "производитель"},
        "category": {"категория", "категория товара"},
        "material": {
            "материал", "материал изделия", "состав",
            "состав материала", "основной материал",
        },
        "country": {"страна", "страна производства"},
        "gender": {"пол", "пол товара", "целевой пол"},
        "color": {"цвет", "цвет товара", "основной цвет"},
    }
    for semantic, names in aliases.items():
        if normalized in names:
            return semantic
    return f"characteristic:{normalized}"


def build_fact_pack(
    *,
    title: str,
    category: str = "",
    brand: str = "",
    characteristics: Optional[Mapping[str, Any]] = None,
    source_prefix: str = "supplier_product",
) -> Dict[str, Any]:
    """Create a bounded, deterministic pack from stored product fields."""
    facts: List[Dict[str, Any]] = []
    seen_semantic_values = set()
    clean_source_prefix = (
        source_prefix
        if source_prefix in {"supplier_product", "imported_product"}
        else "supplier_product"
    )

    def add(kind: str, label: str, value: Any, source: str) -> None:
        clean_label = _clean_text(label, 80)
        clean_value = _clean_text(value)
        if not clean_label or not clean_value or len(facts) >= MAX_FACTS:
            return
        signature = (
            _semantic_label(kind, clean_label),
            _semantic_text(clean_value),
        )
        if signature in seen_semantic_values:
            return
        seen_semantic_values.add(signature)
        facts.append({
            "id": _fact_id(kind, clean_label.casefold()),
            "kind": kind,
            "label": clean_label,
            "value": clean_value,
            "source": source,
            "render_policy": "verbatim",
            "verified_claim": False,
        })

    add("title", "Название", title, f"{clean_source_prefix}.title")
    add("brand", "Бренд", brand, f"{clean_source_prefix}.brand")
    add("category", "Категория", category, f"{clean_source_prefix}.category")
    for key in sorted((characteristics or {}), key=lambda item: str(item).casefold()):
        if str(key).startswith("_"):
            continue
        add("characteristic", str(key), (characteristics or {})[key],
            f"{clean_source_prefix}.characteristics.{key}")
    return {"version": 1, "policy": "verbatim_only", "facts": facts}


def _reference(fact: Dict[str, Any], part: str) -> Dict[str, str]:
    return {"fact_id": fact["id"], "part": part}


def build_fact_safe_rich_content(
    fact_pack: Mapping[str, Any],
    *,
    max_slides: int = 6,
) -> Dict[str, Any]:
    """Build useful slides without an LLM and without unsupported claims."""
    facts = [fact for fact in fact_pack.get("facts", []) if isinstance(fact, dict)]
    by_kind: Dict[str, List[Dict[str, Any]]] = {}
    for fact in facts:
        by_kind.setdefault(str(fact.get("kind")), []).append(fact)
    titles = by_kind.get("title") or []
    if not titles:
        raise ValueError("Для инфографики требуется название товара")

    title_fact = titles[0]
    brand_fact = (by_kind.get("brand") or [None])[0]
    category_fact = (by_kind.get("category") or [None])[0]
    hero = {
        "number": 1,
        "type": "hero",
        "eyebrow": category_fact["value"] if category_fact else "",
        "title": title_fact["value"],
        "subtitle": brand_fact["value"] if brand_fact else "",
        "bullets": [],
        "facts": [],
        "text_sources": {
            "eyebrow": _reference(category_fact, "value") if category_fact else None,
            "title": _reference(title_fact, "value"),
            "subtitle": _reference(brand_fact, "value") if brand_fact else None,
            "bullets": [],
        },
        "claim_ids": [title_fact["id"]]
        + ([brand_fact["id"]] if brand_fact else [])
        + ([category_fact["id"]] if category_fact else []),
        "image_concept": {"scene_key": "luxury"},
    }
    slides = [hero]

    slide_cap = max(1, min(max_slides, 10))
    groups: List[List[Dict[str, Any]]] = []
    current: List[Dict[str, Any]] = []
    current_cost = 0
    for fact in by_kind.get("characteristic", []):
        cost = len(str(fact.get("label") or "")) + len(str(fact.get("value") or ""))
        if cost > 180:
            if current:
                groups.append(current)
                current, current_cost = [], 0
            groups.append([fact])
            continue
        if current and (len(current) >= 4 or current_cost + cost > 300):
            groups.append(current)
            current, current_cost = [], 0
        current.append(fact)
        current_cost += cost
    if current:
        groups.append(current)

    for group in groups[:max(0, slide_cap - 1)]:
        cards = [
            {
                "label": fact["label"],
                "value": fact["value"],
                "text_sources": {
                    "label": _reference(fact, "label"),
                    "value": _reference(fact, "value"),
                },
                "claim_id": fact["id"],
            }
            for fact in group
        ]
        slides.append({
            "number": len(slides) + 1,
            "type": "fact_grid",
            "eyebrow": "",
            "title": "",
            "subtitle": "",
            "bullets": [],
            "facts": cards,
            "layout": "focus" if len(cards) == 1 else "grid",
            "text_sources": {
                "eyebrow": None,
                "title": None,
                "subtitle": None,
                "bullets": [],
            },
            "claim_ids": [fact["id"] for fact in group],
            "image_concept": {"scene_key": "spa"},
        })

    result = {
        "policy": "fact_safe_v2",
        "slides": slides,
        "total_slides": len(slides),
        "fact_pack": dict(fact_pack),
        "design_recommendations": {
            "color_palette": ["#232323", "#c79a55", "#f4efe7", "#ffffff"],
            "font_style": "modern",
            "overall_mood": "catalog",
        },
    }
    ok, errors = validate_fact_safe_rich_content(result)
    if not ok:
        raise ValueError("; ".join(errors))
    return result


def _resolve_reference(reference: Any, facts: Mapping[str, Dict[str, Any]]) -> Optional[str]:
    if not isinstance(reference, dict):
        return None
    fact = facts.get(reference.get("fact_id"))
    part = reference.get("part")
    if not fact or part not in ("label", "value"):
        return None
    return fact.get(part)


def validate_fact_safe_rich_content(content: Mapping[str, Any]) -> Tuple[bool, List[str]]:
    """Fail closed unless every visible product phrase is fact-bound verbatim."""
    errors: List[str] = []
    fact_pack = content.get("fact_pack") if isinstance(content, Mapping) else None
    fact_list = fact_pack.get("facts", []) if isinstance(fact_pack, Mapping) else []
    facts = {
        fact.get("id"): fact for fact in fact_list
        if isinstance(fact, dict) and isinstance(fact.get("id"), str)
    }
    slides = content.get("slides", []) if isinstance(content, Mapping) else []
    if not facts:
        errors.append("fact_pack пуст")
    if not isinstance(slides, list) or not slides:
        errors.append("slides пуст")
        return False, errors
    if len(slides) > 10:
        errors.append("слишком много слайдов")

    for index, slide in enumerate(slides, start=1):
        if not isinstance(slide, dict):
            errors.append(f"slide {index}: ожидается object")
            continue
        sources = slide.get("text_sources")
        if not isinstance(sources, dict):
            errors.append(f"slide {index}: нет text_sources")
            continue
        for field in ("eyebrow", "title", "subtitle"):
            visible = slide.get(field) or ""
            reference = sources.get(field)
            if not visible and reference is None:
                continue
            expected = _resolve_reference(reference, facts)
            if not isinstance(visible, str) or visible != expected:
                errors.append(f"slide {index}: {field} не совпадает с фактом")
            limits = {"eyebrow": 100, "title": 160, "subtitle": 140}
            if isinstance(visible, str) and len(visible) > limits[field]:
                errors.append(f"slide {index}: {field} не помещается без обрезки")
        bullets = slide.get("bullets") or []
        bullet_sources = sources.get("bullets") or []
        if not isinstance(bullets, list) or not isinstance(bullet_sources, list):
            errors.append(f"slide {index}: bullets должны быть arrays")
            continue
        if bullets:
            errors.append(f"slide {index}: bullets пока запрещены safe-zone layout")
        if len(bullets) != len(bullet_sources):
            errors.append(f"slide {index}: bullets не совпадают с источниками")
            continue
        for bullet, reference in zip(bullets, bullet_sources):
            if bullet != _resolve_reference(reference, facts):
                errors.append(f"slide {index}: bullet не совпадает с фактом")
        cards = slide.get("facts") or []
        if not isinstance(cards, list):
            errors.append(f"slide {index}: facts должен быть array")
            cards = []
        if len(cards) > 4:
            errors.append(f"slide {index}: на слайде не более 4 fact cards")
        if slide.get("type") == "fact_grid" and not cards:
            errors.append(f"slide {index}: fact_grid пуст")
        card_cost = 0
        for card_index, card in enumerate(cards, start=1):
            if not isinstance(card, dict):
                errors.append(f"slide {index}: fact {card_index} ожидается object")
                continue
            card_sources = card.get("text_sources")
            if not isinstance(card_sources, dict):
                errors.append(f"slide {index}: fact {card_index} без text_sources")
                continue
            for field, limit in (("label", 80), ("value", 240)):
                visible = card.get(field)
                reference = card_sources.get(field)
                expected = _resolve_reference(reference, facts)
                if not isinstance(visible, str) or visible != expected:
                    errors.append(
                        f"slide {index}: fact {card_index} {field} не совпадает с фактом"
                    )
                elif len(visible) > limit:
                    errors.append(
                        f"slide {index}: fact {card_index} {field} не помещается"
                    )
                if isinstance(visible, str):
                    card_cost += len(visible)
                    if _UNVERIFIED_PROMO_RE.search(visible):
                        fact = facts.get(reference.get("fact_id")) if isinstance(reference, dict) else None
                        if not fact or fact.get("verified_claim") is not True:
                            errors.append(
                                f"slide {index}: fact {card_index} содержит неподтверждённый promo claim"
                            )
        if len(cards) > 1 and card_cost > 360:
            errors.append(f"slide {index}: fact cards переполняют layout")

        field_refs = [
            sources.get("eyebrow"), sources.get("title"),
            sources.get("subtitle"), *bullet_sources,
        ]
        for text, reference in zip(
            [
                slide.get("eyebrow", ""), slide.get("title", ""),
                slide.get("subtitle", ""), *bullets,
            ],
            field_refs,
        ):
            if isinstance(text, str) and _UNVERIFIED_PROMO_RE.search(text):
                fact = facts.get(reference.get("fact_id")) if isinstance(reference, dict) else None
                if not fact or fact.get("verified_claim") is not True:
                    errors.append(f"slide {index}: неподтверждённый promo claim")
    return not errors, errors


def visible_texts(content: Mapping[str, Any]) -> List[str]:
    values: List[str] = []
    for slide in content.get("slides", []) if isinstance(content, Mapping) else []:
        if not isinstance(slide, dict):
            continue
        for value in (slide.get("eyebrow"), slide.get("title"), slide.get("subtitle")):
            if isinstance(value, str) and value:
                values.append(value)
        values.extend(
            value for value in (slide.get("bullets") or [])
            if isinstance(value, str) and value
        )
        for card in slide.get("facts") or []:
            if not isinstance(card, dict):
                continue
            for value in (card.get("label"), card.get("value")):
                if isinstance(value, str) and value:
                    values.append(value)
    return values
