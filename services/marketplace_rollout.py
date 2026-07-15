"""Production rollout controls for the WB -> MarketplaceListing strangler.

The legacy ``Product`` table remains the WB write model.  This module owns the
bounded compatibility projection, durable dual-read parity sweeps and the
fail-safe cutover decision used by the WB catalog list.  It performs no
marketplace or LLM calls.
"""

from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
import hashlib
import json
import secrets

from sqlalchemy import and_, func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import aliased

from models import (
    ImportedProduct,
    Marketplace,
    MarketplaceListing,
    MarketplaceListingLinkEvent,
    MarketplaceProjectionRun,
    Product,
    db,
)


class MarketplaceRolloutError(RuntimeError):
    status_code = 400
    code = "marketplace_rollout_error"


class MarketplaceRolloutValidationError(MarketplaceRolloutError):
    code = "invalid_marketplace_rollout_request"


class MarketplaceRolloutBusy(MarketplaceRolloutError):
    status_code = 409
    code = "marketplace_rollout_busy"


class MarketplaceRolloutConflict(MarketplaceRolloutError):
    status_code = 409
    code = "marketplace_projection_conflict"


class MarketplaceRolloutNotFound(MarketplaceRolloutError):
    status_code = 404
    code = "marketplace_rollout_not_found"


class MarketplaceRolloutService:
    """Own bounded backfill, parity metrics and guarded common-read cutover."""

    MAX_BATCH = 200
    MAX_SELLERS_PER_TICK = 10
    LEASE_TTL = timedelta(minutes=3)
    REFRESH_AFTER = timedelta(hours=6)
    FAILED_RETRY_AFTER = timedelta(minutes=15)
    MAX_DESCRIPTION_CHARS = 100_000
    MAX_JSON_BYTES = 262_144
    MAX_MISMATCH_SAMPLE = 20
    ACTIVE_STATUSES = ("pending", "running", "paused")
    PROJECTION_FIELDS = (
        "offer_id",
        "external_product_id",
        "identifiers_json",
        "external_category_id",
        "title",
        "description",
        "normalized_status",
        "provider_status",
        "visibility",
        "is_archived",
        "is_available",
        "attributes_json",
        "media_json",
        "dimensions_json",
        "price_summary_json",
        "stock_summary_json",
        "list_synced_at",
        "last_seen_at",
        "sync_fingerprint",
    )

    @staticmethod
    def _positive_integer(value: Any, field_name: str) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise MarketplaceRolloutValidationError(
                f"{field_name} должен быть положительным целым числом"
            )
        return value

    @classmethod
    def _batch_limit(cls, value: Any) -> int:
        parsed = cls._positive_integer(value, "limit")
        if parsed > cls.MAX_BATCH:
            raise MarketplaceRolloutValidationError(
                f"limit не может быть больше {cls.MAX_BATCH}"
            )
        return parsed

    @staticmethod
    def _canonical_json(value: Any, fallback: Any) -> str:
        if not isinstance(value, type(fallback)):
            value = fallback
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if len(encoded.encode("utf-8")) > MarketplaceRolloutService.MAX_JSON_BYTES:
            encoded = json.dumps(
                fallback,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        return encoded

    @classmethod
    def _legacy_json(cls, raw: Any, fallback: Any) -> str:
        if raw in (None, ""):
            return cls._canonical_json(fallback, fallback)
        try:
            value = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError):
            value = fallback
        return cls._canonical_json(value, fallback)

    @staticmethod
    def _text(value: Any, maximum: int) -> Optional[str]:
        if value is None:
            return None
        normalized = str(value).strip()
        return normalized[:maximum] if normalized else None

    @staticmethod
    def _fingerprint(payload: Mapping[str, Any]) -> str:
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    @staticmethod
    def _wb_marketplace() -> Marketplace:
        marketplace = Marketplace.query.filter_by(code="wb").first()
        if marketplace is None:
            raise MarketplaceRolloutConflict(
                "Определение marketplace WB отсутствует"
            )
        return marketplace

    @classmethod
    def _expected_projection(
        cls,
        product: Product,
        *,
        imported_product_id: Optional[int],
    ) -> Dict[str, Any]:
        nm_id = str(product.nm_id)
        offer_id = cls._text(product.vendor_code, 200) or f"wb-nm-{nm_id}"
        active = bool(product.is_active)
        identifiers = {"nm_id": nm_id}
        if product.imt_id is not None:
            identifiers["imt_id"] = str(product.imt_id)
        identifiers_json = cls._canonical_json(identifiers, {})
        media_json = cls._canonical_json(
            {
                "photos": json.loads(
                    cls._legacy_json(product.photos_json, [])
                )
            },
            {},
        )
        attributes_json = cls._legacy_json(
            product.characteristics_json,
            [],
        )
        dimensions_json = cls._legacy_json(product.dimensions_json, {})
        price_summary = {
            "available": product.price is not None,
            "currency": "RUB",
            "price": str(product.price) if product.price is not None else None,
            "discount_price": (
                str(product.discount_price)
                if product.discount_price is not None
                else None
            ),
            "source": "legacy_wb_projection",
        }
        stock_summary = {
            "available": True,
            "present": int(product.quantity or 0),
            "source": "legacy_wb_projection",
        }
        category_id = (
            str(product.subject_id) if product.subject_id is not None else None
        )
        status = "active" if active else "inactive"
        snapshot = {
            "offer_id": offer_id,
            "external_product_id": nm_id,
            "title": cls._text(product.title, 500),
            "category_id": category_id,
            "status": status,
            "identifiers": identifiers,
            "media": json.loads(media_json),
            "attributes": json.loads(attributes_json),
            "dimensions": json.loads(dimensions_json),
            "price": price_summary,
            "stock": stock_summary,
        }
        return {
            "seller_id": product.seller_id,
            "legacy_product_id": product.id,
            "imported_product_id": imported_product_id,
            "offer_id": offer_id,
            "external_product_id": nm_id,
            "identifiers_json": identifiers_json,
            "external_category_id": category_id,
            "title": cls._text(product.title, 500),
            "description": cls._text(
                product.description,
                cls.MAX_DESCRIPTION_CHARS,
            ),
            "normalized_status": status,
            "provider_status": "legacy_wb_projection",
            "visibility": status,
            "is_archived": False,
            "is_available": True,
            "attributes_json": attributes_json,
            "media_json": media_json,
            "dimensions_json": dimensions_json,
            "price_summary_json": cls._canonical_json(price_summary, {}),
            "stock_summary_json": cls._canonical_json(stock_summary, {}),
            "list_synced_at": product.last_sync,
            "last_seen_at": (
                product.last_sync or product.updated_at or product.created_at
            ),
            "sync_fingerprint": cls._fingerprint(snapshot),
        }

    @staticmethod
    def _latest_imported_by_product(
        *,
        seller_id: int,
        product_ids: Sequence[int],
    ) -> Dict[int, int]:
        if not product_ids:
            return {}
        result: Dict[int, int] = {}
        rows = ImportedProduct.query.filter(
            ImportedProduct.seller_id == seller_id,
            ImportedProduct.product_id.in_(product_ids),
        ).order_by(
            ImportedProduct.product_id.asc(),
            ImportedProduct.id.desc(),
        ).all()
        for row in rows:
            if row.product_id is not None:
                result.setdefault(int(row.product_id), int(row.id))
        return result

    @staticmethod
    def _link_event(
        listing: MarketplaceListing,
        *,
        action: str,
        source: str,
        previous_imported_product_id: Optional[int],
        evidence: Mapping[str, Any],
    ) -> None:
        db.session.add(MarketplaceListingLinkEvent(
            seller_id=listing.seller_id,
            marketplace_id=listing.marketplace_id,
            account_id=None,
            listing_id=listing.id,
            previous_imported_product_id=previous_imported_product_id,
            imported_product_id=listing.imported_product_id,
            action=action,
            source=source,
            evidence_json=MarketplaceRolloutService._canonical_json(
                dict(evidence),
                {},
            ),
            actor_user_id=None,
            link_version=listing.link_version,
        ))

    @classmethod
    def _apply_product(
        cls,
        *,
        marketplace: Marketplace,
        product: Product,
        imported_product_id: Optional[int],
        listing: Optional[MarketplaceListing],
        now: datetime,
    ) -> str:
        expected = cls._expected_projection(
            product,
            imported_product_id=imported_product_id,
        )
        if listing is None:
            listing = MarketplaceListing(
                marketplace_id=marketplace.id,
                account_id=None,
                primary_sku=None,
                external_type_id=None,
                product_type_id=None,
                last_catalog_sync_id=None,
                last_catalog_sync_phase=None,
                statuses_json="{}",
                moderation_errors_json="[]",
                complex_attributes_json="[]",
                barcodes_json="[]",
                has_fbo_stocks=False,
                has_fbs_stocks=False,
                link_status=(
                    "linked" if imported_product_id is not None else "unlinked"
                ),
                link_source=(
                    "wb_backfill" if imported_product_id is not None else None
                ),
                link_evidence_json="{}",
                link_version=1,
                linked_at=now if imported_product_id is not None else None,
                created_at=now,
                updated_at=now,
                **expected,
            )
            db.session.add(listing)
            db.session.flush()
            if imported_product_id is not None:
                cls._link_event(
                    listing,
                    action="bootstrap",
                    source="wb_backfill",
                    previous_imported_product_id=None,
                    evidence={},
                )
            return "inserted"

        if (
            listing.seller_id != product.seller_id
            or listing.marketplace_id != marketplace.id
            or listing.account_id is not None
        ):
            raise MarketplaceRolloutConflict(
                f"WB projection identity conflict for Product.id={product.id}"
            )

        changed = False
        for field_name in cls.PROJECTION_FIELDS:
            value = expected[field_name]
            if getattr(listing, field_name) != value:
                setattr(listing, field_name, value)
                changed = True

        # A direct ImportedProduct.product_id FK is deterministic evidence.  A
        # conflicting existing non-null link is never overwritten silently;
        # the parity run keeps cutover blocked for manual investigation.
        if imported_product_id is not None and listing.imported_product_id is None:
            previous = listing.imported_product_id
            listing.imported_product_id = imported_product_id
            listing.link_status = "linked"
            listing.link_source = "wb_product_fk"
            listing.link_evidence_json = cls._canonical_json(
                {"product_id": product.id},
                {},
            )
            listing.link_version = max(int(listing.link_version or 0), 1) + 1
            listing.linked_at = now
            listing.linked_by_user_id = None
            db.session.flush()
            cls._link_event(
                listing,
                action="auto_link",
                source="wb_product_fk",
                previous_imported_product_id=previous,
                evidence={"product_id": product.id},
            )
            changed = True
        elif (
            imported_product_id is not None
            and listing.imported_product_id == imported_product_id
            and listing.link_status != "linked"
        ):
            # The FK already carries deterministic seller-owned evidence, so
            # repairing only its stale metadata is safe and remains audited.
            listing.link_status = "linked"
            listing.link_source = "wb_product_fk"
            listing.link_evidence_json = cls._canonical_json(
                {"product_id": product.id, "metadata_repair": True},
                {},
            )
            listing.link_version = max(int(listing.link_version or 0), 1) + 1
            listing.linked_at = now
            listing.linked_by_user_id = None
            db.session.flush()
            cls._link_event(
                listing,
                action="auto_link",
                source="wb_product_fk",
                previous_imported_product_id=imported_product_id,
                evidence={"product_id": product.id, "metadata_repair": True},
            )
            changed = True

        if changed:
            listing.updated_at = now

        return "updated" if changed else "unchanged"

    @staticmethod
    def _latest_run(
        *,
        seller_id: int,
        marketplace_id: int,
        run_kind: str,
        statuses: Optional[Iterable[str]] = None,
    ) -> Optional[MarketplaceProjectionRun]:
        query = MarketplaceProjectionRun.query.filter_by(
            seller_id=seller_id,
            marketplace_id=marketplace_id,
            run_kind=run_kind,
        )
        if statuses is not None:
            query = query.filter(MarketplaceProjectionRun.status.in_(tuple(statuses)))
        return query.order_by(MarketplaceProjectionRun.id.desc()).first()

    @classmethod
    def _active_run(
        cls,
        *,
        seller_id: int,
        marketplace_id: int,
        run_kind: str,
    ) -> Optional[MarketplaceProjectionRun]:
        return cls._latest_run(
            seller_id=seller_id,
            marketplace_id=marketplace_id,
            run_kind=run_kind,
            statuses=cls.ACTIVE_STATUSES,
        )

    @classmethod
    def _product_watermark(cls, seller_id: int) -> Tuple[int, int]:
        count, maximum = db.session.query(
            func.count(Product.id),
            func.max(Product.id),
        ).filter(Product.seller_id == seller_id).one()
        return int(count or 0), int(maximum or 0)

    @classmethod
    def _latest_source_changed_at(cls, seller_id: int) -> Optional[datetime]:
        return db.session.query(func.max(func.coalesce(
            Product.updated_at,
            Product.created_at,
        ))).filter(Product.seller_id == seller_id).scalar()

    @classmethod
    def _latest_projection_changed_at(
        cls,
        *,
        seller_id: int,
        marketplace_id: int,
    ) -> Optional[datetime]:
        return db.session.query(func.max(func.coalesce(
            MarketplaceListing.updated_at,
            MarketplaceListing.created_at,
        ))).filter(
            MarketplaceListing.seller_id == seller_id,
            MarketplaceListing.marketplace_id == marketplace_id,
            MarketplaceListing.account_id.is_(None),
            MarketplaceListing.legacy_product_id.isnot(None),
        ).scalar()

    @classmethod
    def _first_missing_product_id(
        cls,
        *,
        seller_id: int,
        marketplace_id: int,
    ) -> Optional[int]:
        row = db.session.query(Product.id).outerjoin(
            MarketplaceListing,
            and_(
                MarketplaceListing.legacy_product_id == Product.id,
                MarketplaceListing.seller_id == seller_id,
                MarketplaceListing.marketplace_id == marketplace_id,
                MarketplaceListing.account_id.is_(None),
            ),
        ).filter(
            Product.seller_id == seller_id,
            MarketplaceListing.id.is_(None),
        ).order_by(Product.id.asc()).first()
        return int(row[0]) if row is not None else None

    @classmethod
    def _first_link_mismatch_product_id(
        cls,
        *,
        seller_id: int,
        marketplace_id: int,
    ) -> Optional[int]:
        latest_imported = db.session.query(
            ImportedProduct.product_id.label("product_id"),
            func.max(ImportedProduct.id).label("imported_product_id"),
        ).filter(
            ImportedProduct.seller_id == seller_id,
            ImportedProduct.product_id.isnot(None),
        ).group_by(ImportedProduct.product_id).subquery()
        row = db.session.query(Product.id).join(
            MarketplaceListing,
            and_(
                MarketplaceListing.legacy_product_id == Product.id,
                MarketplaceListing.seller_id == seller_id,
                MarketplaceListing.marketplace_id == marketplace_id,
                MarketplaceListing.account_id.is_(None),
            ),
        ).outerjoin(
            latest_imported,
            latest_imported.c.product_id == Product.id,
        ).filter(
            Product.seller_id == seller_id,
            or_(
                and_(
                    latest_imported.c.imported_product_id.is_(None),
                    or_(
                        MarketplaceListing.imported_product_id.isnot(None),
                        MarketplaceListing.link_status != "unlinked",
                    ),
                ),
                and_(
                    latest_imported.c.imported_product_id.isnot(None),
                    or_(
                        MarketplaceListing.imported_product_id.is_(None),
                        MarketplaceListing.imported_product_id
                        != latest_imported.c.imported_product_id,
                        MarketplaceListing.link_status != "linked",
                    ),
                ),
            ),
        ).order_by(Product.id.asc()).first()
        return int(row[0]) if row is not None else None

    @classmethod
    def _create_run(
        cls,
        *,
        seller_id: int,
        marketplace_id: int,
        run_kind: str,
        cursor_product_id: int,
        target_product_id: int,
        now: datetime,
    ) -> MarketplaceProjectionRun:
        run = MarketplaceProjectionRun(
            seller_id=seller_id,
            marketplace_id=marketplace_id,
            run_kind=run_kind,
            status="pending",
            cursor_product_id=max(int(cursor_product_id), 0),
            target_product_id=max(int(target_product_id), 0),
            started_at=now,
            heartbeat_at=now,
        )
        db.session.add(run)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            existing = cls._active_run(
                seller_id=seller_id,
                marketplace_id=marketplace_id,
                run_kind=run_kind,
            )
            if existing is None:
                raise MarketplaceRolloutBusy(
                    "Projection run changed concurrently"
                ) from None
            return existing
        return run

    @classmethod
    def _ensure_backfill_run(
        cls,
        *,
        seller_id: int,
        marketplace: Marketplace,
        force_full: bool,
        now: datetime,
    ) -> Optional[MarketplaceProjectionRun]:
        active = cls._active_run(
            seller_id=seller_id,
            marketplace_id=marketplace.id,
            run_kind="wb_backfill",
        )
        if active is not None:
            return active
        count, target = cls._product_watermark(seller_id)
        if not count:
            return None

        latest = cls._latest_run(
            seller_id=seller_id,
            marketplace_id=marketplace.id,
            run_kind="wb_backfill",
            statuses=("completed",),
        )
        failed = cls._latest_run(
            seller_id=seller_id,
            marketplace_id=marketplace.id,
            run_kind="wb_backfill",
            statuses=("failed",),
        )
        if (
            failed is not None
            and (latest is None or failed.id > latest.id)
            and not force_full
            and failed.updated_at
            and failed.updated_at > now - cls.FAILED_RETRY_AFTER
        ):
            return failed

        first_missing = cls._first_missing_product_id(
            seller_id=seller_id,
            marketplace_id=marketplace.id,
        )
        first_link_mismatch = cls._first_link_mismatch_product_id(
            seller_id=seller_id,
            marketplace_id=marketplace.id,
        )
        parity = cls._latest_run(
            seller_id=seller_id,
            marketplace_id=marketplace.id,
            run_kind="wb_parity",
            statuses=("completed",),
        )
        repair_required = bool(
            parity
            and (parity.missing_count or parity.mismatched_count)
            and (latest is None or parity.completed_at >= latest.completed_at)
        )
        refresh_due = bool(
            latest is None
            or latest.completed_at is None
            or latest.completed_at <= now - cls.REFRESH_AFTER
        )
        source_changed_at = cls._latest_source_changed_at(seller_id)
        if (
            latest is not None
            and latest.completed_at is not None
            and source_changed_at is not None
            and source_changed_at > latest.started_at
        ):
            refresh_due = True
        if not (
            force_full
            or first_missing is not None
            or first_link_mismatch is not None
            or repair_required
            or refresh_due
        ):
            return latest
        cursor = 0
        first_repair = min(
            value
            for value in (first_missing, first_link_mismatch)
            if value is not None
        ) if (first_missing is not None or first_link_mismatch is not None) else None
        if first_repair is not None and not (
            force_full or repair_required or refresh_due
        ):
            cursor = max(first_repair - 1, 0)
        return cls._create_run(
            seller_id=seller_id,
            marketplace_id=marketplace.id,
            run_kind="wb_backfill",
            cursor_product_id=cursor,
            target_product_id=target,
            now=now,
        )

    @classmethod
    def _ensure_parity_run(
        cls,
        *,
        seller_id: int,
        marketplace: Marketplace,
        force_full: bool,
        now: datetime,
    ) -> Optional[MarketplaceProjectionRun]:
        active = cls._active_run(
            seller_id=seller_id,
            marketplace_id=marketplace.id,
            run_kind="wb_parity",
        )
        if active is not None:
            return active
        count, target = cls._product_watermark(seller_id)
        if not count:
            return None
        if cls._first_missing_product_id(
            seller_id=seller_id,
            marketplace_id=marketplace.id,
        ) is not None:
            return None
        backfill = cls._latest_run(
            seller_id=seller_id,
            marketplace_id=marketplace.id,
            run_kind="wb_backfill",
            statuses=("completed",),
        )
        if backfill is None:
            return None
        latest = cls._latest_run(
            seller_id=seller_id,
            marketplace_id=marketplace.id,
            run_kind="wb_parity",
            statuses=("completed",),
        )
        failed = cls._latest_run(
            seller_id=seller_id,
            marketplace_id=marketplace.id,
            run_kind="wb_parity",
            statuses=("failed",),
        )
        if (
            failed is not None
            and (latest is None or failed.id > latest.id)
            and not force_full
            and failed.updated_at
            and failed.updated_at > now - cls.FAILED_RETRY_AFTER
        ):
            return failed
        projection_changed_at = cls._latest_projection_changed_at(
            seller_id=seller_id,
            marketplace_id=marketplace.id,
        )
        due = bool(
            force_full
            or latest is None
            or latest.completed_at is None
            or latest.completed_at <= now - cls.REFRESH_AFTER
            or latest.target_product_id < target
            or (
                projection_changed_at is not None
                and latest.started_at is not None
                and projection_changed_at > latest.started_at
            )
            or (
                backfill.completed_at
                and latest.completed_at
                and backfill.completed_at > latest.completed_at
            )
        )
        if not due:
            return latest
        return cls._create_run(
            seller_id=seller_id,
            marketplace_id=marketplace.id,
            run_kind="wb_parity",
            cursor_product_id=0,
            target_product_id=target,
            now=now,
        )

    @classmethod
    def _claim_run(
        cls,
        run: MarketplaceProjectionRun,
        *,
        now: datetime,
    ) -> Optional[str]:
        if run.status == "paused":
            return None
        if run.status not in {"pending", "running"}:
            return None
        token = secrets.token_hex(32)
        updated = MarketplaceProjectionRun.query.filter(
            MarketplaceProjectionRun.id == run.id,
            MarketplaceProjectionRun.status.in_(("pending", "running")),
            or_(
                MarketplaceProjectionRun.lease_owner.is_(None),
                MarketplaceProjectionRun.lease_expires_at.is_(None),
                MarketplaceProjectionRun.lease_expires_at <= now,
            ),
        ).update({
            MarketplaceProjectionRun.status: "running",
            MarketplaceProjectionRun.lease_owner: token,
            MarketplaceProjectionRun.lease_expires_at: now + cls.LEASE_TTL,
            MarketplaceProjectionRun.heartbeat_at: now,
            MarketplaceProjectionRun.updated_at: now,
        }, synchronize_session=False)
        db.session.commit()
        return token if updated == 1 else None

    @classmethod
    def _owned_claimed_run(
        cls,
        *,
        run_id: int,
        token: str,
    ) -> MarketplaceProjectionRun:
        run = MarketplaceProjectionRun.query.filter_by(
            id=run_id,
            lease_owner=token,
            status="running",
        ).first()
        if run is None:
            raise MarketplaceRolloutBusy("Projection batch lease was lost")
        return run

    @classmethod
    def _batch_products(cls, run: MarketplaceProjectionRun, *, limit: int) -> List[Product]:
        return Product.query.filter(
            Product.seller_id == run.seller_id,
            Product.id > run.cursor_product_id,
            Product.id <= run.target_product_id,
        ).order_by(Product.id.asc()).limit(limit).all()

    @classmethod
    def _complete_or_advance(
        cls,
        run: MarketplaceProjectionRun,
        *,
        products: Sequence[Product],
        now: datetime,
    ) -> None:
        if products:
            run.cursor_product_id = int(products[-1].id)
        if not products or run.cursor_product_id >= run.target_product_id:
            run.status = "completed"
            run.completed_at = now
        else:
            run.status = "running"
        run.heartbeat_at = now
        run.lease_owner = None
        run.lease_expires_at = None
        run.error_code = None
        run.error_message = None

    @classmethod
    def _mark_failed(
        cls,
        run_id: int,
        error: Exception,
        *,
        token: str,
        now: datetime,
    ) -> None:
        db.session.rollback()
        run = db.session.get(MarketplaceProjectionRun, run_id)
        if (
            run is None
            or run.status == "completed"
            or run.lease_owner != token
        ):
            return
        run.status = "failed"
        run.lease_owner = None
        run.lease_expires_at = None
        run.heartbeat_at = now
        run.error_code = (
            error.code if isinstance(error, MarketplaceRolloutError)
            else "marketplace_projection_failed"
        )[:100]
        message = (
            str(error)
            if isinstance(error, MarketplaceRolloutError)
            else "Не удалось обработать локальную WB projection batch"
        )
        run.error_message = " ".join(message.split())[:1000]
        db.session.commit()

    @classmethod
    def run_backfill_batch(
        cls,
        *,
        seller_id: int,
        limit: int = MAX_BATCH,
        force_full: bool = False,
        now: Optional[datetime] = None,
    ) -> Optional[MarketplaceProjectionRun]:
        seller_id = cls._positive_integer(seller_id, "seller_id")
        limit = cls._batch_limit(limit)
        if not isinstance(force_full, bool):
            raise MarketplaceRolloutValidationError(
                "force_full должен быть boolean"
            )
        current_time = now or datetime.utcnow()
        marketplace = cls._wb_marketplace()
        run = cls._ensure_backfill_run(
            seller_id=seller_id,
            marketplace=marketplace,
            force_full=force_full,
            now=current_time,
        )
        if run is None or run.status in {"completed", "failed", "paused"}:
            return run
        token = cls._claim_run(run, now=current_time)
        if token is None:
            return db.session.get(MarketplaceProjectionRun, run.id)
        try:
            run = cls._owned_claimed_run(run_id=run.id, token=token)
            products = cls._batch_products(run, limit=limit)
            product_ids = [int(product.id) for product in products]
            imported = cls._latest_imported_by_product(
                seller_id=seller_id,
                product_ids=product_ids,
            )
            listings = {
                int(listing.legacy_product_id): listing
                for listing in MarketplaceListing.query.filter(
                    MarketplaceListing.seller_id == seller_id,
                    MarketplaceListing.marketplace_id == marketplace.id,
                    MarketplaceListing.account_id.is_(None),
                    MarketplaceListing.legacy_product_id.in_(product_ids),
                ).all()
                if listing.legacy_product_id is not None
            } if product_ids else {}
            counters = {"inserted": 0, "updated": 0, "unchanged": 0}
            for product in products:
                outcome = cls._apply_product(
                    marketplace=marketplace,
                    product=product,
                    imported_product_id=imported.get(int(product.id)),
                    listing=listings.get(int(product.id)),
                    now=current_time,
                )
                counters[outcome] += 1
            run.scanned_count += len(products)
            run.inserted_count += counters["inserted"]
            run.updated_count += counters["updated"]
            run.unchanged_count += counters["unchanged"]
            cls._complete_or_advance(run, products=products, now=current_time)
            db.session.commit()
            return run
        except Exception as exc:
            cls._mark_failed(
                run.id,
                exc,
                token=token,
                now=current_time,
            )
            raise

    @classmethod
    def _listing_mismatches(
        cls,
        *,
        listing: MarketplaceListing,
        expected: Mapping[str, Any],
        marketplace_id: int,
    ) -> List[str]:
        fields: List[str] = []
        if listing.marketplace_id != marketplace_id:
            fields.append("marketplace_id")
        if listing.account_id is not None:
            fields.append("account_id")
        if listing.seller_id != expected["seller_id"]:
            fields.append("seller_id")
        for field_name in cls.PROJECTION_FIELDS:
            if getattr(listing, field_name) != expected[field_name]:
                fields.append(field_name)
        if listing.imported_product_id != expected["imported_product_id"]:
            fields.append("imported_product_id")
        expected_link_status = (
            "linked"
            if expected["imported_product_id"] is not None
            else "unlinked"
        )
        if listing.link_status != expected_link_status:
            fields.append("link_status")
        return fields

    @classmethod
    def run_parity_batch(
        cls,
        *,
        seller_id: int,
        limit: int = MAX_BATCH,
        force_full: bool = False,
        now: Optional[datetime] = None,
    ) -> Optional[MarketplaceProjectionRun]:
        seller_id = cls._positive_integer(seller_id, "seller_id")
        limit = cls._batch_limit(limit)
        if not isinstance(force_full, bool):
            raise MarketplaceRolloutValidationError(
                "force_full должен быть boolean"
            )
        current_time = now or datetime.utcnow()
        marketplace = cls._wb_marketplace()
        run = cls._ensure_parity_run(
            seller_id=seller_id,
            marketplace=marketplace,
            force_full=force_full,
            now=current_time,
        )
        if run is None or run.status in {"completed", "failed", "paused"}:
            return run
        token = cls._claim_run(run, now=current_time)
        if token is None:
            return db.session.get(MarketplaceProjectionRun, run.id)
        try:
            run = cls._owned_claimed_run(run_id=run.id, token=token)
            products = cls._batch_products(run, limit=limit)
            product_ids = [int(product.id) for product in products]
            imported = cls._latest_imported_by_product(
                seller_id=seller_id,
                product_ids=product_ids,
            )
            listings = {
                int(listing.legacy_product_id): listing
                for listing in MarketplaceListing.query.filter(
                    MarketplaceListing.seller_id == seller_id,
                    MarketplaceListing.marketplace_id == marketplace.id,
                    MarketplaceListing.account_id.is_(None),
                    MarketplaceListing.legacy_product_id.in_(product_ids),
                ).all()
                if listing.legacy_product_id is not None
            } if product_ids else {}
            field_counts = MarketplaceProjectionRun._json_value(
                run.mismatch_fields_json,
                {},
            )
            samples = MarketplaceProjectionRun._json_value(
                run.mismatch_sample_json,
                [],
            )
            for product in products:
                expected = cls._expected_projection(
                    product,
                    imported_product_id=imported.get(int(product.id)),
                )
                listing = listings.get(int(product.id))
                if listing is None:
                    run.missing_count += 1
                    fields = ["listing_missing"]
                else:
                    fields = cls._listing_mismatches(
                        listing=listing,
                        expected=expected,
                        marketplace_id=marketplace.id,
                    )
                    if fields:
                        run.mismatched_count += 1
                    else:
                        run.matched_count += 1
                if fields:
                    for field_name in fields:
                        field_counts[field_name] = int(
                            field_counts.get(field_name, 0)
                        ) + 1
                    if len(samples) < cls.MAX_MISMATCH_SAMPLE:
                        samples.append({
                            "product_id": int(product.id),
                            "nm_id": str(product.nm_id),
                            "listing_id": listing.id if listing else None,
                            "fields": fields,
                        })
            run.scanned_count += len(products)
            run.mismatch_fields_json = cls._canonical_json(field_counts, {})
            run.mismatch_sample_json = cls._canonical_json(samples, [])
            cls._complete_or_advance(run, products=products, now=current_time)
            db.session.commit()
            return run
        except Exception as exc:
            cls._mark_failed(
                run.id,
                exc,
                token=token,
                now=current_time,
            )
            raise

    @classmethod
    def maintenance_tick(
        cls,
        *,
        seller_limit: int = 3,
        batch_size: int = MAX_BATCH,
        dual_read_enabled: bool = True,
        now: Optional[datetime] = None,
    ) -> Dict[str, int]:
        seller_limit = cls._positive_integer(seller_limit, "seller_limit")
        if seller_limit > cls.MAX_SELLERS_PER_TICK:
            raise MarketplaceRolloutValidationError(
                f"seller_limit не может быть больше {cls.MAX_SELLERS_PER_TICK}"
            )
        batch_size = cls._batch_limit(batch_size)
        if not isinstance(dual_read_enabled, bool):
            raise MarketplaceRolloutValidationError(
                "dual_read_enabled должен быть boolean"
            )
        current_time = now or datetime.utcnow()
        activity = db.session.query(
            MarketplaceProjectionRun.seller_id.label("seller_id"),
            func.max(MarketplaceProjectionRun.updated_at).label("last_activity"),
        ).group_by(MarketplaceProjectionRun.seller_id).subquery()
        seller_rows = db.session.query(Product.seller_id).outerjoin(
            activity,
            activity.c.seller_id == Product.seller_id,
        ).group_by(Product.seller_id, activity.c.last_activity).order_by(
            func.coalesce(
                activity.c.last_activity,
                datetime(1970, 1, 1),
            ).asc(),
            Product.seller_id.asc(),
        ).limit(seller_limit).all()
        result = {
            "selected_sellers": len(seller_rows),
            "backfill_batches": 0,
            "parity_batches": 0,
            "busy": 0,
            "failed": 0,
        }
        for row in seller_rows:
            seller_id = int(row[0])
            try:
                backfill = cls.run_backfill_batch(
                    seller_id=seller_id,
                    limit=batch_size,
                    now=current_time,
                )
                backfill_advanced = bool(
                    backfill is not None
                    and backfill.status in {"running", "completed"}
                    and backfill.heartbeat_at == current_time
                )
                if backfill_advanced:
                    result["backfill_batches"] += 1
                if (
                    dual_read_enabled
                    and backfill is not None
                    and backfill.status == "completed"
                ):
                    parity = cls.run_parity_batch(
                        seller_id=seller_id,
                        limit=batch_size,
                        now=current_time,
                    )
                    if (
                        parity is not None
                        and parity.status in {"running", "completed"}
                        and parity.heartbeat_at == current_time
                    ):
                        result["parity_batches"] += 1
            except MarketplaceRolloutBusy:
                result["busy"] += 1
            except Exception:
                result["failed"] += 1
        return result

    @classmethod
    def readiness(cls, *, seller_id: int) -> Dict[str, Any]:
        seller_id = cls._positive_integer(seller_id, "seller_id")
        marketplace = cls._wb_marketplace()
        product_count, maximum = cls._product_watermark(seller_id)
        source_changed_at = cls._latest_source_changed_at(seller_id)
        projection_changed_at = cls._latest_projection_changed_at(
            seller_id=seller_id,
            marketplace_id=marketplace.id,
        )
        projection_count = MarketplaceListing.query.filter_by(
            seller_id=seller_id,
            marketplace_id=marketplace.id,
            account_id=None,
        ).filter(
            MarketplaceListing.legacy_product_id.isnot(None)
        ).count()
        first_missing = cls._first_missing_product_id(
            seller_id=seller_id,
            marketplace_id=marketplace.id,
        )
        first_link_mismatch = cls._first_link_mismatch_product_id(
            seller_id=seller_id,
            marketplace_id=marketplace.id,
        )
        latest_backfill = cls._latest_run(
            seller_id=seller_id,
            marketplace_id=marketplace.id,
            run_kind="wb_backfill",
        )
        latest_parity = cls._latest_run(
            seller_id=seller_id,
            marketplace_id=marketplace.id,
            run_kind="wb_parity",
        )
        blockers: List[str] = []
        if projection_count != product_count:
            blockers.append("wb_projection_count_mismatch")
        if product_count:
            if first_missing is not None:
                blockers.append("wb_projection_incomplete")
            if first_link_mismatch is not None:
                blockers.append("wb_canonical_link_mismatch")

            if latest_backfill is None or latest_backfill.status != "completed":
                blockers.append("wb_backfill_not_completed")
            else:
                if latest_backfill.target_product_id < maximum:
                    blockers.append("wb_backfill_not_covering_watermark")
                if (
                    source_changed_at is not None
                    and latest_backfill.started_at < source_changed_at
                ):
                    blockers.append("wb_source_changed_after_sweep")

            if latest_parity is None or latest_parity.status != "completed":
                blockers.append("wb_parity_not_completed")
            else:
                if latest_parity.target_product_id < maximum:
                    blockers.append("wb_parity_not_covering_watermark")
                if latest_parity.missing_count or latest_parity.mismatched_count:
                    blockers.append("wb_parity_mismatch")
                if (
                    source_changed_at is not None
                    and latest_parity.started_at < source_changed_at
                ):
                    blockers.append("wb_source_changed_after_sweep")
                if (
                    projection_changed_at is not None
                    and latest_parity.started_at < projection_changed_at
                ):
                    blockers.append("wb_projection_changed_after_parity")

            if (
                latest_backfill is not None
                and latest_backfill.status == "completed"
                and latest_parity is not None
                and latest_parity.status == "completed"
                and (
                    latest_backfill.completed_at is None
                    or latest_parity.completed_at is None
                    or latest_parity.completed_at < latest_backfill.completed_at
                )
            ):
                blockers.append("wb_parity_precedes_backfill")

        blockers = list(dict.fromkeys(blockers))
        cutover_ready = not blockers
        return {
            "seller_id": seller_id,
            "legacy_product_count": product_count,
            "projection_count": projection_count,
            "first_missing_product_id": first_missing,
            "first_link_mismatch_product_id": first_link_mismatch,
            "product_id_watermark": maximum,
            "latest_source_changed_at": (
                source_changed_at.isoformat() if source_changed_at else None
            ),
            "latest_projection_changed_at": (
                projection_changed_at.isoformat()
                if projection_changed_at else None
            ),
            "cutover_ready": cutover_ready,
            "blockers": blockers,
            "latest_backfill": (
                latest_backfill.to_public_dict() if latest_backfill else None
            ),
            "latest_parity": (
                latest_parity.to_public_dict() if latest_parity else None
            ),
        }

    @classmethod
    def wb_product_query(
        cls,
        *,
        seller_id: int,
        common_read_requested: bool,
    ):
        seller_id = cls._positive_integer(seller_id, "seller_id")
        if not isinstance(common_read_requested, bool):
            raise MarketplaceRolloutValidationError(
                "common_read_requested должен быть boolean"
            )
        query = Product.query.filter(Product.seller_id == seller_id)
        if not common_read_requested:
            return query, {
                "seller_id": seller_id,
                "read_mode": "legacy",
                "common_read_requested": False,
                "cutover_ready": None,
                "blockers": [],
            }

        state = cls.readiness(seller_id=seller_id)
        if state["cutover_ready"]:
            marketplace = cls._wb_marketplace()
            missing_product = aliased(Product)
            missing_listing = aliased(MarketplaceListing)
            missing_at_execution = db.session.query(
                missing_product.id,
            ).outerjoin(
                missing_listing,
                and_(
                    missing_listing.legacy_product_id == missing_product.id,
                    missing_listing.seller_id == seller_id,
                    missing_listing.marketplace_id == marketplace.id,
                    missing_listing.account_id.is_(None),
                ),
            ).filter(
                missing_product.seller_id == seller_id,
                missing_listing.id.is_(None),
            ).exists()
            query = Product.query.outerjoin(
                MarketplaceListing,
                and_(
                    MarketplaceListing.legacy_product_id == Product.id,
                    MarketplaceListing.seller_id == seller_id,
                    MarketplaceListing.marketplace_id == marketplace.id,
                    MarketplaceListing.account_id.is_(None),
                ),
            ).filter(
                Product.seller_id == seller_id,
                or_(
                    MarketplaceListing.id.isnot(None),
                    missing_at_execution,
                ),
            )
            state["read_mode"] = "marketplace_listing"
            state["execution_race_fallback"] = True
        else:
            state["read_mode"] = "legacy_fallback"
        state["common_read_requested"] = common_read_requested
        return query, state

    @classmethod
    def pause_run(cls, *, run_id: int) -> MarketplaceProjectionRun:
        run_id = cls._positive_integer(run_id, "run_id")
        run = db.session.get(MarketplaceProjectionRun, run_id)
        if run is None:
            raise MarketplaceRolloutNotFound("Projection run не найден")
        if run.status == "paused":
            return run
        if run.status in {"completed", "failed"}:
            raise MarketplaceRolloutConflict(
                "Завершённый projection run нельзя поставить на паузу"
            )
        now = datetime.utcnow()
        updated = MarketplaceProjectionRun.query.filter(
            MarketplaceProjectionRun.id == run_id,
            MarketplaceProjectionRun.status.in_(("pending", "running")),
            or_(
                MarketplaceProjectionRun.lease_owner.is_(None),
                MarketplaceProjectionRun.lease_expires_at.is_(None),
                MarketplaceProjectionRun.lease_expires_at <= now,
            ),
        ).update({
            MarketplaceProjectionRun.status: "paused",
            MarketplaceProjectionRun.lease_owner: None,
            MarketplaceProjectionRun.lease_expires_at: None,
            MarketplaceProjectionRun.heartbeat_at: now,
            MarketplaceProjectionRun.updated_at: now,
        }, synchronize_session=False)
        db.session.commit()
        if updated != 1:
            current = db.session.get(MarketplaceProjectionRun, run_id)
            if (
                current is not None
                and current.lease_owner
                and current.lease_expires_at
                and current.lease_expires_at > now
            ):
                raise MarketplaceRolloutBusy(
                    "Текущий bounded batch ещё выполняется; повторите после heartbeat"
                )
            raise MarketplaceRolloutConflict(
                "Projection run изменился одновременно с pause"
            )
        return db.session.get(MarketplaceProjectionRun, run_id)

    @classmethod
    def resume_run(cls, *, run_id: int) -> MarketplaceProjectionRun:
        run_id = cls._positive_integer(run_id, "run_id")
        run = db.session.get(MarketplaceProjectionRun, run_id)
        if run is None:
            raise MarketplaceRolloutNotFound("Projection run не найден")
        if run.status not in {"paused", "failed"}:
            raise MarketplaceRolloutConflict(
                "Resume доступен только для paused/failed run"
            )
        existing = cls._active_run(
            seller_id=run.seller_id,
            marketplace_id=run.marketplace_id,
            run_kind=run.run_kind,
        )
        if existing is not None and existing.id != run.id:
            raise MarketplaceRolloutBusy(
                "Для этого seller/kind уже существует активный run"
            )
        run.status = "running"
        run.lease_owner = None
        run.lease_expires_at = None
        run.error_code = None
        run.error_message = None
        run.heartbeat_at = datetime.utcnow()
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            raise MarketplaceRolloutBusy(
                "Projection run changed concurrently"
            ) from None
        return run
