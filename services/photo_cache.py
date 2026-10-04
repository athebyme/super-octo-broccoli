# -*- coding: utf-8 -*-
"""
Система кэширования фото по поставщикам

Фото скачиваются в фоновом режиме и сохраняются на диск.
Разные продавцы, импортирующие товары одного поставщика, используют общий кэш.

Структура хранения:
    data/photo_cache/{supplier_type}/{external_id}/{photo_hash}.jpg
"""

import os
import hashlib
import threading
import queue
import logging
import time
import shutil
import copy
from dataclasses import dataclass
from typing import Callable, Optional, Dict, List, Tuple
from io import BytesIO
from urllib.parse import urljoin, urlsplit
import requests
from requests.cookies import RequestsCookieJar
from PIL import Image

logger = logging.getLogger(__name__)


# ============================================================================
# КОНФИГУРАЦИЯ
# ============================================================================

def _bounded_env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    """Читает целочисленный runtime-бюджет и удерживает его в safe range."""
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


# Базовая директория для кэша фото
PHOTO_CACHE_DIR = os.environ.get('PHOTO_CACHE_DIR', 'data/photo_cache')

# Фоновый photo transport намеренно мал: он не конкурирует с web request pool
# за внешние соединения и не создаёт тысячный хвост после открытия каталога.
MAX_DOWNLOAD_QUEUE_SIZE = _bounded_env_int(
    'PHOTO_DOWNLOAD_QUEUE_SIZE', 256, 32, 2000,
)
NUM_DOWNLOAD_WORKERS = _bounded_env_int(
    'PHOTO_DOWNLOAD_WORKERS', 2, 1, 4,
)
DOWNLOAD_TOTAL_BUDGET = _bounded_env_int(
    'PHOTO_DOWNLOAD_TOTAL_SECONDS', 12, 3, 30,
)
DOWNLOAD_CONNECT_TIMEOUT = 3
DOWNLOAD_READ_TIMEOUT = 5
DOWNLOAD_MAX_BYTES = 10 * 1024 * 1024
DOWNLOAD_MAX_PIXELS = 50_000_000

_SUPPLIER_AUTH_HOST = 'sexoptovik.ru'
_SUPPLIER_AUTH_REFERER = 'https://sexoptovik.ru/admin/'


def _is_supplier_auth_origin(url: str) -> bool:
    """Whether a URL is the exact HTTPS origin that issued supplier auth."""
    try:
        parsed = urlsplit(url)
        return (
            parsed.scheme.lower() == 'https'
            and (parsed.hostname or '').lower() == _SUPPLIER_AUTH_HOST
            and parsed.port in (None, 443)
            and parsed.username is None
            and parsed.password is None
        )
    except (TypeError, ValueError):
        return False


def _is_supplier_auth_cookie(cookie) -> bool:
    """Whether a cookie belongs to the supplier host or its subdomains."""
    try:
        domain = (cookie.domain or '').lower().lstrip('.').rstrip('.')
    except (AttributeError, TypeError):
        return False
    return (
        domain == _SUPPLIER_AUTH_HOST
        or domain.endswith('.' + _SUPPLIER_AUTH_HOST)
    )


def _supplier_cookie_snapshot(session):
    """Return a validated Session cookie snapshot, or None on invalid jars."""
    cookie_jar = getattr(session, 'cookies', None)
    if not isinstance(cookie_jar, RequestsCookieJar):
        return None
    try:
        cookies = tuple(cookie_jar)
    except Exception:
        return None
    return cookie_jar, cookies


def _send_without_supplier_jar_cookies(
    session,
    url: str,
    headers: dict,
    cookies: dict,
    timeout,
    cookie_snapshot,
):
    """Send with a request-local filtered jar while keeping Session semantics.

    Requests automatically merges Session.cookies even when ``cookies={}`` is
    passed. Preparing on a shallow Session copy with a filtered jar prevents a
    host-only, non-Secure supplier cookie from leaking to an untrusted hop.
    The original Session still sends the prepared request, so it retains normal
    adapter/TLS/environment handling and extracts response cookies into its
    original, correctly scoped jar.
    """
    if cookie_snapshot is None:
        return None

    cookie_jar, cookie_values = cookie_snapshot
    try:
        filtered_jar = RequestsCookieJar()
        filtered_jar.set_policy(copy.copy(cookie_jar.get_policy()))
        for cookie in cookie_values:
            if not _is_supplier_auth_cookie(cookie):
                filtered_jar.set_cookie(copy.copy(cookie))

        request_session = copy.copy(session)
        request_session.cookies = filtered_jar
        prepared = request_session.prepare_request(requests.Request(
            method='GET',
            url=url,
            headers=headers,
            cookies=cookies,
        ))
        settings = session.merge_environment_settings(
            prepared.url, {}, True, None, None,
        )
        return session.send(
            prepared,
            timeout=timeout,
            allow_redirects=False,
            **settings,
        )
    except Exception:
        # The filtered request must fail closed if the cookie jar cannot be
        # copied/prepared or Requests cannot send it with Session settings.
        logger.debug('Unable to prepare filtered supplier-cookie request')
        return None

# Дисковый кэш восстанавливаем из source URL, поэтому он обязан иметь cap.
# При достижении cap старые JPEG удаляются до low-water mark в отдельном
# daemon thread. Это не выполняется внутри HTTP request.
PHOTO_CACHE_MAX_BYTES = _bounded_env_int(
    'PHOTO_CACHE_MAX_BYTES', 15 * 1024 ** 3, 1024 ** 3, 100 * 1024 ** 3,
)
PHOTO_CACHE_PRUNE_TO_BYTES = min(
    PHOTO_CACHE_MAX_BYTES,
    _bounded_env_int(
        'PHOTO_CACHE_PRUNE_TO_BYTES', 12 * 1024 ** 3,
        512 * 1024 ** 2, 100 * 1024 ** 3,
    ),
)
PHOTO_CACHE_MIN_FREE_BYTES = _bounded_env_int(
    'PHOTO_CACHE_MIN_FREE_BYTES', 5 * 1024 ** 3,
    512 * 1024 ** 2, 100 * 1024 ** 3,
)
PHOTO_CACHE_MAINTENANCE_INTERVAL = _bounded_env_int(
    'PHOTO_CACHE_MAINTENANCE_INTERVAL_SECONDS', 1800, 60, 86400,
)
PHOTO_CACHE_HARD_MIN_FREE_BYTES = 256 * 1024 ** 2


@dataclass
class _PhotoDownloadTask:
    supplier_type: str
    external_id: str
    url: str
    auth_cookies: Optional[dict]
    target_size: Tuple[int, int]
    background_color: str
    fallback_urls: List[str]
    auth_cookies_provider: Optional[Callable[[], dict]]
    dedupe_key: Tuple[str, str, str]


# ============================================================================
# PHOTO CACHE MANAGER
# ============================================================================

class PhotoCacheManager:
    """
    Менеджер кэша фотографий

    Позволяет:
    - Сохранять фото по поставщику и ID товара
    - Загружать фото из кэша
    - Ставить загрузку в фоновую очередь
    - Проверять наличие фото
    """

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return

        self._initialized = True
        self._download_queue = queue.Queue(maxsize=MAX_DOWNLOAD_QUEUE_SIZE)
        self._workers = []
        self._running = False
        self._pending_downloads = set()
        self._pending_lock = threading.Lock()
        self._maintenance_lock = threading.Lock()
        self._last_maintenance_started = 0.0
        self._stats = {
            'cache_hits': 0,
            'cache_misses': 0,
            'downloads_queued': 0,
            'downloads_completed': 0,
            'downloads_failed': 0,
            'downloads_deduplicated': 0,
            'queue_rejected': 0,
            'maintenance_runs': 0,
            'maintenance_deleted_files': 0,
            'maintenance_deleted_bytes': 0,
        }

        # Создаем базовую директорию
        os.makedirs(PHOTO_CACHE_DIR, exist_ok=True)

        # Запускаем воркеры
        self.start_workers()

    def start_workers(self):
        """Запускает фоновые воркеры для загрузки фото"""
        if self._running:
            return

        self._running = True
        for i in range(NUM_DOWNLOAD_WORKERS):
            worker = threading.Thread(
                target=self._download_worker,
                name=f'PhotoDownloader-{i}',
                daemon=True
            )
            worker.start()
            self._workers.append(worker)

        logger.info(f"Запущено {NUM_DOWNLOAD_WORKERS} воркеров для загрузки фото")

    def stop_workers(self):
        """Останавливает воркеры"""
        self._running = False
        # Добавляем None для завершения воркеров
        for _ in self._workers:
            try:
                self._download_queue.put_nowait(None)
            except queue.Full:
                pass

    def _download_worker(self):
        """Воркер для фоновой загрузки фото"""
        while self._running:
            try:
                task = self._download_queue.get(timeout=1)
                if task is None:
                    self._download_queue.task_done()
                    break
                try:
                    auth_cookies = task.auth_cookies
                    if task.auth_cookies_provider is not None:
                        try:
                            auth_cookies = task.auth_cookies_provider() or {}
                        except Exception as exc:
                            logger.debug(
                                "Не удалось подготовить auth cookies для %s: %s",
                                task.supplier_type, exc,
                            )
                            auth_cookies = {}

                    downloaded = self._download_and_save(
                        task.supplier_type,
                        task.external_id,
                        task.url,
                        auth_cookies,
                        task.target_size,
                        task.background_color,
                        task.fallback_urls,
                    )
                    if downloaded:
                        self._stats['downloads_completed'] += 1
                    else:
                        self._stats['downloads_failed'] += 1
                except Exception as e:
                    logger.debug(
                        "Ошибка загрузки фото %s: %s", task.url[:50], e,
                    )
                    self._stats['downloads_failed'] += 1
                finally:
                    with self._pending_lock:
                        self._pending_downloads.discard(task.dedupe_key)
                    self._download_queue.task_done()

            except queue.Empty:
                continue
            except Exception as e:
                logger.error(f"Ошибка воркера загрузки фото: {e}")

    @staticmethod
    def get_photo_hash(url: str) -> str:
        """Генерирует хэш для URL фото"""
        return hashlib.md5(url.encode('utf-8')).hexdigest()[:16]

    def get_cache_path(self, supplier_type: str, external_id: str, url: str) -> str:
        """Возвращает путь к кэшированному файлу"""
        photo_hash = self.get_photo_hash(url)
        safe_supplier_type = "".join(
            c if c.isalnum() or c in '-_' else '_'
            for c in str(supplier_type)
        ) or 'unknown'
        safe_ext_id = "".join(c if c.isalnum() or c in '-_' else '_' for c in str(external_id))
        return os.path.join(
            PHOTO_CACHE_DIR,
            safe_supplier_type,
            safe_ext_id,
            f"{photo_hash}.jpg"
        )

    def is_cached(self, supplier_type: str, external_id: str, url: str) -> bool:
        """Проверяет, есть ли фото в кэше"""
        cache_path = self.get_cache_path(supplier_type, external_id, url)
        return os.path.exists(cache_path)

    def get_cached_photo(self, supplier_type: str, external_id: str, url: str) -> Optional[bytes]:
        """
        Получает фото из кэша

        Returns:
            Байты изображения или None если не найдено
        """
        cache_path = self.get_cache_path(supplier_type, external_id, url)

        if os.path.exists(cache_path):
            self._stats['cache_hits'] += 1
            try:
                with open(cache_path, 'rb') as f:
                    return f.read()
            except Exception as e:
                logger.error(f"Ошибка чтения кэша: {e}")
                return None

        self._stats['cache_misses'] += 1
        return None

    def save_to_cache(
        self,
        supplier_type: str,
        external_id: str,
        url: str,
        image_bytes: bytes,
    ) -> bool:
        """Атомарно сохраняет фото и запускает async maintenance кэша."""
        cache_path = self.get_cache_path(supplier_type, external_id, url)
        cache_dir = os.path.dirname(cache_path)
        os.makedirs(cache_dir, exist_ok=True)

        try:
            free_bytes = shutil.disk_usage(PHOTO_CACHE_DIR).free
        except OSError:
            free_bytes = PHOTO_CACHE_HARD_MIN_FREE_BYTES
        if free_bytes < PHOTO_CACHE_HARD_MIN_FREE_BYTES:
            logger.warning(
                "Photo cache write skipped: свободно только %s bytes",
                free_bytes,
            )
            self._schedule_cache_maintenance()
            return False

        temp_path = (
            f"{cache_path}.tmp-{os.getpid()}-{threading.get_ident()}"
        )

        try:
            with open(temp_path, 'wb') as f:
                f.write(image_bytes)
                f.flush()
            os.replace(temp_path, cache_path)
            self._schedule_cache_maintenance()
            return True
        except Exception as e:
            logger.error(f"Ошибка сохранения в кэш: {e}")
            return False
        finally:
            try:
                if os.path.exists(temp_path):
                    os.unlink(temp_path)
            except OSError:
                pass

    def _download_key(
        self, supplier_type: str, external_id: str, url: str,
    ) -> Tuple[str, str, str]:
        return (
            str(supplier_type),
            str(external_id),
            self.get_photo_hash(url),
        )

    def _build_download_task(
        self,
        supplier_type: str,
        external_id: str,
        url: str,
        auth_cookies: Optional[dict],
        target_size: Tuple[int, int],
        background_color: str,
        fallback_urls: Optional[List[str]],
        auth_cookies_provider: Optional[Callable[[], dict]],
    ) -> _PhotoDownloadTask:
        return _PhotoDownloadTask(
            supplier_type=str(supplier_type),
            external_id=str(external_id),
            url=url,
            auth_cookies=dict(auth_cookies or {}),
            target_size=target_size,
            background_color=background_color,
            fallback_urls=list(fallback_urls or []),
            auth_cookies_provider=auth_cookies_provider,
            dedupe_key=self._download_key(supplier_type, external_id, url),
        )

    def _enqueue_task(
        self,
        task: _PhotoDownloadTask,
        *,
        block: bool = False,
        timeout: Optional[float] = None,
    ) -> bool:
        if self.is_cached(task.supplier_type, task.external_id, task.url):
            return False

        with self._pending_lock:
            if task.dedupe_key in self._pending_downloads:
                self._stats['downloads_deduplicated'] += 1
                return False
            self._pending_downloads.add(task.dedupe_key)

        try:
            if block:
                self._download_queue.put(task, block=True, timeout=timeout)
            else:
                self._download_queue.put_nowait(task)
            self._stats['downloads_queued'] += 1
            return True
        except queue.Full:
            with self._pending_lock:
                self._pending_downloads.discard(task.dedupe_key)
            self._stats['queue_rejected'] += 1
            logger.warning("Очередь загрузки фото переполнена")
            return False

    def queue_download(
        self,
        supplier_type: str,
        external_id: str,
        url: str,
        auth_cookies: Optional[dict] = None,
        target_size: Tuple[int, int] = (1200, 1200),
        background_color: str = 'white',
        fallback_urls: Optional[List[str]] = None,
        auth_cookies_provider: Optional[Callable[[], dict]] = None,
    ) -> bool:
        """
        Ставит загрузку фото в очередь

        Returns:
            True если добавлено в очередь, False если очередь полна или фото уже есть
        """
        task = self._build_download_task(
            supplier_type,
            external_id,
            url,
            auth_cookies,
            target_size,
            background_color,
            fallback_urls,
            auth_cookies_provider,
        )
        return self._enqueue_task(task)

    def _download_and_save(
        self,
        supplier_type: str,
        external_id: str,
        url: str,
        auth_cookies: Optional[dict],
        target_size: Tuple[int, int],
        background_color: str,
        fallback_urls: List[str]
    ) -> bool:
        """Скачивает и сохраняет фото в общем wall-clock/size бюджете."""
        if self.is_cached(supplier_type, external_id, url):
            return True

        urls_to_try = [url] + fallback_urls

        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'image/*,*/*;q=0.8',
        }

        if _is_supplier_auth_origin(url):
            headers['Referer'] = _SUPPLIER_AUTH_REFERER

        deadline = time.monotonic() + DOWNLOAD_TOTAL_BUDGET
        with requests.Session() as session:
            for current_url in urls_to_try:
                try:
                    content = self._download_image_bytes(
                        session,
                        current_url,
                        headers,
                        auth_cookies or {},
                        deadline,
                    )
                    if content is None:
                        continue

                    img = Image.open(BytesIO(content))
                    try:
                        if img.width * img.height > DOWNLOAD_MAX_PIXELS:
                            raise ValueError('image_pixel_limit_exceeded')
                        img.load()
                        if img.size != target_size:
                            processed = self._resize_with_padding(
                                img, target_size, background_color,
                            )
                        elif img.mode != 'RGB':
                            processed = img.convert('RGB')
                        else:
                            processed = img

                        output = BytesIO()
                        processed.save(output, format='JPEG', quality=95)
                        if processed is not img:
                            processed.close()
                    finally:
                        img.close()

                    return self.save_to_cache(
                        supplier_type, external_id, url, output.getvalue(),
                    )
                except Exception as e:
                    logger.debug(
                        "Ошибка загрузки %s: %s", current_url[:50], e,
                    )
                    continue
        return False

    @staticmethod
    def _download_image_bytes(
        session: requests.Session,
        url: str,
        headers: dict,
        auth_cookies: dict,
        deadline: float,
    ) -> Optional[bytes]:
        """Bounded manual-redirect download with exact-origin supplier auth."""
        from services.url_security import validate_external_url

        current_url = url
        for _ in range(5):
            if validate_external_url(current_url) is not None:
                return None
            remaining = deadline - time.monotonic()
            if remaining <= 0.5:
                return None

            trusted_supplier_origin = _is_supplier_auth_origin(current_url)
            request_cookies = (auth_cookies or {}) if trusted_supplier_origin else {}
            request_headers = headers
            if not trusted_supplier_origin and any(
                str(name).lower() == 'referer'
                and value == _SUPPLIER_AUTH_REFERER
                for name, value in headers.items()
            ):
                request_headers = {
                    name: value for name, value in headers.items()
                    if not (
                        str(name).lower() == 'referer'
                        and value == _SUPPLIER_AUTH_REFERER
                    )
                }

            timeout = (
                min(DOWNLOAD_CONNECT_TIMEOUT, remaining),
                min(DOWNLOAD_READ_TIMEOUT, remaining),
            )
            if trusted_supplier_origin:
                response = session.get(
                    current_url,
                    headers=request_headers,
                    cookies=request_cookies,
                    timeout=timeout,
                    allow_redirects=False,
                    stream=True,
                )
            else:
                cookie_snapshot = _supplier_cookie_snapshot(session)
                if cookie_snapshot is None:
                    return None
                _cookie_jar, cookie_values = cookie_snapshot
                if any(_is_supplier_auth_cookie(cookie) for cookie in cookie_values):
                    response = _send_without_supplier_jar_cookies(
                        session,
                        current_url,
                        request_headers,
                        request_cookies,
                        timeout,
                        cookie_snapshot,
                    )
                    if response is None:
                        return None
                else:
                    response = session.get(
                        current_url,
                        headers=request_headers,
                        cookies=request_cookies,
                        timeout=timeout,
                        allow_redirects=False,
                        stream=True,
                    )
            try:
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get('Location')
                    if not location:
                        return None
                    current_url = urljoin(current_url, location)
                    continue

                response.raise_for_status()
                content_length = response.headers.get('Content-Length')
                if content_length:
                    try:
                        if int(content_length) > DOWNLOAD_MAX_BYTES:
                            return None
                    except ValueError:
                        pass

                chunks = []
                total = 0
                for chunk in response.iter_content(chunk_size=65536):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if (
                        total > DOWNLOAD_MAX_BYTES
                        or time.monotonic() > deadline
                    ):
                        return None
                    chunks.append(chunk)
                content = b''.join(chunks)
                content_type = response.headers.get('Content-Type', '')
                if not content_type.startswith('image/') and len(content) < 1024:
                    return None
                return content
            finally:
                response.close()
        return None

    def _schedule_cache_maintenance(self) -> None:
        """Запускает не более одного low-frequency prune вне caller thread."""
        now = time.monotonic()
        with self._maintenance_lock:
            if (
                now - self._last_maintenance_started
                < PHOTO_CACHE_MAINTENANCE_INTERVAL
            ):
                return
            self._last_maintenance_started = now

        thread = threading.Thread(
            target=self._run_cache_maintenance,
            name='PhotoCacheMaintenance',
            daemon=True,
        )
        thread.start()

    def _run_cache_maintenance(self) -> None:
        """Удаляет самые старые восстанавливаемые JPEG до low-water mark."""
        try:
            import fcntl

            os.makedirs(PHOTO_CACHE_DIR, exist_ok=True)
            lock_path = os.path.join(PHOTO_CACHE_DIR, '.maintenance.lock')
            with open(lock_path, 'a+b') as lock_file:
                try:
                    fcntl.flock(
                        lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB,
                    )
                except BlockingIOError:
                    return
                self._prune_cache_files()
        except Exception as exc:
            logger.warning("Photo cache maintenance failed: %s", exc)

    def _prune_cache_files(self) -> None:
        files = []
        total_bytes = 0
        now = time.time()

        for directory, _subdirs, names in os.walk(PHOTO_CACHE_DIR):
            for name in names:
                path = os.path.join(directory, name)
                if '.tmp-' in name:
                    try:
                        stat = os.stat(path, follow_symlinks=False)
                        if now - stat.st_mtime > 3600:
                            os.unlink(path)
                    except OSError:
                        pass
                    continue
                if not name.endswith('.jpg'):
                    continue
                try:
                    stat = os.stat(path, follow_symlinks=False)
                except OSError:
                    continue
                if not os.path.isfile(path):
                    continue
                files.append((stat.st_mtime, stat.st_size, path))
                total_bytes += stat.st_size

        try:
            free_bytes = shutil.disk_usage(PHOTO_CACHE_DIR).free
        except OSError:
            free_bytes = PHOTO_CACHE_MIN_FREE_BYTES

        target_bytes = total_bytes
        if total_bytes > PHOTO_CACHE_MAX_BYTES:
            target_bytes = min(target_bytes, PHOTO_CACHE_PRUNE_TO_BYTES)
        if free_bytes < PHOTO_CACHE_MIN_FREE_BYTES:
            target_bytes = min(
                target_bytes,
                max(
                    0,
                    total_bytes
                    - (PHOTO_CACHE_MIN_FREE_BYTES - free_bytes),
                ),
            )

        self._stats['maintenance_runs'] += 1
        if target_bytes >= total_bytes:
            return

        deleted_files = 0
        deleted_bytes = 0
        for _mtime, size, path in sorted(files):
            if total_bytes - deleted_bytes <= target_bytes:
                break
            try:
                os.unlink(path)
            except FileNotFoundError:
                continue
            except OSError as exc:
                logger.debug("Не удалось удалить cache file %s: %s", path, exc)
                continue
            deleted_files += 1
            deleted_bytes += size

        self._stats['maintenance_deleted_files'] += deleted_files
        self._stats['maintenance_deleted_bytes'] += deleted_bytes
        logger.info(
            "Photo cache maintenance: total=%s target=%s deleted=%s files/%s bytes",
            total_bytes,
            target_bytes,
            deleted_files,
            deleted_bytes,
        )

    @staticmethod
    def _resize_with_padding(
        img: Image.Image,
        target_size: Tuple[int, int],
        background_color: str = 'white'
    ) -> Image.Image:
        """Изменяет размер с добавлением padding"""
        if img.mode != 'RGB':
            img = img.convert('RGB')

        img_width, img_height = img.size
        target_width, target_height = target_size

        ratio = min(target_width / img_width, target_height / img_height)
        new_width = int(img_width * ratio)
        new_height = int(img_height * ratio)

        img_resized = img.resize((new_width, new_height), Image.Resampling.LANCZOS)

        new_img = Image.new('RGB', target_size, background_color)
        paste_x = (target_width - new_width) // 2
        paste_y = (target_height - new_height) // 2
        new_img.paste(img_resized, (paste_x, paste_y))

        return new_img

    def download_now(
        self,
        supplier_type: str,
        external_id: str,
        url: str,
        auth_cookies: Optional[dict] = None,
        fallback_urls: Optional[List[str]] = None
    ) -> bool:
        """
        Синхронная загрузка фото (блокирующая).
        Используется когда нужно гарантированно получить фото сразу.

        Returns:
            True если фото успешно скачано и закэшировано
        """
        if self.is_cached(supplier_type, external_id, url):
            return True

        try:
            self._download_and_save(
                supplier_type, external_id, url,
                auth_cookies, (1200, 1200), 'white',
                fallback_urls or []
            )
            return self.is_cached(supplier_type, external_id, url)
        except Exception as e:
            logger.debug(f"Синхронная загрузка не удалась {url[:50]}: {e}")
            return False

    def list_cached_photos(self, supplier_type: str, external_id: str) -> List[str]:
        """Список всех закэшированных фото для товара поставщика"""
        safe_ext_id = "".join(c if c.isalnum() or c in '-_' else '_' for c in str(external_id))
        dir_path = os.path.join(PHOTO_CACHE_DIR, supplier_type, safe_ext_id)
        if not os.path.isdir(dir_path):
            return []
        return sorted([
            os.path.join(dir_path, f)
            for f in os.listdir(dir_path)
            if f.endswith('.jpg')
        ])

    def get_stats(self) -> Dict:
        """Возвращает статистику кэша"""
        return {
            **self._stats,
            'queue_size': self._download_queue.qsize(),
            'workers_running': len([w for w in self._workers if w.is_alive()])
        }

    def get_product_photos(
        self,
        supplier_type: str,
        external_id: str,
        photo_urls: List[Dict]
    ) -> List[Dict]:
        """
        Получает фото для товара (из кэша или ставит в очередь)

        Args:
            supplier_type: Тип поставщика (sexoptovik, etc.)
            external_id: Внешний ID товара
            photo_urls: Список словарей с URL фото

        Returns:
            Список словарей с информацией о фото
        """
        result = []

        for photo_data in photo_urls:
            # Определяем основной URL
            url = photo_data.get('sexoptovik') or photo_data.get('original') or photo_data.get('blur')
            if not url:
                continue

            # Проверяем кэш
            is_cached = self.is_cached(supplier_type, external_id, url)

            if not is_cached:
                # Ставим в очередь на загрузку
                fallbacks = []
                if photo_data.get('blur'):
                    fallbacks.append(photo_data['blur'])
                if photo_data.get('original'):
                    fallbacks.append(photo_data['original'])

                self.queue_download(
                    supplier_type=supplier_type,
                    external_id=external_id,
                    url=url,
                    fallback_urls=fallbacks
                )

            result.append({
                **photo_data,
                'cached': is_cached,
                'cache_path': self.get_cache_path(supplier_type, external_id, url) if is_cached else None
            })

        return result

    def bulk_download_for_supplier(self, supplier_id: int) -> Dict:
        """
        Запускает фоновое скачивание ВСЕХ фото поставщика.
        Фото подаются в очередь постепенно через фоновый поток,
        чтобы не переполнять очередь.

        Args:
            supplier_id: ID поставщика в БД

        Returns:
            dict: {total_photos, already_cached, queued, errors}
        """
        import json
        from models import SupplierProduct, Supplier

        supplier = Supplier.query.get(supplier_id)
        if not supplier:
            return {'total_photos': 0, 'already_cached': 0, 'queued': 0, 'errors': ['Поставщик не найден']}

        supplier_type = supplier.code or 'unknown'
        total_photos = 0
        already_cached = 0

        # Собираем все задания на скачивание (без постановки в очередь)
        download_tasks = []

        page = 1
        batch_size = 200
        while True:
            products = SupplierProduct.query.filter_by(
                supplier_id=supplier_id
            ).filter(
                SupplierProduct.photo_urls_json.isnot(None),
                SupplierProduct.photo_urls_json != '[]'
            ).limit(batch_size).offset((page - 1) * batch_size).all()

            if not products:
                break

            for product in products:
                try:
                    photo_urls = json.loads(product.photo_urls_json)
                except (json.JSONDecodeError, TypeError):
                    continue

                external_id = product.external_id or ''

                for ph in photo_urls:
                    if not isinstance(ph, dict):
                        continue

                    url = ph.get('sexoptovik') or ph.get('original') or ph.get('blur')
                    if not url:
                        continue

                    total_photos += 1

                    if self.is_cached(supplier_type, external_id, url):
                        already_cached += 1
                        continue

                    # Собираем fallback URLs
                    fallbacks = []
                    if ph.get('blur') and ph['blur'] != url:
                        fallbacks.append(ph['blur'])
                    if ph.get('original') and ph['original'] != url:
                        fallbacks.append(ph['original'])

                    download_tasks.append(self._build_download_task(
                        supplier_type,
                        external_id,
                        url,
                        None,  # auth_cookies
                        (1200, 1200),  # target_size
                        'white',  # background_color
                        fallbacks,
                        None,  # auth_cookies_provider
                    ))

            page += 1

        to_queue = len(download_tasks)

        logger.info(
            f"Bulk download для {supplier_type}: "
            f"всего={total_photos}, в кэше={already_cached}, к загрузке={to_queue}"
        )

        # Запускаем фоновый поток-фидер, который подаёт задания в очередь
        # с блокировкой (ждёт когда освободится место)
        if download_tasks:
            def _feeder():
                fed = 0
                for task in download_tasks:
                    try:
                        if self._enqueue_task(
                            task, block=True, timeout=300,
                        ):
                            fed += 1
                        if fed and fed % 500 == 0:
                            logger.info(f"Фидер {supplier_type}: подано {fed}/{to_queue} в очередь")
                    except Exception as e:
                        logger.warning(f"Фидер: ошибка постановки в очередь: {e}")
                        break
                logger.info(f"Фидер {supplier_type}: завершён, подано {fed}/{to_queue}")

            feeder_thread = threading.Thread(
                target=_feeder,
                name=f'photo-feeder-{supplier_type}',
                daemon=True
            )
            feeder_thread.start()

        return {
            'total_photos': total_photos,
            'already_cached': already_cached,
            'queued': to_queue,
            'errors': []
        }

    def get_download_progress(self, supplier_id: int) -> Dict:
        """
        Возвращает прогресс скачивания фото для поставщика.

        Returns:
            dict: {total, cached, pending, percent}
        """
        import json
        from models import SupplierProduct, Supplier

        supplier = Supplier.query.get(supplier_id)
        if not supplier:
            return {'total': 0, 'cached': 0, 'pending': 0, 'percent': 0}

        supplier_type = supplier.code or 'unknown'
        total = 0
        cached = 0

        products = SupplierProduct.query.filter_by(
            supplier_id=supplier_id
        ).filter(
            SupplierProduct.photo_urls_json.isnot(None),
            SupplierProduct.photo_urls_json != '[]'
        ).all()

        for product in products:
            try:
                photo_urls = json.loads(product.photo_urls_json)
            except (json.JSONDecodeError, TypeError):
                continue

            external_id = product.external_id or ''

            for ph in photo_urls:
                if not isinstance(ph, dict):
                    continue
                url = ph.get('sexoptovik') or ph.get('original') or ph.get('blur')
                if not url:
                    continue

                total += 1
                if self.is_cached(supplier_type, external_id, url):
                    cached += 1

        pending = total - cached
        percent = round((cached / total * 100), 1) if total > 0 else 100.0

        return {
            'total': total,
            'cached': cached,
            'pending': pending,
            'percent': percent,
            'queue_size': self._download_queue.qsize()
        }


# Глобальный экземпляр
_photo_cache: Optional[PhotoCacheManager] = None


def get_photo_cache() -> PhotoCacheManager:
    """Возвращает глобальный экземпляр кэша фото"""
    global _photo_cache
    if _photo_cache is None:
        _photo_cache = PhotoCacheManager()
    return _photo_cache


def queue_product_photos(
    supplier_type: str,
    external_id: str,
    photo_urls: List[Dict],
    auth_cookies: Optional[dict] = None
):
    """
    Удобная функция для постановки фото товара в очередь загрузки

    Args:
        supplier_type: Тип поставщика
        external_id: Внешний ID товара
        photo_urls: Список URL фото
        auth_cookies: Куки авторизации (для sexoptovik)
    """
    cache = get_photo_cache()

    for photo_data in photo_urls:
        url = photo_data.get('sexoptovik') or photo_data.get('original') or photo_data.get('blur')
        if not url:
            continue

        fallbacks = []
        if photo_data.get('blur') and photo_data.get('blur') != url:
            fallbacks.append(photo_data['blur'])
        if photo_data.get('original') and photo_data.get('original') != url:
            fallbacks.append(photo_data['original'])

        cache.queue_download(
            supplier_type=supplier_type,
            external_id=external_id,
            url=url,
            auth_cookies=auth_cookies,
            fallback_urls=fallbacks
        )


def get_cached_photo_path(supplier_type: str, external_id: str, url: str) -> Optional[str]:
    """
    Возвращает путь к кэшированному фото если оно есть

    Returns:
        Путь к файлу или None
    """
    cache = get_photo_cache()
    if cache.is_cached(supplier_type, external_id, url):
        return cache.get_cache_path(supplier_type, external_id, url)
    return None


def get_supplier_photo_url(supplier_type: str, external_id: str, url: str) -> str:
    """
    Возвращает безопасный URL для раздачи фото поставщика через наш сервер.
    Маршрут: /photos/supplier/{supplier_type}/{safe_external_id}/{photo_hash}

    Args:
        supplier_type: Тип поставщика (sexoptovik, etc.)
        external_id: Внешний ID товара
        url: Оригинальный URL фото поставщика

    Returns:
        Относительный URL для serve через наш сервер
    """
    cache = get_photo_cache()
    photo_hash = cache.get_photo_hash(url)
    safe_id = "".join(c if c.isalnum() or c in '-_' else '_' for c in str(external_id))
    return f"/photos/supplier/{supplier_type}/{safe_id}/{photo_hash}"


def bulk_download_supplier_photos(supplier_id: int) -> Dict:
    """
    Удобная функция для запуска массового скачивания фото поставщика.

    Args:
        supplier_id: ID поставщика

    Returns:
        dict: {total_photos, already_cached, queued, errors}
    """
    cache = get_photo_cache()
    return cache.bulk_download_for_supplier(supplier_id)
