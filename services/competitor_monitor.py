"""
Сервис мониторинга конкурентов через публичные API Wildberries.

Использует рабочие публичные эндпоинты WB:
- basket-XX.wb.ru — карточки товаров, информация о продавцах
- search.wb.ru — поиск товаров с ценами и остатками
- feedbacks2.wb.ru — рейтинги и отзывы

Ключевые особенности:
- Отдельный rate limiter (не влияет на seller API)
- Комбинированный fetch (basket + search)
- Delta storage (снимки только при изменении)
- Непрерывный цикл мониторинга
- Кросс-селлер кэш (если несколько селлеров следят за одним nm_id)
"""

import time
import logging
import threading
import requests
from datetime import datetime, timedelta
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from services.wb_api_client import RateLimiter

logger = logging.getLogger(__name__)

# Кросс-селлер кэш: {nm_id: (data, timestamp)}
_product_cache = {}
_cache_lock = threading.Lock()
CACHE_TTL_SECONDS = 300  # 5 минут

# Активные мониторинг-треды: {seller_id: threading.Event}
_stop_events = {}
_monitor_threads = {}
_threads_lock = threading.Lock()

# Маппинг vol -> basket номер
_BASKET_RANGES = [
    (143, '01'), (287, '02'), (431, '03'), (719, '04'),
    (1007, '05'), (1061, '06'), (1115, '07'), (1169, '08'),
    (1313, '09'), (1601, '10'), (1655, '11'), (1919, '12'),
    (2045, '13'), (2189, '14'), (2405, '15'), (2621, '16'),
    (2837, '17'),
]


def _get_basket(nm_id):
    """Определить basket-номер для nm_id"""
    vol = nm_id // 100000
    for max_vol, basket in _BASKET_RANGES:
        if vol <= max_vol:
            return basket
    return '18'


def _get_basket_base_url(nm_id):
    """Получить базовый URL для basket-эндпоинта"""
    basket = _get_basket(nm_id)
    vol = nm_id // 100000
    part = nm_id // 1000
    return f"https://basket-{basket}.wb.ru/vol{vol}/part{part}/{nm_id}"


def _get_image_url(nm_id):
    """Получить URL фото товара"""
    basket = _get_basket(nm_id)
    vol = nm_id // 100000
    part = nm_id // 1000
    return f"https://basket-{basket}.wbbasket.ru/vol{vol}/part{part}/{nm_id}/images/big/1.webp"


class CompetitorMonitorService:
    """Сервис мониторинга конкурентов через публичные API WB"""

    # Рабочие эндпоинты (март 2026)
    SEARCH_URL = "https://search.wb.ru/exactmatch/ru/common/v18/search"
    FEEDBACKS_URL = "https://feedbacks2.wb.ru/feedbacks/v2"

    BATCH_SIZE = 100  # макс nm_id на один запрос

    # Дефолтные параметры запросов
    DEFAULT_PARAMS = {
        'appType': '1',
        'curr': 'rub',
        'dest': '-1257786',
        'lang': 'ru',
    }

    def __init__(self, requests_per_minute=60):
        self._rate_limiter = RateLimiter(
            max_requests=requests_per_minute,
            time_window=60
        )
        self._session = self._create_session()

    def _create_session(self):
        """Создать HTTP сессию с retry и пулом соединений"""
        session = requests.Session()
        retry = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
        )
        adapter = HTTPAdapter(
            max_retries=retry,
            pool_connections=5,
            pool_maxsize=10,
        )
        session.mount('https://', adapter)
        session.headers.update({
            'User-Agent': (
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                'AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/131.0.0.0 Safari/537.36'
            ),
            'Accept': 'application/json',
            'Accept-Language': 'ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7',
        })
        return session

    def fetch_products_batch(self, nm_ids):
        """
        Получить данные о товарах по списку nm_id.
        1. basket API для метаданных (без rate limit, по одному)
        2. search API по брендам для цен (группируем по бренду = меньше запросов)

        Args:
            nm_ids: список nm_id товаров

        Returns:
            dict: {nm_id: product_data} для найденных товаров
        """
        if not nm_ids:
            return {}

        # Проверяем кэш
        results = {}
        uncached_ids = []
        now = time.time()

        with _cache_lock:
            for nm_id in nm_ids:
                cached = _product_cache.get(nm_id)
                if cached and (now - cached[1]) < CACHE_TTL_SECONDS:
                    results[nm_id] = cached[0]
                else:
                    uncached_ids.append(nm_id)

        if not uncached_ids:
            return results

        # Шаг 1: загрузить метаданные из basket API (быстро, нет rate limit)
        basket_data = {}  # {nm_id: {title, brand, supplier_name, ...}}
        for nm_id in uncached_ids:
            meta = self._fetch_basket_metadata(nm_id)
            if meta:
                basket_data[nm_id] = meta

        # Шаг 2: сгруппировать по бренду и загрузить цены через search
        brands = {}  # {brand: [nm_id, ...]}
        for nm_id, meta in basket_data.items():
            brand = meta.get('brand', '')
            if brand:
                brands.setdefault(brand, []).append(nm_id)
            else:
                brands.setdefault('__no_brand__', []).append(nm_id)

        # Один search-запрос на бренд (вместо одного на товар)
        price_data = {}  # {nm_id: {price, sale_price, ...}}
        for brand, brand_nm_ids in brands.items():
            if brand == '__no_brand__':
                continue
            found = self._search_products_by_brand(brand, set(brand_nm_ids))
            price_data.update(found)

        # Шаг 3: собрать результаты
        for nm_id, meta in basket_data.items():
            prices = price_data.get(nm_id, {})
            product_data = {
                **meta,
                'price': prices.get('price'),
                'sale_price': prices.get('sale_price'),
                'rating': prices.get('rating'),
                'feedbacks_count': prices.get('feedbacks_count'),
                'total_stock': prices.get('total_stock'),
            }
            results[nm_id] = product_data
            with _cache_lock:
                _product_cache[nm_id] = (product_data, time.time())

        return results

    def _fetch_basket_metadata(self, nm_id):
        """Загрузить метаданные товара из basket API (name, brand, supplier)."""
        base_url = _get_basket_base_url(nm_id)

        # Карточка товара
        card_data = None
        try:
            response = self._session.get(f"{base_url}/info/ru/card.json", timeout=10)
            if response.status_code == 200:
                card_data = response.json()
            else:
                logger.warning(f"Товар {nm_id}: card.json -> {response.status_code}")
                return None
        except requests.exceptions.RequestException as e:
            logger.error(f"Товар {nm_id}: ошибка card.json: {e}")
            return None

        # Информация о продавце
        seller_data = None
        try:
            response = self._session.get(f"{base_url}/info/sellers.json", timeout=10)
            if response.status_code == 200:
                seller_data = response.json()
        except requests.exceptions.RequestException:
            pass

        selling = card_data.get('selling', {})
        return {
            'nm_id': nm_id,
            'title': card_data.get('imt_name', ''),
            'brand': selling.get('brand_name', ''),
            'supplier_name': (
                seller_data.get('supplierName', '') if seller_data
                else selling.get('brand_name', '')
            ),
            'wb_supplier_id': selling.get('supplier_id') or (
                seller_data.get('supplierId') if seller_data else None
            ),
            'image_url': _get_image_url(nm_id),
        }

    def _search_products_by_brand(self, brand, target_nm_ids):
        """
        Один search-запрос по бренду, возвращает цены для всех target_nm_ids.
        Пагинирует до 3 страниц, пока не найдёт все товары.

        Returns:
            dict: {nm_id: {price, sale_price, rating, feedbacks_count, total_stock}}
        """
        found = {}
        remaining = set(target_nm_ids)

        for page in range(1, 4):  # макс 3 страницы
            if not remaining:
                break

            self._rate_limiter.wait_if_needed()

            params = {
                **self.DEFAULT_PARAMS,
                'query': brand,
                'resultset': 'catalog',
                'sort': 'popular',
                'spp': '30',
                'page': str(page),
            }

            try:
                response = self._session.get(
                    self.SEARCH_URL, params=params, timeout=30
                )
                if response.status_code == 429:
                    logger.warning(f"Search API rate limited при поиске '{brand}'")
                    time.sleep(5)
                    break
                response.raise_for_status()
                data = response.json()

                products = data.get('products', [])
                if not products:
                    products = data.get('data', {}).get('products', [])
                if not products:
                    break

                for p in products:
                    pid = p.get('id')
                    if pid in remaining:
                        found[pid] = self._parse_search_product(p)
                        remaining.discard(pid)
                        logger.info(
                            f"Товар {pid}: цена найдена через search "
                            f"(brand=\"{brand}\", стр.{page})"
                        )

            except requests.exceptions.RequestException as e:
                logger.error(f"Ошибка search '{brand}': {e}")
                break

        if remaining:
            logger.info(
                f"Товары не найдены в search по бренду '{brand}': {remaining}"
            )

        return found

    def _parse_search_product(self, raw):
        """Распарсить данные товара из ответа search API (v18 формат)"""
        # Цены в новом формате: sizes[0].price.basic/product (в копейках)
        sizes = raw.get('sizes', [])
        price = None
        sale_price = None
        total_stock = raw.get('totalQuantity', 0)

        if sizes:
            price_obj = sizes[0].get('price', {})
            if price_obj.get('basic'):
                price = price_obj['basic'] // 100
            if price_obj.get('product'):
                sale_price = price_obj['product'] // 100

        return {
            'price': price,
            'sale_price': sale_price,
            'rating': raw.get('reviewRating', 0),
            'feedbacks_count': raw.get('feedbacks', 0),
            'total_stock': total_stock,
        }

    def search_products(self, query, limit=100):
        """
        Поиск товаров на WB по запросу.

        Args:
            query: поисковый запрос
            limit: максимум результатов

        Returns:
            list: список product_data
        """
        self._rate_limiter.wait_if_needed()

        params = {
            **self.DEFAULT_PARAMS,
            'query': query,
            'resultset': 'catalog',
            'sort': 'popular',
            'spp': '30',
        }

        try:
            response = self._session.get(
                self.SEARCH_URL,
                params=params,
                timeout=30
            )
            response.raise_for_status()
            data = response.json()

            products = data.get('products', [])
            if not products:
                products = data.get('data', {}).get('products', [])

            return [self._parse_full_search_product(p) for p in products[:limit]]

        except requests.exceptions.RequestException as e:
            logger.error(f"Ошибка поиска на WB: {e}")
            return []

    def _parse_full_search_product(self, raw):
        """Распарсить полные данные товара из search API"""
        nm_id = raw.get('id', 0)
        sizes = raw.get('sizes', [])
        price = None
        sale_price = None
        total_stock = raw.get('totalQuantity', 0)

        if sizes:
            price_obj = sizes[0].get('price', {})
            if price_obj.get('basic'):
                price = price_obj['basic'] // 100
            if price_obj.get('product'):
                sale_price = price_obj['product'] // 100

        return {
            'nm_id': nm_id,
            'title': raw.get('name', ''),
            'brand': raw.get('brand', ''),
            'supplier_name': raw.get('supplier', ''),
            'wb_supplier_id': raw.get('supplierId'),
            'image_url': _get_image_url(nm_id),
            'price': price,
            'sale_price': sale_price,
            'rating': raw.get('reviewRating', 0),
            'feedbacks_count': raw.get('feedbacks', 0),
            'total_stock': total_stock,
        }

    def fetch_seller_catalog(self, wb_supplier_id, limit=1000):
        """
        Получить каталог продавца по его supplier_id.

        Стратегия:
        1. catalog.wb.ru/sellers/catalog — основной endpoint
        2. Если не работает — search API по имени продавца с фильтрацией

        Args:
            wb_supplier_id: ID продавца на WB
            limit: максимум товаров

        Returns:
            list: список product_data
        """
        # Попытка 1: catalog.wb.ru (основной endpoint для каталога продавца)
        result = self._fetch_seller_via_catalog_api(wb_supplier_id, limit)
        if result:
            return result

        # Попытка 2: search API по имени продавца
        logger.info(
            f"Каталог продавца {wb_supplier_id}: catalog API не сработал, "
            f"пробуем search API"
        )
        return self._fetch_seller_via_search(wb_supplier_id, limit)

    def _fetch_seller_via_catalog_api(self, wb_supplier_id, limit):
        """Получить каталог через catalog.wb.ru/sellers/catalog"""
        CATALOG_URLS = [
            "https://catalog.wb.ru/sellers/catalog",
            "https://catalog.wb.ru/sellers/v2/catalog",
        ]

        all_products = []
        page = 1
        working_url = None

        while len(all_products) < limit:
            self._rate_limiter.wait_if_needed()

            params = {
                'appType': '1',
                'curr': 'rub',
                'dest': '-1257786',
                'supplier': str(wb_supplier_id),
                'sort': 'popular',
                'page': str(page),
                'limit': '100',
            }

            try:
                data = None
                urls = [working_url] if working_url else CATALOG_URLS

                for url in urls:
                    logger.info(
                        f"Каталог продавца {wb_supplier_id}, стр. {page}: {url}"
                    )
                    response = self._session.get(url, params=params, timeout=30)

                    if response.status_code in (403, 404):
                        logger.warning(f"URL {url} вернул {response.status_code}")
                        continue
                    if response.status_code == 429:
                        logger.warning(f"URL {url} rate limited (429)")
                        time.sleep(3)
                        return None  # Попробуем fallback

                    response.raise_for_status()
                    data = response.json()
                    working_url = url
                    break

                if data is None:
                    return None

                products = data.get('data', {}).get('products', [])
                if not products:
                    if page == 1:
                        logger.info(
                            f"Каталог продавца {wb_supplier_id}: 0 товаров. "
                            f"Ответ: {str(data)[:300]}"
                        )
                        return None
                    break

                all_products.extend(
                    self._parse_full_search_product(p) for p in products
                )
                logger.info(
                    f"Каталог продавца {wb_supplier_id}: стр. {page}, "
                    f"{len(products)} товаров (всего {len(all_products)})"
                )
                page += 1

            except requests.exceptions.RequestException as e:
                logger.error(f"Ошибка catalog API для продавца {wb_supplier_id}: {e}")
                return None

        return all_products[:limit] if all_products else None

    def _fetch_seller_via_search(self, wb_supplier_id, limit):
        """
        Fallback: найти товары продавца через search API.
        Ищем по имени продавца и фильтруем по supplierId.
        """
        # Сначала узнаём имя продавца через любой его товар (если есть в базе)
        seller_name = self._get_seller_name(wb_supplier_id)
        if not seller_name:
            logger.warning(
                f"Продавец {wb_supplier_id}: не удалось определить имя для поиска"
            )
            return []

        all_products = []
        page = 1

        while len(all_products) < limit and page <= 5:
            self._rate_limiter.wait_if_needed()

            params = {
                **self.DEFAULT_PARAMS,
                'query': seller_name,
                'resultset': 'catalog',
                'sort': 'popular',
                'spp': '30',
                'page': str(page),
            }

            try:
                response = self._session.get(
                    self.SEARCH_URL,
                    params=params,
                    timeout=30
                )
                if response.status_code == 429:
                    logger.warning("Search API rate limited")
                    time.sleep(5)
                    continue

                response.raise_for_status()
                data = response.json()

                products = data.get('products', [])
                if not products:
                    products = data.get('data', {}).get('products', [])
                if not products:
                    break

                # Фильтруем по supplierId
                seller_products = [
                    p for p in products
                    if p.get('supplierId') == wb_supplier_id
                ]
                parsed = [
                    self._parse_full_search_product(p) for p in seller_products
                ]
                all_products.extend(parsed)

                logger.info(
                    f"Каталог продавца {wb_supplier_id} (search): стр. {page}, "
                    f"найдено {len(seller_products)} из {len(products)} "
                    f"(всего {len(all_products)})"
                )

                # Если на странице нет товаров этого продавца, дальше не ищем
                if not seller_products:
                    break

                page += 1

            except requests.exceptions.RequestException as e:
                logger.error(f"Ошибка search для продавца {wb_supplier_id}: {e}")
                break

        return all_products[:limit]

    def _get_seller_name(self, wb_supplier_id):
        """Получить имя продавца. Ищем в базе или через API."""
        # Пробуем из базы
        try:
            from models import CompetitorProduct
            product = CompetitorProduct.query.filter_by(
                wb_supplier_id=wb_supplier_id
            ).first()
            if product and product.supplier_name:
                return product.supplier_name
        except Exception:
            pass

        # Если нет — ничего не можем сделать без nm_id
        return None

    @classmethod
    def sync_seller_competitors(cls, seller_id, flask_app):
        """
        Полный цикл синхронизации конкурентов для одного селлера.
        Обновляет текущие значения, создаёт delta-снимки, генерирует алерты.

        Args:
            seller_id: ID селлера
            flask_app: Flask app для контекста
        """
        from models import (
            db, CompetitorMonitorSettings, CompetitorProduct,
            CompetitorPriceSnapshot, CompetitorAlert
        )

        with flask_app.app_context():
            settings = CompetitorMonitorSettings.query.filter_by(seller_id=seller_id).first()
            if not settings or not settings.is_enabled:
                return

            # Создаём сервис с настройками rate limit
            service = cls(requests_per_minute=settings.requests_per_minute)

            # Получаем все активные товары, отсортированные по приоритету
            products = CompetitorProduct.query.filter_by(
                seller_id=seller_id,
                is_active=True
            ).order_by(
                CompetitorProduct.priority.asc(),
                CompetitorProduct.last_fetched_at.asc().nullsfirst()
            ).limit(settings.max_products).all()

            if not products:
                settings.last_sync_at = datetime.utcnow()
                settings.last_sync_status = 'idle'
                settings.total_products_monitored = 0
                db.session.commit()
                return 'no_products'

            logger.info(f"[Seller {seller_id}] Начинаем синк {len(products)} конкурентов")

            total_updated = 0
            total_alerts = 0
            errors = 0

            # Batch-загрузка всех товаров сразу
            all_nm_ids = [p.nm_id for p in products]
            fetched_data = service.fetch_products_batch(all_nm_ids)

            for product in products:
                try:
                    data = fetched_data.get(product.nm_id)

                    if not data:
                        product.fetch_error_count += 1
                        logger.warning(
                            f"[Seller {seller_id}] Товар {product.nm_id} не найден "
                            f"(ошибка #{product.fetch_error_count})"
                        )
                        if product.fetch_error_count >= 20:
                            product.is_active = False
                            logger.warning(
                                f"Деактивирован товар {product.nm_id} после 20 ошибок"
                            )
                        db.session.commit()
                        continue

                    product.fetch_error_count = 0

                    # Обновляем метаданные
                    if data.get('title'):
                        product.title = data['title']
                    if data.get('brand'):
                        product.brand = data['brand']
                    if data.get('supplier_name'):
                        product.supplier_name = data['supplier_name']
                    if data.get('wb_supplier_id'):
                        product.wb_supplier_id = data['wb_supplier_id']
                    if data.get('image_url'):
                        product.image_url = data['image_url']

                    # Delta storage: создаём снимок только если что-то изменилось
                    price_changed = (
                        product.current_price != data.get('price') or
                        product.current_sale_price != data.get('sale_price')
                    )
                    stock_changed = product.current_total_stock != data.get('total_stock')
                    rating_changed = product.current_rating != data.get('rating')

                    if price_changed or stock_changed or rating_changed or product.current_price is None:
                        price_change_pct = None
                        if product.current_sale_price and data.get('sale_price'):
                            old_p = product.current_sale_price
                            new_p = data['sale_price']
                            if old_p > 0:
                                price_change_pct = round((new_p - old_p) / old_p * 100, 2)

                        snapshot = CompetitorPriceSnapshot(
                            product_id=product.id,
                            seller_id=seller_id,
                            price=data.get('price'),
                            sale_price=data.get('sale_price'),
                            rating=data.get('rating'),
                            feedbacks_count=data.get('feedbacks_count'),
                            total_stock=data.get('total_stock'),
                            price_change_percent=price_change_pct,
                        )
                        db.session.add(snapshot)

                        # Генерируем алерты
                        alerts = cls._generate_alerts(
                            seller_id, product, data,
                            settings.price_change_alert_percent
                        )
                        for alert in alerts:
                            db.session.add(alert)
                            total_alerts += 1

                    # Обновляем текущие значения
                    product.current_price = data.get('price')
                    product.current_sale_price = data.get('sale_price')
                    product.current_rating = data.get('rating')
                    product.current_feedbacks_count = data.get('feedbacks_count')
                    product.current_total_stock = data.get('total_stock')
                    product.last_fetched_at = datetime.utcnow()

                    total_updated += 1
                    db.session.commit()

                except Exception as e:
                    errors += 1
                    logger.error(
                        f"Ошибка синка товара {product.nm_id} "
                        f"(seller={seller_id}): {e}"
                    )
                    db.session.rollback()

            # Обновляем статистику
            settings.last_sync_at = datetime.utcnow()
            settings.last_sync_status = 'success' if errors == 0 else 'partial'
            settings.total_products_monitored = total_updated

            if errors > 0:
                settings.last_sync_error = f"{errors} product(s) failed"
            else:
                settings.last_sync_error = None

            db.session.commit()

            logger.info(
                f"[Seller {seller_id}] Синк завершён: "
                f"{total_updated} обновлено, {total_alerts} алертов, {errors} ошибок"
            )

    @classmethod
    def _generate_alerts(cls, seller_id, product, new_data, alert_threshold):
        """Генерация алертов при значительных изменениях."""
        from models import CompetitorAlert

        alerts = []

        # Алерт на изменение цены
        if product.current_sale_price and new_data.get('sale_price'):
            old_p = product.current_sale_price
            new_p = new_data['sale_price']
            if old_p > 0:
                change_pct = round((new_p - old_p) / old_p * 100, 2)
                if abs(change_pct) >= alert_threshold:
                    alert_type = 'price_drop' if change_pct < 0 else 'price_increase'
                    severity = 'critical' if abs(change_pct) >= alert_threshold * 2 else 'warning'
                    alerts.append(CompetitorAlert(
                        seller_id=seller_id,
                        product_id=product.id,
                        group_id=product.group_id,
                        alert_type=alert_type,
                        severity=severity,
                        old_value=old_p,
                        new_value=new_p,
                        change_percent=change_pct,
                        message=(
                            f"{product.title or product.nm_id}: "
                            f"цена {'снизилась' if change_pct < 0 else 'выросла'} "
                            f"на {abs(change_pct):.1f}% ({old_p} -> {new_p})"
                        ),
                    ))

        # Алерт на изменение скидки (>= 5 п.п.)
        old_discount = None
        new_discount = None
        if product.current_price and product.current_sale_price and product.current_price > 0:
            old_discount = round((1 - product.current_sale_price / product.current_price) * 100, 1)
        if new_data.get('price') and new_data.get('sale_price') and new_data['price'] > 0:
            new_discount = round((1 - new_data['sale_price'] / new_data['price']) * 100, 1)

        if old_discount is not None and new_discount is not None:
            discount_change = round(new_discount - old_discount, 1)
            if abs(discount_change) >= 5:
                alert_type = 'discount_increase' if discount_change > 0 else 'discount_decrease'
                severity = 'warning' if abs(discount_change) >= 10 else 'info'
                alerts.append(CompetitorAlert(
                    seller_id=seller_id,
                    product_id=product.id,
                    group_id=product.group_id,
                    alert_type=alert_type,
                    severity=severity,
                    old_value=old_discount,
                    new_value=new_discount,
                    change_percent=discount_change,
                    message=(
                        f"{product.title or product.nm_id}: "
                        f"скидка {'увеличилась' if discount_change > 0 else 'уменьшилась'} "
                        f"на {abs(discount_change):.0f} п.п. ({old_discount:.0f}% → {new_discount:.0f}%)"
                    ),
                ))

        # Алерт: товар закончился
        if product.current_total_stock and product.current_total_stock > 0:
            if new_data.get('total_stock', 0) == 0:
                alerts.append(CompetitorAlert(
                    seller_id=seller_id,
                    product_id=product.id,
                    group_id=product.group_id,
                    alert_type='out_of_stock',
                    severity='info',
                    old_value=product.current_total_stock,
                    new_value=0,
                    message=(
                        f"{product.title or product.nm_id}: "
                        f"товар закончился (было {product.current_total_stock} шт.)"
                    ),
                ))

        # Алерт: товар снова в наличии
        if (product.current_total_stock is not None and product.current_total_stock == 0
                and new_data.get('total_stock', 0) > 0):
            alerts.append(CompetitorAlert(
                seller_id=seller_id,
                product_id=product.id,
                group_id=product.group_id,
                alert_type='back_in_stock',
                severity='info',
                old_value=0,
                new_value=new_data['total_stock'],
                message=(
                    f"{product.title or product.nm_id}: "
                    f"товар снова в наличии ({new_data['total_stock']} шт.)"
                ),
            ))

        return alerts

    @classmethod
    def compact_old_snapshots(cls, flask_app, days_hourly=7, days_daily=90):
        """
        Компакция старых снимков:
        - Старше days_hourly дней: оставляем 1 снимок в час
        - Старше days_daily дней: оставляем 1 снимок в день
        """
        from models import db, CompetitorPriceSnapshot

        with flask_app.app_context():
            now = datetime.utcnow()

            hourly_cutoff = now - timedelta(days=days_hourly)
            daily_cutoff = now - timedelta(days=days_daily)

            deleted_hourly = db.session.execute(db.text("""
                DELETE FROM competitor_price_snapshots
                WHERE id NOT IN (
                    SELECT MAX(id) FROM competitor_price_snapshots
                    WHERE created_at < :hourly_cutoff AND created_at >= :daily_cutoff
                    GROUP BY product_id, strftime('%Y-%m-%d %H', created_at)
                )
                AND created_at < :hourly_cutoff AND created_at >= :daily_cutoff
            """), {'hourly_cutoff': hourly_cutoff, 'daily_cutoff': daily_cutoff})

            deleted_daily = db.session.execute(db.text("""
                DELETE FROM competitor_price_snapshots
                WHERE id NOT IN (
                    SELECT MAX(id) FROM competitor_price_snapshots
                    WHERE created_at < :daily_cutoff
                    GROUP BY product_id, strftime('%Y-%m-%d', created_at)
                )
                AND created_at < :daily_cutoff
            """), {'daily_cutoff': daily_cutoff})

            db.session.commit()
            logger.info(
                f"Компакция снимков: удалено {deleted_hourly.rowcount} hourly, "
                f"{deleted_daily.rowcount} daily"
            )

    @classmethod
    def get_price_history(cls, product_id, period_days=30, flask_app=None):
        """Получить историю цен для графика"""
        from models import CompetitorPriceSnapshot

        cutoff = datetime.utcnow() - timedelta(days=period_days)
        snapshots = CompetitorPriceSnapshot.query.filter(
            CompetitorPriceSnapshot.product_id == product_id,
            CompetitorPriceSnapshot.created_at >= cutoff
        ).order_by(CompetitorPriceSnapshot.created_at.asc()).all()

        return [s.to_dict() for s in snapshots]

    @classmethod
    def get_group_comparison(cls, group_id):
        """Получить сравнение товаров в группе"""
        from models import CompetitorProduct

        products = CompetitorProduct.query.filter_by(
            group_id=group_id,
            is_active=True
        ).all()

        return [p.to_dict() for p in products]


def start_competitor_monitor_loop(seller_id, flask_app):
    """
    Запустить непрерывный цикл мониторинга конкурентов для селлера.
    Каждый селлер — отдельный daemon-тред.
    """
    from models import db, CompetitorMonitorSettings

    with _threads_lock:
        if seller_id in _monitor_threads and _monitor_threads[seller_id].is_alive():
            logger.info(f"[Seller {seller_id}] Мониторинг-тред уже работает")
            return

        stop_event = threading.Event()
        _stop_events[seller_id] = stop_event

        def _loop():
            logger.info(f"[Seller {seller_id}] Запущен непрерывный цикл мониторинга конкурентов")
            cycle_count = 0

            while not stop_event.is_set():
                try:
                    with flask_app.app_context():
                        settings = CompetitorMonitorSettings.query.filter_by(
                            seller_id=seller_id
                        ).first()

                        if not settings or not settings.is_enabled:
                            settings.is_running = False
                            db.session.commit()
                            logger.info(f"[Seller {seller_id}] Мониторинг выключен, выходим")
                            break

                        settings.is_running = True
                        settings.last_sync_status = 'running'
                        db.session.commit()

                    start_time = time.time()
                    result = CompetitorMonitorService.sync_seller_competitors(
                        seller_id, flask_app
                    )
                    duration = time.time() - start_time

                    if result == 'no_products':
                        logger.info(
                            f"[Seller {seller_id}] Нет товаров для мониторинга, пауза 5 мин"
                        )
                        stop_event.wait(timeout=300)
                        continue

                    cycle_count += 1

                    with flask_app.app_context():
                        settings = CompetitorMonitorSettings.query.filter_by(
                            seller_id=seller_id
                        ).first()
                        if settings:
                            settings.last_full_cycle_duration = round(duration, 2)
                            settings.total_cycles_completed = cycle_count
                            db.session.commit()

                    logger.info(
                        f"[Seller {seller_id}] Цикл #{cycle_count} завершён за {duration:.1f}с"
                    )

                    with flask_app.app_context():
                        settings = CompetitorMonitorSettings.query.filter_by(
                            seller_id=seller_id
                        ).first()
                        pause = settings.pause_between_cycles_seconds if settings else 60

                    if pause > 0:
                        stop_event.wait(timeout=pause)

                except Exception as e:
                    logger.error(f"[Seller {seller_id}] Ошибка в цикле мониторинга: {e}")
                    stop_event.wait(timeout=30)

            with flask_app.app_context():
                settings = CompetitorMonitorSettings.query.filter_by(
                    seller_id=seller_id
                ).first()
                if settings:
                    settings.is_running = False
                    db.session.commit()

            logger.info(f"[Seller {seller_id}] Мониторинг-тред завершён")

        thread = threading.Thread(
            target=_loop, daemon=True, name=f"competitor-monitor-{seller_id}"
        )
        _monitor_threads[seller_id] = thread
        thread.start()


def stop_competitor_monitor_loop(seller_id):
    """Остановить мониторинг для селлера"""
    with _threads_lock:
        stop_event = _stop_events.get(seller_id)
        if stop_event:
            stop_event.set()
            logger.info(f"[Seller {seller_id}] Отправлен сигнал остановки мониторинга")


def check_and_restart_monitor_loops(flask_app):
    """
    Проверка и перезапуск упавших тредов.
    Вызывается из scheduler каждые 5 минут.
    """
    from models import CompetitorMonitorSettings

    with flask_app.app_context():
        enabled_settings = CompetitorMonitorSettings.query.filter_by(is_enabled=True).all()

        for settings in enabled_settings:
            with _threads_lock:
                thread = _monitor_threads.get(settings.seller_id)
                if thread is None or not thread.is_alive():
                    logger.info(
                        f"[Seller {settings.seller_id}] Перезапуск мониторинг-треда"
                    )
                    start_competitor_monitor_loop(settings.seller_id, flask_app)


def stop_all_monitor_loops():
    """Graceful shutdown всех тредов"""
    with _threads_lock:
        for seller_id, stop_event in _stop_events.items():
            stop_event.set()
        logger.info(
            f"Отправлен сигнал остановки всем мониторинг-тредам ({len(_stop_events)})"
        )


def clear_product_cache():
    """Очистка кэша продуктов"""
    with _cache_lock:
        _product_cache.clear()
