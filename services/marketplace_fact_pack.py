"""Marketplace-neutral, provenance-aware facts for product projections.

Legacy supplier AI output can contain inferred physical and compliance values.
This module deliberately keeps those values in ``unverified_suggestions`` and
never promotes them into the observed fact set used by deterministic draft
mapping.
"""

from __future__ import annotations

from hashlib import sha256
import json
import math
from typing import Any, Dict, Iterable, Optional, Tuple

from models import ImportedProduct, SupplierProduct


class MarketplaceFactPackError(ValueError):
    pass


class MarketplaceFactPackBuilder:
    VERSION = 1
    MAX_SERIALIZED_BYTES = 256 * 1024
    MAX_TEXT = 20_000
    MAX_DESCRIPTION = 100_000
    MAX_ITEMS = 200
    MAX_IMAGES = 100

    _SENSITIVE_KEYS = {
        "api_key",
        "apikey",
        "authorization",
        "client_id",
        "client_secret",
        "credentials",
        "instruction",
        "instructions",
        "password",
        "prompt",
        "secret",
        "token",
    }

    @staticmethod
    def _stable_json(value: Any) -> str:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    @classmethod
    def _load_object(cls, raw_value: Any) -> dict:
        if isinstance(raw_value, dict):
            return raw_value
        if not isinstance(raw_value, str) or not raw_value:
            return {}
        if len(raw_value.encode("utf-8", errors="ignore")) > cls.MAX_SERIALIZED_BYTES:
            return {}
        try:
            value = json.loads(raw_value)
        except (TypeError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    @classmethod
    def _load_list(cls, raw_value: Any) -> list:
        if isinstance(raw_value, list):
            return raw_value
        if not isinstance(raw_value, str) or not raw_value:
            return []
        if len(raw_value.encode("utf-8", errors="ignore")) > cls.MAX_SERIALIZED_BYTES:
            return []
        try:
            value = json.loads(raw_value)
        except (TypeError, ValueError):
            return []
        return value if isinstance(value, list) else []

    @classmethod
    def _text(
        cls,
        value: Any,
        *,
        maximum: Optional[int] = None,
    ) -> Optional[str]:
        if not isinstance(value, str):
            return None
        value = value.strip()
        if not value:
            return None
        if any(
            ord(character) < 32 and character not in "\n\t"
            for character in value
        ) or any(ord(character) == 127 for character in value):
            return None
        return value[: maximum or cls.MAX_TEXT]

    @staticmethod
    def _number(value: Any) -> Optional[float]:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        if not math.isfinite(float(value)):
            return None
        return value

    @classmethod
    def _safe_scalar(cls, value: Any) -> Optional[Any]:
        text = cls._text(value)
        if text is not None:
            return text
        number = cls._number(value)
        if number is not None:
            return number
        if isinstance(value, bool):
            return value
        return None

    @classmethod
    def _safe_value(cls, value: Any, *, depth: int = 0) -> Optional[Any]:
        if depth > 4:
            return None
        scalar = cls._safe_scalar(value)
        if scalar is not None:
            return scalar
        if isinstance(value, list):
            result = []
            for item in value[: cls.MAX_ITEMS]:
                safe = cls._safe_value(item, depth=depth + 1)
                if safe is not None:
                    result.append(safe)
            return result
        if isinstance(value, dict):
            result = {}
            for raw_key in sorted(value, key=lambda item: str(item))[: cls.MAX_ITEMS]:
                key = cls._text(raw_key, maximum=200)
                if not key or key.casefold() in cls._SENSITIVE_KEYS:
                    continue
                safe = cls._safe_value(value[raw_key], depth=depth + 1)
                if safe is not None:
                    result[key] = safe
            return result
        return None

    @classmethod
    def _json_field(cls, raw_value: Any, fallback: Any) -> Any:
        if isinstance(raw_value, type(fallback)):
            return raw_value
        if not isinstance(raw_value, str) or not raw_value:
            return fallback
        if len(raw_value.encode("utf-8", errors="ignore")) > cls.MAX_SERIALIZED_BYTES:
            return fallback
        try:
            value = json.loads(raw_value)
        except (TypeError, ValueError):
            return fallback
        return value if isinstance(value, type(fallback)) else fallback

    @classmethod
    def _characteristics(cls, value: Any) -> list:
        result = []
        if isinstance(value, dict):
            iterator: Iterable[Tuple[Any, Any]] = value.items()
        elif isinstance(value, list):
            normalized = []
            for item in value:
                if not isinstance(item, dict):
                    continue
                normalized.append((
                    item.get("name") or item.get("key") or item.get("title"),
                    item.get("value") if "value" in item else item.get("values"),
                ))
            iterator = normalized
        else:
            return []
        seen = set()
        for raw_name, raw_value in list(iterator)[: cls.MAX_ITEMS]:
            name = cls._text(raw_name, maximum=500)
            safe_value = cls._safe_value(raw_value)
            if not name or safe_value in (None, "", []):
                continue
            identity = name.casefold()
            if identity in seen:
                continue
            seen.add(identity)
            result.append({"name": name, "value": safe_value})
        return result

    @classmethod
    def _string_list(cls, value: Any, *, maximum: int) -> list:
        if isinstance(value, str):
            raw_text = value.strip()
            try:
                decoded = json.loads(raw_text)
            except (TypeError, ValueError):
                decoded = None
            value = decoded if isinstance(decoded, list) else [raw_text]
        if not isinstance(value, list):
            return []
        result = []
        seen = set()
        for item in value[:maximum]:
            text = cls._text(item, maximum=2_000)
            if not text or text in seen:
                continue
            seen.add(text)
            result.append(text)
        return result

    @staticmethod
    def _first(*values: Any) -> Any:
        for value in values:
            if value not in (None, "", [], {}):
                return value
        return None

    @classmethod
    def _record(
        cls,
        target: dict,
        provenance: dict,
        path: str,
        value: Any,
        *,
        source: str,
        trust: str,
    ) -> None:
        if value in (None, "", [], {}):
            return
        cursor = target
        parts = path.split(".")
        for part in parts[:-1]:
            cursor = cursor.setdefault(part, {})
        cursor[parts[-1]] = value
        provenance[path] = {"source": source, "trust": trust}

    @classmethod
    def wb_projection_drift(cls, imported_product: ImportedProduct) -> dict:
        """Compare bounded common content with the linked WB projection.

        ``ImportedProduct`` remains the canonical source.  This helper never
        merges channel data; it only makes divergence visible before another
        marketplace projection is prepared.
        """
        if not isinstance(imported_product, ImportedProduct):
            raise MarketplaceFactPackError(
                "WB projection comparison requires an ImportedProduct"
            )
        wb_product = imported_product.product
        if wb_product is None:
            return {
                "linked": False,
                "in_sync": None,
                "differing_fields": [],
            }

        pairs = {
            "title": (imported_product.title, wb_product.title),
            "description": (
                imported_product.description,
                wb_product.description,
            ),
            "brand": (imported_product.brand, wb_product.brand),
        }

        def comparable(value: Any, maximum: int) -> Optional[str]:
            text = cls._text(value, maximum=maximum)
            return " ".join(text.split()) if text is not None else None

        limits = {
            "title": 500,
            "description": cls.MAX_DESCRIPTION,
            "brand": 200,
        }
        differing_fields = [
            field
            for field, (canonical_value, wb_value) in pairs.items()
            if comparable(canonical_value, limits[field])
            != comparable(wb_value, limits[field])
        ]
        return {
            "linked": True,
            "wb_product_id": wb_product.id,
            "wb_nm_id": (
                str(wb_product.nm_id)
                if wb_product.nm_id is not None else None
            ),
            "in_sync": not differing_fields,
            "differing_fields": differing_fields,
        }

    @classmethod
    def build(cls, imported_product: ImportedProduct) -> Dict[str, Any]:
        if not isinstance(imported_product, ImportedProduct):
            raise MarketplaceFactPackError(
                "Marketplace fact pack requires an ImportedProduct"
            )

        supplier_product: Optional[SupplierProduct] = imported_product.supplier_product
        imported_original = cls._load_object(imported_product.original_data)
        supplier_original = cls._load_object(
            supplier_product.original_data_json if supplier_product else None
        )
        original = imported_original or supplier_original
        original_source = (
            "imported_product.original_data"
            if imported_original else "supplier_product.original_data_json"
        )

        facts: Dict[str, Any] = {}
        provenance: Dict[str, Any] = {}

        def original_text(key: str, maximum: int = cls.MAX_TEXT) -> Optional[str]:
            return cls._text(original.get(key), maximum=maximum)

        title = cls._text(imported_product.title, maximum=500)
        if title:
            cls._record(
                facts,
                provenance,
                "identity.title",
                title,
                source="imported_product.title",
                trust="seller_current",
            )
        description = cls._text(
            imported_product.description,
            maximum=cls.MAX_DESCRIPTION,
        )
        if description:
            cls._record(
                facts,
                provenance,
                "identity.description",
                description,
                source="imported_product.description",
                trust="seller_current",
            )
        brand = original_text("brand", 200)
        if brand:
            cls._record(
                facts,
                provenance,
                "identity.brand",
                brand,
                source=f"{original_source}.brand",
                trust="observed",
            )
        category = cls._first(
            original_text("category", 500),
            cls._text(imported_product.category, maximum=500),
        )
        if category:
            cls._record(
                facts,
                provenance,
                "identity.source_category",
                category,
                source=(
                    f"{original_source}.category"
                    if original_text("category", 500)
                    else "imported_product.category"
                ),
                trust="observed",
            )

        vendor_code = cls._first(
            original_text("vendor_code", 200),
            cls._text(imported_product.external_vendor_code, maximum=200),
        )
        if vendor_code:
            cls._record(
                facts,
                provenance,
                "identifiers.vendor_code",
                vendor_code,
                source=(
                    f"{original_source}.vendor_code"
                    if original_text("vendor_code", 200)
                    else "imported_product.external_vendor_code"
                ),
                trust="observed",
            )
        external_id = cls._text(imported_product.external_id, maximum=200)
        if external_id:
            cls._record(
                facts,
                provenance,
                "identifiers.external_id",
                external_id,
                source="imported_product.external_id",
                trust="observed",
            )
        raw_barcodes = original.get("barcodes")
        if raw_barcodes in (None, [], "") and original.get("barcode"):
            raw_barcodes = [original.get("barcode")]
        barcodes = cls._string_list(raw_barcodes, maximum=100)
        barcode_source = f"{original_source}.barcodes"
        if not barcodes:
            barcodes = cls._string_list(imported_product.barcodes, maximum=100)
            barcode_source = "imported_product.barcodes"
        if barcodes:
            cls._record(
                facts,
                provenance,
                "identifiers.barcodes",
                barcodes,
                source=barcode_source,
                trust="observed",
            )

        original_characteristics = cls._characteristics(
            original.get("characteristics")
        )
        if original_characteristics:
            cls._record(
                facts,
                provenance,
                "attributes.characteristics",
                original_characteristics,
                source=f"{original_source}.characteristics",
                trust="observed",
            )
        for key, maximum in (
            ("colors", 50),
            ("materials", 50),
            ("sizes", 100),
        ):
            value = original.get(key)
            if isinstance(value, dict) and key == "sizes":
                value = cls._safe_value(value)
            else:
                value = cls._string_list(value, maximum=maximum)
            if value:
                cls._record(
                    facts,
                    provenance,
                    f"attributes.{key}",
                    value,
                    source=f"{original_source}.{key}",
                    trust="observed",
                )
        for key in ("country", "gender", "season", "age_group"):
            value = original_text(key, 200)
            if value:
                cls._record(
                    facts,
                    provenance,
                    f"attributes.{key}",
                    value,
                    source=f"{original_source}.{key}",
                    trust="observed",
                )

        dimensions = cls._safe_value(original.get("dimensions"))
        if isinstance(dimensions, dict) and dimensions:
            cls._record(
                facts,
                provenance,
                "physical.dimensions",
                dimensions,
                source=f"{original_source}.dimensions",
                trust="observed",
            )

        images = cls._string_list(original.get("photo_urls"), maximum=cls.MAX_IMAGES)
        image_source = f"{original_source}.photo_urls"
        if not images:
            images = cls._string_list(
                imported_product.photo_urls,
                maximum=cls.MAX_IMAGES,
            )
            image_source = "imported_product.photo_urls"
        if images:
            cls._record(
                facts,
                provenance,
                "media.images",
                images,
                source=image_source,
                trust="observed",
            )

        price = cls._number(imported_product.calculated_price)
        old_price = cls._number(imported_product.calculated_price_before_discount)
        if price is not None and price > 0:
            cls._record(
                facts,
                provenance,
                "commercial.price",
                price,
                source="imported_product.calculated_price",
                trust="seller_calculation",
            )
        if old_price is not None and old_price > 0:
            cls._record(
                facts,
                provenance,
                "commercial.old_price",
                old_price,
                source="imported_product.calculated_price_before_discount",
                trust="seller_calculation",
            )

        suggestions: Dict[str, Any] = {}
        if "brand" not in facts.get("identity", {}):
            current_brand = cls._text(imported_product.brand, maximum=200)
            if current_brand:
                suggestions["brand"] = current_brand
        if not original_characteristics:
            current_raw: Any = imported_product.characteristics
            if isinstance(current_raw, str):
                try:
                    current_raw = json.loads(current_raw)
                except (TypeError, ValueError):
                    current_raw = None
            current_characteristics = cls._characteristics(current_raw)
            if current_characteristics:
                suggestions["legacy_characteristics"] = current_characteristics
        for key, raw_value in (
            ("colors", imported_product.colors),
            ("materials", imported_product.materials),
            ("sizes", imported_product.sizes),
        ):
            if key in facts.get("attributes", {}):
                continue
            value = cls._json_field(raw_value, [])
            safe = cls._safe_value(value)
            if safe not in (None, [], {}):
                suggestions[key] = safe
        for key in ("country", "gender"):
            if key in facts.get("attributes", {}):
                continue
            current = cls._text(getattr(imported_product, key, None), maximum=200)
            if current:
                suggestions[key] = current
        if supplier_product and supplier_product.ai_parsed_data_json:
            ai_data = cls._load_object(supplier_product.ai_parsed_data_json)
            allowed_ai_sections = {}
            for section in (
                "audience",
                "color",
                "contents",
                "functionality",
                "materials",
                "origin",
                "package",
                "physical",
                "sizing",
            ):
                safe = cls._safe_value(ai_data.get(section))
                if safe not in (None, [], {}):
                    allowed_ai_sections[section] = safe
            if allowed_ai_sections:
                suggestions["legacy_ai"] = allowed_ai_sections

        source = {
            "imported_product_id": imported_product.id,
            "supplier_product_id": imported_product.supplier_product_id,
            "supplier_id": imported_product.supplier_id,
            "source_type": cls._text(imported_product.source_type, maximum=80)
            or "unknown",
        }
        hash_payload = {
            "version": cls.VERSION,
            "source": source,
            "facts": facts,
            "provenance": provenance,
            "unverified_suggestions": suggestions,
        }
        canonical = cls._stable_json(hash_payload)
        if len(canonical.encode("utf-8")) > cls.MAX_SERIALIZED_BYTES:
            raise MarketplaceFactPackError(
                "Marketplace fact pack exceeds the safety limit"
            )
        result = dict(hash_payload)
        result["fact_hash"] = sha256(canonical.encode("utf-8")).hexdigest()
        return result
