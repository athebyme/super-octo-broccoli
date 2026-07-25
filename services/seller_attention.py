# -*- coding: utf-8 -*-
"""Сводка «что требует внимания» для главной страницы продавца.

Дашборд показывал общие числа каталога, но не отвечал на главный вопрос дня:
чем заняться прямо сейчас. Здесь собираются сигналы, у каждого из которых есть
понятное следствие и конкретный переход — иначе это просто ещё одна цифра.

Сервис только считает и только в пределах одного продавца: никаких вызовов
маркетплейсов и никаких записей.
"""

from typing import Any, Dict, List

from models import (
    ImportedProduct,
    MarketplaceListing,
    PriceChangeBatch,
    Product,
    SupplierProduct,
    db,
)

# Порог, ниже которого считаем, что карточку почти не видят покупатели.
LOW_VIEWS_THRESHOLD = 30


class SellerAttentionService:
    """Считает сигналы для главной: что мешает продавать и что можно сделать."""

    @classmethod
    def summary(cls, *, seller_id: int) -> Dict[str, Any]:
        signals: List[Dict[str, Any]] = []

        # 1. Спрос есть, продаж нет — самая дорогая проблема
        losing = db.session.query(db.func.count(Product.id)).filter(
            Product.seller_id == seller_id,
            Product.is_active.is_(True),
            Product.wb_views_30d >= LOW_VIEWS_THRESHOLD,
            db.or_(Product.wb_orders_30d == 0, Product.wb_orders_30d.is_(None)),
        ).scalar() or 0
        if losing:
            signals.append({
                'key': 'losing_sales',
                'tone': 'danger',
                'count': losing,
                'title': 'Смотрят, но не покупают',
                'text': 'Покупатели заходят в карточку и уходят — обычно мешает контент или цена.',
                'action': 'Разобрать карточки',
                'href': '/card-quality/beta',
            })

        # 2. Карточки без единой фотографии — не продаются в принципе
        no_photos = db.session.query(db.func.count(ImportedProduct.id)).filter(
            ImportedProduct.seller_id == seller_id,
            db.or_(
                ImportedProduct.photo_urls.is_(None),
                ImportedProduct.photo_urls == '',
                ImportedProduct.photo_urls == '[]',
            ),
        ).scalar() or 0
        if no_photos:
            signals.append({
                'key': 'no_photos',
                'tone': 'warn',
                'count': no_photos,
                'title': 'Товары без фотографий',
                'text': 'Без фото карточку не пропустит модерация и не купит покупатель.',
                'action': 'Показать товары',
                'href': '/my-products/beta?photos=no',
            })

        # 3. Поставщик обновил данные — их можно подтянуть одним действием
        supplier_updates = db.session.query(
            db.func.count(ImportedProduct.id)
        ).join(
            SupplierProduct,
            SupplierProduct.id == ImportedProduct.supplier_product_id,
        ).filter(
            ImportedProduct.seller_id == seller_id,
            SupplierProduct.content_revision
            > ImportedProduct.supplier_content_revision,
        ).scalar() or 0
        if supplier_updates:
            signals.append({
                'key': 'supplier_updates',
                'tone': 'info',
                'count': supplier_updates,
                'title': 'Новые данные у поставщика',
                'text': 'Описания, характеристики или фото изменились — можно обновить свои карточки.',
                'action': 'Обновить карточки',
                'href': '/my-products/beta?tab=updates',
            })

        # 4. Готовы к публикации, но лежат
        ready = db.session.query(db.func.count(ImportedProduct.id)).filter(
            ImportedProduct.seller_id == seller_id,
            ImportedProduct.import_status == 'validated',
        ).scalar() or 0
        if ready:
            signals.append({
                'key': 'ready_to_publish',
                'tone': 'ok',
                'count': ready,
                'title': 'Готовы к публикации',
                'text': 'Карточки проверены и ждут отправки на маркетплейс.',
                'action': 'Отправить',
                'href': '/my-products/beta?tab=validated',
            })

        # 5. Карточки маркетплейса с ошибкой — их не видит покупатель
        listing_errors = db.session.query(
            db.func.count(MarketplaceListing.id)
        ).filter(
            MarketplaceListing.seller_id == seller_id,
            MarketplaceListing.normalized_status == 'error',
            MarketplaceListing.is_available.is_(True),
        ).scalar() or 0
        if listing_errors:
            signals.append({
                'key': 'listing_errors',
                'tone': 'danger',
                'count': listing_errors,
                'title': 'Карточки с ошибкой на площадке',
                'text': 'Площадка отклонила карточку — товар не продаётся, пока ошибка не исправлена.',
                'action': 'Посмотреть',
                'href': '/marketplaces/listings/beta?status=error',
            })

        # 6. Цены ждут подтверждения или зависли частично применёнными
        price_waiting = db.session.query(
            db.func.count(PriceChangeBatch.id)
        ).filter(
            PriceChangeBatch.seller_id == seller_id,
            PriceChangeBatch.status.in_(('submitted', 'applying')),
        ).scalar() or 0
        if price_waiting:
            signals.append({
                'key': 'prices_waiting',
                'tone': 'info',
                'count': price_waiting,
                'title': 'Цены ждут подтверждения',
                'text': 'Wildberries ещё обрабатывает отправленные цены — проверим и покажем результат.',
                'action': 'Открыть',
                'href': '/prices/',
            })

        price_partial = db.session.query(
            db.func.count(PriceChangeBatch.id)
        ).filter(
            PriceChangeBatch.seller_id == seller_id,
            PriceChangeBatch.status == 'partially_applied',
        ).scalar() or 0
        if price_partial:
            signals.append({
                'key': 'prices_partial',
                'tone': 'warn',
                'count': price_partial,
                'title': 'Цены применились не полностью',
                'text': 'Часть позиций площадка отклонила — их можно отправить повторно.',
                'action': 'Доотправить',
                'href': '/prices/',
            })

        # Порядок: сначала то, что стоит денег прямо сейчас
        tone_order = {'danger': 0, 'warn': 1, 'info': 2, 'ok': 3}
        signals.sort(key=lambda s: (tone_order.get(s['tone'], 9), -s['count']))
        return {
            'signals': signals,
            'total_signals': len(signals),
        }
