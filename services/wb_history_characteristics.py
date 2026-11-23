"""Read-only legacy WB characteristic labels for seller history display."""

from collections import defaultdict
from collections.abc import Mapping

from models import (
    Marketplace,
    MarketplaceCategory,
    MarketplaceCategoryCharacteristic,
)
from services.wb_edit_review import schema_for_subject


def _positive_id(value):
    if type(value) is int and value > 0:
        return value
    if isinstance(value, str) and value.isascii() and value.isdecimal():
        parsed = int(value)
        return parsed if parsed > 0 else None
    return None


def _has_saved_name(item):
    for key in ("name", "charcName"):
        value = item.get(key)
        if value is None or value == "":
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return True
    return False


def _add_current_names(items, names_by_id):
    if not isinstance(items, list):
        return items, False

    display_items = []
    used_current_name = False
    for item in items:
        if not isinstance(item, Mapping):
            display_items.append(item)
            continue
        display_item = dict(item)
        characteristic_id = _positive_id(display_item.get("id"))
        current_name = names_by_id.get(characteristic_id)
        if (
            current_name
            and not _has_saved_name(display_item)
        ):
            display_item["name"] = current_name
            used_current_name = True
        display_items.append(display_item)
    return display_items, used_current_name


def present_legacy_wb_characteristics(product_changes, seller_id):
    """Add current cached labels to copied legacy snapshots when references agree.

    This reads only the local WB cache. It never changes stored snapshots or
    accesses the provider. Labels are used only when the seller-owned Product
    has a typed subject, both saved category names match its current category,
    the exact available category name is unique, and the current schema is
    usable through the normal WB reference freshness checks.
    """
    if type(seller_id) is not int or seller_id <= 0:
        return {}

    candidates = []
    category_names = set()
    for change in product_changes or ():
        product = getattr(change, "product", None)
        change_id = getattr(change, "id", None)
        product_id = getattr(change, "product_id", None)
        raw_subject_id = getattr(product, "subject_id", None)
        if (
            type(change_id) is not int or change_id <= 0
            or type(product_id) is not int or product_id <= 0
            or type(getattr(change, "seller_id", None)) is not int
            or change.seller_id != seller_id
            or product is None
            or type(getattr(product, "id", None)) is not int
            or product.id != product_id
            or type(getattr(product, "seller_id", None)) is not int
            or product.seller_id != seller_id
            or type(raw_subject_id) is not int
            or raw_subject_id <= 0
        ):
            continue
        subject_id = raw_subject_id

        changed_fields = getattr(change, "changed_fields", None)
        if (
            not isinstance(changed_fields, (list, tuple))
            or "characteristics" not in changed_fields
        ):
            continue

        before_snapshot = getattr(change, "snapshot_before", None)
        after_snapshot = getattr(change, "snapshot_after", None)
        if (
            not isinstance(before_snapshot, Mapping)
            or not isinstance(after_snapshot, Mapping)
        ):
            continue

        category_name = getattr(product, "object_name", None)
        if (
            not isinstance(category_name, str)
            or not category_name.strip()
            or before_snapshot.get("object_name") != category_name
            or after_snapshot.get("object_name") != category_name
        ):
            continue

        before_items = before_snapshot.get("characteristics")
        after_items = after_snapshot.get("characteristics")
        if not isinstance(before_items, list) or not isinstance(after_items, list):
            continue

        missing_name_ids = set()
        for item in before_items + after_items:
            if not isinstance(item, Mapping) or _has_saved_name(item):
                continue
            characteristic_id = _positive_id(item.get("id"))
            if characteristic_id is not None:
                missing_name_ids.add(characteristic_id)
        if not missing_name_ids:
            continue

        candidates.append({
            "change": change,
            "subject_id": subject_id,
            "category_name": category_name,
            "before_items": before_items,
            "after_items": after_items,
        })
        category_names.add(category_name)

    if not candidates:
        return {}

    try:
        marketplace = Marketplace.query.filter_by(code="wb").first()
    except Exception:
        return {}
    if marketplace is None or getattr(marketplace, "is_active", False) is not True:
        return {}

    try:
        cached_categories = MarketplaceCategory.query.filter(
            MarketplaceCategory.marketplace_id == marketplace.id,
            MarketplaceCategory.is_available.is_(True),
            MarketplaceCategory.subject_name.in_(category_names),
        ).all()
    except Exception:
        return {}
    categories_by_exact_name = defaultdict(list)
    for category in cached_categories:
        name = getattr(category, "subject_name", None)
        if (
            isinstance(name, str)
            and name in category_names
            and getattr(category, "marketplace_id", None) == marketplace.id
            and getattr(category, "is_available", False) is True
        ):
            categories_by_exact_name[name].append(category)

    validation_cache = {}
    schemas_by_subject = {}
    validated_candidates = []
    for candidate in candidates:
        category_matches = categories_by_exact_name.get(
            candidate["category_name"],
        ) or []
        if len(category_matches) != 1:
            continue
        category = category_matches[0]
        if (
            type(getattr(category, "subject_id", None)) is not int
            or category.subject_id != candidate["subject_id"]
        ):
            continue

        subject_id = candidate["subject_id"]
        if subject_id not in schemas_by_subject:
            try:
                schemas_by_subject[subject_id] = schema_for_subject(
                    subject_id,
                    validation_cache=validation_cache,
                )
            except Exception:
                schemas_by_subject[subject_id] = None
        schema = schemas_by_subject[subject_id]
        if (
            not isinstance(schema, Mapping)
            or schema.get("usable") is not True
            or type(schema.get("subject_id")) is not int
            or schema.get("subject_id") != subject_id
            or schema.get("subject_name") != candidate["category_name"]
        ):
            continue

        category_id = getattr(category, "id", None)
        if type(category_id) is not int or category_id <= 0:
            continue
        candidate["category_id"] = category_id
        validated_candidates.append(candidate)

    category_ids = {candidate["category_id"] for candidate in validated_candidates}
    if not category_ids:
        return {}

    try:
        cached_characteristics = MarketplaceCategoryCharacteristic.query.filter(
            MarketplaceCategoryCharacteristic.marketplace_id == marketplace.id,
            MarketplaceCategoryCharacteristic.category_id.in_(category_ids),
            MarketplaceCategoryCharacteristic.is_available.is_(True),
        ).all()
    except Exception:
        return {}

    names_by_category = defaultdict(dict)
    duplicate_ids_by_category = defaultdict(set)
    for characteristic in cached_characteristics:
        category_id = getattr(characteristic, "category_id", None)
        characteristic_id = getattr(characteristic, "charc_id", None)
        name = getattr(characteristic, "name", None)
        if (
            type(category_id) is not int or category_id not in category_ids
            or type(getattr(characteristic, "marketplace_id", None)) is not int
            or characteristic.marketplace_id != marketplace.id
            or getattr(characteristic, "is_available", False) is not True
            or type(characteristic_id) is not int or characteristic_id <= 0
            or not isinstance(name, str) or not name.strip()
        ):
            continue
        if characteristic_id in names_by_category[category_id]:
            duplicate_ids_by_category[category_id].add(characteristic_id)
        else:
            names_by_category[category_id][characteristic_id] = name.strip()
    for category_id, duplicate_ids in duplicate_ids_by_category.items():
        for characteristic_id in duplicate_ids:
            names_by_category[category_id].pop(characteristic_id, None)

    presentations = {}
    for candidate in validated_candidates:
        names_by_id = names_by_category.get(candidate["category_id"], {})

        before_display, before_used = _add_current_names(
            candidate["before_items"], names_by_id,
        )
        after_display, after_used = _add_current_names(
            candidate["after_items"], names_by_id,
        )
        if before_used or after_used:
            presentations[candidate["change"].id] = {
                "before": before_display,
                "after": after_display,
                "source_label": "По сохранённому справочнику WB",
            }

    return presentations
