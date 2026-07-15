"""Fail-closed Ozon taxonomy, type, attribute and dictionary synchronization."""

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
import fcntl
import hashlib
import json
import os
import tempfile
import unicodedata

from sqlalchemy.orm import joinedload
from sqlalchemy import or_

from models import (
    Marketplace,
    MarketplaceAttributeDefinition,
    MarketplaceAttributeValue,
    MarketplaceCredentialEncryptionError,
    MarketplaceProductType,
    MarketplaceReferenceAccount,
    MarketplaceTaxonomyCategory,
    db,
)
from services.marketplace_adapters import (
    MarketplaceCredentials,
    get_marketplace_registry,
)
from services.ozon_api_client import OzonAPIError


class OzonReferenceError(RuntimeError):
    pass


class OzonReferenceValidationError(OzonReferenceError):
    pass


_UNSET = object()


class OzonReferenceService:
    HARD_TTL_HOURS = 48
    TREE_REFRESH_AFTER_HOURS = 24
    TREE_MAX_DEPTH = 20
    TREE_MAX_NODES = 100_000
    CATEGORY_SHRINK_GUARD_MIN = 50
    TYPE_SHRINK_GUARD_MIN = 100
    SHRINK_GUARD_RATIO = 0.60
    ATTRIBUTE_SHRINK_GUARD_MIN = 8
    ATTRIBUTE_SHRINK_GUARD_RATIO = 0.50
    ATTRIBUTE_MAX_ITEMS = 5_000
    VALUES_PAGE_SIZE = 5_000
    VALUES_MAX_PAGES = 100
    VALUES_MAX_ITEMS = 200_000
    VALUE_SHRINK_GUARD_MIN = 20
    VALUE_SHRINK_GUARD_RATIO = 0.50

    @staticmethod
    def _stable_json(value: Any) -> str:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @classmethod
    def _hash(cls, value: Any) -> str:
        return hashlib.sha256(cls._stable_json(value).encode("utf-8")).hexdigest()

    @staticmethod
    def _integer(value: Any, field_name: str, *, minimum: int = 1) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
            raise OzonReferenceValidationError(
                f"Ozon {field_name} must be an integer >= {minimum}"
            )
        return value

    @staticmethod
    def _boolean(value: Any, field_name: str) -> bool:
        if not isinstance(value, bool):
            raise OzonReferenceValidationError(
                f"Ozon {field_name} must be a boolean"
            )
        return value

    @staticmethod
    def _text(
        value: Any,
        field_name: str,
        *,
        maximum: int,
        allow_empty: bool = False,
    ) -> str:
        if not isinstance(value, str):
            raise OzonReferenceValidationError(
                f"Ozon {field_name} must be a string"
            )
        value = value.strip()
        if not value and not allow_empty:
            raise OzonReferenceValidationError(
                f"Ozon {field_name} must be non-empty"
            )
        if len(value) > maximum:
            raise OzonReferenceValidationError(
                f"Ozon {field_name} exceeds {maximum} characters"
            )
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise OzonReferenceValidationError(
                f"Ozon {field_name} contains control characters"
            )
        return value

    @classmethod
    def _optional_text(
        cls,
        value: Any,
        field_name: str,
        *,
        maximum: int,
    ) -> Optional[str]:
        if value in (None, ""):
            return None
        return cls._text(value, field_name, maximum=maximum)

    @classmethod
    def _external_optional_integer(
        cls,
        value: Any,
        field_name: str,
    ) -> Optional[str]:
        if value in (None, 0):
            return None
        return str(cls._integer(value, field_name))

    @staticmethod
    def _result_list(response: Any, reference_name: str) -> list:
        if not isinstance(response, dict):
            raise OzonReferenceValidationError(
                f"Ozon {reference_name} response must be an object"
            )
        result = response.get("result")
        if not isinstance(result, list):
            raise OzonReferenceValidationError(
                f"Ozon {reference_name} response has no result list"
            )
        return result

    @staticmethod
    def _try_claim(scope: str, local_id: int):
        directory = os.path.join(
            tempfile.gettempdir(),
            "seller-hub-ozon-reference-locks",
        )
        os.makedirs(directory, mode=0o700, exist_ok=True)
        path = os.path.join(directory, f"{scope}-{int(local_id)}.lock")
        lock_file = open(path, "a+", encoding="ascii")
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock_file.close()
            return None
        return lock_file

    @staticmethod
    def _release_claim(lock_file) -> None:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        finally:
            lock_file.close()

    @staticmethod
    def _marketplace(marketplace_id: int) -> Marketplace:
        if (
            not isinstance(marketplace_id, int)
            or isinstance(marketplace_id, bool)
            or marketplace_id <= 0
        ):
            raise OzonReferenceError("marketplace_id must be a positive integer")
        marketplace = Marketplace.query.filter_by(
            id=marketplace_id,
            code="ozon",
            is_active=True,
        ).first()
        if marketplace is None:
            raise OzonReferenceError("Active Ozon marketplace not found")
        return marketplace

    @classmethod
    def _adapter_credentials(
        cls,
        marketplace: Marketplace,
        *,
        adapter=None,
        credentials: Optional[MarketplaceCredentials] = None,
        now: Optional[datetime] = None,
    ) -> Tuple[Any, MarketplaceCredentials]:
        if adapter is not None or credentials is not None:
            if adapter is None or credentials is None:
                raise OzonReferenceError(
                    "adapter and credentials must be injected together"
                )
            return adapter, credentials

        reference = MarketplaceReferenceAccount.query.filter_by(
            marketplace_id=marketplace.id,
        ).first()
        if reference is None or not reference.has_credentials:
            raise OzonReferenceError("Ozon reference account is not configured")
        if reference.connection_status != "connected":
            raise OzonReferenceError(
                "Ozon reference account must pass connection check"
            )
        current_time = now or datetime.utcnow()
        if (
            reference.credential_expires_at
            and reference.credential_expires_at <= current_time
        ):
            raise OzonReferenceError("Ozon reference credential has expired")
        try:
            secret = reference.get_credentials()
            resolved = MarketplaceCredentials(
                external_account_id=reference.external_account_id,
                api_key=secret["api_key"],
            )
            del secret
        except (KeyError, ValueError, MarketplaceCredentialEncryptionError):
            raise OzonReferenceError(
                "Ozon reference credentials cannot be read"
            ) from None
        return get_marketplace_registry().get("ozon"), resolved

    @classmethod
    def normalize_tree(cls, response: Any) -> Dict[str, Any]:
        roots = cls._result_list(response, "description category tree")
        if not roots:
            raise OzonReferenceValidationError(
                "Ozon description category tree is empty"
            )
        categories: List[Dict[str, Any]] = []
        product_types: List[Dict[str, Any]] = []
        category_ids = set()
        type_pairs = set()
        total_nodes = 0

        def visit(
            node: Any,
            *,
            parent_category_id: Optional[str],
            path: Tuple[str, ...],
            depth: int,
            ancestor_disabled: bool,
        ) -> None:
            nonlocal total_nodes
            total_nodes += 1
            if total_nodes > cls.TREE_MAX_NODES:
                raise OzonReferenceValidationError(
                    "Ozon category tree exceeds the node safety limit"
                )
            if depth > cls.TREE_MAX_DEPTH:
                raise OzonReferenceValidationError(
                    "Ozon category tree exceeds the depth safety limit"
                )
            if not isinstance(node, dict):
                raise OzonReferenceValidationError(
                    "Ozon category tree node must be an object"
                )
            disabled = cls._boolean(node.get("disabled"), "tree.disabled")
            available = not (ancestor_disabled or disabled)
            children = node.get("children")
            if not isinstance(children, list):
                raise OzonReferenceValidationError(
                    "Ozon category tree children must be a list"
                )
            has_category = "description_category_id" in node
            has_type = "type_id" in node
            if has_category == has_type:
                raise OzonReferenceValidationError(
                    "Ozon tree node must contain exactly one category/type identity"
                )

            if has_category:
                external_id = str(cls._integer(
                    node.get("description_category_id"),
                    "description_category_id",
                ))
                name = cls._text(
                    node.get("category_name"),
                    "category_name",
                    maximum=500,
                )
                if external_id in category_ids:
                    raise OzonReferenceValidationError(
                        f"Ozon tree duplicated description_category_id {external_id}"
                    )
                category_ids.add(external_id)
                full_path_parts = path + (name,)
                full_path = " / ".join(full_path_parts)
                if len(full_path) > 2000:
                    raise OzonReferenceValidationError(
                        "Ozon category path exceeds 2000 characters"
                    )
                categories.append({
                    "external_category_id": external_id,
                    "parent_external_category_id": parent_category_id,
                    "name": name,
                    "full_path": full_path,
                    "depth": depth,
                    "disabled": disabled,
                    "available": available,
                })
                for child in children:
                    visit(
                        child,
                        parent_category_id=external_id,
                        path=full_path_parts,
                        depth=depth + 1,
                        ancestor_disabled=not available,
                    )
                return

            if parent_category_id is None:
                raise OzonReferenceValidationError(
                    "Ozon product type has no parent description category"
                )
            external_type_id = str(cls._integer(node.get("type_id"), "type_id"))
            type_name = cls._text(
                node.get("type_name"),
                "type_name",
                maximum=500,
            )
            if children:
                raise OzonReferenceValidationError(
                    "Ozon product type node unexpectedly contains children"
                )
            pair = (parent_category_id, external_type_id)
            if pair in type_pairs:
                raise OzonReferenceValidationError(
                    "Ozon tree duplicated category/type pair"
                )
            type_pairs.add(pair)
            product_types.append({
                "external_category_id": parent_category_id,
                "external_type_id": external_type_id,
                "name": type_name,
                "disabled": disabled,
                "available": available,
            })

        for root in roots:
            visit(
                root,
                parent_category_id=None,
                path=(),
                depth=0,
                ancestor_disabled=False,
            )
        if not categories or not product_types:
            raise OzonReferenceValidationError(
                "Ozon tree must contain categories and product types"
            )
        canonical = {
            "categories": categories,
            "product_types": product_types,
        }
        canonical["snapshot_hash"] = cls._hash({
            "categories": sorted(
                categories,
                key=lambda item: item["external_category_id"],
            ),
            "product_types": sorted(
                product_types,
                key=lambda item: (
                    item["external_category_id"],
                    item["external_type_id"],
                ),
            ),
        })
        return canonical

    @classmethod
    def sync_tree(
        cls,
        marketplace_id: int,
        *,
        adapter=None,
        credentials: Optional[MarketplaceCredentials] = None,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        marketplace = cls._marketplace(marketplace_id)
        lock_file = cls._try_claim("tree", marketplace.id)
        if lock_file is None:
            return {
                "success": False,
                "skipped": True,
                "error": "Ozon category tree sync is already running",
            }
        synced_at = now or datetime.utcnow()
        marketplace.categories_sync_status = "running"
        marketplace.categories_sync_error = None
        db.session.commit()
        try:
            adapter, credentials = cls._adapter_credentials(
                marketplace,
                adapter=adapter,
                credentials=credentials,
                now=synced_at,
            )
            response = adapter.fetch_category_tree(
                credentials,
                {"language": "DEFAULT"},
            )
            snapshot = cls.normalize_tree(response)
            existing_categories = {
                category.external_category_id: category
                for category in MarketplaceTaxonomyCategory.query.filter_by(
                    marketplace_id=marketplace.id,
                ).all()
            }
            existing_types = {
                (item.category.external_category_id, item.external_type_id): item
                for item in MarketplaceProductType.query.options(
                    joinedload(MarketplaceProductType.category),
                ).filter_by(marketplace_id=marketplace.id).all()
            }
            previous_categories = sum(
                1 for item in existing_categories.values() if item.is_available
            )
            previous_types = sum(
                1 for item in existing_types.values() if item.is_available
            )
            available_categories = sum(
                1 for item in snapshot["categories"] if item["available"]
            )
            available_types = sum(
                1 for item in snapshot["product_types"] if item["available"]
            )
            cls._guard_shrink(
                "category",
                previous_categories,
                available_categories,
                cls.CATEGORY_SHRINK_GUARD_MIN,
                cls.SHRINK_GUARD_RATIO,
            )
            cls._guard_shrink(
                "product type",
                previous_types,
                available_types,
                cls.TYPE_SHRINK_GUARD_MIN,
                cls.SHRINK_GUARD_RATIO,
            )

            added_categories = 0
            updated_categories = 0
            seen_categories = set()
            parent_by_category = {}
            for item in snapshot["categories"]:
                external_id = item["external_category_id"]
                seen_categories.add(external_id)
                category = existing_categories.get(external_id)
                if category is None:
                    category = MarketplaceTaxonomyCategory(
                        marketplace_id=marketplace.id,
                        external_category_id=external_id,
                        name=item["name"],
                        full_path=item["full_path"],
                        depth=item["depth"],
                        is_disabled_upstream=item["disabled"],
                        is_available=item["available"],
                        last_seen_at=synced_at,
                    )
                    db.session.add(category)
                    existing_categories[external_id] = category
                    added_categories += 1
                else:
                    changed = any((
                        category.name != item["name"],
                        category.full_path != item["full_path"],
                        category.depth != item["depth"],
                        bool(category.is_disabled_upstream) != item["disabled"],
                        bool(category.is_available) != item["available"],
                    ))
                    category.name = item["name"]
                    category.full_path = item["full_path"]
                    category.depth = item["depth"]
                    category.is_disabled_upstream = item["disabled"]
                    category.is_available = item["available"]
                    category.last_seen_at = synced_at
                    if changed:
                        category.updated_at = synced_at
                        updated_categories += 1
                parent_by_category[external_id] = item[
                    "parent_external_category_id"
                ]
            db.session.flush()
            for external_id, parent_external_id in parent_by_category.items():
                category = existing_categories[external_id]
                category.parent_id = (
                    existing_categories[parent_external_id].id
                    if parent_external_id else None
                )
            for external_id, category in existing_categories.items():
                if external_id not in seen_categories and category.is_available:
                    category.is_available = False
                    category.updated_at = synced_at
                    updated_categories += 1

            added_types = 0
            updated_types = 0
            seen_types = set()
            for item in snapshot["product_types"]:
                pair = (
                    item["external_category_id"],
                    item["external_type_id"],
                )
                seen_types.add(pair)
                product_type = existing_types.get(pair)
                category = existing_categories[item["external_category_id"]]
                is_available = item["available"] and bool(category.is_available)
                if product_type is None:
                    product_type = MarketplaceProductType(
                        marketplace_id=marketplace.id,
                        category_id=category.id,
                        external_type_id=item["external_type_id"],
                        name=item["name"],
                        is_disabled_upstream=item["disabled"],
                        is_available=is_available,
                        is_enabled=False,
                        last_seen_at=synced_at,
                    )
                    db.session.add(product_type)
                    existing_types[pair] = product_type
                    added_types += 1
                else:
                    changed = any((
                        product_type.category_id != category.id,
                        product_type.name != item["name"],
                        bool(product_type.is_disabled_upstream) != item["disabled"],
                        bool(product_type.is_available) != is_available,
                    ))
                    product_type.category_id = category.id
                    product_type.name = item["name"]
                    product_type.is_disabled_upstream = item["disabled"]
                    product_type.is_available = is_available
                    product_type.last_seen_at = synced_at
                    if changed:
                        product_type.updated_at = synced_at
                        updated_types += 1
            for pair, product_type in existing_types.items():
                if pair not in seen_types and product_type.is_available:
                    product_type.is_available = False
                    product_type.updated_at = synced_at
                    updated_types += 1

            changed_snapshot = (
                marketplace.categories_snapshot_hash != snapshot["snapshot_hash"]
            )
            marketplace.categories_snapshot_hash = snapshot["snapshot_hash"]
            marketplace.categories_synced_at = synced_at
            marketplace.categories_sync_status = "success"
            marketplace.categories_sync_error = None
            if changed_snapshot:
                marketplace.categories_version = (
                    marketplace.categories_version or 0
                ) + 1
            marketplace.total_categories = available_categories
            marketplace.total_product_types = available_types
            db.session.commit()
            return {
                "success": True,
                "categories_added": added_categories,
                "categories_updated": updated_categories,
                "types_added": added_types,
                "types_updated": updated_types,
                "available_categories": available_categories,
                "available_types": available_types,
                "version": marketplace.categories_version,
                "snapshot_hash": snapshot["snapshot_hash"],
            }
        except Exception as exc:
            db.session.rollback()
            marketplace = Marketplace.query.filter_by(id=marketplace_id).first()
            if marketplace is not None:
                marketplace.categories_sync_status = "failed"
                marketplace.categories_sync_error = cls._safe_error(exc)
                db.session.commit()
            return {"success": False, "error": cls._safe_error(exc)}
        finally:
            cls._release_claim(lock_file)

    @staticmethod
    def _guard_shrink(
        label: str,
        previous_count: int,
        new_count: int,
        minimum: int,
        ratio: float,
    ) -> None:
        if previous_count >= minimum and new_count < previous_count * ratio:
            raise OzonReferenceValidationError(
                f"Ozon {label} snapshot shrank anomalously "
                f"({previous_count} -> {new_count}); cache preserved"
            )

    @staticmethod
    def _safe_error(exc: Exception) -> str:
        if isinstance(exc, (OzonReferenceError, OzonAPIError)):
            return str(exc)[:1000]
        return "Unexpected Ozon reference synchronization error"

    @classmethod
    def normalize_attributes(cls, response: Any) -> Dict[str, Any]:
        result = cls._result_list(response, "description category attributes")
        if not result:
            raise OzonReferenceValidationError(
                "Ozon attribute schema is empty"
            )
        if len(result) > cls.ATTRIBUTE_MAX_ITEMS:
            raise OzonReferenceValidationError(
                "Ozon attribute schema exceeds the item safety limit"
            )
        attributes = []
        seen = set()
        for index, item in enumerate(result):
            if not isinstance(item, dict):
                raise OzonReferenceValidationError(
                    f"Ozon attribute[{index}] must be an object"
                )
            external_id = str(cls._integer(item.get("id"), f"attribute[{index}].id"))
            if external_id in seen:
                raise OzonReferenceValidationError(
                    f"Ozon attribute schema duplicated id {external_id}"
                )
            seen.add(external_id)
            normalized = {
                "external_attribute_id": external_id,
                "name": cls._text(
                    item.get("name"),
                    f"attribute[{index}].name",
                    maximum=500,
                ),
                "description": cls._optional_text(
                    item.get("description"),
                    f"attribute[{index}].description",
                    maximum=10_000,
                ),
                "data_type": cls._text(
                    item.get("type"),
                    f"attribute[{index}].type",
                    maximum=100,
                ),
                "is_required": cls._boolean(
                    item.get("is_required"),
                    f"attribute[{index}].is_required",
                ),
                "dictionary_id": cls._external_optional_integer(
                    item.get("dictionary_id"),
                    f"attribute[{index}].dictionary_id",
                ),
                "max_value_count": cls._integer(
                    item.get("max_value_count"),
                    f"attribute[{index}].max_value_count",
                    minimum=0,
                ),
                "attribute_complex_id": cls._external_optional_integer(
                    item.get("attribute_complex_id"),
                    f"attribute[{index}].attribute_complex_id",
                ),
                "complex_is_collection": cls._optional_boolean(
                    item,
                    "complex_is_collection",
                    index,
                ),
                "is_collection": cls._optional_boolean(
                    item,
                    "is_collection",
                    index,
                ),
                "category_dependent": cls._optional_boolean(
                    item,
                    "category_dependent",
                    index,
                ),
                "is_filterable": cls._optional_boolean(
                    item,
                    "is_aspect",
                    index,
                ),
                "group_id": cls._external_optional_integer(
                    item.get("group_id"),
                    f"attribute[{index}].group_id",
                ),
                "group_name": cls._optional_text(
                    item.get("group_name"),
                    f"attribute[{index}].group_name",
                    maximum=500,
                ),
                "sort_order": index,
            }
            attributes.append(normalized)
        canonical = {"attributes": attributes}
        canonical["schema_hash"] = cls._hash(canonical)
        return canonical

    @classmethod
    def _optional_boolean(
        cls,
        item: dict,
        field_name: str,
        index: int,
    ) -> bool:
        if field_name not in item or item[field_name] is None:
            return False
        return cls._boolean(
            item[field_name],
            f"attribute[{index}].{field_name}",
        )

    @classmethod
    def sync_attributes(
        cls,
        product_type_id: int,
        *,
        adapter=None,
        credentials: Optional[MarketplaceCredentials] = None,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        if (
            not isinstance(product_type_id, int)
            or isinstance(product_type_id, bool)
            or product_type_id <= 0
        ):
            return {"success": False, "error": "Invalid product_type_id"}
        product_type = MarketplaceProductType.query.filter_by(
            id=product_type_id,
        ).first()
        if (
            product_type is None
            or product_type.marketplace.code != "ozon"
            or not product_type.is_available
            or not product_type.category.is_available
        ):
            return {"success": False, "error": "Available Ozon product type not found"}
        lock_file = cls._try_claim("attributes", product_type.id)
        if lock_file is None:
            return {
                "success": False,
                "skipped": True,
                "error": "Ozon attribute sync is already running",
            }
        synced_at = now or datetime.utcnow()
        product_type.attributes_sync_status = "running"
        product_type.attributes_sync_error = None
        db.session.commit()
        try:
            adapter, credentials = cls._adapter_credentials(
                product_type.marketplace,
                adapter=adapter,
                credentials=credentials,
                now=synced_at,
            )
            category_id = cls._stored_external_integer(
                product_type.category.external_category_id,
                "stored description_category_id",
            )
            external_type_id = cls._stored_external_integer(
                product_type.external_type_id,
                "stored type_id",
            )
            response = adapter.fetch_attribute_schema(
                credentials,
                {
                    "description_category_id": category_id,
                    "type_id": external_type_id,
                    "language": "DEFAULT",
                },
            )
            snapshot = cls.normalize_attributes(response)
            existing = {
                item.external_attribute_id: item
                for item in MarketplaceAttributeDefinition.query.filter_by(
                    product_type_id=product_type.id,
                ).all()
            }
            previous_available = sum(
                1 for item in existing.values() if item.is_available
            )
            cls._guard_shrink(
                "attribute",
                previous_available,
                len(snapshot["attributes"]),
                cls.ATTRIBUTE_SHRINK_GUARD_MIN,
                cls.ATTRIBUTE_SHRINK_GUARD_RATIO,
            )
            added = 0
            updated = 0
            seen = set()
            for item in snapshot["attributes"]:
                external_id = item["external_attribute_id"]
                seen.add(external_id)
                attribute = existing.get(external_id)
                changed_before = None
                if attribute is None:
                    attribute = MarketplaceAttributeDefinition(
                        marketplace_id=product_type.marketplace_id,
                        product_type_id=product_type.id,
                        external_attribute_id=external_id,
                        is_enabled=True,
                        is_available=True,
                        ai_instruction_source="generated",
                    )
                    db.session.add(attribute)
                    existing[external_id] = attribute
                    added += 1
                else:
                    changed_before = cls._attribute_state(attribute)
                cls._apply_attribute(attribute, item, synced_at)
                if attribute.is_required:
                    attribute.is_enabled = True
                if attribute.ai_instruction_source != "custom":
                    attribute.ai_instruction = cls._generated_instruction(item)
                    attribute.ai_instruction_source = "generated"
                if (
                    changed_before is not None
                    and changed_before != cls._attribute_state(attribute)
                ):
                    attribute.updated_at = synced_at
                    updated += 1
            for external_id, attribute in existing.items():
                if external_id not in seen and attribute.is_available:
                    attribute.is_available = False
                    attribute.updated_at = synced_at
                    updated += 1

            changed_schema = (
                product_type.attributes_schema_hash != snapshot["schema_hash"]
            )
            product_type.attributes_schema_hash = snapshot["schema_hash"]
            product_type.attributes_synced_at = synced_at
            product_type.attributes_sync_status = "success"
            product_type.attributes_sync_error = None
            product_type.attributes_count = len(snapshot["attributes"])
            product_type.required_attributes_count = sum(
                1 for item in snapshot["attributes"] if item["is_required"]
            )
            if changed_schema:
                product_type.attributes_version = (
                    product_type.attributes_version or 0
                ) + 1
            product_type.marketplace.total_characteristics = (
                MarketplaceAttributeDefinition.query.filter_by(
                    marketplace_id=product_type.marketplace_id,
                    is_available=True,
                ).count()
            )
            db.session.commit()
            return {
                "success": True,
                "added": added,
                "updated": updated,
                "total": product_type.attributes_count,
                "required": product_type.required_attributes_count,
                "version": product_type.attributes_version,
                "schema_hash": snapshot["schema_hash"],
            }
        except Exception as exc:
            db.session.rollback()
            product_type = MarketplaceProductType.query.filter_by(
                id=product_type_id,
            ).first()
            if product_type is not None:
                product_type.attributes_sync_status = "failed"
                product_type.attributes_sync_error = cls._safe_error(exc)
                db.session.commit()
            return {"success": False, "error": cls._safe_error(exc)}
        finally:
            cls._release_claim(lock_file)

    @classmethod
    def _stored_external_integer(cls, value: str, field_name: str) -> int:
        if not isinstance(value, str) or not value.isdigit() or value.startswith("0"):
            raise OzonReferenceValidationError(
                f"Ozon {field_name} is not a canonical positive integer"
            )
        return cls._integer(int(value), field_name)

    @staticmethod
    def _attribute_state(attribute: MarketplaceAttributeDefinition) -> tuple:
        return (
            attribute.name,
            attribute.description,
            attribute.data_type,
            bool(attribute.is_required),
            attribute.dictionary_id,
            int(attribute.max_value_count or 0),
            attribute.attribute_complex_id,
            bool(attribute.complex_is_collection),
            bool(attribute.is_collection),
            bool(attribute.category_dependent),
            bool(attribute.is_filterable),
            attribute.group_id,
            attribute.group_name,
            int(attribute.sort_order or 0),
            bool(attribute.is_available),
        )

    @staticmethod
    def _apply_attribute(
        attribute: MarketplaceAttributeDefinition,
        item: Dict[str, Any],
        synced_at: datetime,
    ) -> None:
        attribute.name = item["name"]
        attribute.description = item["description"]
        attribute.data_type = item["data_type"]
        attribute.is_required = item["is_required"]
        attribute.dictionary_id = item["dictionary_id"]
        attribute.max_value_count = item["max_value_count"]
        attribute.attribute_complex_id = item["attribute_complex_id"]
        attribute.complex_is_collection = item["complex_is_collection"]
        attribute.is_collection = item["is_collection"]
        attribute.category_dependent = item["category_dependent"]
        attribute.is_filterable = item["is_filterable"]
        attribute.group_id = item["group_id"]
        attribute.group_name = item["group_name"]
        attribute.sort_order = item["sort_order"]
        attribute.is_available = True
        attribute.last_seen_at = synced_at

    @staticmethod
    def _generated_instruction(item: Dict[str, Any]) -> str:
        requirement = "Обязательное поле." if item["is_required"] else ""
        dictionary = (
            "Выбирай только значение из актуального справочника Ozon."
            if item["dictionary_id"] else
            "Используй только явно подтверждённый факт из источника."
        )
        count = (
            f" Максимум значений: {item['max_value_count']}."
            if item["max_value_count"] else ""
        )
        return " ".join(
            part for part in (requirement, dictionary, count.strip()) if part
        )

    @classmethod
    def _generated_instruction_for_model(
        cls,
        attribute: MarketplaceAttributeDefinition,
    ) -> str:
        return cls._generated_instruction({
            "is_required": bool(attribute.is_required),
            "dictionary_id": attribute.dictionary_id,
            "max_value_count": int(attribute.max_value_count or 0),
        })

    @classmethod
    def normalize_values_page(cls, response: Any) -> Dict[str, Any]:
        result = cls._result_list(response, "attribute values")
        values = []
        seen = set()
        for index, item in enumerate(result):
            if not isinstance(item, dict):
                raise OzonReferenceValidationError(
                    f"Ozon value[{index}] must be an object"
                )
            external_id = str(cls._integer(item.get("id"), f"value[{index}].id"))
            if external_id in seen:
                raise OzonReferenceValidationError(
                    f"Ozon values page duplicated id {external_id}"
                )
            seen.add(external_id)
            value = cls._text(
                item.get("value"),
                f"value[{index}].value",
                maximum=1000,
            )
            values.append({
                "external_value_id": external_id,
                "value": value,
                "value_normalized": cls.normalize_value(value),
                "info": cls._optional_text(
                    item.get("info"),
                    f"value[{index}].info",
                    maximum=10_000,
                ),
                "picture_url": cls._optional_text(
                    item.get("picture"),
                    f"value[{index}].picture",
                    maximum=2000,
                ),
            })
        has_next = response.get("has_next")
        if has_next is not None:
            has_next = cls._boolean(has_next, "attribute values has_next")
        return {"values": values, "has_next": has_next}

    @staticmethod
    def normalize_value(value: str) -> str:
        return " ".join(
            unicodedata.normalize("NFKC", value).casefold().split()
        )

    @classmethod
    def sync_attribute_values(
        cls,
        attribute_id: int,
        *,
        adapter=None,
        credentials: Optional[MarketplaceCredentials] = None,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        if (
            not isinstance(attribute_id, int)
            or isinstance(attribute_id, bool)
            or attribute_id <= 0
        ):
            return {"success": False, "error": "Invalid attribute_id"}
        attribute = MarketplaceAttributeDefinition.query.filter_by(
            id=attribute_id,
        ).first()
        if (
            attribute is None
            or attribute.marketplace.code != "ozon"
            or not attribute.is_available
            or not attribute.dictionary_id
            or not attribute.product_type.is_available
        ):
            return {
                "success": False,
                "error": "Available Ozon dictionary attribute not found",
            }
        lock_file = cls._try_claim("values", attribute.id)
        if lock_file is None:
            return {
                "success": False,
                "skipped": True,
                "error": "Ozon attribute value sync is already running",
            }
        synced_at = now or datetime.utcnow()
        attribute.values_sync_status = "running"
        attribute.values_sync_error = None
        # This is an observational durable checkpoint only. Values are kept in
        # memory and every retry restarts at cursor 0; resuming from this cursor
        # without a staging table would create an incomplete snapshot.
        attribute.values_sync_checkpoint = cls._stable_json({
            "page": 0,
            "cursor": 0,
            "fetched": 0,
        })
        db.session.commit()
        try:
            adapter, credentials = cls._adapter_credentials(
                attribute.marketplace,
                adapter=adapter,
                credentials=credentials,
                now=synced_at,
            )
            product_type = attribute.product_type
            base_payload = {
                "description_category_id": cls._stored_external_integer(
                    product_type.category.external_category_id,
                    "stored description_category_id",
                ),
                "type_id": cls._stored_external_integer(
                    product_type.external_type_id,
                    "stored type_id",
                ),
                "attribute_id": cls._stored_external_integer(
                    attribute.external_attribute_id,
                    "stored attribute_id",
                ),
                "language": "DEFAULT",
                "limit": cls.VALUES_PAGE_SIZE,
            }
            all_values = []
            seen_ids = set()
            cursor = 0
            previous_numeric_id = 0
            for _page_number in range(cls.VALUES_MAX_PAGES):
                payload = dict(base_payload)
                payload["last_value_id"] = cursor
                response = adapter.fetch_attribute_values(credentials, payload)
                page = cls.normalize_values_page(response)
                if len(page["values"]) > cls.VALUES_PAGE_SIZE:
                    raise OzonReferenceValidationError(
                        "Ozon dictionary page exceeds the requested limit"
                    )
                for item in page["values"]:
                    external_id = item["external_value_id"]
                    if external_id in seen_ids:
                        raise OzonReferenceValidationError(
                            "Ozon dictionary pagination returned duplicate values"
                        )
                    numeric_id = int(external_id)
                    if numeric_id <= previous_numeric_id:
                        raise OzonReferenceValidationError(
                            "Ozon dictionary pagination is not strictly ordered"
                        )
                    previous_numeric_id = numeric_id
                    seen_ids.add(external_id)
                    all_values.append(item)
                    if len(all_values) > cls.VALUES_MAX_ITEMS:
                        raise OzonReferenceValidationError(
                            "Ozon dictionary exceeds the item safety limit"
                        )
                if not page["values"]:
                    if not all_values:
                        raise OzonReferenceValidationError(
                            "Ozon dictionary snapshot is empty"
                        )
                    if page["has_next"] is True:
                        raise OzonReferenceValidationError(
                            "Ozon dictionary returned an empty page before completion"
                        )
                    break
                next_cursor = int(page["values"][-1]["external_value_id"])
                if next_cursor <= cursor:
                    raise OzonReferenceValidationError(
                        "Ozon dictionary pagination cursor did not advance"
                    )
                cursor = next_cursor
                attribute.values_sync_checkpoint = cls._stable_json({
                    "page": _page_number + 1,
                    "cursor": cursor,
                    "fetched": len(all_values),
                })
                # No reference value row is changed before the complete sweep;
                # committing progress cannot expose a partial official cache.
                db.session.commit()
                has_next = page["has_next"]
                if has_next is False:
                    break
                if has_next is None and len(page["values"]) < cls.VALUES_PAGE_SIZE:
                    break
            else:
                raise OzonReferenceValidationError(
                    "Ozon dictionary pagination exceeded the page safety limit"
                )

            canonical = {"values": all_values}
            snapshot_hash = cls._hash(canonical)
            existing = {
                item.external_value_id: item
                for item in MarketplaceAttributeValue.query.filter_by(
                    attribute_id=attribute.id,
                ).all()
            }
            previous_available = sum(
                1 for item in existing.values() if item.is_available
            )
            cls._guard_shrink(
                "dictionary value",
                previous_available,
                len(all_values),
                cls.VALUE_SHRINK_GUARD_MIN,
                cls.VALUE_SHRINK_GUARD_RATIO,
            )
            added = 0
            updated = 0
            for item in all_values:
                external_id = item["external_value_id"]
                value = existing.get(external_id)
                if value is None:
                    value = MarketplaceAttributeValue(
                        marketplace_id=attribute.marketplace_id,
                        product_type_id=attribute.product_type_id,
                        attribute_id=attribute.id,
                        external_value_id=external_id,
                        value=item["value"],
                        value_normalized=item["value_normalized"],
                        info=item["info"],
                        picture_url=item["picture_url"],
                        is_available=True,
                        last_seen_at=synced_at,
                    )
                    db.session.add(value)
                    added += 1
                else:
                    changed = any((
                        value.value != item["value"],
                        value.value_normalized != item["value_normalized"],
                        value.info != item["info"],
                        value.picture_url != item["picture_url"],
                        not value.is_available,
                    ))
                    value.value = item["value"]
                    value.value_normalized = item["value_normalized"]
                    value.info = item["info"]
                    value.picture_url = item["picture_url"]
                    value.is_available = True
                    value.last_seen_at = synced_at
                    if changed:
                        value.updated_at = synced_at
                        updated += 1
            for external_id, value in existing.items():
                if external_id not in seen_ids and value.is_available:
                    value.is_available = False
                    value.updated_at = synced_at
                    updated += 1
            changed_snapshot = attribute.values_snapshot_hash != snapshot_hash
            attribute.values_snapshot_hash = snapshot_hash
            attribute.values_synced_at = synced_at
            attribute.values_sync_status = "success"
            attribute.values_sync_error = None
            attribute.values_count = len(all_values)
            attribute.values_sync_checkpoint = None
            if changed_snapshot:
                attribute.values_version = (attribute.values_version or 0) + 1
            db.session.commit()
            return {
                "success": True,
                "added": added,
                "updated": updated,
                "total": len(all_values),
                "version": attribute.values_version,
                "snapshot_hash": snapshot_hash,
            }
        except Exception as exc:
            db.session.rollback()
            attribute = MarketplaceAttributeDefinition.query.filter_by(
                id=attribute_id,
            ).first()
            if attribute is not None:
                attribute.values_sync_status = "failed"
                attribute.values_sync_error = cls._safe_error(exc)
                db.session.commit()
            return {"success": False, "error": cls._safe_error(exc)}
        finally:
            cls._release_claim(lock_file)

    @classmethod
    def reference_is_fresh(
        cls,
        product_type: MarketplaceProductType,
        *,
        now: Optional[datetime] = None,
    ) -> bool:
        current_time = now or datetime.utcnow()
        return bool(
            product_type.is_available
            and product_type.category.is_available
            and product_type.marketplace.categories_synced_at
            and product_type.marketplace.categories_synced_at
            >= current_time - timedelta(hours=cls.HARD_TTL_HOURS)
            and product_type.marketplace.categories_snapshot_hash
            and product_type.attributes_synced_at
            and product_type.attributes_synced_at
            >= current_time - timedelta(hours=cls.HARD_TTL_HOURS)
            and product_type.attributes_schema_hash
        )

    @classmethod
    def dictionary_is_fresh(
        cls,
        attribute: MarketplaceAttributeDefinition,
        *,
        now: Optional[datetime] = None,
    ) -> bool:
        current_time = now or datetime.utcnow()
        return bool(
            attribute.is_available
            and attribute.dictionary_id
            and cls.reference_is_fresh(attribute.product_type, now=current_time)
            and attribute.values_synced_at
            and attribute.values_synced_at
            >= current_time - timedelta(hours=cls.HARD_TTL_HOURS)
            and attribute.values_snapshot_hash
        )

    @classmethod
    def set_product_type_enabled(
        cls,
        product_type_id: int,
        is_enabled: Any,
        *,
        adapter=None,
        credentials: Optional[MarketplaceCredentials] = None,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        if not isinstance(is_enabled, bool):
            return {"success": False, "error": "is_enabled must be a boolean"}
        product_type = MarketplaceProductType.query.filter_by(
            id=product_type_id,
        ).first()
        if (
            product_type is None
            or product_type.marketplace.code != "ozon"
            or not product_type.is_available
            or not product_type.category.is_available
        ):
            return {"success": False, "error": "Available Ozon product type not found"}
        synced = False
        if is_enabled and not cls.reference_is_fresh(product_type, now=now):
            result = cls.sync_attributes(
                product_type.id,
                adapter=adapter,
                credentials=credentials,
                now=now,
            )
            if not result.get("success"):
                return {
                    "success": False,
                    "error": result.get("error") or "Ozon schema sync failed",
                }
            synced = True
            product_type = MarketplaceProductType.query.filter_by(
                id=product_type_id,
            ).one()
        product_type.is_enabled = is_enabled
        db.session.commit()
        return {
            "success": True,
            "is_enabled": bool(product_type.is_enabled),
            "synced": synced,
            "attributes_count": int(product_type.attributes_count or 0),
        }

    @classmethod
    def update_attribute_configuration(
        cls,
        attribute_id: int,
        *,
        is_enabled: Any = _UNSET,
        ai_instruction: Any = _UNSET,
        restriction_value_ids: Any = _UNSET,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        supplied = sum(
            value is not _UNSET
            for value in (is_enabled, ai_instruction, restriction_value_ids)
        )
        if supplied != 1:
            raise OzonReferenceValidationError(
                "Update exactly one Ozon attribute setting per request"
            )
        attribute = MarketplaceAttributeDefinition.query.filter_by(
            id=attribute_id,
            is_available=True,
        ).first()
        if (
            attribute is None
            or attribute.marketplace.code != "ozon"
            or not attribute.product_type.is_available
            or not attribute.product_type.category.is_available
        ):
            raise OzonReferenceValidationError(
                "Available Ozon attribute not found"
            )

        if is_enabled is not _UNSET:
            if not isinstance(is_enabled, bool):
                raise OzonReferenceValidationError(
                    "is_enabled must be a boolean"
                )
            if attribute.is_required and not is_enabled:
                raise OzonReferenceValidationError(
                    "Required Ozon attribute cannot be disabled"
                )
            attribute.is_enabled = is_enabled

        if ai_instruction is not _UNSET:
            if not isinstance(ai_instruction, str):
                raise OzonReferenceValidationError(
                    "ai_instruction must be a string"
                )
            instruction = ai_instruction.strip()
            if len(instruction) > 4000 or any(
                ord(character) < 32 and character not in "\n\t"
                for character in instruction
            ):
                raise OzonReferenceValidationError(
                    "ai_instruction is invalid"
                )
            if instruction:
                attribute.ai_instruction = instruction
                attribute.ai_instruction_source = "custom"
            else:
                attribute.ai_instruction = cls._generated_instruction_for_model(
                    attribute
                )
                attribute.ai_instruction_source = "generated"

        if restriction_value_ids is not _UNSET:
            cls._apply_value_restriction(
                attribute,
                restriction_value_ids,
                now=now,
            )

        db.session.commit()
        return {
            "success": True,
            "is_enabled": bool(attribute.is_enabled),
            "ai_instruction": attribute.ai_instruction or "",
            "ai_instruction_source": attribute.ai_instruction_source,
            "restriction_value_ids": attribute.restriction_value_ids,
        }

    @classmethod
    def _apply_value_restriction(
        cls,
        attribute: MarketplaceAttributeDefinition,
        raw_ids: Any,
        *,
        now: Optional[datetime] = None,
    ) -> None:
        if not isinstance(raw_ids, list) or len(raw_ids) > 5000:
            raise OzonReferenceValidationError(
                "restriction_value_ids must be an array of at most 5000 IDs"
            )
        if raw_ids and not cls.dictionary_is_fresh(attribute, now=now):
            raise OzonReferenceValidationError(
                "Fresh official Ozon dictionary is required for a restriction"
            )
        normalized = []
        seen = set()
        for value in raw_ids:
            if (
                not isinstance(value, str)
                or not value.isdigit()
                or value.startswith("0")
                or len(value) > 100
                or value in seen
            ):
                raise OzonReferenceValidationError(
                    "restriction_value_ids must contain unique canonical string IDs"
                )
            seen.add(value)
            normalized.append(value)
        if normalized:
            available_ids = set()
            # Stay below SQLite/provider bind-variable limits for a large
            # platform restriction while still validating the exact set.
            for offset in range(0, len(normalized), 500):
                chunk = normalized[offset:offset + 500]
                available_ids.update(
                    row.external_value_id
                    for row in MarketplaceAttributeValue.query.filter(
                        MarketplaceAttributeValue.attribute_id == attribute.id,
                        MarketplaceAttributeValue.is_available.is_(True),
                        MarketplaceAttributeValue.external_value_id.in_(chunk),
                    ).all()
                )
            if available_ids != set(normalized):
                raise OzonReferenceValidationError(
                    "Restriction contains a value outside the official Ozon dictionary"
                )
            normalized.sort(key=int)
            attribute.restriction_value_ids_json = json.dumps(
                normalized,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        else:
            attribute.restriction_value_ids_json = None

    @classmethod
    def sync_stale_enabled_types(
        cls,
        marketplace_id: int,
        *,
        limit: int = 50,
        dictionary_limit: int = 25,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= 200
            or not isinstance(dictionary_limit, int)
            or isinstance(dictionary_limit, bool)
            or not 0 <= dictionary_limit <= 200
        ):
            return {"success": False, "error": "Invalid Ozon refresh limits"}
        marketplace = cls._marketplace(marketplace_id)
        current_time = now or datetime.utcnow()
        refresh_before = current_time - timedelta(
            hours=cls.TREE_REFRESH_AFTER_HOURS
        )
        adapter, credentials = cls._adapter_credentials(
            marketplace,
            now=current_time,
        )
        product_types = MarketplaceProductType.query.join(
            MarketplaceTaxonomyCategory,
            MarketplaceProductType.category_id == MarketplaceTaxonomyCategory.id,
        ).filter(
            MarketplaceProductType.marketplace_id == marketplace.id,
            MarketplaceProductType.is_enabled.is_(True),
            MarketplaceProductType.is_available.is_(True),
            MarketplaceTaxonomyCategory.is_available.is_(True),
            or_(
                MarketplaceProductType.attributes_synced_at.is_(None),
                MarketplaceProductType.attributes_synced_at < refresh_before,
                MarketplaceProductType.attributes_sync_status.is_(None),
                MarketplaceProductType.attributes_sync_status != "success",
            ),
        ).order_by(
            MarketplaceProductType.attributes_synced_at.asc(),
            MarketplaceProductType.id.asc(),
        ).limit(limit).all()

        synced = 0
        failed = 0
        dictionaries_synced = 0
        dictionaries_failed = 0
        for product_type in product_types:
            result = cls.sync_attributes(
                product_type.id,
                adapter=adapter,
                credentials=credentials,
                now=current_time,
            )
            if result.get("success"):
                synced += 1
            else:
                failed += 1
                continue
            remaining = dictionary_limit - dictionaries_synced - dictionaries_failed
            if remaining <= 0:
                continue
            required_dictionaries = MarketplaceAttributeDefinition.query.filter_by(
                product_type_id=product_type.id,
                is_required=True,
                is_available=True,
            ).filter(
                MarketplaceAttributeDefinition.dictionary_id.isnot(None),
            ).order_by(
                MarketplaceAttributeDefinition.values_synced_at.asc(),
                MarketplaceAttributeDefinition.id.asc(),
            ).limit(remaining).all()
            for attribute in required_dictionaries:
                if cls.dictionary_is_fresh(attribute, now=current_time):
                    continue
                value_result = cls.sync_attribute_values(
                    attribute.id,
                    adapter=adapter,
                    credentials=credentials,
                    now=current_time,
                )
                if value_result.get("success"):
                    dictionaries_synced += 1
                else:
                    dictionaries_failed += 1
        return {
            "success": failed == 0 and dictionaries_failed == 0,
            "selected": len(product_types),
            "synced": synced,
            "failed": failed,
            "dictionaries_synced": dictionaries_synced,
            "dictionaries_failed": dictionaries_failed,
        }
