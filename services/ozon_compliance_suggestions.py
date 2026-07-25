"""Explainable, read-only compliance suggestions for Ozon drafts.

Suggestions are deliberately not draft values.  They rank only values from a
fresh exact product-type dictionary and require a seller click in the repair
editor before the ordinary strict draft validator can accept them.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional
import unicodedata

from models import (
    MarketplaceAttributeDefinition,
    MarketplaceAttributeValue,
    MarketplaceProductDraft,
)
from services.ozon_reference_service import OzonReferenceService


class OzonComplianceSuggestionService:
    TNVED_ATTRIBUTE_ID = "22232"
    MAX_DICTIONARY_VALUES = 5_000
    MAX_SUGGESTIONS = 3
    _CODE = re.compile(r"^\s*([0-9]{10})\b")
    _WORDS = re.compile(r"[0-9a-zа-яё]+")
    _STOP_WORDS = frozenset({
        "без",
        "более",
        "включая",
        "для",
        "другие",
        "других",
        "из",
        "или",
        "кроме",
        "менее",
        "на",
        "не",
        "от",
        "по",
        "прочая",
        "прочее",
        "прочие",
        "прочих",
        "с",
        "со",
        "товар",
        "товара",
        "товаров",
    })

    @staticmethod
    def _object(raw: Any) -> dict:
        if isinstance(raw, dict):
            return raw
        if not isinstance(raw, str) or not raw:
            return {}
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _normalize(value: Any) -> str:
        if not isinstance(value, str):
            return ""
        return " ".join(
            unicodedata.normalize("NFKC", value).casefold().split()
        )

    @classmethod
    def _tokens(cls, value: Any) -> set[str]:
        return {
            token
            for token in cls._WORDS.findall(cls._normalize(value))
            if len(token) >= 4 and token not in cls._STOP_WORDS
        }

    @classmethod
    def _observed_signals(cls, draft: MarketplaceProductDraft) -> dict:
        document = cls._object(draft.source_facts_json)
        facts = document.get("facts")
        if not isinstance(facts, dict):
            facts = {}
        identity = facts.get("identity")
        if not isinstance(identity, dict):
            identity = {}
        attributes = facts.get("attributes")
        if not isinstance(attributes, dict):
            attributes = {}

        source_title = identity.get("source_title")
        source_categories = identity.get("source_categories")
        if not isinstance(source_categories, list):
            source_categories = []
        category_values = [
            value
            for value in [
                identity.get("source_category"),
                *source_categories,
            ]
            if isinstance(value, str) and value.strip()
        ]
        categories = " | ".join(category_values)
        materials = attributes.get("materials")
        if not isinstance(materials, list):
            materials = [materials] if isinstance(materials, str) else []

        official_type = (
            draft.product_type.name
            if draft.product_type is not None
            else ""
        )
        official_path = (
            draft.product_type.category.full_path
            if (
                draft.product_type is not None
                and draft.product_type.category is not None
            )
            else ""
        )
        title_signal = cls._normalize(source_title)
        category_signal = cls._normalize(categories)
        material_signal = cls._normalize(" | ".join(materials))
        official_signal = cls._normalize(
            f"{official_type} | {official_path}"
        )
        negative_vibration = "без вибрации" in category_signal
        positive_vibration = bool(
            "с вибрацией" in category_signal
            or (
                "вибрац" in title_signal
                and not negative_vibration
            )
        )
        adult_attachment = bool(
            (
                "эротич" in official_signal
                and "насад" in official_signal
            )
            or "насадки и кольца" in category_signal
        )
        rubber_like = bool(
            re.search(
                r"\b(?:tpr|tpe|резин\w*|эластомер\w*|"
                r"термоэластопласт\w*)\b",
                material_signal,
            )
        )
        plastic_like = bool(
            re.search(
                r"\b(?:пластик\w*|пластмасс\w*|полимер\w*|"
                r"термопласт\w*|tpr|tpe)\b",
                material_signal,
            )
        )
        lexical = " | ".join(
            value
            for value in (
                official_type,
                official_path,
                categories,
                source_title if isinstance(source_title, str) else "",
                " ".join(materials),
            )
            if value
        )
        return {
            "adult_attachment": adult_attachment,
            "positive_vibration": positive_vibration,
            "negative_vibration": negative_vibration,
            "rubber_like": rubber_like,
            "plastic_like": plastic_like,
            "material_label": ", ".join(materials[:3]),
            "lexical_tokens": cls._tokens(lexical),
        }

    @classmethod
    def _score_value(cls, value: str, signals: dict) -> Optional[dict]:
        if not isinstance(value, str):
            return None
        normalized = cls._normalize(value)
        code_match = cls._CODE.match(value)
        if code_match is None:
            return None
        code = code_match.group(1)
        score = 0
        reasons = []

        if signals["adult_attachment"]:
            if (
                signals["positive_vibration"]
                and "электрические вибромассажные" in normalized
            ):
                score += 130
                reasons.append(
                    "в источнике явно указана вибрация, а описание кода "
                    "относится к электрическим вибромассажным аппаратам"
                )
            elif (
                signals["positive_vibration"]
                and "аппараты массажные" in normalized
            ):
                score += 65
                reasons.append(
                    "в источнике указана вибрация, а описание кода "
                    "относится к массажной аппаратуре"
                )
            if (
                signals["negative_vibration"]
                and (
                    "вибромассаж" in normalized
                    or "аппараты массажные" in normalized
                )
            ):
                score -= 200
            if (
                signals["rubber_like"]
                and "вулканизованной резины" in normalized
                and "для гражданских воздушных судов" not in normalized
            ):
                score += 55
                reasons.append(
                    "наблюдённый материал относится к TPR/TPE/резине; "
                    "нужно подтвердить точный состав и технологию материала"
                )
            if (
                signals["plastic_like"]
                and "из пластмасс" in normalized
                and "для гражданских воздушных судов" not in normalized
                and "контактных линз" not in normalized
                and "транспортных средств" not in normalized
            ):
                score += 45
                reasons.append(
                    "наблюдённый материал может относиться к "
                    "термопластам; нужно подтвердить точный состав"
                )
        else:
            overlap = (
                signals["lexical_tokens"]
                & cls._tokens(value)
            )
            if len(overlap) >= 2:
                score += len(overlap) * 12
                reasons.append(
                    "совпали значимые слова официального типа, категории "
                    "и описания кода"
                )

        if score < 25 or not reasons:
            return None
        return {
            "code": code,
            "value": value,
            "score": score,
            "confidence": "medium" if score >= 90 else "low",
            "confidence_label": (
                "средняя уверенность"
                if score >= 90
                else "низкая уверенность"
            ),
            "reason": "; ".join(dict.fromkeys(reasons))[:500],
            "marking_explicit": (
                True if "маркировка рф" in normalized else None
            ),
        }

    @classmethod
    def tnved_suggestions(
        cls,
        *,
        draft: MarketplaceProductDraft,
        definition: MarketplaceAttributeDefinition,
    ) -> list[dict]:
        """Rank bounded official values without persisting or confirming one."""
        if (
            not isinstance(draft, MarketplaceProductDraft)
            or not isinstance(definition, MarketplaceAttributeDefinition)
            or definition.product_type_id != draft.product_type_id
            or definition.external_attribute_id != cls.TNVED_ATTRIBUTE_ID
            or not definition.dictionary_id
            or not definition.is_available
            or not definition.is_enabled
            or not OzonReferenceService.dictionary_is_fresh(definition)
        ):
            return []

        values = MarketplaceAttributeValue.query.filter_by(
            attribute_id=definition.id,
            is_available=True,
        )
        restriction = set(definition.restriction_value_ids)
        if restriction:
            values = values.filter(
                MarketplaceAttributeValue.external_value_id.in_(
                    restriction
                )
            )
        rows = values.order_by(
            MarketplaceAttributeValue.id.asc(),
        ).limit(cls.MAX_DICTIONARY_VALUES + 1).all()
        if len(rows) > cls.MAX_DICTIONARY_VALUES:
            return []

        signals = cls._observed_signals(draft)
        ranked = []
        for row in rows:
            suggestion = cls._score_value(row.value, signals)
            if suggestion is None:
                continue
            suggestion["dictionary_value_id"] = row.external_value_id
            ranked.append(suggestion)
        ranked.sort(
            key=lambda item: (
                -item["score"],
                item["code"],
                item["dictionary_value_id"],
            )
        )

        result = []
        seen_values = set()
        for item in ranked:
            identity = (
                item["code"],
                bool(item["marking_explicit"]),
            )
            if identity in seen_values:
                continue
            seen_values.add(identity)
            result.append({
                key: value
                for key, value in item.items()
                if key != "score"
            })
            if len(result) >= cls.MAX_SUGGESTIONS:
                break
        return result
