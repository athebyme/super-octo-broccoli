"""Audited relationship between canonical seller products and channel listings.

``ImportedProduct`` is the current canonical seller-owned card.  A
``MarketplaceListing`` is only a marketplace/account projection.  This service
is intentionally deterministic: automatic links require one unique exact
offer/vendor identity.  Titles and LLM similarity are never auto-link signals.
"""

from datetime import datetime
import json
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload

from models import (
    ImportedProduct,
    Marketplace,
    MarketplaceListing,
    MarketplaceListingLinkEvent,
    MarketplaceProductDraft,
    Product,
    SupplierProduct,
    db,
)


class MarketplaceProductLinkError(RuntimeError):
    status_code = 400
    code = "marketplace_product_link_error"


class MarketplaceProductLinkValidationError(MarketplaceProductLinkError):
    status_code = 400
    code = "invalid_marketplace_product_link"


class MarketplaceProductLinkNotFound(MarketplaceProductLinkError):
    status_code = 404
    code = "marketplace_product_link_not_found"


class MarketplaceProductLinkConflict(MarketplaceProductLinkError):
    status_code = 409
    code = "marketplace_product_link_conflict"


class MarketplaceProductLinkService:
    """Own canonical-card linking, reconciliation and append-only audit."""

    MAX_BATCH = 1000
    MAX_SEARCH_RESULTS = 25
    MAX_JSON_BYTES = 32_768
    MATCH_FIELDS = (
        "imported.external_vendor_code",
        "imported.external_id",
        "wb.vendor_code",
        "wb.supplier_vendor_code",
        "supplier.external_id",
        "supplier.vendor_code",
        "supplier.additional_vendor_code",
    )

    @staticmethod
    def _positive_integer(value: Any, field_name: str) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise MarketplaceProductLinkValidationError(
                f"{field_name} должен быть положительным целым числом"
            )
        return value

    @staticmethod
    def _commit() -> None:
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            raise MarketplaceProductLinkConflict(
                "Связь изменилась конкурентно; обновите страницу и повторите"
            ) from None

    @classmethod
    def _canonical_json(cls, value: Any) -> str:
        if not isinstance(value, dict):
            value = {}
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if len(encoded.encode("utf-8")) > cls.MAX_JSON_BYTES:
            raise MarketplaceProductLinkValidationError(
                "Данные подтверждения связи превышают лимит"
            )
        return encoded

    @staticmethod
    def _identity(value: Any) -> Optional[str]:
        if not isinstance(value, str):
            return None
        normalized = value.strip()
        return normalized if normalized and len(normalized) <= 200 else None

    @classmethod
    def _owned_listing(
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
            joinedload(MarketplaceListing.imported_product).joinedload(
                ImportedProduct.product
            ),
            joinedload(MarketplaceListing.imported_product).joinedload(
                ImportedProduct.supplier_product
            ),
        ).filter_by(
            id=listing_id,
            seller_id=seller_id,
        ).first()
        if listing is None:
            raise MarketplaceProductLinkNotFound("Листинг не найден")
        return listing

    @classmethod
    def _owned_product(
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
            joinedload(ImportedProduct.product),
            joinedload(ImportedProduct.supplier_product),
        ).filter_by(
            id=imported_product_id,
            seller_id=seller_id,
        ).first()
        if product is None:
            raise MarketplaceProductLinkNotFound(
                "Внутренняя карточка не найдена"
            )
        return product

    @classmethod
    def _candidate_query(cls, *, seller_id: int):
        return ImportedProduct.query.options(
            joinedload(ImportedProduct.product),
            joinedload(ImportedProduct.supplier_product),
        ).outerjoin(
            Product,
            ImportedProduct.product_id == Product.id,
        ).outerjoin(
            SupplierProduct,
            ImportedProduct.supplier_product_id == SupplierProduct.id,
        ).filter(ImportedProduct.seller_id == seller_id)

    @classmethod
    def _exact_candidate_rows(
        cls,
        *,
        seller_id: int,
        offers: Sequence[str],
    ) -> List[ImportedProduct]:
        exact = sorted({value for value in offers if cls._identity(value)})
        if not exact:
            return []
        return cls._candidate_query(seller_id=seller_id).filter(or_(
            ImportedProduct.external_vendor_code.in_(exact),
            ImportedProduct.external_id.in_(exact),
            Product.vendor_code.in_(exact),
            Product.supplier_vendor_code.in_(exact),
            SupplierProduct.external_id.in_(exact),
            SupplierProduct.vendor_code.in_(exact),
            SupplierProduct.additional_vendor_code.in_(exact),
        )).all()

    @classmethod
    def _evidence_for_offer(
        cls,
        product: ImportedProduct,
        offer_id: str,
    ) -> List[str]:
        wb = product.product
        supplier = product.supplier_product
        values = {
            "imported.external_vendor_code": product.external_vendor_code,
            "imported.external_id": product.external_id,
            "wb.vendor_code": wb.vendor_code if wb else None,
            "wb.supplier_vendor_code": (
                wb.supplier_vendor_code if wb else None
            ),
            "supplier.external_id": supplier.external_id if supplier else None,
            "supplier.vendor_code": supplier.vendor_code if supplier else None,
            "supplier.additional_vendor_code": (
                supplier.additional_vendor_code if supplier else None
            ),
        }
        return [
            field
            for field in cls.MATCH_FIELDS
            if cls._identity(values.get(field)) == offer_id
        ]

    @classmethod
    def _event(
        cls,
        listing: MarketplaceListing,
        *,
        previous_imported_product_id: Optional[int],
        action: str,
        source: str,
        evidence: Mapping[str, Any],
        actor_user_id: Optional[int],
    ) -> None:
        db.session.add(MarketplaceListingLinkEvent(
            seller_id=listing.seller_id,
            marketplace_id=listing.marketplace_id,
            account_id=listing.account_id,
            listing_id=listing.id,
            previous_imported_product_id=previous_imported_product_id,
            imported_product_id=listing.imported_product_id,
            action=action,
            source=source,
            evidence_json=cls._canonical_json(dict(evidence)),
            actor_user_id=actor_user_id,
            link_version=listing.link_version,
        ))

    @classmethod
    def _apply_link(
        cls,
        listing: MarketplaceListing,
        product: ImportedProduct,
        *,
        action: str,
        source: str,
        evidence: Mapping[str, Any],
        actor_user_id: Optional[int],
        now: datetime,
    ) -> None:
        previous = listing.imported_product_id
        listing.imported_product_id = product.id
        listing.link_status = "linked"
        listing.link_source = source
        listing.link_evidence_json = cls._canonical_json(dict(evidence))
        listing.link_version = max(int(listing.link_version or 0), 1) + 1
        listing.linked_at = now
        listing.linked_by_user_id = actor_user_id
        cls._event(
            listing,
            previous_imported_product_id=previous,
            action=action,
            source=source,
            evidence=evidence,
            actor_user_id=actor_user_id,
        )

    @classmethod
    def _mark_ambiguous(
        cls,
        listing: MarketplaceListing,
        *,
        evidence: Mapping[str, Any],
        now: datetime,
    ) -> bool:
        encoded = cls._canonical_json(dict(evidence))
        if (
            listing.imported_product_id is None
            and listing.link_status == "ambiguous"
            and listing.link_evidence_json == encoded
        ):
            return False
        listing.imported_product_id = None
        listing.link_status = "ambiguous"
        listing.link_source = "exact_offer_identity"
        listing.link_evidence_json = encoded
        listing.link_version = max(int(listing.link_version or 0), 1) + 1
        listing.linked_at = None
        listing.linked_by_user_id = None
        cls._event(
            listing,
            previous_imported_product_id=None,
            action="ambiguous",
            source="exact_offer_identity",
            evidence=evidence,
            actor_user_id=None,
        )
        return True

    @classmethod
    def reconcile_objects(
        cls,
        *,
        seller_id: int,
        listings: Iterable[MarketplaceListing],
        now: Optional[datetime] = None,
        commit: bool = False,
    ) -> Dict[str, int]:
        """Auto-link a bounded exact seller set without title/AI inference."""
        seller_id = cls._positive_integer(seller_id, "seller_id")
        rows = list(listings)
        if len(rows) > cls.MAX_BATCH:
            raise MarketplaceProductLinkValidationError(
                f"За один reconcile разрешено не более {cls.MAX_BATCH} листингов"
            )
        ids = [row.id for row in rows if row.id is not None]
        if len(ids) != len(set(ids)):
            raise MarketplaceProductLinkValidationError(
                "Набор листингов содержит дубли"
            )
        eligible = []
        for row in rows:
            if row.seller_id != seller_id:
                raise MarketplaceProductLinkNotFound("Листинг не найден")
            code = row.marketplace.code if row.marketplace else None
            if code == "ozon" and row.account_id and row.imported_product_id is None:
                eligible.append(row)
        if not eligible:
            return {"linked": 0, "ambiguous": 0, "unmatched": 0}

        offers = [cls._identity(row.offer_id) for row in eligible]
        candidate_rows = cls._exact_candidate_rows(
            seller_id=seller_id,
            offers=[value for value in offers if value],
        )
        candidates_by_offer: Dict[str, Dict[int, Dict[str, Any]]] = {}
        for product in candidate_rows:
            for offer_id in offers:
                if not offer_id:
                    continue
                fields = cls._evidence_for_offer(product, offer_id)
                if fields:
                    candidates_by_offer.setdefault(offer_id, {})[product.id] = {
                        "product": product,
                        "fields": fields,
                    }

        candidate_ids = {
            product_id
            for candidates in candidates_by_offer.values()
            for product_id in candidates
        }
        account_ids = {row.account_id for row in eligible if row.account_id}
        occupied = set()
        if candidate_ids and account_ids:
            occupied = {
                (row.account_id, row.imported_product_id)
                for row in MarketplaceListing.query.filter(
                    MarketplaceListing.seller_id == seller_id,
                    MarketplaceListing.account_id.in_(account_ids),
                    MarketplaceListing.imported_product_id.in_(candidate_ids),
                ).all()
                if row.imported_product_id is not None
            }

        result = {"linked": 0, "ambiguous": 0, "unmatched": 0}
        now = now or datetime.utcnow()
        for listing, offer_id in zip(eligible, offers):
            matches = candidates_by_offer.get(offer_id or "", {})
            if not matches:
                result["unmatched"] += 1
                continue
            if len(matches) != 1:
                cls._mark_ambiguous(
                    listing,
                    evidence={
                        "offer_id": offer_id,
                        "candidate_product_ids": sorted(matches),
                        "reason": "multiple_exact_internal_identities",
                    },
                    now=now,
                )
                result["ambiguous"] += 1
                continue
            match = next(iter(matches.values()))
            product = match["product"]
            key = (listing.account_id, product.id)
            if key in occupied:
                cls._mark_ambiguous(
                    listing,
                    evidence={
                        "offer_id": offer_id,
                        "candidate_product_ids": [product.id],
                        "reason": "canonical_product_already_linked_in_account",
                    },
                    now=now,
                )
                result["ambiguous"] += 1
                continue
            cls._apply_link(
                listing,
                product,
                action="auto_link",
                source="exact_offer_identity",
                evidence={
                    "offer_id": offer_id,
                    "matched_fields": match["fields"],
                },
                actor_user_id=None,
                now=now,
            )
            occupied.add(key)
            result["linked"] += 1
        if commit:
            cls._commit()
        return result

    @classmethod
    def reconcile_listing(
        cls,
        *,
        seller_id: int,
        listing_id: int,
    ) -> MarketplaceListing:
        listing = cls._owned_listing(
            seller_id=seller_id,
            listing_id=listing_id,
        )
        cls.reconcile_objects(
            seller_id=seller_id,
            listings=[listing],
            commit=True,
        )
        return cls._owned_listing(
            seller_id=seller_id,
            listing_id=listing_id,
        )

    @classmethod
    def record_known_link(
        cls,
        *,
        listing: MarketplaceListing,
        product: ImportedProduct,
        source: str,
        actor_user_id: Optional[int] = None,
        evidence: Optional[Mapping[str, Any]] = None,
        now: Optional[datetime] = None,
    ) -> bool:
        """Record a provenance-backed local link such as confirmed publication."""
        if (
            not isinstance(listing, MarketplaceListing)
            or not isinstance(product, ImportedProduct)
            or listing.seller_id != product.seller_id
        ):
            raise MarketplaceProductLinkConflict(
                "Листинг и внутренняя карточка имеют разный seller scope"
            )
        if listing.imported_product_id not in (None, product.id):
            raise MarketplaceProductLinkConflict(
                "Листинг уже связан с другой внутренней карточкой"
            )
        if listing.account_id is not None:
            duplicate = MarketplaceListing.query.filter(
                MarketplaceListing.seller_id == listing.seller_id,
                MarketplaceListing.account_id == listing.account_id,
                MarketplaceListing.imported_product_id == product.id,
                MarketplaceListing.id != listing.id,
            ).first()
            if duplicate is not None:
                raise MarketplaceProductLinkConflict(
                    "Внутренняя карточка уже связана с другим листингом кабинета"
                )
        if (
            listing.imported_product_id == product.id
            and listing.link_status == "linked"
            and listing.link_source
        ):
            return False
        cls._apply_link(
            listing,
            product,
            action="auto_link",
            source=source,
            evidence=dict(evidence or {}),
            actor_user_id=actor_user_id,
            now=now or datetime.utcnow(),
        )
        return True

    @classmethod
    def link(
        cls,
        *,
        seller_id: int,
        listing_id: int,
        imported_product_id: int,
        expected_link_version: int,
        actor_user_id: Optional[int],
    ) -> MarketplaceListing:
        expected_link_version = cls._positive_integer(
            expected_link_version,
            "expected_link_version",
        )
        listing = cls._owned_listing(
            seller_id=seller_id,
            listing_id=listing_id,
        )
        if not listing.marketplace or listing.marketplace.code != "ozon":
            raise MarketplaceProductLinkConflict(
                "Ручная связь доступна только для Ozon; WB projection связан источником импорта"
            )
        if listing.link_version != expected_link_version:
            raise MarketplaceProductLinkConflict(
                "Связь изменилась; обновите страницу и повторите"
            )
        product = cls._owned_product(
            seller_id=seller_id,
            imported_product_id=imported_product_id,
        )
        if listing.imported_product_id == product.id:
            return listing
        if listing.imported_product_id is not None:
            raise MarketplaceProductLinkConflict(
                "Сначала отвяжите текущую внутреннюю карточку"
            )
        duplicate = MarketplaceListing.query.filter(
            MarketplaceListing.seller_id == seller_id,
            MarketplaceListing.account_id == listing.account_id,
            MarketplaceListing.imported_product_id == product.id,
            MarketplaceListing.id != listing.id,
        ).first()
        if duplicate is not None:
            raise MarketplaceProductLinkConflict(
                "Эта внутренняя карточка уже связана с другим листингом кабинета"
            )
        cls._apply_link(
            listing,
            product,
            action="manual_link",
            source="seller_confirmation",
            evidence={"confirmed_imported_product_id": product.id},
            actor_user_id=actor_user_id,
            now=datetime.utcnow(),
        )
        cls._commit()
        return cls._owned_listing(
            seller_id=seller_id,
            listing_id=listing_id,
        )

    @classmethod
    def unlink(
        cls,
        *,
        seller_id: int,
        listing_id: int,
        expected_link_version: int,
        actor_user_id: Optional[int],
    ) -> MarketplaceListing:
        expected_link_version = cls._positive_integer(
            expected_link_version,
            "expected_link_version",
        )
        listing = cls._owned_listing(
            seller_id=seller_id,
            listing_id=listing_id,
        )
        if not listing.marketplace or listing.marketplace.code != "ozon":
            raise MarketplaceProductLinkConflict(
                "WB projection нельзя отвязать через Ozon workflow"
            )
        if listing.link_version != expected_link_version:
            raise MarketplaceProductLinkConflict(
                "Связь изменилась; обновите страницу и повторите"
            )
        if listing.imported_product_id is None:
            return listing
        bound_draft = MarketplaceProductDraft.query.filter_by(
            seller_id=seller_id,
            published_listing_id=listing.id,
        ).first()
        if bound_draft is not None:
            raise MarketplaceProductLinkConflict(
                "Связь используется Ozon-черновиком; сначала завершите или архивируйте его"
            )
        previous = listing.imported_product_id
        listing.imported_product_id = None
        listing.link_status = "unlinked"
        listing.link_source = "seller_unlink"
        listing.link_evidence_json = "{}"
        listing.link_version = max(int(listing.link_version or 0), 1) + 1
        listing.linked_at = None
        listing.linked_by_user_id = actor_user_id
        cls._event(
            listing,
            previous_imported_product_id=previous,
            action="unlink",
            source="seller_confirmation",
            evidence={"previous_imported_product_id": previous},
            actor_user_id=actor_user_id,
        )
        cls._commit()
        return cls._owned_listing(
            seller_id=seller_id,
            listing_id=listing_id,
        )

    @classmethod
    def _candidate_summary(cls, product: ImportedProduct) -> dict:
        wb = product.product
        supplier = product.supplier_product
        if supplier and (
            supplier.ai_parsed_at is not None
            or bool(supplier.ai_parsed_data_json)
        ):
            ai_source = "supplier_product_cache"
        elif any((
            product.ai_analysis_at,
            product.ai_keywords,
            product.ai_attributes,
            product.ai_seo_title,
        )):
            ai_source = "imported_product_cache"
        else:
            ai_source = None
        return {
            "id": product.id,
            "title": product.title,
            "external_id": product.external_id,
            "external_vendor_code": product.external_vendor_code,
            "supplier_product_id": product.supplier_product_id,
            "wb_product_id": product.product_id,
            "wb_nm_id": (
                str(wb.nm_id) if wb and wb.nm_id is not None
                else str(product.wb_nm_id) if product.wb_nm_id is not None
                else None
            ),
            "ai_cache_available": ai_source is not None,
            "ai_source": ai_source,
            "has_source_photos": bool(product.photo_urls),
        }

    @classmethod
    def search_candidates(
        cls,
        *,
        seller_id: int,
        listing_id: int,
        query: Optional[str] = None,
        limit: int = 20,
    ) -> List[dict]:
        listing = cls._owned_listing(
            seller_id=seller_id,
            listing_id=listing_id,
        )
        return cls._search_candidates_for_listing(
            seller_id=seller_id,
            listing=listing,
            query=query,
            limit=limit,
        )

    @classmethod
    def _search_candidates_for_listing(
        cls,
        *,
        seller_id: int,
        listing: MarketplaceListing,
        query: Optional[str],
        limit: int = 20,
    ) -> List[dict]:
        limit = cls._positive_integer(limit, "limit")
        if limit > cls.MAX_SEARCH_RESULTS:
            raise MarketplaceProductLinkValidationError(
                f"limit не может быть больше {cls.MAX_SEARCH_RESULTS}"
            )
        raw_query = query if isinstance(query, str) else ""
        term = raw_query.strip()
        if len(term) > 200:
            raise MarketplaceProductLinkValidationError(
                "Поисковый запрос длиннее 200 символов"
            )
        if not term:
            products = cls._exact_candidate_rows(
                seller_id=seller_id,
                offers=[listing.offer_id],
            )
        else:
            escaped = (
                term.replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
            )
            pattern = f"%{escaped}%"
            filters = [
                ImportedProduct.title.ilike(pattern, escape="\\"),
                ImportedProduct.external_id.ilike(pattern, escape="\\"),
                ImportedProduct.external_vendor_code.ilike(pattern, escape="\\"),
                Product.vendor_code.ilike(pattern, escape="\\"),
                Product.supplier_vendor_code.ilike(pattern, escape="\\"),
                SupplierProduct.external_id.ilike(pattern, escape="\\"),
                SupplierProduct.vendor_code.ilike(pattern, escape="\\"),
            ]
            if term.isascii() and term.isdigit():
                filters.append(ImportedProduct.id == int(term))
                filters.append(Product.nm_id == int(term))
            products = cls._candidate_query(seller_id=seller_id).filter(
                or_(*filters)
            ).order_by(
                ImportedProduct.updated_at.desc(),
                ImportedProduct.id.desc(),
            ).limit(limit).all()
        unique = {product.id: product for product in products}
        return [
            cls._candidate_summary(product)
            for product in list(unique.values())[:limit]
        ]

    @classmethod
    def context(
        cls,
        *,
        seller_id: int,
        listing_id: int,
        query: Optional[str] = None,
        listing: Optional[MarketplaceListing] = None,
    ) -> dict:
        seller_id = cls._positive_integer(seller_id, "seller_id")
        listing_id = cls._positive_integer(listing_id, "listing_id")
        if (
            not isinstance(listing, MarketplaceListing)
            or listing.id != listing_id
            or listing.seller_id != seller_id
        ):
            listing = cls._owned_listing(
                seller_id=seller_id,
                listing_id=listing_id,
            )
        events = MarketplaceListingLinkEvent.query.filter_by(
            seller_id=seller_id,
            listing_id=listing.id,
        ).order_by(
            MarketplaceListingLinkEvent.id.desc()
        ).limit(20).all()
        return {
            "canonical_product": (
                cls._candidate_summary(listing.imported_product)
                if listing.imported_product else None
            ),
            "candidates": (
                []
                if listing.imported_product_id is not None
                else cls._search_candidates_for_listing(
                    seller_id=seller_id,
                    listing=listing,
                    query=query,
                )
            ),
            "events": [event.to_public_dict() for event in events],
        }
