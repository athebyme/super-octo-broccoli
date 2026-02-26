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
from typing import Optional, Dict, List, Tuple
from io import BytesIO
from datetime import datetime
import requests
from PIL import Image

logger = logging.getLogger(__name__)


# ============================================================================
# КОНФИГУРАЦИЯ
# ============================================================================

# Базовая директория для кэша фото
PHOTO_CACHE_DIR = os.environ.get('PHOTO_CACHE_DIR', 'data/photo_cache')

# Максимальный размер очереди загрузки
MAX_DOWNLOAD_QUEUE_SIZE = 1000

# Количество воркеров для фоновой загрузки
NUM_DOWNLOAD_WORKERS = 3

# Таймаут для загрузки одного фото
DOWNLOAD_TIMEOUT = 15


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
        self._session = requests.Session()
        self._stats = {
            'cache_hits': 0,
            'cache_misses': 0,
            'downloads_queued': 0,
            'downloads_completed': 0,
            'downloads_failed': 0
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
                    break

                supplier_type, external_id, url, auth_cookies, target_size, bg_color, fallbacks = task

                try:
                    self._download_and_save(
                        supplier_type, external_id, url,
                        auth_cookies, target_size, bg_color, fallbacks
                    )
                    self._stats['downloads_completed'] += 1
                except Exception as e:
                    logger.debug(f"Ошибка загрузки фото {url[:50]}: {e}")
                    self._stats['downloads_failed'] += 1

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
        # Безопасный external_id для имени файла
        safe_ext_id = "".join(c if c.isalnum() or c in '-_' else '_' for c in str(external_id))
        return os.path.join(
            PHOTO_CACHE_DIR,
            supplier_type,
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

    def save_to_cache(self, supplier_type: str, external_id: str, url: str, image_bytes: bytes):
        """Сохраняет фото в кэш"""
        cache_path = self.get_cache_path(supplier_type, external_id, url)

        # Создаем директорию если нужно
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)

        try:
            with open(cache_path, 'wb') as f:
                f.write(image_bytes)
        except Exception as e:
            logger.error(f"Ошибка сохранения в кэш: {e}")

    def queue_download(
        self,
        supplier_type: str,
        external_id: str,
        url: str,
        auth_cookies: Optional[dict] = None,
        target_size: Tuple[int, int] = (1200, 1200),
        background_color: str = 'white',
        fallback_urls: Optional[List[str]] = None
    ) -> bool:
        """
        Ставит загрузку фото в очередь

        Returns:
            True если добавлено в очередь, False если очередь полна или фото уже есть
        """
        # Если уже в кэше - не качаем
        if self.is_cached(supplier_type, external_id, url):
            return False

        try:
            self._download_queue.put_nowait((
                supplier_type,
                external_id,
                url,
                auth_cookies,
                target_size,
                background_color,
                fallback_urls or []
            ))
            self._stats['downloads_queued'] += 1
            return True
        except queue.Full:
            logger.warning("Очередь загрузки фото переполнена")
            return False

    def _download_and_save(
        self,
        supplier_type: str,
        external_id: str,
        url: str,
        auth_cookies: Optional[dict],
        target_size: Tuple[int, int],
        background_color: str,
        fallback_urls: List[str]
    ):
        """Скачивает и сохраняет фото"""
        urls_to_try = [url] + fallback_urls

        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'image/*,*/*;q=0.8',
        }

        if 'sexoptovik.ru' in url:
            headers['Referer'] = 'https://sexoptovik.ru/admin/'

        for current_url in urls_to_try:
            try:
                response = self._session.get(
                    current_url,
                    headers=headers,
                    cookies=auth_cookies,
                    timeout=DOWNLOAD_TIMEOUT,
                    allow_redirects=True
                )
                response.raise_for_status()

                # Проверяем что это изображение
                content_type = response.headers.get('Content-Type', '')
                if not content_type.startswith('image/') and len(response.content) < 1024:
                    continue

                # Открываем и обрабатываем
                img = Image.open(BytesIO(response.content))

                # Resize с padding если нужно
                if img.size != target_size:
                    img = self._resize_with_padding(img, target_size, background_color)

                # Сохраняем в кэш
                output = BytesIO()
                if img.mode != 'RGB':
                    img = img.convert('RGB')
                img.save(output, format='JPEG', quality=95)

                self.save_to_cache(supplier_type, external_id, url, output.getvalue())
                return

            except Exception as e:
                logger.debug(f"Ошибка загрузки {current_url[:50]}: {e}")
                continue

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
