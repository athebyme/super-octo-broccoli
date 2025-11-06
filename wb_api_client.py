"""
Wildberries API Client с оптимизацией и кэшированием
"""
import logging
import time
from datetime import datetime, timedelta
from functools import lru_cache, wraps
from typing import Dict, List, Optional, Any
from urllib.parse import urljoin

import requests
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry

# Настройка логирования
logger = logging.getLogger('wb_api')


class WBAPIException(Exception):
    """Базовое исключение для WB API"""
    pass


class WBAuthException(WBAPIException):
    """Ошибка аутентификации"""
    pass


class WBRateLimitException(WBAPIException):
    """Превышен лимит запросов"""
    pass


class RateLimiter:
    """Rate limiter для соблюдения лимитов API WB"""

    def __init__(self, max_requests: int = 100, time_window: int = 60):
        """
        Args:
            max_requests: Максимальное количество запросов
            time_window: Временное окно в секундах
        """
        self.max_requests = max_requests
        self.time_window = time_window
        self.requests_log: List[float] = []

    def wait_if_needed(self):
        """Ожидание если достигнут лимит запросов"""
        now = time.time()

        # Очистка старых записей
        self.requests_log = [
            req_time for req_time in self.requests_log
            if now - req_time < self.time_window
        ]

        # Проверка лимита
        if len(self.requests_log) >= self.max_requests:
            oldest_request = self.requests_log[0]
            sleep_time = self.time_window - (now - oldest_request)
            if sleep_time > 0:
                logger.warning(f"Rate limit reached. Sleeping for {sleep_time:.2f}s")
                time.sleep(sleep_time)

        self.requests_log.append(now)


class WildberriesAPIClient:
    """
    Оптимизированный клиент для работы с API Wildberries

    Особенности:
    - Connection pooling для переиспользования соединений
    - Автоматические retry при временных ошибках
    - Rate limiting для соблюдения лимитов API
    - Кэширование результатов
    - Логирование всех запросов
    """

    # Базовые URL для разных API
    CONTENT_API_URL = "https://content-api.wildberries.ru"
    STATISTICS_API_URL = "https://statistics-api.wildberries.ru"
    MARKETPLACE_API_URL = "https://marketplace-api.wildberries.ru"

    # Sandbox URLs для тестирования
    CONTENT_API_SANDBOX = "https://content-api-sandbox.wildberries.ru"
    STATISTICS_API_SANDBOX = "https://statistics-api-sandbox.wildberries.ru"

    def __init__(
        self,
        api_key: str,
        sandbox: bool = False,
        max_retries: int = 3,
        rate_limit: int = 100,
        timeout: int = 30
    ):
        """
        Args:
            api_key: API ключ Wildberries
            sandbox: Использовать sandbox-окружение
            max_retries: Максимальное количество повторов при ошибках
            rate_limit: Максимальное количество запросов в минуту
            timeout: Таймаут запроса в секундах
        """
        self.api_key = api_key
        self.sandbox = sandbox
        self.timeout = timeout

        # Rate limiter
        self.rate_limiter = RateLimiter(max_requests=rate_limit, time_window=60)

        # Настройка сессии с connection pooling
        self.session = self._create_session(max_retries)

        logger.info(f"WB API Client initialized (sandbox={sandbox})")

    def _create_session(self, max_retries: int) -> requests.Session:
        """Создание сессии с retry-логикой и connection pooling"""
        session = requests.Session()

        # Настройка retry-стратегии
        retry_strategy = Retry(
            total=max_retries,
            backoff_factor=1,  # 1s, 2s, 4s, ...
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["HEAD", "GET", "OPTIONS", "POST", "PUT"]
        )

        adapter = HTTPAdapter(
            max_retries=retry_strategy,
            pool_connections=10,  # Количество connection pools
            pool_maxsize=20       # Максимум соединений в pool
        )

        session.mount("http://", adapter)
        session.mount("https://", adapter)

        # Заголовки по умолчанию
        session.headers.update({
            'Authorization': self.api_key,
            'Content-Type': 'application/json',
            'Accept': 'application/json'
        })

        return session

    def _get_base_url(self, api_type: str) -> str:
        """Получить базовый URL для типа API"""
        urls = {
            'content': self.CONTENT_API_SANDBOX if self.sandbox else self.CONTENT_API_URL,
            'statistics': self.STATISTICS_API_SANDBOX if self.sandbox else self.STATISTICS_API_URL,
            'marketplace': self.MARKETPLACE_API_URL  # Нет sandbox для marketplace
        }
        return urls.get(api_type, self.CONTENT_API_URL)

    def _make_request(
        self,
        method: str,
        api_type: str,
        endpoint: str,
        **kwargs
    ) -> requests.Response:
        """
        Базовый метод для выполнения запросов с оптимизацией

        Args:
            method: HTTP метод (GET, POST, etc.)
            api_type: Тип API (content, statistics, marketplace)
            endpoint: Эндпоинт (без базового URL)
            **kwargs: Дополнительные параметры для requests

        Returns:
            Response object

        Raises:
            WBAuthException: Ошибка авторизации
            WBRateLimitException: Превышен лимит запросов
            WBAPIException: Общая ошибка API
        """
        # Rate limiting
        self.rate_limiter.wait_if_needed()

        # Формирование URL
        base_url = self._get_base_url(api_type)
        url = urljoin(base_url, endpoint)

        # Установка таймаута если не указан
        if 'timeout' not in kwargs:
            kwargs['timeout'] = self.timeout

        # Логирование запроса
        params_str = f" params={kwargs.get('params')}" if kwargs.get('params') else ""
        logger.info(f"WB API Request: {method} {url}{params_str}")
        logger.debug(f"API Key (first 10 chars): {self.api_key[:10]}...")
        start_time = time.time()

        try:
            response = self.session.request(method, url, **kwargs)

            # Логирование времени выполнения
            elapsed = time.time() - start_time
            logger.info(f"WB API Response: {response.status_code} ({elapsed:.2f}s)")

            # Обработка ошибок
            if response.status_code == 401:
                raise WBAuthException("Ошибка авторизации. Проверьте API ключ.")
            elif response.status_code == 429:
                raise WBRateLimitException("Превышен лимит запросов к API.")
            elif response.status_code >= 400:
                error_msg = f"API Error {response.status_code}"
                try:
                    error_data = response.json()
                    error_msg = error_data.get('message', error_msg)
                except:
                    error_msg = response.text or error_msg
                raise WBAPIException(error_msg)

            return response

        except requests.exceptions.Timeout:
            logger.error(f"Request timeout for {url} after {self.timeout}s")
            raise WBAPIException(f"Timeout при запросе к API ({self.timeout}s). Попробуйте позже.")
        except requests.exceptions.SSLError as e:
            logger.error(f"SSL error for {url}: {e}")
            raise WBAPIException(f"Ошибка SSL соединения: {str(e)}. Проверьте сетевое подключение.")
        except requests.exceptions.ConnectionError as e:
            logger.error(f"Connection error for {url}: {e}")
            error_msg = str(e)
            if "Name or service not known" in error_msg or "getaddrinfo failed" in error_msg:
                raise WBAPIException("Не удалось разрешить имя хоста API Wildberries. Проверьте интернет-соединение.")
            elif "Connection refused" in error_msg:
                raise WBAPIException("Подключение отклонено сервером API Wildberries. Проверьте URL и доступность API.")
            else:
                raise WBAPIException(f"Ошибка соединения с API Wildberries: {error_msg}")
        except (WBAuthException, WBRateLimitException, WBAPIException):
            raise
        except Exception as e:
            logger.exception(f"Unexpected error for {url}: {e}")
            raise WBAPIException(f"Неожиданная ошибка: {str(e)}")

    # ==================== CONTENT API ====================

    def get_cards_list(
        self,
        limit: int = 100,
        offset: int = 0,
        filter_nm_id: Optional[int] = None,
        cursor_updated_at: Optional[str] = None,
        cursor_nm_id: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Получить список карточек товаров (Content API v2)

        Args:
            limit: Количество карточек (макс 100)
            offset: Смещение для пагинации (deprecated, используйте cursor)
            filter_nm_id: Фильтр по nmID (артикулу WB)
            cursor_updated_at: Для пагинации - updatedAt из предыдущего ответа
            cursor_nm_id: Для пагинации - nmID из предыдущего ответа

        Returns:
            Словарь с данными карточек

        Note:
            API v2 использует POST метод и JSON body вместо GET с query params
        """
        endpoint = "/content/v2/get/cards/list"

        # Формируем body согласно документации WB API v2
        body = {
            "settings": {
                "cursor": {
                    "limit": min(limit, 100)  # WB ограничивает до 100
                },
                "filter": {
                    "withPhoto": -1  # -1 = все товары
                }
            }
        }

        # Добавляем cursor для пагинации (если указан)
        if cursor_updated_at and cursor_nm_id:
            body["settings"]["cursor"]["updatedAt"] = cursor_updated_at
            body["settings"]["cursor"]["nmID"] = cursor_nm_id

        # Фильтр по конкретному nmID
        if filter_nm_id:
            body["settings"]["filter"]["textSearch"] = str(filter_nm_id)

        response = self._make_request('POST', 'content', endpoint, json=body)
        return response.json()

    def get_card_by_vendor_code(self, vendor_code: str) -> Dict[str, Any]:
        """
        Получить карточку товара по артикулу поставщика

        Args:
            vendor_code: Артикул поставщика

        Returns:
            Данные карточки товара
        """
        endpoint = "/content/v2/get/cards/list"

        body = {
            "settings": {
                "cursor": {
                    "limit": 1
                },
                "filter": {
                    "textSearch": vendor_code,  # Поиск по артикулу
                    "withPhoto": -1
                }
            }
        }

        response = self._make_request('POST', 'content', endpoint, json=body)
        data = response.json()

        cards = data.get('cards', [])
        if not cards:
            raise WBAPIException(f"Товар с артикулом {vendor_code} не найден")

        return cards[0]

    def get_all_cards(self, batch_size: int = 100) -> List[Dict[str, Any]]:
        """
        Получить все карточки товаров с автоматической cursor-based пагинацией

        Args:
            batch_size: Размер пачки для одного запроса (макс 100)

        Returns:
            Список всех карточек

        Note:
            API v2 использует cursor-based пагинацию вместо offset
        """
        all_cards = []
        cursor_updated_at = None
        cursor_nm_id = None

        while True:
            # Запрос с cursor для пагинации
            data = self.get_cards_list(
                limit=batch_size,
                cursor_updated_at=cursor_updated_at,
                cursor_nm_id=cursor_nm_id
            )

            cards = data.get('cards', [])

            if not cards:
                logger.info(f"No more cards to load. Total: {len(all_cards)}")
                break

            all_cards.extend(cards)
            logger.info(f"Loaded {len(all_cards)} cards so far...")

            # Получаем cursor для следующей страницы
            cursor = data.get('cursor')
            if not cursor:
                logger.info(f"No cursor in response. Total cards: {len(all_cards)}")
                break

            # Если есть cursor, используем его для следующего запроса
            cursor_updated_at = cursor.get('updatedAt')
            cursor_nm_id = cursor.get('nmID')

            # Если нет данных для cursor, значит это последняя страница
            if not cursor_updated_at or not cursor_nm_id:
                logger.info(f"Pagination complete. Total cards: {len(all_cards)}")
                break

            # Если total указывает что мы получили все
            total = cursor.get('total', 0)
            if total > 0 and len(all_cards) >= total:
                logger.info(f"All {total} cards loaded")
                break

        logger.info(f"Total cards loaded: {len(all_cards)}")
        return all_cards

    # ==================== STATISTICS API ====================

    def get_sales_report(
        self,
        date_from: str,
        date_to: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Получить отчет о продажах (Statistics API)

        Args:
            date_from: Дата начала в формате YYYY-MM-DD
            date_to: Дата окончания (опционально)

        Returns:
            Список продаж
        """
        endpoint = "/api/v1/supplier/reportDetailByPeriod"

        params = {'dateFrom': date_from}
        if date_to:
            params['dateTo'] = date_to

        response = self._make_request('GET', 'statistics', endpoint, params=params)
        return response.json()

    def get_orders(
        self,
        date_from: str,
        flag: int = 0
    ) -> List[Dict[str, Any]]:
        """
        Получить заказы (Statistics API)

        Args:
            date_from: Дата начала в формате YYYY-MM-DD
            flag: Фильтр (0 - все, 1 - только новые)

        Returns:
            Список заказов
        """
        endpoint = "/api/v1/supplier/orders"

        params = {
            'dateFrom': date_from,
            'flag': flag
        }

        response = self._make_request('GET', 'statistics', endpoint, params=params)
        return response.json()

    def get_stocks(self, date_from: str) -> List[Dict[str, Any]]:
        """
        Получить остатки товаров (Statistics API)

        Args:
            date_from: Дата начала в формате YYYY-MM-DD

        Returns:
            Список остатков
        """
        endpoint = "/api/v1/supplier/stocks"

        params = {'dateFrom': date_from}

        response = self._make_request('GET', 'statistics', endpoint, params=params)
        return response.json()

    # ==================== MARKETPLACE API ====================

    def get_prices(self, quantity: int = 0) -> List[Dict[str, Any]]:
        """
        Получить цены товаров (Marketplace API)

        Args:
            quantity: Количество товаров (0 - все)

        Returns:
            Список цен
        """
        endpoint = "/api/v2/list/goods/filter"

        params = {'quantity': quantity}

        response = self._make_request('GET', 'marketplace', endpoint, params=params)
        return response.json()

    def get_warehouse_stocks(self, skip: int = 0, take: int = 1000) -> Dict[str, Any]:
        """
        Получить остатки по складам (Marketplace API)

        Args:
            skip: Сколько записей пропустить
            take: Сколько записей получить (макс 1000)

        Returns:
            Словарь с остатками по складам

        Endpoint: POST /api/v3/stocks/{warehouse_id}
        """
        endpoint = "/api/v3/stocks/0"  # 0 = все склады

        body = {
            "skip": skip,
            "take": min(take, 1000)  # WB ограничивает до 1000
        }

        response = self._make_request('POST', 'marketplace', endpoint, json=body)
        return response.json()

    def get_all_warehouse_stocks(self, batch_size: int = 1000) -> List[Dict[str, Any]]:
        """
        Получить все остатки по складам с автоматической пагинацией

        Args:
            batch_size: Размер пачки для одного запроса (макс 1000)

        Returns:
            Список всех остатков
        """
        all_stocks = []
        skip = 0

        while True:
            data = self.get_warehouse_stocks(skip=skip, take=batch_size)
            stocks = data.get('stocks', [])

            if not stocks:
                logger.info(f"No more stocks to load. Total: {len(all_stocks)}")
                break

            all_stocks.extend(stocks)
            logger.info(f"Loaded {len(all_stocks)} stock records so far...")

            # Если получили меньше чем лимит, значит это последняя пачка
            if len(stocks) < batch_size:
                break

            skip += len(stocks)

        logger.info(f"Total stock records loaded: {len(all_stocks)}")
        return all_stocks

    def update_card(
        self,
        nm_id: int,
        updates: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Обновить карточку товара (Content API v2)

        Args:
            nm_id: Артикул WB (nmID)
            updates: Словарь с обновляемыми полями
                Возможные поля:
                - vendorCode: артикул продавца
                - title: название товара
                - description: описание
                - brand: бренд
                - characteristics: список характеристик
                  [{"id": 123, "value": "значение"}]

        Returns:
            Результат обновления
        """
        endpoint = "/content/v2/card/update"

        # Формируем тело запроса
        body = {
            "nmID": nm_id,
            **updates
        }

        response = self._make_request('POST', 'content', endpoint, json=body)
        return response.json()

    def update_card_characteristics(
        self,
        nm_id: int,
        characteristics: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """
        Обновить характеристики карточки товара

        Args:
            nm_id: Артикул WB (nmID)
            characteristics: Список характеристик
                Формат: [{"id": 123, "value": "значение"}, ...]

        Returns:
            Результат обновления
        """
        return self.update_card(nm_id, {"characteristics": characteristics})

    def update_prices(
        self,
        prices: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """
        Обновить цены товаров (Prices API)

        Args:
            prices: Список обновлений цен
                Формат: [{"nmId": 12345, "price": 1000}, ...]

        Returns:
            Результат обновления
        """
        endpoint = "/public/api/v1/prices"

        body = prices

        response = self._make_request('POST', 'content', endpoint, json=body)
        return response.json()

    def get_card_characteristics_config(
        self,
        object_name: str
    ) -> Dict[str, Any]:
        """
        Получить конфигурацию характеристик для типа товара

        Args:
            object_name: Название типа товара (например, "Футболки")

        Returns:
            Конфигурация характеристик с возможными значениями
        """
        endpoint = "/content/v2/object/charcs/list/filter"

        body = {
            "name": object_name
        }

        response = self._make_request('POST', 'content', endpoint, json=body)
        return response.json()

    # ==================== УТИЛИТЫ ====================

    def test_connection(self) -> bool:
        """
        Проверить подключение к API

        Returns:
            True если подключение успешно
        """
        try:
            logger.info(f"Testing API connection to {self.CONTENT_API_URL}")
            # Пробуем получить одну карточку
            result = self.get_cards_list(limit=1)
            logger.info(f"API connection test successful. Response keys: {list(result.keys())}")
            return True
        except WBAuthException as e:
            logger.error(f"API auth test failed: {e}")
            return False
        except WBAPIException as e:
            logger.error(f"API connection test failed: {e}")
            return False
        except Exception as e:
            logger.exception(f"Unexpected error during connection test: {e}")
            return False

    def close(self):
        """Закрыть сессию и освободить ресурсы"""
        self.session.close()
        logger.info("WB API Client closed")

    def __enter__(self):
        """Context manager support"""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager cleanup"""
        self.close()


# ==================== КЭШИРОВАНИЕ ====================

class CachedWBAPIClient(WildberriesAPIClient):
    """
    Клиент с кэшированием результатов
    Использует LRU cache для часто запрашиваемых данных
    """

    def __init__(self, *args, cache_size: int = 128, cache_ttl: int = 300, **kwargs):
        """
        Args:
            cache_size: Размер кэша (количество элементов)
            cache_ttl: Время жизни кэша в секундах
        """
        super().__init__(*args, **kwargs)
        self.cache_ttl = cache_ttl
        self._cache_timestamps: Dict[str, float] = {}

    def _is_cache_valid(self, cache_key: str) -> bool:
        """Проверить актуальность кэша"""
        if cache_key not in self._cache_timestamps:
            return False

        age = time.time() - self._cache_timestamps[cache_key]
        return age < self.cache_ttl

    @lru_cache(maxsize=128)
    def _get_cards_list_cached(
        self,
        limit: int,
        cursor_updated_at: Optional[str],
        cursor_nm_id: Optional[int],
        filter_nm_id: Optional[int],
        timestamp: float  # Для инвалидации кэша по времени
    ) -> Dict[str, Any]:
        """Кэшированная версия get_cards_list"""
        return super().get_cards_list(
            limit=limit,
            cursor_updated_at=cursor_updated_at,
            cursor_nm_id=cursor_nm_id,
            filter_nm_id=filter_nm_id
        )

    def get_cards_list(
        self,
        limit: int = 100,
        offset: int = 0,
        filter_nm_id: Optional[int] = None,
        cursor_updated_at: Optional[str] = None,
        cursor_nm_id: Optional[int] = None,
        use_cache: bool = True
    ) -> Dict[str, Any]:
        """Получить карточки с кэшированием (поддержка cursor-based пагинации)"""
        if not use_cache:
            return super().get_cards_list(
                limit=limit,
                offset=offset,
                filter_nm_id=filter_nm_id,
                cursor_updated_at=cursor_updated_at,
                cursor_nm_id=cursor_nm_id
            )

        # Кэш-ключ теперь включает cursor параметры
        cache_key = f"cards_{limit}_{cursor_updated_at}_{cursor_nm_id}_{filter_nm_id}"

        # Проверка актуальности кэша
        if not self._is_cache_valid(cache_key):
            # Обновляем timestamp для инвалидации старого кэша
            self._cache_timestamps[cache_key] = time.time()

        # Получаем данные (из кэша или API)
        timestamp = self._cache_timestamps.get(cache_key, time.time())
        return self._get_cards_list_cached(
            limit, cursor_updated_at, cursor_nm_id, filter_nm_id, timestamp
        )


# ==================== ПРИМЕРЫ ИСПОЛЬЗОВАНИЯ ====================

if __name__ == "__main__":
    # Пример 1: Базовое использование
    api_key = "your_api_key_here"

    with WildberriesAPIClient(api_key, sandbox=True) as client:
        # Проверка подключения
        if client.test_connection():
            print("✓ Подключение к API успешно")

            # Получение карточек товаров
            cards = client.get_cards_list(limit=10)
            print(f"Загружено {len(cards.get('cards', []))} карточек")

    # Пример 2: С кэшированием
    with CachedWBAPIClient(api_key, cache_ttl=600) as client:
        # Первый запрос - идет в API
        cards1 = client.get_cards_list(limit=100)

        # Второй запрос - из кэша (быстрее)
        cards2 = client.get_cards_list(limit=100)

        print(f"Загружено {len(cards1.get('cards', []))} карточек")
