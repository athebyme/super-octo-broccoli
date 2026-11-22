"""Exact, local preparation of Ozon update drafts from selected listings.

The batch is a thin adapter around the guarded single-listing update-draft
contract. Preview reads local seller-owned projections only. Apply requires a
fresh, whole-set digest and delegates each ready row to the existing atomic
single-listing writer; it never calls a provider or publication queue.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from typing import Any, Dict, List, Tuple

from models import Marketplace, MarketplaceListing, MarketplaceProductDraft
from services.marketplace_drafts import (
    MarketplaceDraftConflict,
    MarketplaceDraftError,
    MarketplaceDraftNotFound,
    MarketplaceDraftService,
    MarketplaceDraftValidationError,
)
from services.ozon_bulk_upload import OzonBulkUploadService


class OzonExistingUpdateBatch:
    """Preview and prepare an ordered batch of existing Ozon listing drafts."""

    MAX_ITEMS = OzonBulkUploadService.MAX_ITEMS
    DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
    STALE_REVIEW_MESSAGE = (
        "Состояние выбранных карточек изменилось; перечитайте список перед подготовкой"
    )

    @classmethod
    def _listing_ids(cls, values: Any) -> List[int]:
        if (
            not isinstance(values, list)
            or not 1 <= len(values) <= cls.MAX_ITEMS
            or any(
                not isinstance(value, int)
                or isinstance(value, bool)
                or value <= 0
                or value > MarketplaceDraftService.MAX_SIGNED_INT64
                for value in values
            )
            or len(set(values)) != len(values)
        ):
            raise MarketplaceDraftValidationError(
                f"listing_ids должен быть упорядоченным списком из 1–{cls.MAX_ITEMS} "
                "уникальных положительных целых ID"
            )
        return list(values)

    @staticmethod
    def _message(error: Exception) -> str:
        message = str(error).strip()
        return message[:500] if message else "Состояние карточки не подтверждено"

    @staticmethod
    def _listing_proof(listing: MarketplaceListing) -> dict:
        evidence = listing.link_evidence_json or ""
        snapshot_json = json.dumps(
            {
                "normalized_status": listing.normalized_status,
                "provider_status": listing.provider_status,
                "visibility": listing.visibility,
                "statuses_json": listing.statuses_json,
                "moderation_errors_json": listing.moderation_errors_json,
                "attributes_json": listing.attributes_json,
                "complex_attributes_json": listing.complex_attributes_json,
                "media_json": listing.media_json,
                "dimensions_json": listing.dimensions_json,
                "barcodes_json": listing.barcodes_json,
                "price_summary_json": listing.price_summary_json,
                "stock_summary_json": listing.stock_summary_json,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return {
            "listing_id": listing.id,
            "seller_id": listing.seller_id,
            "account_id": listing.account_id,
            "marketplace_id": listing.marketplace_id,
            "imported_product_id": listing.imported_product_id,
            "offer_id": listing.offer_id,
            "external_product_id": listing.external_product_id,
            "external_category_id": listing.external_category_id,
            "external_type_id": listing.external_type_id,
            "link_version": listing.link_version,
            "link_status": listing.canonical_link_status,
            "link_source": listing.link_source,
            "linked_at": listing.linked_at.isoformat() if listing.linked_at else None,
            "link_evidence_sha256": hashlib.sha256(
                evidence.encode("utf-8")
            ).hexdigest(),
            "snapshot_sha256": hashlib.sha256(
                snapshot_json.encode("utf-8")
            ).hexdigest(),
            "title": listing.title or "",
            "is_available": bool(listing.is_available),
            "is_archived": bool(listing.is_archived),
            "sync_fingerprint": listing.sync_fingerprint or "",
            "last_seen_at": (
                listing.last_seen_at.isoformat() if listing.last_seen_at else None
            ),
            "attributes_synced_at": (
                listing.attributes_synced_at.isoformat()
                if listing.attributes_synced_at else None
            ),
            "prices_synced_at": (
                listing.prices_synced_at.isoformat()
                if listing.prices_synced_at else None
            ),
        }

    @classmethod
    def _blocked_item(
        cls,
        listing: MarketplaceListing,
        message: str,
        *,
        extra_proof: dict | None = None,
    ) -> dict:
        proof = {
            "listing": cls._listing_proof(listing),
            "status": "blocked",
            "message": message,
        }
        if extra_proof:
            proof["extra"] = extra_proof
        return {
            "public": {
                "listing_id": listing.id,
                "title": listing.title or "",
                "offer_id": listing.offer_id,
                "status": "blocked",
                "message": message,
            },
            "proof": proof,
        }

    @classmethod
    def _existing_item(
        cls,
        *,
        seller_id: int,
        account_id: int,
        listing: MarketplaceListing,
        context: dict,
    ) -> dict:
        draft_id = context.get("draft_id")
        draft = MarketplaceProductDraft.query.filter_by(
            id=draft_id,
            seller_id=seller_id,
            account_id=account_id,
        ).first()
        if (
            draft is None
            or draft.marketplace_id != listing.marketplace_id
            or draft.imported_product_id != listing.imported_product_id
            or draft.published_listing_id != listing.id
            or draft.offer_id != listing.offer_id
        ):
            return cls._blocked_item(
                listing,
                "Существующий черновик не прошёл точную проверку; перечитайте карточку",
                extra_proof={"draft_id": draft_id},
            )

        draft_proof = {
            "draft_id": draft.id,
            "seller_id": draft.seller_id,
            "account_id": draft.account_id,
            "marketplace_id": draft.marketplace_id,
            "imported_product_id": draft.imported_product_id,
            "published_listing_id": draft.published_listing_id,
            "offer_id": draft.offer_id,
            "version": draft.version,
            "status": draft.status,
        }
        if draft.status == "archived":
            return cls._blocked_item(
                listing,
                "Архивный черновик нельзя включить в обновление",
                extra_proof=draft_proof,
            )
        try:
            MarketplaceDraftService._assert_no_active_publication(draft)
            from services.ozon_bulk_repair import OzonBulkRepairService

            reviewed_holds = OzonBulkRepairService._active_reviewed_draft_ids(
                seller_id=seller_id,
                account_id=account_id,
                draft_ids=[draft.id],
            )
            if draft.id in reviewed_holds:
                raise MarketplaceDraftConflict(
                    "Черновик занят проверенной отправкой или операцией; "
                    "дождитесь её результата"
                )
        except MarketplaceDraftError as exc:
            return cls._blocked_item(
                listing,
                cls._message(exc),
                extra_proof=draft_proof,
            )
        return {
            "public": {
                "listing_id": listing.id,
                "title": listing.title or "",
                "offer_id": listing.offer_id,
                "status": "existing",
                "draft_id": draft.id,
            },
            "proof": {
                "listing": cls._listing_proof(listing),
                "status": "existing",
                "draft": draft_proof,
            },
        }

    @classmethod
    def _ready_item(
        cls,
        *,
        listing: MarketplaceListing,
        context: dict,
    ) -> dict:
        imported_product_id = context.get("imported_product_id")
        expected = context.get("expected_existing_listing")
        if (
            not isinstance(imported_product_id, int)
            or isinstance(imported_product_id, bool)
            or imported_product_id <= 0
            or not isinstance(expected, dict)
            or expected.get("listing_id") != listing.id
            or imported_product_id != listing.imported_product_id
        ):
            return cls._blocked_item(
                listing,
                "Проверка не подтвердила точную связь листинга; перечитайте карточку",
                extra_proof={
                    "imported_product_id": imported_product_id,
                    "expected_existing_listing": expected,
                },
            )
        return {
            "public": {
                "listing_id": listing.id,
                "title": listing.title or "",
                "offer_id": listing.offer_id,
                "status": "ready",
            },
            "proof": {
                "listing": cls._listing_proof(listing),
                "status": "ready",
                "imported_product_id": imported_product_id,
                "expected_existing_listing": expected,
            },
        }

    @classmethod
    def _build_preview(
        cls,
        *,
        seller_id: int,
        account_id: int,
        listing_ids: Any,
    ) -> Tuple[dict, List[dict]]:
        seller_id = MarketplaceDraftService._positive_signed_int64(
            seller_id,
            "seller_id",
        )
        account_id = MarketplaceDraftService._positive_signed_int64(
            account_id,
            "account_id",
        )
        ordered_ids = cls._listing_ids(listing_ids)
        account = MarketplaceDraftService._owned_account(
            seller_id=seller_id,
            account_id=account_id,
        )

        # Validate the complete scope before per-row classification. This
        # deliberately gives the same safe not-found result for missing and
        # foreign listing IDs.
        listings = MarketplaceListing.query.join(
            Marketplace,
            Marketplace.id == MarketplaceListing.marketplace_id,
        ).filter(
            MarketplaceListing.id.in_(ordered_ids),
            MarketplaceListing.seller_id == seller_id,
            MarketplaceListing.account_id == account_id,
            MarketplaceListing.marketplace_id == account.marketplace_id,
            Marketplace.code == "ozon",
            Marketplace.is_active.is_(True),
        ).all()
        by_id = {listing.id: listing for listing in listings}
        if len(by_id) != len(ordered_ids):
            raise MarketplaceDraftNotFound(
                "Один или несколько листингов Ozon не найдены"
            )

        items = []
        for listing_id in ordered_ids:
            listing = by_id[listing_id]
            try:
                context = MarketplaceDraftService.existing_listing_update_draft_context(
                    seller_id=seller_id,
                    listing_id=listing_id,
                )
                if (
                    context.get("account_id") != account_id
                    or context.get("imported_product_id")
                    != listing.imported_product_id
                ):
                    item = cls._blocked_item(
                        listing,
                        "Связь магазина или внутренней карточки изменилась; перечитайте список",
                        extra_proof={
                            "context_account_id": context.get("account_id"),
                            "context_imported_product_id": context.get(
                                "imported_product_id"
                            ),
                        },
                    )
                elif context.get("status") == "ready":
                    item = cls._ready_item(listing=listing, context=context)
                elif context.get("status") == "existing":
                    item = cls._existing_item(
                        seller_id=seller_id,
                        account_id=account_id,
                        listing=listing,
                        context=context,
                    )
                else:
                    item = cls._blocked_item(
                        listing,
                        "Состояние карточки не подтверждено; перечитайте список",
                        extra_proof={"context_status": context.get("status")},
                    )
            except MarketplaceDraftError as exc:
                item = cls._blocked_item(listing, cls._message(exc))
            items.append(item)

        digest_document = {
            "version": 1,
            "seller_id": seller_id,
            "account_id": account_id,
            "listing_ids": ordered_ids,
            "rows": [item["proof"] for item in items],
        }
        canonical = json.dumps(
            digest_document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        document = {
            "account_id": account.id,
            "account_label": account.label or "",
            "listing_ids": ordered_ids,
            "rows": [item["public"] for item in items],
            "digest": digest,
        }
        return document, items

    @classmethod
    def preview(
        cls,
        *,
        seller_id: int,
        account_id: int,
        listing_ids: list,
    ) -> dict:
        """Return ordered local readiness without creating or changing drafts."""
        document, _items = cls._build_preview(
            seller_id=seller_id,
            account_id=account_id,
            listing_ids=listing_ids,
        )
        return document

    @classmethod
    def apply(
        cls,
        *,
        seller_id: int,
        account_id: int,
        listing_ids: list,
        expected_digest: str,
    ) -> dict:
        """Create only freshly reviewed ready drafts and return exact outcomes."""
        if (
            not isinstance(expected_digest, str)
            or not cls.DIGEST_PATTERN.fullmatch(expected_digest)
        ):
            raise MarketplaceDraftConflict(cls.STALE_REVIEW_MESSAGE)

        document, items = cls._build_preview(
            seller_id=seller_id,
            account_id=account_id,
            listing_ids=listing_ids,
        )
        if not hmac.compare_digest(document["digest"], expected_digest):
            raise MarketplaceDraftConflict(cls.STALE_REVIEW_MESSAGE)

        result_rows = []
        successful_drafts = []
        for item in items:
            row = dict(item["public"])
            proof = item["proof"]
            status = row["status"]
            if status == "ready":
                try:
                    draft = MarketplaceDraftService.create_draft(
                        seller_id=seller_id,
                        account_id=account_id,
                        imported_product_id=proof["imported_product_id"],
                        expected_existing_listing=proof[
                            "expected_existing_listing"
                        ],
                    )
                except MarketplaceDraftError as exc:
                    row["status"] = "blocked"
                    row["message"] = cls._message(exc)
                else:
                    if (
                        draft.seller_id != seller_id
                        or draft.account_id != account_id
                        or draft.imported_product_id != proof["imported_product_id"]
                        or draft.published_listing_id != row["listing_id"]
                        or draft.offer_id != row["offer_id"]
                        or draft.status == "archived"
                    ):
                        row["status"] = "blocked"
                        row["message"] = (
                            "Созданный черновик не прошёл проверку связи; перечитайте карточку"
                        )
                    else:
                        row["status"] = "created"
                        row["draft_id"] = draft.id
                        successful_drafts.append({
                            "row": row,
                            "draft_id": draft.id,
                            "listing_id": row["listing_id"],
                            "imported_product_id": draft.imported_product_id,
                            "offer_id": draft.offer_id,
                            "version": draft.version,
                        })
            elif status == "existing":
                # Preview established exact, non-archived ownership and the
                # digest pins the draft identity and version. Do not rewrite it.
                row["status"] = "existing"
                draft_proof = proof["draft"]
                successful_drafts.append({
                    "row": row,
                    "draft_id": draft_proof["draft_id"],
                    "listing_id": row["listing_id"],
                    "imported_product_id": draft_proof["imported_product_id"],
                    "offer_id": draft_proof["offer_id"],
                    "version": draft_proof["version"],
                })
            else:
                row["status"] = "blocked"
            result_rows.append(row)

        # A successful per-row return is not enough for a reliable handoff:
        # another writer may archive, rebind, or hold a draft while later rows
        # are being prepared. Re-read the exact bounded success set once, then
        # retain only identities that still match the reviewed versions.
        draft_ids = [item["draft_id"] for item in successful_drafts]
        duplicate_ids = {
            draft_id
            for draft_id in draft_ids
            if draft_ids.count(draft_id) > 1
        }
        by_id = {}
        reviewed_holds = set()
        if draft_ids:
            by_id = {
                draft.id: draft
                for draft in MarketplaceProductDraft.query.filter(
                    MarketplaceProductDraft.id.in_(set(draft_ids)),
                    MarketplaceProductDraft.seller_id == seller_id,
                    MarketplaceProductDraft.account_id == account_id,
                ).all()
            }
            from services.ozon_bulk_repair import OzonBulkRepairService

            reviewed_holds = OzonBulkRepairService._active_reviewed_draft_ids(
                seller_id=seller_id,
                account_id=account_id,
                draft_ids=list(set(draft_ids)),
            )

        for item in successful_drafts:
            row = item["row"]
            draft_id = item["draft_id"]
            draft = by_id.get(draft_id)
            message = None
            if draft_id in duplicate_ids:
                message = "Несколько выбранных карточек связаны с одним черновиком"
            elif (
                draft is None
                or draft.published_listing_id != item["listing_id"]
                or draft.imported_product_id != item["imported_product_id"]
                or draft.offer_id != item["offer_id"]
                or draft.version != item["version"]
                or draft.status == "archived"
            ):
                message = "Состояние черновика изменилось; перечитайте список"
            elif draft.id in reviewed_holds:
                message = (
                    "Черновик занят проверенной отправкой или операцией; "
                    "дождитесь её результата"
                )
            else:
                try:
                    MarketplaceDraftService._assert_no_active_publication(draft)
                except MarketplaceDraftError as exc:
                    message = cls._message(exc)
            if message:
                row["status"] = "blocked"
                row.pop("draft_id", None)
                row["message"] = message

        return {
            "account_id": document["account_id"],
            "account_label": document["account_label"],
            "listing_ids": document["listing_ids"],
            "rows": result_rows,
        }
