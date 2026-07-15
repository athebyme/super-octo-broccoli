"""Exact-account, read-only Ozon accrual snapshot synchronization."""

from datetime import date, datetime, timedelta
from hashlib import sha256
from typing import Any, Dict, Mapping, Optional, Tuple
import fcntl
import json
import os
import tempfile

from sqlalchemy import case, desc, func, or_
from sqlalchemy.exc import IntegrityError

from models import (
    MarketplaceCredentialEncryptionError,
    MarketplaceFinanceAccrualType,
    MarketplaceFinanceComponent,
    MarketplaceFinanceFact,
    MarketplaceFinanceFactItem,
    MarketplaceFinanceSync,
    MarketplaceListing,
    MarketplacePosting,
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
from services.ozon_finance_contracts import (
    OzonFinanceContractError,
    build_accrual_by_day_request,
    build_accrual_types_request,
    normalize_accrual_by_day_response,
    normalize_accrual_types_response,
)


class MarketplaceFinanceError(RuntimeError):
    status_code = 400
    code = "marketplace_finance_error"


class MarketplaceFinanceValidationError(MarketplaceFinanceError):
    code = "invalid_marketplace_finance_request"


class MarketplaceFinanceNotFound(MarketplaceFinanceError):
    status_code = 404
    code = "marketplace_finance_not_found"


class MarketplaceFinanceConfigurationError(MarketplaceFinanceError):
    status_code = 409
    code = "marketplace_finance_not_ready"


class MarketplaceFinanceBusy(MarketplaceFinanceError):
    status_code = 409
    code = "marketplace_finance_busy"


class MarketplaceFinanceProtocolError(MarketplaceFinanceError):
    status_code = 502
    code = "ozon_finance_protocol_error"


class MarketplaceFinanceService:
    """Build immutable snapshots; only completed snapshots are seller-visible."""

    CONTRACT_VERSION = "ozon-finance-accrual-v1"
    DEFINITION_CODE = "ozon-accrual-total-amount-v1"
    SUPPORTED_PERIODS = {"7d": 7, "30d": 30}
    CACHE_TTL = timedelta(hours=6)
    STALE_RUNNING_AFTER = timedelta(minutes=30)
    MAX_PAGES_PER_CALL = 10
    MAX_COMPLETED_SNAPSHOTS = 8
    LOCK_DIRECTORY = "seller-hub-ozon-finance-locks"

    @staticmethod
    def _positive_integer(
        value: Any,
        field_name: str,
        *,
        maximum: Optional[int] = None,
    ) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise MarketplaceFinanceValidationError(
                f"{field_name} должен быть положительным целым числом"
            )
        if maximum is not None and value > maximum:
            raise MarketplaceFinanceValidationError(
                f"{field_name} превышает лимит {maximum}"
            )
        return value

    @classmethod
    def _period(cls, period_code: Any, *, today: date) -> Tuple[str, date, date]:
        if not isinstance(period_code, str) or period_code not in cls.SUPPORTED_PERIODS:
            raise MarketplaceFinanceValidationError(
                "Для финансов Ozon доступны периоды 7d и 30d"
            )
        end = today
        start = end - timedelta(days=cls.SUPPORTED_PERIODS[period_code] - 1)
        return period_code, start, end

    @staticmethod
    def _stable_json(value: Any) -> str:
        def encode(item: Any) -> Any:
            if isinstance(item, (date, datetime)):
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
            "endpoints": [
                "/v1/finance/accrual/types",
                "/v1/finance/accrual/by-day",
            ],
            "top_level_amount_definition": cls.DEFINITION_CODE,
        })

    @staticmethod
    def _try_claim(account_id: int):
        directory = os.path.join(
            tempfile.gettempdir(),
            MarketplaceFinanceService.LOCK_DIRECTORY,
        )
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
            raise MarketplaceFinanceNotFound("Кабинет Ozon не найден") from None
        if not account.is_active or account.connection_status != "connected":
            raise MarketplaceFinanceConfigurationError(
                "Кабинет Ozon должен быть активен и пройти проверку подключения"
            )
        if account.credential_expires_at and account.credential_expires_at <= now:
            raise MarketplaceFinanceConfigurationError(
                "Срок действия API key Ozon истёк"
            )
        if (adapter is None) != (credentials is None):
            raise MarketplaceFinanceValidationError(
                "adapter and credentials must be injected together"
            )
        if adapter is None:
            if not account.has_credentials:
                raise MarketplaceFinanceConfigurationError(
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
                raise MarketplaceFinanceConfigurationError(
                    "Credentials кабинета Ozon невозможно прочитать"
                ) from None
            adapter = get_marketplace_registry().get("ozon")
        try:
            adapter.require_capability("finance_read")
        except MarketplaceAdapterError as exc:
            raise MarketplaceFinanceConfigurationError(str(exc)) from None
        return account, adapter, credentials

    @classmethod
    def _listing_maps(
        cls,
        *,
        seller_id: int,
        account_id: int,
    ) -> Tuple[Dict[str, MarketplaceListing], set]:
        candidates: Dict[str, MarketplaceListing] = {}
        ambiguous = set()
        listings = MarketplaceListing.query.filter(
            MarketplaceListing.seller_id == seller_id,
            MarketplaceListing.account_id == account_id,
        ).order_by(MarketplaceListing.id.asc()).all()
        for listing in listings:
            skus = {listing.primary_sku} if listing.primary_sku else set()
            try:
                identifiers = json.loads(listing.identifiers_json or "{}")
            except (TypeError, json.JSONDecodeError):
                identifiers = {}
            if isinstance(identifiers, dict):
                for key in ("sku", "primary_sku", "fbo_sku", "fbs_sku"):
                    raw = identifiers.get(key)
                    if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0:
                        skus.add(str(raw))
                    elif (
                        isinstance(raw, str)
                        and raw.isdigit()
                        and not raw.startswith("0")
                    ):
                        skus.add(raw)
            for raw_sku in skus:
                sku = str(raw_sku or "").strip()
                if not sku:
                    continue
                previous = candidates.get(sku)
                if previous is not None and previous.id != listing.id:
                    ambiguous.add(sku)
                    candidates.pop(sku, None)
                elif sku not in ambiguous:
                    candidates[sku] = listing
        return candidates, ambiguous

    @staticmethod
    def _posting_map(*, seller_id: int, account_id: int) -> Dict[str, MarketplacePosting]:
        return {
            posting.posting_number: posting
            for posting in MarketplacePosting.query.filter(
                MarketplacePosting.seller_id == seller_id,
                MarketplacePosting.account_id == account_id,
            ).all()
        }

    @classmethod
    def _latest_completed(
        cls,
        *,
        seller_id: int,
        account_id: int,
        period_code: str,
    ) -> Optional[MarketplaceFinanceSync]:
        return MarketplaceFinanceSync.query.filter(
            MarketplaceFinanceSync.seller_id == seller_id,
            MarketplaceFinanceSync.account_id == account_id,
            MarketplaceFinanceSync.period_code == period_code,
            MarketplaceFinanceSync.status == "completed",
            MarketplaceFinanceSync.contract_version == cls.CONTRACT_VERSION,
        ).order_by(
            MarketplaceFinanceSync.completed_at.desc(),
            MarketplaceFinanceSync.id.desc(),
        ).first()

    @classmethod
    def _latest_covering_completed(
        cls,
        *,
        seller_id: int,
        account_id: int,
        period_start: date,
        period_end: date,
    ) -> Optional[MarketplaceFinanceSync]:
        return MarketplaceFinanceSync.query.filter(
            MarketplaceFinanceSync.seller_id == seller_id,
            MarketplaceFinanceSync.account_id == account_id,
            MarketplaceFinanceSync.status == "completed",
            MarketplaceFinanceSync.contract_version == cls.CONTRACT_VERSION,
            MarketplaceFinanceSync.period_start <= period_start,
            MarketplaceFinanceSync.period_end >= period_end,
        ).order_by(
            MarketplaceFinanceSync.completed_at.desc(),
            MarketplaceFinanceSync.id.desc(),
        ).first()

    @classmethod
    def _latest_overlapping_completed(
        cls,
        *,
        seller_id: int,
        account_id: int,
        period_start: date,
        period_end: date,
    ) -> Optional[MarketplaceFinanceSync]:
        """Return last-good data while a new calendar window is rebuilding.

        A snapshot completed yesterday no longer fully covers a window ending
        today.  It is still safer and more useful than exposing the partial
        replacement run, so reads may use its overlapping days and explicitly
        report incomplete coverage to the caller.
        """
        return MarketplaceFinanceSync.query.filter(
            MarketplaceFinanceSync.seller_id == seller_id,
            MarketplaceFinanceSync.account_id == account_id,
            MarketplaceFinanceSync.status == "completed",
            MarketplaceFinanceSync.contract_version == cls.CONTRACT_VERSION,
            MarketplaceFinanceSync.period_start <= period_end,
            MarketplaceFinanceSync.period_end >= period_start,
        ).order_by(
            MarketplaceFinanceSync.period_end.desc(),
            MarketplaceFinanceSync.completed_at.desc(),
            MarketplaceFinanceSync.period_start.asc(),
            MarketplaceFinanceSync.id.desc(),
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
    ) -> Optional[MarketplaceFinanceSync]:
        run = cls._latest_completed(
            seller_id=seller_id,
            account_id=account_id,
            period_code=period_code,
        )
        if (
            run is not None
            and run.period_start == period_start
            and run.period_end == period_end
            and run.completed_at is not None
            and run.completed_at >= now - cls.CACHE_TTL
            and run.request_fingerprint == cls._run_fingerprint(period_start, period_end)
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
    ) -> Optional[MarketplaceFinanceSync]:
        run = MarketplaceFinanceSync.query.filter(
            MarketplaceFinanceSync.seller_id == seller_id,
            MarketplaceFinanceSync.account_id == account_id,
            MarketplaceFinanceSync.status == "running",
        ).order_by(MarketplaceFinanceSync.id.desc()).first()
        if run is None:
            return None
        heartbeat = run.last_page_at or run.started_at
        if heartbeat and heartbeat < now - cls.STALE_RUNNING_AFTER:
            run.status = "failed"
            run.error_code = "finance_sync_interrupted"
            run.error_message = "Синхронизация прервана до завершения страницы"
            run.completed_at = now
            db.session.commit()
            return None
        expected = cls._run_fingerprint(run.period_start, run.period_end)
        if (
            run.contract_version != cls.CONTRACT_VERSION
            or run.request_fingerprint != expected
            or run.current_date < run.period_start
            or run.current_date > run.period_end
        ):
            run.status = "failed"
            run.error_code = "finance_contract_drift"
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
    ) -> MarketplaceFinanceSync:
        run = MarketplaceFinanceSync(
            seller_id=account.seller_id,
            marketplace_id=account.marketplace_id,
            account_id=account.id,
            period_code=period_code,
            period_start=period_start,
            period_end=period_end,
            status="running",
            phase="types",
            current_date=period_start,
            next_cursor=None,
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
    def _persist_types(
        cls,
        *,
        run: MarketplaceFinanceSync,
        normalized: Mapping[str, Any],
        now: datetime,
    ) -> None:
        for item in normalized["types"]:
            record = MarketplaceFinanceAccrualType.query.filter(
                MarketplaceFinanceAccrualType.account_id == run.account_id,
                MarketplaceFinanceAccrualType.external_type_id == item["type_id"],
            ).first()
            if record is None:
                record = MarketplaceFinanceAccrualType(
                    seller_id=run.seller_id,
                    marketplace_id=run.marketplace_id,
                    account_id=run.account_id,
                    external_type_id=item["type_id"],
                    name=item["name"],
                    last_seen_at=now,
                )
                db.session.add(record)
            record.last_sync_id = run.id
            record.name = item["name"]
            record.description = item["description"]
            record.source_endpoint = "/v1/finance/accrual/types"
            record.last_seen_at = now
        run.phase = "accruals"
        run.next_cursor = None
        run.page_count += 1
        run.last_page_at = now
        db.session.commit()

    @staticmethod
    def _type_names(*, seller_id: int, account_id: int) -> Dict[int, str]:
        return {
            item.external_type_id: item.name
            for item in MarketplaceFinanceAccrualType.query.filter(
                MarketplaceFinanceAccrualType.seller_id == seller_id,
                MarketplaceFinanceAccrualType.account_id == account_id,
            ).all()
        }

    @staticmethod
    def _listing_match(
        sku: Optional[str],
        listing_map: Mapping[str, MarketplaceListing],
        ambiguous_skus: set,
    ) -> Tuple[Optional[MarketplaceListing], str]:
        if sku in ambiguous_skus:
            return None, "ambiguous"
        listing = listing_map.get(sku) if sku else None
        return (listing, "matched") if listing is not None else (None, "unmatched")

    @classmethod
    def _persist_accrual_page(
        cls,
        *,
        run: MarketplaceFinanceSync,
        normalized: Mapping[str, Any],
        listing_map: Mapping[str, MarketplaceListing],
        ambiguous_skus: set,
        posting_map: Mapping[str, MarketplacePosting],
        type_names: Mapping[int, str],
        now: datetime,
    ) -> None:
        matched = unmatched = ambiguous = 0
        item_count = component_count = 0
        for row in normalized["rows"]:
            duplicate = MarketplaceFinanceFact.query.filter(
                MarketplaceFinanceFact.sync_id == run.id,
                MarketplaceFinanceFact.accrual_id == row["accrual_id"],
            ).first()
            if duplicate is not None:
                raise OzonFinanceContractError(
                    "accrual_id repeated across finance pages"
                )
            amount = row["amount"]
            sign = "positive" if amount > 0 else ("negative" if amount < 0 else "zero")
            posting = posting_map.get(row["unit_number"])
            fact = MarketplaceFinanceFact(
                sync_id=run.id,
                seller_id=run.seller_id,
                marketplace_id=run.marketplace_id,
                account_id=run.account_id,
                posting_id=posting.id if posting else None,
                accrual_id=row["accrual_id"],
                fact_date=row["date"],
                unit_number=row["unit_number"],
                accrued_category=row["category"],
                total_amount=amount,
                currency=row["currency"],
                amount_sign=sign,
                definition_code=cls.DEFINITION_CODE,
                source_endpoint="/v1/finance/accrual/by-day",
                contract_version=cls.CONTRACT_VERSION,
                source_fingerprint=cls._fingerprint(row),
                observed_at=now,
            )
            db.session.add(fact)
            db.session.flush()
            for sku in row["skus"]:
                listing, match_status = cls._listing_match(
                    sku,
                    listing_map,
                    ambiguous_skus,
                )
                if match_status == "matched":
                    matched += 1
                elif match_status == "ambiguous":
                    ambiguous += 1
                else:
                    unmatched += 1
                db.session.add(MarketplaceFinanceFactItem(
                    fact_id=fact.id,
                    seller_id=run.seller_id,
                    account_id=run.account_id,
                    listing_id=listing.id if listing else None,
                    external_sku=sku,
                    match_status=match_status,
                ))
                item_count += 1
            for component in row["components"]:
                listing, _ = cls._listing_match(
                    component["sku"],
                    listing_map,
                    ambiguous_skus,
                )
                component_key = cls._fingerprint({
                    "kind": component["component_kind"],
                    "sku": component["sku"],
                    "type_id": component["type_id"],
                })
                db.session.add(MarketplaceFinanceComponent(
                    fact_id=fact.id,
                    seller_id=run.seller_id,
                    account_id=run.account_id,
                    listing_id=listing.id if listing else None,
                    component_key=component_key,
                    component_kind=component["component_kind"],
                    external_type_id=component["type_id"],
                    type_name=type_names.get(component["type_id"]),
                    external_sku=component["sku"],
                    amount=component["amount"],
                    currency=component["currency"],
                    rollup_role="explanatory_only",
                ))
                component_count += 1
        run.fact_count += len(normalized["rows"])
        run.item_count += item_count
        run.component_count += component_count
        run.matched_item_count += matched
        run.unmatched_item_count += unmatched
        run.ambiguous_item_count += ambiguous
        if normalized["has_next"]:
            run.next_cursor = normalized["next_last_id"]
        elif run.current_date < run.period_end:
            run.current_date = run.current_date + timedelta(days=1)
            run.next_cursor = None
        else:
            run.phase = "completed"
            run.status = "completed"
            run.next_cursor = None
            run.completed_at = now
        run.page_count += 1
        run.last_page_at = now
        db.session.commit()

    @classmethod
    def _prune_completed(cls, run: MarketplaceFinanceSync) -> None:
        stale = MarketplaceFinanceSync.query.filter(
            MarketplaceFinanceSync.account_id == run.account_id,
            MarketplaceFinanceSync.period_code == run.period_code,
            MarketplaceFinanceSync.status == "completed",
        ).order_by(
            MarketplaceFinanceSync.completed_at.desc(),
            MarketplaceFinanceSync.id.desc(),
        ).offset(cls.MAX_COMPLETED_SNAPSHOTS).all()
        if not stale:
            return
        for old in stale:
            db.session.delete(old)
        db.session.commit()

    @staticmethod
    def _safe_error(exc: Exception) -> Tuple[str, str]:
        if isinstance(exc, OzonAPIError):
            return str(exc.code or "ozon_finance_error")[:100], str(exc)[:1000]
        if isinstance(exc, OzonFinanceContractError):
            return "ozon_finance_protocol_error", str(exc)[:1000]
        if isinstance(exc, MarketplaceFinanceError):
            return exc.code[:100], str(exc)[:1000]
        return "marketplace_finance_unexpected", (
            f"Unexpected finance error: {type(exc).__name__}"
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
    ) -> MarketplaceFinanceSync:
        if not isinstance(force, bool):
            raise MarketplaceFinanceValidationError("force должен быть boolean")
        max_pages = cls._positive_integer(
            max_pages,
            "max_pages",
            maximum=cls.MAX_PAGES_PER_CALL,
        )
        current_time = now or datetime.utcnow()
        current_date = today or current_time.date()
        period_code, period_start, period_end = cls._period(
            period_code,
            today=current_date,
        )
        account, resolved_adapter, resolved_credentials = cls._account_adapter_credentials(
            seller_id=seller_id,
            account_id=account_id,
            adapter=adapter,
            credentials=credentials,
            now=current_time,
        )
        has_running = MarketplaceFinanceSync.query.filter(
            MarketplaceFinanceSync.seller_id == account.seller_id,
            MarketplaceFinanceSync.account_id == account.id,
            MarketplaceFinanceSync.status == "running",
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
            raise MarketplaceFinanceBusy(
                "Финансы этого кабинета уже синхронизируются"
            )
        run: Optional[MarketplaceFinanceSync] = None
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
                run.error_code = "finance_period_superseded"
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
            listing_map, ambiguous_skus = cls._listing_maps(
                seller_id=account.seller_id,
                account_id=account.id,
            )
            posting_map = cls._posting_map(
                seller_id=account.seller_id,
                account_id=account.id,
            )
            type_names = cls._type_names(
                seller_id=account.seller_id,
                account_id=account.id,
            )
            for _ in range(max_pages):
                if run.status != "running":
                    break
                if run.phase == "types":
                    response = resolved_adapter.read_finance_accrual_types(
                        resolved_credentials,
                        build_accrual_types_request(),
                    )
                    normalized_types = normalize_accrual_types_response(response)
                    cls._persist_types(
                        run=run,
                        normalized=normalized_types,
                        now=current_time,
                    )
                    type_names.update({
                        item["type_id"]: item["name"]
                        for item in normalized_types["types"]
                    })
                elif run.phase == "accruals":
                    payload = build_accrual_by_day_request(
                        accrual_date=run.current_date,
                        last_id=run.next_cursor,
                    )
                    response = resolved_adapter.read_finance_accrual_by_day(
                        resolved_credentials,
                        payload,
                    )
                    normalized_page = normalize_accrual_by_day_response(
                        response,
                        requested_date=run.current_date,
                        requested_last_id=run.next_cursor,
                    )
                    cls._persist_accrual_page(
                        run=run,
                        normalized=normalized_page,
                        listing_map=listing_map,
                        ambiguous_skus=ambiguous_skus,
                        posting_map=posting_map,
                        type_names=type_names,
                        now=current_time,
                    )
                else:
                    raise MarketplaceFinanceProtocolError(
                        "Неизвестная фаза Ozon finance sync"
                    )
                db.session.refresh(run)
            if run.status == "completed":
                cls._prune_completed(run)
            return run
        except Exception as exc:
            db.session.rollback()
            if run is not None:
                persisted = MarketplaceFinanceSync.query.filter(
                    MarketplaceFinanceSync.id == run.id,
                    MarketplaceFinanceSync.seller_id == seller_id,
                    MarketplaceFinanceSync.account_id == account_id,
                ).first()
                if persisted is not None and persisted.status == "running":
                    code, message = cls._safe_error(exc)
                    persisted.status = "failed"
                    persisted.error_code = code
                    persisted.error_message = message
                    persisted.completed_at = current_time
                    db.session.commit()
            if isinstance(exc, MarketplaceFinanceError):
                raise
            if isinstance(exc, (OzonFinanceContractError, OzonAPIError)):
                raise MarketplaceFinanceProtocolError(str(exc)) from None
            raise
        finally:
            cls._release_claim(claim)

    @classmethod
    def _owned_account(
        cls,
        *,
        seller_id: int,
        account_id: int,
    ) -> SellerMarketplaceAccount:
        try:
            return MarketplaceAccountService.get_owned_account(
                seller_id=cls._positive_integer(seller_id, "seller_id"),
                account_id=cls._positive_integer(account_id, "account_id"),
                marketplace_code="ozon",
            )
        except MarketplaceAccountNotFound:
            raise MarketplaceFinanceNotFound("Кабинет Ozon не найден") from None

    @classmethod
    def _page_args(cls, *, page: Any, per_page: Any) -> Tuple[int, int]:
        return (
            cls._positive_integer(page, "page", maximum=100_000),
            cls._positive_integer(per_page, "per_page", maximum=100),
        )

    @staticmethod
    def _search(value: Any) -> str:
        if not isinstance(value, str):
            raise MarketplaceFinanceValidationError("search должен быть строкой")
        normalized = value.strip()
        if len(normalized) > 200:
            raise MarketplaceFinanceValidationError("search слишком длинный")
        return normalized

    @classmethod
    def _latest_attempt(
        cls,
        *,
        seller_id: int,
        account_id: int,
        period_code: str,
    ) -> Optional[MarketplaceFinanceSync]:
        return MarketplaceFinanceSync.query.filter(
            MarketplaceFinanceSync.seller_id == seller_id,
            MarketplaceFinanceSync.account_id == account_id,
            MarketplaceFinanceSync.period_code == period_code,
        ).order_by(
            MarketplaceFinanceSync.created_at.desc(),
            MarketplaceFinanceSync.id.desc(),
        ).first()

    @classmethod
    def list_facts(
        cls,
        *,
        seller_id: int,
        account_id: int,
        page: int = 1,
        per_page: int = 50,
        period_code: str = "30d",
        category: Optional[str] = None,
        amount_sign: Optional[str] = None,
        type_id: Optional[int] = None,
        search: str = "",
        today: Optional[date] = None,
    ) -> Dict[str, Any]:
        account = cls._owned_account(seller_id=seller_id, account_id=account_id)
        page, per_page = cls._page_args(page=page, per_page=per_page)
        period_code, period_start, period_end = cls._period(
            period_code,
            today=today or date.today(),
        )
        snapshot = cls._latest_covering_completed(
            seller_id=account.seller_id,
            account_id=account.id,
            period_start=period_start,
            period_end=period_end,
        )
        if snapshot is None:
            snapshot = cls._latest_overlapping_completed(
                seller_id=account.seller_id,
                account_id=account.id,
                period_start=period_start,
                period_end=period_end,
            )
        latest_attempt = cls._latest_attempt(
            seller_id=account.seller_id,
            account_id=account.id,
            period_code=period_code,
        )
        if snapshot is None:
            return {
                "items": [],
                "pagination": {"page": page, "per_page": per_page, "total": 0, "pages": 0},
                "totals": [],
                "category_totals": [],
                "type_counts": [],
                "sync": latest_attempt.to_public_dict() if latest_attempt else None,
                "snapshot_sync": None,
                "coverage": {
                    "requested_start": period_start.isoformat(),
                    "requested_end": period_end.isoformat(),
                    "snapshot_start": None,
                    "snapshot_end": None,
                    "complete": False,
                },
                "definitions": cls._definitions(),
                "scope": {"account_id": account.id, "marketplace": "ozon"},
            }
        query = MarketplaceFinanceFact.query.filter(
            MarketplaceFinanceFact.sync_id == snapshot.id,
            MarketplaceFinanceFact.seller_id == account.seller_id,
            MarketplaceFinanceFact.account_id == account.id,
            MarketplaceFinanceFact.fact_date >= period_start,
            MarketplaceFinanceFact.fact_date <= period_end,
        )
        if category:
            if category not in {"UNSPECIFIED", "POSTING", "ITEM", "NON_ITEM"}:
                raise MarketplaceFinanceValidationError("Неизвестная категория начисления")
            query = query.filter(MarketplaceFinanceFact.accrued_category == category)
        if amount_sign:
            if amount_sign not in {"positive", "negative", "zero"}:
                raise MarketplaceFinanceValidationError("Неизвестный знак суммы")
            query = query.filter(MarketplaceFinanceFact.amount_sign == amount_sign)
        if type_id is not None:
            type_id = cls._positive_integer(type_id, "type_id")
            query = query.filter(
                MarketplaceFinanceFact.components.any(
                    MarketplaceFinanceComponent.external_type_id == type_id
                )
            )
        search = cls._search(search)
        if search:
            pattern = f"%{search}%"
            query = query.filter(or_(
                MarketplaceFinanceFact.accrual_id.ilike(pattern),
                MarketplaceFinanceFact.unit_number.ilike(pattern),
                MarketplaceFinanceFact.items.any(
                    MarketplaceFinanceFactItem.external_sku.ilike(pattern)
                ),
            ))

        totals = []
        for currency, count_value, positive, negative, net in query.with_entities(
            MarketplaceFinanceFact.currency,
            func.count(MarketplaceFinanceFact.id),
            func.sum(case(
                (MarketplaceFinanceFact.total_amount > 0, MarketplaceFinanceFact.total_amount),
                else_=0,
            )),
            func.sum(case(
                (MarketplaceFinanceFact.total_amount < 0, MarketplaceFinanceFact.total_amount),
                else_=0,
            )),
            func.sum(MarketplaceFinanceFact.total_amount),
        ).group_by(MarketplaceFinanceFact.currency).order_by(
            MarketplaceFinanceFact.currency.asc()
        ).all():
            totals.append({
                "currency": currency,
                "fact_count": count_value,
                "positive": str(positive or 0),
                "negative": str(negative or 0),
                "net": str(net or 0),
            })

        category_totals = [{
            "category": row[0],
            "currency": row[1],
            "fact_count": row[2],
            "amount": str(row[3] or 0),
        } for row in query.with_entities(
            MarketplaceFinanceFact.accrued_category,
            MarketplaceFinanceFact.currency,
            func.count(MarketplaceFinanceFact.id),
            func.sum(MarketplaceFinanceFact.total_amount),
        ).group_by(
            MarketplaceFinanceFact.accrued_category,
            MarketplaceFinanceFact.currency,
        ).order_by(
            MarketplaceFinanceFact.accrued_category.asc(),
            MarketplaceFinanceFact.currency.asc(),
        ).all()]

        type_rows = db.session.query(
            MarketplaceFinanceComponent.external_type_id,
            MarketplaceFinanceComponent.type_name,
            func.count(MarketplaceFinanceComponent.id),
        ).join(
            MarketplaceFinanceFact,
            MarketplaceFinanceFact.id == MarketplaceFinanceComponent.fact_id,
        ).filter(
            MarketplaceFinanceFact.sync_id == snapshot.id,
            MarketplaceFinanceFact.seller_id == account.seller_id,
            MarketplaceFinanceFact.account_id == account.id,
            MarketplaceFinanceFact.fact_date >= period_start,
            MarketplaceFinanceFact.fact_date <= period_end,
        ).group_by(
            MarketplaceFinanceComponent.external_type_id,
            MarketplaceFinanceComponent.type_name,
        ).order_by(
            func.count(MarketplaceFinanceComponent.id).desc(),
            MarketplaceFinanceComponent.external_type_id.asc(),
        ).limit(50).all()
        type_counts = [{
            "external_type_id": row[0],
            "name": row[1],
            "occurrence_count": row[2],
        } for row in type_rows]

        pagination = query.order_by(
            MarketplaceFinanceFact.fact_date.desc(),
            MarketplaceFinanceFact.id.desc(),
        ).paginate(page=page, per_page=per_page, error_out=False)
        return {
            "items": [item.to_public_dict(detail=True) for item in pagination.items],
            "pagination": {
                "page": page,
                "per_page": per_page,
                "total": pagination.total,
                "pages": pagination.pages,
            },
            "totals": totals,
            "category_totals": category_totals,
            "type_counts": type_counts,
            "sync": latest_attempt.to_public_dict() if latest_attempt else None,
            "snapshot_sync": snapshot.to_public_dict(),
            "coverage": {
                "requested_start": period_start.isoformat(),
                "requested_end": period_end.isoformat(),
                "snapshot_start": snapshot.period_start.isoformat(),
                "snapshot_end": snapshot.period_end.isoformat(),
                "complete": (
                    snapshot.period_start <= period_start
                    and snapshot.period_end >= period_end
                ),
            },
            "definitions": cls._definitions(),
            "scope": {"account_id": account.id, "marketplace": "ozon"},
        }

    @classmethod
    def get_fact(
        cls,
        *,
        seller_id: int,
        account_id: int,
        fact_id: int,
    ) -> MarketplaceFinanceFact:
        account = cls._owned_account(seller_id=seller_id, account_id=account_id)
        fact = MarketplaceFinanceFact.query.join(
            MarketplaceFinanceSync,
            MarketplaceFinanceSync.id == MarketplaceFinanceFact.sync_id,
        ).filter(
            MarketplaceFinanceFact.id == cls._positive_integer(fact_id, "fact_id"),
            MarketplaceFinanceFact.seller_id == account.seller_id,
            MarketplaceFinanceFact.account_id == account.id,
            MarketplaceFinanceSync.status == "completed",
        ).first()
        if fact is None:
            raise MarketplaceFinanceNotFound("Начисление Ozon не найдено")
        return fact

    @classmethod
    def _definitions(cls) -> Dict[str, Any]:
        return {
            "source": "/v1/finance/accrual/by-day",
            "amount_field": "accruals[].total_amount",
            "definition_code": cls.DEFINITION_CODE,
            "positive": "total_amount > 0",
            "negative": "total_amount < 0",
            "net": "sum(total_amount) within one currency",
            "profit": False,
            "component_rollup": "forbidden",
            "cross_currency_rollup": "forbidden",
            "cross_marketplace_comparable": False,
        }
