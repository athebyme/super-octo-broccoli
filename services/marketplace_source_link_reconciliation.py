"""Durable bounded reconciliation of encoded Ozon/WB supplier identities."""

from __future__ import annotations

from datetime import datetime, timedelta
import json
import logging
import secrets
import uuid
from typing import Any, Dict, Iterable, Optional

from sqlalchemy import or_
from sqlalchemy.orm import joinedload

from models import (
    BackgroundJob,
    Marketplace,
    MarketplaceListing,
    SellerMarketplaceAccount,
    db,
)
from services.marketplace_product_links import MarketplaceProductLinkService


logger = logging.getLogger(__name__)


class MarketplaceSourceLinkReconciliation:
    """Advance existing Ozon link backlog without provider or LLM calls."""

    JOB_TYPE = "marketplace_source_link_reconcile"
    MAX_BATCH = 200
    MAX_ACCOUNT_SCOPES = 3
    STALE_AFTER = timedelta(minutes=15)
    FAILED_RETRY_AFTER = timedelta(minutes=10)
    RESCAN_AFTER = timedelta(hours=6)
    MAX_JSON_BYTES = 32_768

    @staticmethod
    def _positive_integer(value: Any, field_name: str) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ValueError(f"{field_name} должен быть положительным целым числом")
        return value

    @classmethod
    def _batch_limit(cls, value: Any) -> int:
        parsed = cls._positive_integer(value, "batch_size")
        if parsed > cls.MAX_BATCH:
            raise ValueError(
                f"batch_size не может быть больше {cls.MAX_BATCH}"
            )
        return parsed

    @classmethod
    def _account_limit(cls, value: Any) -> int:
        parsed = cls._positive_integer(value, "seller_limit")
        if parsed > cls.MAX_ACCOUNT_SCOPES:
            raise ValueError(
                f"seller_limit не может быть больше {cls.MAX_ACCOUNT_SCOPES}"
            )
        return parsed

    @classmethod
    def _document(cls, job: BackgroundJob) -> dict:
        try:
            value = json.loads(job.progress_data or "{}")
        except (TypeError, json.JSONDecodeError):
            value = {}
        if not isinstance(value, dict):
            raise ValueError("Повреждён progress source-link reconciliation")
        required = {
            "account_id",
            "marketplace_id",
            "cursor_listing_id",
            "target_listing_id",
            "generation",
            "counts",
        }
        if not required.issubset(value):
            raise ValueError("Неполный progress source-link reconciliation")
        for field in (
            "account_id",
            "marketplace_id",
            "target_listing_id",
            "generation",
        ):
            if (
                not isinstance(value[field], int)
                or isinstance(value[field], bool)
                or value[field] <= 0
            ):
                raise ValueError(f"Некорректный {field} source-link reconciliation")
        cursor = value["cursor_listing_id"]
        if (
            not isinstance(cursor, int)
            or isinstance(cursor, bool)
            or cursor < 0
            or cursor > value["target_listing_id"]
        ):
            raise ValueError("Некорректный cursor source-link reconciliation")
        counts = value["counts"]
        if not isinstance(counts, dict):
            raise ValueError("Некорректные counters source-link reconciliation")
        for field in (
            "scanned",
            "linked",
            "materialized",
            "wb_attached",
            "ambiguous",
            "unmatched",
        ):
            current = counts.get(field, 0)
            if (
                not isinstance(current, int)
                or isinstance(current, bool)
                or current < 0
            ):
                raise ValueError(
                    f"Некорректный counter {field} source-link reconciliation"
                )
            counts[field] = current
        return value

    @classmethod
    def _encoded(cls, document: dict) -> str:
        encoded = json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if len(encoded.encode("utf-8")) > cls.MAX_JSON_BYTES:
            raise ValueError("Progress source-link reconciliation превышает лимит")
        return encoded

    @staticmethod
    def _eligible_filter():
        return (
            MarketplaceListing.imported_product_id.is_(None),
            or_(
                MarketplaceListing.link_source.is_(None),
                MarketplaceListing.link_source != "seller_unlink",
            ),
        )

    @classmethod
    def _jobs_by_account(
        cls,
        jobs: Iterable[BackgroundJob],
    ) -> Dict[int, BackgroundJob]:
        result = {}
        for job in jobs:
            try:
                account_id = cls._document(job)["account_id"]
            except ValueError:
                continue
            result.setdefault(account_id, job)
        return result

    @classmethod
    def ensure_jobs(
        cls,
        *,
        account_limit: int = MAX_ACCOUNT_SCOPES,
        now: Optional[datetime] = None,
    ) -> int:
        account_limit = cls._account_limit(account_limit)
        now = now or datetime.utcnow()
        ozon = Marketplace.query.filter_by(code="ozon", is_active=True).first()
        if ozon is None:
            return 0

        active_jobs = BackgroundJob.query.filter(
            BackgroundJob.job_type == cls.JOB_TYPE,
            BackgroundJob.status.in_(("pending", "running")),
        ).order_by(BackgroundJob.id.desc()).all()
        active_by_account = cls._jobs_by_account(active_jobs)

        recent_completed = BackgroundJob.query.filter(
            BackgroundJob.job_type == cls.JOB_TYPE,
            BackgroundJob.status == "completed",
            BackgroundJob.updated_at >= now - cls.RESCAN_AFTER,
        ).order_by(BackgroundJob.id.desc()).limit(500).all()
        recent_by_account = cls._jobs_by_account(recent_completed)
        recent_failed = BackgroundJob.query.filter(
            BackgroundJob.job_type == cls.JOB_TYPE,
            BackgroundJob.status == "failed",
            BackgroundJob.updated_at >= now - cls.FAILED_RETRY_AFTER,
        ).order_by(BackgroundJob.id.desc()).limit(500).all()
        failed_by_account = cls._jobs_by_account(recent_failed)

        excluded_account_ids = (
            set(active_by_account)
            | set(recent_by_account)
            | set(failed_by_account)
        )
        eligible_listing_exists = db.session.query(
            MarketplaceListing.id
        ).filter(
            MarketplaceListing.seller_id
            == SellerMarketplaceAccount.seller_id,
            MarketplaceListing.marketplace_id == ozon.id,
            MarketplaceListing.account_id == SellerMarketplaceAccount.id,
            *cls._eligible_filter(),
        ).exists()
        account_query = SellerMarketplaceAccount.query.filter(
            SellerMarketplaceAccount.marketplace_id == ozon.id,
            SellerMarketplaceAccount.is_active.is_(True),
            SellerMarketplaceAccount.connection_status == "connected",
            eligible_listing_exists,
        )
        if excluded_account_ids:
            account_query = account_query.filter(
                SellerMarketplaceAccount.id.notin_(excluded_account_ids)
            )
        accounts = account_query.order_by(
            SellerMarketplaceAccount.updated_at.asc(),
            SellerMarketplaceAccount.id.asc(),
        ).limit(account_limit).all()
        created = 0
        for account in accounts:
            max_listing_id = db.session.query(
                db.func.max(MarketplaceListing.id)
            ).filter(
                MarketplaceListing.seller_id == account.seller_id,
                MarketplaceListing.marketplace_id == ozon.id,
                MarketplaceListing.account_id == account.id,
                *cls._eligible_filter(),
            ).scalar()
            if not max_listing_id:
                continue
            total = MarketplaceListing.query.filter(
                MarketplaceListing.seller_id == account.seller_id,
                MarketplaceListing.marketplace_id == ozon.id,
                MarketplaceListing.account_id == account.id,
                MarketplaceListing.id <= int(max_listing_id),
                *cls._eligible_filter(),
            ).count()
            document = {
                "version": 1,
                "account_id": account.id,
                "marketplace_id": ozon.id,
                "cursor_listing_id": 0,
                "target_listing_id": int(max_listing_id),
                "generation": 1,
                "claim_token": None,
                "counts": {
                    "scanned": 0,
                    "linked": 0,
                    "materialized": 0,
                    "wb_attached": 0,
                    "ambiguous": 0,
                    "unmatched": 0,
                },
            }
            job = BackgroundJob(
                job_uid=str(uuid.uuid4()),
                seller_id=account.seller_id,
                job_type=cls.JOB_TYPE,
                status="pending",
                total=total,
                processed=0,
                succeeded=0,
                failed_count=0,
                progress_data=cls._encoded(document),
                result_data="{}",
            )
            db.session.add(job)
            active_by_account[account.id] = job
            created += 1
        if created:
            db.session.commit()
        return created

    @classmethod
    def _claim(
        cls,
        *,
        job: BackgroundJob,
        now: datetime,
    ) -> Optional[tuple[BackgroundJob, dict]]:
        document = cls._document(job)
        previous_progress = job.progress_data
        document["generation"] += 1
        document["claim_token"] = secrets.token_hex(16)
        claimed_progress = cls._encoded(document)
        stale_cutoff = now - cls.STALE_AFTER
        updated = BackgroundJob.query.filter(
            BackgroundJob.id == job.id,
            BackgroundJob.job_type == cls.JOB_TYPE,
            BackgroundJob.progress_data == previous_progress,
            or_(
                BackgroundJob.status == "pending",
                (
                    (BackgroundJob.status == "running")
                    & (BackgroundJob.updated_at <= stale_cutoff)
                ),
            ),
        ).update({
            BackgroundJob.status: "running",
            BackgroundJob.progress_data: claimed_progress,
            BackgroundJob.updated_at: now,
        }, synchronize_session=False)
        db.session.commit()
        if updated != 1:
            return None
        claimed = BackgroundJob.query.filter_by(
            id=job.id,
            job_type=cls.JOB_TYPE,
        ).first()
        return (claimed, document) if claimed is not None else None

    @classmethod
    def _finish(
        cls,
        *,
        job: BackgroundJob,
        document: dict,
        status: str,
        now: datetime,
    ) -> None:
        document["claim_token"] = None
        job.status = status
        job.progress_data = cls._encoded(document)
        job.processed = document["counts"]["scanned"]
        job.succeeded = document["counts"]["linked"]
        job.failed_count = document["counts"]["ambiguous"]
        job.error_message = None
        if status == "completed":
            job.result_data = json.dumps(
                {
                    "account_id": document["account_id"],
                    "target_listing_id": document["target_listing_id"],
                    **document["counts"],
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        job.updated_at = now
        db.session.commit()

    @classmethod
    def _fail(
        cls,
        *,
        job_id: int,
        now: datetime,
    ) -> None:
        db.session.rollback()
        job = db.session.get(BackgroundJob, job_id)
        if job is None or job.job_type != cls.JOB_TYPE:
            return
        job.status = "failed"
        job.error_message = (
            "Не удалось локально сопоставить source ID; задача безопасно остановлена"
        )
        job.updated_at = now
        db.session.commit()

    @classmethod
    def process_job_batch(
        cls,
        *,
        job: BackgroundJob,
        batch_size: int = MAX_BATCH,
        now: Optional[datetime] = None,
    ) -> dict:
        batch_size = cls._batch_limit(batch_size)
        now = now or datetime.utcnow()
        claimed = cls._claim(job=job, now=now)
        if claimed is None:
            return {"claimed": False}
        job, document = claimed
        try:
            account = SellerMarketplaceAccount.query.filter_by(
                id=document["account_id"],
                seller_id=job.seller_id,
                marketplace_id=document["marketplace_id"],
                is_active=True,
            ).first()
            if account is None:
                raise ValueError("Ozon account scope больше недоступен")
            rows = MarketplaceListing.query.options(
                joinedload(MarketplaceListing.marketplace),
            ).filter(
                MarketplaceListing.seller_id == job.seller_id,
                MarketplaceListing.marketplace_id
                == document["marketplace_id"],
                MarketplaceListing.account_id == account.id,
                MarketplaceListing.id > document["cursor_listing_id"],
                MarketplaceListing.id <= document["target_listing_id"],
                *cls._eligible_filter(),
            ).order_by(MarketplaceListing.id.asc()).limit(batch_size).all()
            if not rows:
                document["cursor_listing_id"] = document["target_listing_id"]
                cls._finish(
                    job=job,
                    document=document,
                    status="completed",
                    now=now,
                )
                return {
                    "claimed": True,
                    "completed": True,
                    **document["counts"],
                }

            result = MarketplaceProductLinkService.reconcile_objects(
                seller_id=job.seller_id,
                listings=rows,
                now=now,
                commit=True,
                allow_materialization=True,
            )
            if result.get("busy"):
                cls._finish(
                    job=job,
                    document=document,
                    status="pending",
                    now=now,
                )
                return {
                    "claimed": True,
                    "completed": False,
                    "busy": result["busy"],
                }

            document["cursor_listing_id"] = int(rows[-1].id)
            document["counts"]["scanned"] += len(rows)
            for field in (
                "linked",
                "materialized",
                "wb_attached",
                "ambiguous",
                "unmatched",
            ):
                document["counts"][field] += int(result.get(field, 0))
            cls._finish(
                job=job,
                document=document,
                status="pending",
                now=now,
            )
            return {
                "claimed": True,
                "completed": False,
                **result,
            }
        except Exception as exc:
            logger.exception(
                "Marketplace source-link batch failed job_id=%s error_type=%s",
                job.id,
                type(exc).__name__,
            )
            cls._fail(job_id=job.id, now=now)
            return {"claimed": True, "completed": False, "failed": 1}

    @classmethod
    def maintenance_tick(
        cls,
        *,
        account_limit: int = MAX_ACCOUNT_SCOPES,
        batch_size: int = MAX_BATCH,
        now: Optional[datetime] = None,
    ) -> dict:
        account_limit = cls._account_limit(account_limit)
        batch_size = cls._batch_limit(batch_size)
        now = now or datetime.utcnow()
        created = cls.ensure_jobs(account_limit=account_limit, now=now)
        stale_cutoff = now - cls.STALE_AFTER
        candidates = BackgroundJob.query.filter(
            BackgroundJob.job_type == cls.JOB_TYPE,
            or_(
                BackgroundJob.status == "pending",
                (
                    (BackgroundJob.status == "running")
                    & (BackgroundJob.updated_at <= stale_cutoff)
                ),
            ),
        ).order_by(
            BackgroundJob.created_at.asc(),
            BackgroundJob.id.asc(),
        ).limit(account_limit).all()
        processed = 0
        linked = 0
        materialized = 0
        ambiguous = 0
        busy = 0
        failed = 0
        for job in candidates:
            outcome = cls.process_job_batch(
                job=job,
                batch_size=batch_size,
                now=now,
            )
            if not outcome.get("claimed"):
                continue
            processed += 1
            linked += int(outcome.get("linked", 0))
            materialized += int(outcome.get("materialized", 0))
            ambiguous += int(outcome.get("ambiguous", 0))
            busy += int(bool(outcome.get("busy")))
            failed += int(outcome.get("failed", 0))
        return {
            "created": created,
            "processed_account_batches": processed,
            "linked": linked,
            "materialized": materialized,
            "ambiguous": ambiguous,
            "busy": busy,
            "failed": failed,
        }
