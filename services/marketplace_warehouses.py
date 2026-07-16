"""Seller-scoped Ozon warehouse and exact FBS stock read projections."""

from datetime import datetime, timedelta
import hashlib
import json
from typing import Any, Dict, Mapping, Optional, Tuple

from sqlalchemy.exc import IntegrityError

from models import (
    Marketplace,
    MarketplaceCredentialEncryptionError,
    MarketplaceListing,
    MarketplaceWarehouse,
    MarketplaceWarehouseStock,
    MarketplaceWarehouseSync,
    SellerMarketplaceAccount,
    db,
)
from services.marketplace_accounts import (
    MarketplaceAccountNotFound,
    MarketplaceAccountService,
)
from services.marketplace_adapters import (
    MarketplaceAdapterError,
    MarketplaceCredentials,
    get_marketplace_registry,
)
from services.marketplace_operation_locks import (
    release_account_operation_lock,
    try_account_operation_lock,
)
from services.ozon_api_client import OzonAPIError
from services.ozon_commercial_contracts import (
    OzonCommercialContractError,
    OzonStockContract,
    OzonWarehouseContract,
)


class MarketplaceWarehouseError(RuntimeError):
    status_code = 400
    code = "marketplace_warehouse_error"


class MarketplaceWarehouseValidationError(MarketplaceWarehouseError):
    status_code = 400
    code = "invalid_marketplace_warehouse_request"


class MarketplaceWarehouseNotFound(MarketplaceWarehouseError):
    status_code = 404
    code = "marketplace_warehouse_not_found"


class MarketplaceWarehouseConflict(MarketplaceWarehouseError):
    status_code = 409
    code = "marketplace_warehouse_conflict"


class MarketplaceWarehouseConfigurationError(MarketplaceWarehouseError):
    status_code = 503
    code = "marketplace_warehouse_configuration_error"


class MarketplaceWarehouseSyncError(MarketplaceWarehouseError):
    status_code = 502
    code = "marketplace_warehouse_sync_error"


class MarketplaceWarehouseService:
    MAX_WAREHOUSE_PAGES = 100
    MAX_STOCK_PAGES = 100
    STALE_RUN_AFTER = timedelta(minutes=15)

    @staticmethod
    def _positive_integer(value: Any, field_name: str) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise MarketplaceWarehouseValidationError(
                f"{field_name} должен быть положительным целым числом"
            )
        return value

    @staticmethod
    def _stable_json(value: Mapping[str, Any]) -> str:
        return json.dumps(
            dict(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @classmethod
    def _fingerprint(cls, value: Mapping[str, Any]) -> str:
        return hashlib.sha256(cls._stable_json(value).encode("utf-8")).hexdigest()

    @staticmethod
    def _sanitize_error(error: Exception) -> Tuple[str, str]:
        if isinstance(error, MarketplaceWarehouseError):
            code = error.code
            message = str(error)
        elif isinstance(error, OzonAPIError):
            code = error.code
            message = str(error)
        elif isinstance(error, OzonCommercialContractError):
            code = error.code
            message = str(error)
        else:
            code = "marketplace_warehouse_sync_failed"
            message = "Не удалось безопасно синхронизировать данные Ozon"
        return str(code)[:100], " ".join(str(message).split())[:1000]

    @classmethod
    def _account_adapter_credentials(
        cls,
        *,
        seller_id: int,
        account_id: int,
        capabilities: Tuple[str, ...],
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
            raise MarketplaceWarehouseNotFound("Кабинет Ozon не найден") from None
        current_time = now or datetime.utcnow()
        if not account.is_active or account.connection_status != "connected":
            raise MarketplaceWarehouseConfigurationError(
                "Кабинет Ozon должен быть активен и пройти проверку подключения"
            )
        if (
            account.credential_expires_at
            and account.credential_expires_at <= current_time
        ):
            raise MarketplaceWarehouseConfigurationError(
                "Срок действия API key Ozon истёк"
            )
        if adapter is not None or credentials is not None:
            if adapter is None or credentials is None:
                raise MarketplaceWarehouseValidationError(
                    "adapter and credentials must be injected together"
                )
            resolved_adapter = adapter
            resolved_credentials = credentials
        else:
            if not account.has_credentials:
                raise MarketplaceWarehouseConfigurationError(
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
                raise MarketplaceWarehouseConfigurationError(
                    "Credentials кабинета Ozon невозможно прочитать"
                ) from None
            resolved_adapter = get_marketplace_registry().get("ozon")
        try:
            for capability in capabilities:
                resolved_adapter.require_capability(capability)
        except MarketplaceAdapterError as exc:
            raise MarketplaceWarehouseConfigurationError(str(exc)) from None
        return account, resolved_adapter, resolved_credentials

    @classmethod
    def _create_run(
        cls,
        *,
        account: SellerMarketplaceAccount,
        now: datetime,
    ) -> MarketplaceWarehouseSync:
        running = MarketplaceWarehouseSync.query.filter_by(
            seller_id=account.seller_id,
            account_id=account.id,
            status="running",
        ).order_by(MarketplaceWarehouseSync.id.desc()).first()
        if running is not None:
            if running.started_at and running.started_at > now - cls.STALE_RUN_AFTER:
                raise MarketplaceWarehouseConflict(
                    "Синхронизация складов этого кабинета уже выполняется"
                )
            running.status = "failed"
            running.error_code = "stale_warehouse_sync"
            running.error_message = "Предыдущая синхронизация складов прервана"
            running.completed_at = now
            db.session.commit()
        run = MarketplaceWarehouseSync(
            seller_id=account.seller_id,
            marketplace_id=account.marketplace_id,
            account_id=account.id,
            status="running",
            started_at=now,
        )
        db.session.add(run)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            raise MarketplaceWarehouseConflict(
                "Синхронизация складов этого кабинета уже выполняется"
            ) from None
        return run

    @classmethod
    def _fetch_all_warehouses(
        cls,
        *,
        adapter,
        credentials: MarketplaceCredentials,
    ) -> Tuple[Dict[str, dict], int]:
        cursor = ""
        seen_cursors = set()
        warehouses: Dict[str, dict] = {}
        page_count = 0
        while True:
            if page_count >= cls.MAX_WAREHOUSE_PAGES:
                raise MarketplaceWarehouseSyncError(
                    "Ozon warehouse pagination превысила безопасный лимит"
                )
            page = OzonWarehouseContract.normalize_page(
                adapter.read_warehouses(
                    credentials,
                    OzonWarehouseContract.request_payload(cursor=cursor),
                )
            )
            page_count += 1
            for item in page["warehouses"]:
                external_id = item["warehouse_id"]
                if external_id in warehouses:
                    raise MarketplaceWarehouseSyncError(
                        "Ozon повторил warehouse_id между страницами"
                    )
                warehouses[external_id] = item
            if not page["has_next"]:
                break
            next_cursor = page["cursor"]
            if next_cursor == cursor or next_cursor in seen_cursors:
                raise MarketplaceWarehouseSyncError(
                    "Ozon warehouse pagination зациклилась"
                )
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        return warehouses, page_count

    @classmethod
    def _apply_warehouses(
        cls,
        *,
        run: MarketplaceWarehouseSync,
        warehouses: Dict[str, dict],
        page_count: int,
        now: datetime,
    ) -> MarketplaceWarehouseSync:
        existing = {
            item.external_warehouse_id: item
            for item in MarketplaceWarehouse.query.filter_by(
                seller_id=run.seller_id,
                marketplace_id=run.marketplace_id,
                account_id=run.account_id,
            ).all()
        }
        created = 0
        updated = 0
        seen_ids = set(warehouses)
        for external_id, item in warehouses.items():
            fingerprint = cls._fingerprint(item)
            row = existing.get(external_id)
            if row is None:
                row = MarketplaceWarehouse(
                    seller_id=run.seller_id,
                    marketplace_id=run.marketplace_id,
                    account_id=run.account_id,
                    external_warehouse_id=external_id,
                    name=item["name"],
                    sync_fingerprint=fingerprint,
                    last_seen_at=now,
                    last_synced_at=now,
                )
                db.session.add(row)
                created += 1
            else:
                updated += int(
                    row.sync_fingerprint != fingerprint or not row.is_available
                )
            row.name = item["name"]
            row.status = item.get("status")
            row.warehouse_type = item.get("warehouse_type")
            row.carriage_label_type = item.get("carriage_label_type")
            row.flags_json = cls._stable_json(item.get("flags", {}))
            row.limits_json = cls._stable_json(item.get("limits", {}))
            row.is_available = True
            row.sync_fingerprint = fingerprint
            row.last_seen_at = now
            row.last_synced_at = now
        unavailable = 0
        for external_id, row in existing.items():
            if external_id not in seen_ids and row.is_available:
                row.is_available = False
                row.last_synced_at = now
                unavailable += 1
        run.status = "completed"
        run.page_count = page_count
        run.seen_count = len(warehouses)
        run.created_count = created
        run.updated_count = updated
        run.unavailable_count = unavailable
        run.error_code = None
        run.error_message = None
        run.completed_at = now
        db.session.commit()
        return run

    @classmethod
    def _mark_run_failed(cls, run_id: int, error: Exception) -> None:
        db.session.rollback()
        run = db.session.get(MarketplaceWarehouseSync, run_id)
        if run is None or run.status == "completed":
            return
        run.status = "failed"
        run.error_code, run.error_message = cls._sanitize_error(error)
        run.completed_at = datetime.utcnow()
        db.session.commit()

    @classmethod
    def sync_warehouses(
        cls,
        *,
        seller_id: int,
        account_id: int,
        adapter=None,
        credentials: Optional[MarketplaceCredentials] = None,
        now: Optional[datetime] = None,
    ) -> MarketplaceWarehouseSync:
        seller_id = cls._positive_integer(seller_id, "seller_id")
        account_id = cls._positive_integer(account_id, "account_id")
        current_time = now or datetime.utcnow()
        account, resolved_adapter, resolved_credentials = (
            cls._account_adapter_credentials(
                seller_id=seller_id,
                account_id=account_id,
                capabilities=("warehouses_read",),
                adapter=adapter,
                credentials=credentials,
                now=current_time,
            )
        )
        lock_file = try_account_operation_lock(account.id)
        if lock_file is None:
            raise MarketplaceWarehouseConflict(
                "Кабинет Ozon занят другой безопасной операцией"
            )
        run = None
        try:
            run = cls._create_run(account=account, now=current_time)
            warehouses, page_count = cls._fetch_all_warehouses(
                adapter=resolved_adapter,
                credentials=resolved_credentials,
            )
            return cls._apply_warehouses(
                run=run,
                warehouses=warehouses,
                page_count=page_count,
                now=datetime.utcnow(),
            )
        except (MarketplaceWarehouseError, OzonAPIError, OzonCommercialContractError) as exc:
            if run is not None:
                cls._mark_run_failed(run.id, exc)
            if isinstance(exc, MarketplaceWarehouseError):
                raise
            raise MarketplaceWarehouseSyncError(str(exc)) from None
        except Exception as exc:
            if run is not None:
                cls._mark_run_failed(run.id, exc)
            raise
        finally:
            release_account_operation_lock(lock_file)

    @classmethod
    def _owned_listing(
        cls,
        *,
        seller_id: int,
        listing_id: int,
    ) -> MarketplaceListing:
        listing = MarketplaceListing.query.join(Marketplace).filter(
            MarketplaceListing.id == listing_id,
            MarketplaceListing.seller_id == seller_id,
            Marketplace.code == "ozon",
        ).first()
        if listing is None or listing.account_id is None:
            raise MarketplaceWarehouseNotFound("Листинг Ozon не найден")
        return listing

    @classmethod
    def _fetch_listing_stocks(
        cls,
        *,
        listing: MarketplaceListing,
        adapter,
        credentials: MarketplaceCredentials,
    ) -> Dict[str, dict]:
        cursor = ""
        seen_cursors = set()
        stocks: Dict[str, dict] = {}
        for _page_number in range(cls.MAX_STOCK_PAGES):
            payload = {
                "limit": 100,
                "cursor": cursor,
                "offer_id": [listing.offer_id],
            }
            page = OzonStockContract.normalize_fbs_page(
                adapter.read_stocks_by_warehouse_fbs(credentials, payload)
            )
            for item in page["products"]:
                if (
                    item["offer_id"] != listing.offer_id
                    or item["product_id"] != listing.external_product_id
                ):
                    raise MarketplaceWarehouseSyncError(
                        "Ozon вернул чужой товар в warehouse stock response"
                    )
                warehouse_external_id = item["warehouse_id"]
                if warehouse_external_id in stocks:
                    raise MarketplaceWarehouseSyncError(
                        "Ozon повторил listing/warehouse stock между страницами"
                    )
                stocks[warehouse_external_id] = item
            if not page["has_next"]:
                return stocks
            next_cursor = page["cursor"]
            if next_cursor == cursor or next_cursor in seen_cursors:
                raise MarketplaceWarehouseSyncError(
                    "Ozon warehouse stock pagination зациклилась"
                )
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        raise MarketplaceWarehouseSyncError(
            "Ozon warehouse stock pagination превысила безопасный лимит"
        )

    @classmethod
    def refresh_listing_stocks(
        cls,
        *,
        seller_id: int,
        listing_id: int,
        adapter=None,
        credentials: Optional[MarketplaceCredentials] = None,
        now: Optional[datetime] = None,
    ) -> list:
        seller_id = cls._positive_integer(seller_id, "seller_id")
        listing_id = cls._positive_integer(listing_id, "listing_id")
        listing = cls._owned_listing(seller_id=seller_id, listing_id=listing_id)
        account, resolved_adapter, resolved_credentials = (
            cls._account_adapter_credentials(
                seller_id=seller_id,
                account_id=listing.account_id,
                capabilities=("stocks_read",),
                adapter=adapter,
                credentials=credentials,
                now=now,
            )
        )
        lock_file = try_account_operation_lock(account.id)
        if lock_file is None:
            raise MarketplaceWarehouseConflict(
                "Кабинет Ozon занят другой безопасной операцией"
            )
        try:
            observed_at = now or datetime.utcnow()
            stocks = cls._fetch_listing_stocks(
                listing=listing,
                adapter=resolved_adapter,
                credentials=resolved_credentials,
            )
            warehouses = {
                row.external_warehouse_id: row
                for row in MarketplaceWarehouse.query.filter_by(
                    seller_id=seller_id,
                    marketplace_id=listing.marketplace_id,
                    account_id=listing.account_id,
                    is_available=True,
                ).all()
            }
            unknown = sorted(set(stocks) - set(warehouses))
            if unknown:
                raise MarketplaceWarehouseConflict(
                    "Список складов устарел; сначала синхронизируйте склады Ozon"
                )
            existing_rows = MarketplaceWarehouseStock.query.filter_by(
                seller_id=seller_id,
                marketplace_id=listing.marketplace_id,
                account_id=listing.account_id,
                listing_id=listing.id,
            ).all()
            existing = {
                row.warehouse.external_warehouse_id: row
                for row in existing_rows
                if row.warehouse is not None
            }
            for external_id, item in stocks.items():
                row = existing.get(external_id)
                if row is None:
                    row = MarketplaceWarehouseStock(
                        seller_id=seller_id,
                        marketplace_id=listing.marketplace_id,
                        account_id=listing.account_id,
                        listing_id=listing.id,
                        warehouse_id=warehouses[external_id].id,
                        offer_id=listing.offer_id,
                        external_product_id=listing.external_product_id,
                        sku=item["sku"],
                        present=item["present"],
                        reserved=item["reserved"],
                        free_stock=item["free_stock"],
                        is_available=True,
                        sync_fingerprint=cls._fingerprint(item),
                        observed_at=observed_at,
                    )
                    db.session.add(row)
                else:
                    row.offer_id = listing.offer_id
                    row.external_product_id = listing.external_product_id
                    row.sku = item["sku"]
                    row.present = item["present"]
                    row.reserved = item["reserved"]
                    row.free_stock = item["free_stock"]
                    row.is_available = True
                    row.sync_fingerprint = cls._fingerprint(item)
                    row.observed_at = observed_at
            for external_id, row in existing.items():
                if external_id not in stocks:
                    row.is_available = False
                    row.observed_at = observed_at
            db.session.commit()
            return MarketplaceWarehouseStock.query.filter_by(
                seller_id=seller_id,
                account_id=listing.account_id,
                listing_id=listing.id,
            ).order_by(MarketplaceWarehouseStock.warehouse_id.asc()).all()
        except (OzonAPIError, OzonCommercialContractError) as exc:
            db.session.rollback()
            raise MarketplaceWarehouseSyncError(str(exc)) from None
        except Exception:
            db.session.rollback()
            raise
        finally:
            release_account_operation_lock(lock_file)

    @classmethod
    def list_warehouses(
        cls,
        *,
        seller_id: int,
        account_id: int,
        include_unavailable: bool = False,
    ) -> list:
        seller_id = cls._positive_integer(seller_id, "seller_id")
        account_id = cls._positive_integer(account_id, "account_id")
        try:
            MarketplaceAccountService.get_owned_account(
                seller_id=seller_id,
                account_id=account_id,
                marketplace_code="ozon",
            )
        except MarketplaceAccountNotFound:
            raise MarketplaceWarehouseNotFound("Кабинет Ozon не найден") from None
        if not isinstance(include_unavailable, bool):
            raise MarketplaceWarehouseValidationError(
                "include_unavailable должен быть boolean"
            )
        query = MarketplaceWarehouse.query.filter_by(
            seller_id=seller_id,
            account_id=account_id,
        )
        if not include_unavailable:
            query = query.filter(MarketplaceWarehouse.is_available.is_(True))
        return query.order_by(
            MarketplaceWarehouse.name.asc(),
            MarketplaceWarehouse.id.asc(),
        ).all()

    @classmethod
    def list_listing_stocks(
        cls,
        *,
        seller_id: int,
        listing_id: int,
        include_unavailable: bool = False,
    ) -> list:
        seller_id = cls._positive_integer(seller_id, "seller_id")
        listing_id = cls._positive_integer(listing_id, "listing_id")
        cls._owned_listing(seller_id=seller_id, listing_id=listing_id)
        if not isinstance(include_unavailable, bool):
            raise MarketplaceWarehouseValidationError(
                "include_unavailable должен быть boolean"
            )
        query = MarketplaceWarehouseStock.query.filter_by(
            seller_id=seller_id,
            listing_id=listing_id,
        )
        if not include_unavailable:
            query = query.filter(MarketplaceWarehouseStock.is_available.is_(True))
        return query.order_by(MarketplaceWarehouseStock.warehouse_id.asc()).all()
