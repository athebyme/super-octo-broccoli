"""
Сервис мониторинга конкурентов через публичные API Wildberries.

Использует бесплатные публичные эндпоинты WB (card.wb.ru, search.wb.ru)
для отслеживания цен, скидок, рейтингов и остатков конкурентов.

Ключевые особенности:
- Отдельный rate limiter (не влияет на seller API)
- Batch-запросы (до 100 nm_id за раз)
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


class CompetitorMonitorService:
    """Сервис мониторинга конкурентов через публичные API WB"""

    DETAIL_URL = "https://card.wb.ru/cards/detail"
    DETAIL_URL_V2 = "https://card.wb.ru/cards/v2/detail"
    SEARCH_URL = "https://search.wb.ru/exactmatch/ru/common/v7/search"
    SELLER_CATALOG_URL = "https://catalog.wb.ru/sellers/catalog"
    SELLER_CATALOG_URLS = [
        "https://catalog.wb.ru/sellers/catalog",
        "https://catalog.wb.ru/sellers/v2/catalog",
    ]
    BATCH_SIZE = 100  # макс nm_id на один запрос

    # Дефолтные параметры запросов
    DEFAULT_PARAMS = {
        'appType': '1',
        'curr': 'rub',
        'dest': '-1257786',
        'spp': '30',
    }

    def __init__(self, requests_per_minute=60):
        """
        Args:
            requests_per_minute: лимит запросов к публичному API
        """
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
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'application/json',
            'Accept-Language': 'ru-RU,ru;q=0.9',
        })
        return session

    def fetch_products_batch(self, nm_ids):
        """
        Получить данные о товарах по списку nm_id (до 100 штук).

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

        # Запрос к API — пробуем v1, затем v2
        nm_str = ';'.join(str(x) for x in uncached_ids)

        detail_urls = [self.DETAIL_URL, self.DETAIL_URL_V2]

        for url in detail_urls:
            self._rate_limiter.wait_if_needed()
            params = {**self.DEFAULT_PARAMS, 'nm': nm_str}

            try:
                logger.info(f"Запрос деталей товаров: {url}, nm_ids={uncached_ids[:5]}...")
                response = self._session.get(url, params=params, timeout=30)

                if response.status_code == 404:
                    logger.warning(f"URL {url} вернул 404, пробуем следующий")
                    continue

                response.raise_for_status()
                data = response.json()

                products = data.get('data', {}).get('products', [])

                if not products:
                    logger.info(
                        f"URL {url}: пустой список products. "
                        f"Ответ: {str(data)[:300]}"
                    )
                    continue

                with _cache_lock:
                    for product in products:
                        nm_id = product.get('id')
                        if nm_id:
                            parsed = self._parse_product_data(product)
                            results[nm_id] = parsed
                            _product_cache[nm_id] = (parsed, time.time())

                # Нашли товары — выходим из цикла
                break

            except requests.exceptions.RequestException as e:
                logger.error(f"Ошибка при запросе {url}: {e}")
                continue
            except (ValueError, KeyError) as e:
                logger.error(f"Ошибка парсинга ответа WB ({url}): {e}")
                continue

        # Логируем не найденные товары
        not_found = [nm_id for nm_id in uncached_ids if nm_id not in results]
        if not_found:
            logger.warning(
                f"Товары не найдены ни через v1, ни через v2 API: {not_found}"
            )

        return results

    def _parse_product_data(self, raw):
        """Распарсить данные одного товара из ответа API"""
        # Цены в API приходят в копейках
        price_info = raw.get('sizes', [{}])
        price = raw.get('priceU', 0) // 100 if raw.get('priceU') else None
        sale_price = raw.get('salePriceU', 0) // 100 if raw.get('salePriceU') else None

        # Общий остаток по всем размерам/складам
        total_stock = 0
        for size in raw.get('sizes', []):
            for stock in size.get('stocks', []):
                total_stock += stock.get('qty', 0)

        # URL фото
        nm_id = raw.get('id', 0)
        vol = nm_id // 100000
        part = nm_id // 1000
        # Определяем basket
        if vol <= 143:
            basket = '01'
        elif vol <= 287:
            basket = '02'
        elif vol <= 431:
            basket = '03'
        elif vol <= 719:
            basket = '04'
        elif vol <= 1007:
            basket = '05'
        elif vol <= 1061:
            basket = '06'
        elif vol <= 1115:
            basket = '07'
        elif vol <= 1169:
            basket = '08'
        elif vol <= 1313:
            basket = '09'
        elif vol <= 1601:
            basket = '10'
        elif vol <= 1655:
            basket = '11'
        elif vol <= 1919:
            basket = '12'
        elif vol <= 2045:
            basket = '13'
        elif vol <= 2189:
            basket = '14'
        elif vol <= 2405:
            basket = '15'
        elif vol <= 2621:
            basket = '16'
        elif vol <= 2837:
            basket = '17'
        else:
            basket = '18'
        image_url = f"https://basket-{basket}.wbbasket.ru/vol{vol}/part{part}/{nm_id}/images/big/1.webp"

        return {
            'nm_id': nm_id,
            'title': raw.get('name', ''),
            'brand': raw.get('brand', ''),
            'supplier_name': raw.get('supplier', ''),
            'wb_supplier_id': raw.get('supplierId'),
            'image_url': image_url,
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
            'limit': min(limit, 300),
            'sort': 'popular',
        }

        try:
            response = self._session.get(
                self.SEARCH_URL,
                params=params,
                timeout=30
            )
            response.raise_for_status()
            data = response.json()

            products = data.get('data', {}).get('products', [])
            return [self._parse_product_data(p) for p in products[:limit]]

        except requests.exceptions.RequestException as e:
            logger.error(f"Ошибка поиска на WB: {e}")
            return []

    def fetch_seller_catalog(self, wb_supplier_id, limit=1000):
        """
        Получить каталог продавца по его supplier_id на WB.

        Args:
            wb_supplier_id: ID продавца на WB
            limit: максимум товаров

        Returns:
            list: список product_data
        """
        all_products = []
        page = 1
        working_url = None

        while len(all_products) < limit:
            self._rate_limiter.wait_if_needed()

            params = {
                **self.DEFAULT_PARAMS,
                'supplier': str(wb_supplier_id),
                'sort': 'popular',
                'page': str(page),
                'limit': '100',
            }

            try:
                data = None

                if working_url:
                    # Используем уже найденный рабочий URL
                    urls_to_try = [working_url]
                else:
                    urls_to_try = self.SELLER_CATALOG_URLS

                for url in urls_to_try:
                    logger.info(
                        f"Запрос каталога продавца {wb_supplier_id}, стр. {page}: "
                        f"{url}?supplier={wb_supplier_id}"
                    )
                    response = self._session.get(
                        url,
                        params=params,
                        timeout=30
                    )
                    if response.status_code == 404:
                        logger.warning(
                            f"URL {url} вернул 404, пробуем следующий"
                        )
                        continue
                    response.raise_for_status()
                    data = response.json()
                    working_url = url
                    break

                if data is None:
                    logger.error(
                        f"Все URL каталога продавца вернули ошибку для {wb_supplier_id}"
                    )
                    break

                products = data.get('data', {}).get('products', [])
                if not products:
                    logger.info(
                        f"Каталог продавца {wb_supplier_id}: получено 0 товаров на стр. {page}. "
                        f"Ответ: {str(data)[:500]}"
                    )
                    break

                all_products.extend(self._parse_product_data(p) for p in products)
                logger.info(
                    f"Каталог продавца {wb_supplier_id}: стр. {page}, "
                    f"получено {len(products)} товаров (всего {len(all_products)})"
                )
                page += 1

            except requests.exceptions.RequestException as e:
                logger.error(f"Ошибка получения каталога продавца {wb_supplier_id}: {e}")
                break

        return all_products[:limit]

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

            # Разбиваем на batch по 100
            total_updated = 0
            total_alerts = 0
            errors = 0

            for i in range(0, len(products), cls.BATCH_SIZE):
                batch = products[i:i + cls.BATCH_SIZE]
                nm_ids = [p.nm_id for p in batch]

                try:
                    fetched_data = service.fetch_products_batch(nm_ids)

                    for product in batch:
                        data = fetched_data.get(product.nm_id)
                        if not data:
                            product.fetch_error_count += 1
                            logger.warning(
                                f"[Seller {seller_id}] Товар {product.nm_id} не найден в API "
                                f"(ошибка #{product.fetch_error_count})"
                            )
                            # Деактивируем после 20 ошибок подряд
                            if product.fetch_error_count >= 20:
                                product.is_active = False
                                logger.warning(
                                    f"Деактивирован товар {product.nm_id} после 20 ошибок подряд"
                                )
                            continue

                        product.fetch_error_count = 0

                        # Обновляем метаданные (всегда, чтобы заполнить заглушки)
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
                            # Вычисляем процент изменения цены
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
                    logger.error(f"Ошибка batch синка (seller={seller_id}, batch {i//cls.BATCH_SIZE}): {e}")
                    db.session.rollback()

            # Обновляем статистику
            settings.last_sync_at = datetime.utcnow()
            settings.last_sync_status = 'success' if errors == 0 else 'partial'
            settings.total_products_monitored = total_updated

            if errors > 0:
                settings.last_sync_error = f"{errors} batch(es) failed"
            else:
                settings.last_sync_error = None

            db.session.commit()

            logger.info(
                f"[Seller {seller_id}] Синк завершён: "
                f"{total_updated} обновлено, {total_alerts} алертов, {errors} ошибок"
            )

    @classmethod
    def _generate_alerts(cls, seller_id, product, new_data, alert_threshold):
        """
        Генерация алертов при значительных изменениях.

        Returns:
            list[CompetitorAlert]
        """
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
                        message=f"{product.title or product.nm_id}: цена {'снизилась' if change_pct < 0 else 'выросла'} на {abs(change_pct):.1f}% ({old_p} -> {new_p})",
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
                    message=f"{product.title or product.nm_id}: товар закончился (было {product.current_total_stock} шт.)",
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
                message=f"{product.title or product.nm_id}: товар снова в наличии ({new_data['total_stock']} шт.)",
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

            # Удаляем дубли старше days_hourly (оставляем 1 в час)
            hourly_cutoff = now - timedelta(days=days_hourly)
            daily_cutoff = now - timedelta(days=days_daily)

            # Удаляем лишние снимки старше days_hourly но моложе days_daily
            # Оставляем последний снимок в каждом часе
            deleted_hourly = db.session.execute(db.text("""
                DELETE FROM competitor_price_snapshots
                WHERE id NOT IN (
                    SELECT MAX(id) FROM competitor_price_snapshots
                    WHERE created_at < :hourly_cutoff AND created_at >= :daily_cutoff
                    GROUP BY product_id, strftime('%Y-%m-%d %H', created_at)
                )
                AND created_at < :hourly_cutoff AND created_at >= :daily_cutoff
            """), {'hourly_cutoff': hourly_cutoff, 'daily_cutoff': daily_cutoff})

            # Удаляем лишние снимки старше days_daily
            # Оставляем последний снимок в каждом дне
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
        # Если уже работает — не запускаем дубль
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
                            logger.info(f"[Seller {seller_id}] Мониторинг выключен, выходим из цикла")
                            break

                        settings.is_running = True
                        settings.last_sync_status = 'running'
                        db.session.commit()

                    # Полный цикл синка
                    start_time = time.time()
                    result = CompetitorMonitorService.sync_seller_competitors(seller_id, flask_app)
                    duration = time.time() - start_time

                    # Если нет товаров — долгая пауза, не считаем цикл
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

                    # Пауза между циклами (минимум 60 секунд)
                    with flask_app.app_context():
                        settings = CompetitorMonitorSettings.query.filter_by(
                            seller_id=seller_id
                        ).first()
                        pause = settings.pause_between_cycles_seconds if settings else 60

                    pause = max(pause, 60)  # минимум 60 секунд между циклами
                    stop_event.wait(timeout=pause)

                except Exception as e:
                    logger.error(f"[Seller {seller_id}] Ошибка в цикле мониторинга: {e}")
                    # Ждём 30 секунд перед повтором после ошибки
                    stop_event.wait(timeout=30)

            # Финализация
            with flask_app.app_context():
                settings = CompetitorMonitorSettings.query.filter_by(
                    seller_id=seller_id
                ).first()
                if settings:
                    settings.is_running = False
                    db.session.commit()

            logger.info(f"[Seller {seller_id}] Мониторинг-тред завершён")

        thread = threading.Thread(target=_loop, daemon=True, name=f"competitor-monitor-{seller_id}")
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
        logger.info(f"Отправлен сигнал остановки всем мониторинг-тредам ({len(_stop_events)})")


def clear_product_cache():
    """Очистка кэша продуктов"""
    with _cache_lock:
        _product_cache.clear()
