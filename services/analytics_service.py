# -*- coding: utf-8 -*-
"""
Сервис аналитики продаж.

Агрегирует данные из WB Analytics API (воронка продаж v3),
кэширует снимки в БД, предоставляет данные для дашборда.
"""
import logging
from datetime import date, datetime, timedelta
from typing import Dict, Any, Optional, List

from models import db, AnalyticsSnapshot, ProductAnalytics, Seller, APILog
from services.wb_api_client import WildberriesAPIClient, WBAPIException

logger = logging.getLogger('analytics_service')


class AnalyticsService:
    """Сервис для работы с аналитикой продаж"""

    CACHE_TTL_HOURS = 4  # Кэш снимка актуален 4 часа

    @staticmethod
    def _get_wb_client(seller: Seller) -> WildberriesAPIClient:
        return WildberriesAPIClient(
            api_key=seller.wb_api_key,
            db_logger_callback=lambda **kwargs: APILog.log_request(**kwargs)
        )

    @staticmethod
    def _calc_period(period_code: str) -> tuple:
        """Вычислить даты начала/конца из кода периода."""
        today = date.today()
        if period_code == '7d':
            start = today - timedelta(days=7)
        elif period_code == '30d':
            start = today - timedelta(days=30)
        elif period_code == '90d':
            start = today - timedelta(days=90)
        elif period_code == '1y':
            start = today - timedelta(days=365)
        else:
            start = today - timedelta(days=30)
        return start, today

    @staticmethod
    def _calc_past_period(start: date, end: date) -> tuple:
        """Вычислить предыдущий аналогичный период для сравнения."""
        delta = (end - start).days
        past_end = start - timedelta(days=1)
        past_start = past_end - timedelta(days=delta)
        return past_start, past_end

    @classmethod
    def get_cached_snapshot(cls, seller_id: int, period_start: date, period_end: date) -> Optional[AnalyticsSnapshot]:
        """Получить актуальный кэшированный снимок (не старше CACHE_TTL_HOURS)."""
        cutoff = datetime.utcnow() - timedelta(hours=cls.CACHE_TTL_HOURS)
        return AnalyticsSnapshot.query.filter(
            AnalyticsSnapshot.seller_id == seller_id,
            AnalyticsSnapshot.period_start == period_start,
            AnalyticsSnapshot.period_end == period_end,
            AnalyticsSnapshot.created_at >= cutoff,
        ).order_by(AnalyticsSnapshot.created_at.desc()).first()

    @classmethod
    def fetch_and_cache_snapshot(
        cls,
        seller: Seller,
        period_code: str = '30d',
        force: bool = False,
    ) -> Dict[str, Any]:
        """
        Получить аналитику за период. Сначала проверяет кэш, при необходимости
        загружает данные из WB API и сохраняет снимок.

        Args:
            seller: Объект продавца
            period_code: Код периода ('7d', '30d', '90d', '1y')
            force: Принудительное обновление, игнорировать кэш

        Returns:
            Словарь с KPI, динамикой, графиками и топ-товарами
        """
        period_start, period_end = cls._calc_period(period_code)

        # Проверяем кэш
        if not force:
            cached = cls.get_cached_snapshot(seller.id, period_start, period_end)
            if cached and (cached.orders_count or cached.open_card_count):
                logger.info(f"Using cached analytics snapshot for seller={seller.id}, period={period_code}")
                return cached.to_dict()
            elif cached:
                logger.info(f"Cached snapshot is empty (0 orders), refreshing from API")

        # При force-обновлении удаляем старые пустые снимки
        if force:
            AnalyticsSnapshot.query.filter(
                AnalyticsSnapshot.seller_id == seller.id,
                AnalyticsSnapshot.period_start == period_start,
                AnalyticsSnapshot.period_end == period_end,
            ).delete()
            db.session.commit()

        # Загружаем из WB API
        logger.info(f"Fetching analytics from WB API for seller={seller.id}, period={period_code}")
        past_start, past_end = cls._calc_past_period(period_start, period_end)

        client = cls._get_wb_client(seller)

        try:
            # 1. Получаем статистику по всем товарам за выбранный период
            result = client.get_sales_funnel_products(
                period_start=period_start.isoformat(),
                period_end=period_end.isoformat(),
                past_period_start=past_start.isoformat(),
                past_period_end=past_end.isoformat(),
                order_by={'field': 'orderSum', 'mode': 'desc'},
                limit=50,
                offset=0,
                seller_id=seller.id,
            )

            logger.info(f"WB API raw response keys: {list(result.keys()) if isinstance(result, dict) else type(result)}")

            # Проверяем на ошибку API
            if isinstance(result, dict) and result.get('error'):
                error_text = result.get('errorText', 'Unknown error')
                additional = result.get('additionalErrors', [])
                logger.error(f"WB Analytics API error: {error_text}, additional: {additional}")
                return cls._empty_snapshot(period_start, period_end)

            # Извлекаем products — структура может быть разной
            data = result.get('data', result)
            products = []
            if isinstance(data, dict):
                products = data.get('products', [])
                # Если products пуст, логируем data keys для диагностики
                if not products:
                    logger.warning(f"WB API data keys (no products found): {list(data.keys())}")
                    # Попытка: может products лежит на уровне выше
                    products = result.get('products', [])
            elif isinstance(data, list):
                products = data

            logger.info(f"WB API returned {len(products)} products for analytics")
            if products:
                # Логируем структуру первого продукта для диагностики
                first = products[0]
                logger.info(f"First product keys: {list(first.keys())}")
                if 'statistic' in first:
                    stat_keys = list(first['statistic'].keys())
                    logger.info(f"First product statistic keys: {stat_keys}")
                    selected = first['statistic'].get('selected', {})
                    logger.info(f"First product selected keys: {list(selected.keys())}")
                    logger.info(f"First product orderSum={selected.get('orderSum')}, orderCount={selected.get('orderCount')}")

            # Агрегируем KPI
            snapshot = cls._build_snapshot(seller.id, period_start, period_end, products)

            # Сохраняем в БД
            db.session.add(snapshot)

            # Сохраняем детализацию по товарам
            cls._save_product_analytics(seller.id, period_start, period_end, products)

            db.session.commit()
            logger.info(f"Analytics snapshot saved: {snapshot}")

            return snapshot.to_dict()

        except WBAPIException as e:
            logger.error(f"WB API error fetching analytics: {e}")
            # Пытаемся вернуть последний доступный кэш
            fallback = AnalyticsSnapshot.query.filter(
                AnalyticsSnapshot.seller_id == seller.id,
                AnalyticsSnapshot.period_start == period_start,
                AnalyticsSnapshot.period_end == period_end,
            ).order_by(AnalyticsSnapshot.created_at.desc()).first()
            if fallback:
                return fallback.to_dict()
            return cls._empty_snapshot(period_start, period_end)

        except Exception as e:
            logger.exception(f"Unexpected error fetching analytics: {e}")
            db.session.rollback()
            return cls._empty_snapshot(period_start, period_end)

        finally:
            client.close()

    @classmethod
    def _build_snapshot(
        cls,
        seller_id: int,
        period_start: date,
        period_end: date,
        products: List[Dict],
    ) -> AnalyticsSnapshot:
        """Построить агрегированный снимок из данных продуктов воронки."""
        total_revenue = 0
        total_orders = 0
        total_buyouts = 0
        total_buyouts_sum = 0
        total_cancels = 0
        total_cancel_sum = 0
        total_open = 0
        total_cart = 0
        top_products = []

        # Прошлый период — для расчёта динамики из агрегатов
        past_revenue = 0
        past_orders = 0
        past_buyouts = 0

        for item in products:
            product_info = item.get('product', {})
            stats = item.get('statistic', {})
            selected = stats.get('selected', {})
            comparison = stats.get('comparison', {})

            order_sum = selected.get('orderSum', 0) or 0
            order_count = selected.get('orderCount', 0) or 0
            buyout_count = selected.get('buyoutCount', 0) or 0
            buyout_sum = selected.get('buyoutSum', 0) or 0
            cancel_count = selected.get('cancelCount', 0) or 0
            cancel_sum = selected.get('cancelSum', 0) or 0
            open_count = selected.get('openCount', 0) or 0
            cart_count = selected.get('cartCount', 0) or 0

            total_revenue += order_sum
            total_orders += order_count
            total_buyouts += buyout_count
            total_buyouts_sum += buyout_sum
            total_cancels += cancel_count
            total_cancel_sum += cancel_sum
            total_open += open_count
            total_cart += cart_count

            # Суммируем абсолютные значения прошлого периода из comparison
            past_revenue += (comparison.get('orderSum', 0) or 0)
            past_orders += (comparison.get('orderCount', 0) or 0)
            past_buyouts += (comparison.get('buyoutCount', 0) or 0)

            # Собираем топ товаров (первые 10)
            if len(top_products) < 10:
                top_products.append({
                    'nmId': product_info.get('nmId'),
                    'title': product_info.get('title', ''),
                    'vendorCode': product_info.get('vendorCode', ''),
                    'brandName': product_info.get('brandName', ''),
                    'orderSum': order_sum,
                    'orderCount': order_count,
                    'buyoutSum': buyout_sum,
                })

        # Динамика — считаем из агрегированных сумм текущего и прошлого периодов
        def _calc_dynamics_pct(current, past):
            if past == 0:
                return 100.0 if current > 0 else 0.0
            return round((current - past) / past * 100, 1)

        revenue_dynamics = _calc_dynamics_pct(total_revenue, past_revenue)
        orders_dynamics = _calc_dynamics_pct(total_orders, past_orders)
        buyouts_dynamics = _calc_dynamics_pct(total_buyouts, past_buyouts)

        logger.info(f"Dynamics computed from totals: revenue={total_revenue}/{past_revenue}={revenue_dynamics}%, "
                     f"orders={total_orders}/{past_orders}={orders_dynamics}%, "
                     f"buyouts={total_buyouts}/{past_buyouts}={buyouts_dynamics}%")

        # Конверсии (средние)
        avg_cart_pct = (total_cart / total_open * 100) if total_open else 0
        avg_order_pct = (total_orders / total_cart * 100) if total_cart else 0
        avg_buyout_pct = (total_buyouts / total_orders * 100) if total_orders else 0

        snapshot = AnalyticsSnapshot(
            seller_id=seller_id,
            period_start=period_start,
            period_end=period_end,
            revenue=total_revenue,
            orders_count=total_orders,
            buyouts_count=total_buyouts,
            buyouts_sum=total_buyouts_sum,
            cancel_count=total_cancels,
            cancel_sum=total_cancel_sum,
            open_card_count=total_open,
            add_to_cart_count=total_cart,
            avg_add_to_cart_percent=round(avg_cart_pct, 1),
            avg_cart_to_order_percent=round(avg_order_pct, 1),
            avg_buyout_percent=round(avg_buyout_pct, 1),
            revenue_dynamics=revenue_dynamics,
            orders_dynamics=orders_dynamics,
            buyouts_dynamics=buyouts_dynamics,
            daily_data=None,  # Заполняется отдельно через grouped history
            top_products=top_products,
        )
        return snapshot

    @classmethod
    def _save_product_analytics(
        cls,
        seller_id: int,
        period_start: date,
        period_end: date,
        products: List[Dict],
    ):
        """Сохранить детализацию по товарам."""
        # Удаляем старые записи за этот период
        ProductAnalytics.query.filter(
            ProductAnalytics.seller_id == seller_id,
            ProductAnalytics.period_start == period_start,
            ProductAnalytics.period_end == period_end,
        ).delete()

        for item in products:
            product_info = item.get('product', {})
            stats = item.get('statistic', {})
            selected = stats.get('selected', {})
            conversions = selected.get('conversions', {})
            stocks = product_info.get('stocks', {})

            pa = ProductAnalytics(
                seller_id=seller_id,
                nm_id=product_info.get('nmId', 0),
                period_start=period_start,
                period_end=period_end,
                title=product_info.get('title'),
                vendor_code=product_info.get('vendorCode'),
                brand_name=product_info.get('brandName'),
                subject_name=product_info.get('subjectName'),
                open_card_count=selected.get('openCount', 0),
                add_to_cart_count=selected.get('cartCount', 0),
                orders_count=selected.get('orderCount', 0),
                orders_sum=selected.get('orderSum', 0),
                buyouts_count=selected.get('buyoutCount', 0),
                buyouts_sum=selected.get('buyoutSum', 0),
                cancel_count=selected.get('cancelCount', 0),
                cancel_sum=selected.get('cancelSum', 0),
                add_to_cart_percent=conversions.get('addToCartPercent'),
                cart_to_order_percent=conversions.get('cartToOrderPercent'),
                buyout_percent=conversions.get('buyoutPercent'),
                stocks_wb=stocks.get('wb', 0),
                stocks_mp=stocks.get('mp', 0),
            )
            db.session.add(pa)

    # WB API: макс. 3 запроса/мин к grouped history. Берём 3 чанка ≈ 21 день.
    MAX_DAILY_CHUNKS = 3

    @classmethod
    def fetch_daily_data(
        cls,
        seller: Seller,
        period_start: date,
        period_end: date,
    ) -> List[Dict]:
        """
        Получить дневную историю через grouped history API.
        Макс. за неделю, поэтому для длинных периодов разбиваем на чанки.

        WB API допускает 3 запроса/мин — делаем до 3 чанков быстро (без sleep).
        Для периодов > 21 дня загружаем только последние 21 день.

        Returns:
            Список [{date, orderCount, orderSum, buyoutCount, buyoutSum, openCount, cartCount}]
        """
        client = cls._get_wb_client(seller)
        all_history = []

        # Ограничиваем период чтобы уложиться в 3 запроса (= лимит API/мин)
        max_days = cls.MAX_DAILY_CHUNKS * 7
        effective_start = period_start
        if (period_end - period_start).days > max_days:
            effective_start = period_end - timedelta(days=max_days)
            logger.info(f"Daily data: trimming period from {period_start} to {effective_start}..{period_end} "
                        f"(max {cls.MAX_DAILY_CHUNKS} chunks)")

        try:
            # API ограничен неделей, разбиваем на чанки.
            # До 3 чанков — без пауз (лимит 3 req/min).
            chunk_start = effective_start
            chunk_idx = 0
            while chunk_start < period_end and chunk_idx < cls.MAX_DAILY_CHUNKS:
                chunk_end = min(chunk_start + timedelta(days=6), period_end)

                logger.info(f"Daily data chunk {chunk_idx}: {chunk_start}..{chunk_end}")
                result = client.get_sales_funnel_grouped_history(
                    period_start=chunk_start.isoformat(),
                    period_end=chunk_end.isoformat(),
                    aggregation_level='day',
                    seller_id=seller.id,
                )

                data = result.get('data', [])
                for group in data:
                    history = group.get('history', [])
                    for day in history:
                        all_history.append({
                            'date': day.get('date'),
                            'orderCount': day.get('orderCount', 0),
                            'orderSum': day.get('orderSum', 0),
                            'buyoutCount': day.get('buyoutCount', 0),
                            'buyoutSum': day.get('buyoutSum', 0),
                            'openCount': day.get('openCount', 0),
                            'cartCount': day.get('cartCount', 0),
                        })

                chunk_start = chunk_end + timedelta(days=1)
                chunk_idx += 1

            # Агрегируем по дате если несколько групп
            daily_map = {}
            for entry in all_history:
                d = entry['date']
                if d not in daily_map:
                    daily_map[d] = {
                        'date': d,
                        'orderCount': 0, 'orderSum': 0,
                        'buyoutCount': 0, 'buyoutSum': 0,
                        'openCount': 0, 'cartCount': 0,
                    }
                for key in ['orderCount', 'orderSum', 'buyoutCount', 'buyoutSum', 'openCount', 'cartCount']:
                    daily_map[d][key] += entry.get(key, 0)

            result_list = sorted(daily_map.values(), key=lambda x: x['date'])
            logger.info(f"Fetched {len(result_list)} daily data points for seller={seller.id}")
            return result_list

        except WBAPIException as e:
            logger.error(f"WB API error fetching daily data: {e}")
            return []
        except Exception as e:
            logger.exception(f"Error fetching daily data: {e}")
            return []
        finally:
            client.close()

    @classmethod
    def update_snapshot_daily_data(cls, seller: Seller, period_code: str = '30d') -> Optional[List[Dict]]:
        """Обновить daily_data для последнего снимка."""
        period_start, period_end = cls._calc_period(period_code)
        snapshot = AnalyticsSnapshot.query.filter(
            AnalyticsSnapshot.seller_id == seller.id,
            AnalyticsSnapshot.period_start == period_start,
            AnalyticsSnapshot.period_end == period_end,
        ).order_by(AnalyticsSnapshot.created_at.desc()).first()

        if not snapshot:
            # Снимка ещё нет — сначала загружаем основные данные
            logger.info(f"No snapshot found for daily data, fetching summary first")
            cls.fetch_and_cache_snapshot(seller=seller, period_code=period_code)
            snapshot = AnalyticsSnapshot.query.filter(
                AnalyticsSnapshot.seller_id == seller.id,
                AnalyticsSnapshot.period_start == period_start,
                AnalyticsSnapshot.period_end == period_end,
            ).order_by(AnalyticsSnapshot.created_at.desc()).first()
            if not snapshot:
                return None

        daily = cls.fetch_daily_data(seller, period_start, period_end)
        if daily:
            snapshot.daily_data = daily
            db.session.commit()
        return daily

    @classmethod
    def get_product_analytics_list(
        cls,
        seller_id: int,
        period_code: str = '30d',
        sort_by: str = 'orders_sum',
        sort_dir: str = 'desc',
        search: str = '',
        page: int = 1,
        per_page: int = 20,
    ) -> Dict[str, Any]:
        """Получить список аналитики по товарам с пагинацией."""
        period_start, period_end = cls._calc_period(period_code)

        query = ProductAnalytics.query.filter(
            ProductAnalytics.seller_id == seller_id,
            ProductAnalytics.period_start == period_start,
            ProductAnalytics.period_end == period_end,
        )

        if search:
            search_filter = f'%{search}%'
            query = query.filter(
                db.or_(
                    ProductAnalytics.title.ilike(search_filter),
                    ProductAnalytics.vendor_code.ilike(search_filter),
                    ProductAnalytics.brand_name.ilike(search_filter),
                )
            )

        # Сортировка
        sort_column = getattr(ProductAnalytics, sort_by, ProductAnalytics.orders_sum)
        if sort_dir == 'asc':
            query = query.order_by(sort_column.asc())
        else:
            query = query.order_by(sort_column.desc())

        pagination = query.paginate(page=page, per_page=per_page, error_out=False)

        return {
            'items': [p.to_dict() for p in pagination.items],
            'total': pagination.total,
            'page': page,
            'pages': pagination.pages,
            'per_page': per_page,
        }

    @staticmethod
    def _empty_snapshot(period_start: date, period_end: date) -> Dict[str, Any]:
        """Пустой снимок когда данных нет."""
        return {
            'period_start': period_start.isoformat(),
            'period_end': period_end.isoformat(),
            'kpi': {
                'revenue': 0, 'orders': 0, 'avgCheck': 0,
                'buyouts': 0, 'cancels': 0,
                'openCardCount': 0, 'addToCartCount': 0,
            },
            'conversions': {
                'addToCartPercent': 0,
                'cartToOrderPercent': 0,
                'buyoutPercent': 0,
            },
            'dynamics': {
                'revenue': 0, 'orders': 0, 'buyouts': 0,
            },
            'dailyData': [],
            'topProducts': [],
            'created_at': None,
        }
