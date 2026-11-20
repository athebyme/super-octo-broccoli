"""Reviewed, durable Ozon price and FBS stock mutation workflow."""

from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import json
import re
import secrets
from typing import Any, Dict, Mapping, Optional, Tuple
import uuid

from flask import current_app
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload
from sqlalchemy.orm.exc import StaleDataError

from models import (
    AgentTask,
    Marketplace,
    MarketplaceCommercialProposal,
    MarketplaceListing,
    MarketplaceListingSnapshot,
    MarketplaceOperation,
    MarketplaceWarehouse,
    MarketplaceWarehouseStock,
    PricingSettings,
    Seller,
    SellerMarketplaceAccount,
    db,
)
from services.marketplace_adapters import MarketplaceCredentials
from services.marketplace_operation_locks import (
    release_account_operation_lock,
    try_account_operation_lock,
)
from services.marketplace_operation_retry import provider_read_deferred, read_retry_at
from services.marketplace_warehouses import (
    MarketplaceWarehouseError,
    MarketplaceWarehouseService,
)
from services.ozon_api_client import (
    OzonAPIError,
    OzonAmbiguousWriteError,
)
from services.ozon_commercial_contracts import (
    OzonCommercialContractError,
    OzonCommercialProtocolError,
    OzonPriceContract,
    OzonStockContract,
)
from services import ozon_write_quarantine as write_quarantine


class MarketplaceCommercialError(RuntimeError):
    status_code = 400
    code = "marketplace_commercial_error"


class MarketplaceCommercialValidationError(MarketplaceCommercialError):
    status_code = 400
    code = "invalid_marketplace_commercial_request"


class MarketplaceCommercialNotFound(MarketplaceCommercialError):
    status_code = 404
    code = "marketplace_commercial_not_found"


class MarketplaceCommercialConflict(MarketplaceCommercialError):
    status_code = 409
    code = "marketplace_commercial_conflict"


class QuarantinedCommercialConflict(MarketplaceCommercialConflict):
    code = "ozon_write_quarantined"

    def __init__(self, hold):
        super().__init__(write_quarantine.hold_message(hold))
        self.write_quarantine = write_quarantine.hold_document(hold)


class MarketplaceCommercialBusy(MarketplaceCommercialError):
    status_code = 409
    code = "marketplace_commercial_busy"


class MarketplaceCommercialConfigurationError(MarketplaceCommercialError):
    status_code = 503
    code = "marketplace_commercial_configuration_error"


class MarketplaceCommercialUpstreamError(MarketplaceCommercialError):
    status_code = 502
    code = "marketplace_commercial_upstream_error"

    def __init__(self, message, *, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


class MarketplaceCommercialService:
    MAX_JSON_BYTES = 65_536
    MAX_PRICE_READ_PAGES = 10
    MAX_PRICE_CHANGE_PCT = Decimal("50")
    MAX_BATCH_APPROVAL = 100
    BULK_PRICE_PREVIEW_MAX_AGE = 600
    BULK_PRICE_PREVIEW_MAX_TOKEN_BYTES = 131_072
    BULK_PRICE_PREVIEW_SALT = "marketplace-commercial-bulk-price-preview-v1"

    @staticmethod
    def target_unavailable_reason(proposal) -> Optional[str]:
        """Negative local gate for NEW writes, never for attempted reconciliation."""
        listing = proposal.listing
        if listing is None or not listing.is_available or listing.is_archived:
            return "Товар недоступен или находится в архиве. Обновите каталог перед новым изменением."
        if proposal.proposal_kind == "stock" and (
            proposal.warehouse is None or not proposal.warehouse.is_available
        ):
            return "Склад больше недоступен. Обновите список складов и подготовьте новую заявку."
        if proposal.proposal_kind == "price":
            try:
                proposed = json.loads(proposal.proposed_state_json or "{}")
                OzonPriceContract.whole_rub_price(proposed.get("price"))
            except (ValueError, AttributeError, OzonCommercialContractError):
                return (
                    "В этой заявке нет подтверждённой цены в целых рублях. "
                    "Закройте её без отправки и подготовьте новое сравнение."
                )
        return None

    @classmethod
    def _require_available_target(cls, proposal) -> None:
        reason = cls.target_unavailable_reason(proposal)
        if reason:
            raise MarketplaceCommercialConflict(reason)

    @staticmethod
    def _require_not_quarantined(proposals) -> None:
        """Early approval check under the account lock, before any provider read."""
        for proposal in proposals:
            hold = write_quarantine.proposal_hold(proposal)
            if hold is not None:
                raise QuarantinedCommercialConflict(hold)

    @classmethod
    def _stop_quarantined_batch(cls, operations, proposals, *, now):
        """A frozen batch is indivisible: one held member stops every write."""
        hold = None
        for operation in operations:
            if operation.attempt_count == 0:
                hold = write_quarantine.operation_hold(operation)
                if hold is not None:
                    break
        if hold is None:
            return False
        message = write_quarantine.hold_message(hold)
        for operation, proposal in zip(operations, proposals):
            cls._set_write_failed(
                operation=operation, proposal=proposal,
                code="ozon_write_quarantined", message=message, now=now,
            )
        db.session.commit()
        return True
    POLL_INTERVAL = timedelta(seconds=30)
    DEADLINE = timedelta(hours=24)
    ACTIVE_PROPOSAL_STATUSES = {
        "pending_review",
        "approved",
        "applying",
        "uncertain",
    }
    COMMERCIAL_OPERATION_KINDS = {
        "price_update",
        "stock_update",
        "price_rollback",
        "stock_rollback",
    }
    TERMINAL_OPERATION_STATUSES = {
        "succeeded",
        "partial",
        "failed",
        "cancelled",
    }
    _IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9_.:-]{8,128}$")

    @staticmethod
    def _positive_integer(value: Any, field_name: str) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise MarketplaceCommercialValidationError(
                f"{field_name} должен быть положительным целым числом"
            )
        return value

    @classmethod
    def _bulk_price_listing_ids(cls, value: Any) -> list[int]:
        if not isinstance(value, list) or not 1 <= len(value) <= cls.MAX_BATCH_APPROVAL:
            raise MarketplaceCommercialValidationError(
                f"listing_ids должен содержать 1..{cls.MAX_BATCH_APPROVAL} листингов"
            )
        ids = [
            cls._positive_integer(item, f"listing_ids[{index}]")
            for index, item in enumerate(value)
        ]
        if len(set(ids)) != len(ids):
            raise MarketplaceCommercialValidationError(
                "listing_ids содержит повторяющиеся листинги"
            )
        return ids

    @classmethod
    def _bulk_price_mode_value(cls, mode: Any, value: Any) -> tuple[str, str]:
        if not isinstance(mode, str) or mode not in {"set", "percent"}:
            raise MarketplaceCommercialValidationError(
                "mode должен быть set или percent"
            )
        if isinstance(value, bool) or isinstance(value, float) or not isinstance(
            value, (str, int)
        ):
            raise MarketplaceCommercialValidationError(
                "value передавайте decimal-строкой или целым числом"
            )
        rendered = str(value)
        pattern = r"\d+(?:\.\d{1,2})?" if mode == "set" else r"[+-]?\d+(?:\.\d{1,2})?"
        if len(rendered) > 32 or not re.fullmatch(pattern, rendered):
            raise MarketplaceCommercialValidationError(
                "value должен быть decimal-числом с точностью до сотых"
            )
        if mode == "set":
            try:
                return mode, OzonPriceContract.money(rendered, "value")
            except OzonCommercialContractError as exc:
                raise MarketplaceCommercialValidationError(str(exc)) from None

        try:
            percent = Decimal(rendered)
        except (InvalidOperation, TypeError, ValueError):
            raise MarketplaceCommercialValidationError(
                "Процент должен быть decimal-строкой"
            ) from None
        if (
            not percent.is_finite()
            or percent.as_tuple().exponent < -2
            or percent < Decimal("-99.99")
            or percent > Decimal("500")
        ):
            raise MarketplaceCommercialValidationError(
                "Процент должен быть от -99.99 до 500 с точностью до сотых"
            )
        return mode, format(percent, "f")

    @classmethod
    def _bulk_price_review(
        cls,
        *,
        mode: str,
        value: str,
        baseline_price: str,
    ) -> dict:
        try:
            if mode == "set":
                return OzonPriceContract.prepare_price(value)
            percentage = Decimal(value)
            calculated = Decimal(baseline_price) * (
                Decimal("1") + percentage / Decimal("100")
            )
            requested = calculated.quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            review = OzonPriceContract.prepare_price(format(requested, "f"))
            review["calculation"] = {
                "mode": "percent",
                "change_pct": value,
            }
            return review
        except OzonCommercialContractError as exc:
            raise MarketplaceCommercialValidationError(str(exc)) from None

    @classmethod
    def _price_proposal_details(
        cls,
        *,
        listing: MarketplaceListing,
        baseline: dict,
        price_review: dict,
        source: str,
        allow_price_decrease: bool,
        allow_large_change: bool,
        guardrail_note: Optional[Any],
        pricing_min_profit: Optional[Decimal] = None,
    ) -> tuple[dict, dict]:
        item = OzonPriceContract.build_item(
            offer_id=listing.offer_id,
            product_id=listing.external_product_id,
            price=price_review["price"],
            currency_code=baseline["currency_code"],
            old_price=baseline.get("old_price", "0"),
        )
        guardrails = cls._guard_price_change(
            listing=listing,
            baseline=baseline,
            proposed_price=item["price"],
            source=source,
            allow_price_decrease=allow_price_decrease,
            allow_large_change=allow_large_change,
            guardrail_note=guardrail_note,
            pricing_min_profit=pricing_min_profit,
        )
        guardrails["price_review"] = dict(price_review)
        proposed = {
            "kind": "price",
            "offer_id": listing.offer_id,
            "product_id": listing.external_product_id,
            "price": item["price"],
            "old_price": item.get("old_price", "0"),
            "min_price": baseline.get("min_price", "0"),
            "currency_code": item["currency_code"],
            "auto_action_enabled": baseline.get("auto_action_enabled"),
            "auto_add_to_ozon_actions_list_enabled": baseline.get(
                "auto_add_to_ozon_actions_list_enabled"
            ),
        }
        return proposed, guardrails

    @classmethod
    def _bulk_price_scope(
        cls,
        *,
        seller_id: int,
        account_id: int,
        listing_ids: list[int],
        require_available: bool,
        include_imported: bool = True,
    ) -> tuple[SellerMarketplaceAccount, list[MarketplaceListing]]:
        account = cls._bulk_price_account(
            seller_id=seller_id,
            account_id=account_id,
        )
        query = MarketplaceListing.query
        if include_imported:
            query = query.options(joinedload(MarketplaceListing.imported_product))
        rows = query.join(Marketplace).filter(
            MarketplaceListing.id.in_(listing_ids),
            MarketplaceListing.seller_id == seller_id,
            MarketplaceListing.account_id == account_id,
            MarketplaceListing.marketplace_id == account.marketplace_id,
            Marketplace.code == "ozon",
        ).populate_existing().all()
        by_id = {row.id: row for row in rows}
        if len(by_id) != len(listing_ids):
            raise MarketplaceCommercialNotFound(
                "Один или несколько листингов Ozon не найдены в выбранном магазине"
            )
        listings = [by_id[listing_id] for listing_id in listing_ids]
        if require_available and any(
            row.is_archived or not row.is_available for row in listings
        ):
            raise MarketplaceCommercialConflict(
                "Массовая подготовка доступна только для доступных неархивных листингов"
            )
        return account, listings

    @staticmethod
    def _bulk_price_account(
        *,
        seller_id: int,
        account_id: int,
    ) -> SellerMarketplaceAccount:
        account = SellerMarketplaceAccount.query.join(Marketplace).filter(
            SellerMarketplaceAccount.id == account_id,
            SellerMarketplaceAccount.seller_id == seller_id,
            SellerMarketplaceAccount.marketplace_id == Marketplace.id,
            Marketplace.code == "ozon",
        ).populate_existing().first()
        if account is None:
            raise MarketplaceCommercialNotFound("Магазин Ozon не найден")
        return account

    @staticmethod
    def _begin_bulk_price_validation_transaction() -> None:
        """Lock SQLite writers before the final local revalidation/save."""
        if db.engine.dialect.name != "sqlite":
            raise MarketplaceCommercialConfigurationError(
                "Атомарное сохранение массовых цен поддерживается только на SQLite"
            )
        session = db.session()
        if session.in_transaction():
            session.rollback()
        try:
            connection = session.connection()
            raw_connection = connection.connection.driver_connection
            if getattr(raw_connection, "in_transaction", False):
                raise RuntimeError("bulk_price_transaction_not_fresh")
            previous_timeout = raw_connection.execute(
                "PRAGMA busy_timeout"
            ).fetchone()[0]
            raw_connection.execute("PRAGMA busy_timeout = 200")
            try:
                connection.exec_driver_sql("BEGIN IMMEDIATE")
            finally:
                raw_connection.execute(
                    f"PRAGMA busy_timeout = {int(previous_timeout)}"
                )
        except Exception:
            session.rollback()
            raise MarketplaceCommercialBusy(
                "База занята. Повторите сохранение после завершения другой операции."
            ) from None

    @staticmethod
    def _timestamp(value) -> Optional[str]:
        return value.isoformat() if value is not None else None

    @classmethod
    def _bulk_listing_version(cls, listing: MarketplaceListing) -> dict:
        return {
            "listing_id": listing.id,
            "offer_id": listing.offer_id,
            "product_id": listing.external_product_id,
            "sync_fingerprint": listing.sync_fingerprint,
            "title": cls._safe_text(listing.title, maximum=500),
            "updated_at": cls._timestamp(listing.updated_at),
            "prices_synced_at": cls._timestamp(listing.prices_synced_at),
            "is_available": bool(listing.is_available),
            "is_archived": bool(listing.is_archived),
            "imported_product_id": listing.imported_product_id,
            "link_version": listing.link_version,
        }

    @classmethod
    def _bulk_price_policy(
        cls,
        listing: MarketplaceListing,
        *,
        pricing_min_profit: Decimal,
    ) -> dict:
        imported = listing.imported_product
        return {
            "imported_product_id": imported.id if imported is not None else None,
            "supplier_price": (
                str(imported.supplier_price)
                if imported is not None and imported.supplier_price is not None
                else None
            ),
            "pricing_min_profit": format(pricing_min_profit, "f"),
        }

    @classmethod
    def _bulk_pricing_min_profit(cls, seller_id: int) -> Decimal:
        pricing = PricingSettings.query.filter_by(seller_id=seller_id).first()
        raw = pricing.min_profit if pricing is not None else None
        try:
            result = Decimal(str(raw if raw is not None else 0))
        except (InvalidOperation, TypeError, ValueError):
            raise MarketplaceCommercialConflict(
                "Настройки минимальной прибыли требуют проверки"
            ) from None
        if not result.is_finite():
            raise MarketplaceCommercialConflict(
                "Настройки минимальной прибыли требуют проверки"
            )
        return result

    @classmethod
    def _bulk_price_row(
        cls,
        *,
        listing: MarketplaceListing,
        baseline: dict,
        mode: str,
        value: str,
        active_proposal: bool,
        allow_price_decrease: bool,
        allow_large_change: bool,
        guardrail_note: Optional[str],
        pricing_min_profit: Decimal,
    ) -> dict:
        current_price = baseline["price"]
        try:
            price_review = cls._bulk_price_review(
                mode=mode, value=value, baseline_price=current_price
            )
        except MarketplaceCommercialValidationError as exc:
            return {
                "status": "blocked",
                "reason_code": "price_out_of_range",
                "reason": str(exc),
                "price_review": None,
                "proposed": None,
                "guardrails": {},
            }

        proposed_price = price_review["price"]
        if active_proposal:
            return {
                "status": "blocked",
                "reason_code": "active_price_proposal",
                "reason": "Для товара уже есть незавершённая заявка на изменение цены.",
                "price_review": price_review,
                "proposed": None,
                "guardrails": {},
            }
        if Decimal(proposed_price) == Decimal(current_price):
            return {
                "status": "noop",
                "reason_code": "price_unchanged",
                "reason": "Цена после округления совпадает с текущей.",
                "price_review": price_review,
                "proposed": None,
                "guardrails": {},
            }

        try:
            proposed, guardrails = cls._price_proposal_details(
                listing=listing,
                baseline=baseline,
                price_review=price_review,
                source="user",
                allow_price_decrease=allow_price_decrease,
                allow_large_change=allow_large_change,
                guardrail_note=guardrail_note,
                pricing_min_profit=pricing_min_profit,
            )
        except MarketplaceCommercialValidationError as exc:
            message = str(exc)
            reason_code = (
                "price_decrease_requires_review"
                if message.startswith("Снижение цены требует")
                else "large_change_requires_review"
                if message.startswith("Изменение цены больше 50%")
                else "override_note_required"
                if message.startswith("Для price override")
                else "old_price_limit"
                if message.startswith("Новая цена должна оставаться ниже")
                else "min_price_limit"
                if message.startswith("Новая цена ниже текущей min_price")
                else "supplier_floor"
                if message.startswith("Новая цена ниже закупочной")
                else "guardrail_blocked"
            )
            return {
                "status": "blocked",
                "reason_code": reason_code,
                "reason": message,
                "price_review": price_review,
                "proposed": None,
                "guardrails": {},
            }
        except OzonCommercialContractError:
            return {
                "status": "blocked",
                "reason_code": "unsupported_currency_or_price",
                "reason": "Эту цену нельзя подготовить для текущей валюты или значения.",
                "price_review": price_review,
                "proposed": None,
                "guardrails": {},
            }
        return {
            "status": "eligible",
            "reason_code": None,
            "reason": None,
            "price_review": price_review,
            "proposed": proposed,
            "guardrails": guardrails,
        }

    @classmethod
    def _bulk_price_serializer(cls):
        secret = current_app.config.get("SECRET_KEY")
        if not secret:
            raise MarketplaceCommercialConfigurationError(
                "Не настроена подпись временного сравнения цены"
            )
        return URLSafeTimedSerializer(
            secret,
            salt=cls.BULK_PRICE_PREVIEW_SALT,
        )

    @classmethod
    def _bulk_account_version(cls, account: SellerMarketplaceAccount) -> dict:
        return {
            "id": account.id,
            "seller_id": account.seller_id,
            "marketplace_id": account.marketplace_id,
            "version": account.version,
            "credential_version": account.credential_version,
            "external_account_fingerprint": hashlib.sha256(
                str(account.external_account_id or "").encode("utf-8")
            ).hexdigest(),
            "credentials_updated_at": cls._timestamp(account.credentials_updated_at),
            "credential_expires_at": cls._timestamp(account.credential_expires_at),
            "is_active": bool(account.is_active),
            "connection_status": account.connection_status,
        }

    @classmethod
    def _bulk_claims(cls, token: Any, *, seller_id: int, user_id: int) -> dict:
        if not isinstance(token, str) or not token:
            raise MarketplaceCommercialValidationError(
                "preview_token обязателен"
            )
        try:
            token_size = len(token.encode("utf-8"))
        except UnicodeEncodeError:
            raise MarketplaceCommercialValidationError(
                "preview_token имеет неверный формат"
            ) from None
        if token_size > cls.BULK_PRICE_PREVIEW_MAX_TOKEN_BYTES:
            raise MarketplaceCommercialValidationError(
                "preview_token превышает безопасный размер"
            )
        try:
            claims = cls._bulk_price_serializer().loads(
                token,
                max_age=cls.BULK_PRICE_PREVIEW_MAX_AGE,
            )
        except SignatureExpired:
            raise MarketplaceCommercialConflict(
                "Срок сравнения истёк. Проведите новое сравнение цен."
            ) from None
        except BadSignature:
            raise MarketplaceCommercialValidationError(
                "Подпись сравнения недействительна"
            ) from None
        expected_keys = {
            "version", "seller_id", "user_id", "account_id", "account",
            "listing_ids", "mode", "value", "allow_price_decrease",
            "allow_large_change", "guardrail_note", "nonce", "items",
        }
        if not isinstance(claims, dict) or set(claims) != expected_keys:
            raise MarketplaceCommercialValidationError(
                "Формат сравнения не распознан"
            )
        if (
            type(claims.get("version")) is not int
            or claims["version"] != 1
            or type(claims.get("seller_id")) is not int
            or claims["seller_id"] != seller_id
            or type(claims.get("user_id")) is not int
            or claims["user_id"] != user_id
        ):
            raise MarketplaceCommercialNotFound(
                "Сравнение цены не принадлежит текущему продавцу и пользователю"
            )
        account_id = cls._positive_integer(claims.get("account_id"), "account_id")
        listing_ids = cls._bulk_price_listing_ids(claims.get("listing_ids"))
        account = claims.get("account")
        if (
            not isinstance(account, dict)
            or set(account) != {
                "id", "seller_id", "marketplace_id", "version",
                "credential_version", "external_account_fingerprint",
                "credentials_updated_at", "credential_expires_at",
                "is_active", "connection_status",
            }
            or type(account.get("id")) is not int
            or account["id"] != account_id
            or type(account.get("seller_id")) is not int
            or account["seller_id"] != seller_id
            or type(account.get("marketplace_id")) is not int
            or type(account.get("version")) is not int
            or type(account.get("credential_version")) is not int
            or not isinstance(account.get("external_account_fingerprint"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", account["external_account_fingerprint"])
            or (
                account.get("credentials_updated_at") is not None
                and not isinstance(account.get("credentials_updated_at"), str)
            )
            or (
                account.get("credential_expires_at") is not None
                and not isinstance(account.get("credential_expires_at"), str)
            )
            or not isinstance(account.get("is_active"), bool)
            or not isinstance(account.get("connection_status"), str)
        ):
            raise MarketplaceCommercialValidationError("Сравнение повреждено")
        mode, value = cls._bulk_price_mode_value(
            claims.get("mode"), claims.get("value")
        )
        if value != claims.get("value"):
            raise MarketplaceCommercialValidationError("Сравнение повреждено")
        if not isinstance(claims.get("allow_price_decrease"), bool) or not isinstance(
            claims.get("allow_large_change"), bool
        ):
            raise MarketplaceCommercialValidationError("Сравнение повреждено")
        note = claims.get("guardrail_note")
        if note is not None and not isinstance(note, str):
            raise MarketplaceCommercialValidationError("Сравнение повреждено")
        if not isinstance(claims.get("nonce"), str) or not re.fullmatch(
            r"[0-9a-f]{32}", claims["nonce"]
        ):
            raise MarketplaceCommercialValidationError("Сравнение повреждено")
        items = claims.get("items")
        if not isinstance(items, list) or len(items) != len(listing_ids):
            raise MarketplaceCommercialValidationError("Сравнение повреждено")
        expected_item_keys = {
            "listing_id", "listing_version", "policy", "baseline",
            "active_proposal", "status", "reason_code", "reason",
            "price_review", "proposed", "guardrails",
        }
        expected_listing_version_keys = {
            "listing_id", "offer_id", "product_id", "sync_fingerprint",
            "title", "updated_at", "prices_synced_at", "is_available",
            "is_archived", "imported_product_id", "link_version",
        }
        expected_policy_keys = {
            "imported_product_id", "supplier_price", "pricing_min_profit",
        }
        for index, item in enumerate(items):
            if (
                not isinstance(item, dict)
                or set(item) != expected_item_keys
                or item.get("listing_id") != listing_ids[index]
                or not isinstance(item.get("active_proposal"), bool)
                or item.get("status") not in ("eligible", "noop", "blocked")
                or not isinstance(item.get("listing_version"), dict)
                or set(item["listing_version"]) != expected_listing_version_keys
                or item["listing_version"].get("listing_id") != listing_ids[index]
                or not isinstance(item.get("policy"), dict)
                or set(item["policy"]) != expected_policy_keys
                or not isinstance(item.get("baseline"), dict)
                or (
                    item.get("price_review") is not None
                    and not isinstance(item.get("price_review"), dict)
                )
                or not isinstance(item.get("guardrails"), dict)
                or (item.get("proposed") is not None and not isinstance(item.get("proposed"), dict))
                or (item.get("reason_code") is not None and not isinstance(item.get("reason_code"), str))
                or (item.get("reason") is not None and not isinstance(item.get("reason"), str))
                or (
                    item.get("status") == "eligible"
                    and (item.get("proposed") is None or item.get("price_review") is None)
                )
                or (
                    item.get("status") != "eligible"
                    and item.get("proposed") is not None
                )
                or (
                    item.get("price_review") is None
                    and item.get("reason_code") != "price_out_of_range"
                )
            ):
                raise MarketplaceCommercialValidationError("Сравнение повреждено")
        if account_id <= 0:
            raise MarketplaceCommercialValidationError("Сравнение повреждено")
        return claims

    @classmethod
    def _bulk_expected_proposals(
        cls,
        *,
        claims: dict,
        seller_id: int,
        user_id: int,
    ) -> tuple[list[dict], dict[str, str]]:
        eligible = [item for item in claims["items"] if item["status"] == "eligible"]
        keys = {
            f"bulk-price:{claims['nonce']}:{item['listing_id']}": cls._proposal_request_fingerprint(
                proposal_kind="price",
                listing_id=item["listing_id"],
                warehouse_id=None,
                baseline=item["baseline"],
                proposed=item["proposed"],
                source="user",
            )
            for item in eligible
        }
        if len(keys) != len(eligible) or any(len(key) > 128 for key in keys):
            raise MarketplaceCommercialValidationError("Сравнение повреждено")
        return eligible, keys

    @classmethod
    def _bulk_replay_items(
        cls,
        *,
        claims: dict,
        seller_id: int,
        user_id: int,
        eligible: list[dict],
        fingerprints: dict[str, str],
    ) -> Optional[list[dict]]:
        if not eligible:
            return None
        found = MarketplaceCommercialProposal.query.filter(
            MarketplaceCommercialProposal.seller_id == seller_id,
            MarketplaceCommercialProposal.account_id == claims["account_id"],
            MarketplaceCommercialProposal.proposal_kind == "price",
            MarketplaceCommercialProposal.idempotency_key.in_(list(fingerprints)),
        ).options(
            joinedload(MarketplaceCommercialProposal.listing),
        ).all()
        if not found:
            return None
        by_key = {proposal.idempotency_key: proposal for proposal in found}
        if len(by_key) != len(eligible) or set(by_key) != set(fingerprints):
            raise MarketplaceCommercialConflict(
                "Сохранён только неполный набор массовых заявок. Проверьте список заявок перед новым сравнением."
            )
        proposals = []
        for item in eligible:
            key = f"bulk-price:{claims['nonce']}:{item['listing_id']}"
            proposal = by_key.get(key)
            if (
                proposal is None
                or proposal.listing_id != item["listing_id"]
                or proposal.warehouse_id is not None
                or proposal.created_by_user_id != user_id
                or proposal.source != "user"
                or proposal.contract_version != OzonPriceContract.CONTRACT_VERSION
                or proposal.request_fingerprint != fingerprints[key]
                or proposal.marketplace_id != claims["account"].get("marketplace_id")
                or proposal.listing is None
                or proposal.listing.seller_id != seller_id
            ):
                raise MarketplaceCommercialConflict(
                    "Подтверждение уже использовано для другого набора заявок"
                )
            proposals.append({
                "listing_id": item["listing_id"],
                "status": proposal.status,
                "proposal": proposal,
            })
        return proposals

    @classmethod
    def preview_bulk_price_proposals(
        cls,
        *,
        seller_id: int,
        account_id: int,
        listing_ids: Any,
        mode: Any,
        value: Any,
        created_by_user_id: Optional[Any],
        allow_price_decrease: bool = False,
        allow_large_change: bool = False,
        guardrail_note: Optional[Any] = None,
        adapter=None,
        credentials: Optional[MarketplaceCredentials] = None,
        now: Optional[datetime] = None,
    ) -> dict:
        seller_id = cls._positive_integer(seller_id, "seller_id")
        account_id = cls._positive_integer(account_id, "account_id")
        user_id = cls._validate_user(
            seller_id=seller_id,
            user_id=created_by_user_id,
        )
        if user_id is None:
            raise MarketplaceCommercialValidationError(
                "Массовое сравнение требует авторизованного пользователя"
            )
        listing_ids = cls._bulk_price_listing_ids(listing_ids)
        mode, value = cls._bulk_price_mode_value(mode, value)
        if not isinstance(allow_price_decrease, bool) or not isinstance(
            allow_large_change, bool
        ):
            raise MarketplaceCommercialValidationError(
                "Price override flags должны быть boolean"
            )
        if guardrail_note is not None and not isinstance(guardrail_note, str):
            raise MarketplaceCommercialValidationError(
                "guardrail_note должен быть строкой"
            )
        guardrail_note = (
            cls._safe_text(guardrail_note, maximum=1000)
            if guardrail_note is not None else None
        )

        # The ownership/account/availability gate precedes credentials and every
        # provider read, so a mixed or archived set cannot touch Ozon.
        cls._bulk_price_scope(
            seller_id=seller_id,
            account_id=account_id,
            listing_ids=listing_ids,
            require_available=True,
        )
        resolved_account, adapter, credentials = cls._resolve_account(
            seller_id=seller_id,
            account_id=account_id,
            proposal_kind="price",
            adapter=adapter,
            credentials=credentials,
            now=now,
        )
        resolved_account_version = cls._bulk_account_version(resolved_account)
        db.session.rollback()
        lock_file = try_account_operation_lock(account_id)
        if lock_file is None:
            raise MarketplaceCommercialBusy("Кабинет Ozon занят другой операцией")
        try:
            account, listings = cls._bulk_price_scope(
                seller_id=seller_id,
                account_id=account_id,
                listing_ids=listing_ids,
                require_available=True,
            )
            if cls._bulk_account_version(account) != resolved_account_version:
                raise MarketplaceCommercialConflict(
                    "Настройки кабинета изменились. Повторите сравнение."
                )
            pricing_min_profit = cls._bulk_pricing_min_profit(seller_id)
            active_ids = {
                row[0]
                for row in db.session.query(
                    MarketplaceCommercialProposal.listing_id
                ).filter(
                    MarketplaceCommercialProposal.seller_id == seller_id,
                    MarketplaceCommercialProposal.account_id == account_id,
                    MarketplaceCommercialProposal.proposal_kind == "price",
                    MarketplaceCommercialProposal.listing_id.in_(listing_ids),
                    MarketplaceCommercialProposal.status.in_(
                        cls.ACTIVE_PROPOSAL_STATUSES
                    ),
                ).all()
            }
            baselines = cls._read_price_states(
                listings=listings,
                adapter=adapter,
                credentials=credentials,
            )
            claim_items = []
            response_items = []
            for listing in listings:
                baseline = baselines[listing.id]
                row = cls._bulk_price_row(
                    listing=listing,
                    baseline=baseline,
                    mode=mode,
                    value=value,
                    active_proposal=listing.id in active_ids,
                    allow_price_decrease=allow_price_decrease,
                    allow_large_change=allow_large_change,
                    guardrail_note=guardrail_note,
                    pricing_min_profit=pricing_min_profit,
                )
                claim_items.append({
                    "listing_id": listing.id,
                    "listing_version": cls._bulk_listing_version(listing),
                    "policy": cls._bulk_price_policy(
                        listing, pricing_min_profit=pricing_min_profit
                    ),
                    "baseline": baseline,
                    "active_proposal": listing.id in active_ids,
                    **row,
                })
                response_items.append({
                    "listing_id": listing.id,
                    "offer_id": listing.offer_id,
                    "product_id": listing.external_product_id,
                    "title": cls._safe_text(listing.title, maximum=500),
                    "status": row["status"],
                    "reason_code": row["reason_code"],
                    "reason": row["reason"],
                    "current_price": baseline["price"],
                    "proposed_price": (
                        row["price_review"]["price"]
                        if row["price_review"] is not None else None
                    ),
                    "currency_code": baseline["currency_code"],
                    "price_review": row["price_review"],
                    "guardrails": row["guardrails"],
                })
            counts = {
                "selected_count": len(response_items),
                "eligible_count": sum(
                    item["status"] == "eligible" for item in response_items
                ),
                "noop_count": sum(
                    item["status"] == "noop" for item in response_items
                ),
                "blocked_count": sum(
                    item["status"] == "blocked" for item in response_items
                ),
            }
            claims = {
                "version": 1,
                "seller_id": seller_id,
                "user_id": user_id,
                "account_id": account_id,
                "account": cls._bulk_account_version(account),
                "listing_ids": listing_ids,
                "mode": mode,
                "value": value,
                "allow_price_decrease": allow_price_decrease,
                "allow_large_change": allow_large_change,
                "guardrail_note": guardrail_note,
                "nonce": secrets.token_hex(16),
                "items": claim_items,
            }
            token = cls._bulk_price_serializer().dumps(claims)
            if len(token.encode("utf-8")) > cls.BULK_PRICE_PREVIEW_MAX_TOKEN_BYTES:
                raise MarketplaceCommercialValidationError(
                    "Слишком большой набор сравнения. Выберите меньше товаров."
                )
            return {
                "success": True,
                **counts,
                "preview_token": token,
                "expires_in_seconds": cls.BULK_PRICE_PREVIEW_MAX_AGE,
                "items": response_items,
            }
        finally:
            try:
                db.session.rollback()
            finally:
                release_account_operation_lock(lock_file)

    @classmethod
    def confirm_bulk_price_proposals(
        cls,
        *,
        seller_id: int,
        created_by_user_id: Optional[Any],
        preview_token: Any,
    ) -> dict:
        seller_id = cls._positive_integer(seller_id, "seller_id")
        user_id = cls._validate_user(
            seller_id=seller_id,
            user_id=created_by_user_id,
        )
        if user_id is None:
            raise MarketplaceCommercialValidationError(
                "Сохранение требует авторизованного пользователя"
            )
        claims = cls._bulk_claims(
            preview_token,
            seller_id=seller_id,
            user_id=user_id,
        )
        eligible, fingerprints = cls._bulk_expected_proposals(
            claims=claims,
            seller_id=seller_id,
            user_id=user_id,
        )
        if not eligible:
            raise MarketplaceCommercialConflict(
                "В сравнении нет заявок, готовых к сохранению"
            )

        account_id = claims["account_id"]
        listing_ids = claims["listing_ids"]
        cls._bulk_price_account(
            seller_id=seller_id,
            account_id=account_id,
        )

        # A retry after a lost response is resolved from durable exact keys
        # before consulting mutable account/listing/policy state.
        replay = cls._bulk_replay_items(
            claims=claims,
            seller_id=seller_id,
            user_id=user_id,
            eligible=eligible,
            fingerprints=fingerprints,
        )
        if replay is not None:
            return {
                "selected_count": len(listing_ids),
                "created_count": 0,
                "saved_count": len(eligible),
                "replayed_count": len(eligible),
                "replayed": True,
                "noop_count": sum(item["status"] == "noop" for item in claims["items"]),
                "blocked_count": sum(item["status"] == "blocked" for item in claims["items"]),
                "items": replay,
            }
        db.session.rollback()

        lock_file = try_account_operation_lock(account_id)
        if lock_file is None:
            raise MarketplaceCommercialBusy("Кабинет Ozon занят другой операцией")
        try:
            cls._bulk_price_account(
                seller_id=seller_id,
                account_id=account_id,
            )
            replay = cls._bulk_replay_items(
                claims=claims,
                seller_id=seller_id,
                user_id=user_id,
                eligible=eligible,
                fingerprints=fingerprints,
            )
            if replay is not None:
                return {
                    "selected_count": len(listing_ids),
                    "created_count": 0,
                    "saved_count": len(eligible),
                    "replayed_count": len(eligible),
                    "replayed": True,
                    "noop_count": sum(item["status"] == "noop" for item in claims["items"]),
                    "blocked_count": sum(item["status"] == "blocked" for item in claims["items"]),
                    "items": replay,
                }
            db.session.rollback()
            cls._begin_bulk_price_validation_transaction()
            # The account file lock may have waited while the signed preview
            # approached expiry. Recheck its timed signature before using the
            # claims for the now-locked local validation and insert.
            if cls._bulk_claims(
                preview_token,
                seller_id=seller_id,
                user_id=user_id,
            ) != claims:
                raise MarketplaceCommercialValidationError(
                    "Сравнение цены истекло или изменилось. Проведите его снова."
                )
            account, listings = cls._bulk_price_scope(
                seller_id=seller_id,
                account_id=account_id,
                listing_ids=listing_ids,
                require_available=False,
            )
            by_id = {listing.id: listing for listing in listings}
            if any(not listing.is_available or listing.is_archived for listing in listings):
                raise MarketplaceCommercialConflict(
                    "Один из выбранных товаров больше недоступен. Проведите новое сравнение."
                )
            if cls._bulk_account_version(account) != claims["account"]:
                raise MarketplaceCommercialConflict(
                    "Настройки кабинета изменились. Проведите новое сравнение."
                )
            pricing_min_profit = cls._bulk_pricing_min_profit(seller_id)
            active_ids = {
                row[0]
                for row in db.session.query(
                    MarketplaceCommercialProposal.listing_id
                ).filter(
                    MarketplaceCommercialProposal.seller_id == seller_id,
                    MarketplaceCommercialProposal.account_id == account_id,
                    MarketplaceCommercialProposal.proposal_kind == "price",
                    MarketplaceCommercialProposal.listing_id.in_(listing_ids),
                    MarketplaceCommercialProposal.status.in_(
                        cls.ACTIVE_PROPOSAL_STATUSES
                    ),
                ).all()
            }
            expected_active_ids = {
                item["listing_id"]
                for item in claims["items"]
                if item["active_proposal"]
            }
            if active_ids != expected_active_ids:
                raise MarketplaceCommercialConflict(
                    "Незавершённые заявки по выбранным товарам изменились. Проведите новое сравнение."
                )
            verified_rows = {}
            for item in claims["items"]:
                listing = by_id[item["listing_id"]]
                current_version = cls._bulk_listing_version(listing)
                current_policy = cls._bulk_price_policy(
                    listing, pricing_min_profit=pricing_min_profit
                )
                if (
                    current_version != item["listing_version"]
                    or current_policy != item["policy"]
                ):
                    raise MarketplaceCommercialConflict(
                        "Данные товара или правило цены изменились. Проведите новое сравнение."
                    )
                row = cls._bulk_price_row(
                    listing=listing,
                    baseline=item["baseline"],
                    mode=claims["mode"],
                    value=claims["value"],
                    active_proposal=item["active_proposal"],
                    allow_price_decrease=claims["allow_price_decrease"],
                    allow_large_change=claims["allow_large_change"],
                    guardrail_note=claims["guardrail_note"],
                    pricing_min_profit=pricing_min_profit,
                )
                comparable = {
                    key: row[key]
                    for key in (
                        "status", "reason_code", "reason", "price_review",
                        "proposed", "guardrails",
                    )
                }
                original = {key: item[key] for key in comparable}
                if comparable != original:
                    raise MarketplaceCommercialConflict(
                        "Правило или предложение изменилось. Проведите новое сравнение."
                    )
                verified_rows[item["listing_id"]] = row

            created = []
            try:
                for item in eligible:
                    listing = by_id[item["listing_id"]]
                    row = verified_rows[listing.id]
                    created.append(cls._persist_proposal(
                        listing=listing,
                        warehouse=None,
                        proposal_kind="price",
                        baseline=item["baseline"],
                        proposed=row["proposed"],
                        guardrails=row["guardrails"],
                        source="user",
                        idempotency_key=f"bulk-price:{claims['nonce']}:{listing.id}",
                        created_by_user_id=user_id,
                        agent_task_id=None,
                        commit=False,
                    ))
                db.session.commit()
            except IntegrityError:
                db.session.rollback()
                replay = cls._bulk_replay_items(
                    claims=claims,
                    seller_id=seller_id,
                    user_id=user_id,
                    eligible=eligible,
                    fingerprints=fingerprints,
                )
                if replay is not None:
                    return {
                        "selected_count": len(listing_ids),
                        "created_count": 0,
                        "saved_count": len(eligible),
                        "replayed_count": len(eligible),
                        "replayed": True,
                        "noop_count": sum(item["status"] == "noop" for item in claims["items"]),
                        "blocked_count": sum(item["status"] == "blocked" for item in claims["items"]),
                        "items": replay,
                    }
                raise MarketplaceCommercialConflict(
                    "Набор заявок изменился при сохранении. Проведите новое сравнение."
                ) from None
            except Exception:
                db.session.rollback()
                replay = cls._bulk_replay_items(
                    claims=claims,
                    seller_id=seller_id,
                    user_id=user_id,
                    eligible=eligible,
                    fingerprints=fingerprints,
                )
                if replay is not None:
                    return {
                        "selected_count": len(listing_ids),
                        "created_count": 0,
                        "saved_count": len(eligible),
                        "replayed_count": len(eligible),
                        "replayed": True,
                        "noop_count": sum(item["status"] == "noop" for item in claims["items"]),
                        "blocked_count": sum(item["status"] == "blocked" for item in claims["items"]),
                        "items": replay,
                    }
                raise
            return {
                "selected_count": len(listing_ids),
                "created_count": len(created),
                "saved_count": len(created),
                "replayed_count": 0,
                "replayed": False,
                "noop_count": sum(item["status"] == "noop" for item in claims["items"]),
                "blocked_count": sum(item["status"] == "blocked" for item in claims["items"]),
                "items": [
                    {
                        "listing_id": proposal.listing_id,
                        "status": proposal.status,
                        "proposal": proposal,
                    }
                    for proposal in created
                ],
            }
        finally:
            try:
                db.session.rollback()
            finally:
                release_account_operation_lock(lock_file)

    @classmethod
    def _approval_items(cls, value: Any) -> list:
        if (
            not isinstance(value, (list, tuple))
            or not 1 <= len(value) <= cls.MAX_BATCH_APPROVAL
        ):
            raise MarketplaceCommercialValidationError(
                f"Batch approval должен содержать 1..{cls.MAX_BATCH_APPROVAL} proposals"
            )
        normalized = []
        seen = set()
        for index, item in enumerate(value):
            if not isinstance(item, Mapping) or set(item) != {
                "proposal_id",
                "expected_version",
            }:
                raise MarketplaceCommercialValidationError(
                    f"items[{index}] должен содержать proposal_id и expected_version"
                )
            proposal_id = cls._positive_integer(
                item.get("proposal_id"),
                f"items[{index}].proposal_id",
            )
            expected_version = cls._positive_integer(
                item.get("expected_version"),
                f"items[{index}].expected_version",
            )
            if proposal_id in seen:
                raise MarketplaceCommercialValidationError(
                    "Batch approval содержит duplicate proposal_id"
                )
            seen.add(proposal_id)
            normalized.append({
                "proposal_id": proposal_id,
                "expected_version": expected_version,
            })
        return normalized

    @classmethod
    def _json(cls, value: Any, expected_type: type) -> str:
        if not isinstance(value, expected_type):
            raise MarketplaceCommercialValidationError(
                "Commercial snapshot has an invalid type"
            )
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if len(encoded.encode("utf-8")) > cls.MAX_JSON_BYTES:
            raise MarketplaceCommercialValidationError(
                "Commercial snapshot exceeds storage limit"
            )
        return encoded

    @classmethod
    def _load_object(cls, value: Any, field_name: str) -> dict:
        try:
            parsed = json.loads(value or "")
        except (TypeError, json.JSONDecodeError):
            raise MarketplaceCommercialConflict(
                f"Сохранённое поле {field_name} повреждено"
            ) from None
        if not isinstance(parsed, dict):
            raise MarketplaceCommercialConflict(
                f"Сохранённое поле {field_name} повреждено"
            )
        return parsed

    @classmethod
    def _fingerprint(cls, value: Mapping[str, Any]) -> str:
        return hashlib.sha256(cls._json(dict(value), dict).encode("utf-8")).hexdigest()

    @staticmethod
    def _safe_text(value: Any, *, maximum: int) -> str:
        return " ".join(str(value or "").replace("\x00", " ").split())[:maximum]

    @classmethod
    def _idempotency_key(cls, value: Optional[Any]) -> str:
        if value is None:
            return uuid.uuid4().hex
        if not isinstance(value, str) or not cls._IDEMPOTENCY_KEY.fullmatch(value):
            raise MarketplaceCommercialValidationError(
                "idempotency_key должен содержать 8–128 безопасных символов"
            )
        return value

    @classmethod
    def _validate_user(cls, *, seller_id: int, user_id: Optional[Any]) -> Optional[int]:
        if user_id is None:
            return None
        user_id = cls._positive_integer(user_id, "user_id")
        if Seller.query.filter_by(id=seller_id, user_id=user_id).first() is None:
            raise MarketplaceCommercialNotFound("Seller user scope не найден")
        return user_id

    @classmethod
    def _validate_source(
        cls,
        *,
        seller_id: int,
        source: Any,
        agent_task_id: Optional[Any],
        created_by_user_id: Optional[int],
    ) -> Tuple[str, Optional[str]]:
        if not isinstance(source, str) or source not in {"user", "agent", "system", "rollback"}:
            raise MarketplaceCommercialValidationError("Неизвестный source proposal")
        if source == "user" and created_by_user_id is None:
            raise MarketplaceCommercialValidationError(
                "User proposal требует created_by_user_id"
            )
        if source == "agent":
            if not isinstance(agent_task_id, str) or not agent_task_id.strip():
                raise MarketplaceCommercialValidationError(
                    "Agent proposal требует typed task scope"
                )
            task_id = agent_task_id.strip()
            task = AgentTask.query.filter_by(id=task_id, seller_id=seller_id).first()
            if task is None:
                raise MarketplaceCommercialNotFound("Agent task scope не найден")
            return source, task_id
        if agent_task_id is not None:
            raise MarketplaceCommercialValidationError(
                "agent_task_id допустим только для agent proposal"
            )
        return source, None

    @classmethod
    def _owned_listing(cls, *, seller_id: int, listing_id: int) -> MarketplaceListing:
        listing = MarketplaceListing.query.options(
            joinedload(MarketplaceListing.account),
            joinedload(MarketplaceListing.imported_product),
        ).join(Marketplace).filter(
            MarketplaceListing.id == listing_id,
            MarketplaceListing.seller_id == seller_id,
            Marketplace.code == "ozon",
        ).first()
        if listing is None or listing.account_id is None:
            raise MarketplaceCommercialNotFound("Листинг Ozon не найден")
        if (
            listing.account is None
            or listing.account.seller_id != seller_id
            or listing.account.marketplace_id != listing.marketplace_id
        ):
            raise MarketplaceCommercialNotFound("Листинг Ozon не найден")
        if listing.is_archived or not listing.is_available:
            raise MarketplaceCommercialConflict(
                "Цена и остаток изменяются только для доступного неархивного листинга"
            )
        return listing

    @classmethod
    def _owned_proposal(
        cls,
        *,
        seller_id: int,
        proposal_id: int,
    ) -> MarketplaceCommercialProposal:
        seller_id = cls._positive_integer(seller_id, "seller_id")
        proposal_id = cls._positive_integer(proposal_id, "proposal_id")
        proposal = MarketplaceCommercialProposal.query.options(
            joinedload(MarketplaceCommercialProposal.marketplace),
            joinedload(MarketplaceCommercialProposal.account),
            joinedload(MarketplaceCommercialProposal.listing),
            joinedload(MarketplaceCommercialProposal.warehouse),
            joinedload(MarketplaceCommercialProposal.operation).joinedload(
                MarketplaceOperation.snapshot
            ),
        ).filter_by(id=proposal_id, seller_id=seller_id).first()
        if proposal is None:
            raise MarketplaceCommercialNotFound("Commercial proposal не найден")
        if (
            proposal.account is None
            or proposal.listing is None
            or proposal.marketplace is None
            or proposal.marketplace.code != "ozon"
            or proposal.account.seller_id != seller_id
            or proposal.listing.seller_id != seller_id
            or proposal.listing.account_id != proposal.account_id
        ):
            raise MarketplaceCommercialNotFound("Commercial proposal не найден")
        if proposal.proposal_kind == "stock" and (
            proposal.warehouse is None
            or proposal.warehouse.seller_id != seller_id
            or proposal.warehouse.account_id != proposal.account_id
        ):
            raise MarketplaceCommercialNotFound("Commercial proposal не найден")
        return proposal

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
            joinedload(MarketplaceOperation.account),
            joinedload(MarketplaceOperation.listing),
            joinedload(MarketplaceOperation.snapshot),
        ).filter_by(id=operation_id, seller_id=seller_id).first()
        if (
            operation is None
            or operation.operation_kind not in cls.COMMERCIAL_OPERATION_KINDS
            or operation.account is None
            or operation.account.seller_id != seller_id
            or operation.listing is None
            or operation.listing.seller_id != seller_id
        ):
            raise MarketplaceCommercialNotFound("Commercial operation не найден")
        return operation

    @classmethod
    def _resolve_account(
        cls,
        *,
        seller_id: int,
        account_id: int,
        proposal_kind: str,
        adapter=None,
        credentials: Optional[MarketplaceCredentials] = None,
        now: Optional[datetime] = None,
        write: bool = False,
    ):
        capabilities = (
            ("prices_read", "prices_write")
            if proposal_kind == "price" and write
            else ("prices_read",)
            if proposal_kind == "price"
            else ("stocks_read", "stocks_write")
            if write
            else ("stocks_read",)
        )
        try:
            return MarketplaceWarehouseService._account_adapter_credentials(
                seller_id=seller_id,
                account_id=account_id,
                capabilities=capabilities,
                adapter=adapter,
                credentials=credentials,
                now=now,
            )
        except MarketplaceWarehouseError as exc:
            raise MarketplaceCommercialConfigurationError(str(exc)) from None

    @classmethod
    def _read_price_state(
        cls,
        *,
        listing: MarketplaceListing,
        adapter,
        credentials: MarketplaceCredentials,
    ) -> dict:
        return cls._read_price_states(
            listings=[listing],
            adapter=adapter,
            credentials=credentials,
        )[listing.id]

    @classmethod
    def _read_price_states(
        cls,
        *,
        listings: list,
        adapter,
        credentials: MarketplaceCredentials,
    ) -> dict:
        try:
            return cls._read_price_states_unwrapped(
                listings=listings,
                adapter=adapter,
                credentials=credentials,
            )
        except OzonAPIError as exc:
            raise MarketplaceCommercialUpstreamError(
                "Не удалось прочитать актуальную цену Ozon",
                retry_after=exc.retry_after,
            ) from None
        except OzonCommercialContractError:
            raise MarketplaceCommercialUpstreamError(
                "Ozon вернул некорректный ответ чтения цены"
            ) from None

    @classmethod
    def _read_price_states_unwrapped(
        cls,
        *,
        listings: list,
        adapter,
        credentials: MarketplaceCredentials,
    ) -> dict:
        if not 1 <= len(listings) <= cls.MAX_BATCH_APPROVAL:
            raise MarketplaceCommercialValidationError(
                "Price live read требует 1..100 listings"
            )
        expected = {}
        product_ids = []
        for listing in listings:
            identity = (listing.offer_id, str(listing.external_product_id))
            if identity in expected:
                raise MarketplaceCommercialConflict(
                    "Price live read содержит duplicate listing identity"
                )
            expected[identity] = listing.id
            try:
                product_ids.append(int(listing.external_product_id))
            except (TypeError, ValueError):
                raise MarketplaceCommercialConflict(
                    "Listing содержит некорректный Ozon product_id"
                ) from None
        cursor = ""
        seen_cursors = set()
        items = {}
        expected_total = None
        for _page_number in range(cls.MAX_PRICE_READ_PAGES):
            page = OzonPriceContract.normalize_read_page(
                adapter.read_prices(
                    credentials,
                    {
                        "filter": {
                            "product_id": product_ids,
                            "visibility": "ALL",
                        },
                        "cursor": cursor,
                        "limit": 100,
                    },
                )
            )
            # Ozon can return a cursor on the last populated page, followed by
            # an empty terminal page with total=0. Keep the populated-page
            # total and exact identity checks below; this sentinel is not a
            # new snapshot total and cannot make an incomplete read succeed.
            if (
                expected_total is not None
                and not page["items"]
                and not page["cursor"]
                and page["total"] == 0
            ):
                break
            if expected_total is None:
                expected_total = page["total"]
            elif page["total"] != expected_total:
                raise MarketplaceCommercialConflict(
                    "Ozon изменил total во время price preflight"
                )
            for item in page["items"]:
                identity = (item["offer_id"], item["product_id"])
                if identity not in expected:
                    raise MarketplaceCommercialConflict(
                        "Ozon price preflight вернул чужой товар"
                    )
                if identity in items:
                    raise MarketplaceCommercialConflict(
                        "Ozon price preflight повторил товар между страницами"
                    )
                items[identity] = item
            if not page["cursor"]:
                break
            next_cursor = page["cursor"]
            if next_cursor == cursor or next_cursor in seen_cursors:
                raise MarketplaceCommercialConflict(
                    "Ozon price pagination зациклилась"
                )
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        else:
            raise MarketplaceCommercialConflict(
                "Ozon price pagination превысила безопасный лимит"
            )
        if (
            expected_total != len(items)
            or set(items) != set(expected)
            or len(items) != len(listings)
        ):
            raise MarketplaceCommercialConflict(
                "Ozon price preflight не вернул exact-set ожидаемых товаров"
            )
        result = {}
        for identity, item in items.items():
            result[expected[identity]] = {
                "kind": "price",
                "offer_id": item["offer_id"],
                "product_id": item["product_id"],
                "price": item["price"],
                "old_price": item.get("old_price", "0"),
                "min_price": item.get("min_price", "0"),
                "currency_code": item["currency_code"],
                "auto_action_enabled": item.get("auto_action_enabled"),
                "auto_add_to_ozon_actions_list_enabled": item.get(
                    "auto_add_to_ozon_actions_list_enabled"
                ),
            }
        return result

    @classmethod
    def _read_stock_state(
        cls,
        *,
        listing: MarketplaceListing,
        warehouse: MarketplaceWarehouse,
        adapter,
        credentials: MarketplaceCredentials,
    ) -> dict:
        return cls._read_stock_targets(
            targets=[(listing.id, listing, warehouse)],
            adapter=adapter,
            credentials=credentials,
        )[listing.id]

    @classmethod
    def _read_stock_targets(
        cls,
        *,
        targets: list,
        adapter,
        credentials: MarketplaceCredentials,
    ) -> dict:
        if not 1 <= len(targets) <= cls.MAX_BATCH_APPROVAL:
            raise MarketplaceCommercialValidationError(
                "Stock live read требует 1..100 targets"
            )
        expected = {}
        known_listings = set()
        offers = []
        seen_offers = set()
        for key, listing, warehouse in targets:
            listing_identity = (listing.offer_id, str(listing.external_product_id))
            identity = (*listing_identity, str(warehouse.external_warehouse_id))
            if identity in expected:
                raise MarketplaceCommercialConflict(
                    "Stock live read содержит duplicate listing/warehouse identity"
                )
            expected[identity] = key
            known_listings.add(listing_identity)
            if listing.offer_id not in seen_offers:
                seen_offers.add(listing.offer_id)
                offers.append(listing.offer_id)
        cursor = ""
        seen_cursors = set()
        matched = {}
        try:
            for _page_number in range(MarketplaceWarehouseService.MAX_STOCK_PAGES):
                page = OzonStockContract.normalize_fbs_page(
                    adapter.read_stocks_by_warehouse_fbs(
                        credentials,
                        {"limit": 100, "cursor": cursor, "offer_id": offers},
                    )
                )
                for item in page["products"]:
                    listing_identity = (item["offer_id"], item["product_id"])
                    if listing_identity not in known_listings:
                        raise MarketplaceCommercialConflict(
                            "Ozon stock preflight вернул чужой товар"
                        )
                    identity = (*listing_identity, item["warehouse_id"])
                    if identity in expected:
                        if identity in matched:
                            raise MarketplaceCommercialConflict(
                                "Ozon stock preflight повторил target между страницами"
                            )
                        matched[identity] = item
                if not page["has_next"]:
                    break
                next_cursor = page["cursor"]
                if next_cursor == cursor or next_cursor in seen_cursors:
                    raise MarketplaceCommercialConflict(
                        "Ozon stock pagination зациклилась"
                    )
                seen_cursors.add(next_cursor)
                cursor = next_cursor
            else:
                raise MarketplaceCommercialConflict(
                    "Ozon stock pagination превысила безопасный лимит"
                )
        except OzonAPIError as exc:
            raise MarketplaceCommercialUpstreamError(
                "Не удалось прочитать exact-set остатков Ozon по складам",
                retry_after=exc.retry_after,
            ) from None
        except (OzonCommercialContractError, MarketplaceWarehouseError):
            raise MarketplaceCommercialUpstreamError(
                "Не удалось прочитать exact-set остатков Ozon по складам"
            ) from None
        if set(matched) != set(expected):
            raise MarketplaceCommercialConflict(
                "Ozon stock preflight не вернул каждый выбранный listing/warehouse"
            )
        return {
            expected[identity]: {
                "kind": "stock",
                "offer_id": item["offer_id"],
                "product_id": item["product_id"],
                "warehouse_id": item["warehouse_id"],
                "sku": item["sku"],
                "stock": item["free_stock"],
            }
            for identity, item in matched.items()
        }

    @classmethod
    def _read_live_state(
        cls,
        *,
        proposal: MarketplaceCommercialProposal,
        adapter,
        credentials: MarketplaceCredentials,
    ) -> dict:
        return cls._read_live_states(
            proposals=[proposal],
            adapter=adapter,
            credentials=credentials,
        )[proposal.id]

    @classmethod
    def _read_live_states(
        cls,
        *,
        proposals: list,
        adapter,
        credentials: MarketplaceCredentials,
    ) -> dict:
        if not proposals or len({proposal.proposal_kind for proposal in proposals}) != 1:
            raise MarketplaceCommercialValidationError(
                "Live batch должен содержать один proposal kind"
            )
        if proposals[0].proposal_kind == "price":
            by_listing = cls._read_price_states(
                listings=[proposal.listing for proposal in proposals],
                adapter=adapter,
                credentials=credentials,
            )
            return {
                proposal.id: by_listing[proposal.listing_id]
                for proposal in proposals
            }
        return cls._read_stock_targets(
            targets=[
                (proposal.id, proposal.listing, proposal.warehouse)
                for proposal in proposals
            ],
            adapter=adapter,
            credentials=credentials,
        )

    @classmethod
    def _guard_price_change(
        cls,
        *,
        listing: MarketplaceListing,
        baseline: Mapping[str, Any],
        proposed_price: str,
        source: str,
        allow_price_decrease: bool,
        allow_large_change: bool,
        guardrail_note: Optional[Any],
        pricing_min_profit: Optional[Decimal] = None,
    ) -> dict:
        if not isinstance(allow_price_decrease, bool) or not isinstance(
            allow_large_change, bool
        ):
            raise MarketplaceCommercialValidationError(
                "Price override flags должны быть boolean"
            )
        current = Decimal(baseline["price"])
        proposed = Decimal(proposed_price)
        direction = "increase" if proposed > current else "decrease" if proposed < current else "same"
        if direction == "same":
            raise MarketplaceCommercialValidationError("Новая цена не отличается от текущей")
        if source == "agent" and (allow_price_decrease or allow_large_change):
            raise MarketplaceCommercialValidationError(
                "Agent proposal не может устанавливать price override"
            )
        if direction == "decrease" and not allow_price_decrease and source != "rollback":
            raise MarketplaceCommercialValidationError(
                "Снижение цены требует отдельного явного разрешения"
            )
        change_pct = (abs(proposed - current) / current * Decimal("100"))
        if (
            change_pct > cls.MAX_PRICE_CHANGE_PCT
            and not allow_large_change
            and source != "rollback"
        ):
            raise MarketplaceCommercialValidationError(
                "Изменение цены больше 50% требует отдельного разрешения"
            )
        overrides = allow_price_decrease or allow_large_change
        note = ""
        if guardrail_note is not None:
            if not isinstance(guardrail_note, str):
                raise MarketplaceCommercialValidationError(
                    "guardrail_note должен быть строкой"
                )
            note = cls._safe_text(guardrail_note, maximum=1000)
        if overrides and len(note) < 8:
            raise MarketplaceCommercialValidationError(
                "Для price override укажите содержательное обоснование"
            )
        old_price = Decimal(str(baseline.get("old_price", "0")))
        if old_price > 0 and proposed >= old_price:
            raise MarketplaceCommercialValidationError(
                "Новая цена должна оставаться ниже текущей old_price; implicit old_price change запрещён"
            )
        minimum_price = Decimal(str(baseline.get("min_price", "0")))
        if minimum_price > 0 and proposed < minimum_price:
            raise MarketplaceCommercialValidationError(
                "Новая цена ниже текущей min_price Ozon"
            )
        supplier_floor = None
        imported = listing.imported_product
        if imported is not None and imported.supplier_price is not None:
            try:
                supplier_price = Decimal(str(imported.supplier_price))
            except (InvalidOperation, ValueError):
                supplier_price = Decimal("0")
            if pricing_min_profit is None:
                pricing = PricingSettings.query.filter_by(
                    seller_id=listing.seller_id
                ).first()
                min_profit = Decimal(str(
                    pricing.min_profit
                    if pricing is not None and pricing.min_profit is not None
                    else 0
                ))
            else:
                min_profit = pricing_min_profit
            if supplier_price > 0 and min_profit >= 0:
                floor = supplier_price * (Decimal("1") + min_profit / Decimal("100"))
                supplier_floor = format(floor.quantize(Decimal("0.01")), "f")
                if proposed < floor:
                    raise MarketplaceCommercialValidationError(
                        "Новая цена ниже закупочной цены с минимальной прибылью"
                    )
        return {
            "direction": direction,
            "change_pct": format(change_pct.quantize(Decimal("0.01")), "f"),
            "max_change_pct": format(cls.MAX_PRICE_CHANGE_PCT, "f"),
            "allow_price_decrease": allow_price_decrease,
            "allow_large_change": allow_large_change,
            "override_note": note,
            "supplier_floor": supplier_floor,
            "preserved_old_price": baseline.get("old_price", "0"),
            "preserved_min_price": baseline.get("min_price", "0"),
        }

    @classmethod
    def _persist_proposal(
        cls,
        *,
        listing: MarketplaceListing,
        warehouse: Optional[MarketplaceWarehouse],
        proposal_kind: str,
        baseline: dict,
        proposed: dict,
        guardrails: dict,
        source: str,
        idempotency_key: str,
        created_by_user_id: Optional[int],
        agent_task_id: Optional[str],
        rollback_of_operation_id: Optional[int] = None,
        commit: bool = True,
    ) -> MarketplaceCommercialProposal:
        baseline_fingerprint = cls._fingerprint(baseline)
        proposed_fingerprint = cls._fingerprint(proposed)
        request_fingerprint = cls._proposal_request_fingerprint(
            proposal_kind=proposal_kind,
            listing_id=listing.id,
            warehouse_id=warehouse.id if warehouse else None,
            baseline=baseline,
            proposed=proposed,
            source=source,
            rollback_of_operation_id=rollback_of_operation_id,
        )
        existing = MarketplaceCommercialProposal.query.filter_by(
            account_id=listing.account_id,
            proposal_kind=proposal_kind,
            idempotency_key=idempotency_key,
        ).first()
        if existing is not None:
            if (
                existing.seller_id == listing.seller_id
                and existing.listing_id == listing.id
                and existing.warehouse_id == (warehouse.id if warehouse else None)
                and existing.request_fingerprint == request_fingerprint
            ):
                return existing
            raise MarketplaceCommercialConflict(
                "idempotency_key уже использован для другого proposal"
            )
        contract_version = (
            OzonPriceContract.CONTRACT_VERSION
            if proposal_kind == "price"
            else OzonStockContract.CONTRACT_VERSION
        )
        proposal = MarketplaceCommercialProposal(
            seller_id=listing.seller_id,
            marketplace_id=listing.marketplace_id,
            account_id=listing.account_id,
            listing_id=listing.id,
            warehouse_id=warehouse.id if warehouse else None,
            rollback_of_operation_id=rollback_of_operation_id,
            agent_task_id=agent_task_id,
            created_by_user_id=created_by_user_id,
            proposal_kind=proposal_kind,
            source=source,
            status="pending_review",
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
            contract_version=contract_version,
            baseline_fingerprint=baseline_fingerprint,
            proposed_fingerprint=proposed_fingerprint,
            baseline_state_json=cls._json(baseline, dict),
            proposed_state_json=cls._json(proposed, dict),
            guardrails_json=cls._json(guardrails, dict),
        )
        db.session.add(proposal)
        if not commit:
            return proposal
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            duplicate = MarketplaceCommercialProposal.query.filter_by(
                account_id=listing.account_id,
                proposal_kind=proposal_kind,
                idempotency_key=idempotency_key,
            ).first()
            if duplicate is not None and duplicate.request_fingerprint == request_fingerprint:
                return duplicate
            raise MarketplaceCommercialConflict(
                "Для листинга уже есть активный commercial proposal"
            ) from None
        return proposal

    @classmethod
    def _proposal_request_fingerprint(
        cls,
        *,
        proposal_kind: str,
        listing_id: int,
        warehouse_id: Optional[int],
        baseline: dict,
        proposed: dict,
        source: str,
        rollback_of_operation_id: Optional[int] = None,
    ) -> str:
        return cls._fingerprint({
            "proposal_kind": proposal_kind,
            "listing_id": listing_id,
            "warehouse_id": warehouse_id,
            "baseline": baseline,
            "proposed": proposed,
            "source": source,
            "rollback_of_operation_id": rollback_of_operation_id,
        })

    @classmethod
    def create_price_proposal(
        cls,
        *,
        seller_id: int,
        listing_id: int,
        price: Any,
        source: str = "user",
        idempotency_key: Optional[Any] = None,
        created_by_user_id: Optional[Any] = None,
        agent_task_id: Optional[Any] = None,
        allow_price_decrease: bool = False,
        allow_large_change: bool = False,
        guardrail_note: Optional[Any] = None,
        adapter=None,
        credentials: Optional[MarketplaceCredentials] = None,
        now: Optional[datetime] = None,
    ) -> MarketplaceCommercialProposal:
        seller_id = cls._positive_integer(seller_id, "seller_id")
        listing_id = cls._positive_integer(listing_id, "listing_id")
        created_by = cls._validate_user(
            seller_id=seller_id,
            user_id=created_by_user_id,
        )
        source, task_id = cls._validate_source(
            seller_id=seller_id,
            source=source,
            agent_task_id=agent_task_id,
            created_by_user_id=created_by,
        )
        if source not in {"user", "agent"}:
            raise MarketplaceCommercialValidationError(
                "Rollback/system price proposal создаётся только typed workflow"
            )
        key = cls._idempotency_key(idempotency_key)
        listing = cls._owned_listing(seller_id=seller_id, listing_id=listing_id)
        _, resolved_adapter, resolved_credentials = cls._resolve_account(
            seller_id=seller_id,
            account_id=listing.account_id,
            proposal_kind="price",
            adapter=adapter,
            credentials=credentials,
            now=now,
        )
        lock_file = try_account_operation_lock(listing.account_id)
        if lock_file is None:
            raise MarketplaceCommercialBusy("Кабинет Ozon занят другой операцией")
        try:
            price_review = OzonPriceContract.prepare_price(price)
            baseline = cls._read_price_state(
                listing=listing,
                adapter=resolved_adapter,
                credentials=resolved_credentials,
            )
            proposed, guardrails = cls._price_proposal_details(
                listing=listing,
                baseline=baseline,
                price_review=price_review,
                source=source,
                allow_price_decrease=allow_price_decrease,
                allow_large_change=allow_large_change,
                guardrail_note=guardrail_note,
            )
            return cls._persist_proposal(
                listing=listing,
                warehouse=None,
                proposal_kind="price",
                baseline=baseline,
                proposed=proposed,
                guardrails=guardrails,
                source=source,
                idempotency_key=key,
                created_by_user_id=created_by,
                agent_task_id=task_id,
            )
        except OzonCommercialContractError as exc:
            db.session.rollback()
            raise MarketplaceCommercialValidationError(str(exc)) from None
        finally:
            release_account_operation_lock(lock_file)

    @classmethod
    def create_stock_proposal(
        cls,
        *,
        seller_id: int,
        listing_id: int,
        warehouse_id: int,
        stock: Any,
        source: str = "user",
        idempotency_key: Optional[Any] = None,
        created_by_user_id: Optional[Any] = None,
        agent_task_id: Optional[Any] = None,
        adapter=None,
        credentials: Optional[MarketplaceCredentials] = None,
        now: Optional[datetime] = None,
    ) -> MarketplaceCommercialProposal:
        seller_id = cls._positive_integer(seller_id, "seller_id")
        listing_id = cls._positive_integer(listing_id, "listing_id")
        warehouse_id = cls._positive_integer(warehouse_id, "warehouse_id")
        created_by = cls._validate_user(
            seller_id=seller_id,
            user_id=created_by_user_id,
        )
        source, task_id = cls._validate_source(
            seller_id=seller_id,
            source=source,
            agent_task_id=agent_task_id,
            created_by_user_id=created_by,
        )
        if source not in {"user", "agent"}:
            raise MarketplaceCommercialValidationError(
                "Rollback/system stock proposal создаётся только typed workflow"
            )
        key = cls._idempotency_key(idempotency_key)
        listing = cls._owned_listing(seller_id=seller_id, listing_id=listing_id)
        warehouse = MarketplaceWarehouse.query.filter_by(
            id=warehouse_id,
            seller_id=seller_id,
            marketplace_id=listing.marketplace_id,
            account_id=listing.account_id,
            is_available=True,
        ).first()
        if warehouse is None:
            raise MarketplaceCommercialNotFound("Склад Ozon не найден")
        _, resolved_adapter, resolved_credentials = cls._resolve_account(
            seller_id=seller_id,
            account_id=listing.account_id,
            proposal_kind="stock",
            adapter=adapter,
            credentials=credentials,
            now=now,
        )
        lock_file = try_account_operation_lock(listing.account_id)
        if lock_file is None:
            raise MarketplaceCommercialBusy("Кабинет Ozon занят другой операцией")
        try:
            baseline = cls._read_stock_state(
                listing=listing,
                warehouse=warehouse,
                adapter=resolved_adapter,
                credentials=resolved_credentials,
            )
            item = OzonStockContract.build_item(
                offer_id=listing.offer_id,
                product_id=listing.external_product_id,
                warehouse_id=warehouse.external_warehouse_id,
                stock=stock,
            )
            if item["stock"] == baseline["stock"]:
                raise MarketplaceCommercialValidationError(
                    "Новый остаток не отличается от текущего"
                )
            proposed = {
                "kind": "stock",
                "offer_id": listing.offer_id,
                "product_id": listing.external_product_id,
                "warehouse_id": warehouse.external_warehouse_id,
                "sku": baseline["sku"],
                "stock": item["stock"],
            }
            guardrails = {
                "warehouse_scoped": True,
                "stock_semantics": "free_stock",
                "direction": (
                    "increase" if item["stock"] > baseline["stock"] else "decrease"
                ),
                "zero_stock": item["stock"] == 0,
            }
            return cls._persist_proposal(
                listing=listing,
                warehouse=warehouse,
                proposal_kind="stock",
                baseline=baseline,
                proposed=proposed,
                guardrails=guardrails,
                source=source,
                idempotency_key=key,
                created_by_user_id=created_by,
                agent_task_id=task_id,
            )
        except OzonCommercialContractError as exc:
            db.session.rollback()
            raise MarketplaceCommercialValidationError(str(exc)) from None
        finally:
            release_account_operation_lock(lock_file)

    @classmethod
    def reject_proposal(
        cls,
        *,
        seller_id: int,
        proposal_id: int,
        expected_version: int,
        reviewed_by_user_id: int,
        note: Optional[Any] = None,
    ) -> MarketplaceCommercialProposal:
        expected_version = cls._positive_integer(expected_version, "expected_version")
        reviewer = cls._validate_user(
            seller_id=seller_id,
            user_id=reviewed_by_user_id,
        )
        proposal = cls._owned_proposal(
            seller_id=seller_id,
            proposal_id=proposal_id,
        )
        if proposal.version != expected_version or proposal.status != "pending_review":
            raise MarketplaceCommercialConflict(
                "Proposal уже изменился; обновите страницу"
            )
        if note is not None and not isinstance(note, str):
            raise MarketplaceCommercialValidationError("note должен быть строкой")
        proposal.status = "rejected"
        proposal.reviewed_by_user_id = reviewer
        proposal.review_note = cls._safe_text(note, maximum=1000) if note else None
        proposal.reviewed_at = datetime.utcnow()
        if proposal.source == "rollback" and proposal.rollback_of_operation is not None:
            original_snapshot = proposal.rollback_of_operation.snapshot
            if original_snapshot is not None and original_snapshot.rollback_status == "pending":
                original_snapshot.rollback_status = "available"
        try:
            db.session.commit()
        except StaleDataError:
            db.session.rollback()
            raise MarketplaceCommercialConflict(
                "Proposal уже изменился; обновите страницу"
            ) from None
        return proposal

    @classmethod
    def _set_baseline_drift(
        cls,
        *,
        proposal: MarketplaceCommercialProposal,
        reviewer_id: int,
        now: datetime,
    ) -> None:
        proposal.status = "conflict"
        proposal.reviewed_by_user_id = reviewer_id
        proposal.reviewed_at = now
        proposal.error_code = "commercial_baseline_drift"
        proposal.error_message = (
            "Live state изменился после создания proposal; write не отправлен"
        )
        if proposal.source == "rollback" and proposal.rollback_of_operation is not None:
            original_snapshot = proposal.rollback_of_operation.snapshot
            if original_snapshot is not None:
                original_snapshot.rollback_status = "conflict"
                original_snapshot.rollback_error_code = proposal.error_code
                original_snapshot.rollback_error_message = proposal.error_message

    @classmethod
    def _payload_for_proposal(cls, proposal: MarketplaceCommercialProposal) -> dict:
        return cls._payload_for_proposals([proposal])

    @classmethod
    def _payload_for_proposals(cls, proposals: list) -> dict:
        if not proposals or len(proposals) > cls.MAX_BATCH_APPROVAL:
            raise MarketplaceCommercialValidationError(
                "Commercial payload batch имеет недопустимый размер"
            )
        kinds = {proposal.proposal_kind for proposal in proposals}
        if len(kinds) != 1:
            raise MarketplaceCommercialValidationError(
                "Один Ozon batch не может смешивать price и stock"
            )
        proposed_states = []
        for proposal in proposals:
            proposed = cls._load_object(
                proposal.proposed_state_json,
                "proposed_state",
            )
            if cls._fingerprint(proposed) != proposal.proposed_fingerprint:
                raise MarketplaceCommercialConflict(
                    "Proposal fingerprint не совпадает"
                )
            proposed_states.append(proposed)
        if proposals[0].proposal_kind == "price":
            return OzonPriceContract.build_payload([{
                "offer_id": proposed.get("offer_id"),
                "product_id": proposed.get("product_id"),
                "price": proposed.get("price"),
                "old_price": proposed.get("old_price", "0"),
                "currency_code": proposed.get("currency_code"),
            } for proposed in proposed_states])
        return OzonStockContract.build_payload([{
            "offer_id": proposed.get("offer_id"),
            "product_id": proposed.get("product_id"),
            "warehouse_id": proposed.get("warehouse_id"),
            "stock": proposed.get("stock"),
        } for proposed in proposed_states])

    @classmethod
    def _proposal_identity(
        cls,
        proposal: MarketplaceCommercialProposal,
    ) -> tuple:
        proposed = cls._load_object(
            proposal.proposed_state_json,
            "proposed_state",
        )
        if proposal.proposal_kind == "price":
            return (
                str(proposed.get("offer_id")),
                str(proposed.get("product_id")),
            )
        return (
            str(proposed.get("offer_id")),
            str(proposed.get("product_id")),
            str(proposed.get("warehouse_id")),
        )

    @staticmethod
    def _result_identity(proposal_kind: str, item: Mapping[str, Any]) -> tuple:
        if proposal_kind == "price":
            return (str(item.get("offer_id")), str(item.get("product_id")))
        return (
            str(item.get("offer_id")),
            str(item.get("product_id")),
            str(item.get("warehouse_id")),
        )

    @classmethod
    def _build_operation(
        cls,
        *,
        proposal: MarketplaceCommercialProposal,
        reviewer_id: int,
        now: datetime,
    ) -> MarketplaceOperation:
        baseline = cls._load_object(proposal.baseline_state_json, "baseline_state")
        proposed = cls._load_object(proposal.proposed_state_json, "proposed_state")
        operation_kind = (
            f"{proposal.proposal_kind}_rollback"
            if proposal.source == "rollback"
            else f"{proposal.proposal_kind}_update"
        )
        operation_key = hashlib.sha256(
            f"proposal:{proposal.id}:{proposal.idempotency_key}".encode("utf-8")
        ).hexdigest()
        operation = MarketplaceOperation(
            seller_id=proposal.seller_id,
            marketplace_id=proposal.marketplace_id,
            account_id=proposal.account_id,
            listing_id=proposal.listing_id,
            parent_operation_id=proposal.rollback_of_operation_id,
            created_by_user_id=reviewer_id,
            operation_kind=operation_kind,
            status="queued",
            idempotency_key=operation_key,
            request_fingerprint=proposal.request_fingerprint,
            contract_version=proposal.contract_version,
            request_summary_json=cls._json({
                "proposal_id": proposal.id,
                "proposal_kind": proposal.proposal_kind,
                "listing_id": proposal.listing_id,
                "warehouse_id": proposal.warehouse_id,
                "offer_id": baseline.get("offer_id"),
                "before": baseline,
                "proposed": proposed,
            }, dict),
            quota_snapshot_json="{}",
            quota_reserved=0,
            next_poll_at=now,
            deadline_at=now + cls.DEADLINE,
        )
        db.session.add(operation)
        db.session.flush()
        snapshot = MarketplaceListingSnapshot(
            seller_id=proposal.seller_id,
            marketplace_id=proposal.marketplace_id,
            account_id=proposal.account_id,
            operation_id=operation.id,
            listing_id=proposal.listing_id,
            snapshot_kind=proposal.proposal_kind,
            source_fingerprint=proposal.listing.sync_fingerprint,
            before_fingerprint=proposal.baseline_fingerprint,
            submitted_fingerprint=proposal.proposed_fingerprint,
            before_state_json=proposal.baseline_state_json,
            submitted_state_json=proposal.proposed_state_json,
            rollback_state_json=proposal.baseline_state_json,
            rollback_status="not_requested",
        )
        db.session.add(snapshot)
        if proposal.source == "rollback" and proposal.rollback_of_operation is not None:
            original_snapshot = proposal.rollback_of_operation.snapshot
            if original_snapshot is None:
                raise MarketplaceCommercialConflict(
                    "Original rollback snapshot отсутствует"
                )
            original_snapshot.rollback_operation_id = operation.id
            original_snapshot.rollback_status = "pending"
        proposal.status = "approved"
        proposal.reviewed_by_user_id = reviewer_id
        proposal.reviewed_at = now
        proposal.operation_id = operation.id
        return operation

    @classmethod
    def _create_operation(
        cls,
        *,
        proposal: MarketplaceCommercialProposal,
        reviewer_id: int,
        now: datetime,
    ) -> MarketplaceOperation:
        operation = cls._build_operation(
            proposal=proposal,
            reviewer_id=reviewer_id,
            now=now,
        )
        db.session.commit()
        return operation

    @classmethod
    def _set_write_failed(
        cls,
        *,
        operation: MarketplaceOperation,
        proposal: MarketplaceCommercialProposal,
        code: str,
        message: str,
        now: datetime,
        request_id: Optional[str] = None,
    ) -> None:
        operation.status = "failed"
        operation.error_code = cls._safe_text(code, maximum=100)
        operation.error_message = cls._safe_text(message, maximum=1000)
        operation.next_poll_at = None
        operation.completed_at = now
        if request_id:
            operation.provider_request_ids_json = cls._json([request_id[:200]], list)
        proposal.status = "failed"
        proposal.error_code = operation.error_code
        proposal.error_message = operation.error_message
        if operation.snapshot is not None:
            operation.snapshot.rollback_status = "unavailable"
        if proposal.source == "rollback" and proposal.rollback_of_operation is not None:
            original_snapshot = proposal.rollback_of_operation.snapshot
            if original_snapshot is not None:
                original_snapshot.rollback_status = "failed"
                original_snapshot.rollback_error_code = operation.error_code
                original_snapshot.rollback_error_message = operation.error_message

    @classmethod
    def _mark_write_failed(
        cls,
        *,
        operation: MarketplaceOperation,
        proposal: MarketplaceCommercialProposal,
        code: str,
        message: str,
        now: datetime,
        request_id: Optional[str] = None,
    ) -> None:
        cls._set_write_failed(
            operation=operation,
            proposal=proposal,
            code=code,
            message=message,
            now=now,
            request_id=request_id,
        )
        db.session.commit()

    @classmethod
    def _submit_locked(
        cls,
        *,
        operation: MarketplaceOperation,
        proposal: MarketplaceCommercialProposal,
        adapter,
        credentials: MarketplaceCredentials,
        now: datetime,
    ) -> None:
        if operation.attempt_count != 0 or operation.status != "queued":
            raise MarketplaceCommercialConflict(
                "Commercial write уже мог быть отправлен; повтор запрещён"
            )
        if cls._stop_quarantined_batch([operation], [proposal], now=now):
            return
        unavailable = cls.target_unavailable_reason(proposal)
        if unavailable:
            cls._mark_write_failed(
                operation=operation, proposal=proposal,
                code="commercial_review_requires_refresh", message=unavailable,
                now=now,
            )
            return
        payload = cls._payload_for_proposal(proposal)
        operation.status = "submitting"
        operation.attempt_count = 1
        operation.submitted_at = now
        operation.next_poll_at = now
        proposal.status = "applying"
        db.session.commit()
        try:
            response = (
                adapter.update_prices(credentials, payload)
                if proposal.proposal_kind == "price"
                else adapter.update_stocks(credentials, payload)
            )
            normalized = (
                OzonPriceContract.normalize_response(response, payload)
                if proposal.proposal_kind == "price"
                else OzonStockContract.normalize_response(response, payload)
            )
        except OzonAmbiguousWriteError as exc:
            operation.status = "uncertain"
            operation.error_code = exc.code
            operation.error_message = "Результат Ozon write неизвестен; повтор запрещён"
            operation.next_poll_at = now
            if exc.request_id:
                operation.provider_request_ids_json = cls._json(
                    [exc.request_id[:200]], list
                )
            proposal.status = "uncertain"
            proposal.error_code = operation.error_code
            proposal.error_message = operation.error_message
            db.session.commit()
            return
        except OzonAPIError as exc:
            cls._mark_write_failed(
                operation=operation,
                proposal=proposal,
                code=exc.code,
                message="Ozon отклонил commercial write до подтверждения результата",
                now=now,
                request_id=exc.request_id,
            )
            return
        except (OzonCommercialProtocolError, OzonCommercialContractError):
            operation.status = "uncertain"
            operation.error_code = "ozon_commercial_response_invalid"
            operation.error_message = (
                "Ozon вернул неизвестный write response; повтор запрещён"
            )
            operation.next_poll_at = now
            proposal.status = "uncertain"
            proposal.error_code = operation.error_code
            proposal.error_message = operation.error_message
            db.session.commit()
            return
        operation.item_results_json = cls._json(normalized["items"], list)
        if normalized["updated"] != 1 or normalized["failed"] != 0:
            cls._mark_write_failed(
                operation=operation,
                proposal=proposal,
                code="ozon_commercial_item_rejected",
                message="Ozon отклонил изменение; подробности сохранены в операции",
                now=now,
            )
            return
        operation.status = "submitted"
        operation.error_code = None
        operation.error_message = None
        operation.next_poll_at = now
        db.session.commit()

    @classmethod
    def _submit_batch_locked(
        cls,
        *,
        operations: list,
        proposals: list,
        adapter,
        credentials: MarketplaceCredentials,
        now: datetime,
    ) -> None:
        if (
            not operations
            or len(operations) != len(proposals)
            or len(operations) > cls.MAX_BATCH_APPROVAL
        ):
            raise MarketplaceCommercialValidationError(
                "Commercial batch operation set некорректен"
            )
        account_ids = {proposal.account_id for proposal in proposals}
        kinds = {proposal.proposal_kind for proposal in proposals}
        if len(account_ids) != 1 or len(kinds) != 1:
            raise MarketplaceCommercialValidationError(
                "Один Ozon write batch требует один account и один proposal kind"
            )
        for operation, proposal in zip(operations, proposals):
            if (
                operation.attempt_count != 0
                or operation.status != "queued"
                or proposal.operation_id != operation.id
                or proposal.status != "approved"
            ):
                raise MarketplaceCommercialConflict(
                    "Один из commercial writes уже мог быть отправлен; batch запрещён"
                )
        if cls._stop_quarantined_batch(operations, proposals, now=now):
            return
        payload = cls._payload_for_proposals(proposals)
        for operation, proposal in zip(operations, proposals):
            operation.status = "submitting"
            operation.attempt_count = 1
            operation.submitted_at = now
            operation.next_poll_at = now
            proposal.status = "applying"
        db.session.commit()

        proposal_kind = proposals[0].proposal_kind
        try:
            response = (
                adapter.update_prices(credentials, payload)
                if proposal_kind == "price"
                else adapter.update_stocks(credentials, payload)
            )
            normalized = (
                OzonPriceContract.normalize_response(response, payload)
                if proposal_kind == "price"
                else OzonStockContract.normalize_response(response, payload)
            )
        except OzonAmbiguousWriteError as exc:
            for operation, proposal in zip(operations, proposals):
                operation.status = "uncertain"
                operation.error_code = cls._safe_text(exc.code, maximum=100)
                operation.error_message = (
                    "Результат batch Ozon write неизвестен; повтор запрещён"
                )
                operation.next_poll_at = now
                if exc.request_id:
                    operation.provider_request_ids_json = cls._json(
                        [exc.request_id[:200]],
                        list,
                    )
                proposal.status = "uncertain"
                proposal.error_code = operation.error_code
                proposal.error_message = operation.error_message
            db.session.commit()
            return
        except OzonAPIError as exc:
            for operation, proposal in zip(operations, proposals):
                cls._set_write_failed(
                    operation=operation,
                    proposal=proposal,
                    code=exc.code,
                    message="Ozon отклонил batch commercial write",
                    now=now,
                    request_id=exc.request_id,
                )
            db.session.commit()
            return
        except (OzonCommercialProtocolError, OzonCommercialContractError):
            for operation, proposal in zip(operations, proposals):
                operation.status = "uncertain"
                operation.error_code = "ozon_commercial_batch_response_invalid"
                operation.error_message = (
                    "Ozon вернул неизвестный batch response; повтор запрещён"
                )
                operation.next_poll_at = now
                proposal.status = "uncertain"
                proposal.error_code = operation.error_code
                proposal.error_message = operation.error_message
            db.session.commit()
            return

        results = {
            cls._result_identity(proposal_kind, item): item
            for item in normalized["items"]
        }
        for operation, proposal in zip(operations, proposals):
            item = results[cls._proposal_identity(proposal)]
            operation.item_results_json = cls._json([item], list)
            if item["updated"] and not item["errors"]:
                operation.status = "submitted"
                operation.error_code = None
                operation.error_message = None
                operation.next_poll_at = now
            else:
                cls._set_write_failed(
                    operation=operation,
                    proposal=proposal,
                    code="ozon_commercial_item_rejected",
                    message=(
                        "Ozon отклонил элемент batch; подробности сохранены в операции"
                    ),
                    now=now,
                )
        db.session.commit()

    @classmethod
    def _project_confirmed_state(
        cls,
        *,
        proposal: MarketplaceCommercialProposal,
        confirmed: dict,
        now: datetime,
    ) -> None:
        listing = proposal.listing
        if proposal.proposal_kind == "price":
            try:
                summary = json.loads(listing.price_summary_json or "{}")
            except (TypeError, json.JSONDecodeError):
                summary = {}
            if not isinstance(summary, dict):
                summary = {}
            values = summary.get("values")
            if not isinstance(values, dict):
                values = {}
            values.update({
                "price": confirmed["price"],
                "old_price": confirmed.get("old_price", "0"),
                "min_price": confirmed.get("min_price", "0"),
            })
            summary.update({
                "available": True,
                "currency": confirmed["currency_code"],
                "values": values,
            })
            listing.price_summary_json = cls._json(summary, dict)
            listing.prices_synced_at = now
        else:
            row = MarketplaceWarehouseStock.query.filter_by(
                seller_id=proposal.seller_id,
                account_id=proposal.account_id,
                listing_id=proposal.listing_id,
                warehouse_id=proposal.warehouse_id,
            ).first()
            if row is not None:
                row.free_stock = confirmed["stock"]
                row.sync_fingerprint = cls._fingerprint(confirmed)
                row.observed_at = now
                row.is_available = True
        listing.sync_fingerprint = cls._fingerprint({
            "previous": listing.sync_fingerprint,
            "commercial_proposal_id": proposal.id,
            "confirmed": confirmed,
        })

    @classmethod
    def _reconcile_locked(
        cls,
        *,
        operation: MarketplaceOperation,
        proposal: MarketplaceCommercialProposal,
        adapter,
        credentials: MarketplaceCredentials,
        now: datetime,
    ) -> None:
        cls._reconcile_batch_locked(
            operations=[operation],
            proposals=[proposal],
            adapter=adapter,
            credentials=credentials,
            now=now,
        )

    @classmethod
    def _set_reconciliation_read_failure(
        cls,
        *,
        operation: MarketplaceOperation,
        proposal: MarketplaceCommercialProposal,
        now: datetime,
        retry_after=None,
    ) -> None:
        due = read_retry_at(
            operation, now=now, interval=cls.POLL_INTERVAL,
            retry_after=retry_after,
        )
        if operation.deadline_at and due >= operation.deadline_at:
            operation.status = "uncertain"
            operation.next_poll_at = None
            operation.error_code = "commercial_reconciliation_deadline"
            operation.error_message = (
                "Результат Ozon не подтверждён. Срок автоматической проверки "
                "истёк или пауза API выходит за его пределы; запись не повторяется"
            )
            proposal.status = "uncertain"
            proposal.error_code = operation.error_code
            proposal.error_message = operation.error_message
            return
        operation.next_poll_at = due
        if operation.status != "uncertain":
            operation.status = "polling"
            operation.error_code = "commercial_reconciliation_read_failed"
            operation.error_message = (
                "Временная live-сверка Ozon не удалась; write не повторяется"
            )

    @classmethod
    def _apply_reconciliation_state(
        cls,
        *,
        operation: MarketplaceOperation,
        proposal: MarketplaceCommercialProposal,
        live: dict,
        now: datetime,
    ) -> None:
        live_fingerprint = cls._fingerprint(live)
        if live_fingerprint == proposal.proposed_fingerprint:
            operation.status = "succeeded"
            operation.error_code = None
            operation.error_message = None
            operation.next_poll_at = None
            operation.completed_at = now
            proposal.status = "applied"
            proposal.error_code = None
            proposal.error_message = None
            proposal.applied_at = now
            snapshot = operation.snapshot
            snapshot.confirmed_state_json = cls._json(live, dict)
            snapshot.confirmed_fingerprint = live_fingerprint
            snapshot.rollback_status = "available"
            if proposal.source == "rollback" and proposal.rollback_of_operation is not None:
                original_snapshot = proposal.rollback_of_operation.snapshot
                if original_snapshot is not None:
                    original_snapshot.rollback_status = "succeeded"
                    original_snapshot.rollback_error_code = None
                    original_snapshot.rollback_error_message = None
            cls._project_confirmed_state(
                proposal=proposal,
                confirmed=live,
                now=now,
            )
            return
        if live_fingerprint == proposal.baseline_fingerprint:
            if operation.error_code == "commercial_reconciliation_read_failed":
                operation.error_code = None
                operation.error_message = None
            if operation.deadline_at and now >= operation.deadline_at:
                operation.status = "uncertain"
                operation.next_poll_at = None
                operation.error_code = "commercial_write_not_confirmed"
                operation.error_message = (
                    "Ozon сохранил before-state; автоматический повтор запрещён"
                )
                proposal.status = "uncertain"
                proposal.error_code = operation.error_code
                proposal.error_message = operation.error_message
            else:
                if operation.status != "uncertain":
                    operation.status = "polling"
                operation.next_poll_at = now + cls.POLL_INTERVAL
            return
        operation.status = "uncertain"
        operation.error_code = "commercial_live_state_conflict"
        operation.error_message = (
            "Live state не совпадает ни с before, ни с proposed; blind rollback запрещён"
        )
        operation.next_poll_at = None
        proposal.status = "conflict"
        proposal.error_code = operation.error_code
        proposal.error_message = operation.error_message
        operation.snapshot.rollback_status = "conflict"
        if proposal.source == "rollback" and proposal.rollback_of_operation is not None:
            original_snapshot = proposal.rollback_of_operation.snapshot
            if original_snapshot is not None:
                original_snapshot.rollback_status = "conflict"
                original_snapshot.rollback_error_code = operation.error_code
                original_snapshot.rollback_error_message = operation.error_message

    @classmethod
    def _reconcile_batch_locked(
        cls,
        *,
        operations: list,
        proposals: list,
        adapter,
        credentials: MarketplaceCredentials,
        now: datetime,
    ) -> None:
        if not operations or len(operations) != len(proposals):
            raise MarketplaceCommercialValidationError(
                "Reconciliation batch set некорректен"
            )
        for operation, proposal in zip(operations, proposals):
            if operation.snapshot is None:
                raise MarketplaceCommercialConflict(
                    "Commercial operation snapshot отсутствует"
                )
            if operation.id != proposal.operation_id:
                raise MarketplaceCommercialConflict(
                    "Commercial operation/proposal linkage повреждён"
                )
            operation.reconcile_count += 1
            operation.last_polled_at = now
        try:
            live_states = cls._read_live_states(
                proposals=proposals,
                adapter=adapter,
                credentials=credentials,
            )
        except (MarketplaceCommercialUpstreamError, MarketplaceCommercialConflict) as exc:
            for operation, proposal in zip(operations, proposals):
                cls._set_reconciliation_read_failure(
                    operation=operation,
                    proposal=proposal,
                    now=now,
                    retry_after=getattr(exc, "retry_after", None),
                )
                if write_quarantine.has_origin_hold(operation):
                    operation.next_poll_at = None
            db.session.commit()
            return
        for operation, proposal in zip(operations, proposals):
            cls._apply_reconciliation_state(
                operation=operation,
                proposal=proposal,
                live=live_states[proposal.id],
                now=now,
            )
            if write_quarantine.has_origin_hold(operation):
                operation.next_poll_at = None
        db.session.commit()

    @classmethod
    def create_rollback_proposal(
        cls,
        *,
        seller_id: int,
        operation_id: int,
        idempotency_key: Optional[Any] = None,
        created_by_user_id: int,
        adapter=None,
        credentials: Optional[MarketplaceCredentials] = None,
        now: Optional[datetime] = None,
    ) -> MarketplaceCommercialProposal:
        seller_id = cls._positive_integer(seller_id, "seller_id")
        operation_id = cls._positive_integer(operation_id, "operation_id")
        created_by = cls._validate_user(
            seller_id=seller_id,
            user_id=created_by_user_id,
        )
        key = cls._idempotency_key(idempotency_key)
        original = cls._owned_operation(
            seller_id=seller_id,
            operation_id=operation_id,
        )
        if original.operation_kind not in {"price_update", "stock_update"}:
            raise MarketplaceCommercialConflict(
                "Rollback разрешён только для исходного price/stock update"
            )
        if original.status != "succeeded" or original.snapshot is None:
            raise MarketplaceCommercialConflict(
                "Rollback требует подтверждённую succeeded operation со snapshot"
            )
        if original.snapshot.rollback_status not in {"available", "pending"}:
            raise MarketplaceCommercialConflict(
                "Rollback этой операции сейчас недоступен"
            )
        existing = MarketplaceCommercialProposal.query.filter_by(
            seller_id=seller_id,
            rollback_of_operation_id=original.id,
            proposal_kind=original.snapshot.snapshot_kind,
            idempotency_key=key,
        ).first()
        if existing is not None:
            return existing
        original_proposal = MarketplaceCommercialProposal.query.filter_by(
            seller_id=seller_id,
            operation_id=original.id,
        ).first()
        if original_proposal is None:
            raise MarketplaceCommercialConflict(
                "Original commercial proposal отсутствует"
            )
        proposal_kind = original_proposal.proposal_kind
        _, resolved_adapter, resolved_credentials = cls._resolve_account(
            seller_id=seller_id,
            account_id=original.account_id,
            proposal_kind=proposal_kind,
            adapter=adapter,
            credentials=credentials,
            now=now,
        )
        lock_file = try_account_operation_lock(original.account_id)
        if lock_file is None:
            raise MarketplaceCommercialBusy("Кабинет Ozon занят другой операцией")
        try:
            db.session.expire_all()
            original = cls._owned_operation(
                seller_id=seller_id,
                operation_id=operation_id,
            )
            original_proposal = MarketplaceCommercialProposal.query.filter_by(
                seller_id=seller_id,
                operation_id=original.id,
            ).first()
            before = cls._load_object(
                original.snapshot.before_state_json,
                "original_before_state",
            )
            submitted = cls._load_object(
                original.snapshot.submitted_state_json,
                "original_submitted_state",
            )
            if (
                cls._fingerprint(before) != original.snapshot.before_fingerprint
                or cls._fingerprint(submitted) != original.snapshot.submitted_fingerprint
            ):
                raise MarketplaceCommercialConflict(
                    "Original snapshot fingerprint не совпадает"
                )
            if proposal_kind == "price":
                try:
                    OzonPriceContract.whole_rub_price(before.get("price"))
                except OzonCommercialContractError:
                    raise MarketplaceCommercialConflict(
                        "Исходная цена содержит копейки: точный откат недоступен. "
                        "Подготовьте новое сравнение и подтвердите цену в целых рублях."
                    ) from None
            live = cls._read_live_state(
                proposal=original_proposal,
                adapter=resolved_adapter,
                credentials=resolved_credentials,
            )
            if cls._fingerprint(live) != original.snapshot.submitted_fingerprint:
                original.snapshot.rollback_status = "conflict"
                original.snapshot.rollback_error_code = "rollback_live_state_drift"
                original.snapshot.rollback_error_message = (
                    "Live state изменился после исходной операции; rollback не создан"
                )
                db.session.commit()
                raise MarketplaceCommercialConflict(
                    "Live state изменился; blind rollback запрещён"
                )
            guardrails = {
                "rollback_of_operation_id": original.id,
                "restore_exact_before_state": True,
                "requires_second_human_review": True,
                "conflict_preflight": "current_equals_original_submitted",
            }
            rollback = cls._persist_proposal(
                listing=original.listing,
                warehouse=original_proposal.warehouse,
                proposal_kind=proposal_kind,
                baseline=live,
                proposed=before,
                guardrails=guardrails,
                source="rollback",
                idempotency_key=key,
                created_by_user_id=created_by,
                agent_task_id=None,
                rollback_of_operation_id=original.id,
            )
            original.snapshot.rollback_status = "pending"
            db.session.commit()
            return rollback
        finally:
            release_account_operation_lock(lock_file)

    @classmethod
    def approve_proposals(
        cls,
        *,
        seller_id: int,
        items: Any,
        reviewed_by_user_id: int,
        adapter=None,
        credentials: Optional[MarketplaceCredentials] = None,
        now: Optional[datetime] = None,
    ) -> list:
        """Approve an exact proposal set through one provider batch write."""

        seller_id = cls._positive_integer(seller_id, "seller_id")
        approval_items = cls._approval_items(items)
        reviewer = cls._validate_user(
            seller_id=seller_id,
            user_id=reviewed_by_user_id,
        )

        def load_and_validate() -> list:
            proposals = [
                cls._owned_proposal(
                    seller_id=seller_id,
                    proposal_id=item["proposal_id"],
                )
                for item in approval_items
            ]
            if len({proposal.account_id for proposal in proposals}) != 1:
                raise MarketplaceCommercialValidationError(
                    "Batch approval требует proposals одного Ozon account"
                )
            if len({proposal.proposal_kind for proposal in proposals}) != 1:
                raise MarketplaceCommercialValidationError(
                    "Batch approval не может смешивать price и stock"
                )
            for proposal, item in zip(proposals, approval_items):
                if (
                    proposal.version != item["expected_version"]
                    or proposal.status != "pending_review"
                ):
                    raise MarketplaceCommercialConflict(
                        "Один из proposals уже изменился; batch write не начат"
                    )
                cls._require_available_target(proposal)
            return proposals

        proposals = load_and_validate()
        proposal_kind = proposals[0].proposal_kind
        account_id = proposals[0].account_id
        _, resolved_adapter, resolved_credentials = cls._resolve_account(
            seller_id=seller_id,
            account_id=account_id,
            proposal_kind=proposal_kind,
            adapter=adapter,
            credentials=credentials,
            now=now,
            write=True,
        )
        lock_file = try_account_operation_lock(account_id)
        if lock_file is None:
            raise MarketplaceCommercialBusy("Кабинет Ozon занят другой операцией")
        current_time = now or datetime.utcnow()
        proposal_ids = [item["proposal_id"] for item in approval_items]
        try:
            db.session.expire_all()
            proposals = load_and_validate()
            cls._require_not_quarantined(proposals)
            live_states = cls._read_live_states(
                proposals=proposals,
                adapter=resolved_adapter,
                credentials=resolved_credentials,
            )
            drifted = [
                proposal
                for proposal in proposals
                if cls._fingerprint(live_states[proposal.id])
                != proposal.baseline_fingerprint
            ]
            if drifted:
                for proposal in drifted:
                    cls._set_baseline_drift(
                        proposal=proposal,
                        reviewer_id=reviewer,
                        now=current_time,
                    )
                db.session.commit()
                raise MarketplaceCommercialConflict(
                    "Live state части batch изменился; ни один write не отправлен"
                )

            operations = [
                cls._build_operation(
                    proposal=proposal,
                    reviewer_id=reviewer,
                    now=current_time,
                )
                for proposal in proposals
            ]
            db.session.commit()
            proposals = [
                cls._owned_proposal(
                    seller_id=seller_id,
                    proposal_id=proposal_id,
                )
                for proposal_id in proposal_ids
            ]
            operations = [
                cls._owned_operation(
                    seller_id=seller_id,
                    operation_id=proposal.operation_id,
                )
                for proposal in proposals
            ]
            cls._submit_batch_locked(
                operations=operations,
                proposals=proposals,
                adapter=resolved_adapter,
                credentials=resolved_credentials,
                now=current_time,
            )
            reconcile_proposals = []
            reconcile_operations = []
            for proposal_id in proposal_ids:
                current_proposal = cls._owned_proposal(
                    seller_id=seller_id,
                    proposal_id=proposal_id,
                )
                current_operation = cls._owned_operation(
                    seller_id=seller_id,
                    operation_id=current_proposal.operation_id,
                )
                if current_operation.status in {"submitted", "polling", "uncertain"}:
                    reconcile_proposals.append(current_proposal)
                    reconcile_operations.append(current_operation)
            if reconcile_operations:
                cls._reconcile_batch_locked(
                    operations=reconcile_operations,
                    proposals=reconcile_proposals,
                    adapter=resolved_adapter,
                    credentials=resolved_credentials,
                    now=current_time,
                )
            return [
                cls._owned_proposal(
                    seller_id=seller_id,
                    proposal_id=proposal_id,
                )
                for proposal_id in proposal_ids
            ]
        except (StaleDataError, IntegrityError):
            db.session.rollback()
            raise MarketplaceCommercialConflict(
                "Один из proposals уже изменился; batch write не начат"
            ) from None
        finally:
            release_account_operation_lock(lock_file)

    @classmethod
    def approve_proposal(
        cls,
        *,
        seller_id: int,
        proposal_id: int,
        expected_version: int,
        reviewed_by_user_id: int,
        adapter=None,
        credentials: Optional[MarketplaceCredentials] = None,
        now: Optional[datetime] = None,
    ) -> MarketplaceCommercialProposal:
        expected_version = cls._positive_integer(expected_version, "expected_version")
        reviewer = cls._validate_user(
            seller_id=seller_id,
            user_id=reviewed_by_user_id,
        )
        proposal = cls._owned_proposal(seller_id=seller_id, proposal_id=proposal_id)
        if proposal.version != expected_version or proposal.status != "pending_review":
            raise MarketplaceCommercialConflict(
                "Proposal уже изменился; обновите страницу"
            )
        cls._require_available_target(proposal)
        _, resolved_adapter, resolved_credentials = cls._resolve_account(
            seller_id=seller_id,
            account_id=proposal.account_id,
            proposal_kind=proposal.proposal_kind,
            adapter=adapter,
            credentials=credentials,
            now=now,
            write=True,
        )
        lock_file = try_account_operation_lock(proposal.account_id)
        if lock_file is None:
            raise MarketplaceCommercialBusy("Кабинет Ozon занят другой операцией")
        current_time = now or datetime.utcnow()
        try:
            db.session.expire_all()
            proposal = cls._owned_proposal(
                seller_id=seller_id,
                proposal_id=proposal_id,
            )
            if proposal.version != expected_version or proposal.status != "pending_review":
                raise MarketplaceCommercialConflict(
                    "Proposal уже изменился; обновите страницу"
                )
            cls._require_available_target(proposal)
            cls._require_not_quarantined([proposal])
            live = cls._read_live_state(
                proposal=proposal,
                adapter=resolved_adapter,
                credentials=resolved_credentials,
            )
            if cls._fingerprint(live) != proposal.baseline_fingerprint:
                cls._set_baseline_drift(
                    proposal=proposal,
                    reviewer_id=reviewer,
                    now=current_time,
                )
                db.session.commit()
                return proposal
            operation = cls._create_operation(
                proposal=proposal,
                reviewer_id=reviewer,
                now=current_time,
            )
            proposal = cls._owned_proposal(
                seller_id=seller_id,
                proposal_id=proposal_id,
            )
            operation = cls._owned_operation(
                seller_id=seller_id,
                operation_id=operation.id,
            )
            cls._submit_locked(
                operation=operation,
                proposal=proposal,
                adapter=resolved_adapter,
                credentials=resolved_credentials,
                now=current_time,
            )
            operation = cls._owned_operation(
                seller_id=seller_id,
                operation_id=operation.id,
            )
            proposal = cls._owned_proposal(
                seller_id=seller_id,
                proposal_id=proposal_id,
            )
            if operation.status in {"submitted", "polling", "uncertain"}:
                cls._reconcile_locked(
                    operation=operation,
                    proposal=proposal,
                    adapter=resolved_adapter,
                    credentials=resolved_credentials,
                    now=current_time,
                )
            return cls._owned_proposal(
                seller_id=seller_id,
                proposal_id=proposal_id,
            )
        except (StaleDataError, IntegrityError):
            db.session.rollback()
            raise MarketplaceCommercialConflict(
                "Proposal уже изменился; обновите страницу"
            ) from None
        finally:
            release_account_operation_lock(lock_file)

    @classmethod
    def poll_operation(
        cls,
        *,
        seller_id: int,
        operation_id: int,
        adapter=None,
        credentials: Optional[MarketplaceCredentials] = None,
        now: Optional[datetime] = None,
        allow_submission: bool = False,
    ) -> MarketplaceOperation:
        if not isinstance(allow_submission, bool):
            raise MarketplaceCommercialValidationError(
                "allow_submission должен быть boolean"
            )
        operation = cls._owned_operation(
            seller_id=seller_id,
            operation_id=operation_id,
        )
        if operation.status in cls.TERMINAL_OPERATION_STATUSES:
            return operation
        proposal = MarketplaceCommercialProposal.query.filter_by(
            seller_id=seller_id,
            operation_id=operation.id,
        ).first()
        if proposal is None:
            raise MarketplaceCommercialConflict(
                "Commercial operation не связана с proposal"
            )
        lock_file = try_account_operation_lock(operation.account_id)
        if lock_file is None:
            raise MarketplaceCommercialBusy("Кабинет Ozon занят другой операцией")
        current_time = now or datetime.utcnow()
        try:
            db.session.expire_all()
            operation = cls._owned_operation(
                seller_id=seller_id,
                operation_id=operation_id,
            )
            if operation.status in cls.TERMINAL_OPERATION_STATUSES:
                return operation
            proposal = MarketplaceCommercialProposal.query.filter_by(
                seller_id=seller_id, operation_id=operation.id,
            ).first()
            if operation.status == "queued" and allow_submission:
                if cls._stop_quarantined_batch(
                    [operation], [proposal], now=current_time,
                ):
                    return cls._owned_operation(
                        seller_id=seller_id, operation_id=operation_id,
                    )
            if provider_read_deferred(operation, current_time):
                return operation
            if operation.status == "queued" and not allow_submission:
                return operation
            _, resolved_adapter, resolved_credentials = cls._resolve_account(
                seller_id=seller_id,
                account_id=operation.account_id,
                proposal_kind=proposal.proposal_kind,
                adapter=adapter,
                credentials=credentials,
                now=current_time,
                write=operation.status == "queued" and allow_submission,
            )
            if operation.status == "queued":
                cls._submit_locked(
                    operation=operation,
                    proposal=proposal,
                    adapter=resolved_adapter,
                    credentials=resolved_credentials,
                    now=current_time,
                )
            operation = cls._owned_operation(
                seller_id=seller_id,
                operation_id=operation_id,
            )
            proposal = cls._owned_proposal(
                seller_id=seller_id,
                proposal_id=proposal.id,
            )
            if operation.status not in cls.TERMINAL_OPERATION_STATUSES:
                cls._reconcile_locked(
                    operation=operation,
                    proposal=proposal,
                    adapter=resolved_adapter,
                    credentials=resolved_credentials,
                    now=current_time,
                )
            return cls._owned_operation(
                seller_id=seller_id,
                operation_id=operation_id,
            )
        finally:
            release_account_operation_lock(lock_file)

    @classmethod
    def poll_due_operations(
        cls,
        *,
        limit: int = 20,
        now: Optional[datetime] = None,
        allow_submission: bool = False,
    ) -> dict:
        limit = cls._positive_integer(limit, "limit")
        if limit > 100:
            raise MarketplaceCommercialValidationError("limit не может быть больше 100")
        if not isinstance(allow_submission, bool):
            raise MarketplaceCommercialValidationError(
                "allow_submission должен быть boolean"
            )
        current_time = now or datetime.utcnow()
        statuses = {"submitting", "submitted", "polling", "uncertain"}
        if allow_submission:
            statuses.add("queued")
        rows = MarketplaceOperation.query.filter(
            MarketplaceOperation.operation_kind.in_(cls.COMMERCIAL_OPERATION_KINDS),
            MarketplaceOperation.status.in_(statuses),
            write_quarantine.automatic_reconciliation_allowed(),
            MarketplaceOperation.next_poll_at.isnot(None),
            MarketplaceOperation.next_poll_at <= current_time,
        ).order_by(
            MarketplaceOperation.next_poll_at.asc(),
            MarketplaceOperation.id.asc(),
        ).limit(limit).all()
        result = {"selected": len(rows), "processed": 0, "busy": 0, "failed": 0}
        for row in rows:
            try:
                cls.poll_operation(
                    seller_id=row.seller_id,
                    operation_id=row.id,
                    now=current_time,
                    allow_submission=allow_submission,
                )
                result["processed"] += 1
            except MarketplaceCommercialBusy:
                db.session.rollback()
                result["busy"] += 1
            except Exception:
                db.session.rollback()
                result["failed"] += 1
        return result

    @classmethod
    def get_proposal(
        cls,
        *,
        seller_id: int,
        proposal_id: int,
    ) -> MarketplaceCommercialProposal:
        return cls._owned_proposal(seller_id=seller_id, proposal_id=proposal_id)

    @classmethod
    def list_proposals(
        cls,
        *,
        seller_id: int,
        account_id: Optional[int] = None,
        proposal_kind: Optional[str] = None,
        status: Optional[str] = None,
        page: int = 1,
        per_page: int = 50,
        proposal_ids: Optional[Any] = None,
    ):
        seller_id = cls._positive_integer(seller_id, "seller_id")
        page = cls._positive_integer(page, "page")
        per_page = cls._positive_integer(per_page, "per_page")
        if per_page > 100:
            raise MarketplaceCommercialValidationError(
                "per_page не может быть больше 100"
            )
        exact_ids = None
        if proposal_ids is not None:
            if (
                account_id is None
                or proposal_kind != "price"
                or status != "pending_review"
                or page != 1
                or not isinstance(proposal_ids, list)
                or not 1 <= len(proposal_ids) <= cls.MAX_BATCH_APPROVAL
            ):
                raise MarketplaceCommercialValidationError(
                    "Точный набор требует магазин, price, pending_review и первую страницу"
                )
            exact_ids = [
                cls._positive_integer(value, f"proposal_ids[{index}]")
                for index, value in enumerate(proposal_ids)
            ]
            if len(set(exact_ids)) != len(exact_ids):
                raise MarketplaceCommercialValidationError(
                    "proposal_ids содержит повторяющиеся заявки"
                )
            account = SellerMarketplaceAccount.query.join(Marketplace).filter(
                SellerMarketplaceAccount.id == account_id,
                SellerMarketplaceAccount.seller_id == seller_id,
                Marketplace.code == "ozon",
            ).first()
            if account is None:
                raise MarketplaceCommercialNotFound("Магазин Ozon не найден")
            if per_page not in {50, len(exact_ids)}:
                raise MarketplaceCommercialValidationError(
                    "Точный набор не принимает произвольный per_page"
                )
            per_page = len(exact_ids)
        query = MarketplaceCommercialProposal.query.options(
            joinedload(MarketplaceCommercialProposal.marketplace),
            joinedload(MarketplaceCommercialProposal.account),
            joinedload(MarketplaceCommercialProposal.listing),
            joinedload(MarketplaceCommercialProposal.warehouse),
            joinedload(MarketplaceCommercialProposal.operation).joinedload(MarketplaceOperation.snapshot),
        ).filter(MarketplaceCommercialProposal.seller_id == seller_id)
        if account_id is not None:
            account_id = cls._positive_integer(account_id, "account_id")
            query = query.filter(MarketplaceCommercialProposal.account_id == account_id)
        if proposal_kind is not None:
            if proposal_kind not in {"price", "stock"}:
                raise MarketplaceCommercialValidationError(
                    "Неизвестный proposal_kind"
                )
            query = query.filter(
                MarketplaceCommercialProposal.proposal_kind == proposal_kind
            )
        if status is not None:
            allowed = {
                "pending_review", "approved", "rejected", "applying", "applied",
                "failed", "conflict", "uncertain", "cancelled",
            }
            if not isinstance(status, str) or status not in allowed:
                raise MarketplaceCommercialValidationError("Неизвестный status")
            query = query.filter(MarketplaceCommercialProposal.status == status)
        if exact_ids is not None:
            query = query.filter(MarketplaceCommercialProposal.id.in_(exact_ids))
        pagination = query.order_by(
            MarketplaceCommercialProposal.created_at.desc(),
            MarketplaceCommercialProposal.id.desc(),
        ).paginate(page=page, per_page=per_page, error_out=False)
        if exact_ids is not None and (
            pagination.total != len(exact_ids)
            or {item.id for item in pagination.items} != set(exact_ids)
        ):
            raise MarketplaceCommercialNotFound(
                "Одна или несколько заявок не найдены в этом магазине"
            )
        return pagination
