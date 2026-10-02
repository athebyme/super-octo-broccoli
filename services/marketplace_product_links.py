"""Audited relationship between canonical seller products and channel listings.

``ImportedProduct`` is the current canonical seller-owned card.  A
``MarketplaceListing`` is only a marketplace/account projection.  This service
is intentionally deterministic: automatic links require one unique exact
offer/vendor identity.  Titles and LLM similarity are never auto-link signals.
"""

from datetime import datetime
import json
from typing import (
    Any,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
)

from sqlalchemy import case, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload

from models import (
    ImportedProduct,
    Marketplace,
    MarketplaceListing,
    MarketplaceListingLinkEvent,
    MarketplaceProductDraft,
    Product,
    SellerMarketplaceAccount,
    SellerSupplier,
    Supplier,
    SupplierProduct,
    db,
)
from services.marketplace_source_identity import (
    ParsedSourceIdentity,
    SourceIdentityKey,
    encoded_record_identities,
    normalize_identity_text,
    parse_encoded_source_identities,
    source_record_identities,
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
    MAX_EVIDENCE_CANDIDATES = 25
    QUERY_CHUNK = 100
    MAX_TARGET_PRODUCTS = 200
    MAX_TARGET_LISTINGS = 1000
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
    def ozon_listings_for_products(
        cls,
        *,
        seller_id: int,
        product_ids: Sequence[int],
    ) -> Dict[int, List[dict]]:
        """Bulk read-проекция «WB Product → его Ozon-листинги» для UI-страницы.

        Один SELECT по цепочке Product ← ImportedProduct.product_id ←
        MarketplaceListing.imported_product_id (Marketplace.code == 'ozon'),
        tenant-scoped. Возвращает {product_id: [{listing_id, status,
        is_available, account_id}]} только для переданных ID; ничего не пишет.
        """
        if not isinstance(seller_id, int) or isinstance(seller_id, bool):
            return {}
        ids = [
            pid for pid in product_ids
            if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0
        ]
        if not ids:
            return {}
        rows = (
            db.session.query(
                ImportedProduct.product_id,
                MarketplaceListing.id,
                MarketplaceListing.normalized_status,
                MarketplaceListing.is_available,
                MarketplaceListing.account_id,
            )
            .join(
                MarketplaceListing,
                MarketplaceListing.imported_product_id == ImportedProduct.id,
            )
            .join(
                Marketplace,
                Marketplace.id == MarketplaceListing.marketplace_id,
            )
            .filter(
                ImportedProduct.seller_id == seller_id,
                MarketplaceListing.seller_id == seller_id,
                ImportedProduct.product_id.in_(ids),
                Marketplace.code == "ozon",
            )
            .all()
        )
        listings_map: Dict[int, List[dict]] = {}
        for product_id, listing_id, status, is_available, account_id in rows:
            listings_map.setdefault(product_id, []).append({
                "listing_id": listing_id,
                "status": status,
                "is_available": bool(is_available),
                "account_id": account_id,
            })
        return listings_map

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
        if not cls._candidate_fk_valid(product):
            raise MarketplaceProductLinkConflict(
                "Связи внутренней карточки с источником требуют проверки"
            )
        return product

    @staticmethod
    def _candidate_fk_valid(product: ImportedProduct) -> bool:
        wb = product.product
        supplier = product.supplier_product
        return (
            (product.product_id is None or wb is not None)
            and (product.supplier_product_id is None or supplier is not None)
            and (wb is None or wb.seller_id == product.seller_id)
            and (supplier is None or (
                product.supplier_id is None
                or supplier.supplier_id == product.supplier_id
            ))
        )

    @classmethod
    def _candidate_query(cls, *, seller_id: int):
        return ImportedProduct.query.options(
            joinedload(ImportedProduct.product),
            joinedload(ImportedProduct.supplier_product).joinedload(
                SupplierProduct.supplier
            ),
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

    @staticmethod
    def _chunks(values: Iterable[Any], size: int) -> Iterable[List[Any]]:
        chunk = []
        for value in values:
            chunk.append(value)
            if len(chunk) >= size:
                yield chunk
                chunk = []
        if chunk:
            yield chunk

    @staticmethod
    def _fold_identity(value: Any) -> Optional[str]:
        normalized = normalize_identity_text(value)
        return normalized.casefold() if normalized else None

    @classmethod
    def _product_identity_parts(
        cls,
        product: Product,
    ) -> Dict[str, Set[SourceIdentityKey]]:
        encoded = set(encoded_record_identities(
            product.vendor_code,
            product.supplier_vendor_code,
        ))
        vendor = cls._fold_identity(product.supplier_vendor_code)
        supplier_vendor = (
            {SourceIdentityKey("*", "vendor_code", vendor)}
            if vendor else set()
        )
        return {
            "wb.encoded_vendor_code": encoded,
            "wb.supplier_vendor_code": supplier_vendor,
        }

    @classmethod
    def _supplier_identity_parts(
        cls,
        product: SupplierProduct,
    ) -> Dict[str, Set[SourceIdentityKey]]:
        source_code = (
            product.supplier.code
            if product.supplier is not None
            else None
        )
        return {
            "supplier.source_identity": set(source_record_identities(
                source_code=source_code,
                external_id=product.external_id,
                vendor_codes=(
                    product.vendor_code,
                    product.additional_vendor_code,
                ),
            )),
        }

    @classmethod
    def _import_identity_parts(
        cls,
        product: ImportedProduct,
    ) -> Dict[str, Set[SourceIdentityKey]]:
        result = {
            "imported.source_identity": set(source_record_identities(
                source_code=product.source_type,
                external_id=product.external_id,
                vendor_codes=(product.external_vendor_code,),
            )),
        }
        if product.supplier_product is not None:
            result.update(cls._supplier_identity_parts(
                product.supplier_product
            ))
        if product.product is not None:
            result.update(cls._product_identity_parts(product.product))
        return result

    @classmethod
    def _raw_product_identities(
        cls,
        product: ImportedProduct,
    ) -> Set[str]:
        supplier = product.supplier_product
        wb_product = product.product
        return {
            value
            for value in (
                cls._identity(product.external_vendor_code),
                cls._identity(product.external_id),
                cls._identity(
                    wb_product.vendor_code if wb_product else None
                ),
                cls._identity(
                    wb_product.supplier_vendor_code if wb_product else None
                ),
                cls._identity(
                    supplier.external_id if supplier else None
                ),
                cls._identity(
                    supplier.vendor_code if supplier else None
                ),
                cls._identity(
                    supplier.additional_vendor_code if supplier else None
                ),
            )
            if value is not None
        }

    @staticmethod
    def _like_literal(value: str) -> str:
        return (
            value.replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
        )

    @classmethod
    def _target_listing_candidates(
        cls,
        *,
        seller_id: int,
        account: SellerMarketplaceAccount,
        products: Sequence[ImportedProduct],
    ) -> List[MarketplaceListing]:
        """Load a bounded SQL superset for exact selected-product matching."""
        exact_values: Set[str] = set()
        identity_keys: Set[SourceIdentityKey] = set()
        for product in products:
            exact_values.update(cls._raw_product_identities(product))
            identity_keys.update(cls._combined_identity_parts(
                cls._import_identity_parts(product)
            ))

        sex_ids = sorted({
            key.value for key in identity_keys
            if key.source_code == "sexoptovik"
            and key.kind == "external_id"
        })
        andrey_external = sorted({
            key.value for key in identity_keys
            if key.source_code == "andrey"
            and key.kind == "external_id"
        })
        andrey_serials = sorted({
            key.value for key in identity_keys
            if key.source_code == "andrey"
            and key.kind == "serial"
        })
        vendor_codes = sorted({
            key.value for key in identity_keys
            if key.source_code == "*" and key.kind == "vendor_code"
        })
        base = MarketplaceListing.query.options(
            joinedload(MarketplaceListing.marketplace),
        ).filter(
            MarketplaceListing.seller_id == seller_id,
            MarketplaceListing.marketplace_id == account.marketplace_id,
            MarketplaceListing.account_id == account.id,
        )
        rows: Dict[int, MarketplaceListing] = {}

        def collect(condition: Any) -> None:
            found = base.filter(condition).order_by(
                MarketplaceListing.id.asc()
            ).limit(cls.MAX_TARGET_LISTINGS + 1).all()
            if len(found) > cls.MAX_TARGET_LISTINGS:
                raise MarketplaceProductLinkConflict(
                    "Слишком много карточек Ozon имеют один source ID; "
                    "массовое создание безопасно остановлено"
                )
            rows.update({row.id: row for row in found})
            if len(rows) > cls.MAX_TARGET_LISTINGS:
                raise MarketplaceProductLinkConflict(
                    "Слишком много карточек Ozon совпали по exact source ID; "
                    "уменьшите выборку"
                )

        for chunk in cls._chunks(sorted(exact_values), cls.QUERY_CHUNK):
            collect(MarketplaceListing.offer_id.in_(chunk))
        for chunk in cls._chunks(sex_ids, cls.QUERY_CHUNK // 2):
            conditions = []
            for value in chunk:
                conditions.extend((
                    MarketplaceListing.offer_id.like(f"id-{value}-%"),
                    MarketplaceListing.offer_id.like(f"%S{value}"),
                ))
            collect(or_(*conditions))
        for chunk in cls._chunks(andrey_external, cls.QUERY_CHUNK):
            collect(or_(*[
                MarketplaceListing.offer_id.ilike(
                    f"%A{cls._like_literal(value)}",
                    escape="\\",
                )
                for value in chunk
            ]))
        for chunk in cls._chunks(
            andrey_serials,
            cls.QUERY_CHUNK // 2,
        ):
            conditions = []
            for value in chunk:
                literal = cls._like_literal(value)
                conditions.extend((
                    MarketplaceListing.offer_id.ilike(
                        f"%A%{literal}",
                        escape="\\",
                    ),
                    MarketplaceListing.offer_id.ilike(
                        f"id-%{literal}-%A",
                        escape="\\",
                    ),
                ))
            collect(or_(*conditions))
        for chunk in cls._chunks(vendor_codes, cls.QUERY_CHUNK):
            collect(or_(*[
                MarketplaceListing.offer_id.ilike(
                    f"%V{cls._like_literal(value)}",
                    escape="\\",
                )
                for value in chunk
            ]))
        return list(rows.values())

    @staticmethod
    def _combined_identity_parts(
        parts: Mapping[str, Set[SourceIdentityKey]],
    ) -> Set[SourceIdentityKey]:
        return {
            key
            for values in parts.values()
            for key in values
        }

    @classmethod
    def _parsed_offer_context(
        cls,
        offers: Sequence[Optional[str]],
    ) -> tuple[
        Dict[str, tuple[ParsedSourceIdentity, ...]],
        Dict[SourceIdentityKey, Set[str]],
    ]:
        parsed_by_offer = {}
        offers_by_key: Dict[SourceIdentityKey, Set[str]] = {}
        for offer in offers:
            if not offer or offer in parsed_by_offer:
                continue
            parsed = parse_encoded_source_identities(offer)
            parsed_by_offer[offer] = parsed
            for item in parsed:
                offers_by_key.setdefault(item.key, set()).add(offer)
        return parsed_by_offer, offers_by_key

    @classmethod
    def _query_wb_identity_candidates(
        cls,
        *,
        seller_id: int,
        keys: Iterable[SourceIdentityKey],
    ) -> List[Product]:
        """Load a bounded superset; Python performs the final exact gate."""
        identities = set(keys)
        sex_ids = sorted({
            key.value for key in identities
            if key.source_code == "sexoptovik"
            and key.kind == "external_id"
        })
        andrey_serials = sorted({
            key.value for key in identities
            if key.source_code == "andrey" and key.kind == "serial"
        })
        andrey_external = sorted({
            key.value for key in identities
            if key.source_code == "andrey" and key.kind == "external_id"
        })
        vendor_codes = sorted({
            key.value for key in identities
            if key.source_code == "*" and key.kind == "vendor_code"
        })
        rows: Dict[int, Product] = {}

        for chunk in cls._chunks(sex_ids, cls.QUERY_CHUNK):
            filters = []
            for value in chunk:
                filters.extend((
                    Product.vendor_code.like(f"id-{value}-%"),
                    Product.vendor_code.like(f"%S{value}"),
                ))
            for row in Product.query.filter(
                Product.seller_id == seller_id,
                or_(*filters),
            ).all():
                rows[row.id] = row

        for chunk in cls._chunks(andrey_serials, cls.QUERY_CHUNK):
            filters = []
            for value in chunk:
                filters.extend((
                    Product.vendor_code.like(f"%{value}"),
                    Product.vendor_code.like(f"%{value}%A"),
                ))
            for row in Product.query.filter(
                Product.seller_id == seller_id,
                or_(*filters),
            ).all():
                rows[row.id] = row

        for chunk in cls._chunks(andrey_external, cls.QUERY_CHUNK):
            filters = [
                Product.vendor_code.ilike(f"%A{value}")
                for value in chunk
            ]
            for row in Product.query.filter(
                Product.seller_id == seller_id,
                or_(*filters),
            ).all():
                rows[row.id] = row

        for chunk in cls._chunks(vendor_codes, cls.QUERY_CHUNK):
            filters = [Product.supplier_vendor_code.ilike(value) for value in chunk]
            filters.extend(
                Product.vendor_code.ilike(f"%V{value}")
                for value in chunk
            )
            for row in Product.query.filter(
                Product.seller_id == seller_id,
                or_(*filters),
            ).all():
                rows[row.id] = row
        return list(rows.values())

    @classmethod
    def _query_supplier_identity_candidates(
        cls,
        *,
        keys: Iterable[SourceIdentityKey],
    ) -> List[SupplierProduct]:
        identities = set(keys)
        sex_ids = sorted({
            key.value for key in identities
            if key.source_code == "sexoptovik"
            and key.kind == "external_id"
        })
        andrey_serials = sorted({
            key.value for key in identities
            if key.source_code == "andrey" and key.kind == "serial"
        })
        andrey_external = sorted({
            key.value for key in identities
            if key.source_code == "andrey" and key.kind == "external_id"
        })
        vendor_codes = sorted({
            key.value for key in identities
            if key.source_code == "*" and key.kind == "vendor_code"
        })
        base = SupplierProduct.query.options(
            joinedload(SupplierProduct.supplier)
        ).join(Supplier, SupplierProduct.supplier_id == Supplier.id)
        rows: Dict[int, SupplierProduct] = {}

        for chunk in cls._chunks(sex_ids, cls.QUERY_CHUNK):
            for row in base.filter(
                Supplier.code == "sexoptovik",
                SupplierProduct.external_id.in_(chunk),
            ).all():
                rows[row.id] = row

        for chunk in cls._chunks(
            andrey_external,
            cls.QUERY_CHUNK,
        ):
            filters = [
                SupplierProduct.external_id.ilike(value)
                for value in chunk
            ]
            for row in base.filter(
                Supplier.code == "andrey",
                or_(*filters),
            ).all():
                rows[row.id] = row

        for chunk in cls._chunks(andrey_serials, cls.QUERY_CHUNK):
            filters = [
                SupplierProduct.external_id.like(f"%{value}")
                for value in chunk
            ]
            for row in base.filter(
                Supplier.code == "andrey",
                or_(*filters),
            ).all():
                rows[row.id] = row

        for chunk in cls._chunks(vendor_codes, cls.QUERY_CHUNK):
            filters = []
            for value in chunk:
                filters.extend((
                    SupplierProduct.vendor_code.ilike(value),
                    SupplierProduct.additional_vendor_code.ilike(value),
                ))
            for row in base.filter(or_(*filters)).all():
                rows[row.id] = row
        return list(rows.values())

    @classmethod
    def _query_import_identity_candidates(
        cls,
        *,
        seller_id: int,
        offers: Sequence[str],
        keys: Iterable[SourceIdentityKey],
        supplier_product_ids: Sequence[int],
        wb_product_ids: Sequence[int],
    ) -> List[ImportedProduct]:
        identities = set(keys)
        rows = {
            row.id: row
            for row in cls._exact_candidate_rows(
                seller_id=seller_id,
                offers=offers,
            )
        }
        sex_ids = sorted({
            key.value for key in identities
            if key.source_code == "sexoptovik"
            and key.kind == "external_id"
        })
        andrey_serials = sorted({
            key.value for key in identities
            if key.source_code == "andrey" and key.kind == "serial"
        })
        andrey_external = sorted({
            key.value for key in identities
            if key.source_code == "andrey" and key.kind == "external_id"
        })
        vendor_codes = sorted({
            key.value for key in identities
            if key.source_code == "*" and key.kind == "vendor_code"
        })
        base = cls._candidate_query(seller_id=seller_id)

        for chunk in cls._chunks(sex_ids, cls.QUERY_CHUNK):
            for row in base.filter(
                ImportedProduct.source_type.ilike("sexoptovik"),
                ImportedProduct.external_id.in_(chunk),
            ).all():
                rows[row.id] = row

        for chunk in cls._chunks(andrey_external, cls.QUERY_CHUNK):
            filters = [
                ImportedProduct.external_id.ilike(value)
                for value in chunk
            ]
            for row in base.filter(
                ImportedProduct.source_type.ilike("andrey"),
                or_(*filters),
            ).all():
                rows[row.id] = row

        for chunk in cls._chunks(andrey_serials, cls.QUERY_CHUNK):
            filters = [
                ImportedProduct.external_id.like(f"%{value}")
                for value in chunk
            ]
            for row in base.filter(
                ImportedProduct.source_type.ilike("andrey"),
                or_(*filters),
            ).all():
                rows[row.id] = row

        for chunk in cls._chunks(vendor_codes, cls.QUERY_CHUNK):
            filters = [
                ImportedProduct.external_vendor_code.ilike(value)
                for value in chunk
            ]
            for row in base.filter(or_(*filters)).all():
                rows[row.id] = row

        for chunk in cls._chunks(
            sorted(set(supplier_product_ids)),
            cls.QUERY_CHUNK * 5,
        ):
            for row in base.filter(
                ImportedProduct.supplier_product_id.in_(chunk)
            ).all():
                rows[row.id] = row

        for chunk in cls._chunks(
            sorted(set(wb_product_ids)),
            cls.QUERY_CHUNK * 5,
        ):
            for row in base.filter(
                ImportedProduct.product_id.in_(chunk)
            ).all():
                rows[row.id] = row
        return list(rows.values())

    @classmethod
    def _candidate_identity_sources(
        cls,
        parts: Mapping[str, Set[SourceIdentityKey]],
        offer_keys: Set[SourceIdentityKey],
    ) -> List[str]:
        return sorted(
            label
            for label, values in parts.items()
            if values & offer_keys
        )

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
        source: str = "exact_offer_identity",
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
        listing.link_source = source
        listing.link_evidence_json = encoded
        listing.link_version = max(int(listing.link_version or 0), 1) + 1
        listing.linked_at = None
        listing.linked_by_user_id = None
        cls._event(
            listing,
            previous_imported_product_id=None,
            action="ambiguous",
            source=source,
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
        allow_materialization: bool = False,
        include_seller_unlinked: bool = False,
        _materialization_lock_owned: bool = False,
    ) -> Dict[str, int]:
        """Auto-link a bounded exact seller set without title/AI inference.

        ``allow_materialization`` is reserved for a lock-owning, committing
        caller.  It may create the missing canonical source copy only when one
        unique connected-supplier row and one unique seller WB row prove the
        same parsed source identity.
        """
        seller_id = cls._positive_integer(seller_id, "seller_id")
        if not isinstance(commit, bool):
            raise MarketplaceProductLinkValidationError(
                "commit должен быть boolean"
            )
        if not isinstance(allow_materialization, bool):
            raise MarketplaceProductLinkValidationError(
                "allow_materialization должен быть boolean"
            )
        if not isinstance(include_seller_unlinked, bool):
            raise MarketplaceProductLinkValidationError(
                "include_seller_unlinked должен быть boolean"
            )
        if not isinstance(_materialization_lock_owned, bool):
            raise MarketplaceProductLinkValidationError(
                "_materialization_lock_owned должен быть boolean"
            )
        if allow_materialization and not commit:
            raise MarketplaceProductLinkValidationError(
                "Materialization требует commit под seller-scoped lock"
            )
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
            seller_unlinked = (
                row.link_source == "seller_unlink"
                and row.imported_product_id is None
            )
            if (
                code == "ozon"
                and row.account_id
                and row.imported_product_id is None
                and (include_seller_unlinked or not seller_unlinked)
            ):
                eligible.append(row)
        result = {
            "linked": 0,
            "materialized": 0,
            "wb_attached": 0,
            "ambiguous": 0,
            "unmatched": 0,
            "busy": 0,
        }
        if not eligible:
            return result
        if allow_materialization and not _materialization_lock_owned:
            from services.marketplace_operation_locks import (
                release_marketplace_source_link_lock,
                try_marketplace_source_link_lock,
            )

            lock_file = try_marketplace_source_link_lock(seller_id)
            if lock_file is None:
                result["busy"] = len(eligible)
                return result
            try:
                return cls.reconcile_objects(
                    seller_id=seller_id,
                    listings=eligible,
                    now=now,
                    commit=True,
                    allow_materialization=True,
                    include_seller_unlinked=include_seller_unlinked,
                    _materialization_lock_owned=True,
                )
            finally:
                release_marketplace_source_link_lock(lock_file)

        offers = [cls._identity(row.offer_id) for row in eligible]
        parsed_by_offer, offers_by_key = cls._parsed_offer_context(offers)
        offer_keys = set(offers_by_key)
        wb_rows = cls._query_wb_identity_candidates(
            seller_id=seller_id,
            keys=offer_keys,
        )
        supplier_rows = cls._query_supplier_identity_candidates(
            keys=offer_keys,
        )
        candidate_rows = cls._query_import_identity_candidates(
            seller_id=seller_id,
            offers=[value for value in offers if value],
            keys=offer_keys,
            supplier_product_ids=[
                row.id for row in supplier_rows if row.id is not None
            ],
            wb_product_ids=[
                row.id for row in wb_rows if row.id is not None
            ],
        )

        wb_by_offer: Dict[str, Dict[int, Dict[str, Any]]] = {}
        for wb_product in wb_rows:
            parts = cls._product_identity_parts(wb_product)
            for key in cls._combined_identity_parts(parts):
                for offer_id in offers_by_key.get(key, ()):
                    entry = wb_by_offer.setdefault(
                        offer_id,
                        {},
                    ).setdefault(wb_product.id, {
                        "product": wb_product,
                        "identity_keys": set(),
                        "identity_sources": set(),
                    })
                    entry["identity_keys"].add(key)
                    entry["identity_sources"].update(
                        cls._candidate_identity_sources(
                            parts,
                            {key},
                        )
                    )

        supplier_by_offer: Dict[str, Dict[int, Dict[str, Any]]] = {}
        for supplier_product in supplier_rows:
            parts = cls._supplier_identity_parts(supplier_product)
            for key in cls._combined_identity_parts(parts):
                for offer_id in offers_by_key.get(key, ()):
                    entry = supplier_by_offer.setdefault(
                        offer_id,
                        {},
                    ).setdefault(supplier_product.id, {
                        "product": supplier_product,
                        "identity_keys": set(),
                        "identity_sources": set(),
                    })
                    entry["identity_keys"].add(key)
                    entry["identity_sources"].update(
                        cls._candidate_identity_sources(
                            parts,
                            {key},
                        )
                    )

        exact_offers = {
            offer for offer in offers if offer
        }
        candidates_by_offer: Dict[str, Dict[int, Dict[str, Any]]] = {}
        for product in candidate_rows:
            parts = cls._import_identity_parts(product)
            candidate_keys = cls._combined_identity_parts(parts)
            matched_offers = {
                offer_id
                for key in candidate_keys
                for offer_id in offers_by_key.get(key, ())
            }
            raw_values = {
                cls._identity(product.external_vendor_code),
                cls._identity(product.external_id),
                cls._identity(
                    product.product.vendor_code
                    if product.product else None
                ),
                cls._identity(
                    product.product.supplier_vendor_code
                    if product.product else None
                ),
                cls._identity(
                    product.supplier_product.external_id
                    if product.supplier_product else None
                ),
                cls._identity(
                    product.supplier_product.vendor_code
                    if product.supplier_product else None
                ),
                cls._identity(
                    product.supplier_product.additional_vendor_code
                    if product.supplier_product else None
                ),
            } - {None}
            matched_offers.update(raw_values & exact_offers)
            for offer_id in matched_offers:
                entry = candidates_by_offer.setdefault(
                    offer_id,
                    {},
                ).setdefault(product.id, {
                        "product": product,
                        "fields": set(),
                        "identity_keys": set(),
                        "identity_sources": set(),
                    })
                entry["fields"].update(
                    cls._evidence_for_offer(product, offer_id)
                )
                matched_keys = {
                    parsed.key
                    for parsed in parsed_by_offer.get(offer_id, ())
                    if parsed.key in candidate_keys
                }
                entry["identity_keys"].update(matched_keys)
                entry["identity_sources"].update(
                    cls._candidate_identity_sources(parts, matched_keys)
                )

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

        active_supplier_ids = set()
        if allow_materialization and supplier_rows:
            active_supplier_ids = {
                row.supplier_id
                for row in SellerSupplier.query.filter(
                    SellerSupplier.seller_id == seller_id,
                    SellerSupplier.is_active.is_(True),
                    SellerSupplier.supplier_id.in_({
                        row.supplier_id for row in supplier_rows
                    }),
                ).all()
            }

        now = now or datetime.utcnow()
        for listing, offer_id in zip(eligible, offers):
            matches = candidates_by_offer.get(offer_id or "", {})
            wb_matches = wb_by_offer.get(offer_id or "", {})
            supplier_matches = supplier_by_offer.get(offer_id or "", {})
            parsed = parsed_by_offer.get(offer_id or "", ())
            parsed_match_source = bool(parsed)
            link_source = (
                "exact_source_identity"
                if parsed_match_source
                else "exact_offer_identity"
            )

            if not matches:
                if (
                    allow_materialization
                    and len(wb_matches) == 1
                    and len(supplier_matches) == 1
                ):
                    wb_product = next(iter(wb_matches.values()))["product"]
                    supplier_product = next(
                        iter(supplier_matches.values())
                    )["product"]
                    if supplier_product.supplier_id in active_supplier_ids:
                        existing = cls._candidate_query(
                            seller_id=seller_id
                        ).filter(or_(
                            ImportedProduct.product_id == wb_product.id,
                            ImportedProduct.supplier_product_id
                            == supplier_product.id,
                        )).all()
                        exact_existing = []
                        offer_key_set = {
                            item.key for item in parsed
                        }
                        for candidate in existing:
                            candidate_keys = cls._combined_identity_parts(
                                cls._import_identity_parts(candidate)
                            )
                            if candidate_keys & offer_key_set:
                                exact_existing.append(candidate)
                        if len(exact_existing) == 1:
                            product = exact_existing[0]
                        elif len(exact_existing) > 1:
                            matches = {
                                candidate.id: {
                                    "product": candidate,
                                    "fields": set(),
                                    "identity_keys": (
                                        cls._combined_identity_parts(
                                            cls._import_identity_parts(candidate)
                                        ) & offer_key_set
                                    ),
                                    "identity_sources": {
                                        "existing_exact_source_copy"
                                    },
                                }
                                for candidate in exact_existing
                            }
                            product = None
                        elif existing:
                            cls._mark_ambiguous(
                                listing,
                                evidence={
                                    "offer_id": offer_id,
                                    "reason": (
                                        "existing_canonical_relationship_conflict"
                                    ),
                                    "candidate_count": len(existing),
                                    "candidate_product_ids": sorted(
                                        candidate.id for candidate in existing
                                    )[:cls.MAX_EVIDENCE_CANDIDATES],
                                    "wb_product_id": wb_product.id,
                                    "supplier_product_id": supplier_product.id,
                                },
                                now=now,
                                source=link_source,
                            )
                            result["ambiguous"] += 1
                            continue
                        else:
                            from services.supplier_service import SupplierService

                            product = (
                                SupplierService
                                .build_imported_product_from_supplier(
                                    seller_id,
                                    supplier_product,
                                )
                            )
                            product.product_id = wb_product.id
                            product.wb_nm_id = wb_product.nm_id
                            product.import_status = "imported"
                            product.import_error = None
                            product.imported_at = now
                            db.session.add(product)
                            db.session.flush()
                            result["materialized"] += 1
                        if product is not None:
                            matches = {
                                product.id: {
                                    "product": product,
                                    "fields": set(),
                                    "identity_keys": {
                                        item.key for item in parsed
                                    },
                                    "identity_sources": {
                                        "supplier.source_identity",
                                        "wb.encoded_vendor_code",
                                    },
                                }
                            }
                            candidates_by_offer.setdefault(
                                offer_id or "",
                                {},
                            ).update(matches)

                if not matches:
                    if len(wb_matches) > 1 or len(supplier_matches) > 1:
                        cls._mark_ambiguous(
                            listing,
                            evidence={
                                "offer_id": offer_id,
                                "reason": "multiple_exact_source_candidates",
                                "wb_candidate_count": len(wb_matches),
                                "wb_product_ids": sorted(wb_matches)[
                                    :cls.MAX_EVIDENCE_CANDIDATES
                                ],
                                "supplier_candidate_count": len(
                                    supplier_matches
                                ),
                                "supplier_product_ids": sorted(
                                    supplier_matches
                                )[:cls.MAX_EVIDENCE_CANDIDATES],
                                "parsed_identities": [
                                    item.to_evidence() for item in parsed
                                ],
                            },
                            now=now,
                            source=link_source,
                        )
                        result["ambiguous"] += 1
                    else:
                        result["unmatched"] += 1
                    continue

            match = None
            if len(matches) == 1:
                match = next(iter(matches.values()))
            else:
                preferred_ids = []
                if len(wb_matches) == 1:
                    wb_product_id = next(iter(wb_matches))
                    aligned = {
                        product_id for product_id, value in matches.items()
                        if value["product"].product_id == wb_product_id
                    }
                    if len(aligned) == 1:
                        preferred_ids.append(next(iter(aligned)))
                if len(supplier_matches) == 1:
                    supplier_product_id = next(iter(supplier_matches))
                    aligned = {
                        product_id for product_id, value in matches.items()
                        if value["product"].supplier_product_id
                        == supplier_product_id
                    }
                    if len(aligned) == 1:
                        preferred_ids.append(next(iter(aligned)))
                if (
                    preferred_ids
                    and len(set(preferred_ids)) == 1
                ):
                    match = matches[preferred_ids[0]]

            if match is None:
                cls._mark_ambiguous(
                    listing,
                    evidence={
                        "offer_id": offer_id,
                        "candidate_count": len(matches),
                        "candidate_product_ids": sorted(matches)[
                            :cls.MAX_EVIDENCE_CANDIDATES
                        ],
                        "reason": "multiple_exact_internal_identities",
                        "parsed_identities": [
                            item.to_evidence() for item in parsed
                        ],
                    },
                    now=now,
                    source=link_source,
                )
                result["ambiguous"] += 1
                continue

            product = match["product"]
            matched_wb_ids = set(wb_matches)
            if (
                product.product_id is not None
                and matched_wb_ids
                and product.product_id not in matched_wb_ids
            ):
                cls._mark_ambiguous(
                    listing,
                    evidence={
                        "offer_id": offer_id,
                        "candidate_product_ids": [product.id],
                        "canonical_wb_product_id": product.product_id,
                        "matched_wb_product_ids": sorted(matched_wb_ids)[
                            :cls.MAX_EVIDENCE_CANDIDATES
                        ],
                        "reason": "canonical_wb_source_identity_conflict",
                        "parsed_identities": [
                            item.to_evidence() for item in parsed
                        ],
                    },
                    now=now,
                    source=link_source,
                )
                result["ambiguous"] += 1
                continue

            if (
                allow_materialization
                and product.product_id is None
                and len(wb_matches) == 1
            ):
                wb_product = next(iter(wb_matches.values()))["product"]
                conflicting_import = ImportedProduct.query.filter(
                    ImportedProduct.seller_id == seller_id,
                    ImportedProduct.product_id == wb_product.id,
                    ImportedProduct.id != product.id,
                ).first()
                if conflicting_import is not None:
                    cls._mark_ambiguous(
                        listing,
                        evidence={
                            "offer_id": offer_id,
                            "candidate_product_ids": [product.id],
                            "conflicting_imported_product_id": (
                                conflicting_import.id
                            ),
                            "wb_product_id": wb_product.id,
                            "reason": "wb_product_already_has_canonical_source",
                        },
                        now=now,
                        source=link_source,
                    )
                    result["ambiguous"] += 1
                    continue
                product.product_id = wb_product.id
                product.wb_nm_id = wb_product.nm_id
                product.import_status = "imported"
                product.import_error = None
                product.imported_at = product.imported_at or now
                result["wb_attached"] += 1

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
                    source=link_source,
                )
                result["ambiguous"] += 1
                continue

            identity_matches = [
                item.to_evidence()
                for item in parsed
                if item.key in match["identity_keys"]
            ]
            successful_link_source = (
                "exact_source_identity"
                if match["identity_keys"]
                else "exact_offer_identity"
            )
            cls._apply_link(
                listing,
                product,
                action="auto_link",
                source=successful_link_source,
                evidence={
                    "offer_id": offer_id,
                    "matched_fields": sorted(match["fields"]),
                    "matched_identity_sources": sorted(
                        match["identity_sources"]
                    ),
                    "parsed_identities": identity_matches,
                    "wb_product_id": product.product_id,
                    "supplier_product_id": product.supplier_product_id,
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
    def reconcile_account_products(
        cls,
        *,
        seller_id: int,
        account_id: int,
        products: Sequence[ImportedProduct],
        now: Optional[datetime] = None,
    ) -> dict:
        """Run the selected-source account reconciliation and commit it."""
        return cls._reconcile_account_products_impl(
            seller_id=seller_id,
            account_id=account_id,
            products=products,
            now=now,
            commit=True,
        )

    @classmethod
    def _reconcile_account_products_impl(
        cls,
        *,
        seller_id: int,
        account_id: int,
        products: Sequence[ImportedProduct],
        now: Optional[datetime] = None,
        commit: bool,
        guard_seller_unlinked_siblings: bool = False,
    ) -> dict:
        """Resolve existing Ozon listings before a selected create/update flow.

        The method performs local exact matching only.  When an equivalent
        Ozon offer exists but cannot be linked uniquely, the affected selected
        product is returned in ``blocked`` so callers cannot create a duplicate
        offer under a different seller suffix.  The private caller-owned mode
        is used only by the atomic existing-linked draft-create path; its
        caller must inspect counters/session mutations and own rollback/commit.
        """
        if not isinstance(commit, bool):
            raise MarketplaceProductLinkValidationError(
                "commit должен быть boolean"
            )
        if not isinstance(guard_seller_unlinked_siblings, bool):
            raise MarketplaceProductLinkValidationError(
                "guard_seller_unlinked_siblings должен быть boolean"
            )
        seller_id = cls._positive_integer(seller_id, "seller_id")
        account_id = cls._positive_integer(account_id, "account_id")
        rows = list(products)
        if not rows or len(rows) > cls.MAX_TARGET_PRODUCTS:
            raise MarketplaceProductLinkValidationError(
                f"Разрешено от 1 до {cls.MAX_TARGET_PRODUCTS} товаров"
            )
        product_ids = []
        for product in rows:
            if (
                not isinstance(product, ImportedProduct)
                or product.id is None
                or product.seller_id != seller_id
            ):
                raise MarketplaceProductLinkNotFound(
                    "Внутренняя карточка не найдена"
                )
            product_ids.append(product.id)
        if len(product_ids) != len(set(product_ids)):
            raise MarketplaceProductLinkValidationError(
                "Набор товаров содержит дубли"
            )

        account = SellerMarketplaceAccount.query.options(
            joinedload(SellerMarketplaceAccount.marketplace),
        ).filter_by(
            id=account_id,
            seller_id=seller_id,
        ).first()
        if (
            account is None
            or account.marketplace is None
            or account.marketplace.code != "ozon"
        ):
            raise MarketplaceProductLinkNotFound("Кабинет Ozon не найден")

        candidates = cls._target_listing_candidates(
            seller_id=seller_id,
            account=account,
            products=rows,
        )
        reconcile_result = cls.reconcile_objects(
            seller_id=seller_id,
            listings=candidates,
            now=now,
            commit=commit,
        )

        linked_rows = MarketplaceListing.query.filter(
            MarketplaceListing.seller_id == seller_id,
            MarketplaceListing.marketplace_id == account.marketplace_id,
            MarketplaceListing.account_id == account.id,
            MarketplaceListing.imported_product_id.in_(product_ids),
        ).order_by(MarketplaceListing.id.asc()).all()
        resolved = {}
        for listing in linked_rows:
            product_id = listing.imported_product_id
            if product_id in resolved:
                raise MarketplaceProductLinkConflict(
                    "Одна внутренняя карточка связана с несколькими "
                    "листингами одного кабинета Ozon"
                )
            resolved[product_id] = listing.id

        products_by_key: Dict[SourceIdentityKey, Set[int]] = {}
        products_by_raw: Dict[str, Set[int]] = {}
        for product in rows:
            for key in cls._combined_identity_parts(
                cls._import_identity_parts(product)
            ):
                products_by_key.setdefault(key, set()).add(product.id)
            for value in cls._raw_product_identities(product):
                products_by_raw.setdefault(value, set()).add(product.id)

        relevant: Dict[int, List[MarketplaceListing]] = {
            product_id: [] for product_id in product_ids
        }
        for listing in candidates:
            matched_ids = set(products_by_raw.get(listing.offer_id, ()))
            for key in encoded_record_identities(listing.offer_id):
                matched_ids.update(products_by_key.get(key, ()))
            for product_id in matched_ids:
                relevant[product_id].append(listing)

        blocked = {}
        seller_unlinked_siblings = {}
        for product_id in product_ids:
            if guard_seller_unlinked_siblings:
                sibling_ids = sorted({
                    listing.id
                    for listing in relevant[product_id]
                    if (
                        listing.id != resolved.get(product_id)
                        and listing.imported_product_id is None
                        and listing.link_source == "seller_unlink"
                    )
                })
                if sibling_ids:
                    seller_unlinked_siblings[product_id] = sibling_ids[
                        :cls.MAX_EVIDENCE_CANDIDATES
                    ]
            if product_id in resolved or not relevant[product_id]:
                continue
            listing_ids = sorted({
                listing.id for listing in relevant[product_id]
            })
            blocked[product_id] = {
                "code": "existing_ozon_listing_link_unresolved",
                "message": (
                    "В Ozon уже есть карточка с тем же source ID, но её "
                    "точную связь нельзя подтвердить автоматически. "
                    "Создание дубля остановлено; проверьте связь листинга."
                ),
                "listing_ids": listing_ids[
                    :cls.MAX_EVIDENCE_CANDIDATES
                ],
            }
        result = {
            **reconcile_result,
            "candidate_count": len(candidates),
            "resolved_listing_ids": resolved,
            "blocked": blocked,
        }
        if guard_seller_unlinked_siblings:
            result["unresolved_seller_unlink_listing_ids"] = (
                seller_unlinked_siblings
            )
        return result

    @classmethod
    def reconcile_listing(
        cls,
        *,
        seller_id: int,
        listing_id: int,
    ) -> MarketplaceListing:
        listing, _outcome = cls.reconcile_listing_with_outcome(
            seller_id=seller_id,
            listing_id=listing_id,
        )
        return listing

    @classmethod
    def reconcile_listing_with_outcome(
        cls,
        *,
        seller_id: int,
        listing_id: int,
    ) -> Tuple[MarketplaceListing, Dict[str, int]]:
        """Тот же reconcile, но с counters: busy-lock не выдаётся за «нет совпадений»."""
        listing = cls._owned_listing(
            seller_id=seller_id,
            listing_id=listing_id,
        )
        outcome = cls.reconcile_objects(
            seller_id=seller_id,
            listings=[listing],
            commit=True,
            allow_materialization=True,
            include_seller_unlinked=True,
        )
        refreshed = cls._owned_listing(
            seller_id=seller_id,
            listing_id=listing_id,
        )
        return refreshed, outcome if isinstance(outcome, dict) else {}

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
        from services.source_photo_display import imported_photo_previews

        wb = product.product
        supplier = product.supplier_product
        photos = imported_photo_previews(product)
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
            "has_source_photos": bool(photos),
            "photo_preview_url": next(iter(photos.values()), None),
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
            exact_imported_id = int(term) if term.isascii() and term.isdigit() else None
            if exact_imported_id is not None:
                filters.append(ImportedProduct.id == exact_imported_id)
                filters.append(Product.nm_id == exact_imported_id)
            order = [
                ImportedProduct.updated_at.desc(),
                ImportedProduct.id.desc(),
            ]
            if exact_imported_id is not None:
                # Put an exact internal ID inside the bounded page even when
                # many newer titles/vendor codes contain the same digits.
                order.insert(0, case(
                    (ImportedProduct.id == exact_imported_id, 0), else_=1,
                ))
            products = cls._candidate_query(seller_id=seller_id).filter(
                or_(*filters)
            ).order_by(*order).limit(limit).all()
        unique = {
            product.id: product for product in products
            if cls._candidate_fk_valid(product)
        }
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
        bound_draft = None
        if listing.imported_product_id is not None:
            bound_draft = MarketplaceProductDraft.query.filter_by(
                seller_id=seller_id,
                published_listing_id=listing.id,
            ).first()
        ozon = bool(listing.marketplace and listing.marketplace.code == "ozon")
        return {
            "canonical_product": (
                cls._candidate_summary(listing.imported_product)
                if listing.imported_product and cls._candidate_fk_valid(listing.imported_product)
                else None
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
            "actions": {
                "can_link": ozon and listing.imported_product_id is None,
                "can_unlink": ozon and listing.imported_product_id is not None and bound_draft is None,
                "unlink_reason": (
                    "Связь используется Ozon-черновиком; сначала завершите или архивируйте его"
                    if bound_draft is not None else None
                ),
                "bound_draft_id": bound_draft.id if bound_draft is not None else None,
                "link_version": listing.link_version,
            },
        }
