"""Pure, bounded review of a saved Ozon draft's category replacement."""

import hashlib
import json

from flask import current_app
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer


MAX_IMPACT_ROWS = 500
MAX_IMPACT_BYTES = 262_144
PAGE_SIZE = 25
TOKEN_AGE_SECONDS = 600


def _document(raw, fallback):
    if raw is None or raw == "":
        return fallback
    value = json.loads(raw)
    if not isinstance(value, type(fallback)):
        raise ValueError("Сохранённые характеристики имеют неверный формат")
    return value


def _identity(item):
    if not isinstance(item, dict):
        raise ValueError("Сохранённая характеристика имеет неверный формат")
    return str(item.get("attribute_id")), str(item.get("complex_id") or "0")


def _name(names, item):
    attribute_id, complex_id = _identity(item)
    return names.get((attribute_id, complex_id)) or f"Характеристика {attribute_id}"


def _row(kind, path, label, before, after):
    return {
        "kind": kind,
        "path": path,
        "label": label,
        "before": before,
        "after": after,
    }


def build_impact(draft, *, target_type, auto_attributes, names, save_mapping, changing):
    """Compare exact saved documents with the type-change write result."""
    old_simple = _document(draft.attributes_json, [])
    old_complex = _document(draft.complex_attributes_json, [])
    old_removals = _document(draft.attribute_removals_json, [])
    if not all(isinstance(value, list) for value in (old_simple, old_complex, old_removals)):
        raise ValueError("Сохранённые характеристики имеют неверный формат")
    old_by_key = {_identity(item): item for item in old_simple}
    new_by_key = {_identity(item): item for item in auto_attributes}
    if len(old_by_key) != len(old_simple) or len(new_by_key) != len(auto_attributes):
        raise ValueError("Сохранённые характеристики содержат повторяющиеся поля")
    rows = []
    for key in sorted(set(old_by_key) | set(new_by_key)):
        before = old_by_key.get(key)
        after = new_by_key.get(key)
        if before != after:
            item = before or after
            rows.append(_row(
                "attribute", f"attributes.{key[0]}:{key[1]}",
                _name(names, item), before, after,
            ))
    for group_index, group in enumerate(old_complex if changing else []):
        if not isinstance(group, dict) or not isinstance(group.get("attributes"), list):
            raise ValueError("Сохранённая составная характеристика имеет неверный формат")
        for item_index, item in enumerate(group["attributes"]):
            rows.append(_row(
                "complex_attribute", f"complex_attributes.{group_index}.{item_index}",
                f"Группа {group_index + 1} · {_name(names, item)}", item, None,
            ))
    for index, item in enumerate(old_removals if changing else []):
        rows.append(_row(
            "attribute_removal", f"attribute_removals.{index}",
            f"План удаления · {_name(names, item)}", item, None,
        ))
    if (draft.category_mapping_id is not None and changing) or save_mapping:
        rows.append(_row(
            "category_mapping", "category_mapping_id", "Соответствие категории источника",
            {"id": draft.category_mapping_id} if draft.category_mapping_id is not None else None,
            {"new_mapping_requested": True} if save_mapping else None,
        ))
    if len(rows) > MAX_IMPACT_ROWS:
        raise ValueError("Изменений слишком много для безопасного просмотра")
    canonical = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(canonical.encode("utf-8")) > MAX_IMPACT_BYTES:
        raise ValueError("Значения слишком велики для безопасного просмотра")
    return {
        "rows": rows,
        "digest": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "counts": {
            kind: sum(row["kind"] == kind for row in rows)
            for kind in ("attribute", "complex_attribute", "attribute_removal", "category_mapping")
        },
        "total": len(rows),
        "target_type": {
            "id": target_type.id,
            "name": target_type.name,
            "category_path": target_type.category.full_path,
        } if target_type else None,
    }


def _scope(draft, *, actor_user_id, target_type, save_mapping, impact):
    return {
        "seller": draft.seller_id,
        "user": actor_user_id,
        "account": draft.account_id,
        "draft": draft.id,
        "version": draft.version,
        "old_type": draft.product_type_id,
        "target_type": target_type.id if target_type else None,
        "target_schema": target_type.attributes_schema_hash if target_type else None,
        "target_schema_version": target_type.attributes_version if target_type else None,
        "source_hash": draft.source_fact_hash,
        "source_json_hash": hashlib.sha256((draft.source_facts_json or "").encode()).hexdigest(),
        "save_mapping": save_mapping,
        "impact": impact["digest"],
    }


def _serializer():
    return URLSafeTimedSerializer(current_app.secret_key, salt="ozon-category-impact-v1")


def issue_token(draft, *, actor_user_id, target_type, save_mapping, impact):
    return _serializer().dumps(_scope(
        draft, actor_user_id=actor_user_id, target_type=target_type,
        save_mapping=save_mapping, impact=impact,
    ))


def valid_token(token, draft, *, actor_user_id, target_type, save_mapping, impact):
    if not isinstance(token, str) or not token or len(token) > 2048:
        return False
    try:
        claims = _serializer().loads(token, max_age=TOKEN_AGE_SECONDS)
    except (BadSignature, SignatureExpired):
        return False
    return claims == _scope(
        draft, actor_user_id=actor_user_id, target_type=target_type,
        save_mapping=save_mapping, impact=impact,
    )
