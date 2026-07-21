"""Account-scoped Ozon analytics synchronization and normalized read models."""

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, Mapping, Optional, Tuple
import fcntl
import json
import os
import tempfile

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload

from models import (
    MarketplaceAnalyticsSync,
    MarketplaceCredentialEncryptionError,
    MarketplaceListing,
    MarketplaceMetricFact,
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
from services.ozon_analytics_contracts import (
    METRIC_BY_CODE,
    METRIC_DEFINITIONS,
    REQUEST_METRIC_DEFINITIONS,
    PAGE_LIMIT,
    OzonAnalyticsContractError,
    build_analytics_request,
    metric_definitions_public,
    normalize_analytics_response,
    request_fingerprint,
)
from services.ozon_api_client import OzonAPIError


class MarketplaceAnalyticsError(RuntimeError):
    status_code = 400
    code = "marketplace_analytics_error"


class MarketplaceAnalyticsValidationError(MarketplaceAnalyticsError):
    status_code = 400
    code = "invalid_marketplace_analytics_request"


class MarketplaceAnalyticsNotFound(MarketplaceAnalyticsError):
    status_code = 404
    code = "marketplace_analytics_not_found"


class MarketplaceAnalyticsConfigurationError(MarketplaceAnalyticsError):
    status_code = 409
    code = "marketplace_analytics_not_ready"


class MarketplaceAnalyticsBusy(MarketplaceAnalyticsError):
    status_code = 409
    code = "marketplace_analytics_busy"


class MarketplaceAnalyticsProtocolError(MarketplaceAnalyticsError):
    status_code = 502
    code = "ozon_analytics_protocol_error"


class MarketplaceAnalyticsService:
    CONTRACT_VERSION = "ozon-analytics-core-v2"
    SUPPORTED_PERIODS = {"7d": 7, "30d": 30}
    CACHE_TTL = timedelta(hours=4)
    STALE_RUNNING_AFTER = timedelta(minutes=30)
    MAX_PAGES_PER_CALL = 5
    MAX_PRODUCT_ROWS = 20_000
    LOCK_DIRECTORY = "seller-hub-ozon-analytics-locks"

    @staticmethod
    def _positive_integer(
        value: Any,
        field_name: str,
        *,
        maximum: Optional[int] = None,
    ) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise MarketplaceAnalyticsValidationError(
                f"{field_name} должен быть положительным целым числом"
            )
        if maximum is not None and value > maximum:
            raise MarketplaceAnalyticsValidationError(
                f"{field_name} превышает лимит {maximum}"
            )
        return value

    @classmethod
    def _period(cls, period_code: Any, *, today: Optional[date] = None) -> Tuple[str, date, date]:
        if not isinstance(period_code, str) or period_code not in cls.SUPPORTED_PERIODS:
            raise MarketplaceAnalyticsValidationError(
                "Для Ozon доступны периоды 7d и 30d"
            )
        end = today or date.today()
        start = end - timedelta(days=cls.SUPPORTED_PERIODS[period_code] - 1)
        return period_code, start, end

    @staticmethod
    def _stable_json(value: Any) -> str:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @staticmethod
    def _json_object(raw: Optional[str]) -> Dict[str, Any]:
        try:
            value = json.loads(raw or "{}")
        except (TypeError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _safe_error(exc: Exception) -> Tuple[str, str]:
        if isinstance(exc, OzonAPIError):
            return str(exc.code or "ozon_analytics_error")[:100], str(exc)[:1000]
        if isinstance(exc, OzonAnalyticsContractError):
            return "ozon_analytics_protocol_error", str(exc)[:1000]
        if isinstance(exc, MarketplaceAnalyticsError):
            return exc.code[:100], str(exc)[:1000]
        return "marketplace_analytics_unexpected", (
            f"Unexpected analytics error: {type(exc).__name__}"
        )[:1000]

    @classmethod
    def _try_claim(cls, account_id: int):
        directory = os.path.join(tempfile.gettempdir(), cls.LOCK_DIRECTORY)
        os.makedirs(directory, mode=0o700, exist_ok=True)
        path = os.path.join(directory, f"account-{int(account_id)}.lock")
        lock_file = open(path, "a+", encoding="ascii")
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
        now: Optional[datetime] = None,
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
            raise MarketplaceAnalyticsNotFound("Кабинет Ozon не найден") from None
        current_time = now or datetime.utcnow()
        if not account.is_active or account.connection_status != "connected":
            raise MarketplaceAnalyticsConfigurationError(
                "Кабинет Ozon должен быть активен и пройти проверку подключения"
            )
        if account.credential_expires_at and account.credential_expires_at <= current_time:
            raise MarketplaceAnalyticsConfigurationError(
                "Срок действия API key Ozon истёк"
            )
        if (adapter is None) != (credentials is None):
            raise MarketplaceAnalyticsValidationError(
                "adapter and credentials must be injected together"
            )
        if adapter is None:
            if not account.has_credentials:
                raise MarketplaceAnalyticsConfigurationError(
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
                raise MarketplaceAnalyticsConfigurationError(
                    "Credentials кабинета Ozon невозможно прочитать"
                ) from None
            adapter = get_marketplace_registry().get("ozon")
        try:
            adapter.require_capability("analytics_read")
        except MarketplaceAdapterError as exc:
            raise MarketplaceAnalyticsConfigurationError(str(exc)) from None
        return account, adapter, credentials

    @classmethod
    def _latest_completed_query(
        cls,
        *,
        seller_id: int,
        account_id: int,
        period_code: str,
    ):
        return MarketplaceAnalyticsSync.query.filter(
            MarketplaceAnalyticsSync.seller_id == seller_id,
            MarketplaceAnalyticsSync.account_id == account_id,
            MarketplaceAnalyticsSync.period_code == period_code,
            MarketplaceAnalyticsSync.status == "completed",
        ).order_by(
            MarketplaceAnalyticsSync.completed_at.desc(),
            MarketplaceAnalyticsSync.id.desc(),
        )

    @classmethod
    def latest_completed_sync(
        cls,
        *,
        seller_id: int,
        account_id: int,
        period_code: str,
        exact_current_period: bool = False,
        today: Optional[date] = None,
    ) -> Optional[MarketplaceAnalyticsSync]:
        seller_id = cls._positive_integer(seller_id, "seller_id")
        account_id = cls._positive_integer(account_id, "account_id")
        normalized, start, end = cls._period(period_code, today=today)
        query = cls._latest_completed_query(
            seller_id=seller_id,
            account_id=account_id,
            period_code=normalized,
        )
        if exact_current_period:
            query = query.filter(
                MarketplaceAnalyticsSync.period_start == start,
                MarketplaceAnalyticsSync.period_end == end,
            )
        return query.first()

    @classmethod
    def _fresh_cached_sync(
        cls,
        *,
        seller_id: int,
        account_id: int,
        period_code: str,
        now: datetime,
        today: date,
    ) -> Optional[MarketplaceAnalyticsSync]:
        sync = cls.latest_completed_sync(
            seller_id=seller_id,
            account_id=account_id,
            period_code=period_code,
            exact_current_period=True,
            today=today,
        )
        expected_fingerprint = request_fingerprint(
            period_start=sync.period_start,
            period_end=sync.period_end,
        ) if sync is not None else None
        if (
            sync
            and sync.contract_version == cls.CONTRACT_VERSION
            and sync.request_fingerprint == expected_fingerprint
            and sync.completed_at
            and sync.completed_at >= now - cls.CACHE_TTL
        ):
            return sync
        return None

    @classmethod
    def _identifier_map(
        cls,
        *,
        seller_id: int,
        account: SellerMarketplaceAccount,
    ) -> Dict[str, MarketplaceListing]:
        listings = MarketplaceListing.query.filter(
            MarketplaceListing.seller_id == seller_id,
            MarketplaceListing.marketplace_id == account.marketplace_id,
            MarketplaceListing.account_id == account.id,
        ).all()
        result: Dict[str, MarketplaceListing] = {}
        for listing in listings:
            identifiers = MarketplaceListing._json_value(
                listing.identifiers_json,
                {},
            )
            candidates = []
            for value in (
                listing.primary_sku,
                identifiers.get("sku"),
                identifiers.get("sku_fbo"),
                identifiers.get("sku_fbs"),
            ):
                if value not in (None, ""):
                    candidates.append(str(value))
            sources = identifiers.get("sources")
            if isinstance(sources, list):
                for source in sources[:100]:
                    if not isinstance(source, dict):
                        continue
                    for key in ("sku", "sku_fbo", "sku_fbs"):
                        value = source.get(key)
                        if value not in (None, ""):
                            candidates.append(str(value))
            for candidate in candidates:
                if (
                    not candidate.isdigit()
                    or candidate.startswith("0")
                    or len(candidate) > 100
                ):
                    continue
                previous = result.get(candidate)
                if previous is not None and previous.id != listing.id:
                    raise MarketplaceAnalyticsConfigurationError(
                        "Один Ozon SKU связан с несколькими локальными карточками"
                    )
                result[candidate] = listing
        return result

    @classmethod
    def _create_run(
        cls,
        *,
        account: SellerMarketplaceAccount,
        period_code: str,
        period_start: date,
        period_end: date,
        now: datetime,
    ) -> MarketplaceAnalyticsSync:
        run = MarketplaceAnalyticsSync(
            seller_id=account.seller_id,
            marketplace_id=account.marketplace_id,
            account_id=account.id,
            period_code=period_code,
            period_start=period_start,
            period_end=period_end,
            status="running",
            phase="product",
            next_offset=0,
            request_fingerprint=request_fingerprint(
                period_start=period_start,
                period_end=period_end,
            ),
            contract_version=cls.CONTRACT_VERSION,
            metrics_json=cls._stable_json(metric_definitions_public()),
            totals_json="{}",
            started_at=now,
        )
        db.session.add(run)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            existing = MarketplaceAnalyticsSync.query.filter(
                MarketplaceAnalyticsSync.seller_id == account.seller_id,
                MarketplaceAnalyticsSync.account_id == account.id,
                MarketplaceAnalyticsSync.period_code == period_code,
                MarketplaceAnalyticsSync.status == "running",
            ).first()
            if existing is None:
                raise
            return existing
        return run

    @classmethod
    def _running_run(
        cls,
        *,
        seller_id: int,
        account_id: int,
        period_code: str,
        now: datetime,
    ) -> Optional[MarketplaceAnalyticsSync]:
        run = MarketplaceAnalyticsSync.query.filter(
            MarketplaceAnalyticsSync.seller_id == seller_id,
            MarketplaceAnalyticsSync.account_id == account_id,
            MarketplaceAnalyticsSync.period_code == period_code,
            MarketplaceAnalyticsSync.status == "running",
        ).order_by(MarketplaceAnalyticsSync.id.desc()).first()
        if run is None:
            return None
        heartbeat = run.last_page_at or run.started_at
        if heartbeat and heartbeat < now - cls.STALE_RUNNING_AFTER:
            run.status = "failed"
            run.error_code = "analytics_sync_interrupted"
            run.error_message = "Синхронизация прервана до завершения страницы"
            run.completed_at = now
            db.session.commit()
            return None
        expected = request_fingerprint(
            period_start=run.period_start,
            period_end=run.period_end,
        )
        if (
            run.contract_version != cls.CONTRACT_VERSION
            or run.request_fingerprint != expected
        ):
            run.status = "failed"
            run.error_code = "analytics_contract_drift"
            run.error_message = "Контракт незавершённой синхронизации изменился"
            run.completed_at = now
            db.session.commit()
            return None
        return run

    @classmethod
    def _stored_totals(cls, normalized: Mapping[str, Decimal]) -> Dict[str, Any]:
        return {
            code: {
                "value": format(value, "f"),
                "unit": METRIC_BY_CODE[code].unit,
                "definition_code": METRIC_BY_CODE[code].definition_code,
                "cross_marketplace_comparable": False,
            }
            for code, value in normalized.items()
        }

    @staticmethod
    def _totals_values(raw: Mapping[str, Any]) -> Dict[str, str]:
        values = {}
        for code, item in raw.items():
            if isinstance(item, dict) and isinstance(item.get("value"), str):
                values[code] = item["value"]
        return values

    @classmethod
    def _persist_page(
        cls,
        *,
        run: MarketplaceAnalyticsSync,
        normalized: Mapping[str, Any],
        dimension_kind: str,
        identifiers: Mapping[str, MarketplaceListing],
        now: datetime,
    ) -> None:
        dimension_ids = [row["dimension_id"] for row in normalized["rows"]]
        if dimension_ids:
            duplicate = MarketplaceMetricFact.query.filter(
                MarketplaceMetricFact.sync_id == run.id,
                MarketplaceMetricFact.dimension_kind == (
                    "listing" if dimension_kind == "product" else "day"
                ),
                MarketplaceMetricFact.dimension_id.in_(dimension_ids),
            ).first()
            if duplicate is not None:
                raise MarketplaceAnalyticsProtocolError(
                    "Ozon analytics повторил dimension из предыдущей страницы"
                )

        if dimension_kind == "product":
            current_totals = cls._stored_totals(normalized["totals"])
            stored_totals = cls._json_object(run.totals_json)
            if not stored_totals:
                run.totals_json = cls._stable_json(current_totals)
            elif cls._totals_values(stored_totals) != cls._totals_values(current_totals):
                raise MarketplaceAnalyticsProtocolError(
                    "Ozon analytics totals изменились во время pagination"
                )

        matched = unmatched = 0
        fact_dimension = "listing" if dimension_kind == "product" else "day"
        for row in normalized["rows"]:
            listing = identifiers.get(row["dimension_id"]) if dimension_kind == "product" else None
            if dimension_kind == "product":
                if listing is None:
                    unmatched += 1
                else:
                    matched += 1
            for definition in REQUEST_METRIC_DEFINITIONS:
                db.session.add(MarketplaceMetricFact(
                    sync_id=run.id,
                    seller_id=run.seller_id,
                    marketplace_id=run.marketplace_id,
                    account_id=run.account_id,
                    listing_id=listing.id if listing is not None else None,
                    dimension_kind=fact_dimension,
                    dimension_id=row["dimension_id"],
                    dimension_name=row["dimension_name"],
                    fact_date=row["fact_date"],
                    metric_code=definition.metric_code,
                    provider_metric=definition.provider_metric,
                    metric_value=row["metrics"][definition.metric_code],
                    unit=definition.unit,
                    definition_code=definition.definition_code,
                    cross_marketplace_comparable=False,
                    source_endpoint="/v1/analytics/data",
                    observed_at=now,
                ))
        run.page_count += 1
        run.row_count += len(normalized["rows"])
        run.matched_rows += matched
        run.unmatched_rows += unmatched
        run.fact_count += (
            len(normalized["rows"]) * len(REQUEST_METRIC_DEFINITIONS)
        )
        run.last_page_at = now
        run.response_timestamp = normalized["timestamp"]

        if normalized["has_more"]:
            next_offset = run.next_offset + len(normalized["rows"])
            if next_offset > cls.MAX_PRODUCT_ROWS:
                raise MarketplaceAnalyticsProtocolError(
                    "Ozon analytics превысил лимит строк одной синхронизации"
                )
            run.next_offset = next_offset
        elif dimension_kind == "product":
            run.phase = "day"
            run.next_offset = 0
        else:
            run.phase = "completed"
            run.status = "completed"
            run.completed_at = now
        db.session.commit()

    @classmethod
    def sync_account(
        cls,
        *,
        seller_id: int,
        account_id: int,
        period_code: str = "30d",
        force: bool = False,
        max_pages: int = 2,
        adapter=None,
        credentials: Optional[MarketplaceCredentials] = None,
        now: Optional[datetime] = None,
        today: Optional[date] = None,
    ) -> MarketplaceAnalyticsSync:
        if not isinstance(force, bool):
            raise MarketplaceAnalyticsValidationError("force должен быть boolean")
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
        if not force:
            cached = cls._fresh_cached_sync(
                seller_id=account.seller_id,
                account_id=account.id,
                period_code=period_code,
                now=current_time,
                today=current_date,
            )
            if cached is not None:
                return cached

        claim = cls._try_claim(account.id)
        if claim is None:
            raise MarketplaceAnalyticsBusy(
                "Аналитика этого кабинета уже синхронизируется"
            )
        run: Optional[MarketplaceAnalyticsSync] = None
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
                period_code=period_code,
                now=current_time,
            )
            if run is None:
                run = cls._create_run(
                    account=account,
                    period_code=period_code,
                    period_start=period_start,
                    period_end=period_end,
                    now=current_time,
                )
            identifiers = cls._identifier_map(
                seller_id=account.seller_id,
                account=account,
            )
            for _ in range(max_pages):
                if run.status != "running":
                    break
                dimension_kind = "product" if run.phase == "product" else "day"
                payload = build_analytics_request(
                    period_start=run.period_start,
                    period_end=run.period_end,
                    dimension_kind=dimension_kind,
                    offset=run.next_offset,
                    limit=PAGE_LIMIT,
                )
                response = resolved_adapter.read_analytics(
                    resolved_credentials,
                    payload,
                )
                normalized = normalize_analytics_response(
                    response,
                    dimension_kind=dimension_kind,
                    requested_limit=PAGE_LIMIT,
                )
                cls._persist_page(
                    run=run,
                    normalized=normalized,
                    dimension_kind=dimension_kind,
                    identifiers=identifiers,
                    now=current_time,
                )
                db.session.refresh(run)
            return run
        except Exception as exc:
            db.session.rollback()
            if run is not None:
                persisted = MarketplaceAnalyticsSync.query.filter(
                    MarketplaceAnalyticsSync.id == run.id,
                    MarketplaceAnalyticsSync.seller_id == seller_id,
                    MarketplaceAnalyticsSync.account_id == account_id,
                ).first()
                if persisted is not None and persisted.status == "running":
                    code, message = cls._safe_error(exc)
                    persisted.status = "failed"
                    persisted.error_code = code
                    persisted.error_message = message
                    persisted.completed_at = current_time
                    db.session.commit()
            if isinstance(exc, MarketplaceAnalyticsError):
                raise
            if isinstance(exc, OzonAnalyticsContractError):
                raise MarketplaceAnalyticsProtocolError(str(exc)) from None
            if isinstance(exc, OzonAPIError):
                raise MarketplaceAnalyticsProtocolError(str(exc)) from None
            raise
        finally:
            cls._release_claim(claim)

    @classmethod
    def _owned_sync(
        cls,
        *,
        seller_id: int,
        account_id: int,
        sync_id: int,
    ) -> MarketplaceAnalyticsSync:
        sync_id = cls._positive_integer(sync_id, "sync_id")
        sync = MarketplaceAnalyticsSync.query.options(
            joinedload(MarketplaceAnalyticsSync.account),
            joinedload(MarketplaceAnalyticsSync.marketplace),
        ).filter(
            MarketplaceAnalyticsSync.id == sync_id,
            MarketplaceAnalyticsSync.seller_id == seller_id,
            MarketplaceAnalyticsSync.account_id == account_id,
        ).first()
        if sync is None:
            raise MarketplaceAnalyticsNotFound("Снимок аналитики не найден")
        return sync

    @classmethod
    def _sync_for_read(
        cls,
        *,
        seller_id: int,
        account_id: int,
        period_code: str,
        sync_id: Optional[int] = None,
    ) -> Optional[MarketplaceAnalyticsSync]:
        if sync_id is not None:
            sync = cls._owned_sync(
                seller_id=seller_id,
                account_id=account_id,
                sync_id=sync_id,
            )
            return sync if sync.status == "completed" else None
        return cls.latest_completed_sync(
            seller_id=seller_id,
            account_id=account_id,
            period_code=period_code,
        )

    @staticmethod
    def _public_metric_value(value: Any) -> float:
        return float(value or 0)

    @classmethod
    def _daily_rows(cls, sync: MarketplaceAnalyticsSync) -> list:
        facts = MarketplaceMetricFact.query.filter(
            MarketplaceMetricFact.sync_id == sync.id,
            MarketplaceMetricFact.seller_id == sync.seller_id,
            MarketplaceMetricFact.account_id == sync.account_id,
            MarketplaceMetricFact.dimension_kind == "day",
        ).order_by(
            MarketplaceMetricFact.fact_date.asc(),
            MarketplaceMetricFact.metric_code.asc(),
        ).all()
        by_day: Dict[str, Dict[str, Any]] = {}
        for fact in facts:
            key = fact.fact_date.isoformat()
            row = by_day.setdefault(key, {"date": key})
            row[fact.metric_code] = cls._public_metric_value(fact.metric_value)
        return [by_day[key] for key in sorted(by_day)]

    @classmethod
    def _product_rows(cls, sync: MarketplaceAnalyticsSync) -> list:
        facts = MarketplaceMetricFact.query.options(
            joinedload(MarketplaceMetricFact.listing),
        ).filter(
            MarketplaceMetricFact.sync_id == sync.id,
            MarketplaceMetricFact.seller_id == sync.seller_id,
            MarketplaceMetricFact.account_id == sync.account_id,
            MarketplaceMetricFact.dimension_kind == "listing",
        ).order_by(
            MarketplaceMetricFact.dimension_id.asc(),
            MarketplaceMetricFact.metric_code.asc(),
        ).all()
        by_dimension: Dict[str, Dict[str, Any]] = {}
        for fact in facts:
            listing = fact.listing
            row = by_dimension.setdefault(fact.dimension_id, {
                "entity_kind": "marketplace_listing",
                "listing_id": fact.listing_id,
                "sku": fact.dimension_id,
                "title": (
                    listing.title if listing is not None else fact.dimension_name
                ),
                "offer_id": listing.offer_id if listing is not None else None,
                "matched": listing is not None,
                "metrics": {},
            })
            row["metrics"][fact.metric_code] = cls._public_metric_value(
                fact.metric_value
            )
        return list(by_dimension.values())

    @classmethod
    def get_summary(
        cls,
        *,
        seller_id: int,
        account_id: int,
        period_code: str = "30d",
        sync_id: Optional[int] = None,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        seller_id = cls._positive_integer(seller_id, "seller_id")
        account_id = cls._positive_integer(account_id, "account_id")
        period_code, expected_start, expected_end = cls._period(period_code)
        account = MarketplaceAccountService.get_owned_account(
            seller_id=seller_id,
            account_id=account_id,
            marketplace_code="ozon",
        )
        sync = cls._sync_for_read(
            seller_id=seller_id,
            account_id=account.id,
            period_code=period_code,
            sync_id=sync_id,
        )
        if sync is None:
            running = MarketplaceAnalyticsSync.query.filter(
                MarketplaceAnalyticsSync.seller_id == seller_id,
                MarketplaceAnalyticsSync.account_id == account.id,
                MarketplaceAnalyticsSync.period_code == period_code,
                MarketplaceAnalyticsSync.status == "running",
            ).order_by(MarketplaceAnalyticsSync.id.desc()).first()
            return {
                "scope": {
                    "marketplace_code": "ozon",
                    "account_id": account.id,
                    "account_label": account.label,
                    "comparison_scope": "marketplace_account_only",
                    "cross_marketplace_comparable": False,
                },
                "status": "syncing" if running else "no_data",
                "sync": running.to_public_dict() if running else None,
                "definitions": metric_definitions_public(),
                "kpi": {},
                "dailyData": [],
                "topProducts": [],
            }
        totals_raw = cls._json_object(sync.totals_json)
        totals = {
            code: cls._public_metric_value(item.get("value"))
            for code, item in totals_raw.items()
            if isinstance(item, dict)
        }
        revenue = totals.get("ordered_revenue_rub", 0.0)
        orders = totals.get("ordered_units", 0.0)
        products = cls._product_rows(sync)
        products.sort(
            key=lambda row: (
                -row["metrics"].get("ordered_revenue_rub", 0),
                row["sku"],
            )
        )
        current_time = now or datetime.utcnow()
        stale = bool(
            sync.period_start != expected_start
            or sync.period_end != expected_end
            or not sync.completed_at
            or sync.completed_at < current_time - cls.CACHE_TTL
        )
        return {
            "scope": {
                "marketplace_code": "ozon",
                "account_id": account.id,
                "account_label": account.label,
                "comparison_scope": "marketplace_account_only",
                "cross_marketplace_comparable": False,
            },
            "status": "stale" if stale else "ready",
            "sync": sync.to_public_dict(),
            "definitions": metric_definitions_public(),
            "kpi": {
                "revenue": revenue,
                "orders": orders,
                "avgCheck": round(revenue / orders, 2) if orders else 0,
                "views": totals.get("views"),
                "cartAdditions": totals.get("cart_additions"),
                "cartConversionPercent": totals.get(
                    "cart_conversion_percent"
                ),
                "delivered": totals.get("delivered_units"),
                "cancellations": totals.get("cancelled_units"),
                "returns": totals.get("returned_units"),
            },
            "dailyData": cls._daily_rows(sync),
            "topProducts": products[:20],
        }

    @classmethod
    def get_products(
        cls,
        *,
        seller_id: int,
        account_id: int,
        period_code: str = "30d",
        sync_id: Optional[int] = None,
        sort_by: str = "ordered_revenue_rub",
        sort_dir: str = "desc",
        search: str = "",
        page: int = 1,
        per_page: int = 20,
    ) -> Dict[str, Any]:
        allowed_sorts = {
            item.metric_code for item in REQUEST_METRIC_DEFINITIONS
        }
        if sort_by not in allowed_sorts:
            raise MarketplaceAnalyticsValidationError("Неизвестная метрика сортировки")
        if sort_dir not in {"asc", "desc"}:
            raise MarketplaceAnalyticsValidationError("sort_dir должен быть asc или desc")
        page = cls._positive_integer(page, "page")
        per_page = cls._positive_integer(per_page, "per_page", maximum=100)
        if not isinstance(search, str) or len(search) > 200:
            raise MarketplaceAnalyticsValidationError("Некорректный поиск")
        seller_id = cls._positive_integer(seller_id, "seller_id")
        account_id = cls._positive_integer(account_id, "account_id")
        try:
            account = MarketplaceAccountService.get_owned_account(
                seller_id=seller_id,
                account_id=account_id,
                marketplace_code="ozon",
            )
        except MarketplaceAccountNotFound:
            raise MarketplaceAnalyticsNotFound("Кабинет Ozon не найден") from None
        period_code, _, _ = cls._period(period_code)
        sync = cls._sync_for_read(
            seller_id=seller_id,
            account_id=account.id,
            period_code=period_code,
            sync_id=sync_id,
        )
        if sync is None:
            return {"items": [], "total": 0, "page": page, "pages": 0}
        items = cls._product_rows(sync)
        needle = search.strip().casefold()
        if needle:
            items = [
                row for row in items
                if needle in (row.get("title") or "").casefold()
                or needle in row["sku"].casefold()
                or needle in (row.get("offer_id") or "").casefold()
            ]
        reverse = sort_dir == "desc"
        items.sort(
            key=lambda row: (
                row["metrics"].get(sort_by, 0),
                row["sku"],
            ),
            reverse=reverse,
        )
        total = len(items)
        start = (page - 1) * per_page
        return {
            "items": items[start:start + per_page],
            "total": total,
            "page": page,
            "pages": (total + per_page - 1) // per_page if total else 0,
            "sync_id": sync.id,
        }

    @classmethod
    def latest_listing_metrics(
        cls,
        *,
        seller_id: int,
        account_id: int,
        listing_ids: list,
        period_code: str = "30d",
        now: Optional[datetime] = None,
        today: Optional[date] = None,
    ) -> Tuple[Optional[MarketplaceAnalyticsSync], Dict[int, Dict[str, float]]]:
        if not isinstance(listing_ids, list) or len(listing_ids) > 500:
            raise MarketplaceAnalyticsValidationError(
                "listing_ids должен быть массивом до 500 элементов"
            )
        normalized = []
        seen = set()
        for value in listing_ids:
            value = cls._positive_integer(value, "listing_id")
            if value in seen:
                raise MarketplaceAnalyticsValidationError(
                    "listing_ids не должен содержать дубли"
                )
            seen.add(value)
            normalized.append(value)
        current_time = now or datetime.utcnow()
        current_date = today or current_time.date()
        period_code, _, _ = cls._period(period_code, today=current_date)
        sync = cls._fresh_cached_sync(
            seller_id=seller_id,
            account_id=account_id,
            period_code=period_code,
            now=current_time,
            today=current_date,
        )
        if sync is None or not normalized:
            return sync, {}
        facts = MarketplaceMetricFact.query.filter(
            MarketplaceMetricFact.sync_id == sync.id,
            MarketplaceMetricFact.seller_id == seller_id,
            MarketplaceMetricFact.account_id == account_id,
            MarketplaceMetricFact.listing_id.in_(normalized),
            MarketplaceMetricFact.dimension_kind == "listing",
        ).all()
        result: Dict[int, Dict[str, float]] = {}
        for fact in facts:
            result.setdefault(fact.listing_id, {})[fact.metric_code] = (
                cls._public_metric_value(fact.metric_value)
            )
        return sync, result
