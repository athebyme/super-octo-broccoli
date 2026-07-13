# -*- coding: utf-8 -*-
"""
Детерминированный Quality Score карточки (0-100).

Считает взвешенную оценку по измерениям (фото, характеристики, описание,
заголовок, бренд, штрихкоды, цена, категория) и формирует рекомендации
«как поднять». Чистые функции без БД — пригодны для unit-тестов.
"""
import json
import re
from datetime import datetime
from typing import Dict, Any

WEIGHTS = {
    'characteristics': 25,
    'photos': 20,
    'description': 20,
    'title': 15,
    'brand': 8,
    'barcodes': 6,
    'price': 3,
    'category': 3,
}


def score_status(score: float) -> str:
    if score >= 85:
        return 'excellent'
    if score >= 70:
        return 'good'
    if score >= 50:
        return 'average'
    return 'poor'


_WORD_RE = re.compile(r'[а-яёa-z0-9]+')


def _significant_words(text: str) -> list:
    """Значимые слова: ≥3 символов, не чисто цифровые, lower-case."""
    return [w for w in _WORD_RE.findall((text or '').lower())
            if len(w) >= 3 and not w.isdigit()]


def _dim_photos(card) -> tuple:
    count = len(card.get('photos') or [])
    if count == 0:
        return 0, 'error', 'Нет фото — добавьте минимум 5 (до 30 на WB)'
    sub = min(100, count * 10)
    if count < 5:
        return min(sub, 40), 'warning', f'Мало фото ({count}) — минимум 5, рекомендуем 10+'
    if count < 10:
        return sub, 'ok', f'Можно добавить фото ({count}/10)'
    return 100, 'ok', ''


def _count_characteristics(chars) -> int:
    if isinstance(chars, dict):
        return len([k for k, v in chars.items() if not str(k).startswith('_') and v])
    if isinstance(chars, list):
        return len(chars)
    return 0


def _norm_char_name(name) -> str:
    return str(name or '').strip().lower()


def _filled_char_names(chars) -> set:
    names = set()
    if isinstance(chars, dict):
        for k, v in chars.items():
            if not str(k).startswith('_') and v:
                names.add(_norm_char_name(k))
    elif isinstance(chars, list):
        for item in chars:
            if isinstance(item, dict):
                nm = item.get('name') or item.get('charcName')
                if nm and (item.get('value') or item.get('values')):
                    names.add(_norm_char_name(nm))
    return names


def _dim_characteristics(card) -> tuple:
    available = card.get('available_charcs')
    count = _count_characteristics(card.get('characteristics'))
    if not available:
        # Fallback без конфига категории: полноту подтвердить нельзя — потолок 70
        sub = min(70, count * 10)
        if count == 0:
            return 0, 'error', 'Заполните характеристики товара'
        if count < 3:
            return sub, 'warning', f'Мало характеристик ({count}) — WB может отклонить'
        return sub, 'ok', f'Заполнено {count}; для точной оценки нужен конфиг категории'
    filled = _filled_char_names(card.get('characteristics'))
    total_w = filled_w = total_n = filled_n = 0
    for ch in available:
        name = _norm_char_name(ch.get('name'))
        if not name:
            continue
        w = 3 if ch.get('required') else 1
        total_w += w
        total_n += 1
        if name in filled:
            filled_w += w
            filled_n += 1
    if total_w == 0:
        return (100, 'ok', '') if count else (0, 'error', 'Заполните характеристики товара')
    sub = round(100 * filled_w / total_w)
    if filled_n == 0:
        return 0, 'error', f'Заполните характеристики (0 из {total_n})'
    if sub < 60:
        return sub, 'warning', f'Заполнено {filled_n} из {total_n} характеристик категории'
    if sub < 100:
        return sub, 'ok', f'Можно заполнить ещё ({filled_n}/{total_n})'
    return sub, 'ok', ''


def _dim_description(card) -> tuple:
    text = card.get('description') or ''
    length = len(text)
    if length == 0:
        return 0, 'error', 'Добавьте описание товара'
    sub = min(100, length * 100 // 600)
    hints = []
    if card.get('description_dup'):
        sub = int(sub * 0.4)
        hints.append('Описание дублируется у нескольких карточек — сделайте уникальным')
    if len(set(_significant_words(text))) < 15:
        sub = int(sub * 0.6)
        hints.append('Описание малосодержательное — добавьте конкретики')
    if length < 300:
        hints.append('Короткое описание — расширьте до 600+ символов')
    if hints:
        return sub, 'warning', '; '.join(hints)
    return sub, 'ok', ('' if sub >= 100 else 'Можно расширить описание до 600+ символов')


def _dim_title(card) -> tuple:
    title = card.get('title') or ''
    length = len(title)
    if length == 0:
        return 0, 'error', 'Нет заголовка'
    if length > 60:
        return 50, 'warning', 'Заголовок длиннее 60 символов — WB обрежет'
    sub = 100 if length >= 25 else min(100, length * 100 // 25)
    hints = [] if length >= 25 else ['Короткий заголовок — добавьте деталей']
    words = _significant_words(title)
    if len(words) < 4:
        sub -= 30
        hints.append('Мало значимых слов в заголовке (нужно 4+)')
    counts = {}
    for w in words:
        counts[w] = counts.get(w, 0) + 1
    if counts and max(counts.values()) >= 3:
        sub -= 20
        hints.append('Слово повторяется 3+ раз — уберите спам')
    letters = [c for c in title if c.isalpha()]
    if len(letters) >= 10 and all(c.isupper() for c in letters):
        sub -= 20
        hints.append('Заголовок капсом — снижает доверие')
    sub = max(0, sub)
    if hints:
        return sub, 'warning', '; '.join(hints)
    return sub, 'ok', ''


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


def product_to_card_input(product, context=None) -> Dict[str, Any]:
    """Построить нормализованный dict из опубликованной карточки Product.

    context (см. build_seller_scoring_context) — опционально; если передан,
    добавляет description_dup и available_charcs. Без context поведение
    прежнее (обратная совместимость).
    """
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

    card = {
        'photos': photos if isinstance(photos, list) else [],
        'characteristics': chars,
        'title': getattr(product, 'title', '') or '',
        'description': getattr(product, 'description', '') or '',
        'brand': getattr(product, 'brand', '') or '',
        'barcodes': barcodes,
        'price': price,
        'subject_id': getattr(product, 'subject_id', None),
    }

    if context:
        desc = (getattr(product, 'description', '') or '').strip()
        card['description_dup'] = bool(
            desc and desc in (context.get('dup_descriptions') or ()))
        card['available_charcs'] = (context.get('charcs_by_subject') or {}).get(
            getattr(product, 'subject_id', None))
    return card


DUP_DESCRIPTION_MIN_LEN = 50
DUP_DESCRIPTION_MIN_COUNT = 3


def build_seller_scoring_context(seller_id) -> Dict[str, Any]:
    """Контекст скоринга по каталогу продавца: дубликаты описаний + конфиги категорий.

    Один проход по (description, subject_id) активных карточек; конфиги — из кэша.
    """
    from models import db, Product
    from services.subject_charcs_cache import get_available_charcs

    rows = db.session.query(Product.description, Product.subject_id).filter(
        Product.seller_id == seller_id,
        Product.is_active == True,  # noqa: E712
    ).all()
    counter = {}
    subjects = set()
    for desc, subj in rows:
        if desc:
            key = desc.strip()
            if len(key) >= DUP_DESCRIPTION_MIN_LEN:
                counter[key] = counter.get(key, 0) + 1
        if subj:
            subjects.add(subj)
    dups = {k for k, v in counter.items() if v >= DUP_DESCRIPTION_MIN_COUNT}
    charcs = {s: get_available_charcs(s) for s in subjects}
    return {'dup_descriptions': dups, 'charcs_by_subject': charcs}


def _persisted_recommendations(dimensions: Dict[str, Any]) -> list:
    """Пересобрать recommendations из persisted breakdown той же сортировкой,
    что compute_card_quality: weight*(100-score) убыв., затем имя измерения."""
    rec_candidates = []
    for name, dim in dimensions.items():
        if not isinstance(dim, dict):
            continue
        hint = dim.get('hint')
        if hint:
            weight = dim.get('weight') or 0
            sub = dim.get('score') or 0
            rec_candidates.append((weight * (100 - sub), name, hint))
    rec_candidates.sort(key=lambda t: (-t[0], t[1]))
    return [hint for _, _, hint in rec_candidates]


def card_quality_detail(product) -> Dict[str, Any]:
    """Полный payload карточки для UI: WB-рейтинг + Quality Score + рекомендации.

    Показанный score обязан совпадать с персистентным v2 (product.quality_score),
    по которому работают фильтры/бакеты списка. Если он ещё не посчитан или
    breakdown повреждён — используется прежний live fallback (без category
    schema context, с потолком 70 по характеристикам).
    """
    card_input = product_to_card_input(product)
    photos = card_input.get('photos') or []

    persisted_score = getattr(product, 'quality_score', None)
    persisted_breakdown = None
    if persisted_score is not None:
        parsed = _loads(getattr(product, 'quality_breakdown_json', None), None)
        if isinstance(parsed, dict) and parsed:
            persisted_breakdown = parsed

    if persisted_breakdown is not None:
        score = persisted_score
        status = score_status(score)
        dimensions = persisted_breakdown
        recommendations = _persisted_recommendations(dimensions)
    else:
        cq = compute_card_quality(card_input)
        score = cq['score']
        status = cq['status']
        dimensions = cq['dimensions']
        recommendations = cq['recommendations']

    checked = getattr(product, 'nm_rating_checked_at', None)
    return {
        'product_id': getattr(product, 'id', None),
        'nm_id': getattr(product, 'nm_id', None),
        'vendor_code': getattr(product, 'vendor_code', None),
        'title': getattr(product, 'title', None),
        'wb_product_rating': getattr(product, 'nm_rating', None),       # 0-10
        'wb_feedback_rating': getattr(product, 'wb_feedback_rating', None),  # 0-5
        'nm_rating_checked_at': checked.isoformat() if checked else None,
        'photos': photos,
        'quality_score': score,
        'quality_status': status,
        'dimensions': dimensions,
        'recommendations': recommendations,
        'attention_reasons': [r for r in (getattr(product, 'attention_reasons', None) or '').split(',') if r],
        'quality_impact': getattr(product, 'quality_impact', None),
        'wb_views_30d': getattr(product, 'wb_views_30d', None),
        'wb_orders_30d': getattr(product, 'wb_orders_30d', None),
        'wb_cart_conv': getattr(product, 'wb_cart_conv', None),
        'wb_buyout_rate': getattr(product, 'wb_buyout_rate', None),
    }


def compute_quality_summary(seller_id: int) -> Dict[str, Any]:
    """Сводка по качеству карточек продавца для кокпита и виджета дашборда.

    distribution — бакеты по score_status(quality_score);
    need_attention — карточки с непустым attention_reasons;
    reason_counts — счётчик карточек по каждой причине;
    trend — средний quality_score по дням за 30 дней (CardRatingHistory).
    """
    from datetime import timedelta
    from sqlalchemy import func
    from models import db, Product, CardRatingHistory

    rows = db.session.query(
        Product.quality_score, Product.nm_rating, Product.attention_reasons,
    ).filter(
        Product.seller_id == seller_id,
        Product.is_active == True,  # noqa: E712
    ).all()

    distribution = {'poor': 0, 'average': 0, 'good': 0, 'excellent': 0}
    reason_counts = {r: 0 for r in ATTENTION_REASONS}
    total = len(rows)
    need_attention = 0
    q_sum = q_cnt = r_sum = r_cnt = 0

    for quality_score, nm_rating, reasons_csv in rows:
        if quality_score is not None:
            distribution[score_status(quality_score)] += 1
            q_sum += quality_score
            q_cnt += 1
        if nm_rating is not None:
            r_sum += nm_rating
            r_cnt += 1
        reasons = [r for r in (reasons_csv or '').split(',') if r]
        if reasons:
            need_attention += 1
            for r in reasons:
                if r in reason_counts:
                    reason_counts[r] += 1

    since = datetime.utcnow() - timedelta(days=30)
    trend_rows = db.session.query(
        func.date(CardRatingHistory.captured_at),
        func.avg(CardRatingHistory.quality_score),
    ).filter(
        CardRatingHistory.seller_id == seller_id,
        CardRatingHistory.captured_at >= since,
        CardRatingHistory.quality_score.isnot(None),
    ).group_by(func.date(CardRatingHistory.captured_at)) \
     .order_by(func.date(CardRatingHistory.captured_at)).all()
    trend = [{'date': str(d), 'avg_quality': round(v, 1)}
             for d, v in trend_rows if v is not None]

    return {
        'avg_quality': round(q_sum / q_cnt, 1) if q_cnt else None,
        'avg_wb_rating': round(r_sum / r_cnt, 1) if r_cnt else None,
        'total': total,
        'need_attention': need_attention,
        'distribution': distribution,
        'reason_counts': reason_counts,
        'reason_labels': REASON_LABELS,
        'trend': trend,
    }


# ── Причины «требует внимания» и приоритет фикса ──────────────────────

ATTENTION_REASONS = (
    'few_photos', 'weak_chars', 'weak_description', 'weak_title',
    'no_views', 'low_cart_conv', 'low_buyout', 'low_rating', 'no_sales_signal',
)

REASON_LABELS = {
    'few_photos': 'Мало фото',
    'weak_chars': 'Слабые характеристики',
    'weak_description': 'Слабое описание',
    'weak_title': 'Слабый заголовок',
    'no_views': 'Нет просмотров',
    'low_cart_conv': 'Не кладут в корзину',
    'low_buyout': 'Низкий выкуп',
    'low_rating': 'Низкий рейтинг',
    'no_sales_signal': 'Нет данных о продажах',
}

NO_VIEWS_THRESHOLD = 30
LOW_CART_CONV_MIN_VIEWS = 100
LOW_CART_CONV_THRESHOLD = 4.0
LOW_BUYOUT_MIN_ORDERS = 5
LOW_BUYOUT_THRESHOLD = 30.0
LOW_NM_RATING = 6.0
LOW_FEEDBACK_RATING = 4.0

_REASON_BOOSTS = {'no_views': 10.0, 'low_cart_conv': 8.0, 'low_buyout': 5.0, 'low_rating': 5.0}
_WEAK_DIM_SUB = 60


def compute_attention(card, dimensions, nm_rating=None, feedback_rating=None,
                      views_30d=None, orders_30d=None, cart_conv=None,
                      buyout_rate=None) -> Dict[str, Any]:
    """Причины «требует внимания» и impact (потенциал фикса) карточки.

    None-значения поведенческих метрик означают «нет данных» и не создают
    причин (кроме no_sales_signal: нет ни рейтинга, ни просмотров).
    """
    reasons = []
    if len(card.get('photos') or []) < 5:
        reasons.append('few_photos')
    if dimensions['characteristics']['score'] < _WEAK_DIM_SUB:
        reasons.append('weak_chars')
    if dimensions['description']['score'] < _WEAK_DIM_SUB:
        reasons.append('weak_description')
    if dimensions['title']['score'] < _WEAK_DIM_SUB:
        reasons.append('weak_title')
    if views_30d is not None and views_30d < NO_VIEWS_THRESHOLD:
        reasons.append('no_views')
    if (views_30d is not None and cart_conv is not None
            and views_30d >= LOW_CART_CONV_MIN_VIEWS
            and cart_conv < LOW_CART_CONV_THRESHOLD):
        reasons.append('low_cart_conv')
    if (orders_30d is not None and buyout_rate is not None
            and orders_30d >= LOW_BUYOUT_MIN_ORDERS
            and buyout_rate < LOW_BUYOUT_THRESHOLD):
        reasons.append('low_buyout')
    if ((nm_rating is not None and nm_rating < LOW_NM_RATING)
            or (feedback_rating is not None and feedback_rating < LOW_FEEDBACK_RATING)):
        reasons.append('low_rating')
    if nm_rating is None and not views_30d:
        reasons.append('no_sales_signal')

    impact = sum(d['weight'] * (100 - d['score']) / 100.0 for d in dimensions.values())
    impact += sum(_REASON_BOOSTS.get(r, 0.0) for r in reasons)
    return {'reasons': reasons, 'impact': round(impact, 1)}


def recompute_and_persist(product, capture_history: bool = True,
                          context=None) -> Dict[str, Any]:
    """Пересчитать Quality Score v2 карточки и записать в Product.

    Выставляет quality_score, quality_breakdown_json, attention_reasons (CSV),
    quality_impact, quality_checked_at. context=None → строится по продавцу
    (полный проход по каталогу; для батчей передавайте готовый context).
    НЕ делает commit — коммитит вызывающий код.
    """
    from models import db, CardRatingHistory

    if context is None and getattr(product, 'seller_id', None):
        context = build_seller_scoring_context(product.seller_id)

    card = product_to_card_input(product, context)
    cq = compute_card_quality(card)
    att = compute_attention(
        card, cq['dimensions'],
        nm_rating=getattr(product, 'nm_rating', None),
        feedback_rating=getattr(product, 'wb_feedback_rating', None),
        views_30d=getattr(product, 'wb_views_30d', None),
        orders_30d=getattr(product, 'wb_orders_30d', None),
        cart_conv=getattr(product, 'wb_cart_conv', None),
        buyout_rate=getattr(product, 'wb_buyout_rate', None),
    )

    product.quality_score = cq['score']
    product.quality_breakdown_json = json.dumps(cq['dimensions'], ensure_ascii=False)
    product.attention_reasons = ','.join(att['reasons'])
    product.quality_impact = att['impact']
    product.quality_checked_at = datetime.utcnow()

    if capture_history:
        db.session.add(CardRatingHistory(
            seller_id=getattr(product, 'seller_id', None),
            product_id=getattr(product, 'id', None),
            nm_id=getattr(product, 'nm_id', None),
            wb_product_rating=getattr(product, 'nm_rating', None),
            wb_feedback_rating=getattr(product, 'wb_feedback_rating', None),
            quality_score=cq['score'],
        ))

    cq['reasons'] = att['reasons']
    cq['impact'] = att['impact']
    return cq
