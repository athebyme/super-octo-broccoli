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


def build_fact_pack(
    *,
    title: str,
    category: str = "",
    brand: str = "",
    characteristics: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Create a bounded, deterministic pack from stored product fields."""
    facts: List[Dict[str, Any]] = []

    def add(kind: str, label: str, value: Any, source: str) -> None:
        clean_label = _clean_text(label, 80)
        clean_value = _clean_text(value)
        if not clean_label or not clean_value or len(facts) >= MAX_FACTS:
            return
        facts.append({
            "id": _fact_id(kind, clean_label.casefold()),
            "kind": kind,
            "label": clean_label,
            "value": clean_value,
            "source": source,
            "render_policy": "verbatim",
            "verified_claim": False,
        })

    add("title", "Название", title, "supplier_product.title")
    add("brand", "Бренд", brand, "supplier_product.brand")
    add("category", "Категория", category, "supplier_product.category")
    for key in sorted((characteristics or {}), key=lambda item: str(item).casefold()):
        if str(key).startswith("_"):
            continue
        add("characteristic", str(key), (characteristics or {})[key],
            f"supplier_product.characteristics.{key}")
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
    secondary = (by_kind.get("brand") or by_kind.get("category") or [None])[0]
    hero = {
        "number": 1,
        "type": "hero",
        "title": title_fact["value"],
        "subtitle": secondary["value"] if secondary else "",
        "bullets": [],
        "text_sources": {
            "title": _reference(title_fact, "value"),
            "subtitle": _reference(secondary, "value") if secondary else None,
            "bullets": [],
        },
        "claim_ids": [title_fact["id"]] + ([secondary["id"]] if secondary else []),
        "image_concept": {"scene_key": "luxury"},
    }
    slides = [hero]

    for fact in by_kind.get("characteristic", []):
        if len(slides) >= max(1, min(max_slides, 10)):
            break
        slides.append({
            "number": len(slides) + 1,
            "type": "characteristics",
            "title": fact["label"],
            "subtitle": fact["value"],
            "bullets": [],
            "text_sources": {
                "title": _reference(fact, "label"),
                "subtitle": _reference(fact, "value"),
                "bullets": [],
            },
            "claim_ids": [fact["id"]],
            "image_concept": {"scene_key": "spa"},
        })

    result = {
        "policy": "fact_safe_v1",
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
        for field in ("title", "subtitle"):
            visible = slide.get(field) or ""
            reference = sources.get(field)
            if not visible and reference is None:
                continue
            expected = _resolve_reference(reference, facts)
            if not isinstance(visible, str) or visible != expected:
                errors.append(f"slide {index}: {field} не совпадает с фактом")
            if isinstance(visible, str) and len(visible) > (120 if field == "title" else 100):
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
        field_refs = [sources.get("title"), sources.get("subtitle"), *bullet_sources]
        for text, reference in zip(
            [slide.get("title", ""), slide.get("subtitle", ""), *bullets],
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
        for value in (slide.get("title"), slide.get("subtitle")):
            if isinstance(value, str) and value:
                values.append(value)
        values.extend(
            value for value in (slide.get("bullets") or [])
            if isinstance(value, str) and value
        )
    return values
