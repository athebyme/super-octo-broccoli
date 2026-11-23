"""Exact, read-only supplier facts for a signed WB characteristic review.

Only parser-stamped raw color and country values can be compared with WB global
directories. Other safe source snapshot values are presentation-only.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from sqlalchemy import func
from sqlalchemy.orm import joinedload

from models import ImportedProduct, Product, SupplierProduct, db
from services.supplier_field_observations import (
    load_source_snapshot as _load_source_snapshot,
    normalized_snapshot_values as _normalized_snapshot_values,
    observed_supplier_fields as _observed_supplier_fields,
    source_values as _source_values,
)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
        allow_nan=False,
    )


def _bounded_fact_values(value: Any, *, field_name: str) -> list[str]:
    if field_name == "sizes":
        if isinstance(value, dict):
            value = value.get("raw")
        if isinstance(value, str):
            value = [value]
        elif not isinstance(value, list):
            value = None
    elif field_name == "country":
        value = [value] if isinstance(value, str) else None
    elif not isinstance(value, list):
        value = None
    if not isinstance(value, list):
        return []
    return [
        item.strip()
        for item in value[:20]
        if isinstance(item, str) and item.strip() and len(item) <= 200
    ]


def _unknown_supplier_evidence(reason: str) -> dict[str, Any]:
    return {
        "status": "unknown",
        "reason": reason,
        "supplier_name": None,
        "source_checked_at": None,
        "facts": [],
    }


def build_supplier_evidence(
    products: list[Product],
    *,
    subject_id: int,
    schema: Mapping[str, Any],
    changes_by_product: Mapping[int, list[Mapping[str, Any]]],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], str]:
    """Read exact, revision-bound supplier facts with bounded queries."""
    product_ids = [int(product.id) for product in products]
    seller_ids = {int(product.seller_id) for product in products}
    evidence_by_product = {
        str(product_id): _unknown_supplier_evidence("supplier_link_missing")
        for product_id in product_ids
    }
    comparisons: dict[str, dict[str, Any]] = {}
    if not product_ids or len(seller_ids) != 1:
        return evidence_by_product, comparisons, hashlib.sha256(b"[]").hexdigest()

    seller_id = next(iter(seller_ids))
    imported_rows = (
        ImportedProduct.query
        .options(
            joinedload(ImportedProduct.supplier_product).joinedload(
                SupplierProduct.supplier,
            ),
            joinedload(ImportedProduct.product),
        )
        .filter(
            ImportedProduct.seller_id == seller_id,
            ImportedProduct.product_id.in_(product_ids),
        )
        .all()
    )
    imported_by_product: dict[int, list[ImportedProduct]] = {}
    for imported in imported_rows:
        if isinstance(imported.product_id, int) and not isinstance(imported.product_id, bool):
            imported_by_product.setdefault(imported.product_id, []).append(imported)

    nm_ids = {
        int(product.nm_id) for product in products
        if isinstance(product.nm_id, int)
        and not isinstance(product.nm_id, bool)
        and product.nm_id > 0
    }
    duplicate_nm_ids = set()
    if nm_ids:
        duplicate_nm_ids = {
            int(nm_id)
            for nm_id, _count in (
                db.session.query(Product.nm_id, func.count(Product.id))
                .filter(
                    Product.seller_id == seller_id,
                    Product.nm_id.in_(nm_ids),
                    Product.nm_id > 0,
                )
                .group_by(Product.nm_id)
                .having(func.count(Product.id) > 1)
                .all()
            )
        }

    schema_fields = [
        field for field in schema.get("characteristics", [])
        if isinstance(field, dict)
    ]
    fields_by_id = {
        int(field["id"]): field
        for field in schema_fields if type(field.get("id")) is int
    }
    directory_to_source = {"colors": "colors", "countries": "country"}
    fields_by_directory: dict[str, list[dict[str, Any]]] = {}
    for field in schema_fields:
        directory = field.get("dictionary_source")
        if directory in directory_to_source:
            fields_by_directory.setdefault(directory, []).append(field)
    allowed_by_characteristic: dict[int, dict[str, list[str]]] = {}
    all_changes = [
        change
        for product_changes in changes_by_product.values()
        for change in product_changes
    ]
    for char_id in {int(change.get("id")) for change in all_changes}:
        field = fields_by_id.get(char_id)
        if field is None:
            continue
        allowed_by_key: dict[str, list[str]] = {}
        for allowed in field.get("dictionary_values") or []:
            if isinstance(allowed, str) and allowed.strip():
                allowed_by_key.setdefault(
                    allowed.strip().casefold(), [],
                ).append(allowed.strip())
        allowed_by_characteristic[char_id] = allowed_by_key

    guard_rows = []
    for product in products:
        product_id = int(product.id)
        rows = imported_by_product.get(product_id) or []
        imported = rows[0] if len(rows) == 1 else None
        supplier_product = imported.supplier_product if imported else None
        supplier = supplier_product.supplier if supplier_product else None
        copied = _load_source_snapshot(imported.original_data) if imported else None
        current = None
        source_fields: dict[str, Any] = {}
        source_verified = False
        display_safe = False
        relation_valid = False
        reason = "supplier_link_missing"

        if len(rows) > 1:
            reason = "supplier_link_ambiguous"
        elif imported is not None:
            relation_valid = bool(
                imported.product_id == product.id
                and imported.seller_id == product.seller_id
                and imported.product is not None
                and imported.product.id == product.id
                and imported.product.seller_id == product.seller_id
                and supplier_product is not None
                and imported.supplier_product_id == supplier_product.id
                and imported.supplier_id == supplier_product.supplier_id
                and supplier is not None
                and supplier.id == supplier_product.supplier_id
                and imported.source_type == supplier.code
                and (
                    imported.wb_nm_id is None
                    or imported.wb_nm_id == product.nm_id
                )
                and not (
                    imported.external_id
                    and supplier_product.external_id
                    and imported.external_id != supplier_product.external_id
                )
            )
            if not relation_valid:
                reason = "supplier_link_invalid"
            elif product.nm_id in duplicate_nm_ids:
                reason = "wb_nmid_ambiguous"
            elif (
                type(product.subject_id) is not int
                or product.subject_id != subject_id
                or imported.wb_subject_id != subject_id
                or supplier_product.wb_subject_id != subject_id
            ):
                reason = "supplier_subject_mismatch"
            elif (
                type(imported.supplier_content_revision) is not int
                or imported.supplier_content_revision <= 0
                or imported.supplier_content_revision
                != int(supplier_product.content_revision or 0)
            ):
                reason = "supplier_revision_stale"
            else:
                display_safe = True
                try:
                    from services.supplier_service import _supplier_observed_snapshot
                    current = _load_source_snapshot(
                        _supplier_observed_snapshot(supplier_product),
                    )
                except Exception:
                    current = None
                if not copied or not current:
                    reason = "supplier_snapshot_missing"
                else:
                    current_present, current_valid, current_fields = (
                        _observed_supplier_fields(current)
                    )
                    copied_present, copied_valid, copied_fields = (
                        _observed_supplier_fields(copied)
                    )
                    if not current_present or not current_valid:
                        reason = "source_marker_unavailable"
                    elif copied_present and (
                        not copied_valid or copied_fields != current_fields
                    ):
                        reason = "source_marker_mismatch"
                    else:
                        copy_matches = True
                        for field_name, marker_value in current_fields.items():
                            normalized_marker = _normalized_snapshot_values(
                                marker_value, field_name,
                            )
                            normalized_copy = _normalized_snapshot_values(
                                copied.get(field_name), field_name,
                            )
                            normalized_current = _normalized_snapshot_values(
                                current.get(field_name), field_name,
                            )
                            if not (
                                normalized_marker is not None
                                and normalized_copy is not None
                                and normalized_current is not None
                                and {v.casefold() for v in normalized_marker}
                                == {v.casefold() for v in normalized_copy}
                                == {v.casefold() for v in normalized_current}
                            ):
                                copy_matches = False
                                break
                        if copy_matches and current_fields:
                            source_fields = current_fields
                            source_verified = True
                            reason = "source_not_confirmed"
                        elif copy_matches:
                            reason = "source_marker_has_no_supported_fields"
                        else:
                            reason = "supplier_snapshot_values_disagree"

        source_identity = None
        if imported is not None and supplier_product is not None and supplier is not None:
            source_identity = {
                "imported_product_id": imported.id,
                "supplier_product_id": supplier_product.id,
                "supplier_id": supplier.id,
                "source_type": imported.source_type,
                "imported_external_id": imported.external_id,
                "source_external_id": supplier_product.external_id,
                "imported_wb_nm_id": imported.wb_nm_id,
                "imported_subject_id": imported.wb_subject_id,
                "source_subject_id": supplier_product.wb_subject_id,
                "copied_revision": imported.supplier_content_revision,
                "current_revision": supplier_product.content_revision,
            }

        display_snapshot = copied or current or {}
        fact_rows = []
        for field_name, label in (
            ("colors", "Цвет"),
            ("country", "Страна"),
        ):
            value = (
                source_fields.get(field_name)
                if source_verified and field_name in source_fields
                else display_snapshot.get(field_name)
            )
            values = _bounded_fact_values(value, field_name=field_name)
            if values:
                fact_rows.append({
                    "field": field_name,
                    "label": label,
                    "values": values,
                    "unit": None,
                    "availability": (
                        "confirmed"
                        if source_verified and field_name in source_fields
                        else "snapshot_only"
                    ),
                    "source": (
                        "observed_supplier_fields"
                        if source_verified and field_name in source_fields
                        else "supplier_snapshot"
                    ),
                })
        for field_name, label in (
            ("materials", "Материалы"),
            ("sizes", "Размеры"),
        ):
            values = _bounded_fact_values(
                display_snapshot.get(field_name), field_name=field_name,
            )
            if values:
                fact_rows.append({
                    "field": field_name,
                    "label": label,
                    "values": values,
                    "unit": None,
                    "availability": "snapshot_only",
                    "source": "supplier_snapshot",
                })
        if not display_safe:
            fact_rows = []

        evidence_by_product[str(product_id)] = {
            "status": "available" if source_verified else "unknown",
            "reason": None if source_verified else reason,
            "supplier_name": (
                supplier.name[:200]
                if relation_valid and supplier is not None
                and isinstance(supplier.name, str) else None
            ),
            "source_checked_at": (
                supplier.last_sync_at.isoformat()
                if relation_valid and supplier is not None
                and supplier.last_sync_at else None
            ),
            "facts": fact_rows[:60],
        }

        product_changes = changes_by_product.get(product_id, [])
        for change in product_changes:
            char_id = int(change.get("id"))
            field = fields_by_id.get(char_id)
            comparison = {
                "characteristic_id": char_id,
                "characteristic": field.get("name") if field else None,
                "status": "unknown",
                "reason": reason if not source_verified else "no_exact_field_mapping",
                "source_values": [],
                "proposed_matches_source": None,
            }
            if source_verified and field is not None:
                directory = field.get("dictionary_source")
                source_field = directory_to_source.get(directory)
                mapped_fields = fields_by_directory.get(directory, [])
                if source_field is None:
                    comparison["reason"] = "no_exact_field_mapping"
                elif len(mapped_fields) != 1 or mapped_fields[0].get("id") != char_id:
                    comparison["reason"] = "ambiguous_wb_characteristic_mapping"
                elif field.get("charc_type") != 1:
                    comparison["reason"] = "unsupported_characteristic_type"
                else:
                    raw_source_values = source_fields.get(source_field)
                    source_values = _source_values(raw_source_values, source_field)
                    allowed_by_key = allowed_by_characteristic.get(char_id, {})
                    canonical_source = []
                    for raw_value in source_values or []:
                        candidates = allowed_by_key.get(raw_value.casefold(), [])
                        if len(candidates) != 1:
                            canonical_source = []
                            break
                        canonical_source.append(candidates[0])
                    canonical_proposed = []
                    raw_proposed = change.get("value")
                    if isinstance(raw_proposed, list):
                        proposed = raw_proposed
                    elif isinstance(raw_proposed, str):
                        proposed = [raw_proposed]
                    else:
                        proposed = []
                    for raw_value in proposed:
                        if not isinstance(raw_value, str):
                            canonical_proposed = []
                            break
                        raw_value = raw_value.strip()
                        if not raw_value:
                            canonical_proposed = []
                            break
                        candidates = allowed_by_key.get(raw_value.casefold(), [])
                        if len(candidates) != 1:
                            canonical_proposed = []
                            break
                        canonical_proposed.append(candidates[0])
                    if not canonical_source or len({
                        value.casefold() for value in canonical_source
                    }) != len(canonical_source):
                        comparison["reason"] = "source_value_not_exact_directory_match"
                    elif (
                        field.get("max_count")
                        and len(canonical_source) > int(field.get("max_count") or 0)
                    ):
                        comparison["reason"] = "source_value_exceeds_characteristic_limit"
                    elif not canonical_proposed or len({
                        value.casefold() for value in canonical_proposed
                    }) != len(canonical_proposed):
                        comparison["reason"] = "proposed_value_not_exact_directory_match"
                    else:
                        matches = (
                            {value.casefold() for value in canonical_source}
                            == {value.casefold() for value in canonical_proposed}
                        )
                        comparison.update({
                            "status": "match" if matches else "conflict",
                            "reason": None if matches else "proposed_value_differs_from_source",
                            "source_values": canonical_source,
                            "proposed_matches_source": matches,
                        })
            comparisons[f"{product_id}:{char_id}"] = comparison

        guard_rows.append({
            "product_id": product_id,
            "seller_id": product.seller_id,
            "nm_id": product.nm_id,
            "subject_id": product.subject_id,
            "source_identity": source_identity,
            "source_verified": source_verified,
            "source_values": source_fields if source_verified else None,
            "comparisons": {
                f"{product_id}:{int(change.get('id'))}": {
                    key: value
                    for key, value in comparisons.get(
                        f"{product_id}:{int(change.get('id'))}",
                        {},
                    ).items()
                    if key != "characteristic"
                }
                for change in changes_by_product.get(product_id, [])
            },
        })

    guard = hashlib.sha256(_canonical_json({
        "v": 1,
        "selected_product_ids": product_ids,
        "subject_id": subject_id,
        "source_rows": guard_rows,
    }).encode("utf-8")).hexdigest()
    return evidence_by_product, comparisons, guard
