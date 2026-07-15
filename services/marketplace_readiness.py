"""Sanitized seller-scoped operational readiness for marketplace rollout."""

from datetime import datetime, timedelta
from typing import Any, Dict, Mapping

from sqlalchemy import func

from models import (
    Marketplace,
    MarketplaceAnalyticsSync,
    MarketplaceCatalogSync,
    MarketplaceCommercialProposal,
    MarketplaceFinanceSync,
    MarketplaceFulfillmentSync,
    MarketplaceInboxSync,
    MarketplaceListing,
    MarketplaceOperation,
    MarketplaceProductDraft,
    SellerMarketplaceAccount,
)
from services.marketplace_rollout import MarketplaceRolloutService
from services.ozon_reference_service import OzonReferenceService


class MarketplaceReadinessService:
    """Build one bounded status document without decrypting credentials."""

    OPERATION_ACTIVE = (
        "queued",
        "submitting",
        "submitted",
        "polling",
        "uncertain",
    )
    SYNC_MODELS = (
        ("catalog", MarketplaceCatalogSync),
        ("analytics", MarketplaceAnalyticsSync),
        ("fulfillment", MarketplaceFulfillmentSync),
        ("finance", MarketplaceFinanceSync),
        ("inbox", MarketplaceInboxSync),
    )

    @staticmethod
    def _status_counts(query, column) -> Dict[str, int]:
        return {
            str(status): int(count)
            for status, count in query.with_entities(
                column,
                func.count(),
            ).group_by(column).all()
            if status is not None
        }

    @staticmethod
    def _flags(config: Mapping[str, Any]) -> Dict[str, bool]:
        return {
            "ozon_read": bool(config.get("MARKETPLACE_OZON_ENABLED", False)),
            "ozon_publication": bool(config.get(
                "MARKETPLACE_OZON_PUBLICATION_ENABLED",
                False,
            )),
            "ozon_auto_publish": bool(config.get(
                "MARKETPLACE_OZON_AUTO_PUBLISH_ENABLED",
                False,
            )),
            "ozon_commercial_writes": bool(config.get(
                "MARKETPLACE_OZON_COMMERCIAL_WRITES_ENABLED",
                False,
            )),
            "wb_projection": bool(config.get(
                "MARKETPLACE_WB_PROJECTION_ENABLED",
                True,
            )),
            "wb_dual_read": bool(config.get(
                "MARKETPLACE_WB_DUAL_READ_ENABLED",
                True,
            )),
            "wb_common_read": bool(config.get(
                "MARKETPLACE_WB_COMMON_READ_ENABLED",
                False,
            )),
        }

    @classmethod
    def build(
        cls,
        *,
        seller_id: int,
        config: Mapping[str, Any],
        now: datetime = None,
    ) -> Dict[str, Any]:
        seller_id = MarketplaceRolloutService._positive_integer(
            seller_id,
            "seller_id",
        )
        current_time = now or datetime.utcnow()
        flags = cls._flags(config)
        projection = MarketplaceRolloutService.readiness(seller_id=seller_id)

        ozon = Marketplace.query.filter_by(code="ozon").first()
        if ozon is None:
            account_query = SellerMarketplaceAccount.query.filter(
                SellerMarketplaceAccount.id == -1,
            )
            listing_query = MarketplaceListing.query.filter(
                MarketplaceListing.id == -1,
            )
        else:
            account_query = SellerMarketplaceAccount.query.filter_by(
                seller_id=seller_id,
                marketplace_id=ozon.id,
            )
            listing_query = MarketplaceListing.query.filter_by(
                seller_id=seller_id,
                marketplace_id=ozon.id,
            )
        accounts = account_query.order_by(SellerMarketplaceAccount.id).all()
        account_statuses = cls._status_counts(
            account_query,
            SellerMarketplaceAccount.connection_status,
        )
        active_accounts = [account for account in accounts if account.is_active]
        expired_accounts = sum(
            1
            for account in active_accounts
            if account.credential_expires_at
            and account.credential_expires_at <= current_time
        )
        connected_accounts = [
            account
            for account in active_accounts
            if account.connection_status == "connected"
        ]
        accounts_summary = {
            "total": len(accounts),
            "active": len(active_accounts),
            "connected": len(connected_accounts),
            "with_credentials": sum(
                1 for account in accounts if account._credentials_encrypted
            ),
            "connected_without_credentials": sum(
                1
                for account in connected_accounts
                if not account._credentials_encrypted
            ),
            "unhealthy_active": sum(
                1
                for account in active_accounts
                if account.connection_status != "connected"
            ),
            "expired": expired_accounts,
            "statuses": account_statuses,
            "items": [
                {
                    "id": account.id,
                    "label": account.label,
                    "is_active": bool(account.is_active),
                    "connection_status": account.connection_status,
                    "has_credentials": bool(account._credentials_encrypted),
                    "credential_expires_at": (
                        account.credential_expires_at.isoformat()
                        if account.credential_expires_at else None
                    ),
                    "connection_checked_at": (
                        account.connection_checked_at.isoformat()
                        if account.connection_checked_at else None
                    ),
                    "last_error_code": account.last_error_code,
                }
                for account in accounts
            ],
        }

        listing_statuses = cls._status_counts(
            listing_query,
            MarketplaceListing.normalized_status,
        )
        listings_summary = {
            "total": listing_query.count(),
            "available": listing_query.filter(
                MarketplaceListing.is_available.is_(True)
            ).count(),
            "linked": listing_query.filter(
                MarketplaceListing.imported_product_id.isnot(None)
            ).count(),
            "statuses": listing_statuses,
        }

        ozon_marketplace_id = ozon.id if ozon is not None else -1
        drafts_query = MarketplaceProductDraft.query.filter_by(
            seller_id=seller_id,
            marketplace_id=ozon_marketplace_id,
        )
        drafts_summary = {
            "total": drafts_query.count(),
            "validation": cls._status_counts(
                drafts_query,
                MarketplaceProductDraft.validation_status,
            ),
        }
        operations_query = MarketplaceOperation.query.filter_by(
            seller_id=seller_id,
            marketplace_id=ozon_marketplace_id,
        )
        operation_statuses = cls._status_counts(
            operations_query,
            MarketplaceOperation.status,
        )
        operations_summary = {
            "total": operations_query.count(),
            "active": sum(
                operation_statuses.get(status, 0)
                for status in cls.OPERATION_ACTIVE
            ),
            "uncertain": operation_statuses.get("uncertain", 0),
            "partial": operation_statuses.get("partial", 0),
            "failed": operation_statuses.get("failed", 0),
            "statuses": operation_statuses,
        }
        proposals_query = MarketplaceCommercialProposal.query.filter_by(
            seller_id=seller_id,
            marketplace_id=ozon_marketplace_id,
        )
        proposal_statuses = cls._status_counts(
            proposals_query,
            MarketplaceCommercialProposal.status,
        )
        proposals_summary = {
            "total": proposals_query.count(),
            "needs_attention": sum(
                proposal_statuses.get(status, 0)
                for status in ("pending_review", "uncertain", "conflict")
            ),
            "statuses": proposal_statuses,
        }

        syncs = {}
        for name, model in cls.SYNC_MODELS:
            query = model.query.filter_by(
                seller_id=seller_id,
                marketplace_id=ozon_marketplace_id,
            )
            syncs[name] = {
                "total": query.count(),
                "statuses": cls._status_counts(query, model.status),
            }

        reference_fresh = bool(
            ozon is not None
            and ozon.categories_sync_status == "success"
            and ozon.categories_synced_at is not None
            and ozon.categories_synced_at >= current_time - timedelta(
                hours=OzonReferenceService.HARD_TTL_HOURS,
            )
            and ozon.categories_snapshot_hash
        )
        reference = {
            "available": ozon is not None,
            "fresh": reference_fresh,
            "snapshot_present": bool(
                ozon and ozon.categories_snapshot_hash
            ),
            "categories_status": ozon.categories_sync_status if ozon else None,
            "categories_synced_at": (
                ozon.categories_synced_at.isoformat()
                if ozon and ozon.categories_synced_at else None
            ),
            "categories_version": int(ozon.categories_version or 0) if ozon else 0,
            "total_categories": int(ozon.total_categories or 0) if ozon else 0,
            "total_product_types": int(ozon.total_product_types or 0) if ozon else 0,
        }

        blockers = []
        warnings = []
        if not flags["wb_projection"]:
            blockers.append("wb_projection_disabled")
        if not flags["wb_dual_read"]:
            blockers.append("wb_dual_read_disabled")
        if not projection["cutover_ready"]:
            blockers.extend(projection["blockers"])
        if flags["wb_common_read"] and not projection["cutover_ready"]:
            blockers.append("wb_common_read_requested_before_parity")
        if not flags["ozon_read"]:
            warnings.append("ozon_read_dark")
        if accounts_summary["connected"] == 0:
            blockers.append("ozon_connected_account_missing")
        if accounts_summary["connected_without_credentials"]:
            blockers.append("ozon_credentials_missing")
        if accounts_summary["unhealthy_active"]:
            blockers.append("ozon_active_account_unhealthy")
        if accounts_summary["expired"]:
            blockers.append("ozon_credentials_expired")
        if not reference["fresh"]:
            blockers.append("ozon_reference_not_ready")
        if operations_summary["uncertain"]:
            blockers.append("ozon_uncertain_operations")
        if operations_summary["partial"] or operations_summary["failed"]:
            warnings.append("ozon_terminal_operations_need_review")
        if proposals_summary["needs_attention"]:
            warnings.append("ozon_proposals_need_review")
        blockers = list(dict.fromkeys(blockers))
        warnings = list(dict.fromkeys(warnings))

        stage_ready = not blockers
        publication_ready = bool(
            stage_ready
            and flags["ozon_read"]
            and flags["ozon_publication"]
        )
        return {
            "seller_id": seller_id,
            "generated_at": current_time.isoformat(),
            "stage_ready": stage_ready,
            "publication_ready": publication_ready,
            "blockers": blockers,
            "warnings": warnings,
            "flags": flags,
            "wb_projection": projection,
            "ozon": {
                "accounts": accounts_summary,
                "reference": reference,
                "listings": listings_summary,
                "drafts": drafts_summary,
                "operations": operations_summary,
                "commercial_proposals": proposals_summary,
                "syncs": syncs,
            },
        }
