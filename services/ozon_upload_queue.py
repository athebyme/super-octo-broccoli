"""Bounded local preparation for explicitly staged Ozon card uploads.

This module never calls an Ozon adapter.  ``MarketplaceOperation`` owns every
provider attempt.  A run lease fences worker transitions after draft methods
commit independently, and the reviewed enqueue path repeats that fence inside
the operation/item transaction.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
import secrets
import time
from typing import Optional

from flask import current_app
from sqlalchemy import or_

from models import (
    BackgroundJob,
    ImportedProduct,
    MarketplaceOperation,
    OzonBulkUploadItem,
    OzonBulkUploadRun,
    SellerMarketplaceAccount,
    db,
)
from services.marketplace_drafts import MarketplaceDraftError, MarketplaceDraftService
from services.marketplace_product_links import (
    MarketplaceProductLinkError,
    MarketplaceProductLinkService,
)
from services.marketplace_publications import (
    MarketplacePublicationError,
    MarketplacePublicationService,
)


logger = logging.getLogger(__name__)


class OzonUploadLeaseLost(RuntimeError):
    """A newer worker owns the run; the stale worker must not persist."""


@dataclass(frozen=True)
class ItemAdvanceResult:
    processed: bool
    phase: str
    outcome: str
    operation_id: Optional[int] = None
    next_due_at: Optional[datetime] = None


class OzonUploadQueueService:
    LEASE_SECONDS = 120
    REFERENCE_WAIT_SECONDS = 6 * 60 * 60
    REFERENCE_RECHECK = timedelta(seconds=60)
    LOCAL_FAILURE_DELAYS = (
        timedelta(minutes=1),
        timedelta(minutes=5),
        timedelta(minutes=15),
    )
    ACTIVE_PHASES = {"pending", "preparing", "waiting_reference", "reviewed"}
    SOURCE_TERMINAL_PHASES = {
        "prepared", "needs_input", "failed_local", "excluded",
        "needs_manual_reconciliation",
    }

    @classmethod
    def claim_due_run(
        cls, *, now: datetime, exclude_run_ids: set[int],
    ) -> Optional[tuple[int, str]]:
        """Claim one due run using an atomic update, never holding a network I/O lock."""
        candidates = OzonBulkUploadRun.query.with_entities(
            OzonBulkUploadRun.id,
        ).filter(
            OzonBulkUploadRun.state == "active",
            OzonBulkUploadRun.next_due_at.isnot(None),
            OzonBulkUploadRun.next_due_at <= now,
            or_(
                OzonBulkUploadRun.lease_until.is_(None),
                OzonBulkUploadRun.lease_until <= now,
            ),
            ~OzonBulkUploadRun.id.in_(exclude_run_ids or {-1}),
        ).order_by(
            OzonBulkUploadRun.next_due_at.asc(),
            OzonBulkUploadRun.last_attempt_at.asc(),
            OzonBulkUploadRun.id.asc(),
        ).limit(100).all()
        for (run_id,) in candidates:
            token = secrets.token_hex(16)
            updated = OzonBulkUploadRun.query.filter(
                OzonBulkUploadRun.id == run_id,
                OzonBulkUploadRun.state == "active",
                OzonBulkUploadRun.next_due_at <= now,
                or_(
                    OzonBulkUploadRun.lease_until.is_(None),
                    OzonBulkUploadRun.lease_until <= now,
                ),
            ).update({
                OzonBulkUploadRun.lease_token: token,
                OzonBulkUploadRun.lease_until: now + timedelta(
                    seconds=cls.LEASE_SECONDS,
                ),
                OzonBulkUploadRun.last_attempt_at: now,
                OzonBulkUploadRun.updated_at: now,
            }, synchronize_session=False)
            db.session.commit()
            if updated == 1:
                return run_id, token
        return None

    @classmethod
    def lease_current(
        cls, *, run_id: int, lease_token: str, now: datetime,
    ) -> bool:
        return db.session.query(OzonBulkUploadRun.id).filter(
            OzonBulkUploadRun.id == run_id,
            OzonBulkUploadRun.state == "active",
            OzonBulkUploadRun.lease_token == lease_token,
            OzonBulkUploadRun.lease_until.isnot(None),
            OzonBulkUploadRun.lease_until > now,
        ).first() is not None

    @classmethod
    def _fence_lease(
        cls, *, run_id: int, lease_token: str, now: datetime,
    ) -> None:
        """Issue a write CAS inside the same transaction as the item mutation."""
        updated = OzonBulkUploadRun.query.filter(
            OzonBulkUploadRun.id == run_id,
            OzonBulkUploadRun.state == "active",
            OzonBulkUploadRun.lease_token == lease_token,
            OzonBulkUploadRun.lease_until.isnot(None),
            OzonBulkUploadRun.lease_until > now,
        ).update({OzonBulkUploadRun.updated_at: now}, synchronize_session=False)
        if updated != 1:
            db.session.rollback()
            raise OzonUploadLeaseLost("Ozon upload run lease changed")

    @classmethod
    def transition_item(
        cls, *, run_id: int, item_id: int, lease_token: str,
        now: datetime, phase: str, next_due_at: Optional[datetime] = None,
        draft_id: Optional[int] = None,
        prepared_version: Optional[int] = None,
        offer_id: Optional[str] = None,
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
        reference_wait_started_at: Optional[datetime] = None,
        local_failure_count: Optional[int] = None,
    ) -> OzonBulkUploadItem:
        """Commit a post-draft local transition only under the current lease."""
        cls._fence_lease(run_id=run_id, lease_token=lease_token, now=now)
        item = OzonBulkUploadItem.query.filter_by(
            id=item_id, run_id=run_id,
        ).first()
        if item is None or item.operation_id is not None:
            db.session.rollback()
            raise OzonUploadLeaseLost("Ozon upload item changed")
        item.phase = phase
        item.next_due_at = next_due_at
        item.error_code = error_code
        item.error_message = error_message
        item.updated_at = now
        if draft_id is not None:
            item.draft_id = draft_id
        if prepared_version is not None:
            item.prepared_version = prepared_version
        if offer_id is not None:
            item.offer_id_snapshot = offer_id
        if reference_wait_started_at is not None:
            item.reference_wait_started_at = reference_wait_started_at
        if local_failure_count is not None:
            item.local_failure_count = local_failure_count
        db.session.commit()
        return item

    @classmethod
    def _safe_text(cls, value: object, maximum: int) -> Optional[str]:
        if not isinstance(value, str):
            return None
        clean = " ".join(value.strip().split())
        return clean[:maximum] or None

    @classmethod
    def _local_failure(
        cls, *, run_id: int, item_id: int, lease_token: str,
        now: datetime, exc: Exception,
    ) -> ItemAdvanceResult:
        db.session.rollback()
        item = OzonBulkUploadItem.query.filter_by(
            id=item_id, run_id=run_id,
        ).first()
        if item is None:
            raise OzonUploadLeaseLost("Ozon upload item disappeared")
        failures = min(3, item.local_failure_count + 1)
        due = (
            now + cls.LOCAL_FAILURE_DELAYS[failures - 1]
            if failures < 3 else None
        )
        run = OzonBulkUploadRun.query.filter_by(id=run_id).first()
        phase = (
            "reviewed" if run is not None and run.mode == "reviewed_drafts"
            else "preparing"
        ) if due is not None else "failed_local"
        code = cls._safe_text(getattr(exc, "code", None), 100)
        if code is None:
            code = "upload_local_preparation_failed"
        cls.transition_item(
            run_id=run_id, item_id=item_id, lease_token=lease_token,
            now=now, phase=phase, next_due_at=due,
            error_code=code,
            error_message=(
                cls._safe_text(str(exc), 700)
                if isinstance(exc, (MarketplaceDraftError, MarketplacePublicationError,
                                    MarketplaceProductLinkError))
                else "Локальная подготовка прервалась; безопасно повторим после паузы"
            ),
            local_failure_count=failures,
        )
        return ItemAdvanceResult(True, phase, "failed", next_due_at=due)

    @classmethod
    def _source_item(
        cls, *, run: OzonBulkUploadRun, item: OzonBulkUploadItem,
        lease_token: str, now: datetime,
    ) -> ItemAdvanceResult:
        from services.supplier_service import hydrate_missing_imported_observed_snapshot
        from services.ozon_bulk_upload import OzonBulkUploadService

        if not current_app.config.get("MARKETPLACE_OZON_ENABLED", False):
            due = now + cls.REFERENCE_RECHECK
            cls.transition_item(
                run_id=run.id, item_id=item.id, lease_token=lease_token,
                now=now, phase=item.phase, next_due_at=due,
                error_code="ozon_feature_disabled",
                error_message="Подготовка Ozon временно выключена оператором",
            )
            return ItemAdvanceResult(True, item.phase, "waiting", next_due_at=due)
        account = SellerMarketplaceAccount.query.filter_by(
            id=run.account_id, seller_id=run.seller_id,
        ).first()
        if account is None or not account.is_active:
            cls.transition_item(
                run_id=run.id, item_id=item.id, lease_token=lease_token,
                now=now, phase="needs_input",
                error_code="ozon_account_disabled",
                error_message="Кабинет Ozon отключён; проверьте его перед подготовкой",
            )
            return ItemAdvanceResult(True, "needs_input", "needs_input")
        product = ImportedProduct.query.filter_by(
            id=item.imported_product_id, seller_id=run.seller_id,
        ).first()
        if product is None:
            cls.transition_item(
                run_id=run.id, item_id=item.id, lease_token=lease_token,
                now=now, phase="needs_input",
                error_code="source_product_missing",
                error_message="Исходный товар больше не доступен продавцу",
            )
            return ItemAdvanceResult(True, "needs_input", "needs_input")

        try:
            link_result = MarketplaceProductLinkService.reconcile_account_products(
                seller_id=run.seller_id, account_id=run.account_id,
                products=[product],
            )
            blocked = link_result["blocked"].get(product.id)
            if blocked is not None:
                cls.transition_item(
                    run_id=run.id, item_id=item.id, lease_token=lease_token,
                    now=now, phase="needs_input",
                    error_code=cls._safe_text(blocked.get("code"), 100),
                    error_message=cls._safe_text(blocked.get("message"), 700),
                )
                return ItemAdvanceResult(True, "needs_input", "needs_input")
            hydrate_missing_imported_observed_snapshot(product)
            draft = MarketplaceDraftService.create_draft(
                seller_id=run.seller_id, account_id=run.account_id,
                imported_product_id=product.id,
                corrected_by_user_id=run.created_by_user_id,
                source_link_preflight=False,
                observed_mapping_preflight=False,
            )
            cls.transition_item(
                run_id=run.id, item_id=item.id, lease_token=lease_token,
                now=datetime.utcnow(), phase="preparing", next_due_at=now,
                draft_id=draft.id, prepared_version=draft.version,
                offer_id=draft.offer_id,
            )
            active = MarketplaceOperation.query.filter(
                MarketplaceOperation.seller_id == run.seller_id,
                MarketplaceOperation.account_id == run.account_id,
                MarketplaceOperation.draft_id == draft.id,
                MarketplaceOperation.status.in_(
                    MarketplacePublicationService.ACTIVE_STATUSES,
                ),
            ).first()
            if active is not None:
                cls.transition_item(
                    run_id=run.id, item_id=item.id, lease_token=lease_token,
                    now=datetime.utcnow(), phase="needs_input",
                    error_code="already_in_progress",
                    error_message="По черновику уже выполняется операция Ozon",
                )
                return ItemAdvanceResult(True, "needs_input", "needs_input")
            draft = MarketplaceDraftService.rebase_source_defaults(
                seller_id=run.seller_id, draft_id=draft.id,
                expected_version=draft.version,
            )
            draft = MarketplaceDraftService.apply_reference_defaults(
                seller_id=run.seller_id, draft_id=draft.id,
                expected_version=draft.version,
            )
            draft = MarketplaceDraftService.apply_account_defaults(
                seller_id=run.seller_id, draft_id=draft.id,
                expected_version=draft.version,
            )
            draft = MarketplaceDraftService.validate_draft(
                seller_id=run.seller_id, draft_id=draft.id,
                expected_version=draft.version,
            )
            if draft.status != "ready":
                if OzonBulkUploadService._reference_wait_required(draft):
                    started = item.reference_wait_started_at or now
                    if (now - started).total_seconds() >= cls.REFERENCE_WAIT_SECONDS:
                        cls.transition_item(
                            run_id=run.id, item_id=item.id,
                            lease_token=lease_token, now=datetime.utcnow(),
                            phase="needs_input",
                            error_code="ozon_reference_sync_timeout",
                            error_message="Справочник Ozon не загрузился за 6 часов",
                            draft_id=draft.id, prepared_version=draft.version,
                        )
                        return ItemAdvanceResult(True, "needs_input", "needs_input")
                    due = now + cls.REFERENCE_RECHECK
                    cls.transition_item(
                        run_id=run.id, item_id=item.id,
                        lease_token=lease_token, now=datetime.utcnow(),
                        phase="waiting_reference", next_due_at=due,
                        draft_id=draft.id, prepared_version=draft.version,
                        reference_wait_started_at=started,
                        error_code="ozon_reference_sync_pending",
                        error_message="Загружаем официальную схему Ozon",
                    )
                    return ItemAdvanceResult(True, "waiting_reference", "waiting", next_due_at=due)
                errors = OzonBulkUploadService._validation_errors(draft)
                cls.transition_item(
                    run_id=run.id, item_id=item.id,
                    lease_token=lease_token, now=datetime.utcnow(),
                    phase="needs_input", draft_id=draft.id,
                    prepared_version=draft.version,
                    error_code=(errors[0]["code"] if errors else "draft_not_ready"),
                    error_message=(errors[0]["message"] if errors else
                                   "Черновик требует проверки"),
                )
                return ItemAdvanceResult(True, "needs_input", "needs_input")
            cls.transition_item(
                run_id=run.id, item_id=item.id, lease_token=lease_token,
                now=datetime.utcnow(), phase="prepared", draft_id=draft.id,
                prepared_version=draft.version, offer_id=draft.offer_id,
            )
            return ItemAdvanceResult(True, "prepared", "prepared")
        except OzonUploadLeaseLost:
            raise
        except MarketplaceProductLinkError as exc:
            db.session.rollback()
            cls.transition_item(
                run_id=run.id, item_id=item.id,
                lease_token=lease_token, now=datetime.utcnow(),
                phase="needs_input",
                error_code=cls._safe_text(getattr(exc, "code", None), 100)
                or "source_link_review_required",
                error_message=cls._safe_text(str(exc), 700)
                or "Проверьте связь исходного товара с Ozon",
            )
            return ItemAdvanceResult(True, "needs_input", "needs_input")
        except Exception as exc:
            logger.exception(
                "Ozon local preparation failed run_id=%s item_id=%s",
                run.id, item.id,
            )
            return cls._local_failure(
                run_id=run.id, item_id=item.id,
                lease_token=lease_token, now=datetime.utcnow(), exc=exc,
            )

    @classmethod
    def _reviewed_item(
        cls, *, run: OzonBulkUploadRun, item: OzonBulkUploadItem,
        lease_token: str, now: datetime,
    ) -> ItemAdvanceResult:
        if not (
            current_app.config.get("MARKETPLACE_OZON_ENABLED", False)
            and current_app.config.get("MARKETPLACE_OZON_PUBLICATION_ENABLED", False)
        ):
            due = now + cls.REFERENCE_RECHECK
            cls.transition_item(
                run_id=run.id, item_id=item.id, lease_token=lease_token,
                now=now, phase="reviewed", next_due_at=due,
                error_code="ozon_publication_disabled",
                error_message="Отправка Ozon временно выключена оператором",
            )
            return ItemAdvanceResult(True, "reviewed", "waiting", next_due_at=due)
        try:
            from services.ozon_bulk_upload import OzonBulkUploadService
            OzonBulkUploadService._upload_account(
                seller_id=run.seller_id, account_id=run.account_id,
            )
            operation = MarketplacePublicationService.enqueue_reviewed_upload_item(
                seller_id=run.seller_id, account_id=run.account_id,
                draft_id=item.reviewed_draft_id,
                expected_version=item.reviewed_version,
                run_item_id=item.id, lease_token=lease_token,
                created_by_user_id=run.created_by_user_id,
                now=now,
            )
            return ItemAdvanceResult(
                True, "operation_linked", "enqueued",
                operation_id=operation.id,
            )
        except OzonUploadLeaseLost:
            raise
        except Exception as exc:
            from services.ozon_bulk_upload import OzonBulkUploadError
            if isinstance(exc, OzonBulkUploadError):
                db.session.rollback()
                cls.transition_item(
                    run_id=run.id, item_id=item.id, lease_token=lease_token,
                    now=datetime.utcnow(), phase="needs_input",
                    error_code=cls._safe_text(getattr(exc, "code", None), 100)
                    or "ozon_account_not_connected",
                    error_message=cls._safe_text(str(exc), 700),
                )
                return ItemAdvanceResult(True, "needs_input", "needs_input")
            if not isinstance(exc, MarketplacePublicationError):
                logger.exception(
                    "Ozon reviewed enqueue failed run_id=%s item_id=%s",
                    run.id, item.id,
                )
                return cls._local_failure(
                    run_id=run.id, item_id=item.id,
                    lease_token=lease_token, now=datetime.utcnow(), exc=exc,
                )
            db.session.rollback()
            cls.transition_item(
                run_id=run.id, item_id=item.id, lease_token=lease_token,
                now=datetime.utcnow(), phase="needs_input",
                error_code=cls._safe_text(getattr(exc, "code", None), 100)
                or "publication_enqueue_failed",
                error_message=cls._safe_text(str(exc), 700)
                or "Карточка требует повторной проверки",
            )
            return ItemAdvanceResult(True, "needs_input", "needs_input")

    @classmethod
    def advance_item(
        cls, *, run_id: int, item_id: int, lease_token: str,
        now: Optional[datetime] = None,
    ) -> ItemAdvanceResult:
        current_time = now or datetime.utcnow()
        if not cls.lease_current(
            run_id=run_id, lease_token=lease_token, now=current_time,
        ):
            raise OzonUploadLeaseLost("Ozon upload run lease expired")
        run = OzonBulkUploadRun.query.filter_by(id=run_id, state="active").first()
        item = OzonBulkUploadItem.query.filter_by(
            id=item_id, run_id=run_id,
        ).first()
        if run is None or item is None:
            raise OzonUploadLeaseLost("Ozon upload run or item changed")
        if item.phase not in cls.ACTIVE_PHASES:
            return ItemAdvanceResult(False, item.phase, "skipped", item.operation_id)
        if item.next_due_at is not None and item.next_due_at > current_time:
            return ItemAdvanceResult(False, item.phase, "skipped", item.operation_id)
        if run.mode == "source_prepare":
            return cls._source_item(
                run=run, item=item, lease_token=lease_token, now=current_time,
            )
        return cls._reviewed_item(
            run=run, item=item, lease_token=lease_token, now=current_time,
        )

    @classmethod
    def release_run(
        cls, *, run_id: int, lease_token: str, now: datetime,
        next_due_at: Optional[datetime], completed: bool,
    ) -> None:
        updated = OzonBulkUploadRun.query.filter(
            OzonBulkUploadRun.id == run_id,
            OzonBulkUploadRun.state == "active",
            OzonBulkUploadRun.lease_token == lease_token,
            OzonBulkUploadRun.lease_until.isnot(None),
            OzonBulkUploadRun.lease_until > now,
        ).update({
            OzonBulkUploadRun.lease_token: None,
            OzonBulkUploadRun.lease_until: None,
            OzonBulkUploadRun.state: "completed" if completed else "active",
            OzonBulkUploadRun.next_due_at: None if completed else next_due_at,
            OzonBulkUploadRun.updated_at: now,
        }, synchronize_session=False)
        if updated != 1:
            db.session.rollback()
            raise OzonUploadLeaseLost("Ozon upload run lease changed")
        db.session.commit()

    @classmethod
    def _mapping_preflight(
        cls, *, run: OzonBulkUploadRun, lease_token: str, now: datetime,
    ) -> str:
        """Perform the full seller evidence scan once, outside the HTTP request.

        An incomplete scan cannot bless a selected subset: the old automatic
        mappings might have contrary evidence elsewhere in the seller catalog.
        """
        if run.mode != "source_prepare" or run.mapping_preflight_at is not None:
            return "ready"
        if not cls.lease_current(
            run_id=run.id, lease_token=lease_token, now=now,
        ):
            raise OzonUploadLeaseLost("Ozon upload run lease expired")
        from services.marketplace_accounts import MarketplaceAccountService

        account = MarketplaceAccountService.get_owned_account(
            seller_id=run.seller_id, account_id=run.account_id,
            marketplace_code="ozon",
        )
        if not account.is_active:
            cls._manual_all(
                run_id=run.id, lease_token=lease_token,
                code="ozon_account_disabled",
                message="Кабинет Ozon отключён; проверьте его перед подготовкой",
            )
            return "manual"
        result = MarketplaceDraftService.reconcile_observed_category_mappings(
            seller_id=run.seller_id, marketplace_id=account.marketplace_id,
        )
        finished = datetime.utcnow()
        if result.get("success") is True:
            cls._fence_lease(
                run_id=run.id, lease_token=lease_token, now=finished,
            )
            run = OzonBulkUploadRun.query.filter_by(id=run.id).first()
            run.mapping_preflight_at = finished
            db.session.commit()
            return "ready"
        code = result.get("code")
        if code == "observed_category_mapping_busy":
            return "busy"
        # The scan is intentionally hard bounded. A failed/truncated scan
        # cannot become a successful mapping marker or selected-card scan.
        cls._manual_all(
            run_id=run.id, lease_token=lease_token,
            code=cls._safe_text(code, 100)
            or "observed_category_mapping_unavailable",
            message=(
                "Автоматическое сопоставление категории не проверено по всему "
                "каталогу; выберите категорию вручную"
            ),
        )
        return "manual"

    @classmethod
    def _manual_all(
        cls, *, run_id: int, lease_token: str, code: str,
        message: str,
    ) -> None:
        now = datetime.utcnow()
        cls._fence_lease(run_id=run_id, lease_token=lease_token, now=now)
        OzonBulkUploadItem.query.filter(
            OzonBulkUploadItem.run_id == run_id,
            OzonBulkUploadItem.phase.in_(cls.ACTIVE_PHASES),
            OzonBulkUploadItem.operation_id.is_(None),
        ).update({
            OzonBulkUploadItem.phase: "needs_input",
            OzonBulkUploadItem.next_due_at: None,
            OzonBulkUploadItem.error_code: code,
            OzonBulkUploadItem.error_message: message,
            OzonBulkUploadItem.updated_at: now,
        }, synchronize_session=False)
        db.session.commit()

    @classmethod
    def _next_item(
        cls, *, run_id: int, now: datetime,
    ) -> Optional[OzonBulkUploadItem]:
        return OzonBulkUploadItem.query.filter(
            OzonBulkUploadItem.run_id == run_id,
            OzonBulkUploadItem.phase.in_(cls.ACTIVE_PHASES),
            OzonBulkUploadItem.next_due_at.isnot(None),
            OzonBulkUploadItem.next_due_at <= now,
        ).order_by(
            OzonBulkUploadItem.next_due_at.asc(),
            OzonBulkUploadItem.ordinal.asc(),
        ).first()

    @classmethod
    def _release_after_step(
        cls, *, run_id: int, lease_token: str,
        defer_until: Optional[datetime] = None,
    ) -> None:
        next_due = db.session.query(
            db.func.min(OzonBulkUploadItem.next_due_at),
        ).filter(
            OzonBulkUploadItem.run_id == run_id,
            OzonBulkUploadItem.phase.in_(cls.ACTIVE_PHASES),
        ).scalar()
        release_time = datetime.utcnow()
        # A run with more immediately due items moves behind older accounts.
        # Keeping the first item's ancient due would hide run 21 behind the
        # first 20 large runs until all their cards had been prepared.
        if next_due is not None:
            next_due = max(next_due, release_time)
        cls.release_run(
            run_id=run_id, lease_token=lease_token, now=release_time,
            next_due_at=(
                max(next_due, defer_until)
                if next_due is not None and defer_until is not None
                else next_due
            ),
            completed=next_due is None,
        )

    @classmethod
    def run_due_preparation(
        cls, *, now: Optional[datetime] = None, run_limit: int = 20,
        item_limit: int = 40, seconds_budget: int = 45,
    ) -> dict:
        """One bounded scheduler tick, one item per run before a second pass."""
        if not 1 <= run_limit <= 20 or not 1 <= item_limit <= 40:
            raise ValueError("Ozon upload worker budget exceeds the hard cap")
        if not 1 <= seconds_budget <= 45:
            raise ValueError("Ozon upload worker time budget exceeds the hard cap")
        result = {key: 0 for key in (
            "selected", "processed_items", "prepared", "enqueued",
            "waiting", "needs_input", "busy", "failed",
        )}
        deadline = time.monotonic() + seconds_budget
        selected_ids: list[int] = []
        counts: dict[int, int] = {}
        from services.ozon_bulk_upload import OzonBulkUploadService

        while len(selected_ids) < run_limit and time.monotonic() < deadline:
            current = now or datetime.utcnow()
            claimed = cls.claim_due_run(
                now=current, exclude_run_ids=set(selected_ids),
            )
            if claimed is None:
                break
            run_id, token = claimed
            selected_ids.append(run_id)
            result["selected"] += 1
            counts[run_id] = 0
            cls._work_one_claim(
                run_id=run_id, lease_token=token, result=result,
                counts=counts, deadline=deadline,
            )
            if result["processed_items"] >= item_limit:
                break
        # A second pass lets a small run make progress without allowing the
        # first active run to monopolize the tick ahead of run 21.
        while (
            selected_ids and result["processed_items"] < item_limit
            and time.monotonic() < deadline
        ):
            progressed = False
            for run_id in selected_ids:
                if (
                    result["processed_items"] >= item_limit
                    or time.monotonic() >= deadline
                ):
                    break
                if counts[run_id] >= 20:
                    continue
                current = now or datetime.utcnow()
                token = secrets.token_hex(16)
                claimed = OzonBulkUploadRun.query.filter(
                    OzonBulkUploadRun.id == run_id,
                    OzonBulkUploadRun.state == "active",
                    OzonBulkUploadRun.next_due_at <= current,
                    or_(OzonBulkUploadRun.lease_until.is_(None),
                        OzonBulkUploadRun.lease_until <= current),
                ).update({
                    OzonBulkUploadRun.lease_token: token,
                    OzonBulkUploadRun.lease_until: current + timedelta(
                        seconds=cls.LEASE_SECONDS,
                    ),
                    OzonBulkUploadRun.last_attempt_at: current,
                }, synchronize_session=False)
                db.session.commit()
                if claimed != 1:
                    continue
                before = result["processed_items"]
                cls._work_one_claim(
                    run_id=run_id, lease_token=token, result=result,
                    counts=counts, deadline=deadline,
                )
                progressed = progressed or result["processed_items"] > before
            if not progressed:
                break
        for run_id in selected_ids:
            run = OzonBulkUploadRun.query.filter_by(id=run_id).first()
            if run is not None:
                OzonBulkUploadService.reconcile_run(
                    seller_id=run.seller_id,
                    job_uid=BackgroundJob.query.filter_by(id=run.job_id).first().job_uid,
                )
        return result

    @classmethod
    def _work_one_claim(
        cls, *, run_id: int, lease_token: str, result: dict,
        counts: dict[int, int], deadline: float,
    ) -> None:
        defer_until = None
        try:
            run = OzonBulkUploadRun.query.filter_by(id=run_id).first()
            if run is None:
                raise OzonUploadLeaseLost("Ozon upload run disappeared")
            if run.mode == "source_prepare" and run.mapping_preflight_at is None:
                if not current_app.config.get("MARKETPLACE_OZON_ENABLED", False):
                    defer_until = datetime.utcnow() + cls.REFERENCE_RECHECK
                    result["waiting"] += 1
                    return
                if time.monotonic() >= deadline - 5:
                    result["busy"] += 1
                    return
                preflight = cls._mapping_preflight(
                    run=run, lease_token=lease_token, now=datetime.utcnow(),
                )
                if preflight != "ready":
                    result["busy" if preflight == "busy" else "needs_input"] += 1
                    if preflight == "busy":
                        # Durable backoff; the mapping lock is a local resource.
                        defer_until = datetime.utcnow() + cls.REFERENCE_RECHECK
                    return
            item = cls._next_item(run_id=run_id, now=datetime.utcnow())
            if item is None or time.monotonic() >= deadline:
                return
            advanced = cls.advance_item(
                run_id=run_id, item_id=item.id, lease_token=lease_token,
            )
            if advanced.processed:
                counts[run_id] += 1
                result["processed_items"] += 1
                key = {
                    "prepared": "prepared", "enqueued": "enqueued",
                    "waiting": "waiting", "needs_input": "needs_input",
                    "failed": "failed",
                }.get(advanced.outcome)
                if key:
                    result[key] += 1
        except OzonUploadLeaseLost:
            db.session.rollback()
            result["busy"] += 1
        except Exception:
            db.session.rollback()
            logger.exception("Ozon upload worker run_id=%s failed", run_id)
            result["failed"] += 1
        finally:
            try:
                cls._release_after_step(
                    run_id=run_id, lease_token=lease_token,
                    defer_until=defer_until,
                )
            except OzonUploadLeaseLost:
                db.session.rollback()
