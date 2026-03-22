# -*- coding: utf-8 -*-
"""
Кэш фото для контент-фабрики.

При генерации контента скачивает фото товаров (с WB CDN или локального кэша)
и сохраняет в data/content_photos/{nm_id}/{index}.jpg.
Фото раздаются через публичный роут /content-photos/{nm_id}/{index}.jpg
без авторизации.
"""
import io
import os
import logging
from pathlib import Path
from typing import List, Optional

import requests
from PIL import Image

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
CONTENT_PHOTOS_DIR = BASE_DIR / 'data' / 'content_photos'

_BROWSER_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/120.0.0.0 Safari/537.36'
    ),
    'Accept': 'image/webp,image/apng,image/*,*/*;q=0.8',
    'Referer': 'https://www.wildberries.ru/',
}


def get_cached_photo_path(nm_id: int, index: int) -> Path:
    """Путь к кэшированному фото на диске."""
    return CONTENT_PHOTOS_DIR / str(nm_id) / f'{index}.jpg'


def is_photo_cached(nm_id: int, index: int) -> bool:
    """Проверяет, есть ли фото в кэше."""
    path = get_cached_photo_path(nm_id, index)
    return path.exists() and path.stat().st_size > 1024


def _content_photo_sig(nm_id: int, index: int) -> str:
    """Генерирует HMAC-подпись для публичного URL фото."""
    import hmac
    import hashlib
    from flask import current_app
    secret = current_app.config.get('SECRET_KEY', '').encode()
    return hmac.new(secret, f'{nm_id}:{index}'.encode(), hashlib.sha256).hexdigest()[:32]


def get_content_photo_url(nm_id: int, index: int) -> str:
    """Возвращает публичный URL для кэшированного фото (с HMAC-подписью)."""
    from flask import current_app
    sig = _content_photo_sig(nm_id, index)
    public_base = current_app.config.get('PUBLIC_BASE_URL', '').rstrip('/')
    if public_base:
        return f'{public_base}/content-photos/{nm_id}/{index}.jpg?sig={sig}'
    from flask import url_for
    return url_for('serve_content_photo', nm_id=nm_id, index=index, sig=sig, _external=True)


def download_and_cache_photo(nm_id: int, index: int, source_url: str) -> bool:
    """Скачивает фото, конвертирует в JPEG и сохраняет в кэш.

    Returns: True если успешно.
    """
    if is_photo_cached(nm_id, index):
        return True

    try:
        resp = requests.get(source_url, timeout=15, headers=_BROWSER_HEADERS)
        if resp.status_code != 200:
            logger.warning(f'Content photo download HTTP {resp.status_code}: {source_url}')
            return False

        raw = resp.content
        if len(raw) < 512:
            logger.warning(f'Content photo too small ({len(raw)}B): {source_url}')
            return False

        # Конвертируем в JPEG
        img = Image.open(io.BytesIO(raw))
        if img.mode not in ('RGB',):
            img = img.convert('RGB')
        buf = io.BytesIO()
        img.save(buf, format='JPEG', quality=93)
        jpeg_bytes = buf.getvalue()

        # Сохраняем на диск
        path = get_cached_photo_path(nm_id, index)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(jpeg_bytes)

        logger.info(f'Cached content photo: nm_id={nm_id} idx={index} ({len(jpeg_bytes)}B)')
        return True

    except Exception as e:
        logger.error(f'Content photo cache error: nm_id={nm_id} idx={index}: {e}')
        return False


def cache_product_photos(nm_id: int, source_urls: List[str]) -> List[str]:
    """Скачивает и кэширует все фото товара.

    Returns: список публичных URL для успешно закэшированных фото.
    """
    result_urls = []
    for i, url in enumerate(source_urls):
        index = i + 1
        ok = download_and_cache_photo(nm_id, index, url)
        if ok:
            try:
                result_urls.append(get_content_photo_url(nm_id, index))
            except RuntimeError:
                # Нет app context (планировщик) — формируем URL вручную
                result_urls.append(f'/content-photos/{nm_id}/{index}.jpg')
    return result_urls


def get_cached_photo_urls(nm_id: int) -> List[str]:
    """Возвращает URL всех закэшированных фото для nm_id."""
    nm_dir = CONTENT_PHOTOS_DIR / str(nm_id)
    if not nm_dir.exists():
        return []
    urls = []
    for jpg in sorted(nm_dir.glob('*.jpg')):
        if jpg.stat().st_size > 1024:
            index = int(jpg.stem)
            try:
                urls.append(get_content_photo_url(nm_id, index))
            except RuntimeError:
                urls.append(f'/content-photos/{nm_id}/{index}.jpg')
    return urls
