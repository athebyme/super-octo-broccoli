# -*- coding: utf-8 -*-
"""
Helpers for building Wildberries Content API payloads.

WB moved the packed product weight from characteristics to
dimensions.weightBrutto. Keep that rule in one place so importer, update
pipeline and tests use the same contract.
"""
from __future__ import annotations

import copy
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)


PACKED_WEIGHT_CHARC_IDS = {88952}

DEFAULT_DIMENSIONS = {
    "length": 10,
    "width": 10,
    "height": 5,
    "weightBrutto": 0.1,
}


def _to_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def is_packed_weight_charc_id(value: Any) -> bool:
    value_int = _to_int(value)
    return value_int in PACKED_WEIGHT_CHARC_IDS


@dataclass
class CharacteristicExtraction:
    values: Dict[str, Any] = field(default_factory=dict)
    wb_characteristics: List[Dict[str, Any]] = field(default_factory=list)
    dimensions: Dict[str, Any] = field(default_factory=dict)
    dropped: List[str] = field(default_factory=list)


def _first_value(value: Any) -> Any:
    if isinstance(value, (list, tuple)):
        for item in value:
            if item not in (None, "", [], {}):
                return item
        return None
    return value


def _to_number(value: Any) -> Optional[float]:
    value = _first_value(value)
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", ".")
    match = re.search(r"([-+]?\d+(?:\.\d+)?)", text)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def _norm_name(name: Any) -> str:
    return re.sub(r"\s+", " ", str(name or "").strip().lower())


def is_packed_weight_name(name: Any) -> bool:
    normalized = _norm_name(name)
    if not normalized or "без упаков" in normalized:
        return False
    return (
        "weightbrutto" in normalized
        or "вес брутто" in normalized
        or "вес с упаков" in normalized
        or "вес товара с упаков" in normalized
        or "вес упаков" in normalized
        or "gross weight" in normalized
        or "packed weight" in normalized
        or "package weight" in normalized
    )


def _dimension_key_from_name(name: Any) -> Optional[str]:
    normalized = _norm_name(name)
    if not normalized:
        return None
    if "упаков" not in normalized and "packed" not in normalized and "package" not in normalized:
        return None
    if any(token in normalized for token in ("длина", "length")):
        return "length"
    if any(token in normalized for token in ("ширина", "width")):
        return "width"
    if any(token in normalized for token in ("высота", "height")):
        return "height"
    if is_packed_weight_name(normalized):
        return "weightBrutto"
    return None


def _dimension_key_from_mapping_key(key: Any) -> Optional[str]:
    normalized = _norm_name(key).replace(" ", "_")
    direct = {
        "length": "length",
        "length_cm": "length",
        "length_mm": "length",
        "package_length": "length",
        "package_length_cm": "length",
        "package_length_mm": "length",
        "packed_length": "length",
        "packed_length_mm": "length",
        "length_packed": "length",
        "width": "width",
        "width_cm": "width",
        "width_mm": "width",
        "package_width": "width",
        "package_width_cm": "width",
        "package_width_mm": "width",
        "packed_width": "width",
        "packed_width_mm": "width",
        "width_packed": "width",
        "height": "height",
        "height_cm": "height",
        "height_mm": "height",
        "package_height": "height",
        "package_height_cm": "height",
        "package_height_mm": "height",
        "packed_height": "height",
        "packed_height_mm": "height",
        "height_packed": "height",
        "weightbrutto": "weightBrutto",
        "weight_brutto": "weightBrutto",
        "package_weight": "weightBrutto",
        "package_weight_kg": "weightBrutto",
        "package_weight_g": "weightBrutto",
        "packed_weight": "weightBrutto",
        "packed_weight_kg": "weightBrutto",
        "packed_weight_g": "weightBrutto",
        "weight_packed": "weightBrutto",
    }
    if normalized in direct:
        return direct[normalized]
    return _dimension_key_from_name(key)


def _is_gram_source(name_or_key: Any, raw_value: Any) -> bool:
    normalized = _norm_name(name_or_key).replace("_", " ")
    if re.search(r"(^|[^а-яa-z])г([^а-яa-z]|$)", normalized) or normalized.endswith(" g"):
        return True
    if "gram" in normalized or normalized.endswith("_g"):
        return True
    text = str(_first_value(raw_value) or "").lower()
    if re.search(r"\d\s*(г|гр|g)\b", text):
        return True
    return False


def _is_millimeter_source(name_or_key: Any, raw_value: Any) -> bool:
    normalized = _norm_name(name_or_key).replace("_", " ")
    if "мм" in normalized or "millimeter" in normalized or normalized.endswith(" mm"):
        return True
    text = str(_first_value(raw_value) or "").lower()
    return bool(re.search(r"\d\s*(мм|mm)\b", text))


def _coerce_dimension_value(key: str, value: Any, source_name: Any = "") -> Optional[float]:
    number = _to_number(value)
    if number is None or number <= 0:
        return None

    if key == "weightBrutto":
        if _is_gram_source(source_name, value) or number > 30:
            number = number / 1000
        return round(number, 3)

    if _is_millimeter_source(source_name, value):
        number = number / 10
    return int(round(number))


def extract_characteristics(raw: Any) -> CharacteristicExtraction:
    """
    Split incoming characteristics into mappable values, raw WB [{id,value}]
    entries and dimensions extracted from legacy packed-weight fields.
    """
    result = CharacteristicExtraction()
    if not raw:
        return result

    if isinstance(raw, Mapping):
        iterable: Iterable[Tuple[Any, Any]] = raw.items()
        for name, value in iterable:
            if str(name).startswith("_"):
                continue
            dim_key = _dimension_key_from_mapping_key(name)
            if dim_key:
                coerced = _coerce_dimension_value(dim_key, value, name)
                if coerced is not None:
                    result.dimensions[dim_key] = coerced
                    result.dropped.append(str(name))
                    continue
            if is_packed_weight_name(name):
                coerced = _coerce_dimension_value("weightBrutto", value, name)
                if coerced is not None:
                    result.dimensions["weightBrutto"] = coerced
                result.dropped.append(str(name))
                continue
            result.values[str(name)] = value
        return result

    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, Mapping):
                continue
            char_id = item.get("id") or item.get("charcID")
            value = item.get("value")
            name = item.get("name") or item.get("charcName") or item.get("characteristic")

            if is_packed_weight_charc_id(char_id):
                coerced = _coerce_dimension_value("weightBrutto", value, name or "weightBrutto")
                if coerced is not None:
                    result.dimensions["weightBrutto"] = coerced
                result.dropped.append(str(char_id))
                continue

            if name:
                dim_key = _dimension_key_from_name(name)
                if dim_key:
                    coerced = _coerce_dimension_value(dim_key, value, name)
                    if coerced is not None:
                        result.dimensions[dim_key] = coerced
                        result.dropped.append(str(name))
                        continue
                if is_packed_weight_name(name):
                    coerced = _coerce_dimension_value("weightBrutto", value, name)
                    if coerced is not None:
                        result.dimensions["weightBrutto"] = coerced
                    result.dropped.append(str(name))
                    continue
                result.values[str(name)] = value
                continue

            if char_id and "value" in item:
                result.wb_characteristics.append({"id": char_id, "value": value})

    return result


def extract_dimensions(raw: Any) -> Dict[str, Any]:
    """Extract WB dimensions from dict/list legacy sources."""
    dims: Dict[str, Any] = {}
    if not raw:
        return dims

    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, Mapping):
                nested = extract_dimensions(item)
                if nested:
                    dims.update(nested)
        if dims:
            return dims
        return extract_characteristics(raw).dimensions

    if not isinstance(raw, Mapping):
        return dims

    for key, value in raw.items():
        if isinstance(value, Mapping) and key in ("package", "package_dimensions", "packed"):
            dims.update(extract_dimensions(value))
            continue
        dim_key = _dimension_key_from_mapping_key(key)
        if not dim_key:
            continue
        coerced = _coerce_dimension_value(dim_key, value, key)
        if coerced is not None:
            dims[dim_key] = coerced
    return dims


def normalize_dimensions(raw: Any = None, defaults: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """Return dimensions in the official WB object shape."""
    result: Dict[str, Any] = {}
    source = defaults if isinstance(defaults, Mapping) else DEFAULT_DIMENSIONS
    for key in ("length", "width", "height", "weightBrutto"):
        coerced = _coerce_dimension_value(key, source.get(key), key)
        if coerced is not None:
            result[key] = coerced

    extracted = extract_dimensions(raw)
    for key, value in extracted.items():
        coerced = _coerce_dimension_value(key, value, key)
        if coerced is not None:
            result[key] = coerced

    for key, fallback in DEFAULT_DIMENSIONS.items():
        if key not in result:
            result[key] = fallback

    return result


def build_dimensions(defaults: Optional[Mapping[str, Any]] = None, *sources: Any) -> Dict[str, Any]:
    dimensions = normalize_dimensions(defaults=defaults if isinstance(defaults, Mapping) else None)
    if defaults is not None and not isinstance(defaults, Mapping):
        sources = (defaults, *sources)
    for source in sources:
        extracted = extract_dimensions(source)
        for key, value in extracted.items():
            coerced = _coerce_dimension_value(key, value, key)
            if coerced is not None:
                dimensions[key] = coerced
    return normalize_dimensions(dimensions)


def sanitize_wb_characteristics(characteristics: Any) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Remove deprecated packed-weight characteristics from already mapped
    WB characteristics and return dimensions extracted from them.
    """
    cleaned: List[Dict[str, Any]] = []
    extracted_dimensions: Dict[str, Any] = {}
    if not characteristics:
        return cleaned, extracted_dimensions

    iterable = characteristics if isinstance(characteristics, list) else []
    for char in iterable:
        if not isinstance(char, Mapping):
            continue
        char_id = char.get("id") or char.get("charcID")
        name = char.get("name") or char.get("charcName")
        value = char.get("value")
        if is_packed_weight_charc_id(char_id) or is_packed_weight_name(name):
            coerced = _coerce_dimension_value("weightBrutto", value, name or "weightBrutto")
            if coerced is not None:
                extracted_dimensions["weightBrutto"] = coerced
            logger.info("Dropped deprecated WB packed-weight characteristic id=%s name=%s", char_id, name)
            continue
        cleaned.append({"id": char_id, "value": value})

    return cleaned, extracted_dimensions


def normalize_create_cards_payload(cards: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Deep-copy and normalize a /content/v2/cards/upload payload."""
    normalized = copy.deepcopy(cards)
    for card in normalized:
        for variant in card.get("variants") or []:
            chars, dims_from_chars = sanitize_wb_characteristics(variant.get("characteristics", []))
            variant["characteristics"] = chars
            variant["dimensions"] = build_dimensions(
                variant.get("dimensions") or DEFAULT_DIMENSIONS,
                dims_from_chars,
            )
    return normalized


def normalize_update_card_payload(card: Dict[str, Any]) -> Dict[str, Any]:
    """Deep-copy and normalize one /content/v2/cards/update card."""
    normalized = copy.deepcopy(card)
    chars, dims_from_chars = sanitize_wb_characteristics(normalized.get("characteristics", []))
    if "characteristics" in normalized:
        normalized["characteristics"] = chars
    normalized["dimensions"] = build_dimensions(normalized.get("dimensions") or DEFAULT_DIMENSIONS, dims_from_chars)
    return normalized
