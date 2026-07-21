"""
Wildberries API Client с оптимизацией и кэшированием
"""
import logging
import copy
import threading as _threading
import time
from datetime import datetime, timedelta
from functools import lru_cache, wraps
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urljoin

import requests
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry

# Настройка логирования
logger = logging.getLogger('wb_api')

MAX_WB_MEDIA_FILES = 30


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


def normalize_cards_error_list(errors) -> tuple:
    """Нормализует записи /content/v2/cards/error/list в индексы для матчинга.

    Актуальный формат WB (проверен на живом API):
        {"batchUUID": "...", "vendorCodes": ["VC"],
         "errors": {"VC": ["текст"]}, "updatedAt": "..."}
    Легаси/документационный формат:
        {"object": "...", "nmID": 123, "vendorCode": "VC", "errors": ["текст"]}

    Returns:
        (errors_by_nm: {nm_id: [msgs]}, errors_by_vendor: {vendor_code: [msgs]})
    """
    errors_by_nm = {}
    errors_by_vendor = {}
    for err in errors or []:
        if not isinstance(err, dict):
            continue
        raw = err.get('errors')
        if isinstance(raw, dict):
            # Актуальный формат: errors — dict {vendorCode: [messages]}
            for vendor, msgs in raw.items():
                if vendor and msgs:
                    errors_by_vendor[str(vendor)] = [str(m) for m in msgs]
            continue
        # Легаси формат: errors — список, идентификаторы на верхнем уровне
        msgs = [str(m) for m in (raw or [])]
        if not msgs:
            continue
        nm = err.get('nmID') or err.get('nmId')
        if nm:
            try:
                errors_by_nm[int(nm)] = msgs
            except (TypeError, ValueError):
                pass
        vendor = err.get('vendorCode') or err.get('vendor_code')
        if vendor:
            errors_by_vendor[str(vendor)] = msgs
        for vendor in (err.get('vendorCodes') or []):
            if vendor:
                errors_by_vendor[str(vendor)] = msgs
    return errors_by_nm, errors_by_vendor


class WBAPIException(Exception):
    """Базовое исключение для WB API"""
    pass


class WBAuthException(WBAPIException):
    """Ошибка аутентификации"""
    pass


class WBRateLimitException(WBAPIException):
    """Превышен лимит запросов.

    retry_after — секунды до повтора из заголовка X-Ratelimit-Retry (или None).
    """

    def __init__(self, message: str, retry_after: int = None):
        super().__init__(message)
        self.retry_after = retry_after


class WBTransportUncertainException(WBAPIException):
    """Transport/server failure whose write may already have reached WB."""

    def __init__(self, message: str, *, request_may_have_been_applied: bool):
        super().__init__(message)
        self.request_may_have_been_applied = bool(request_may_have_been_applied)


class RateLimiter:
    """Thread-safe rate limiter для соблюдения лимитов API WB"""

    def __init__(self, max_requests: int = 100, time_window: int = 60):
        """
        Args:
            max_requests: Максимальное количество запросов
            time_window: Временное окно в секундах
        """
        import threading
        self.max_requests = max_requests
        self.time_window = time_window
        self.requests_log: List[float] = []
        self._lock = threading.Lock()

    def wait_if_needed(self):
        """Ожидание если достигнут лимит запросов (thread-safe)"""
        while True:
            with self._lock:
                now = time.time()
                self.requests_log = [
                    req_time for req_time in self.requests_log
                    if now - req_time < self.time_window
                ]
                if len(self.requests_log) < self.max_requests:
                    self.requests_log.append(now)
                    return
                sleep_time = self.time_window - (now - self.requests_log[0])

            if sleep_time > 0:
                logger.warning(f"Rate limit reached. Sleeping for {sleep_time:.2f}s")
                time.sleep(sleep_time)


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

    # Наши бюджеты держим ЧУТЬ НИЖЕ официальных лимитов WB — запас на
    # параллельные обновления цен/остатков и на правило «4XX считается за 10».
    # Отдельные лимиты WB для конкретных методов. Integer values retain the
    # legacy per-minute window; tuples explicitly define (requests, seconds).
    ENDPOINT_RATE_LIMITS = {
        '/content/v2/cards/update': 8,
        '/content/v2/cards/upload': 8,
        '/content/v2/cards/upload/add': 8,
        '/api/content/v1/brands': (1, 1),
    }

    # Бюджеты по категориям API: (запросов, окно в секундах).
    # WB: Контент 100/мин; Цены и скидки 10/6с; Маркетплейс 300/мин.
    CATEGORY_RATE_LIMITS = {
        'content': (80, 60),
        'discounts': (8, 6),
        'marketplace': (240, 60),
    }
    # Content API additionally enforces roughly 600 ms between requests and
    # permits a burst of five. This shared short-window bucket prevents a
    # scheduler batch from consuming the whole minute bucket at once.
    CONTENT_BURST_RATE_LIMIT = (5, 3)

    # Лимитеры общие для ВСЕХ инстансов клиента с одним токеном (в рамках
    # процесса): параллельные джобы цен/остатков и фото делят один бюджет WB.
    _shared_limiters: Dict[tuple, 'RateLimiter'] = {}
    _shared_limiters_lock = _threading.Lock()

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

        # Rate limiter (грубый пул инстанса) + общие вёдра категорий/методов
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

    @classmethod
    def _shared_limiter(cls, key: tuple, max_requests: int, time_window: int) -> RateLimiter:
        """Лимитер из общего реестра процесса (создаёт при первом обращении)."""
        with cls._shared_limiters_lock:
            limiter = cls._shared_limiters.get(key)
            if limiter is None:
                limiter = RateLimiter(max_requests=max_requests, time_window=time_window)
                cls._shared_limiters[key] = limiter
            return limiter

    def _limiter_for_endpoint(self, endpoint: str):
        """Общий RateLimiter для методов с собственным лимитом WB (или None)."""
        config = self.ENDPOINT_RATE_LIMITS.get(endpoint)
        if config is None:
            return None
        if isinstance(config, tuple):
            limit, window = config
        else:
            limit, window = config, 60
        return self._shared_limiter(
            (self.api_key, 'endpoint', endpoint), limit, window,
        )

    def _limiter_for_category(self, api_type: str):
        """Общий RateLimiter категории API (content/discounts/marketplace) или None."""
        cfg = self.CATEGORY_RATE_LIMITS.get(api_type)
        if not cfg:
            return None
        max_requests, time_window = cfg
        return self._shared_limiter(
            (self.api_key, 'category', api_type), max_requests, time_window)

    def _limiter_for_content_burst(self, api_type: str):
        if api_type != 'content':
            return None
        max_requests, time_window = self.CONTENT_BURST_RATE_LIMIT
        return self._shared_limiter(
            (self.api_key, 'category', 'content_burst'),
            max_requests,
            time_window,
        )

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
        # Rate limiting: пул инстанса → бюджет категории → бюджет метода
        self.rate_limiter.wait_if_needed()
        category_limiter = self._limiter_for_category(api_type)
        if category_limiter is not None:
            category_limiter.wait_if_needed()
        content_burst_limiter = self._limiter_for_content_burst(api_type)
        if content_burst_limiter is not None:
            content_burst_limiter.wait_if_needed()
        endpoint_limiter = self._limiter_for_endpoint(endpoint)
        if endpoint_limiter is not None:
            endpoint_limiter.wait_if_needed()

        # Формирование URL
        base_url = self._get_base_url(api_type)
        url = urljoin(base_url, endpoint)

        # Установка таймаута если не указан
        if 'timeout' not in kwargs:
            kwargs['timeout'] = self.timeout

        # Логирование запроса
        params_str = f" params={kwargs.get('params')}" if kwargs.get('params') else ""
        logger.info(f"WB API Request: {method} {url}{params_str}")
        logger.debug("WB API credential is configured")
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
            if response.status_code in {401, 403}:
                raise WBAuthException("Ошибка авторизации. Проверьте API ключ.")
            elif response.status_code == 429:
                retry_after = None
                try:
                    retry_after = int(
                        response.headers.get('X-Ratelimit-Retry')
                        or response.headers.get('Retry-After')
                        or ''
                    )
                except (TypeError, ValueError):
                    pass
                suffix = f" Повтор через {retry_after}с." if retry_after else ""
                raise WBRateLimitException(
                    f"Превышен лимит запросов к API.{suffix}", retry_after=retry_after)
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
                if response.status_code == 408 or response.status_code >= 500:
                    raise WBTransportUncertainException(
                        error_msg,
                        request_may_have_been_applied=(
                            str(method).upper() not in {'GET', 'HEAD', 'OPTIONS'}
                        ),
                    )
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

            raise WBTransportUncertainException(
                f"Timeout при запросе к API ({self.timeout}s). Попробуйте позже.",
                request_may_have_been_applied=(
                    str(method).upper() not in {'GET', 'HEAD', 'OPTIONS'}
                ),
            )
        except requests.exceptions.SSLError as e:
            logger.error(f"SSL error for {url}: {e}")
            raise WBTransportUncertainException(
                f"Ошибка SSL соединения: {str(e)}. Проверьте сетевое подключение.",
                request_may_have_been_applied=(
                    str(method).upper() not in {'GET', 'HEAD', 'OPTIONS'}
                ),
            )
        except requests.exceptions.ConnectionError as e:
            logger.error(f"Connection error for {url}: {e}")
            error_msg = str(e)
            request_may_have_been_applied = (
                str(method).upper() not in {'GET', 'HEAD', 'OPTIONS'}
            )
            if "Name or service not known" in error_msg or "getaddrinfo failed" in error_msg:
                message = "Не удалось разрешить имя хоста API Wildberries. Проверьте интернет-соединение."
                request_may_have_been_applied = False
            elif "Connection refused" in error_msg:
                message = "Подключение отклонено сервером API Wildberries. Проверьте URL и доступность API."
                request_may_have_been_applied = False
            else:
                message = f"Ошибка соединения с API Wildberries: {error_msg}"
            raise WBTransportUncertainException(
                message,
                request_may_have_been_applied=request_may_have_been_applied,
            )
        except (WBAuthException, WBRateLimitException, WBAPIException):
            raise
        except Exception as e:
            logger.exception(f"Unexpected error for {url}: {e}")
            raise WBTransportUncertainException(
                f"Неожиданная ошибка: {str(e)}",
                request_may_have_been_applied=(
                    str(method).upper() not in {'GET', 'HEAD', 'OPTIONS'}
                ),
            )

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
            except WBTransportUncertainException:
                # Reference freshness failed at transport level. Do not hide
                # it as a merely missing size snapshot.
                raise
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
        validate: bool = True,
        snapshot_context: Optional[Dict[str, Any]] = None,
        before_send_callback: Optional[
            Callable[[Dict[str, Dict[str, Any]]], None]
        ] = None,
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
        from services.wb_validators import _mark_wb_card_as_fetched

        updates = dict(updates or {})
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
                _mark_wb_card_as_fetched(full_card)
            except Exception as e:
                logger.error(f"❌ Failed to fetch full card for merging: {str(e)}")
                raise WBAPIException(
                    f"Нельзя безопасно обновить nmID={nm_id}: "
                    "не удалось получить полную текущую карточку WB"
                ) from e
            else:
                # Supplier/AI characteristics обязаны пройти строгую проверку
                # по category schema + dictionaries, синхронизированным в админке.
                if 'characteristics' in updates:
                    from services.marketplace_validator import (
                        build_wb_characteristic_patch,
                    )
                    updates['characteristics'] = build_wb_characteristic_patch(
                        full_card.get('subjectID'), updates['characteristics'])
                    updates['characteristics'] = clean_characteristics_for_update(
                        updates['characteristics'])

                # Подготавливаем полную карточку; characteristic patch мержится по id.
                card_to_send = prepare_card_for_update(full_card, updates)
        else:
            raise WBAPIException(
                'Обновление карточки без merge_with_existing запрещено: '
                'Content API принимает full replacement и может стереть поля'
            )

        # prepare_card_for_update переносит внутренний category/patch context
        # до batch-boundary. Одиночный update уже прошёл проверку выше, поэтому
        # служебные поля нужно удалить до wire payload.
        from services.wb_validators import (
            WB_SUBJECT_CONTEXT_KEY,
            WB_CHARACTERISTICS_CHANGED_KEY,
            WB_PREPARED_CONTEXT_KEY,
        )
        card_to_send.pop(WB_SUBJECT_CONTEXT_KEY, None)
        card_to_send.pop(WB_CHARACTERISTICS_CHANGED_KEY, None)
        card_to_send.pop(WB_PREPARED_CONTEXT_KEY, None)

        # Content API performs a full replacement. Even when only title or
        # description changes, an invalid dictionary-bound value already
        # present in the fresh card would otherwise be sent back to WB.
        from services.marketplace_validator import (
            WBCharacteristicValidationError,
            validate_wb_full_card_dictionary_values,
        )
        full_validation = validate_wb_full_card_dictionary_values(
            full_card.get('subjectID'),
            card_to_send.get('characteristics'),
        )
        if not full_validation['valid']:
            raise WBCharacteristicValidationError(full_validation)

        # Валидация данных перед отправкой
        if validate:
            if not validate_and_log_errors(card_to_send, operation="update"):
                raise WBAPIException(f"Validation failed for card nmID={nm_id}")

        # WB Content API v2 эндпоинт для обновления
        endpoint = "/content/v2/cards/update"

        # A caller that needs rollback guarantees may durably persist this
        # exact fresh-before/sent-after pair before the external side effect.
        # The callback receives its own deep copy and cannot mutate wire data.
        before_snapshot = copy.deepcopy(full_card)
        from services.wb_validators import WB_SOURCE_CONTEXT_KEY
        before_snapshot.pop(WB_SOURCE_CONTEXT_KEY, None)
        exact_snapshot = {
            'before': before_snapshot,
            'after': copy.deepcopy(card_to_send),
        }
        if snapshot_context is not None:
            snapshot_context.clear()
            snapshot_context.update(copy.deepcopy(exact_snapshot))
        if before_send_callback is not None:
            before_send_callback(copy.deepcopy(exact_snapshot))

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
            if isinstance(result, dict) and result.get('error'):
                details = result.get('errorText') or result.get(
                    'additionalErrors') or 'WB отклонил обновление карточки'
                raise WBAPIException(str(details))
            logger.info(f"✅ Card nmID={nm_id} update response: {result}")
            return result
        except WBAPIException as e:
            logger.error(f"❌ WB API error updating card nmID={nm_id}: {str(e)}")
            logger.error(f"Sent data structure: {list(card_to_send.keys())}")
            raise
        except Exception as e:
            logger.error(f"❌ Unexpected error updating card nmID={nm_id}: {str(e)}")
            raise

    def fetch_cards_by_nm_ids(
        self,
        nm_ids: List[int],
        log_to_db: bool = False,
        seller_id: int = None,
        sweep_threshold: int = 100
    ) -> Dict[int, Dict[str, Any]]:
        """
        Получить полные карточки по списку nmID: {nm_id: card}.

        Адаптивно: до sweep_threshold — точечные запросы (1 запрос на карточку);
        больше — курсорный обход всего каталога (100 карточек за запрос),
        что при сотнях целей дешевле по бюджету Контент-API.
        """
        targets = {int(x) for x in nm_ids if x}
        found: Dict[int, Dict[str, Any]] = {}
        if not targets:
            return found

        if len(targets) <= sweep_threshold:
            for nm in targets:
                card = self.get_card_by_nm_id(nm, log_to_db=log_to_db, seller_id=seller_id)
                if card:
                    found[nm] = card
            from services.wb_validators import _mark_wb_card_as_fetched
            return {
                nm: _mark_wb_card_as_fetched(card)
                for nm, card in found.items()
            }

        cursor_updated_at = None
        cursor_nm_id = None
        prev_cursor = None
        while True:
            resp = self.get_cards_list(
                limit=100,
                cursor_updated_at=cursor_updated_at,
                cursor_nm_id=cursor_nm_id,
                log_to_db=log_to_db,
                seller_id=seller_id,
            )
            cards = resp.get('cards', []) or []
            for card in cards:
                nm = card.get('nmID')
                if nm in targets and nm not in found:
                    found[nm] = card
            cursor = resp.get('cursor', {}) or {}
            if len(found) == len(targets) or not cards or cursor.get('total', 0) < 100:
                break
            cursor_updated_at = cursor.get('updatedAt')
            cursor_nm_id = cursor.get('nmID')
            if not cursor_updated_at or not cursor_nm_id:
                break
            # Защита от зацикливания: WB вернул тот же курсор, что и раньше
            if (cursor_updated_at, cursor_nm_id) == prev_cursor:
                logger.warning("fetch_cards_by_nm_ids: cursor stalled, aborting sweep")
                break
            prev_cursor = (cursor_updated_at, cursor_nm_id)
        from services.wb_validators import _mark_wb_card_as_fetched
        return {
            nm: _mark_wb_card_as_fetched(card)
            for nm, card in found.items()
        }

    def update_cards_merged(
        self,
        nm_updates: Dict[int, Dict[str, Any]],
        log_to_db: bool = False,
        seller_id: int = None,
        chunk_size: int = 1000
    ) -> Dict[str, Any]:
        """
        Пакетное редактирование: слить updates с полными карточками и отправить
        чанками через cards/update (до 3000 карточек и 10 МБ на запрос у WB;
        держим ≤chunk_size и ≤8 МБ). Экономит бюджет 8 запросов/мин: сотни
        карточек уходят за 1-2 запроса вместо запроса на карточку.

        Args:
            nm_updates: {nm_id: {обновляемые поля как в update_card}}

        Returns:
            {'sent': [nm...], 'missing': [nm без карточки в WB],
             'invalid': {nm: 'ошибка валидации'},
             'failed': {nm: 'ошибка отправки'}, 'requests': N}

        Ошибка чанка НЕ роняет вызов и НЕ теряет прогресс: чанк бисектится
        до одиночных карточек, виновники попадают в 'failed', остальные
        отправляются (одна плохая карточка из 1000 ≈ +2*log2(1000) запросов).
        """
        import json as _json
        from services.wb_validators import (
            prepare_card_for_update, validate_card_update,
            clean_characteristics_for_update, WBValidationError,
            WB_PREPARED_CONTEXT_KEY, WB_SOURCE_CONTEXT_KEY,
        )
        from services.marketplace_validator import (
            WBCharacteristicValidationError,
            build_wb_characteristic_patch,
            validate_wb_full_card_dictionary_values,
        )

        result = {
            'sent': [], 'missing': [], 'invalid': {}, 'failed': {},
            'requests': 0, 'snapshots': {},
        }
        nm_updates = {int(k): v for k, v in (nm_updates or {}).items() if v}
        if not nm_updates:
            return result

        cards_map = self.fetch_cards_by_nm_ids(
            list(nm_updates), log_to_db=log_to_db, seller_id=seller_id)

        prepared: List[Dict[str, Any]] = []
        characteristic_validation_cache = {}
        for nm, updates in nm_updates.items():
            full_card = cards_map.get(nm)
            if not full_card:
                result['missing'].append(nm)
                continue
            upd = dict(updates)
            try:
                if 'characteristics' in upd:
                    upd['characteristics'] = build_wb_characteristic_patch(
                        full_card.get('subjectID'),
                        upd['characteristics'],
                        validation_cache=characteristic_validation_cache,
                    )
                    upd['characteristics'] = clean_characteristics_for_update(
                        upd['characteristics'])
                merged = prepare_card_for_update(full_card, upd)
            except (WBCharacteristicValidationError, WBValidationError) as exc:
                result['invalid'][nm] = str(exc)
                logger.warning(
                    f"Card nmID={nm} failed WB dictionary validation: {exc}")
                continue
            is_valid, errors = validate_card_update(merged)
            if not is_valid:
                result['invalid'][nm] = '; '.join(errors)
                logger.warning(f"Card nmID={nm} failed validation: {result['invalid'][nm]}")
                continue
            prepared.append(merged)

        def _send_chunk(chunk: List[Dict[str, Any]]):
            """Отправка чанка; при ошибке — бисекция, чтобы изолировать виновников."""
            if not chunk:
                return
            try:
                resp = self.update_cards_batch(
                    chunk, log_to_db=log_to_db, seller_id=seller_id, validate=False)
                # WB может ответить HTTP 200 с error:true в теле — это отказ
                if isinstance(resp, dict) and resp.get('error'):
                    raise WBAPIException(
                        str(resp.get('errorText') or 'WB вернул ошибку в теле ответа'))
                result['requests'] += 1
                for card in chunk:
                    nm_id = card['nmID']
                    result['sent'].append(nm_id)
                    after = copy.deepcopy(card)
                    after.pop(WB_PREPARED_CONTEXT_KEY, None)
                    before = copy.deepcopy(cards_map[nm_id])
                    before.pop(WB_SOURCE_CONTEXT_KEY, None)
                    result['snapshots'][nm_id] = {
                        'before': before,
                        'after': after,
                    }
            except (
                WBTransportUncertainException,
                WBAuthException,
                WBRateLimitException,
            ):
                # A timeout/5xx during a write cannot be bisected safely: WB
                # may already be applying the original chunk. Auth/rate-limit
                # errors are batch-wide and immediate bisection only creates a
                # retry storm.
                raise
            except Exception as e:
                result['requests'] += 1
                if len(chunk) == 1:
                    result['failed'][chunk[0]['nmID']] = str(e)
                    logger.error(f"Card nmID={chunk[0]['nmID']} rejected by WB: {e}")
                    return
                mid = len(chunk) // 2
                logger.warning(
                    f"Chunk of {len(chunk)} cards rejected ({e}); bisecting to isolate")
                _send_chunk(chunk[:mid])
                _send_chunk(chunk[mid:])

        # Чанки по количеству и по объёму (~8 МБ, лимит WB — 10 МБ).
        # Размер считаем в wire-формате: requests сериализует JSON с
        # ensure_ascii=True, где кириллица занимает ~6 байт (\uXXXX).
        max_bytes = 8 * 1024 * 1024
        batch: List[Dict[str, Any]] = []
        batch_bytes = 0
        for card in prepared:
            size_card = {
                key: value for key, value in card.items()
                if key != WB_PREPARED_CONTEXT_KEY
            }
            size = len(_json.dumps(size_card))
            if batch and (len(batch) >= chunk_size or batch_bytes + size > max_bytes):
                _send_chunk(batch)
                batch, batch_bytes = [], 0
            batch.append(card)
            batch_bytes += size
        _send_chunk(batch)

        logger.info(
            f"📦 Merged batch update: sent={len(result['sent'])} "
            f"missing={len(result['missing'])} invalid={len(result['invalid'])} "
            f"failed={len(result['failed'])} requests={result['requests']}"
        )
        return result

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

        # Dictionary validation выполняется всегда, независимо от флага
        # validate (он управляет только общей shape-валидацией). Batch endpoint
        # принимает только карточки с opaque context из prepare_card_for_update:
        # subjectID из произвольного payload нельзя считать доказательством
        # фактической категории nmID.
        from services.marketplace_validator import (
            WBCharacteristicValidationError,
            build_wb_characteristic_patch,
            merge_wb_characteristics,
            validate_wb_full_card_dictionary_values,
        )
        from services.wb_validators import (
            WB_SUBJECT_CONTEXT_KEY,
            WB_CHARACTERISTICS_CHANGED_KEY,
            WB_PREPARED_CONTEXT_KEY,
            WBValidationError,
            _read_wb_prepared_context,
            clean_characteristics_for_update,
        )
        characteristic_validation_cache = {}
        guarded_cards = []
        no_characteristic_patch = object()
        for index, raw_card in enumerate(cards):
            if not isinstance(raw_card, dict):
                raise WBAPIException(f'Card #{index + 1} must be an object')
            card = dict(raw_card)
            if (
                WB_SUBJECT_CONTEXT_KEY in card
                or WB_CHARACTERISTICS_CHANGED_KEY in card
            ):
                raise WBAPIException(
                    f'Card #{index + 1}: legacy internal context marker is forbidden')

            opaque_context_present = WB_PREPARED_CONTEXT_KEY in card
            opaque_context = card.pop(WB_PREPARED_CONTEXT_KEY, None)
            if not opaque_context_present:
                raise WBAPIException(
                    f'Card #{index + 1}: raw batch payload is forbidden; '
                    'use safe full-card preparation')
            try:
                (
                    internal_subject_id,
                    changed_ids,
                    removed_ids,
                ) = _read_wb_prepared_context(
                    opaque_context,
                    card.get('characteristics'),
                    card.get('nmID'),
                )
            except WBValidationError as exc:
                raise WBAPIException(
                    f'Card #{index + 1}: invalid internal context') from exc
            wire_subject_id = card.pop('subjectID', None)
            if (
                wire_subject_id is not None
                and str(wire_subject_id) != str(internal_subject_id)
            ):
                raise WBAPIException(
                    f'Card #{index + 1}: subjectID conflicts with safe context')
            subject_id = internal_subject_id

            full_validation = validate_wb_full_card_dictionary_values(
                subject_id,
                card.get('characteristics'),
                validation_cache=characteristic_validation_cache,
            )
            if not full_validation['valid']:
                raise WBCharacteristicValidationError(full_validation)

            if 'characteristics' not in card:
                raise WBAPIException(
                    f'Card #{index + 1}: full update payload must contain '
                    'characteristics to prevent accidental erase')

            if 'characteristics' in card:
                chars = card.get('characteristics')
                if chars is None:
                    proposed_patch = chars
                elif chars in ([], {}):
                    changed_set = set(changed_ids)
                    removed_set = set(removed_ids)
                    if (
                        not opaque_context_present
                        or changed_set != removed_set
                    ):
                        raise WBAPIException(
                            f'Card #{index + 1}: empty characteristics are allowed '
                            'only for a safely prepared unchanged/removal card')
                    if removed_set:
                        from services.marketplace_validator import (
                            require_wb_characteristic_ids,
                        )
                        require_wb_characteristic_ids(
                            subject_id,
                            removed_set,
                            validation_cache=characteristic_validation_cache,
                        )
                    proposed_patch = no_characteristic_patch
                elif opaque_context_present:
                    changed_set = set(changed_ids)
                    removed_set = set(removed_ids)
                    if not removed_set.issubset(changed_set):
                        raise WBAPIException(
                            f'Card #{index + 1}: invalid removed characteristic context')
                    proposed_patch = []
                    found_ids = set()
                    for item in chars if isinstance(chars, list) else []:
                        if not isinstance(item, dict):
                            continue
                        try:
                            charc_id = int(item.get('id'))
                        except (TypeError, ValueError):
                            continue
                        if charc_id in changed_set:
                            proposed_patch.append(item)
                            found_ids.add(charc_id)
                    if found_ids != changed_set - removed_set:
                        raise WBAPIException(
                            f'Card #{index + 1}: characteristic patch was lost during merge')
                    if removed_set:
                        from services.marketplace_validator import (
                            require_wb_characteristic_ids,
                        )
                        require_wb_characteristic_ids(
                            subject_id,
                            removed_set,
                            validation_cache=characteristic_validation_cache,
                        )
                    if not changed_set:
                        proposed_patch = no_characteristic_patch
                else:
                    # Raw public payload has no trustworthy changed-ID context.
                    proposed_patch = chars

                if proposed_patch is not no_characteristic_patch and subject_id is None:
                    raise WBAPIException(
                        f'Card #{index + 1}: subjectID is required for mandatory '
                        'WB admin-dictionary validation')
                if proposed_patch is not no_characteristic_patch:
                    normalized_patch = build_wb_characteristic_patch(
                        subject_id,
                        proposed_patch,
                        validation_cache=characteristic_validation_cache,
                    )
                    card['characteristics'] = merge_wb_characteristics(
                        card.get('characteristics'), normalized_patch)
            guarded_cards.append(card)

        from services.wb_content_payload import normalize_update_card_payload
        cards = [normalize_update_card_payload(card) for card in guarded_cards]
        for card in cards:
            if 'characteristics' in card:
                if not isinstance(card['characteristics'], list):
                    raise WBAPIException("Field 'characteristics' must be an array")
                card['characteristics'] = clean_characteristics_for_update(card['characteristics'])

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
            if isinstance(result, dict) and result.get('error'):
                details = result.get('errorText') or result.get(
                    'additionalErrors') or 'WB отклонил batch-обновление карточек'
                raise WBAPIException(str(details))
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
        if len(photo_paths) > MAX_WB_MEDIA_FILES:
            logger.warning(
                f"WB media limit: trimming photos from {len(photo_paths)} to {MAX_WB_MEDIA_FILES}"
            )
            photo_paths = photo_paths[:MAX_WB_MEDIA_FILES]
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
        if len(photo_urls) > MAX_WB_MEDIA_FILES:
            logger.warning(
                f"WB media limit: trimming photo URLs from {len(photo_urls)} to {MAX_WB_MEDIA_FILES}"
            )
            photo_urls = photo_urls[:MAX_WB_MEDIA_FILES]

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
            - Цена должна быть целым числом в валюте магазина
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
            if price is None or not isinstance(price, (int, float)) or price <= 0:
                logger.warning(f"⚠️ Skipping nmID {nm_id}: price={price} (must be > 0)")
                invalid_count += 1
                continue
            if isinstance(price, float) and not price.is_integer():
                logger.warning(f"⚠️ Skipping nmID {nm_id}: price={price} (must be a whole number)")
                invalid_count += 1
                continue

            item = {
                'nmID': nm_id,
                'price': int(price),
            }

            discount = p.get('discount')
            if discount is not None:
                if (
                    not isinstance(discount, (int, float))
                    or isinstance(discount, float) and not discount.is_integer()
                    or discount < 0
                    or discount > 99
                ):
                    logger.warning(f"⚠️ Skipping nmID {nm_id}: discount={discount} (must be 0-99)")
                    invalid_count += 1
                    continue
                item['discount'] = int(discount)

            valid_prices.append(item)

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
        try:
            result = self.get_cards_errors_list(log_to_db=log_to_db, seller_id=seller_id)
            data = result.get('data')
            if isinstance(data, dict):
                errors = data.get('items', []) or []
            else:
                errors = data or []
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
        except WBTransportUncertainException:
            # A failed read means "unknown", not "card is missing". Let the
            # caller distinguish a safe pre-write failure from WB absence.
            raise
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

    def get_directory_tnved(self, subject_id: int, locale: str = 'ru') -> Dict[str, Any]:
        """Получить справочник кодов ТНВЭД для конкретного предмета WB."""
        if not subject_id:
            raise ValueError('subject_id is required for the TNVED directory')
        endpoint = "/content/v2/directory/tnved"
        params = {'subjectID': int(subject_id)}
        if locale:
            params['locale'] = locale

        logger.info(f"📋 Getting TNVED codes directory (subjectID={subject_id}, locale={locale})")
        try:
            response = self._make_request('GET', 'content', endpoint, params=params)
            result = response.json()
            logger.info(f"✅ TNVED codes loaded: {len(result.get('data', []))} items")
            return result
        except Exception as e:
            logger.error(f"❌ Failed to get TNVED codes: {str(e)}")
            raise

    def get_brands_by_subject(self, subject_id: int, top: int = 5000) -> Dict[str, Any]:
        """Получить полный bounded snapshot брендов одной категории."""
        result = self.fetch_all_brands([subject_id], top=top)
        return {
            **result,
            'brands': result.get('data', []),
        }

    def get_brands_by_subject_quick(self, subject_id: int, pattern: str = 'а',
                                     top: int = 5000) -> Dict[str, Any]:
        """
        Первый cursor page брендов одной категории (один запрос).

        Args:
            subject_id: ID предмета (обязателен)
            pattern: legacy argument, игнорируется
            top: legacy argument, игнорируется
        """
        endpoint = "/api/content/v1/brands"
        params = {'subjectId': subject_id}
        response = self._make_request('GET', 'content', endpoint, params=params)
        return response.json()

    def fetch_all_brands(self, subject_ids: list, top: int = 5000,
                         progress_callback=None, max_requests: int = 200,
                         max_pages_per_subject: int = 200) -> Dict[str, Any]:
        """
        Получить бренды из WB по списку категорий через cursor pagination.

        Args:
            subject_ids: список ID предметов (subjectID)
            top: legacy argument, больше не отправляется в актуальный WB API
            progress_callback: callable(done, total, brands_so_far)
            max_requests: hard budget на весь sweep
            max_pages_per_subject: защита от бесконечной пагинации
        """
        endpoint = "/api/content/v1/brands"
        all_brands = {}  # id -> brand_data from complete subject snapshots
        subject_brands = {}  # subject_id -> complete list[brand_data]
        self._fetch_debug = None
        fetch_errors = []
        fetch_warnings = []
        request_count = 0
        completed_subjects = 0
        normalized_subject_ids = []
        seen_subject_ids = set()
        for raw_subject_id in subject_ids or []:
            try:
                subject_id = int(raw_subject_id)
            except (TypeError, ValueError):
                continue
            if subject_id <= 0 or subject_id in seen_subject_ids:
                continue
            normalized_subject_ids.append(subject_id)
            seen_subject_ids.add(subject_id)
        total = len(normalized_subject_ids)

        stop_sweep = False
        for subject_id in normalized_subject_ids:
            cursor = None
            seen_cursors = set()
            subject_brand_map = {}
            subject_seen_brand_ids = set()
            subject_items_seen = 0
            excluded_empty_names = 0
            expected_total = None
            subject_complete = False

            for page_index in range(max(1, int(max_pages_per_subject))):
                if request_count >= max(1, int(max_requests)):
                    fetch_errors.append({
                        'subject_id': subject_id,
                        'code': 'request_budget_exhausted',
                        'error': f'Brand sweep exceeded {max_requests} requests',
                    })
                    stop_sweep = True
                    break

                params = {'subjectId': subject_id}
                if cursor is not None:
                    params['next'] = cursor

                request_count += 1
                try:
                    response = self._make_request(
                        'GET', 'content', endpoint, params=params,
                    )
                    if not self._fetch_debug:
                        self._fetch_debug = {
                            'url': response.url,
                            'status': response.status_code,
                            'raw_body': response.text[:500],
                        }

                    result = response.json()
                    brands = result.get('brands') if isinstance(result, dict) else None
                    if not isinstance(brands, list):
                        raise ValueError('WB brand page has no brands list')
                    try:
                        page_total = int(result.get('total'))
                    except (TypeError, ValueError):
                        raise ValueError('WB brand page has no valid total')
                    if page_total < 0:
                        raise ValueError('WB brand page returned negative total')
                    if expected_total is None:
                        expected_total = page_total
                    elif page_total != expected_total:
                        raise ValueError('WB brand total changed during pagination')
                    for brand in brands:
                        if not isinstance(brand, dict):
                            raise ValueError('WB brand page contains a non-object item')
                        brand_id = brand.get('id')
                        if (
                            not isinstance(brand_id, int)
                            or isinstance(brand_id, bool)
                            or brand_id <= 0
                        ):
                            raise ValueError('WB brand page contains an invalid brand id')
                        if brand_id in subject_seen_brand_ids:
                            raise ValueError('WB brand page contains a duplicate brand id')
                        subject_seen_brand_ids.add(brand_id)
                        subject_items_seen += 1
                        raw_name = brand.get('name')
                        name = raw_name.strip() if isinstance(raw_name, str) else ''
                        if not name:
                            excluded_empty_names += 1
                            continue
                        normalized_brand = dict(brand)
                        normalized_brand['id'] = brand_id
                        normalized_brand['name'] = name
                        subject_brand_map[brand_id] = normalized_brand

                    next_cursor = result.get('next')
                    if next_cursor in (None, 0, '0', ''):
                        if subject_items_seen != expected_total:
                            raise ValueError(
                                'WB brand pagination ended before declared total'
                            )
                        if excluded_empty_names:
                            fetch_warnings.append({
                                'subject_id': subject_id,
                                'code': 'brands_excluded_invalid_name',
                                'count': excluded_empty_names,
                            })
                            logger.warning(
                                'Excluded %s WB brands without a usable name '
                                'for subjectId=%s',
                                excluded_empty_names, subject_id,
                            )
                        subject_complete = True
                        break
                    try:
                        next_cursor = int(next_cursor)
                    except (TypeError, ValueError):
                        raise ValueError('WB brand page returned invalid next cursor')
                    if next_cursor in seen_cursors or next_cursor == cursor:
                        raise ValueError('WB brand pagination repeated cursor')
                    if not brands:
                        raise ValueError('WB brand page is empty before pagination end')
                    seen_cursors.add(next_cursor)
                    cursor = next_cursor
                except Exception as error:
                    if not self._fetch_debug:
                        self._fetch_debug = {
                            'error': f'{type(error).__name__}: {str(error)[:300]}',
                            'subjectId': subject_id,
                            'next': cursor,
                        }
                    logger.warning(
                        'Failed brand page subjectId=%s next=%s: %s',
                        subject_id, cursor, error,
                    )
                    fetch_errors.append({
                        'subject_id': subject_id,
                        'next': cursor,
                        'code': 'page_error',
                        'error': str(error)[:300],
                    })
                    stop_sweep = True
                    break
            else:
                fetch_errors.append({
                    'subject_id': subject_id,
                    'code': 'page_budget_exhausted',
                    'error': (
                        f'Brand pagination exceeded {max_pages_per_subject} pages'
                    ),
                })
                stop_sweep = True

            if subject_complete:
                completed_subjects += 1
                subject_snapshot = list(subject_brand_map.values())
                subject_brands[subject_id] = subject_snapshot
                for brand in subject_snapshot:
                    all_brands[brand['id']] = brand
            if progress_callback:
                progress_callback(
                    completed_subjects, total, len(all_brands),
                )
            if stop_sweep or not subject_complete:
                break

        brands_list = list(all_brands.values())
        logger.info(
            'Fetched %s unique brands from %s/%s categories in %s requests',
            len(brands_list), completed_subjects, total, request_count,
        )
        return {
            'data': brands_list,
            'subject_brands': subject_brands,
            'completed_subject_ids': list(subject_brands),
            'next_subject_id': (
                normalized_subject_ids[completed_subjects]
                if completed_subjects < total else None
            ),
            'complete': (
                total > 0
                and completed_subjects == total
                and not fetch_errors
            ),
            'errors': fetch_errors[:100],
            'warnings': fetch_warnings[:100],
            'requests': request_count,
            'categories_completed': completed_subjects,
            'categories_total': total,
        }

    def search_brands(self, pattern: str, top: int = 50,
                      subject_id: int = None) -> Dict[str, Any]:
        """
        Bounded поиск по полному cursor snapshot конкретной категории.

        Args:
            pattern: Строка поиска (часть названия бренда)
            top: Максимальное количество результатов
            subject_id: ID категории WB, обязателен

        Returns:
            Dict с данными о брендах: {"data": [...]}
        """
        if not pattern or not pattern.strip():
            return {'data': [], 'complete': True}
        if not subject_id:
            raise ValueError('subject_id is required for WB brand search')

        snapshot = self.get_brands_by_subject(subject_id)
        needle = pattern.strip().casefold()
        matches = [
            brand for brand in snapshot.get('data', [])
            if needle in str(brand.get('name') or '').casefold()
        ][:max(1, min(int(top), 200))]
        return {
            'data': matches,
            'complete': snapshot.get('complete') is True,
            'errors': snapshot.get('errors') or [],
        }

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

        if not subject_id:
            return {
                'valid': False,
                'exact_match': None,
                'suggestions': [],
                'complete': False,
                'error': 'category_scope_required',
            }

        try:
            snapshot = self.get_brands_by_subject(subject_id)
            all_brands = snapshot.get('data', [])
            brand_lower = brand_name.casefold().strip()
            brand_normalized = ''.join(
                char.casefold() for char in brand_name if char.isalnum()
            )
            exact_match = None
            suggestions = []

            for brand in all_brands:
                brand_wb_name = str(brand.get('name') or '')
                wb_name_lower = brand_wb_name.casefold().strip()
                wb_name_normalized = ''.join(
                    char.casefold() for char in brand_wb_name if char.isalnum()
                )

                if (
                    wb_name_lower == brand_lower
                    or wb_name_normalized == brand_normalized
                ):
                    exact_match = brand
                    break
                if (
                    brand_normalized
                    and (
                        brand_normalized in wb_name_normalized
                        or wb_name_normalized in brand_normalized
                    )
                ):
                    suggestions.append(brand)

            is_valid = exact_match is not None
            response = {
                'valid': is_valid,
                'exact_match': exact_match,
                'suggestions': suggestions[:15],
                'complete': snapshot.get('complete') is True,
            }
            if not is_valid and snapshot.get('complete') is not True:
                response['error'] = 'incomplete_brand_snapshot'
            return response
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
        if len(variants) > 30:
            raise WBAPIException(
                f"Too many variants ({len(variants)}). Max 30 variants per imtID."
            )

        endpoint = "/content/v2/cards/upload"

        # Формируем тело запроса согласно спецификации WB API
        from services.marketplace_validator import build_wb_create_characteristics
        characteristic_validation_cache = {}
        validated_variants = []
        for variant in variants:
            validated_variant = dict(variant)
            validated_variant['characteristics'] = (
                build_wb_create_characteristics(
                    subject_id,
                    validated_variant.get('characteristics', []),
                    validation_cache=characteristic_validation_cache,
                )
            )
            validated_variants.append(validated_variant)
        request_body = [{
            'subjectID': subject_id,
            'variants': validated_variants,
        }]

        from services.wb_validators import prepare_create_cards_for_wb
        request_body = prepare_create_cards_for_wb(request_body)

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

    def create_product_cards_batch(
        self,
        cards: List[Dict[str, Any]],
        log_to_db: bool = True,
        seller_id: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Batch-создание нескольких карточек за 1 API-вызов.

        WB API: POST /content/v2/cards/upload
        Лимит: 10 req/min, до 100 карточек (imtID) за запрос.

        Args:
            cards: Список [{subjectID: int, variants: [{...}]}]
                   Каждый элемент — отдельная карточка (imtID).
                   Разные subjectID в одном запросе — OK.
            log_to_db: Логировать запрос
            seller_id: ID продавца

        Returns:
            Ответ API WB (создание асинхронное, 200 = принято в очередь)
        """
        if len(cards) > 100:
            raise WBAPIException(
                f"Too many cards ({len(cards)}). Max 100 per request."
            )

        for idx, card in enumerate(cards):
            variants = card.get('variants') if isinstance(card, dict) else None
            if isinstance(variants, list) and len(variants) > 30:
                raise WBAPIException(
                    f"Too many variants in card[{idx}] ({len(variants)}). "
                    f"Max 30 variants per imtID."
                )

        endpoint = "/content/v2/cards/upload"

        # Центральный safety boundary для всех create-paths, включая ручной
        # UI и supplier batch: характеристики проверяются до HTTP-вызова.
        from services.marketplace_validator import build_wb_create_characteristics
        characteristic_validation_cache = {}
        validated_cards = []
        for card in cards:
            validated_card = dict(card)
            subject_id = validated_card.get('subjectID')
            validated_variants = []
            for variant in validated_card.get('variants') or []:
                validated_variant = dict(variant)
                validated_variant['characteristics'] = (
                    build_wb_create_characteristics(
                        subject_id,
                        validated_variant.get('characteristics', []),
                        validation_cache=characteristic_validation_cache,
                    )
                )
                validated_variants.append(validated_variant)
            validated_card['variants'] = validated_variants
            validated_cards.append(validated_card)

        logger.info(f"📤 Batch creating {len(cards)} product cards")

        from services.wb_validators import prepare_create_cards_for_wb
        cards = prepare_create_cards_for_wb(validated_cards)

        try:
            start_time = time.time()
            response = self._make_request(
                'POST', 'content', endpoint,
                json=cards,
                log_to_db=log_to_db,
                seller_id=seller_id
            )
            elapsed = time.time() - start_time
            result = response.json()

            if result.get('error'):
                error_text = result.get('errorText', 'Unknown error')
                logger.error(f"❌ Batch card creation failed: {error_text}")
                raise WBAPIException(f"Batch create failed: {error_text}")

            logger.info(f"✅ Batch card creation accepted in {elapsed:.2f}s")
            return result

        except WBAPIException:
            raise
        except Exception as e:
            logger.error(f"❌ Batch card creation error: {e}")
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
        body = {
            "cursor": {
                "limit": 100
            },
            "order": {
                "ascending": False
            }
        }

        logger.info("Getting cards errors list")

        try:
            response = self._make_request(
                'POST',
                'content',
                endpoint,
                json=body,
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
        use_cache: bool = True,
        log_to_db: bool = False,
        seller_id: int = None
    ) -> Dict[str, Any]:
        """Получить карточки с кэшированием (поддержка cursor-based пагинации)"""
        if not use_cache:
            return super().get_cards_list(
                limit=limit,
                offset=offset,
                filter_nm_id=filter_nm_id,
                cursor_updated_at=cursor_updated_at,
                cursor_nm_id=cursor_nm_id,
                log_to_db=log_to_db,
                seller_id=seller_id
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
