"""Typed listing-media targets for shared product content workflows.

The canonical :class:`ImportedProduct` remains the only identity/photo source
for Image Lab.  A marketplace listing only contributes an exact seller/account
target and provider-specific output constraints.  This prevents an observed
Ozon media snapshot from silently becoming a second master product.
"""

from __future__ import annotations

from hashlib import sha256
import json
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urlparse

from sqlalchemy.orm import joinedload

from models import MarketplaceListing


class MarketplaceListingMediaError(ValueError):
    """A listing cannot safely be used as a media target."""


class MarketplaceListingMediaService:
    """Resolve exact listing targets without provider calls or side effects."""

    CONTRACT_VERSION = 1
    OZON_MAX_MAIN_IMAGES = 30
    OUTPUT_WIDTH = 900
    OUTPUT_HEIGHT = 1200

    @staticmethod
    def _positive_int(value: Any, field_name: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise MarketplaceListingMediaError(
                f"{field_name} должен быть положительным целым числом"
            )
        return value

    @staticmethod
    def _stored_object(raw_value: Any) -> dict:
        if isinstance(raw_value, dict):
            return raw_value
        if not isinstance(raw_value, str) or not raw_value:
            return {}
        try:
            value = json.loads(raw_value)
        except (TypeError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _public_http_url(value: Any) -> Optional[str]:
        if not isinstance(value, str):
            return None
        value = value.strip()
        if not value or len(value) > 2_000:
            return None
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return None
        return value

    @classmethod
    def observed_main_images(cls, listing: MarketplaceListing) -> List[str]:
        """Return the bounded, ordered Ozon main-image observation."""
        media = cls._stored_object(listing.media_json)
        result: List[str] = []
        primary = cls._public_http_url(media.get("primary_image"))
        if primary:
            result.append(primary)
        images = media.get("images")
        if isinstance(images, list):
            for raw_url in images[: cls.OZON_MAX_MAIN_IMAGES]:
                url = cls._public_http_url(raw_url)
                if url and url not in result:
                    result.append(url)
                if len(result) >= cls.OZON_MAX_MAIN_IMAGES:
                    break
        return result

    @classmethod
    def _context(cls, listing: MarketplaceListing) -> Dict[str, Any]:
        marketplace_code = (
            listing.marketplace.code if listing.marketplace is not None else None
        )
        if marketplace_code != "ozon":
            raise MarketplaceListingMediaError(
                "Фотостудия пока поддерживает listing-target только для Ozon"
            )
        if listing.account is None or listing.account_id is None:
            raise MarketplaceListingMediaError("Ozon-листинг не привязан к кабинету")
        if listing.account.seller_id != listing.seller_id:
            raise MarketplaceListingMediaError("Нарушен seller scope кабинета Ozon")
        if listing.account.marketplace_id != listing.marketplace_id:
            raise MarketplaceListingMediaError(
                "Кабинет и листинг относятся к разным маркетплейсам"
            )
        if not listing.account.is_active:
            raise MarketplaceListingMediaError(
                "Неактивный кабинет Ozon нельзя выбрать целью"
            )
        if listing.canonical_link_status != "linked" or listing.imported_product is None:
            raise MarketplaceListingMediaError(
                "Сначала свяжите Ozon-листинг с общей внутренней карточкой"
            )
        if not listing.is_available or listing.is_archived:
            raise MarketplaceListingMediaError(
                "Недоступный или архивный Ozon-листинг нельзя выбрать целью"
            )

        observed = cls.observed_main_images(listing)
        observed_hash = sha256(
            json.dumps(
                observed,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return {
            "contract_version": cls.CONTRACT_VERSION,
            "entity_kind": "marketplace_listing",
            "listing_id": listing.id,
            "marketplace_code": marketplace_code,
            "account_id": listing.account_id,
            "account_label": listing.account.label or f"Ozon #{listing.account_id}",
            "imported_product_id": listing.imported_product_id,
            "listing_title": (listing.title or listing.imported_product.title or "")[:500],
            "offer_id": (listing.offer_id or "")[:200],
            "source_policy": "canonical_imported_product_only",
            "observed_media": {
                "main_image_count": len(observed),
                "main_image_fingerprint": observed_hash,
                "available_main_slots": max(
                    0,
                    cls.OZON_MAX_MAIN_IMAGES - len(observed),
                ),
            },
            "constraints": {
                "preferred_width": cls.OUTPUT_WIDTH,
                "preferred_height": cls.OUTPUT_HEIGHT,
                "aspect_ratio": "3:4",
                "max_main_images": cls.OZON_MAX_MAIN_IMAGES,
                "requires_public_http_url": True,
                "images360_supported": False,
                "local_artifact_attachable": False,
                "attachment_workflow": "human_review_then_public_host_then_draft",
                "automatic_attachment": False,
                "automatic_publication": False,
            },
            "account_state": {
                "active": True,
                "connection_status": listing.account.connection_status,
            },
        }

    @classmethod
    def resolve_target(
        cls,
        *,
        seller_id: int,
        listing_id: int,
        expected_imported_product_id: Optional[int] = None,
        marketplace_code: Optional[str] = None,
        account_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Ground one untrusted browser target to an exact seller-owned row."""
        seller_id = cls._positive_int(seller_id, "seller_id")
        listing_id = cls._positive_int(listing_id, "listing_id")
        if expected_imported_product_id is not None:
            expected_imported_product_id = cls._positive_int(
                expected_imported_product_id,
                "expected_imported_product_id",
            )
        if marketplace_code is not None and marketplace_code != "ozon":
            raise MarketplaceListingMediaError("marketplace_code должен быть ozon")
        if account_id is not None:
            account_id = cls._positive_int(account_id, "account_id")

        listing = MarketplaceListing.query.options(
            joinedload(MarketplaceListing.marketplace),
            joinedload(MarketplaceListing.account),
            joinedload(MarketplaceListing.imported_product),
        ).filter_by(
            id=listing_id,
            seller_id=seller_id,
        ).first()
        if listing is None:
            raise MarketplaceListingMediaError("Ozon-листинг не найден")
        context = cls._context(listing)
        if (
            expected_imported_product_id is not None
            and context["imported_product_id"] != expected_imported_product_id
        ):
            raise MarketplaceListingMediaError(
                "Ozon-листинг связан с другой внутренней карточкой"
            )
        if marketplace_code is not None and context["marketplace_code"] != marketplace_code:
            raise MarketplaceListingMediaError("marketplace_code не совпадает с листингом")
        if account_id is not None and context["account_id"] != account_id:
            raise MarketplaceListingMediaError("account_id не совпадает с листингом")
        return context

    @classmethod
    def targets_for_products(
        cls,
        *,
        seller_id: int,
        imported_product_ids: Iterable[int],
    ) -> Dict[int, List[Dict[str, Any]]]:
        """Load Ozon targets for a page in one bounded query."""
        seller_id = cls._positive_int(seller_id, "seller_id")
        product_ids: List[int] = []
        for value in imported_product_ids:
            value = cls._positive_int(value, "imported_product_id")
            if value not in product_ids:
                product_ids.append(value)
            if len(product_ids) > 150:
                raise MarketplaceListingMediaError("Слишком много карточек")
        result: Dict[int, List[Dict[str, Any]]] = {
            product_id: [] for product_id in product_ids
        }
        if not product_ids:
            return result
        listings = MarketplaceListing.query.options(
            joinedload(MarketplaceListing.marketplace),
            joinedload(MarketplaceListing.account),
            joinedload(MarketplaceListing.imported_product),
        ).filter(
            MarketplaceListing.seller_id == seller_id,
            MarketplaceListing.imported_product_id.in_(product_ids),
            MarketplaceListing.is_available.is_(True),
            MarketplaceListing.is_archived.is_(False),
        ).all()
        for listing in listings:
            try:
                context = cls._context(listing)
            except MarketplaceListingMediaError:
                continue
            result[listing.imported_product_id].append(context)
        for targets in result.values():
            targets.sort(
                key=lambda item: (
                    item["account_label"].casefold(),
                    item["listing_id"],
                )
            )
        return result
