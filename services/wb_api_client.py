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
    ANALYTICS_API_URL = "https://seller-analytics-api.wildberries.ru"  # Analytics/Reports API

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
            'discounts': self.DISCOUNTS_API_URL,  # Prices API v2
            'analytics': self.ANALYTICS_API_URL  # Analytics/Reports API
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
        logger.debug("API Key: ***настроен***")
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
                    # WB API возвращает ошибки в разных полях
                    wb_error = (
                        error_data.get('errorText')
                        or error_data.get('message')
                        or error_data.get('error')
                        or error_msg
                    )
                    # additionalErrors содержит детали по конкретным полям
                    additional = error_data.get('additionalErrors')
                    if additional:
                        if isinstance(additional, dict):
                            details = '; '.join(f'{k}: {v}' for k, v in additional.items())
                        else:
                            details = str(additional)
                        error_msg = f"{wb_error} | Детали: {details}"
                    else:
                        error_msg = str(wb_error) if wb_error != error_msg else error_msg

                    # Для 400 Bad Request без деталей — пытаемся дать подсказку
                    if response.status_code == 400 and error_msg in ('bad request', 'Bad Request', 'API Error 400'):
                        hints = []
                        # Анализируем request body для подсказок
                        if request_body_str:
                            try:
                                import json as _json
                                req_data = _json.loads(request_body_str)
                                if isinstance(req_data, list) and req_data:
                                    card = req_data[0] if isinstance(req_data[0], dict) else {}
                                    variants = card.get('variants', [])
                                    if variants:
                                        v = variants[0]
                                        chars = v.get('characteristics', [])
                                        if not chars:
                                            hints.append('нет характеристик')
                                        if not v.get('brand'):
                                            hints.append('не указан бренд')
                                        sizes = v.get('sizes', [])
                                        if sizes:
                                            for s in sizes:
                                                if not s.get('skus') or not s['skus'][0]:
                                                    hints.append('пустые баркоды (skus)')
                                                    break
                                        dims = v.get('dimensions', {})
                                        if not dims or not dims.get('length'):
                                            hints.append('не указаны габариты')
                                    if not card.get('subjectID'):
                                        hints.append('не указан subjectID (категория)')
                            except Exception:
                                pass
                        if hints:
                            error_msg = f"bad request (возможные причины: {', '.join(hints)})"

                    # Логируем полный ответ для отладки
                    logger.error(f"WB API {response.status_code} full response: {error_data}")
                    if request_body_str:
                        logger.error(f"WB API {response.status_code} request body: {request_body_str[:2000]}")
                except Exception:
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
        date_to: Optional[str] = None,
        limit: int = 100000,
    ) -> List[Dict[str, Any]]:
        """
        Получить отчет о продажах / реализации (Statistics API v5).

        Использует пагинацию через rrdid. Автоматически загружает
        все страницы до исчерпания данных.

        Args:
            date_from: Дата начала в формате YYYY-MM-DD
            date_to: Дата окончания (опционально)
            limit: Макс. строк на запрос (до 100000)

        Returns:
            Список строк отчёта реализации
        """
        endpoint = "/api/v5/supplier/reportDetailByPeriod"

        all_rows: List[Dict[str, Any]] = []
        rrdid = 0

        while True:
            params = {
                'dateFrom': date_from,
                'rrdid': rrdid,
                'limit': limit,
            }
            if date_to:
                params['dateTo'] = date_to

            response = self._make_request('GET', 'statistics', endpoint, params=params)

            # 204 = нет данных (конец пагинации или пустой отчёт)
            if response.status_code == 204:
                break

            page = response.json()
            if not isinstance(page, list) or not page:
                break

            all_rows.extend(page)

            # Если страница неполная — данные кончились
            if len(page) < limit:
                break

            # Пагинация: берём rrd_id последней строки
            last_rrd_id = page[-1].get('rrd_id', 0)
            if last_rrd_id and last_rrd_id != rrdid:
                rrdid = last_rrd_id
                # reportDetailByPeriod: макс 1 запрос/мин
                logger.info(f"Pagination: fetched {len(all_rows)} rows, next rrdid={rrdid}, waiting 61s...")
                time.sleep(61)
            else:
                break

        return all_rows

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

    def get_fresh_sizes_map(
        self,
        nm_ids: List[int],
        log_to_db: bool = False,
        seller_id: int = None
    ) -> Dict[int, list]:
        """
        Получить актуальные sizes (с chrtID) для списка nmID из WB API.

        Используется для безопасного batch-обновления карточек:
        sizes из локальной БД могут не содержать chrtID, что приводит
        к ошибке "Неуникальный баркод" при обновлении.

        Args:
            nm_ids: Список nmID карточек
            log_to_db: Логировать запросы в БД
            seller_id: ID продавца для логирования

        Returns:
            Словарь {nmID: sizes_list} с актуальными размерами из WB
        """
        sizes_map = {}
        if not nm_ids:
            return sizes_map

        # Фильтруем невалидные nm_ids
        valid_nm_ids = [nm for nm in nm_ids if nm and nm > 0]
        if not valid_nm_ids:
            return sizes_map

        logger.info(f"📥 Fetching fresh sizes for {len(valid_nm_ids)} cards from WB API...")

        for nm_id in valid_nm_ids:
            try:
                card = self.get_card_by_nm_id(nm_id, log_to_db=log_to_db, seller_id=seller_id)
                if card and card.get('sizes'):
                    sizes_map[nm_id] = card['sizes']
            except Exception as e:
                logger.warning(f"⚠️ Failed to fetch sizes for nmID={nm_id}: {e}")

        logger.info(f"✅ Fetched fresh sizes for {len(sizes_map)}/{len(valid_nm_ids)} cards")
        return sizes_map

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
        from services.wb_validators import prepare_card_for_update, validate_and_log_errors, clean_characteristics_for_update

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
            from services.wb_validators import validate_card_update
            for i, card in enumerate(cards):
                is_valid, validation_errors = validate_card_update(card)
                if not is_valid:
                    nm_id = card.get('nmID', '?')
                    vendor_code = card.get('vendorCode', '?')
                    errors_str = '; '.join(validation_errors)
                    msg = (
                        f"Ошибка валидации карточки nmID={nm_id} "
                        f"({vendor_code}): {errors_str}"
                    )
                    logger.error(f"❌ {msg}")
                    raise WBAPIException(msg)

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

    def upload_photos_to_card(
        self,
        nm_id: int,
        photo_paths: List[str],
        seller_id: int = None
    ) -> List[Dict]:
        """
        Загрузить фото в карточку WB через Content API v3 media/file

        Args:
            nm_id: Артикул WB (nmID)
            photo_paths: Список путей к JPEG-файлам на диске
            seller_id: ID продавца для логирования

        Returns:
            Список результатов по каждому фото

        Note:
            WB API /content/v3/media/file принимает multipart/form-data
            Параметры передаются в ЗАГОЛОВКАХ:
              X-Nm-Id: артикул WB (nmID)
              X-Photo-Number: номер фото (1-based)
            Body: multipart/form-data с полем uploadfile
        """
        endpoint = "/content/v3/media/file"
        results = []

        for idx, path in enumerate(photo_paths):
            photo_number = idx + 1
            logger.info(f"📤 Uploading photo {photo_number}/{len(photo_paths)} for nmID={nm_id}: {path}")

            try:
                with open(path, 'rb') as f:
                    files = {'uploadfile': (f'photo_{photo_number}.jpg', f, 'image/jpeg')}
                    # WB API требует X-Nm-Id и X-Photo-Number в ЗАГОЛОВКАХ, не в query
                    extra_headers = {
                        'X-Nm-Id': str(nm_id),
                        'X-Photo-Number': str(photo_number),
                    }

                    # Remove Content-Type header for multipart upload
                    old_content_type = self.session.headers.pop('Content-Type', None)
                    try:
                        response = self._make_request(
                            'POST', 'content', endpoint,
                            headers=extra_headers,
                            files=files,
                            log_to_db=False,
                            seller_id=seller_id
                        )
                    finally:
                        if old_content_type:
                            self.session.headers['Content-Type'] = old_content_type

                    result = response.json() if response.content else {}
                    # WB может вернуть 200 с error в теле
                    if result.get('error'):
                        error_text = result.get('errorText', result.get('message', 'Unknown error'))
                        logger.error(f"❌ Photo {photo_number} API error (200 body): {error_text}")
                        results.append({'photo_number': photo_number, 'success': False, 'error': error_text, 'response': result})
                    else:
                        logger.info(f"✅ Photo {photo_number} uploaded: {result}")
                        results.append({'photo_number': photo_number, 'success': True, 'response': result})

            except Exception as e:
                logger.error(f"❌ Failed to upload photo {photo_number} for nmID={nm_id}: {e}")
                results.append({'photo_number': photo_number, 'success': False, 'error': str(e)})

        return results

    def upload_photos_by_url(
        self,
        nm_id: int,
        photo_urls: List[str],
        seller_id: int = None
    ) -> Dict[str, Any]:
        """
        Загрузить фото в карточку WB по URL через Content API v3 media/save

        Args:
            nm_id: Артикул WB (nmID)
            photo_urls: Список публичных URL фотографий
            seller_id: ID продавца для логирования

        Returns:
            Результат загрузки

        Note:
            POST /content/v3/media/save принимает JSON:
            {"nmId": 123, "data": ["url1", "url2"]}
            Новые фото ЗАМЕНЯЮТ старые. Чтобы добавить — укажите и новые, и старые URL.
        """
        endpoint = "/content/v3/media/save"

        body = {
            "nmId": nm_id,
            "data": photo_urls
        }

        logger.info(f"📤 Uploading {len(photo_urls)} photos by URL for nmID={nm_id}")

        try:
            response = self._make_request(
                'POST', 'content', endpoint,
                json=body,
                log_to_db=True,
                seller_id=seller_id
            )
            result = response.json() if response.content else {}
            # WB может вернуть 200 с error в теле
            if result.get('error'):
                error_text = result.get('errorText', result.get('message', 'Unknown error'))
                logger.error(f"❌ Photos by URL API error (200 body): {error_text}")
                raise WBAPIException(f"Media save error: {error_text}")
            logger.info(f"✅ Photos uploaded by URL: {result}")
            return result
        except WBAPIException:
            raise
        except Exception as e:
            logger.error(f"❌ Failed to upload photos by URL for nmID={nm_id}: {e}")
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

        # Валидация: фильтруем невалидные элементы перед отправкой
        valid_prices = []
        invalid_count = 0
        for p in prices:
            nm_id = p.get('nmID')
            price = p.get('price')
            if not nm_id or not isinstance(nm_id, int) or nm_id <= 0:
                logger.warning(f"⚠️ Skipping invalid nmID: {nm_id}")
                invalid_count += 1
                continue
            if price is None or (isinstance(price, (int, float)) and price <= 0):
                logger.warning(f"⚠️ Skipping nmID {nm_id}: price={price} (must be > 0)")
                invalid_count += 1
                continue
            valid_prices.append(p)

        if invalid_count > 0:
            logger.warning(f"⚠️ Filtered out {invalid_count} invalid items before upload")

        if not valid_prices:
            logger.warning("⚠️ No valid prices to upload after filtering")
            return {'data': None, 'error': False, 'errorText': ''}

        endpoint = "/api/v2/upload/task"

        # Преобразуем формат для API
        body = {
            "data": valid_prices
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
                error_str = str(e)
                logger.error(f"  ❌ Batch {i+1} failed: {error_str}")

                # Если батч упал целиком — пробуем отправить меньшими частями
                # чтобы изолировать невалидные элементы от валидных
                if len(batch) > 1:
                    logger.info(f"  🔄 Retrying batch {i+1} in sub-batches of 100...")
                    sub_batches = chunk_list(batch, 100)
                    for si, sub_batch in enumerate(sub_batches):
                        try:
                            self.upload_prices_v2(
                                sub_batch,
                                log_to_db=False,
                                seller_id=seller_id
                            )
                            result['success'] += len(sub_batch)
                            logger.info(f"    Sub-batch {si+1}/{len(sub_batches)}: OK ({len(sub_batch)} items)")
                        except WBAPIException as sub_e:
                            # Суб-батч тоже упал — помечаем эти nmID как failed
                            result['failed'] += len(sub_batch)
                            result['errors'].append({
                                'batch': i + 1,
                                'sub_batch': si + 1,
                                'error': str(sub_e),
                                'nm_ids': [p.get('nmID') for p in sub_batch]
                            })
                            logger.error(f"    Sub-batch {si+1}/{len(sub_batches)}: FAILED ({len(sub_batch)} items)")
                else:
                    result['failed'] += len(batch)
                    result['errors'].append({
                        'batch': i + 1,
                        'error': error_str,
                        'nm_ids': [p.get('nmID') for p in batch]
                    })

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

    def get_cards_error_list(
        self,
        log_to_db: bool = False,
        seller_id: int = None
    ) -> List[Dict[str, Any]]:
        """
        Получить список ошибок создания/обновления карточек (Content API v2)

        WB API возвращает 200 даже при ошибке merge/unmerge.
        Реальные ошибки нужно проверять через этот endpoint.

        Returns:
            Список ошибок: [{"object": "...", "nmID": 123, "updatedAt": "...", "errors": ["..."]}]
        """
        endpoint = "/content/v2/cards/error/list"

        try:
            response = self._make_request(
                'GET', 'content', endpoint,
                log_to_db=log_to_db,
                seller_id=seller_id
            )
            result = response.json()
            errors = result.get('data', []) or []
            if errors:
                logger.warning(f"WB cards error list: {len(errors)} errors found")
            return errors
        except Exception as e:
            logger.error(f"Failed to get cards error list: {e}")
            return []

    def get_card_by_nm_id(
        self,
        nm_id: int,
        log_to_db: bool = False,
        seller_id: int = None
    ) -> Optional[Dict[str, Any]]:
        """
        Получить карточку по nmID для определения актуального imtID.

        Returns:
            Данные карточки или None
        """
        try:
            result = self.get_cards_list(
                limit=100,
                filter_nm_id=nm_id,
                log_to_db=log_to_db,
                seller_id=seller_id
            )
            cards = result.get('cards', [])
            for card in cards:
                if card.get('nmID') == nm_id:
                    return card
            return None
        except Exception as e:
            logger.error(f"Failed to get card by nmID={nm_id}: {e}")
            return None

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

    def get_brands_by_subject(self, subject_id: int, top: int = 5000) -> Dict[str, Any]:
        """
        Получить бренды по ID предмета (категории) из WB API.

        Эндпоинт: GET /api/content/v1/brands
        Обязательные параметры: subjectId, pattern, top.

        Перебирает буквы алфавита (а-я, a-z, 0-9) как pattern,
        чтобы собрать максимум брендов для данной категории.

        Args:
            subject_id: ID предмета (subjectID) — обязателен
            top: максимальное количество брендов на запрос

        Returns:
            {"data": [{"id": 123, "name": "Brand Name"}, ...]}
        """
        endpoint = "/api/content/v1/brands"
        all_brands = {}  # id -> brand_data

        # Перебираем буквы алфавита для полноты покрытия
        patterns = list('абвгдежзиклмнопрстуфхцчшщэюя') + list('abcdefghijklmnopqrstuvwxyz') + list('0123456789')

        for pattern in patterns:
            params = {
                'subjectId': subject_id,
                'top': top,
                'pattern': pattern,
                'locale': 'ru',
            }

            try:
                response = self._make_request('GET', 'content', endpoint, params=params)
                result = response.json()
                brands = result.get('brands', [])
                for b in brands:
                    bid = b.get('id')
                    if bid and bid not in all_brands:
                        all_brands[bid] = b
                time.sleep(0.3)
            except WBRateLimitException:
                logger.warning(f"Rate limited on brands subjectId={subject_id} pattern='{pattern}', waiting 60s")
                time.sleep(60)
                try:
                    response = self._make_request('GET', 'content', endpoint, params=params)
                    for b in response.json().get('brands', []):
                        bid = b.get('id')
                        if bid and bid not in all_brands:
                            all_brands[bid] = b
                except Exception:
                    pass
            except Exception as e:
                logger.warning(f"Failed brands subjectId={subject_id} pattern='{pattern}': {e}")

        brands_list = list(all_brands.values())
        logger.info(f"Found {len(brands_list)} unique brands for subjectId={subject_id}")
        return {'data': brands_list}

    def get_brands_by_subject_quick(self, subject_id: int, pattern: str = 'а',
                                     top: int = 5000) -> Dict[str, Any]:
        """
        Быстрый запрос брендов для одной категории (один запрос).

        Args:
            subject_id: ID предмета (обязателен)
            pattern: строка поиска
            top: макс. результатов
        """
        endpoint = "/api/content/v1/brands"
        params = {
            'subjectId': subject_id,
            'top': top,
            'pattern': pattern,
            'locale': 'ru',
        }
        response = self._make_request('GET', 'content', endpoint, params=params)
        return response.json()

    def fetch_all_brands(self, subject_ids: list, top: int = 5000,
                         progress_callback=None) -> Dict[str, Any]:
        """
        Получить бренды из WB по списку категорий.

        Для каждой категории перебирает несколько ключевых букв алфавита
        (а, е, к, о, с, a, e, o, s, 1) как pattern для покрытия.

        Args:
            subject_ids: список ID предметов (subjectID)
            top: макс. результатов на один запрос
            progress_callback: callable(done, total, brands_so_far)
        """
        endpoint = "/api/content/v1/brands"
        all_brands = {}  # id -> brand_data
        self._fetch_debug = None

        # Ключевые буквы для покрытия (гласные + частые согласные + цифра)
        key_patterns = list('аеиокстнрabcdemost1')
        total = len(subject_ids)

        for i, subject_id in enumerate(subject_ids):
            for pattern in key_patterns:
                params = {
                    'subjectId': subject_id,
                    'top': top,
                    'pattern': pattern,
                    'locale': 'ru',
                }

                try:
                    response = self._make_request('GET', 'content', endpoint, params=params)

                    # Диагностика первого успешного запроса
                    if not self._fetch_debug:
                        self._fetch_debug = {
                            'url': response.url,
                            'status': response.status_code,
                            'raw_body': response.text[:500],
                        }

                    result = response.json()
                    brands = result.get('brands', [])
                    for b in brands:
                        bid = b.get('id')
                        if bid and bid not in all_brands:
                            all_brands[bid] = b
                    time.sleep(0.3)
                except WBRateLimitException:
                    if not self._fetch_debug:
                        self._fetch_debug = {'error': 'WBRateLimitException', 'pattern': pattern, 'subjectId': subject_id}
                    logger.warning(f"Rate limited on brands, waiting 60s")
                    time.sleep(60)
                    try:
                        response = self._make_request('GET', 'content', endpoint, params=params)
                        for b in response.json().get('brands', []):
                            bid = b.get('id')
                            if bid and bid not in all_brands:
                                all_brands[bid] = b
                    except Exception:
                        pass
                except Exception as e:
                    if not self._fetch_debug:
                        self._fetch_debug = {'error': f'{type(e).__name__}: {str(e)[:300]}', 'pattern': pattern, 'subjectId': subject_id}
                    logger.warning(f"Failed brands subjectId={subject_id} pattern='{pattern}': {e}")

            if progress_callback:
                progress_callback(i + 1, total, len(all_brands))

        brands_list = list(all_brands.values())
        logger.info(f"Fetched {len(brands_list)} unique brands from {total} categories")
        return {'data': brands_list}

    def search_brands(self, pattern: str, top: int = 50) -> Dict[str, Any]:
        """
        Поиск брендов по названию через WB API автокомплит.

        Args:
            pattern: Строка поиска (часть названия бренда)
            top: Максимальное количество результатов

        Returns:
            Dict с данными о брендах: {"data": [...]}
        """
        if not pattern or not pattern.strip():
            return {'data': []}

        logger.info(f"Searching brands with pattern: '{pattern}'")
        endpoint = "/api/content/v1/brands"

        # subjectId обязателен — берём распространённый subject для широкого поиска
        # Используем переданный subject_id или дефолтный (Футболки = 1)
        search_subject = getattr(self, '_default_subject_id', None) or 1

        params = {
            'subjectId': search_subject,
            'top': top,
            'pattern': pattern.strip(),
            'locale': 'ru',
        }

        try:
            response = self._make_request('GET', 'content', endpoint, params=params)
            result = response.json()
            brands = result.get('brands', [])
            logger.info(f"Found {len(brands)} brands matching '{pattern}'")
            return {'data': brands}
        except Exception as e:
            logger.error(f"Failed to search brands '{pattern}': {str(e)}")
            raise

    def validate_brand(self, brand_name: str, subject_id: int = None) -> Dict[str, Any]:
        """
        Проверить существует ли бренд в справочнике WB.

        Использует GET /api/content/v1/brands с subjectId.
        Если subject_id не указан, проверяет по нескольким категориям.

        Args:
            brand_name: Название бренда для проверки
            subject_id: Опциональный ID предмета для проверки

        Returns:
            Dict с результатом:
            {
                "valid": bool,
                "exact_match": {"id": int, "name": str} или None,
                "suggestions": [{"id": int, "name": str}, ...]
            }
        """
        logger.info(f"🔍 Validating brand: '{brand_name}'" + (f" (subjectId={subject_id})" if subject_id else ""))

        try:
            all_brands = []
            seen_ids = set()

            if subject_id:
                # Проверяем конкретную категорию
                subject_ids = [subject_id]
            else:
                # Берём несколько популярных категорий для поиска
                subjects_result = self.get_subjects_list(limit=100)
                subjects = subjects_result.get('data', [])
                subject_ids = [s.get('subjectID') for s in subjects[:30] if s.get('subjectID')]

            for sid in subject_ids:
                try:
                    result = self.get_brands_by_subject(sid)
                    for brand in result.get('brands', result.get('data', [])):
                        brand_id = brand.get('id')
                        if brand_id and brand_id not in seen_ids:
                            seen_ids.add(brand_id)
                            all_brands.append(brand)
                    time.sleep(0.1)
                except Exception as e:
                    logger.warning(f"   Brands for subjectId={sid} failed: {e}")
                    continue

                # Проверяем, нашли ли уже точное совпадение (для ранней остановки)
                brand_lower = brand_name.lower().strip()
                brand_normalized = ''.join(c.lower() for c in brand_name if c.isalnum())
                for b in all_brands:
                    b_name = b.get('name', '')
                    if b_name.lower().strip() == brand_lower:
                        logger.info(f"✅ Brand '{brand_name}' found: exact match '{b_name}'")
                        return {
                            'valid': True,
                            'exact_match': b,
                            'suggestions': [],
                        }

            # Полный поиск по собранным брендам
            brand_lower = brand_name.lower().strip()
            brand_normalized = ''.join(c.lower() for c in brand_name if c.isalnum())

            exact_match = None
            close_match = None
            suggestions = []

            for brand in all_brands:
                brand_wb_name = brand.get('name', '')
                wb_name_lower = brand_wb_name.lower().strip()
                wb_name_normalized = ''.join(c.lower() for c in brand_wb_name if c.isalnum())

                if wb_name_lower == brand_lower:
                    exact_match = brand
                    continue

                if wb_name_normalized == brand_normalized and not exact_match:
                    exact_match = brand
                    logger.info(f"   Found normalized match: '{brand_wb_name}' for '{brand_name}'")
                    continue

                if brand_normalized in wb_name_normalized or wb_name_normalized in brand_normalized:
                    if not close_match:
                        close_match = brand

                suggestions.append(brand)

            if not exact_match and close_match:
                exact_match = close_match
                logger.info(f"   Using close match: '{close_match.get('name')}' for '{brand_name}'")

            is_valid = exact_match is not None

            logger.info(f"{'✅' if is_valid else '⚠️'} Brand '{brand_name}' validation: {'found' if is_valid else 'not found'}, exact='{exact_match.get('name') if exact_match else None}', {len(suggestions)} suggestions")

            return {
                'valid': is_valid,
                'exact_match': exact_match,
                'suggestions': suggestions[:15]
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
        Получить список несозданных карточек товаров с ошибками.

        WB API v2 POST /content/v2/cards/error/list
        Возвращает структуру:
        {
            "data": {
                "items": [
                    {
                        "batchUUID": "...",
                        "vendorCodes": ["id-xxx-1366"],
                        "errors": {"id-xxx-1366": ["Ошибка 1", "Ошибка 2"]},
                        "updatedAt": "..."
                    }
                ],
                "cursor": {"next": false, ...}
            },
            "error": false,
            "errorText": ""
        }

        Args:
            log_to_db: Логировать ли запрос в БД
            seller_id: ID продавца для логирования

        Returns:
            Полный ответ от WB API
        """
        endpoint = "/content/v2/cards/error/list"

        logger.info("Getting cards errors list")

        try:
            response = self._make_request(
                'POST',
                'content',
                endpoint,
                json={},
                log_to_db=log_to_db,
                seller_id=seller_id
            )
            result = response.json()

            # Новый формат: data.items вместо data (массив)
            items = []
            data = result.get('data')
            if isinstance(data, dict):
                items = data.get('items', [])
            elif isinstance(data, list):
                items = data  # Старый формат (fallback)

            logger.info(f"Cards errors list loaded: {len(items)} batches with errors")

            return result

        except Exception as e:
            logger.error(f"Failed to get cards errors list: {str(e)}")
            raise

    # ==================== ЗАБЛОКИРОВАННЫЕ / СКРЫТЫЕ КАРТОЧКИ ====================

    def get_blocked_cards(
        self,
        sort: str = 'nmId',
        order: str = 'asc',
        log_to_db: bool = True,
        seller_id: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """
        Получить список заблокированных карточек товаров с причинами блокировки

        API: GET https://seller-analytics-api.wildberries.ru/api/v1/analytics/banned-products/blocked
        Лимит: 1 запрос в 10 секунд, всплеск 6

        Args:
            sort: Поле сортировки (brand, nmId, title, vendorCode, reason)
            order: Порядок сортировки (asc, desc)
            log_to_db: Логировать запрос в БД
            seller_id: ID продавца для логирования

        Returns:
            Список заблокированных карточек:
            [
                {
                    "brand": "Бренд",
                    "nmId": 82722944,
                    "title": "Наименование товара",
                    "vendorCode": "артикул-продавца",
                    "reason": "Причина блокировки"
                }
            ]
        """
        endpoint = "/api/v1/analytics/banned-products/blocked"

        valid_sort = ['brand', 'nmId', 'title', 'vendorCode', 'reason']
        if sort not in valid_sort:
            sort = 'nmId'

        params = {
            'sort': sort,
            'order': order if order in ('asc', 'desc') else 'asc'
        }

        logger.info(f"📋 Getting blocked cards (sort={sort}, order={order})")

        try:
            response = self._make_request(
                'GET', 'analytics', endpoint,
                params=params,
                log_to_db=log_to_db,
                seller_id=seller_id
            )
            result = response.json()
            cards = result.get('report', [])
            if cards is None:
                cards = []
            if not cards and result:
                logger.warning(
                    f"⚠️ Blocked cards API returned empty report. "
                    f"Response keys: {list(result.keys())}. "
                    f"Check that API token has 'contentanalytics' category permission."
                )
            logger.info(f"✅ Blocked cards loaded: {len(cards)} items")
            return cards
        except Exception as e:
            logger.error(f"❌ Failed to get blocked cards: {str(e)}")
            raise

    def get_shadowed_cards(
        self,
        sort: str = 'nmId',
        order: str = 'asc',
        log_to_db: bool = True,
        seller_id: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """
        Получить список товаров, скрытых из каталога

        API: GET https://seller-analytics-api.wildberries.ru/api/v1/analytics/banned-products/shadowed
        Лимит: 1 запрос в 10 секунд, всплеск 6

        Args:
            sort: Поле сортировки (brand, nmId, title, vendorCode, nmRating)
            order: Порядок сортировки (asc, desc)
            log_to_db: Логировать запрос в БД
            seller_id: ID продавца для логирования

        Returns:
            Список скрытых карточек:
            [
                {
                    "brand": "Бренд",
                    "nmId": 166658151,
                    "title": "Наименование товара",
                    "vendorCode": "артикул-продавца",
                    "nmRating": 3.1
                }
            ]
        """
        endpoint = "/api/v1/analytics/banned-products/shadowed"

        valid_sort = ['brand', 'nmId', 'title', 'vendorCode', 'nmRating']
        if sort not in valid_sort:
            sort = 'nmId'

        params = {
            'sort': sort,
            'order': order if order in ('asc', 'desc') else 'asc'
        }

        logger.info(f"📋 Getting shadowed cards (sort={sort}, order={order})")

        try:
            response = self._make_request(
                'GET', 'analytics', endpoint,
                params=params,
                log_to_db=log_to_db,
                seller_id=seller_id
            )
            result = response.json()
            cards = result.get('report', [])
            if cards is None:
                cards = []
            if not cards and result:
                logger.warning(
                    f"⚠️ Shadowed cards API returned empty report. "
                    f"Response keys: {list(result.keys())}. "
                    f"Check that API token has 'contentanalytics' category permission."
                )
            logger.info(f"✅ Shadowed cards loaded: {len(cards)} items")
            return cards
        except Exception as e:
            logger.error(f"❌ Failed to get shadowed cards: {str(e)}")
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

    # ==================== АНАЛИТИКА: ВОРОНКА ПРОДАЖ (V3) ====================

    def get_sales_funnel_products(
        self,
        period_start: str,
        period_end: str,
        past_period_start: Optional[str] = None,
        past_period_end: Optional[str] = None,
        nm_ids: Optional[List[int]] = None,
        brand_names: Optional[List[str]] = None,
        subject_ids: Optional[List[int]] = None,
        order_by: Optional[Dict] = None,
        limit: int = 50,
        offset: int = 0,
        log_to_db: bool = True,
        seller_id: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Статистика карточек товаров за период (воронка продаж v3).

        API: POST https://seller-analytics-api.wildberries.ru/api/analytics/v3/sales-funnel/products
        Лимит: 3 запроса в минуту, интервал 20 секунд

        Args:
            period_start: Начало периода (YYYY-MM-DD)
            period_end: Конец периода (YYYY-MM-DD)
            past_period_start: Начало периода для сравнения
            past_period_end: Конец периода для сравнения
            nm_ids: Артикулы WB для фильтрации (до 1000)
            brand_names: Бренды для фильтрации
            subject_ids: ID предметов для фильтрации
            order_by: Сортировка {field, mode}
            limit: Количество карточек в ответе
            offset: Сколько элементов пропустить

        Returns:
            Данные воронки продаж с products[]
        """
        body = {
            'selectedPeriod': {
                'start': period_start,
                'end': period_end,
            },
            'limit': limit,
            'offset': offset,
        }

        if past_period_start and past_period_end:
            body['pastPeriod'] = {
                'start': past_period_start,
                'end': past_period_end,
            }

        if nm_ids:
            body['nmIds'] = nm_ids
        if brand_names:
            body['brandNames'] = brand_names
        if subject_ids:
            body['subjectIds'] = subject_ids
        if order_by:
            body['orderBy'] = order_by

        response = self._make_request(
            'POST', 'analytics',
            '/api/analytics/v3/sales-funnel/products',
            json=body,
            log_to_db=log_to_db,
            seller_id=seller_id
        )
        return response.json()

    def get_sales_funnel_products_all(
        self,
        period_start: str,
        period_end: str,
        past_period_start: Optional[str] = None,
        past_period_end: Optional[str] = None,
        nm_ids: Optional[List[int]] = None,
        brand_names: Optional[List[str]] = None,
        subject_ids: Optional[List[int]] = None,
        log_to_db: bool = True,
        seller_id: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """
        Получить ВСЕ карточки из воронки продаж с пагинацией.
        Автоматически запрашивает все страницы.

        Returns:
            Полный список products
        """
        all_products = []
        offset = 0
        page_size = 50

        while True:
            result = self.get_sales_funnel_products(
                period_start=period_start,
                period_end=period_end,
                past_period_start=past_period_start,
                past_period_end=past_period_end,
                nm_ids=nm_ids,
                brand_names=brand_names,
                subject_ids=subject_ids,
                limit=page_size,
                offset=offset,
                log_to_db=log_to_db,
                seller_id=seller_id
            )

            data = result.get('data', {})
            products = data.get('products', [])
            all_products.extend(products)

            if len(products) < page_size:
                break

            offset += page_size
            time.sleep(20)  # Лимит: 3 запроса в минуту, интервал 20 секунд

        logger.info(f"Loaded {len(all_products)} products from sales funnel")
        return all_products

    def get_sales_funnel_history(
        self,
        period_start: str,
        period_end: str,
        nm_ids: List[int],
        aggregation_level: str = 'day',
        log_to_db: bool = True,
        seller_id: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """
        Статистика карточек товаров по дням (воронка продаж v3).

        API: POST https://seller-analytics-api.wildberries.ru/api/analytics/v3/sales-funnel/products/history
        Лимит: 3 запроса в минуту, до 20 nmIds

        Args:
            period_start: Начало периода (YYYY-MM-DD), макс. за неделю
            period_end: Конец периода (YYYY-MM-DD)
            nm_ids: Артикулы WB (до 20 шт.)
            aggregation_level: Уровень агрегации ('day' или 'week')

        Returns:
            Массив [{product, history: [{date, orderCount, orderSum, ...}]}]
        """
        body = {
            'selectedPeriod': {
                'start': period_start,
                'end': period_end,
            },
            'nmIds': nm_ids[:20],
            'aggregationLevel': aggregation_level,
        }

        response = self._make_request(
            'POST', 'analytics',
            '/api/analytics/v3/sales-funnel/products/history',
            json=body,
            log_to_db=log_to_db,
            seller_id=seller_id
        )
        return response.json()

    def get_sales_funnel_grouped_history(
        self,
        period_start: str,
        period_end: str,
        brand_names: Optional[List[str]] = None,
        subject_ids: Optional[List[int]] = None,
        tag_ids: Optional[List[int]] = None,
        aggregation_level: str = 'day',
        log_to_db: bool = True,
        seller_id: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Статистика групп карточек товаров по дням (воронка продаж v3).

        API: POST https://seller-analytics-api.wildberries.ru/api/analytics/v3/sales-funnel/grouped/history
        Лимит: 3 запроса в минуту, макс. за неделю

        Args:
            period_start: Начало периода (YYYY-MM-DD)
            period_end: Конец периода (YYYY-MM-DD)
            brand_names: Бренды для фильтрации
            subject_ids: ID предметов для фильтрации
            tag_ids: ID ярлыков
            aggregation_level: 'day' или 'week'

        Returns:
            Данные с history[] по дням
        """
        body = {
            'selectedPeriod': {
                'start': period_start,
                'end': period_end,
            },
            'aggregationLevel': aggregation_level,
        }

        if brand_names:
            body['brandNames'] = brand_names
        if subject_ids:
            body['subjectIds'] = subject_ids
        if tag_ids:
            body['tagIds'] = tag_ids

        response = self._make_request(
            'POST', 'analytics',
            '/api/analytics/v3/sales-funnel/grouped/history',
            json=body,
            log_to_db=log_to_db,
            seller_id=seller_id
        )
        return response.json()

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
