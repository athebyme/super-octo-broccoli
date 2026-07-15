"""Seller-scoped marketplace drafts and deterministic Ozon validation.

No provider request and no LLM call is allowed in this module.  Draft values
must resolve against the current SQL reference snapshot; publication will be a
separate durable P5 operation that revalidates this state.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
import json
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
import unicodedata
from urllib.parse import urlsplit

from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload
from sqlalchemy.orm.exc import StaleDataError

from models import (
    ImportedProduct,
    Marketplace,
    MarketplaceAttributeDefinition,
    MarketplaceAttributeValue,
    MarketplaceCategoryMapping,
    MarketplaceListing,
    MarketplaceOperation,
    MarketplaceProductDraft,
    MarketplaceProductType,
    MarketplaceTaxonomyCategory,
    Seller,
    SellerMarketplaceAccount,
    db,
)
from services.marketplace_fact_pack import (
    MarketplaceFactPackBuilder,
    MarketplaceFactPackError,
)
from services.ozon_reference_service import OzonReferenceService


class MarketplaceDraftError(RuntimeError):
    status_code = 400
    code = "marketplace_draft_error"


class MarketplaceDraftValidationError(MarketplaceDraftError):
    status_code = 400
    code = "invalid_marketplace_draft"


class MarketplaceDraftNotFound(MarketplaceDraftError):
    status_code = 404
    code = "marketplace_draft_not_found"


class MarketplaceDraftConflict(MarketplaceDraftError):
    status_code = 409
    code = "marketplace_draft_conflict"


class MarketplaceDraftService:
    MAX_JSON_BYTES = 256 * 1024
    MAX_ATTRIBUTES = 5_000
    MAX_COMPLEX_GROUPS = 500
    MAX_ATTRIBUTE_VALUES = 100
    MAX_IMAGES = 30
    MAX_BARCODES = 100
    MAX_IMPORT_BARCODES = 1
    MAX_OZON_OFFER_ID_CHARS = 50
    OZON_DESCRIPTION_ATTRIBUTE_ID = "4191"
    MAX_VALIDATION_ITEMS = 250
    DIMENSION_UNITS = {"MILLIMETERS", "CENTIMETERS", "INCHES"}
    WEIGHT_UNITS = {"GRAMS", "KILOGRAMS", "POUNDS"}
    VAT_VALUES = {"0", "0.05", "0.07", "0.1", "0.10", "0.2", "0.20", "0.22"}
    CURRENCY_CODES = {"RUB"}
    DATA_TYPES = {"string", "integer", "decimal", "boolean"}
    ACTIVE_PUBLICATION_STATUSES = {
        "queued",
        "submitting",
        "submitted",
        "polling",
        "uncertain",
    }

    @staticmethod
    def _positive_integer(value: Any, field_name: str) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise MarketplaceDraftValidationError(
                f"{field_name} должен быть положительным целым числом"
            )
        return value

    @staticmethod
    def _strict_boolean(value: Any, field_name: str) -> bool:
        if not isinstance(value, bool):
            raise MarketplaceDraftValidationError(
                f"{field_name} должен быть boolean"
            )
        return value

    @classmethod
    def _assert_no_active_publication(
        cls,
        draft: MarketplaceProductDraft,
    ) -> None:
        active = MarketplaceOperation.query.filter(
            MarketplaceOperation.seller_id == draft.seller_id,
            MarketplaceOperation.marketplace_id == draft.marketplace_id,
            MarketplaceOperation.account_id == draft.account_id,
            MarketplaceOperation.draft_id == draft.id,
            MarketplaceOperation.status.in_(cls.ACTIVE_PUBLICATION_STATUSES),
        ).first()
        if active is not None:
            raise MarketplaceDraftConflict(
                "Черновик нельзя менять, пока публикация не завершена"
            )

    @staticmethod
    def _text(
        value: Any,
        field_name: str,
        *,
        maximum: int,
        required: bool = True,
        multiline: bool = False,
    ) -> str:
        if not isinstance(value, str):
            raise MarketplaceDraftValidationError(
                f"{field_name} должен быть строкой"
            )
        normalized = value.strip()
        if required and not normalized:
            raise MarketplaceDraftValidationError(f"{field_name} обязателен")
        if len(normalized) > maximum:
            raise MarketplaceDraftValidationError(
                f"{field_name} длиннее {maximum} символов"
            )
        allowed_controls = "\n\t" if multiline else ""
        if any(
            ord(character) < 32 and character not in allowed_controls
            for character in normalized
        ) or any(ord(character) == 127 for character in normalized):
            raise MarketplaceDraftValidationError(
                f"{field_name} содержит управляющие символы"
            )
        return normalized

    @classmethod
    def _optional_text(
        cls,
        value: Any,
        field_name: str,
        *,
        maximum: int,
        multiline: bool = False,
    ) -> str:
        if value in (None, ""):
            return ""
        return cls._text(
            value,
            field_name,
            maximum=maximum,
            required=False,
            multiline=multiline,
        )

    @staticmethod
    def _normalized_text(value: str) -> str:
        return " ".join(unicodedata.normalize("NFKC", value).casefold().split())

    @classmethod
    def _external_id(cls, value: Any, field_name: str) -> str:
        value = cls._text(value, field_name, maximum=100)
        if not value.isascii() or not value.isdigit() or value.startswith("0"):
            raise MarketplaceDraftValidationError(
                f"{field_name} должен быть canonical positive string ID"
            )
        return value

    @staticmethod
    def _decimal(value: Any, field_name: str, *, positive: bool = False) -> str:
        if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
            raise MarketplaceDraftValidationError(
                f"{field_name} должен быть числом"
            )
        raw = str(value).strip()
        if not raw or not re.fullmatch(r"-?(?:0|[1-9]\d*)(?:\.\d+)?", raw):
            raise MarketplaceDraftValidationError(
                f"{field_name} должен быть canonical decimal"
            )
        try:
            parsed = Decimal(raw)
        except InvalidOperation:
            raise MarketplaceDraftValidationError(
                f"{field_name} должен быть числом"
            ) from None
        if not parsed.is_finite() or (positive and parsed <= 0):
            qualifier = "положительным " if positive else ""
            raise MarketplaceDraftValidationError(
                f"{field_name} должен быть {qualifier}числом"
            )
        rendered = format(parsed, "f")
        if "." in rendered:
            rendered = rendered.rstrip("0").rstrip(".")
        return rendered or "0"

    @classmethod
    def _canonical_json(cls, value: Any, expected_type: type) -> str:
        if not isinstance(value, expected_type):
            raise MarketplaceDraftValidationError(
                f"Ожидался JSON {expected_type.__name__}"
            )
        try:
            rendered = json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError):
            raise MarketplaceDraftValidationError(
                "JSON содержит неподдерживаемое значение"
            ) from None
        if len(rendered.encode("utf-8")) > cls.MAX_JSON_BYTES:
            raise MarketplaceDraftValidationError(
                "JSON черновика превышает лимит размера"
            )
        return rendered

    @staticmethod
    def _stored_json(raw_value: Optional[str], expected_type: type) -> Any:
        try:
            value = json.loads(raw_value or "")
        except (TypeError, ValueError):
            return expected_type()
        return value if isinstance(value, expected_type) else expected_type()

    @classmethod
    def _owned_account(
        cls,
        *,
        seller_id: int,
        account_id: int,
    ) -> SellerMarketplaceAccount:
        seller_id = cls._positive_integer(seller_id, "seller_id")
        account_id = cls._positive_integer(account_id, "account_id")
        account = SellerMarketplaceAccount.query.join(Marketplace).filter(
            SellerMarketplaceAccount.id == account_id,
            SellerMarketplaceAccount.seller_id == seller_id,
            Marketplace.code == "ozon",
            Marketplace.is_active.is_(True),
        ).first()
        if account is None:
            raise MarketplaceDraftNotFound("Кабинет Ozon не найден")
        return account

    @classmethod
    def _owned_imported_product(
        cls,
        *,
        seller_id: int,
        imported_product_id: int,
    ) -> ImportedProduct:
        seller_id = cls._positive_integer(seller_id, "seller_id")
        imported_product_id = cls._positive_integer(
            imported_product_id,
            "imported_product_id",
        )
        product = ImportedProduct.query.options(
            joinedload(ImportedProduct.supplier_product),
            joinedload(ImportedProduct.product),
        ).filter_by(
            id=imported_product_id,
            seller_id=seller_id,
        ).first()
        if product is None:
            raise MarketplaceDraftNotFound("Импортированный товар не найден")
        return product

    @classmethod
    def get_draft(
        cls,
        *,
        seller_id: int,
        draft_id: int,
    ) -> MarketplaceProductDraft:
        seller_id = cls._positive_integer(seller_id, "seller_id")
        draft_id = cls._positive_integer(draft_id, "draft_id")
        draft = MarketplaceProductDraft.query.options(
            joinedload(MarketplaceProductDraft.marketplace),
            joinedload(MarketplaceProductDraft.account),
            joinedload(MarketplaceProductDraft.imported_product).joinedload(
                ImportedProduct.product
            ),
            joinedload(MarketplaceProductDraft.imported_product).joinedload(
                ImportedProduct.supplier_product
            ),
            joinedload(MarketplaceProductDraft.product_type).joinedload(
                MarketplaceProductType.category
            ),
            joinedload(MarketplaceProductDraft.category_mapping),
        ).filter_by(
            id=draft_id,
            seller_id=seller_id,
        ).first()
        if draft is None:
            raise MarketplaceDraftNotFound("Черновик не найден")
        if (
            not draft.account
            or draft.account.seller_id != seller_id
            or draft.account.marketplace_id != draft.marketplace_id
            or not draft.marketplace
            or draft.marketplace.code != "ozon"
            or not draft.imported_product
            or draft.imported_product.seller_id != seller_id
        ):
            raise MarketplaceDraftNotFound("Черновик не найден")
        return draft

    @classmethod
    def _product_type(
        cls,
        *,
        marketplace_id: int,
        product_type_id: int,
    ) -> MarketplaceProductType:
        product_type_id = cls._positive_integer(product_type_id, "product_type_id")
        product_type = MarketplaceProductType.query.options(
            joinedload(MarketplaceProductType.category),
            joinedload(MarketplaceProductType.marketplace),
        ).filter_by(
            id=product_type_id,
            marketplace_id=marketplace_id,
            is_available=True,
            is_enabled=True,
        ).first()
        if (
            product_type is None
            or product_type.marketplace.code != "ozon"
            or not product_type.category
            or not product_type.category.is_available
        ):
            raise MarketplaceDraftValidationError(
                "Доступный и включённый тип товара Ozon не найден"
            )
        return product_type

    @staticmethod
    def _scope_key(product: ImportedProduct) -> str:
        if product.supplier_id:
            return f"supplier:{product.supplier_id}"
        source_type = MarketplaceDraftService._normalized_text(
            product.source_type or "unknown"
        )
        return f"source:{source_type}"[:220]

    @classmethod
    def _source_category(cls, product: ImportedProduct) -> Tuple[str, str]:
        category = cls._optional_text(
            product.category,
            "source_category",
            maximum=500,
        )
        if not category:
            try:
                original = json.loads(product.original_data or "{}")
            except (TypeError, ValueError):
                original = {}
            if isinstance(original, dict):
                category = cls._optional_text(
                    original.get("category"),
                    "source_category",
                    maximum=500,
                )
        return category, cls._normalized_text(category) if category else ""

    @classmethod
    def _mapping_identities(cls, product: ImportedProduct) -> list:
        """Return exact category identities in deterministic priority order.

        A confirmed WB subject is stronger and more reusable than a supplier
        category label.  The legacy supplier/source identity remains as a
        fallback so existing mappings keep working after this rollout.
        """
        identities = []
        wb_product = product.product
        remote_wb_subject_id = (
            wb_product.subject_id
            if wb_product is not None
            and isinstance(wb_product.subject_id, int)
            and not isinstance(wb_product.subject_id, bool)
            and wb_product.subject_id > 0
            else None
        )
        imported_wb_subject_id = (
            product.wb_subject_id
            if isinstance(product.wb_subject_id, int)
            and not isinstance(product.wb_subject_id, bool)
            and product.wb_subject_id > 0
            else None
        )
        has_confirmed_wb_projection = bool(
            wb_product is not None
            or product.product_id is not None
            or product.wb_nm_id is not None
            or product.import_status == "imported"
        )
        wb_subject_id = remote_wb_subject_id or (
            imported_wb_subject_id
            if has_confirmed_wb_projection else None
        )
        if wb_subject_id is not None:
            wb_label = cls._optional_text(
                product.mapped_wb_category,
                "mapped_wb_category",
                maximum=400,
            )
            if not wb_label and wb_product is not None:
                wb_label = cls._optional_text(
                    wb_product.object_name,
                    "wb_object_name",
                    maximum=400,
                )
            identities.append({
                "scope_key": "wb_subject",
                "supplier_id": None,
                "source_type": "wb",
                "source_category": (
                    f"{wb_label} · WB subject {wb_subject_id}"
                    if wb_label else f"WB subject {wb_subject_id}"
                ),
                "source_category_normalized": f"wb_subject:{wb_subject_id}",
                "evidence": {
                    "wb_subject_id": wb_subject_id,
                    "wb_subject_source": (
                        "product_projection"
                        if remote_wb_subject_id is not None
                        else "confirmed_imported_projection"
                    ),
                },
            })

        category, normalized = cls._source_category(product)
        if category and normalized:
            identities.append({
                "scope_key": cls._scope_key(product),
                "supplier_id": product.supplier_id,
                "source_type": cls._optional_text(
                    product.source_type,
                    "source_type",
                    maximum=80,
                ) or "unknown",
                "source_category": category,
                "source_category_normalized": normalized,
                "evidence": {},
            })
        return identities

    @classmethod
    def _active_mapping(
        cls,
        *,
        seller_id: int,
        marketplace_id: int,
        product: ImportedProduct,
    ) -> Optional[MarketplaceCategoryMapping]:
        for identity in cls._mapping_identities(product):
            mapping = MarketplaceCategoryMapping.query.options(
                joinedload(MarketplaceCategoryMapping.product_type).joinedload(
                    MarketplaceProductType.category
                )
            ).filter_by(
                seller_id=seller_id,
                marketplace_id=marketplace_id,
                scope_key=identity["scope_key"],
                source_category_normalized=(
                    identity["source_category_normalized"]
                ),
                mapping_status="active",
            ).first()
            if mapping is None:
                continue
            if (
                mapping.product_type
                and mapping.product_type.is_enabled
                and mapping.product_type.is_available
                and mapping.product_type.category
                and mapping.product_type.category.is_available
            ):
                return mapping
            # A stored stronger identity must fail closed when its target was
            # disabled/removed; silently falling through to a weaker category
            # label could route the product to a different Ozon type.
            return None
        return None

    @classmethod
    def _upsert_mapping(
        cls,
        *,
        seller_id: int,
        marketplace_id: int,
        product: ImportedProduct,
        product_type: MarketplaceProductType,
        corrected_by_user_id: Optional[int],
    ) -> MarketplaceCategoryMapping:
        identities = cls._mapping_identities(product)
        if not identities:
            raise MarketplaceDraftValidationError(
                "Нельзя сохранить mapping без исходной категории"
            )
        identity = identities[0]
        if corrected_by_user_id is not None:
            corrected_by_user_id = cls._positive_integer(
                corrected_by_user_id,
                "corrected_by_user_id",
            )
            if Seller.query.filter_by(
                id=seller_id,
                user_id=corrected_by_user_id,
            ).first() is None:
                raise MarketplaceDraftValidationError(
                    "corrected_by_user_id не принадлежит seller"
                )
        mapping = MarketplaceCategoryMapping.query.filter_by(
            seller_id=seller_id,
            marketplace_id=marketplace_id,
            scope_key=identity["scope_key"],
            source_category_normalized=(
                identity["source_category_normalized"]
            ),
        ).first()
        evidence = {"confirmation": "seller"}
        evidence.update(identity["evidence"])
        evidence_json = json.dumps(
            evidence,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if mapping is None:
            mapping = MarketplaceCategoryMapping(
                seller_id=seller_id,
                marketplace_id=marketplace_id,
                supplier_id=identity["supplier_id"],
                scope_key=identity["scope_key"],
                source_type=identity["source_type"],
                source_category=identity["source_category"],
                source_category_normalized=(
                    identity["source_category_normalized"]
                ),
                product_type_id=product_type.id,
                external_category_id=product_type.category.external_category_id,
                external_type_id=product_type.external_type_id,
                mapping_source="manual",
                mapping_status="active",
                confidence=1.0,
                evidence_json=evidence_json,
                corrected_by_user_id=corrected_by_user_id,
            )
            db.session.add(mapping)
        else:
            mapping.supplier_id = identity["supplier_id"]
            mapping.source_type = identity["source_type"]
            mapping.source_category = identity["source_category"]
            mapping.product_type_id = product_type.id
            mapping.external_category_id = product_type.category.external_category_id
            mapping.external_type_id = product_type.external_type_id
            mapping.mapping_source = "manual"
            mapping.mapping_status = "active"
            mapping.confidence = 1.0
            mapping.evidence_json = evidence_json
            mapping.corrected_by_user_id = corrected_by_user_id
            mapping.updated_at = datetime.utcnow()
        db.session.flush()
        return mapping

    @classmethod
    def search_product_types(
        cls,
        *,
        seller_id: int,
        query: Any = "",
        limit: int = 50,
    ) -> list:
        cls._positive_integer(seller_id, "seller_id")
        limit = cls._positive_integer(limit, "limit")
        if limit > 100:
            raise MarketplaceDraftValidationError("limit не может быть больше 100")
        search = cls._optional_text(query, "query", maximum=200)
        rows = MarketplaceProductType.query.join(Marketplace).join(
            MarketplaceTaxonomyCategory,
            MarketplaceProductType.category_id == MarketplaceTaxonomyCategory.id,
        ).filter(
            Marketplace.code == "ozon",
            Marketplace.is_active.is_(True),
            MarketplaceProductType.is_available.is_(True),
            MarketplaceProductType.is_enabled.is_(True),
            MarketplaceTaxonomyCategory.is_available.is_(True),
        )
        if search:
            pattern = f"%{search}%"
            rows = rows.filter(or_(
                MarketplaceProductType.name.ilike(pattern),
                MarketplaceTaxonomyCategory.full_path.ilike(pattern),
            ))
        product_types = rows.order_by(
            MarketplaceTaxonomyCategory.full_path.asc(),
            MarketplaceProductType.name.asc(),
            MarketplaceProductType.id.asc(),
        ).limit(limit).all()
        return [{
            "id": item.id,
            "name": item.name,
            "category_path": item.category.full_path,
            "external_category_id": item.category.external_category_id,
            "external_type_id": item.external_type_id,
            "schema_fresh": OzonReferenceService.reference_is_fresh(item),
            "schema_version": item.attributes_version,
        } for item in product_types]

    @classmethod
    def mapping_readiness(
        cls,
        *,
        seller_id: int,
        draft_id: int,
    ) -> dict:
        """Return a bounded explanation of WB/canonical -> Ozon readiness.

        This is a local read.  It deliberately reports exact reference state
        instead of guessing whether a value can be translated between the WB
        and Ozon schemas.  ``validate_draft`` remains the final publishability
        authority because it also checks content, media, physical values,
        commercial data and account health.
        """
        draft = cls.get_draft(seller_id=seller_id, draft_id=draft_id)
        product_type = draft.product_type
        definitions = []
        if product_type is not None:
            definitions = MarketplaceAttributeDefinition.query.filter_by(
                product_type_id=product_type.id,
                is_available=True,
                is_enabled=True,
            ).order_by(
                MarketplaceAttributeDefinition.sort_order.asc(),
                MarketplaceAttributeDefinition.id.asc(),
            ).all()

        supplied_ids = set()
        for item in cls._stored_json(draft.attributes_json, list):
            if isinstance(item, dict) and isinstance(item.get("attribute_id"), str):
                supplied_ids.add(item["attribute_id"])
        for group in cls._stored_json(draft.complex_attributes_json, list):
            if not isinstance(group, dict):
                continue
            for item in group.get("attributes", []):
                if isinstance(item, dict) and isinstance(item.get("attribute_id"), str):
                    supplied_ids.add(item["attribute_id"])
        content = cls._stored_json(draft.content_json, dict)
        if isinstance(content.get("description"), str) and content["description"].strip():
            supplied_ids.add(cls.OZON_DESCRIPTION_ATTRIBUTE_ID)

        required = [item for item in definitions if item.is_required]
        missing_required = [
            {
                "attribute_id": item.external_attribute_id,
                "name": item.name,
                "complex_id": item.attribute_complex_id or "0",
            }
            for item in required
            if item.external_attribute_id not in supplied_ids
        ]
        dictionary_definitions = [
            item for item in definitions if item.dictionary_id
        ]
        stale_dictionaries = [
            {
                "attribute_id": item.external_attribute_id,
                "name": item.name,
                "dictionary_id": item.dictionary_id,
            }
            for item in dictionary_definitions
            if not OzonReferenceService.dictionary_is_fresh(item)
        ]
        schema_fresh = bool(
            product_type
            and OzonReferenceService.reference_is_fresh(product_type)
        )

        product = draft.imported_product
        supplier_product = product.supplier_product if product else None
        if supplier_product and (
            supplier_product.ai_parsed_at is not None
            or bool(supplier_product.ai_parsed_data_json)
        ):
            ai_source = "supplier_product_cache"
        elif product and any((
            product.ai_analysis_at,
            product.ai_keywords,
            product.ai_attributes,
            product.ai_seo_title,
        )):
            ai_source = "imported_product_cache"
        else:
            ai_source = None

        try:
            source_facts_fresh = bool(
                product
                and MarketplaceFactPackBuilder.build(product)["fact_hash"]
                == draft.source_fact_hash
            )
        except MarketplaceFactPackError:
            source_facts_fresh = False
        try:
            wb_projection = MarketplaceFactPackBuilder.wb_projection_drift(
                product
            ) if product else {
                "linked": False,
                "in_sync": None,
                "differing_fields": [],
            }
        except MarketplaceFactPackError:
            wb_projection = {
                "linked": False,
                "in_sync": None,
                "differing_fields": [],
            }
        now = datetime.utcnow()
        account_ready = bool(
            draft.account
            and draft.account.is_active
            and draft.account.connection_status == "connected"
            and draft.account.has_credentials
            and (
                draft.account.credential_expires_at is None
                or draft.account.credential_expires_at > now
            )
        )

        validation = cls._stored_json(draft.validation_result_json, dict)
        if product_type is None:
            overall = "needs_category"
        elif not source_facts_fresh:
            overall = "source_stale"
        elif not schema_fresh or stale_dictionaries:
            overall = "references_stale"
        elif missing_required:
            overall = "needs_attributes"
        elif not account_ready:
            overall = "account_blocked"
        elif draft.status == "ready" and validation.get("publishable") is True:
            overall = "ready"
        elif draft.validation_status in {"never_validated", "stale"}:
            overall = "needs_validation"
        else:
            overall = "blocked"

        if draft.category_mapping_id is not None:
            category_status = "exact_mapping"
        elif draft.published_listing_id is not None and product_type is not None:
            category_status = "linked_listing"
        elif product_type is not None:
            category_status = "selected"
        else:
            category_status = "missing"

        wb_product = product.product if product else None
        wb_nm_id = None
        if wb_product is not None and wb_product.nm_id is not None:
            wb_nm_id = str(wb_product.nm_id)
        elif product is not None and product.wb_nm_id is not None:
            wb_nm_id = str(product.wb_nm_id)

        return {
            "version": 1,
            "overall": overall,
            "source": {
                "kind": "canonical_imported_product",
                "imported_product_id": draft.imported_product_id,
                "wb_projection_linked": bool(
                    product and (product.product_id is not None or wb_nm_id)
                ),
                "wb_product_id": product.product_id if product else None,
                "wb_nm_id": wb_nm_id,
                "ai_cache_reused": ai_source is not None,
                "ai_source": ai_source,
                "facts_fresh": source_facts_fresh,
                "wb_projection": wb_projection,
            },
            "account": {
                "ready": account_ready,
                "active": bool(draft.account and draft.account.is_active),
                "connection_status": (
                    draft.account.connection_status if draft.account else None
                ),
                "has_credentials": bool(
                    draft.account and draft.account.has_credentials
                ),
            },
            "category": {
                "status": category_status,
                "mapping_id": draft.category_mapping_id,
                "mapping_source_type": (
                    draft.category_mapping.source_type
                    if draft.category_mapping else None
                ),
                "mapping_source_category": (
                    draft.category_mapping.source_category
                    if draft.category_mapping else None
                ),
                "product_type_id": draft.product_type_id,
                "external_category_id": draft.external_category_id,
                "external_type_id": draft.external_type_id,
            },
            "schema": {
                "fresh": schema_fresh,
                "version": (
                    product_type.attributes_version if product_type else None
                ),
                "hash": (
                    product_type.attributes_schema_hash if product_type else None
                ),
            },
            "attributes": {
                "schema_total": len(definitions),
                "supplied_known": sum(
                    1
                    for item in definitions
                    if item.external_attribute_id in supplied_ids
                ),
                "required_total": len(required),
                "required_supplied": len(required) - len(missing_required),
                "missing_required": missing_required[:50],
                "missing_required_truncated": len(missing_required) > 50,
            },
            "dictionaries": {
                "total": len(dictionary_definitions),
                "fresh": len(dictionary_definitions) - len(stale_dictionaries),
                "stale": stale_dictionaries[:50],
                "stale_truncated": len(stale_dictionaries) > 50,
            },
            "validation": {
                "status": draft.validation_status,
                "publishable": validation.get("publishable") is True,
                "error_count": len(validation.get("errors", []))
                if isinstance(validation.get("errors"), list) else 0,
                "warning_count": len(validation.get("warnings", []))
                if isinstance(validation.get("warnings"), list) else 0,
            },
            "reverse_mapping": {
                "automatic_round_trip": False,
                "mode": "reviewed_common_fact_diff",
            },
        }

    @classmethod
    def list_drafts(
        cls,
        *,
        seller_id: int,
        account_id: Optional[int] = None,
        status: Optional[str] = None,
        page: int = 1,
        per_page: int = 50,
    ):
        seller_id = cls._positive_integer(seller_id, "seller_id")
        page = cls._positive_integer(page, "page")
        per_page = cls._positive_integer(per_page, "per_page")
        if per_page > 100:
            raise MarketplaceDraftValidationError(
                "per_page не может быть больше 100"
            )
        query = MarketplaceProductDraft.query.options(
            joinedload(MarketplaceProductDraft.marketplace),
            joinedload(MarketplaceProductDraft.account),
            joinedload(MarketplaceProductDraft.product_type).joinedload(
                MarketplaceProductType.category
            ),
        ).filter_by(seller_id=seller_id)
        if account_id is not None:
            cls._owned_account(seller_id=seller_id, account_id=account_id)
            query = query.filter(MarketplaceProductDraft.account_id == account_id)
        if status:
            status = cls._text(status, "status", maximum=30)
            if status not in {
                "needs_category", "draft", "blocked", "ready", "published", "archived"
            }:
                raise MarketplaceDraftValidationError("Неизвестный статус черновика")
            query = query.filter(MarketplaceProductDraft.status == status)
        return query.order_by(
            MarketplaceProductDraft.updated_at.desc(),
            MarketplaceProductDraft.id.desc(),
        ).paginate(page=page, per_page=per_page, error_out=False)

    @classmethod
    def recent_sources(cls, *, seller_id: int, limit: int = 100) -> list:
        seller_id = cls._positive_integer(seller_id, "seller_id")
        limit = cls._positive_integer(limit, "limit")
        if limit > 200:
            raise MarketplaceDraftValidationError("limit не может быть больше 200")
        return ImportedProduct.query.filter_by(seller_id=seller_id).order_by(
            ImportedProduct.updated_at.desc(),
            ImportedProduct.id.desc(),
        ).limit(limit).all()

    @classmethod
    def _fact_snapshot(cls, product: ImportedProduct) -> Tuple[dict, dict, str]:
        try:
            pack = MarketplaceFactPackBuilder.build(product)
        except MarketplaceFactPackError as exc:
            raise MarketplaceDraftValidationError(str(exc)) from None
        facts_document = {
            "version": pack["version"],
            "source": pack["source"],
            "facts": pack["facts"],
            "unverified_suggestions": pack["unverified_suggestions"],
        }
        cls._canonical_json(facts_document, dict)
        cls._canonical_json(pack["provenance"], dict)
        return facts_document, pack["provenance"], pack["fact_hash"]

    @classmethod
    def _derive_offer_id(cls, product: ImportedProduct, facts_document: dict) -> str:
        identifiers = facts_document.get("facts", {}).get("identifiers", {})
        candidate = (
            identifiers.get("vendor_code")
            or identifiers.get("external_id")
            or f"sellerhub-{product.id}"
        )
        return cls._text(candidate, "offer_id", maximum=200)

    @classmethod
    def _decimal_from_fact(cls, value: Any) -> Optional[str]:
        try:
            return cls._decimal(value, "fact", positive=True)
        except MarketplaceDraftValidationError:
            return None

    @classmethod
    def _dimensions_from_facts(cls, facts_document: dict) -> dict:
        raw = facts_document.get("facts", {}).get("physical", {}).get(
            "dimensions", {}
        )
        if not isinstance(raw, dict):
            return {}
        normalized = {
            cls._normalized_text(str(key)).replace(" ", "_"): value
            for key, value in raw.items()
            if isinstance(key, str)
        }

        def dimension(name: str, aliases: Sequence[str]) -> Tuple[Optional[str], Optional[str]]:
            for alias in aliases:
                if alias not in normalized:
                    continue
                raw_value = normalized[alias]
                try:
                    value = Decimal(cls._decimal(raw_value, name, positive=True))
                except MarketplaceDraftValidationError:
                    continue
                if alias.endswith("_mm"):
                    return cls._decimal(value, name, positive=True), "MILLIMETERS"
                if alias.endswith("_cm"):
                    return cls._decimal(value * 10, name, positive=True), "MILLIMETERS"
                if alias.endswith("_inch") or alias.endswith("_in"):
                    return cls._decimal(value, name, positive=True), "INCHES"
                unit = str(
                    normalized.get("dimension_unit")
                    or normalized.get("unit")
                    or ""
                ).strip().upper()
                unit_aliases = {
                    "MM": "MILLIMETERS",
                    "ММ": "MILLIMETERS",
                    "CM": "CENTIMETERS",
                    "СМ": "CENTIMETERS",
                    "IN": "INCHES",
                    "INCH": "INCHES",
                }
                unit = unit_aliases.get(unit, unit)
                return (
                    cls._decimal(value, name, positive=True),
                    unit if unit in cls.DIMENSION_UNITS else None,
                )
            return None, None

        width, width_unit = dimension(
            "width",
            ("package_width_mm", "package_width_cm", "width_mm", "width_cm", "width"),
        )
        height, height_unit = dimension(
            "height",
            ("package_height_mm", "package_height_cm", "height_mm", "height_cm", "height"),
        )
        depth, depth_unit = dimension(
            "depth",
            (
                "package_depth_mm", "package_length_mm", "package_depth_cm",
                "package_length_cm", "depth_mm", "length_mm", "depth_cm",
                "length_cm", "depth", "length",
            ),
        )
        result = {}
        if width:
            result["width"] = width
        if height:
            result["height"] = height
        if depth:
            result["depth"] = depth
        units = {item for item in (width_unit, height_unit, depth_unit) if item}
        if len(units) == 1:
            result["dimension_unit"] = units.pop()

        for alias in (
            "package_weight_g", "weight_g", "package_weight_kg", "weight_kg", "weight"
        ):
            if alias not in normalized:
                continue
            try:
                value = Decimal(cls._decimal(normalized[alias], "weight", positive=True))
            except MarketplaceDraftValidationError:
                continue
            if alias.endswith("_g"):
                result["weight"] = cls._decimal(value, "weight", positive=True)
                result["weight_unit"] = "GRAMS"
            elif alias.endswith("_kg"):
                result["weight"] = cls._decimal(value * 1000, "weight", positive=True)
                result["weight_unit"] = "GRAMS"
            else:
                unit = str(normalized.get("weight_unit") or "").strip().upper()
                unit_aliases = {
                    "G": "GRAMS",
                    "Г": "GRAMS",
                    "KG": "KILOGRAMS",
                    "КГ": "KILOGRAMS",
                    "LB": "POUNDS",
                }
                unit = unit_aliases.get(unit, unit)
                result["weight"] = cls._decimal(value, "weight", positive=True)
                if unit in cls.WEIGHT_UNITS:
                    result["weight_unit"] = unit
            break
        return result

    @classmethod
    def _content_from_facts(cls, facts_document: dict) -> dict:
        identity = facts_document.get("facts", {}).get("identity", {})
        result = {}
        name = identity.get("title")
        description = identity.get("description")
        if isinstance(name, str) and name.strip():
            result["name"] = name.strip()[:500]
        if isinstance(description, str) and description.strip():
            result["description"] = description.strip()[:100_000]
        return result

    @classmethod
    def _media_from_facts(cls, facts_document: dict) -> dict:
        images = facts_document.get("facts", {}).get("media", {}).get("images", [])
        if not isinstance(images, list):
            return {}
        normalized = []
        seen = set()
        for raw in images[: cls.MAX_IMAGES]:
            if not isinstance(raw, str):
                continue
            value = raw.strip()
            if not value or len(value) > 2_000 or value in seen:
                continue
            seen.add(value)
            normalized.append(value)
        return {"images": normalized} if normalized else {}

    @classmethod
    def _barcodes_from_facts(cls, facts_document: dict) -> list:
        values = facts_document.get("facts", {}).get("identifiers", {}).get(
            "barcodes", []
        )
        if not isinstance(values, list):
            return []
        result = []
        seen = set()
        for value in values[: cls.MAX_BARCODES]:
            if not isinstance(value, str):
                continue
            value = value.strip()
            if value and len(value) <= 100 and value not in seen:
                seen.add(value)
                result.append(value)
        return result

    @classmethod
    def _commercial_from_facts(cls, facts_document: dict) -> dict:
        source = facts_document.get("facts", {}).get("commercial", {})
        result = {}
        for key in ("price", "old_price"):
            value = cls._decimal_from_fact(source.get(key))
            if value:
                result[key] = value
        return result

    @staticmethod
    def _attribute_candidate_values(facts_document: dict) -> Dict[str, Any]:
        facts = facts_document.get("facts", {})
        identity = facts.get("identity", {}) if isinstance(facts, dict) else {}
        attributes = facts.get("attributes", {}) if isinstance(facts, dict) else {}
        candidates: Dict[str, Any] = {}
        characteristics = attributes.get("characteristics", [])
        if isinstance(characteristics, list):
            for item in characteristics:
                if not isinstance(item, dict):
                    continue
                name = item.get("name")
                value = item.get("value")
                if isinstance(name, str) and value not in (None, "", [], {}):
                    candidates[MarketplaceDraftService._normalized_text(name)] = value
        aliases = {
            "brand": ("бренд", "brand"),
            "country": ("страна производства", "страна-изготовитель", "country of origin"),
            "gender": ("пол", "gender"),
            "season": ("сезон", "season"),
            "age_group": ("возрастная группа", "age group"),
            "colors": ("цвет", "цвет товара", "color"),
            "materials": ("материал", "материал изделия", "material"),
            "sizes": ("размер", "размер товара", "size"),
        }
        source_values = {
            "brand": identity.get("brand"),
            "country": attributes.get("country"),
            "gender": attributes.get("gender"),
            "season": attributes.get("season"),
            "age_group": attributes.get("age_group"),
            "colors": attributes.get("colors"),
            "materials": attributes.get("materials"),
            "sizes": attributes.get("sizes"),
        }
        for source_key, names in aliases.items():
            value = source_values.get(source_key)
            if value in (None, "", [], {}):
                continue
            for name in names:
                candidates.setdefault(
                    MarketplaceDraftService._normalized_text(name),
                    value,
                )
        return candidates

    @classmethod
    def _value_strings(cls, raw_value: Any) -> list:
        values = raw_value if isinstance(raw_value, list) else [raw_value]
        result = []
        seen = set()
        for value in values[: cls.MAX_ATTRIBUTE_VALUES]:
            if isinstance(value, bool):
                rendered = "true" if value else "false"
            elif isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
                try:
                    rendered = cls._decimal(value, "attribute value")
                except MarketplaceDraftValidationError:
                    continue
            elif isinstance(value, str):
                rendered = value.strip()
            else:
                continue
            if not rendered or len(rendered) > 1_000 or rendered in seen:
                continue
            seen.add(rendered)
            result.append(rendered)
        return result

    @classmethod
    def _auto_map_attributes(
        cls,
        *,
        product_type: MarketplaceProductType,
        facts_document: dict,
    ) -> list:
        if not OzonReferenceService.reference_is_fresh(product_type):
            return []
        definitions = MarketplaceAttributeDefinition.query.filter_by(
            product_type_id=product_type.id,
            is_available=True,
            is_enabled=True,
        ).order_by(MarketplaceAttributeDefinition.sort_order.asc()).all()
        candidates = cls._attribute_candidate_values(facts_document)
        matched: List[Tuple[MarketplaceAttributeDefinition, list]] = []
        for attribute in definitions:
            if attribute.attribute_complex_id:
                continue
            raw_value = candidates.get(cls._normalized_text(attribute.name))
            values = cls._value_strings(raw_value) if raw_value is not None else []
            if attribute.max_value_count:
                values = values[: attribute.max_value_count]
            if values:
                matched.append((attribute, values))

        dictionary_pairs = []
        attribute_ids = set()
        normalized_values = set()
        for attribute, values in matched:
            if not attribute.dictionary_id:
                continue
            if not OzonReferenceService.dictionary_is_fresh(attribute):
                continue
            for value in values:
                normalized = OzonReferenceService.normalize_value(value)
                dictionary_pairs.append((attribute, value, normalized))
                attribute_ids.add(attribute.id)
                normalized_values.add(normalized)

        value_rows: Dict[Tuple[int, str], list] = {}
        if attribute_ids and normalized_values:
            attribute_id_list = list(attribute_ids)
            normalized_value_list = list(normalized_values)
            for attribute_chunk_start in range(0, len(attribute_ids), 300):
                attribute_chunk = attribute_id_list[
                    attribute_chunk_start:attribute_chunk_start + 300
                ]
                for value_chunk_start in range(0, len(normalized_value_list), 500):
                    value_chunk = normalized_value_list[
                        value_chunk_start:value_chunk_start + 500
                    ]
                    rows = MarketplaceAttributeValue.query.filter(
                        MarketplaceAttributeValue.attribute_id.in_(attribute_chunk),
                        MarketplaceAttributeValue.value_normalized.in_(value_chunk),
                        MarketplaceAttributeValue.is_available.is_(True),
                    ).all()
                    for row in rows:
                        value_rows.setdefault(
                            (row.attribute_id, row.value_normalized),
                            [],
                        ).append(row)

        result = []
        for attribute, values in matched:
            canonical_values = []
            if attribute.dictionary_id:
                if not OzonReferenceService.dictionary_is_fresh(attribute):
                    continue
                restriction = set(attribute.restriction_value_ids)
                for value in values:
                    key = (
                        attribute.id,
                        OzonReferenceService.normalize_value(value),
                    )
                    matches = value_rows.get(key, [])
                    if len(matches) != 1:
                        continue
                    row = matches[0]
                    if restriction and row.external_value_id not in restriction:
                        continue
                    canonical_values.append({
                        "dictionary_value_id": row.external_value_id,
                        "value": row.value,
                    })
            else:
                canonical_values = [{"value": value} for value in values]
            if not canonical_values:
                continue
            result.append({
                "attribute_id": attribute.external_attribute_id,
                "complex_id": attribute.attribute_complex_id or "0",
                "values": canonical_values,
            })
        return result

    @classmethod
    def _bind_type(
        cls,
        draft: MarketplaceProductDraft,
        product_type: MarketplaceProductType,
    ) -> None:
        draft.product_type_id = product_type.id
        draft.external_category_id = product_type.category.external_category_id
        draft.external_type_id = product_type.external_type_id
        draft.schema_version = None
        draft.schema_hash = None
        draft.validation_status = "stale"
        draft.validation_result_json = '{}'
        draft.validated_at = None
        draft.status = "draft"

    @classmethod
    def create_draft(
        cls,
        *,
        seller_id: int,
        account_id: int,
        imported_product_id: int,
        product_type_id: Optional[int] = None,
        offer_id: Optional[Any] = None,
        save_mapping: bool = False,
        corrected_by_user_id: Optional[int] = None,
    ) -> MarketplaceProductDraft:
        if not isinstance(save_mapping, bool):
            raise MarketplaceDraftValidationError("save_mapping должен быть boolean")
        if product_type_id is not None:
            product_type_id = cls._positive_integer(
                product_type_id,
                "product_type_id",
            )
        if offer_id is not None:
            offer_id = cls._text(offer_id, "offer_id", maximum=200)
        if save_mapping and product_type_id is None:
            raise MarketplaceDraftValidationError(
                "save_mapping требует product_type_id"
            )
        account = cls._owned_account(seller_id=seller_id, account_id=account_id)
        product = cls._owned_imported_product(
            seller_id=seller_id,
            imported_product_id=imported_product_id,
        )
        existing = MarketplaceProductDraft.query.filter_by(
            seller_id=seller_id,
            account_id=account.id,
            imported_product_id=product.id,
        ).first()
        if existing is not None:
            linked_listing = MarketplaceListing.query.filter_by(
                seller_id=seller_id,
                account_id=account.id,
                imported_product_id=product.id,
                offer_id=existing.offer_id,
            ).first()
            if (
                linked_listing is not None
                and existing.published_listing_id is None
            ):
                existing.published_listing_id = linked_listing.id
                db.session.commit()
            return cls.get_draft(seller_id=seller_id, draft_id=existing.id)

        facts_document, provenance, fact_hash = cls._fact_snapshot(product)
        normalized_offer = offer_id or cls._derive_offer_id(product, facts_document)
        duplicate_listing = MarketplaceListing.query.filter_by(
            account_id=account.id,
            offer_id=normalized_offer,
        ).first()
        if duplicate_listing is not None and (
            duplicate_listing.seller_id != seller_id
            or duplicate_listing.marketplace_id != account.marketplace_id
            or duplicate_listing.imported_product_id != product.id
        ):
            raise MarketplaceDraftConflict(
                "offer_id принадлежит листингу без подтверждённой связи с этой внутренней карточкой"
            )

        selected_type = None
        mapping = None
        if product_type_id is not None:
            selected_type = cls._product_type(
                marketplace_id=account.marketplace_id,
                product_type_id=product_type_id,
            )
        elif duplicate_listing is not None and duplicate_listing.product_type:
            selected_type = duplicate_listing.product_type
        else:
            mapping = cls._active_mapping(
                seller_id=seller_id,
                marketplace_id=account.marketplace_id,
                product=product,
            )
            selected_type = mapping.product_type if mapping else None

        draft = MarketplaceProductDraft(
            seller_id=seller_id,
            marketplace_id=account.marketplace_id,
            account_id=account.id,
            imported_product_id=product.id,
            supplier_product_id=product.supplier_product_id,
            published_listing_id=(
                duplicate_listing.id if duplicate_listing is not None else None
            ),
            category_mapping_id=mapping.id if mapping else None,
            offer_id=normalized_offer,
            status="needs_category",
            source_fact_hash=fact_hash,
            source_facts_json=cls._canonical_json(facts_document, dict),
            provenance_json=cls._canonical_json(provenance, dict),
            content_json=cls._canonical_json(
                cls._content_from_facts(facts_document), dict
            ),
            media_json=cls._canonical_json(
                cls._media_from_facts(facts_document), dict
            ),
            dimensions_json=cls._canonical_json(
                cls._dimensions_from_facts(facts_document), dict
            ),
            barcodes_json=cls._canonical_json(
                cls._barcodes_from_facts(facts_document), list
            ),
            commercial_json=cls._canonical_json(
                cls._commercial_from_facts(facts_document), dict
            ),
            attributes_json='[]',
            complex_attributes_json='[]',
            validation_status="never_validated",
            validation_result_json='{}',
        )
        db.session.add(draft)
        if selected_type:
            cls._bind_type(draft, selected_type)
            draft.attributes_json = cls._canonical_json(
                cls._auto_map_attributes(
                    product_type=selected_type,
                    facts_document=facts_document,
                ),
                list,
            )
            if save_mapping:
                mapping = cls._upsert_mapping(
                    seller_id=seller_id,
                    marketplace_id=account.marketplace_id,
                    product=product,
                    product_type=selected_type,
                    corrected_by_user_id=corrected_by_user_id,
                )
                draft.category_mapping_id = mapping.id
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            existing = MarketplaceProductDraft.query.filter_by(
                seller_id=seller_id,
                account_id=account.id,
                imported_product_id=product.id,
            ).first()
            if existing is not None:
                return cls.get_draft(seller_id=seller_id, draft_id=existing.id)
            raise MarketplaceDraftConflict(
                "offer_id уже используется другим черновиком кабинета"
            ) from None
        return cls.get_draft(seller_id=seller_id, draft_id=draft.id)

    @classmethod
    def _normalize_content(cls, value: Any) -> dict:
        if not isinstance(value, dict) or set(value) - {"name", "description"}:
            raise MarketplaceDraftValidationError(
                "content должен содержать только name и description"
            )
        return {
            "name": cls._optional_text(
                value.get("name"), "content.name", maximum=500
            ),
            "description": cls._optional_text(
                value.get("description"),
                "content.description",
                maximum=100_000,
                multiline=True,
            ),
        }

    @classmethod
    def _normalize_value_items(cls, value: Any, field_name: str) -> list:
        if not isinstance(value, list) or len(value) > cls.MAX_ATTRIBUTE_VALUES:
            raise MarketplaceDraftValidationError(
                f"{field_name} должен быть массивом до {cls.MAX_ATTRIBUTE_VALUES} значений"
            )
        result = []
        for index, item in enumerate(value):
            if not isinstance(item, dict) or set(item) - {
                "dictionary_value_id", "value"
            }:
                raise MarketplaceDraftValidationError(
                    f"{field_name}[{index}] имеет неизвестные поля"
                )
            dictionary_value_id = item.get("dictionary_value_id")
            normalized = {
                "value": cls._text(
                    item.get("value"),
                    f"{field_name}[{index}].value",
                    maximum=1_000,
                    multiline=True,
                )
            }
            if dictionary_value_id not in (None, ""):
                normalized["dictionary_value_id"] = cls._external_id(
                    dictionary_value_id,
                    f"{field_name}[{index}].dictionary_value_id",
                )
            result.append(normalized)
        return result

    @classmethod
    def _normalize_attribute_items(cls, value: Any, field_name: str) -> list:
        if not isinstance(value, list) or len(value) > cls.MAX_ATTRIBUTES:
            raise MarketplaceDraftValidationError(
                f"{field_name} должен быть массивом до {cls.MAX_ATTRIBUTES} элементов"
            )
        result = []
        seen = set()
        for index, item in enumerate(value):
            if not isinstance(item, dict) or set(item) - {
                "attribute_id", "complex_id", "values"
            }:
                raise MarketplaceDraftValidationError(
                    f"{field_name}[{index}] имеет неизвестные поля"
                )
            attribute_id = cls._external_id(
                item.get("attribute_id"),
                f"{field_name}[{index}].attribute_id",
            )
            complex_raw = item.get("complex_id", "0")
            if complex_raw == "0":
                complex_id = "0"
            else:
                complex_id = cls._external_id(
                    complex_raw,
                    f"{field_name}[{index}].complex_id",
                )
            identity = (attribute_id, complex_id)
            if identity in seen:
                raise MarketplaceDraftValidationError(
                    f"{field_name} содержит дубликат attribute_id/complex_id"
                )
            seen.add(identity)
            result.append({
                "attribute_id": attribute_id,
                "complex_id": complex_id,
                "values": cls._normalize_value_items(
                    item.get("values"),
                    f"{field_name}[{index}].values",
                ),
            })
        return result

    @classmethod
    def _normalize_complex_attributes(cls, value: Any) -> list:
        if not isinstance(value, list) or len(value) > cls.MAX_COMPLEX_GROUPS:
            raise MarketplaceDraftValidationError(
                f"complex_attributes должен быть массивом до {cls.MAX_COMPLEX_GROUPS} групп"
            )
        result = []
        total_attributes = 0
        for index, group in enumerate(value):
            if not isinstance(group, dict) or set(group) != {"attributes"}:
                raise MarketplaceDraftValidationError(
                    f"complex_attributes[{index}] должен содержать только attributes"
                )
            attributes = cls._normalize_attribute_items(
                group.get("attributes"),
                f"complex_attributes[{index}].attributes",
            )
            total_attributes += len(attributes)
            if total_attributes > cls.MAX_ATTRIBUTES:
                raise MarketplaceDraftValidationError(
                    "complex_attributes содержит слишком много атрибутов суммарно"
                )
            result.append({"attributes": attributes})
        return result

    @classmethod
    def _normalize_dimensions(cls, value: Any) -> dict:
        allowed = {
            "width", "height", "depth", "dimension_unit", "weight", "weight_unit"
        }
        if not isinstance(value, dict) or set(value) - allowed:
            raise MarketplaceDraftValidationError(
                "dimensions содержит неизвестные поля"
            )
        result = {}
        for field_name in ("width", "height", "depth", "weight"):
            if value.get(field_name) not in (None, ""):
                result[field_name] = cls._decimal(
                    value[field_name],
                    f"dimensions.{field_name}",
                    positive=True,
                )
        if value.get("dimension_unit") not in (None, ""):
            unit = cls._text(
                value["dimension_unit"],
                "dimensions.dimension_unit",
                maximum=30,
            ).upper()
            if unit not in cls.DIMENSION_UNITS:
                raise MarketplaceDraftValidationError(
                    "Неизвестная единица габаритов"
                )
            result["dimension_unit"] = unit
        if value.get("weight_unit") not in (None, ""):
            unit = cls._text(
                value["weight_unit"],
                "dimensions.weight_unit",
                maximum=30,
            ).upper()
            if unit not in cls.WEIGHT_UNITS:
                raise MarketplaceDraftValidationError("Неизвестная единица веса")
            result["weight_unit"] = unit
        return result

    @classmethod
    def _normalize_media(cls, value: Any) -> dict:
        allowed = {"images", "primary_image", "color_image"}
        if not isinstance(value, dict) or set(value) - allowed:
            raise MarketplaceDraftValidationError(
                "media поддерживает только images, primary_image и color_image; "
                "images360 больше не поддерживается Ozon"
            )
        images = value.get("images", [])
        primary_image = value.get("primary_image")
        if primary_image not in (None, ""):
            primary_image = cls._text(
                primary_image,
                "media.primary_image",
                maximum=2_000,
            )
        else:
            primary_image = None
        maximum_images = cls.MAX_IMAGES - (1 if primary_image else 0)
        if not isinstance(images, list) or len(images) > maximum_images:
            raise MarketplaceDraftValidationError(
                f"media.images должен быть массивом до {maximum_images} URL"
            )
        result = []
        seen = {primary_image} if primary_image else set()
        for index, image in enumerate(images):
            image = cls._text(
                image,
                f"media.images[{index}]",
                maximum=2_000,
            )
            if image in seen:
                raise MarketplaceDraftValidationError(
                    "media.images содержит дубликат"
                )
            seen.add(image)
            result.append(image)
        normalized = {"images": result}
        if primary_image:
            normalized["primary_image"] = primary_image
        color_image = value.get("color_image")
        if color_image not in (None, ""):
            color_image = cls._text(
                color_image,
                "media.color_image",
                maximum=2_000,
            )
            if color_image in seen:
                raise MarketplaceDraftValidationError(
                    "media.color_image не должен дублировать основную фотографию"
                )
            normalized["color_image"] = color_image
        return normalized

    @classmethod
    def _normalize_barcodes(cls, value: Any) -> list:
        if not isinstance(value, list) or len(value) > cls.MAX_BARCODES:
            raise MarketplaceDraftValidationError(
                f"barcodes должен быть массивом до {cls.MAX_BARCODES} значений"
            )
        result = []
        seen = set()
        for index, barcode in enumerate(value):
            barcode = cls._text(
                barcode,
                f"barcodes[{index}]",
                maximum=100,
            )
            if barcode in seen:
                raise MarketplaceDraftValidationError("barcodes содержит дубликат")
            seen.add(barcode)
            result.append(barcode)
        return result

    @classmethod
    def _normalize_commercial(cls, value: Any) -> dict:
        if not isinstance(value, dict) or set(value) - {
            "price", "old_price", "vat", "currency_code"
        }:
            raise MarketplaceDraftValidationError(
                "commercial содержит неизвестные поля"
            )
        result = {}
        for key in ("price", "old_price"):
            if value.get(key) not in (None, ""):
                result[key] = cls._decimal(
                    value[key],
                    f"commercial.{key}",
                    positive=True,
                )
        if value.get("vat") not in (None, ""):
            vat = cls._text(value["vat"], "commercial.vat", maximum=10)
            if vat not in cls.VAT_VALUES:
                raise MarketplaceDraftValidationError(
                    "commercial.vat не входит в поддерживаемый Ozon enum"
                )
            result["vat"] = vat
        if value.get("currency_code") not in (None, ""):
            currency_code = cls._text(
                value["currency_code"],
                "commercial.currency_code",
                maximum=3,
            ).upper()
            if currency_code not in cls.CURRENCY_CODES:
                raise MarketplaceDraftValidationError(
                    "На текущем этапе поддерживается только currency_code=RUB"
                )
            result["currency_code"] = currency_code
        return result

    @classmethod
    def update_draft(
        cls,
        *,
        seller_id: int,
        draft_id: int,
        expected_version: int,
        patch: Dict[str, Any],
        corrected_by_user_id: Optional[int] = None,
    ) -> MarketplaceProductDraft:
        expected_version = cls._positive_integer(expected_version, "expected_version")
        if not isinstance(patch, dict) or not patch:
            raise MarketplaceDraftValidationError("patch должен быть непустым объектом")
        allowed = {
            "offer_id", "product_type_id", "save_mapping", "content",
            "attributes", "complex_attributes", "media", "dimensions",
            "barcodes", "commercial",
        }
        unknown = set(patch) - allowed
        if unknown:
            raise MarketplaceDraftValidationError(
                "patch содержит неизвестные поля: " + ", ".join(sorted(unknown))
            )
        draft = cls.get_draft(seller_id=seller_id, draft_id=draft_id)
        if draft.version != expected_version:
            raise MarketplaceDraftConflict(
                "Черновик изменился; обновите страницу и повторите"
            )
        if draft.status == "archived":
            raise MarketplaceDraftConflict(
                "Архивный черновик нельзя редактировать до восстановления listing"
            )
        cls._assert_no_active_publication(draft)

        product_type_changed = False
        if "offer_id" in patch:
            offer_id = cls._text(patch["offer_id"], "offer_id", maximum=200)
            conflict = MarketplaceProductDraft.query.filter(
                MarketplaceProductDraft.account_id == draft.account_id,
                MarketplaceProductDraft.offer_id == offer_id,
                MarketplaceProductDraft.id != draft.id,
            ).first()
            listing = MarketplaceListing.query.filter_by(
                account_id=draft.account_id,
                offer_id=offer_id,
            ).first()
            if conflict or (listing and listing.id != draft.published_listing_id):
                raise MarketplaceDraftConflict(
                    "offer_id уже используется в этом кабинете"
                )
            draft.offer_id = offer_id

        selected_type = draft.product_type
        if "product_type_id" in patch:
            if patch["product_type_id"] is None:
                selected_type = None
                draft.product_type_id = None
                draft.category_mapping_id = None
                draft.external_category_id = None
                draft.external_type_id = None
                draft.attributes_json = '[]'
                draft.complex_attributes_json = '[]'
                draft.status = "needs_category"
                product_type_changed = True
            else:
                selected_type = cls._product_type(
                    marketplace_id=draft.marketplace_id,
                    product_type_id=patch["product_type_id"],
                )
                if selected_type.id != draft.product_type_id:
                    cls._bind_type(draft, selected_type)
                    facts_document = cls._stored_json(draft.source_facts_json, dict)
                    draft.attributes_json = cls._canonical_json(
                        cls._auto_map_attributes(
                            product_type=selected_type,
                            facts_document=facts_document,
                        ),
                        list,
                    )
                    draft.complex_attributes_json = '[]'
                    draft.category_mapping_id = None
                    product_type_changed = True

        save_mapping = patch.get("save_mapping", False)
        if "save_mapping" in patch:
            save_mapping = cls._strict_boolean(save_mapping, "save_mapping")
        if save_mapping:
            if selected_type is None:
                raise MarketplaceDraftValidationError(
                    "save_mapping требует выбранный product_type_id"
                )
            mapping = cls._upsert_mapping(
                seller_id=seller_id,
                marketplace_id=draft.marketplace_id,
                product=draft.imported_product,
                product_type=selected_type,
                corrected_by_user_id=corrected_by_user_id,
            )
            draft.category_mapping_id = mapping.id

        field_normalizers = {
            "content": (cls._normalize_content, "content_json", dict),
            "attributes": (
                lambda value: cls._normalize_attribute_items(value, "attributes"),
                "attributes_json",
                list,
            ),
            "complex_attributes": (
                cls._normalize_complex_attributes,
                "complex_attributes_json",
                list,
            ),
            "media": (cls._normalize_media, "media_json", dict),
            "dimensions": (cls._normalize_dimensions, "dimensions_json", dict),
            "barcodes": (cls._normalize_barcodes, "barcodes_json", list),
            "commercial": (cls._normalize_commercial, "commercial_json", dict),
        }
        for patch_name, (normalizer, column_name, expected_type) in field_normalizers.items():
            if patch_name in patch:
                normalized = normalizer(patch[patch_name])
                setattr(
                    draft,
                    column_name,
                    cls._canonical_json(normalized, expected_type),
                )

        if draft.product_type_id:
            draft.status = "draft"
        elif product_type_changed:
            draft.status = "needs_category"
        draft.validation_status = "stale"
        draft.validation_result_json = cls._canonical_json({
            "publishable": False,
            "errors": [{
                "code": "validation_stale",
                "field": "draft",
                "message": "Черновик изменён и требует повторной валидации",
            }],
            "warnings": [],
        }, dict)
        draft.validated_at = None
        draft.updated_at = datetime.utcnow()
        try:
            db.session.commit()
        except StaleDataError:
            db.session.rollback()
            raise MarketplaceDraftConflict(
                "Черновик изменился параллельно; повторите после обновления"
            ) from None
        except IntegrityError:
            db.session.rollback()
            raise MarketplaceDraftConflict(
                "Черновик конфликтует с offer/category mapping этого кабинета"
            ) from None
        return cls.get_draft(seller_id=seller_id, draft_id=draft_id)

    @classmethod
    def refresh_facts(
        cls,
        *,
        seller_id: int,
        draft_id: int,
        expected_version: int,
    ) -> MarketplaceProductDraft:
        expected_version = cls._positive_integer(expected_version, "expected_version")
        draft = cls.get_draft(seller_id=seller_id, draft_id=draft_id)
        if draft.version != expected_version:
            raise MarketplaceDraftConflict(
                "Черновик изменился; обновите страницу и повторите"
            )
        if draft.status == "archived":
            raise MarketplaceDraftConflict(
                "Архивный черновик нельзя обновлять до восстановления listing"
            )
        cls._assert_no_active_publication(draft)
        facts_document, provenance, fact_hash = cls._fact_snapshot(
            draft.imported_product
        )
        draft.source_facts_json = cls._canonical_json(facts_document, dict)
        draft.provenance_json = cls._canonical_json(provenance, dict)
        draft.source_fact_hash = fact_hash
        draft.validation_status = "stale"
        draft.validation_result_json = cls._canonical_json({
            "publishable": False,
            "errors": [{
                "code": "facts_refreshed",
                "field": "source_facts",
                "message": "Факты обновлены; пользовательские поля не перезаписаны",
            }],
            "warnings": [],
        }, dict)
        draft.validated_at = None
        if draft.product_type_id:
            draft.status = "draft"
        try:
            db.session.commit()
        except StaleDataError:
            db.session.rollback()
            raise MarketplaceDraftConflict(
                "Черновик изменился параллельно; повторите после обновления"
            ) from None
        return cls.get_draft(seller_id=seller_id, draft_id=draft_id)

    @staticmethod
    def _validation_item(code: str, field: str, message: str) -> dict:
        return {"code": code, "field": field, "message": message}

    @classmethod
    def _attribute_type_error(
        cls,
        attribute: MarketplaceAttributeDefinition,
        value: str,
    ) -> Optional[str]:
        data_type = cls._normalized_text(attribute.data_type)
        if data_type not in cls.DATA_TYPES:
            return "unsupported"
        if data_type == "string":
            return None if value.strip() else "string"
        if data_type == "integer":
            return None if re.fullmatch(r"-?(?:0|[1-9]\d*)", value) else "integer"
        if data_type == "decimal":
            return None if re.fullmatch(
                r"-?(?:0|[1-9]\d*)(?:\.\d+)?", value
            ) else "decimal"
        if data_type == "boolean":
            return None if value in {"true", "false"} else "boolean"
        return "unsupported"

    @classmethod
    def _validate_attributes(
        cls,
        *,
        product_type: MarketplaceProductType,
        attributes: list,
        complex_groups: list,
        errors: list,
        implicitly_supplied: Optional[set] = None,
    ) -> None:
        implicitly_supplied = implicitly_supplied or set()
        definitions = MarketplaceAttributeDefinition.query.filter_by(
            product_type_id=product_type.id,
            is_available=True,
        ).all()
        definitions_by_external = {
            item.external_attribute_id: item for item in definitions
        }
        occurrences: Dict[str, int] = {}
        supplied: List[Tuple[dict, MarketplaceAttributeDefinition, str]] = []

        def collect(items: Any, container: str, group_index: Optional[int] = None) -> None:
            if not isinstance(items, list):
                errors.append(cls._validation_item(
                    "malformed_attributes", container, "Атрибуты повреждены"
                ))
                return
            seen = set()
            for index, item in enumerate(items):
                path = f"{container}[{index}]"
                if not isinstance(item, dict):
                    errors.append(cls._validation_item(
                        "malformed_attribute", path, "Атрибут должен быть объектом"
                    ))
                    continue
                external_id = item.get("attribute_id")
                complex_id = item.get("complex_id", "0")
                identity = (external_id, complex_id)
                if identity in seen:
                    errors.append(cls._validation_item(
                        "duplicate_attribute", path, "Атрибут продублирован в группе"
                    ))
                    continue
                seen.add(identity)
                definition = definitions_by_external.get(external_id)
                if definition is None:
                    errors.append(cls._validation_item(
                        "unknown_attribute", path, "Атрибут отсутствует в текущей Ozon schema"
                    ))
                    continue
                expected_complex = definition.attribute_complex_id or "0"
                if complex_id != expected_complex:
                    errors.append(cls._validation_item(
                        "complex_id_mismatch", path, "complex_id не совпадает со schema"
                    ))
                if container == "attributes" and definition.attribute_complex_id:
                    errors.append(cls._validation_item(
                        "complex_attribute_outside_group", path,
                        "Complex-атрибут должен находиться в complex_attributes",
                    ))
                if container != "attributes" and not definition.attribute_complex_id:
                    errors.append(cls._validation_item(
                        "simple_attribute_inside_group", path,
                        "Обычный атрибут не должен находиться в complex_attributes",
                    ))
                if not definition.is_enabled:
                    errors.append(cls._validation_item(
                        "attribute_disabled", path, "Атрибут отключён администратором"
                    ))
                values = item.get("values")
                if not isinstance(values, list) or not values:
                    errors.append(cls._validation_item(
                        "attribute_values_empty", path, "У атрибута нет значений"
                    ))
                    continue
                if len(values) > cls.MAX_ATTRIBUTE_VALUES:
                    errors.append(cls._validation_item(
                        "attribute_values_limit", path, "Слишком много значений атрибута"
                    ))
                    continue
                if definition.max_value_count and len(values) > definition.max_value_count:
                    errors.append(cls._validation_item(
                        "attribute_max_value_count", path,
                        f"Допустимо не более {definition.max_value_count} значений",
                    ))
                if not definition.is_collection and len(values) > 1:
                    errors.append(cls._validation_item(
                        "attribute_not_collection", path,
                        f"Атрибут «{definition.name}» принимает одно значение",
                    ))
                occurrences[external_id] = occurrences.get(external_id, 0) + 1
                supplied.append((item, definition, path))

        collect(attributes, "attributes")
        if not isinstance(complex_groups, list):
            errors.append(cls._validation_item(
                "malformed_complex_attributes", "complex_attributes",
                "Complex-атрибуты повреждены",
            ))
        else:
            for group_index, group in enumerate(complex_groups):
                path = f"complex_attributes[{group_index}]"
                if not isinstance(group, dict) or not isinstance(group.get("attributes"), list):
                    errors.append(cls._validation_item(
                        "malformed_complex_group", path,
                        "Complex-группа должна содержать attributes",
                    ))
                    continue
                collect(group["attributes"], f"{path}.attributes", group_index)

        for definition in definitions:
            if definition.is_required and occurrences.get(
                definition.external_attribute_id, 0
            ) == 0 and definition.external_attribute_id not in implicitly_supplied:
                errors.append(cls._validation_item(
                    "required_attribute_missing",
                    f"attributes.{definition.external_attribute_id}",
                    f"Обязательный атрибут «{definition.name}» не заполнен",
                ))
            if (
                definition.attribute_complex_id
                and not definition.complex_is_collection
                and occurrences.get(definition.external_attribute_id, 0) > 1
            ):
                errors.append(cls._validation_item(
                    "complex_attribute_repeated",
                    f"attributes.{definition.external_attribute_id}",
                    f"Complex-атрибут «{definition.name}» не является коллекцией",
                ))
            if (
                definition.is_required
                and definition.dictionary_id
                and not OzonReferenceService.dictionary_is_fresh(definition)
            ):
                errors.append(cls._validation_item(
                    "dictionary_stale",
                    f"attributes.{definition.external_attribute_id}",
                    f"Справочник «{definition.name}» устарел или не синхронизирован",
                ))

        dictionary_requests: Dict[int, set] = {}
        for item, definition, path in supplied:
            if definition.dictionary_id:
                if not OzonReferenceService.dictionary_is_fresh(definition):
                    errors.append(cls._validation_item(
                        "dictionary_stale", path,
                        f"Справочник «{definition.name}» устарел или не синхронизирован",
                    ))
                    continue
                for value in item.get("values", []):
                    if not isinstance(value, dict):
                        continue
                    external_value_id = value.get("dictionary_value_id")
                    if not isinstance(external_value_id, str):
                        errors.append(cls._validation_item(
                            "dictionary_value_id_missing", path,
                            f"«{definition.name}» требует exact dictionary_value_id",
                        ))
                        continue
                    dictionary_requests.setdefault(definition.id, set()).add(
                        external_value_id
                    )
            else:
                for value in item.get("values", []):
                    if not isinstance(value, dict):
                        errors.append(cls._validation_item(
                            "malformed_attribute_value", path,
                            "Значение атрибута должно быть объектом",
                        ))
                        continue
                    if value.get("dictionary_value_id") not in (None, ""):
                        errors.append(cls._validation_item(
                            "unexpected_dictionary_value_id", path,
                            f"«{definition.name}» не является справочником",
                        ))
                    raw_value = value.get("value")
                    if not isinstance(raw_value, str):
                        errors.append(cls._validation_item(
                            "attribute_value_not_string", path,
                            "Ozon attribute value должен быть строкой",
                        ))
                        continue
                    type_error = cls._attribute_type_error(definition, raw_value)
                    if type_error == "unsupported":
                        errors.append(cls._validation_item(
                            "unsupported_attribute_type", path,
                            f"Тип Ozon «{definition.data_type}» не поддержан валидатором",
                        ))
                    elif type_error:
                        errors.append(cls._validation_item(
                            "attribute_type_mismatch", path,
                            f"«{definition.name}» ожидает {definition.data_type}",
                        ))

        resolved: Dict[Tuple[int, str], MarketplaceAttributeValue] = {}
        for attribute_id, ids in dictionary_requests.items():
            for offset in range(0, len(ids), 500):
                chunk = list(ids)[offset:offset + 500]
                rows = MarketplaceAttributeValue.query.filter(
                    MarketplaceAttributeValue.attribute_id == attribute_id,
                    MarketplaceAttributeValue.external_value_id.in_(chunk),
                    MarketplaceAttributeValue.is_available.is_(True),
                ).all()
                for row in rows:
                    resolved[(attribute_id, row.external_value_id)] = row

        for item, definition, path in supplied:
            if not definition.dictionary_id:
                continue
            restriction = set(definition.restriction_value_ids)
            for value in item.get("values", []):
                if not isinstance(value, dict):
                    continue
                external_id = value.get("dictionary_value_id")
                if not isinstance(external_id, str):
                    continue
                row = resolved.get((definition.id, external_id))
                if row is None:
                    errors.append(cls._validation_item(
                        "dictionary_value_out_of_scope", path,
                        f"Значение отсутствует в справочнике «{definition.name}» этого типа",
                    ))
                    continue
                if restriction and external_id not in restriction:
                    errors.append(cls._validation_item(
                        "dictionary_value_restricted", path,
                        f"Значение запрещено admin allowlist для «{definition.name}»",
                    ))
                if value.get("value") != row.value:
                    errors.append(cls._validation_item(
                        "dictionary_display_mismatch", path,
                        f"Display value для «{definition.name}» не совпадает с official value",
                    ))

    @classmethod
    def _build_validation_result(
        cls,
        draft: MarketplaceProductDraft,
    ) -> dict:
        errors: List[dict] = []
        warnings: List[dict] = []
        now = datetime.utcnow()

        if (
            draft.account.seller_id != draft.seller_id
            or draft.account.marketplace_id != draft.marketplace_id
            or draft.marketplace.code != "ozon"
        ):
            errors.append(cls._validation_item(
                "account_scope_mismatch", "account_id",
                "Кабинет не совпадает с seller/marketplace scope черновика",
            ))
        if not draft.account.is_active:
            errors.append(cls._validation_item(
                "account_inactive", "account_id", "Кабинет Ozon отключён"
            ))
        if draft.account.connection_status != "connected":
            errors.append(cls._validation_item(
                "account_not_connected", "account_id",
                "Кабинет Ozon должен пройти проверку подключения",
            ))
        if not draft.account.has_credentials:
            errors.append(cls._validation_item(
                "account_credentials_missing", "account_id",
                "В кабинете Ozon отсутствует API key",
            ))
        if (
            draft.account.credential_expires_at
            and draft.account.credential_expires_at <= now
        ):
            errors.append(cls._validation_item(
                "account_credentials_expired", "account_id",
                "Срок действия Ozon credentials истёк",
            ))

        try:
            current_pack = MarketplaceFactPackBuilder.build(draft.imported_product)
            if current_pack["fact_hash"] != draft.source_fact_hash:
                errors.append(cls._validation_item(
                    "source_facts_stale", "source_facts",
                    "Исходный товар изменился; обновите fact snapshot",
                ))
        except MarketplaceFactPackError:
            errors.append(cls._validation_item(
                "source_facts_unavailable", "source_facts",
                "Не удалось повторно проверить исходные факты",
            ))

        try:
            wb_projection = MarketplaceFactPackBuilder.wb_projection_drift(
                draft.imported_product
            )
        except MarketplaceFactPackError:
            wb_projection = {
                "linked": False,
                "differing_fields": [],
            }
        if wb_projection.get("differing_fields"):
            warnings.append(cls._validation_item(
                "wb_projection_differs_from_canonical",
                "source_facts",
                "Связанная WB-карточка отличается в общих полях: "
                + ", ".join(wb_projection["differing_fields"])
                + ". Ozon-черновик использует общую внутреннюю карточку; "
                "сначала проверьте diff, если нужна именно версия WB.",
            ))

        facts_document = cls._stored_json(draft.source_facts_json, dict)
        suggestions = facts_document.get("unverified_suggestions", {})
        if isinstance(suggestions, dict) and suggestions:
            warnings.append(cls._validation_item(
                "unverified_ai_suggestions_ignored", "source_facts",
                "Неподтверждённые legacy AI-предложения не включены автоматически",
            ))

        if not draft.offer_id:
            errors.append(cls._validation_item(
                "offer_id_required", "offer_id",
                "offer_id обязателен в /v3/product/import с 10.07.2026",
            ))
        elif len(draft.offer_id) > cls.MAX_OZON_OFFER_ID_CHARS:
            errors.append(cls._validation_item(
                "offer_id_too_long", "offer_id",
                f"offer_id длиннее {cls.MAX_OZON_OFFER_ID_CHARS} символов для /v3/product/import",
            ))
        listing = MarketplaceListing.query.filter_by(
            account_id=draft.account_id,
            offer_id=draft.offer_id,
        ).first()
        if listing and listing.id != draft.published_listing_id:
            errors.append(cls._validation_item(
                "offer_id_already_published", "offer_id",
                "offer_id уже принадлежит другой опубликованной карточке",
            ))

        product_type = draft.product_type
        if product_type is None:
            errors.append(cls._validation_item(
                "product_type_required", "product_type_id",
                "Выберите точную пару Ozon category/type",
            ))
        elif (
            product_type.marketplace_id != draft.marketplace_id
            or not product_type.is_enabled
            or not product_type.is_available
            or not product_type.category
            or not product_type.category.is_available
        ):
            errors.append(cls._validation_item(
                "product_type_unavailable", "product_type_id",
                "Выбранный Ozon product type недоступен",
            ))
        elif (
            draft.external_category_id != product_type.category.external_category_id
            or draft.external_type_id != product_type.external_type_id
        ):
            errors.append(cls._validation_item(
                "product_type_identity_mismatch", "product_type_id",
                "Сохранённая category/type identity не совпадает со schema",
            ))
        elif not OzonReferenceService.reference_is_fresh(product_type, now=now):
            errors.append(cls._validation_item(
                "schema_stale", "product_type_id",
                "Ozon category/attribute schema старше hard TTL или неполна",
            ))

        content = cls._stored_json(draft.content_json, dict)
        attributes = cls._stored_json(draft.attributes_json, list)
        implicit_attribute_ids = set()
        if not isinstance(content.get("name"), str) or not content.get("name", "").strip():
            errors.append(cls._validation_item(
                "name_required", "content.name", "Название товара обязательно"
            ))
        elif len(content["name"]) > 500:
            errors.append(cls._validation_item(
                "name_too_long", "content.name", "Название длиннее 500 символов"
            ))
        description_value = content.get("description")
        if (
            not isinstance(description_value, str)
            or not description_value.strip()
        ):
            errors.append(cls._validation_item(
                "description_required", "content.description",
                "Описание товара обязательно",
            ))

        if product_type is not None:
            description_definitions = MarketplaceAttributeDefinition.query.filter_by(
                product_type_id=product_type.id,
                external_attribute_id=cls.OZON_DESCRIPTION_ATTRIBUTE_ID,
                is_available=True,
                is_enabled=True,
            ).all()
            if len(description_definitions) != 1:
                errors.append(cls._validation_item(
                    "description_attribute_unavailable",
                    "content.description",
                    "Fresh Ozon schema должен содержать один enabled атрибут описания 4191",
                ))
            elif (
                description_definitions[0].dictionary_id
                or description_definitions[0].attribute_complex_id
            ):
                errors.append(cls._validation_item(
                    "description_attribute_unsupported",
                    "content.description",
                    "Атрибут описания 4191 имеет неподдерживаемую Ozon schema",
                ))
            elif isinstance(description_value, str) and description_value.strip():
                implicit_attribute_ids.add(cls.OZON_DESCRIPTION_ATTRIBUTE_ID)

        description_attributes = [
            item for item in attributes
            if isinstance(item, dict)
            and item.get("attribute_id") == cls.OZON_DESCRIPTION_ATTRIBUTE_ID
        ]
        if len(description_attributes) > 1:
            errors.append(cls._validation_item(
                "description_attribute_duplicated",
                "attributes.4191",
                "Атрибут описания 4191 продублирован",
            ))
        elif description_attributes and isinstance(description_value, str):
            values = description_attributes[0].get("values")
            if (
                not isinstance(values, list)
                or len(values) != 1
                or not isinstance(values[0], dict)
                or values[0].get("dictionary_value_id") not in (None, "")
                or not isinstance(values[0].get("value"), str)
                or values[0]["value"].strip() != description_value.strip()
            ):
                errors.append(cls._validation_item(
                    "description_attribute_conflict",
                    "attributes.4191",
                    "Атрибут 4191 должен точно совпадать с content.description",
                ))

        media = cls._stored_json(draft.media_json, dict)
        images = media.get("images") if isinstance(media, dict) else None
        primary_image = media.get("primary_image") if isinstance(media, dict) else None
        color_image = media.get("color_image") if isinstance(media, dict) else None
        if not isinstance(images, list) or (not images and not primary_image):
            errors.append(cls._validation_item(
                "images_required", "media", "Нужна хотя бы одна основная фотография"
            ))
        else:
            maximum_images = cls.MAX_IMAGES - (1 if primary_image else 0)
            if len(images) > maximum_images:
                errors.append(cls._validation_item(
                    "images_limit", "media.images",
                    f"Допустимо не более {maximum_images} фотографий в images",
                ))
            seen_images = set()
            checked_images = []
            if primary_image is not None:
                checked_images.append(("media.primary_image", primary_image))
            checked_images.extend(
                (f"media.images[{index}]", value)
                for index, value in enumerate(images)
            )
            if color_image is not None:
                checked_images.append(("media.color_image", color_image))
            for field, value in checked_images:
                if not isinstance(value, str) or len(value) > 2_000:
                    errors.append(cls._validation_item(
                        "image_url_invalid", field, "Некорректный URL изображения"
                    ))
                    continue
                parsed = urlsplit(value)
                if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                    errors.append(cls._validation_item(
                        "image_url_not_public", field,
                        "Ozon требует публичный HTTP(S) URL изображения",
                    ))
                if value in seen_images:
                    errors.append(cls._validation_item(
                        "image_duplicate", field, "URL изображения продублирован"
                    ))
                seen_images.add(value)
            if "images360" in media:
                errors.append(cls._validation_item(
                    "images360_removed", "media.images360",
                    "images360 удалён из /v3/product/import 10.07.2026",
                ))

        dimensions = cls._stored_json(draft.dimensions_json, dict)
        for field_name in ("width", "height", "depth", "weight"):
            try:
                rendered = cls._decimal(
                    dimensions.get(field_name),
                    f"dimensions.{field_name}",
                    positive=True,
                )
                parsed = Decimal(rendered)
                if parsed != parsed.to_integral_value():
                    errors.append(cls._validation_item(
                        "physical_fact_not_integer",
                        f"dimensions.{field_name}",
                        f"{field_name} должен быть целым числом в выбранной единице Ozon",
                    ))
            except MarketplaceDraftValidationError:
                errors.append(cls._validation_item(
                    "physical_fact_required",
                    f"dimensions.{field_name}",
                    f"{field_name} должен быть подтверждённым положительным значением",
                ))
        if dimensions.get("dimension_unit") not in cls.DIMENSION_UNITS:
            errors.append(cls._validation_item(
                "dimension_unit_required", "dimensions.dimension_unit",
                "Укажите поддерживаемую Ozon единицу габаритов",
            ))
        if dimensions.get("weight_unit") not in cls.WEIGHT_UNITS:
            errors.append(cls._validation_item(
                "weight_unit_required", "dimensions.weight_unit",
                "Укажите поддерживаемую Ozon единицу веса",
            ))

        barcodes = cls._stored_json(draft.barcodes_json, list)
        if len(barcodes) > cls.MAX_IMPORT_BARCODES:
            errors.append(cls._validation_item(
                "barcodes_limit", "barcodes",
                "/v3/product/import принимает один штрихкод; дополнительные добавляются отдельным workflow",
            ))
        seen_barcodes = set()
        for index, barcode in enumerate(barcodes):
            if (
                not isinstance(barcode, str)
                or not barcode.strip()
                or len(barcode) > 100
            ):
                errors.append(cls._validation_item(
                    "barcode_invalid", f"barcodes[{index}]", "Некорректный штрихкод"
                ))
            elif barcode in seen_barcodes:
                errors.append(cls._validation_item(
                    "barcode_duplicate", f"barcodes[{index}]", "Штрихкод продублирован"
                ))
            seen_barcodes.add(barcode)

        commercial = cls._stored_json(draft.commercial_json, dict)
        price = None
        try:
            price = Decimal(cls._decimal(
                commercial.get("price"), "commercial.price", positive=True
            ))
        except MarketplaceDraftValidationError:
            errors.append(cls._validation_item(
                "price_required", "commercial.price",
                "Цена продажи должна быть явно рассчитана и положительна",
            ))
        if commercial.get("old_price") not in (None, ""):
            try:
                old_price = Decimal(cls._decimal(
                    commercial["old_price"], "commercial.old_price", positive=True
                ))
                if price is not None and old_price <= price:
                    errors.append(cls._validation_item(
                        "old_price_not_greater", "commercial.old_price",
                        "old_price должен быть больше price",
                    ))
            except MarketplaceDraftValidationError:
                errors.append(cls._validation_item(
                    "old_price_invalid", "commercial.old_price",
                    "old_price должен быть положительным числом",
                ))
        if commercial.get("vat") not in cls.VAT_VALUES:
            errors.append(cls._validation_item(
                "vat_required", "commercial.vat",
                "Выберите явную ставку НДС из поддерживаемого Ozon enum",
            ))
        if commercial.get("currency_code") not in cls.CURRENCY_CODES:
            errors.append(cls._validation_item(
                "currency_code_required", "commercial.currency_code",
                "Укажите явный currency_code=RUB для текущего rollout",
            ))

        if product_type and OzonReferenceService.reference_is_fresh(product_type, now=now):
            cls._validate_attributes(
                product_type=product_type,
                attributes=attributes,
                complex_groups=cls._stored_json(
                    draft.complex_attributes_json, list
                ),
                errors=errors,
                implicitly_supplied=implicit_attribute_ids,
            )

        if len(errors) > cls.MAX_VALIDATION_ITEMS:
            errors = errors[: cls.MAX_VALIDATION_ITEMS]
            errors.append(cls._validation_item(
                "validation_items_truncated", "draft",
                "Список ошибок обрезан safety limit",
            ))
        return {
            "version": 1,
            "marketplace": "ozon",
            "publishable": not errors,
            "errors": errors,
            "warnings": warnings,
            "schema": {
                "product_type_id": product_type.id if product_type else None,
                "external_category_id": (
                    product_type.category.external_category_id
                    if product_type and product_type.category else None
                ),
                "external_type_id": (
                    product_type.external_type_id if product_type else None
                ),
                "version": product_type.attributes_version if product_type else None,
                "hash": product_type.attributes_schema_hash if product_type else None,
                "fresh": bool(
                    product_type
                    and OzonReferenceService.reference_is_fresh(product_type, now=now)
                ),
            },
            "validated_at": now.isoformat(),
        }

    @classmethod
    def validate_draft(
        cls,
        *,
        seller_id: int,
        draft_id: int,
        expected_version: int,
    ) -> MarketplaceProductDraft:
        expected_version = cls._positive_integer(expected_version, "expected_version")
        draft = cls.get_draft(seller_id=seller_id, draft_id=draft_id)
        if draft.version != expected_version:
            raise MarketplaceDraftConflict(
                "Черновик изменился; обновите страницу и повторите"
            )
        if draft.status == "archived":
            raise MarketplaceDraftConflict(
                "Архивный черновик нельзя валидировать до восстановления listing"
            )
        cls._assert_no_active_publication(draft)
        result = cls._build_validation_result(draft)
        draft.validation_result_json = cls._canonical_json(result, dict)
        draft.validation_status = "valid" if result["publishable"] else "invalid"
        if draft.product_type:
            draft.schema_version = draft.product_type.attributes_version
            draft.schema_hash = draft.product_type.attributes_schema_hash
        else:
            draft.schema_version = None
            draft.schema_hash = None
        draft.validated_at = datetime.utcnow()
        draft.status = (
            "ready"
            if result["publishable"]
            else "needs_category" if draft.product_type is None else "blocked"
        )
        try:
            db.session.commit()
        except StaleDataError:
            db.session.rollback()
            raise MarketplaceDraftConflict(
                "Черновик изменился параллельно; повторите после обновления"
            ) from None
        return cls.get_draft(seller_id=seller_id, draft_id=draft_id)
