"""Seller-scoped selection and filter helpers for the legacy WB catalog.

The catalog page and its explicit cross-page selection resolver share this
query builder so a selection can never grow beyond the filters the seller saw.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import and_, or_

from models import (
    BlockedCard,
    ImportedProduct,
    Product,
    ProductStock,
    ShadowedCard,
    Supplier,
    db,
)


MAX_BULK_PRODUCT_SELECTION = 200
SELECTION_TOKEN_MAX_AGE_SECONDS = 15 * 60
SELECTION_TOKEN_SALT = 'wb-product-selection-v1'

FILTER_KEYS = (
    'search',
    'active_only',
    'disabled_only',
    'brand',
    'category',
    'has_stock',
    'block_status',
    'rating_min',
    'rating_max',
    'quality_weak',
    'supplier_id',
)
LIST_KEYS = FILTER_KEYS + ('sort', 'order', 'page', 'per_page')
SORT_COLUMNS = {
    'updated_at': Product.updated_at,
    'created_at': Product.created_at,
    'vendor_code': Product.vendor_code,
    'title': Product.title,
    'brand': Product.brand,
    'nm_id': Product.nm_id,
    'category': Product.object_name,
    'price': Product.price,
    'supplier_price': Product.supplier_price,
    'nm_rating': Product.nm_rating,
    'quality_score': Product.quality_score,
}


class ProductSelectionError(ValueError):
    """A product selection is malformed, stale, or outside its exact scope."""


def _values(source: Any, key: str) -> list[Any]:
    if hasattr(source, 'getlist'):
        return list(source.getlist(key))
    if isinstance(source, Mapping) and key in source:
        value = source[key]
        return list(value) if isinstance(value, list) else [value]
    return []


def _single(source: Any, key: str, *, strict: bool) -> Any:
    values = _values(source, key)
    if strict and len(values) > 1:
        raise ProductSelectionError(f'Параметр {key} передан несколько раз')
    return values[0] if values else None


def _truthy(value: Any, name: str, *, strict: bool) -> bool:
    if value in (None, '', False, 0):
        return False
    normalized = str(value).strip().casefold()
    if normalized in {'1', 'true', 'on', 'yes'}:
        return True
    if normalized in {'0', 'false', 'off', 'no'}:
        return False
    if strict:
        raise ProductSelectionError(f'Некорректное значение фильтра {name}')
    return False


def _finite_float(value: Any, name: str, *, strict: bool) -> float | None:
    if value in (None, ''):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        if strict:
            raise ProductSelectionError(f'Некорректное значение фильтра {name}')
        return None
    if not math.isfinite(parsed):
        if strict:
            raise ProductSelectionError(f'Некорректное значение фильтра {name}')
        return None
    return parsed


def _positive_int(value: Any, name: str, *, strict: bool) -> int | None:
    if value in (None, ''):
        return None
    if isinstance(value, bool):
        parsed = None
    elif isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and value.isascii() and value.isdecimal():
        parsed = int(value)
    else:
        parsed = None
    if parsed is None or parsed <= 0:
        if strict:
            raise ProductSelectionError(f'Некорректное значение фильтра {name}')
        return None
    return parsed


def parse_product_list_state(
    source: Any,
    *,
    strict: bool = False,
    reject_unknown: bool = False,
) -> dict[str, Any]:
    """Parse the filters and display controls used by ``/products``.

    When ``strict`` is true, duplicate and malformed values fail closed. The
    seller-facing GET path keeps its historical forgiving behavior.
    """
    if strict and reject_unknown:
        keys = set(source.keys()) if isinstance(source, Mapping) else set()
        unknown = keys.difference(LIST_KEYS)
        if unknown:
            raise ProductSelectionError('Переданы неподдерживаемые фильтры')

    search = str(_single(source, 'search', strict=strict) or '').strip()
    if len(search) > 200:
        if strict:
            raise ProductSelectionError('Поисковая фраза слишком длинная')
        search = search[:200]

    active_only = _truthy(
        _single(source, 'active_only', strict=strict), 'active_only', strict=strict,
    )
    disabled_only = _truthy(
        _single(source, 'disabled_only', strict=strict), 'disabled_only', strict=strict,
    ) and not active_only

    brand = str(_single(source, 'brand', strict=strict) or '').strip()
    category = str(_single(source, 'category', strict=strict) or '').strip()
    has_stock = str(_single(source, 'has_stock', strict=strict) or '').strip()
    if has_stock not in {'', 'yes', 'no'}:
        if strict:
            raise ProductSelectionError('Некорректный фильтр наличия')
        has_stock = ''
    block_status = str(_single(source, 'block_status', strict=strict) or '').strip()
    if block_status not in {'', 'blocked', 'shadowed', 'ok'}:
        if strict:
            raise ProductSelectionError('Некорректный фильтр статуса карточки')
        block_status = ''

    rating_min = _finite_float(
        _single(source, 'rating_min', strict=strict), 'rating_min', strict=strict,
    )
    rating_max = _finite_float(
        _single(source, 'rating_max', strict=strict), 'rating_max', strict=strict,
    )
    quality_weak = _truthy(
        _single(source, 'quality_weak', strict=strict), 'quality_weak', strict=strict,
    )
    supplier_id = _positive_int(
        _single(source, 'supplier_id', strict=strict), 'supplier_id', strict=strict,
    )

    sort = str(_single(source, 'sort', strict=strict) or 'updated_at').strip()
    if sort not in SORT_COLUMNS:
        if strict:
            raise ProductSelectionError('Некорректная сортировка')
        sort = 'updated_at'
    order = str(_single(source, 'order', strict=strict) or 'desc').strip().lower()
    if order not in {'asc', 'desc'}:
        if strict:
            raise ProductSelectionError('Некорректное направление сортировки')
        order = 'desc'
    page = _positive_int(_single(source, 'page', strict=strict), 'page', strict=strict) or 1
    per_page = _positive_int(
        _single(source, 'per_page', strict=strict), 'per_page', strict=strict,
    ) or 50
    per_page = min(per_page, 200)

    filters = {
        'search': search,
        'active_only': active_only,
        'disabled_only': disabled_only,
        'brand': brand,
        'category': category,
        'has_stock': has_stock,
        'block_status': block_status,
        'rating_min': rating_min,
        'rating_max': rating_max,
        'quality_weak': quality_weak,
        'supplier_id': supplier_id,
    }
    return {
        **filters,
        'sort': sort,
        'order': order,
        'page': page,
        'per_page': per_page,
        'filters': filters,
    }


def build_wb_base_query(seller_id: int, *, common_read_requested: bool = False):
    """Build the same readiness-gated WB catalog source as the list route."""
    try:
        from services.marketplace_rollout import MarketplaceRolloutService

        return MarketplaceRolloutService.wb_product_query(
            seller_id=seller_id,
            common_read_requested=bool(common_read_requested),
        )
    except Exception:
        return Product.query.filter_by(seller_id=seller_id), {
            'read_mode': 'legacy_fallback',
            'common_read_requested': bool(common_read_requested),
            'cutover_ready': False,
            'blockers': ['rollout_readiness_unavailable'],
        }


def apply_product_list_filters(query, seller_id: int, filters: Mapping[str, Any]):
    """Apply canonical WB catalog filters to a seller-owned query."""
    query = query.filter(Product.seller_id == seller_id)
    if filters.get('active_only'):
        query = query.filter(Product.is_active.is_(True))
    elif filters.get('disabled_only'):
        query = query.filter(Product.is_active.is_(False))

    search = filters.get('search') or ''
    if search:
        search_filter = or_(
            Product.vendor_code.ilike(f'%{search}%'),
            Product.title.ilike(f'%{search}%'),
            Product.brand.ilike(f'%{search}%'),
            Product.nm_id.cast(db.String).ilike(f'%{search}%'),
        )
        query = query.filter(search_filter)

    # Brand/category controls are populated from distinct stored values. Exact
    # equality prevents selecting "Pipedream" from also selecting a similarly
    # named brand. Free-text search remains available through ``search``.
    if filters.get('brand'):
        query = query.filter(Product.brand == filters['brand'])
    if filters.get('category'):
        query = query.filter(Product.object_name == filters['category'])

    if filters.get('has_stock') == 'yes':
        query = query.filter(db.exists().where(and_(
            ProductStock.product_id == Product.id,
            ProductStock.quantity > 0,
        )))
    elif filters.get('has_stock') == 'no':
        query = query.filter(~db.exists().where(and_(
            ProductStock.product_id == Product.id,
            ProductStock.quantity > 0,
        )))

    block_status = filters.get('block_status')
    if block_status in {'blocked', 'shadowed', 'ok'}:
        blocked_ids = db.session.query(BlockedCard.nm_id).filter_by(
            seller_id=seller_id, is_active=True,
        ).subquery()
        shadowed_ids = db.session.query(ShadowedCard.nm_id).filter_by(
            seller_id=seller_id, is_active=True,
        ).subquery()
        if block_status == 'blocked':
            query = query.filter(Product.nm_id.in_(blocked_ids))
        elif block_status == 'shadowed':
            query = query.filter(Product.nm_id.in_(shadowed_ids))
        else:
            query = query.filter(
                ~Product.nm_id.in_(blocked_ids),
                ~Product.nm_id.in_(shadowed_ids),
            )

    if filters.get('rating_min') is not None:
        query = query.filter(Product.nm_rating >= filters['rating_min'])
    if filters.get('rating_max') is not None:
        query = query.filter(Product.nm_rating <= filters['rating_max'])
    if filters.get('quality_weak'):
        query = query.filter(
            Product.attention_reasons.isnot(None),
            Product.attention_reasons != '',
        )
    if filters.get('supplier_id') is not None:
        query = query.filter(db.exists().where(and_(
            ImportedProduct.product_id == Product.id,
            ImportedProduct.seller_id == seller_id,
            ImportedProduct.supplier_id == filters['supplier_id'],
        )))
    return query


def order_product_list_query(query, state: Mapping[str, Any]):
    column = SORT_COLUMNS.get(state.get('sort'), Product.updated_at)
    ordered = column.asc() if state.get('order') == 'asc' else column.desc()
    return query.order_by(ordered, Product.id.asc())


def build_product_list_query(
    seller_id: int,
    source: Any,
    *,
    common_read_requested: bool = False,
    strict: bool = False,
):
    state = parse_product_list_state(source, strict=strict)
    base_query, read_state = build_wb_base_query(
        seller_id, common_read_requested=common_read_requested,
    )
    filtered = apply_product_list_filters(
        base_query, seller_id, state['filters'],
    )
    return order_product_list_query(filtered, state), state, base_query, read_state


def product_filter_fingerprint(seller_id: int, filters: Mapping[str, Any]) -> str:
    payload = {
        'seller_id': int(seller_id),
        'marketplace': 'wb',
        'filters': {key: filters.get(key) for key in FILTER_KEYS},
    }
    raw = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
    ).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()


def product_list_url_args(state: Mapping[str, Any]) -> dict[str, Any]:
    """Return supported query values in a template-friendly form."""
    args: dict[str, Any] = {
        'sort': state['sort'],
        'order': state['order'],
        'per_page': state['per_page'],
    }
    filters = state['filters']
    for key in FILTER_KEYS:
        value = filters.get(key)
        if value is None or value == '' or (isinstance(value, bool) and not value):
            continue
        args[key] = '1' if isinstance(value, bool) else value
    return args


def build_products_return_url(state: Mapping[str, Any]) -> str:
    args = product_list_url_args(state)
    args['page'] = state['page']
    return '/products?' + urlencode(args)


def safe_products_return_url(value: Any) -> str:
    """Accept only a local ``/products`` URL with supported single query keys."""
    fallback = '/products'
    if not isinstance(value, str) or len(value) > 2048:
        return fallback
    if any(ord(char) < 32 or char == '\\' for char in value):
        return fallback
    try:
        parsed = urlsplit(value)
    except ValueError:
        return fallback
    if parsed.scheme or parsed.netloc or parsed.fragment or parsed.path != '/products':
        return fallback
    try:
        pairs = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        return fallback
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)) or set(keys).difference(LIST_KEYS):
        return fallback
    try:
        state = parse_product_list_state(dict(pairs), strict=True, reject_unknown=True)
    except ProductSelectionError:
        return fallback
    args = product_list_url_args(state)
    args['page'] = state['page']
    return urlunsplit(('', '', '/products', urlencode(args), ''))


def parse_selected_product_ids(raw_ids: Any, *, from_query: bool = False) -> list[int]:
    """Strictly parse a non-empty selection of at most 200 unique IDs."""
    if not isinstance(raw_ids, list) or not raw_ids:
        raise ProductSelectionError('Выберите хотя бы один товар')
    if len(raw_ids) > MAX_BULK_PRODUCT_SELECTION:
        raise ProductSelectionError(
            f'Выбрано больше {MAX_BULK_PRODUCT_SELECTION} товаров; сузьте фильтры '
            'или выберите явную партию'
        )
    ids = []
    seen = set()
    for raw in raw_ids:
        if from_query and isinstance(raw, str) and raw.isascii() and raw.isdecimal():
            product_id = int(raw)
        elif isinstance(raw, int) and not isinstance(raw, bool):
            product_id = raw
        else:
            raise ProductSelectionError('ID товара должен быть positive integer')
        if product_id <= 0:
            raise ProductSelectionError('ID товара должен быть positive integer')
        if product_id in seen:
            raise ProductSelectionError('В выборе есть повторяющиеся товары')
        seen.add(product_id)
        ids.append(product_id)
    return ids


def _serializer(secret_key: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(secret_key, salt=SELECTION_TOKEN_SALT)


def issue_product_selection_token(
    *,
    secret_key: str,
    user_id: int,
    seller_id: int,
    product_ids: list[int],
    state: Mapping[str, Any],
    return_to: Any,
    wb_account_id: Any = None,
) -> str:
    ids = parse_selected_product_ids(product_ids)
    payload = {
        'v': 1,
        'user_id': int(user_id),
        'seller_id': int(seller_id),
        'marketplace': 'wb',
        'wb_account_id': str(wb_account_id).strip() if wb_account_id else None,
        'ids': ids,
        'filters': state['filters'],
        'filter_fingerprint': product_filter_fingerprint(seller_id, state['filters']),
        'sort': state['sort'],
        'order': state['order'],
        'page': state['page'],
        'per_page': state['per_page'],
        'return_to': safe_products_return_url(return_to),
    }
    return _serializer(secret_key).dumps(payload)


def load_product_selection_token(
    token: Any,
    *,
    secret_key: str,
    user_id: int,
    seller_id: int,
    wb_account_id: Any = None,
    max_age: int = SELECTION_TOKEN_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    if not isinstance(token, str) or not token or len(token) > 8192:
        raise ProductSelectionError('Выбор товаров отсутствует или повреждён')
    try:
        payload = _serializer(secret_key).loads(token, max_age=max_age)
    except SignatureExpired as exc:
        raise ProductSelectionError('Выбор устарел; вернитесь в каталог и выберите товары снова') from exc
    except BadSignature as exc:
        raise ProductSelectionError('Подпись выбора товаров неверна') from exc
    if not isinstance(payload, dict):
        raise ProductSelectionError('Формат выбора товаров неверен')
    if (
        payload.get('v') != 1
        or payload.get('marketplace') != 'wb'
        or payload.get('user_id') != int(user_id)
        or payload.get('seller_id') != int(seller_id)
        or payload.get('wb_account_id')
        != (str(wb_account_id).strip() if wb_account_id else None)
    ):
        raise ProductSelectionError('Выбор относится к другому пользователю или продавцу')
    payload['ids'] = parse_selected_product_ids(payload.get('ids'))
    payload['return_to'] = safe_products_return_url(payload.get('return_to'))
    filters = payload.get('filters')
    if not isinstance(filters, dict) or set(filters).difference(FILTER_KEYS):
        raise ProductSelectionError('Фильтры выбора товаров повреждены')
    normalized = parse_product_list_state(
        {**filters, 'sort': payload.get('sort'), 'order': payload.get('order'),
         'page': payload.get('page'), 'per_page': payload.get('per_page')},
        strict=True,
    )
    if payload.get('filter_fingerprint') != product_filter_fingerprint(
        seller_id, normalized['filters'],
    ):
        raise ProductSelectionError('Фильтры выбора товаров изменились')
    payload.update({
        'filters': normalized['filters'],
        'sort': normalized['sort'],
        'order': normalized['order'],
        'page': normalized['page'],
        'per_page': normalized['per_page'],
    })
    return payload


def query_for_selection(base_query, seller_id: int, state: Mapping[str, Any], product_ids=None):
    query = apply_product_list_filters(base_query, seller_id, state['filters'])
    if product_ids is not None:
        query = query.filter(Product.id.in_(product_ids))
    return order_product_list_query(query, state)
