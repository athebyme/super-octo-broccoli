"""Account-scoped, read-only Ozon postings/returns synchronization."""

from datetime import date, datetime, timedelta
from hashlib import sha256
from typing import Any, Dict, Mapping, Optional, Tuple
import fcntl
import json
import os
import tempfile

from sqlalchemy import asc, desc, func, or_
from sqlalchemy.exc import IntegrityError

from models import (
    MarketplaceCancellation,
    MarketplaceCredentialEncryptionError,
    MarketplaceFulfillmentSync,
    MarketplaceListing,
    MarketplacePosting,
    MarketplacePostingItem,
    MarketplacePostingStatusEvent,
    MarketplaceReturn,
    SellerMarketplaceAccount,
    db,
)
from services.marketplace_accounts import (
    MarketplaceAccountNotFound,
    MarketplaceAccountService,
)
from services.marketplace_adapters import MarketplaceCredentials, get_marketplace_registry
from services.marketplace_adapters.base import MarketplaceAdapterError
from services.ozon_api_client import OzonAPIError
from services.ozon_fulfillment_contracts import (
    POSTING_PAGE_LIMIT,
    RETURN_PAGE_LIMIT,
    OzonFulfillmentContractError,
    build_conditional_cancellation_request,
    build_posting_request,
    build_return_request,
    build_rfbs_return_request,
    normalize_conditional_cancellation_response,
    normalize_posting_response,
    normalize_return_response,
    normalize_rfbs_return_response,
)


class MarketplaceFulfillmentError(RuntimeError):
    status_code = 400
    code = "marketplace_fulfillment_error"


class MarketplaceFulfillmentValidationError(MarketplaceFulfillmentError):
    code = "invalid_marketplace_fulfillment_request"


class MarketplaceFulfillmentNotFound(MarketplaceFulfillmentError):
    status_code = 404
    code = "marketplace_fulfillment_not_found"


class MarketplaceFulfillmentConfigurationError(MarketplaceFulfillmentError):
    status_code = 409
    code = "marketplace_fulfillment_not_ready"


class MarketplaceFulfillmentBusy(MarketplaceFulfillmentError):
    status_code = 409
    code = "marketplace_fulfillment_busy"


class MarketplaceFulfillmentProtocolError(MarketplaceFulfillmentError):
    status_code = 502
    code = "ozon_fulfillment_protocol_error"


class MarketplaceFulfillmentService:
    CONTRACT_VERSION = "ozon-fulfillment-v1"
    SUPPORTED_PERIODS = {"7d": 7, "30d": 30}
    PHASES = (
        "fbs_postings",
        "fbo_postings",
        "returns",
        "rfbs_returns",
        "conditional_cancellations",
    )
    CACHE_TTL = timedelta(minutes=15)
    STALE_RUNNING_AFTER = timedelta(minutes=30)
    MAX_PAGES_PER_CALL = 10
    LOCK_DIRECTORY = "seller-hub-ozon-fulfillment-locks"

    @staticmethod
    def _positive_integer(value: Any, field_name: str, *, maximum: Optional[int] = None) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise MarketplaceFulfillmentValidationError(
                f"{field_name} должен быть положительным целым числом"
            )
        if maximum is not None and value > maximum:
            raise MarketplaceFulfillmentValidationError(
                f"{field_name} превышает лимит {maximum}"
            )
        return value

    @classmethod
    def _period(cls, period_code: Any, *, today: date) -> Tuple[str, date, date]:
        if not isinstance(period_code, str) or period_code not in cls.SUPPORTED_PERIODS:
            raise MarketplaceFulfillmentValidationError(
                "Для заказов Ozon доступны периоды 7d и 30d"
            )
        end = today
        start = end - timedelta(days=cls.SUPPORTED_PERIODS[period_code] - 1)
        return period_code, start, end

    @staticmethod
    def _stable_json(value: Any) -> str:
        def encode(item: Any) -> Any:
            if isinstance(item, datetime):
                return item.isoformat()
            if hasattr(item, "as_tuple"):
                return format(item, "f")
            raise TypeError(type(item).__name__)

        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=encode,
        )

    @classmethod
    def _fingerprint(cls, value: Any) -> str:
        return sha256(cls._stable_json(value).encode("utf-8")).hexdigest()

    @classmethod
    def _run_fingerprint(cls, period_start: date, period_end: date) -> str:
        return cls._fingerprint({
            "contract": cls.CONTRACT_VERSION,
            "period_start": period_start.isoformat(),
            "period_end": period_end.isoformat(),
            "phases": list(cls.PHASES),
            "endpoints": {
                "fbs_postings": "/v4/posting/fbs/list",
                "fbo_postings": "/v3/posting/fbo/list",
                "returns": "/v1/returns/list",
                "rfbs_returns": "/v2/returns/rfbs/list",
                "conditional_cancellations": "/v2/conditional-cancellation/list",
            },
        })

    @staticmethod
    def _try_claim(account_id: int):
        directory = os.path.join(tempfile.gettempdir(), MarketplaceFulfillmentService.LOCK_DIRECTORY)
        os.makedirs(directory, mode=0o700, exist_ok=True)
        lock_file = open(
            os.path.join(directory, f"account-{int(account_id)}.lock"),
            "a+",
            encoding="ascii",
        )
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock_file.close()
            return None
        return lock_file

    @staticmethod
    def _release_claim(lock_file) -> None:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        finally:
            lock_file.close()

    @classmethod
    def _account_adapter_credentials(
        cls,
        *,
        seller_id: int,
        account_id: int,
        adapter=None,
        credentials: Optional[MarketplaceCredentials] = None,
        now: datetime,
    ) -> Tuple[SellerMarketplaceAccount, Any, MarketplaceCredentials]:
        seller_id = cls._positive_integer(seller_id, "seller_id")
        account_id = cls._positive_integer(account_id, "account_id")
        try:
            account = MarketplaceAccountService.get_owned_account(
                seller_id=seller_id,
                account_id=account_id,
                marketplace_code="ozon",
            )
        except MarketplaceAccountNotFound:
            raise MarketplaceFulfillmentNotFound("Кабинет Ozon не найден") from None
        if not account.is_active or account.connection_status != "connected":
            raise MarketplaceFulfillmentConfigurationError(
                "Кабинет Ozon должен быть активен и пройти проверку подключения"
            )
        if account.credential_expires_at and account.credential_expires_at <= now:
            raise MarketplaceFulfillmentConfigurationError(
                "Срок действия API key Ozon истёк"
            )
        if (adapter is None) != (credentials is None):
            raise MarketplaceFulfillmentValidationError(
                "adapter and credentials must be injected together"
            )
        if adapter is None:
            if not account.has_credentials:
                raise MarketplaceFulfillmentConfigurationError(
                    "В кабинете Ozon нет сохранённого API key"
                )
            try:
                secret = account.get_credentials()
                credentials = MarketplaceCredentials(
                    external_account_id=account.external_account_id,
                    api_key=secret["api_key"],
                )
                del secret
            except (KeyError, ValueError, MarketplaceCredentialEncryptionError):
                raise MarketplaceFulfillmentConfigurationError(
                    "Credentials кабинета Ozon невозможно прочитать"
                ) from None
            adapter = get_marketplace_registry().get("ozon")
        try:
            adapter.require_capability("orders_read")
        except MarketplaceAdapterError as exc:
            raise MarketplaceFulfillmentConfigurationError(str(exc)) from None
        return account, adapter, credentials

    @classmethod
    def _listing_maps(cls, *, seller_id: int, account_id: int) -> Tuple[Dict[str, MarketplaceListing], Dict[str, MarketplaceListing]]:
        offer_map: Dict[str, MarketplaceListing] = {}
        sku_map: Dict[str, MarketplaceListing] = {}
        listings = MarketplaceListing.query.filter(
            MarketplaceListing.seller_id == seller_id,
            MarketplaceListing.account_id == account_id,
        ).order_by(MarketplaceListing.id.asc()).all()
        for listing in listings:
            if listing.offer_id:
                previous = offer_map.get(listing.offer_id)
                if previous is not None and previous.id != listing.id:
                    raise MarketplaceFulfillmentConfigurationError(
                        "Один offer_id связан с несколькими карточками кабинета"
                    )
                offer_map[listing.offer_id] = listing
            candidates = {listing.primary_sku} if listing.primary_sku else set()
            try:
                raw_identifiers = json.loads(listing.identifiers_json or "{}")
            except (TypeError, json.JSONDecodeError):
                raw_identifiers = {}
            if isinstance(raw_identifiers, dict):
                for key in ("sku", "primary_sku", "fbo_sku", "fbs_sku"):
                    value = raw_identifiers.get(key)
                    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                        candidates.add(str(value))
                    elif isinstance(value, str) and value.isdigit() and not value.startswith("0"):
                        candidates.add(value)
            for sku in candidates:
                if not sku:
                    continue
                previous = sku_map.get(str(sku))
                if previous is not None and previous.id != listing.id:
                    raise MarketplaceFulfillmentConfigurationError(
                        "Один Ozon SKU связан с несколькими локальными карточками"
                    )
                sku_map[str(sku)] = listing
        return offer_map, sku_map

    @staticmethod
    def _match_listing(
        product: Mapping[str, Any],
        offer_map: Mapping[str, MarketplaceListing],
        sku_map: Mapping[str, MarketplaceListing],
    ) -> Optional[MarketplaceListing]:
        offer_id = product.get("offer_id")
        if offer_id and offer_id in offer_map:
            return offer_map[offer_id]
        sku = product.get("sku")
        return sku_map.get(sku) if sku else None

    @classmethod
    def _latest_completed(cls, *, seller_id: int, account_id: int, period_code: str) -> Optional[MarketplaceFulfillmentSync]:
        return MarketplaceFulfillmentSync.query.filter(
            MarketplaceFulfillmentSync.seller_id == seller_id,
            MarketplaceFulfillmentSync.account_id == account_id,
            MarketplaceFulfillmentSync.period_code == period_code,
            MarketplaceFulfillmentSync.status == "completed",
        ).order_by(
            MarketplaceFulfillmentSync.completed_at.desc(),
            MarketplaceFulfillmentSync.id.desc(),
        ).first()

    @classmethod
    def _fresh_completed(
        cls,
        *,
        seller_id: int,
        account_id: int,
        period_code: str,
        period_start: date,
        period_end: date,
        now: datetime,
    ) -> Optional[MarketplaceFulfillmentSync]:
        run = cls._latest_completed(
            seller_id=seller_id,
            account_id=account_id,
            period_code=period_code,
        )
        if (
            run is not None
            and run.period_start == period_start
            and run.period_end == period_end
            and run.completed_at
            and run.completed_at >= now - cls.CACHE_TTL
            and run.contract_version == cls.CONTRACT_VERSION
            and run.request_fingerprint
            == cls._run_fingerprint(period_start, period_end)
        ):
            return run
        return None

    @classmethod
    def _running_run(
        cls,
        *,
        seller_id: int,
        account_id: int,
        now: datetime,
    ) -> Optional[MarketplaceFulfillmentSync]:
        run = MarketplaceFulfillmentSync.query.filter(
            MarketplaceFulfillmentSync.seller_id == seller_id,
            MarketplaceFulfillmentSync.account_id == account_id,
            MarketplaceFulfillmentSync.status == "running",
        ).order_by(MarketplaceFulfillmentSync.id.desc()).first()
        if run is None:
            return None
        heartbeat = run.last_page_at or run.started_at
        expected = cls._run_fingerprint(run.period_start, run.period_end)
        if heartbeat and heartbeat < now - cls.STALE_RUNNING_AFTER:
            run.status = "failed"
            run.error_code = "fulfillment_sync_interrupted"
            run.error_message = "Синхронизация прервана до завершения страницы"
            run.completed_at = now
            db.session.commit()
            return None
        if (
            run.contract_version != cls.CONTRACT_VERSION
            or run.request_fingerprint != expected
            or run.next_cursor is None
        ):
            run.status = "failed"
            run.error_code = "fulfillment_contract_drift"
            run.error_message = "Контракт незавершённой синхронизации изменился"
            run.completed_at = now
            db.session.commit()
            return None
        return run

    @classmethod
    def _create_run(
        cls,
        *,
        account: SellerMarketplaceAccount,
        period_code: str,
        period_start: date,
        period_end: date,
        now: datetime,
    ) -> MarketplaceFulfillmentSync:
        run = MarketplaceFulfillmentSync(
            seller_id=account.seller_id,
            marketplace_id=account.marketplace_id,
            account_id=account.id,
            period_code=period_code,
            period_start=period_start,
            period_end=period_end,
            status="running",
            phase=cls.PHASES[0],
            next_offset=0,
            next_cursor="0",
            contract_version=cls.CONTRACT_VERSION,
            request_fingerprint=cls._run_fingerprint(period_start, period_end),
            started_at=now,
        )
        db.session.add(run)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            existing = cls._running_run(
                seller_id=account.seller_id,
                account_id=account.id,
                now=now,
            )
            if existing is None:
                raise
            return existing
        return run

    @classmethod
    def _next_phase(cls, run: MarketplaceFulfillmentSync, now: datetime) -> None:
        index = cls.PHASES.index(run.phase)
        run.next_offset = 0
        run.next_cursor = "0"
        if index + 1 < len(cls.PHASES):
            run.phase = cls.PHASES[index + 1]
        else:
            run.phase = "completed"
            run.status = "completed"
            run.completed_at = now

    @classmethod
    def _upsert_posting(
        cls,
        *,
        run: MarketplaceFulfillmentSync,
        row: Mapping[str, Any],
        offer_map: Mapping[str, MarketplaceListing],
        sku_map: Mapping[str, MarketplaceListing],
        now: datetime,
    ) -> Tuple[int, int]:
        posting = MarketplacePosting.query.filter(
            MarketplacePosting.account_id == run.account_id,
            MarketplacePosting.posting_number == row["posting_number"],
        ).first()
        is_new = posting is None
        previous_status = (posting.status, posting.substatus) if posting is not None else None
        endpoint = (
            "/v4/posting/fbs/list"
            if row["fulfillment_kind"] == "fbs"
            else "/v3/posting/fbo/list"
        )
        fingerprint = cls._fingerprint(row)
        if posting is None:
            posting = MarketplacePosting(
                seller_id=run.seller_id,
                marketplace_id=run.marketplace_id,
                account_id=run.account_id,
                posting_number=row["posting_number"],
                last_seen_at=now,
                source_endpoint=endpoint,
                sync_fingerprint=fingerprint,
                fulfillment_kind=row["fulfillment_kind"],
                status=row["status"],
            )
            db.session.add(posting)
        posting.last_sync_id = run.id
        posting.external_order_id = row["external_order_id"]
        posting.external_order_number = row["external_order_number"]
        posting.fulfillment_kind = row["fulfillment_kind"]
        posting.status = row["status"]
        posting.substatus = row["substatus"]
        posting.upstream_created_at = row["created_at"]
        posting.shipment_at = row["shipment_at"]
        posting.delivered_at = row["delivered_at"]
        posting.cancelled_at = row["cancelled_at"]
        posting.cancellation_reason_code = row["cancellation_reason_code"]
        posting.cancellation_reason = row["cancellation_reason"]
        posting.source_endpoint = endpoint
        posting.sync_fingerprint = fingerprint
        posting.last_seen_at = now
        db.session.flush()

        current_status = (posting.status, posting.substatus)
        if is_new or previous_status != current_status:
            db.session.add(MarketplacePostingStatusEvent(
                posting_id=posting.id,
                seller_id=run.seller_id,
                account_id=run.account_id,
                status=posting.status,
                substatus=posting.substatus,
                event_fingerprint=cls._fingerprint({
                    "status": posting.status,
                    "substatus": posting.substatus,
                    "observed_at": now.isoformat(),
                }),
                observed_at=now,
            ))

        MarketplacePostingItem.query.filter(
            MarketplacePostingItem.posting_id == posting.id
        ).delete(synchronize_session=False)
        matched = unmatched = 0
        for product in row["products"]:
            listing = cls._match_listing(product, offer_map, sku_map)
            if listing is None:
                unmatched += 1
            else:
                matched += 1
            identity_key = cls._fingerprint({
                "offer_id": product["offer_id"],
                "sku": product["sku"],
            })
            db.session.add(MarketplacePostingItem(
                posting_id=posting.id,
                seller_id=run.seller_id,
                account_id=run.account_id,
                listing_id=listing.id if listing else None,
                identity_key=identity_key,
                offer_id=product["offer_id"],
                external_sku=product["sku"],
                name=product["name"],
                quantity=product["quantity"],
                unit_price=product["unit_price"],
                currency=product["currency"],
            ))

        explicitly_cancelled = (
            posting.status.casefold() in {"cancelled", "canceled"}
            or posting.cancelled_at is not None
            or posting.cancellation_reason_code is not None
            or posting.cancellation_reason is not None
        )
        if explicitly_cancelled:
            cancellation_row = {
                "source_kind": f"posting_{posting.fulfillment_kind}",
                "external_cancellation_id": posting.posting_number,
                "posting_number": posting.posting_number,
                "status": posting.status,
                "status_label": posting.substatus,
                "initiator": None,
                "reason_code": posting.cancellation_reason_code,
                "reason": posting.cancellation_reason,
                "requested_at": posting.cancelled_at,
                "resolved_at": posting.cancelled_at,
            }
            cls._upsert_cancellation(
                run=run,
                row=cancellation_row,
                endpoint=endpoint,
                now=now,
                posting=posting,
            )
        return matched, unmatched

    @classmethod
    def _upsert_return(
        cls,
        *,
        run: MarketplaceFulfillmentSync,
        row: Mapping[str, Any],
        offer_map: Mapping[str, MarketplaceListing],
        sku_map: Mapping[str, MarketplaceListing],
        endpoint: str,
        now: datetime,
    ) -> bool:
        product = row["product"]
        listing = cls._match_listing(product, offer_map, sku_map)
        posting = None
        if row["posting_number"]:
            posting = MarketplacePosting.query.filter(
                MarketplacePosting.account_id == run.account_id,
                MarketplacePosting.posting_number == row["posting_number"],
            ).first()
        record = MarketplaceReturn.query.filter(
            MarketplaceReturn.account_id == run.account_id,
            MarketplaceReturn.source_kind == row["source_kind"],
            MarketplaceReturn.external_return_id == row["external_return_id"],
        ).first()
        fulfillment = str(row.get("fulfillment_kind") or "").casefold()
        if "rfbs" in fulfillment:
            fulfillment = "rfbs"
        elif fulfillment == "fbo":
            fulfillment = "fbo"
        elif fulfillment == "fbs":
            fulfillment = "fbs"
        else:
            fulfillment = "unknown"
        fingerprint = cls._fingerprint(row)
        if record is None:
            record = MarketplaceReturn(
                seller_id=run.seller_id,
                marketplace_id=run.marketplace_id,
                account_id=run.account_id,
                source_kind=row["source_kind"],
                external_return_id=row["external_return_id"],
                fulfillment_kind=fulfillment,
                status=row["status"],
                quantity=product["quantity"],
                source_endpoint=endpoint,
                sync_fingerprint=fingerprint,
                last_seen_at=now,
            )
            db.session.add(record)
        record.last_sync_id = run.id
        record.posting_id = posting.id if posting else None
        record.listing_id = listing.id if listing else None
        record.posting_number = row["posting_number"]
        record.external_order_id = row["external_order_id"]
        record.fulfillment_kind = fulfillment
        record.status = row["status"]
        record.status_label = row["status_label"]
        record.reason = row["reason"]
        record.upstream_created_at = row["created_at"]
        record.status_changed_at = row["status_changed_at"]
        record.completed_at = row["completed_at"]
        record.offer_id = product["offer_id"]
        record.external_sku = product["sku"]
        record.product_name = product["name"]
        record.quantity = product["quantity"]
        record.unit_price = product["unit_price"]
        record.currency = product["currency"]
        record.source_endpoint = endpoint
        record.sync_fingerprint = fingerprint
        record.last_seen_at = now
        return listing is not None

    @classmethod
    def _upsert_cancellation(
        cls,
        *,
        run: MarketplaceFulfillmentSync,
        row: Mapping[str, Any],
        endpoint: str,
        now: datetime,
        posting: Optional[MarketplacePosting] = None,
    ) -> None:
        if posting is None:
            posting = MarketplacePosting.query.filter(
                MarketplacePosting.account_id == run.account_id,
                MarketplacePosting.posting_number == row["posting_number"],
            ).first()
        record = MarketplaceCancellation.query.filter(
            MarketplaceCancellation.account_id == run.account_id,
            MarketplaceCancellation.source_kind == row["source_kind"],
            MarketplaceCancellation.external_cancellation_id
            == row["external_cancellation_id"],
        ).first()
        fingerprint = cls._fingerprint(row)
        if record is None:
            record = MarketplaceCancellation(
                seller_id=run.seller_id,
                marketplace_id=run.marketplace_id,
                account_id=run.account_id,
                source_kind=row["source_kind"],
                external_cancellation_id=row["external_cancellation_id"],
                posting_number=row["posting_number"],
                status=row["status"],
                source_endpoint=endpoint,
                sync_fingerprint=fingerprint,
                last_seen_at=now,
            )
            db.session.add(record)
        record.last_sync_id = run.id
        record.posting_id = posting.id if posting else None
        record.posting_number = row["posting_number"]
        record.status = row["status"]
        record.status_label = row["status_label"]
        record.initiator = row["initiator"]
        record.reason_code = row["reason_code"]
        record.reason = row["reason"]
        record.requested_at = row["requested_at"]
        record.resolved_at = row["resolved_at"]
        record.source_endpoint = endpoint
        record.sync_fingerprint = fingerprint
        record.last_seen_at = now

    @classmethod
    def _persist_page(
        cls,
        *,
        run: MarketplaceFulfillmentSync,
        normalized: Mapping[str, Any],
        offer_map: Mapping[str, MarketplaceListing],
        sku_map: Mapping[str, MarketplaceListing],
        now: datetime,
    ) -> None:
        phase = run.phase
        matched = unmatched = 0
        if phase in {"fbs_postings", "fbo_postings"}:
            for row in normalized["rows"]:
                row_matched, row_unmatched = cls._upsert_posting(
                    run=run,
                    row=row,
                    offer_map=offer_map,
                    sku_map=sku_map,
                    now=now,
                )
                matched += row_matched
                unmatched += row_unmatched
            run.posting_count += len(normalized["rows"])
            if normalized["has_next"]:
                run.next_offset = normalized["next_offset"]
            else:
                cls._next_phase(run, now)
        elif phase in {"returns", "rfbs_returns"}:
            endpoint = (
                "/v1/returns/list"
                if phase == "returns"
                else "/v2/returns/rfbs/list"
            )
            for row in normalized["rows"]:
                if cls._upsert_return(
                    run=run,
                    row=row,
                    offer_map=offer_map,
                    sku_map=sku_map,
                    endpoint=endpoint,
                    now=now,
                ):
                    matched += 1
                else:
                    unmatched += 1
            run.return_count += len(normalized["rows"])
            if normalized["has_next"]:
                run.next_cursor = str(normalized["next_last_id"])
            else:
                cls._next_phase(run, now)
        else:
            endpoint = "/v2/conditional-cancellation/list"
            for row in normalized["rows"]:
                cls._upsert_cancellation(
                    run=run,
                    row=row,
                    endpoint=endpoint,
                    now=now,
                )
            run.cancellation_count += len(normalized["rows"])
            if normalized["has_next"]:
                run.next_cursor = str(normalized["next_last_id"])
            else:
                cls._next_phase(run, now)
        run.page_count += 1
        run.matched_item_count += matched
        run.unmatched_item_count += unmatched
        run.last_page_at = now
        db.session.commit()

    @staticmethod
    def _cursor(run: MarketplaceFulfillmentSync) -> int:
        try:
            value = int(run.next_cursor)
        except (TypeError, ValueError):
            raise MarketplaceFulfillmentProtocolError(
                "Локальный cursor синхронизации повреждён"
            ) from None
        if value < 0:
            raise MarketplaceFulfillmentProtocolError(
                "Локальный cursor синхронизации повреждён"
            )
        return value

    @classmethod
    def _read_page(
        cls,
        *,
        run: MarketplaceFulfillmentSync,
        adapter: Any,
        credentials: MarketplaceCredentials,
    ) -> Mapping[str, Any]:
        if run.phase == "fbs_postings":
            payload = build_posting_request(
                fulfillment_kind="fbs",
                period_start=run.period_start,
                period_end=run.period_end,
                offset=run.next_offset,
                limit=POSTING_PAGE_LIMIT,
            )
            response = adapter.read_fbs_postings(credentials, payload)
            return normalize_posting_response(
                response,
                fulfillment_kind="fbs",
                requested_limit=POSTING_PAGE_LIMIT,
                requested_offset=run.next_offset,
            )
        if run.phase == "fbo_postings":
            payload = build_posting_request(
                fulfillment_kind="fbo",
                period_start=run.period_start,
                period_end=run.period_end,
                offset=run.next_offset,
                limit=POSTING_PAGE_LIMIT,
            )
            response = adapter.read_fbo_postings(credentials, payload)
            return normalize_posting_response(
                response,
                fulfillment_kind="fbo",
                requested_limit=POSTING_PAGE_LIMIT,
                requested_offset=run.next_offset,
            )
        cursor = cls._cursor(run)
        if run.phase == "returns":
            payload = build_return_request(
                period_start=run.period_start,
                period_end=run.period_end,
                last_id=cursor,
                limit=RETURN_PAGE_LIMIT,
            )
            response = adapter.read_returns(credentials, payload)
            return normalize_return_response(
                response,
                requested_limit=RETURN_PAGE_LIMIT,
                requested_last_id=cursor,
            )
        if run.phase == "rfbs_returns":
            payload = build_rfbs_return_request(
                period_start=run.period_start,
                period_end=run.period_end,
                last_id=cursor,
                limit=RETURN_PAGE_LIMIT,
            )
            response = adapter.read_rfbs_returns(credentials, payload)
            return normalize_rfbs_return_response(
                response,
                requested_limit=RETURN_PAGE_LIMIT,
                requested_last_id=cursor,
            )
        if run.phase == "conditional_cancellations":
            payload = build_conditional_cancellation_request(
                last_id=cursor,
                limit=RETURN_PAGE_LIMIT,
            )
            response = adapter.read_conditional_cancellations(credentials, payload)
            return normalize_conditional_cancellation_response(
                response,
                requested_limit=RETURN_PAGE_LIMIT,
                requested_last_id=cursor,
            )
        raise MarketplaceFulfillmentProtocolError(
            "Неизвестная фаза Ozon fulfillment sync"
        )

    @staticmethod
    def _safe_error(exc: Exception) -> Tuple[str, str]:
        if isinstance(exc, OzonAPIError):
            return str(exc.code or "ozon_fulfillment_error")[:100], str(exc)[:1000]
        if isinstance(exc, OzonFulfillmentContractError):
            return "ozon_fulfillment_protocol_error", str(exc)[:1000]
        if isinstance(exc, MarketplaceFulfillmentError):
            return exc.code[:100], str(exc)[:1000]
        return "marketplace_fulfillment_unexpected", (
            f"Unexpected fulfillment error: {type(exc).__name__}"
        )[:1000]

    @classmethod
    def sync_account(
        cls,
        *,
        seller_id: int,
        account_id: int,
        period_code: str = "30d",
        force: bool = False,
        max_pages: int = 5,
        adapter=None,
        credentials: Optional[MarketplaceCredentials] = None,
        now: Optional[datetime] = None,
        today: Optional[date] = None,
    ) -> MarketplaceFulfillmentSync:
        if not isinstance(force, bool):
            raise MarketplaceFulfillmentValidationError("force должен быть boolean")
        max_pages = cls._positive_integer(
            max_pages, "max_pages", maximum=cls.MAX_PAGES_PER_CALL
        )
        current_time = now or datetime.utcnow()
        current_date = today or current_time.date()
        period_code, period_start, period_end = cls._period(
            period_code, today=current_date
        )
        account, resolved_adapter, resolved_credentials = cls._account_adapter_credentials(
            seller_id=seller_id,
            account_id=account_id,
            adapter=adapter,
            credentials=credentials,
            now=current_time,
        )
        has_running = MarketplaceFulfillmentSync.query.filter(
            MarketplaceFulfillmentSync.seller_id == account.seller_id,
            MarketplaceFulfillmentSync.account_id == account.id,
            MarketplaceFulfillmentSync.status == "running",
        ).first() is not None
        if not force and not has_running:
            cached = cls._fresh_completed(
                seller_id=account.seller_id,
                account_id=account.id,
                period_code=period_code,
                period_start=period_start,
                period_end=period_end,
                now=current_time,
            )
            if cached is not None:
                return cached
        claim = cls._try_claim(account.id)
        if claim is None:
            raise MarketplaceFulfillmentBusy(
                "Заказы этого кабинета уже синхронизируются"
            )
        run: Optional[MarketplaceFulfillmentSync] = None
        try:
            db.session.expire_all()
            account = MarketplaceAccountService.get_owned_account(
                seller_id=seller_id,
                account_id=account_id,
                marketplace_code="ozon",
            )
            run = cls._running_run(
                seller_id=account.seller_id,
                account_id=account.id,
                now=current_time,
            )
            if run is not None and (
                run.period_code != period_code
                or run.period_start != period_start
                or run.period_end != period_end
            ):
                run.status = "cancelled"
                run.error_code = "fulfillment_period_superseded"
                run.error_message = "Новый период заменил незавершённую синхронизацию"
                run.completed_at = current_time
                db.session.commit()
                run = None
            if run is None:
                run = cls._create_run(
                    account=account,
                    period_code=period_code,
                    period_start=period_start,
                    period_end=period_end,
                    now=current_time,
                )
            offer_map, sku_map = cls._listing_maps(
                seller_id=account.seller_id,
                account_id=account.id,
            )
            for _ in range(max_pages):
                if run.status != "running":
                    break
                normalized = cls._read_page(
                    run=run,
                    adapter=resolved_adapter,
                    credentials=resolved_credentials,
                )
                cls._persist_page(
                    run=run,
                    normalized=normalized,
                    offer_map=offer_map,
                    sku_map=sku_map,
                    now=current_time,
                )
                db.session.refresh(run)
            return run
        except Exception as exc:
            db.session.rollback()
            if run is not None:
                persisted = MarketplaceFulfillmentSync.query.filter(
                    MarketplaceFulfillmentSync.id == run.id,
                    MarketplaceFulfillmentSync.seller_id == seller_id,
                    MarketplaceFulfillmentSync.account_id == account_id,
                ).first()
                if persisted is not None and persisted.status == "running":
                    code, message = cls._safe_error(exc)
                    persisted.status = "failed"
                    persisted.error_code = code
                    persisted.error_message = message
                    persisted.completed_at = current_time
                    db.session.commit()
            if isinstance(exc, MarketplaceFulfillmentError):
                raise
            if isinstance(exc, (OzonFulfillmentContractError, OzonAPIError)):
                raise MarketplaceFulfillmentProtocolError(str(exc)) from None
            raise
        finally:
            cls._release_claim(claim)

    @classmethod
    def _owned_account(cls, *, seller_id: int, account_id: int) -> SellerMarketplaceAccount:
        try:
            return MarketplaceAccountService.get_owned_account(
                seller_id=cls._positive_integer(seller_id, "seller_id"),
                account_id=cls._positive_integer(account_id, "account_id"),
                marketplace_code="ozon",
            )
        except MarketplaceAccountNotFound:
            raise MarketplaceFulfillmentNotFound("Кабинет Ozon не найден") from None

    @classmethod
    def _page_args(cls, *, page: Any, per_page: Any) -> Tuple[int, int]:
        return (
            cls._positive_integer(page, "page", maximum=100_000),
            cls._positive_integer(per_page, "per_page", maximum=100),
        )

    @staticmethod
    def _search(value: Any) -> str:
        if not isinstance(value, str):
            raise MarketplaceFulfillmentValidationError("search должен быть строкой")
        value = value.strip()
        if len(value) > 200:
            raise MarketplaceFulfillmentValidationError("search слишком длинный")
        return value

    @classmethod
    def list_postings(
        cls,
        *,
        seller_id: int,
        account_id: int,
        page: int = 1,
        per_page: int = 50,
        period_code: str = "30d",
        fulfillment_kind: Optional[str] = None,
        status: Optional[str] = None,
        search: str = "",
        today: Optional[date] = None,
    ) -> Dict[str, Any]:
        account = cls._owned_account(seller_id=seller_id, account_id=account_id)
        page, per_page = cls._page_args(page=page, per_page=per_page)
        period_code, period_start, _ = cls._period(
            period_code, today=today or date.today()
        )
        period_floor = datetime.combine(period_start, datetime.min.time())
        query = MarketplacePosting.query.filter(
            MarketplacePosting.seller_id == account.seller_id,
            MarketplacePosting.account_id == account.id,
            func.coalesce(
                MarketplacePosting.upstream_created_at,
                MarketplacePosting.last_seen_at,
            ) >= period_floor,
        )
        if fulfillment_kind:
            if fulfillment_kind not in {"fbo", "fbs"}:
                raise MarketplaceFulfillmentValidationError("Неизвестная схема заказа")
            query = query.filter(MarketplacePosting.fulfillment_kind == fulfillment_kind)
        if status:
            if not isinstance(status, str) or len(status) > 120:
                raise MarketplaceFulfillmentValidationError("Некорректный статус")
            query = query.filter(MarketplacePosting.status == status)
        search = cls._search(search)
        if search:
            pattern = f"%{search}%"
            query = query.filter(or_(
                MarketplacePosting.posting_number.ilike(pattern),
                MarketplacePosting.external_order_number.ilike(pattern),
                MarketplacePosting.items.any(
                    or_(
                        MarketplacePostingItem.offer_id.ilike(pattern),
                        MarketplacePostingItem.external_sku.ilike(pattern),
                        MarketplacePostingItem.name.ilike(pattern),
                    )
                ),
            ))
        pagination = query.order_by(
            func.coalesce(
                MarketplacePosting.upstream_created_at,
                MarketplacePosting.last_seen_at,
            ).desc(),
            MarketplacePosting.id.desc(),
        ).paginate(page=page, per_page=per_page, error_out=False)
        status_rows = db.session.query(
            MarketplacePosting.status,
            func.count(MarketplacePosting.id),
        ).filter(
            MarketplacePosting.seller_id == account.seller_id,
            MarketplacePosting.account_id == account.id,
            func.coalesce(
                MarketplacePosting.upstream_created_at,
                MarketplacePosting.last_seen_at,
            ) >= period_floor,
        ).group_by(MarketplacePosting.status).all()
        latest = cls._latest_completed(
            seller_id=account.seller_id,
            account_id=account.id,
            period_code=period_code,
        )
        running = MarketplaceFulfillmentSync.query.filter_by(
            seller_id=account.seller_id,
            account_id=account.id,
            status="running",
        ).order_by(MarketplaceFulfillmentSync.id.desc()).first()
        return {
            "items": [item.to_public_dict(detail=True) for item in pagination.items],
            "pagination": {
                "page": page,
                "per_page": per_page,
                "total": pagination.total,
                "pages": pagination.pages,
            },
            "status_counts": {row[0]: row[1] for row in status_rows},
            "sync": (running or latest).to_public_dict() if (running or latest) else None,
            "scope": {"account_id": account.id, "marketplace": "ozon"},
        }

    @classmethod
    def get_posting(cls, *, seller_id: int, account_id: int, posting_id: int) -> MarketplacePosting:
        account = cls._owned_account(seller_id=seller_id, account_id=account_id)
        posting = MarketplacePosting.query.filter(
            MarketplacePosting.id == cls._positive_integer(posting_id, "posting_id"),
            MarketplacePosting.seller_id == account.seller_id,
            MarketplacePosting.account_id == account.id,
        ).first()
        if posting is None:
            raise MarketplaceFulfillmentNotFound("Отправление Ozon не найдено")
        return posting

    @classmethod
    def list_returns(
        cls,
        *,
        seller_id: int,
        account_id: int,
        page: int = 1,
        per_page: int = 50,
        period_code: str = "30d",
        source_kind: Optional[str] = None,
        status: Optional[str] = None,
        search: str = "",
        today: Optional[date] = None,
    ) -> Dict[str, Any]:
        account = cls._owned_account(seller_id=seller_id, account_id=account_id)
        page, per_page = cls._page_args(page=page, per_page=per_page)
        _, period_start, _ = cls._period(period_code, today=today or date.today())
        period_floor = datetime.combine(period_start, datetime.min.time())
        query = MarketplaceReturn.query.filter(
            MarketplaceReturn.seller_id == account.seller_id,
            MarketplaceReturn.account_id == account.id,
            func.coalesce(
                MarketplaceReturn.status_changed_at,
                MarketplaceReturn.upstream_created_at,
                MarketplaceReturn.last_seen_at,
            ) >= period_floor,
        )
        if source_kind:
            if source_kind not in {"fbo_fbs", "rfbs"}:
                raise MarketplaceFulfillmentValidationError("Неизвестный источник возврата")
            query = query.filter(MarketplaceReturn.source_kind == source_kind)
        if status:
            if not isinstance(status, str) or len(status) > 120:
                raise MarketplaceFulfillmentValidationError("Некорректный статус")
            query = query.filter(MarketplaceReturn.status == status)
        search = cls._search(search)
        if search:
            pattern = f"%{search}%"
            query = query.filter(or_(
                MarketplaceReturn.posting_number.ilike(pattern),
                MarketplaceReturn.external_return_id.ilike(pattern),
                MarketplaceReturn.offer_id.ilike(pattern),
                MarketplaceReturn.external_sku.ilike(pattern),
                MarketplaceReturn.product_name.ilike(pattern),
            ))
        pagination = query.order_by(
            func.coalesce(
                MarketplaceReturn.status_changed_at,
                MarketplaceReturn.upstream_created_at,
            ).desc(),
            MarketplaceReturn.id.desc(),
        ).paginate(page=page, per_page=per_page, error_out=False)
        return {
            "items": [item.to_public_dict() for item in pagination.items],
            "pagination": {
                "page": page,
                "per_page": per_page,
                "total": pagination.total,
                "pages": pagination.pages,
            },
            "scope": {"account_id": account.id, "marketplace": "ozon"},
        }

    @classmethod
    def list_cancellations(
        cls,
        *,
        seller_id: int,
        account_id: int,
        page: int = 1,
        per_page: int = 50,
        period_code: str = "30d",
        source_kind: Optional[str] = None,
        status: Optional[str] = None,
        search: str = "",
        today: Optional[date] = None,
    ) -> Dict[str, Any]:
        account = cls._owned_account(seller_id=seller_id, account_id=account_id)
        page, per_page = cls._page_args(page=page, per_page=per_page)
        _, period_start, _ = cls._period(period_code, today=today or date.today())
        period_floor = datetime.combine(period_start, datetime.min.time())
        query = MarketplaceCancellation.query.filter(
            MarketplaceCancellation.seller_id == account.seller_id,
            MarketplaceCancellation.account_id == account.id,
            func.coalesce(
                MarketplaceCancellation.requested_at,
                MarketplaceCancellation.last_seen_at,
            ) >= period_floor,
        )
        if source_kind:
            allowed = {"posting_fbo", "posting_fbs", "rfbs_conditional"}
            if source_kind not in allowed:
                raise MarketplaceFulfillmentValidationError("Неизвестный источник отмены")
            query = query.filter(MarketplaceCancellation.source_kind == source_kind)
        if status:
            if not isinstance(status, str) or len(status) > 120:
                raise MarketplaceFulfillmentValidationError("Некорректный статус")
            query = query.filter(MarketplaceCancellation.status == status)
        search = cls._search(search)
        if search:
            pattern = f"%{search}%"
            query = query.filter(or_(
                MarketplaceCancellation.posting_number.ilike(pattern),
                MarketplaceCancellation.external_cancellation_id.ilike(pattern),
                MarketplaceCancellation.reason.ilike(pattern),
            ))
        pagination = query.order_by(
            MarketplaceCancellation.requested_at.desc(),
            MarketplaceCancellation.id.desc(),
        ).paginate(page=page, per_page=per_page, error_out=False)
        return {
            "items": [item.to_public_dict() for item in pagination.items],
            "pagination": {
                "page": page,
                "per_page": per_page,
                "total": pagination.total,
                "pages": pagination.pages,
            },
            "scope": {"account_id": account.id, "marketplace": "ozon"},
        }
