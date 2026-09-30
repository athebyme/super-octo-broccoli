"""Seller-scoped common content edits with signed preview and full-set CAS."""

from __future__ import annotations

from datetime import datetime
from hashlib import sha256
import json
import math
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import and_, update
from sqlalchemy.orm import joinedload

from models import (
    AgentChangeSnapshot,
    ImportedProduct,
    MarketplaceListing,
    MarketplaceProductDraft,
    Product,
    SupplierProduct,
    db,
)


SCHEMA_VERSION = 1
FIELD_COLUMNS = {
    "title": "title",
    "description": "description",
    "photos": "photo_urls",
    "characteristics": "characteristics",
}
MAX_ITEMS = 50
MAX_BODY_BYTES = 256 * 1024
MAX_PREVIEW_TOKEN_BYTES = 240 * 1024
MAX_RECIPIENTS_PER_ITEM = 30
MAX_RECIPIENTS_TOTAL = 100
MAX_TITLE = 500
MAX_DESCRIPTION = 100_000
MAX_PHOTOS = 30
MAX_CHARACTERISTICS = 100
MAX_MANUAL_NUMBER_ABS = 10**15
_PROVIDER_ID_KEYS = {
    "id", "attributeid", "charcid", "valueid", "wbsubjectid",
    "subjectid", "nmid", "imtid", "marketplaceid",
    "price", "sellerprice", "discountprice", "stock", "quantity",
    "amount", "остаток", "цена", "артикул", "штрихкод", "barcode",
    "sellerid", "productid", "categoryid", "sku", "offerid",
    "цены", "остатки", "количество", "склад", "артикулы",
    "идентификатор", "штрихкоды", "номенклатура", "nmид",
}


class CommonProductContentError(ValueError):
    status_code = 400
    code = "common_content_error"

    def __init__(self, message: str, *, status_code: int | None = None, code: str | None = None):
        super().__init__(message)
        if status_code is not None:
            self.status_code = status_code
        if code:
            self.code = code


class CommonProductContentNotFound(CommonProductContentError):
    status_code = 404
    code = "common_content_not_found"


class CommonProductContentConflict(CommonProductContentError):
    status_code = 409
    code = "common_content_conflict"


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _fingerprint_value(value: Any) -> Any:
    """Make legacy in-memory non-finite values hashable without normalizing raw DB text."""
    if isinstance(value, float) and not math.isfinite(value):
        return {"__nonfinite_float__": repr(value)}
    if isinstance(value, list):
        return [_fingerprint_value(item) for item in value]
    if isinstance(value, tuple):
        return [_fingerprint_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _fingerprint_value(item) for key, item in value.items()}
    return value


def _fingerprint(value: Any) -> str:
    return sha256(_stable_json(_fingerprint_value(value)).encode("utf-8")).hexdigest()


def _manual_number_is_supported(value: Any) -> bool:
    if isinstance(value, bool):
        return True
    if isinstance(value, int):
        return abs(value) <= MAX_MANUAL_NUMBER_ABS
    return (
        isinstance(value, float)
        and math.isfinite(value)
        and abs(value) <= MAX_MANUAL_NUMBER_ABS
    )


def _inherited_number_text(value: int | float) -> str:
    try:
        rendered = str(value)
    except (ValueError, OverflowError):
        rendered = "число не представлено"
    if isinstance(value, float) and not math.isfinite(value):
        return f"{rendered} · некорректное число источника"
    return f"{rendered} · значение источника слишком велико"


def _load_json(raw: Any, fallback: Any) -> Any:
    if isinstance(raw, type(fallback)):
        return raw
    if not isinstance(raw, str) or len(raw.encode("utf-8", errors="ignore")) > MAX_BODY_BYTES:
        return fallback
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError, RecursionError):
        return fallback
    if fallback is None:
        return parsed
    return parsed if isinstance(parsed, type(fallback)) else fallback


def _default(field: str) -> Any:
    return [] if field in {"photos", "characteristics"} else ""


def _safe_json_value(value: Any, *, depth: int = 0) -> bool:
    if depth > 8:
        return False
    if value is None or isinstance(value, (str, bool)):
        return True
    if isinstance(value, int):
        return abs(value) <= MAX_MANUAL_NUMBER_ABS
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, list):
        return len(value) <= 500 and all(_safe_json_value(item, depth=depth + 1) for item in value)
    if isinstance(value, dict):
        return len(value) <= 500 and all(
            isinstance(key, str) and len(key) <= 500
            and _safe_json_value(item, depth=depth + 1)
            for key, item in value.items()
        )
    return False


def _strict_text(value: Any, field: str, *, maximum: int, multiline: bool, allow_empty: bool) -> str:
    if not isinstance(value, str):
        raise CommonProductContentError(f"Поле «{field}» должно быть текстом")
    result = value.strip()
    if any(
        (ord(char) < 32 and not (multiline and char in "\n\t")) or ord(char) == 127
        for char in result
    ):
        raise CommonProductContentError(f"Поле «{field}» содержит управляющие символы")
    if len(result) > maximum:
        raise CommonProductContentError(f"Поле «{field}» длиннее {maximum} символов")
    if not result and not allow_empty:
        raise CommonProductContentError(f"Поле «{field}» не может быть пустым")
    return result


def _primary_photo_url(entry: Any) -> str | None:
    if isinstance(entry, str):
        candidate = entry
    elif isinstance(entry, dict):
        candidate = next((
            entry.get(key) for key in ("sexoptovik", "original", "url", "blur")
            if isinstance(entry.get(key), str) and entry.get(key).strip()
        ), None)
    else:
        candidate = None
    if not isinstance(candidate, str):
        return None
    candidate = candidate.strip()
    if not candidate or len(candidate) > 2_000 or any(ord(ch) < 32 for ch in candidate):
        return None
    try:
        parts = urlsplit(candidate)
    except ValueError:
        return None
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
        return None
    return candidate


def _photo_urls(value: Any) -> list[str]:
    if isinstance(value, str):
        try:
            parsed = json.loads(value) if len(value.encode("utf-8", errors="ignore")) <= MAX_BODY_BYTES else None
        except (TypeError, ValueError, RecursionError):
            parsed = None
        if isinstance(parsed, list):
            value = parsed
        elif value.strip().startswith(("http://", "https://")):
            value = [value]
        else:
            return []
    if not isinstance(value, list):
        return []
    result = []
    seen = set()
    for entry in value[:100]:
        url = _primary_photo_url(entry)
        if url and url not in seen:
            seen.add(url)
            result.append(url)
        if len(result) >= MAX_PHOTOS:
            break
    return result


def _characteristic_rows(value: Any, *, validate: bool = False) -> list[dict]:
    if isinstance(value, str):
        if validate:
            raise CommonProductContentError("Передайте характеристики как список или объект с парами названия и значения")
        raw_text = value
        value = _load_json(value, None)
        if value is None:
            if validate:
                raise CommonProductContentError("Характеристики должны быть корректным JSON-объектом или списком")
            return [{"name": "Источник · нераспознанные данные", "value": raw_text[:2_000]}]
    if isinstance(value, dict):
        if validate and len(value) > MAX_CHARACTERISTICS:
            raise CommonProductContentError(f"Не больше {MAX_CHARACTERISTICS} характеристик")
        rows = [{"name": name, "value": item} for name, item in list(value.items())[:MAX_CHARACTERISTICS]]
    elif isinstance(value, list):
        if validate and len(value) > MAX_CHARACTERISTICS:
            raise CommonProductContentError(f"Не больше {MAX_CHARACTERISTICS} характеристик")
        rows = []
        for item in value[:MAX_CHARACTERISTICS]:
            if not isinstance(item, dict):
                if validate:
                    raise CommonProductContentError("Каждая характеристика должна содержать только название и значение")
                rows.append({"name": f"Источник · {len(rows) + 1}", "value": item})
                continue
            if validate and set(item) != {"name", "value"}:
                raise CommonProductContentError(
                    "Ручные характеристики принимают только пары name/value; идентификаторы площадок нельзя менять"
                )
            name = item.get("name") or item.get("charcName") or item.get("key")
            if "value" in item:
                raw_value = item.get("value")
            elif "values" in item:
                raw_value = item.get("values")
            else:
                raw_value = item.get("valueId", item)
            if not validate and name is None:
                raw_value = item
            rows.append({"name": name, "value": raw_value})
    elif value in (None, ""):
        return []
    elif validate:
        raise CommonProductContentError("Характеристики должны быть объектом или списком")
    else:
        return [{"name": "Источник · нераспознанные данные", "value": str(value)[:2_000]}]

    result = []
    seen = set()
    for index, row in enumerate(rows):
        name = row.get("name")
        if not isinstance(name, str):
            if validate:
                raise CommonProductContentError(f"Характеристика {index + 1}: укажите название")
            name = f"Источник · {index + 1}"
        if validate:
            name = _strict_text(name, "характеристики.name", maximum=120, multiline=False, allow_empty=False)
        else:
            name = name.strip()[:120] or f"Значение источника {index + 1}"
        identity = "".join(char for char in name.casefold() if char.isalnum())
        if identity in _PROVIDER_ID_KEYS:
            if validate:
                raise CommonProductContentError(f"«{name}» — системный идентификатор площадки; укажите название свойства")
            continue
        if name.casefold() in seen and validate:
            if validate:
                raise CommonProductContentError(f"Характеристика «{name}» указана дважды")
        if name.casefold() in seen and not validate:
            name = f"{name} · {index + 1}"
        item_value = row.get("value")
        if isinstance(item_value, str) and validate:
            item_value = _strict_text(item_value, "характеристики.value", maximum=2_000, multiline=True, allow_empty=True)
        elif isinstance(item_value, str):
            item_value = item_value[:2_000]
        elif isinstance(item_value, bool):
            pass
        elif isinstance(item_value, (int, float)) and not isinstance(item_value, bool):
            if validate and not _manual_number_is_supported(item_value):
                raise CommonProductContentError(
                    f"Число характеристики должно быть конечным и не больше {MAX_MANUAL_NUMBER_ABS} по модулю"
                )
            if not validate and not _manual_number_is_supported(item_value):
                item_value = _inherited_number_text(item_value)
        elif isinstance(item_value, list):
            if validate and len(item_value) > 20:
                raise CommonProductContentError("В одном свойстве допускается не больше 20 значений")
            normalized = []
            for subvalue in item_value[:20]:
                if isinstance(subvalue, str):
                    normalized.append(
                        _strict_text(subvalue, "характеристики.value", maximum=2_000, multiline=False, allow_empty=True)
                        if validate else subvalue[:2_000]
                    )
                elif isinstance(subvalue, bool):
                    normalized.append(subvalue)
                elif isinstance(subvalue, (int, float)):
                    if validate and not _manual_number_is_supported(subvalue):
                        raise CommonProductContentError(
                            f"Число характеристики должно быть конечным и не больше {MAX_MANUAL_NUMBER_ABS} по модулю"
                        )
                    normalized.append(
                        subvalue if _manual_number_is_supported(subvalue)
                        else _inherited_number_text(subvalue)
                    )
                elif not validate and _safe_json_value(subvalue):
                    normalized.append(subvalue)
                else:
                    if validate:
                        raise CommonProductContentError("Значения свойства должны быть текстом или числом")
                    normalized.append(str(subvalue)[:500])
            item_value = normalized
        elif isinstance(item_value, dict) and not validate and _safe_json_value(item_value):
            item_value = item_value
        elif validate:
            raise CommonProductContentError("Значения свойства должны быть текстом, числом или списком")
        elif not _safe_json_value(item_value):
            item_value = str(item_value)[:500]
        seen.add(name.casefold())
        result.append({"name": name, "value": item_value})
    return result


def normalize_value(field: str, value: Any) -> Any:
    if field == "title":
        return _strict_text(value, "название", maximum=MAX_TITLE, multiline=False, allow_empty=False)
    if field == "description":
        return _strict_text(value, "описание", maximum=MAX_DESCRIPTION, multiline=True, allow_empty=True)
    if field == "photos":
        if not isinstance(value, list) or len(value) > MAX_PHOTOS:
            raise CommonProductContentError(f"Выберите не больше {MAX_PHOTOS} фотографий")
        result = []
        for item in value:
            if not isinstance(item, str):
                raise CommonProductContentError("Каждая фотография должна быть выбранным источником")
            url = _primary_photo_url(item)
            if url != item:
                raise CommonProductContentError("Фото должно совпадать с доступным изображением товара")
            if url in result:
                raise CommonProductContentError("Список фотографий содержит повтор")
            result.append(url)
        return result
    if field == "characteristics":
        return _characteristic_rows(value, validate=True)
    raise CommonProductContentError("Поле общего товара не поддерживается")


def _serialize_field(field: str, value: Any) -> Any:
    if field in {"photos", "characteristics"}:
        return _stable_json(value)
    return value


def _raw_column_value(product: ImportedProduct, field: str) -> Any:
    return getattr(product, FIELD_COLUMNS[field], None)


def _read_column(product: ImportedProduct, field: str) -> Any:
    raw = _raw_column_value(product, field)
    if field in {"title", "description"}:
        return raw if isinstance(raw, str) else ""
    if field == "photos":
        return _photo_urls(raw)
    if field == "characteristics":
        return _characteristic_rows(raw)
    return None


def _parse_overrides(product: ImportedProduct) -> dict:
    raw = product.content_overrides_json
    if raw in (None, ""):
        return {"schema_version": SCHEMA_VERSION, "fields": {}}
    if not isinstance(raw, str) or len(raw.encode("utf-8", errors="ignore")) > MAX_BODY_BYTES:
        raise CommonProductContentConflict("Ручные переопределения повреждены; сохранение заблокировано")
    try:
        state = json.loads(raw)
    except (TypeError, ValueError, RecursionError):
        raise CommonProductContentConflict("Ручные переопределения повреждены; сохранение заблокировано") from None
    if (
        not isinstance(state, dict)
        or set(state) != {"schema_version", "fields"}
        or state.get("schema_version") != SCHEMA_VERSION
        or not isinstance(state.get("fields"), dict)
    ):
        raise CommonProductContentConflict("Версия ручных переопределений неизвестна; сохранение заблокировано")
    for field, entry in state["fields"].items():
        if field not in FIELD_COLUMNS or not isinstance(entry, dict):
            raise CommonProductContentConflict("В ручных переопределениях есть неподдерживаемое поле")
        if set(entry) != {
            "value", "inherited_value", "inherited_origin",
            "edited_by_user_id", "edited_at", "edit_version",
        }:
            raise CommonProductContentConflict("Формат ручного переопределения неизвестен")
        if (
            isinstance(entry.get("edited_by_user_id"), bool)
            or not isinstance(entry.get("edited_by_user_id"), int)
            or entry["edited_by_user_id"] <= 0
            or not isinstance(entry.get("edited_at"), str)
            or entry.get("inherited_origin") not in {
                "source", "ai_suggestion", "supplier_enrichment", "unknown",
            }
            or isinstance(entry.get("edit_version"), bool)
            or not isinstance(entry.get("edit_version"), int)
            or entry["edit_version"] < 1
        ):
            raise CommonProductContentConflict("Метаданные ручного переопределения повреждены")
        try:
            normalize_value(field, entry["value"])
        except CommonProductContentError:
            raise CommonProductContentConflict("Значение ручного переопределения повреждено") from None
        if not _safe_json_value(entry["inherited_value"]):
            raise CommonProductContentConflict("Источник ручного переопределения повреждён")
    return state


def active_override_fields(product: ImportedProduct) -> set[str]:
    return set(_parse_overrides(product)["fields"])


def common_content_override_projection(product: ImportedProduct) -> dict:
    """Return only verified seller overrides for explicit channel draft preparation.

    This projection is separate from observed facts and is not suitable as AI
    source evidence. Existing channel drafts do not consume it on read/save.
    """
    overrides = _parse_overrides(product)
    projected = {}
    for field in ("title", "description", "photos"):
        entry = overrides["fields"].get(field)
        if entry is None:
            continue
        expected = normalize_value(field, entry["value"])
        effective = _read_column(product, field)
        if effective != expected:
            raise CommonProductContentConflict(
                f"Общее поле «{field}» товара #{product.id} расходится с ручным переопределением"
            )
        projected[field] = {
            "value": effective,
            "origin": "seller_override",
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "content_edit_version": int(product.content_edit_version or 1),
        "fields": projected,
    }


def _source_documents(product: ImportedProduct) -> tuple[dict, SupplierProduct | None]:
    source = _load_json(product.original_data, {})
    if not isinstance(source, dict):
        source = {}
    supplier = product.supplier_product
    if supplier is not None and supplier.id != product.supplier_product_id:
        supplier = None
    return source, supplier


def _source_raw_values(product: ImportedProduct, overrides: dict | None = None) -> dict:
    original, supplier = _source_documents(product)
    if supplier is not None:
        title = supplier.title if supplier.title is not None else original.get("title", product.title)
        description = supplier.description or supplier.ai_description or original.get("description", product.description) or ""
        photo_raw = supplier.photo_urls_json or original.get("photo_urls", product.photo_urls)
        try:
            from services.supplier_service import _supplier_characteristics_payload
            characteristics_raw = _supplier_characteristics_payload(supplier)
        except Exception:
            characteristics_raw = supplier.characteristics_json or original.get("characteristics", product.characteristics)
        return {
            "title": title,
            "description": description,
            "photos": photo_raw,
            "characteristics": characteristics_raw,
        }

    override_fields = (overrides or {}).get("fields", {})
    result = {}
    for field, raw_key in (
        ("title", "title"),
        ("description", "description"),
        ("photos", "photo_urls"),
        ("characteristics", "characteristics"),
    ):
        if raw_key in original and original.get(raw_key) not in (None, ""):
            result[field] = original.get(raw_key)
        elif field in override_fields and "inherited_value" in override_fields[field]:
            result[field] = override_fields[field]["inherited_value"]
        else:
            result[field] = _raw_column_value(product, field)
    return result


def _source_origins(product: ImportedProduct, overrides: dict | None = None) -> dict[str, str]:
    """Return provenance labels without treating copied/effective data as source evidence."""
    original, supplier = _source_documents(product)
    override_fields = (overrides or {}).get("fields", {})
    if supplier is not None:
        origins = {}
        title_from_supplier = supplier.title is not None
        origins["title"] = "source" if title_from_supplier or original.get("title") not in (None, "") else "unknown"
        if supplier.description not in (None, ""):
            if supplier.description_source == "ai":
                origins["description"] = "ai_suggestion"
            elif supplier.description_source == "manual":
                origins["description"] = "supplier_enrichment"
            else:
                origins["description"] = "source"
        elif supplier.ai_description not in (None, ""):
            origins["description"] = "ai_suggestion"
        elif original.get("description") not in (None, ""):
            origins["description"] = "source"
        else:
            origins["description"] = "unknown"

        if supplier.photo_urls_json not in (None, "") or original.get("photo_urls") not in (None, ""):
            origins["photos"] = "source"
        else:
            origins["photos"] = "unknown"

        enriched_characteristics = None
        try:
            from services.supplier_service import _supplier_characteristics_payload
            enriched_characteristics = _supplier_characteristics_payload(supplier)
        except Exception:
            pass
        if enriched_characteristics not in (None, ""):
            has_marketplace_enrichment = False
            try:
                marketplace_payload = json.loads(supplier.ai_marketplace_json or "{}")
                meta = marketplace_payload.get("_meta") if isinstance(marketplace_payload, dict) else None
                has_marketplace_enrichment = (
                    isinstance(meta, dict)
                    and meta.get("source") == "supplier_catalog_enrichment"
                    and bool(supplier.ai_marketplace_json)
                )
            except (TypeError, ValueError, RecursionError):
                pass
            origins["characteristics"] = "supplier_enrichment" if has_marketplace_enrichment else "source"
        elif original.get("characteristics") not in (None, ""):
            origins["characteristics"] = "source"
        else:
            origins["characteristics"] = "unknown"
        return origins

    origins = {}
    for field, raw_key in (
        ("title", "title"),
        ("description", "description"),
        ("photos", "photo_urls"),
        ("characteristics", "characteristics"),
    ):
        if original.get(raw_key) not in (None, "", [], {}):
            origins[field] = "source"
        elif field in override_fields:
            origins[field] = override_fields[field].get("inherited_origin", "unknown")
        else:
            origins[field] = "unknown"
    return origins


def _inherited_normalize(field: str, value: Any) -> Any:
    """Tolerant projection for source snapshots; strict validation is manual-only."""
    if field == "title":
        return value[:MAX_TITLE] if isinstance(value, str) else ""
    if field == "description":
        return value[:MAX_DESCRIPTION] if isinstance(value, str) else ""
    if field == "photos":
        return _photo_urls(value)
    if field == "characteristics":
        return _characteristic_rows(value)
    return _default(field)


def inherited_value(product: ImportedProduct, field: str, overrides: dict | None = None) -> Any:
    raw = _source_raw_values(product, overrides).get(field)
    return _inherited_normalize(field, raw)


def _source_state(product: ImportedProduct, overrides: dict) -> dict:
    _, supplier = _source_documents(product)
    raw_values = _source_raw_values(product, overrides)
    origins = _source_origins(product, overrides)
    source = {
        "type": str(product.source_type or "unknown")[:80],
        "supplier_id": product.supplier_id,
        "supplier_product_id": product.supplier_product_id,
        "copied_revision": int(product.supplier_content_revision or 0),
        "revision": None,
        "observed_at": None,
        "changed_since_refresh": None,
    }
    if supplier is not None:
        source["revision"] = int(supplier.content_revision or 1)
        source["observed_at"] = supplier.updated_at.isoformat() if supplier.updated_at else None
        source["changed_since_refresh"] = source["revision"] != source["copied_revision"]
        source_payload = {
            "source_type": source["type"],
            "supplier_id": supplier.supplier_id,
            "supplier_product_id": supplier.id,
            "revision": source["revision"],
            "observed_at": source["observed_at"],
            "copied_revision": source["copied_revision"],
            "description_source": supplier.description_source,
            "title": supplier.title,
            "description": supplier.description,
            "ai_description": supplier.ai_description,
            "characteristics": supplier.characteristics_json,
            "ai_marketplace": supplier.ai_marketplace_json,
            "photo_urls": supplier.photo_urls_json,
            "original_data": supplier.original_data_json,
            "identity": {
                "imported_product_id": product.id,
                "seller_id": product.seller_id,
                "source_type": product.source_type,
                "supplier_id": product.supplier_id,
                "supplier_product_id": product.supplier_product_id,
                "external_id": product.external_id,
            },
            "field_origins": origins,
            "inherited_raw_values": raw_values,
            "inherited_values": {
                field: _inherited_normalize(field, raw_values.get(field))
                for field in FIELD_COLUMNS
            },
        }
    else:
        source["revision"] = source["copied_revision"] or None
        source["observed_at"] = None
        source_payload = {
            "identity": {
                "imported_product_id": product.id,
                "seller_id": product.seller_id,
                "source_type": product.source_type,
                "supplier_id": product.supplier_id,
                "supplier_product_id": product.supplier_product_id,
                "supplier_content_revision": product.supplier_content_revision,
                "external_id": product.external_id,
            },
            "original_data_raw": product.original_data,
            "field_origins": origins,
            "inherited_raw_values": raw_values,
            "values": {
                field: _inherited_normalize(field, raw_values.get(field))
                for field in FIELD_COLUMNS
            },
        }
    source["fingerprint"] = _fingerprint(source_payload)
    source["identity"] = {
        "source_type": source["type"],
        "supplier_id": product.supplier_id,
        "supplier_product_id": product.supplier_product_id,
        "external_id": str(product.external_id or "")[:200],
    }
    source["field_origins"] = origins
    source["values"] = {
        field: _inherited_normalize(field, raw_values.get(field))
        for field in FIELD_COLUMNS
    }
    return source


def _current_fingerprint(product: ImportedProduct, overrides: dict) -> str:
    return _fingerprint({
        "version": int(product.content_edit_version or 1),
        "identity": {
            "id": product.id,
            "seller_id": product.seller_id,
            "source_type": product.source_type,
            "external_id": product.external_id,
            "supplier_id": product.supplier_id,
            "supplier_product_id": product.supplier_product_id,
            "supplier_content_revision": product.supplier_content_revision,
        },
        # Fingerprint the exact persisted representation as well as typed fields.
        # Legacy JSON whitespace/key-order edits still change the reviewed baseline.
        "raw_values": {
            field: _raw_column_value(product, field)
            for field in FIELD_COLUMNS
        },
        "raw_overrides": product.content_overrides_json,
        "overrides": overrides,
    })


def _photo_options(product: ImportedProduct) -> list[dict]:
    original = _load_json(product.original_data, {})
    original_photos = original.get("photo_urls") if isinstance(original, dict) else None
    supplier_photos = product.supplier_product.photo_urls_json if product.supplier_product else None
    selected = _read_column(product, "photos")
    pool = []
    # Keep currently selected source URLs even if a cache preview has gone away.
    for url in selected:
        if url not in pool:
            pool.append(url)
    for value in (supplier_photos, original_photos, product.photo_urls):
        for url in _photo_urls(value):
            if url not in pool:
                pool.append(url)
            if len(pool) >= MAX_PHOTOS * 2:
                break
        if len(pool) >= MAX_PHOTOS * 2:
            break
    try:
        from services.source_photo_display import imported_photo_previews
        previews = imported_photo_previews(product)
    except Exception:
        previews = {}
    source_urls = set(_photo_urls(supplier_photos))
    return [{
        "url": url,
        "preview_url": previews.get(url),
        "source": "поставщик" if url in source_urls else "общий товар",
        "available_for_selection": bool(previews.get(url)) or url in selected,
    } for url in pool]


def _public_state(product: ImportedProduct, seller_id: int) -> dict:
    overrides = _parse_overrides(product)
    source = _source_state(product, overrides)
    fields = {}
    for field in FIELD_COLUMNS:
        is_override = field in overrides["fields"]
        inherited = source["values"][field]
        fields[field] = {
            "effective": _read_column(product, field),
            "inherited": inherited,
            "origin": "seller_override" if is_override else source["field_origins"].get(field, "unknown"),
            "inherited_origin": source["field_origins"].get(field, "unknown"),
            "is_overridden": is_override,
        }
    recipients = _recipient_records(seller_id, product)
    return {
        "product_id": product.id,
        "title": product.title or "Без названия",
        "external_id": str(product.external_id or "")[:200],
        "content_edit_version": int(product.content_edit_version or 1),
        "source": {key: value for key, value in source.items() if key != "values"},
        "fields": fields,
        "photo_options": _photo_options(product),
        "recipients": [
            {key: value for key, value in record.items() if key not in {"object", "fingerprint"}}
            | {"revision_fingerprint": record["fingerprint"]}
            for record in recipients
        ],
        "recipients_truncated": len(recipients) >= MAX_RECIPIENTS_PER_ITEM,
    }

def _recipient_records(seller_id: int, product: ImportedProduct) -> list[dict]:
    records = []
    drafts = MarketplaceProductDraft.query.options(
        joinedload(MarketplaceProductDraft.account),
        joinedload(MarketplaceProductDraft.marketplace),
    ).filter_by(
        seller_id=seller_id,
        imported_product_id=product.id,
    ).order_by(MarketplaceProductDraft.id.asc()).limit(MAX_RECIPIENTS_PER_ITEM + 1).all()
    for draft in drafts:
        account = draft.account
        marketplace = draft.marketplace
        if account is None or account.seller_id != seller_id or marketplace is None:
            continue
        ref = {"kind": "marketplace_draft", "id": int(draft.id)}
        state = {
            "ref": ref,
            "channel": str(marketplace.code or "unknown")[:50],
            "channel_label": str(marketplace.name or marketplace.code or "Площадка")[:100],
            "account_id": int(account.id),
            "account_label": str(account.label or marketplace.name or marketplace.code)[:120],
            "status": str(draft.status or "unknown")[:40],
            "version": int(draft.version or 1),
            "account_version": int(account.version or 1),
            "source_fact_hash": str(draft.source_fact_hash or "")[:64],
            "updated_at": draft.updated_at.isoformat() if draft.updated_at else None,
            "href": f"/marketplaces/drafts/{draft.id}/editor",
            "object": draft,
        }
        state["fingerprint"] = _fingerprint({
            "state": {
                key: value for key, value in state.items()
                if key not in {"object", "href"}
            },
            "raw_context": {
                "source_facts_json": draft.source_facts_json,
                "provenance_json": draft.provenance_json,
                "content_json": draft.content_json,
                "attributes_json": draft.attributes_json,
                "complex_attributes_json": draft.complex_attributes_json,
                "media_json": draft.media_json,
                "dimensions_json": draft.dimensions_json,
                "barcodes_json": draft.barcodes_json,
                "validation_result_json": draft.validation_result_json,
            },
        })
        records.append(state)

    listings = MarketplaceListing.query.options(
        joinedload(MarketplaceListing.account),
        joinedload(MarketplaceListing.marketplace),
    ).filter_by(
        seller_id=seller_id,
        imported_product_id=product.id,
    ).order_by(MarketplaceListing.id.asc()).limit(MAX_RECIPIENTS_PER_ITEM + 1).all()
    for listing in listings:
        account = listing.account
        marketplace = listing.marketplace
        if marketplace is None or (account is not None and account.seller_id != seller_id):
            continue
        ref = {"kind": "marketplace_listing", "id": int(listing.id)}
        state = {
            "ref": ref,
            "channel": str(marketplace.code or "unknown")[:50],
            "channel_label": str(marketplace.name or marketplace.code or "Площадка")[:100],
            "account_id": int(account.id) if account else None,
            "account_label": (
                str(account.label or marketplace.name or marketplace.code)[:120]
                if account else "Без кабинета"
            ),
            "status": str(listing.normalized_status or "unknown")[:40],
            "version": int(listing.link_version or 1),
            "link_status": str(listing.link_status or "unknown")[:40],
            "sync_fingerprint": str(listing.sync_fingerprint or "")[:64],
            "updated_at": listing.updated_at.isoformat() if listing.updated_at else None,
            "href": f"/marketplaces/listings/beta/{listing.id}",
            "object": listing,
        }
        state["fingerprint"] = _fingerprint({
            "state": {
                key: value for key, value in state.items()
                if key not in {"object", "href"}
            },
            "raw_context": {
                "title": listing.title,
                "description": listing.description,
                "attributes_json": listing.attributes_json,
                "complex_attributes_json": listing.complex_attributes_json,
                "media_json": listing.media_json,
                "dimensions_json": listing.dimensions_json,
                "barcodes_json": listing.barcodes_json,
            },
        })
        records.append(state)

    linked = product.product
    if linked is not None and product.product_id == linked.id and linked.seller_id == seller_id:
        ref = {"kind": "wb_product", "id": int(linked.id)}
        state = {
            "ref": ref,
            "channel": "wb",
            "channel_label": "Wildberries",
            "account_id": None,
            "account_label": "Карточка продавца",
            "status": "linked",
            "version": 1,
            "updated_at": linked.updated_at.isoformat() if linked.updated_at else None,
            "href": f"/products/{linked.id}",
            "object": linked,
        }
        state["fingerprint"] = _fingerprint({
            "state": {
                key: value for key, value in state.items()
                if key not in {"object", "href"}
            },
            "raw_context": {
                "seller_id": linked.seller_id,
                "nm_id": linked.nm_id,
                "title": linked.title,
                "description": linked.description,
                "photos_json": linked.photos_json,
                "characteristics_json": linked.characteristics_json,
            },
        })
        records.append(state)
    return records[:MAX_RECIPIENTS_PER_ITEM]


def _recipient_json_object(raw: Any) -> dict:
    value = _load_json(raw, None)
    return value if isinstance(value, dict) else {}


def _recipient_media_urls(media: Any) -> list[str]:
    if not isinstance(media, dict):
        return []
    images = media.get("images")
    if not isinstance(images, list):
        images = []
    primary = media.get("primary_image")
    return _photo_urls(([primary] if isinstance(primary, str) else []) + images)


def _recipient_diff(record: dict, effective: dict, changed_fields: set[str]) -> list[dict]:
    target = record["object"]
    if record["ref"]["kind"] == "marketplace_draft":
        content = _recipient_json_object(target.content_json)
        media = _recipient_json_object(target.media_json)
        current_values = {
            "title": content.get("name"),
            "description": content.get("description"),
            "photos": _recipient_media_urls(media),
        }
    elif record["ref"]["kind"] == "marketplace_listing":
        media = _recipient_json_object(target.media_json)
        current_values = {
            "title": target.title,
            "description": target.description,
            "photos": _recipient_media_urls(media),
        }
    else:
        current_values = {
            "title": target.title,
            "description": target.description,
            "photos": _photo_urls(target.photos_json),
        }
    diffs = []
    for field in sorted(changed_fields):
        if field == "characteristics":
            diffs.append({
                "field": field,
                "mapped": False,
                "message": "Общие характеристики не применяются к каналу без отдельного сопоставления.",
            })
            continue
        diffs.append({
            "field": field,
            "current": current_values.get(field),
            "common_after_save": effective[field],
            "matches": current_values.get(field) == effective[field],
            "effect": "local_only",
        })
    return diffs


def _parse_reference(value: Any) -> dict:
    if (
        not isinstance(value, dict) or set(value) != {"kind", "id"}
        or value.get("kind") not in {"marketplace_draft", "marketplace_listing", "wb_product"}
        or isinstance(value.get("id"), bool) or not isinstance(value.get("id"), int)
        or value["id"] <= 0
    ):
        raise CommonProductContentError("Контекст канала имеет неверный формат")
    return {"kind": value["kind"], "id": value["id"]}


def _parse_changes(raw: Any) -> dict:
    if not isinstance(raw, dict) or not raw or set(raw) - set(FIELD_COLUMNS):
        raise CommonProductContentError("Изменения должны содержать поддерживаемые поля общего товара")
    result = {}
    for field, change in raw.items():
        if not isinstance(change, dict) or "mode" not in change:
            raise CommonProductContentError(f"Для поля «{field}» выберите источник или своё значение")
        if change["mode"] == "inherit":
            if set(change) != {"mode"}:
                raise CommonProductContentError(f"Поля «{field}»: режим наследования не принимает значение")
            result[field] = {"mode": "inherit"}
        elif change["mode"] == "override":
            if set(change) != {"mode", "value"}:
                raise CommonProductContentError(f"Поля «{field}»: режим своего значения требует только value")
            result[field] = {"mode": "override", "value": normalize_value(field, change["value"])}
        else:
            raise CommonProductContentError(f"Для поля «{field}» неизвестный режим")
    return result


def _parse_items(raw_items: Any) -> list[dict]:
    if not isinstance(raw_items, list) or not raw_items:
        raise CommonProductContentError("Нужно выбрать от 1 до 50 товаров")
    if len(raw_items) > MAX_ITEMS:
        raise CommonProductContentError(
            "За один раз можно изменить не больше 50 товаров",
            status_code=413,
            code="too_many_items",
        )
    parsed = []
    seen = set()
    recipient_total = 0
    for index, item in enumerate(raw_items):
        if not isinstance(item, dict) or set(item) != {
            "product_id", "expected_content_edit_version", "changes", "recipients"
        }:
            raise CommonProductContentError(f"Строка {index + 1}: неверные поля")
        product_id = item["product_id"]
        version = item["expected_content_edit_version"]
        if isinstance(product_id, bool) or not isinstance(product_id, int) or product_id <= 0:
            raise CommonProductContentError(f"Строка {index + 1}: неверный ID товара")
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise CommonProductContentError(f"Товар #{product_id}: версия должна быть положительным целым числом")
        if product_id in seen:
            raise CommonProductContentError("Список товаров содержит повтор")
        seen.add(product_id)
        changes = _parse_changes(item["changes"])
        references = item["recipients"]
        if not isinstance(references, list) or len(references) > MAX_RECIPIENTS_PER_ITEM:
            raise CommonProductContentError(f"Товар #{product_id}: выберите до {MAX_RECIPIENTS_PER_ITEM} контекстов каналов")
        parsed_refs = [_parse_reference(ref) for ref in references]
        if len({(ref["kind"], ref["id"]) for ref in parsed_refs}) != len(parsed_refs):
            raise CommonProductContentError(f"Товар #{product_id}: контекст канала повторяется")
        recipient_total += len(parsed_refs)
        parsed.append({
            "product_id": product_id,
            "expected_content_edit_version": version,
            "changes": changes,
            "recipients": parsed_refs,
        })
    if recipient_total > MAX_RECIPIENTS_TOTAL:
        raise CommonProductContentError(
            f"В одном просмотре допускается не больше {MAX_RECIPIENTS_TOTAL} контекстов каналов",
            status_code=413,
            code="too_many_recipients",
        )
    return parsed


class CommonProductContentService:
    @classmethod
    def _owned_products(cls, *, seller_id: int, product_ids: list[int]) -> dict[int, ImportedProduct]:
        products = ImportedProduct.query.options(
            joinedload(ImportedProduct.supplier_product),
            joinedload(ImportedProduct.product),
        ).filter(
            ImportedProduct.seller_id == seller_id,
            ImportedProduct.id.in_(product_ids),
        ).all()
        by_id = {product.id: product for product in products}
        if set(by_id) != set(product_ids):
            raise CommonProductContentNotFound("Общий товар не найден")
        return by_id

    @classmethod
    def read_many(cls, *, seller_id: int, product_ids: list[int]) -> list[dict]:
        if (
            not isinstance(product_ids, list)
            or not product_ids
            or len(product_ids) > MAX_ITEMS
            or len(set(product_ids)) != len(product_ids)
            or any(isinstance(product_id, bool) or not isinstance(product_id, int) or product_id <= 0 for product_id in product_ids)
        ):
            raise CommonProductContentError("Нужно передать от 1 до 50 уникальных товаров")
        products = cls._owned_products(seller_id=seller_id, product_ids=product_ids)
        return [_public_state(products[product_id], seller_id) for product_id in product_ids]

    @classmethod
    def preview(cls, *, seller_id: int, user_id: int, raw_items: Any) -> dict:
        items = _parse_items(raw_items)
        product_ids = [item["product_id"] for item in items]
        products = cls._owned_products(seller_id=seller_id, product_ids=product_ids)
        token_items = []
        result_items = []
        for item in items:
            product = products[item["product_id"]]
            version = int(product.content_edit_version or 1)
            if version != item["expected_content_edit_version"]:
                raise CommonProductContentConflict(f"Товар #{product.id} изменился; обновите данные и проверьте diff заново")
            overrides = _parse_overrides(product)
            source = _source_state(product, overrides)
            before = {field: _read_column(product, field) for field in FIELD_COLUMNS}
            inherited = source["values"]
            after = dict(before)
            field_diffs = []
            changed_fields = set()
            current_overrides = overrides["fields"]
            for field, change in item["changes"].items():
                old_value = before[field]
                is_overridden = field in current_overrides
                if change["mode"] == "inherit":
                    if not is_overridden:
                        raise CommonProductContentError(f"Товар #{product.id}: поле «{field}» уже наследуется")
                    value = inherited[field]
                    after[field] = value
                else:
                    value = change["value"]
                    if field == "photos":
                        photo_pool = {
                            option["url"] for option in _photo_options(product)
                            if option["available_for_selection"]
                        } | set(before["photos"])
                        if not set(value).issubset(photo_pool):
                            raise CommonProductContentError(f"Товар #{product.id}: выберите только фотографии из доступного списка")
                    if is_overridden and value == old_value:
                        raise CommonProductContentError(f"Товар #{product.id}: поле «{field}» уже содержит это значение")
                    after[field] = value
                changed_fields.add(field)
                field_diffs.append({
                    "field": field,
                    "before": old_value,
                    "inherited": inherited[field],
                    "after": value,
                    "before_origin": "seller_override" if is_overridden else source["field_origins"].get(field, "unknown"),
                    "after_origin": source["field_origins"].get(field, "unknown") if change["mode"] == "inherit" else "seller_override",
                    "changed": old_value != value or change["mode"] != ("override" if is_overridden else "inherit"),
                })

            records = _recipient_records(seller_id, product)
            available = {(row["ref"]["kind"], row["ref"]["id"]): row for row in records}
            chosen = []
            for ref in item["recipients"]:
                record = available.get((ref["kind"], ref["id"]))
                if record is None:
                    raise CommonProductContentNotFound("Контекст канала не найден")
                chosen.append(record)
            output_recipients = []
            for record in chosen:
                output_recipients.append({
                    **{key: value for key, value in record.items() if key not in {"object", "fingerprint"}},
                    "revision_fingerprint": record["fingerprint"],
                    "diff": _recipient_diff(record, after, changed_fields),
                    "notice": "Сохранение обновит только общий товар. Этот черновик или карточка останется без изменений.",
                })
            token_items.append({
                "product_id": product.id,
                "expected_content_edit_version": version,
                "source_fingerprint": source["fingerprint"],
                "current_fingerprint": _current_fingerprint(product, overrides),
                "changes": item["changes"],
                "recipients": [row["ref"] for row in chosen],
                "recipient_fingerprints": [row["fingerprint"] for row in chosen],
            })
            result_items.append({
                "product_id": product.id,
                "before_version": version,
                "after_version": version + 1,
                "source": {key: value for key, value in source.items() if key != "values"},
                "fields": field_diffs,
                "recipients": output_recipients,
            })

        from flask import current_app
        from itsdangerous import URLSafeTimedSerializer

        token = URLSafeTimedSerializer(
            current_app.config["SECRET_KEY"],
            salt="seller-hub-common-content-preview-v1",
        ).dumps({
            "version": SCHEMA_VERSION,
            "seller_id": seller_id,
            "user_id": user_id,
            "items": token_items,
        })
        if len(token.encode("utf-8")) > MAX_PREVIEW_TOKEN_BYTES:
            raise CommonProductContentError(
                "Просмотр слишком велик для безопасного токена. Сократите текст или разделите товары.",
                status_code=413,
                code="preview_too_large",
            )
        return {"items": result_items, "preview_token": token, "expires_in_seconds": 600}

    @classmethod
    def apply(cls, *, seller_id: int, user_id: int, token: Any) -> list[dict]:
        if not isinstance(token, str) or not token or len(token.encode("utf-8")) > MAX_PREVIEW_TOKEN_BYTES:
            raise CommonProductContentError("Предпросмотр сохранения отсутствует или слишком велик")
        from flask import current_app
        from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

        serializer = URLSafeTimedSerializer(
            current_app.config["SECRET_KEY"],
            salt="seller-hub-common-content-preview-v1",
        )
        try:
            document = serializer.loads(token, max_age=600)
        except SignatureExpired:
            raise CommonProductContentConflict("Предпросмотр устарел. Откройте его заново.", code="preview_expired") from None
        except BadSignature:
            raise CommonProductContentError("Подпись предпросмотра неверна") from None
        if (
            not isinstance(document, dict)
            or set(document) != {"version", "seller_id", "user_id", "items"}
            or document.get("version") != SCHEMA_VERSION
            or document.get("seller_id") != seller_id
            or document.get("user_id") != user_id
            or not isinstance(document.get("items"), list)
            or not 1 <= len(document["items"]) <= MAX_ITEMS
        ):
            raise CommonProductContentConflict("Предпросмотр создан для другого продавца или пользователя")
        token_items = document["items"]
        ids = [item.get("product_id") for item in token_items if isinstance(item, dict)]
        if (
            len(ids) != len(token_items)
            or any(isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0 for pid in ids)
            or len(ids) != len(set(ids))
        ):
            raise CommonProductContentError("Предпросмотр имеет неверный набор товаров")
        products = cls._owned_products(seller_id=seller_id, product_ids=ids)
        plans = []
        for item in token_items:
            if set(item) != {
                "product_id", "expected_content_edit_version", "source_fingerprint",
                "current_fingerprint", "changes", "recipients", "recipient_fingerprints",
            }:
                raise CommonProductContentError("Предпросмотр имеет неизвестный формат")
            product = products[item["product_id"]]
            version = int(product.content_edit_version or 1)
            if version != item["expected_content_edit_version"]:
                raise CommonProductContentConflict(f"Товар #{product.id} изменился после предпросмотра; проверьте diff заново")
            overrides = _parse_overrides(product)
            source = _source_state(product, overrides)
            if source["fingerprint"] != item["source_fingerprint"]:
                raise CommonProductContentConflict(f"Источник товара #{product.id} изменился после предпросмотра")
            if _current_fingerprint(product, overrides) != item["current_fingerprint"]:
                raise CommonProductContentConflict(f"Общий товар #{product.id} изменился после предпросмотра")

            changes = _parse_changes(item["changes"])
            refs = [_parse_reference(ref) for ref in item["recipients"]]
            records = _recipient_records(seller_id, product)
            available = {(row["ref"]["kind"], row["ref"]["id"]): row for row in records}
            chosen = [available.get((ref["kind"], ref["id"])) for ref in refs]
            if any(record is None for record in chosen):
                raise CommonProductContentConflict(f"Контекст канала для товара #{product.id} изменился; проверьте diff заново")
            if [record["fingerprint"] for record in chosen] != item["recipient_fingerprints"]:
                raise CommonProductContentConflict(f"Контекст канала для товара #{product.id} изменился; проверьте diff заново")

            before = {field: _read_column(product, field) for field in FIELD_COLUMNS}
            after = dict(before)
            overrides_after = json.loads(_stable_json(overrides))
            raw_values = {}
            for field, change in changes.items():
                if change["mode"] == "inherit":
                    if field not in overrides_after["fields"]:
                        raise CommonProductContentConflict(f"Поле «{field}» товара #{product.id} уже не переопределено")
                    after[field] = inherited_value(product, field, overrides)
                    raw_values[field] = _serialize_field(field, after[field])
                    overrides_after["fields"].pop(field, None)
                else:
                    if field == "photos":
                        photo_pool = {
                            option["url"] for option in _photo_options(product)
                            if option["available_for_selection"]
                        } | set(_read_column(product, "photos"))
                        if not set(change["value"]).issubset(photo_pool):
                            raise CommonProductContentConflict(f"Фото товара #{product.id} больше не доступно; откройте предпросмотр заново")
                    new_value = normalize_value(field, change["value"])
                    overrides_after["fields"][field] = {
                        "value": new_value,
                        "inherited_value": inherited_value(product, field, overrides),
                        "inherited_origin": source["field_origins"].get(field, "unknown"),
                        "edited_by_user_id": user_id,
                        "edited_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                        "edit_version": version + 1,
                    }
                    after[field] = new_value
                    raw_values[field] = _serialize_field(field, new_value)

            raw_overrides = _stable_json(overrides_after) if overrides_after["fields"] else None
            if raw_overrides and len(raw_overrides.encode("utf-8")) > MAX_BODY_BYTES:
                raise CommonProductContentError("Слишком большой набор общих переопределений", status_code=413)
            plans.append({
                "product": product,
                "before": before,
                "after": after,
                "raw_values": raw_values,
                "raw_overrides": raw_overrides,
                "expected_version": version,
                "next_version": version + 1,
            })

        savepoint = db.session.begin_nested()
        try:
            applied = []
            for plan in plans:
                product = plan["product"]
                values = {
                    FIELD_COLUMNS[field]: value
                    for field, value in plan["raw_values"].items()
                }
                values.update({
                    "content_overrides_json": plan["raw_overrides"],
                    "content_edit_version": plan["next_version"],
                    "updated_at": datetime.utcnow(),
                })
                conditions = [
                    ImportedProduct.id == product.id,
                    ImportedProduct.seller_id == seller_id,
                    ImportedProduct.source_type.is_(None) if product.source_type is None else ImportedProduct.source_type == product.source_type,
                    ImportedProduct.external_id.is_(None) if product.external_id is None else ImportedProduct.external_id == product.external_id,
                    ImportedProduct.supplier_id.is_(None) if product.supplier_id is None else ImportedProduct.supplier_id == product.supplier_id,
                    ImportedProduct.supplier_product_id.is_(None) if product.supplier_product_id is None else ImportedProduct.supplier_product_id == product.supplier_product_id,
                    ImportedProduct.content_edit_version == plan["expected_version"],
                    ImportedProduct.content_overrides_json.is_(None)
                    if product.content_overrides_json is None
                    else ImportedProduct.content_overrides_json == product.content_overrides_json,
                ]
                for field in FIELD_COLUMNS:
                    column = getattr(ImportedProduct, FIELD_COLUMNS[field])
                    old_value = _raw_column_value(product, field)
                    conditions.append(column.is_(None) if old_value is None else column == old_value)
                if product.supplier_product is None:
                    conditions.append(
                        ImportedProduct.original_data.is_(None)
                        if product.original_data is None
                        else ImportedProduct.original_data == product.original_data
                    )
                else:
                    supplier = product.supplier_product
                    conditions.append(ImportedProduct.supplier_product.has(and_(
                        SupplierProduct.supplier_id == supplier.supplier_id,
                        SupplierProduct.content_revision == supplier.content_revision,
                        SupplierProduct.updated_at.is_(None)
                        if supplier.updated_at is None else SupplierProduct.updated_at == supplier.updated_at,
                        SupplierProduct.title.is_(None)
                        if supplier.title is None else SupplierProduct.title == supplier.title,
                        SupplierProduct.description.is_(None)
                        if supplier.description is None else SupplierProduct.description == supplier.description,
                        SupplierProduct.ai_description.is_(None)
                        if supplier.ai_description is None else SupplierProduct.ai_description == supplier.ai_description,
                        SupplierProduct.description_source.is_(None)
                        if supplier.description_source is None else SupplierProduct.description_source == supplier.description_source,
                        SupplierProduct.characteristics_json.is_(None)
                        if supplier.characteristics_json is None else SupplierProduct.characteristics_json == supplier.characteristics_json,
                        SupplierProduct.ai_marketplace_json.is_(None)
                        if supplier.ai_marketplace_json is None else SupplierProduct.ai_marketplace_json == supplier.ai_marketplace_json,
                        SupplierProduct.photo_urls_json.is_(None)
                        if supplier.photo_urls_json is None else SupplierProduct.photo_urls_json == supplier.photo_urls_json,
                        SupplierProduct.original_data_json.is_(None)
                        if supplier.original_data_json is None else SupplierProduct.original_data_json == supplier.original_data_json,
                    )))
                rowcount = db.session.execute(
                    update(ImportedProduct).where(*conditions).values(**values)
                ).rowcount
                if rowcount != 1:
                    raise CommonProductContentConflict(f"Общий товар #{product.id} изменился во время сохранения")
                db.session.add(AgentChangeSnapshot(
                    task_id=None,
                    imported_product_id=product.id,
                    agent_id="seller-common-content-v1",
                    previous_values=_stable_json(plan["before"]),
                    new_values=_stable_json({
                        **plan["after"],
                        "__seller_common_content_audit": {
                            "actor_user_id": user_id,
                            "content_edit_version": plan["next_version"],
                        },
                    }),
                ))
                applied.append({
                    "product_id": product.id,
                    "content_edit_version": plan["next_version"],
                    "changed_fields": sorted(plan["raw_values"]),
                    "message": "Общий товар сохранён. Черновики и карточки на площадках не изменены.",
                })
            db.session.flush()
            db.session.expire_all()
            for item in token_items:
                product = cls._owned_products(
                    seller_id=seller_id,
                    product_ids=[item["product_id"]],
                )[item["product_id"]]
                current_overrides = _parse_overrides(product)
                if _source_state(product, current_overrides)["fingerprint"] != item["source_fingerprint"]:
                    raise CommonProductContentConflict(
                        f"Источник товара #{product.id} изменился во время сохранения; повторите просмотр"
                    )
                current_records = _recipient_records(seller_id, product)
                current_by_key = {
                    (row["ref"]["kind"], row["ref"]["id"]): row
                    for row in current_records
                }
                checked_records = [
                    current_by_key.get((ref["kind"], ref["id"]))
                    for ref in item["recipients"]
                ]
                if (
                    any(row is None for row in checked_records)
                    or [row["fingerprint"] for row in checked_records]
                    != item["recipient_fingerprints"]
                ):
                    raise CommonProductContentConflict(
                        f"Контекст канала для товара #{product.id} изменился во время сохранения; повторите просмотр"
                    )
            savepoint.commit()
            return applied
        except Exception:
            savepoint.rollback()
            raise


def refresh_from_source(
    product: ImportedProduct,
    source_values: dict,
    *,
    provenance: dict[str, str] | None = None,
) -> None:
    """Update inherited common columns while preserving active seller values."""
    if not isinstance(product, ImportedProduct) or not isinstance(source_values, dict):
        raise TypeError("source refresh requires an ImportedProduct and field map")
    if set(source_values) - set(FIELD_COLUMNS):
        raise ValueError("source refresh contains an unsupported common field")
    provenance = provenance or {}
    if set(provenance) - set(source_values) or any(
        value not in {"source", "ai_suggestion", "supplier_enrichment", "unknown"}
        for value in provenance.values()
    ):
        raise ValueError("source refresh contains invalid field provenance")
    overrides = _parse_overrides(product)
    original = _load_json(product.original_data, {})
    if not isinstance(original, dict):
        original = {}
    source_keys = {
        "title": "title",
        "description": "description",
        "photos": "photo_urls",
        "characteristics": "characteristics",
    }
    source_snapshot_changed = False
    for field, incoming in source_values.items():
        normalized = _inherited_normalize(field, incoming)
        origin = provenance.get(field, "unknown")
        if origin == "source":
            observed = incoming
            if field in {"photos", "characteristics"} and isinstance(incoming, str):
                observed = _load_json(incoming, None)
                if observed is None:
                    observed = incoming
            if _safe_json_value(observed):
                original[source_keys[field]] = observed
                source_snapshot_changed = True
        if field in overrides["fields"]:
            overrides["fields"][field]["inherited_value"] = normalized
            overrides["fields"][field]["inherited_origin"] = origin
            continue
        if field in {"photos", "characteristics"}:
            raw_for_column = incoming if isinstance(incoming, str) else _stable_json(incoming)
        else:
            raw_for_column = incoming if isinstance(incoming, str) else ""
        setattr(product, FIELD_COLUMNS[field], raw_for_column)
    if source_snapshot_changed:
        product.original_data = _stable_json(original)
    product.content_overrides_json = _stable_json(overrides) if overrides["fields"] else None
