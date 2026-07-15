"""Marketplace-scoped draft provisioning and Ozon auto-publication.

WB keeps its legacy importer.  Ozon never mutates the WB-shaped
``ImportedProduct.import_status`` while publishing: every account receives an
independent draft, run item and durable marketplace operation.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import json
import logging
import uuid
from typing import Any, Iterable, Optional, Sequence

from flask import current_app
from sqlalchemy import case, func

from models import (
    AutoPublishItem,
    AutoPublishRun,
    AutoPublishSettings,
    ImportedProduct,
    Marketplace,
    MarketplaceOperation,
    MarketplaceProductDraft,
    Notification,
    Seller,
    SellerMarketplaceAccount,
    db,
)
from services.marketplace_drafts import (
    MarketplaceDraftConflict,
    MarketplaceDraftError,
    MarketplaceDraftService,
)
from services.marketplace_publications import (
    MarketplacePublicationBusy,
    MarketplacePublicationError,
    MarketplacePublicationService,
)


logger = logging.getLogger(__name__)


class MarketplaceAutoPublishError(RuntimeError):
    """Safe auto-publish configuration or scope error."""


def _positive_integer(value: Any, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise MarketplaceAutoPublishError(
            f"{field_name} должен быть положительным целым числом"
        )
    return value


def _strict_ids(values: Any, *, maximum: int = 200) -> list[int]:
    if not isinstance(values, list) or not 1 <= len(values) <= maximum:
        raise MarketplaceAutoPublishError(
            f"imported_product_ids должен содержать от 1 до {maximum} ID"
        )
    result: list[int] = []
    seen: set[int] = set()
    for value in values:
        parsed = _positive_integer(value, "imported_product_id")
        if parsed in seen:
            raise MarketplaceAutoPublishError(
                "imported_product_ids содержит дубликаты"
            )
        seen.add(parsed)
        result.append(parsed)
    return result


def _safe_message(value: Any, maximum: int = 500) -> str:
    normalized = " ".join(
        str(value or "").replace("\x00", " ").replace("\r", " ").split()
    )
    return normalized[:maximum] or "Неизвестная ошибка"


def _ozon_feature_enabled() -> bool:
    return bool(current_app.config.get("MARKETPLACE_OZON_ENABLED", False))


def _ozon_auto_publish_enabled() -> bool:
    return bool(
        current_app.config.get("MARKETPLACE_OZON_ENABLED", False)
        and current_app.config.get("MARKETPLACE_OZON_PUBLICATION_ENABLED", False)
        and current_app.config.get(
            "MARKETPLACE_OZON_AUTO_PUBLISH_ENABLED", False
        )
    )


class MarketplaceDraftProvisioner:
    """Create one deterministic Ozon draft per enabled target account."""

    MAX_PRODUCTS = 200
    MAX_ACCOUNTS = 10
    MAX_ERRORS = 100

    @classmethod
    def _enabled_targets(
        cls,
        *,
        seller_id: int,
        account_ids: Optional[Sequence[int]] = None,
    ) -> list[SellerMarketplaceAccount]:
        seller_id = _positive_integer(seller_id, "seller_id")
        query = SellerMarketplaceAccount.query.join(
            Marketplace,
            Marketplace.id == SellerMarketplaceAccount.marketplace_id,
        ).join(
            AutoPublishSettings,
            AutoPublishSettings.account_id == SellerMarketplaceAccount.id,
        ).filter(
            SellerMarketplaceAccount.seller_id == seller_id,
            SellerMarketplaceAccount.is_active.is_(True),
            Marketplace.code == "ozon",
            Marketplace.is_active.is_(True),
            AutoPublishSettings.seller_id == seller_id,
            AutoPublishSettings.marketplace_code == "ozon",
            AutoPublishSettings.is_enabled.is_(True),
        )
        expected: Optional[set[int]] = None
        if account_ids is not None:
            if not isinstance(account_ids, (list, tuple)):
                raise MarketplaceAutoPublishError(
                    "account_ids должен быть массивом"
                )
            parsed: list[int] = []
            expected = set()
            for raw in account_ids:
                account_id = _positive_integer(raw, "account_id")
                if account_id in expected:
                    raise MarketplaceAutoPublishError(
                        "account_ids содержит дубликаты"
                    )
                expected.add(account_id)
                parsed.append(account_id)
            if len(parsed) > cls.MAX_ACCOUNTS:
                raise MarketplaceAutoPublishError(
                    f"Нельзя выбрать больше {cls.MAX_ACCOUNTS} кабинетов"
                )
            if parsed:
                query = query.filter(SellerMarketplaceAccount.id.in_(parsed))
            else:
                return []
        targets = query.order_by(SellerMarketplaceAccount.id.asc()).all()
        if expected is not None and {target.id for target in targets} != expected:
            raise MarketplaceAutoPublishError(
                "Один из Ozon-кабинетов не принадлежит seller или не включён"
            )
        if len(targets) > cls.MAX_ACCOUNTS:
            raise MarketplaceAutoPublishError(
                "Число включённых Ozon-кабинетов превышает безопасный лимит"
            )
        return targets

    @classmethod
    def provision(
        cls,
        *,
        seller_id: int,
        imported_product_ids: list[int],
        account_ids: Optional[Sequence[int]] = None,
    ) -> dict:
        """Provision an exact product set for all enabled target accounts.

        Local draft failures do not roll back the supplier import. They are
        returned as bounded per-product diagnostics and remain visible when
        the auto-publish queue later retries provisioning.
        """
        seller_id = _positive_integer(seller_id, "seller_id")
        product_ids = _strict_ids(
            imported_product_ids,
            maximum=cls.MAX_PRODUCTS,
        )
        if not _ozon_feature_enabled():
            return {
                "targets": 0,
                "created": 0,
                "existing": 0,
                "failed": 0,
                "draft_ids": [],
                "errors": [],
                "feature_disabled": True,
            }
        targets = cls._enabled_targets(
            seller_id=seller_id,
            account_ids=account_ids,
        )
        if not targets:
            return {
                "targets": 0,
                "created": 0,
                "existing": 0,
                "failed": 0,
                "draft_ids": [],
                "errors": [],
                "feature_disabled": False,
            }
        products = ImportedProduct.query.filter(
            ImportedProduct.seller_id == seller_id,
            ImportedProduct.id.in_(product_ids),
        ).all()
        by_id = {product.id: product for product in products}
        if set(by_id) != set(product_ids):
            raise MarketplaceAutoPublishError(
                "ImportedProduct exact-set не принадлежит текущему seller"
            )
        target_ids = [target.id for target in targets]
        existing_rows = MarketplaceProductDraft.query.filter(
            MarketplaceProductDraft.seller_id == seller_id,
            MarketplaceProductDraft.account_id.in_(target_ids),
            MarketplaceProductDraft.imported_product_id.in_(product_ids),
        ).all()
        existing = {
            (draft.account_id, draft.imported_product_id): draft
            for draft in existing_rows
        }
        result = {
            "targets": len(targets),
            "created": 0,
            "existing": 0,
            "failed": 0,
            "draft_ids": [],
            "errors": [],
            "feature_disabled": False,
        }
        for product_id in product_ids:
            for account in targets:
                draft = existing.get((account.id, product_id))
                if draft is not None:
                    result["existing"] += 1
                    result["draft_ids"].append(draft.id)
                    continue
                try:
                    draft = MarketplaceDraftService.create_draft(
                        seller_id=seller_id,
                        account_id=account.id,
                        imported_product_id=product_id,
                    )
                    existing[(account.id, product_id)] = draft
                    result["created"] += 1
                    result["draft_ids"].append(draft.id)
                except MarketplaceDraftError as exc:
                    db.session.rollback()
                    result["failed"] += 1
                    if len(result["errors"]) < cls.MAX_ERRORS:
                        result["errors"].append({
                            "imported_product_id": product_id,
                            "account_id": account.id,
                            "code": getattr(exc, "code", "draft_error"),
                            "message": _safe_message(exc),
                        })
                except Exception:
                    db.session.rollback()
                    result["failed"] += 1
                    if len(result["errors"]) < cls.MAX_ERRORS:
                        result["errors"].append({
                            "imported_product_id": product_id,
                            "account_id": account.id,
                            "code": "draft_internal_error",
                            "message": "Не удалось безопасно создать Ozon-черновик",
                        })
        result["draft_ids"] = sorted(set(result["draft_ids"]))
        return result

    @classmethod
    def provision_in_chunks(
        cls,
        *,
        seller_id: int,
        imported_product_ids: Iterable[int],
    ) -> dict:
        raw_ids = list(imported_product_ids)
        if not raw_ids:
            return {
                "targets": 0,
                "created": 0,
                "existing": 0,
                "failed": 0,
                "draft_ids": [],
                "errors": [],
                "feature_disabled": not _ozon_feature_enabled(),
            }
        # Validate the complete exact-set before the first DB mutation.
        seen: set[int] = set()
        validated: list[int] = []
        for raw in raw_ids:
            product_id = _positive_integer(raw, "imported_product_id")
            if product_id in seen:
                raise MarketplaceAutoPublishError(
                    "imported_product_ids содержит дубликаты"
                )
            seen.add(product_id)
            validated.append(product_id)
        aggregate = {
            "targets": 0,
            "created": 0,
            "existing": 0,
            "failed": 0,
            "draft_ids": [],
            "errors": [],
            "feature_disabled": False,
        }
        for offset in range(0, len(validated), cls.MAX_PRODUCTS):
            chunk = validated[offset:offset + cls.MAX_PRODUCTS]
            chunk_result = cls.provision(
                seller_id=seller_id,
                imported_product_ids=chunk,
            )
            aggregate["targets"] = max(
                aggregate["targets"], chunk_result["targets"]
            )
            aggregate["created"] += chunk_result["created"]
            aggregate["existing"] += chunk_result["existing"]
            aggregate["failed"] += chunk_result["failed"]
            aggregate["draft_ids"].extend(chunk_result["draft_ids"])
            remaining_error_slots = cls.MAX_ERRORS - len(aggregate["errors"])
            if remaining_error_slots > 0:
                aggregate["errors"].extend(
                    chunk_result["errors"][:remaining_error_slots]
                )
            aggregate["feature_disabled"] = bool(
                aggregate["feature_disabled"]
                or chunk_result["feature_disabled"]
            )
        aggregate["draft_ids"] = sorted(set(aggregate["draft_ids"]))
        return aggregate


class OzonAutoPublishService:
    """Quota-aware async auto-publication for one exact Ozon account."""

    ACTIVE_RUN_STATUSES = {"running", "waiting", "cancelling"}
    ACTIVE_OPERATION_STATUSES = MarketplacePublicationService.ACTIVE_STATUSES
    FINAL_ITEM_STATUSES = {
        "completed", "failed", "skipped", "deferred", "uncertain",
    }
    MAX_RECONCILE_RUNS = 50

    def __init__(self, seller: Seller, settings: AutoPublishSettings):
        if seller is None or settings is None:
            raise MarketplaceAutoPublishError("Seller/settings не найдены")
        fresh = AutoPublishSettings.query.join(
            SellerMarketplaceAccount,
            SellerMarketplaceAccount.id == AutoPublishSettings.account_id,
        ).join(
            Marketplace,
            Marketplace.id == SellerMarketplaceAccount.marketplace_id,
        ).filter(
            AutoPublishSettings.id == settings.id,
            AutoPublishSettings.seller_id == seller.id,
            AutoPublishSettings.marketplace_code == "ozon",
            AutoPublishSettings.account_id.isnot(None),
            SellerMarketplaceAccount.seller_id == seller.id,
            Marketplace.code == "ozon",
        ).first()
        if fresh is None:
            raise MarketplaceAutoPublishError(
                "Настройки Ozon не принадлежат seller/account scope"
            )
        self.seller = seller
        self.settings = fresh
        self.account = fresh.account
        self.logger = logging.getLogger(
            f"auto_publish.ozon.seller_{seller.id}.account_{self.account.id}"
        )

    def _validate_write_settings(self) -> None:
        bounds = (
            ("check_interval_minutes", 15, 180),
            ("batch_size", 1, 50),
            ("max_daily_publishes", 1, 500),
            ("max_retries_per_product", 0, 10),
            ("retry_delay_minutes", 15, 360),
            ("failure_threshold", 1, 20),
        )
        for field_name, minimum, maximum in bounds:
            value = getattr(self.settings, field_name, None)
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or not minimum <= value <= maximum
            ):
                raise MarketplaceAutoPublishError(
                    f"Некорректная runtime-настройка {field_name}"
                )
        if self.settings.validation_mode != "strict":
            raise MarketplaceAutoPublishError(
                "Ozon auto-publish требует strict validation"
            )
        self._configured_supplier_ids()

    def _configured_supplier_ids(self) -> list[int]:
        raw = self.settings.supplier_ids_json
        if raw in (None, ""):
            return []
        try:
            values = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise MarketplaceAutoPublishError(
                "supplier_ids_json повреждён; scope не расширен"
            ) from exc
        if not isinstance(values, list) or len(values) > 200:
            raise MarketplaceAutoPublishError(
                "supplier_ids_json имеет неизвестный формат"
            )
        seen: set[int] = set()
        result: list[int] = []
        for value in values:
            supplier_id = _positive_integer(value, "supplier_id")
            if supplier_id in seen:
                raise MarketplaceAutoPublishError(
                    "supplier_ids_json содержит дубликаты"
                )
            seen.add(supplier_id)
            result.append(supplier_id)
        return result

    def _try_acquire_lock(self, token: str) -> bool:
        result = db.session.execute(
            db.text(
                "UPDATE auto_publish_settings SET run_lock_token=:token "
                "WHERE id=:settings_id AND seller_id=:seller_id "
                "AND account_id=:account_id AND marketplace_code='ozon' "
                "AND run_lock_token IS NULL"
            ),
            {
                "token": token,
                "settings_id": self.settings.id,
                "seller_id": self.seller.id,
                "account_id": self.account.id,
            },
        )
        db.session.commit()
        if result.rowcount != 1:
            return False
        db.session.refresh(self.settings)
        return True

    def _release_lock(self, token: str) -> None:
        try:
            db.session.execute(
                db.text(
                    "UPDATE auto_publish_settings SET run_lock_token=NULL "
                    "WHERE id=:settings_id AND run_lock_token=:token"
                ),
                {"settings_id": self.settings.id, "token": token},
            )
            db.session.commit()
        except Exception:
            db.session.rollback()
            self.logger.exception("Не удалось освободить Ozon auto-publish lock")

    def _reset_daily_counter(self, now: datetime) -> None:
        reset_at = self.settings.daily_count_reset_at
        if reset_at is None or now.date() > reset_at.date():
            self.settings.daily_published_count = 0
            self.settings.daily_count_reset_at = now
            db.session.commit()

    def _schedule_next_run(self, now: datetime) -> None:
        self.settings.last_run_at = now
        self.settings.next_run_at = now + timedelta(
            minutes=self.settings.check_interval_minutes
        )

    def _run_status(self, run_id: int) -> Optional[str]:
        """Read run status directly, bypassing the current session identity map."""
        return db.session.execute(
            db.text(
                "SELECT status FROM auto_publish_runs "
                "WHERE id=:run_id AND settings_id=:settings_id "
                "AND seller_id=:seller_id AND marketplace_code='ozon' "
                "AND account_id=:account_id"
            ),
            {
                "run_id": run_id,
                "settings_id": self.settings.id,
                "seller_id": self.seller.id,
                "account_id": self.account.id,
            },
        ).scalar()

    def _find_item_operation(
        self,
        item: AutoPublishItem,
    ) -> Optional[MarketplaceOperation]:
        operation = None
        if item.operation_id:
            operation = MarketplaceOperation.query.filter_by(
                id=item.operation_id,
                seller_id=self.seller.id,
                account_id=self.account.id,
                draft_id=item.draft_id,
                operation_kind="product_import",
            ).first()
        if operation is None and item.idempotency_key:
            operation = MarketplaceOperation.query.filter_by(
                seller_id=self.seller.id,
                account_id=self.account.id,
                draft_id=item.draft_id,
                operation_kind="product_import",
                idempotency_key=item.idempotency_key,
            ).first()
        return operation

    def _mark_cancelled_before_write(
        self,
        item: AutoPublishItem,
        *,
        now: datetime,
    ) -> None:
        item.status = "skipped"
        item.step = "cancelled_before_write"
        item.error_step = "cancelled_before_write"
        item.error_message = "Отменено до создания provider operation"
        item.next_retry_at = None
        item.completed_at = now

    def _notify_once(
        self,
        run: AutoPublishRun,
        *,
        event: str,
        category: str,
        title: str,
        message: str,
    ) -> None:
        metadata = json.dumps({
            "account_id": self.account.id,
            "event_key": f"ozon-auto-publish:{run.id}:{event}",
            "marketplace_code": "ozon",
            "run_id": run.id,
            "settings_id": self.settings.id,
        }, ensure_ascii=False, sort_keys=True)
        exists = Notification.query.filter_by(
            seller_id=self.seller.id,
            metadata_json=metadata,
        ).first()
        if exists is not None:
            return
        db.session.add(Notification(
            seller_id=self.seller.id,
            category=category,
            title=title[:200],
            message=_safe_message(message, 500),
            link=(
                "/auto-publish?marketplace=ozon&account_id="
                f"{self.account.id}"
            ),
            metadata_json=metadata,
        ))

    def _notify_run_state(self, run: AutoPublishRun) -> None:
        scope = f"Ozon · {self.account.label}"
        if run.status == "paused" and self.settings.notify_on_pause:
            self._notify_once(
                run,
                event="paused",
                category="error",
                title="Ozon auto-publish приостановлен",
                message=(
                    f"{scope}: circuit breaker остановил новые публикации. "
                    "Проверьте ошибки запуска перед возобновлением."
                ),
            )
        elif run.status == "attention" and self.settings.notify_on_failure:
            self._notify_once(
                run,
                event="attention",
                category="warning",
                title="Ozon-публикация требует сверки",
                message=(
                    f"{scope}: результат одной или нескольких операций "
                    "остаётся uncertain; новый write не выполняется."
                ),
            )
        elif run.status == "failed" and self.settings.notify_on_failure:
            self._notify_once(
                run,
                event="failed",
                category="warning",
                title="Ошибка Ozon auto-publish",
                message=(
                    f"{scope}: опубликовано {run.total_published}, "
                    f"ошибок {run.total_failed}, требует исправления "
                    f"{run.total_skipped}."
                ),
            )
        elif (
            run.status == "completed"
            and (run.total_failed > 0 or run.total_skipped > 0)
            and self.settings.notify_on_failure
        ):
            self._notify_once(
                run,
                event="completed_with_problems",
                category="warning",
                title="Ozon auto-publish завершён частично",
                message=(
                    f"{scope}: опубликовано {run.total_published}, "
                    f"ошибок {run.total_failed}, требует исправления "
                    f"{run.total_skipped}."
                ),
            )
        elif (
            run.status == "completed"
            and run.total_published > 0
            and run.total_failed == 0
            and run.total_skipped == 0
            and self.settings.notify_on_success
        ):
            self._notify_once(
                run,
                event="completed",
                category="success",
                title="Товары опубликованы на Ozon",
                message=(
                    f"{scope}: успешно подтверждено публикаций "
                    f"{run.total_published}."
                ),
            )

    def _claim_submission_boundary(
        self,
        item: AutoPublishItem,
        *,
        key: str,
        now: datetime,
    ) -> bool:
        """Atomically cross the no-write cancellation boundary.

        Cancellation/pause/disable wins when it commits first. If this claim
        commits first, the UI keeps the run in ``cancelling`` until the durable
        operation is discovered and reconciled; it never claims that an
        in-flight provider write was cancelled.
        """
        result = db.session.execute(
            db.text(
                "UPDATE auto_publish_items SET idempotency_key=:key, "
                "status='processing', step='submitting', "
                "started_at=COALESCE(started_at, :now) "
                "WHERE id=:item_id AND run_id=:run_id "
                "AND seller_id=:seller_id AND marketplace_code='ozon' "
                "AND account_id=:account_id AND draft_id=:draft_id "
                "AND status='processing' "
                "AND step IN ('ready_to_submit', 'submitting') "
                "AND (idempotency_key IS NULL OR idempotency_key=:key) "
                "AND EXISTS ("
                "SELECT 1 FROM auto_publish_runs r "
                "WHERE r.id=:run_id AND r.settings_id=:settings_id "
                "AND r.seller_id=:seller_id AND r.marketplace_code='ozon' "
                "AND r.account_id=:account_id "
                "AND r.status IN ('running', 'waiting')"
                ") "
                "AND EXISTS ("
                "SELECT 1 FROM auto_publish_settings s "
                "WHERE s.id=:settings_id AND s.seller_id=:seller_id "
                "AND s.marketplace_code='ozon' AND s.account_id=:account_id "
                "AND s.is_enabled=1 AND s.is_paused=0"
                ")"
            ),
            {
                "key": key,
                "now": now,
                "item_id": item.id,
                "run_id": item.run_id,
                "settings_id": self.settings.id,
                "seller_id": self.seller.id,
                "account_id": self.account.id,
                "draft_id": item.draft_id,
            },
        )
        db.session.commit()
        db.session.refresh(item)
        return result.rowcount == 1

    def _new_run(self, *, triggered_by: str, now: datetime) -> AutoPublishRun:
        run = AutoPublishRun(
            settings_id=self.settings.id,
            seller_id=self.seller.id,
            marketplace_code="ozon",
            account_id=self.account.id,
            run_uid=str(uuid.uuid4()),
            status="running",
            triggered_by=triggered_by,
            started_at=now,
        )
        db.session.add(run)
        db.session.commit()
        return run

    def _supplier_filter(self, query):
        supplier_ids = self._configured_supplier_ids()
        if supplier_ids:
            return query.filter(ImportedProduct.supplier_id.in_(supplier_ids))
        return query

    def _provision_missing_sources(self) -> dict:
        existing_ids = db.session.query(
            MarketplaceProductDraft.imported_product_id
        ).filter(
            MarketplaceProductDraft.seller_id == self.seller.id,
            MarketplaceProductDraft.account_id == self.account.id,
        )
        query = ImportedProduct.query.filter(
            ImportedProduct.seller_id == self.seller.id,
            ~ImportedProduct.id.in_(existing_ids),
        )
        query = self._supplier_filter(query)
        products = query.order_by(ImportedProduct.id.asc()).limit(
            max(1, self.settings.batch_size)
        ).all()
        if not products:
            return {
                "targets": 1,
                "created": 0,
                "existing": 0,
                "failed": 0,
                "draft_ids": [],
                "errors": [],
                "feature_disabled": False,
            }
        return MarketplaceDraftProvisioner.provision(
            seller_id=self.seller.id,
            imported_product_ids=[product.id for product in products],
            account_ids=[self.account.id],
        )

    def pending_candidate_count(self) -> int:
        query = MarketplaceProductDraft.query.join(
            ImportedProduct,
            ImportedProduct.id == MarketplaceProductDraft.imported_product_id,
        ).filter(
            MarketplaceProductDraft.seller_id == self.seller.id,
            MarketplaceProductDraft.account_id == self.account.id,
            MarketplaceProductDraft.published_listing_id.is_(None),
            MarketplaceProductDraft.status.in_((
                "needs_category", "draft", "blocked", "ready",
            )),
        )
        supplier_ids = self._configured_supplier_ids()
        if supplier_ids:
            query = query.filter(ImportedProduct.supplier_id.in_(supplier_ids))
        return query.count()

    def _candidate_drafts(self, *, now: datetime) -> list[MarketplaceProductDraft]:
        active_drafts = db.session.query(MarketplaceOperation.draft_id).filter(
            MarketplaceOperation.seller_id == self.seller.id,
            MarketplaceOperation.account_id == self.account.id,
            MarketplaceOperation.status.in_(self.ACTIVE_OPERATION_STATUSES),
            MarketplaceOperation.draft_id.isnot(None),
        )
        latest_attempt_ids = db.session.query(
            func.max(AutoPublishItem.id).label("item_id")
        ).filter(
            AutoPublishItem.seller_id == self.seller.id,
            AutoPublishItem.account_id == self.account.id,
            AutoPublishItem.marketplace_code == "ozon",
        ).group_by(AutoPublishItem.imported_product_id).subquery()
        latest_attempt_select = db.select(latest_attempt_ids.c.item_id)
        cooldown_products = db.session.query(
            AutoPublishItem.imported_product_id
        ).filter(
            AutoPublishItem.seller_id == self.seller.id,
            AutoPublishItem.account_id == self.account.id,
            AutoPublishItem.marketplace_code == "ozon",
            AutoPublishItem.id.in_(latest_attempt_select),
            AutoPublishItem.status.in_((
                "failed", "skipped", "deferred", "uncertain",
            )),
            AutoPublishItem.next_retry_at.isnot(None),
            AutoPublishItem.next_retry_at > now,
        )
        exhausted_products = db.session.query(
            AutoPublishItem.imported_product_id
        ).filter(
            AutoPublishItem.seller_id == self.seller.id,
            AutoPublishItem.account_id == self.account.id,
            AutoPublishItem.marketplace_code == "ozon",
            AutoPublishItem.id.in_(latest_attempt_select),
            AutoPublishItem.status == "failed",
            AutoPublishItem.retry_count >= max(
                1, self.settings.max_retries_per_product
            ),
        )
        query = MarketplaceProductDraft.query.join(
            ImportedProduct,
            ImportedProduct.id == MarketplaceProductDraft.imported_product_id,
        ).filter(
            MarketplaceProductDraft.seller_id == self.seller.id,
            MarketplaceProductDraft.account_id == self.account.id,
            MarketplaceProductDraft.published_listing_id.is_(None),
            MarketplaceProductDraft.status.in_((
                "needs_category", "draft", "blocked", "ready",
            )),
            ~MarketplaceProductDraft.id.in_(active_drafts),
            ~MarketplaceProductDraft.imported_product_id.in_(cooldown_products),
            ~MarketplaceProductDraft.imported_product_id.in_(exhausted_products),
        )
        supplier_ids = self._configured_supplier_ids()
        if supplier_ids:
            query = query.filter(ImportedProduct.supplier_id.in_(supplier_ids))
        return query.order_by(
            case((MarketplaceProductDraft.status == "ready", 0), else_=1),
            MarketplaceProductDraft.id.asc(),
        ).limit(self.settings.batch_size).all()

    def _previous_retry_count(self, imported_product_id: int) -> int:
        previous = AutoPublishItem.query.filter(
            AutoPublishItem.seller_id == self.seller.id,
            AutoPublishItem.account_id == self.account.id,
            AutoPublishItem.marketplace_code == "ozon",
            AutoPublishItem.imported_product_id == imported_product_id,
            AutoPublishItem.status == "failed",
        ).order_by(AutoPublishItem.id.desc()).first()
        return max(0, int(previous.retry_count or 0)) if previous else 0

    def _create_items(
        self,
        *,
        run: AutoPublishRun,
        drafts: Sequence[MarketplaceProductDraft],
    ) -> list[AutoPublishItem]:
        items: list[AutoPublishItem] = []
        for draft in drafts:
            item = AutoPublishItem(
                run_id=run.id,
                imported_product_id=draft.imported_product_id,
                seller_id=self.seller.id,
                marketplace_code="ozon",
                account_id=self.account.id,
                draft_id=draft.id,
                step="queued",
                status="pending",
                retry_count=self._previous_retry_count(
                    draft.imported_product_id
                ),
            )
            db.session.add(item)
            items.append(item)
        run.total_candidates = len(items)
        db.session.commit()
        return items

    def _retry_at(self, item: AutoPublishItem, now: datetime) -> datetime:
        retry_number = max(1, int(item.retry_count or 0))
        minutes = self.settings.retry_delay_minutes * (2 ** min(retry_number - 1, 7))
        return now + timedelta(minutes=min(minutes, 24 * 60))

    def _mark_deferred(
        self,
        item: AutoPublishItem,
        *,
        code: str,
        message: str,
        now: datetime,
    ) -> None:
        item.status = "deferred"
        item.step = code[:30]
        item.error_step = code[:30]
        item.error_message = _safe_message(message)
        item.next_retry_at = now + timedelta(
            minutes=self.settings.check_interval_minutes
        )
        item.completed_at = now

    def _mark_failed(
        self,
        item: AutoPublishItem,
        *,
        code: str,
        message: str,
        now: datetime,
    ) -> None:
        item.status = "failed"
        item.step = "failed"
        item.error_step = code[:30]
        item.error_message = _safe_message(message)
        item.retry_count = int(item.retry_count or 0) + 1
        item.next_retry_at = (
            self._retry_at(item, now)
            if item.retry_count < self.settings.max_retries_per_product
            else None
        )
        item.completed_at = now

    def _mark_needs_review(
        self,
        item: AutoPublishItem,
        *,
        code: str,
        message: str,
        now: datetime,
    ) -> None:
        item.status = "skipped"
        item.step = "needs_review"
        item.error_step = code[:30]
        item.error_message = _safe_message(message)
        item.next_retry_at = now + timedelta(
            minutes=max(self.settings.retry_delay_minutes, 15)
        )
        item.completed_at = now

    def _refresh_and_validate(
        self,
        item: AutoPublishItem,
        *,
        now: datetime,
    ) -> Optional[MarketplaceProductDraft]:
        item.status = "processing"
        item.step = "validating"
        item.started_at = item.started_at or now
        db.session.commit()
        try:
            draft = MarketplaceDraftService.get_draft(
                seller_id=self.seller.id,
                draft_id=item.draft_id,
            )
            _, _, current_hash = MarketplaceDraftService._fact_snapshot(
                draft.imported_product
            )
            if current_hash != draft.source_fact_hash:
                draft = MarketplaceDraftService.refresh_facts(
                    seller_id=self.seller.id,
                    draft_id=draft.id,
                    expected_version=draft.version,
                )
            draft = MarketplaceDraftService.validate_draft(
                seller_id=self.seller.id,
                draft_id=draft.id,
                expected_version=draft.version,
            )
        except MarketplaceDraftConflict as exc:
            db.session.rollback()
            self._mark_deferred(
                item,
                code="draft_conflict",
                message=str(exc),
                now=now,
            )
            db.session.commit()
            return None
        except MarketplaceDraftError as exc:
            db.session.rollback()
            self._mark_needs_review(
                item,
                code=getattr(exc, "code", "draft_error"),
                message=str(exc),
                now=now,
            )
            db.session.commit()
            return None
        except Exception:
            db.session.rollback()
            self._mark_failed(
                item,
                code="validation_internal_error",
                message="Не удалось безопасно провалидировать Ozon-черновик",
                now=now,
            )
            db.session.commit()
            return None

        item.validation_result_json = draft.validation_result_json
        if draft.status != "ready" or draft.validation_status != "valid":
            try:
                result = json.loads(draft.validation_result_json or "{}")
            except (TypeError, json.JSONDecodeError):
                result = {}
            errors = result.get("errors") if isinstance(result, dict) else []
            messages = [
                _safe_message(error.get("message"), 180)
                for error in (errors or [])[:3]
                if isinstance(error, dict)
            ]
            self._mark_needs_review(
                item,
                code="draft_not_publishable",
                message="; ".join(messages) or "Черновик требует исправления",
                now=now,
            )
            db.session.commit()
            return None
        item.draft_version = draft.version
        item.step = "ready_to_submit"
        db.session.commit()
        return draft

    def _operation_to_item(
        self,
        item: AutoPublishItem,
        operation: MarketplaceOperation,
        *,
        now: datetime,
    ) -> None:
        item.operation_id = operation.id
        item.listing_id = operation.listing_id
        if operation.status == "succeeded":
            if item.status != "completed":
                self.settings.daily_published_count += 1
            item.status = "completed"
            item.step = "published"
            item.error_step = None
            item.error_message = None
            item.next_retry_at = None
            item.completed_at = now
            if operation.draft and operation.draft.published_listing_id:
                item.listing_id = operation.draft.published_listing_id
            return
        if operation.status in self.ACTIVE_OPERATION_STATUSES:
            if operation.status == "uncertain" and operation.next_poll_at is None:
                item.status = "uncertain"
                item.step = "manual_review"
                item.error_step = (operation.error_code or "uncertain")[:30]
                item.error_message = _safe_message(
                    operation.error_message
                    or "Upstream outcome остаётся uncertain"
                )
                item.next_retry_at = None
                item.completed_at = now
            else:
                item.status = "processing"
                item.step = (
                    "reconciling_uncertain"
                    if operation.status == "uncertain"
                    else "awaiting_ozon"
                )
                item.error_step = (
                    operation.error_code[:30] if operation.error_code else None
                )
                item.error_message = operation.error_message
                item.completed_at = None
            return
        code = operation.error_code or "ozon_operation_failed"
        message = operation.error_message or "Ozon не подтвердил публикацию"
        if code in {
            "quota_exhausted",
            "quota_preflight_failed",
            "quota_contract_invalid",
            "account_operation_busy",
        }:
            self._mark_deferred(
                item,
                code="quota_deferred",
                message=message,
                now=now,
            )
        elif operation.attempt_count == 0:
            self._mark_needs_review(
                item,
                code=code,
                message=message,
                now=now,
            )
        else:
            self._mark_failed(
                item,
                code=code,
                message=message,
                now=now,
            )

    def _submit_item(
        self,
        item: AutoPublishItem,
        *,
        now: datetime,
    ) -> Optional[MarketplaceOperation]:
        if item.draft_id is None or item.draft_version is None:
            self._mark_failed(
                item,
                code="missing_validated_draft",
                message="Auto-publish item не имеет validated draft version",
                now=now,
            )
            db.session.commit()
            return None
        key = item.idempotency_key or (
            f"auto-publish:{self.settings.id}:item:{item.id}:"
            f"draft:{item.draft_id}:v{item.draft_version}"
        )
        if not _ozon_auto_publish_enabled():
            self._mark_deferred(
                item,
                code="write_flag_disabled",
                message="Новый Ozon write выключен feature flag",
                now=now,
            )
            db.session.commit()
            return None
        if not self._claim_submission_boundary(item, key=key, now=now):
            existing = self._find_item_operation(item)
            if existing is not None:
                self._operation_to_item(item, existing, now=now)
            elif self._run_status(item.run_id) in {"cancelling", "cancelled"}:
                self._mark_cancelled_before_write(item, now=now)
            else:
                self._mark_deferred(
                    item,
                    code="submit_claim_blocked",
                    message=(
                        "Новый provider write не начат: scope был "
                        "приостановлен, выключен или изменён"
                    ),
                    now=now,
                )
            db.session.commit()
            return existing

        existing = self._find_item_operation(item)
        if existing is not None:
            self._operation_to_item(item, existing, now=now)
            db.session.commit()
            return existing
        try:
            operation = MarketplacePublicationService.start_publication(
                seller_id=self.seller.id,
                draft_id=item.draft_id,
                expected_version=item.draft_version,
                idempotency_key=key,
                created_by_user_id=None,
                now=now,
            )
        except MarketplacePublicationBusy as exc:
            db.session.rollback()
            self._mark_deferred(
                item,
                code="account_operation_busy",
                message=str(exc),
                now=now,
            )
            db.session.commit()
            return None
        except MarketplacePublicationError as exc:
            db.session.rollback()
            self._mark_failed(
                item,
                code=getattr(exc, "code", "publication_error"),
                message=str(exc),
                now=now,
            )
            db.session.commit()
            return None
        except Exception:
            db.session.rollback()
            # The durable publication operation, when created, is discoverable
            # by the committed idempotency key on the next reconciliation.
            operation = self._find_item_operation(item)
            if operation is None:
                self._mark_failed(
                    item,
                    code="publication_internal_error",
                    message="Не удалось безопасно начать Ozon-публикацию",
                    now=now,
                )
                db.session.commit()
                return None
        self._operation_to_item(item, operation, now=now)
        db.session.commit()
        return operation

    def _recalculate_run(self, run: AutoPublishRun, *, now: datetime) -> None:
        items = AutoPublishItem.query.filter_by(
            run_id=run.id,
            seller_id=self.seller.id,
            account_id=self.account.id,
        ).all()
        run.total_candidates = len(items)
        run.total_validated = sum(
            1 for item in items if item.draft_version is not None
        )
        run.total_published = sum(
            1 for item in items if item.status == "completed"
        )
        run.total_failed = sum(1 for item in items if item.status == "failed")
        run.total_skipped = sum(1 for item in items if item.status == "skipped")
        run.total_deferred = sum(
            1 for item in items if item.status == "deferred"
        )
        active = any(item.status in {"pending", "processing"} for item in items)
        attention = any(item.status == "uncertain" for item in items)
        errors: dict[str, int] = {}
        for item in items:
            if item.status not in {"failed", "skipped", "deferred", "uncertain"}:
                continue
            code = item.error_step or item.status
            errors[code] = errors.get(code, 0) + 1
        run.error_summary = (
            json.dumps(errors, ensure_ascii=False, sort_keys=True)
            if errors else None
        )
        persisted_status = self._run_status(run.id)
        cancellation_requested = persisted_status in {
            "cancelling", "cancelled",
        }
        if cancellation_requested and active:
            run.status = "cancelling"
        elif cancellation_requested and attention:
            run.status = "attention"
        elif cancellation_requested:
            run.status = "cancelled"
        elif active:
            run.status = "waiting"
        elif attention:
            run.status = "attention"
        elif not items and run.status == "failed":
            run.status = "failed"
        elif items and run.total_deferred == len(items):
            run.status = "deferred"
        elif run.status == "paused":
            run.status = "paused"
        elif run.total_failed and not run.total_published:
            run.status = "failed"
        else:
            run.status = "completed"
        if not active:
            run.completed_at = now
            if run.started_at:
                run.duration_seconds = (now - run.started_at).total_seconds()
        self._schedule_next_run(now)
        self._notify_run_state(run)
        db.session.commit()

    def _reconcile_run(self, run: AutoPublishRun, *, now: datetime) -> None:
        items = AutoPublishItem.query.filter(
            AutoPublishItem.run_id == run.id,
            AutoPublishItem.seller_id == self.seller.id,
            AutoPublishItem.account_id == self.account.id,
            AutoPublishItem.status.in_(("pending", "processing", "uncertain")),
        ).order_by(AutoPublishItem.id.asc()).all()
        for item in items:
            operation = self._find_item_operation(item)
            run_status = self._run_status(run.id)
            if operation is None and run_status in {"cancelling", "cancelled"}:
                self._mark_cancelled_before_write(item, now=now)
                db.session.commit()
                continue
            if operation is None and not item.idempotency_key:
                self._mark_deferred(
                    item,
                    code="restart_before_write",
                    message=(
                        "Run был прерван до durable write boundary; "
                        "карточка безопасно отложена"
                    ),
                    now=now,
                )
                db.session.commit()
                continue
            if operation is None and item.idempotency_key:
                if (
                    _ozon_auto_publish_enabled()
                    and self.settings.is_enabled
                    and not self.settings.is_paused
                ):
                    operation = self._submit_item(item, now=now)
                else:
                    self._mark_deferred(
                        item,
                        code="write_flag_disabled",
                        message="Новый Ozon write выключен feature flag",
                        now=now,
                    )
                    db.session.commit()
            if operation is not None:
                self._operation_to_item(item, operation, now=now)
                db.session.commit()
        self._recalculate_run(run, now=now)

    def _pause_after_failures(self, run: AutoPublishRun, *, now: datetime) -> None:
        self.settings.is_paused = True
        self.settings.paused_at = now
        self.settings.paused_reason = (
            "Авто-пауза Ozon после серии ошибок в одном account scope "
            f"({run.total_failed}/{self.settings.failure_threshold})"
        )
        has_active = AutoPublishItem.query.filter(
            AutoPublishItem.run_id == run.id,
            AutoPublishItem.seller_id == self.seller.id,
            AutoPublishItem.account_id == self.account.id,
            AutoPublishItem.status.in_(("pending", "processing")),
        ).first() is not None
        run.status = "waiting" if has_active else "paused"
        db.session.commit()

    def execute_run(
        self,
        *,
        triggered_by: str = "scheduler",
        now: Optional[datetime] = None,
    ) -> Optional[AutoPublishRun]:
        if triggered_by not in {"scheduler", "manual"}:
            raise MarketplaceAutoPublishError("Неизвестный источник запуска")
        if not _ozon_auto_publish_enabled():
            raise MarketplaceAutoPublishError(
                "Ozon auto-publish требует staging, publication и auto-publish flags"
            )
        if not self.settings.is_enabled or self.settings.is_paused:
            raise MarketplaceAutoPublishError(
                "Ozon auto-publish scope выключен или приостановлен"
            )
        self._validate_write_settings()
        if (
            not self.account.is_active
            or not self.account.marketplace.is_active
            or not self.account.has_credentials
            or self.account.connection_status != "connected"
        ):
            raise MarketplaceAutoPublishError(
                "Ozon account не готов к новой публикации"
            )
        token = str(uuid.uuid4())
        try:
            if not self._try_acquire_lock(token):
                return None
        except Exception as exc:
            db.session.rollback()
            raise MarketplaceAutoPublishError(
                "Не удалось захватить account-scoped auto-publish lock"
            ) from exc
        current_time = now or datetime.utcnow()
        run: Optional[AutoPublishRun] = None
        try:
            self._reset_daily_counter(current_time)
            waiting = AutoPublishRun.query.filter(
                AutoPublishRun.settings_id == self.settings.id,
                AutoPublishRun.seller_id == self.seller.id,
                AutoPublishRun.account_id == self.account.id,
                AutoPublishRun.status.in_(self.ACTIVE_RUN_STATUSES),
            ).order_by(AutoPublishRun.id.asc()).all()
            for active_run in waiting:
                self._reconcile_run(active_run, now=current_time)
            still_waiting = AutoPublishRun.query.filter(
                AutoPublishRun.settings_id == self.settings.id,
                AutoPublishRun.status.in_(self.ACTIVE_RUN_STATUSES),
            ).first()
            if still_waiting is not None:
                return still_waiting
            if self.settings.daily_published_count >= self.settings.max_daily_publishes:
                self._schedule_next_run(current_time)
                db.session.commit()
                return None

            run = self._new_run(triggered_by=triggered_by, now=current_time)
            provisioning = self._provision_missing_sources()
            drafts = self._candidate_drafts(now=current_time)
            items = self._create_items(run=run, drafts=drafts)
            if not items:
                if provisioning.get("failed"):
                    run.status = "failed"
                    run.error_summary = json.dumps({
                        "draft_provision_failed": provisioning["failed"],
                    }, ensure_ascii=False)
                self._recalculate_run(run, now=current_time)
                return run

            ready_items: list[AutoPublishItem] = []
            for index, item in enumerate(items):
                if self._run_status(run.id) != "running":
                    for remaining in items[index:]:
                        if self._find_item_operation(remaining) is None:
                            self._mark_cancelled_before_write(
                                remaining,
                                now=current_time,
                            )
                    db.session.commit()
                    break
                if self._refresh_and_validate(item, now=current_time) is not None:
                    ready_items.append(item)
            if not ready_items:
                self._recalculate_run(run, now=current_time)
                return run

            try:
                capacity = MarketplacePublicationService.get_account_quota_capacity(
                    seller_id=self.seller.id,
                    account_id=self.account.id,
                    mode="create",
                    now=current_time,
                )
                quota_slots = int(capacity["available"])
            except MarketplacePublicationError as exc:
                for item in ready_items:
                    self._mark_deferred(
                        item,
                        code="quota_preflight_failed",
                        message=str(exc),
                        now=current_time,
                    )
                run.status = "failed"
                db.session.commit()
                self._recalculate_run(run, now=current_time)
                return run

            daily_slots = max(
                0,
                self.settings.max_daily_publishes
                - self.settings.daily_published_count,
            )
            slots = min(quota_slots, daily_slots)
            consecutive_failures = 0
            for item in ready_items:
                if self._run_status(run.id) != "running":
                    if self._find_item_operation(item) is None:
                        self._mark_cancelled_before_write(
                            item,
                            now=current_time,
                        )
                        db.session.commit()
                    continue
                if consecutive_failures >= self.settings.failure_threshold:
                    self._mark_deferred(
                        item,
                        code="circuit_deferred",
                        message="Остановлено account-scoped circuit breaker",
                        now=current_time,
                    )
                    continue
                if slots <= 0:
                    reason = (
                        "Дневной лимит auto-publish достигнут"
                        if daily_slots <= 0
                        else "Ozon quota исчерпана; хвост отложен без write"
                    )
                    self._mark_deferred(
                        item,
                        code="quota_deferred",
                        message=reason,
                        now=current_time,
                    )
                    continue
                operation = self._submit_item(item, now=current_time)
                if operation is not None and (
                    operation.attempt_count > 0
                    or operation.status in self.ACTIVE_OPERATION_STATUSES
                    or operation.status == "succeeded"
                ):
                    slots -= 1
                    daily_slots = max(0, daily_slots - 1)
                if item.status == "failed":
                    consecutive_failures += 1
                elif item.status not in {"deferred", "skipped"}:
                    consecutive_failures = 0
            db.session.commit()
            self._recalculate_run(run, now=current_time)
            if (
                run.total_failed >= self.settings.failure_threshold
                and self._run_status(run.id) not in {"cancelling", "cancelled"}
            ):
                self._pause_after_failures(run, now=current_time)
                self._recalculate_run(run, now=current_time)
            return run
        except Exception as exc:
            db.session.rollback()
            if run is not None:
                run = db.session.get(AutoPublishRun, run.id)
                if run is not None:
                    run.status = "failed"
                    run.error_summary = json.dumps({
                        "critical_error": "Ozon auto-publish run interrupted safely",
                    }, ensure_ascii=False)
                    run.completed_at = current_time
                    db.session.commit()
            if isinstance(exc, MarketplaceAutoPublishError):
                raise
            self.logger.error(
                "Ozon auto-publish run failed (%s)",
                type(exc).__name__,
            )
            return run
        finally:
            self._release_lock(token)

    @classmethod
    def reconcile_waiting_runs(
        cls,
        *,
        limit: int = 50,
        now: Optional[datetime] = None,
    ) -> dict:
        limit = _positive_integer(limit, "limit")
        if limit > cls.MAX_RECONCILE_RUNS:
            raise MarketplaceAutoPublishError(
                f"limit не может быть больше {cls.MAX_RECONCILE_RUNS}"
            )
        runs = AutoPublishRun.query.filter(
            AutoPublishRun.marketplace_code == "ozon",
            AutoPublishRun.status.in_(cls.ACTIVE_RUN_STATUSES),
        ).order_by(AutoPublishRun.id.asc()).limit(limit).all()
        if len(runs) < limit:
            selected_ids = [run.id for run in runs]
            resolved_attention = AutoPublishRun.query.join(
                AutoPublishItem,
                AutoPublishItem.run_id == AutoPublishRun.id,
            ).join(
                MarketplaceOperation,
                MarketplaceOperation.id == AutoPublishItem.operation_id,
            ).filter(
                AutoPublishRun.marketplace_code == "ozon",
                AutoPublishRun.status == "attention",
                AutoPublishItem.status == "uncertain",
                MarketplaceOperation.status != "uncertain",
            )
            if selected_ids:
                resolved_attention = resolved_attention.filter(
                    ~AutoPublishRun.id.in_(selected_ids)
                )
            runs.extend(
                resolved_attention.order_by(AutoPublishRun.id.asc())
                .distinct()
                .limit(limit - len(runs))
                .all()
            )
        result = {"selected": len(runs), "processed": 0, "busy": 0, "failed": 0}
        for selected in runs:
            seller = db.session.get(Seller, selected.seller_id)
            settings = db.session.get(AutoPublishSettings, selected.settings_id)
            if seller is None or settings is None:
                selected.status = "failed"
                selected.error_summary = json.dumps({
                    "scope_missing": 1,
                }, ensure_ascii=False)
                selected.completed_at = now or datetime.utcnow()
                db.session.commit()
                result["failed"] += 1
                continue
            try:
                service = cls(seller, settings)
                token = str(uuid.uuid4())
                if not service._try_acquire_lock(token):
                    result["busy"] += 1
                    continue
                try:
                    current_time = now or datetime.utcnow()
                    service._reset_daily_counter(current_time)
                    fresh_run = AutoPublishRun.query.filter_by(
                        id=selected.id,
                        seller_id=seller.id,
                        settings_id=settings.id,
                        account_id=settings.account_id,
                    ).first()
                    if fresh_run is not None:
                        service._reconcile_run(fresh_run, now=current_time)
                    result["processed"] += 1
                finally:
                    service._release_lock(token)
            except Exception as exc:
                db.session.rollback()
                logger.error(
                    "Не удалось сверить Ozon auto-publish run %s (%s)",
                    selected.id,
                    type(exc).__name__,
                )
                result["failed"] += 1
        return result
