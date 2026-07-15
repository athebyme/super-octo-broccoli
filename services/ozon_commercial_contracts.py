"""Strict, ORM-free Ozon price, stock and warehouse contracts.

All builders are whitelist-only.  All write response normalizers require an
exact response set so a partial provider result can never be reported as a
successful batch.  HTTP and tenant authorization remain outside this module.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import re
from typing import Any, Mapping, Sequence


class OzonCommercialContractError(RuntimeError):
    code = "ozon_commercial_contract_error"


class OzonCommercialPayloadError(OzonCommercialContractError):
    code = "ozon_commercial_payload_invalid"


class OzonCommercialProtocolError(OzonCommercialContractError):
    code = "ozon_commercial_protocol_error"


class _Contract:
    _PROVIDER_ID = re.compile(r"^[1-9][0-9]*$")
    _OFFER_ID = re.compile(r"^[^\x00-\x1f\x7f]{1,200}$")

    @classmethod
    def provider_id(cls, value: Any, field_name: str) -> str:
        if isinstance(value, int) and not isinstance(value, bool):
            value = str(value)
        if not isinstance(value, str) or not cls._PROVIDER_ID.fullmatch(value):
            raise OzonCommercialPayloadError(
                f"{field_name} must be a canonical positive Ozon ID"
            )
        if int(value) > 9_223_372_036_854_775_807:
            raise OzonCommercialPayloadError(f"{field_name} is out of range")
        return value

    @classmethod
    def response_provider_id(cls, value: Any, field_name: str) -> str:
        try:
            return cls.provider_id(value, field_name)
        except OzonCommercialPayloadError as exc:
            raise OzonCommercialProtocolError(str(exc)) from None

    @classmethod
    def offer_id(cls, value: Any, field_name: str = "offer_id") -> str:
        if not isinstance(value, str):
            raise OzonCommercialPayloadError(f"{field_name} must be a string")
        normalized = value.strip()
        if not cls._OFFER_ID.fullmatch(normalized):
            raise OzonCommercialPayloadError(f"{field_name} is invalid")
        return normalized

    @classmethod
    def response_offer_id(cls, value: Any, field_name: str) -> str:
        try:
            return cls.offer_id(value, field_name)
        except OzonCommercialPayloadError as exc:
            raise OzonCommercialProtocolError(str(exc)) from None

    @staticmethod
    def boolean(value: Any, field_name: str) -> bool:
        if not isinstance(value, bool):
            raise OzonCommercialProtocolError(f"{field_name} must be boolean")
        return value

    @staticmethod
    def integer(
        value: Any,
        field_name: str,
        *,
        minimum: int = 0,
        maximum: int = 2_147_483_647,
        payload: bool = False,
    ) -> int:
        error = OzonCommercialPayloadError if payload else OzonCommercialProtocolError
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or value < minimum
            or value > maximum
        ):
            raise error(
                f"{field_name} must be an integer between {minimum} and {maximum}"
            )
        return value

    @staticmethod
    def text(
        value: Any,
        field_name: str,
        *,
        maximum: int,
        allow_empty: bool = False,
    ) -> str:
        if not isinstance(value, str):
            raise OzonCommercialProtocolError(f"{field_name} must be a string")
        normalized = value.strip()
        if not normalized and not allow_empty:
            raise OzonCommercialProtocolError(f"{field_name} must be non-empty")
        if len(normalized) > maximum or any(
            ord(character) < 32 or ord(character) == 127
            for character in normalized
        ):
            raise OzonCommercialProtocolError(f"{field_name} is invalid")
        return normalized

    @staticmethod
    def money(
        value: Any,
        field_name: str,
        *,
        allow_zero: bool = False,
    ) -> str:
        if isinstance(value, bool):
            raise OzonCommercialPayloadError(f"{field_name} must be decimal")
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            raise OzonCommercialPayloadError(
                f"{field_name} must be decimal"
            ) from None
        minimum = Decimal("0") if allow_zero else Decimal("0.01")
        if (
            not parsed.is_finite()
            or parsed < minimum
            or parsed > Decimal("999999999.99")
            or parsed.as_tuple().exponent < -2
        ):
            raise OzonCommercialPayloadError(
                f"{field_name} must be a bounded decimal with at most 2 digits"
            )
        rendered = format(parsed, "f")
        if "." in rendered:
            rendered = rendered.rstrip("0").rstrip(".")
        return rendered or "0"

    @classmethod
    def response_errors(cls, value: Any, field_name: str) -> list:
        if not isinstance(value, list) or len(value) > 100:
            raise OzonCommercialProtocolError(
                f"{field_name} must be a bounded list"
            )
        errors = []
        for index, raw in enumerate(value):
            if not isinstance(raw, dict):
                raise OzonCommercialProtocolError(
                    f"{field_name}[{index}] must be an object"
                )
            code = raw.get("code")
            message = raw.get("message")
            errors.append({
                "code": cls.text(
                    code,
                    f"{field_name}[{index}].code",
                    maximum=100,
                    allow_empty=True,
                ) if code is not None else "",
                "message": cls.text(
                    message,
                    f"{field_name}[{index}].message",
                    maximum=1000,
                    allow_empty=True,
                ) if message is not None else "",
            })
        return errors


class OzonPriceContract(_Contract):
    CONTRACT_VERSION = "product-import-prices-v1@2026-07-15"
    MAX_BATCH = 100

    @classmethod
    def build_item(
        cls,
        *,
        offer_id: Any,
        product_id: Any,
        price: Any,
        currency_code: Any = "RUB",
        old_price: Any = None,
    ) -> dict:
        normalized_offer = cls.offer_id(offer_id)
        normalized_product = cls.provider_id(product_id, "product_id")
        normalized_price = cls.money(price, "price")
        if not isinstance(currency_code, str) or currency_code.strip().upper() != "RUB":
            raise OzonCommercialPayloadError(
                "Only RUB price updates are enabled in the current rollout"
            )
        item = {
            "offer_id": normalized_offer,
            "product_id": int(normalized_product),
            "price": normalized_price,
            "currency_code": "RUB",
        }
        if old_price is not None:
            normalized_old = cls.money(old_price, "old_price", allow_zero=True)
            if Decimal(normalized_old) != 0 and Decimal(normalized_old) <= Decimal(
                normalized_price
            ):
                raise OzonCommercialPayloadError(
                    "old_price must be zero or greater than price"
                )
            item["old_price"] = normalized_old
        return item

    @classmethod
    def build_payload(cls, items: Sequence[Mapping[str, Any]]) -> dict:
        if not isinstance(items, (list, tuple)) or not 1 <= len(items) <= cls.MAX_BATCH:
            raise OzonCommercialPayloadError(
                f"Price batch must contain 1..{cls.MAX_BATCH} items"
            )
        normalized = []
        identities = set()
        for index, item in enumerate(items):
            if not isinstance(item, Mapping):
                raise OzonCommercialPayloadError(
                    f"prices[{index}] must be an object"
                )
            unexpected = set(item) - {
                "offer_id", "product_id", "price", "currency_code", "old_price"
            }
            if unexpected:
                raise OzonCommercialPayloadError(
                    f"prices[{index}] contains unsupported fields"
                )
            result = cls.build_item(**dict(item))
            identity = (result["offer_id"], result["product_id"])
            if identity in identities:
                raise OzonCommercialPayloadError("Duplicate price identity")
            identities.add(identity)
            normalized.append(result)
        return {"prices": normalized}

    @classmethod
    def normalize_response(cls, response: Any, expected_payload: Mapping[str, Any]) -> dict:
        expected = expected_payload.get("prices") if isinstance(expected_payload, Mapping) else None
        if not isinstance(expected, list) or not expected:
            raise OzonCommercialPayloadError("Expected price payload is invalid")
        if not isinstance(response, dict) or not isinstance(response.get("result"), list):
            raise OzonCommercialProtocolError("Ozon price response has no result list")
        raw_results = response["result"]
        if len(raw_results) != len(expected):
            raise OzonCommercialProtocolError("Ozon price response is not exact-set")
        expected_by_offer = {item["offer_id"]: item for item in expected}
        if len(expected_by_offer) != len(expected):
            raise OzonCommercialPayloadError("Expected prices contain duplicate offers")
        results = []
        seen = set()
        for index, raw in enumerate(raw_results):
            if not isinstance(raw, dict):
                raise OzonCommercialProtocolError(
                    f"Ozon price result[{index}] must be an object"
                )
            offer = cls.response_offer_id(raw.get("offer_id"), f"result[{index}].offer_id")
            product = cls.response_provider_id(
                raw.get("product_id"), f"result[{index}].product_id"
            )
            expected_item = expected_by_offer.get(offer)
            if expected_item is None or product != str(expected_item["product_id"]):
                raise OzonCommercialProtocolError("Ozon price response has foreign identity")
            if offer in seen:
                raise OzonCommercialProtocolError("Ozon price response has duplicate identity")
            seen.add(offer)
            results.append({
                "offer_id": offer,
                "product_id": product,
                "updated": cls.boolean(raw.get("updated"), f"result[{index}].updated"),
                "errors": cls.response_errors(raw.get("errors"), f"result[{index}].errors"),
            })
        return {
            "items": results,
            "updated": sum(item["updated"] and not item["errors"] for item in results),
            "failed": sum((not item["updated"]) or bool(item["errors"]) for item in results),
        }


class OzonStockContract(_Contract):
    CONTRACT_VERSION = "products-stocks-v2@2026-07-15"
    READ_CONTRACT_VERSION = "product-stocks-by-warehouse-fbs-v2@2026-07-15"
    MAX_BATCH = 100

    @classmethod
    def build_item(
        cls,
        *,
        offer_id: Any,
        product_id: Any,
        warehouse_id: Any,
        stock: Any,
    ) -> dict:
        return {
            "offer_id": cls.offer_id(offer_id),
            "product_id": int(cls.provider_id(product_id, "product_id")),
            "warehouse_id": int(cls.provider_id(warehouse_id, "warehouse_id")),
            "stock": cls.integer(
                stock,
                "stock",
                minimum=0,
                maximum=2_147_483_647,
                payload=True,
            ),
        }

    @classmethod
    def build_payload(cls, items: Sequence[Mapping[str, Any]]) -> dict:
        if not isinstance(items, (list, tuple)) or not 1 <= len(items) <= cls.MAX_BATCH:
            raise OzonCommercialPayloadError(
                f"Stock batch must contain 1..{cls.MAX_BATCH} items"
            )
        normalized = []
        identities = set()
        for index, item in enumerate(items):
            if not isinstance(item, Mapping) or set(item) != {
                "offer_id", "product_id", "warehouse_id", "stock"
            }:
                raise OzonCommercialPayloadError(
                    f"stocks[{index}] must contain exact stock fields"
                )
            result = cls.build_item(**dict(item))
            identity = (
                result["offer_id"],
                result["product_id"],
                result["warehouse_id"],
            )
            if identity in identities:
                raise OzonCommercialPayloadError("Duplicate stock identity")
            identities.add(identity)
            normalized.append(result)
        return {"stocks": normalized}

    @classmethod
    def normalize_response(cls, response: Any, expected_payload: Mapping[str, Any]) -> dict:
        expected = expected_payload.get("stocks") if isinstance(expected_payload, Mapping) else None
        if not isinstance(expected, list) or not expected:
            raise OzonCommercialPayloadError("Expected stock payload is invalid")
        if not isinstance(response, dict) or not isinstance(response.get("result"), list):
            raise OzonCommercialProtocolError("Ozon stock response has no result list")
        raw_results = response["result"]
        if len(raw_results) != len(expected):
            raise OzonCommercialProtocolError("Ozon stock response is not exact-set")
        expected_by_identity = {
            (item["offer_id"], str(item["product_id"]), str(item["warehouse_id"])): item
            for item in expected
        }
        results = []
        seen = set()
        for index, raw in enumerate(raw_results):
            if not isinstance(raw, dict):
                raise OzonCommercialProtocolError(
                    f"Ozon stock result[{index}] must be an object"
                )
            identity = (
                cls.response_offer_id(raw.get("offer_id"), f"result[{index}].offer_id"),
                cls.response_provider_id(raw.get("product_id"), f"result[{index}].product_id"),
                cls.response_provider_id(raw.get("warehouse_id"), f"result[{index}].warehouse_id"),
            )
            if identity not in expected_by_identity:
                raise OzonCommercialProtocolError("Ozon stock response has foreign identity")
            if identity in seen:
                raise OzonCommercialProtocolError("Ozon stock response has duplicate identity")
            seen.add(identity)
            results.append({
                "offer_id": identity[0],
                "product_id": identity[1],
                "warehouse_id": identity[2],
                "updated": cls.boolean(raw.get("updated"), f"result[{index}].updated"),
                "errors": cls.response_errors(raw.get("errors"), f"result[{index}].errors"),
            })
        return {
            "items": results,
            "updated": sum(item["updated"] and not item["errors"] for item in results),
            "failed": sum((not item["updated"]) or bool(item["errors"]) for item in results),
        }

    @classmethod
    def normalize_fbs_page(cls, response: Any) -> dict:
        if not isinstance(response, dict) or not isinstance(response.get("products"), list):
            raise OzonCommercialProtocolError("Ozon FBS stock response has no products")
        raw_products = response["products"]
        if len(raw_products) > 1000:
            raise OzonCommercialProtocolError("Ozon FBS stock page is too large")
        cursor_raw = response.get("cursor")
        cursor = "" if cursor_raw in (None, "") else cls.text(
            cursor_raw, "cursor", maximum=2000
        )
        has_next = cls.boolean(response.get("has_next"), "has_next")
        if has_next and not cursor:
            raise OzonCommercialProtocolError("Ozon FBS stock page has no next cursor")
        products = []
        seen = set()
        for index, raw in enumerate(raw_products):
            if not isinstance(raw, dict):
                raise OzonCommercialProtocolError(f"products[{index}] must be an object")
            identity = (
                cls.response_offer_id(raw.get("offer_id"), f"products[{index}].offer_id"),
                cls.response_provider_id(raw.get("product_id"), f"products[{index}].product_id"),
                cls.response_provider_id(raw.get("warehouse_id"), f"products[{index}].warehouse_id"),
            )
            if identity in seen:
                raise OzonCommercialProtocolError("Duplicate FBS stock identity")
            seen.add(identity)
            present = cls.integer(raw.get("present"), f"products[{index}].present")
            reserved = cls.integer(raw.get("reserved"), f"products[{index}].reserved")
            free_stock = cls.integer(raw.get("free_stock"), f"products[{index}].free_stock")
            products.append({
                "offer_id": identity[0],
                "product_id": identity[1],
                "warehouse_id": identity[2],
                "sku": cls.response_provider_id(raw.get("sku"), f"products[{index}].sku"),
                "warehouse_name": cls.text(
                    raw.get("warehouse_name"),
                    f"products[{index}].warehouse_name",
                    maximum=500,
                ),
                "present": present,
                "reserved": reserved,
                "free_stock": free_stock,
            })
        return {"products": products, "cursor": cursor, "has_next": has_next}


class OzonWarehouseContract(_Contract):
    CONTRACT_VERSION = "warehouse-list-v2@2026-07-15"
    PAGE_SIZE = 100

    @classmethod
    def request_payload(cls, *, cursor: str = "") -> dict:
        if not isinstance(cursor, str) or len(cursor) > 2000:
            raise OzonCommercialPayloadError("Warehouse cursor is invalid")
        return {"limit": cls.PAGE_SIZE, "cursor": cursor}

    @classmethod
    def normalize_page(cls, response: Any) -> dict:
        if not isinstance(response, dict) or not isinstance(response.get("warehouses"), list):
            raise OzonCommercialProtocolError("Ozon warehouse response has no warehouses")
        raw_warehouses = response["warehouses"]
        if len(raw_warehouses) > cls.PAGE_SIZE:
            raise OzonCommercialProtocolError("Ozon warehouse page is too large")
        cursor_raw = response.get("cursor")
        cursor = "" if cursor_raw in (None, "") else cls.text(
            cursor_raw, "cursor", maximum=2000
        )
        has_next = cls.boolean(response.get("has_next"), "has_next")
        if has_next and not cursor:
            raise OzonCommercialProtocolError("Ozon warehouse page has no next cursor")
        warehouses = []
        seen = set()
        for index, raw in enumerate(raw_warehouses):
            if not isinstance(raw, dict):
                raise OzonCommercialProtocolError(
                    f"warehouses[{index}] must be an object"
                )
            warehouse_id = cls.response_provider_id(
                raw.get("warehouse_id"), f"warehouses[{index}].warehouse_id"
            )
            if warehouse_id in seen:
                raise OzonCommercialProtocolError("Duplicate warehouse_id")
            seen.add(warehouse_id)
            optional_text = {}
            for key, maximum in (
                ("status", 100),
                ("warehouse_type", 100),
                ("carriage_label_type", 100),
            ):
                if raw.get(key) is not None:
                    optional_text[key] = cls.text(
                        raw[key], f"warehouses[{index}].{key}", maximum=maximum
                    )
            flags = {}
            for key in (
                "has_entrusted_acceptance",
                "has_postings_limit",
                "is_auto_assembly",
                "is_comfort",
                "is_express",
                "is_kgt",
                "is_rfbs",
                "is_waybill_enabled",
                "with_item_list",
            ):
                if raw.get(key) is not None:
                    flags[key] = cls.boolean(
                        raw[key], f"warehouses[{index}].{key}"
                    )
            limits = {}
            for key, minimum in (
                ("cut_in_time", 0),
                ("sla_cut_in", 0),
                ("min_postings_limit", 0),
                ("postings_limit", -1),
            ):
                if raw.get(key) is not None:
                    limits[key] = cls.integer(
                        raw[key], f"warehouses[{index}].{key}", minimum=minimum
                    )
            warehouses.append({
                "warehouse_id": warehouse_id,
                "name": cls.text(
                    raw.get("name"), f"warehouses[{index}].name", maximum=500
                ),
                **optional_text,
                "flags": flags,
                "limits": limits,
            })
        return {"warehouses": warehouses, "cursor": cursor, "has_next": has_next}
