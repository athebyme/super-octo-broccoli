# services/standard_photos.py
# -*- coding: utf-8 -*-
"""Композиция стандартных фото продавца в упорядоченный набор URL для карточки."""
import os

# Максимум фото в карточке на Wildberries
WB_MAX_PHOTOS = 30


def public_media_url(seller_id: int, filename: str) -> str:
    """Абсолютный публичный URL стандартного фото (WB забирает по нему)."""
    base = (os.environ.get('PUBLIC_BASE_URL')
            or os.environ.get('DOMAIN')
            or '').rstrip('/')
    # Добавляем схему, если указан только домен без протокола
    if base and not base.startswith('http'):
        base = 'https://' + base
    return f"{base}/media/standard/{seller_id}/{filename}"


def compose_card_photo_urls(own_urls, media_items, seller_id, min_photos):
    """Итог = [first: pin + (fill если sparse)] + own + [last: ...], дедуп, кап 30.

    Возвращает [] если добавлять нечего (итог == own).
    """
    own = list(own_urls or [])
    # «Разреженная» карточка — своих фото меньше минимума
    sparse = len(own) < (int(min_photos) if min_photos is not None else 4)
    # Фильтруем только фото (не видео и не другие типы)
    photos = [it for it in (media_items or []) if (it or {}).get('type', 'photo') == 'photo']

    def picked(position):
        """Выбирает медиа для указанной позиции с учётом режима pin/fill."""
        sel = [it for it in photos
               if it.get('position', 'last') == position
               and (it.get('mode', 'fill') == 'pin' or sparse)]
        # Сортируем по полю order для воспроизводимого порядка
        sel.sort(key=lambda it: it.get('order', 0))
        return [public_media_url(seller_id, it['filename']) for it in sel]

    first, last = picked('first'), picked('last')
    # Если ничего не добавляется — возвращаем пустой список (сигнал «без изменений»)
    if not first and not last:
        return []

    # Собираем финальный список: сначала «first», потом свои, потом «last»
    result, seen = [], set()
    for url in first + own + last:
        if url in seen:
            continue
        seen.add(url)
        result.append(url)
    # Ограничиваем 30 фото — лимит WB
    return result[:WB_MAX_PHOTOS]


def compose_card_photo_paths(own_paths, media_items, media_dir, min_photos):
    """Локальный аналог ``compose_card_photo_urls`` для multipart upload.

    В набор попадают только существующие обычные файлы внутри ``media_dir``.
    Пустой список означает, что к галерее поставщика нечего добавлять.
    """
    own = list(own_paths or [])
    sparse = len(own) < (int(min_photos) if min_photos is not None else 4)
    photos = [
        item for item in (media_items or [])
        if (item or {}).get('type', 'photo') == 'photo'
    ]
    base = os.path.realpath(os.fspath(media_dir))

    def picked(position):
        selected = [
            item for item in photos
            if item.get('position', 'last') == position
            and (item.get('mode', 'fill') == 'pin' or sparse)
        ]
        selected.sort(key=lambda item: item.get('order', 0))

        paths = []
        for item in selected:
            filename = item.get('filename')
            if not isinstance(filename, str) or os.path.basename(filename) != filename:
                continue
            candidate = os.path.realpath(os.path.join(base, filename))
            if not candidate.startswith(base + os.sep) or not os.path.isfile(candidate):
                continue
            paths.append(candidate)
        return paths

    first, last = picked('first'), picked('last')
    if not first and not last:
        return []

    result, seen = [], set()
    for path in first + own + last:
        normalized = os.path.realpath(os.fspath(path))
        if normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result[:WB_MAX_PHOTOS]
