"""Exact, bounded Ozon product state used by full update/rollback workflows.

The Seller API product import endpoint is replace-style: updating a product
requires the complete item.  This module reads the independent catalog,
attribute, price and picture projections, proves that all responses describe
one requested identity, and rebuilds a whitelist-only import payload.  Raw
provider responses are never persisted by callers.
"""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence

from services.marketplace_adapters.types import MarketplaceCredentials
from services.marketplace_listings import (
    MarketplaceCatalogProtocolError,
    MarketplaceListingService,
)
from services.ozon_product_import import (
    OzonProductImportContract,
    OzonProductImportPayloadError,
)


class OzonProductStateError(RuntimeError):
    code = "ozon_product_state_error"


class OzonProductStateProtocolError(OzonProductStateError):
    code = "ozon_product_state_protocol_error"


class OzonProductStateUnavailable(OzonProductStateError):
    code = "ozon_product_state_not_reconstructable"


class OzonProductStateContract:
    """Read and canonicalize exactly one complete Ozon import item."""

    CONTRACT_VERSION = "product-full-state@2026-07-25"
    PICTURES_CONTRACT_VERSION = "product-pictures-info-v2@2026-07-10"
    MAX_URLS = 30
    MAX_COLOR_URLS = 1
    ARCHIVE_CONTRACT_VERSION = "product-archive-v1@2026-07-15"

    _DIMENSION_UNITS = {
        "MM": "mm",
        "MILLIMETER": "mm",
        "MILLIMETERS": "mm",
        "CM": "cm",
        "CENTIMETER": "cm",
        "CENTIMETERS": "cm",
        "IN": "in",
        "INCH": "in",
        "INCHES": "in",
    }
    _WEIGHT_UNITS = {
        "G": "g",
        "GRAM": "g",
        "GRAMS": "g",
        "KG": "kg",
        "KILOGRAM": "kg",
        "KILOGRAMS": "kg",
        "LB": "lb",
        "POUND": "lb",
        "POUNDS": "lb",
    }
    _DRAFT_DIMENSION_UNITS = {
        "mm": "MILLIMETERS",
        "cm": "CENTIMETERS",
        "in": "INCHES",
    }
    _DRAFT_WEIGHT_UNITS = {
        "g": "GRAMS",
        "kg": "KILOGRAMS",
        "lb": "POUNDS",
    }

    @staticmethod
    def _text(
        value: Any,
        field_name: str,
        *,
        maximum: int,
        allow_empty: bool = False,
        allow_newlines: bool = False,
    ) -> str:
        if not isinstance(value, str):
            raise OzonProductStateProtocolError(
                f"Ozon {field_name} must be a string"
            )
        normalized = value.strip()
        if not normalized and not allow_empty:
            raise OzonProductStateProtocolError(
                f"Ozon {field_name} must be non-empty"
            )
        allowed_controls = {9, 10, 13} if allow_newlines else set()
        if len(normalized) > maximum or any(
            (
                ord(character) < 32
                and ord(character) not in allowed_controls
            )
            or ord(character) == 127
            for character in normalized
        ):
            raise OzonProductStateProtocolError(
                f"Ozon {field_name} is outside the supported contract"
            )
        return normalized

    @classmethod
    def _id(cls, value: Any, field_name: str) -> str:
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return str(value)
        text = cls._text(value, field_name, maximum=100)
        if not text.isascii() or not text.isdigit() or text.startswith("0"):
            raise OzonProductStateProtocolError(
                f"Ozon {field_name} must be a canonical positive integer ID"
            )
        return text

    @classmethod
    def _api_id(cls, value: Any, field_name: str) -> int:
        parsed = int(cls._id(value, field_name))
        if parsed > 9_223_372_036_854_775_807:
            raise OzonProductStateProtocolError(
                f"Ozon {field_name} is out of range"
            )
        return parsed

    @classmethod
    def _urls(
        cls,
        value: Any,
        field_name: str,
        *,
        maximum: int,
    ) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list) or len(value) > maximum:
            raise OzonProductStateProtocolError(
                f"Ozon {field_name} must be a bounded URL list"
            )
        result = []
        seen = set()
        for index, raw in enumerate(value):
            url = cls._text(
                raw,
                f"{field_name}[{index}]",
                maximum=2_000,
            )
            if url in seen:
                raise OzonProductStateProtocolError(
                    f"Ozon {field_name} contains duplicate URLs"
                )
            seen.add(url)
            result.append(url)
        return result

    @classmethod
    def normalize_pictures(
        cls,
        response: Any,
        *,
        expected_product_ids: Sequence[str],
    ) -> Dict[str, dict]:
        expected = [cls._id(value, "expected_product_id") for value in expected_product_ids]
        if not expected or len(expected) > 1_000 or len(set(expected)) != len(expected):
            raise OzonProductStateProtocolError(
                "Expected picture product IDs must be a unique non-empty set"
            )
        if not isinstance(response, dict) or not isinstance(response.get("items"), list):
            raise OzonProductStateProtocolError(
                "Ozon product pictures response has no items list"
            )
        raw_items = response["items"]
        if len(raw_items) > 1_000:
            raise OzonProductStateProtocolError(
                "Ozon product pictures response exceeds batch limit"
            )
        result: Dict[str, dict] = {}
        for index, raw in enumerate(raw_items):
            if not isinstance(raw, dict):
                raise OzonProductStateProtocolError(
                    f"Ozon pictures.items[{index}] must be an object"
                )
            product_id = cls._id(
                raw.get("product_id"),
                f"pictures.items[{index}].product_id",
            )
            if product_id in result:
                raise OzonProductStateProtocolError(
                    "Ozon product pictures response repeats product_id"
                )
            primary = cls._urls(
                raw.get("primary_photo"),
                f"pictures.items[{index}].primary_photo",
                maximum=1,
            )
            photos = cls._urls(
                raw.get("photo"),
                f"pictures.items[{index}].photo",
                maximum=cls.MAX_URLS,
            )
            colors = cls._urls(
                raw.get("color_photo"),
                f"pictures.items[{index}].color_photo",
                maximum=cls.MAX_COLOR_URLS,
            )
            legacy_360 = raw.get("photo_360")
            if legacy_360 not in (None, []):
                raise OzonProductStateUnavailable(
                    "Existing images360 cannot be preserved by the current Ozon write contract"
                )
            errors = raw.get("errors", [])
            if not isinstance(errors, list) or len(errors) > 200:
                raise OzonProductStateProtocolError(
                    "Ozon product picture errors must be a bounded list"
                )
            if errors:
                raise OzonProductStateUnavailable(
                    "Ozon reports unresolved image errors for the current product"
                )
            primary_url = primary[0] if primary else None
            if primary_url and primary_url in photos:
                photos = [url for url in photos if url != primary_url]
            combined = ([primary_url] if primary_url else []) + photos
            if not combined or len(combined) > cls.MAX_URLS:
                raise OzonProductStateUnavailable(
                    "Current Ozon product has no reconstructable main image set"
                )
            if len(set(combined)) != len(combined):
                raise OzonProductStateProtocolError(
                    "Ozon product pictures contain duplicate main images"
                )
            media = {"images": photos}
            if primary_url:
                media["primary_image"] = primary_url
            if colors:
                if colors[0] in set(combined):
                    raise OzonProductStateUnavailable(
                        "Ozon color image duplicates the main image set"
                    )
                media["color_image"] = colors[0]
            result[product_id] = media
        if set(result) != set(expected):
            raise OzonProductStateProtocolError(
                "Ozon product pictures response does not match the requested exact set"
            )
        return result

    @classmethod
    def canonical_media(cls, item: Mapping[str, Any]) -> dict:
        images = cls._urls(
            item.get("images"),
            "import.items.images",
            maximum=cls.MAX_URLS,
        )
        primary = item.get("primary_image")
        if primary not in (None, ""):
            primary = cls._text(
                primary,
                "import.items.primary_image",
                maximum=2_000,
            )
            if primary in images:
                raise OzonProductStateProtocolError(
                    "Ozon primary_image duplicates images"
                )
        elif images:
            primary, images = images[0], images[1:]
        else:
            raise OzonProductStateProtocolError(
                "Ozon import item has no main images"
            )
        result = {"primary_image": primary, "images": images}
        color = item.get("color_image")
        if color not in (None, ""):
            color = cls._text(color, "import.items.color_image", maximum=2_000)
            if color == primary or color in images:
                raise OzonProductStateProtocolError(
                    "Ozon color_image duplicates a main image"
                )
            result["color_image"] = color
        return result

    @classmethod
    def canonical_payload(cls, payload: Any) -> dict:
        """Canonical comparison form for one already validated full payload."""
        if (
            not isinstance(payload, dict)
            or set(payload) != {"items"}
            or not isinstance(payload.get("items"), list)
            or len(payload["items"]) != 1
            or not isinstance(payload["items"][0], dict)
        ):
            raise OzonProductStateProtocolError(
                "Full Ozon payload must contain exactly one item"
            )
        item = deepcopy(payload["items"][0])
        media = cls.canonical_media(item)
        item.pop("primary_image", None)
        item.pop("color_image", None)
        item["images"] = media.pop("images")
        item["primary_image"] = media.pop("primary_image")
        if media:
            item.update(media)

        attributes = item.get("attributes")
        if not isinstance(attributes, list):
            raise OzonProductStateProtocolError(
                "Full Ozon payload attributes must be a list"
            )
        item["attributes"] = sorted(
            attributes,
            key=lambda value: (
                value.get("complex_id", 0) if isinstance(value, dict) else -1,
                value.get("id", 0) if isinstance(value, dict) else -1,
            ),
        )
        groups = item.get("complex_attributes")
        if not isinstance(groups, list):
            raise OzonProductStateProtocolError(
                "Full Ozon payload complex_attributes must be a list"
            )
        normalized_groups = []
        for group in groups:
            if not isinstance(group, dict) or set(group) != {"attributes"}:
                raise OzonProductStateProtocolError(
                    "Full Ozon payload contains a malformed complex group"
                )
            group_attributes = group["attributes"]
            if not isinstance(group_attributes, list):
                raise OzonProductStateProtocolError(
                    "Full Ozon payload complex group attributes must be a list"
                )
            normalized_groups.append({
                "attributes": sorted(
                    group_attributes,
                    key=lambda value: (
                        value.get("complex_id", 0) if isinstance(value, dict) else -1,
                        value.get("id", 0) if isinstance(value, dict) else -1,
                    ),
                )
            })
        item["complex_attributes"] = normalized_groups
        result = {"items": [item]}
        OzonProductImportContract.canonical_json(
            result,
            maximum=OzonProductImportContract.MAX_BODY_BYTES,
        )
        return result

    @classmethod
    def fingerprint(cls, payload: Any) -> str:
        return OzonProductImportContract.fingerprint(
            cls.canonical_payload(payload)
        )

    @classmethod
    def provider_roundtrip_omissions(
        cls,
        submitted_payload: Any,
        live_payload: Any,
        *,
        omittable_simple_attribute_ids: Iterable[Any],
    ) -> Optional[tuple[str, ...]]:
        """Prove exact provider equality modulo allowlisted missing attributes.

        Some Ozon import-only simple attributes are accepted by
        ``/v3/product/import`` but are not returned by the product attributes
        read model.  Callers must independently prove which exact IDs are safe
        to treat this way.  This contract then permits only a complete omission
        from live state: a changed value, duplicate, complex occurrence, or any
        other payload difference remains non-equivalent.

        ``()`` means exact equality, a non-empty tuple lists the only omitted
        IDs, and ``None`` means the states are different.
        """
        adjustments = cls.provider_roundtrip_adjustments(
            submitted_payload,
            live_payload,
            omittable_simple_attribute_ids=(
                omittable_simple_attribute_ids
            ),
            offer_id_canonicalized_simple_attribute_ids=(),
        )
        if adjustments is None:
            return None
        return adjustments["omitted_attribute_ids"]

    @classmethod
    def provider_roundtrip_adjustments(
        cls,
        submitted_payload: Any,
        live_payload: Any,
        *,
        omittable_simple_attribute_ids: Iterable[Any],
        offer_id_canonicalized_simple_attribute_ids: Iterable[Any],
    ) -> Optional[dict]:
        """Prove exact equality modulo two narrow provider transformations.

        In addition to an allowlisted complete omission, current Ozon may
        canonicalize a provider-owned simple attribute to the item's exact
        ``offer_id``.  That transformation is accepted only when both payloads
        contain one plain-string occurrence of the allowlisted attribute, the
        live value equals the unchanged top-level ``offer_id``, and every
        other byte of canonical state compares exactly.
        """
        submitted = cls.canonical_payload(submitted_payload)
        live = cls.canonical_payload(live_payload)
        empty = {
            "omitted_attribute_ids": (),
            "offer_id_canonicalized_attribute_ids": (),
        }
        if submitted == live:
            return empty

        omitted_allowed: set[int] = set()
        for index, value in enumerate(omittable_simple_attribute_ids):
            omitted_allowed.add(cls._api_id(
                value,
                f"omittable_simple_attribute_ids[{index}]",
            ))
        canonicalized_allowed: set[int] = set()
        for index, value in enumerate(
            offer_id_canonicalized_simple_attribute_ids
        ):
            canonicalized_allowed.add(cls._api_id(
                value,
                (
                    "offer_id_canonicalized_simple_attribute_ids"
                    f"[{index}]"
                ),
            ))
        if not omitted_allowed and not canonicalized_allowed:
            return None

        submitted_item = submitted["items"][0]
        live_item = live["items"][0]
        submitted_attributes = submitted_item["attributes"]
        live_attributes = live_item["attributes"]
        canonicalized: list[int] = []

        if submitted_item.get("offer_id") == live_item.get("offer_id"):
            for attribute_id in sorted(canonicalized_allowed):
                submitted_matches = [
                    attribute
                    for attribute in submitted_attributes
                    if (
                        isinstance(attribute, dict)
                        and attribute.get("id") == attribute_id
                    )
                ]
                live_matches = [
                    attribute
                    for attribute in live_attributes
                    if (
                        isinstance(attribute, dict)
                        and attribute.get("id") == attribute_id
                    )
                ]
                if len(submitted_matches) != 1 or len(live_matches) != 1:
                    continue
                submitted_attribute = submitted_matches[0]
                live_attribute = live_matches[0]
                submitted_values = submitted_attribute.get("values")
                live_values = live_attribute.get("values")
                if (
                    submitted_attribute.get("complex_id") != 0
                    or live_attribute.get("complex_id") != 0
                    or not isinstance(submitted_values, list)
                    or not isinstance(live_values, list)
                    or len(submitted_values) != 1
                    or len(live_values) != 1
                    or not isinstance(submitted_values[0], dict)
                    or not isinstance(live_values[0], dict)
                    or set(submitted_values[0]) != {"value"}
                    or set(live_values[0]) != {"value"}
                    or live_values[0]["value"]
                    != live_item.get("offer_id")
                    or submitted_values == live_values
                ):
                    continue
                submitted_attribute["values"] = deepcopy(live_values)
                canonicalized.append(attribute_id)

        omitted: list[int] = []
        for attribute_id in sorted(omitted_allowed):
            submitted_matches = [
                attribute
                for attribute in submitted_attributes
                if (
                    isinstance(attribute, dict)
                    and attribute.get("id") == attribute_id
                )
            ]
            live_matches = [
                attribute
                for attribute in live_attributes
                if (
                    isinstance(attribute, dict)
                    and attribute.get("id") == attribute_id
                )
            ]
            if live_matches:
                # A returned value must compare exactly; it may never be
                # discarded merely because its ID is on the omission allowlist.
                continue
            if (
                len(submitted_matches) != 1
                or submitted_matches[0].get("complex_id") != 0
            ):
                continue
            submitted_attributes.remove(submitted_matches[0])
            omitted.append(attribute_id)

        if (not omitted and not canonicalized) or submitted != live:
            return None
        return {
            "omitted_attribute_ids": tuple(
                str(attribute_id) for attribute_id in omitted
            ),
            "offer_id_canonicalized_attribute_ids": tuple(
                str(attribute_id) for attribute_id in canonicalized
            ),
        }

    @classmethod
    def archive_payload(cls, product_id: Any) -> dict:
        return {"product_id": [cls._api_id(product_id, "product_id")]}

    @classmethod
    def normalize_simple_result(cls, response: Any, *, endpoint: str) -> bool:
        if not isinstance(response, dict) or not isinstance(response.get("result"), bool):
            raise OzonProductStateProtocolError(
                f"Ozon {endpoint} response must contain a boolean result"
            )
        return response["result"]

    @classmethod
    def _attribute(
        cls,
        raw: Mapping[str, Any],
        field_name: str,
    ) -> Optional[dict]:
        if not isinstance(raw, dict):
            raise OzonProductStateProtocolError(
                f"Ozon {field_name} must be an object"
            )
        result = {
            "id": cls._api_id(raw.get("id"), f"{field_name}.id"),
            "complex_id": (
                cls._api_id(raw["complex_id"], f"{field_name}.complex_id")
                if raw.get("complex_id") not in (None, "", "0", 0)
                else 0
            ),
        }
        raw_values = raw.get("values")
        if not isinstance(raw_values, list) or len(raw_values) > 500:
            raise OzonProductStateUnavailable(
                f"Ozon {field_name} has malformed values"
            )
        # Ozon attributes v4 may expose optional empty placeholders.  They have
        # no import semantics and are safely equivalent to an absent attribute.
        if not raw_values:
            return None
        values = []
        for index, raw_value in enumerate(raw_values):
            if not isinstance(raw_value, dict):
                raise OzonProductStateProtocolError(
                    f"Ozon {field_name}.values[{index}] must be an object"
                )
            value = cls._text(
                raw_value.get("value"),
                f"{field_name}.values[{index}].value",
                maximum=100_000,
                allow_newlines=True,
            )
            normalized_value = {"value": value}
            if raw_value.get("dictionary_value_id") not in (None, "", "0", 0):
                normalized_value["dictionary_value_id"] = cls._api_id(
                    raw_value["dictionary_value_id"],
                    f"{field_name}.values[{index}].dictionary_value_id",
                )
            values.append(normalized_value)
        result["values"] = values
        return result

    @classmethod
    def _unit(cls, value: Any, field_name: str, mapping: Mapping[str, str]) -> str:
        normalized = cls._text(value, field_name, maximum=30).upper()
        result = mapping.get(normalized)
        if result is None:
            raise OzonProductStateUnavailable(
                f"Ozon {field_name} uses an unsupported unit"
            )
        return result

    @classmethod
    def _positive_integer(cls, value: Any, field_name: str) -> int:
        try:
            return OzonProductImportContract._positive_api_integer(value, field_name)
        except OzonProductImportPayloadError as exc:
            raise OzonProductStateUnavailable(str(exc)) from None

    @classmethod
    def _commercial_item(cls, summary: Mapping[str, Any]) -> dict:
        values = summary.get("values")
        if not isinstance(values, dict):
            raise OzonProductStateUnavailable(
                "Current Ozon base price is unavailable"
            )
        try:
            price = OzonProductImportContract._price(
                values.get("price"),
                "current_price.price",
            )
        except OzonProductImportPayloadError as exc:
            raise OzonProductStateUnavailable(str(exc)) from None
        currency = values.get("currency_code") or summary.get("currency")
        if currency != "RUB":
            raise OzonProductStateUnavailable(
                "Current full-state update supports Ozon RUB accounts only"
            )
        vat = OzonProductImportContract.VAT_CANONICAL.get(str(values.get("vat")))
        if vat is None:
            raise OzonProductStateUnavailable(
                "Current Ozon VAT cannot be reconstructed exactly"
            )
        result = {"price": price, "currency_code": "RUB", "vat": vat}
        old_price = values.get("old_price")
        if old_price not in (None, "", 0, "0"):
            try:
                normalized_old = OzonProductImportContract._price(
                    old_price,
                    "current_price.old_price",
                )
            except OzonProductImportPayloadError as exc:
                raise OzonProductStateUnavailable(str(exc)) from None
            if Decimal(normalized_old) > Decimal(price):
                result["old_price"] = normalized_old
        return result

    @classmethod
    def _projection_media(cls, value: Any) -> dict:
        """Normalize the catalog projection to pictures-info wire semantics.

        Product info/attributes can repeat ``primary_image`` inside ``images``;
        pictures-info reports the same gallery as a separate primary slot.
        Removing that exact duplicate is therefore a representation
        normalization, not a lossy media edit.
        """
        if not isinstance(value, Mapping):
            raise OzonProductStateUnavailable(
                "Current Ozon media projection is unavailable"
            )
        images = cls._urls(
            value.get("images"),
            "listing.media.images",
            maximum=cls.MAX_URLS,
        )
        primary = value.get("primary_image")
        if primary not in (None, ""):
            primary = cls._text(
                primary,
                "listing.media.primary_image",
                maximum=2_000,
            )
            images = [url for url in images if url != primary]
        elif images:
            primary, images = images[0], images[1:]
        else:
            raise OzonProductStateUnavailable(
                "Current Ozon listing has no reconstructable main image set"
            )
        result = {"primary_image": primary, "images": images}
        color = value.get("color_image")
        if color not in (None, ""):
            color = cls._text(
                color,
                "listing.media.color_image",
                maximum=2_000,
            )
            if color == primary or color in images:
                raise OzonProductStateUnavailable(
                    "Current Ozon color image duplicates the main image set"
                )
            result["color_image"] = color
        if value.get("images360") not in (None, []):
            raise OzonProductStateUnavailable(
                "Existing images360 cannot be preserved by the current Ozon write contract"
            )
        return result

    @classmethod
    def _projection_attributes(
        cls,
        value: Any,
        field_name: str,
        *,
        complex_groups: bool = False,
    ) -> list:
        if not isinstance(value, list):
            raise OzonProductStateUnavailable(
                f"Current Ozon {field_name} projection is unavailable"
            )
        if not complex_groups:
            result = []
            for index, raw in enumerate(value):
                if not isinstance(raw, Mapping):
                    raise OzonProductStateProtocolError(
                        f"Ozon {field_name}[{index}] must be an object"
                    )
                normalized = dict(raw)
                # Publication-created rows before this contract stored the
                # draft spelling.  Accept that exact local legacy shape until
                # the next catalog sweep rewrites it to provider projection.
                if "id" not in normalized and "attribute_id" in normalized:
                    normalized["id"] = normalized.pop("attribute_id")
                parsed = cls._attribute(
                    normalized,
                    f"{field_name}[{index}]",
                )
                if parsed is not None:
                    result.append(parsed)
            return result
        result = []
        for group_index, raw_group in enumerate(value):
            if (
                not isinstance(raw_group, Mapping)
                or set(raw_group) != {"attributes"}
                or not isinstance(raw_group.get("attributes"), list)
            ):
                raise OzonProductStateProtocolError(
                    "Ozon complex attribute projection is malformed"
                )
            # Attributes v4 may return an empty ``{}`` complex block after
            # normalization.  It carries no write semantics and is equivalent
            # to an absent group.
            if not raw_group["attributes"]:
                continue
            parsed_attributes = []
            for index, raw in enumerate(raw_group["attributes"]):
                normalized = raw
                if (
                    isinstance(raw, Mapping)
                    and "id" not in raw
                    and "attribute_id" in raw
                ):
                    normalized = {
                        **raw,
                        "id": raw.get("attribute_id"),
                    }
                parsed = cls._attribute(
                    normalized,
                    f"{field_name}[{group_index}].attributes[{index}]",
                )
                if parsed is not None:
                    parsed_attributes.append(parsed)
            if parsed_attributes:
                result.append({"attributes": parsed_attributes})
        return result

    @classmethod
    def from_listing_projection(cls, projection: Mapping[str, Any]) -> dict:
        """Rebuild a full import item from one exact normalized catalog row.

        This method is deliberately ORM-free.  The caller proves tenant scope
        and freshness, then passes only the whitelist-normalized fields stored
        by ``MarketplaceListingService``.  The returned fingerprint is used as
        an optimistic before-state gate; the publication worker still performs
        an independent exact live read immediately before any write.
        """
        if not isinstance(projection, Mapping):
            raise OzonProductStateProtocolError(
                "Ozon listing projection must be an object"
            )
        product_id = cls._id(
            projection.get("product_id"),
            "listing.product_id",
        )
        offer_id = cls._text(
            projection.get("offer_id"),
            "listing.offer_id",
            maximum=200,
        )
        attributes = cls._projection_attributes(
            projection.get("attributes"),
            "listing.attributes",
        )
        complex_attributes = cls._projection_attributes(
            projection.get("complex_attributes"),
            "listing.complex_attributes",
            complex_groups=True,
        )
        dimensions = projection.get("dimensions")
        if not isinstance(dimensions, Mapping):
            raise OzonProductStateUnavailable(
                "Current Ozon physical dimensions are unavailable"
            )
        media = cls._projection_media(projection.get("media"))
        barcodes = projection.get("barcodes")
        if not isinstance(barcodes, list) or len(barcodes) > 1:
            raise OzonProductStateUnavailable(
                "Current Ozon barcode set cannot be restored through /v3/product/import"
            )

        item = {
            "attributes": attributes,
            "complex_attributes": complex_attributes,
            "currency_code": "RUB",
            "depth": cls._positive_integer(
                dimensions.get("depth"),
                "listing.dimensions.depth",
            ),
            "description_category_id": cls._api_id(
                projection.get("category_id"),
                "listing.description_category_id",
            ),
            "dimension_unit": cls._unit(
                dimensions.get("dimension_unit"),
                "listing.dimensions.dimension_unit",
                cls._DIMENSION_UNITS,
            ),
            "height": cls._positive_integer(
                dimensions.get("height"),
                "listing.dimensions.height",
            ),
            "images": list(media["images"]),
            "name": cls._text(
                projection.get("title"),
                "listing.name",
                maximum=500,
            ),
            "offer_id": offer_id,
            "primary_image": media["primary_image"],
            "type_id": cls._api_id(
                projection.get("type_id"),
                "listing.type_id",
            ),
            "weight": cls._positive_integer(
                dimensions.get("weight"),
                "listing.dimensions.weight",
            ),
            "weight_unit": cls._unit(
                dimensions.get("weight_unit"),
                "listing.dimensions.weight_unit",
                cls._WEIGHT_UNITS,
            ),
            "width": cls._positive_integer(
                dimensions.get("width"),
                "listing.dimensions.width",
            ),
        }
        price_summary = projection.get("price") or {}
        if (
            isinstance(price_summary, Mapping)
            and not isinstance(price_summary.get("values"), Mapping)
            and price_summary.get("price") not in (None, "")
        ):
            price_summary = {
                "currency": price_summary.get("currency_code"),
                "values": dict(price_summary),
            }
        item.update(cls._commercial_item(price_summary))
        if media.get("color_image"):
            item["color_image"] = media["color_image"]
        if barcodes:
            item["barcode"] = cls._text(
                barcodes[0],
                "listing.barcode",
                maximum=100,
            )
        payload = {"items": [item]}
        canonical_payload = cls.canonical_payload(payload)
        return {
            "identity": {
                "product_id": product_id,
                "offer_id": offer_id,
            },
            "payload": payload,
            "canonical_payload": canonical_payload,
            "fingerprint": OzonProductImportContract.fingerprint(
                canonical_payload
            ),
            "media": media,
        }

    @classmethod
    def draft_documents(cls, payload: Mapping[str, Any]) -> dict:
        """Convert one validated full import payload to normalized draft blocks."""
        canonical = cls.canonical_payload(payload)
        item = canonical["items"][0]

        def draft_attribute(raw: Mapping[str, Any]) -> dict:
            return {
                "attribute_id": str(raw["id"]),
                "complex_id": str(raw.get("complex_id") or 0),
                "values": [
                    {
                        key: (
                            str(value)
                            if key == "dictionary_value_id"
                            else value
                        )
                        for key, value in raw_value.items()
                    }
                    for raw_value in raw["values"]
                ],
            }

        attributes = [
            draft_attribute(raw)
            for raw in item["attributes"]
        ]
        description = None
        for attribute in attributes:
            if (
                attribute["attribute_id"]
                == OzonProductImportContract.DESCRIPTION_ATTRIBUTE_ID
                and attribute["complex_id"] == "0"
                and len(attribute["values"]) == 1
                and set(attribute["values"][0]) == {"value"}
            ):
                description = attribute["values"][0]["value"]
                break
        complex_attributes = [{
            "attributes": [
                draft_attribute(raw)
                for raw in group["attributes"]
            ]
        } for group in item["complex_attributes"]]
        media = {
            "primary_image": item["primary_image"],
            "images": list(item["images"]),
        }
        if item.get("color_image"):
            media["color_image"] = item["color_image"]
        commercial = {
            "price": item["price"],
            "vat": item["vat"],
            "currency_code": item["currency_code"],
        }
        if item.get("old_price"):
            commercial["old_price"] = item["old_price"]
        return {
            "content": {
                "name": item["name"],
                "description": description or "",
            },
            "attributes": attributes,
            "complex_attributes": complex_attributes,
            "media": media,
            "dimensions": {
                "width": str(item["width"]),
                "height": str(item["height"]),
                "depth": str(item["depth"]),
                "weight": str(item["weight"]),
                "dimension_unit": cls._DRAFT_DIMENSION_UNITS[
                    item["dimension_unit"]
                ],
                "weight_unit": cls._DRAFT_WEIGHT_UNITS[
                    item["weight_unit"]
                ],
            },
            "barcodes": (
                [item["barcode"]]
                if item.get("barcode")
                else []
            ),
            "commercial": commercial,
        }

    @classmethod
    def _exact_page_item(
        cls,
        page: Mapping[str, Any],
        *,
        product_id: str,
        endpoint: str,
    ) -> dict:
        # Ozon may return an opaque next cursor even when an exact product
        # filter already returned its declared total on this one-item page.
        # Exact identity + total=1 proves completeness; cursor emptiness does
        # not, and requiring it breaks current /v5/product/info/prices reads.
        if page.get("total") != 1:
            raise OzonProductStateProtocolError(
                f"Ozon {endpoint} did not return one complete exact-set page"
            )
        items = page.get("items")
        if not isinstance(items, dict) or set(items) != {product_id}:
            raise OzonProductStateProtocolError(
                f"Ozon {endpoint} response does not match the requested product"
            )
        return items[product_id]

    @classmethod
    def read_full_payload(
        cls,
        *,
        adapter,
        credentials: MarketplaceCredentials,
        product_id: Any,
        offer_id: str,
    ) -> dict:
        """Read a complete replace-style payload for one exact live product."""
        canonical_product_id = cls._id(product_id, "product_id")
        api_product_id = cls._api_id(product_id, "product_id")
        canonical_offer_id = cls._text(offer_id, "offer_id", maximum=200)
        try:
            info_items = MarketplaceListingService.normalize_product_info(
                adapter.get_products(
                    credentials,
                    {"product_id": [api_product_id]},
                )
            )
            attributes_page = MarketplaceListingService.normalize_product_attributes_page(
                adapter.get_product_attributes(credentials, {
                    "filter": {
                        "product_id": [api_product_id],
                        "visibility": "ALL",
                    },
                    "last_id": "",
                    "limit": 1,
                })
            )
            prices_page = MarketplaceListingService.normalize_prices_page(
                adapter.read_prices(credentials, {
                    "filter": {
                        "product_id": [api_product_id],
                        "visibility": "ALL",
                    },
                    "cursor": "",
                    "limit": 1,
                })
            )
        except MarketplaceCatalogProtocolError as exc:
            raise OzonProductStateProtocolError(str(exc)) from exc
        if set(info_items) != {canonical_product_id}:
            raise OzonProductStateProtocolError(
                "Ozon product info does not match the requested exact set"
            )
        info = info_items[canonical_product_id]
        attributes = cls._exact_page_item(
            attributes_page,
            product_id=canonical_product_id,
            endpoint="product attributes",
        )
        price = cls._exact_page_item(
            prices_page,
            product_id=canonical_product_id,
            endpoint="product prices",
        )
        pictures = cls.normalize_pictures(
            adapter.get_product_pictures(
                credentials,
                {"product_id": [api_product_id]},
            ),
            expected_product_ids=[canonical_product_id],
        )[canonical_product_id]
        for endpoint, item in (
            ("product info", info),
            ("product attributes", attributes),
            ("product prices", price),
        ):
            returned_offer = item.get("offer_id")
            if returned_offer not in (None, canonical_offer_id):
                raise OzonProductStateProtocolError(
                    f"Ozon {endpoint} returned a conflicting offer_id"
                )
        if info.get("archived") is True:
            raise OzonProductStateUnavailable(
                "Archived Ozon products must be restored before full update"
            )

        raw_attributes = attributes.get("attributes")
        raw_complex = attributes.get("complex_attributes")
        if not isinstance(raw_attributes, list) or not isinstance(raw_complex, list):
            raise OzonProductStateProtocolError(
                "Ozon product attributes are incomplete"
            )
        normalized_attributes = []
        for index, raw in enumerate(raw_attributes):
            parsed = cls._attribute(raw, f"attributes[{index}]")
            if parsed is not None:
                normalized_attributes.append(parsed)
        normalized_complex = []
        for group_index, raw_group in enumerate(raw_complex):
            if not isinstance(raw_group, dict) or set(raw_group) != {"attributes"}:
                raise OzonProductStateProtocolError(
                    "Ozon complex attribute group is malformed"
                )
            group_attributes = raw_group.get("attributes")
            if not isinstance(group_attributes, list):
                raise OzonProductStateProtocolError(
                    "Ozon complex attribute group attributes are malformed"
                )
            if not group_attributes:
                # Current attributes v4 may emit an empty complex placeholder.
                # It has no import semantics and is equivalent to no group.
                continue
            parsed_attributes = []
            for index, raw in enumerate(group_attributes):
                parsed = cls._attribute(
                    raw,
                    f"complex_attributes[{group_index}].attributes[{index}]",
                )
                if parsed is not None:
                    parsed_attributes.append(parsed)
            if parsed_attributes:
                normalized_complex.append(
                    {"attributes": parsed_attributes}
                )

        dimensions = attributes.get("dimensions")
        if not isinstance(dimensions, dict):
            raise OzonProductStateUnavailable(
                "Current Ozon physical dimensions are unavailable"
            )
        barcodes = attributes.get("barcodes") or info.get("barcodes") or []
        if not isinstance(barcodes, list) or len(barcodes) > 1:
            raise OzonProductStateUnavailable(
                "Current Ozon barcode set cannot be restored through /v3/product/import"
            )
        name = attributes.get("title") or info.get("title")
        item = {
            "attributes": normalized_attributes,
            "complex_attributes": normalized_complex,
            "currency_code": "RUB",
            "depth": cls._positive_integer(dimensions.get("depth"), "dimensions.depth"),
            "description_category_id": cls._api_id(
                attributes.get("category_id") or info.get("category_id"),
                "description_category_id",
            ),
            "dimension_unit": cls._unit(
                dimensions.get("dimension_unit"),
                "dimensions.dimension_unit",
                cls._DIMENSION_UNITS,
            ),
            "height": cls._positive_integer(dimensions.get("height"), "dimensions.height"),
            "images": list(pictures.get("images", [])),
            "name": cls._text(name, "name", maximum=500),
            "offer_id": canonical_offer_id,
            "type_id": cls._api_id(
                attributes.get("type_id") or info.get("type_id"),
                "type_id",
            ),
            "weight": cls._positive_integer(dimensions.get("weight"), "dimensions.weight"),
            "weight_unit": cls._unit(
                dimensions.get("weight_unit"),
                "dimensions.weight_unit",
                cls._WEIGHT_UNITS,
            ),
            "width": cls._positive_integer(dimensions.get("width"), "dimensions.width"),
        }
        item.update(cls._commercial_item(price["summary"]))
        if pictures.get("primary_image"):
            item["primary_image"] = pictures["primary_image"]
        if pictures.get("color_image"):
            item["color_image"] = pictures["color_image"]
        if barcodes:
            item["barcode"] = cls._text(barcodes[0], "barcode", maximum=100)
        payload = {"items": [item]}
        canonical_payload = cls.canonical_payload(payload)
        return {
            "identity": {
                "product_id": canonical_product_id,
                "offer_id": canonical_offer_id,
            },
            "payload": payload,
            "canonical_payload": canonical_payload,
            "fingerprint": OzonProductImportContract.fingerprint(canonical_payload),
            "media": cls.canonical_media(item),
        }


__all__ = [
    "OzonProductStateContract",
    "OzonProductStateError",
    "OzonProductStateProtocolError",
    "OzonProductStateUnavailable",
]
