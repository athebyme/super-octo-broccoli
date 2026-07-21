# -*- coding: utf-8 -*-
"""Admin bestseller intelligence built only from local synchronized facts.

WB actual sales and Ozon ordered analytics have different definitions.  They
may be displayed in one worklist, but financial values are summarized per
marketplace and opportunity ranks are normalized inside each marketplace.
Creating a recommendation is a local, durable handoff to the seller; it never
starts image generation or performs a marketplace write.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime, timedelta
import json
import math
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from sqlalchemy import and_, case, func, or_
from sqlalchemy.orm import aliased, joinedload

from models import (
    AdminAuditLog,
    BestsellerImageRecommendation,
    ImportedProduct,
    Marketplace,
    MarketplaceAnalyticsSync,
    MarketplaceListing,
    MarketplaceMetricFact,
    MarketplaceQualityAssessment,
    Product,
    Seller,
    SellerMarketplaceAccount,
    WBSale,
    db,
)


class AdminSalesIntelligenceError(ValueError):
    """The requested dashboard/recommendation scope is invalid."""


@dataclass(frozen=True)
class SalesDashboardFilters:
    period_code: str = "30d"
    marketplace: str = "all"
    seller_id: Optional[int] = None
    search: str = ""
    sort: str = "opportunity"
    ready_only: bool = False
    page: int = 1
    per_page: int = 50

    PERIODS = {"7d", "30d"}
    MARKETPLACES = {"all", "wb", "ozon"}
    SORTS = {"opportunity", "revenue", "units", "photos", "quality"}
    PAGE_SIZES = {25, 50, 100}

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> "SalesDashboardFilters":
        period_code = str(values.get("period", "30d") or "30d").strip()
        marketplace = str(values.get("marketplace", "all") or "all").strip()
        sort = str(values.get("sort", "opportunity") or "opportunity").strip()
        if period_code not in cls.PERIODS:
            raise AdminSalesIntelligenceError("Период должен быть 7d или 30d")
        if marketplace not in cls.MARKETPLACES:
            raise AdminSalesIntelligenceError("Неизвестный маркетплейс")
        if sort not in cls.SORTS:
            raise AdminSalesIntelligenceError("Неизвестная сортировка")

        seller_raw = values.get("seller_id")
        seller_id = None
        if seller_raw not in (None, "", "all"):
            seller_text = str(seller_raw)
            if not seller_text.isascii() or not seller_text.isdigit() or seller_text.startswith("0"):
                raise AdminSalesIntelligenceError("seller_id должен быть положительным целым")
            seller_id = int(seller_text)

        search = str(values.get("q", "") or "").strip()
        if len(search) > 160:
            raise AdminSalesIntelligenceError("Поисковый запрос слишком длинный")

        page = cls._positive_integer(values.get("page", 1), "page", maximum=100_000)
        per_page = cls._positive_integer(values.get("per_page", 50), "per_page", maximum=100)
        if per_page not in cls.PAGE_SIZES:
            raise AdminSalesIntelligenceError("per_page должен быть 25, 50 или 100")

        ready_raw = str(values.get("ready", "") or "").strip().lower()
        if ready_raw not in {"", "0", "1", "false", "true"}:
            raise AdminSalesIntelligenceError("ready должен быть boolean")
        return cls(
            period_code=period_code,
            marketplace=marketplace,
            seller_id=seller_id,
            search=search,
            sort=sort,
            ready_only=ready_raw in {"1", "true"},
            page=page,
            per_page=per_page,
        )

    @staticmethod
    def _positive_integer(value: Any, field_name: str, *, maximum: int) -> int:
        text = str(value)
        if not text.isascii() or not text.isdigit() or text.startswith("0"):
            raise AdminSalesIntelligenceError(
                f"{field_name} должен быть положительным целым"
            )
        result = int(text)
        if result > maximum:
            raise AdminSalesIntelligenceError(f"{field_name} превышает лимит {maximum}")
        return result

    def query_params(self, *, page: Optional[int] = None) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "period": self.period_code,
            "marketplace": self.marketplace,
            "sort": self.sort,
            "per_page": self.per_page,
        }
        if self.seller_id is not None:
            result["seller_id"] = self.seller_id
        if self.search:
            result["q"] = self.search
        if self.ready_only:
            result["ready"] = 1
        result["page"] = page if page is not None else self.page
        return result


class AdminSalesIntelligenceService:
    """Cross-seller local read model and safe recommendation handoff."""

    MAX_SOURCE_ROWS = 5_000
    MAX_RECOMMENDATIONS = 50
    SELLER_RECOMMENDATION_LIMIT = 50
    METRIC_CODES = {
        "ordered_revenue_rub",
        "ordered_units",
        "returned_units",
    }
    WB_DEFINITIONS = {
        "revenue": {
            "code": "seller-hub.wb-sales-v1/net-finished-price",
            "label": "Нетто-выручка продаж WB",
            "cross_marketplace_comparable": False,
        },
        "units": {
            "code": "seller-hub.wb-sales-v1/net-units",
            "label": "Нетто-продажи WB",
            "cross_marketplace_comparable": False,
        },
    }
    OZON_DEFINITIONS = {
        "revenue": {
            "code": "ozon.analytics.v1/revenue",
            "label": "Заказано на Ozon",
            "cross_marketplace_comparable": False,
        },
        "units": {
            "code": "ozon.analytics.v1/ordered_units",
            "label": "Заказано единиц на Ozon",
            "cross_marketplace_comparable": False,
        },
    }

    @staticmethod
    def _chunks(values: Sequence[int], size: int = 400) -> Iterable[Sequence[int]]:
        for index in range(0, len(values), size):
            yield values[index:index + size]

    @staticmethod
    def _json_object(raw_value: Any) -> dict:
        if isinstance(raw_value, dict):
            return raw_value
        if not isinstance(raw_value, str) or not raw_value:
            return {}
        try:
            value = json.loads(raw_value)
        except (TypeError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _photo_count(
        raw_value: Any,
        *,
        allow_wb_indices: bool = False,
    ) -> int:
        if not raw_value:
            return 0
        value = raw_value
        if isinstance(raw_value, str):
            try:
                value = json.loads(raw_value)
            except (TypeError, ValueError):
                return 0
        if not isinstance(value, list):
            return 0
        count = 0
        for item in value[:100]:
            if isinstance(item, str) and item.strip():
                count += 1
            elif (
                allow_wb_indices
                and isinstance(item, int)
                and not isinstance(item, bool)
                and item > 0
            ):
                count += 1
            elif isinstance(item, dict) and any(
                isinstance(item.get(key), str) and item.get(key).strip()
                for key in ("url", "source", "src", "original")
            ):
                count += 1
        return count

    @classmethod
    def _effective_wb_photo_count(
        cls,
        *,
        imported: Optional[ImportedProduct],
        product: Optional[Product],
    ) -> Tuple[int, Optional[str]]:
        """Use canonical photos first, then the exact linked WB gallery."""
        canonical_count = cls._photo_count(
            imported.photo_urls if imported is not None else None,
        )
        if canonical_count:
            return canonical_count, "canonical"
        gallery_count = cls._photo_count(
            product.photos_json if product is not None else None,
            allow_wb_indices=True,
        )
        if gallery_count:
            return gallery_count, "wb_gallery"
        return 0, None

    @classmethod
    def _connected_scope(
        cls,
        *,
        include_ozon: bool,
        now: datetime,
    ) -> Tuple[Dict[int, Seller], set, Dict[int, SellerMarketplaceAccount]]:
        wb_sellers = Seller.query.filter(
            Seller._wb_api_key_encrypted.isnot(None),
            func.length(func.trim(Seller._wb_api_key_encrypted)) > 0,
        ).order_by(Seller.id.asc()).all()
        sellers = {seller.id: seller for seller in wb_sellers}
        wb_ids = set(sellers)
        accounts: Dict[int, SellerMarketplaceAccount] = {}
        if include_ozon:
            account_rows = SellerMarketplaceAccount.query.options(
                joinedload(SellerMarketplaceAccount.seller),
                joinedload(SellerMarketplaceAccount.marketplace),
            ).join(
                Marketplace,
                Marketplace.id == SellerMarketplaceAccount.marketplace_id,
            ).filter(
                Marketplace.code == "ozon",
                Marketplace.is_active.is_(True),
                SellerMarketplaceAccount.is_active.is_(True),
                SellerMarketplaceAccount.connection_status == "connected",
                or_(
                    SellerMarketplaceAccount.credential_expires_at.is_(None),
                    SellerMarketplaceAccount.credential_expires_at > now,
                ),
            ).order_by(SellerMarketplaceAccount.id.asc()).all()
            for account in account_rows:
                if account.seller is None:
                    continue
                accounts[account.id] = account
                sellers[account.seller_id] = account.seller
        return sellers, wb_ids, accounts

    @classmethod
    def connected_sellers(
        cls,
        *,
        include_ozon: bool = True,
        now: Optional[datetime] = None,
    ) -> List[Dict[str, Any]]:
        current_time = now or datetime.utcnow()
        sellers, wb_ids, accounts = cls._connected_scope(
            include_ozon=include_ozon,
            now=current_time,
        )
        ozon_ids = {account.seller_id for account in accounts.values()}
        return [
            {
                "id": seller.id,
                "company_name": seller.company_name,
                "marketplaces": [
                    code for code, enabled in (
                        ("wb", seller.id in wb_ids),
                        ("ozon", seller.id in ozon_ids),
                    ) if enabled
                ],
            }
            for seller in sorted(
                sellers.values(),
                key=lambda item: ((item.company_name or "").casefold(), item.id),
            )
        ]

    @classmethod
    def _imported_products_for_legacy(
        cls,
        products: Sequence[Product],
    ) -> Tuple[Dict[int, ImportedProduct], set]:
        product_ids = sorted({product.id for product in products if product is not None})
        grouped: Dict[int, List[ImportedProduct]] = {}
        for chunk in cls._chunks(product_ids):
            items = ImportedProduct.query.filter(
                ImportedProduct.product_id.in_(chunk),
            ).order_by(ImportedProduct.id.desc()).all()
            for item in items:
                grouped.setdefault(item.product_id, []).append(item)
        exact: Dict[int, ImportedProduct] = {}
        ambiguous = set()
        for product in products:
            candidates = [
                item for item in grouped.get(product.id, [])
                if item.seller_id == product.seller_id
            ]
            if len(candidates) == 1:
                exact[product.id] = candidates[0]
            elif len(candidates) > 1:
                ambiguous.add(product.id)
        return exact, ambiguous

    @classmethod
    def _wb_rows(
        cls,
        *,
        seller_ids: Sequence[int],
        period_days: int,
        now: datetime,
    ) -> Tuple[List[Dict[str, Any]], bool]:
        if not seller_ids:
            return [], False
        period_start = (now - timedelta(days=period_days - 1)).replace(
            hour=0, minute=0, second=0, microsecond=0,
        )
        unit_value = case((WBSale.is_return.is_(True), -1), else_=1)
        gross_unit = case((WBSale.is_return.is_(True), 0), else_=1)
        return_unit = case((WBSale.is_return.is_(True), 1), else_=0)
        price_value = func.abs(func.coalesce(
            func.nullif(WBSale.finished_price, 0),
            WBSale.price_with_disc,
            0,
        ))
        revenue_value = case(
            (WBSale.is_return.is_(True), -price_value),
            else_=price_value,
        )
        aggregates = db.session.query(
            WBSale.seller_id.label("seller_id"),
            WBSale.nm_id.label("nm_id"),
            func.sum(unit_value).label("units"),
            func.sum(revenue_value).label("revenue"),
            func.sum(gross_unit).label("gross_units"),
            func.sum(return_unit).label("return_units"),
            func.max(WBSale.subject).label("subject"),
            func.max(WBSale.supplier_article).label("supplier_article"),
            func.max(WBSale.last_change_date).label("observed_at"),
        ).filter(
            WBSale.seller_id.in_(seller_ids),
            WBSale.nm_id.isnot(None),
            WBSale.date >= period_start,
        ).group_by(
            WBSale.seller_id,
            WBSale.nm_id,
        ).order_by(
            func.sum(revenue_value).desc(),
            WBSale.seller_id.asc(),
            WBSale.nm_id.asc(),
        ).limit(cls.MAX_SOURCE_ROWS + 1).all()
        truncated = len(aggregates) > cls.MAX_SOURCE_ROWS
        aggregates = aggregates[:cls.MAX_SOURCE_ROWS]

        by_seller: Dict[int, List[int]] = {}
        for item in aggregates:
            by_seller.setdefault(item.seller_id, []).append(int(item.nm_id))
        products_by_scope: Dict[Tuple[int, int], Product] = {}
        for seller_id, nm_ids in by_seller.items():
            unique_nm_ids = sorted(set(nm_ids))
            for chunk in cls._chunks(unique_nm_ids):
                products = Product.query.filter(
                    Product.seller_id == seller_id,
                    Product.nm_id.in_(chunk),
                ).order_by(Product.id.asc()).all()
                for product in products:
                    products_by_scope.setdefault(
                        (product.seller_id, int(product.nm_id)),
                        product,
                    )
        products = list(products_by_scope.values())
        imported_by_product, ambiguous_products = cls._imported_products_for_legacy(
            products,
        )
        sellers = {
            seller.id: seller
            for seller in Seller.query.filter(Seller.id.in_(seller_ids)).all()
        }

        rows: List[Dict[str, Any]] = []
        for aggregate in aggregates:
            product = products_by_scope.get((aggregate.seller_id, int(aggregate.nm_id)))
            imported = imported_by_product.get(product.id) if product is not None else None
            seller = sellers.get(aggregate.seller_id)
            photo_count, photo_source = cls._effective_wb_photo_count(
                imported=imported,
                product=product,
            )
            blocker = None
            if product is None:
                blocker = "Карточка WB не найдена в локальном каталоге"
            elif product.id in ambiguous_products:
                blocker = "У карточки несколько неоднозначных внутренних источников"
            elif imported is None:
                blocker = "Карточка не связана с внутренним товаром Фотостудии"
            elif photo_count < 1:
                blocker = "Нет исходной фотографии для безопасной генерации"
            units = float(aggregate.units or 0)
            revenue = round(float(aggregate.revenue or 0), 2)
            gross_units = int(aggregate.gross_units or 0)
            return_units = int(aggregate.return_units or 0)
            rows.append({
                "scope_key": (
                    f"wb:product:{product.id}"
                    if product is not None
                    else f"wb:nm:{aggregate.seller_id}:{int(aggregate.nm_id)}"
                ),
                "marketplace_code": "wb",
                "marketplace_label": "Wildberries",
                "seller_id": aggregate.seller_id,
                "seller_company": seller.company_name if seller else f"Продавец #{aggregate.seller_id}",
                "account_id": None,
                "account_label": None,
                "legacy_product_id": product.id if product is not None else None,
                "listing_id": None,
                "imported_product_id": imported.id if imported is not None else None,
                "external_id": str(int(aggregate.nm_id)),
                "vendor_code": (
                    product.vendor_code if product is not None
                    else aggregate.supplier_article
                ) or "",
                "title": (
                    product.title if product is not None else aggregate.subject
                ) or f"WB {int(aggregate.nm_id)}",
                "units": units,
                "revenue_rub": revenue,
                "gross_units": gross_units,
                "return_units": return_units,
                "return_rate": round(100 * return_units / gross_units, 1) if gross_units else 0,
                "photo_count": photo_count,
                "photo_source": photo_source,
                "quality_score": (
                    float(product.quality_score)
                    if product is not None and product.quality_score is not None
                    else None
                ),
                "source_observed_at": aggregate.observed_at,
                "metric_definitions": cls.WB_DEFINITIONS,
                "metric_units_label": "нетто-продажи",
                "metric_revenue_label": "нетто-выручка",
                "generation_ready": blocker is None,
                "generation_blocker": blocker,
                "target_available": product is not None,
            })
        return rows, truncated

    @classmethod
    def _latest_ozon_syncs(
        cls,
        *,
        account_ids: Sequence[int],
        period_code: str,
    ) -> List[MarketplaceAnalyticsSync]:
        if not account_ids:
            return []
        newer = aliased(MarketplaceAnalyticsSync)
        newer_exists = db.session.query(newer.id).filter(
            newer.account_id == MarketplaceAnalyticsSync.account_id,
            newer.period_code == period_code,
            newer.status == "completed",
            or_(
                newer.completed_at > MarketplaceAnalyticsSync.completed_at,
                and_(
                    newer.completed_at == MarketplaceAnalyticsSync.completed_at,
                    newer.id > MarketplaceAnalyticsSync.id,
                ),
                and_(
                    MarketplaceAnalyticsSync.completed_at.is_(None),
                    newer.completed_at.isnot(None),
                ),
                and_(
                    MarketplaceAnalyticsSync.completed_at.is_(None),
                    newer.completed_at.is_(None),
                    newer.id > MarketplaceAnalyticsSync.id,
                ),
            ),
        ).exists()
        return MarketplaceAnalyticsSync.query.filter(
            MarketplaceAnalyticsSync.account_id.in_(account_ids),
            MarketplaceAnalyticsSync.period_code == period_code,
            MarketplaceAnalyticsSync.status == "completed",
            ~newer_exists,
        ).order_by(MarketplaceAnalyticsSync.account_id.asc()).all()

    @classmethod
    def _ozon_rows(
        cls,
        *,
        accounts: Dict[int, SellerMarketplaceAccount],
        period_code: str,
    ) -> Tuple[List[Dict[str, Any]], bool]:
        syncs = cls._latest_ozon_syncs(
            account_ids=sorted(accounts),
            period_code=period_code,
        )
        if not syncs:
            return [], False
        sync_by_id = {sync.id: sync for sync in syncs}
        facts_by_scope: Dict[Tuple[int, int], Dict[str, Any]] = {}
        sync_ids = sorted(sync_by_id)
        for chunk in cls._chunks(sync_ids):
            facts = MarketplaceMetricFact.query.filter(
                MarketplaceMetricFact.sync_id.in_(chunk),
                MarketplaceMetricFact.dimension_kind == "listing",
                MarketplaceMetricFact.listing_id.isnot(None),
                MarketplaceMetricFact.metric_code.in_(cls.METRIC_CODES),
            ).order_by(MarketplaceMetricFact.id.asc()).all()
            for fact in facts:
                sync = sync_by_id.get(fact.sync_id)
                if sync is None or fact.account_id != sync.account_id or fact.seller_id != sync.seller_id:
                    continue
                scope = (fact.sync_id, fact.listing_id)
                row = facts_by_scope.setdefault(scope, {
                    "metrics": {},
                    "observed_at": fact.observed_at,
                })
                row["metrics"][fact.metric_code] = (
                    row["metrics"].get(fact.metric_code, 0.0)
                    + float(fact.metric_value or 0)
                )
                if fact.observed_at and (
                    row["observed_at"] is None or fact.observed_at > row["observed_at"]
                ):
                    row["observed_at"] = fact.observed_at

        ranked_scopes = sorted(
            facts_by_scope.items(),
            key=lambda item: (
                -item[1]["metrics"].get("ordered_revenue_rub", 0),
                item[0],
            ),
        )
        truncated = len(ranked_scopes) > cls.MAX_SOURCE_ROWS
        ranked_scopes = ranked_scopes[:cls.MAX_SOURCE_ROWS]
        listing_ids = sorted({scope[1] for scope, _ in ranked_scopes})
        listings: Dict[int, MarketplaceListing] = {}
        for chunk in cls._chunks(listing_ids):
            items = MarketplaceListing.query.options(
                joinedload(MarketplaceListing.imported_product),
                joinedload(MarketplaceListing.account),
            ).filter(MarketplaceListing.id.in_(chunk)).all()
            listings.update({item.id: item for item in items})
        quality_by_listing: Dict[int, MarketplaceQualityAssessment] = {}
        for chunk in cls._chunks(listing_ids):
            assessments = MarketplaceQualityAssessment.query.filter(
                MarketplaceQualityAssessment.listing_id.in_(chunk),
            ).all()
            quality_by_listing.update({item.listing_id: item for item in assessments})

        rows: List[Dict[str, Any]] = []
        for (sync_id, listing_id), metric_row in ranked_scopes:
            sync = sync_by_id[sync_id]
            account = accounts.get(sync.account_id)
            listing = listings.get(listing_id)
            if (
                account is None
                or listing is None
                or listing.seller_id != sync.seller_id
                or listing.account_id != sync.account_id
                or listing.marketplace_id != sync.marketplace_id
            ):
                continue
            imported = listing.imported_product
            photo_count = cls._photo_count(imported.photo_urls if imported else None)
            blocker = None
            if listing.canonical_link_status != "linked" or imported is None:
                blocker = "Ozon-листинг не связан с общей внутренней карточкой"
            elif imported.seller_id != listing.seller_id:
                blocker = "Нарушена связь продавца с внутренней карточкой"
            elif not listing.is_available or listing.is_archived:
                blocker = "Ozon-листинг сейчас недоступен или находится в архиве"
            elif not account.is_active or account.connection_status != "connected":
                blocker = "Кабинет Ozon больше не активен"
            elif photo_count < 1:
                blocker = "Нет исходной фотографии для безопасной генерации"
            metrics = metric_row["metrics"]
            units = float(metrics.get("ordered_units", 0))
            returned_raw = metrics.get("returned_units")
            returned = (
                float(returned_raw) if returned_raw is not None else None
            )
            quality = quality_by_listing.get(listing.id)
            rows.append({
                "scope_key": f"ozon:account:{account.id}:listing:{listing.id}",
                "marketplace_code": "ozon",
                "marketplace_label": "Ozon",
                "seller_id": listing.seller_id,
                "seller_company": account.seller.company_name,
                "account_id": account.id,
                "account_label": account.label,
                "legacy_product_id": None,
                "listing_id": listing.id,
                "imported_product_id": imported.id if imported is not None else None,
                "external_id": listing.offer_id,
                "vendor_code": listing.offer_id,
                "title": listing.title or (imported.title if imported else None) or f"Ozon {listing.id}",
                "units": units,
                "revenue_rub": round(float(metrics.get("ordered_revenue_rub", 0)), 2),
                "gross_units": units,
                "return_units": returned,
                "return_rate": (
                    round(100 * returned / units, 1)
                    if returned is not None and units else None
                ),
                "photo_count": photo_count,
                "photo_source": "canonical" if photo_count else None,
                "quality_score": (
                    float(quality.score)
                    if quality is not None and quality.score is not None
                    else None
                ),
                "source_observed_at": metric_row["observed_at"] or sync.completed_at,
                "metric_definitions": cls.OZON_DEFINITIONS,
                "metric_units_label": "заказано единиц",
                "metric_revenue_label": "заказано на сумму",
                "generation_ready": blocker is None,
                "generation_blocker": blocker,
                "target_available": bool(listing.is_available and not listing.is_archived),
            })
        return rows, truncated

    @staticmethod
    def _percentile(value: float, sorted_values: Sequence[float]) -> float:
        if not sorted_values:
            return 0.0
        if value <= 0 and sorted_values[-1] <= 0:
            return 0.0
        if len(sorted_values) == 1:
            return 1.0 if value > 0 else 0.0
        return (bisect_right(sorted_values, value) - 1) / (len(sorted_values) - 1)

    @classmethod
    def _score_rows(cls, rows: List[Dict[str, Any]]) -> None:
        by_marketplace: Dict[str, List[Dict[str, Any]]] = {}
        for row in rows:
            by_marketplace.setdefault(row["marketplace_code"], []).append(row)
        for marketplace_rows in by_marketplace.values():
            revenues = sorted(max(0.0, float(row["revenue_rub"])) for row in marketplace_rows)
            units = sorted(max(0.0, float(row["units"])) for row in marketplace_rows)
            for row in marketplace_rows:
                revenue_rank = cls._percentile(max(0.0, row["revenue_rub"]), revenues)
                units_rank = cls._percentile(max(0.0, row["units"]), units)
                sales_strength = 0.65 * revenue_rank + 0.35 * units_rank
                photo_count = row["photo_count"]
                if photo_count <= 1:
                    media_gap = 1.0
                elif photo_count == 2:
                    media_gap = 0.8
                elif photo_count == 3:
                    media_gap = 0.6
                elif photo_count == 4:
                    media_gap = 0.4
                elif photo_count == 5:
                    media_gap = 0.2
                else:
                    media_gap = 0.0
                quality_score = row["quality_score"]
                quality_gap = (
                    max(0.0, min(1.0, (100.0 - quality_score) / 100.0))
                    if quality_score is not None else 0.35
                )
                opportunity = 100 * (
                    0.72 * sales_strength
                    + 0.20 * media_gap
                    + 0.08 * quality_gap
                )
                positive_sales = row["units"] > 0 or row["revenue_rub"] > 0
                row["sales_percentile"] = round(100 * sales_strength, 1)
                row["opportunity_score"] = round(max(0.0, min(100.0, opportunity)), 1)
                row["recommendable"] = bool(row["generation_ready"] and positive_sales)
                row["is_candidate"] = bool(
                    row["recommendable"]
                    and sales_strength >= 0.60
                    and (photo_count < 5 or (quality_score is not None and quality_score < 75))
                )
                if photo_count == 0:
                    reason = "Сильные продажи, но нет безопасного исходника для генерации"
                elif photo_count == 1:
                    reason = "Сильные продажи и только одно исходное фото"
                elif photo_count < 5:
                    reason = f"Сильные продажи; в галерее только {photo_count} исходных фото"
                elif quality_score is not None and quality_score < 75:
                    reason = "Сильные продажи при низком общем Quality Score — стоит проверить визуал"
                else:
                    reason = "Сильный товар — кандидат для аккуратного A/B-теста визуала"
                row["reason"] = reason
                row["rank_scope"] = "marketplace"

    @classmethod
    def dashboard(
        cls,
        filters: SalesDashboardFilters,
        *,
        include_ozon: bool = True,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        if not isinstance(filters, SalesDashboardFilters):
            raise AdminSalesIntelligenceError("Некорректные фильтры")
        current_time = now or datetime.utcnow()
        sellers, wb_ids, accounts = cls._connected_scope(
            include_ozon=include_ozon,
            now=current_time,
        )
        all_wb_ids = set(wb_ids)
        all_accounts = dict(accounts)
        if filters.seller_id is not None:
            if filters.seller_id not in sellers:
                wb_ids = set()
                accounts = {}
            else:
                wb_ids &= {filters.seller_id}
                accounts = {
                    account_id: account
                    for account_id, account in accounts.items()
                    if account.seller_id == filters.seller_id
                }

        rows: List[Dict[str, Any]] = []
        truncated = False
        if filters.marketplace in {"all", "wb"}:
            wb_rows, wb_truncated = cls._wb_rows(
                seller_ids=sorted(wb_ids),
                period_days=7 if filters.period_code == "7d" else 30,
                now=current_time,
            )
            rows.extend(wb_rows)
            truncated = truncated or wb_truncated
        if include_ozon and filters.marketplace in {"all", "ozon"}:
            ozon_rows, ozon_truncated = cls._ozon_rows(
                accounts=accounts,
                period_code=filters.period_code,
            )
            rows.extend(ozon_rows)
            truncated = truncated or ozon_truncated

        cls._score_rows(rows)
        needle = filters.search.casefold()
        if needle:
            rows = [
                row for row in rows
                if any(
                    needle in str(value or "").casefold()
                    for value in (
                        row["title"], row["vendor_code"], row["external_id"],
                        row["seller_company"], row["account_label"],
                    )
                )
            ]
        if filters.ready_only:
            rows = [row for row in rows if row["recommendable"]]

        summaries = cls._summaries(rows)
        if filters.sort == "revenue":
            sort_key = lambda row: (-row["revenue_rub"], -row["units"], row["scope_key"])
        elif filters.sort == "units":
            sort_key = lambda row: (-row["units"], -row["revenue_rub"], row["scope_key"])
        elif filters.sort == "photos":
            sort_key = lambda row: (row["photo_count"], -row["opportunity_score"], row["scope_key"])
        elif filters.sort == "quality":
            sort_key = lambda row: (
                row["quality_score"] is None,
                row["quality_score"] if row["quality_score"] is not None else math.inf,
                -row["opportunity_score"],
            )
        else:
            sort_key = lambda row: (-row["opportunity_score"], -row["revenue_rub"], row["scope_key"])
        rows.sort(key=sort_key)

        total = len(rows)
        pages = math.ceil(total / filters.per_page) if total else 0
        page = min(filters.page, pages) if pages else 1
        start = (page - 1) * filters.per_page
        page_rows = rows[start:start + filters.per_page]
        cls._attach_recommendation_status(page_rows)
        return {
            "items": page_rows,
            "summaries": summaries,
            "total": total,
            "candidate_count": sum(1 for row in rows if row["is_candidate"]),
            "ready_count": sum(1 for row in rows if row["recommendable"]),
            "pagination": {
                "page": page,
                "per_page": filters.per_page,
                "pages": pages,
                "has_prev": page > 1,
                "has_next": page < pages,
                "from": start + 1 if page_rows else 0,
                "to": start + len(page_rows),
            },
            "truncated": truncated,
            "connected_sellers": cls._seller_options(
                sellers,
                all_wb_ids,
                all_accounts,
            ),
            "include_ozon": include_ozon,
            "comparison": {
                "cross_marketplace_financial_rollup": False,
                "rank_scope": "marketplace",
                "message": (
                    "WB и Ozon используют разные определения продаж. "
                    "Итоги показаны раздельно, а приоритет нормализован внутри каждого маркетплейса."
                ),
            },
        }

    @staticmethod
    def _seller_options(
        sellers: Dict[int, Seller],
        wb_ids: set,
        accounts: Dict[int, SellerMarketplaceAccount],
    ) -> List[Dict[str, Any]]:
        ozon_ids = {account.seller_id for account in accounts.values()}
        return [
            {
                "id": seller.id,
                "company_name": seller.company_name,
                "marketplaces": [
                    code for code, enabled in (
                        ("wb", seller.id in wb_ids),
                        ("ozon", seller.id in ozon_ids),
                    ) if enabled
                ],
            }
            for seller in sorted(
                sellers.values(),
                key=lambda item: ((item.company_name or "").casefold(), item.id),
            )
        ]

    @staticmethod
    def _summaries(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        summaries: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            summary = summaries.setdefault(row["marketplace_code"], {
                "marketplace_code": row["marketplace_code"],
                "marketplace_label": row["marketplace_label"],
                "product_count": 0,
                "units": 0.0,
                "revenue_rub": 0.0,
                "seller_ids": set(),
                "ready_count": 0,
                "observed_at": None,
                "metric_units_label": row["metric_units_label"],
                "metric_revenue_label": row["metric_revenue_label"],
            })
            summary["product_count"] += 1
            summary["units"] += row["units"]
            summary["revenue_rub"] += row["revenue_rub"]
            summary["seller_ids"].add(row["seller_id"])
            summary["ready_count"] += int(row["recommendable"])
            observed = row["source_observed_at"]
            if observed and (summary["observed_at"] is None or observed > summary["observed_at"]):
                summary["observed_at"] = observed
        result = []
        for code in ("wb", "ozon"):
            if code not in summaries:
                continue
            summary = summaries[code]
            summary["units"] = round(summary["units"], 2)
            summary["revenue_rub"] = round(summary["revenue_rub"], 2)
            summary["seller_count"] = len(summary.pop("seller_ids"))
            result.append(summary)
        return result

    @staticmethod
    def _attach_recommendation_status(rows: Sequence[Dict[str, Any]]) -> None:
        keys = [row["scope_key"] for row in rows]
        if not keys:
            return
        recommendations = BestsellerImageRecommendation.query.filter(
            BestsellerImageRecommendation.scope_key.in_(keys),
        ).all()
        by_scope = {
            (item.seller_id, item.scope_key): item
            for item in recommendations
        }
        for row in rows:
            item = by_scope.get((row["seller_id"], row["scope_key"]))
            row["recommendation"] = (
                {"id": item.id, "status": item.status}
                if item is not None else None
            )

    @classmethod
    def recommend(
        cls,
        *,
        filters: SalesDashboardFilters,
        row_keys: Sequence[str],
        admin_user_id: int,
        include_ozon: bool = True,
        remote_addr: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        if not isinstance(admin_user_id, int) or isinstance(admin_user_id, bool) or admin_user_id <= 0:
            raise AdminSalesIntelligenceError("Некорректный администратор")
        if not isinstance(row_keys, (list, tuple)):
            raise AdminSalesIntelligenceError("Выберите товары")
        normalized: List[str] = []
        for value in row_keys:
            if not isinstance(value, str) or not value or len(value) > 220:
                continue
            if value not in normalized:
                normalized.append(value)
            if len(normalized) > cls.MAX_RECOMMENDATIONS:
                raise AdminSalesIntelligenceError(
                    f"За один раз можно рекомендовать не больше {cls.MAX_RECOMMENDATIONS} товаров"
                )
        if not normalized:
            raise AdminSalesIntelligenceError("Выберите хотя бы один товар")

        current_time = now or datetime.utcnow()
        dashboard = cls.dashboard(
            filters,
            include_ozon=include_ozon,
            now=current_time,
        )
        grounded = {
            row["scope_key"]: row
            for row in dashboard["items"]
            if row["recommendable"]
        }
        selected = [grounded[key] for key in normalized if key in grounded]
        skipped = len(normalized) - len(selected)
        if not selected:
            raise AdminSalesIntelligenceError(
                "Выбранные товары больше не готовы к передаче в Фотостудию"
            )

        existing_rows = BestsellerImageRecommendation.query.filter(
            BestsellerImageRecommendation.scope_key.in_([row["scope_key"] for row in selected]),
        ).all()
        existing = {
            (item.seller_id, item.scope_key): item
            for item in existing_rows
        }
        created = 0
        updated = 0
        recommendation_rows = []
        for row in selected:
            key = (row["seller_id"], row["scope_key"])
            recommendation = existing.get(key)
            if recommendation is None:
                recommendation = BestsellerImageRecommendation(
                    seller_id=row["seller_id"],
                    scope_key=row["scope_key"],
                    created_at=current_time,
                )
                db.session.add(recommendation)
                created += 1
            else:
                updated += 1
            recommendation.imported_product_id = row["imported_product_id"]
            recommendation.marketplace_code = row["marketplace_code"]
            recommendation.account_id = row["account_id"]
            recommendation.marketplace_listing_id = row["listing_id"]
            recommendation.legacy_product_id = row["legacy_product_id"]
            recommendation.period_code = filters.period_code
            recommendation.status = "recommended"
            recommendation.opportunity_score = row["opportunity_score"]
            recommendation.units = max(0.0, float(row["units"]))
            recommendation.revenue_rub = max(0.0, float(row["revenue_rub"]))
            recommendation.photo_count = row["photo_count"]
            recommendation.quality_score = row["quality_score"]
            recommendation.source_observed_at = row["source_observed_at"]
            recommendation.metric_definitions_json = json.dumps(
                row["metric_definitions"], ensure_ascii=False, separators=(",", ":"),
            )
            recommendation.snapshot_json = json.dumps({
                "title": str(row["title"] or "")[:500],
                "external_id": str(row["external_id"] or "")[:200],
                "reason": str(row["reason"] or "")[:500],
                "sales_percentile": row["sales_percentile"],
                "rank_scope": "marketplace",
            }, ensure_ascii=False, separators=(",", ":"))
            recommendation.recommended_by_user_id = admin_user_id
            recommendation.reviewed_by_user_id = None
            recommendation.reviewed_at = None
            recommendation.updated_at = current_time
            recommendation_rows.append(recommendation)

        db.session.flush()
        seller_ids = sorted({item.seller_id for item in recommendation_rows})
        audit = AdminAuditLog(
            admin_user_id=admin_user_id,
            action="recommend_bestseller_images",
            target_type="bestseller_image_batch",
            target_id=seller_ids[0] if len(seller_ids) == 1 else None,
            details=json.dumps({
                "recommendation_ids": [item.id for item in recommendation_rows],
                "seller_ids": seller_ids,
                "marketplaces": sorted({item.marketplace_code for item in recommendation_rows}),
                "period_code": filters.period_code,
                "created": created,
                "updated": updated,
                "skipped": skipped,
                "provider_calls": 0,
            }, ensure_ascii=False, separators=(",", ":")),
            ip_address=(remote_addr or "")[:45] or None,
        )
        db.session.add(audit)
        db.session.commit()
        return {
            "created": created,
            "updated": updated,
            "skipped": skipped,
            "total": len(recommendation_rows),
            "recommendation_ids": [item.id for item in recommendation_rows],
        }

    @classmethod
    def seller_recommendations(
        cls,
        *,
        seller_id: int,
        limit: int = SELLER_RECOMMENDATION_LIMIT,
    ) -> List[Dict[str, Any]]:
        if not isinstance(seller_id, int) or isinstance(seller_id, bool) or seller_id <= 0:
            raise AdminSalesIntelligenceError("Некорректный продавец")
        limit = max(1, min(int(limit), cls.SELLER_RECOMMENDATION_LIMIT))
        items = BestsellerImageRecommendation.query.options(
            joinedload(BestsellerImageRecommendation.imported_product),
            joinedload(BestsellerImageRecommendation.marketplace_listing),
            joinedload(BestsellerImageRecommendation.account),
            joinedload(BestsellerImageRecommendation.legacy_product),
        ).filter(
            BestsellerImageRecommendation.seller_id == seller_id,
            BestsellerImageRecommendation.status == "recommended",
        ).order_by(
            BestsellerImageRecommendation.opportunity_score.desc(),
            BestsellerImageRecommendation.updated_at.desc(),
            BestsellerImageRecommendation.id.desc(),
        ).limit(limit).all()
        result = []
        for item in items:
            product = item.imported_product
            if product is None or product.seller_id != seller_id:
                continue
            snapshot = cls._json_object(item.snapshot_json)
            listing_id = None
            if item.marketplace_code == "ozon":
                current_photo_count = cls._photo_count(product.photo_urls)
                target_ready = current_photo_count > 0
                listing = item.marketplace_listing
                account = item.account
                target_ready = bool(
                    target_ready
                    and listing is not None
                    and account is not None
                    and listing.seller_id == seller_id
                    and listing.account_id == account.id
                    and listing.imported_product_id == product.id
                    and listing.canonical_link_status == "linked"
                    and listing.is_available
                    and not listing.is_archived
                    and account.seller_id == seller_id
                    and account.is_active
                )
                if target_ready:
                    listing_id = listing.id
            else:
                legacy_product = item.legacy_product
                current_photo_count, _photo_source = cls._effective_wb_photo_count(
                    imported=product,
                    product=legacy_product,
                )
                target_ready = current_photo_count > 0
                target_ready = bool(
                    target_ready
                    and legacy_product is not None
                    and legacy_product.seller_id == seller_id
                    and product.product_id == legacy_product.id
                )
            result.append({
                "id": item.id,
                "product_id": product.id,
                "listing_id": listing_id,
                "marketplace_code": item.marketplace_code,
                "marketplace_label": "Ozon" if item.marketplace_code == "ozon" else "Wildberries",
                "title": product.title or snapshot.get("title") or f"Товар {product.id}",
                "reason": snapshot.get("reason") or "Сильный товар для проверки нового визуала",
                "opportunity_score": round(item.opportunity_score, 1),
                "units": item.units,
                "revenue_rub": item.revenue_rub,
                "period_code": item.period_code,
                "photo_count": current_photo_count,
                "target_ready": target_ready,
                "updated_at": item.updated_at.isoformat() if item.updated_at else None,
            })
        return result

    @classmethod
    def active_product_ids(cls, *, seller_id: int) -> List[int]:
        return [
            item[0]
            for item in db.session.query(
                BestsellerImageRecommendation.imported_product_id,
            ).filter(
                BestsellerImageRecommendation.seller_id == seller_id,
                BestsellerImageRecommendation.status == "recommended",
                BestsellerImageRecommendation.imported_product_id.isnot(None),
            ).order_by(
                BestsellerImageRecommendation.opportunity_score.desc(),
                BestsellerImageRecommendation.id.desc(),
            ).limit(cls.SELLER_RECOMMENDATION_LIMIT).all()
        ]

    @classmethod
    def review_recommendation(
        cls,
        *,
        seller_id: int,
        recommendation_id: int,
        user_id: int,
        status: str,
        now: Optional[datetime] = None,
    ) -> BestsellerImageRecommendation:
        if status not in {"dismissed", "completed"}:
            raise AdminSalesIntelligenceError("Неизвестный статус рекомендации")
        recommendation = BestsellerImageRecommendation.query.filter_by(
            id=recommendation_id,
            seller_id=seller_id,
        ).first()
        if recommendation is None:
            raise AdminSalesIntelligenceError("Рекомендация не найдена")
        recommendation.status = status
        recommendation.reviewed_by_user_id = user_id
        recommendation.reviewed_at = now or datetime.utcnow()
        recommendation.updated_at = recommendation.reviewed_at
        db.session.commit()
        return recommendation
