# -*- coding: utf-8 -*-
"""
Детерминированный Quality Score карточки (0-100).

Считает взвешенную оценку по измерениям (фото, характеристики, описание,
заголовок, бренд, штрихкоды, цена, категория) и формирует рекомендации
«как поднять». Чистые функции без БД — пригодны для unit-тестов.
"""
import json
from typing import Dict, Any, Optional

WEIGHTS = {
    'characteristics': 25,
    'photos': 20,
    'description': 15,
    'title': 10,
    'brand': 10,
    'barcodes': 10,
    'price': 5,
    'category': 5,
}


def score_status(score: float) -> str:
    if score >= 85:
        return 'excellent'
    if score >= 70:
        return 'good'
    if score >= 50:
        return 'average'
    return 'poor'


def _dim_photos(card) -> tuple:
    count = len(card.get('photos') or [])
    sub = min(100, count * 100 // 8)
    if count == 0:
        return 0, 'error', 'Нет фото — добавьте минимум 5 (до 30 на WB)'
    if count < 5:
        return sub, 'warning', f'Мало фото ({count}) — рекомендуем 8+ (до 30)'
    if count < 8:
        return sub, 'ok', f'Можно добавить фото ({count}/8+)'
    return sub, 'ok', ''


def _count_characteristics(chars) -> int:
    if isinstance(chars, dict):
        return len([k for k, v in chars.items() if not str(k).startswith('_') and v])
    if isinstance(chars, list):
        return len(chars)
    return 0


def _dim_characteristics(card) -> tuple:
    count = _count_characteristics(card.get('characteristics'))
    sub = min(100, count * 10)
    if count == 0:
        return 0, 'error', 'Заполните характеристики товара'
    if count < 3:
        return sub, 'warning', f'Мало характеристик ({count}) — WB может отклонить'
    if count < 10:
        return sub, 'ok', f'Добавьте характеристики ({count}/10)'
    return sub, 'ok', ''


def _dim_description(card) -> tuple:
    length = len(card.get('description') or '')
    sub = min(100, length * 100 // 400)
    if length == 0:
        return 0, 'error', 'Добавьте описание товара'
    if length < 200:
        return sub, 'warning', 'Короткое описание — расширьте до 400+ символов'
    return sub, 'ok', ''


def _dim_title(card) -> tuple:
    length = len(card.get('title') or '')
    if length == 0:
        return 0, 'error', 'Нет заголовка'
    if length > 60:
        return 50, 'warning', 'Заголовок длиннее 60 символов — WB обрежет'
    if length < 25:
        return min(100, length * 100 // 25), 'warning', 'Короткий заголовок — добавьте деталей'
    return 100, 'ok', ''


def _dim_brand(card) -> tuple:
    return (100, 'ok', '') if card.get('brand') else (0, 'warning', 'Не указан бренд')


def _dim_barcodes(card) -> tuple:
    return (100, 'ok', '') if (card.get('barcodes') or []) else (0, 'warning', 'Нет штрихкодов')


def _dim_price(card) -> tuple:
    price = card.get('price') or 0
    return (100, 'ok', '') if price and price > 0 else (0, 'error', 'Нет цены')


def _dim_category(card) -> tuple:
    return (100, 'ok', '') if card.get('subject_id') else (0, 'error', 'Не задана категория WB')


_DIMENSIONS = {
    'characteristics': _dim_characteristics,
    'photos': _dim_photos,
    'description': _dim_description,
    'title': _dim_title,
    'brand': _dim_brand,
    'barcodes': _dim_barcodes,
    'price': _dim_price,
    'category': _dim_category,
}


def compute_card_quality(card: Dict[str, Any]) -> Dict[str, Any]:
    dimensions = {}
    weighted_sum = 0
    rec_candidates = []  # (impact, name, hint)

    for name, fn in _DIMENSIONS.items():
        weight = WEIGHTS[name]
        sub, status, hint = fn(card)
        dimensions[name] = {'score': sub, 'status': status, 'weight': weight, 'hint': hint}
        weighted_sum += sub * weight
        if hint:
            rec_candidates.append((weight * (100 - sub), name, hint))

    score = round(weighted_sum / 100.0, 1)
    rec_candidates.sort(key=lambda t: (-t[0], t[1]))
    recommendations = [hint for _, _, hint in rec_candidates]

    return {
        'score': score,
        'status': score_status(score),
        'dimensions': dimensions,
        'recommendations': recommendations,
    }


def _loads(raw, default):
    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


def product_to_card_input(product) -> Dict[str, Any]:
    """Построить нормализованный dict из опубликованной карточки Product."""
    photos = _loads(getattr(product, 'photos_json', None), [])
    chars = _loads(getattr(product, 'characteristics_json', None), {})
    sizes = _loads(getattr(product, 'sizes_json', None), [])

    barcodes = []
    if isinstance(sizes, list):
        for sz in sizes:
            if isinstance(sz, dict):
                bc = sz.get('skus') or sz.get('barcodes') or sz.get('barcode')
                if isinstance(bc, list):
                    barcodes.extend(bc)
                elif bc:
                    barcodes.append(bc)

    price = 0
    if getattr(product, 'price', None):
        try:
            price = float(product.price)
        except (TypeError, ValueError):
            price = 0

    return {
        'photos': photos if isinstance(photos, list) else [],
        'characteristics': chars,
        'title': getattr(product, 'title', '') or '',
        'description': getattr(product, 'description', '') or '',
        'brand': getattr(product, 'brand', '') or '',
        'barcodes': barcodes,
        'price': price,
        'subject_id': getattr(product, 'subject_id', None),
    }


def card_quality_detail(product) -> Dict[str, Any]:
    """Полный payload карточки для UI: WB-рейтинг + Quality Score + рекомендации."""
    cq = compute_card_quality(product_to_card_input(product))
    checked = getattr(product, 'nm_rating_checked_at', None)
    return {
        'product_id': getattr(product, 'id', None),
        'nm_id': getattr(product, 'nm_id', None),
        'vendor_code': getattr(product, 'vendor_code', None),
        'title': getattr(product, 'title', None),
        'wb_product_rating': getattr(product, 'nm_rating', None),       # 0-10
        'wb_feedback_rating': getattr(product, 'wb_feedback_rating', None),  # 0-5
        'nm_rating_checked_at': checked.isoformat() if checked else None,
        'quality_score': cq['score'],
        'quality_status': cq['status'],
        'dimensions': cq['dimensions'],
        'recommendations': cq['recommendations'],
    }


WEAK_QUALITY_THRESHOLD = 50.0
WEAK_WB_RATING_THRESHOLD = 6.0


def is_weak(quality_score: Optional[float], nm_rating: Optional[float]) -> bool:
    """Карточка «слабая», если Quality Score < 50 ИЛИ WB-рейтинг карточки < 6.

    None-значения игнорируются (не делают карточку слабой сами по себе).
    """
    if quality_score is not None and quality_score < WEAK_QUALITY_THRESHOLD:
        return True
    if nm_rating is not None and nm_rating < WEAK_WB_RATING_THRESHOLD:
        return True
    return False


def compute_quality_summary(seller_id: int) -> Dict[str, Any]:
    """Сводка по качеству карточек продавца для кокпита.

    distribution — бакеты по score_status(quality_score): poor/average/good/excellent.
    need_attention — число «слабых» карточек по is_weak(quality_score, nm_rating).
    """
    from models import db, Product

    rows = db.session.query(Product.quality_score, Product.nm_rating).filter(
        Product.seller_id == seller_id,
        Product.is_active == True,  # noqa: E712
    ).all()

    distribution = {'poor': 0, 'average': 0, 'good': 0, 'excellent': 0}
    total = len(rows)
    need_attention = 0
    q_sum = 0.0
    q_cnt = 0
    r_sum = 0.0
    r_cnt = 0

    for quality_score, nm_rating in rows:
        if quality_score is not None:
            distribution[score_status(quality_score)] += 1
            q_sum += quality_score
            q_cnt += 1
        if nm_rating is not None:
            r_sum += nm_rating
            r_cnt += 1
        if is_weak(quality_score, nm_rating):
            need_attention += 1

    return {
        'avg_quality': round(q_sum / q_cnt, 1) if q_cnt else None,
        'avg_wb_rating': round(r_sum / r_cnt, 1) if r_cnt else None,
        'total': total,
        'need_attention': need_attention,
        'distribution': distribution,
    }
