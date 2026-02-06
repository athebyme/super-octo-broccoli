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


def chunk_list(items: List, chunk_size: int) -> List[List]:
    """
    Разбить список на чанки (батчи)

    Args:
        items: Список элементов
        chunk_size: Размер чанка

    Returns:
        Список чанков

    Example:
        >>> chunk_list([1,2,3,4,5], 2)
        [[1,2], [3,4], [5]]
    """
    chunks = []
    for i in range(0, len(items), chunk_size):
        chunks.append(items[i:i + chunk_size])
    return chunks


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
    DISCOUNTS_API_URL = "https://discounts-prices-api.wildberries.ru"  # Prices API v2

    # Sandbox URLs для тестирования
    CONTENT_API_SANDBOX = "https://content-api-sandbox.wildberries.ru"
    STATISTICS_API_SANDBOX = "https://statistics-api-sandbox.wildberries.ru"

    def __init__(
        self,
        api_key: str,
        sandbox: bool = False,
        max_retries: int = 3,
        rate_limit: int = 100,
        timeout: int = 30,
        db_logger_callback = None
    ):
        """
        Args:
            api_key: API ключ Wildberries
            sandbox: Использовать sandbox-окружение
            max_retries: Максимальное количество повторов при ошибках
            rate_limit: Максимальное количество запросов в минуту
            timeout: Таймаут запроса в секундах
            db_logger_callback: Функция для логирования в БД
        """
        self.api_key = api_key
        self.sandbox = sandbox
        self.timeout = timeout
        self.db_logger_callback = db_logger_callback

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
            'marketplace': self.MARKETPLACE_API_URL,  # Нет sandbox для marketplace
            'discounts': self.DISCOUNTS_API_URL  # Prices API v2
        }
        return urls.get(api_type, self.CONTENT_API_URL)

    def _make_request(
        self,
        method: str,
        api_type: str,
        endpoint: str,
        log_to_db: bool = False,
        seller_id: int = None,
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

        # Сохраняем request body для логирования
        request_body_str = None
        if 'json' in kwargs and kwargs['json']:
            try:
                import json as json_module
                request_body_str = json_module.dumps(kwargs['json'], ensure_ascii=False)
            except:
                request_body_str = str(kwargs['json'])

        try:
            response = self.session.request(method, url, **kwargs)

            # Логирование времени выполнения
            elapsed = time.time() - start_time
            logger.info(f"WB API Response: {response.status_code} ({elapsed:.2f}s)")

            # Сохраняем response body для логирования
            response_body_str = None
            try:
                response_body_str = response.text
            except:
                pass

            # Логируем в БД если предоставлен callback
            if log_to_db and self.db_logger_callback and seller_id:
                try:
                    self.db_logger_callback(
                        seller_id=seller_id,
                        endpoint=endpoint,
                        method=method,
                        status_code=response.status_code,
                        response_time=elapsed,
                        success=(response.status_code < 400),
                        request_body=request_body_str,
                        response_body=response_body_str
                    )
                except Exception as log_error:
                    logger.warning(f"Failed to log to DB: {log_error}")

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

        except requests.exceptions.Timeout as e:
            elapsed = time.time() - start_time
            logger.error(f"Request timeout for {url} after {self.timeout}s")

            # Логируем timeout в БД
            if log_to_db and self.db_logger_callback and seller_id:
                try:
                    self.db_logger_callback(
                        seller_id=seller_id,
                        endpoint=endpoint,
                        method=method,
                        status_code=None,
                        response_time=elapsed,
                        success=False,
                        error_message=f"Timeout after {self.timeout}s",
                        request_body=request_body_str
                    )
                except Exception as log_error:
                    logger.warning(f"Failed to log timeout to DB: {log_error}")

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
        cursor_nm_id: Optional[int] = None,
        log_to_db: bool = False,
        seller_id: int = None
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

        response = self._make_request(
            'POST', 'content', endpoint,
            log_to_db=log_to_db,
            seller_id=seller_id,
            json=body
        )
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
            # Сохраняем текущий cursor перед запросом для проверки на зацикливание
            prev_cursor_updated_at = cursor_updated_at
            prev_cursor_nm_id = cursor_nm_id

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

            # Проверка на зацикливание - новый cursor не должен совпадать с предыдущим
            if (prev_cursor_updated_at is not None and
                prev_cursor_updated_at == cursor_updated_at and
                prev_cursor_nm_id == cursor_nm_id):
                logger.warning(f"Cursor not changing, stopping to avoid infinite loop. Total: {len(all_cards)}")
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
        updates: Dict[str, Any],
        merge_with_existing: bool = True,
        log_to_db: bool = False,
        seller_id: int = None,
        validate: bool = True
    ) -> Dict[str, Any]:
        """
        Обновить карточку товара (Content API v2)

        Args:
            nm_id: Артикул WB (nmID)
            updates: Словарь с обновляемыми полями
                Возможные поля:
                - vendorCode: артикул продавца
                - title: название товара (макс 60 символов)
                - description: описание (макс 5000 символов)
                - brand: бренд
                - dimensions: габариты (см и кг)
                - characteristics: список характеристик
                  [{"id": 123, "value": "значение"}]
                - sizes: массив размеров (обязательно)
            merge_with_existing: Если True, сначала получит полную карточку и объединит с изменениями
            log_to_db: Логировать запрос в БД
            seller_id: ID продавца для логирования
            validate: Валидировать данные перед отправкой

        Returns:
            Результат обновления

        Note:
            WB API v2 требует отправлять ПОЛНУЮ карточку товара.
            Метод автоматически получает текущую карточку и объединяет с изменениями.
        """
        from wb_validators import prepare_card_for_update, validate_and_log_errors, clean_characteristics_for_update

        logger.info(f"🔧 Updating card nmID={nm_id} with updates: {list(updates.keys())}")
        logger.debug(f"Update data: {updates}")

        # WB API требует полную карточку - получаем её сначала
        if merge_with_existing:
            logger.info(f"📥 Fetching full card for nmID={nm_id} to merge changes")
            try:
                full_card = self.get_card_by_nm_id(
                    nm_id,
                    log_to_db=log_to_db,
                    seller_id=seller_id
                )
                if not full_card:
                    raise WBAPIException(f"Card nmID={nm_id} not found in WB API")

                # Очищаем и валидируем характеристики если они есть в обновлениях
                if 'characteristics' in updates and updates['characteristics']:
                    updates['characteristics'] = clean_characteristics_for_update(updates['characteristics'])

                # Подготавливаем карточку для обновления (удаляем нередактируемые поля)
                card_to_send = prepare_card_for_update(full_card, updates)

            except Exception as e:
                logger.error(f"❌ Failed to fetch full card for merging: {str(e)}")
                logger.warning("⚠️ Trying to update with partial data (may fail)")
                card_to_send = {"nmID": nm_id, **updates}
        else:
            card_to_send = {"nmID": nm_id, **updates}

        # Валидация данных перед отправкой
        if validate:
            if not validate_and_log_errors(card_to_send, operation="update"):
                raise WBAPIException(f"Validation failed for card nmID={nm_id}")

        # WB Content API v2 эндпоинт для обновления
        endpoint = "/content/v2/cards/update"

        logger.info(f"📤 Sending update request for nmID={nm_id}")
        logger.debug(f"Card to send keys: {list(card_to_send.keys())}")

        # Логируем характеристики если они есть
        if 'characteristics' in card_to_send:
            logger.info(f"📋 Sending {len(card_to_send['characteristics'])} characteristics:")
            for i, char in enumerate(card_to_send['characteristics'][:5]):  # Первые 5
                logger.info(f"   Char #{i+1}: id={char.get('id')}, value={char.get('value')} (type: {type(char.get('value')).__name__})")
            if len(card_to_send['characteristics']) > 5:
                logger.info(f"   ... and {len(card_to_send['characteristics']) - 5} more")

        try:
            response = self._make_request(
                'POST', 'content', endpoint,
                log_to_db=log_to_db,
                seller_id=seller_id,
                json=[card_to_send]
            )
            result = response.json()
            logger.info(f"✅ Card nmID={nm_id} update response: {result}")
            return result
        except WBAPIException as e:
            logger.error(f"❌ WB API error updating card nmID={nm_id}: {str(e)}")
            logger.error(f"Sent data structure: {list(card_to_send.keys())}")
            raise
        except Exception as e:
            logger.error(f"❌ Unexpected error updating card nmID={nm_id}: {str(e)}")
            raise

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

    def update_cards_batch(
        self,
        cards: List[Dict[str, Any]],
        log_to_db: bool = False,
        seller_id: int = None,
        validate: bool = True
    ) -> Dict[str, Any]:
        """
        Обновить несколько карточек одним запросом (Content API v2)

        Args:
            cards: Список подготовленных карточек для обновления
                   Каждая карточка должна содержать:
                   - nmID: обязательно
                   - vendorCode: обязательно
                   - sizes: обязательно (массив)
                   - другие поля опционально
            log_to_db: Логировать запрос в БД
            seller_id: ID продавца для логирования
            validate: Валидировать данные перед отправкой

        Returns:
            Результат обновления

        Raises:
            WBAPIException: если слишком много карточек или размер запроса превышает лимит

        Note:
            - Максимум 3000 карточек за раз
            - Максимальный размер запроса 10 МБ
            - Все карточки должны быть ПОЛНЫМИ (не частичные обновления)
        """
        import sys

        if len(cards) > 3000:
            raise WBAPIException(
                f"Too many cards ({len(cards)}). "
                f"Maximum 3000 cards per request. Use chunking."
            )

        if not cards:
            logger.warning("⚠️ Empty cards list provided to update_cards_batch")
            return {'success': True, 'updated': 0}

        # Проверка размера запроса
        import json
        size_bytes = sys.getsizeof(json.dumps(cards))
        size_mb = size_bytes / 1024 / 1024

        if size_mb > 10:
            raise WBAPIException(
                f"Request size too large ({size_mb:.2f} MB). "
                f"Maximum 10 MB. Reduce batch size or remove heavy fields."
            )

        logger.info(f"📤 Batch update: {len(cards)} cards, size: {size_mb:.2f} MB")

        # Валидация карточек
        if validate:
            from wb_validators import validate_and_log_errors
            for i, card in enumerate(cards):
                if not validate_and_log_errors(card, operation="update"):
                    logger.error(f"❌ Validation failed for card #{i} (nmID={card.get('nmID')})")
                    raise WBAPIException(f"Validation failed for card #{i}")

        endpoint = "/content/v2/cards/update"

        try:
            response = self._make_request(
                'POST', 'content', endpoint,
                log_to_db=log_to_db,
                seller_id=seller_id,
                json=cards  # Отправляем массив карточек
            )
            result = response.json()
            logger.info(f"✅ Batch update result: {result}")
            return result
        except WBAPIException as e:
            logger.error(f"❌ WB API error in batch update: {str(e)}")
            raise
        except Exception as e:
            logger.error(f"❌ Unexpected error in batch update: {str(e)}")
            raise

    def update_prices(
        self,
        prices: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """
        Обновить цены товаров (Prices API) - DEPRECATED, используйте upload_prices_v2

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

    # ==================== PRICES API v2 ====================

    def get_goods_prices(
        self,
        limit: int = 1000,
        offset: int = 0,
        filter_nm_id: Optional[int] = None,
        log_to_db: bool = False,
        seller_id: int = None
    ) -> Dict[str, Any]:
        """
        Получить информацию о ценах товаров (Prices API v2)

        Args:
            limit: Количество записей (макс 1000)
            offset: Смещение для пагинации
            filter_nm_id: Фильтр по конкретному nmID
            log_to_db: Логировать запрос в БД
            seller_id: ID продавца для логирования

        Returns:
            {
                "data": {
                    "listGoods": [
                        {
                            "nmID": 12345,
                            "vendorCode": "ABC-123",
                            "sizes": [
                                {
                                    "sizeID": 0,
                                    "price": 1500,
                                    "discountedPrice": 1200,
                                    "techSizeName": "0"
                                }
                            ],
                            "currencyIsoCode4217": "RUB",
                            "discount": 20,
                            "editableSizePrice": false
                        }
                    ]
                }
            }
        """
        endpoint = "/api/v2/list/goods/filter"

        params = {
            'limit': min(limit, 1000),
            'offset': offset
        }

        if filter_nm_id:
            params['filterNmID'] = filter_nm_id

        logger.info(f"📋 Getting goods prices (limit={limit}, offset={offset})")

        try:
            response = self._make_request(
                'GET', 'discounts', endpoint,
                params=params,
                log_to_db=log_to_db,
                seller_id=seller_id
            )
            result = response.json()
            goods_count = len(result.get('data', {}).get('listGoods', []))
            logger.info(f"✅ Goods prices loaded: {goods_count} items")
            return result
        except Exception as e:
            logger.error(f"❌ Failed to get goods prices: {str(e)}")
            raise

    def get_all_goods_prices(
        self,
        batch_size: int = 1000,
        log_to_db: bool = False,
        seller_id: int = None
    ) -> List[Dict[str, Any]]:
        """
        Получить цены всех товаров с автоматической пагинацией

        Args:
            batch_size: Размер пачки для одного запроса (макс 1000)
            log_to_db: Логировать запросы в БД
            seller_id: ID продавца для логирования

        Returns:
            Список всех товаров с ценами
        """
        all_goods = []
        offset = 0

        while True:
            data = self.get_goods_prices(
                limit=batch_size,
                offset=offset,
                log_to_db=log_to_db,
                seller_id=seller_id
            )

            goods = data.get('data', {}).get('listGoods', [])

            if not goods:
                logger.info(f"No more goods to load. Total: {len(all_goods)}")
                break

            all_goods.extend(goods)
            logger.info(f"Loaded {len(all_goods)} goods so far...")

            # Если получили меньше чем лимит, значит это последняя пачка
            if len(goods) < batch_size:
                break

            offset += len(goods)

        logger.info(f"Total goods prices loaded: {len(all_goods)}")
        return all_goods

    def upload_prices_v2(
        self,
        prices: List[Dict[str, Any]],
        log_to_db: bool = False,
        seller_id: int = None
    ) -> Dict[str, Any]:
        """
        Загрузить цены и скидки (Prices API v2)

        Args:
            prices: Список обновлений цен
                Формат: [
                    {
                        "nmID": 12345,
                        "price": 1500,      # Цена до скидки
                        "discount": 20      # Скидка в процентах (опционально)
                    },
                    ...
                ]
            log_to_db: Логировать запрос в БД
            seller_id: ID продавца для логирования

        Returns:
            {
                "data": null,
                "error": false,
                "errorText": "",
                "additionalErrors": {}
            }

        Note:
            - Макс 1000 товаров за запрос
            - Цена должна быть в копейках (целое число) или в рублях (число с плавающей точкой)
            - Скидка указывается в процентах (0-99)
        """
        if len(prices) > 1000:
            raise WBAPIException(
                f"Too many prices ({len(prices)}). "
                f"Maximum 1000 items per request. Use chunking."
            )

        if not prices:
            logger.warning("⚠️ Empty prices list provided to upload_prices_v2")
            return {'data': None, 'error': False, 'errorText': ''}

        endpoint = "/api/v2/upload/task"

        # Преобразуем формат для API
        body = {
            "data": prices
        }

        logger.info(f"📤 Uploading {len(prices)} prices to WB")

        try:
            response = self._make_request(
                'POST', 'discounts', endpoint,
                json=body,
                log_to_db=log_to_db,
                seller_id=seller_id
            )
            result = response.json()

            if result.get('error'):
                logger.error(f"❌ WB API returned error: {result.get('errorText')}")
                additional_errors = result.get('additionalErrors', {})
                if additional_errors:
                    logger.error(f"   Additional errors: {additional_errors}")
                raise WBAPIException(f"API Error: {result.get('errorText')}")

            logger.info(f"✅ Prices uploaded successfully")
            return result

        except WBAPIException as e:
            logger.error(f"❌ WB API error in upload_prices_v2: {str(e)}")
            raise
        except Exception as e:
            logger.error(f"❌ Unexpected error in upload_prices_v2: {str(e)}")
            raise

    def upload_prices_batch(
        self,
        prices: List[Dict[str, Any]],
        batch_size: int = 1000,
        log_to_db: bool = False,
        seller_id: int = None
    ) -> Dict[str, Any]:
        """
        Загрузить цены пачками (для больших списков)

        Args:
            prices: Полный список обновлений цен
            batch_size: Размер одной пачки (макс 1000)
            log_to_db: Логировать запросы в БД
            seller_id: ID продавца для логирования

        Returns:
            {
                "total": 1500,
                "success": 1490,
                "failed": 10,
                "errors": [...]
            }
        """
        result = {
            'total': len(prices),
            'success': 0,
            'failed': 0,
            'errors': []
        }

        batches = chunk_list(prices, batch_size)
        logger.info(f"📦 Uploading {len(prices)} prices in {len(batches)} batches")

        for i, batch in enumerate(batches):
            logger.info(f"  Batch {i+1}/{len(batches)}: {len(batch)} items")
            try:
                self.upload_prices_v2(
                    batch,
                    log_to_db=log_to_db,
                    seller_id=seller_id
                )
                result['success'] += len(batch)
            except WBAPIException as e:
                result['failed'] += len(batch)
                result['errors'].append({
                    'batch': i + 1,
                    'error': str(e),
                    'nm_ids': [p.get('nmID') for p in batch]
                })
                logger.error(f"  ❌ Batch {i+1} failed: {str(e)}")

        logger.info(f"📊 Upload complete: {result['success']}/{result['total']} success")
        return result

    def get_price_upload_status(
        self,
        limit: int = 100,
        offset: int = 0,
        log_to_db: bool = False,
        seller_id: int = None
    ) -> Dict[str, Any]:
        """
        Получить статус обработанных загрузок цен (Prices API v2)

        Args:
            limit: Количество записей (макс 100)
            offset: Смещение для пагинации
            log_to_db: Логировать запрос в БД
            seller_id: ID продавца для логирования

        Returns:
            {
                "data": {
                    "uploadID": 123,
                    "status": 3,  # 3 = processed
                    "uploadDate": "2024-01-15T10:30:00Z",
                    "activationDate": "2024-01-15T10:35:00Z",
                    "overAllGoodsNumber": 100,
                    "successGoodsNumber": 98,
                    "failedGoods": [...]
                }
            }
        """
        endpoint = "/api/v2/history/tasks"

        params = {
            'limit': min(limit, 100),
            'offset': offset
        }

        logger.info(f"📋 Getting price upload status (limit={limit})")

        try:
            response = self._make_request(
                'GET', 'discounts', endpoint,
                params=params,
                log_to_db=log_to_db,
                seller_id=seller_id
            )
            result = response.json()
            logger.info(f"✅ Price upload status loaded")
            return result
        except Exception as e:
            logger.error(f"❌ Failed to get price upload status: {str(e)}")
            raise

    def get_price_buffer_status(
        self,
        limit: int = 100,
        offset: int = 0,
        log_to_db: bool = False,
        seller_id: int = None
    ) -> Dict[str, Any]:
        """
        Получить статус необработанных (буферных) загрузок цен (Prices API v2)

        Args:
            limit: Количество записей (макс 100)
            offset: Смещение для пагинации
            log_to_db: Логировать запрос в БД
            seller_id: ID продавца для логирования

        Returns:
            Список загрузок в буфере ожидающих обработки
        """
        endpoint = "/api/v2/buffer/tasks"

        params = {
            'limit': min(limit, 100),
            'offset': offset
        }

        logger.info(f"📋 Getting price buffer status (limit={limit})")

        try:
            response = self._make_request(
                'GET', 'discounts', endpoint,
                params=params,
                log_to_db=log_to_db,
                seller_id=seller_id
            )
            result = response.json()
            logger.info(f"✅ Price buffer status loaded")
            return result
        except Exception as e:
            logger.error(f"❌ Failed to get price buffer status: {str(e)}")
            raise

    def get_quarantine_goods(
        self,
        limit: int = 1000,
        offset: int = 0,
        log_to_db: bool = False,
        seller_id: int = None
    ) -> Dict[str, Any]:
        """
        Получить товары в карантине (Prices API v2)

        Карантин - это товары с потенциально ошибочными ценами,
        которые требуют проверки перед публикацией.

        Args:
            limit: Количество записей (макс 1000)
            offset: Смещение для пагинации
            log_to_db: Логировать запрос в БД
            seller_id: ID продавца для логирования

        Returns:
            {
                "data": {
                    "listGoods": [
                        {
                            "nmID": 12345,
                            "vendorCode": "ABC-123",
                            "sizes": [...],
                            "quarantineReason": "Цена ниже минимальной"
                        }
                    ]
                }
            }
        """
        endpoint = "/api/v2/quarantine/goods"

        params = {
            'limit': min(limit, 1000),
            'offset': offset
        }

        logger.info(f"📋 Getting quarantine goods (limit={limit})")

        try:
            response = self._make_request(
                'GET', 'discounts', endpoint,
                params=params,
                log_to_db=log_to_db,
                seller_id=seller_id
            )
            result = response.json()
            goods_count = len(result.get('data', {}).get('listGoods', []))
            logger.info(f"✅ Quarantine goods loaded: {goods_count} items")
            return result
        except Exception as e:
            logger.error(f"❌ Failed to get quarantine goods: {str(e)}")
            raise

    def get_card_by_nm_id(
        self,
        nm_id: int,
        log_to_db: bool = False,
        seller_id: int = None
    ) -> Optional[Dict[str, Any]]:
        """
        Получить полную карточку товара по nmID

        Args:
            nm_id: Артикул WB (nmID)
            log_to_db: Логировать запрос в БД
            seller_id: ID продавца для логирования

        Returns:
            Полная карточка товара или None если не найдена
        """
        logger.info(f"🔍 Getting card by nmID={nm_id}")

        try:
            data = self.get_cards_list(
                limit=1,
                filter_nm_id=nm_id,
                log_to_db=log_to_db,
                seller_id=seller_id
            )
            cards = data.get('cards', [])

            if not cards:
                logger.warning(f"⚠️ Card nmID={nm_id} not found in WB API")
                return None

            card = cards[0]
            logger.info(f"✅ Card nmID={nm_id} found: {card.get('vendorCode', 'N/A')}")
            return card
        except Exception as e:
            logger.error(f"❌ Failed to get card nmID={nm_id}: {str(e)}")
            raise

    def merge_cards(
        self,
        target_imt_id: int,
        nm_ids: List[int],
        log_to_db: bool = False,
        seller_id: int = None
    ) -> Dict[str, Any]:
        """
        Объединить карточки товаров (Content API v2)

        Карточки будут объединены под одним imtID (target_imt_id).
        Можно объединять только карточки с одинаковым предметом (subject_id).

        Args:
            target_imt_id: Существующий imtID, под которым необходимо объединить карточки
            nm_ids: Список nmID которые необходимо объединить (максимум 30)
            log_to_db: Логировать запрос в БД
            seller_id: ID продавца для логирования

        Returns:
            Результат объединения
            {
                "data": null,
                "error": false,
                "errorText": "",
                "additionalErrors": {}
            }

        Raises:
            WBAPIException: если слишком много карточек или другие ошибки

        Note:
            - Максимум 30 карточек за раз
            - Объединить можно только карточки с одинаковым предметом
        """
        if len(nm_ids) > 30:
            raise WBAPIException(
                f"Too many cards ({len(nm_ids)}). "
                f"Maximum 30 cards per request."
            )

        if not nm_ids:
            logger.warning("⚠️ Empty nm_ids list provided to merge_cards")
            return {'data': None, 'error': False, 'errorText': ''}

        endpoint = "/content/v2/cards/moveNm"

        body = {
            "targetIMT": target_imt_id,
            "nmIDs": nm_ids
        }

        logger.info(f"🔗 Merging {len(nm_ids)} cards to imtID={target_imt_id}")
        logger.debug(f"  nmIDs: {nm_ids}")

        try:
            response = self._make_request(
                'POST', 'content', endpoint,
                log_to_db=log_to_db,
                seller_id=seller_id,
                json=body
            )
            result = response.json()

            if result.get('error'):
                logger.error(f"❌ WB API returned error: {result.get('errorText')}")
                raise WBAPIException(f"API Error: {result.get('errorText')}")

            logger.info(f"✅ Cards merged successfully to imtID={target_imt_id}")
            return result
        except WBAPIException as e:
            logger.error(f"❌ WB API error in merge_cards: {str(e)}")
            raise
        except Exception as e:
            logger.error(f"❌ Unexpected error in merge_cards: {str(e)}")
            raise

    def unmerge_cards(
        self,
        nm_ids: List[int],
        log_to_db: bool = False,
        seller_id: int = None
    ) -> Dict[str, Any]:
        """
        Разъединить карточки товаров (Content API v2)

        Для разъединенных карточек будут сгенерированы новые imtID.

        Args:
            nm_ids: Список nmID которые необходимо разъединить (максимум 30)
            log_to_db: Логировать запрос в БД
            seller_id: ID продавца для логирования

        Returns:
            Результат разъединения
            {
                "data": null,
                "error": false,
                "errorText": "",
                "additionalErrors": {}
            }

        Raises:
            WBAPIException: если слишком много карточек или другие ошибки

        Note:
            - Максимум 30 карточек за раз
            - Если разъединить несколько карточек одновременно, они объединятся в одну с новым imtID
            - Чтобы присвоить каждой карточке уникальный imtID, передавайте по одной за запрос
        """
        if len(nm_ids) > 30:
            raise WBAPIException(
                f"Too many cards ({len(nm_ids)}). "
                f"Maximum 30 cards per request."
            )

        if not nm_ids:
            logger.warning("⚠️ Empty nm_ids list provided to unmerge_cards")
            return {'data': None, 'error': False, 'errorText': ''}

        endpoint = "/content/v2/cards/moveNm"

        body = {
            "nmIDs": nm_ids
        }

        logger.info(f"🔓 Unmerging {len(nm_ids)} cards")
        logger.debug(f"  nmIDs: {nm_ids}")

        try:
            response = self._make_request(
                'POST', 'content', endpoint,
                log_to_db=log_to_db,
                seller_id=seller_id,
                json=body
            )
            result = response.json()

            if result.get('error'):
                logger.error(f"❌ WB API returned error: {result.get('errorText')}")
                raise WBAPIException(f"API Error: {result.get('errorText')}")

            logger.info(f"✅ Cards unmerged successfully")
            return result
        except WBAPIException as e:
            logger.error(f"❌ WB API error in unmerge_cards: {str(e)}")
            raise
        except Exception as e:
            logger.error(f"❌ Unexpected error in unmerge_cards: {str(e)}")
            raise

    def get_subjects_list(
        self,
        name: Optional[str] = None,
        limit: int = 1000,
        offset: int = 0
    ) -> Dict[str, Any]:
        """
        Получить список предметов (subjects) из WB API

        Args:
            name: Поиск по названию предмета (опционально)
            limit: Количество предметов (максимум 1000)
            offset: Сколько элементов пропустить

        Returns:
            Список предметов с их ID и названиями
        """
        endpoint = "/content/v2/object/all"

        params = {
            'limit': min(limit, 1000),
            'offset': offset
        }

        if name:
            params['name'] = name

        logger.info(f"🔍 Getting subjects list (name={name}, limit={limit})")

        try:
            response = self._make_request('GET', 'content', endpoint, params=params)
            result = response.json()
            logger.info(f"✅ Subjects list loaded: {len(result.get('data', []))} items")
            return result
        except Exception as e:
            logger.error(f"❌ Failed to get subjects list: {str(e)}")
            raise

    def get_subject_id_by_name(self, object_name: str) -> Optional[int]:
        """
        Получить subject_id по названию предмета

        Args:
            object_name: Название предмета (например, "Футболки")

        Returns:
            subject_id или None если не найден
        """
        logger.info(f"🔍 Looking for subject_id for: {object_name}")

        try:
            result = self.get_subjects_list(name=object_name, limit=100)
            subjects = result.get('data', [])

            # Ищем точное совпадение по имени
            for subject in subjects:
                if subject.get('subjectName', '').lower() == object_name.lower():
                    subject_id = subject.get('subjectID')
                    logger.info(f"✅ Found exact match: {object_name} -> subjectID={subject_id}")
                    return subject_id

            # Если точного совпадения нет, берём первый результат
            if subjects:
                subject = subjects[0]
                subject_id = subject.get('subjectID')
                subject_name = subject.get('subjectName')
                logger.warning(f"⚠️ No exact match, using first result: {subject_name} -> subjectID={subject_id}")
                return subject_id

            logger.warning(f"⚠️ No subject found for: {object_name}")
            return None

        except Exception as e:
            logger.error(f"❌ Failed to get subject_id for {object_name}: {str(e)}")
            return None

    def get_card_characteristics_config(
        self,
        subject_id: int
    ) -> Dict[str, Any]:
        """
        Получить конфигурацию характеристик для предмета по его ID

        Args:
            subject_id: ID предмета (subjectID из WB API)

        Returns:
            Конфигурация характеристик с возможными значениями
        """
        endpoint = f"/content/v2/object/charcs/{subject_id}"

        logger.info(f"🔍 Getting characteristics config for subjectID: {subject_id}")

        try:
            response = self._make_request('GET', 'content', endpoint)
            result = response.json()
            logger.info(f"✅ Characteristics config loaded: {len(result.get('data', []))} items")
            return result
        except Exception as e:
            logger.error(f"❌ Failed to get characteristics config for subjectID={subject_id}: {str(e)}")
            raise

    def get_card_characteristics_by_object_name(
        self,
        object_name: str
    ) -> Dict[str, Any]:
        """
        Получить конфигурацию характеристик для типа товара по названию

        Args:
            object_name: Название типа товара (например, "Футболки")

        Returns:
            Конфигурация характеристик с возможными значениями

        Note:
            Этот метод сначала получает subject_id по названию,
            а затем запрашивает характеристики
        """
        logger.info(f"🔍 Getting characteristics for object: {object_name}")

        # Получаем subject_id по названию
        subject_id = self.get_subject_id_by_name(object_name)

        if not subject_id:
            raise WBAPIException(f"Subject не найден для: {object_name}")

        # Получаем характеристики по subject_id
        return self.get_card_characteristics_config(subject_id)

    def get_parent_categories(
        self,
        locale: str = 'ru'
    ) -> Dict[str, Any]:
        """
        Получить список родительских категорий товаров

        Args:
            locale: Язык для названий категорий ('ru', 'en', 'zh')

        Returns:
            Список родительских категорий с ID и названиями
        """
        endpoint = "/content/v2/object/parent/all"

        params = {}
        if locale:
            params['locale'] = locale

        logger.info(f"🔍 Getting parent categories (locale={locale})")

        try:
            response = self._make_request('GET', 'content', endpoint, params=params)
            result = response.json()
            logger.info(f"✅ Parent categories loaded: {len(result.get('data', []))} items")
            return result
        except Exception as e:
            logger.error(f"❌ Failed to get parent categories: {str(e)}")
            raise

    def get_directory_colors(self, locale: str = 'ru') -> Dict[str, Any]:
        """Получить справочник цветов"""
        endpoint = "/content/v2/directory/colors"
        params = {'locale': locale} if locale else {}

        logger.info(f"🎨 Getting colors directory (locale={locale})")
        try:
            response = self._make_request('GET', 'content', endpoint, params=params)
            result = response.json()
            logger.info(f"✅ Colors loaded: {len(result.get('data', []))} items")
            return result
        except Exception as e:
            logger.error(f"❌ Failed to get colors: {str(e)}")
            raise

    def get_directory_countries(self, locale: str = 'ru') -> Dict[str, Any]:
        """Получить справочник стран производства"""
        endpoint = "/content/v2/directory/countries"
        params = {'locale': locale} if locale else {}

        logger.info(f"🌍 Getting countries directory (locale={locale})")
        try:
            response = self._make_request('GET', 'content', endpoint, params=params)
            result = response.json()
            logger.info(f"✅ Countries loaded: {len(result.get('data', []))} items")
            return result
        except Exception as e:
            logger.error(f"❌ Failed to get countries: {str(e)}")
            raise

    def get_directory_kinds(self, locale: str = 'ru') -> Dict[str, Any]:
        """Получить справочник полов"""
        endpoint = "/content/v2/directory/kinds"
        params = {'locale': locale} if locale else {}

        logger.info(f"👤 Getting kinds/genders directory (locale={locale})")
        try:
            response = self._make_request('GET', 'content', endpoint, params=params)
            result = response.json()
            logger.info(f"✅ Kinds loaded: {len(result.get('data', []))} items")
            return result
        except Exception as e:
            logger.error(f"❌ Failed to get kinds: {str(e)}")
            raise

    def get_directory_seasons(self, locale: str = 'ru') -> Dict[str, Any]:
        """Получить справочник сезонов"""
        endpoint = "/content/v2/directory/seasons"
        params = {'locale': locale} if locale else {}

        logger.info(f"🌤️ Getting seasons directory (locale={locale})")
        try:
            response = self._make_request('GET', 'content', endpoint, params=params)
            result = response.json()
            logger.info(f"✅ Seasons loaded: {len(result.get('data', []))} items")
            return result
        except Exception as e:
            logger.error(f"❌ Failed to get seasons: {str(e)}")
            raise

    def get_directory_vat(self, locale: str = 'ru') -> Dict[str, Any]:
        """Получить справочник ставок НДС"""
        endpoint = "/content/v2/directory/vat"
        params = {'locale': locale} if locale else {}

        logger.info(f"💰 Getting VAT rates directory (locale={locale})")
        try:
            response = self._make_request('GET', 'content', endpoint, params=params)
            result = response.json()
            logger.info(f"✅ VAT rates loaded: {len(result.get('data', []))} items")
            return result
        except Exception as e:
            logger.error(f"❌ Failed to get VAT rates: {str(e)}")
            raise

    def get_directory_tnved(self, locale: str = 'ru') -> Dict[str, Any]:
        """Получить справочник кодов ТНВЭД"""
        endpoint = "/content/v2/directory/tnved"
        params = {'locale': locale} if locale else {}

        logger.info(f"📋 Getting TNVED codes directory (locale={locale})")
        try:
            response = self._make_request('GET', 'content', endpoint, params=params)
            result = response.json()
            logger.info(f"✅ TNVED codes loaded: {len(result.get('data', []))} items")
            return result
        except Exception as e:
            logger.error(f"❌ Failed to get TNVED codes: {str(e)}")
            raise

    def search_brands(self, pattern: str, top: int = 50) -> Dict[str, Any]:
        """
        Поиск брендов по названию в справочнике WB

        Args:
            pattern: Строка поиска (часть названия бренда)
            top: Максимальное количество результатов (по умолчанию 50)

        Returns:
            Dict с данными о брендах:
            {
                "data": [
                    {"id": 123, "name": "Brand Name"},
                    ...
                ]
            }

        Example:
            >>> client.search_brands("Nike")
            {"data": [{"id": 1234, "name": "Nike"}]}
        """
        endpoint = "/content/v2/directory/brands"
        params = {
            'pattern': pattern,
            'top': top
        }

        logger.info(f"🔍 Searching brands with pattern: '{pattern}'")
        try:
            response = self._make_request('GET', 'content', endpoint, params=params)
            result = response.json()
            brands_count = len(result.get('data', []))
            logger.info(f"✅ Found {brands_count} brands matching '{pattern}'")
            return result
        except Exception as e:
            logger.error(f"❌ Failed to search brands: {str(e)}")
            raise

    def validate_brand(self, brand_name: str) -> Dict[str, Any]:
        """
        Проверить существует ли бренд в справочнике WB

        Args:
            brand_name: Название бренда для проверки

        Returns:
            Dict с результатом:
            {
                "valid": bool,
                "exact_match": {"id": int, "name": str} или None,
                "suggestions": [{"id": int, "name": str}, ...]
            }

        Example:
            >>> client.validate_brand("Nike")
            {"valid": True, "exact_match": {"id": 1234, "name": "Nike"}, "suggestions": []}
        """
        logger.info(f"🔍 Validating brand: '{brand_name}'")

        try:
            all_brands = []
            seen_ids = set()

            # Попробуем несколько вариантов поиска для лучшего покрытия
            search_variants = [
                brand_name,  # Оригинальный запрос
                brand_name.lower(),  # Нижний регистр
                brand_name.upper(),  # Верхний регистр
                brand_name.capitalize(),  # С заглавной
            ]

            # Если бренд содержит несколько слов, попробуем первое слово
            words = brand_name.split()
            if len(words) > 1:
                search_variants.append(words[0])

            # Если бренд длинный, попробуем сокращенный вариант
            if len(brand_name) > 5:
                search_variants.append(brand_name[:5])

            # Удаляем дубликаты, сохраняя порядок
            unique_variants = []
            seen_variants = set()
            for v in search_variants:
                v_lower = v.lower()
                if v_lower not in seen_variants:
                    seen_variants.add(v_lower)
                    unique_variants.append(v)

            for variant in unique_variants:
                try:
                    result = self.search_brands(variant, top=30)
                    brands = result.get('data', [])
                    logger.info(f"   Search '{variant}': found {len(brands)} brands")

                    for brand in brands:
                        brand_id = brand.get('id')
                        if brand_id and brand_id not in seen_ids:
                            seen_ids.add(brand_id)
                            all_brands.append(brand)

                    # Если нашли достаточно - выходим
                    if len(all_brands) >= 20:
                        break
                except Exception as e:
                    logger.warning(f"   Search '{variant}' failed: {e}")
                    continue

            # Ищем точное совпадение (регистронезависимо)
            brand_lower = brand_name.lower().strip()
            exact_match = None
            suggestions = []

            for brand in all_brands:
                brand_wb_name = brand.get('name', '')
                if brand_wb_name.lower().strip() == brand_lower:
                    exact_match = brand
                else:
                    suggestions.append(brand)

            is_valid = exact_match is not None

            logger.info(f"{'✅' if is_valid else '⚠️'} Brand '{brand_name}' validation: {'found' if is_valid else 'not found'}, {len(suggestions)} suggestions")

            return {
                'valid': is_valid,
                'exact_match': exact_match,
                'suggestions': suggestions[:15]  # Максимум 15 предложений
            }
        except Exception as e:
            logger.error(f"❌ Failed to validate brand: {str(e)}")
            return {
                'valid': False,
                'exact_match': None,
                'suggestions': [],
                'error': str(e)
            }

    def create_product_card(
        self,
        subject_id: int,
        variants: List[Dict[str, Any]],
        log_to_db: bool = True,
        seller_id: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Создать новую карточку товара в WB

        Args:
            subject_id: ID предмета (категории товара)
            variants: Список вариантов товара. Каждый вариант - это dict с полями:
                - vendorCode (обязательно): Артикул продавца
                - brand: Бренд
                - title: Название товара (макс 60 символов)
                - description: Описание товара (1000-5000 символов в зависимости от категории)
                - dimensions: Габариты и вес {length, width, height, weightBrutto}
                - sizes: Массив размеров [{techSize, wbSize, price, skus}]
                - characteristics: Характеристики [{id, value}]
            log_to_db: Логировать ли запрос в БД
            seller_id: ID продавца для логирования

        Returns:
            Ответ от API WB

        Example:
            >>> client.create_product_card(
            ...     subject_id=106,
            ...     variants=[{
            ...         'vendorCode': 'MY-PRODUCT-001',
            ...         'brand': 'MyBrand',
            ...         'title': 'Футболка мужская',
            ...         'description': 'Качественная футболка из хлопка...',
            ...         'dimensions': {
            ...             'length': 30,
            ...             'width': 20,
            ...             'height': 5,
            ...             'weightBrutto': 0.2
            ...         },
            ...         'sizes': [{
            ...             'techSize': 'L',
            ...             'wbSize': '48',
            ...             'price': 1500,
            ...             'skus': ['2000000123456']
            ...         }],
            ...         'characteristics': [
            ...             {'id': 1234, 'value': ['Хлопок']},
            ...             {'id': 5678, 'value': ['Синий']}
            ...         ]
            ...     }]
            ... )
        """
        endpoint = "/content/v2/cards/upload"

        # Формируем тело запроса согласно спецификации WB API
        request_body = [{
            'subjectID': subject_id,
            'variants': variants
        }]

        logger.info(f"📤 Creating product card: subjectID={subject_id}, variants={len(variants)}")

        try:
            start_time = time.time()
            response = self._make_request(
                'POST',
                'content',
                endpoint,
                json=request_body,  # Исправлено: json вместо json_data
                log_to_db=log_to_db,
                seller_id=seller_id
            )
            response_time = time.time() - start_time

            result = response.json()

            # Проверяем ответ на ошибки
            if result.get('error'):
                error_text = result.get('errorText', 'Unknown error')
                logger.error(f"❌ Failed to create card: {error_text}")
                raise WBAPIException(f"Failed to create card: {error_text}")

            logger.info(f"✅ Product card created successfully in {response_time:.2f}s")
            logger.info(f"   Response: {result}")

            return result

        except Exception as e:
            logger.error(f"❌ Failed to create product card: {str(e)}")
            raise

    def get_cards_errors_list(
        self,
        log_to_db: bool = True,
        seller_id: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Получить список несозданных карточек товаров с ошибками

        Args:
            log_to_db: Логировать ли запрос в БД
            seller_id: ID продавца для логирования

        Returns:
            Список карточек с ошибками создания
        """
        endpoint = "/content/v2/cards/error/list"

        logger.info(f"🔍 Getting cards errors list")

        try:
            response = self._make_request(
                'POST',
                'content',
                endpoint,
                json={},  # Исправлено: json вместо json_data
                log_to_db=log_to_db,
                seller_id=seller_id
            )
            result = response.json()

            error_cards = result.get('data', [])
            logger.info(f"✅ Cards errors list loaded: {len(error_cards)} cards with errors")

            return result

        except Exception as e:
            logger.error(f"❌ Failed to get cards errors list: {str(e)}")
            raise

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
