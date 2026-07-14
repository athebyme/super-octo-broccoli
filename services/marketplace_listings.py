"""Tenant-scoped marketplace listing queries and Ozon catalog reconciliation.

The synchronizer owns pagination and persistence.  Adapters only perform typed
provider calls.  Every Ozon page is fully validated and enriched before any
listing mutation is committed; a failed or paused run never marks unseen rows
missing.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
import fcntl
import hashlib
import json
import math
import os
import tempfile

from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload

from models import (
    Marketplace,
    MarketplaceCatalogSync,
    MarketplaceCredentialEncryptionError,
    MarketplaceListing,
    MarketplaceProductType,
    MarketplaceTaxonomyCategory,
    SellerMarketplaceAccount,
    db,
)
from services.marketplace_accounts import (
    MarketplaceAccountNotFound,
    MarketplaceAccountService,
)
from services.marketplace_adapters import (
    MarketplaceCredentials,
    get_marketplace_registry,
)
from services.marketplace_adapters.base import MarketplaceAdapterError
from services.ozon_api_client import OzonAPIError


class MarketplaceListingError(RuntimeError):
    status_code = 400
    code = "marketplace_listing_error"


class MarketplaceListingValidationError(MarketplaceListingError):
    status_code = 400
    code = "invalid_marketplace_listing_request"


class MarketplaceListingNotFound(MarketplaceListingError):
    status_code = 404
    code = "marketplace_listing_not_found"


class MarketplaceCatalogConfigurationError(MarketplaceListingError):
    status_code = 409
    code = "marketplace_catalog_not_ready"


class MarketplaceCatalogBusy(MarketplaceListingError):
    status_code = 409
    code = "marketplace_catalog_busy"


class MarketplaceCatalogProtocolError(MarketplaceListingError):
    status_code = 502
    code = "ozon_catalog_protocol_error"


class MarketplaceCatalogSyncError(MarketplaceListingError):
    status_code = 502
    code = "ozon_catalog_sync_failed"


class MarketplaceListingService:
    PAGE_SIZE = 1000
    MAX_SYNC_PAGES_PER_CALL = 50
    MAX_ENRICHMENT_PAGES = 20
    MAX_JSON_BYTES = 262_144
    MAX_DESCRIPTION_CHARS = 100_000
    STALE_RUN_AFTER = timedelta(minutes=15)
    PHASES = {
        "active": "ALL",
        "archived": "ARCHIVED",
    }
    NORMALIZED_STATUSES = {
        "active",
        "moderation",
        "creating",
        "error",
        "archived",
        "inactive",
        "unknown",
    }

    @staticmethod
    def _positive_integer(value: Any, field_name: str) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise MarketplaceListingValidationError(
                f"{field_name} должен быть положительным целым числом"
            )
        return value

    @staticmethod
    def _provider_integer(
        value: Any,
        field_name: str,
        *,
        minimum: int = 0,
    ) -> int:
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or value < minimum
        ):
            raise MarketplaceCatalogProtocolError(
                f"Ozon {field_name} must be an integer >= {minimum}"
            )
        return value

    @staticmethod
    def _provider_bool(value: Any, field_name: str) -> bool:
        if not isinstance(value, bool):
            raise MarketplaceCatalogProtocolError(
                f"Ozon {field_name} must be a boolean"
            )
        return value

    @staticmethod
    def _provider_text(
        value: Any,
        field_name: str,
        *,
        maximum: int,
        allow_empty: bool = False,
        allow_newlines: bool = False,
    ) -> str:
        if not isinstance(value, str):
            raise MarketplaceCatalogProtocolError(
                f"Ozon {field_name} must be a string"
            )
        normalized = value.strip()
        if not normalized and not allow_empty:
            raise MarketplaceCatalogProtocolError(
                f"Ozon {field_name} must be non-empty"
            )
        if len(normalized) > maximum:
            raise MarketplaceCatalogProtocolError(
                f"Ozon {field_name} exceeds {maximum} characters"
            )
        allowed_controls = {9, 10, 13} if allow_newlines else set()
        if any(
            (ord(character) < 32 and ord(character) not in allowed_controls)
            or ord(character) == 127
            for character in normalized
        ):
            raise MarketplaceCatalogProtocolError(
                f"Ozon {field_name} contains control characters"
            )
        return normalized

    @classmethod
    def _optional_provider_text(
        cls,
        value: Any,
        field_name: str,
        *,
        maximum: int,
        allow_newlines: bool = False,
    ) -> Optional[str]:
        if value in (None, ""):
            return None
        return cls._provider_text(
            value,
            field_name,
            maximum=maximum,
            allow_newlines=allow_newlines,
        )

    @classmethod
    def _opaque_id(
        cls,
        value: Any,
        field_name: str,
        *,
        optional: bool = False,
        maximum: int = 100,
    ) -> Optional[str]:
        if optional and value in (None, "", 0):
            return None
        if isinstance(value, int) and not isinstance(value, bool):
            if value <= 0:
                raise MarketplaceCatalogProtocolError(
                    f"Ozon {field_name} must be positive"
                )
            return str(value)
        return cls._provider_text(value, field_name, maximum=maximum)

    @classmethod
    def _provider_datetime(
        cls,
        value: Any,
        field_name: str,
    ) -> Optional[datetime]:
        if value in (None, ""):
            return None
        text = cls._provider_text(value, field_name, maximum=80)
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            raise MarketplaceCatalogProtocolError(
                f"Ozon {field_name} must be an ISO-8601 datetime"
            ) from None
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed

    @classmethod
    def _bounded_value(
        cls,
        value: Any,
        field_name: str,
        *,
        depth: int = 0,
    ) -> Any:
        """Copy provider metadata without permitting unbounded raw snapshots."""
        if depth > 6:
            raise MarketplaceCatalogProtocolError(
                f"Ozon {field_name} exceeds nesting limit"
            )
        if value is None or isinstance(value, (bool, int)):
            return value
        if isinstance(value, float):
            if not math.isfinite(value):
                raise MarketplaceCatalogProtocolError(
                    f"Ozon {field_name} contains a non-finite number"
                )
            return value
        if isinstance(value, str):
            return cls._provider_text(
                value,
                field_name,
                maximum=5000,
                allow_empty=True,
                allow_newlines=True,
            )
        if isinstance(value, list):
            if len(value) > 2000:
                raise MarketplaceCatalogProtocolError(
                    f"Ozon {field_name} exceeds list limit"
                )
            return [
                cls._bounded_value(
                    item,
                    f"{field_name}[{index}]",
                    depth=depth + 1,
                )
                for index, item in enumerate(value)
            ]
        if isinstance(value, dict):
            if len(value) > 300:
                raise MarketplaceCatalogProtocolError(
                    f"Ozon {field_name} exceeds object limit"
                )
            result = {}
            for raw_key, item in value.items():
                key = cls._provider_text(
                    raw_key,
                    f"{field_name}.key",
                    maximum=200,
                )
                result[key] = cls._bounded_value(
                    item,
                    f"{field_name}.{key}",
                    depth=depth + 1,
                )
            return result
        raise MarketplaceCatalogProtocolError(
            f"Ozon {field_name} contains an unsupported value"
        )

    @classmethod
    def _stable_json(cls, value: Any, fallback: Any) -> str:
        if not isinstance(value, type(fallback)):
            value = fallback
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if len(encoded.encode("utf-8")) > cls.MAX_JSON_BYTES:
            raise MarketplaceCatalogProtocolError(
                "Normalized Ozon listing snapshot exceeds storage limit"
            )
        return encoded

    @classmethod
    def _fingerprint(cls, value: Mapping[str, Any]) -> str:
        payload = cls._stable_json(dict(value), {})
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _try_claim(account_id: int):
        directory = os.path.join(
            tempfile.gettempdir(),
            "seller-hub-marketplace-catalog-locks",
        )
        os.makedirs(directory, mode=0o700, exist_ok=True)
        path = os.path.join(directory, f"account-{int(account_id)}.lock")
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

    @classmethod
    def _account_adapter_credentials(
        cls,
        *,
        seller_id: int,
        account_id: int,
        adapter=None,
        credentials: Optional[MarketplaceCredentials] = None,
        now: Optional[datetime] = None,
    ) -> Tuple[SellerMarketplaceAccount, Any, MarketplaceCredentials]:
        try:
            account = MarketplaceAccountService.get_owned_account(
                seller_id=seller_id,
                account_id=account_id,
                marketplace_code="ozon",
            )
        except MarketplaceAccountNotFound:
            raise MarketplaceListingNotFound("Кабинет Ozon не найден") from None
        current_time = now or datetime.utcnow()
        if not account.is_active or account.connection_status != "connected":
            raise MarketplaceCatalogConfigurationError(
                "Кабинет Ozon должен быть активен и пройти проверку подключения"
            )
        if (
            account.credential_expires_at
            and account.credential_expires_at <= current_time
        ):
            raise MarketplaceCatalogConfigurationError(
                "Срок действия API key Ozon истёк"
            )
        if adapter is not None or credentials is not None:
            if adapter is None or credentials is None:
                raise MarketplaceListingValidationError(
                    "adapter and credentials must be injected together"
                )
            resolved_adapter = adapter
            resolved_credentials = credentials
        else:
            if not account.has_credentials:
                raise MarketplaceCatalogConfigurationError(
                    "В кабинете Ozon нет сохранённого API key"
                )
            try:
                secret = account.get_credentials()
                resolved_credentials = MarketplaceCredentials(
                    external_account_id=account.external_account_id,
                    api_key=secret["api_key"],
                )
                del secret
            except (KeyError, ValueError, MarketplaceCredentialEncryptionError):
                raise MarketplaceCatalogConfigurationError(
                    "Credentials кабинета Ozon невозможно прочитать"
                ) from None
            resolved_adapter = get_marketplace_registry().get("ozon")
        try:
            resolved_adapter.require_capability("catalog_read")
        except MarketplaceAdapterError as exc:
            raise MarketplaceCatalogConfigurationError(str(exc)) from None
        return account, resolved_adapter, resolved_credentials

    @classmethod
    def normalize_product_list_page(cls, response: Any) -> Dict[str, Any]:
        if not isinstance(response, dict):
            raise MarketplaceCatalogProtocolError(
                "Ozon product list response must be an object"
            )
        result = response.get("result")
        if not isinstance(result, dict):
            raise MarketplaceCatalogProtocolError(
                "Ozon product list response has no result object"
            )
        raw_items = result.get("items")
        if not isinstance(raw_items, list) or len(raw_items) > cls.PAGE_SIZE:
            raise MarketplaceCatalogProtocolError(
                "Ozon product list items must be a bounded list"
            )
        total = cls._provider_integer(result.get("total"), "product_list.total")
        last_id = cls._provider_text(
            result.get("last_id", ""),
            "product_list.last_id",
            maximum=1000,
            allow_empty=True,
        )
        items = []
        product_ids = set()
        offer_ids = set()
        for index, raw in enumerate(raw_items):
            if not isinstance(raw, dict):
                raise MarketplaceCatalogProtocolError(
                    f"Ozon product_list.items[{index}] must be an object"
                )
            product_id = cls._opaque_id(
                raw.get("product_id"),
                f"product_list.items[{index}].product_id",
            )
            offer_id = cls._provider_text(
                raw.get("offer_id"),
                f"product_list.items[{index}].offer_id",
                maximum=200,
            )
            if product_id in product_ids or offer_id in offer_ids:
                raise MarketplaceCatalogProtocolError(
                    "Ozon product list page contains duplicate identities"
                )
            product_ids.add(product_id)
            offer_ids.add(offer_id)
            item = {
                "product_id": product_id,
                "offer_id": offer_id,
                "archived": cls._provider_bool(
                    raw.get("archived", False),
                    f"product_list.items[{index}].archived",
                ),
                "has_fbo_stocks": cls._provider_bool(
                    raw.get("has_fbo_stocks", False),
                    f"product_list.items[{index}].has_fbo_stocks",
                ),
                "has_fbs_stocks": cls._provider_bool(
                    raw.get("has_fbs_stocks", False),
                    f"product_list.items[{index}].has_fbs_stocks",
                ),
            }
            items.append(item)
        return {"items": items, "total": total, "cursor": last_id}

    @classmethod
    def _string_list(
        cls,
        value: Any,
        field_name: str,
        *,
        maximum_items: int = 100,
        maximum_length: int = 2000,
    ) -> List[str]:
        if value is None:
            return []
        if not isinstance(value, list) or len(value) > maximum_items:
            raise MarketplaceCatalogProtocolError(
                f"Ozon {field_name} must be a bounded list"
            )
        result = []
        seen = set()
        for index, item in enumerate(value):
            text = cls._provider_text(
                item,
                f"{field_name}[{index}]",
                maximum=maximum_length,
            )
            if text not in seen:
                seen.add(text)
                result.append(text)
        return result

    @classmethod
    def _status_metadata(cls, value: Any, field_name: str) -> Dict[str, Any]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise MarketplaceCatalogProtocolError(
                f"Ozon {field_name} must be an object"
            )
        allowed = {
            "is_created",
            "moderate_status",
            "status",
            "status_description",
            "status_failed",
            "status_name",
            "status_tooltip",
            "status_updated_at",
            "validation_status",
        }
        result = {}
        for key in sorted(allowed.intersection(value)):
            if key in {"is_created", "status_failed"}:
                result[key] = cls._provider_bool(
                    value[key],
                    f"{field_name}.{key}",
                )
            else:
                result[key] = cls._bounded_value(
                    value[key],
                    f"{field_name}.{key}",
                )
        return result

    @classmethod
    def _visibility_metadata(
        cls,
        value: Any,
        field_name: str,
    ) -> Dict[str, Any]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise MarketplaceCatalogProtocolError(
                f"Ozon {field_name} must be an object"
            )
        allowed = {
            "active_product",
            "has_price",
            "has_stock",
            "reasons",
        }
        result = {}
        for key in sorted(allowed.intersection(value)):
            if key in {"active_product", "has_price", "has_stock"}:
                result[key] = cls._provider_bool(
                    value[key],
                    f"{field_name}.{key}",
                )
            else:
                result[key] = cls._bounded_value(
                    value[key],
                    f"{field_name}.{key}",
                )
        return result

    @classmethod
    def _errors(cls, value: Any, field_name: str) -> List[Any]:
        if value is None:
            return []
        if not isinstance(value, list) or len(value) > 500:
            raise MarketplaceCatalogProtocolError(
                f"Ozon {field_name} must be a bounded list"
            )
        return [
            cls._bounded_value(item, f"{field_name}[{index}]")
            for index, item in enumerate(value)
        ]

    @classmethod
    def normalize_product_info(cls, response: Any) -> Dict[str, Dict[str, Any]]:
        if not isinstance(response, dict) or not isinstance(response.get("items"), list):
            raise MarketplaceCatalogProtocolError(
                "Ozon product info response has no items list"
            )
        raw_items = response["items"]
        if len(raw_items) > cls.PAGE_SIZE:
            raise MarketplaceCatalogProtocolError(
                "Ozon product info response exceeds batch limit"
            )
        result: Dict[str, Dict[str, Any]] = {}
        for index, raw in enumerate(raw_items):
            if not isinstance(raw, dict):
                raise MarketplaceCatalogProtocolError(
                    f"Ozon product_info.items[{index}] must be an object"
                )
            product_id = cls._opaque_id(
                raw.get("id", raw.get("product_id")),
                f"product_info.items[{index}].id",
            )
            if product_id in result:
                raise MarketplaceCatalogProtocolError(
                    "Ozon product info response contains duplicate product_id"
                )
            offer_id = cls._provider_text(
                raw.get("offer_id"),
                f"product_info.items[{index}].offer_id",
                maximum=200,
            )
            statuses = cls._status_metadata(
                raw.get("statuses"),
                f"product_info.items[{index}].statuses",
            )
            visibility = cls._visibility_metadata(
                raw.get("visibility_details"),
                f"product_info.items[{index}].visibility_details",
            )
            identifiers: Dict[str, Any] = {
                "product_id": product_id,
                "offer_id": offer_id,
            }
            sources = raw.get("sources")
            if sources is not None:
                if not isinstance(sources, list) or len(sources) > 100:
                    raise MarketplaceCatalogProtocolError(
                        "Ozon product_info.sources must be a bounded list"
                    )
                normalized_sources = [
                    cls._bounded_value(
                        item,
                        f"product_info.items[{index}].sources[{source_index}]",
                    )
                    for source_index, item in enumerate(sources)
                ]
                identifiers["sources"] = normalized_sources
                for source in normalized_sources:
                    if not isinstance(source, dict):
                        continue
                    for key in ("sku", "sku_fbo", "sku_fbs"):
                        if source.get(key) not in (None, ""):
                            identifiers.setdefault(key, str(source[key])[:100])
            for key in ("sku", "sku_fbo", "sku_fbs"):
                if raw.get(key) not in (None, ""):
                    identifiers[key] = cls._opaque_id(
                        raw[key],
                        f"product_info.items[{index}].{key}",
                    )
            primary_image = cls._optional_provider_text(
                raw.get("primary_image"),
                f"product_info.items[{index}].primary_image",
                maximum=2000,
            )
            images = cls._string_list(
                raw.get("images"),
                f"product_info.items[{index}].images",
            )
            media = {"images": images}
            if primary_image:
                media["primary_image"] = primary_image
            archived_value = raw.get("is_archived", False)
            archived = cls._provider_bool(
                archived_value,
                f"product_info.items[{index}].is_archived",
            )
            errors = cls._errors(
                raw.get("errors"),
                f"product_info.items[{index}].errors",
            )
            result[product_id] = {
                "product_id": product_id,
                "offer_id": offer_id,
                "title": cls._optional_provider_text(
                    raw.get("name"),
                    f"product_info.items[{index}].name",
                    maximum=500,
                    allow_newlines=True,
                ),
                "description": cls._optional_provider_text(
                    raw.get("description"),
                    f"product_info.items[{index}].description",
                    maximum=cls.MAX_DESCRIPTION_CHARS,
                    allow_newlines=True,
                ),
                "category_id": cls._opaque_id(
                    raw.get("description_category_id"),
                    f"product_info.items[{index}].description_category_id",
                    optional=True,
                ),
                "type_id": cls._opaque_id(
                    raw.get("type_id"),
                    f"product_info.items[{index}].type_id",
                    optional=True,
                ),
                "archived": archived,
                "barcodes": cls._string_list(
                    raw.get("barcodes"),
                    f"product_info.items[{index}].barcodes",
                    maximum_items=200,
                    maximum_length=500,
                ),
                "media": media,
                "statuses": statuses,
                "visibility_details": visibility,
                "errors": errors,
                "identifiers": identifiers,
                "created_at": cls._provider_datetime(
                    raw.get("created_at"),
                    f"product_info.items[{index}].created_at",
                ),
                "updated_at": cls._provider_datetime(
                    raw.get("updated_at"),
                    f"product_info.items[{index}].updated_at",
                ),
            }
        return result

    @classmethod
    def _normalize_attribute_values(
        cls,
        values: Any,
        field_name: str,
    ) -> List[Dict[str, Optional[str]]]:
        if not isinstance(values, list) or len(values) > 500:
            raise MarketplaceCatalogProtocolError(
                f"Ozon {field_name} must be a bounded list"
            )
        result = []
        for index, raw in enumerate(values):
            if not isinstance(raw, dict):
                raise MarketplaceCatalogProtocolError(
                    f"Ozon {field_name}[{index}] must be an object"
                )
            result.append({
                "dictionary_value_id": cls._opaque_id(
                    raw.get("dictionary_value_id"),
                    f"{field_name}[{index}].dictionary_value_id",
                    optional=True,
                ),
                "value": cls._optional_provider_text(
                    raw.get("value"),
                    f"{field_name}[{index}].value",
                    maximum=5000,
                    allow_newlines=True,
                ),
            })
        return result

    @classmethod
    def _normalize_attributes(
        cls,
        attributes: Any,
        field_name: str,
    ) -> List[Dict[str, Any]]:
        if not isinstance(attributes, list) or len(attributes) > 2000:
            raise MarketplaceCatalogProtocolError(
                f"Ozon {field_name} must be a bounded list"
            )
        result = []
        seen = set()
        for index, raw in enumerate(attributes):
            if not isinstance(raw, dict):
                raise MarketplaceCatalogProtocolError(
                    f"Ozon {field_name}[{index}] must be an object"
                )
            attribute_id = cls._opaque_id(
                raw.get("id"),
                f"{field_name}[{index}].id",
            )
            complex_id = cls._opaque_id(
                raw.get("complex_id"),
                f"{field_name}[{index}].complex_id",
                optional=True,
            )
            key = (attribute_id, complex_id)
            if key in seen:
                raise MarketplaceCatalogProtocolError(
                    f"Ozon {field_name} contains duplicate attributes"
                )
            seen.add(key)
            result.append({
                "id": attribute_id,
                "complex_id": complex_id,
                "values": cls._normalize_attribute_values(
                    raw.get("values"),
                    f"{field_name}[{index}].values",
                ),
            })
        return result

    @classmethod
    def _normalize_complex_attributes(
        cls,
        value: Any,
        field_name: str,
    ) -> List[Dict[str, Any]]:
        if value is None:
            return []
        if not isinstance(value, list) or len(value) > 200:
            raise MarketplaceCatalogProtocolError(
                f"Ozon {field_name} must be a bounded list"
            )
        result = []
        for index, raw in enumerate(value):
            if not isinstance(raw, dict):
                raise MarketplaceCatalogProtocolError(
                    f"Ozon {field_name}[{index}] must be an object"
                )
            result.append({
                "attributes": cls._normalize_attributes(
                    raw.get("attributes"),
                    f"{field_name}[{index}].attributes",
                )
            })
        return result

    @classmethod
    def normalize_product_attributes_page(
        cls,
        response: Any,
    ) -> Dict[str, Any]:
        if not isinstance(response, dict) or not isinstance(response.get("result"), list):
            raise MarketplaceCatalogProtocolError(
                "Ozon product attributes response has no result list"
            )
        raw_items = response["result"]
        if len(raw_items) > cls.PAGE_SIZE:
            raise MarketplaceCatalogProtocolError(
                "Ozon product attributes page exceeds batch limit"
            )
        total = cls._provider_integer(
            response.get("total"),
            "product_attributes.total",
        )
        cursor = cls._provider_text(
            response.get("last_id", ""),
            "product_attributes.last_id",
            maximum=1000,
            allow_empty=True,
        )
        items: Dict[str, Dict[str, Any]] = {}
        for index, raw in enumerate(raw_items):
            if not isinstance(raw, dict):
                raise MarketplaceCatalogProtocolError(
                    f"Ozon product_attributes.result[{index}] must be an object"
                )
            product_id = cls._opaque_id(
                raw.get("id", raw.get("product_id")),
                f"product_attributes.result[{index}].id",
            )
            if product_id in items:
                raise MarketplaceCatalogProtocolError(
                    "Ozon product attributes page contains duplicate product_id"
                )
            offer_id = cls._provider_text(
                raw.get("offer_id"),
                f"product_attributes.result[{index}].offer_id",
                maximum=200,
            )
            dimensions = {}
            for key in (
                "depth", "dimension_unit", "height", "weight",
                "weight_unit", "width",
            ):
                if key in raw and raw[key] is not None:
                    dimensions[key] = cls._bounded_value(
                        raw[key],
                        f"product_attributes.result[{index}].{key}",
                    )
            primary_image = cls._optional_provider_text(
                raw.get("primary_image"),
                f"product_attributes.result[{index}].primary_image",
                maximum=2000,
            )
            media = {
                "images": cls._string_list(
                    raw.get("images"),
                    f"product_attributes.result[{index}].images",
                )
            }
            if primary_image:
                media["primary_image"] = primary_image
            sku = cls._opaque_id(
                raw.get("sku"),
                f"product_attributes.result[{index}].sku",
                optional=True,
            )
            raw_barcodes = raw.get("barcodes")
            if raw_barcodes is None and raw.get("barcode") not in (None, ""):
                raw_barcodes = [raw["barcode"]]
            items[product_id] = {
                "product_id": product_id,
                "offer_id": offer_id,
                "title": cls._optional_provider_text(
                    raw.get("name"),
                    f"product_attributes.result[{index}].name",
                    maximum=500,
                    allow_newlines=True,
                ),
                "category_id": cls._opaque_id(
                    raw.get("description_category_id"),
                    f"product_attributes.result[{index}].description_category_id",
                    optional=True,
                ),
                "type_id": cls._opaque_id(
                    raw.get("type_id"),
                    f"product_attributes.result[{index}].type_id",
                    optional=True,
                ),
                "sku": sku,
                "attributes": cls._normalize_attributes(
                    raw.get("attributes"),
                    f"product_attributes.result[{index}].attributes",
                ),
                "complex_attributes": cls._normalize_complex_attributes(
                    raw.get("complex_attributes"),
                    f"product_attributes.result[{index}].complex_attributes",
                ),
                "dimensions": dimensions,
                "barcodes": cls._string_list(
                    raw_barcodes,
                    f"product_attributes.result[{index}].barcodes",
                    maximum_items=200,
                    maximum_length=500,
                ),
                "media": media,
            }
        return {"items": items, "total": total, "cursor": cursor}

    @classmethod
    def _price_summary(cls, raw: Dict[str, Any], field_name: str) -> Dict[str, Any]:
        price = raw.get("price")
        if price is None:
            price = {}
        if not isinstance(price, dict):
            raise MarketplaceCatalogProtocolError(
                f"Ozon {field_name}.price must be an object"
            )
        allowed_price = {
            "auto_action_enabled",
            "marketing_seller_price",
            "min_price",
            "old_price",
            "premium_price",
            "recommended_price",
            "retail_price",
            "vat",
        }
        values = {
            key: cls._bounded_value(price[key], f"{field_name}.price.{key}")
            for key in sorted(allowed_price.intersection(price))
        }
        currency = raw.get("currency_code", raw.get("currency"))
        return {
            "available": bool(values),
            "currency": cls._optional_provider_text(
                currency,
                f"{field_name}.currency",
                maximum=20,
            ),
            "values": values,
            "price_indexes": cls._bounded_value(
                raw.get("price_indexes"),
                f"{field_name}.price_indexes",
            ) if raw.get("price_indexes") is not None else None,
            "commissions": cls._bounded_value(
                raw.get("commissions"),
                f"{field_name}.commissions",
            ) if raw.get("commissions") is not None else [],
            "marketing_actions": cls._bounded_value(
                raw.get("marketing_actions"),
                f"{field_name}.marketing_actions",
            ) if raw.get("marketing_actions") is not None else {},
        }

    @classmethod
    def normalize_prices_page(cls, response: Any) -> Dict[str, Any]:
        return cls._normalize_summary_page(
            response,
            endpoint="product_prices",
            normalizer=cls._price_summary,
        )

    @classmethod
    def _stock_summary(cls, raw: Dict[str, Any], field_name: str) -> Dict[str, Any]:
        raw_stocks = raw.get("stocks")
        if raw_stocks is None:
            raw_stocks = []
        if not isinstance(raw_stocks, list) or len(raw_stocks) > 200:
            raise MarketplaceCatalogProtocolError(
                f"Ozon {field_name}.stocks must be a bounded list"
            )
        stocks = []
        total_present = 0
        total_reserved = 0
        by_type: Dict[str, Dict[str, int]] = {}
        for index, item in enumerate(raw_stocks):
            if not isinstance(item, dict):
                raise MarketplaceCatalogProtocolError(
                    f"Ozon {field_name}.stocks[{index}] must be an object"
                )
            stock_type = cls._provider_text(
                item.get("type", "UNKNOWN"),
                f"{field_name}.stocks[{index}].type",
                maximum=100,
            )
            present = cls._provider_integer(
                item.get("present", 0),
                f"{field_name}.stocks[{index}].present",
            )
            reserved = cls._provider_integer(
                item.get("reserved", 0),
                f"{field_name}.stocks[{index}].reserved",
            )
            warehouse_ids = item.get("warehouse_ids", [])
            if not isinstance(warehouse_ids, list) or len(warehouse_ids) > 500:
                raise MarketplaceCatalogProtocolError(
                    f"Ozon {field_name}.stocks[{index}].warehouse_ids invalid"
                )
            normalized_warehouses = [
                cls._opaque_id(
                    value,
                    f"{field_name}.stocks[{index}].warehouse_ids[{position}]",
                )
                for position, value in enumerate(warehouse_ids)
            ]
            stocks.append({
                "type": stock_type,
                "present": present,
                "reserved": reserved,
                "warehouse_ids": normalized_warehouses,
            })
            total_present += present
            total_reserved += reserved
            bucket = by_type.setdefault(
                stock_type,
                {"present": 0, "reserved": 0},
            )
            bucket["present"] += present
            bucket["reserved"] += reserved
        return {
            "available": True,
            "present": total_present,
            "reserved": total_reserved,
            "by_type": by_type,
            "stocks": stocks,
        }

    @classmethod
    def normalize_stocks_page(cls, response: Any) -> Dict[str, Any]:
        return cls._normalize_summary_page(
            response,
            endpoint="product_stocks",
            normalizer=cls._stock_summary,
        )

    @classmethod
    def _normalize_summary_page(
        cls,
        response: Any,
        *,
        endpoint: str,
        normalizer,
    ) -> Dict[str, Any]:
        if not isinstance(response, dict) or not isinstance(response.get("items"), list):
            raise MarketplaceCatalogProtocolError(
                f"Ozon {endpoint} response has no items list"
            )
        raw_items = response["items"]
        if len(raw_items) > cls.PAGE_SIZE:
            raise MarketplaceCatalogProtocolError(
                f"Ozon {endpoint} page exceeds batch limit"
            )
        total = cls._provider_integer(response.get("total"), f"{endpoint}.total")
        cursor = cls._provider_text(
            response.get("cursor", ""),
            f"{endpoint}.cursor",
            maximum=1000,
            allow_empty=True,
        )
        items = {}
        for index, raw in enumerate(raw_items):
            if not isinstance(raw, dict):
                raise MarketplaceCatalogProtocolError(
                    f"Ozon {endpoint}.items[{index}] must be an object"
                )
            product_id = cls._opaque_id(
                raw.get("product_id", raw.get("id")),
                f"{endpoint}.items[{index}].product_id",
            )
            if product_id in items:
                raise MarketplaceCatalogProtocolError(
                    f"Ozon {endpoint} contains duplicate product_id"
                )
            items[product_id] = {
                "product_id": product_id,
                "offer_id": cls._optional_provider_text(
                    raw.get("offer_id"),
                    f"{endpoint}.items[{index}].offer_id",
                    maximum=200,
                ),
                "summary": normalizer(
                    raw,
                    f"{endpoint}.items[{index}]",
                ),
            }
        return {"items": items, "total": total, "cursor": cursor}

    @classmethod
    def _validate_enrichment_identities(
        cls,
        items: Mapping[str, Mapping[str, Any]],
        base_items: Mapping[str, Mapping[str, Any]],
        endpoint: str,
    ) -> None:
        for product_id, item in items.items():
            base = base_items.get(product_id)
            if base is None:
                raise MarketplaceCatalogProtocolError(
                    f"Ozon {endpoint} returned an unrequested product_id"
                )
            offer_id = item.get("offer_id")
            if offer_id and offer_id != base["offer_id"]:
                raise MarketplaceCatalogProtocolError(
                    f"Ozon {endpoint} returned a conflicting offer_id"
                )

    @classmethod
    def _collect_cursor_pages(
        cls,
        *,
        fetch,
        payload: Dict[str, Any],
        normalize,
        cursor_field: str,
        endpoint: str,
    ) -> Dict[str, Dict[str, Any]]:
        cursor = ""
        expected_total = None
        result: Dict[str, Dict[str, Any]] = {}
        for _ in range(cls.MAX_ENRICHMENT_PAGES):
            request_payload = dict(payload)
            request_payload[cursor_field] = cursor
            page = normalize(fetch(request_payload))
            total = page["total"]
            if expected_total is None:
                expected_total = total
            elif total != expected_total:
                raise MarketplaceCatalogProtocolError(
                    f"Ozon {endpoint} total changed during pagination"
                )
            for product_id, item in page["items"].items():
                if product_id in result:
                    raise MarketplaceCatalogProtocolError(
                        f"Ozon {endpoint} repeated product_id across pages"
                    )
                result[product_id] = item
            if len(result) > total:
                raise MarketplaceCatalogProtocolError(
                    f"Ozon {endpoint} returned more rows than total"
                )
            next_cursor = page["cursor"]
            if len(result) == total:
                return result
            if not page["items"] or not next_cursor or next_cursor == cursor:
                raise MarketplaceCatalogProtocolError(
                    f"Ozon {endpoint} pagination ended before total"
                )
            cursor = next_cursor
        raise MarketplaceCatalogProtocolError(
            f"Ozon {endpoint} exceeded pagination safety limit"
        )

    @classmethod
    def _fetch_enrichment(
        cls,
        *,
        adapter,
        credentials: MarketplaceCredentials,
        base_items: Sequence[Mapping[str, Any]],
    ) -> Dict[str, Dict[str, Dict[str, Any]]]:
        if not base_items:
            return {"info": {}, "attributes": {}, "prices": {}, "stocks": {}}
        product_ids = [item["product_id"] for item in base_items]
        base_by_product = {item["product_id"]: item for item in base_items}
        info = cls.normalize_product_info(
            adapter.get_products(credentials, {"product_id": product_ids})
        )
        attributes = cls._collect_cursor_pages(
            fetch=lambda payload: adapter.get_product_attributes(
                credentials,
                payload,
            ),
            payload={
                "filter": {
                    "product_id": product_ids,
                    "visibility": "ALL",
                },
                "limit": cls.PAGE_SIZE,
            },
            normalize=cls.normalize_product_attributes_page,
            cursor_field="last_id",
            endpoint="product_attributes",
        )
        prices = cls._collect_cursor_pages(
            fetch=lambda payload: adapter.read_prices(credentials, payload),
            payload={
                "filter": {
                    "product_id": product_ids,
                    "visibility": "ALL",
                },
                "limit": cls.PAGE_SIZE,
            },
            normalize=cls.normalize_prices_page,
            cursor_field="cursor",
            endpoint="product_prices",
        )
        stocks = cls._collect_cursor_pages(
            fetch=lambda payload: adapter.read_stocks(credentials, payload),
            payload={
                "filter": {
                    "product_id": product_ids,
                    "visibility": "ALL",
                },
                "limit": cls.PAGE_SIZE,
            },
            normalize=cls.normalize_stocks_page,
            cursor_field="cursor",
            endpoint="product_stocks",
        )
        for endpoint, items in (
            ("product_info", info),
            ("product_attributes", attributes),
            ("product_prices", prices),
            ("product_stocks", stocks),
        ):
            cls._validate_enrichment_identities(
                items,
                base_by_product,
                endpoint,
            )
        return {
            "info": info,
            "attributes": attributes,
            "prices": prices,
            "stocks": stocks,
        }

    @classmethod
    def _normalize_status(
        cls,
        *,
        archived: bool,
        statuses: Mapping[str, Any],
        visibility: Mapping[str, Any],
        errors: Sequence[Any],
    ) -> Tuple[str, Optional[str], Optional[str]]:
        provider_status = next((
            str(statuses[key]).strip()
            for key in (
                "status", "status_name", "validation_status", "moderate_status"
            )
            if statuses.get(key) not in (None, "")
        ), None)
        visibility_name = None
        if visibility:
            visibility_name = (
                "active" if visibility.get("active_product") is True else "inactive"
            )
        if archived:
            return "archived", provider_status, visibility_name or "archived"
        if errors or statuses.get("status_failed") is True:
            return "error", provider_status, visibility_name
        status_text = " ".join(
            str(value).casefold()
            for value in statuses.values()
            if isinstance(value, str)
        )
        if any(word in status_text for word in ("error", "fail", "reject", "ошиб")):
            return "error", provider_status, visibility_name
        if any(word in status_text for word in ("moder", "провер", "pending")):
            return "moderation", provider_status, visibility_name
        if statuses.get("is_created") is False or any(
            word in status_text for word in ("creat", "созда")
        ):
            return "creating", provider_status, visibility_name
        if visibility.get("active_product") is False:
            return "inactive", provider_status, visibility_name
        if statuses or visibility:
            return "active", provider_status, visibility_name or "active"
        return "unknown", provider_status, visibility_name

    @classmethod
    def _type_map(
        cls,
        marketplace_id: int,
        pairs: Iterable[Tuple[Optional[str], Optional[str]]],
    ) -> Dict[Tuple[str, str], MarketplaceProductType]:
        pairs = {
            (category_id, type_id)
            for category_id, type_id in pairs
            if category_id and type_id
        }
        if not pairs:
            return {}
        category_ids = {pair[0] for pair in pairs}
        type_ids = {pair[1] for pair in pairs}
        rows = (
            MarketplaceProductType.query
            .join(MarketplaceTaxonomyCategory)
            .filter(
                MarketplaceProductType.marketplace_id == marketplace_id,
                MarketplaceProductType.external_type_id.in_(type_ids),
                MarketplaceTaxonomyCategory.external_category_id.in_(category_ids),
            )
            .all()
        )
        return {
            (row.category.external_category_id, row.external_type_id): row
            for row in rows
            if (row.category.external_category_id, row.external_type_id) in pairs
        }

    @classmethod
    def _apply_catalog_page(
        cls,
        *,
        run: MarketplaceCatalogSync,
        page: Mapping[str, Any],
        enrichment: Mapping[str, Mapping[str, Mapping[str, Any]]],
        now: datetime,
    ) -> None:
        items = list(page["items"])
        product_ids = [item["product_id"] for item in items]
        offer_ids = [item["offer_id"] for item in items]
        existing = []
        if items:
            existing = MarketplaceListing.query.filter(
                MarketplaceListing.seller_id == run.seller_id,
                MarketplaceListing.marketplace_id == run.marketplace_id,
                MarketplaceListing.account_id == run.account_id,
                or_(
                    MarketplaceListing.external_product_id.in_(product_ids),
                    MarketplaceListing.offer_id.in_(offer_ids),
                ),
            ).all()
        by_product = {row.external_product_id: row for row in existing}
        by_offer = {row.offer_id: row for row in existing}

        resolved_pairs = []
        for base in items:
            info = enrichment["info"].get(base["product_id"], {})
            attrs = enrichment["attributes"].get(base["product_id"], {})
            info_pair = (info.get("category_id"), info.get("type_id"))
            attrs_pair = (attrs.get("category_id"), attrs.get("type_id"))
            if all(info_pair) and all(attrs_pair) and info_pair != attrs_pair:
                raise MarketplaceCatalogProtocolError(
                    "Ozon product info and attributes returned different category/type"
                )
            resolved_pairs.append(info_pair if all(info_pair) else attrs_pair)
        type_map = cls._type_map(run.marketplace_id, resolved_pairs)

        created = 0
        updated = 0
        newly_seen = 0
        warnings = 0
        for base, pair in zip(items, resolved_pairs):
            product_id = base["product_id"]
            offer_id = base["offer_id"]
            product_match = by_product.get(product_id)
            offer_match = by_offer.get(offer_id)
            if product_match is not None and offer_match is not None and product_match.id != offer_match.id:
                raise MarketplaceCatalogProtocolError(
                    "Ozon listing identities collide with two local rows"
                )
            listing = offer_match or product_match
            if (
                listing is not None
                and listing.last_catalog_sync_id == run.id
                and listing.last_catalog_sync_phase == run.phase
            ):
                raise MarketplaceCatalogProtocolError(
                    "Ozon product list repeated an identity across cursor pages"
                )
            if listing is None:
                listing = MarketplaceListing(
                    seller_id=run.seller_id,
                    marketplace_id=run.marketplace_id,
                    account_id=run.account_id,
                    offer_id=offer_id,
                    external_product_id=product_id,
                    sync_fingerprint="0" * 64,
                )
                db.session.add(listing)
                created += 1
            else:
                if listing.last_catalog_sync_id != run.id:
                    newly_seen += 1
                updated += 1
            if listing.id is None:
                newly_seen += 1

            conflicting_product = by_product.get(product_id)
            conflicting_offer = by_offer.get(offer_id)
            if (
                conflicting_product is not None
                and conflicting_product is not listing
            ) or (
                conflicting_offer is not None
                and conflicting_offer is not listing
            ):
                raise MarketplaceCatalogProtocolError(
                    "Ozon listing identity update would violate uniqueness"
                )
            listing.offer_id = offer_id
            listing.external_product_id = product_id

            info = enrichment["info"].get(product_id)
            attrs = enrichment["attributes"].get(product_id)
            price = enrichment["prices"].get(product_id)
            stock = enrichment["stocks"].get(product_id)
            warnings += sum(item is None for item in (info, attrs, price, stock))
            info = info or {}
            attrs = attrs or {}
            statuses = info.get("statuses", {})
            visibility = info.get("visibility_details", {})
            errors = info.get("errors", [])
            archived = bool(base["archived"] or info.get("archived", False))
            normalized_status, provider_status, visibility_name = cls._normalize_status(
                archived=archived,
                statuses=statuses,
                visibility=visibility,
                errors=errors,
            )
            category_id, type_id = pair
            identifiers = dict(info.get("identifiers", {}))
            identifiers.update({
                "product_id": product_id,
                "offer_id": offer_id,
            })
            if attrs.get("sku"):
                identifiers["sku"] = attrs["sku"]
            primary_sku = next((
                identifiers.get(key)
                for key in ("sku", "sku_fbo", "sku_fbs")
                if identifiers.get(key)
            ), None)
            media = dict(info.get("media", {}))
            if attrs.get("media"):
                media.update(attrs["media"])
            barcodes = attrs.get("barcodes") or info.get("barcodes") or []
            price_summary = (
                price["summary"] if price else {"available": False}
            )
            stock_summary = (
                stock["summary"] if stock else {"available": False}
            )
            snapshot = {
                "offer_id": offer_id,
                "external_product_id": product_id,
                "primary_sku": primary_sku,
                "identifiers": identifiers,
                "category_id": category_id,
                "type_id": type_id,
                "title": info.get("title") or attrs.get("title"),
                "description": info.get("description"),
                "normalized_status": normalized_status,
                "provider_status": provider_status,
                "visibility": visibility_name,
                "is_archived": archived,
                "has_fbo_stocks": bool(base["has_fbo_stocks"]),
                "has_fbs_stocks": bool(base["has_fbs_stocks"]),
                "statuses": {"statuses": statuses, "visibility": visibility},
                "errors": errors,
                "attributes": attrs.get("attributes", []),
                "complex_attributes": attrs.get("complex_attributes", []),
                "media": media,
                "dimensions": attrs.get("dimensions", {}),
                "barcodes": barcodes,
                "price": price_summary,
                "stock": stock_summary,
            }
            listing.primary_sku = primary_sku
            listing.identifiers_json = cls._stable_json(identifiers, {})
            listing.external_category_id = category_id
            listing.external_type_id = type_id
            listing.product_type_id = (
                type_map[(category_id, type_id)].id
                if (category_id, type_id) in type_map else None
            )
            new_title = info.get("title") or attrs.get("title")
            if new_title is not None:
                listing.title = new_title
            if "description" in info and info.get("description") is not None:
                listing.description = info["description"]
            listing.normalized_status = normalized_status
            listing.provider_status = provider_status
            listing.visibility = visibility_name or (
                "archived" if archived else run.visibility
            )
            listing.is_archived = archived
            listing.is_available = True
            listing.has_fbo_stocks = bool(base["has_fbo_stocks"])
            listing.has_fbs_stocks = bool(base["has_fbs_stocks"])
            listing.statuses_json = cls._stable_json(
                {"statuses": statuses, "visibility": visibility},
                {},
            )
            listing.moderation_errors_json = cls._stable_json(errors, [])
            listing.attributes_json = cls._stable_json(
                attrs.get("attributes", []),
                [],
            )
            listing.complex_attributes_json = cls._stable_json(
                attrs.get("complex_attributes", []),
                [],
            )
            listing.media_json = cls._stable_json(media, {})
            listing.dimensions_json = cls._stable_json(
                attrs.get("dimensions", {}),
                {},
            )
            listing.barcodes_json = cls._stable_json(barcodes, [])
            listing.price_summary_json = cls._stable_json(price_summary, {})
            listing.stock_summary_json = cls._stable_json(stock_summary, {})
            listing.upstream_created_at = info.get("created_at")
            listing.upstream_updated_at = info.get("updated_at")
            listing.list_synced_at = now
            listing.info_synced_at = now if info else listing.info_synced_at
            listing.attributes_synced_at = now
            listing.prices_synced_at = now
            listing.stocks_synced_at = now
            listing.last_seen_at = now
            listing.last_catalog_sync_id = run.id
            listing.last_catalog_sync_phase = run.phase
            listing.sync_fingerprint = cls._fingerprint(snapshot)
            by_product[product_id] = listing
            by_offer[offer_id] = listing

        prior_expected = run.phase_expected_total
        if prior_expected is None:
            run.phase_expected_total = page["total"]
        elif prior_expected != page["total"]:
            raise MarketplaceCatalogProtocolError(
                "Ozon product list total changed during a phase"
            )
        next_phase_count = run.phase_seen_count + len(items)
        if next_phase_count > page["total"]:
            raise MarketplaceCatalogProtocolError(
                "Ozon product list returned more rows than total"
            )
        next_cursor = page["cursor"]
        phase_complete = next_phase_count == page["total"]
        if not phase_complete and (
            not items or not next_cursor or next_cursor == run.cursor
        ):
            raise MarketplaceCatalogProtocolError(
                "Ozon product list pagination ended before total"
            )

        run.phase_seen_count = next_phase_count
        run.page_count += 1
        run.seen_count += newly_seen
        run.created_count += created
        run.updated_count += updated
        run.warning_count += warnings
        run.heartbeat_at = now
        run.error_code = None
        run.error_message = None
        if phase_complete:
            if run.phase == "active":
                run.phase = "archived"
                run.visibility = cls.PHASES["archived"]
                run.cursor = ""
                run.phase_seen_count = 0
                run.phase_expected_total = None
            else:
                run.phase = "finalize"
                run.cursor = ""
        else:
            run.cursor = next_cursor

        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            raise MarketplaceCatalogProtocolError(
                "Ozon listing identities violate local uniqueness"
            ) from None

    @classmethod
    def _finalize_run(
        cls,
        run: MarketplaceCatalogSync,
        *,
        now: datetime,
    ) -> None:
        if run.phase != "finalize":
            raise MarketplaceCatalogProtocolError(
                "Catalog run cannot finalize before both visibility phases"
            )
        missing_rows = MarketplaceListing.query.filter(
            MarketplaceListing.seller_id == run.seller_id,
            MarketplaceListing.marketplace_id == run.marketplace_id,
            MarketplaceListing.account_id == run.account_id,
            MarketplaceListing.is_available.is_(True),
            MarketplaceListing.created_at <= run.started_at,
            or_(
                MarketplaceListing.last_catalog_sync_id.is_(None),
                MarketplaceListing.last_catalog_sync_id != run.id,
            ),
        ).all()
        for listing in missing_rows:
            listing.is_available = False
            listing.normalized_status = "inactive"
            listing.visibility = "missing_from_complete_sweep"
            listing.sync_fingerprint = cls._fingerprint({
                "previous": listing.sync_fingerprint,
                "available": False,
                "catalog_sync_id": run.id,
            })
        run.missing_count = len(missing_rows)
        run.phase = "completed"
        run.status = "completed"
        run.completed_at = now
        run.heartbeat_at = now
        run.error_code = None
        run.error_message = None
        db.session.commit()

    @classmethod
    def _claim_or_create_run(
        cls,
        *,
        account: SellerMarketplaceAccount,
        force_restart: bool,
        now: datetime,
    ) -> MarketplaceCatalogSync:
        running = MarketplaceCatalogSync.query.filter_by(
            seller_id=account.seller_id,
            marketplace_id=account.marketplace_id,
            account_id=account.id,
            status="running",
        ).order_by(MarketplaceCatalogSync.id.desc()).first()
        if running is not None:
            heartbeat = running.heartbeat_at or running.started_at
            if heartbeat and heartbeat > now - cls.STALE_RUN_AFTER:
                raise MarketplaceCatalogBusy(
                    "Синхронизация этого кабинета Ozon уже выполняется"
                )
            running.status = "failed"
            running.error_code = "stale_catalog_sync"
            running.error_message = (
                "Предыдущий процесс не обновлял heartbeat; запуск доступен для resume"
            )
            db.session.commit()

        if not force_restart:
            resumable = MarketplaceCatalogSync.query.filter(
                MarketplaceCatalogSync.seller_id == account.seller_id,
                MarketplaceCatalogSync.marketplace_id == account.marketplace_id,
                MarketplaceCatalogSync.account_id == account.id,
                MarketplaceCatalogSync.status.in_(("paused", "failed")),
            ).order_by(MarketplaceCatalogSync.id.desc()).first()
            if resumable is not None:
                resumable.status = "running"
                resumable.heartbeat_at = now
                resumable.error_code = None
                resumable.error_message = None
                try:
                    db.session.commit()
                except IntegrityError:
                    db.session.rollback()
                    raise MarketplaceCatalogBusy(
                        "Синхронизация этого кабинета Ozon уже выполняется"
                    ) from None
                return resumable

        run = MarketplaceCatalogSync(
            seller_id=account.seller_id,
            marketplace_id=account.marketplace_id,
            account_id=account.id,
            status="running",
            phase="active",
            visibility=cls.PHASES["active"],
            cursor="",
            started_at=now,
            heartbeat_at=now,
        )
        db.session.add(run)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            raise MarketplaceCatalogBusy(
                "Синхронизация этого кабинета Ozon уже выполняется"
            ) from None
        return run

    @classmethod
    def _mark_failed(
        cls,
        run_id: int,
        error: Exception,
        *,
        now: datetime,
    ) -> None:
        db.session.rollback()
        run = MarketplaceCatalogSync.query.filter_by(id=run_id).first()
        if run is None or run.status == "completed":
            return
        if isinstance(error, MarketplaceListingError):
            code = error.code
            message = str(error)
        elif isinstance(error, OzonAPIError):
            code = error.code
            message = str(error)
        else:
            code = "ozon_catalog_sync_failed"
            message = "Не удалось синхронизировать каталог Ozon"
        run.status = "failed"
        run.error_code = str(code)[:100]
        run.error_message = " ".join(str(message).split())[:1000]
        run.heartbeat_at = now
        db.session.commit()

    @classmethod
    def sync_ozon_account(
        cls,
        *,
        seller_id: int,
        account_id: int,
        max_pages: int = 5,
        force_restart: bool = False,
        adapter=None,
        credentials: Optional[MarketplaceCredentials] = None,
        now: Optional[datetime] = None,
    ) -> MarketplaceCatalogSync:
        seller_id = cls._positive_integer(seller_id, "seller_id")
        account_id = cls._positive_integer(account_id, "account_id")
        max_pages = cls._positive_integer(max_pages, "max_pages")
        if max_pages > cls.MAX_SYNC_PAGES_PER_CALL:
            raise MarketplaceListingValidationError(
                f"max_pages не может быть больше {cls.MAX_SYNC_PAGES_PER_CALL}"
            )
        if not isinstance(force_restart, bool):
            raise MarketplaceListingValidationError(
                "force_restart должен быть boolean"
            )
        current_time = now or datetime.utcnow()
        account, resolved_adapter, resolved_credentials = (
            cls._account_adapter_credentials(
                seller_id=seller_id,
                account_id=account_id,
                adapter=adapter,
                credentials=credentials,
                now=current_time,
            )
        )
        lock_file = cls._try_claim(account.id)
        if lock_file is None:
            raise MarketplaceCatalogBusy(
                "Синхронизация этого кабинета Ozon уже выполняется"
            )
        run = None
        try:
            run = cls._claim_or_create_run(
                account=account,
                force_restart=force_restart,
                now=current_time,
            )
            processed_pages = 0
            while run.phase != "completed":
                if run.phase == "finalize":
                    cls._finalize_run(run, now=datetime.utcnow())
                    break
                if processed_pages >= max_pages:
                    run.status = "paused"
                    run.heartbeat_at = datetime.utcnow()
                    db.session.commit()
                    break
                visibility = cls.PHASES.get(run.phase)
                if visibility is None:
                    raise MarketplaceCatalogProtocolError(
                        "Catalog sync has an unknown phase"
                    )
                response = resolved_adapter.list_products(
                    resolved_credentials,
                    {
                        "filter": {
                            "offer_id": [],
                            "product_id": [],
                            "visibility": visibility,
                        },
                        "last_id": run.cursor,
                        "limit": cls.PAGE_SIZE,
                    },
                )
                page = cls.normalize_product_list_page(response)
                enrichment = cls._fetch_enrichment(
                    adapter=resolved_adapter,
                    credentials=resolved_credentials,
                    base_items=page["items"],
                )
                cls._apply_catalog_page(
                    run=run,
                    page=page,
                    enrichment=enrichment,
                    now=datetime.utcnow(),
                )
                run = db.session.get(MarketplaceCatalogSync, run.id)
                processed_pages += 1
            return run
        except MarketplaceListingError as exc:
            if run is not None:
                cls._mark_failed(run.id, exc, now=datetime.utcnow())
            raise
        except OzonAPIError as exc:
            if run is not None:
                cls._mark_failed(run.id, exc, now=datetime.utcnow())
            raise MarketplaceCatalogSyncError(str(exc)) from None
        except Exception as exc:
            if run is not None:
                cls._mark_failed(run.id, exc, now=datetime.utcnow())
            raise
        finally:
            cls._release_claim(lock_file)

    @classmethod
    def list_listings(
        cls,
        *,
        seller_id: int,
        marketplace_code: Optional[str] = None,
        account_id: Optional[int] = None,
        normalized_status: Optional[str] = None,
        include_unavailable: bool = True,
        search: Optional[str] = None,
        page: int = 1,
        per_page: int = 50,
    ):
        seller_id = cls._positive_integer(seller_id, "seller_id")
        page = cls._positive_integer(page, "page")
        per_page = cls._positive_integer(per_page, "per_page")
        if per_page > 100:
            raise MarketplaceListingValidationError(
                "per_page не может быть больше 100"
            )
        if not isinstance(include_unavailable, bool):
            raise MarketplaceListingValidationError(
                "include_unavailable должен быть boolean"
            )
        query = MarketplaceListing.query.options(
            joinedload(MarketplaceListing.marketplace),
            joinedload(MarketplaceListing.account),
        ).filter(MarketplaceListing.seller_id == seller_id)
        if marketplace_code:
            if not isinstance(marketplace_code, str):
                raise MarketplaceListingValidationError(
                    "marketplace должен быть строкой"
                )
            code = marketplace_code.strip().lower()
            if code not in {"wb", "ozon"}:
                raise MarketplaceListingValidationError(
                    "Неизвестный marketplace"
                )
            query = query.join(Marketplace).filter(Marketplace.code == code)
        if account_id is not None:
            account_id = cls._positive_integer(account_id, "account_id")
            try:
                MarketplaceAccountService.get_owned_account(
                    seller_id=seller_id,
                    account_id=account_id,
                )
            except MarketplaceAccountNotFound:
                raise MarketplaceListingNotFound("Кабинет не найден") from None
            query = query.filter(MarketplaceListing.account_id == account_id)
        if normalized_status:
            if normalized_status not in cls.NORMALIZED_STATUSES:
                raise MarketplaceListingValidationError(
                    "Неизвестный статус листинга"
                )
            query = query.filter(
                MarketplaceListing.normalized_status == normalized_status
            )
        if not include_unavailable:
            query = query.filter(MarketplaceListing.is_available.is_(True))
        if search:
            if not isinstance(search, str):
                raise MarketplaceListingValidationError(
                    "search должен быть строкой"
                )
            term = search.strip()
            if len(term) > 200:
                raise MarketplaceListingValidationError(
                    "search длиннее 200 символов"
                )
            if term:
                escaped = (
                    term.replace("\\", "\\\\")
                    .replace("%", "\\%")
                    .replace("_", "\\_")
                )
                pattern = f"%{escaped}%"
                query = query.filter(or_(
                    MarketplaceListing.title.ilike(pattern, escape="\\"),
                    MarketplaceListing.offer_id.ilike(pattern, escape="\\"),
                    MarketplaceListing.external_product_id.ilike(
                        pattern,
                        escape="\\",
                    ),
                ))
        return query.order_by(
            MarketplaceListing.is_available.desc(),
            MarketplaceListing.updated_at.desc(),
            MarketplaceListing.id.desc(),
        ).paginate(page=page, per_page=per_page, error_out=False)

    @classmethod
    def get_listing(
        cls,
        *,
        seller_id: int,
        listing_id: int,
    ) -> MarketplaceListing:
        seller_id = cls._positive_integer(seller_id, "seller_id")
        listing_id = cls._positive_integer(listing_id, "listing_id")
        listing = MarketplaceListing.query.options(
            joinedload(MarketplaceListing.marketplace),
            joinedload(MarketplaceListing.account),
        ).filter_by(id=listing_id, seller_id=seller_id).first()
        if listing is None:
            raise MarketplaceListingNotFound("Листинг не найден")
        return listing

    @classmethod
    def latest_syncs(cls, *, seller_id: int) -> Dict[int, MarketplaceCatalogSync]:
        seller_id = cls._positive_integer(seller_id, "seller_id")
        rows = MarketplaceCatalogSync.query.options(
            joinedload(MarketplaceCatalogSync.marketplace),
        ).filter_by(seller_id=seller_id).order_by(
            MarketplaceCatalogSync.id.desc()
        ).all()
        result = {}
        for row in rows:
            result.setdefault(row.account_id, row)
        return result
