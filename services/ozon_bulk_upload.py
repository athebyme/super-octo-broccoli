"""Seller-facing, durable bulk upload flow for Ozon product cards.

The service composes existing deterministic draft preparation and durable
``MarketplaceOperation`` rows.  It never calls Ozon in the HTTP request path:
provider submission and reconciliation remain owned by
``MarketplacePublicationService``.

One bounded ``BackgroundJob`` is the seller-facing run journal.  Its progress
document keeps an exact per-product result so a partial preparation or an Ozon
rejection cannot disappear into a flash message.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from dataclasses import dataclass
import hashlib
import json
import logging
import re
import uuid
from typing import Any, Dict, List, Optional, Sequence

from flask import current_app
from sqlalchemy import case, func, or_
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import selectinload

from models import (
    BackgroundJob,
    ImportedProduct,
    MarketplaceAttributeDefinition,
    MarketplaceListing,
    MarketplaceListingSnapshot,
    MarketplaceOperation,
    MarketplaceProductDraft,
    OzonBulkUploadItem,
    OzonBulkUploadRun,
    db,
)
from services.marketplace_accounts import (
    MarketplaceAccountError,
    MarketplaceAccountService,
)
from services.marketplace_drafts import (
    MarketplaceDraftError,
    MarketplaceDraftService,
)
from services.marketplace_publications import (
    MarketplacePublicationError,
    MarketplacePublicationService,
)
from services.marketplace_product_links import (
    MarketplaceProductLinkError,
    MarketplaceProductLinkService,
)
from services.ozon_reference_service import OzonReferenceService
from services.ozon_catalog_operation_fence import (
    ACTIVE_CATALOG_STATUSES,
    CATALOG_OPERATION_KINDS,
)
from services.ozon_product_import import OzonProductImportContract


logger = logging.getLogger(__name__)


class OzonBulkUploadError(RuntimeError):
    status_code = 400
    code = "ozon_bulk_upload_error"


class OzonBulkUploadValidationError(OzonBulkUploadError):
    code = "ozon_bulk_upload_invalid"


class OzonBulkUploadNotFound(OzonBulkUploadError):
    status_code = 404
    code = "ozon_bulk_upload_not_found"


class OzonBulkUploadConflict(OzonBulkUploadError):
    status_code = 409
    code = "ozon_bulk_upload_conflict"


@dataclass(frozen=True)
class UploadRunAcceptance:
    job: BackgroundJob
    replayed: bool


class OzonBulkUploadService:
    """Create and reconcile bounded manual Ozon upload runs."""

    JOB_TYPE = "ozon_bulk_upload"
    DOCUMENT_VERSION = 2
    SUPPORTED_DOCUMENT_VERSIONS = {1, 2}
    MAX_ITEMS = 200
    ENQUEUE_CHUNK = 50
    MAX_VALIDATION_ERRORS = 2
    MAX_LIST_JOBS = 100
    MAX_PROGRESS_BYTES = 512 * 1024
    PREPARATION_STALE_SECONDS = 300
    REFERENCE_WAIT_TIMEOUT_SECONDS = 6 * 60 * 60
    WAITING_ITEMS_PER_TICK = 50
    JOB_UID_PATTERN = re.compile(r"^ozon-upload-[0-9a-f]{32}$")
    PREPARE_RETRY_KEY_PREFIX = "prepare-retry-v1-"
    PREPARE_RETRY_KEY_PATTERN = re.compile(
        r"^prepare-retry-v1-[0-9a-f]{64}$"
    )
    PREPARE_RETRY_PHASES = frozenset({"needs_input", "failed_local"})
    REFERENCE_ERROR_CODES = {"schema_stale", "dictionary_stale"}
    REQUEST_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_-]{24,128}$")

    @classmethod
    def _request_identity(cls, *, seller_id: int, request_key: str) -> str:
        if (
            not isinstance(request_key, str)
            or not cls.REQUEST_KEY_PATTERN.fullmatch(request_key)
        ):
            raise OzonBulkUploadValidationError(
                "Некорректный ключ подтверждённого запроса"
            )
        return hashlib.sha256(
            f"ozon-upload-v3:{seller_id}:{request_key}".encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _run_fingerprint(document: dict) -> str:
        return hashlib.sha256(json.dumps(
            document, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()

    @classmethod
    def prepare_retry_product_ids(
        cls, *, mode: Any, state: Any, items: Any,
    ) -> List[int]:
        """Return the one exact retryable source-prepare selection.

        This accepts the same rows as ORM objects or the public V3 projection,
        so the read-only result and retry POST cannot drift to separate phase
        rules. Reviewed rows, excluded rows, operation-linked rows, and active
        runs are never eligible.
        """
        if mode != "source_prepare" or state != "completed":
            return []
        if not isinstance(items, (list, tuple)):
            return []

        product_ids = []
        for item in items:
            if isinstance(item, dict):
                phase = item.get("phase", item.get("status"))
                product_id = item.get("imported_product_id")
                operation_id = item.get("operation_id")
                reviewed_draft_id = item.get("reviewed_draft_id")
                reviewed_version = item.get("reviewed_version")
            else:
                phase = getattr(item, "phase", None)
                product_id = getattr(item, "imported_product_id", None)
                operation_id = getattr(item, "operation_id", None)
                reviewed_draft_id = getattr(item, "reviewed_draft_id", None)
                reviewed_version = getattr(item, "reviewed_version", None)
            if (
                phase not in cls.PREPARE_RETRY_PHASES
                or operation_id is not None
                or reviewed_draft_id is not None
                or reviewed_version is not None
                or type(product_id) is not int
                or product_id <= 0
            ):
                continue
            if product_id in product_ids:
                return []
            product_ids.append(product_id)
        if len(product_ids) > cls.MAX_ITEMS:
            return []
        return product_ids

    @classmethod
    def _prepare_retry_request_key(
        cls, *, seller_id: int, account_id: int, parent_job_uid: str,
        product_ids: Sequence[int],
    ) -> str:
        seller_id = cls._positive_integer(seller_id, "seller_id")
        account_id = cls._positive_integer(account_id, "account_id")
        if (
            not isinstance(parent_job_uid, str)
            or not cls.JOB_UID_PATTERN.fullmatch(parent_job_uid)
        ):
            raise OzonBulkUploadValidationError(
                "Некорректный исходный запуск подготовки"
            )
        normalized_ids = cls._prepared_product_ids(list(product_ids))
        identity = {
            "version": 1,
            "seller_id": seller_id,
            "account_id": account_id,
            "parent_job_uid": parent_job_uid,
            "imported_product_ids": normalized_ids,
        }
        digest = hashlib.sha256(json.dumps(
            identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        return f"{cls.PREPARE_RETRY_KEY_PREFIX}{digest}"

    @classmethod
    def prepare_retry_request_key_for_document(
        cls, *, seller_id: int, document: Any,
    ) -> Optional[str]:
        """Bind the HTML key to the same public item snapshot as its count."""
        if not isinstance(document, dict) or document.get("mode") != "source_prepare":
            return None
        items = document.get("items")
        product_ids = cls.prepare_retry_product_ids(
            mode=document.get("mode"), state=document.get("status"),
            items=items,
        )
        retry_count = document.get("prepare_retry_count")
        if (
            type(retry_count) is not int
            or retry_count <= 0
            or retry_count != len(product_ids)
        ):
            return None
        return cls._prepare_retry_request_key(
            seller_id=seller_id,
            account_id=cls._positive_integer(
                document.get("account_id"), "account_id",
            ),
            parent_job_uid=document.get("job_uid"),
            product_ids=product_ids,
        )

    @classmethod
    def _trusted_previous_prepare_job_uid(
        cls, run: OzonBulkUploadRun,
    ) -> Optional[str]:
        parent_id = getattr(run, "parent_prepare_run_id", None)
        if type(parent_id) is not int or parent_id <= 0 or parent_id == run.id:
            return None
        row = db.session.query(BackgroundJob.job_uid).join(
            OzonBulkUploadRun,
            OzonBulkUploadRun.job_id == BackgroundJob.id,
        ).filter(
            OzonBulkUploadRun.id == parent_id,
            OzonBulkUploadRun.seller_id == run.seller_id,
            OzonBulkUploadRun.account_id == run.account_id,
            OzonBulkUploadRun.mode == "source_prepare",
            OzonBulkUploadRun.state == "completed",
            BackgroundJob.seller_id == run.seller_id,
            BackgroundJob.job_type == cls.JOB_TYPE,
        ).first()
        job_uid = row[0] if row is not None else None
        if (
            isinstance(job_uid, str)
            and cls.JOB_UID_PATTERN.fullmatch(job_uid)
        ):
            return job_uid
        return None

    @classmethod
    def _lock_prepare_retry_parent(cls, *, job: BackgroundJob) -> None:
        """Serialize retry selection against repair's parent-job receipt commit.

        Source repair updates its item phase and rotates this same job row in
        one transaction. The no-op update acquires the database write/row lock
        before we re-read the selector and keeps it through child acceptance.
        """
        locked = BackgroundJob.query.filter_by(
            id=job.id,
            seller_id=job.seller_id,
            job_type=cls.JOB_TYPE,
        ).update({
            BackgroundJob.updated_at: BackgroundJob.updated_at,
        }, synchronize_session=False)
        if locked != 1:
            error = OzonBulkUploadConflict(
                "Не удалось подтвердить текущий состав повтора; обновите страницу"
            )
            error.code = "ozon_prepare_retry_scope_changed"
            raise error

    @classmethod
    def _scope_changed_retry_error(cls) -> OzonBulkUploadConflict:
        error = OzonBulkUploadConflict(
            "Состав карточек изменился после открытия результата; обновите страницу и повторите"
        )
        error.code = "ozon_prepare_retry_scope_changed"
        return error

    @classmethod
    def _existing_canonical_prepare_retry(
        cls, *, seller_id: int, parent: OzonBulkUploadRun,
        parent_job_uid: str, request_key: str, request_key_hash: str,
    ) -> Optional[UploadRunAcceptance]:
        retry = OzonBulkUploadRun.query.filter_by(
            seller_id=seller_id,
            request_key_hash=request_key_hash,
        ).first()
        if retry is None:
            return None
        if (
            retry.account_id != parent.account_id
            or retry.mode != "source_prepare"
            or retry.parent_prepare_run_id != parent.id
        ):
            error = OzonBulkUploadConflict(
                "Ключ повтора уже использован для другого запуска"
            )
            error.code = "upload_request_key_conflict"
            raise error
        retry_items = OzonBulkUploadItem.query.filter_by(
            run_id=retry.id,
        ).order_by(OzonBulkUploadItem.ordinal.asc()).all()
        product_ids = [item.imported_product_id for item in retry_items]
        expected_fingerprint = cls._run_fingerprint({
            "version": 3,
            "mode": "source_prepare",
            "account_id": retry.account_id,
            "imported_product_ids": product_ids,
        }) if product_ids else None
        if (
            not product_ids
            or len(product_ids) > cls.MAX_ITEMS
            or len(set(product_ids)) != len(product_ids)
            or retry.request_fingerprint != expected_fingerprint
            or cls._prepare_retry_request_key(
                seller_id=seller_id, account_id=retry.account_id,
                parent_job_uid=parent_job_uid,
                product_ids=product_ids,
            ) != request_key
        ):
            error = OzonBulkUploadConflict(
                "Сохранённый повтор не совпадает с точным запуском; проверьте историю"
            )
            error.code = "upload_request_key_conflict"
            raise error
        job = BackgroundJob.query.filter_by(
            id=retry.job_id,
            seller_id=seller_id,
            job_type=cls.JOB_TYPE,
        ).first()
        if job is None:
            raise OzonBulkUploadConflict(
                "Журнал подтверждённого повтора недоступен"
            )
        return UploadRunAcceptance(job=job, replayed=True)

    @classmethod
    def _existing_acceptance(
        cls, *, seller_id: int, account_id: int,
        request_key_hash: str, fingerprint: str,
    ) -> Optional[UploadRunAcceptance]:
        run = OzonBulkUploadRun.query.filter_by(
            seller_id=seller_id, request_key_hash=request_key_hash,
        ).first()
        if run is None:
            return None
        if run.account_id != account_id or run.request_fingerprint != fingerprint:
            error = OzonBulkUploadConflict(
                "Ключ запроса уже использован для другого выбора карточек"
            )
            error.code = "upload_request_key_conflict"
            raise error
        job = BackgroundJob.query.filter_by(
            id=run.job_id, seller_id=seller_id, job_type=cls.JOB_TYPE,
        ).first()
        if job is None:
            raise OzonBulkUploadConflict(
                "Журнал подтверждённого запроса недоступен"
            )
        return UploadRunAcceptance(job=job, replayed=True)

    @classmethod
    def _accept_v3(
        cls, *, seller_id: int, account, mode: str,
        request_key_hash: str, fingerprint: str,
        created_by_user_id: Optional[int], item_specs: list,
        parent_prepare_run_id: Optional[int] = None,
    ) -> UploadRunAcceptance:
        now = datetime.utcnow()
        job = BackgroundJob(
            job_uid=f"ozon-upload-{uuid.uuid4().hex}",
            seller_id=seller_id, job_type=cls.JOB_TYPE,
            status="running", total=len(item_specs), processed=0,
            succeeded=0, failed_count=0,
        )
        try:
            db.session.add(job)
            db.session.flush()
            run = OzonBulkUploadRun(
                job_id=job.id, seller_id=seller_id, account_id=account.id,
                mode=mode, request_key_hash=request_key_hash,
                request_fingerprint=fingerprint,
                created_by_user_id=created_by_user_id,
                parent_prepare_run_id=parent_prepare_run_id,
                state="active", next_due_at=now,
                created_at=now, updated_at=now,
            )
            db.session.add(run)
            db.session.flush()
            for ordinal, spec in enumerate(item_specs, start=1):
                db.session.add(OzonBulkUploadItem(
                    run_id=run.id, ordinal=ordinal,
                    imported_product_id=spec["imported_product_id"],
                    reviewed_draft_id=spec.get("reviewed_draft_id"),
                    reviewed_version=spec.get("reviewed_version"),
                    draft_id=spec.get("reviewed_draft_id"),
                    phase=("pending" if mode == "source_prepare" else "reviewed"),
                    next_due_at=now,
                    title_snapshot=spec["title"],
                    offer_id_snapshot=spec.get("offer_id"),
                    created_at=now, updated_at=now,
                ))
            job.set_progress({
                "version": 3, "run_id": run.id, "account_id": account.id,
                "mode": mode,
            })
            job.set_result({
                "version": 3, "mode": mode, "total": len(item_specs),
                "processed": 0, "prepared": 0, "needs_input": 0,
                "active": len(item_specs), "outcome": "running",
                "account_id": account.id,
                "account_label": cls._safe_text(account.label, 120),
                "job_uid": job.job_uid,
            })
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            replay = cls._existing_acceptance(
                seller_id=seller_id, account_id=account.id,
                request_key_hash=request_key_hash, fingerprint=fingerprint,
            )
            if replay is not None:
                return replay
            raise OzonBulkUploadConflict(
                "Параллельный запрос изменил выбранные карточки; обновите список"
            ) from None
        return UploadRunAcceptance(job=job, replayed=False)

    @classmethod
    def accept_source_prepare(
        cls, *, seller_id: int, account_id: int,
        imported_product_ids: Any, request_key: str,
        created_by_user_id: Optional[int] = None,
        _parent_prepare_run_id: Optional[int] = None,
    ) -> UploadRunAcceptance:
        seller_id = cls._positive_integer(seller_id, "seller_id")
        account_id = cls._positive_integer(account_id, "account_id")
        product_ids = cls._prepared_product_ids(imported_product_ids)
        if _parent_prepare_run_id is not None and (
            type(_parent_prepare_run_id) is not int or _parent_prepare_run_id <= 0
        ):
            raise OzonBulkUploadValidationError(
                "Некорректный исходный запуск подготовки"
            )
        key_hash = cls._request_identity(
            seller_id=seller_id, request_key=request_key,
        )
        fingerprint = cls._run_fingerprint({
            "version": 3, "mode": "source_prepare", "account_id": account_id,
            "imported_product_ids": product_ids,
        })
        try:
            account = MarketplaceAccountService.get_owned_account(
                seller_id=seller_id, account_id=account_id,
                marketplace_code="ozon",
            )
        except MarketplaceAccountError as exc:
            raise cls._translate_account_error(exc) from None
        if _parent_prepare_run_id is not None:
            parent = OzonBulkUploadRun.query.filter_by(
                id=_parent_prepare_run_id,
                seller_id=seller_id,
                account_id=account_id,
                mode="source_prepare",
                state="completed",
            ).first()
            if parent is None:
                raise OzonBulkUploadConflict(
                    "Исходный запуск подготовки недоступен"
                )
        replay = cls._existing_acceptance(
            seller_id=seller_id, account_id=account_id,
            request_key_hash=key_hash, fingerprint=fingerprint,
        )
        if replay is not None:
            return replay
        if not current_app.config.get("MARKETPLACE_OZON_ENABLED", False):
            error = OzonBulkUploadConflict("Подготовка Ozon выключена оператором")
            error.code = "ozon_feature_disabled"
            raise error
        if not account.is_active:
            raise OzonBulkUploadConflict("Кабинет Ozon отключён")
        rows = ImportedProduct.query.filter(
            ImportedProduct.seller_id == seller_id,
            ImportedProduct.id.in_(product_ids),
        ).with_entities(ImportedProduct.id, ImportedProduct.title).all()
        titles = {row.id: row.title for row in rows}
        if set(titles) != set(product_ids):
            raise OzonBulkUploadValidationError(
                "Выбранные товары не принадлежат текущему продавцу"
            )
        specs = [{
            "imported_product_id": product_id,
            "title": cls._safe_text(titles[product_id], 300)
            or f"Товар #{product_id}",
        } for product_id in product_ids]
        return cls._accept_v3(
            seller_id=seller_id, account=account, mode="source_prepare",
            request_key_hash=key_hash, fingerprint=fingerprint,
            created_by_user_id=created_by_user_id, item_specs=specs,
            parent_prepare_run_id=_parent_prepare_run_id,
        )

    @classmethod
    def accept_reviewed_publish(
        cls, *, seller_id: int, account_id: int, draft_ids: Any,
        expected_versions: Any, request_key: str,
        parent_prepare_job_uid: Optional[str] = None,
        result_job_uid: Optional[str] = None,
        created_by_user_id: Optional[int] = None,
    ) -> UploadRunAcceptance:
        seller_id = cls._positive_integer(seller_id, "seller_id")
        account_id = cls._positive_integer(account_id, "account_id")
        normalized_ids = cls._prepared_product_ids(draft_ids)
        if (
            not isinstance(expected_versions, dict)
            or set(expected_versions) != {str(value) for value in normalized_ids}
            or any(
                type(expected_versions[str(value)]) is not int
                or expected_versions[str(value)] < 1
                for value in normalized_ids
            )
        ):
            error = OzonBulkUploadValidationError(
                "Укажите просмотренную версию каждого выбранного черновика"
            )
            error.code = "draft_review_required"
            raise error
        if parent_prepare_job_uid is not None and (
            not isinstance(parent_prepare_job_uid, str)
            or not cls.JOB_UID_PATTERN.fullmatch(parent_prepare_job_uid)
        ):
            raise OzonBulkUploadValidationError("Некорректный исходный запуск")
        if result_job_uid is not None and (
            not isinstance(result_job_uid, str)
            or not cls.JOB_UID_PATTERN.fullmatch(result_job_uid)
        ):
            raise OzonBulkUploadValidationError("Некорректный исходный результат")
        if parent_prepare_job_uid is not None and result_job_uid is not None:
            raise OzonBulkUploadValidationError(
                "Укажите только один исходный запуск"
            )
        key_hash = cls._request_identity(
            seller_id=seller_id, request_key=request_key,
        )
        fingerprint_document = {
            "version": 3, "mode": "reviewed_drafts", "account_id": account_id,
            "draft_ids": normalized_ids,
            "expected_versions": expected_versions,
            "parent_prepare_job_uid": parent_prepare_job_uid,
        }
        # Omitting result_job_uid preserves replay fingerprints from existing
        # ordinary reviews. A recovery review is bound to its exact source.
        if result_job_uid is not None:
            fingerprint_document["result_job_uid"] = result_job_uid
        fingerprint = cls._run_fingerprint(fingerprint_document)
        try:
            account = MarketplaceAccountService.get_owned_account(
                seller_id=seller_id, account_id=account_id,
                marketplace_code="ozon",
            )
        except MarketplaceAccountError as exc:
            raise cls._translate_account_error(exc) from None
        replay = cls._existing_acceptance(
            seller_id=seller_id, account_id=account_id,
            request_key_hash=key_hash, fingerprint=fingerprint,
        )
        if replay is not None:
            return replay
        account = cls._upload_account(
            seller_id=seller_id, account_id=account_id,
        )
        drafts = MarketplaceProductDraft.query.filter(
            MarketplaceProductDraft.seller_id == seller_id,
            MarketplaceProductDraft.account_id == account_id,
            MarketplaceProductDraft.id.in_(normalized_ids),
        ).all()
        by_id = {draft.id: draft for draft in drafts}
        if set(by_id) != set(normalized_ids):
            raise OzonBulkUploadValidationError(
                "Черновики должны принадлежать продавцу и одному кабинету Ozon"
            )
        product_ids = [by_id[value].imported_product_id for value in normalized_ids]
        if len(set(product_ids)) != len(product_ids):
            raise OzonBulkUploadValidationError(
                "Один товар выбран больше одного раза"
            )
        if any(
            by_id[value].version != expected_versions[str(value)]
            for value in normalized_ids
        ):
            error = OzonBulkUploadConflict(
                "Черновики изменились; обновите список и проверьте версии"
            )
            error.code = "draft_version_conflict"
            raise error
        if result_job_uid is not None:
            cls.validate_review_recovery_scope(
                seller_id=seller_id,
                account_id=account_id,
                result_job_uid=result_job_uid,
                requested_draft_ids=normalized_ids,
            )
        parent_id = None
        if parent_prepare_job_uid is not None:
            parent_job = BackgroundJob.query.filter_by(
                seller_id=seller_id, job_uid=parent_prepare_job_uid,
                job_type=cls.JOB_TYPE,
            ).first()
            parent = (
                OzonBulkUploadRun.query.filter_by(
                    job_id=parent_job.id, seller_id=seller_id,
                    account_id=account_id, mode="source_prepare",
                ).first()
                if parent_job is not None else None
            )
            if parent is None:
                raise OzonBulkUploadNotFound("Исходный запуск подготовки не найден")
            parent_product_ids = {
                row.imported_product_id
                for row in OzonBulkUploadItem.query.filter_by(run_id=parent.id).all()
            }
            if not set(product_ids) <= parent_product_ids:
                raise OzonBulkUploadConflict(
                    "Выбранные черновики не относятся к исходной подготовке"
                )
            parent_id = parent.id
        specs = [{
            "imported_product_id": by_id[draft_id].imported_product_id,
            "reviewed_draft_id": draft_id,
            "reviewed_version": expected_versions[str(draft_id)],
            "title": cls._safe_text(
                MarketplaceDraftService._stored_json(
                    by_id[draft_id].content_json, dict,
                ).get("name"), 300,
            ) or cls._safe_text(by_id[draft_id].offer_id, 200)
            or f"Товар #{by_id[draft_id].imported_product_id}",
            "offer_id": cls._safe_text(by_id[draft_id].offer_id, 200),
        } for draft_id in normalized_ids]
        return cls._accept_v3(
            seller_id=seller_id, account=account, mode="reviewed_drafts",
            request_key_hash=key_hash, fingerprint=fingerprint,
            created_by_user_id=created_by_user_id, item_specs=specs,
            parent_prepare_run_id=parent_id,
        )

    @classmethod
    def find_by_request_key(
        cls, *, seller_id: int, account_id: int, request_key: str,
    ) -> BackgroundJob:
        seller_id = cls._positive_integer(seller_id, "seller_id")
        account_id = cls._positive_integer(account_id, "account_id")
        try:
            MarketplaceAccountService.get_owned_account(
                seller_id=seller_id, account_id=account_id,
                marketplace_code="ozon",
            )
        except MarketplaceAccountError:
            raise OzonBulkUploadNotFound("Запуск загрузки не найден") from None
        key_hash = cls._request_identity(
            seller_id=seller_id, request_key=request_key,
        )
        run = OzonBulkUploadRun.query.filter_by(
            seller_id=seller_id, account_id=account_id,
            request_key_hash=key_hash,
        ).first()
        if run is None:
            raise OzonBulkUploadNotFound("Запуск загрузки не найден")
        job = BackgroundJob.query.filter_by(
            id=run.job_id, seller_id=seller_id, job_type=cls.JOB_TYPE,
        ).first()
        if job is None:
            raise OzonBulkUploadNotFound("Запуск загрузки не найден")
        return job

    @classmethod
    def run_due_preparation(
        cls, *, now: Optional[datetime] = None, run_limit: int = 20,
        item_limit: int = 40, seconds_budget: int = 45,
    ) -> dict:
        from services.ozon_upload_queue import OzonUploadQueueService

        return OzonUploadQueueService.run_due_preparation(
            now=now, run_limit=run_limit, item_limit=item_limit,
            seconds_budget=seconds_budget,
        )

    OPERATION_STATUS_MAP = {
        "queued": "queued",
        "submitting": "submitting",
        "submitted": "submitted",
        "polling": "checking",
        "uncertain": "uncertain",
        "succeeded": "succeeded",
        "partial": "failed",
        "failed": "failed",
        "cancelled": "cancelled",
    }
    ACTIVE_ITEM_STATUSES = {
        "preparing",
        "preparing_media",
        "waiting_reference",
        "queued",
        "submitting",
        "submitted",
        "checking",
        "uncertain",
    }
    SUCCESS_ITEM_STATUSES = {
        "succeeded",
        "already_current",
        "already_published",
    }
    ATTENTION_ITEM_STATUSES = {
        "needs_input",
        "ready_to_retry",
        "failed",
        "cancelled",
        "uncertain",
        "excluded",
    }
    RETRYABLE_ITEM_STATUSES = {
        "needs_input",
        "ready_to_retry",
        "failed",
        "cancelled",
    }
    TERMINAL_ITEM_STATUSES = (
        SUCCESS_ITEM_STATUSES
        | {
            "needs_input",
            "ready_to_retry",
            "failed",
            "cancelled",
            "excluded",
            "needs_manual_reconciliation",
        }
    )

    @classmethod
    def _task_rejection_is_proven(
        cls, *, operation: MarketplaceOperation, snapshot,
    ) -> bool:
        """Require the exact durable Ozon task readback before retrying a write."""
        if (
            operation.status != "failed"
            or operation.error_code != "ozon_import_failed"
            or operation.operation_kind not in {"product_import", "product_update"}
            or not isinstance(operation.external_task_id, str)
            or not operation.external_task_id.strip()
            or operation.attempt_count < 1
            or operation.poll_count < 1
            or operation.submitted_at is None
            or operation.last_polled_at is None
            or operation.completed_at is None
            or snapshot is None
            or snapshot.seller_id != operation.seller_id
            or snapshot.account_id != operation.account_id
            or snapshot.marketplace_id != operation.marketplace_id
            or snapshot.draft_id != operation.draft_id
            or snapshot.snapshot_kind != operation.operation_kind
            or not snapshot.confirmed_fingerprint
            or len(snapshot.confirmed_state_json or "") > 512 * 1024
            or len(operation.item_results_json or "") > 512 * 1024
            or len(operation.request_summary_json or "") > 128 * 1024
        ):
            return False
        try:
            confirmed = json.loads(snapshot.confirmed_state_json or "")
            request_summary = json.loads(operation.request_summary_json or "")
            operation_items = json.loads(operation.item_results_json or "")
        except (TypeError, json.JSONDecodeError):
            return False
        if not isinstance(confirmed, dict) or confirmed.get("source") != "task_status":
            return False
        confirmed_at = confirmed.get("confirmed_at")
        if not isinstance(confirmed_at, str) or not confirmed_at.strip():
            return False
        try:
            datetime.fromisoformat(confirmed_at.replace("Z", "+00:00"))
        except ValueError:
            return False
        result = confirmed.get("result")
        if (
            not isinstance(result, dict)
            or result.get("total") != 1
            or result.get("aggregate_status") != "failed"
            or not isinstance(result.get("items"), list)
            or len(result["items"]) != 1
            or operation_items != result["items"]
            or not isinstance(request_summary, dict)
        ):
            return False
        result_item = result["items"][0]
        offer_id = request_summary.get("offer_id")
        if (
            not isinstance(result_item, dict)
            or result_item.get("status") not in {"failed", "skipped"}
            or not isinstance(offer_id, str)
            or not offer_id
            or result_item.get("offer_id") != offer_id
        ):
            return False
        try:
            fingerprint = OzonProductImportContract.fingerprint(result)
        except (TypeError, ValueError):
            return False
        return snapshot.confirmed_fingerprint == fingerprint

    @classmethod
    def _review_recovery_candidates(
        cls, *, run: OzonBulkUploadRun, rows: Sequence[OzonBulkUploadItem],
        operations: dict, items: Sequence[dict],
    ) -> list[int]:
        """Return exact, current draft/product pairs with a safe review path."""
        if run.mode != "reviewed_drafts":
            return []
        public_items = {
            item.get("operation_id"): item
            for item in items
            if isinstance(item.get("operation_id"), int)
        }
        local_draft_ids = set()
        task_operations = {}
        for row in rows:
            draft_id = row.reviewed_draft_id
            if (
                type(draft_id) is not int
                or draft_id <= 0
                or row.draft_id != draft_id
            ):
                continue
            if row.operation_id is None:
                if row.phase in {"needs_input", "failed_local"}:
                    local_draft_ids.add(draft_id)
                continue
            if row.phase != "operation_linked":
                continue
            operation = operations.get(row.operation_id)
            if (
                operation is None
                or operation.draft_id != draft_id
                or operation.status != "failed"
                or operation.operation_kind not in {"product_import", "product_update"}
                or public_items.get(operation.id, {}).get("status") == "already_current"
            ):
                continue
            no_physical_attempt = (
                operation.attempt_count == 0
                and operation.poll_count == 0
                and not operation.external_task_id
                and operation.submitted_at is None
                and operation.last_polled_at is None
                and operation.error_code != "ozon_import_failed"
            )
            if no_physical_attempt:
                local_draft_ids.add(draft_id)
            elif operation.error_code == "ozon_import_failed":
                task_operations[operation.id] = (draft_id, operation)

        draft_ids = local_draft_ids | {
            draft_id for draft_id, _operation in task_operations.values()
        }
        if not draft_ids:
            return []

        # One bounded projection query proves each row still points to its
        # current seller-owned source product. Historical item IDs alone are
        # not sufficient after a draft is rebound or its source is removed.
        current_drafts = MarketplaceProductDraft.query.with_entities(
            MarketplaceProductDraft.id,
            MarketplaceProductDraft.imported_product_id,
            MarketplaceProductDraft.published_listing_id,
            MarketplaceProductDraft.marketplace_id,
        ).join(
            ImportedProduct,
            ImportedProduct.id == MarketplaceProductDraft.imported_product_id,
        ).filter(
            MarketplaceProductDraft.seller_id == run.seller_id,
            MarketplaceProductDraft.account_id == run.account_id,
            MarketplaceProductDraft.id.in_(draft_ids),
            ImportedProduct.seller_id == run.seller_id,
        ).all()
        current_by_id = {draft.id: draft for draft in current_drafts}
        expected_products = {
            row.reviewed_draft_id: row.imported_product_id
            for row in rows
            if row.reviewed_draft_id in draft_ids
            and row.draft_id == row.reviewed_draft_id
        }
        exact_ids = {
            draft_id for draft_id, draft in current_by_id.items()
            if expected_products.get(draft_id) == draft.imported_product_id
        }
        if not exact_ids:
            return []

        active_draft_ids = set(MarketplaceOperation.query.with_entities(
            MarketplaceOperation.draft_id,
        ).filter(
            MarketplaceOperation.seller_id == run.seller_id,
            MarketplaceOperation.account_id == run.account_id,
            MarketplaceOperation.draft_id.in_(exact_ids),
            MarketplaceOperation.status.in_(MarketplacePublicationService.ACTIVE_STATUSES),
        ).all())
        active_draft_ids = {row[0] for row in active_draft_ids}
        source_ids = set(expected_products.values())
        active_batch_rows = db.session.query(
            OzonBulkUploadItem.draft_id,
            OzonBulkUploadItem.imported_product_id,
        ).join(
            OzonBulkUploadRun,
            OzonBulkUploadRun.id == OzonBulkUploadItem.run_id,
        ).filter(
            OzonBulkUploadRun.seller_id == run.seller_id,
            OzonBulkUploadRun.account_id == run.account_id,
            OzonBulkUploadItem.phase.in_(
                {"pending", "preparing", "waiting_reference", "reviewed"}
            ),
            or_(
                OzonBulkUploadItem.draft_id.in_(exact_ids),
                OzonBulkUploadItem.imported_product_id.in_(source_ids),
            ),
        ).distinct().all()
        active_batch_draft_ids = {
            draft_id for draft_id, product_id in active_batch_rows
            if draft_id in exact_ids
            and expected_products.get(draft_id) == product_id
        }
        active_batch_products = {
            product_id for _draft_id, product_id in active_batch_rows
            if product_id in source_ids
        }
        active_batch_draft_ids.update(
            draft_id for draft_id, product_id in expected_products.items()
            if product_id in active_batch_products
        )
        listing_rows = [
            draft for draft in current_by_id.values()
            if draft.id in exact_ids and draft.published_listing_id is not None
        ]
        active_listing_ids = set()
        if listing_rows:
            listing_ids = {draft.published_listing_id for draft in listing_rows}
            marketplace_ids = {draft.marketplace_id for draft in listing_rows}
            active_listing_ids = set(db.session.query(
                MarketplaceOperation.listing_id,
            ).join(
                MarketplaceListing,
                MarketplaceListing.id == MarketplaceOperation.listing_id,
            ).filter(
                MarketplaceOperation.seller_id == run.seller_id,
                MarketplaceOperation.account_id == run.account_id,
                MarketplaceOperation.marketplace_id.in_(marketplace_ids),
                MarketplaceOperation.listing_id.in_(listing_ids),
                MarketplaceOperation.operation_kind.in_(CATALOG_OPERATION_KINDS),
                MarketplaceOperation.status.in_(ACTIVE_CATALOG_STATUSES),
                MarketplaceListing.seller_id == run.seller_id,
                MarketplaceListing.account_id == run.account_id,
                MarketplaceListing.marketplace_id == MarketplaceOperation.marketplace_id,
            ).distinct().all())
            active_listing_ids = {row[0] for row in active_listing_ids}
        active_listing_by_draft = {
            draft.id for draft in listing_rows
            if draft.published_listing_id in active_listing_ids
        }

        proven_task_ids = set()
        if task_operations:
            task_ids = set(task_operations)
            snapshot_rows = MarketplaceListingSnapshot.query.with_entities(
                MarketplaceListingSnapshot.operation_id,
                MarketplaceListingSnapshot.seller_id,
                MarketplaceListingSnapshot.marketplace_id,
                MarketplaceListingSnapshot.account_id,
                MarketplaceListingSnapshot.draft_id,
                MarketplaceListingSnapshot.snapshot_kind,
                MarketplaceListingSnapshot.confirmed_fingerprint,
                MarketplaceListingSnapshot.confirmed_state_json,
            ).filter(
                MarketplaceListingSnapshot.seller_id == run.seller_id,
                MarketplaceListingSnapshot.account_id == run.account_id,
                MarketplaceListingSnapshot.operation_id.in_(task_ids),
                MarketplaceListingSnapshot.confirmed_fingerprint.isnot(None),
                func.length(MarketplaceListingSnapshot.confirmed_state_json)
                <= 512 * 1024,
            ).all()
            snapshots = {row.operation_id: row for row in snapshot_rows}
            proven_task_ids = {
                task_operations[operation_id][0]
                for operation_id, (_draft_id, operation) in task_operations.items()
                if cls._task_rejection_is_proven(
                    operation=operation,
                    snapshot=snapshots.get(operation_id),
                )
            }
        eligible = (local_draft_ids | proven_task_ids) & exact_ids
        eligible -= active_draft_ids | active_listing_by_draft | active_batch_draft_ids
        ordered = []
        for row in rows:
            draft_id = row.reviewed_draft_id
            if draft_id in eligible and draft_id not in ordered:
                ordered.append(draft_id)
        return ordered

    @classmethod
    def validate_review_recovery_scope(
        cls, *, seller_id: int, account_id: int, result_job_uid: str,
        requested_draft_ids: Sequence[int],
    ) -> list[int]:
        """Validate a bounded subset against one fully settled owned result."""
        seller_id = cls._positive_integer(seller_id, "seller_id")
        account_id = cls._positive_integer(account_id, "account_id")
        if (
            not isinstance(result_job_uid, str)
            or not cls.JOB_UID_PATTERN.fullmatch(result_job_uid)
        ):
            raise OzonBulkUploadValidationError("Некорректный исходный результат")
        job = BackgroundJob.query.filter_by(
            seller_id=seller_id, job_uid=result_job_uid, job_type=cls.JOB_TYPE,
        ).first()
        run = (
            OzonBulkUploadRun.query.filter_by(
                job_id=job.id, seller_id=seller_id, account_id=account_id,
                mode="reviewed_drafts",
            ).first()
            if job is not None else None
        )
        if run is None:
            raise OzonBulkUploadNotFound("Исходный результат недоступен")
        observed = cls._v3_document(
            job=job, run=run, include_recovery=True,
        )
        if observed["summary"]["active"] != 0:
            error = OzonBulkUploadConflict(
                "Дождитесь завершения всех карточек исходного результата"
            )
            error.code = "upload_result_not_settled"
            raise error
        eligible = observed.get("review_recovery_draft_ids", [])
        if (
            not isinstance(requested_draft_ids, Sequence)
            or isinstance(requested_draft_ids, (str, bytes))
            or not requested_draft_ids
            or any(
                type(value) is not int or not 0 < value <= 2**63 - 1
                for value in requested_draft_ids
            )
            or len(set(requested_draft_ids)) != len(requested_draft_ids)
            or not set(requested_draft_ids) <= set(eligible)
        ):
            raise OzonBulkUploadNotFound(
                "Черновик не относится к доступным карточкам этого результата"
            )
        return eligible

    @classmethod
    def _v3_document(
        cls, *, job: BackgroundJob, run: OzonBulkUploadRun,
        include_recovery: bool = False,
    ) -> dict:
        """Derive the public result from exact item→operation links only."""
        rows = OzonBulkUploadItem.query.filter_by(run_id=run.id).order_by(
            OzonBulkUploadItem.ordinal,
        ).limit(cls.MAX_ITEMS + 1).all()
        if len(rows) > cls.MAX_ITEMS:
            raise OzonBulkUploadConflict("Журнал загрузки превысил лимит карточек")
        operation_ids = [row.operation_id for row in rows if row.operation_id]
        operations = {
            operation.id: operation
            for operation in MarketplaceOperation.query.filter(
                MarketplaceOperation.id.in_(operation_ids or [-1]),
                MarketplaceOperation.seller_id == run.seller_id,
                MarketplaceOperation.account_id == run.account_id,
            ).all()
        }
        items = []
        for row in rows:
            item = {
                "imported_product_id": row.imported_product_id,
                "title": row.title_snapshot,
                "offer_id": row.offer_id_snapshot,
                "phase": row.phase,
                "status": row.phase,
                "draft_id": row.draft_id,
                "reviewed_draft_id": row.reviewed_draft_id,
                "reviewed_version": row.reviewed_version,
                "prepared_version": row.prepared_version,
                "operation_id": row.operation_id,
                "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            }
            if row.error_code:
                item["code"] = row.error_code
            if row.error_message:
                item["message"] = row.error_message
            if row.operation_id is not None:
                operation = operations.get(row.operation_id)
                if (
                    operation is None
                    or operation.draft_id != row.draft_id
                ):
                    item.update({
                        "status": "needs_manual_reconciliation",
                        "code": "publication_operation_identity_changed",
                        "message": "Связь операции требует ручной проверки",
                    })
                else:
                    cls._operation_to_item(item, operation)
            items.append(item)
        counts: Dict[str, int] = {}
        for item in items:
            counts[item["status"]] = counts.get(item["status"], 0) + 1
        active_statuses = cls.ACTIVE_ITEM_STATUSES | {"pending", "reviewed"}
        active = sum(
            1 for item in items
            if item["status"] in active_statuses
            and not item.get("reconciliation_stopped")
        )
        succeeded = sum(counts.get(status, 0) for status in cls.SUCCESS_ITEM_STATUSES)
        prepared = counts.get("prepared", 0)
        needs_input = counts.get("needs_input", 0)
        manual = counts.get("needs_manual_reconciliation", 0)
        failed = counts.get("failed", 0) + counts.get("failed_local", 0)
        completed_success = prepared if run.mode == "source_prepare" else succeeded
        if active:
            outcome = "running"
        elif completed_success == len(items):
            outcome = "success"
        elif completed_success:
            outcome = "partial"
        else:
            outcome = "attention"
        summary = {
            "version": 3, "mode": run.mode, "total": len(items),
            "processed": len(items) - active,
            "prepared": prepared, "succeeded": succeeded,
            "created": sum(
                1 for item in items
                if item["status"] == "succeeded" and item.get("action") == "create"
            ),
            "updated": sum(
                1 for item in items
                if item["status"] == "succeeded" and item.get("action") == "update"
            ),
            "needs_input": needs_input, "needs_manual_reconciliation": manual,
            "failed": failed, "active": active, "outcome": outcome,
            "status_counts": counts, "account_id": run.account_id,
            "account_label": (
                job.get_result().get("account_label")
                if isinstance(job.get_result(), dict) else None
            ),
            "job_uid": job.job_uid,
        }
        document = {"items": items, "summary": summary}
        if include_recovery and run.mode == "reviewed_drafts":
            document["review_recovery_draft_ids"] = (
                cls._review_recovery_candidates(
                    run=run, rows=rows, operations=operations, items=items,
                )
                if summary["active"] == 0 else []
            )
        return document

    @classmethod
    def _reconcile_v3(
        cls, *, job: BackgroundJob, run: OzonBulkUploadRun,
        rotate: bool = False,
    ) -> None:
        observed = cls._v3_document(job=job, run=run)
        summary = observed["summary"]
        status = "running" if summary["active"] else "completed"
        changed = job.get_result() != summary or job.status != status
        if changed:
            job.total = summary["total"]
            job.processed = summary["processed"]
            job.succeeded = summary["succeeded"]
            job.failed_count = (
                summary["needs_input"] + summary["needs_manual_reconciliation"]
                + summary["failed"]
            )
            job.status = status
            job.set_result(summary)
        if changed or rotate:
            # Scheduler rotation prevents an unchanged first page from hiding
            # run 21. A plain GET with unchanged state does not write.
            job.updated_at = datetime.utcnow()
            db.session.commit()

    @staticmethod
    def _positive_integer(value: Any, field_name: str) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise OzonBulkUploadValidationError(
                f"{field_name} должен быть положительным целым числом"
            )
        return value

    @staticmethod
    def _safe_text(value: Any, maximum: int = 500) -> Optional[str]:
        if not isinstance(value, str):
            return None
        normalized = " ".join(value.strip().split())
        if not normalized:
            return None
        normalized = "".join(
            character
            for character in normalized
            if ord(character) >= 32 and ord(character) != 127
        )
        encoded = normalized.encode("utf-8")
        if len(encoded) > maximum:
            normalized = encoded[:maximum].decode(
                "utf-8",
                errors="ignore",
            ).rstrip()
        return normalized or None

    @staticmethod
    def _parsed_datetime(value: Any) -> Optional[datetime]:
        if not isinstance(value, str) or not value:
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        if parsed.tzinfo is not None:
            parsed = parsed.replace(tzinfo=None)
        return parsed

    @classmethod
    def _feature_preflight(cls) -> None:
        if not current_app.config.get("MARKETPLACE_OZON_ENABLED", False):
            error = OzonBulkUploadConflict("Ozon выключен оператором")
            error.code = "ozon_feature_disabled"
            raise error
        if not current_app.config.get(
            "MARKETPLACE_OZON_PUBLICATION_ENABLED",
            False,
        ):
            error = OzonBulkUploadConflict(
                "Публикация карточек Ozon пока выключена оператором"
            )
            error.code = "ozon_publication_disabled"
            raise error

    @staticmethod
    def _translate_account_error(
        error: MarketplaceAccountError,
    ) -> OzonBulkUploadError:
        translated = OzonBulkUploadError(str(error))
        translated.status_code = getattr(error, "status_code", 400)
        translated.code = getattr(
            error,
            "code",
            "marketplace_account_error",
        )
        return translated

    @classmethod
    def _prepared_product_ids(cls, values: Any) -> List[int]:
        if not isinstance(values, (list, tuple)) or not values:
            raise OzonBulkUploadValidationError(
                "Выберите хотя бы один товар"
            )
        if len(values) > cls.MAX_ITEMS:
            raise OzonBulkUploadValidationError(
                f"За один запуск можно загрузить не более {cls.MAX_ITEMS} товаров"
            )
        result: List[int] = []
        seen = set()
        for raw in values:
            value = cls._positive_integer(raw, "imported_product_id")
            if value in seen:
                raise OzonBulkUploadValidationError(
                    "В выборе есть повторяющиеся товары"
                )
            seen.add(value)
            result.append(value)
        return result

    @classmethod
    def _load_progress(cls, job: BackgroundJob) -> dict:
        document = job.get_progress()
        if not isinstance(document, dict):
            return {}
        items = document.get("items")
        if (
            document.get("version") not in cls.SUPPORTED_DOCUMENT_VERSIONS
            or not isinstance(items, list)
            or not items
            or len(items) > cls.MAX_ITEMS
            or any(not isinstance(item, dict) for item in items)
            or not isinstance(document.get("account_id"), int)
            or isinstance(document.get("account_id"), bool)
            or document["account_id"] <= 0
        ):
            return {}
        return document

    @classmethod
    def _store_progress(cls, job: BackgroundJob, document: dict) -> None:
        encoded = json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if len(encoded.encode("utf-8")) > cls.MAX_PROGRESS_BYTES:
            raise OzonBulkUploadConflict(
                "Сводка массовой загрузки превысила safety limit"
            )
        job.progress_data = encoded

    @classmethod
    def _owned_job(cls, *, seller_id: int, job_uid: str) -> BackgroundJob:
        seller_id = cls._positive_integer(seller_id, "seller_id")
        if not isinstance(job_uid, str) or not cls.JOB_UID_PATTERN.fullmatch(
            job_uid
        ):
            raise OzonBulkUploadNotFound("Запуск загрузки не найден")
        job = BackgroundJob.query.filter_by(
            seller_id=seller_id,
            job_uid=job_uid,
            job_type=cls.JOB_TYPE,
        ).first()
        if job is None:
            raise OzonBulkUploadNotFound("Запуск загрузки не найден")
        if (
            OzonBulkUploadRun.query.filter_by(
                job_id=job.id, seller_id=seller_id,
            ).first() is None
            and not cls._load_progress(job)
        ):
            raise OzonBulkUploadConflict(
                "Сводка запуска повреждена; операции Ozon сохранены отдельно"
            )
        return job

    @classmethod
    def _validation_errors(cls, draft: MarketplaceProductDraft) -> list:
        try:
            result = json.loads(draft.validation_result_json or "{}")
        except (TypeError, json.JSONDecodeError):
            result = {}
        raw_errors = result.get("errors") if isinstance(result, dict) else []
        if not isinstance(raw_errors, list):
            raw_errors = []
        errors = []
        for raw in raw_errors[: cls.MAX_VALIDATION_ERRORS]:
            if not isinstance(raw, dict):
                continue
            message = cls._safe_text(raw.get("message"), 300)
            if not message:
                continue
            errors.append({
                "code": cls._safe_text(raw.get("code"), 80)
                or "draft_not_ready",
                "field": cls._safe_text(raw.get("field"), 120) or "draft",
                "message": message,
            })
        return errors

    @classmethod
    def _reference_wait_required(
        cls,
        draft: MarketplaceProductDraft,
    ) -> bool:
        try:
            result = json.loads(draft.validation_result_json or "{}")
        except (TypeError, json.JSONDecodeError):
            return False
        raw_errors = result.get("errors") if isinstance(result, dict) else []
        return bool(
            isinstance(raw_errors, list)
            and any(
                isinstance(item, dict)
                and item.get("code") in cls.REFERENCE_ERROR_CODES
                for item in raw_errors
            )
        )

    @classmethod
    def _mark_waiting_reference(
        cls,
        item: dict,
        *,
        now: Optional[datetime] = None,
    ) -> None:
        current_time = now or datetime.utcnow()
        item.update({
            "status": "waiting_reference",
            "code": "ozon_reference_sync_pending",
            "message": (
                "Загружаем официальную схему и обязательные справочники "
                "Ozon; карточка продолжит подготовку автоматически"
            ),
            "reference_wait_started_at": (
                item.get("reference_wait_started_at")
                or current_time.isoformat()
            ),
            "updated_at": current_time.isoformat(),
        })

    @classmethod
    def _draft_references_ready(
        cls,
        draft: MarketplaceProductDraft,
    ) -> bool:
        product_type = draft.product_type
        if (
            product_type is None
            or not product_type.is_available
            or not product_type.is_seller_selectable
            or not product_type.category
            or not product_type.category.is_available
            or not OzonReferenceService.reference_is_fresh(product_type)
        ):
            return False

        supplied_ids = set()
        try:
            flat = json.loads(draft.attributes_json or "[]")
            complex_groups = json.loads(
                draft.complex_attributes_json or "[]"
            )
        except (TypeError, json.JSONDecodeError):
            return True
        if isinstance(flat, list):
            supplied_ids.update(
                item.get("attribute_id")
                for item in flat
                if isinstance(item, dict)
                and isinstance(item.get("attribute_id"), str)
            )
        if isinstance(complex_groups, list):
            for group in complex_groups:
                if not isinstance(group, dict):
                    continue
                supplied_ids.update(
                    item.get("attribute_id")
                    for item in group.get("attributes", [])
                    if isinstance(item, dict)
                    and isinstance(item.get("attribute_id"), str)
                )
        definitions = MarketplaceAttributeDefinition.query.filter(
            MarketplaceAttributeDefinition.product_type_id
            == product_type.id,
            MarketplaceAttributeDefinition.is_available.is_(True),
            MarketplaceAttributeDefinition.is_enabled.is_(True),
            MarketplaceAttributeDefinition.dictionary_id.isnot(None),
        ).all()
        return all(
            (
                not definition.is_required
                and definition.external_attribute_id not in supplied_ids
            )
            or OzonReferenceService.dictionary_is_fresh(definition)
            for definition in definitions
        )

    @classmethod
    def _operation_failure(cls, operation: MarketplaceOperation) -> Dict[str, str]:
        """Return one bounded, useful seller-facing provider failure."""
        summary_message = cls._safe_text(operation.error_message, 700)
        summary_code = cls._safe_text(operation.error_code, 100)
        try:
            results = json.loads(operation.item_results_json or "[]")
        except (TypeError, json.JSONDecodeError):
            results = []
        if isinstance(results, list):
            for result in results[:100]:
                if not isinstance(result, dict):
                    continue
                raw_errors = result.get("errors")
                if not isinstance(raw_errors, list):
                    continue
                for raw_error in raw_errors[:20]:
                    if not isinstance(raw_error, dict):
                        continue
                    detail = (
                        cls._safe_text(raw_error.get("message"), 500)
                        or cls._safe_text(raw_error.get("description"), 500)
                    )
                    if not detail:
                        continue
                    field = (
                        cls._safe_text(raw_error.get("attribute_name"), 160)
                        or cls._safe_text(raw_error.get("field"), 160)
                    )
                    return {
                        "code": (
                            cls._safe_text(raw_error.get("code"), 100)
                            or summary_code
                            or "ozon_rejected"
                        ),
                        "message": f"{field}: {detail}" if field else detail,
                    }
        return {
            "code": summary_code or "ozon_upload_failed",
            "message": summary_message or "Ozon не принял карточку",
        }

    @classmethod
    def _operation_to_item(
        cls,
        item: dict,
        operation: MarketplaceOperation,
    ) -> None:
        item["action"] = (
            "update"
            if operation.operation_kind == "product_update"
            else "create"
        )
        status = cls.OPERATION_STATUS_MAP.get(operation.status, "failed")
        if (
            operation.status == "queued"
            and operation.error_code in {
                "media_preparation_pending",
                "media_preparation_retry",
            }
        ):
            status = "preparing_media"
        try:
            operation_results = json.loads(
                operation.item_results_json or "[]"
            )
        except (TypeError, json.JSONDecodeError):
            operation_results = []
        already_current = (
            operation.error_code == "product_update_no_change"
            or (
                isinstance(operation_results, list)
                and any(
                    isinstance(result, dict)
                    and result.get("status") == "already_current"
                    for result in operation_results[:100]
                )
            )
        )
        if already_current and operation.status in {"failed", "succeeded"}:
            status = "already_current"
        item["operation_id"] = operation.id
        item["status"] = status
        item["updated_at"] = datetime.utcnow().isoformat()
        item.pop("validation_errors", None)
        try:
            request_summary = json.loads(
                operation.request_summary_json or "{}"
            )
        except (TypeError, json.JSONDecodeError):
            request_summary = {}
        if isinstance(request_summary, dict):
            media_state = request_summary.get("media_asset_state")
            if media_state in {
                "pending",
                "preparing",
                "retrying",
                "ready",
                "failed",
            }:
                def media_count(key: str) -> int:
                    value = request_summary.get(key)
                    return (
                        value
                        if (
                            isinstance(value, int)
                            and not isinstance(value, bool)
                            and 0 <= value <= 31
                        )
                        else 0
                    )

                item["media_delivery"] = {
                    "state": media_state,
                    "prepared": media_count("media_asset_prepared"),
                    "total": media_count("media_asset_source_total"),
                }
        if status == "already_current":
            item.pop("code", None)
            item["message"] = "Карточка Ozon уже полностью совпадает"
        elif status == "preparing_media":
            item["code"] = (
                cls._safe_text(operation.error_code, 100)
                or "media_preparation_pending"
            )
            item["message"] = (
                cls._safe_text(operation.error_message, 700)
                or "Подготавливаем публичные фото для Ozon"
            )
        elif status in {"failed", "cancelled", "uncertain"}:
            failure = cls._operation_failure(operation)
            item["code"] = failure["code"]
            item["message"] = failure["message"]
        else:
            item.pop("code", None)
            item.pop("message", None)
        if (
            status == "uncertain"
            and operation.next_poll_at is None
            and operation.error_code == "manual_uncertain_resolution"
        ):
            item["reconciliation_stopped"] = True
        else:
            item.pop("reconciliation_stopped", None)

    @classmethod
    def _summary(cls, items: Sequence[dict]) -> dict:
        counts: Dict[str, int] = {}
        for item in items:
            status = item.get("status")
            if not isinstance(status, str):
                status = "failed"
            counts[status] = counts.get(status, 0) + 1
        succeeded = sum(counts.get(status, 0) for status in cls.SUCCESS_ITEM_STATUSES)
        needs_input = counts.get("needs_input", 0)
        ready_to_retry = counts.get("ready_to_retry", 0)
        excluded = counts.get("excluded", 0)
        waiting_reference = counts.get("waiting_reference", 0)
        preparing_media = counts.get("preparing_media", 0)
        failed = counts.get("failed", 0) + counts.get("cancelled", 0)
        uncertain = counts.get("uncertain", 0)
        uncertain_stopped = sum(
            1
            for item in items
            if (
                item.get("status") == "uncertain"
                and item.get("reconciliation_stopped") is True
            )
        )
        uncertain_active = uncertain - uncertain_stopped
        active = sum(
            1
            for item in items
            if (
                item.get("status") in cls.ACTIVE_ITEM_STATUSES
                and not (
                    item.get("status") == "uncertain"
                    and item.get("reconciliation_stopped") is True
                )
            )
        )
        terminal = len(items) - active
        if active:
            outcome = "running"
        elif succeeded == len(items):
            outcome = "success"
        elif succeeded:
            outcome = "partial"
        else:
            outcome = "attention"
        created = sum(
            1
            for item in items
            if item.get("status") == "succeeded"
            and item.get("action", "create") == "create"
        )
        updated = sum(
            1
            for item in items
            if item.get("status") == "succeeded"
            and item.get("action") == "update"
        )
        return {
            "version": cls.DOCUMENT_VERSION,
            "total": len(items),
            "processed": terminal,
            "succeeded": succeeded,
            "created": created,
            "updated": updated,
            "already_current": counts.get("already_current", 0),
            "already_published": counts.get("already_published", 0),
            "needs_input": needs_input,
            "ready_to_retry": ready_to_retry,
            "excluded": excluded,
            "waiting_reference": waiting_reference,
            "preparing_media": preparing_media,
            "failed": failed,
            "uncertain": uncertain,
            "uncertain_active": uncertain_active,
            "uncertain_stopped": uncertain_stopped,
            "active": active,
            "outcome": outcome,
            "status_counts": counts,
        }

    @classmethod
    def _summary_payload(cls, job: BackgroundJob, document: dict) -> dict:
        summary = cls._summary(document["items"])
        summary["account_id"] = document["account_id"]
        summary["account_label"] = document.get("account_label")
        summary["source"] = document.get("source", "products")
        summary["job_uid"] = job.job_uid
        return summary

    @classmethod
    def _apply_summary(cls, job: BackgroundJob, document: dict) -> dict:
        summary = cls._summary_payload(job, document)
        summary["updated_at"] = datetime.utcnow().isoformat()
        job.total = summary["total"]
        job.processed = summary["processed"]
        job.succeeded = summary["succeeded"]
        job.failed_count = summary["needs_input"] + summary["failed"]
        job.status = "running" if summary["active"] else "completed"
        job.set_result(summary)
        return summary

    @classmethod
    def _persist(cls, job: BackgroundJob, document: dict) -> None:
        document["updated_at"] = datetime.utcnow().isoformat()
        cls._store_progress(job, document)
        cls._apply_summary(job, document)
        db.session.commit()

    @classmethod
    def _enqueue_ready_items(
        cls,
        *,
        seller_id: int,
        account_id: int,
        created_by_user_id: Optional[int],
        ready_items: Dict[int, dict],
        expected_versions: Optional[Dict[int, int]] = None,
    ) -> None:
        """Create bounded durable operations for already validated drafts."""
        grouped = {
            "create": [
                draft_id
                for draft_id, item in ready_items.items()
                if item.get("action", "create") == "create"
            ],
            "update": [
                draft_id
                for draft_id, item in ready_items.items()
                if item.get("action") == "update"
            ],
        }
        for action, ready_ids in grouped.items():
            enqueue = (
                MarketplacePublicationService.enqueue_bulk_updates
                if action == "update"
                else MarketplacePublicationService.enqueue_bulk_publications
            )
            for offset in range(0, len(ready_ids), cls.ENQUEUE_CHUNK):
                chunk = ready_ids[offset:offset + cls.ENQUEUE_CHUNK]
                try:
                    result = enqueue(
                        seller_id=seller_id,
                        account_id=account_id,
                        draft_ids=chunk,
                        created_by_user_id=created_by_user_id,
                        **({"expected_versions": expected_versions}
                           if expected_versions is not None else {}),
                    )
                except MarketplacePublicationError as exc:
                    db.session.rollback()
                    for draft_id in chunk:
                        ready_items[draft_id].update({
                            "status": "needs_input",
                            "code": cls._safe_text(exc.code, 100)
                            or "publication_enqueue_failed",
                            "message": cls._safe_text(str(exc), 700)
                            or "Не удалось поставить карточку в очередь",
                            "updated_at": datetime.utcnow().isoformat(),
                        })
                    continue
                for queued in result.get("queued", []):
                    item = ready_items.get(queued.get("draft_id"))
                    if item is None:
                        continue
                    item["operation_id"] = queued.get("operation_id")
                    item["status"] = "queued"
                    item["updated_at"] = datetime.utcnow().isoformat()
                    item.pop("code", None)
                    item.pop("message", None)
                    item.pop("validation_errors", None)
                    item.pop("reference_wait_started_at", None)
                for skipped in result.get("skipped", []):
                    draft_id = skipped.get("draft_id")
                    item = ready_items.get(draft_id)
                    if item is None:
                        continue
                    active = MarketplaceOperation.query.filter(
                        MarketplaceOperation.seller_id == seller_id,
                        MarketplaceOperation.draft_id == draft_id,
                        MarketplaceOperation.status.in_(
                            MarketplacePublicationService.ACTIVE_STATUSES
                        ),
                    ).order_by(MarketplaceOperation.id.desc()).first()
                    if active is not None:
                        cls._operation_to_item(item, active)
                    else:
                        item.update({
                            "status": "needs_input",
                            "code": "publication_not_queued",
                            "message": cls._safe_text(
                                skipped.get("reason"),
                                700,
                            ) or "Карточка не поставлена в очередь",
                            "updated_at": datetime.utcnow().isoformat(),
                        })

    @classmethod
    def _upload_account(cls, *, seller_id, account_id):
        """Shared local account gate for prepared and reviewed uploads."""
        cls._feature_preflight()
        try:
            account = MarketplaceAccountService.get_owned_account(
                seller_id=seller_id,
                account_id=account_id,
                marketplace_code="ozon",
            )
        except MarketplaceAccountError as exc:
            raise cls._translate_account_error(exc) from None
        if not account.is_active:
            raise OzonBulkUploadConflict("Кабинет Ozon отключён")
        if account.connection_status != "connected" or not account.has_credentials:
            error = OzonBulkUploadConflict(
                "Сначала проверьте подключение выбранного кабинета Ozon"
            )
            error.code = "ozon_account_not_connected"
            raise error
        if (
            account.credential_expires_at is not None
            and account.credential_expires_at <= datetime.utcnow()
        ):
            error = OzonBulkUploadConflict(
                "Срок действия API key Ozon истёк; обновите ключ в кабинете"
            )
            error.code = "ozon_account_credentials_expired"
            raise error

        return account

    @classmethod
    def create_run(
        cls,
        *,
        seller_id: int,
        account_id: int,
        imported_product_ids: Any,
        created_by_user_id: Optional[int] = None,
    ) -> BackgroundJob:
        """Prepare, validate and enqueue up to 200 cards without provider I/O."""
        cls._feature_preflight()
        seller_id = cls._positive_integer(seller_id, "seller_id")
        account_id = cls._positive_integer(account_id, "account_id")
        product_ids = cls._prepared_product_ids(imported_product_ids)
        account = cls._upload_account(seller_id=seller_id, account_id=account_id)

        products = ImportedProduct.query.options(
            selectinload(ImportedProduct.supplier_product),
        ).filter(
            ImportedProduct.seller_id == seller_id,
            ImportedProduct.id.in_(product_ids),
        ).all()
        product_by_id = {product.id: product for product in products}
        if set(product_by_id) != set(product_ids):
            raise OzonBulkUploadValidationError(
                "Выбранные товары не принадлежат текущему продавцу"
            )

        try:
            source_link_preflight = (
                MarketplaceProductLinkService.reconcile_account_products(
                    seller_id=seller_id,
                    account_id=account.id,
                    products=[
                        product_by_id[product_id]
                        for product_id in product_ids
                    ],
                )
            )
        except MarketplaceProductLinkError as exc:
            error = OzonBulkUploadConflict(str(exc))
            error.code = getattr(
                exc,
                "code",
                "existing_ozon_listing_link_failed",
            )
            raise error from None

        # Exact source links to already published cards can prove a reusable
        # category/type mapping without asking the seller to repeat the same
        # choice.  The draft service activates only unanimous groups with at
        # least two current listings and a fresh official Ozon schema; every
        # conflict remains unmapped.
        category_mapping_preflight = (
            MarketplaceDraftService.reconcile_observed_category_mappings(
                seller_id=seller_id,
                marketplace_id=account.marketplace_id,
            )
        )

        now = datetime.utcnow()
        document = {
            "version": cls.DOCUMENT_VERSION,
            "source": "products",
            "account_id": account.id,
            "account_label": cls._safe_text(account.label, 120),
            "category_mapping_preflight": category_mapping_preflight,
            "created_by_user_id": created_by_user_id,
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
            "items": [
                {
                    "imported_product_id": product_id,
                    "title": cls._safe_text(
                        product_by_id[product_id].title,
                        300,
                    ) or f"Товар #{product_id}",
                    "status": "preparing",
                    "updated_at": now.isoformat(),
                }
                for product_id in product_ids
            ],
        }
        job = BackgroundJob(
            job_uid=f"ozon-upload-{uuid.uuid4().hex}",
            seller_id=seller_id,
            job_type=cls.JOB_TYPE,
            status="running",
            total=len(product_ids),
        )
        cls._store_progress(job, document)
        cls._apply_summary(job, document)
        db.session.add(job)
        db.session.commit()

        ready_items: Dict[int, dict] = {}
        for index, item in enumerate(document["items"]):
            product_id = item["imported_product_id"]
            try:
                blocked_link = source_link_preflight["blocked"].get(
                    product_id
                )
                if blocked_link is not None:
                    item.update({
                        "status": "needs_input",
                        "code": blocked_link["code"],
                        "message": blocked_link["message"],
                        "listing_ids": blocked_link["listing_ids"],
                    })
                    continue
                resolved_listing_id = source_link_preflight[
                    "resolved_listing_ids"
                ].get(product_id)
                if resolved_listing_id is not None:
                    item["listing_id"] = resolved_listing_id

                # Explicitly confirmed Ozon sync also repairs legacy source
                # snapshots, but only by filling absent observed fields from
                # the exact SupplierProduct FK.  Seller edits are untouched.
                from services.supplier_service import (
                    hydrate_missing_imported_observed_snapshot,
                )
                hydrate_missing_imported_observed_snapshot(
                    product_by_id[product_id],
                )
                draft = MarketplaceDraftService.create_draft(
                    seller_id=seller_id,
                    account_id=account.id,
                    imported_product_id=product_id,
                    corrected_by_user_id=created_by_user_id,
                    source_link_preflight=False,
                    observed_mapping_preflight=False,
                )
                item.update({
                    "draft_id": draft.id,
                    "offer_id": cls._safe_text(draft.offer_id, 200),
                    "action": (
                        "update"
                        if draft.published_listing_id is not None
                        else "create"
                    ),
                })

                active = MarketplaceOperation.query.filter(
                    MarketplaceOperation.seller_id == seller_id,
                    MarketplaceOperation.draft_id == draft.id,
                    MarketplaceOperation.status.in_(
                        MarketplacePublicationService.ACTIVE_STATUSES
                    ),
                ).order_by(MarketplaceOperation.id.desc()).first()
                if active is not None:
                    cls._operation_to_item(item, active)
                    continue

                draft = MarketplaceDraftService.rebase_source_defaults(
                    seller_id=seller_id,
                    draft_id=draft.id,
                    expected_version=draft.version,
                )
                draft = MarketplaceDraftService.apply_reference_defaults(
                    seller_id=seller_id,
                    draft_id=draft.id,
                    expected_version=draft.version,
                )
                draft = MarketplaceDraftService.apply_account_defaults(
                    seller_id=seller_id,
                    draft_id=draft.id,
                    expected_version=draft.version,
                )
                draft = MarketplaceDraftService.validate_draft(
                    seller_id=seller_id,
                    draft_id=draft.id,
                    expected_version=draft.version,
                )
                item["completeness"] = (
                    MarketplaceDraftService.completeness_summary(draft)
                )
                validation_errors = cls._validation_errors(draft)
                if draft.status != "ready" or validation_errors:
                    if cls._reference_wait_required(draft):
                        cls._mark_waiting_reference(item)
                        item["validation_errors"] = validation_errors
                        continue
                    item.update({
                        "status": "needs_input",
                        "code": (
                            validation_errors[0]["code"]
                            if validation_errors else "draft_not_ready"
                        ),
                        "message": (
                            validation_errors[0]["message"]
                            if validation_errors
                            else "Черновик требует проверки"
                        ),
                        "validation_errors": validation_errors,
                    })
                    continue
                ready_items[draft.id] = item
                item["status"] = "queued"
            except (MarketplaceDraftError, MarketplacePublicationError) as exc:
                db.session.rollback()
                item.update({
                    "status": "needs_input",
                    "code": cls._safe_text(
                        getattr(exc, "code", None),
                        100,
                    ) or "draft_preparation_failed",
                    "message": cls._safe_text(str(exc), 700)
                    or "Не удалось подготовить черновик",
                })
            except Exception:
                db.session.rollback()
                logger.exception(
                    "Ozon bulk preparation failed seller_id=%s product_id=%s",
                    seller_id,
                    product_id,
                )
                item.update({
                    "status": "failed",
                    "code": "draft_preparation_failed",
                    "message": "Не удалось безопасно подготовить карточку",
                })
            finally:
                item["updated_at"] = datetime.utcnow().isoformat()
                if (index + 1) % 10 == 0:
                    cls._persist(job, document)

        cls._enqueue_ready_items(
            seller_id=seller_id,
            account_id=account.id,
            created_by_user_id=created_by_user_id,
            ready_items=ready_items,
        )

        cls._persist(job, document)
        return cls.reconcile_run(seller_id=seller_id, job_uid=job.job_uid)

    @classmethod
    def create_run_from_drafts(
        cls,
        *,
        seller_id: int,
        account_id: int,
        draft_ids: Any,
        created_by_user_id: Optional[int] = None,
        expected_versions: Optional[dict] = None,
    ) -> BackgroundJob:
        """Start the same upload journal from selected seller-owned drafts."""
        seller_id = cls._positive_integer(seller_id, "seller_id")
        account_id = cls._positive_integer(account_id, "account_id")
        if not isinstance(draft_ids, (list, tuple)) or not draft_ids:
            raise OzonBulkUploadValidationError(
                "Выберите хотя бы один черновик"
            )
        if len(draft_ids) > cls.MAX_ITEMS:
            raise OzonBulkUploadValidationError(
                f"За один запуск можно загрузить не более {cls.MAX_ITEMS} карточек"
            )
        normalized_ids: List[int] = []
        seen = set()
        for raw in draft_ids:
            draft_id = cls._positive_integer(raw, "draft_id")
            if draft_id in seen:
                raise OzonBulkUploadValidationError(
                    "В выборе есть повторяющиеся черновики"
                )
            seen.add(draft_id)
            normalized_ids.append(draft_id)

        drafts = MarketplaceProductDraft.query.filter(
            MarketplaceProductDraft.seller_id == seller_id,
            MarketplaceProductDraft.account_id == account_id,
            MarketplaceProductDraft.id.in_(normalized_ids),
        ).all()
        draft_by_id = {draft.id: draft for draft in drafts}
        if set(draft_by_id) != set(normalized_ids):
            raise OzonBulkUploadValidationError(
                "Все выбранные черновики должны принадлежать текущему продавцу "
                "и одному кабинету Ozon"
            )
        product_ids = [
            draft_by_id[draft_id].imported_product_id
            for draft_id in normalized_ids
        ]
        if len(set(product_ids)) != len(product_ids):
            raise OzonBulkUploadValidationError(
                "Один товар выбран больше одного раза"
            )
        if expected_versions is not None:
            # JSON object keys are canonical decimal IDs. Missing/extra entries,
            # bools and coercible strings must not turn a review into "latest".
            if not isinstance(expected_versions, dict) or set(expected_versions) != {
                str(draft_id) for draft_id in normalized_ids
            }:
                raise OzonBulkUploadValidationError(
                    "Укажите просмотренную версию каждого выбранного черновика"
                )
            versions = {}
            for draft_id in normalized_ids:
                value = expected_versions[str(draft_id)]
                if type(value) is not int or value < 1:
                    raise OzonBulkUploadValidationError("Некорректная версия черновика")
                versions[draft_id] = value
                if draft_by_id[draft_id].version != value:
                    error = OzonBulkUploadConflict(
                        "Черновики изменились. Обновите список и проверьте выбор перед отправкой."
                    )
                    error.code = "draft_version_conflict"
                    raise error
            return cls._create_reviewed_draft_run(
                seller_id=seller_id, account_id=account_id,
                drafts=[draft_by_id[draft_id] for draft_id in normalized_ids],
                expected_versions=versions, created_by_user_id=created_by_user_id,
            )
        return cls.create_run(
            seller_id=seller_id,
            account_id=account_id,
            imported_product_ids=product_ids,
            created_by_user_id=created_by_user_id,
        )

    @classmethod
    def _create_reviewed_draft_run(
        cls, *, seller_id, account_id, drafts, expected_versions, created_by_user_id,
    ):
        """Queue reviewed versions unchanged; each enqueue repeats validation.

        Unlike preparation from source products, this path never hydrates,
        rebases, remaps or fills defaults after the seller reviewed a draft.
        A concurrent version/schema change becomes a visible skipped item.
        """
        account = cls._upload_account(seller_id=seller_id, account_id=account_id)
        now = datetime.utcnow().isoformat()
        items = [{
            "imported_product_id": draft.imported_product_id,
            "draft_id": draft.id,
            "reviewed_version": expected_versions[draft.id],
            "offer_id": cls._safe_text(draft.offer_id, 200),
            "title": cls._safe_text(
                MarketplaceDraftService._stored_json(draft.content_json, dict).get("name"), 300,
            ) or cls._safe_text(draft.offer_id, 200),
            "action": "update" if draft.published_listing_id is not None else "create",
            "status": "preparing",
            "updated_at": now,
        } for draft in drafts]
        document = {
            "version": cls.DOCUMENT_VERSION, "source": "drafts",
            "account_id": account.id, "account_label": cls._safe_text(account.label, 120),
            "created_by_user_id": created_by_user_id,
            "created_at": now, "updated_at": now, "items": items,
        }
        job = BackgroundJob(
            job_uid=f"ozon-upload-{uuid.uuid4().hex}", seller_id=seller_id,
            job_type=cls.JOB_TYPE, status="running", total=len(items),
        )
        cls._store_progress(job, document)
        cls._apply_summary(job, document)
        db.session.add(job)
        db.session.commit()
        cls._enqueue_ready_items(
            seller_id=seller_id, account_id=account.id,
            created_by_user_id=created_by_user_id,
            ready_items={item["draft_id"]: item for item in items},
            expected_versions=expected_versions,
        )
        cls._persist(job, document)
        return cls.reconcile_run(seller_id=seller_id, job_uid=job.job_uid)

    @classmethod
    def _resume_waiting_reference_items(
        cls,
        *,
        job: BackgroundJob,
        document: dict,
        item_limit: Optional[int] = None,
    ) -> bool:
        """Resume local preparation after the demanded reference becomes fresh."""
        if not (
            current_app.config.get("MARKETPLACE_OZON_ENABLED", False)
            and current_app.config.get(
                "MARKETPLACE_OZON_PUBLICATION_ENABLED",
                False,
            )
        ):
            return False
        limit = item_limit or cls.WAITING_ITEMS_PER_TICK
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= cls.MAX_ITEMS
        ):
            raise OzonBulkUploadValidationError(
                "Некорректный лимит продолжения Ozon upload"
            )

        current_time = datetime.utcnow()
        waiting_items = [
            item
            for item in document["items"]
            if item.get("status") == "waiting_reference"
        ][:limit]
        if not waiting_items:
            return False

        changed = False
        ready_items: Dict[int, dict] = {}
        for item in waiting_items:
            started_at = cls._parsed_datetime(
                item.get("reference_wait_started_at")
            )
            if started_at is None:
                item["reference_wait_started_at"] = current_time.isoformat()
                started_at = current_time
                changed = True
            if (
                current_time - started_at
            ).total_seconds() >= cls.REFERENCE_WAIT_TIMEOUT_SECONDS:
                item.update({
                    "status": "needs_input",
                    "code": "ozon_reference_sync_timeout",
                    "message": (
                        "Справочник Ozon не загрузился за 6 часов. "
                        "Администратору нужно проверить reference account; "
                        "после исправления карточку можно безопасно повторить"
                    ),
                    "updated_at": current_time.isoformat(),
                })
                changed = True
                continue

            try:
                draft_id = cls._positive_integer(
                    item.get("draft_id"),
                    "draft_id",
                )
                draft = MarketplaceDraftService.get_draft(
                    seller_id=job.seller_id,
                    draft_id=draft_id,
                )
                if draft.account_id != document["account_id"]:
                    item.update({
                        "status": "needs_input",
                        "code": "draft_account_changed",
                        "message": (
                            "Черновик теперь относится к другому кабинету Ozon"
                        ),
                        "updated_at": current_time.isoformat(),
                    })
                    changed = True
                    continue
                item["action"] = (
                    "update"
                    if draft.published_listing_id is not None
                    else "create"
                )

                active = MarketplaceOperation.query.filter(
                    MarketplaceOperation.seller_id == job.seller_id,
                    MarketplaceOperation.draft_id == draft.id,
                    MarketplaceOperation.status.in_(
                        MarketplacePublicationService.ACTIVE_STATUSES
                    ),
                ).order_by(MarketplaceOperation.id.desc()).first()
                if active is not None:
                    cls._operation_to_item(item, active)
                    item.pop("reference_wait_started_at", None)
                    changed = True
                    continue

                product_type = draft.product_type
                if (
                    product_type is None
                    or not product_type.is_available
                    or not product_type.is_seller_selectable
                    or not product_type.category
                    or not product_type.category.is_available
                ):
                    item.update({
                        "status": "needs_input",
                        "code": "product_type_unavailable",
                        "message": (
                            "Выбранный тип Ozon больше недоступен; "
                            "выберите точную категорию заново"
                        ),
                        "updated_at": current_time.isoformat(),
                    })
                    changed = True
                    continue
                if not cls._draft_references_ready(draft):
                    continue

                draft = MarketplaceDraftService.rebase_source_defaults(
                    seller_id=job.seller_id,
                    draft_id=draft.id,
                    expected_version=draft.version,
                )
                draft = MarketplaceDraftService.apply_reference_defaults(
                    seller_id=job.seller_id,
                    draft_id=draft.id,
                    expected_version=draft.version,
                )
                draft = MarketplaceDraftService.apply_account_defaults(
                    seller_id=job.seller_id,
                    draft_id=draft.id,
                    expected_version=draft.version,
                )
                draft = MarketplaceDraftService.validate_draft(
                    seller_id=job.seller_id,
                    draft_id=draft.id,
                    expected_version=draft.version,
                )
                item["completeness"] = (
                    MarketplaceDraftService.completeness_summary(draft)
                )
                validation_errors = cls._validation_errors(draft)
                if cls._reference_wait_required(draft):
                    before = (
                        item.get("validation_errors"),
                        item.get("message"),
                    )
                    cls._mark_waiting_reference(
                        item,
                        now=current_time,
                    )
                    item["validation_errors"] = validation_errors
                    changed = changed or before != (
                        item.get("validation_errors"),
                        item.get("message"),
                    )
                    continue
                if draft.status != "ready" or validation_errors:
                    item.update({
                        "status": "needs_input",
                        "code": (
                            validation_errors[0]["code"]
                            if validation_errors else "draft_not_ready"
                        ),
                        "message": (
                            validation_errors[0]["message"]
                            if validation_errors
                            else "Черновик требует проверки"
                        ),
                        "validation_errors": validation_errors,
                        "updated_at": current_time.isoformat(),
                    })
                    item.pop("reference_wait_started_at", None)
                    changed = True
                    continue
                item["status"] = "queued"
                item["updated_at"] = current_time.isoformat()
                ready_items[draft.id] = item
                changed = True
            except (MarketplaceDraftError, MarketplacePublicationError) as exc:
                db.session.rollback()
                item.update({
                    "status": "needs_input",
                    "code": cls._safe_text(
                        getattr(exc, "code", None),
                        100,
                    ) or "draft_preparation_failed",
                    "message": cls._safe_text(str(exc), 700)
                    or "Не удалось продолжить подготовку карточки",
                    "updated_at": current_time.isoformat(),
                })
                item.pop("reference_wait_started_at", None)
                changed = True
            except Exception:
                db.session.rollback()
                logger.exception(
                    "Ozon reference resume failed job_uid=%s draft_id=%s",
                    job.job_uid,
                    item.get("draft_id"),
                )
                item.update({
                    "status": "failed",
                    "code": "draft_preparation_failed",
                    "message": (
                        "Не удалось безопасно продолжить подготовку карточки"
                    ),
                    "updated_at": current_time.isoformat(),
                })
                item.pop("reference_wait_started_at", None)
                changed = True

        if ready_items:
            cls._enqueue_ready_items(
                seller_id=job.seller_id,
                account_id=document["account_id"],
                created_by_user_id=document.get("created_by_user_id"),
                ready_items=ready_items,
            )
        if changed:
            cls._persist(job, document)
        return changed

    @classmethod
    def reconcile_run(
        cls,
        *,
        seller_id: int,
        job_uid: str,
        rotate: bool = False,
    ) -> BackgroundJob:
        """Reflect current durable operation state into one seller run."""
        job = cls._owned_job(seller_id=seller_id, job_uid=job_uid)
        run = OzonBulkUploadRun.query.filter_by(
            job_id=job.id, seller_id=seller_id,
        ).first()
        if run is not None:
            cls._reconcile_v3(job=job, run=run, rotate=rotate)
            return job
        document = cls._load_progress(job)
        operation_ids = {
            item.get("operation_id")
            for item in document["items"]
            if isinstance(item.get("operation_id"), int)
            and not isinstance(item.get("operation_id"), bool)
        }
        operations = {}
        if operation_ids:
            operations = {
                operation.id: operation
                for operation in MarketplaceOperation.query.filter(
                    MarketplaceOperation.seller_id == seller_id,
                    MarketplaceOperation.account_id == document["account_id"],
                    MarketplaceOperation.id.in_(operation_ids),
                ).all()
            }

        changed = False
        now = datetime.utcnow()
        stale_before = now - timedelta(
            seconds=cls.PREPARATION_STALE_SECONDS,
        )
        for item in document["items"]:
            operation_id = item.get("operation_id")
            if operation_id is None:
                item_updated_at = (
                    cls._parsed_datetime(item.get("updated_at"))
                    or job.created_at
                    or now
                )
                if (
                    item.get("status") in {
                        "preparing", "queued", "waiting_reference",
                    }
                    and (
                        item.get("status") == "waiting_reference"
                        or item_updated_at <= stale_before
                    )
                ):
                    item.update({
                        "status": "needs_manual_reconciliation",
                        "code": "legacy_upload_review_required",
                        "message": (
                            "Старый запуск требует ручной проверки операции "
                            "и нового подтверждения черновика"
                        ),
                        "updated_at": now.isoformat(),
                    })
                    changed = True
                continue
            operation = operations.get(operation_id)
            if operation is not None and (
                isinstance(item.get("draft_id"), int)
                and operation.draft_id != item["draft_id"]
            ):
                operation = None
            if operation is None:
                if item.get("status") not in cls.TERMINAL_ITEM_STATUSES:
                    item.update({
                        "status": "needs_manual_reconciliation",
                        "code": "publication_operation_missing",
                        "message": "Операция публикации недоступна; требуется ручная проверка",
                        "updated_at": datetime.utcnow().isoformat(),
                    })
                    changed = True
                continue
            before = (
                item.get("status"),
                item.get("code"),
                item.get("message"),
            )
            cls._operation_to_item(item, operation)
            after = (
                item.get("status"),
                item.get("code"),
                item.get("message"),
            )
            changed = changed or before != after

        summary_before = job.get_result()
        summary_without_timestamp = {
            key: value
            for key, value in summary_before.items()
            if key != "updated_at"
        }
        expected_summary = cls._summary_payload(job, document)
        expected_job_status = (
            "running" if expected_summary["active"] else "completed"
        )
        if (
            changed
            or expected_summary != summary_without_timestamp
            or job.status != expected_job_status
        ):
            cls._persist(job, document)
        elif rotate and job.status == "running":
            # Round-robin reconciliation even when an operation has not
            # changed: an unchanged first page must not hide run 21.
            job.updated_at = datetime.utcnow()
            db.session.commit()
        return job

    @classmethod
    def reconcile_active_runs(cls, *, limit: int = 20) -> dict:
        """Bounded scheduler hook; no provider calls."""
        limit = cls._positive_integer(limit, "limit")
        if limit > cls.MAX_LIST_JOBS:
            raise OzonBulkUploadValidationError(
                f"limit не может быть больше {cls.MAX_LIST_JOBS}"
            )
        jobs = BackgroundJob.query.filter_by(
            job_type=cls.JOB_TYPE,
            status="running",
        ).order_by(
            BackgroundJob.updated_at.asc(),
            BackgroundJob.id.asc(),
        ).limit(limit).all()
        result = {"selected": len(jobs), "reconciled": 0, "failed": 0}
        for job in jobs:
            try:
                cls.reconcile_run(
                    seller_id=job.seller_id,
                    job_uid=job.job_uid,
                    rotate=True,
                )
                result["reconciled"] += 1
            except Exception:
                db.session.rollback()
                logger.exception(
                    "Ozon bulk run reconciliation failed job_uid=%s",
                    job.job_uid,
                )
                result["failed"] += 1
        return result

    @classmethod
    def get_run(
        cls,
        *,
        seller_id: int,
        job_uid: str,
        reconcile: bool = True,
    ) -> BackgroundJob:
        job = cls._owned_job(seller_id=seller_id, job_uid=job_uid)
        if not reconcile:
            return job
        # V3 result pages derive their live view from the durable run/item/
        # operation rows. Only the scheduler is allowed to persist that
        # summary; a seller GET remains read-only. Legacy runs retain their
        # historical on-read reconciliation behavior.
        run = OzonBulkUploadRun.query.filter_by(
            job_id=job.id, seller_id=seller_id,
        ).first()
        if run is not None:
            return job
        return cls.reconcile_run(seller_id=seller_id, job_uid=job_uid)

    @classmethod
    def retry_run(
        cls,
        *,
        seller_id: int,
        job_uid: str,
        request_key: str,
        created_by_user_id: Optional[int] = None,
        canonical_scope: bool = False,
    ) -> UploadRunAcceptance:
        """Only local source preparation can be retried without new review."""
        previous = cls._owned_job(seller_id=seller_id, job_uid=job_uid)
        run = OzonBulkUploadRun.query.filter_by(
            job_id=previous.id, seller_id=seller_id,
        ).first()
        if run is None or run.mode != "source_prepare":
            error = OzonBulkUploadConflict(
                "Проверьте текущие версии черновиков перед новой отправкой"
            )
            error.code = "draft_review_required"
            raise error
        if run.state != "completed":
            raise OzonBulkUploadConflict(
                "Подготовка ещё выполняется; дождитесь её результата"
            )
        if not isinstance(canonical_scope, bool):
            raise OzonBulkUploadValidationError(
                "canonical_scope должен быть boolean"
            )
        request_key_hash = cls._request_identity(
            seller_id=seller_id, request_key=request_key,
        )
        if canonical_scope:
            if not cls.PREPARE_RETRY_KEY_PATTERN.fullmatch(request_key):
                raise OzonBulkUploadValidationError(
                    "Ключ повтора не соответствует выбранным карточкам"
                )
            replay = cls._existing_canonical_prepare_retry(
                seller_id=seller_id, parent=run,
                parent_job_uid=previous.job_uid,
                request_key=request_key,
                request_key_hash=request_key_hash,
            )
            if replay is not None:
                return replay

        try:
            cls._lock_prepare_retry_parent(job=previous)
            run = OzonBulkUploadRun.query.populate_existing().filter_by(
                job_id=previous.id, seller_id=seller_id,
            ).first()
            if run is None or run.mode != "source_prepare":
                raise cls._scope_changed_retry_error()
            if run.state != "completed":
                raise OzonBulkUploadConflict(
                    "Подготовка ещё выполняется; дождитесь её результата"
                )
            if canonical_scope:
                replay = cls._existing_canonical_prepare_retry(
                    seller_id=seller_id, parent=run,
                    parent_job_uid=previous.job_uid,
                    request_key=request_key,
                    request_key_hash=request_key_hash,
                )
                if replay is not None:
                    db.session.rollback()
                    return replay

            items = OzonBulkUploadItem.query.populate_existing().filter_by(
                run_id=run.id,
            ).order_by(OzonBulkUploadItem.ordinal.asc()).all()
            product_ids = cls.prepare_retry_product_ids(
                mode=run.mode, state=run.state, items=items,
            )
            if not product_ids:
                db.session.rollback()
                raise OzonBulkUploadValidationError(
                    "В этом запуске нет карточек для повторной локальной подготовки"
                )
            if canonical_scope:
                expected_key = cls._prepare_retry_request_key(
                    seller_id=seller_id, account_id=run.account_id,
                    parent_job_uid=previous.job_uid,
                    product_ids=product_ids,
                )
                if expected_key != request_key:
                    raise cls._scope_changed_retry_error()
            acceptance = cls.accept_source_prepare(
                seller_id=seller_id,
                account_id=run.account_id,
                imported_product_ids=product_ids,
                request_key=request_key,
                created_by_user_id=created_by_user_id,
                _parent_prepare_run_id=run.id,
            )
            if acceptance.replayed:
                db.session.rollback()
            return acceptance
        except OperationalError:
            db.session.rollback()
            error = OzonBulkUploadConflict(
                "Не удалось подтвердить состав карточек для повтора; обновите страницу и проверьте результат"
            )
            error.code = "ozon_prepare_retry_scope_unavailable"
            raise error from None
        except Exception:
            # The parent lock and any attempted child acceptance share this
            # transaction. Always release it when validation or construction
            # fails so a later normal retry/read is not left behind a write.
            db.session.rollback()
            raise

    @classmethod
    def list_runs(
        cls,
        *,
        seller_id: int,
        limit: int = 50,
        account_id: Optional[int] = None,
    ) -> list:
        seller_id = cls._positive_integer(seller_id, "seller_id")
        limit = cls._positive_integer(limit, "limit")
        if limit > cls.MAX_LIST_JOBS:
            raise OzonBulkUploadValidationError(
                f"limit не может быть больше {cls.MAX_LIST_JOBS}"
            )
        query = BackgroundJob.query.filter_by(
            seller_id=seller_id,
            job_type=cls.JOB_TYPE,
        )
        if account_id is not None:
            account_id = cls._positive_integer(account_id, 'account_id')
            try:
                MarketplaceAccountService.get_owned_account(
                    seller_id=seller_id, account_id=account_id, marketplace_code='ozon',
                )
            except MarketplaceAccountError as exc:
                raise cls._translate_account_error(exc) from None
            # Filter before the 50-row limit: newer runs of another owned
            # account must not hide this account's history. Bad legacy JSON
            # is not a match and cannot break JSON extraction in SQLite.
            document = case((func.json_valid(BackgroundJob.progress_data) == 1,
                             BackgroundJob.progress_data), else_='{}')
            query = query.filter(func.json_extract(document, '$.account_id') == account_id)
        return query.order_by(
            BackgroundJob.created_at.desc(),
            BackgroundJob.id.desc(),
        ).limit(limit).all()

    @classmethod
    def public_document(cls, job: BackgroundJob, *, detail: bool = False) -> dict:
        if not isinstance(job, BackgroundJob) or job.job_type != cls.JOB_TYPE:
            raise OzonBulkUploadValidationError(
                "Ожидался запуск массовой загрузки Ozon"
            )
        run = OzonBulkUploadRun.query.filter_by(
            job_id=job.id, seller_id=job.seller_id,
        ).first()
        if run is not None:
            observed = cls._v3_document(
                job=job, run=run, include_recovery=detail,
            )
            summary = observed["summary"]
            previous_summary = job.get_result()
            account_label = (
                previous_summary.get("account_label")
                if isinstance(previous_summary, dict) else None
            )
            summary["account_label"] = account_label
            document = {
                "job_uid": job.job_uid, "status": (
                    "running" if summary["active"] else "completed"
                ),
                "mode": run.mode, "account_id": run.account_id,
                "account_label": account_label, "summary": summary,
                "created_at": job.created_at.isoformat() if job.created_at else None,
                "updated_at": job.updated_at.isoformat() if job.updated_at else None,
            }
            if detail and run.mode == "source_prepare":
                # This is a durable page-transition signal.  A locally
                # terminal-looking item summary is insufficient until the
                # coordinator has also committed the run's terminal state.
                document["source_prepare_complete"] = (
                    run.state == "completed" and summary["active"] == 0
                )
                retry_product_ids = cls.prepare_retry_product_ids(
                    mode=run.mode, state=run.state, items=observed["items"],
                )
                document["prepare_retry_count"] = len(retry_product_ids)
                parent_job_uid = cls._trusted_previous_prepare_job_uid(run)
                if parent_job_uid is not None:
                    document["previous_prepare_job_uid"] = parent_job_uid
            if detail:
                document["items"] = observed["items"]
                if run.mode == "reviewed_drafts":
                    document["review_recovery_draft_ids"] = observed.get(
                        "review_recovery_draft_ids", [],
                    )
            return document
        progress = cls._load_progress(job)
        if not progress:
            raise OzonBulkUploadConflict(
                "Сводка запуска повреждена; операции Ozon сохранены отдельно"
            )
        summary = job.get_result()
        document = {
            "job_uid": job.job_uid,
            "status": job.status,
            "account_id": progress.get("account_id"),
            "account_label": progress.get("account_label"),
            "summary": summary,
            "created_at": (
                job.created_at.isoformat() if job.created_at else None
            ),
            "updated_at": (
                job.updated_at.isoformat() if job.updated_at else None
            ),
        }
        if detail:
            document["items"] = progress["items"]
            mapping_preflight = progress.get(
                "category_mapping_preflight"
            )
            if isinstance(mapping_preflight, dict):
                document["category_mapping_preflight"] = {
                    key: mapping_preflight.get(key)
                    for key in (
                        "success",
                        "code",
                        "created",
                        "refreshed",
                        "staled",
                        "protected",
                        "safe_groups",
                        "unsafe_groups",
                        "observed_listings",
                    )
                    if key in mapping_preflight
                }
        return document
