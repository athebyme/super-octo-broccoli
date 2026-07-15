"""Reviewed, durable Ozon price and FBS stock mutation workflow."""

from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
from typing import Any, Dict, Mapping, Optional, Tuple
import uuid

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
    db,
)
from services.marketplace_adapters import MarketplaceCredentials
from services.marketplace_operation_locks import (
    release_account_operation_lock,
    try_account_operation_lock,
)
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


class MarketplaceCommercialBusy(MarketplaceCommercialError):
    status_code = 409
    code = "marketplace_commercial_busy"


class MarketplaceCommercialConfigurationError(MarketplaceCommercialError):
    status_code = 503
    code = "marketplace_commercial_configuration_error"


class MarketplaceCommercialUpstreamError(MarketplaceCommercialError):
    status_code = 502
    code = "marketplace_commercial_upstream_error"


class MarketplaceCommercialService:
    MAX_JSON_BYTES = 65_536
    MAX_PRICE_READ_PAGES = 10
    MAX_PRICE_CHANGE_PCT = Decimal("50")
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
        try:
            return cls._read_price_state_unwrapped(
                listing=listing,
                adapter=adapter,
                credentials=credentials,
            )
        except OzonAPIError:
            raise MarketplaceCommercialUpstreamError(
                "Не удалось прочитать актуальную цену Ozon"
            ) from None
        except OzonCommercialContractError:
            raise MarketplaceCommercialUpstreamError(
                "Ozon вернул некорректный ответ чтения цены"
            ) from None

    @classmethod
    def _read_price_state_unwrapped(
        cls,
        *,
        listing: MarketplaceListing,
        adapter,
        credentials: MarketplaceCredentials,
    ) -> dict:
        cursor = ""
        seen_cursors = set()
        items = []
        expected_total = None
        for _page_number in range(cls.MAX_PRICE_READ_PAGES):
            page = OzonPriceContract.normalize_read_page(
                adapter.read_prices(
                    credentials,
                    {
                        "filter": {
                            "product_id": [int(listing.external_product_id)],
                            "visibility": "ALL",
                        },
                        "cursor": cursor,
                        "limit": 100,
                    },
                )
            )
            if expected_total is None:
                expected_total = page["total"]
            elif page["total"] != expected_total:
                raise MarketplaceCommercialConflict(
                    "Ozon изменил total во время price preflight"
                )
            items.extend(page["items"])
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
        matching = [
            item for item in items
            if item["offer_id"] == listing.offer_id
            and item["product_id"] == listing.external_product_id
        ]
        if expected_total != len(items) or len(matching) != 1 or len(items) != 1:
            raise MarketplaceCommercialConflict(
                "Ozon price preflight не вернул ровно один ожидаемый товар"
            )
        item = matching[0]
        return {
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

    @classmethod
    def _read_stock_state(
        cls,
        *,
        listing: MarketplaceListing,
        warehouse: MarketplaceWarehouse,
        adapter,
        credentials: MarketplaceCredentials,
    ) -> dict:
        try:
            stocks = MarketplaceWarehouseService._fetch_listing_stocks(
                listing=listing,
                adapter=adapter,
                credentials=credentials,
            )
        except (OzonAPIError, OzonCommercialContractError, MarketplaceWarehouseError):
            raise MarketplaceCommercialUpstreamError(
                "Не удалось прочитать точный остаток Ozon по складу"
            ) from None
        item = stocks.get(warehouse.external_warehouse_id)
        if item is None:
            raise MarketplaceCommercialConflict(
                "На выбранном складе Ozon нет точной строки этого товара"
            )
        return {
            "kind": "stock",
            "offer_id": item["offer_id"],
            "product_id": item["product_id"],
            "warehouse_id": item["warehouse_id"],
            "sku": item["sku"],
            "stock": item["free_stock"],
        }

    @classmethod
    def _read_live_state(
        cls,
        *,
        proposal: MarketplaceCommercialProposal,
        adapter,
        credentials: MarketplaceCredentials,
    ) -> dict:
        if proposal.proposal_kind == "price":
            return cls._read_price_state(
                listing=proposal.listing,
                adapter=adapter,
                credentials=credentials,
            )
        return cls._read_stock_state(
            listing=proposal.listing,
            warehouse=proposal.warehouse,
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
            pricing = PricingSettings.query.filter_by(
                seller_id=listing.seller_id
            ).first()
            min_profit = Decimal(str(
                pricing.min_profit
                if pricing is not None and pricing.min_profit is not None
                else 0
            ))
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
    ) -> MarketplaceCommercialProposal:
        baseline_fingerprint = cls._fingerprint(baseline)
        proposed_fingerprint = cls._fingerprint(proposed)
        request_fingerprint = cls._fingerprint({
            "proposal_kind": proposal_kind,
            "listing_id": listing.id,
            "warehouse_id": warehouse.id if warehouse else None,
            "baseline": baseline,
            "proposed": proposed,
            "source": source,
            "rollback_of_operation_id": rollback_of_operation_id,
        })
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
            baseline = cls._read_price_state(
                listing=listing,
                adapter=resolved_adapter,
                credentials=resolved_credentials,
            )
            item = OzonPriceContract.build_item(
                offer_id=listing.offer_id,
                product_id=listing.external_product_id,
                price=price,
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
            )
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
    def _payload_for_proposal(cls, proposal: MarketplaceCommercialProposal) -> dict:
        proposed = cls._load_object(proposal.proposed_state_json, "proposed_state")
        if cls._fingerprint(proposed) != proposal.proposed_fingerprint:
            raise MarketplaceCommercialConflict("Proposal fingerprint не совпадает")
        if proposal.proposal_kind == "price":
            return OzonPriceContract.build_payload([{
                "offer_id": proposed.get("offer_id"),
                "product_id": proposed.get("product_id"),
                "price": proposed.get("price"),
                "old_price": proposed.get("old_price", "0"),
                "currency_code": proposed.get("currency_code"),
            }])
        return OzonStockContract.build_payload([{
            "offer_id": proposed.get("offer_id"),
            "product_id": proposed.get("product_id"),
            "warehouse_id": proposed.get("warehouse_id"),
            "stock": proposed.get("stock"),
        }])

    @classmethod
    def _create_operation(
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
            rollback_status="available",
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
        db.session.commit()
        return operation

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
        if proposal.source == "rollback" and proposal.rollback_of_operation is not None:
            original_snapshot = proposal.rollback_of_operation.snapshot
            if original_snapshot is not None:
                original_snapshot.rollback_status = "failed"
                original_snapshot.rollback_error_code = operation.error_code
                original_snapshot.rollback_error_message = operation.error_message
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
        operation.reconcile_count += 1
        operation.last_polled_at = now
        try:
            live = cls._read_live_state(
                proposal=proposal,
                adapter=adapter,
                credentials=credentials,
            )
            live_fingerprint = cls._fingerprint(live)
        except (MarketplaceCommercialUpstreamError, MarketplaceCommercialConflict):
            if operation.deadline_at and now >= operation.deadline_at:
                operation.status = "uncertain"
                operation.next_poll_at = None
                operation.error_code = "commercial_reconciliation_deadline"
                operation.error_message = (
                    "Не удалось подтвердить Ozon write за отведённое время"
                )
                proposal.status = "uncertain"
                proposal.error_code = operation.error_code
                proposal.error_message = operation.error_message
            else:
                operation.next_poll_at = now + cls.POLL_INTERVAL
                if operation.status != "uncertain":
                    operation.status = "polling"
                    operation.error_code = "commercial_reconciliation_read_failed"
                    operation.error_message = (
                        "Временная live-сверка Ozon не удалась; write не повторяется"
                    )
            db.session.commit()
            return
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
            if snapshot is None:
                raise MarketplaceCommercialConflict(
                    "Commercial operation snapshot отсутствует"
                )
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
            db.session.commit()
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
            db.session.commit()
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
        if operation.snapshot is not None:
            operation.snapshot.rollback_status = "conflict"
        if proposal.source == "rollback" and proposal.rollback_of_operation is not None:
            original_snapshot = proposal.rollback_of_operation.snapshot
            if original_snapshot is not None:
                original_snapshot.rollback_status = "conflict"
                original_snapshot.rollback_error_code = operation.error_code
                original_snapshot.rollback_error_message = operation.error_message
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
            live = cls._read_live_state(
                proposal=proposal,
                adapter=resolved_adapter,
                credentials=resolved_credentials,
            )
            if cls._fingerprint(live) != proposal.baseline_fingerprint:
                proposal.status = "conflict"
                proposal.reviewed_by_user_id = reviewer
                proposal.reviewed_at = current_time
                proposal.error_code = "commercial_baseline_drift"
                proposal.error_message = (
                    "Live state изменился после создания proposal; write не отправлен"
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
        _, resolved_adapter, resolved_credentials = cls._resolve_account(
            seller_id=seller_id,
            account_id=operation.account_id,
            proposal_kind=proposal.proposal_kind,
            adapter=adapter,
            credentials=credentials,
            now=now,
            write=operation.status == "queued" and allow_submission,
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
            proposal = MarketplaceCommercialProposal.query.filter_by(
                seller_id=seller_id,
                operation_id=operation.id,
            ).first()
            if operation.status == "queued":
                if not allow_submission:
                    return operation
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
        status: Optional[str] = None,
        page: int = 1,
        per_page: int = 50,
    ):
        seller_id = cls._positive_integer(seller_id, "seller_id")
        page = cls._positive_integer(page, "page")
        per_page = cls._positive_integer(per_page, "per_page")
        if per_page > 100:
            raise MarketplaceCommercialValidationError(
                "per_page не может быть больше 100"
            )
        query = MarketplaceCommercialProposal.query.options(
            joinedload(MarketplaceCommercialProposal.marketplace),
            joinedload(MarketplaceCommercialProposal.account),
            joinedload(MarketplaceCommercialProposal.listing),
            joinedload(MarketplaceCommercialProposal.warehouse),
            joinedload(MarketplaceCommercialProposal.operation),
        ).filter(MarketplaceCommercialProposal.seller_id == seller_id)
        if account_id is not None:
            account_id = cls._positive_integer(account_id, "account_id")
            query = query.filter(MarketplaceCommercialProposal.account_id == account_id)
        if status is not None:
            allowed = {
                "pending_review", "approved", "rejected", "applying", "applied",
                "failed", "conflict", "uncertain", "cancelled",
            }
            if not isinstance(status, str) or status not in allowed:
                raise MarketplaceCommercialValidationError("Неизвестный status")
            query = query.filter(MarketplaceCommercialProposal.status == status)
        return query.order_by(
            MarketplaceCommercialProposal.created_at.desc(),
            MarketplaceCommercialProposal.id.desc(),
        ).paginate(page=page, per_page=per_page, error_out=False)
