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
    sparse = len(own) < int(min_photos or 4)
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
