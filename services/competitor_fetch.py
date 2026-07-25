# -*- coding: utf-8 -*-
"""
ORM-free fetch-слой мониторинга конкурентов (публичные API WB).

Источники:
- basket CDN (wbbasket.ru card.json/sellers.json) — метаданные; без жёсткого
  IP-лимита, но всё равно проходит общий rate limiter;
- card.wb.ru/cards/v4/detail — exact-nmID витринные цены наших карточек;
- catalog.wb.ru/sellers/v4/catalog — цены каталога продавца (основной);
- search.wb.ru exactmatch v18 — цены через поиск по бренду (fallback) и
  интерактивный поиск.

Правила устойчивости:
- ОДИН глобальный process-wide rate limiter на все публичные вызовы всех
  продавцов (бюджет WB per-IP): env COMPETITOR_PUBLIC_RPM, default 20;
- circuit breaker на источник: 3 подряд 429/5xx -> cooldown 10 минут;
- 429 никогда не ждётся sleep-ом: источник немедленно завершает работу
  в текущем проходе (WBRateLimitedError для интерактивных вызовов);
- кросс-селлер кэш наблюдений: TTL 300с, максимум 10 000 записей.

Цены наружу — В РУБЛЯХ (integer): WB отдаёт копейки, здесь делим на 100.
"""
import logging
import os
import threading
import time

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from services.wb_api_client import RateLimiter

logger = logging.getLogger(__name__)

SOURCE_BASKET = 'basket'
SOURCE_CARD = 'card'
SOURCE_CATALOG = 'catalog'
SOURCE_SEARCH = 'search'

CARD_URL = 'https://card.wb.ru/cards/v4/detail'
SEARCH_URL = 'https://search.wb.ru/exactmatch/ru/common/v18/search'
CATALOG_URL = 'https://catalog.wb.ru/sellers/v4/catalog'
DEFAULT_PARAMS = {'appType': '1', 'curr': 'rub', 'dest': '-1257786', 'lang': 'ru'}
CARD_PARAMS = {
    **DEFAULT_PARAMS,
    'hide_dtype': '13',
    'spp': '30',
}
CATALOG_PARAMS = {
    **DEFAULT_PARAMS,
    'ab_testing': 'false',
    'hide_dtype': '13',
    'sort': 'popular',
    'spp': '30',
}
HTTP_TIMEOUT = 10

CACHE_TTL_SECONDS = 300
CACHE_MAX_ENTRIES = 10_000

HEALTH_FAILURES_TO_COOLDOWN = 3
HEALTH_COOLDOWN_SECONDS = 600

DEFAULT_PUBLIC_RPM = 20
INTERACTIVE_CATALOG_LIMIT = 100
EXACT_CARD_BATCH_LIMIT = 100


class WBRateLimitedError(RuntimeError):
    """WB вернул 429 (или источник недоступен) — публичный IP-бюджет исчерпан."""


def _resolve_public_rpm():
    raw = os.environ.get('COMPETITOR_PUBLIC_RPM', '')
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_PUBLIC_RPM
    return max(1, min(60, value))


_rate_limiter = None
_rate_limiter_lock = threading.Lock()


def get_global_rate_limiter():
    global _rate_limiter
    with _rate_limiter_lock:
        if _rate_limiter is None:
            _rate_limiter = RateLimiter(
                max_requests=_resolve_public_rpm(), time_window=60)
        return _rate_limiter


class SourceHealthRegistry:
    """Circuit breaker per источник: N подряд 429/5xx -> cooldown."""

    def __init__(self):
        self._lock = threading.Lock()
        self._state = {}  # source -> {'consecutive_failures': int, 'cooldown_until': float}

    def _entry(self, source):
        return self._state.setdefault(
            source, {'consecutive_failures': 0, 'cooldown_until': 0.0})

    def allowed(self, source):
        with self._lock:
            return time.time() >= self._entry(source)['cooldown_until']

    def record_success(self, source):
        with self._lock:
            entry = self._entry(source)
            entry['consecutive_failures'] = 0
            entry['cooldown_until'] = 0.0

    def record_failure(self, source):
        with self._lock:
            entry = self._entry(source)
            entry['consecutive_failures'] += 1
            if entry['consecutive_failures'] >= HEALTH_FAILURES_TO_COOLDOWN:
                entry['cooldown_until'] = time.time() + HEALTH_COOLDOWN_SECONDS
                logger.warning(
                    'Источник %s уходит в cooldown на %sс после %s ошибок подряд',
                    source, HEALTH_COOLDOWN_SECONDS, entry['consecutive_failures'])

    def snapshot(self):
        with self._lock:
            return {k: dict(v) for k, v in self._state.items()}


_source_health = SourceHealthRegistry()


def get_source_health():
    return _source_health


# Кросс-селлер кэш наблюдений: {nm_id: (obs, ts)}
_observation_cache = {}
_cache_lock = threading.Lock()


def get_cached_observation(nm_id):
    now = time.time()
    with _cache_lock:
        entry = _observation_cache.get(nm_id)
        if entry and (now - entry[1]) < CACHE_TTL_SECONDS:
            return entry[0]
        if entry:
            _observation_cache.pop(nm_id, None)
    return None


def put_cached_observation(nm_id, obs):
    now = time.time()
    with _cache_lock:
        if len(_observation_cache) >= CACHE_MAX_ENTRIES:
            # грубая очистка: выкинуть протухшие, при нехватке — старейшие
            expired = [k for k, v in _observation_cache.items()
                       if (now - v[1]) >= CACHE_TTL_SECONDS]
            for k in expired:
                _observation_cache.pop(k, None)
            while len(_observation_cache) >= CACHE_MAX_ENTRIES:
                oldest = min(_observation_cache,
                             key=lambda k: _observation_cache[k][1])
                _observation_cache.pop(oldest, None)
        _observation_cache[nm_id] = (obs, now)


def clear_observation_cache():
    with _cache_lock:
        _observation_cache.clear()


def _basket_base_url(nm_id):
    from services.wb_media import wb_basket_base_url
    return wb_basket_base_url(nm_id)


def _image_url(nm_id):
    from services.wb_media import wb_photo_url
    return wb_photo_url(nm_id, 1, 'big')


def _rubles(value):
    """Positive WB kopecks -> integer rubles; invalid/zero is not observed."""
    if isinstance(value, bool):
        return None
    try:
        amount = int(value)
    except (TypeError, ValueError):
        return None
    return amount // 100 if amount > 0 else None


def parse_price_observation(raw):
    """Coherent public price pair from one WB size, in integer rubles.

    ``basic`` is the crossed-out price before discounts. ``total`` is the
    most final storefront value on response variants that expose it;
    current v4 responses normally expose that value as ``product``.  The two
    fields are never assembled from different sizes.
    """
    sizes = raw.get('sizes') or []
    price = None
    sale_price = None
    total_stock = raw.get('totalQuantity', 0) or 0

    for size in sizes:
        if not isinstance(size, dict):
            continue
        price_obj = size.get('price') or {}
        if not isinstance(price_obj, dict):
            continue
        observed_base = _rubles(price_obj.get('basic'))
        observed_final = (
            _rubles(price_obj.get('total'))
            or _rubles(price_obj.get('product'))
        )
        if observed_base is not None or observed_final is not None:
            price = observed_base
            sale_price = observed_final
            break

    if price is None:
        price = _rubles(raw.get('priceU'))
    if sale_price is None:
        sale_price = _rubles(raw.get('salePriceU'))

    if total_stock == 0 and sizes:
        for s in sizes:
            for stock in s.get('stocks') or []:
                total_stock += stock.get('qty', 0) or 0

    return {
        'price': price,
        'sale_price': sale_price,
        'rating': raw.get('reviewRating') or None,
        'feedbacks_count': raw.get('feedbacks') or 0,
        'total_stock': total_stock,
    }


def parse_full_product(raw):
    """Полная карточка из search/catalog: метаданные + наблюдение."""
    nm_id = raw.get('id', 0)
    subject_id = raw.get('subjectId')
    if isinstance(subject_id, bool) or not isinstance(subject_id, int):
        subject_id = None
    photo_count = raw.get('pics')
    if (isinstance(photo_count, bool) or not isinstance(photo_count, int)
            or photo_count < 0):
        photo_count = None
    result = {
        'nm_id': nm_id,
        'title': raw.get('name', ''),
        'brand': raw.get('brand', ''),
        'supplier_name': raw.get('supplier', ''),
        'wb_supplier_id': raw.get('supplierId'),
        'image_url': _image_url(nm_id) if nm_id else None,
        'subject_id': subject_id,
        'subject_name': (
            raw.get('entity') or raw.get('subjectName') or ''),
        'photo_count': photo_count,
        'characteristics': [],
    }
    result.update(parse_price_observation(raw))
    return result


def _bounded_card_characteristics(card, limit=60):
    """Whitelist public card options into a small name/value fact list."""
    rows = []
    seen = set()

    def add_option(option):
        if not isinstance(option, dict) or len(rows) >= limit:
            return
        name = option.get('name')
        value = option.get('value')
        if not isinstance(name, str):
            return
        name = ' '.join(name.split())[:120]
        if not name:
            return
        if isinstance(value, (str, int, float)) and not isinstance(value, bool):
            value = ' '.join(str(value).split())[:240]
        elif isinstance(value, list):
            safe = [
                ' '.join(str(item).split())[:80]
                for item in value[:10]
                if isinstance(item, (str, int, float))
                and not isinstance(item, bool)
            ]
            value = ', '.join(item for item in safe if item)[:240]
        else:
            return
        if not value:
            return
        key = name.casefold()
        if key in seen:
            return
        seen.add(key)
        rows.append({'name': name, 'value': value})

    for option in card.get('options') or []:
        add_option(option)
    for group in card.get('grouped_options') or []:
        if not isinstance(group, dict):
            continue
        for option in group.get('options') or []:
            add_option(option)
    return rows


class CompetitorFetchService:
    """HTTP-клиент публичных WB-источников. Без ORM, без sleep на 429."""

    def __init__(self, proxy_url=None, session=None, rate_limiter=None, health=None):
        self._session = session or self._create_session(proxy_url)
        self._rate_limiter = rate_limiter or get_global_rate_limiter()
        self._health = health or get_source_health()

    @staticmethod
    def _create_session(proxy_url):
        session = requests.Session()
        retry = Retry(total=1, backoff_factor=0.3,
                      status_forcelist=[500, 502, 503, 504])
        adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=8)
        session.mount('https://', adapter)
        session.headers.update({
            'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                           'AppleWebKit/537.36 (KHTML, like Gecko) '
                           'Chrome/131.0.0.0 Safari/537.36'),
            'Accept': 'application/json',
            'Accept-Language': 'ru-RU,ru;q=0.9',
        })
        if proxy_url:
            session.proxies = {'http': proxy_url, 'https': proxy_url}
        return session

    # ---------- метаданные (basket CDN) ----------

    def fetch_basket_metadata(self, nm_id):
        """dict | 'gone' (404 — товар удалён с WB) | None (transient)."""
        base_url = _basket_base_url(nm_id)
        self._rate_limiter.wait_if_needed()
        try:
            response = self._session.get(
                f'{base_url}/info/ru/card.json', timeout=HTTP_TIMEOUT)
        except requests.exceptions.RequestException as e:
            logger.warning('Товар %s: basket card.json недоступен: %s', nm_id, e)
            return None
        if response.status_code == 404:
            return 'gone'
        if response.status_code != 200:
            logger.warning('Товар %s: card.json -> %s', nm_id, response.status_code)
            return None
        try:
            card = response.json()
        except ValueError:
            return None

        seller_data = None
        try:
            self._rate_limiter.wait_if_needed()
            sellers_resp = self._session.get(
                f'{base_url}/info/sellers.json', timeout=HTTP_TIMEOUT)
            if sellers_resp.status_code == 200:
                seller_data = sellers_resp.json()
        except (requests.exceptions.RequestException, ValueError):
            pass

        selling = card.get('selling') or {}
        media = card.get('media') or {}
        card_data = card.get('data') or {}
        subject_id = card_data.get('subject_id')
        if isinstance(subject_id, bool) or not isinstance(subject_id, int):
            subject_id = None
        photo_count = media.get('photo_count')
        if (isinstance(photo_count, bool) or not isinstance(photo_count, int)
                or photo_count < 0):
            photo_count = None
        return {
            'nm_id': nm_id,
            'title': card.get('imt_name', ''),
            'brand': selling.get('brand_name', ''),
            'supplier_name': (
                (seller_data or {}).get('supplierName')
                or selling.get('brand_name', '')),
            'wb_supplier_id': (
                selling.get('supplier_id')
                or (seller_data or {}).get('supplierId')),
            'image_url': _image_url(nm_id),
            'is_adult': bool(selling.get('is_adult', False)),
            'subject_name': card.get('subj_name', ''),
            'subject_id': subject_id,
            'photo_count': photo_count,
            'characteristics': _bounded_card_characteristics(card),
        }

    # ---------- цены ----------

    def _bounded_get(self, source, url, params):
        """Один HTTP GET с health/limiter. 429/5xx -> failure + WBRateLimitedError."""
        if not self._health.allowed(source):
            raise WBRateLimitedError(f'{source} в cooldown')
        self._rate_limiter.wait_if_needed()
        try:
            response = self._session.get(url, params=params, timeout=HTTP_TIMEOUT)
        except requests.exceptions.RequestException as e:
            self._health.record_failure(source)
            raise WBRateLimitedError(f'{source}: transport error: {e}') from e
        if response.status_code == 429:
            self._health.record_failure(source)
            raise WBRateLimitedError(f'{source}: 429')
        if response.status_code >= 500:
            self._health.record_failure(source)
            raise WBRateLimitedError(f'{source}: {response.status_code}')
        if response.status_code != 200:
            # 403/404 и прочее: не считаем поломкой источника, но и данных нет
            return None
        self._health.record_success(source)
        try:
            return response.json()
        except ValueError:
            self._health.record_failure(source)
            return None

    @staticmethod
    def _extract_products(data):
        if not isinstance(data, dict):
            return []
        products = data.get('products')
        if not products:
            products = (data.get('data') or {}).get('products')
        return products or []

    def fetch_exact_prices(self, nm_ids):
        """One bounded card-v4 read for exact nmIDs.

        This is used for our own storefront prices so both comparison lanes
        come from the same public contract as competitor observations.
        """
        values = []
        seen = set()
        for nm_id in nm_ids:
            if (
                isinstance(nm_id, bool)
                or not isinstance(nm_id, int)
                or nm_id <= 0
            ):
                raise ValueError('nm_ids must contain positive integers')
            if nm_id not in seen:
                seen.add(nm_id)
                values.append(nm_id)
        if len(values) > EXACT_CARD_BATCH_LIMIT:
            raise ValueError(
                f'exact card batch exceeds {EXACT_CARD_BATCH_LIMIT}',
            )
        if not values:
            return {}
        data = self._bounded_get(
            SOURCE_CARD,
            CARD_URL,
            {**CARD_PARAMS, 'nm': ';'.join(str(value) for value in values)},
        )
        if data is None:
            return {}
        allowed = set(values)
        found = {}
        for product in self._extract_products(data)[:EXACT_CARD_BATCH_LIMIT]:
            product_id = product.get('id') if isinstance(product, dict) else None
            if product_id in allowed:
                found[product_id] = parse_price_observation(product)
        return found

    def fetch_supplier_prices(self, supplier_id, target_nm_ids, max_pages=5):
        """Цены товаров из каталога продавца. Молча останавливается на 429."""
        found = {}
        remaining = set(target_nm_ids)
        for page in range(1, max_pages + 1):
            if not remaining:
                break
            params = {
                **CATALOG_PARAMS,
                'supplier': str(supplier_id), 'page': str(page),
            }
            try:
                data = self._bounded_get(SOURCE_CATALOG, CATALOG_URL, params)
            except WBRateLimitedError:
                break
            if data is None:
                break
            products = self._extract_products(data)
            if not products:
                break
            for p in products:
                pid = p.get('id')
                if pid in remaining:
                    found[pid] = parse_price_observation(p)
                    remaining.discard(pid)
        return found

    def fetch_brand_prices(self, brand, target_nm_ids, max_pages=2):
        """Цены через search по бренду (fallback). Молча останавливается на 429."""
        found = {}
        remaining = set(target_nm_ids)
        for page in range(1, max_pages + 1):
            if not remaining:
                break
            params = {
                **DEFAULT_PARAMS, 'query': brand, 'resultset': 'catalog',
                'sort': 'popular', 'spp': '30', 'page': str(page),
            }
            try:
                data = self._bounded_get(SOURCE_SEARCH, SEARCH_URL, params)
            except WBRateLimitedError:
                break
            if data is None:
                break
            products = self._extract_products(data)
            if not products:
                break
            for p in products:
                pid = p.get('id')
                if pid in remaining:
                    found[pid] = parse_price_observation(p)
                    remaining.discard(pid)
        return found

    # ---------- интерактивные (bounded, 1 страница) ----------

    def search_products(self, query, limit=100):
        """Одна страница поиска. WBRateLimitedError при 429 — наружу."""
        params = {
            **DEFAULT_PARAMS, 'query': query, 'resultset': 'catalog',
            'sort': 'popular', 'spp': '30',
        }
        data = self._bounded_get(SOURCE_SEARCH, SEARCH_URL, params)
        if data is None:
            return []
        return [parse_full_product(p)
                for p in self._extract_products(data)[:limit]]

    def fetch_seller_catalog_page(self, supplier_id, page=1):
        """Одна страница каталога продавца. WBRateLimitedError при 429."""
        params = {
            **CATALOG_PARAMS,
            'supplier': str(supplier_id), 'page': str(page),
        }
        data = self._bounded_get(SOURCE_CATALOG, CATALOG_URL, params)
        if data is None:
            return []
        return [parse_full_product(p) for p in
                self._extract_products(data)[:INTERACTIVE_CATALOG_LIMIT]]
