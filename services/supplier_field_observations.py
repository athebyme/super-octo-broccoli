"""Marketplace-neutral helpers for parser-owned supplier field markers.

Only explicitly parser-stamped colors and country values are authoritative
observations here. Channel-specific source/link checks remain with callers.
"""

from __future__ import annotations

import json
from typing import Any, Mapping, TYPE_CHECKING

if TYPE_CHECKING:
    from models import ImportedProduct, SupplierProduct


SUPPLIER_SOURCE_KEYS = {"colors", "country"}


def supplier_copy_identity_rejection(
    imported: ImportedProduct,
    supplier_product: SupplierProduct | None,
) -> str | None:
    """Require an exact, current seller copy of one supplier product.

    The result codes match the source-link contract used by marketplace
    evidence. A missing or unversioned copy is unknown, even when its foreign
    key points at a supplier row; callers must not hydrate it from newer data.
    This helper only reads the supplied objects and never changes them.
    """
    if supplier_product is None or supplier_product.supplier is None:
        return "source_link_missing"
    supplier = supplier_product.supplier

    if (
        imported.supplier_product_id is None
        or imported.supplier_product_id != supplier_product.id
        or imported.supplier_id is None
        or imported.supplier_id != supplier_product.supplier_id
        or supplier.id != supplier_product.supplier_id
        or not (supplier.code and supplier.code.strip())
        or imported.source_type != supplier.code
    ):
        return "source_link_invalid"

    imported_sku = imported.external_id
    source_sku = supplier_product.external_id
    if not (
        isinstance(imported_sku, str)
        and imported_sku.strip()
        and isinstance(source_sku, str)
        and source_sku.strip()
    ):
        return "source_sku_unavailable"
    if imported_sku != source_sku:
        return "source_sku_mismatch"

    copied_revision = imported.supplier_content_revision
    current_revision = supplier_product.content_revision
    if (
        type(copied_revision) is not int
        or copied_revision <= 0
        or type(current_revision) is not int
        or current_revision <= 0
        or copied_revision != current_revision
    ):
        return "source_revision_stale"
    return None


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


def same_color_literals(left: Any, right: Any) -> bool:
    """Compare valid typed color lists without deriving or translating hues."""
    left_values = source_values(left, "colors")
    right_values = source_values(right, "colors")
    return bool(
        left_values and right_values
        and {value.casefold() for value in left_values}
        == {value.casefold() for value in right_values}
    )


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
