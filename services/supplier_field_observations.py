"""Marketplace-neutral helpers for parser-owned supplier field markers.

Only explicitly parser-stamped colors and country values are authoritative
observations here. Channel-specific source/link checks remain with callers.
"""

from __future__ import annotations

import json
from typing import Any, Mapping


SUPPLIER_SOURCE_KEYS = {"colors", "country"}


def load_source_snapshot(raw: Any) -> dict[str, Any] | None:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw:
        return None
    if len(raw.encode("utf-8", errors="ignore")) > 256 * 1024:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def observed_supplier_fields(
    snapshot: Mapping[str, Any],
) -> tuple[bool, bool, dict[str, Any]]:
    present = (
        "observed_supplier_fields_version" in snapshot
        or "observed_supplier_fields" in snapshot
    )
    if not present:
        return False, True, {}
    if (
        type(snapshot.get("observed_supplier_fields_version")) is not int
        or snapshot.get("observed_supplier_fields_version") != 1
    ):
        return True, False, {}
    raw_fields = snapshot.get("observed_supplier_fields")
    if not isinstance(raw_fields, dict) or set(raw_fields).difference(SUPPLIER_SOURCE_KEYS):
        return True, False, {}
    result = {}
    for field_name, value in raw_fields.items():
        if field_name == "colors":
            if (
                not isinstance(value, list) or not value or len(value) > 20
                or any(not isinstance(item, str) or not item.strip() or len(item) > 200
                       for item in value)
            ):
                return True, False, {}
            normalized = [item.strip() for item in value]
            if len({item.casefold() for item in normalized}) != len(normalized):
                return True, False, {}
            result[field_name] = normalized
        elif field_name == "country":
            if not isinstance(value, str) or not value.strip() or len(value) > 200:
                return True, False, {}
            result[field_name] = value.strip()
    return True, True, result


def source_values(value: Any, field_name: str) -> list[str] | None:
    if field_name == "colors":
        values = value if isinstance(value, list) else None
    else:
        values = [value] if isinstance(value, str) else None
    if not values:
        return None
    result = []
    seen = set()
    for raw in values:
        if not isinstance(raw, str):
            return None
        item = raw.strip()
        if not item or len(item) > 200 or item.casefold() in seen:
            return None
        seen.add(item.casefold())
        result.append(item)
    return result


def normalized_snapshot_values(
    value: Any,
    field_name: str,
) -> list[str] | None:
    values = source_values(value, field_name)
    if values is None:
        return None
    try:
        from services.data_normalizer import DataNormalizer
        if field_name == "colors":
            values = DataNormalizer.normalize_colors(values)
        else:
            values = [DataNormalizer.clean_string(item) for item in values]
    except Exception:
        return None
    return values if values and all(isinstance(item, str) and item for item in values) else None
