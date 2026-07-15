"""Durable, seller-scoped manual Ozon publication state machine."""

from __future__ import annotations

from datetime import datetime, timedelta
import json
import re
from typing import Any, Optional, Tuple

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload
from sqlalchemy.orm.exc import StaleDataError

from models import (
    Marketplace,
    MarketplaceCredentialEncryptionError,
    MarketplaceListing,
    MarketplaceListingSnapshot,
    MarketplaceOperation,
    MarketplaceProductDraft,
    MarketplaceProductType,
    ImportedProduct,
    Seller,
    SellerMarketplaceAccount,
    db,
)
from services.marketplace_accounts import (
    MarketplaceAccountNotFound,
    MarketplaceAccountService,
)
from services.marketplace_adapters import (
    MarketplaceCredentials,
    get_marketplace_registry,
)
from services.marketplace_adapters.base import MarketplaceAdapterError
from services.marketplace_drafts import MarketplaceDraftService
from services.marketplace_listings import (
    MarketplaceCatalogProtocolError,
    MarketplaceListingService,
)
from services.marketplace_operation_locks import (
    release_account_operation_lock,
    try_account_operation_lock,
)
from services.ozon_api_client import (
    OzonAPIError,
    OzonAmbiguousWriteError,
)
from services.ozon_product_import import (
    OzonProductImportContract,
    OzonProductImportPayloadError,
    OzonProductImportProtocolError,
)


class MarketplacePublicationError(RuntimeError):
    status_code = 400
    code = "marketplace_publication_error"


class MarketplacePublicationValidationError(MarketplacePublicationError):
    status_code = 400
    code = "invalid_marketplace_publication"

    def __init__(self, message: str, *, validation: Optional[dict] = None) -> None:
        super().__init__(message)
        self.validation = validation or {}


class MarketplacePublicationNotFound(MarketplacePublicationError):
    status_code = 404
    code = "marketplace_publication_not_found"


class MarketplacePublicationConflict(MarketplacePublicationError):
    status_code = 409
    code = "marketplace_publication_conflict"


class MarketplacePublicationBusy(MarketplacePublicationError):
    status_code = 409
    code = "marketplace_publication_busy"


class MarketplacePublicationConfigurationError(MarketplacePublicationError):
    status_code = 409
    code = "marketplace_publication_not_ready"


class MarketplacePublicationUpstreamError(MarketplacePublicationError):
    status_code = 502
    code = "ozon_publication_upstream_error"


class MarketplacePublicationService:
    ACTIVE_STATUSES = {
        "queued",
        "submitting",
        "submitted",
        "polling",
        "uncertain",
    }
    TERMINAL_STATUSES = {"succeeded", "partial", "failed", "cancelled"}
    IDEMPOTENCY_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{16,128}$")
    SUBMISSION_DEADLINE = timedelta(hours=24)
    POLL_INTERVAL = timedelta(seconds=15)
    RECONCILE_INTERVAL = timedelta(seconds=60)
    MAX_PROVIDER_REQUEST_IDS = 50
    MAX_DUE_OPERATIONS = 50
    MAX_JSON_BYTES = 512 * 1024

    @staticmethod
    def _positive_integer(value: Any, field_name: str) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise MarketplacePublicationValidationError(
                f"{field_name} должен быть положительным целым числом"
            )
        return value

    @classmethod
    def _idempotency_key(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise MarketplacePublicationValidationError(
                "idempotency_key должен быть строкой"
            )
        normalized = value.strip()
        if not cls.IDEMPOTENCY_PATTERN.fullmatch(normalized):
            raise MarketplacePublicationValidationError(
                "idempotency_key должен содержать 16–128 безопасных символов"
            )
        return normalized

    @staticmethod
    def _safe_text(value: Any, *, maximum: int) -> Optional[str]:
        if value in (None, ""):
            return None
        text = " ".join(
            str(value).replace("\x00", " ").replace("\r", " ").replace("\n", " ").split()
        )
        return text[:maximum] or None

    @classmethod
    def _json(cls, value: Any, expected_type: type) -> str:
        if not isinstance(value, expected_type):
            raise MarketplacePublicationValidationError(
                "Internal publication snapshot has an unexpected type"
            )
        encoded = OzonProductImportContract.canonical_json(value)
        if len(encoded.encode("utf-8")) > cls.MAX_JSON_BYTES:
            raise MarketplacePublicationValidationError(
                "Publication snapshot exceeds storage limit"
            )
        return encoded

    @staticmethod
    def _try_claim(account_id: int):
        return try_account_operation_lock(account_id)

    @staticmethod
    def _release_claim(lock_file) -> None:
        release_account_operation_lock(lock_file)

    @classmethod
    def _owned_operation(
        cls,
        *,
        seller_id: int,
        operation_id: int,
    ) -> MarketplaceOperation:
        seller_id = cls._positive_integer(seller_id, "seller_id")
        operation_id = cls._positive_integer(operation_id, "operation_id")
        operation = MarketplaceOperation.query.options(
            joinedload(MarketplaceOperation.marketplace),
            joinedload(MarketplaceOperation.account),
            joinedload(MarketplaceOperation.draft),
            joinedload(MarketplaceOperation.listing),
            joinedload(MarketplaceOperation.snapshot),
        ).filter_by(
            id=operation_id,
            seller_id=seller_id,
        ).first()
        if operation is None:
            raise MarketplacePublicationNotFound("Операция публикации не найдена")
        if (
            operation.account is None
            or operation.account.seller_id != seller_id
            or operation.account.marketplace_id != operation.marketplace_id
        ):
            raise MarketplacePublicationNotFound("Операция публикации не найдена")
        return operation

    @classmethod
    def get_operation(
        cls,
        *,
        seller_id: int,
        operation_id: int,
    ) -> MarketplaceOperation:
        return cls._owned_operation(
            seller_id=seller_id,
            operation_id=operation_id,
        )

    @classmethod
    def list_for_draft(
        cls,
        *,
        seller_id: int,
        draft_id: int,
        limit: int = 20,
    ) -> list:
        seller_id = cls._positive_integer(seller_id, "seller_id")
        draft_id = cls._positive_integer(draft_id, "draft_id")
        limit = cls._positive_integer(limit, "limit")
        if limit > 100:
            raise MarketplacePublicationValidationError(
                "limit не может быть больше 100"
            )
        draft = MarketplaceDraftService.get_draft(
            seller_id=seller_id,
            draft_id=draft_id,
        )
        return MarketplaceOperation.query.options(
            joinedload(MarketplaceOperation.marketplace),
            joinedload(MarketplaceOperation.account),
        ).filter_by(
            seller_id=seller_id,
            draft_id=draft.id,
            account_id=draft.account_id,
        ).order_by(
            MarketplaceOperation.created_at.desc(),
            MarketplaceOperation.id.desc(),
        ).limit(limit).all()

    @classmethod
    def list_operations(
        cls,
        *,
        seller_id: int,
        account_id: Optional[int] = None,
        status: Optional[str] = None,
        page: int = 1,
        per_page: int = 50,
    ):
        seller_id = cls._positive_integer(seller_id, "seller_id")
        page = cls._positive_integer(page, "page")
        per_page = cls._positive_integer(per_page, "per_page")
        if per_page > 100:
            raise MarketplacePublicationValidationError(
                "per_page не может быть больше 100"
            )
        query = MarketplaceOperation.query.options(
            joinedload(MarketplaceOperation.marketplace),
            joinedload(MarketplaceOperation.account),
            joinedload(MarketplaceOperation.snapshot),
        ).join(Marketplace).filter(
            MarketplaceOperation.seller_id == seller_id,
            Marketplace.code == "ozon",
        )
        if account_id is not None:
            account_id = cls._positive_integer(account_id, "account_id")
            try:
                MarketplaceAccountService.get_owned_account(
                    seller_id=seller_id,
                    account_id=account_id,
                    marketplace_code="ozon",
                )
            except MarketplaceAccountNotFound:
                raise MarketplacePublicationNotFound(
                    "Кабинет Ozon не найден"
                ) from None
            query = query.filter(MarketplaceOperation.account_id == account_id)
        if status is not None:
            if not isinstance(status, str) or status not in (
                cls.ACTIVE_STATUSES | cls.TERMINAL_STATUSES
            ):
                raise MarketplacePublicationValidationError(
                    "Неизвестный статус операции"
                )
            query = query.filter(MarketplaceOperation.status == status)
        return query.order_by(
            MarketplaceOperation.created_at.desc(),
            MarketplaceOperation.id.desc(),
        ).paginate(page=page, per_page=per_page, error_out=False)

    @classmethod
    def _account_adapter_credentials(
        cls,
        *,
        seller_id: int,
        account_id: int,
        adapter=None,
        credentials: Optional[MarketplaceCredentials] = None,
        now: Optional[datetime] = None,
    ) -> Tuple[SellerMarketplaceAccount, Any, MarketplaceCredentials]:
        try:
            account = MarketplaceAccountService.get_owned_account(
                seller_id=seller_id,
                account_id=account_id,
                marketplace_code="ozon",
            )
        except MarketplaceAccountNotFound:
            raise MarketplacePublicationNotFound("Кабинет Ozon не найден") from None
        current_time = now or datetime.utcnow()
        if not account.is_active or account.connection_status != "connected":
            raise MarketplacePublicationConfigurationError(
                "Кабинет Ozon должен быть активен и пройти проверку подключения"
            )
        if (
            account.credential_expires_at
            and account.credential_expires_at <= current_time
        ):
            raise MarketplacePublicationConfigurationError(
                "Срок действия API key Ozon истёк"
            )
        if adapter is not None or credentials is not None:
            if adapter is None or credentials is None:
                raise MarketplacePublicationValidationError(
                    "adapter and credentials must be injected together"
                )
            resolved_adapter = adapter
            resolved_credentials = credentials
        else:
            if not account.has_credentials:
                raise MarketplacePublicationConfigurationError(
                    "В кабинете Ozon нет сохранённого API key"
                )
            try:
                secret = account.get_credentials()
                resolved_credentials = MarketplaceCredentials(
                    external_account_id=account.external_account_id,
                    api_key=secret["api_key"],
                )
                del secret
            except (KeyError, ValueError, MarketplaceCredentialEncryptionError):
                raise MarketplacePublicationConfigurationError(
                    "Credentials кабинета Ozon невозможно прочитать"
                ) from None
            resolved_adapter = get_marketplace_registry().get("ozon")
        try:
            resolved_adapter.require_capability("catalog_write")
            resolved_adapter.require_capability("catalog_read")
        except MarketplaceAdapterError as exc:
            raise MarketplacePublicationConfigurationError(str(exc)) from None
        return account, resolved_adapter, resolved_credentials

    @classmethod
    def _validate_author(
        cls,
        *,
        seller_id: int,
        created_by_user_id: Optional[int],
    ) -> Optional[int]:
        if created_by_user_id is None:
            return None
        user_id = cls._positive_integer(created_by_user_id, "created_by_user_id")
        seller = Seller.query.filter_by(id=seller_id, user_id=user_id).first()
        if seller is None:
            raise MarketplacePublicationNotFound("Seller user scope не найден")
        return user_id

    @classmethod
    def _publication_payload(
        cls,
        *,
        seller_id: int,
        draft_id: int,
        expected_version: int,
    ) -> Tuple[MarketplaceProductDraft, dict, dict]:
        expected_version = cls._positive_integer(
            expected_version,
            "expected_version",
        )
        draft = MarketplaceDraftService.get_draft(
            seller_id=seller_id,
            draft_id=draft_id,
        )
        if draft.version != expected_version:
            raise MarketplacePublicationConflict(
                "Черновик изменился; обновите страницу перед публикацией"
            )
        if draft.status != "ready" or draft.validation_status != "valid":
            raise MarketplacePublicationConflict(
                "Перед публикацией черновик должен иметь статус ready/valid"
            )
        validation = MarketplaceDraftService._build_validation_result(draft)
        if not validation.get("publishable"):
            raise MarketplacePublicationValidationError(
                "Черновик больше не проходит полную проверку публикации",
                validation=validation,
            )
        schema = validation.get("schema") if isinstance(validation, dict) else None
        if (
            not isinstance(schema, dict)
            or schema.get("hash") != draft.schema_hash
            or schema.get("version") != draft.schema_version
        ):
            raise MarketplacePublicationConflict(
                "Ozon schema изменилась после валидации; провалидируйте черновик заново"
            )
        try:
            payload = OzonProductImportContract.build_payload(draft)
        except OzonProductImportPayloadError as exc:
            raise MarketplacePublicationValidationError(str(exc)) from None
        return draft, payload, validation

    @classmethod
    def _request_summary(
        cls,
        *,
        draft: MarketplaceProductDraft,
        payload: dict,
    ) -> dict:
        item = payload["items"][0]
        return {
            "offer_id": item["offer_id"],
            "imported_product_id": draft.imported_product_id,
            "product_type_id": draft.product_type_id,
            "description_category_id": str(item["description_category_id"]),
            "type_id": str(item["type_id"]),
            "draft_version": draft.version,
            "source_fact_hash": draft.source_fact_hash,
            "schema_hash": draft.schema_hash,
            "attribute_count": len(item.get("attributes", [])),
            "complex_group_count": len(item.get("complex_attributes", [])),
            "image_count": len(item.get("images", [])),
            "has_barcode": bool(item.get("barcode")),
            "currency_code": item["currency_code"],
        }

    @classmethod
    def _operation_summary(cls, operation: MarketplaceOperation) -> dict:
        try:
            summary = json.loads(operation.request_summary_json or "")
        except (TypeError, json.JSONDecodeError):
            summary = None
        if not isinstance(summary, dict):
            raise MarketplacePublicationConflict(
                "Сохранённое описание операции повреждено"
            )
        offer_id = summary.get("offer_id")
        if (
            not isinstance(offer_id, str)
            or not offer_id
            or len(offer_id) > OzonProductImportContract.MAX_OFFER_ID_CHARS
        ):
            raise MarketplacePublicationConflict(
                "Сохранённый offer_id операции повреждён"
            )
        return summary

    @classmethod
    def _existing_idempotent_operation(
        cls,
        *,
        seller_id: int,
        draft_id: int,
        expected_version: int,
        idempotency_key: str,
    ) -> Optional[MarketplaceOperation]:
        existing = MarketplaceOperation.query.filter_by(
            seller_id=seller_id,
            draft_id=draft_id,
            operation_kind="product_import",
            idempotency_key=idempotency_key,
        ).first()
        if existing is None:
            return None
        if existing.draft_version != expected_version:
            raise MarketplacePublicationConflict(
                "idempotency_key уже использован для другой версии черновика"
            )
        return cls._owned_operation(
            seller_id=seller_id,
            operation_id=existing.id,
        )

    @classmethod
    def _create_operation(
        cls,
        *,
        draft: MarketplaceProductDraft,
        payload: dict,
        idempotency_key: str,
        created_by_user_id: Optional[int],
        now: datetime,
    ) -> MarketplaceOperation:
        fingerprint = OzonProductImportContract.fingerprint(payload)
        existing = MarketplaceOperation.query.filter_by(
            account_id=draft.account_id,
            operation_kind="product_import",
            idempotency_key=idempotency_key,
        ).first()
        if existing is not None:
            if (
                existing.seller_id != draft.seller_id
                or existing.draft_id != draft.id
                or existing.request_fingerprint != fingerprint
            ):
                raise MarketplacePublicationConflict(
                    "idempotency_key уже использован для другого запроса"
                )
            return cls._owned_operation(
                seller_id=draft.seller_id,
                operation_id=existing.id,
            )

        active = MarketplaceOperation.query.filter(
            MarketplaceOperation.seller_id == draft.seller_id,
            MarketplaceOperation.account_id == draft.account_id,
            MarketplaceOperation.draft_id == draft.id,
            MarketplaceOperation.status.in_(cls.ACTIVE_STATUSES),
        ).first()
        if active is not None:
            if active.request_fingerprint == fingerprint:
                return cls._owned_operation(
                    seller_id=draft.seller_id,
                    operation_id=active.id,
                )
            raise MarketplacePublicationConflict(
                "Для черновика уже выполняется другая публикация"
            )

        summary = cls._request_summary(draft=draft, payload=payload)
        operation = MarketplaceOperation(
            seller_id=draft.seller_id,
            marketplace_id=draft.marketplace_id,
            account_id=draft.account_id,
            draft_id=draft.id,
            created_by_user_id=created_by_user_id,
            operation_kind="product_import",
            status="queued",
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
            contract_version=OzonProductImportContract.CONTRACT_VERSION,
            draft_version=draft.version,
            request_summary_json=cls._json(summary, dict),
            quota_snapshot_json="{}",
            provider_request_ids_json="[]",
            item_results_json="[]",
            next_poll_at=now,
            deadline_at=now + cls.SUBMISSION_DEADLINE,
        )
        db.session.add(operation)
        db.session.flush()
        snapshot = MarketplaceListingSnapshot(
            seller_id=draft.seller_id,
            marketplace_id=draft.marketplace_id,
            account_id=draft.account_id,
            operation_id=operation.id,
            draft_id=draft.id,
            snapshot_kind="product_import",
            source_fingerprint=draft.source_fact_hash,
            submitted_fingerprint=fingerprint,
            before_state_json=cls._json({
                "state": "pending_live_preflight",
                "offer_id": draft.offer_id,
            }, dict),
            submitted_state_json=cls._json(payload, dict),
            confirmed_state_json="{}",
            rollback_state_json="{}",
            rollback_status="unavailable",
        )
        db.session.add(snapshot)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            existing = MarketplaceOperation.query.filter_by(
                account_id=draft.account_id,
                operation_kind="product_import",
                idempotency_key=idempotency_key,
            ).first()
            if existing is None:
                existing = MarketplaceOperation.query.filter(
                    MarketplaceOperation.draft_id == draft.id,
                    MarketplaceOperation.status.in_(cls.ACTIVE_STATUSES),
                ).first()
            if existing is None:
                raise MarketplacePublicationConflict(
                    "Параллельная публикация уже изменила состояние"
                ) from None
            if (
                existing.seller_id != draft.seller_id
                or existing.request_fingerprint != fingerprint
            ):
                raise MarketplacePublicationConflict(
                    "Параллельная публикация конфликтует с текущим запросом"
                ) from None
            return cls._owned_operation(
                seller_id=draft.seller_id,
                operation_id=existing.id,
            )
        return cls._owned_operation(
            seller_id=draft.seller_id,
            operation_id=operation.id,
        )

    @classmethod
    def _append_request_id(
        cls,
        operation: MarketplaceOperation,
        request_id: Optional[str],
    ) -> None:
        request_id = cls._safe_text(request_id, maximum=200)
        if not request_id:
            return
        try:
            values = json.loads(operation.provider_request_ids_json or "[]")
        except (TypeError, json.JSONDecodeError):
            values = []
        if not isinstance(values, list):
            values = []
        values = [value for value in values if isinstance(value, str)]
        if request_id not in values:
            values.append(request_id)
        operation.provider_request_ids_json = cls._json(
            values[-cls.MAX_PROVIDER_REQUEST_IDS:],
            list,
        )

    @classmethod
    def _mark_failed(
        cls,
        operation: MarketplaceOperation,
        *,
        code: str,
        message: str,
        now: datetime,
        request_id: Optional[str] = None,
    ) -> None:
        operation.status = "failed"
        operation.error_code = cls._safe_text(code, maximum=100)
        operation.error_message = cls._safe_text(message, maximum=1000)
        operation.quota_reserved = 0
        operation.next_poll_at = None
        operation.completed_at = now
        cls._append_request_id(operation, request_id)
        db.session.commit()

    @classmethod
    def _mark_uncertain(
        cls,
        operation: MarketplaceOperation,
        *,
        code: str,
        message: str,
        now: datetime,
        request_id: Optional[str] = None,
    ) -> None:
        operation.status = "uncertain"
        operation.error_code = cls._safe_text(code, maximum=100)
        operation.error_message = cls._safe_text(message, maximum=1000)
        operation.next_poll_at = now + cls.RECONCILE_INTERVAL
        cls._append_request_id(operation, request_id)
        db.session.commit()

    @classmethod
    def _defer_prewrite(
        cls,
        operation: MarketplaceOperation,
        *,
        code: str,
        message: str,
        now: datetime,
        request_id: Optional[str] = None,
    ) -> None:
        """Keep a definitely-not-submitted operation safely retryable."""
        operation.status = "queued"
        operation.error_code = cls._safe_text(code, maximum=100)
        operation.error_message = cls._safe_text(message, maximum=1000)
        operation.quota_reserved = 0
        operation.next_poll_at = now + cls.RECONCILE_INTERVAL
        cls._append_request_id(operation, request_id)
        db.session.commit()

    @classmethod
    def _offer_lookup(
        cls,
        *,
        adapter,
        credentials: MarketplaceCredentials,
        offer_id: str,
    ) -> list:
        found = {}
        for visibility in ("ALL", "ARCHIVED"):
            response = adapter.list_products(credentials, {
                "filter": {
                    "offer_id": [offer_id],
                    "visibility": visibility,
                },
                "last_id": "",
                "limit": 100,
            })
            try:
                page = MarketplaceListingService.normalize_product_list_page(response)
            except MarketplaceCatalogProtocolError as exc:
                raise MarketplacePublicationUpstreamError(
                    "Ozon вернул некорректный ответ проверки offer_id"
                ) from exc
            if page["cursor"] or page["total"] != len(page["items"]):
                raise MarketplacePublicationUpstreamError(
                    "Ozon offer_id preflight returned an incomplete page"
                )
            for item in page["items"]:
                if item["offer_id"] != offer_id:
                    raise MarketplacePublicationUpstreamError(
                        "Ozon offer_id preflight returned a foreign offer"
                    )
                product_id = item["product_id"]
                previous = found.get(product_id)
                if previous and previous["offer_id"] != item["offer_id"]:
                    raise MarketplacePublicationUpstreamError(
                        "Ozon offer_id preflight returned conflicting identities"
                    )
                found[product_id] = item
        if len(found) > 1:
            raise MarketplacePublicationUpstreamError(
                "Ozon returned multiple products for one account offer_id"
            )
        return list(found.values())

    @classmethod
    def _live_preflight(
        cls,
        operation: MarketplaceOperation,
        *,
        adapter,
        credentials: MarketplaceCredentials,
        now: datetime,
    ) -> bool:
        summary = cls._operation_summary(operation)
        offer_id = summary["offer_id"]
        found = cls._offer_lookup(
            adapter=adapter,
            credentials=credentials,
            offer_id=offer_id,
        )
        before_state = {
            "checked_at": now.isoformat(),
            "offer_id": offer_id,
            "exists": bool(found),
            "items": [{
                "product_id": item["product_id"],
                "archived": bool(item["archived"]),
            } for item in found],
        }
        snapshot = operation.snapshot
        if snapshot is None:
            cls._mark_failed(
                operation,
                code="publication_snapshot_missing",
                message="Операция не имеет обязательного before-write snapshot",
                now=now,
            )
            return False
        snapshot.before_state_json = cls._json(before_state, dict)
        snapshot.before_fingerprint = OzonProductImportContract.fingerprint(
            before_state
        )
        if found:
            cls._mark_failed(
                operation,
                code="offer_exists_upstream",
                message=(
                    "offer_id уже существует в Ozon; синхронизируйте каталог "
                    "и используйте отдельный validated update workflow"
                ),
                now=now,
            )
            return False
        db.session.commit()
        return True

    @classmethod
    def _reserve_quota(
        cls,
        operation: MarketplaceOperation,
        *,
        adapter,
        credentials: MarketplaceCredentials,
        now: datetime,
    ) -> bool:
        try:
            response = adapter.get_operation_limits(credentials)
            quota = OzonProductImportContract.normalize_quota(
                response,
                mode="create",
            )
        except OzonAPIError as exc:
            cls._mark_failed(
                operation,
                code="quota_preflight_failed",
                message="Не удалось получить текущую квоту Ozon",
                now=now,
                request_id=exc.request_id,
            )
            return False
        except (OzonProductImportProtocolError, MarketplaceAdapterError):
            cls._mark_failed(
                operation,
                code="quota_contract_invalid",
                message="Ozon вернул неизвестный формат квоты товарных операций",
                now=now,
            )
            return False
        except Exception:
            cls._mark_failed(
                operation,
                code="quota_preflight_failed",
                message="Не удалось безопасно проверить квоту Ozon",
                now=now,
            )
            return False

        locally_reserved = db.session.query(
            func.coalesce(func.sum(MarketplaceOperation.quota_reserved), 0)
        ).filter(
            MarketplaceOperation.account_id == operation.account_id,
            MarketplaceOperation.id != operation.id,
            MarketplaceOperation.status.in_(cls.ACTIVE_STATUSES),
        ).scalar() or 0
        locally_reserved = int(locally_reserved)
        available = max(0, quota["remaining"] - locally_reserved)
        quota_snapshot = dict(quota)
        quota_snapshot.update({
            "checked_at": now.isoformat(),
            "local_reserved_before": locally_reserved,
            "available_after_local_reservations": available,
            "requested": 1,
        })
        operation.quota_snapshot_json = cls._json(quota_snapshot, dict)
        if available < 1:
            cls._mark_failed(
                operation,
                code="quota_exhausted",
                message="Квота товарных операций Ozon исчерпана",
                now=now,
            )
            return False
        operation.quota_reserved = 1
        operation.status = "submitting"
        operation.attempt_count += 1
        operation.submitted_at = operation.submitted_at or now
        operation.next_poll_at = now
        operation.deadline_at = operation.deadline_at or (
            now + cls.SUBMISSION_DEADLINE
        )
        operation.error_code = None
        operation.error_message = None
        db.session.commit()
        return True

    @classmethod
    def _submitted_payload(cls, operation: MarketplaceOperation) -> dict:
        snapshot = operation.snapshot
        if snapshot is None:
            raise MarketplacePublicationConflict(
                "Операция не имеет обязательного before-write snapshot"
            )
        try:
            payload = json.loads(snapshot.submitted_state_json)
        except (TypeError, json.JSONDecodeError):
            raise MarketplacePublicationConflict(
                "Snapshot операции повреждён"
            ) from None
        if not isinstance(payload, dict):
            raise MarketplacePublicationConflict("Snapshot операции повреждён")
        if OzonProductImportContract.fingerprint(payload) != operation.request_fingerprint:
            raise MarketplacePublicationConflict(
                "Fingerprint snapshot не совпадает с операцией"
            )
        return payload

    @classmethod
    def _submit(
        cls,
        operation: MarketplaceOperation,
        *,
        adapter,
        credentials: MarketplaceCredentials,
        now: datetime,
    ) -> MarketplaceOperation:
        if operation.deadline_at and now >= operation.deadline_at:
            cls._mark_failed(
                operation,
                code="prewrite_deadline_exceeded",
                message="Публикация не дошла до Ozon за отведённое время",
                now=now,
            )
            return cls._owned_operation(
                seller_id=operation.seller_id,
                operation_id=operation.id,
            )
        try:
            cls._operation_summary(operation)
            payload = cls._submitted_payload(operation)
        except MarketplacePublicationError as exc:
            cls._mark_failed(
                operation,
                code="publication_snapshot_invalid",
                message=str(exc),
                now=now,
            )
            return cls._owned_operation(
                seller_id=operation.seller_id,
                operation_id=operation.id,
            )
        try:
            preflight_ok = cls._live_preflight(
                operation,
                adapter=adapter,
                credentials=credentials,
                now=now,
            )
        except OzonAPIError as exc:
            cls._defer_prewrite(
                operation,
                code="offer_preflight_unavailable",
                message="Не удалось проверить offer_id в Ozon до публикации",
                now=now,
                request_id=exc.request_id,
            )
            return cls._owned_operation(
                seller_id=operation.seller_id,
                operation_id=operation.id,
            )
        except MarketplacePublicationError:
            cls._defer_prewrite(
                operation,
                code="offer_preflight_contract_invalid",
                message="Ozon вернул неизвестный ответ проверки offer_id",
                now=now,
            )
            return cls._owned_operation(
                seller_id=operation.seller_id,
                operation_id=operation.id,
            )
        except Exception:
            cls._defer_prewrite(
                operation,
                code="offer_preflight_failed",
                message="Не удалось безопасно проверить offer_id до публикации",
                now=now,
            )
            return cls._owned_operation(
                seller_id=operation.seller_id,
                operation_id=operation.id,
            )
        if not preflight_ok:
            return cls._owned_operation(
                seller_id=operation.seller_id,
                operation_id=operation.id,
            )
        if not cls._reserve_quota(
            operation,
            adapter=adapter,
            credentials=credentials,
            now=now,
        ):
            return cls._owned_operation(
                seller_id=operation.seller_id,
                operation_id=operation.id,
            )

        try:
            response = adapter.submit_products(credentials, payload)
        except OzonAmbiguousWriteError as exc:
            cls._mark_uncertain(
                operation,
                code=exc.code,
                message=(
                    "Результат импорта Ozon неизвестен; повтор запрещён до сверки"
                ),
                now=now,
                request_id=exc.request_id,
            )
        except OzonAPIError as exc:
            cls._mark_failed(
                operation,
                code=exc.code,
                message=str(exc),
                now=now,
                request_id=exc.request_id,
            )
        except Exception:
            cls._mark_uncertain(
                operation,
                code="ozon_ambiguous_adapter_failure",
                message=(
                    "Адаптер завершился после начала write; требуется сверка перед повтором"
                ),
                now=now,
            )
        else:
            try:
                submission = OzonProductImportContract.normalize_submission(response)
            except OzonProductImportProtocolError:
                cls._mark_uncertain(
                    operation,
                    code="ozon_ambiguous_submission_response",
                    message=(
                        "Ozon вернул неизвестный ответ после write; требуется сверка"
                    ),
                    now=now,
                )
            else:
                operation.external_task_id = submission["task_id"]
                operation.status = "submitted"
                operation.error_code = None
                operation.error_message = None
                operation.next_poll_at = now + cls.POLL_INTERVAL
                db.session.commit()
        return cls._owned_operation(
            seller_id=operation.seller_id,
            operation_id=operation.id,
        )

    @classmethod
    def start_publication(
        cls,
        *,
        seller_id: int,
        draft_id: int,
        expected_version: int,
        idempotency_key: str,
        created_by_user_id: Optional[int],
        adapter=None,
        credentials: Optional[MarketplaceCredentials] = None,
        now: Optional[datetime] = None,
    ) -> MarketplaceOperation:
        seller_id = cls._positive_integer(seller_id, "seller_id")
        draft_id = cls._positive_integer(draft_id, "draft_id")
        expected_version = cls._positive_integer(
            expected_version,
            "expected_version",
        )
        idempotency_key = cls._idempotency_key(idempotency_key)
        created_by_user_id = cls._validate_author(
            seller_id=seller_id,
            created_by_user_id=created_by_user_id,
        )
        current_time = now or datetime.utcnow()
        existing = cls._existing_idempotent_operation(
            seller_id=seller_id,
            draft_id=draft_id,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
        )
        if existing is not None:
            return existing
        draft = MarketplaceDraftService.get_draft(
            seller_id=seller_id,
            draft_id=draft_id,
        )
        account_id = draft.account_id
        claim = cls._try_claim(account_id)
        if claim is None:
            raise MarketplacePublicationBusy(
                "Для кабинета Ozon уже выполняется публикация или сверка"
            )
        try:
            db.session.expire_all()
            draft, payload, _ = cls._publication_payload(
                seller_id=seller_id,
                draft_id=draft_id,
                expected_version=expected_version,
            )
            _, resolved_adapter, resolved_credentials = (
                cls._account_adapter_credentials(
                    seller_id=seller_id,
                    account_id=account_id,
                    adapter=adapter,
                    credentials=credentials,
                    now=current_time,
                )
            )
            operation = cls._create_operation(
                draft=draft,
                payload=payload,
                idempotency_key=idempotency_key,
                created_by_user_id=created_by_user_id,
                now=current_time,
            )
            if operation.status != "queued":
                return operation
            return cls._submit(
                operation,
                adapter=resolved_adapter,
                credentials=resolved_credentials,
                now=current_time,
            )
        finally:
            cls._release_claim(claim)

    @classmethod
    def _outgoing_to_listing_state(
        cls,
        *,
        payload: dict,
        item_result: dict,
        now: datetime,
        source: str,
    ) -> dict:
        item = payload["items"][0]
        description = None
        attributes = []
        for attribute in item.get("attributes", []):
            normalized = {
                "attribute_id": str(attribute["id"]),
                "complex_id": str(attribute.get("complex_id", 0)),
                "values": [{
                    key: str(value) if key == "dictionary_value_id" else value
                    for key, value in raw_value.items()
                } for raw_value in attribute.get("values", [])],
            }
            attributes.append(normalized)
            if (
                attribute["id"]
                == int(OzonProductImportContract.DESCRIPTION_ATTRIBUTE_ID)
                and attribute.get("values")
            ):
                description = attribute["values"][0].get("value")

        complex_attributes = []
        for group in item.get("complex_attributes", []):
            complex_attributes.append({
                "attributes": [{
                    "attribute_id": str(attribute["id"]),
                    "complex_id": str(attribute.get("complex_id", 0)),
                    "values": [{
                        key: str(value) if key == "dictionary_value_id" else value
                        for key, value in raw_value.items()
                    } for raw_value in attribute.get("values", [])],
                } for attribute in group.get("attributes", [])],
            })
        dimensions = {
            "width": item["width"],
            "height": item["height"],
            "depth": item["depth"],
            "dimension_unit": item["dimension_unit"],
            "weight": item["weight"],
            "weight_unit": item["weight_unit"],
        }
        price_summary = {
            "price": item["price"],
            "currency_code": item["currency_code"],
            "vat": item["vat"],
        }
        if "old_price" in item:
            price_summary["old_price"] = item["old_price"]
        return {
            "offer_id": item["offer_id"],
            "external_product_id": item_result["product_id"],
            "external_category_id": str(item["description_category_id"]),
            "external_type_id": str(item["type_id"]),
            "title": item["name"],
            "description": description,
            "attributes": attributes,
            "complex_attributes": complex_attributes,
            "media": {"images": item.get("images", [])},
            "dimensions": dimensions,
            "barcodes": [item["barcode"]] if item.get("barcode") else [],
            "price_summary": price_summary,
            "status": item_result["status"],
            "source": source,
            "confirmed_at": now.isoformat(),
        }

    @classmethod
    def _finalize_success(
        cls,
        operation: MarketplaceOperation,
        *,
        normalized: dict,
        now: datetime,
        source: str,
    ) -> None:
        if operation.snapshot is None:
            cls._mark_uncertain(
                operation,
                code="publication_snapshot_missing",
                message="Ozon подтвердил импорт, но локальный snapshot отсутствует",
                now=now,
            )
            return
        item_results = normalized["items"]
        if len(item_results) != 1 or not item_results[0].get("product_id"):
            cls._mark_uncertain(
                operation,
                code="ozon_import_product_id_missing",
                message="Ozon подтвердил импорт без product_id; требуется live-сверка",
                now=now,
            )
            return
        item_result = item_results[0]
        payload = cls._submitted_payload(operation)
        state = cls._outgoing_to_listing_state(
            payload=payload,
            item_result=item_result,
            now=now,
            source=source,
        )
        summary = cls._operation_summary(operation)
        imported_product_id = summary.get("imported_product_id")
        product_type_id = summary.get("product_type_id")
        if (
            not isinstance(imported_product_id, int)
            or isinstance(imported_product_id, bool)
            or imported_product_id <= 0
            or ImportedProduct.query.filter_by(
                id=imported_product_id,
                seller_id=operation.seller_id,
            ).first() is None
        ):
            imported_product_id = None
        if (
            not isinstance(product_type_id, int)
            or isinstance(product_type_id, bool)
            or product_type_id <= 0
            or MarketplaceProductType.query.filter_by(
                id=product_type_id,
                marketplace_id=operation.marketplace_id,
            ).first() is None
        ):
            product_type_id = None
        listing = MarketplaceListing.query.filter_by(
            account_id=operation.account_id,
            offer_id=state["offer_id"],
        ).first()
        if listing is None:
            product_conflict = MarketplaceListing.query.filter_by(
                account_id=operation.account_id,
                external_product_id=state["external_product_id"],
            ).first()
            if product_conflict is not None:
                cls._mark_uncertain(
                    operation,
                    code="listing_identity_conflict",
                    message="Ozon product_id уже связан с другим локальным offer_id",
                    now=now,
                )
                return
            listing = MarketplaceListing(
                seller_id=operation.seller_id,
                marketplace_id=operation.marketplace_id,
                account_id=operation.account_id,
                imported_product_id=imported_product_id,
                product_type_id=product_type_id,
                offer_id=state["offer_id"],
                external_product_id=state["external_product_id"],
                normalized_status="moderation",
                provider_status=item_result["status"],
                is_available=True,
                is_archived=False,
                sync_fingerprint=OzonProductImportContract.fingerprint(state),
            )
            db.session.add(listing)
        elif (
            listing.seller_id != operation.seller_id
            or listing.marketplace_id != operation.marketplace_id
            or listing.external_product_id != state["external_product_id"]
        ):
            cls._mark_uncertain(
                operation,
                code="listing_identity_conflict",
                message="Локальная listing identity конфликтует с результатом Ozon",
                now=now,
            )
            return

        listing.imported_product_id = imported_product_id
        listing.product_type_id = product_type_id
        listing.external_category_id = state["external_category_id"]
        listing.external_type_id = state["external_type_id"]
        listing.title = state["title"]
        listing.description = state["description"]
        listing.normalized_status = "moderation"
        listing.provider_status = item_result["status"]
        listing.is_available = True
        listing.is_archived = False
        listing.statuses_json = cls._json({
            "publication": {
                "source": source,
                "status": item_result["status"],
                "confirmed_at": now.isoformat(),
            }
        }, dict)
        listing.moderation_errors_json = cls._json(
            item_result.get("errors", []),
            list,
        )
        listing.attributes_json = cls._json(state["attributes"], list)
        listing.complex_attributes_json = cls._json(
            state["complex_attributes"],
            list,
        )
        listing.media_json = cls._json(state["media"], dict)
        listing.dimensions_json = cls._json(state["dimensions"], dict)
        listing.barcodes_json = cls._json(state["barcodes"], list)
        listing.price_summary_json = cls._json(state["price_summary"], dict)
        listing.stock_summary_json = "{}"
        listing.info_synced_at = now
        listing.attributes_synced_at = now
        listing.prices_synced_at = now
        listing.list_synced_at = now
        listing.last_seen_at = now
        listing.sync_fingerprint = OzonProductImportContract.fingerprint(state)
        db.session.flush()

        draft = operation.draft
        if (
            draft is not None
            and draft.seller_id == operation.seller_id
            and draft.account_id == operation.account_id
            and draft.version == operation.draft_version
        ):
            draft.published_listing_id = listing.id
            draft.status = "published"

        snapshot = operation.snapshot
        snapshot.listing_id = listing.id
        snapshot.confirmed_state_json = cls._json(state, dict)
        snapshot.confirmed_fingerprint = listing.sync_fingerprint
        snapshot.rollback_state_json = cls._json({
            "manual_action": "archive_created_listing_in_ozon",
            "product_id": listing.external_product_id,
            "offer_id": listing.offer_id,
            "expected_listing_fingerprint": listing.sync_fingerprint,
            "reason": "automatic_archive_contract_not_verified",
        }, dict)
        snapshot.rollback_status = "unavailable"
        snapshot.rollback_error_code = "automatic_rollback_contract_unverified"
        snapshot.rollback_error_message = (
            "Автоматический rollback не включён без подтверждённого "
            "официального Ozon archive-контракта"
        )
        operation.listing_id = listing.id
        operation.status = "succeeded"
        operation.item_results_json = cls._json(item_results, list)
        operation.error_code = None
        operation.error_message = None
        operation.quota_reserved = 0
        operation.next_poll_at = None
        operation.completed_at = now
        try:
            db.session.commit()
        except (IntegrityError, StaleDataError):
            db.session.rollback()
            operation = cls._owned_operation(
                seller_id=operation.seller_id,
                operation_id=operation.id,
            )
            cls._mark_uncertain(
                operation,
                code="local_commit_after_import_failed",
                message=(
                    "Ozon подтвердил импорт, но локальная фиксация конфликтует; "
                    "требуется сверка"
                ),
                now=now,
            )

    @classmethod
    def _apply_status(
        cls,
        operation: MarketplaceOperation,
        *,
        normalized: dict,
        now: datetime,
    ) -> None:
        operation.item_results_json = cls._json(normalized["items"], list)
        aggregate = normalized["aggregate_status"]
        if aggregate == "pending":
            if operation.deadline_at and now >= operation.deadline_at:
                operation.status = "uncertain"
                operation.error_code = "ozon_import_task_deadline_exceeded"
                operation.error_message = (
                    "Ozon не завершил import task за отведённое время"
                )
                operation.next_poll_at = None
            else:
                operation.status = "polling"
                operation.error_code = None
                operation.error_message = None
                operation.next_poll_at = now + cls.POLL_INTERVAL
            db.session.commit()
            return
        if aggregate == "succeeded":
            cls._finalize_success(
                operation,
                normalized=normalized,
                now=now,
                source="task_status",
            )
            return

        operation.status = "failed" if aggregate == "failed" else "partial"
        operation.error_code = (
            "ozon_import_failed"
            if aggregate == "failed"
            else "ozon_import_partial"
        )
        operation.error_message = (
            "Ozon отклонил товар; подробности сохранены по offer_id"
            if aggregate == "failed"
            else "Ozon обработал пакет частично"
        )
        operation.quota_reserved = 0
        operation.next_poll_at = None
        operation.completed_at = now
        snapshot = operation.snapshot
        if snapshot is not None:
            snapshot.confirmed_state_json = cls._json({
                "source": "task_status",
                "result": normalized,
                "confirmed_at": now.isoformat(),
            }, dict)
            snapshot.confirmed_fingerprint = OzonProductImportContract.fingerprint(
                normalized
            )
            snapshot.rollback_status = "unavailable"
        db.session.commit()

    @classmethod
    def _defer_task_poll(
        cls,
        operation: MarketplaceOperation,
        *,
        code: str,
        message: str,
        now: datetime,
        delay: timedelta,
        request_id: Optional[str] = None,
    ) -> None:
        if operation.deadline_at and now >= operation.deadline_at:
            operation.status = "uncertain"
            operation.error_code = "ozon_task_poll_deadline_exceeded"
            operation.error_message = (
                "Статус Ozon import task не удалось подтвердить за отведённое "
                "время; автоматический polling остановлен"
            )
            operation.next_poll_at = None
        else:
            operation.status = "polling"
            operation.error_code = cls._safe_text(code, maximum=100)
            operation.error_message = cls._safe_text(message, maximum=1000)
            operation.next_poll_at = now + delay
        cls._append_request_id(operation, request_id)
        db.session.commit()

    @classmethod
    def _poll_task(
        cls,
        operation: MarketplaceOperation,
        *,
        adapter,
        credentials: MarketplaceCredentials,
        now: datetime,
    ) -> None:
        try:
            task_id = int(operation.external_task_id or "")
        except ValueError:
            cls._mark_uncertain(
                operation,
                code="ozon_task_id_invalid",
                message="Сохранённый Ozon task_id повреждён; требуется live-сверка",
                now=now,
            )
            return
        if task_id <= 0 or str(task_id) != operation.external_task_id:
            cls._mark_uncertain(
                operation,
                code="ozon_task_id_invalid",
                message="Сохранённый Ozon task_id повреждён; требуется live-сверка",
                now=now,
            )
            return
        operation.poll_count += 1
        operation.last_polled_at = now
        try:
            response = adapter.get_submission(credentials, {"task_id": task_id})
            summary = cls._operation_summary(operation)
            normalized = OzonProductImportContract.normalize_status(
                response,
                expected_offer_ids=[summary["offer_id"]],
            )
        except OzonAPIError as exc:
            cls._defer_task_poll(
                operation,
                code=exc.code,
                message="Не удалось получить статус задачи Ozon",
                now=now,
                delay=timedelta(
                    seconds=max(15, min(int(exc.retry_after or 60), 600))
                ),
                request_id=exc.request_id,
            )
            return
        except MarketplacePublicationError:
            operation.status = "uncertain"
            operation.error_code = "publication_summary_invalid"
            operation.error_message = "Сохранённое описание операции повреждено"
            operation.next_poll_at = None
            db.session.commit()
            return
        except OzonProductImportProtocolError:
            cls._defer_task_poll(
                operation,
                code="ozon_status_contract_invalid",
                message="Ozon вернул неизвестный формат статуса задачи",
                now=now,
                delay=cls.RECONCILE_INTERVAL,
            )
            return
        except Exception:
            cls._defer_task_poll(
                operation,
                code="ozon_status_poll_failed",
                message="Не удалось безопасно получить статус задачи Ozon",
                now=now,
                delay=cls.RECONCILE_INTERVAL,
            )
            return
        cls._apply_status(operation, normalized=normalized, now=now)

    @classmethod
    def _reconcile_live(
        cls,
        operation: MarketplaceOperation,
        *,
        adapter,
        credentials: MarketplaceCredentials,
        now: datetime,
    ) -> None:
        operation.reconcile_count += 1
        operation.last_polled_at = now
        if operation.snapshot is None:
            operation.status = "uncertain"
            operation.error_code = "publication_snapshot_missing"
            operation.error_message = (
                "Нельзя выполнить live-сверку без before-write snapshot"
            )
            operation.next_poll_at = None
            db.session.commit()
            return
        try:
            before_state = json.loads(operation.snapshot.before_state_json)
        except (TypeError, json.JSONDecodeError):
            before_state = {}
        if not isinstance(before_state, dict) or before_state.get("exists") is not False:
            cls._mark_uncertain(
                operation,
                code="reconciliation_before_state_unproven",
                message=(
                    "Нельзя доказать отсутствие offer_id до write; ручной повтор запрещён"
                ),
                now=now,
            )
            return
        try:
            summary = cls._operation_summary(operation)
        except MarketplacePublicationError:
            operation.status = "uncertain"
            operation.error_code = "publication_summary_invalid"
            operation.error_message = "Сохранённое описание операции повреждено"
            operation.next_poll_at = None
            db.session.commit()
            return
        try:
            found = cls._offer_lookup(
                adapter=adapter,
                credentials=credentials,
                offer_id=summary["offer_id"],
            )
        except (OzonAPIError, MarketplacePublicationError):
            found = None
        except Exception:
            found = None
        if found:
            item = found[0]
            normalized = {
                "total": 1,
                "aggregate_status": "succeeded",
                "items": [{
                    "offer_id": summary["offer_id"],
                    "product_id": item["product_id"],
                    "status": "imported",
                    "errors": [],
                    "reconciled_without_task_id": True,
                }],
            }
            cls._finalize_success(
                operation,
                normalized=normalized,
                now=now,
                source="live_offer_reconciliation",
            )
            return

        if operation.deadline_at and now >= operation.deadline_at:
            operation.status = "uncertain"
            operation.error_code = "reconciliation_deadline_exceeded"
            operation.error_message = (
                "Ozon не подтвердил и не опроверг write; автоматический повтор запрещён"
            )
            operation.next_poll_at = None
            db.session.commit()
            return
        operation.status = "uncertain"
        operation.error_code = "reconciliation_pending"
        operation.error_message = (
            "offer_id пока не найден; write не повторяется, сверка будет продолжена"
        )
        operation.next_poll_at = now + cls.RECONCILE_INTERVAL
        db.session.commit()

    @classmethod
    def poll_operation(
        cls,
        *,
        seller_id: int,
        operation_id: int,
        adapter=None,
        credentials: Optional[MarketplaceCredentials] = None,
        now: Optional[datetime] = None,
        allow_submission: bool = True,
    ) -> MarketplaceOperation:
        if not isinstance(allow_submission, bool):
            raise MarketplacePublicationValidationError(
                "allow_submission должен быть boolean"
            )
        current_time = now or datetime.utcnow()
        operation = cls._owned_operation(
            seller_id=seller_id,
            operation_id=operation_id,
        )
        if operation.operation_kind not in {
            "product_import",
            "product_import_rollback",
        }:
            raise MarketplacePublicationValidationError(
                "Commercial operation должна обрабатываться commercial service"
            )
        if operation.status in cls.TERMINAL_STATUSES:
            return operation
        account_id = operation.account_id
        claim = cls._try_claim(account_id)
        if claim is None:
            raise MarketplacePublicationBusy(
                "Для кабинета Ozon уже выполняется публикация или сверка"
            )
        try:
            db.session.expire_all()
            operation = cls._owned_operation(
                seller_id=seller_id,
                operation_id=operation_id,
            )
            if operation.status in cls.TERMINAL_STATUSES:
                return operation
            if operation.status == "queued" and not allow_submission:
                return operation
            _, resolved_adapter, resolved_credentials = (
                cls._account_adapter_credentials(
                    seller_id=seller_id,
                    account_id=account_id,
                    adapter=adapter,
                    credentials=credentials,
                    now=current_time,
                )
            )
            if operation.status == "queued" and allow_submission:
                cls._submit(
                    operation,
                    adapter=resolved_adapter,
                    credentials=resolved_credentials,
                    now=current_time,
                )
            elif operation.external_task_id and operation.status in {
                "submitted",
                "polling",
                "uncertain",
            }:
                cls._poll_task(
                    operation,
                    adapter=resolved_adapter,
                    credentials=resolved_credentials,
                    now=current_time,
                )
            else:
                cls._reconcile_live(
                    operation,
                    adapter=resolved_adapter,
                    credentials=resolved_credentials,
                    now=current_time,
                )
            return cls._owned_operation(
                seller_id=seller_id,
                operation_id=operation_id,
            )
        finally:
            cls._release_claim(claim)

    @classmethod
    def poll_due_operations(
        cls,
        *,
        limit: int = 20,
        now: Optional[datetime] = None,
        allow_submission: bool = True,
    ) -> dict:
        if not isinstance(allow_submission, bool):
            raise MarketplacePublicationValidationError(
                "allow_submission должен быть boolean"
            )
        limit = cls._positive_integer(limit, "limit")
        if limit > cls.MAX_DUE_OPERATIONS:
            raise MarketplacePublicationValidationError(
                f"limit не может быть больше {cls.MAX_DUE_OPERATIONS}"
            )
        current_time = now or datetime.utcnow()
        due_statuses = {
            "submitting",
            "submitted",
            "polling",
            "uncertain",
        }
        if allow_submission:
            due_statuses.add("queued")
        operations = MarketplaceOperation.query.join(Marketplace).filter(
            Marketplace.code == "ozon",
            MarketplaceOperation.operation_kind.in_((
                "product_import",
                "product_import_rollback",
            )),
            MarketplaceOperation.status.in_(due_statuses),
            MarketplaceOperation.next_poll_at.isnot(None),
            MarketplaceOperation.next_poll_at <= current_time,
        ).order_by(
            MarketplaceOperation.next_poll_at.asc(),
            MarketplaceOperation.id.asc(),
        ).limit(limit).all()
        result = {
            "selected": len(operations),
            "processed": 0,
            "busy": 0,
            "failed": 0,
        }
        for selected in operations:
            try:
                cls.poll_operation(
                    seller_id=selected.seller_id,
                    operation_id=selected.id,
                    now=current_time,
                    allow_submission=allow_submission,
                )
                result["processed"] += 1
            except MarketplacePublicationBusy:
                db.session.rollback()
                result["busy"] += 1
            except Exception:
                db.session.rollback()
                result["failed"] += 1
        return result
