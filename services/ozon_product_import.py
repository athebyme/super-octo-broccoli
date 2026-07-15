"""Strict Ozon product-import payload and response contracts.

This module performs no HTTP request and never reads credentials.  It converts
one already seller-scoped draft into the current ``/v3/product/import`` body,
normalizes the asynchronous status response, and reduces the quota response to
the counters needed by the durable operation service.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
from typing import Any, List, Optional, Sequence

from models import (
    MarketplaceAttributeDefinition,
    MarketplaceProductDraft,
)


class OzonProductImportContractError(RuntimeError):
    code = "ozon_product_import_contract_error"


class OzonProductImportPayloadError(OzonProductImportContractError):
    code = "ozon_product_import_payload_invalid"


class OzonProductImportProtocolError(OzonProductImportContractError):
    code = "ozon_product_import_protocol_error"


class OzonProductImportContract:
    """Whitelist-only mapper for the current Ozon async product import."""

    CONTRACT_VERSION = "product-import-v3@2026-07-10"
    STATUS_CONTRACT_VERSION = "product-import-info-v1@2026-07-10"
    QUOTA_CONTRACT_VERSION = "product-info-limit-v4@2026-06-09"
    DESCRIPTION_ATTRIBUTE_ID = "4191"
    MAX_BODY_BYTES = 512 * 1024
    MAX_OFFER_ID_CHARS = 50
    MAX_IMAGES = 30
    MAX_STATUS_ITEMS = 100
    MAX_ERRORS_PER_ITEM = 200
    MAX_PROVIDER_TEXT = 5_000
    TERMINAL_ITEM_STATUSES = {"imported", "failed", "skipped"}
    ITEM_STATUSES = TERMINAL_ITEM_STATUSES | {"pending"}
    DIMENSION_UNITS = {
        "MILLIMETERS": "mm",
        "CENTIMETERS": "cm",
        "INCHES": "in",
    }
    WEIGHT_UNITS = {
        "GRAMS": "g",
        "KILOGRAMS": "kg",
        "POUNDS": "lb",
    }
    VAT_CANONICAL = {
        "0": "0",
        "0.05": "0.05",
        "0.07": "0.07",
        "0.1": "0.10",
        "0.10": "0.10",
        "0.2": "0.20",
        "0.20": "0.20",
        "0.22": "0.22",
    }
    _CANONICAL_PROVIDER_ID = re.compile(r"^[1-9][0-9]*$")

    @staticmethod
    def canonical_json(value: Any, *, maximum: Optional[int] = None) -> str:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        size = len(encoded.encode("utf-8"))
        if maximum is not None and size > maximum:
            raise OzonProductImportPayloadError(
                f"Ozon payload exceeds {maximum} bytes"
            )
        return encoded

    @classmethod
    def fingerprint(cls, value: Any) -> str:
        return hashlib.sha256(
            cls.canonical_json(value).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _stored_json(raw_value: Optional[str], expected_type: type) -> Any:
        try:
            value = json.loads(raw_value or "")
        except (TypeError, json.JSONDecodeError):
            raise OzonProductImportPayloadError(
                "Draft contains malformed normalized JSON"
            ) from None
        if not isinstance(value, expected_type):
            raise OzonProductImportPayloadError(
                "Draft normalized JSON has an unexpected type"
            )
        return value

    @classmethod
    def _provider_id(cls, value: Any, field_name: str) -> int:
        if (
            not isinstance(value, str)
            or not cls._CANONICAL_PROVIDER_ID.fullmatch(value)
        ):
            raise OzonProductImportPayloadError(
                f"{field_name} must be a canonical positive Ozon integer ID"
            )
        parsed = int(value)
        if parsed > 9_223_372_036_854_775_807:
            raise OzonProductImportPayloadError(f"{field_name} is out of range")
        return parsed

    @staticmethod
    def _required_text(
        value: Any,
        field_name: str,
        *,
        maximum: int,
        allow_newlines: bool = False,
    ) -> str:
        if not isinstance(value, str):
            raise OzonProductImportPayloadError(f"{field_name} must be a string")
        normalized = value.strip()
        if not normalized:
            raise OzonProductImportPayloadError(f"{field_name} is required")
        if len(normalized) > maximum:
            raise OzonProductImportPayloadError(
                f"{field_name} exceeds {maximum} characters"
            )
        allowed_controls = {9, 10, 13} if allow_newlines else set()
        if any(
            (ord(character) < 32 and ord(character) not in allowed_controls)
            or ord(character) == 127
            for character in normalized
        ):
            raise OzonProductImportPayloadError(
                f"{field_name} contains control characters"
            )
        return normalized

    @classmethod
    def _positive_api_integer(cls, value: Any, field_name: str) -> int:
        if isinstance(value, bool):
            raise OzonProductImportPayloadError(
                f"{field_name} must be a positive integer"
            )
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            raise OzonProductImportPayloadError(
                f"{field_name} must be a positive integer"
            ) from None
        if not parsed.is_finite() or parsed <= 0 or parsed != parsed.to_integral_value():
            raise OzonProductImportPayloadError(
                f"{field_name} must be an exact positive integer in its selected unit"
            )
        integer = int(parsed)
        if integer > 2_147_483_647:
            raise OzonProductImportPayloadError(f"{field_name} is out of range")
        return integer

    @classmethod
    def _price(cls, value: Any, field_name: str) -> str:
        if isinstance(value, bool):
            raise OzonProductImportPayloadError(f"{field_name} must be positive")
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            raise OzonProductImportPayloadError(
                f"{field_name} must be positive"
            ) from None
        if not parsed.is_finite() or parsed <= 0:
            raise OzonProductImportPayloadError(f"{field_name} must be positive")
        rendered = format(parsed.normalize(), "f")
        if "." in rendered:
            rendered = rendered.rstrip("0").rstrip(".")
        if len(rendered) > 40:
            raise OzonProductImportPayloadError(f"{field_name} is out of range")
        return rendered

    @classmethod
    def _attribute_value(cls, raw: Any, field_name: str) -> dict:
        if not isinstance(raw, dict) or set(raw) - {
            "dictionary_value_id",
            "value",
        }:
            raise OzonProductImportPayloadError(
                f"{field_name} must be a normalized attribute value"
            )
        result = {
            "value": cls._required_text(
                raw.get("value"),
                f"{field_name}.value",
                maximum=100_000,
                allow_newlines=True,
            )
        }
        if raw.get("dictionary_value_id") not in (None, ""):
            result["dictionary_value_id"] = cls._provider_id(
                raw["dictionary_value_id"],
                f"{field_name}.dictionary_value_id",
            )
        return result

    @classmethod
    def _attribute(cls, raw: Any, field_name: str) -> dict:
        if not isinstance(raw, dict) or set(raw) != {
            "attribute_id",
            "complex_id",
            "values",
        }:
            raise OzonProductImportPayloadError(
                f"{field_name} must be a normalized attribute"
            )
        complex_id = raw.get("complex_id")
        if complex_id == "0":
            normalized_complex_id = 0
        else:
            normalized_complex_id = cls._provider_id(
                complex_id,
                f"{field_name}.complex_id",
            )
        values = raw.get("values")
        if not isinstance(values, list) or not values or len(values) > 100:
            raise OzonProductImportPayloadError(
                f"{field_name}.values must contain 1..100 items"
            )
        return {
            "id": cls._provider_id(
                raw.get("attribute_id"),
                f"{field_name}.attribute_id",
            ),
            "complex_id": normalized_complex_id,
            "values": [
                cls._attribute_value(value, f"{field_name}.values[{index}]")
                for index, value in enumerate(values)
            ],
        }

    @classmethod
    def _description_attribute(
        cls,
        draft: MarketplaceProductDraft,
        description: str,
        attributes: list,
    ) -> list:
        if draft.product_type_id is None:
            raise OzonProductImportPayloadError("Draft has no Ozon product type")
        definitions = MarketplaceAttributeDefinition.query.filter_by(
            product_type_id=draft.product_type_id,
            external_attribute_id=cls.DESCRIPTION_ATTRIBUTE_ID,
            is_available=True,
            is_enabled=True,
        ).all()
        if len(definitions) != 1:
            raise OzonProductImportPayloadError(
                "Fresh Ozon schema does not contain one enabled description attribute 4191"
            )
        definition = definitions[0]
        if definition.dictionary_id or definition.attribute_complex_id:
            raise OzonProductImportPayloadError(
                "Ozon description attribute 4191 has an unsupported schema"
            )

        matches = [
            item for item in attributes
            if item.get("id") == int(cls.DESCRIPTION_ATTRIBUTE_ID)
        ]
        if len(matches) > 1:
            raise OzonProductImportPayloadError(
                "Ozon description attribute 4191 is duplicated"
            )
        if matches:
            values = matches[0].get("values")
            if (
                len(values) != 1
                or set(values[0]) != {"value"}
                or values[0]["value"] != description
            ):
                raise OzonProductImportPayloadError(
                    "content.description conflicts with attribute 4191"
                )
            return attributes

        return attributes + [{
            "id": int(cls.DESCRIPTION_ATTRIBUTE_ID),
            "complex_id": 0,
            "values": [{"value": description}],
        }]

    @classmethod
    def build_payload(cls, draft: MarketplaceProductDraft) -> dict:
        """Build one full, current Ozon import item from normalized draft state."""
        if not isinstance(draft, MarketplaceProductDraft):
            raise OzonProductImportPayloadError(
                "MarketplaceProductDraft is required"
            )
        if not draft.product_type or not draft.product_type.category:
            raise OzonProductImportPayloadError(
                "Draft has no exact Ozon category/type binding"
            )

        offer_id = cls._required_text(
            draft.offer_id,
            "offer_id",
            maximum=cls.MAX_OFFER_ID_CHARS,
        )
        content = cls._stored_json(draft.content_json, dict)
        name = cls._required_text(
            content.get("name"),
            "content.name",
            maximum=500,
        )
        description = cls._required_text(
            content.get("description"),
            "content.description",
            maximum=100_000,
            allow_newlines=True,
        )

        raw_attributes = cls._stored_json(draft.attributes_json, list)
        if len(raw_attributes) > 5_000:
            raise OzonProductImportPayloadError("Too many Ozon attributes")
        attributes = [
            cls._attribute(raw, f"attributes[{index}]")
            for index, raw in enumerate(raw_attributes)
        ]
        attributes = cls._description_attribute(
            draft,
            description,
            attributes,
        )

        raw_complex = cls._stored_json(draft.complex_attributes_json, list)
        if len(raw_complex) > 500:
            raise OzonProductImportPayloadError("Too many Ozon complex groups")
        complex_attributes = []
        for group_index, group in enumerate(raw_complex):
            if not isinstance(group, dict) or set(group) != {"attributes"}:
                raise OzonProductImportPayloadError(
                    f"complex_attributes[{group_index}] is malformed"
                )
            group_attributes = group.get("attributes")
            if not isinstance(group_attributes, list) or not group_attributes:
                raise OzonProductImportPayloadError(
                    f"complex_attributes[{group_index}].attributes must be non-empty"
                )
            complex_attributes.append({
                "attributes": [
                    cls._attribute(
                        raw,
                        f"complex_attributes[{group_index}].attributes[{index}]",
                    )
                    for index, raw in enumerate(group_attributes)
                ]
            })

        media = cls._stored_json(draft.media_json, dict)
        if set(media) - {"images", "primary_image", "color_image"}:
            raise OzonProductImportPayloadError(
                "Draft media contains fields outside the current Ozon contract"
            )
        images = media.get("images")
        primary_image = media.get("primary_image")
        if (
            not isinstance(images, list)
            or (not images and primary_image in (None, ""))
            or len(images) > cls.MAX_IMAGES - (1 if primary_image else 0)
        ):
            raise OzonProductImportPayloadError(
                f"Ozon import requires 1..{cls.MAX_IMAGES} main images total"
            )
        normalized_images = [
            cls._required_text(
                image,
                f"media.images[{index}]",
                maximum=2_000,
            )
            for index, image in enumerate(images)
        ]
        if len(set(normalized_images)) != len(normalized_images):
            raise OzonProductImportPayloadError("Ozon image URLs are duplicated")
        normalized_primary = None
        if primary_image not in (None, ""):
            normalized_primary = cls._required_text(
                primary_image,
                "media.primary_image",
                maximum=2_000,
            )
            if normalized_primary in normalized_images:
                raise OzonProductImportPayloadError(
                    "Ozon primary_image duplicates images"
                )
        normalized_color = None
        if media.get("color_image") not in (None, ""):
            normalized_color = cls._required_text(
                media["color_image"],
                "media.color_image",
                maximum=2_000,
            )
            if normalized_color in set(normalized_images) | {normalized_primary}:
                raise OzonProductImportPayloadError(
                    "Ozon color_image duplicates a main image"
                )

        dimensions = cls._stored_json(draft.dimensions_json, dict)
        expected_dimensions = {
            "width",
            "height",
            "depth",
            "dimension_unit",
            "weight",
            "weight_unit",
        }
        if set(dimensions) != expected_dimensions:
            raise OzonProductImportPayloadError(
                "Draft dimensions must be a complete Ozon physical fact set"
            )
        dimension_unit = cls.DIMENSION_UNITS.get(dimensions["dimension_unit"])
        weight_unit = cls.WEIGHT_UNITS.get(dimensions["weight_unit"])
        if dimension_unit is None or weight_unit is None:
            raise OzonProductImportPayloadError("Unsupported Ozon physical unit")

        barcodes = cls._stored_json(draft.barcodes_json, list)
        if len(barcodes) > 1:
            raise OzonProductImportPayloadError(
                "/v3/product/import accepts one barcode; use the barcode workflow for extras"
            )

        commercial = cls._stored_json(draft.commercial_json, dict)
        if set(commercial) - {"price", "old_price", "vat", "currency_code"}:
            raise OzonProductImportPayloadError(
                "Draft commercial data contains unknown fields"
            )
        currency_code = commercial.get("currency_code")
        if currency_code != "RUB":
            raise OzonProductImportPayloadError(
                "Current Ozon rollout supports currency_code=RUB only"
            )
        vat = cls.VAT_CANONICAL.get(commercial.get("vat"))
        if vat is None:
            raise OzonProductImportPayloadError("Unsupported Ozon VAT value")

        item = {
            "attributes": attributes,
            "complex_attributes": complex_attributes,
            "currency_code": "RUB",
            "depth": cls._positive_api_integer(
                dimensions["depth"],
                "dimensions.depth",
            ),
            "description_category_id": cls._provider_id(
                draft.external_category_id,
                "description_category_id",
            ),
            "dimension_unit": dimension_unit,
            "height": cls._positive_api_integer(
                dimensions["height"],
                "dimensions.height",
            ),
            "images": normalized_images,
            "name": name,
            "offer_id": offer_id,
            "price": cls._price(commercial.get("price"), "commercial.price"),
            "type_id": cls._provider_id(
                draft.external_type_id,
                "type_id",
            ),
            "vat": vat,
            "weight": cls._positive_api_integer(
                dimensions["weight"],
                "dimensions.weight",
            ),
            "weight_unit": weight_unit,
            "width": cls._positive_api_integer(
                dimensions["width"],
                "dimensions.width",
            ),
        }
        if normalized_primary:
            item["primary_image"] = normalized_primary
        if normalized_color:
            item["color_image"] = normalized_color
        if barcodes:
            item["barcode"] = cls._required_text(
                barcodes[0],
                "barcodes[0]",
                maximum=100,
            )
        if commercial.get("old_price") not in (None, ""):
            old_price = cls._price(
                commercial["old_price"],
                "commercial.old_price",
            )
            if Decimal(old_price) <= Decimal(item["price"]):
                raise OzonProductImportPayloadError(
                    "commercial.old_price must be greater than price"
                )
            item["old_price"] = old_price

        payload = {"items": [item]}
        cls.canonical_json(payload, maximum=cls.MAX_BODY_BYTES)
        return payload

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
            raise OzonProductImportProtocolError(
                f"Ozon {field_name} must be an integer >= {minimum}"
            )
        return value

    @classmethod
    def _provider_text(
        cls,
        value: Any,
        field_name: str,
        *,
        maximum: int,
        allow_empty: bool = False,
    ) -> str:
        if not isinstance(value, str):
            raise OzonProductImportProtocolError(
                f"Ozon {field_name} must be a string"
            )
        normalized = value.strip()
        if not normalized and not allow_empty:
            raise OzonProductImportProtocolError(
                f"Ozon {field_name} must be non-empty"
            )
        if len(normalized) > maximum:
            raise OzonProductImportProtocolError(
                f"Ozon {field_name} exceeds {maximum} characters"
            )
        if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
            raise OzonProductImportProtocolError(
                f"Ozon {field_name} contains control characters"
            )
        return normalized

    @classmethod
    def normalize_submission(cls, response: Any) -> dict:
        if not isinstance(response, dict):
            raise OzonProductImportProtocolError(
                "Ozon product import response must be an object"
            )
        result = response.get("result")
        if not isinstance(result, dict):
            raise OzonProductImportProtocolError(
                "Ozon product import response has no result object"
            )
        task_id = cls._provider_integer(
            result.get("task_id"),
            "product_import.result.task_id",
            minimum=1,
        )
        return {"task_id": str(task_id)}

    @classmethod
    def _normalized_status_error(cls, value: Any, field_name: str) -> dict:
        if not isinstance(value, dict):
            raise OzonProductImportProtocolError(
                f"Ozon {field_name} must be an object"
            )
        result = {}
        for key, maximum in (
            ("code", 200),
            ("message", 2_000),
            ("state", 200),
            ("level", 100),
            ("description", 2_000),
            ("field", 500),
            ("attribute_name", 500),
        ):
            raw = value.get(key)
            if raw in (None, ""):
                continue
            result[key] = cls._provider_text(
                raw,
                f"{field_name}.{key}",
                maximum=maximum,
            )
        raw_attribute_id = value.get("attribute_id")
        if raw_attribute_id not in (None, 0, ""):
            result["attribute_id"] = str(cls._provider_integer(
                raw_attribute_id,
                f"{field_name}.attribute_id",
                minimum=1,
            ))
        if not result:
            raise OzonProductImportProtocolError(
                f"Ozon {field_name} contains no supported error fields"
            )
        return result

    @classmethod
    def normalize_status(
        cls,
        response: Any,
        *,
        expected_offer_ids: Sequence[str],
    ) -> dict:
        if not isinstance(response, dict):
            raise OzonProductImportProtocolError(
                "Ozon product import status response must be an object"
            )
        if (
            not isinstance(expected_offer_ids, Sequence)
            or isinstance(expected_offer_ids, (str, bytes))
            or not expected_offer_ids
            or len(expected_offer_ids) > cls.MAX_STATUS_ITEMS
        ):
            raise OzonProductImportProtocolError(
                "Expected offer scope must be a bounded non-empty sequence"
            )
        expected = []
        for index, offer_id in enumerate(expected_offer_ids):
            expected.append(cls._provider_text(
                offer_id,
                f"expected_offer_ids[{index}]",
                maximum=cls.MAX_OFFER_ID_CHARS,
            ))
        if len(set(expected)) != len(expected):
            raise OzonProductImportProtocolError(
                "Expected offer scope contains duplicates"
            )

        result = response.get("result")
        if not isinstance(result, dict):
            raise OzonProductImportProtocolError(
                "Ozon product import status has no result object"
            )
        items = result.get("items")
        if not isinstance(items, list) or len(items) > cls.MAX_STATUS_ITEMS:
            raise OzonProductImportProtocolError(
                "Ozon product import status items must be a bounded list"
            )
        total = cls._provider_integer(
            result.get("total"),
            "product_import_info.result.total",
        )
        if total != len(items) or len(items) != len(expected):
            raise OzonProductImportProtocolError(
                "Ozon product import status does not match the submitted exact set"
            )

        normalized_items = []
        seen = set()
        for index, raw in enumerate(items):
            field_name = f"product_import_info.result.items[{index}]"
            if not isinstance(raw, dict):
                raise OzonProductImportProtocolError(
                    f"Ozon {field_name} must be an object"
                )
            offer_id = cls._provider_text(
                raw.get("offer_id"),
                f"{field_name}.offer_id",
                maximum=cls.MAX_OFFER_ID_CHARS,
            )
            if offer_id in seen or offer_id not in expected:
                raise OzonProductImportProtocolError(
                    "Ozon product import status contains a foreign or duplicate offer"
                )
            seen.add(offer_id)
            status = cls._provider_text(
                raw.get("status"),
                f"{field_name}.status",
                maximum=50,
            ).lower()
            if status not in cls.ITEM_STATUSES:
                raise OzonProductImportProtocolError(
                    f"Ozon returned unsupported import status {status!r}"
                )
            product_id = cls._provider_integer(
                raw.get("product_id", 0),
                f"{field_name}.product_id",
            )
            raw_errors = raw.get("errors", [])
            if (
                not isinstance(raw_errors, list)
                or len(raw_errors) > cls.MAX_ERRORS_PER_ITEM
            ):
                raise OzonProductImportProtocolError(
                    f"Ozon {field_name}.errors must be a bounded list"
                )
            normalized_items.append({
                "offer_id": offer_id,
                "product_id": str(product_id) if product_id > 0 else None,
                "status": status,
                "errors": [
                    cls._normalized_status_error(
                        error,
                        f"{field_name}.errors[{error_index}]",
                    )
                    for error_index, error in enumerate(raw_errors)
                ],
            })
        if seen != set(expected):
            raise OzonProductImportProtocolError(
                "Ozon product import status omitted a submitted offer"
            )

        statuses = {item["status"] for item in normalized_items}
        if "pending" in statuses:
            aggregate = "pending"
        elif statuses == {"imported"}:
            aggregate = "succeeded"
        elif statuses <= {"failed", "skipped"}:
            aggregate = "failed"
        else:
            aggregate = "partial"
        return {
            "total": total,
            "aggregate_status": aggregate,
            "items": normalized_items,
        }

    @classmethod
    def _quota_counter(
        cls,
        raw: Any,
        field_name: str,
        *,
        default_name: str,
    ) -> dict:
        if not isinstance(raw, dict):
            raise OzonProductImportProtocolError(
                f"Ozon {field_name} must be an object"
            )
        name = default_name
        for name_key in ("operation", "operation_type", "type", "name"):
            if raw.get(name_key) not in (None, ""):
                name = cls._provider_text(
                    raw[name_key],
                    f"{field_name}.{name_key}",
                    maximum=200,
                ).lower()
                break
        limit = cls._provider_integer(
            raw.get("limit"),
            f"{field_name}.limit",
        )
        usage_value = raw.get("usage", raw.get("used"))
        usage = cls._provider_integer(
            usage_value,
            f"{field_name}.usage",
        )
        if usage > limit:
            raise OzonProductImportProtocolError(
                f"Ozon {field_name}.usage exceeds limit"
            )
        remaining_value = raw.get("remaining", raw.get("available"))
        if remaining_value is None:
            remaining = limit - usage
        else:
            remaining = cls._provider_integer(
                remaining_value,
                f"{field_name}.remaining",
            )
            if remaining > limit or remaining > limit - usage:
                raise OzonProductImportProtocolError(
                    f"Ozon {field_name}.remaining is inconsistent"
                )
        reset_at = raw.get("reset_at")
        if reset_at not in (None, ""):
            reset_at = cls._provider_text(
                reset_at,
                f"{field_name}.reset_at",
                maximum=100,
            )
        else:
            reset_at = None
        return {
            "name": name,
            "limit": limit,
            "usage": usage,
            "remaining": remaining,
            "reset_at": reset_at,
        }

    @staticmethod
    def _quota_name_relevant(name: str, mode: str) -> bool:
        normalized = re.sub(r"[^a-zа-я0-9]+", "_", name.lower()).strip("_")
        global_markers = {"all", "total", "global", "product", "products"}
        if normalized in global_markers:
            return True
        tokens = set(normalized.split("_"))
        has_create = bool(tokens & {"create", "creation", "создание"})
        has_update = bool(tokens & {"update", "updating", "обновление"})
        if has_create or has_update:
            return has_create if mode == "create" else has_update
        if tokens & {"import", "product", "products", "товар", "товары"}:
            return True
        return False

    @classmethod
    def normalize_quota(
        cls,
        response: Any,
        *,
        mode: str = "create",
    ) -> dict:
        """Normalize current or last-compatible quota counters fail-closed.

        Ozon added ``operation_limits`` on 2026-06-09 while retaining legacy
        daily/total counters during the transition.  Unknown counter shapes are
        rejected; we never infer a positive quota from an untyped value.
        """
        if mode not in {"create", "update"}:
            raise OzonProductImportProtocolError("Unknown product quota mode")
        if not isinstance(response, dict):
            raise OzonProductImportProtocolError(
                "Ozon product limit response must be an object"
            )

        entries: List[dict] = []
        source = "operation_limits"
        if "operation_limits" in response:
            raw_limits = response["operation_limits"]
            if isinstance(raw_limits, list):
                if not raw_limits or len(raw_limits) > 100:
                    raise OzonProductImportProtocolError(
                        "Ozon operation_limits must be a bounded non-empty list"
                    )
                for index, raw in enumerate(raw_limits):
                    entries.append(cls._quota_counter(
                        raw,
                        f"operation_limits[{index}]",
                        default_name=f"operation_{index}",
                    ))
            elif isinstance(raw_limits, dict):
                if "limit" in raw_limits:
                    entries.append(cls._quota_counter(
                        raw_limits,
                        "operation_limits",
                        default_name="product_import",
                    ))
                else:
                    if not raw_limits or len(raw_limits) > 100:
                        raise OzonProductImportProtocolError(
                            "Ozon operation_limits object is empty or too large"
                        )
                    for raw_name, raw in raw_limits.items():
                        name = cls._provider_text(
                            raw_name,
                            "operation_limits.key",
                            maximum=200,
                        ).lower()
                        entries.append(cls._quota_counter(
                            raw,
                            f"operation_limits.{name}",
                            default_name=name,
                        ))
            else:
                raise OzonProductImportProtocolError(
                    "Ozon operation_limits has an unsupported type"
                )
            relevant = [
                entry for entry in entries
                if cls._quota_name_relevant(entry["name"], mode)
            ]
            if len(entries) == 1:
                relevant = entries
            if not relevant:
                raise OzonProductImportProtocolError(
                    "Ozon operation_limits has no applicable product counter"
                )
            entries = relevant
        else:
            source = "legacy_daily_counters"
            legacy_names = [f"daily_{mode}", "total"]
            for name in legacy_names:
                if name in response:
                    entries.append(cls._quota_counter(
                        response[name],
                        name,
                        default_name=name,
                    ))
            if not entries:
                raise OzonProductImportProtocolError(
                    "Ozon product limit response contains no recognized counters"
                )

        remaining = min(entry["remaining"] for entry in entries)
        return {
            "contract_version": cls.QUOTA_CONTRACT_VERSION,
            "source": source,
            "mode": mode,
            "remaining": remaining,
            "entries": entries,
        }
