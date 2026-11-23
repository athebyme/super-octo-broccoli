"""Bounded local preparation for explicitly staged Ozon card uploads.

This module never calls an Ozon adapter.  ``MarketplaceOperation`` owns every
provider attempt.  A run lease fences worker transitions after draft methods
commit independently, and the reviewed enqueue path repeats that fence inside
the operation/item transaction.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
import secrets
import time
from types import SimpleNamespace
from typing import Optional

from flask import current_app
from sqlalchemy import or_

from models import (
    BackgroundJob,
    ImportedProduct,
    MarketplaceOperation,
    MarketplaceProductDraft,
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


@dataclass(frozen=True)
class _SourceCategoryPreparation:
    draft: object
    retry_at: Optional[datetime] = None
    retry_reason: Optional[str] = None


class OzonUploadQueueService:
    LEASE_SECONDS = 120
    REFERENCE_WAIT_SECONDS = 6 * 60 * 60
    REFERENCE_RECHECK = timedelta(seconds=60)
    DECISION_CONTINUATION_WINDOW = timedelta(hours=6)
    MAX_DECISION_CONTINUATIONS = 3
    QUEUE_CREATED_DRAFT_CODE = "ozon_source_prepare_created"
    DECISION_CONTINUATION_CODE_PREFIX = "ozon_decisions_waiting_"
    DECISION_RETRY_REASONS = frozenset({
        "rate_limited", "cache_deferred", "cache_busy",
    })
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
    def _decision_continuation_count(cls, code: Optional[str]) -> Optional[int]:
        if code == cls.QUEUE_CREATED_DRAFT_CODE:
            return 0
        if not isinstance(code, str) or not code.startswith(
            cls.DECISION_CONTINUATION_CODE_PREFIX,
        ):
            return None
        suffix = code[len(cls.DECISION_CONTINUATION_CODE_PREFIX):]
        allowed = {
            str(value)
            for value in range(1, cls.MAX_DECISION_CONTINUATIONS + 1)
        }
        if suffix not in allowed:
            return None
        return int(suffix)

    @classmethod
    def _decision_continuation_deadline(
        cls,
        *,
        run: OzonBulkUploadRun,
    ) -> Optional[datetime]:
        run_created = run.created_at
        if type(run_created) is not datetime:
            return None
        if run_created.tzinfo is not None:
            try:
                run_created = run_created.astimezone(timezone.utc).replace(tzinfo=None)
            except (OverflowError, TypeError, ValueError):
                return None
        try:
            return run_created + cls.DECISION_CONTINUATION_WINDOW
        except OverflowError:
            return None

    @classmethod
    def _reference_wait_window_open(
        cls,
        *,
        item: OzonBulkUploadItem,
        now: datetime,
    ) -> bool:
        if item.phase != "waiting_reference":
            return True
        started = item.reference_wait_started_at
        if type(started) is not datetime or type(now) is not datetime:
            return False
        try:
            if started.tzinfo is not None:
                started = started.astimezone(timezone.utc).replace(tzinfo=None)
            if now.tzinfo is not None:
                now = now.astimezone(timezone.utc).replace(tzinfo=None)
        except (OverflowError, TypeError, ValueError):
            return False
        age = (now - started).total_seconds()
        return 0 <= age < cls.REFERENCE_WAIT_SECONDS

    @classmethod
    def _schedule_decision_continuation(
        cls,
        *,
        run: OzonBulkUploadRun,
        item: OzonBulkUploadItem,
        draft: MarketplaceProductDraft,
        lease_token: str,
        retry_at: Optional[datetime],
        retry_reason: str,
    ) -> Optional[ItemAdvanceResult]:
        """Persist one bounded local retry without shortening provider advice."""
        prior_count = cls._decision_continuation_count(item.error_code)
        if prior_count is None or prior_count >= cls.MAX_DECISION_CONTINUATIONS:
            return None
        now = datetime.utcnow()
        retry_count = prior_count + 1
        if retry_at is None:
            due = now + cls.LOCAL_FAILURE_DELAYS[
                min(prior_count, len(cls.LOCAL_FAILURE_DELAYS) - 1)
            ]
        else:
            if type(retry_at) is not datetime:
                logger.warning(
                    "Ozon Decisions deferred result had invalid retry_at; "
                    "automatic continuation disabled",
                )
                return None
            try:
                provider_due = (
                    retry_at.astimezone(timezone.utc).replace(tzinfo=None)
                    if retry_at.tzinfo is not None
                    else retry_at
                )
            except (OverflowError, TypeError, ValueError):
                logger.warning(
                    "Ozon Decisions deferred result had unusable retry_at; "
                    "automatic continuation disabled",
                )
                return None
            # Stay just beyond an expiring lease/cooldown; never shorten the
            # provider's not-before time.
            due = max(provider_due, now + timedelta(seconds=5))

        continuation_deadline = cls._decision_continuation_deadline(run=run)
        if continuation_deadline is None or now > continuation_deadline:
            return None
        if due > continuation_deadline:
            return None

        cls.transition_item(
            run_id=run.id,
            item_id=item.id,
            lease_token=lease_token,
            now=now,
            phase="preparing",
            next_due_at=due,
            draft_id=draft.id,
            prepared_version=draft.version,
            offer_id=draft.offer_id,
            error_code=f"{cls.DECISION_CONTINUATION_CODE_PREFIX}{retry_count}",
            error_message=(
                "Подготовка продолжится автоматически после временной паузы"
            ),
        )
        return ItemAdvanceResult(
            True, "preparing", "waiting", next_due_at=due,
        )

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
        deadline_monotonic: Optional[float] = None,
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
            prior_draft = MarketplaceProductDraft.query.filter_by(
                seller_id=run.seller_id,
                account_id=run.account_id,
                imported_product_id=product.id,
            ).first()
            prior_draft_id_before_create = (
                prior_draft.id if prior_draft is not None else None
            )
            item_draft_id_before_create = item.draft_id
            item_prepared_version_before_create = item.prepared_version
            from services.marketplace_drafts import DraftCreationReceipt
            creation_receipt = DraftCreationReceipt()
            draft = MarketplaceDraftService.create_draft(
                seller_id=run.seller_id, account_id=run.account_id,
                imported_product_id=product.id,
                corrected_by_user_id=run.created_by_user_id,
                source_link_preflight=False,
                observed_mapping_preflight=False,
                creation_receipt=creation_receipt,
            )
            current_draft_created_this_attempt = bool(
                creation_receipt.created_draft_id == draft.id
                and prior_draft_id_before_create is None
                and item_draft_id_before_create is None
            )
            lineage_attempt = cls._decision_continuation_count(item.error_code)
            continuation_deadline = cls._decision_continuation_deadline(run=run)
            continuation_window_open = (
                lineage_attempt == 0
                or (
                    lineage_attempt is not None
                    and continuation_deadline is not None
                    and now <= continuation_deadline
                )
            )
            queue_created_draft_continuation = bool(
                lineage_attempt is not None
                and continuation_window_open
                and cls._reference_wait_window_open(item=item, now=now)
                and item_draft_id_before_create == draft.id
                and item_prepared_version_before_create == draft.version
            )
            queue_created_draft_lineage = bool(
                current_draft_created_this_attempt
                or queue_created_draft_continuation
            )
            cls.transition_item(
                run_id=run.id, item_id=item.id, lease_token=lease_token,
                now=datetime.utcnow(), phase="preparing", next_due_at=now,
                draft_id=draft.id, prepared_version=draft.version,
                offer_id=draft.offer_id,
                error_code=(
                    (
                        item.error_code
                        if queue_created_draft_continuation
                        else cls.QUEUE_CREATED_DRAFT_CODE
                    )
                    if queue_created_draft_lineage else None
                ),
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
            category_preparation = cls._prepare_source_category(
                run=run,
                item=item,
                account=account,
                product=product,
                draft=draft,
                prior_draft_id_before_create=prior_draft_id_before_create,
                item_draft_id_before_create=item_draft_id_before_create,
                item_prepared_version_before_create=(
                    item_prepared_version_before_create
                ),
                current_draft_created_this_attempt=(
                    current_draft_created_this_attempt
                ),
                queue_created_draft_lineage=queue_created_draft_lineage,
                lease_token=lease_token,
                deadline_monotonic=deadline_monotonic,
            )
            draft = category_preparation.draft
            if category_preparation.retry_reason is not None:
                scheduled = cls._schedule_decision_continuation(
                    run=run,
                    item=item,
                    draft=draft,
                    lease_token=lease_token,
                    retry_at=category_preparation.retry_at,
                    retry_reason=category_preparation.retry_reason,
                )
                if scheduled is not None:
                    return scheduled
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
            from services.ozon_prepare_decisions import (
                OzonPrepareDecisionsService,
            )
            OzonPrepareDecisionsService.prepare_source_window(
                run=run,
                current_item=item,
                current_draft=draft,
                lease_token=lease_token,
                now=datetime.utcnow(),
                confirmed_queue_created_draft=queue_created_draft_lineage,
                deadline_monotonic=deadline_monotonic,
            )
            draft = MarketplaceDraftService.get_draft(
                seller_id=run.seller_id, draft_id=draft.id,
            )
            from services.ozon_brand_search import OzonBrandSearch
            OzonBrandSearch.prepare_source_brand(
                run=run,
                item=item,
                account=account,
                product=product,
                draft=draft,
                lease_token=lease_token,
                confirmed_queue_created_draft=queue_created_draft_lineage,
                deadline_monotonic=deadline_monotonic,
                now=datetime.utcnow(),
            )
            draft = MarketplaceDraftService.get_draft(
                seller_id=run.seller_id, draft_id=draft.id,
            )
            draft = MarketplaceDraftService.validate_draft(
                seller_id=run.seller_id, draft_id=draft.id,
                expected_version=draft.version,
                stage_automatic_cleanup=True,
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
                        error_code=(
                            item.error_code
                            if queue_created_draft_lineage
                            else "ozon_reference_sync_pending"
                        ),
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
    def _prepare_source_category(
        cls,
        *,
        run: OzonBulkUploadRun,
        item: OzonBulkUploadItem,
        account: SellerMarketplaceAccount,
        product: ImportedProduct,
        draft,
        prior_draft_id_before_create: Optional[int],
        item_draft_id_before_create: Optional[int],
        item_prepared_version_before_create: Optional[int],
        current_draft_created_this_attempt: bool,
        queue_created_draft_lineage: bool,
        lease_token: str,
        deadline_monotonic: Optional[float],
    ):
        """Apply one confidence-gated exact type choice inside the run fence."""
        fresh_create = current_draft_created_this_attempt is True and (
            prior_draft_id_before_create is None
            and item_draft_id_before_create is None
            and item_prepared_version_before_create is None
        )
        exact_retry = (
            queue_created_draft_lineage is True
            and type(prior_draft_id_before_create) is int
            and prior_draft_id_before_create == draft.id
            and item_draft_id_before_create == draft.id
            and item_prepared_version_before_create == draft.version
        )
        if not (fresh_create or exact_retry) or not (
            draft.status == "needs_category"
            and draft.product_type_id is None
            and draft.category_mapping_id is None
            and draft.external_category_id is None
            and draft.external_type_id is None
            and draft.published_listing_id is None
        ):
            return _SourceCategoryPreparation(draft=draft)
        try:
            from services.ozon_prepare_category_decisions import (
                build_ozon_category_tree_index,
                build_ozon_prepare_category_candidate,
                prepare_ozon_category_window,
                same_ozon_prepare_category_scope,
                selected_product_type_id,
            )
            from models import MarketplaceListing
            if MarketplaceListing.query.filter_by(
                seller_id=run.seller_id,
                marketplace_id=account.marketplace_id,
                account_id=account.id,
                imported_product_id=product.id,
            ).first() is not None:
                return _SourceCategoryPreparation(draft=draft)
            from services.openai_decisions_runtime import (
                create_openai_decisions_pipeline,
            )
            pipeline = create_openai_decisions_pipeline()
            if pipeline is None:
                return _SourceCategoryPreparation(draft=draft)

            if (
                deadline_monotonic is not None
                and deadline_monotonic - time.monotonic() < 22
            ):
                return _SourceCategoryPreparation(
                    draft=draft,
                    retry_reason="tick_budget",
                )

            now = datetime.utcnow()
            sibling_rows = db.session.query(
                OzonBulkUploadItem, ImportedProduct,
            ).join(
                ImportedProduct,
                ImportedProduct.id == OzonBulkUploadItem.imported_product_id,
            ).filter(
                OzonBulkUploadItem.run_id == run.id,
                OzonBulkUploadItem.id != item.id,
                OzonBulkUploadItem.phase.in_(("pending", "preparing")),
                OzonBulkUploadItem.next_due_at.isnot(None),
                OzonBulkUploadItem.next_due_at <= now,
                OzonBulkUploadItem.draft_id.is_(None),
                OzonBulkUploadItem.prepared_version.is_(None),
                OzonBulkUploadItem.reviewed_draft_id.is_(None),
                OzonBulkUploadItem.operation_id.is_(None),
                ImportedProduct.seller_id == run.seller_id,
            ).order_by(
                OzonBulkUploadItem.ordinal.asc(),
                OzonBulkUploadItem.id.asc(),
            ).limit(40).all()
            sibling_pairs = [
                (sibling_item, sibling_product)
                for sibling_item, sibling_product in sibling_rows
            ]

            class DeadlinePipeline:
                def run_category_phase(self, contexts):
                    if (
                        deadline_monotonic is not None
                        and deadline_monotonic - time.monotonic() < 22
                    ):
                        from services.marketplace_decisions_preparation import (
                            decode_decision_plan_answers,
                            plan_category_phase,
                        )
                        plan = plan_category_phase(contexts)
                        return SimpleNamespace(
                            results=decode_decision_plan_answers(
                                plan,
                                {},
                                missing_reasons={
                                    fingerprint: "cache_busy"
                                    for fingerprint in plan.question_fingerprints
                                },
                            ),
                            physical_request_started=False,
                            reservation_state="deadline",
                            finish_state=None,
                        )
                    return pipeline.run_category_phase(contexts)

            window = prepare_ozon_category_window(
                run=run,
                account=account,
                current_item=item,
                current_product=product,
                current_draft=draft,
                prior_draft_id_before_create=prior_draft_id_before_create,
                item_draft_id_before_create=item_draft_id_before_create,
                item_prepared_version_before_create=(
                    item_prepared_version_before_create
                ),
                sibling_pairs=sibling_pairs,
                pipeline=DeadlinePipeline(),
                now=now,
            )
        except OzonUploadLeaseLost:
            raise
        except Exception:
            # This optional local suggestion must not block normal preparation.
            db.session.rollback()
            return _SourceCategoryPreparation(draft=draft)
        selected_type_id = (
            window.current_product_type_id if window is not None else None
        )
        if type(selected_type_id) is not int or selected_type_id <= 0:
            retry_reason = None
            if window is not None:
                retry_reason = (
                    "tick_budget"
                    if window.reservation_state == "deadline"
                    else getattr(window.current_result, "reason", None)
                )
            if (
                queue_created_draft_lineage is True
                and (
                    retry_reason == "tick_budget"
                    or retry_reason in cls.DECISION_RETRY_REASONS
                )
            ):
                return _SourceCategoryPreparation(
                    draft=draft,
                    retry_at=window.retry_at,
                    retry_reason=retry_reason,
                )
            return _SourceCategoryPreparation(draft=draft)
        if db.session.new or db.session.dirty or db.session.deleted:
            db.session.rollback()
            return _SourceCategoryPreparation(draft=draft)
        db.session.expire_all()
        current_time = datetime.utcnow()
        try:
            cls._fence_lease(
                run_id=run.id,
                lease_token=lease_token,
                now=current_time,
            )
            fresh_run = OzonBulkUploadRun.query.filter_by(
                id=run.id,
                seller_id=run.seller_id,
                account_id=run.account_id,
                mode="source_prepare",
                state="active",
            ).first()
            fresh_item = OzonBulkUploadItem.query.filter_by(
                id=item.id, run_id=run.id,
            ).first()
            fresh_account = SellerMarketplaceAccount.query.filter_by(
                id=run.account_id, seller_id=run.seller_id,
            ).first()
            fresh_product = ImportedProduct.query.filter_by(
                id=product.id, seller_id=run.seller_id,
            ).first()
            fresh_draft = MarketplaceDraftService.get_draft(
                seller_id=run.seller_id, draft_id=draft.id,
            )
            if (
                fresh_run is None
                or fresh_item is None
                or fresh_account is None
                or fresh_product is None
                or fresh_draft is None
                or fresh_item.operation_id is not None
                or fresh_item.phase != "preparing"
                or fresh_item.draft_id != fresh_draft.id
                or fresh_item.prepared_version != fresh_draft.version
                or not cls.lease_current(
                    run_id=fresh_run.id,
                    lease_token=lease_token,
                    now=current_time,
                )
            ):
                db.session.rollback()
                return _SourceCategoryPreparation(draft=draft)
            source_category = window.current_candidate.context.source_fields.get(
                "category",
            )
            if not isinstance(source_category, str) or not source_category:
                db.session.rollback()
                return _SourceCategoryPreparation(draft=draft)
            tree_index = build_ozon_category_tree_index(
                fresh_account.marketplace,
                [source_category],
                now=current_time,
            )
            after = build_ozon_prepare_category_candidate(
                run=fresh_run,
                item=fresh_item,
                account=fresh_account,
                product=fresh_product,
                draft=fresh_draft,
                tree_index=tree_index,
                prior_draft_id_before_create=prior_draft_id_before_create,
                item_draft_id_before_create=item_draft_id_before_create,
                item_prepared_version_before_create=(
                    item_prepared_version_before_create
                ),
                now=current_time,
            ) if tree_index is not None else None
            verified_type_id = (
                selected_product_type_id(after, window.current_result)
                if after is not None
                and same_ozon_prepare_category_scope(
                    window.current_candidate, after,
                ) else None
            )
            if (
                type(verified_type_id) is not int
                or verified_type_id <= 0
                or verified_type_id != selected_type_id
            ):
                db.session.rollback()
                return _SourceCategoryPreparation(draft=draft)
            MarketplaceDraftService._update_draft_core(
                seller_id=fresh_run.seller_id,
                draft_id=fresh_draft.id,
                expected_version=fresh_draft.version,
                patch={"product_type_id": verified_type_id},
                category_review_required=False,
                commit=False,
            )
            db.session.commit()
            return _SourceCategoryPreparation(
                draft=MarketplaceDraftService.get_draft(
                    seller_id=fresh_run.seller_id, draft_id=fresh_draft.id,
                ),
            )
        except Exception as exc:
            db.session.rollback()
            if isinstance(exc, OzonUploadLeaseLost):
                raise
            return _SourceCategoryPreparation(draft=draft)

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
        deadline_monotonic: Optional[float] = None,
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
                deadline_monotonic=deadline_monotonic,
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
                deadline_monotonic=deadline,
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
