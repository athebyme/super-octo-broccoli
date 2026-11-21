# -*- coding: utf-8 -*-
"""
Маршруты для управления поставщиками (админ) и каталога (продавец)
"""
import json
import re
import logging
import secrets
from datetime import datetime
from functools import wraps
from urllib.parse import urlsplit

from flask import (
    Blueprint, render_template, redirect, url_for, flash,
    request, abort, jsonify, current_app, session, make_response
)
from flask_login import login_required, current_user
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from models import (
    db, Supplier, SupplierProduct, SellerSupplier,
    ImportedProduct, Seller, AIHistory, log_admin_action, Product,
    BackgroundJob, Notification, AgentChangeSnapshot, Marketplace,
    SellerMarketplaceAccount, MarketplaceListing, MarketplaceProductDraft,
)
from services.supplier_service import (
    SupplierRefreshReviewStale,
    SupplierService,
    supplier_refresh_product_label,
)

logger = logging.getLogger(__name__)

MAX_BULK_PRODUCTS = 200
SUPPLIER_IMPORT_BATCH_TOKEN_SALT = 'supplier-import-workspace-v1'
SUPPLIER_IMPORT_BATCH_MAX_AGE_SECONDS = 60 * 60
SUPPLIER_IMPORT_BATCH_MAX_TOKEN_BYTES = 8 * 1024
SUPPLIER_REFRESH_RECEIPT_TOKEN_SALT = 'supplier-refresh-result-receipt-v1'
SUPPLIER_REFRESH_RECEIPT_MAX_AGE_SECONDS = 60 * 60
SUPPLIER_REFRESH_RECEIPT_MAX_TOKEN_BYTES = 7_500
SUPPLIER_REFRESH_RECEIPT_HEADER = 'X-Supplier-Refresh-Receipt'
SUPPLIER_REFRESH_REVIEW_TOKEN_SALT = 'supplier-refresh-review-v1'
SUPPLIER_REFRESH_REVIEW_MAX_AGE_SECONDS = 10 * 60
SUPPLIER_REFRESH_REVIEW_MAX_TOKEN_BYTES = 8 * 1024
SUPPLIER_REFRESH_REVIEW_MAX_BODY_BYTES = 256 * 1024
SUPPLIER_REFRESH_REVIEW_MAX_RESPONSE_BYTES = 512 * 1024
SUPPLIER_CATALOG_UPDATE_REVIEW_TOKEN_SALT = 'supplier-catalog-update-review-v1'
SUPPLIER_CATALOG_UPDATE_REVIEW_MAX_AGE_SECONDS = 10 * 60
SUPPLIER_CATALOG_UPDATE_REVIEW_MAX_TOKEN_BYTES = 8 * 1024
SUPPLIER_REFRESH_STATUS_REASONS = {
    'updated': None,
    'skipped': 'no_supplier_link',
    'error': 'update_failed',
}
MAX_SIGNED_SQLITE_ID = (1 << 63) - 1
CATALOG_REFRESH_ACTIVE_STATES = frozenset({'pending', 'running'})
CATALOG_REFRESH_TERMINAL_STATES = frozenset({
    'completed', 'partial', 'failed', 'interrupted',
})
CATALOG_REFRESH_UUID_RE = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
)
CATALOG_REFRESH_PRICE_LABELS = {
    'success': 'Цены и остатки обновлены.',
    'failed': 'Не удалось обновить цены и остатки.',
    'partial': 'Цены и остатки обновлены частично.',
    'skipped': 'Проверка цен и остатков пропущена.',
    'not_configured': 'Файл цен и остатков не настроен.',
    'busy': 'Проверка цен и остатков уже выполнялась.',
    'source_changed': 'Источник цен изменился; новые значения не применены.',
    'unknown': 'Результат проверки цен и остатков неизвестен.',
}
CATALOG_REFRESH_ERROR_COPY = {
    'configuration_changed': 'Настройки источника изменились после постановки в очередь. Запустите обновление ещё раз.',
    'source_changed': 'Источник изменился во время загрузки. Проверьте настройки и запустите обновление снова.',
    'catalog_fetch_failed': 'Не удалось загрузить файл каталога. Проверьте адрес и доступность источника.',
    'catalog_validation_failed': 'Файл каталога не прошёл проверку структуры. Исправьте файл и повторите запуск.',
    'admin_not_authorized': 'Право администратора изменилось; сохранение каталога не подтверждено.',
}
CATALOG_REFRESH_COPY = {
    'idle': {
        'headline': 'Нет текущего обновления.',
        'detail': 'Можно запустить обновление каталога.',
    },
    'pending': {
        'headline': 'Обновление поставлено в очередь.',
        'detail': 'Каталог ещё не сохранён.',
    },
    'running_before': {
        'headline': 'Загружается каталог поставщика.',
        'detail': 'Счётчики появятся после сохранения файла.',
    },
    'running_after': {
        'headline': 'Каталог сохранён.',
        'detail': 'Выполняются дополнительные проверки.',
    },
    'completed': {
        'headline': 'Каталог обновлён.',
        'detail': 'Основные и дополнительные проверки завершены.',
    },
    'partial': {
        'headline': 'Каталог обновлён частично.',
        'detail': 'Проверьте результат дополнительных проверок.',
    },
    'failed': {
        'headline': 'Каталог не обновлён.',
        'detail': 'Проверьте источник и запустите обновление снова.',
    },
    'interrupted_before': {
        'headline': 'Обновление прервано.',
        'detail': 'Сохранение каталога не подтверждено.',
    },
    'interrupted_after': {
        'headline': 'Каталог сохранён.',
        'detail': 'Фоновые проверки прервались; проверьте результат.',
    },
    'unknown': {
        'headline': 'Не удалось подтвердить состояние.',
        'detail': 'Обновите страницу, чтобы проверить его снова.',
    },
    'session': {
        'headline': 'Сеанс администратора завершён.',
        'detail': 'Войдите снова, чтобы проверить состояние.',
    },
}


def _supplier_catalog_refresh_view(payload, *, lookup_failed=False):
    """Prepare safe, count-aware copy for the admin catalog page."""
    if lookup_failed:
        return {
            'state': 'unknown', 'run_id': '', 'raw_committed': False,
            'active': False, 'copy_key': 'unknown',
            'headline': CATALOG_REFRESH_COPY['unknown']['headline'],
            'detail': CATALOG_REFRESH_COPY['unknown']['detail'],
            'show_counts': False, 'counts': {}, 'postprocess_errors': None,
            'price_status': None, 'price_label': None, 'price_conflicts': None,
            'error_detail': None,
        }
    if payload is None:
        return {
            'state': 'idle', 'run_id': '', 'raw_committed': False,
            'active': False, 'copy_key': 'idle',
            'headline': CATALOG_REFRESH_COPY['idle']['headline'],
            'detail': CATALOG_REFRESH_COPY['idle']['detail'],
            'show_counts': False, 'counts': {}, 'postprocess_errors': None,
            'price_status': None, 'price_label': None, 'price_conflicts': None,
            'error_detail': None,
        }

    status = payload.get('status')
    if status not in CATALOG_REFRESH_ACTIVE_STATES | CATALOG_REFRESH_TERMINAL_STATES:
        return _supplier_catalog_refresh_view(None, lookup_failed=True)
    raw_committed = bool(payload.get('raw_committed_at'))
    if status == 'running':
        copy_key = 'running_after' if raw_committed else 'running_before'
    elif status == 'interrupted':
        copy_key = 'interrupted_after' if raw_committed else 'interrupted_before'
    else:
        copy_key = status
    copy = CATALOG_REFRESH_COPY[copy_key]

    def safe_count(value):
        return value if type(value) is int and value >= 0 else None

    counts = {
        key: safe_count(payload.get(key))
        for key in ('total', 'added', 'updated')
    }
    show_counts = raw_committed and all(value is not None for value in counts.values())
    postprocess_errors = (
        safe_count(payload.get('postprocess_errors'))
        if raw_committed and status in CATALOG_REFRESH_TERMINAL_STATES else None
    )
    price_status = payload.get('price_status')
    if (
        not raw_committed
        or status not in CATALOG_REFRESH_TERMINAL_STATES
        or price_status not in CATALOG_REFRESH_PRICE_LABELS
    ):
        price_status = None
    price_conflicts = (
        safe_count(payload.get('price_conflicts'))
        if price_status is not None else None
    )
    error_detail = (
        CATALOG_REFRESH_ERROR_COPY.get(payload.get('error_code'))
        if status in {'failed', 'partial', 'interrupted'} else None
    )
    return {
        'state': status,
        'run_id': payload.get('id') or '',
        'raw_committed': raw_committed,
        'active': status in CATALOG_REFRESH_ACTIVE_STATES,
        'copy_key': copy_key,
        'headline': copy['headline'],
        'detail': error_detail or copy['detail'],
        'show_counts': show_counts,
        'counts': counts,
        'postprocess_errors': postprocess_errors,
        'price_status': price_status,
        'price_label': CATALOG_REFRESH_PRICE_LABELS.get(price_status),
        'price_conflicts': price_conflicts,
        'error_detail': error_detail,
    }


def _bulk_product_ids(raw_ids, limit=MAX_BULK_PRODUCTS):
    """Строгий разбор product_ids для массовых действий.

    Инвариант проекта: один запрос — не больше 200 уникальных positive integer
    ID. Bool/float/строки и дубли отклоняют весь запрос, а не фильтруются
    молча: иначе продавец думает, что действие применилось ко всей выборке.
    """
    if not isinstance(raw_ids, list) or not raw_ids:
        raise ValueError('Не выбраны товары')
    if len(raw_ids) > limit:
        raise ValueError(
            f'За один раз можно выбрать не более {limit} карточек'
        )
    normalized = []
    seen = set()
    for index, value in enumerate(raw_ids):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f'Некорректный идентификатор товара в позиции {index + 1}')
        if value in seen:
            raise ValueError('Товары в списке повторяются')
        normalized.append(value)
        seen.add(value)
    return normalized


def _form_bulk_product_ids(raw_ids, limit=MAX_BULK_PRODUCTS):
    """Строго разобрать exact положительные ID из обычной HTML-формы."""
    if not isinstance(raw_ids, list) or not raw_ids:
        return []
    if len(raw_ids) > limit:
        raise ValueError(f'За один раз можно выбрать не более {limit} карточек')
    normalized = []
    seen = set()
    for index, value in enumerate(raw_ids):
        if not isinstance(value, str) or not value.isdecimal():
            raise ValueError(
                f'Некорректный идентификатор карточки в позиции {index + 1}'
            )
        product_id = int(value)
        if product_id <= 0:
            raise ValueError(
                f'Некорректный идентификатор карточки в позиции {index + 1}'
            )
        if product_id in seen:
            raise ValueError('В списке есть повторяющиеся карточки')
        seen.add(product_id)
        normalized.append(product_id)
    return normalized


class SupplierImportBatchTokenError(ValueError):
    """A signed supplier-import workspace selection is invalid or unavailable."""

    def __init__(self, message, *, status_code=400):
        super().__init__(message)
        self.status_code = status_code


def _supplier_import_batch_serializer():
    return URLSafeTimedSerializer(
        current_app.secret_key,
        salt=SUPPLIER_IMPORT_BATCH_TOKEN_SALT,
    )


def _ordered_committed_import_ids(
    seller_id,
    supplier_product_ids,
    imported_count,
    imported_product_ids,
):
    """Verify the result IDs are new committed rows for this exact selection."""
    if (
        type(imported_count) is not int
        or imported_count <= 0
        or imported_count > MAX_BULK_PRODUCTS
    ):
        return []
    try:
        ids = _bulk_product_ids(imported_product_ids)
    except ValueError:
        return []
    if len(ids) != imported_count or any(
        value > MAX_SIGNED_SQLITE_ID for value in ids
    ):
        return []

    rows = db.session.query(
        ImportedProduct.id,
        ImportedProduct.supplier_product_id,
    ).filter(
        ImportedProduct.seller_id == seller_id,
        ImportedProduct.id.in_(ids),
    ).all()
    if len(rows) != len(ids):
        return []
    selected_sources = set(supplier_product_ids)
    observed_sources = [row.supplier_product_id for row in rows]
    if (
        any(source_id not in selected_sources for source_id in observed_sources)
        or len(set(observed_sources)) != len(ids)
    ):
        return []
    # Preserve the import service's exact result order in the receipt.
    return ids


def _issue_supplier_import_batch_token(*, user_id, seller_id, imported_product_ids):
    try:
        ids = _bulk_product_ids(imported_product_ids)
    except ValueError:
        return None
    if len(ids) > MAX_BULK_PRODUCTS or any(
        value > MAX_SIGNED_SQLITE_ID for value in ids
    ):
        return None
    payload = {
        'v': 1,
        'user_id': user_id,
        'seller_id': seller_id,
        'ids': ids,
    }
    token = _supplier_import_batch_serializer().dumps(payload)
    if len(token.encode('utf-8')) > SUPPLIER_IMPORT_BATCH_MAX_TOKEN_BYTES:
        return None
    return token


def _load_supplier_import_batch_ids(*, user_id, seller_id):
    values = request.args.getlist('import_batch')
    if len(values) != 1:
        raise SupplierImportBatchTokenError('Набор импорта недействителен')
    token = values[0]
    if (
        not token
        or len(token.encode('utf-8')) > SUPPLIER_IMPORT_BATCH_MAX_TOKEN_BYTES
    ):
        raise SupplierImportBatchTokenError('Набор импорта недействителен')
    try:
        payload = _supplier_import_batch_serializer().loads(
            token,
            max_age=SUPPLIER_IMPORT_BATCH_MAX_AGE_SECONDS,
        )
    except SignatureExpired as exc:
        raise SupplierImportBatchTokenError(
            'Срок действия набора импорта истёк',
            status_code=410,
        ) from exc
    except BadSignature as exc:
        raise SupplierImportBatchTokenError(
            'Набор импорта недействителен',
        ) from exc

    if not isinstance(payload, dict) or set(payload) != {
        'v', 'user_id', 'seller_id', 'ids',
    }:
        raise SupplierImportBatchTokenError('Набор импорта недействителен')
    if (
        type(payload.get('v')) is not int
        or payload['v'] != 1
        or type(payload.get('user_id')) is not int
        or type(payload.get('seller_id')) is not int
    ):
        raise SupplierImportBatchTokenError('Набор импорта недействителен')
    if payload['user_id'] != user_id or payload['seller_id'] != seller_id:
        raise SupplierImportBatchTokenError(
            'Набор импорта недоступен',
            status_code=404,
        )
    try:
        ids = _bulk_product_ids(payload.get('ids'))
    except ValueError as exc:
        raise SupplierImportBatchTokenError(
            'Набор импорта недействителен',
        ) from exc
    if any(value > MAX_SIGNED_SQLITE_ID for value in ids):
        raise SupplierImportBatchTokenError('Набор импорта недействителен')
    return ids


def _bounded_import_batch_template_value():
    values = request.args.getlist('import_batch')
    if not values:
        return None
    if len(values) != 1:
        return ''
    token = values[0]
    if len(token.encode('utf-8')) > SUPPLIER_IMPORT_BATCH_MAX_TOKEN_BYTES:
        return ''
    return token


def _no_store_supplier_import_batch_response(response):
    """Prevent batch selections and their row payloads from entering caches."""
    response = make_response(response)
    if 'import_batch' in request.args:
        response.headers['Cache-Control'] = 'no-store'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
    return response


class SupplierRefreshReceiptError(ValueError):
    """A supplier refresh receipt is invalid, expired, or out of scope."""

    def __init__(self, message, *, status_code=400):
        super().__init__(message)
        self.status_code = status_code


class SupplierRefreshReviewTokenError(ValueError):
    """A supplier refresh review token is invalid, expired, or out of scope."""

    def __init__(self, message, *, status_code=400, code='supplier_refresh_review_token_invalid'):
        super().__init__(message)
        self.status_code = status_code
        self.code = code


def _supplier_refresh_review_serializer():
    return URLSafeTimedSerializer(
        current_app.secret_key,
        salt=SUPPLIER_REFRESH_REVIEW_TOKEN_SALT,
    )


def _issue_supplier_refresh_review_token(*, user_id, seller_id, ids, digest):
    if (
        type(user_id) is not int or user_id <= 0 or user_id > MAX_SIGNED_SQLITE_ID
        or type(seller_id) is not int or seller_id <= 0 or seller_id > MAX_SIGNED_SQLITE_ID
        or not isinstance(digest, str) or re.fullmatch(r'[0-9a-f]{64}', digest) is None
    ):
        return None
    try:
        normalized_ids = _bulk_product_ids(ids)
    except ValueError:
        return None
    if any(product_id > MAX_SIGNED_SQLITE_ID for product_id in normalized_ids):
        return None
    token = _supplier_refresh_review_serializer().dumps({
        'v': 1,
        'user_id': user_id,
        'seller_id': seller_id,
        'ids': normalized_ids,
        'digest': digest,
    })
    if len(token.encode('utf-8')) > SUPPLIER_REFRESH_REVIEW_MAX_TOKEN_BYTES:
        return None
    return token


def _load_supplier_refresh_review_token(token, *, user_id, seller_id):
    if (
        not isinstance(token, str)
        or not token
        or len(token.encode('utf-8')) > SUPPLIER_REFRESH_REVIEW_MAX_TOKEN_BYTES
    ):
        raise SupplierRefreshReviewTokenError('Проверка обновления недоступна')
    try:
        payload = _supplier_refresh_review_serializer().loads(
            token,
            max_age=SUPPLIER_REFRESH_REVIEW_MAX_AGE_SECONDS,
        )
    except SignatureExpired as exc:
        raise SupplierRefreshReviewTokenError(
            'Проверка обновления истекла. Откройте её заново.',
            status_code=409,
            code='supplier_refresh_review_stale',
        ) from exc
    except BadSignature as exc:
        raise SupplierRefreshReviewTokenError(
            'Проверка обновления недействительна',
        ) from exc
    if not isinstance(payload, dict) or set(payload) != {
        'v', 'user_id', 'seller_id', 'ids', 'digest',
    }:
        raise SupplierRefreshReviewTokenError('Проверка обновления недействительна')
    if (
        type(payload.get('v')) is not int or payload['v'] != 1
        or type(payload.get('user_id')) is not int
        or type(payload.get('seller_id')) is not int
        or payload['user_id'] <= 0 or payload['seller_id'] <= 0
        or payload['user_id'] > MAX_SIGNED_SQLITE_ID
        or payload['seller_id'] > MAX_SIGNED_SQLITE_ID
        or not isinstance(payload.get('digest'), str)
        or re.fullmatch(r'[0-9a-f]{64}', payload['digest']) is None
    ):
        raise SupplierRefreshReviewTokenError('Проверка обновления недействительна')
    if payload['user_id'] != user_id or payload['seller_id'] != seller_id:
        raise SupplierRefreshReviewTokenError(
            'Проверка обновления недоступна', status_code=404,
        )
    try:
        ids = _bulk_product_ids(payload.get('ids'))
    except ValueError as exc:
        raise SupplierRefreshReviewTokenError(
            'Проверка обновления недействительна',
        ) from exc
    if any(product_id > MAX_SIGNED_SQLITE_ID for product_id in ids):
        raise SupplierRefreshReviewTokenError('Проверка обновления недействительна')
    return {'ids': ids, 'digest': payload['digest']}


class SupplierCatalogUpdateReviewTokenError(ValueError):
    """A local supplier catalog review is invalid, expired, or out of scope."""

    def __init__(self, message, *, status_code=400):
        super().__init__(message)
        self.status_code = status_code


def _supplier_catalog_update_review_serializer():
    return URLSafeTimedSerializer(
        current_app.secret_key,
        salt=SUPPLIER_CATALOG_UPDATE_REVIEW_TOKEN_SALT,
    )


def _issue_supplier_catalog_update_review_token(
    *, user_id, seller_id, supplier_id, source_ids, review_digest,
):
    if (
        type(user_id) is not int or user_id <= 0 or user_id > MAX_SIGNED_SQLITE_ID
        or type(seller_id) is not int or seller_id <= 0 or seller_id > MAX_SIGNED_SQLITE_ID
        or type(supplier_id) is not int or supplier_id <= 0 or supplier_id > MAX_SIGNED_SQLITE_ID
        or not isinstance(review_digest, str)
        or re.fullmatch(r'[0-9a-f]{64}', review_digest) is None
    ):
        return None
    if source_ids is not None:
        try:
            source_ids = _bulk_product_ids(source_ids)
        except ValueError:
            return None
        if len(source_ids) > MAX_BULK_PRODUCTS or any(
            product_id > MAX_SIGNED_SQLITE_ID for product_id in source_ids
        ):
            return None
        scope = 'source_ids'
    else:
        scope = 'all'

    token = _supplier_catalog_update_review_serializer().dumps({
        'v': 1,
        'user_id': user_id,
        'seller_id': seller_id,
        'supplier_id': supplier_id,
        'scope': scope,
        'source_ids': source_ids,
        'review_digest': review_digest,
    })
    if len(token.encode('utf-8')) > SUPPLIER_CATALOG_UPDATE_REVIEW_MAX_TOKEN_BYTES:
        return None
    return token


def _load_supplier_catalog_update_review_token(
    token, *, user_id, seller_id, supplier_id,
):
    if (
        not isinstance(token, str)
        or not token
        or len(token.encode('utf-8')) > SUPPLIER_CATALOG_UPDATE_REVIEW_MAX_TOKEN_BYTES
    ):
        raise SupplierCatalogUpdateReviewTokenError(
            'Проверка обновления недоступна',
        )
    try:
        payload = _supplier_catalog_update_review_serializer().loads(
            token,
            max_age=SUPPLIER_CATALOG_UPDATE_REVIEW_MAX_AGE_SECONDS,
        )
    except SignatureExpired as exc:
        raise SupplierCatalogUpdateReviewTokenError(
            'Срок проверки истёк. Откройте её заново.', status_code=409,
        ) from exc
    except BadSignature as exc:
        raise SupplierCatalogUpdateReviewTokenError(
            'Проверка обновления недействительна',
        ) from exc

    if not isinstance(payload, dict) or set(payload) != {
        'v', 'user_id', 'seller_id', 'supplier_id', 'scope',
        'source_ids', 'review_digest',
    }:
        raise SupplierCatalogUpdateReviewTokenError(
            'Проверка обновления недействительна',
        )
    if (
        type(payload.get('v')) is not int or payload['v'] != 1
        or any(
            type(payload.get(field)) is not int
            or payload[field] <= 0
            or payload[field] > MAX_SIGNED_SQLITE_ID
            for field in ('user_id', 'seller_id', 'supplier_id')
        )
        or not isinstance(payload.get('review_digest'), str)
        or re.fullmatch(r'[0-9a-f]{64}', payload['review_digest']) is None
    ):
        raise SupplierCatalogUpdateReviewTokenError(
            'Проверка обновления недействительна',
        )
    if (
        payload['user_id'] != user_id
        or payload['seller_id'] != seller_id
        or payload['supplier_id'] != supplier_id
    ):
        raise SupplierCatalogUpdateReviewTokenError(
            'Проверка недоступна', status_code=404,
        )

    if payload['scope'] == 'all' and payload['source_ids'] is None:
        source_ids = None
    elif payload['scope'] == 'source_ids':
        try:
            source_ids = _bulk_product_ids(payload['source_ids'])
        except ValueError as exc:
            raise SupplierCatalogUpdateReviewTokenError(
                'Проверка обновления недействительна',
            ) from exc
        if len(source_ids) > MAX_BULK_PRODUCTS or any(
            product_id > MAX_SIGNED_SQLITE_ID for product_id in source_ids
        ):
            raise SupplierCatalogUpdateReviewTokenError(
                'Проверка обновления недействительна',
            )
    else:
        raise SupplierCatalogUpdateReviewTokenError(
            'Проверка обновления недействительна',
        )

    return {
        'scope': payload['scope'],
        'source_ids': source_ids,
        'review_digest': payload['review_digest'],
    }


def _supplier_catalog_update_scope_from_form(form):
    supplier_values = form.getlist('supplier_id')
    if len(supplier_values) != 1 or not supplier_values[0].isdecimal():
        raise ValueError('Не указан корректный поставщик')
    supplier_id = int(supplier_values[0])
    if supplier_id <= 0 or supplier_id > MAX_SIGNED_SQLITE_ID:
        raise ValueError('Не указан корректный поставщик')

    selected_ids_raw = form.getlist('update_product_ids')
    fallback_ids_raw = form.getlist('product_ids')
    if selected_ids_raw and fallback_ids_raw:
        raise ValueError('Смешаны два списка товаров. Обновите страницу.')
    all_values = form.getlist('update_all_for_supplier')
    update_all = all_values == ['1']
    if all_values and not update_all:
        raise ValueError('Некорректный режим выбора товаров')

    raw_ids = selected_ids_raw or fallback_ids_raw
    if update_all:
        if raw_ids:
            raise ValueError('Выберите либо карточки, либо весь каталог поставщика')
        return supplier_id, None
    if not raw_ids:
        raise ValueError('Не выбраны карточки для проверки')
    try:
        source_ids = _form_bulk_product_ids(raw_ids)
    except ValueError as exc:
        raise ValueError(str(exc)) from exc
    if any(source_id > MAX_SIGNED_SQLITE_ID for source_id in source_ids):
        raise ValueError('Список товаров поставщика содержит некорректный ID')
    return supplier_id, source_ids


def _safe_supplier_catalog_update_return(supplier_id, candidate):
    fallback = url_for('supplier_catalog_products', supplier_id=supplier_id)
    if not isinstance(candidate, str) or not candidate:
        return fallback
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return fallback
    product_path = f'/supplier-catalog/{supplier_id}/products'
    if (
        parsed.scheme or parsed.netloc or parsed.path.startswith('//')
        or not (
            parsed.path == url_for('supplier_catalog')
            or parsed.path == product_path
            or re.fullmatch(re.escape(product_path) + r'/[1-9][0-9]*', parsed.path)
        )
    ):
        return fallback
    return parsed.path + (f'?{parsed.query}' if parsed.query else '') + (
        f'#{parsed.fragment}' if parsed.fragment else ''
    )


def _render_supplier_catalog_update_review(
    *, supplier, source_ids, preview, review_token, return_to,
    error=None, status=200, allow_recheck=True,
):
    response = make_response(render_template(
        'supplier_catalog_update_review.html',
        supplier=supplier,
        source_ids=source_ids,
        update_all=source_ids is None,
        preview=preview,
        review_token=review_token,
        return_to=return_to,
        error=error,
        allow_recheck=allow_recheck,
    ), status)
    response.headers['Cache-Control'] = 'no-store'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response


def _supplier_refresh_receipt_serializer():
    return URLSafeTimedSerializer(
        current_app.secret_key,
        salt=SUPPLIER_REFRESH_RECEIPT_TOKEN_SALT,
    )


def _validated_supplier_refresh_receipt_subset(ids, statuses, wb_pairs):
    """Validate bounded status and WB-pair facts shared by issue and load."""
    if (
        not isinstance(statuses, list)
        or len(statuses) != len(ids)
        or any(
            not isinstance(status, str)
            or status not in SUPPLIER_REFRESH_STATUS_REASONS
            for status in statuses
        )
        or statuses.count('updated') < 1
        or not isinstance(wb_pairs, list)
        or len(wb_pairs) > statuses.count('updated')
    ):
        return None

    updated_indexes = {
        product_id: index
        for index, (product_id, status) in enumerate(zip(ids, statuses))
        if status == 'updated'
    }
    normalized_pairs = []
    seen_product_ids = set()
    last_updated_index = -1
    for pair in wb_pairs:
        if not isinstance(pair, list) or len(pair) != 2:
            return None
        imported_id, product_id = pair
        if (
            type(imported_id) is not int
            or imported_id not in updated_indexes
            or type(product_id) is not int
            or product_id <= 0
            or product_id > MAX_SIGNED_SQLITE_ID
            or product_id in seen_product_ids
            or updated_indexes[imported_id] <= last_updated_index
        ):
            return None
        last_updated_index = updated_indexes[imported_id]
        seen_product_ids.add(product_id)
        normalized_pairs.append([imported_id, product_id])
    return normalized_pairs


def _issue_supplier_refresh_receipt(
    *, user_id, seller_id, requested_ids, requested, updated, skipped, errors,
    item_results, wb_targets,
):
    """Sign only a fully verified, bounded terminal supplier refresh result."""
    if (
        type(user_id) is not int or user_id <= 0
        or type(seller_id) is not int or seller_id <= 0
        or user_id > MAX_SIGNED_SQLITE_ID
        or seller_id > MAX_SIGNED_SQLITE_ID
    ):
        return None
    try:
        ids = _bulk_product_ids(requested_ids)
    except ValueError:
        return None
    if any(product_id > MAX_SIGNED_SQLITE_ID for product_id in ids):
        return None
    if (
        type(requested) is not int or requested != len(ids)
        or any(type(value) is not int or value < 0 for value in (updated, skipped, errors))
        or updated < 1
        or updated + skipped + errors != requested
        or not isinstance(item_results, list)
        or len(item_results) != requested
        or not isinstance(wb_targets, list)
        or len(wb_targets) > updated
    ):
        return None

    statuses = []
    counts = {'updated': 0, 'skipped': 0, 'error': 0}
    for expected_id, item in zip(ids, item_results):
        status = item.get('status') if isinstance(item, dict) else None
        if (
            not isinstance(item, dict)
            or set(item) != {'product_id', 'status', 'reason'}
            or type(item.get('product_id')) is not int
            or item['product_id'] != expected_id
            or not isinstance(status, str)
            or status not in SUPPLIER_REFRESH_STATUS_REASONS
            or item.get('reason') != SUPPLIER_REFRESH_STATUS_REASONS.get(status)
        ):
            return None
        statuses.append(status)
        counts[status] += 1
    if (
        counts['updated'] != updated
        or counts['skipped'] != skipped
        or counts['error'] != errors
    ):
        return None

    raw_wb_pairs = []
    for target in wb_targets:
        if (
            not isinstance(target, dict)
            or set(target) != {'imported_product_id', 'product_id'}
        ):
            return None
        raw_wb_pairs.append([
            target.get('imported_product_id'), target.get('product_id'),
        ])
    normalized_targets = _validated_supplier_refresh_receipt_subset(
        ids, statuses, raw_wb_pairs,
    )
    if normalized_targets is None:
        return None

    payload = {
        'v': 1,
        'user_id': user_id,
        'seller_id': seller_id,
        'ids': ids,
        'statuses': statuses,
        'wb_targets': normalized_targets,
    }
    token = _supplier_refresh_receipt_serializer().dumps(payload)
    if len(token.encode('utf-8')) > SUPPLIER_REFRESH_RECEIPT_MAX_TOKEN_BYTES:
        return None
    return token


def _load_supplier_refresh_receipt(token, *, user_id, seller_id):
    """Validate signature, lifetime, exact shape, and current caller scope."""
    if (
        not isinstance(token, str)
        or not token
        or len(token.encode('utf-8')) > SUPPLIER_REFRESH_RECEIPT_MAX_TOKEN_BYTES
    ):
        raise SupplierRefreshReceiptError('Итог обновления недоступен')
    try:
        payload = _supplier_refresh_receipt_serializer().loads(
            token,
            max_age=SUPPLIER_REFRESH_RECEIPT_MAX_AGE_SECONDS,
        )
    except SignatureExpired as exc:
        raise SupplierRefreshReceiptError(
            'Срок получения итога обновления истёк',
            status_code=410,
        ) from exc
    except BadSignature as exc:
        raise SupplierRefreshReceiptError('Итог обновления недействителен') from exc

    if not isinstance(payload, dict) or set(payload) != {
        'v', 'user_id', 'seller_id', 'ids', 'statuses', 'wb_targets',
    }:
        raise SupplierRefreshReceiptError('Итог обновления недействителен')
    if (
        type(payload.get('v')) is not int or payload['v'] != 1
        or type(payload.get('user_id')) is not int
        or type(payload.get('seller_id')) is not int
        or payload['user_id'] <= 0
        or payload['seller_id'] <= 0
        or payload['user_id'] > MAX_SIGNED_SQLITE_ID
        or payload['seller_id'] > MAX_SIGNED_SQLITE_ID
    ):
        raise SupplierRefreshReceiptError('Итог обновления недействителен')
    if payload['user_id'] != user_id or payload['seller_id'] != seller_id:
        raise SupplierRefreshReceiptError(
            'Итог обновления недоступен',
            status_code=404,
        )
    try:
        ids = _bulk_product_ids(payload.get('ids'))
    except ValueError as exc:
        raise SupplierRefreshReceiptError('Итог обновления недействителен') from exc
    if any(product_id > MAX_SIGNED_SQLITE_ID for product_id in ids):
        raise SupplierRefreshReceiptError('Итог обновления недействителен')

    statuses = payload.get('statuses')
    normalized_pairs = _validated_supplier_refresh_receipt_subset(
        ids, statuses, payload.get('wb_targets'),
    )
    if normalized_pairs is None:
        raise SupplierRefreshReceiptError('Итог обновления недействителен')
    targets = []
    for imported_id, product_id in normalized_pairs:
        targets.append({
            'imported_product_id': imported_id,
            'product_id': product_id,
        })
    return {'ids': ids, 'statuses': statuses, 'wb_targets': targets}


def _supplier_refresh_result_no_store(view):
    """Mark every API response no-store, including auth and scope failures."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        response = make_response(view(*args, **kwargs))
        response.headers['Cache-Control'] = 'no-store'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
        return response
    return wrapped


def _redirect_after_supplier_import(
    *,
    seller_id,
    user_id,
    supplier_id,
    supplier_product_ids,
    imported_count,
    imported_product_ids,
    summary,
    category,
    zero_import_summary,
    zero_import_category,
):
    """Open only the newly committed rows from a declared-supplier import."""
    if type(imported_count) is int and imported_count > 0:
        imported_ids = _ordered_committed_import_ids(
            seller_id,
            supplier_product_ids,
            imported_count,
            imported_product_ids,
        )
        token = _issue_supplier_import_batch_token(
            user_id=user_id,
            seller_id=seller_id,
            imported_product_ids=imported_ids,
        ) if imported_ids else None
        if token:
            flash(
                f'{summary} Открыты добавленные карточки в рабочем пространстве.',
                category,
            )
            return redirect(
                url_for('seller_my_products_beta', import_batch=token),
                code=303,
            )
        flash(
            f'{summary} Не удалось проверить точный список новых карточек; '
            'вернитесь в каталог поставщика.',
            'warning',
        )
        return _redirect_seller_supplier_catalog(supplier_id)

    flash(zero_import_summary, zero_import_category)
    return _redirect_seller_supplier_catalog(supplier_id)


def _supplier_import_form_selection(seller_id, form):
    """Parse a normal/smart supplier import form without widening a declared scope.

    Forms predating the supplier_id field keep their historical digit-filtering
    behavior. Current catalog/detail forms declare a supplier and must carry a
    complete, unique, bounded ID list that belongs to that seller's active
    supplier connection before either import path can run.
    """
    supplier_values = form.getlist('supplier_id')
    raw_ids = form.getlist('product_ids')
    if not supplier_values:
        return None, [int(value) for value in raw_ids if value.isdigit()]

    if len(supplier_values) != 1 or not supplier_values[0].isdecimal():
        raise ValueError('Не удалось проверить выбранного поставщика')
    supplier_id = _coerce_positive_int(supplier_values[0])
    max_database_id = (1 << 63) - 1
    if supplier_id is None or supplier_id > max_database_id:
        raise ValueError('Не удалось проверить выбранного поставщика')
    product_ids = _form_bulk_product_ids(raw_ids)
    if any(product_id > max_database_id for product_id in product_ids):
        raise ValueError('Список товаров содержит некорректный ID')
    if not product_ids:
        return supplier_id, []

    connection = SellerSupplier.query.filter_by(
        seller_id=seller_id,
        supplier_id=supplier_id,
        is_active=True,
    ).first()
    if not connection:
        raise ValueError('У вас нет активного доступа к этому поставщику')

    found_ids = {
        row[0]
        for row in db.session.query(SupplierProduct.id).filter(
            SupplierProduct.supplier_id == supplier_id,
            SupplierProduct.id.in_(product_ids),
        ).all()
    }
    if found_ids != set(product_ids):
        raise ValueError(
            'Выбранные товары изменились или относятся к другому поставщику. '
            'Обновите список и повторите выбор.'
        )
    return supplier_id, product_ids


# Обычный каталог ограничен 50 строками: legacy 200-row HTML весил >1 МБ и
# запускал сотни image requests. Bulk-вкладка обновлений сохраняет cap 200.
MY_PRODUCTS_PAGE_SIZE = 50
MY_PRODUCTS_UPDATES_PAGE_SIZE = 200
MY_PRODUCTS_STATUSES = frozenset({
    'pending', 'validated', 'imported', 'failed',
})
MY_PRODUCTS_SORTS = frozenset({
    'oldest', 'price_asc', 'price_desc', 'title',
})


# ============================================================================
# SAFE REDIRECTS
# ============================================================================
# Хелперы оборачивают redirect(url_for(...)) с числовыми параметрами,
# полученными из request.form. url_for() всегда возвращает локальный путь,
# поэтому open-redirect здесь невозможен — но semgrep видит путь
# request → redirect и поднимает false positive. Эти helpers перехватывают
# и валидируют id, а заодно дают единое место для fallback.

def _coerce_positive_int(value) -> int | None:
    try:
        v = int(value)
    except (ValueError, TypeError):
        return None
    return v if v > 0 else None


def _redirect_admin_supplier_products(supplier_id):
    """Безопасный редирект на список товаров поставщика (admin)."""
    sid = _coerce_positive_int(supplier_id)
    if sid is None:
        return redirect(url_for('admin_suppliers'))
    return redirect(url_for('admin_supplier_products', supplier_id=sid))


def _redirect_admin_supplier_product_detail(supplier_id, product_id):
    """Безопасный редирект на карточку товара поставщика (admin)."""
    sid = _coerce_positive_int(supplier_id)
    pid = _coerce_positive_int(product_id)
    if sid is None:
        return redirect(url_for('admin_suppliers'))
    if pid is None:
        return redirect(url_for('admin_supplier_products', supplier_id=sid))
    return redirect(url_for(
        'admin_supplier_product_detail', supplier_id=sid, product_id=pid
    ))


def _redirect_seller_supplier_catalog(supplier_id):
    """Безопасный редирект на каталог продавца с фильтром по поставщику."""
    sid = _coerce_positive_int(supplier_id)
    if sid is None:
        return redirect(url_for('supplier_catalog'))
    return redirect(url_for('supplier_catalog_products', supplier_id=sid))


def _my_products_return_args(args):
    """Canonical same-screen state for POST -> GET redirects.

    The supplier-updates view is mutually exclusive with the regular status
    tabs.  Only known filters are carried so a crafted query cannot turn a
    local ``url_for`` redirect into an arbitrary or unbounded URL.
    """
    result = {}
    account_id = _coerce_positive_int(args.get('account_id'))
    if account_id is not None:
        result['account_id'] = account_id
    updates_filter = (
        isinstance(args.get('updates'), str)
        and args.get('updates').strip() == '1'
    )
    if updates_filter:
        result['updates'] = 1
    else:
        status = args.get('status')
        status = status.strip() if isinstance(status, str) else ''
        if status in MY_PRODUCTS_STATUSES:
            result['status'] = status

    search = args.get('search')
    search = search.strip() if isinstance(search, str) else ''
    if search and len(search) <= 200:
        result['search'] = search

    supplier = args.get('supplier')
    supplier = supplier.strip() if isinstance(supplier, str) else ''
    if supplier == 'none':
        result['supplier'] = supplier
    else:
        supplier_id = _coerce_positive_int(supplier)
        if supplier_id is not None:
            result['supplier'] = supplier_id

    for key in ('brand', 'wb_category'):
        value = args.get(key)
        value = value.strip() if isinstance(value, str) else ''
        if value and len(value) <= 200:
            result[key] = value

    has_photos = args.get('has_photos')
    if has_photos in ('yes', 'no'):
        result['has_photos'] = has_photos

    stock = args.get('stock')
    if stock in ('in_stock', 'out_of_stock', 'unknown'):
        result['stock'] = stock

    for key in ('price_min', 'price_max'):
        value = args.get(key)
        value = value.strip() if isinstance(value, str) else ''
        if value and len(value) <= 32 and re.fullmatch(r'\d+(?:\.\d+)?', value):
            result[key] = value

    sort = args.get('sort')
    sort = sort.strip() if isinstance(sort, str) else ''
    if sort in MY_PRODUCTS_SORTS:
        result['sort'] = sort
    return result


def _redirect_seller_my_products(args=None, **extra):
    """Return to the same canonical «Мои товары» view after a POST."""
    target_args = _my_products_return_args(args or {})
    target_args.update(extra)
    return redirect(url_for('seller_my_products', **target_args))


# ============================================================================
# DECORATORS
# ============================================================================

def admin_required(f):
    """Декоратор для проверки прав администратора"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            flash('У вас нет прав для доступа к этой странице', 'danger')
            return redirect(url_for('dashboard'))
        return f(*args, **kwargs)
    return decorated_function


def seller_required(f):
    """Декоратор — пользователь должен быть продавцом"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.seller:
            flash('У вас нет профиля продавца', 'danger')
            return redirect(url_for('dashboard'))
        return f(*args, **kwargs)
    return decorated_function


# ============================================================================
# ADMIN: CRUD поставщиков
# ============================================================================

def register_supplier_routes(app):
    """Регистрирует все маршруты поставщиков в приложении Flask"""

    # -------------------------------------------------------------------
    # Список поставщиков
    # -------------------------------------------------------------------
    @app.route('/admin/suppliers')
    @login_required
    @admin_required
    def admin_suppliers():
        suppliers = SupplierService.list_suppliers()
        stats = {}
        for s in suppliers:
            stats[s.id] = SupplierService.get_product_stats(s.id)
            stats[s.id]['sellers_count'] = s.get_connected_sellers_count()
        return render_template('admin_suppliers.html',
                               suppliers=suppliers, stats=stats)

    # -------------------------------------------------------------------
    # Создание поставщика
    # -------------------------------------------------------------------
    @app.route('/admin/suppliers/add', methods=['GET', 'POST'])
    @login_required
    @admin_required
    def admin_supplier_add():
        if request.method == 'POST':
            code = request.form.get('code', '').strip().lower()
            code = re.sub(r'[^a-z0-9_-]', '', code)
            name = request.form.get('name', '').strip()

            if not name or not code:
                flash('Название и код обязательны', 'warning')
                return render_template('admin_supplier_form.html', supplier=None, mode='add')

            # Проверка уникальности кода
            if Supplier.query.filter_by(code=code).first():
                flash(f'Поставщик с кодом "{code}" уже существует', 'danger')
                return render_template('admin_supplier_form.html', supplier=None, mode='add')

            data = _extract_supplier_form_data(request.form)
            data['name'] = name
            data['code'] = code

            supplier = SupplierService.create_supplier(data, created_by_user_id=current_user.id)

            log_admin_action(
                admin_user_id=current_user.id,
                action='create_supplier',
                target_type='supplier',
                target_id=supplier.id,
                details={'name': name, 'code': code},
                request=request
            )

            flash(f'Поставщик "{name}" успешно создан', 'success')
            return redirect(url_for('admin_supplier_edit', supplier_id=supplier.id))

        return render_template('admin_supplier_form.html', supplier=None, mode='add')

    # -------------------------------------------------------------------
    # Редактирование поставщика
    # -------------------------------------------------------------------
    @app.route('/admin/suppliers/<int:supplier_id>/edit', methods=['GET', 'POST'])
    @login_required
    @admin_required
    def admin_supplier_edit(supplier_id):
        supplier = SupplierService.get_supplier(supplier_id)
        if not supplier:
            flash('Поставщик не найден', 'danger')
            return redirect(url_for('admin_suppliers'))

        if request.method == 'POST':
            data = _extract_supplier_form_data(request.form)
            data['name'] = request.form.get('name', supplier.name).strip()

            SupplierService.update_supplier(supplier_id, data)

            log_admin_action(
                admin_user_id=current_user.id,
                action='update_supplier',
                target_type='supplier',
                target_id=supplier_id,
                details={'name': data['name']},
                request=request
            )

            flash('Данные поставщика обновлены', 'success')
            return redirect(url_for('admin_supplier_edit', supplier_id=supplier_id))

        stats = SupplierService.get_product_stats(supplier_id)
        price_stock_stats = SupplierService.get_price_stock_stats(supplier_id)
        sellers = SupplierService.get_supplier_sellers(supplier_id)

        # Примеры external_id для превью артикулов
        sample_products = db.session.query(
            SupplierProduct.external_id, SupplierProduct.vendor_code
        ).filter(
            SupplierProduct.supplier_id == supplier_id,
            SupplierProduct.external_id.isnot(None),
            SupplierProduct.external_id != ''
        ).limit(3).all()
        sample_external_ids = [
            {'external_id': p.external_id, 'vendor_code': p.vendor_code or ''}
            for p in sample_products
        ]

        return render_template('admin_supplier_form.html',
                               supplier=supplier, mode='edit',
                               stats=stats, price_stock_stats=price_stock_stats,
                               sellers=sellers,
                               sample_external_ids=sample_external_ids)

    # -------------------------------------------------------------------
    # Удаление поставщика
    # -------------------------------------------------------------------
    @app.route('/admin/suppliers/<int:supplier_id>/delete', methods=['POST'])
    @login_required
    @admin_required
    def admin_supplier_delete(supplier_id):
        supplier = SupplierService.get_supplier(supplier_id)
        if not supplier:
            flash('Поставщик не найден', 'danger')
            return redirect(url_for('admin_suppliers'))

        name = supplier.name
        SupplierService.delete_supplier(supplier_id)

        log_admin_action(
            admin_user_id=current_user.id,
            action='delete_supplier',
            target_type='supplier',
            target_id=supplier_id,
            details={'name': name},
            request=request
        )

        flash(f'Поставщик "{name}" удалён', 'success')
        return redirect(url_for('admin_suppliers'))

    # -------------------------------------------------------------------
    # Переключение статуса
    # -------------------------------------------------------------------
    @app.route('/admin/suppliers/<int:supplier_id>/toggle-active', methods=['POST'])
    @login_required
    @admin_required
    def admin_supplier_toggle(supplier_id):
        supplier = SupplierService.get_supplier(supplier_id)
        if not supplier:
            flash('Поставщик не найден', 'danger')
            return redirect(url_for('admin_suppliers'))

        supplier.is_active = not supplier.is_active
        db.session.commit()

        status = 'активирован' if supplier.is_active else 'деактивирован'
        flash(f'Поставщик "{supplier.name}" {status}', 'success')
        return redirect(url_for('admin_suppliers'))

    # -------------------------------------------------------------------
    # Синхронизация каталога из CSV
    # -------------------------------------------------------------------
    @app.route('/admin/suppliers/<int:supplier_id>/sync', methods=['POST'])
    @login_required
    @admin_required
    def admin_supplier_sync(supplier_id):
        supplier = SupplierService.get_supplier(supplier_id)
        if not supplier:
            flash('Поставщик не найден', 'danger')
            return redirect(url_for('admin_suppliers'))

        if not supplier.csv_source_url:
            flash('URL CSV не задан для этого поставщика', 'warning')
            return redirect(url_for('admin_supplier_edit', supplier_id=supplier_id))

        baseline_values = request.form.getlist('catalog_refresh_baseline')
        if len(baseline_values) != 1:
            flash('Форма обновления устарела. Обновите страницу и повторите.', 'warning')
            return redirect(
                url_for('admin_supplier_products', supplier_id=supplier_id),
                code=303,
            )

        expected_latest_id = baseline_values[0] or None
        try:
            # Keep this import lazy: the refresh model is registered only after
            # startup migrations have completed.
            from services.supplier_catalog_refresh import (
                enqueue_catalog_refresh,
            )

            payload, created = enqueue_catalog_refresh(
                supplier_id=supplier_id,
                admin_user_id=current_user.id,
                expected_latest_id=expected_latest_id,
                ip_address=request.remote_addr,
            )
        except Exception as exc:
            error_code = getattr(exc, 'code', None)
            if error_code == 'supplier_not_found':
                flash('Поставщик не найден', 'danger')
            elif error_code == 'invalid_expected_run_id':
                flash('Форма обновления устарела. Обновите страницу и повторите.', 'warning')
            elif error_code == 'database_busy':
                flash('База данных занята. Повторите запуск позже.', 'warning')
            else:
                logger.warning(
                    'Supplier catalog refresh admission failed (%s)',
                    type(exc).__name__,
                )
                flash('Не удалось поставить обновление в очередь. Повторите попытку позже.', 'danger')
            return redirect(
                url_for('admin_supplier_products', supplier_id=supplier_id),
                code=303,
            )

        run_id = payload.get('id') if isinstance(payload, dict) else None
        if not isinstance(run_id, str) or not CATALOG_REFRESH_UUID_RE.fullmatch(run_id):
            flash('Не удалось подтвердить заявку на обновление.', 'warning')
            return redirect(
                url_for('admin_supplier_products', supplier_id=supplier_id),
                code=303,
            )

        if created:
            flash('Обновление каталога поставлено в очередь.', 'success')
        elif payload.get('status') in CATALOG_REFRESH_ACTIVE_STATES:
            flash('Обновление каталога уже выполняется.', 'info')
        else:
            flash('Показан текущий результат. Новое обновление не запускалось.', 'info')

        return redirect(
            url_for(
                'admin_supplier_products',
                supplier_id=supplier_id,
                catalog_refresh_run=run_id,
                _anchor='catalog-refresh-status',
            ),
            code=303,
        )

    @app.route(
        '/admin/suppliers/<int:supplier_id>/sync-status',
        methods=['GET'],
    )
    @login_required
    @admin_required
    def admin_supplier_catalog_refresh_status(supplier_id):
        """Return one exact supplier-scoped refresh receipt without writes."""
        run_ids = request.args.getlist('run_id')
        if (
            len(run_ids) != 1
            or not CATALOG_REFRESH_UUID_RE.fullmatch(run_ids[0])
        ):
            response = jsonify({'error_code': 'invalid_run_id'})
            response.headers['Cache-Control'] = 'private, no-store, max-age=0'
            return response, 400
        run_id = run_ids[0]
        try:
            from services.supplier_catalog_refresh import (
                get_catalog_refresh,
            )

            payload = get_catalog_refresh(supplier_id, run_id=run_id)
        except Exception as exc:
            logger.warning(
                'Supplier catalog status read failed (%s)',
                type(exc).__name__,
            )
            response = jsonify({'error_code': 'status_unavailable'})
            response.headers['Cache-Control'] = 'private, no-store, max-age=0'
            return response, 503
        if payload is None:
            response = jsonify({'error_code': 'refresh_not_found'})
            response.headers['Cache-Control'] = 'private, no-store, max-age=0'
            return response, 404

        response = jsonify(payload)
        response.headers['Cache-Control'] = 'private, no-store, max-age=0'
        return response

    # -------------------------------------------------------------------
    # Синхронизация цен и остатков
    # -------------------------------------------------------------------
    @app.route('/admin/suppliers/<int:supplier_id>/sync-prices', methods=['POST'])
    @login_required
    @admin_required
    def admin_supplier_sync_prices(supplier_id):
        supplier = SupplierService.get_supplier(supplier_id)
        if not supplier:
            flash('Поставщик не найден', 'danger')
            return redirect(url_for('admin_suppliers'))

        if not supplier.price_file_url:
            flash('URL файла цен не задан для этого поставщика', 'warning')
            return redirect(url_for('admin_supplier_edit', supplier_id=supplier_id))

        force = request.form.get('force', '0') == '1'
        result = SupplierService.sync_prices_and_stock(supplier_id, force=force)

        log_admin_action(
            admin_user_id=current_user.id,
            action='sync_supplier_prices',
            target_type='supplier',
            target_id=supplier_id,
            details={
                'updated': result.updated,
                'errors': result.errors,
                'duration': round(result.duration_seconds, 1),
                'cascade_updated': result.seller_rows_updated,
                'conflicting_source_rows': result.conflicting_source_rows,
                'already_running': result.busy,
            },
            request=request
        )

        if result.success:
            msg = f'Файл цен и остатков проверен. Позиций обработано — {result.updated}.'
            if result.seller_rows_updated:
                msg += f' Записей у продавцов изменено — {result.seller_rows_updated}.'
            if result.conflicting_source_rows:
                msg += f' Позиции с противоречивыми строками пропущены. Всего — {result.conflicting_source_rows}.'
            flash(msg, 'warning' if result.conflicting_source_rows else 'success')
        elif result.busy:
            flash('Проверка цен и остатков уже идёт.', 'warning')
        else:
            flash('Не удалось применить файл цен и остатков. Проверьте адрес источника и формат CSV.', 'danger')

        return redirect(url_for('admin_supplier_products', supplier_id=supplier_id))

    # -------------------------------------------------------------------
    # Товары поставщика (админ)
    # -------------------------------------------------------------------
    @app.route('/admin/suppliers/<int:supplier_id>/products')
    @login_required
    @admin_required
    def admin_supplier_products(supplier_id):
        supplier = SupplierService.get_supplier(supplier_id)
        if not supplier:
            flash('Поставщик не найден', 'danger')
            return redirect(url_for('admin_suppliers'))

        page = request.args.get('page', 1, type=int)
        per_page = request.args.get('per_page', 50, type=int)
        search = request.args.get('search', '').strip()
        status = request.args.get('status', '').strip()
        brand = request.args.get('brand', '').strip()
        category = request.args.get('category', '').strip()
        ai_validated = request.args.get('ai_validated')
        stock_status = request.args.get('stock_status', '').strip()
        sort_by = request.args.get('sort_by', 'created_at')
        sort_dir = request.args.get('sort_dir', 'desc')

        ai_val = None
        if ai_validated == '1':
            ai_val = True
        elif ai_validated == '0':
            ai_val = False

        pagination = SupplierService.get_products(
            supplier_id, page=page, per_page=per_page,
            search=search, status=status or None,
            category=category or None, brand=brand or None,
            ai_validated=ai_val, stock_status=stock_status or None,
            sort_by=sort_by, sort_dir=sort_dir
        )

        stats = SupplierService.get_product_stats(supplier_id)
        price_stock_stats = SupplierService.get_price_stock_stats(supplier_id)

        catalog_refresh_latest = None
        catalog_refresh_current = None
        catalog_refresh_lookup_failed = False
        requested_runs = request.args.getlist('catalog_refresh_run')
        try:
            from services.supplier_catalog_refresh import get_catalog_refresh

            catalog_refresh_latest = get_catalog_refresh(supplier_id)
            if len(requested_runs) > 1 or (requested_runs and not requested_runs[0]):
                catalog_refresh_lookup_failed = True
            elif requested_runs:
                catalog_refresh_current = get_catalog_refresh(
                    supplier_id,
                    run_id=requested_runs[0],
                )
                catalog_refresh_lookup_failed = catalog_refresh_current is None
            else:
                catalog_refresh_current = catalog_refresh_latest
        except Exception as exc:
            logger.warning(
                'Supplier catalog status page read failed (%s)',
                type(exc).__name__,
            )
            catalog_refresh_lookup_failed = True
        catalog_refresh_view = _supplier_catalog_refresh_view(
            catalog_refresh_current,
            lookup_failed=catalog_refresh_lookup_failed,
        )
        catalog_refresh_latest_id = (
            catalog_refresh_latest.get('id')
            if isinstance(catalog_refresh_latest, dict)
            else ''
        ) or ''
        catalog_refresh_latest_active = bool(
            isinstance(catalog_refresh_latest, dict)
            and catalog_refresh_latest.get('status') in CATALOG_REFRESH_ACTIVE_STATES
        )
        catalog_refresh_status_url = (
            url_for(
                'admin_supplier_catalog_refresh_status',
                supplier_id=supplier_id,
                run_id=catalog_refresh_current['id'],
            )
            if catalog_refresh_current and catalog_refresh_current.get('id')
            else None
        )

        # Получить список брендов и категорий для фильтров
        brands = db.session.query(SupplierProduct.brand).filter(
            SupplierProduct.supplier_id == supplier_id,
            SupplierProduct.brand.isnot(None),
            SupplierProduct.brand != ''
        ).distinct().order_by(SupplierProduct.brand).all()
        brands = [b[0] for b in brands]

        categories = db.session.query(SupplierProduct.category).filter(
            SupplierProduct.supplier_id == supplier_id,
            SupplierProduct.category.isnot(None),
            SupplierProduct.category != ''
        ).distinct().order_by(SupplierProduct.category).all()
        categories = [c[0] for c in categories]

        return render_template('admin_supplier_products.html',
                               supplier=supplier, pagination=pagination,
                               stats=stats, price_stock_stats=price_stock_stats,
                               brands=brands, categories=categories,
                               search=search, current_status=status,
                               current_brand=brand, current_category=category,
                               ai_validated=ai_validated,
                               stock_status=stock_status,
                               sort_by=sort_by, sort_dir=sort_dir,
                               catalog_refresh_latest=catalog_refresh_latest,
                               catalog_refresh_latest_id=catalog_refresh_latest_id,
                               catalog_refresh_latest_active=catalog_refresh_latest_active,
                               catalog_refresh_view=catalog_refresh_view,
                               catalog_refresh_status_url=catalog_refresh_status_url,
                               catalog_refresh_copy=CATALOG_REFRESH_COPY,
                               catalog_refresh_price_labels=CATALOG_REFRESH_PRICE_LABELS,
                               catalog_refresh_error_copy=CATALOG_REFRESH_ERROR_COPY)

    # -------------------------------------------------------------------
    # Детали / редактирование товара поставщика
    # -------------------------------------------------------------------
    @app.route('/admin/suppliers/<int:supplier_id>/products/<int:product_id>', methods=['GET', 'POST'])
    @login_required
    @admin_required
    def admin_supplier_product_detail(supplier_id, product_id):
        supplier = SupplierService.get_supplier(supplier_id)
        product = SupplierService.get_product(product_id)
        if not supplier or not product or product.supplier_id != supplier_id:
            flash('Товар не найден', 'danger')
            return redirect(url_for('admin_supplier_products', supplier_id=supplier_id))

        if request.method == 'POST':
            data = _extract_product_form_data(request.form)
            SupplierService.update_product(product_id, data)

            log_admin_action(
                admin_user_id=current_user.id,
                action='update_supplier_product',
                target_type='supplier_product',
                target_id=product_id,
                details={'supplier_id': supplier_id, 'title': data.get('title')},
                request=request
            )

            flash('Товар обновлён', 'success')
            return redirect(url_for('admin_supplier_product_detail',
                                    supplier_id=supplier_id, product_id=product_id))

        # Кол-во продавцов, импортировавших этот товар
        import_count = ImportedProduct.query.filter_by(supplier_product_id=product_id).count()

        # Расчёт розничной цены (если есть закупочная)
        price_calc = None
        if product.supplier_price and product.supplier_price > 0:
            try:
                from services.pricing_engine import calculate_price
                price_calc = calculate_price(product.supplier_price)
            except Exception:
                pass

        completeness = _calc_product_completeness(product)

        return render_template('admin_supplier_product_detail.html',
                               supplier=supplier, product=product,
                               import_count=import_count,
                               price_calc=price_calc,
                               completeness=completeness)

    # -------------------------------------------------------------------
    # Массовые действия с товарами
    # -------------------------------------------------------------------
    @app.route('/admin/suppliers/<int:supplier_id>/products/bulk-action', methods=['POST'])
    @login_required
    @admin_required
    def admin_supplier_products_bulk(supplier_id):
        action = request.form.get('action', '')
        product_ids_raw = request.form.getlist('product_ids')
        product_ids = [int(pid) for pid in product_ids_raw if pid.isdigit()]

        if not product_ids:
            flash('Не выбраны товары', 'warning')
            return redirect(url_for('admin_supplier_products', supplier_id=supplier_id))

        if action == 'delete':
            count = SupplierService.delete_products(product_ids)
            # Обновляем total_products
            supplier = SupplierService.get_supplier(supplier_id)
            if supplier:
                supplier.total_products = SupplierProduct.query.filter_by(supplier_id=supplier_id).count()
                db.session.commit()
            flash(f'Удалено {count} товаров', 'success')

        elif action == 'set_status':
            new_status = request.form.get('new_status', 'draft')
            for pid in product_ids:
                p = SupplierProduct.query.get(pid)
                if p and p.supplier_id == supplier_id:
                    p.status = new_status
            db.session.commit()
            flash(f'Статус обновлён для {len(product_ids)} товаров', 'success')

        elif action == 'ai_validate':
            result = SupplierService.ai_validate_bulk(supplier_id, product_ids)
            flash(f'AI валидация: {result.get("validated", 0)} успешно, {result.get("errors", 0)} ошибок', 'success')

        log_admin_action(
            admin_user_id=current_user.id,
            action=f'bulk_{action}_supplier_products',
            target_type='supplier',
            target_id=supplier_id,
            details={'product_count': len(product_ids), 'action': action},
            request=request
        )

        return redirect(url_for('admin_supplier_products', supplier_id=supplier_id))

    # -------------------------------------------------------------------
    # AI операции с товарами поставщика
    # -------------------------------------------------------------------
    @app.route('/admin/suppliers/<int:supplier_id>/products/<int:product_id>/validate_marketplace', methods=['POST'])
    @login_required
    @admin_required
    def admin_supplier_product_validate_marketplace(supplier_id, product_id):
        """Интерактивная валидация JSON данных товара для маркетплейса"""
        supplier = SupplierService.get_supplier(supplier_id)
        product = SupplierService.get_product(product_id)
        if not supplier or not product or product.supplier_id != supplier_id:
            return jsonify({'success': False, 'error': 'Product not found'}), 404
            
        data = request.json
        fields = data.get('marketplace_fields', {})
        
        from services.marketplace_validator import MarketplaceValidator
        
        # Determine marketplace and category
        marketplace_id = data.get('marketplace_id') or 1 # default WB
        subject_id = data.get('subject_id') or product.wb_subject_id
        
        if not subject_id:
            return jsonify({
                'success': False, 
                'error': 'Category (subject_id) is required for validation.'
            }), 400
            
        validator = MarketplaceValidator(marketplace_id)
        validation_result = validator.validate_product_data(subject_id, fields)
        
        # Optional: Save back to product
        return jsonify({
            'success': True,
            'is_valid': validation_result['is_valid'],
            'fill_percentage': validation_result['fill_percentage'],
            'errors': validation_result['errors']
        })

    @app.route('/admin/suppliers/<int:supplier_id>/ai/validate', methods=['POST'])
    @login_required
    @admin_required
    def admin_supplier_ai_validate(supplier_id):
        """AI валидация товаров поставщика"""
        product_ids_raw = request.form.getlist('product_ids')
        product_ids = [int(pid) for pid in product_ids_raw if pid.isdigit()]
        single_product_id = request.form.get('product_id', type=int)

        if single_product_id:
            result = SupplierService.ai_validate_product(single_product_id)
            log_admin_action(
                admin_user_id=current_user.id,
                action='ai_validate_supplier_product',
                target_type='supplier_product',
                target_id=single_product_id,
                details={'supplier_id': supplier_id, 'success': result.get('success'), 'score': result.get('score')},
                request=request
            )
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return jsonify(result)
            if result.get('success'):
                flash(f'AI валидация завершена. Оценка: {result.get("score", 0):.0f}%', 'success')
            else:
                flash(f'Ошибка AI валидации: {result.get("error", "?")}', 'danger')
            return _redirect_admin_supplier_product_detail(supplier_id, single_product_id)

        elif product_ids:
            result = SupplierService.ai_validate_bulk(supplier_id, product_ids)
            log_admin_action(
                admin_user_id=current_user.id,
                action='ai_validate_bulk_supplier_products',
                target_type='supplier',
                target_id=supplier_id,
                details={'count': len(product_ids), 'validated': result.get('validated'), 'errors': result.get('errors')},
                request=request
            )
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return jsonify(result)
            flash(f'AI валидация: {result.get("validated", 0)} успешно, {result.get("errors", 0)} ошибок', 'success')
            return _redirect_admin_supplier_products(supplier_id)

        flash('Не выбраны товары', 'warning')
        return _redirect_admin_supplier_products(supplier_id)

    @app.route('/admin/suppliers/<int:supplier_id>/ai/generate-seo', methods=['POST'])
    @login_required
    @admin_required
    def admin_supplier_ai_seo(supplier_id):
        """AI генерация SEO заголовка"""
        product_id = request.form.get('product_id', type=int)
        if not product_id:
            flash('Не указан товар', 'warning')
            return _redirect_admin_supplier_products(supplier_id)

        result = SupplierService.ai_generate_seo(product_id)
        log_admin_action(
            admin_user_id=current_user.id,
            action='ai_generate_seo_supplier_product',
            target_type='supplier_product',
            target_id=product_id,
            details={'supplier_id': supplier_id, 'success': result.get('success')},
            request=request
        )

        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify(result)

        if result.get('success'):
            flash(f'SEO заголовок сгенерирован: {result.get("title", "")}', 'success')
        else:
            flash(f'Ошибка генерации SEO: {result.get("error", "?")}', 'danger')
        return _redirect_admin_supplier_product_detail(supplier_id, product_id)

    @app.route('/admin/suppliers/<int:supplier_id>/ai/generate-desc', methods=['POST'])
    @login_required
    @admin_required
    def admin_supplier_ai_desc(supplier_id):
        """AI генерация описания"""
        product_id = request.form.get('product_id', type=int)
        if not product_id:
            flash('Не указан товар', 'warning')
            return _redirect_admin_supplier_products(supplier_id)

        result = SupplierService.ai_generate_description(product_id)
        log_admin_action(
            admin_user_id=current_user.id,
            action='ai_generate_desc_supplier_product',
            target_type='supplier_product',
            target_id=product_id,
            details={'supplier_id': supplier_id, 'success': result.get('success')},
            request=request
        )

        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify(result)

        if result.get('success'):
            flash('AI описание сгенерировано', 'success')
        else:
            flash(f'Ошибка генерации описания: {result.get("error", "?")}', 'danger')
        return _redirect_admin_supplier_product_detail(supplier_id, product_id)

    @app.route('/admin/suppliers/<int:supplier_id>/ai/analyze', methods=['POST'])
    @login_required
    @admin_required
    def admin_supplier_ai_analyze(supplier_id):
        """AI анализ товара"""
        product_id = request.form.get('product_id', type=int)
        if not product_id:
            flash('Не указан товар', 'warning')
            return _redirect_admin_supplier_products(supplier_id)

        result = SupplierService.ai_analyze_product(product_id)
        log_admin_action(
            admin_user_id=current_user.id,
            action='ai_analyze_supplier_product',
            target_type='supplier_product',
            target_id=product_id,
            details={'supplier_id': supplier_id, 'success': result.get('success'), 'score': result.get('score')},
            request=request
        )

        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify(result)

        if result.get('success'):
            flash(f'AI анализ завершён. Оценка: {result.get("score", 0):.0f}%', 'success')
        else:
            flash(f'Ошибка AI анализа: {result.get("error", "?")}', 'danger')
        return _redirect_admin_supplier_product_detail(supplier_id, product_id)

    @app.route('/admin/suppliers/<int:supplier_id>/ai/enrich', methods=['POST'])
    @login_required
    @admin_required
    def admin_supplier_ai_enrich(supplier_id):
        """Полное AI обогащение товара (SEO + описание + ключевые слова + анализ)"""
        product_id = request.form.get('product_id', type=int)
        if not product_id:
            flash('Не указан товар', 'warning')
            return _redirect_admin_supplier_products(supplier_id)

        result = SupplierService.ai_full_enrich(product_id)
        log_admin_action(
            admin_user_id=current_user.id,
            action='ai_full_enrich_supplier_product',
            target_type='supplier_product',
            target_id=product_id,
            details={'supplier_id': supplier_id, 'success': result.get('success')},
            request=request
        )

        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify(result)

        if result.get('success'):
            flash('AI обогащение завершено', 'success')
        else:
            errors_str = '; '.join(result.get('errors', []))
            flash(f'AI обогащение частично завершено: {errors_str}', 'warning')
        return _redirect_admin_supplier_product_detail(supplier_id, product_id)

    # -------------------------------------------------------------------
    # AI RICH КОНТЕНТ (инфографика)
    # -------------------------------------------------------------------

    @app.route('/admin/suppliers/<int:supplier_id>/ai/rich-content', methods=['POST'])
    @login_required
    @admin_required
    def admin_supplier_ai_rich_content(supplier_id):
        """Генерация Rich-контента (инфографика) для товара"""
        data = request.get_json() or {}
        product_id = data.get('product_id') or request.form.get('product_id', type=int)
        if not product_id:
            return jsonify({'success': False, 'error': 'Не указан товар'}), 400
        if not SupplierProduct.query.filter_by(
                id=product_id, supplier_id=supplier_id).first():
            return jsonify({'success': False, 'error': 'Товар не найден'}), 404

        result = SupplierService.ai_generate_rich_content(product_id)
        log_admin_action(
            admin_user_id=current_user.id,
            action='ai_rich_content_supplier_product',
            target_type='supplier_product',
            target_id=product_id,
            details={'supplier_id': supplier_id, 'success': result.get('success')},
            request=request
        )
        return jsonify(result)

    @app.route('/admin/suppliers/<int:supplier_id>/ai/render-infographic', methods=['POST'])
    @login_required
    @admin_required
    def admin_supplier_render_infographic(supplier_id):
        """Безопасный foreground-композит + детерминированная типографика."""
        data = request.get_json() or {}
        product_id = data.get('product_id')
        slide_index = data.get('slide_index')  # None = все слайды

        if not product_id:
            return jsonify({'success': False, 'error': 'product_id required'}), 400
        if not SupplierProduct.query.filter_by(
                id=product_id, supplier_id=supplier_id).first():
            return jsonify({'success': False, 'error': 'Товар не найден'}), 404

        result = SupplierService.ai_render_hybrid_infographic(product_id)
        if slide_index is not None and result.get('results'):
            selected = [item for item in result['results']
                        if item.get('slide_number') == slide_index + 1]
            result['results'] = selected
            result['total_slides'] = len(selected)
            result['successful'] = sum(1 for item in selected if item.get('success'))
            result['failed'] = len(selected) - result['successful']
        return jsonify(result)

    @app.route('/admin/suppliers/<int:supplier_id>/ai/render-infographic-preview', methods=['POST'])
    @login_required
    @admin_required
    def admin_supplier_render_infographic_preview(supplier_id):
        """Быстрый превью слайда инфографики"""
        data = request.get_json() or {}
        product_id = data.get('product_id')
        slide_index = data.get('slide_index', 0)

        if not product_id:
            return jsonify({'success': False, 'error': 'product_id required'}), 400
        if not SupplierProduct.query.filter_by(
                id=product_id, supplier_id=supplier_id).first():
            return jsonify({'success': False, 'error': 'Товар не найден'}), 404

        result = SupplierService.ai_render_infographic_preview(
            product_id, slide_index=slide_index,
            preview_width=data.get('preview_width', 720)
        )
        return jsonify(result)

    @app.route('/admin/suppliers/<int:supplier_id>/ai/render-hybrid', methods=['POST'])
    @login_required
    @admin_required
    def admin_supplier_render_hybrid(supplier_id):
        """Гибридный рендер: AI-фон + Playwright текст (качественная инфографика)"""
        data = request.get_json() or {}
        product_id = data.get('product_id')

        if not product_id:
            return jsonify({'success': False, 'error': 'product_id required'}), 400
        if not SupplierProduct.query.filter_by(
                id=product_id, supplier_id=supplier_id).first():
            return jsonify({'success': False, 'error': 'Товар не найден'}), 404

        result = SupplierService.ai_render_hybrid_infographic(product_id)
        return jsonify(result)

    @app.route('/admin/suppliers/<int:supplier_id>/ai/history')
    @login_required
    @admin_required
    def admin_supplier_ai_history(supplier_id):
        """История AI операций для товаров поставщика"""
        supplier = SupplierService.get_supplier(supplier_id)
        if not supplier:
            flash('Поставщик не найден', 'danger')
            return redirect(url_for('admin_suppliers'))

        page = request.args.get('page', 1, type=int)

        # Получаем ID всех ImportedProduct, связанных с этим поставщиком
        subq = db.session.query(ImportedProduct.id).filter(
            ImportedProduct.supplier_id == supplier_id
        ).subquery()

        history_query = AIHistory.query.filter(
            AIHistory.imported_product_id.in_(subq)
        ).order_by(AIHistory.created_at.desc())

        pagination = history_query.paginate(page=page, per_page=50, error_out=False)

        return render_template('admin_supplier_ai_history.html',
                               supplier=supplier, pagination=pagination)

    # -------------------------------------------------------------------
    # AI ПОЛНЫЙ ПАРСИНГ ТОВАРА
    # -------------------------------------------------------------------

    @app.route('/admin/suppliers/<int:supplier_id>/ai/parse', methods=['POST'])
    @login_required
    @admin_required
    def admin_supplier_ai_parse(supplier_id):
        """Retired legacy start: move an exact selection to the Flash review page."""
        if not SupplierService.get_supplier(supplier_id):
            return jsonify({'code': 'supplier_not_found', 'error': 'Поставщик не найден'}), 404
        product_id = request.form.get('product_id', '')
        product_ids_raw = request.form.getlist('product_ids')
        values = product_ids_raw or ([product_id] if product_id else [])
        if (not values or len(values) > 5000 or
                any(len(value) > 20 or not value.isdigit() or int(value) <= 0
                    for value in values) or
                len({int(value) for value in values}) != len(values)):
            return jsonify({
                'code': 'invalid_product_ids',
                'error': 'Выберите от 1 до 5000 уникальных товаров.',
            }), 400
        next_url = url_for(
            'admin_supplier_catalog_enrichment', supplier_id=supplier_id,
            handoff='ids', product_ids=','.join(str(int(value)) for value in values),
        )
        return jsonify({
            'code': 'legacy_parser_retired',
            'error': 'Запуск старого полного AI-парсера закрыт. Проверьте выбор и режим Flash.',
            'next_url': next_url,
        }), 409

    @app.route('/admin/suppliers/<int:supplier_id>/ai/parse-job/<job_id>/status')
    @login_required
    @admin_required
    def admin_supplier_ai_parse_status(supplier_id, job_id):
        """API: статус фоновой задачи AI парсинга (поллинг)"""
        from models import AIParseJob
        # Проверяем stale job при каждом поллинге
        job = AIParseJob.query.get(job_id)
        if job and job.status in ('pending', 'running') and job.is_stale:
            job.status = 'failed'
            stale_sec = int((datetime.utcnow() - (job.heartbeat_at or job.created_at)).total_seconds())
            job.error_message = (
                f'Задача зависла (воркер не отвечает {stale_sec}с). '
                f'Обработано {job.processed} из {job.total} товаров '
                f'({job.succeeded} успешно, {job.failed} с ошибкой). '
                f'Попробуйте повторить запуск.'
            )
            job.updated_at = datetime.utcnow()
            try:
                db.session.commit()
            except Exception:
                db.session.rollback()

        data = SupplierService.get_ai_parse_job(job_id)
        if not data:
            return jsonify({'error': 'Задача не найдена'}), 404
        return jsonify(data)

    @app.route('/admin/suppliers/<int:supplier_id>/ai/parse-job/<job_id>/cancel', methods=['POST'])
    @login_required
    @admin_required
    def admin_supplier_ai_parse_cancel(supplier_id, job_id):
        """API: отмена фоновой задачи AI парсинга"""
        ok = SupplierService.cancel_ai_parse_job(job_id)
        return jsonify({'cancelled': ok})

    @app.route('/admin/suppliers/<int:supplier_id>/ai/parse-by-filter', methods=['POST'])
    @login_required
    @admin_required
    def admin_supplier_ai_parse_by_filter(supplier_id):
        """Retired legacy filter start; preserve filters for strict Flash review."""
        if not SupplierService.get_supplier(supplier_id):
            return jsonify({'code': 'supplier_not_found', 'error': 'Поставщик не найден'}), 404
        names = (
            'search', 'stock_status', 'parse_status', 'fill_max',
            'category', 'brand', 'ai_model', 'has_description',
            'price_min', 'price_max', 'limit',
        )
        if any(len((request.form.get(name) or '').strip()) > 300 for name in names):
            return jsonify({
                'code': 'filter_too_long',
                'error': 'Фильтр слишком длинный; сократите значение до 300 символов.',
            }), 400
        next_url = url_for(
            'admin_supplier_catalog_enrichment', supplier_id=supplier_id,
            handoff='filter',
            **{f'legacy_{name}': (request.form.get(name) or '').strip()
               for name in names if request.form.get(name)},
        )
        return jsonify({
            'code': 'legacy_parser_retired',
            'error': 'Запуск старого полного AI-парсера закрыт. Проверьте фильтр и режим Flash.',
            'next_url': next_url,
        }), 409

    @app.route('/admin/suppliers/<int:supplier_id>/products/<int:product_id>/refresh-data')
    @login_required
    @admin_required
    def admin_supplier_product_refresh_data(supplier_id, product_id):
        """API: свежие данные товара для динамического обновления карточки"""
        product = SupplierService.get_product(product_id)
        if not product or product.supplier_id != supplier_id:
            return jsonify({'error': 'Товар не найден'}), 404

        parsed = product.get_ai_parsed_data()
        pm = parsed.get('parsing_meta', {})
        mp_fields = product.get_marketplace_fields()

        # Собираем статистику заполненности
        completeness = _calc_product_completeness(product)

        return jsonify({
            'title': product.title or '',
            'brand': product.brand or '',
            'description': product.description or '',
            'category': product.category or '',
            'wb_category_name': product.wb_category_name or '',
            'wb_subject_id': product.wb_subject_id or '',
            'gender': product.gender or '',
            'country': product.country or '',
            'ai_seo_title': product.ai_seo_title or '',
            'ai_description': product.ai_description or '',
            'ai_keywords_json': product.ai_keywords_json or '',
            'ai_parsed_data_json': product.ai_parsed_data_json or '',
            'ai_model_used': product.ai_model_used or '',
            'ai_parsed_at': product.ai_parsed_at.strftime('%d.%m.%Y %H:%M') if product.ai_parsed_at else '',
            'parsing_meta': pm,
            'marketplace_fields': mp_fields,
            'marketplace_validation_status': product.marketplace_validation_status or '',
            'marketplace_fill_pct': product.marketplace_fill_pct or 0,
            'completeness': completeness,
        })

    @app.route('/admin/suppliers/<int:supplier_id>/products/<int:product_id>/raw-json')
    @login_required
    @admin_required
    def admin_supplier_product_raw_json(supplier_id, product_id):
        """Полный JSON дамп товара для анализа"""
        supplier = SupplierService.get_supplier(supplier_id)
        product = SupplierService.get_product(product_id)
        if not supplier or not product or product.supplier_id != supplier_id:
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return jsonify({'error': 'Товар не найден'}), 404
            flash('Товар не найден', 'danger')
            return redirect(url_for('admin_supplier_products', supplier_id=supplier_id))

        raw_data = SupplierService.get_product_raw_json(product_id)

        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify(raw_data)

        return render_template('admin_supplier_product_raw_json.html',
                               supplier=supplier, product=product,
                               raw_data=json.dumps(raw_data, ensure_ascii=False, indent=2))

    # -------------------------------------------------------------------
    # СИНХРОНИЗАЦИЯ ОПИСАНИЙ (фон)
    # -------------------------------------------------------------------

    @app.route('/admin/suppliers/<int:supplier_id>/marketplaces', methods=['GET'])
    @login_required
    @admin_required
    def admin_supplier_marketplaces(supplier_id):
        """Интеграции с маркетплейсами для поставщика"""
        supplier = Supplier.query.get_or_404(supplier_id)
        from models import Marketplace, MarketplaceConnection, MarketplaceCategory
        marketplaces = Marketplace.query.all()
        connections_qs = MarketplaceConnection.query.filter_by(supplier_id=supplier.id).all()
        connections = {c.marketplace_id: c for c in connections_qs}

        # Pre-load only enabled categories per marketplace for dropdown (avoid loading 10k+ categories)
        enabled_categories = {}
        for mp in marketplaces:
            enabled_categories[mp.id] = MarketplaceCategory.query.filter_by(
                marketplace_id=mp.id, is_enabled=True
            ).order_by(MarketplaceCategory.subject_name).all()

        return render_template('admin_supplier_marketplaces.html',
                               supplier=supplier,
                               marketplaces=marketplaces,
                               connections=connections,
                               enabled_categories=enabled_categories)

    @app.route('/admin/suppliers/<int:supplier_id>/marketplaces/update', methods=['POST'])
    @login_required
    @admin_required
    def admin_supplier_marketplaces_update(supplier_id):
        """Обновление настроек интеграции"""
        supplier = Supplier.query.get_or_404(supplier_id)
        data = request.json
        marketplace_id = data.get('marketplace_id')
        if not marketplace_id:
            return jsonify({"success": False, "error": "Missing marketplace_id"}), 400
            
        from models import MarketplaceConnection, db
        conn = MarketplaceConnection.query.filter_by(supplier_id=supplier.id, marketplace_id=marketplace_id).first()
        if not conn:
            conn = MarketplaceConnection(supplier_id=supplier.id, marketplace_id=marketplace_id)
            db.session.add(conn)
            
        if 'is_active' in data:
            conn.is_active = bool(data['is_active'])
        if 'auto_map_categories' in data:
            conn.auto_map_categories = bool(data['auto_map_categories'])
        if 'default_category_id' in data:
            try:
                conn.default_category_id = int(data['default_category_id']) if data['default_category_id'] else None
            except (ValueError, TypeError):
                conn.default_category_id = None
                
        db.session.commit()
        return jsonify({"success": True})


    @app.route('/admin/suppliers/<int:supplier_id>/sync-descriptions', methods=['POST'])
    @login_required
    @admin_required
    def admin_supplier_sync_descriptions(supplier_id):
        """Синхронизация описаний — запуск в фоне"""
        supplier = SupplierService.get_supplier(supplier_id)
        if not supplier:
            flash('Поставщик не найден', 'danger')
            return redirect(url_for('admin_suppliers'))

        if not supplier.description_file_url:
            flash('URL файла описаний не задан', 'warning')
            return redirect(url_for('admin_supplier_edit', supplier_id=supplier_id))

        result = SupplierService.start_description_sync_job(
            supplier_id, admin_user_id=current_user.id
        )

        log_admin_action(
            admin_user_id=current_user.id,
            action='sync_supplier_descriptions',
            target_type='supplier',
            target_id=supplier_id,
            details={'job_id': result.get('job_id')},
            request=request
        )

        if result.get('error'):
            flash(f'Ошибка: {result["error"]}', 'danger')
        else:
            flash('Синхронизация описаний запущена в фоне', 'success')

        return redirect(url_for('admin_supplier_ai_parser', supplier_id=supplier_id))

    # -------------------------------------------------------------------
    # AI ПАРСЕР — СТРАНИЦА
    # -------------------------------------------------------------------

    @app.route('/admin/suppliers/<int:supplier_id>/ai/parser')
    @login_required
    @admin_required
    def admin_supplier_ai_parser(supplier_id):
        """Страница AI парсера с выбором товаров"""
        supplier = SupplierService.get_supplier(supplier_id)
        if not supplier:
            flash('Поставщик не найден', 'danger')
            return redirect(url_for('admin_suppliers'))

        page = request.args.get('page', 1, type=int)
        search = request.args.get('search', '').strip()
        stock_status = request.args.get('stock_status', '').strip()
        parse_status = request.args.get('parse_status', '').strip()
        fill_max = request.args.get('fill_max', '', type=str).strip()
        per_page = request.args.get('per_page', 50, type=int)
        per_page = max(10, min(per_page, 500))
        auto_select = request.args.get('auto_select', '').strip()
        # Новые фильтры
        category_filter = request.args.get('category', '').strip()
        brand_filter = request.args.get('brand', '').strip()
        model_filter = request.args.get('ai_model', '').strip()
        has_description = request.args.get('has_description', '').strip()
        price_min = request.args.get('price_min', '', type=str).strip()
        price_max = request.args.get('price_max', '', type=str).strip()

        query = SupplierProduct.query.filter_by(supplier_id=supplier_id)
        if search:
            search_term = f'%{search}%'
            query = query.filter(
                db.or_(
                    SupplierProduct.title.ilike(search_term),
                    SupplierProduct.external_id.ilike(search_term),
                    SupplierProduct.brand.ilike(search_term),
                )
            )
        if stock_status == 'in_stock':
            query = query.filter(SupplierProduct.supplier_status == 'in_stock')
        elif stock_status == 'out_of_stock':
            query = query.filter(SupplierProduct.supplier_status == 'out_of_stock')

        # Фильтр по статусу AI парсинга
        if parse_status == 'not_parsed':
            query = query.filter(SupplierProduct.ai_parsed_data_json.is_(None))
        elif parse_status == 'parsed':
            query = query.filter(SupplierProduct.ai_parsed_data_json.isnot(None))
        elif parse_status == 'fill_below' and fill_max:
            try:
                fill_threshold = float(fill_max)
                query = query.filter(
                    db.or_(
                        SupplierProduct.ai_fill_pct.is_(None),
                        SupplierProduct.ai_fill_pct < fill_threshold
                    )
                )
            except (ValueError, TypeError):
                pass

        # Фильтр по категории
        if category_filter:
            cat_term = f'%{category_filter}%'
            query = query.filter(
                db.or_(
                    SupplierProduct.wb_category_name.ilike(cat_term),
                    SupplierProduct.category.ilike(cat_term),
                )
            )

        # Фильтр по бренду
        if brand_filter:
            query = query.filter(SupplierProduct.brand.ilike(f'%{brand_filter}%'))

        # Фильтр по AI модели
        if model_filter:
            query = query.filter(SupplierProduct.ai_model_used.ilike(f'%{model_filter}%'))

        # Фильтр по наличию описания
        if has_description == 'yes':
            query = query.filter(
                SupplierProduct.description.isnot(None),
                SupplierProduct.description != ''
            )
        elif has_description == 'no':
            query = query.filter(
                db.or_(
                    SupplierProduct.description.is_(None),
                    SupplierProduct.description == ''
                )
            )

        # Фильтр по цене
        if price_min:
            try:
                query = query.filter(SupplierProduct.supplier_price >= float(price_min))
            except (ValueError, TypeError):
                pass
        if price_max:
            try:
                query = query.filter(SupplierProduct.supplier_price <= float(price_max))
            except (ValueError, TypeError):
                pass

        pagination = query.order_by(SupplierProduct.title).paginate(
            page=page, per_page=per_page, error_out=False
        )

        stats = SupplierService.get_product_stats(supplier_id)

        try:
            parsed_count = SupplierProduct.query.filter(
                SupplierProduct.supplier_id == supplier_id,
                SupplierProduct.ai_parsed_data_json.isnot(None)
            ).count()
        except Exception:
            parsed_count = 0

        try:
            active_jobs = SupplierService.get_active_ai_parse_jobs(supplier_id)
            recent_jobs = SupplierService.get_recent_ai_parse_jobs(supplier_id, limit=5)
        except Exception as e:
            app.logger.warning(f"ai_parse_jobs query failed (table may not exist): {e}")
            active_jobs = []
            recent_jobs = []

        # Уникальные категории и бренды для выпадающих списков
        try:
            categories_list = [r[0] for r in db.session.query(
                db.func.coalesce(SupplierProduct.wb_category_name, SupplierProduct.category)
            ).filter(
                SupplierProduct.supplier_id == supplier_id,
                db.or_(
                    SupplierProduct.wb_category_name.isnot(None),
                    SupplierProduct.category.isnot(None)
                )
            ).distinct().order_by(
                db.func.coalesce(SupplierProduct.wb_category_name, SupplierProduct.category)
            ).all() if r[0]]
        except Exception:
            categories_list = []

        try:
            brands_list = [r[0] for r in db.session.query(
                SupplierProduct.brand
            ).filter(
                SupplierProduct.supplier_id == supplier_id,
                SupplierProduct.brand.isnot(None),
                SupplierProduct.brand != ''
            ).distinct().order_by(SupplierProduct.brand).all() if r[0]]
        except Exception:
            brands_list = []

        try:
            models_list = [r[0] for r in db.session.query(
                SupplierProduct.ai_model_used
            ).filter(
                SupplierProduct.supplier_id == supplier_id,
                SupplierProduct.ai_model_used.isnot(None)
            ).distinct().order_by(SupplierProduct.ai_model_used).all() if r[0]]
        except Exception:
            models_list = []

        return render_template('admin_supplier_ai_parser.html',
                               supplier=supplier, pagination=pagination,
                               stats=stats, search=search,
                               stock_status=stock_status,
                               parse_status=parse_status,
                               fill_max=fill_max,
                               per_page=per_page,
                               auto_select=auto_select,
                               parsed_count=parsed_count,
                               active_jobs=active_jobs,
                               recent_jobs=recent_jobs,
                               category_filter=category_filter,
                               brand_filter=brand_filter,
                               model_filter=model_filter,
                               has_description=has_description,
                               price_min=price_min,
                               price_max=price_max,
                               categories_list=categories_list,
                               brands_list=brands_list,
                               models_list=models_list)

    # -------------------------------------------------------------------
    # Управление подключёнными продавцами
    # -------------------------------------------------------------------
    @app.route('/admin/suppliers/<int:supplier_id>/sellers')
    @login_required
    @admin_required
    def admin_supplier_sellers(supplier_id):
        supplier = SupplierService.get_supplier(supplier_id)
        if not supplier:
            flash('Поставщик не найден', 'danger')
            return redirect(url_for('admin_suppliers'))

        connections = SupplierService.get_supplier_sellers(supplier_id, active_only=False)
        # Все продавцы для выбора подключения
        all_sellers = Seller.query.join(Seller.user).order_by(Seller.company_name).all()
        connected_seller_ids = {c.seller_id for c in connections if c.is_active}

        # Примеры external_id для live-preview артикулов
        sample_products = db.session.query(
            SupplierProduct.external_id, SupplierProduct.vendor_code
        ).filter(
            SupplierProduct.supplier_id == supplier_id,
            SupplierProduct.external_id.isnot(None),
            SupplierProduct.external_id != ''
        ).limit(3).all()
        sample_external_ids = [
            {'external_id': p.external_id, 'vendor_code': p.vendor_code or ''}
            for p in sample_products
        ]

        return render_template('admin_supplier_sellers.html',
                               supplier=supplier, connections=connections,
                               all_sellers=all_sellers,
                               connected_seller_ids=connected_seller_ids,
                               sample_external_ids=sample_external_ids)

    @app.route('/admin/suppliers/<int:supplier_id>/sellers/connect', methods=['POST'])
    @login_required
    @admin_required
    def admin_supplier_connect_seller(supplier_id):
        seller_id = request.form.get('seller_id', type=int)
        supplier_code = request.form.get('supplier_code', '').strip()
        vendor_code_pattern = request.form.get('vendor_code_pattern', '').strip()

        if not seller_id:
            flash('Выберите продавца', 'warning')
            return redirect(url_for('admin_supplier_sellers', supplier_id=supplier_id))

        SupplierService.connect_seller(
            seller_id, supplier_id,
            supplier_code=supplier_code,
            vendor_code_pattern=vendor_code_pattern or None
        )

        log_admin_action(
            admin_user_id=current_user.id,
            action='connect_seller_to_supplier',
            target_type='supplier',
            target_id=supplier_id,
            details={'seller_id': seller_id},
            request=request
        )

        flash('Продавец подключён', 'success')
        return redirect(url_for('admin_supplier_sellers', supplier_id=supplier_id))

    @app.route('/admin/suppliers/<int:supplier_id>/sellers/<int:seller_id>/update', methods=['POST'])
    @login_required
    @admin_required
    def admin_supplier_update_seller(supplier_id, seller_id):
        """Обновить настройки подключения продавца к поставщику"""
        conn = SellerSupplier.query.filter_by(
            seller_id=seller_id, supplier_id=supplier_id
        ).first_or_404()

        new_code = request.form.get('supplier_code', '').strip()
        new_pattern = request.form.get('vendor_code_pattern', '').strip()

        if new_code:
            conn.supplier_code = new_code
        if new_pattern:
            conn.vendor_code_pattern = new_pattern

        db.session.commit()
        flash('Настройки подключения обновлены', 'success')
        return redirect(url_for('admin_supplier_sellers', supplier_id=supplier_id))

    @app.route('/admin/suppliers/<int:supplier_id>/sellers/<int:seller_id>/regenerate-vendor-codes', methods=['POST'])
    @login_required
    @admin_required
    def admin_supplier_regenerate_vendor_codes(supplier_id, seller_id):
        """Пересобрать артикулы (vendorCode) для товаров продавца у поставщика"""
        from services.pricing_engine import generate_vendor_code

        conn = SellerSupplier.query.filter_by(
            seller_id=seller_id, supplier_id=supplier_id, is_active=True
        ).first_or_404()

        supplier = SupplierService.get_supplier(supplier_id)

        # Фильтр: только в наличии?
        stock_only = request.form.get('stock_only') == '1'
        limit = request.form.get('limit', 0, type=int)

        # Находим ImportedProduct для этого продавца+поставщика
        query = ImportedProduct.query.filter_by(
            seller_id=seller_id,
            supplier_id=supplier_id
        )

        if stock_only:
            # Только товары, у которых SupplierProduct в наличии
            query = query.join(
                SupplierProduct,
                ImportedProduct.supplier_product_id == SupplierProduct.id
            ).filter(
                SupplierProduct.supplier_status == 'in_stock',
                SupplierProduct.supplier_quantity > 0
            )

        if limit > 0:
            query = query.limit(limit)

        imported_products = query.all()

        pattern = conn.vendor_code_pattern or supplier.default_vendor_code_pattern or 'id-{product_id}-{supplier_code}'
        sup_code = conn.supplier_code or ''

        updated = 0
        for ip in imported_products:
            new_vc = generate_vendor_code(
                pattern=pattern,
                supplier_code=sup_code,
                external_id=ip.external_id,
                external_vendor_code=ip.external_vendor_code or '',
                supplier=supplier,
                fallback_id=ip.id,
                fallback_seller_id=seller_id,
            )

            # Обновляем Product.vendor_code если есть связанный товар
            if ip.product_id:
                product = Product.query.get(ip.product_id)
                if product and product.vendor_code != new_vc:
                    product.vendor_code = new_vc
                    updated += 1

        db.session.commit()

        log_admin_action(
            admin_user_id=current_user.id,
            action='regenerate_vendor_codes',
            target_type='supplier',
            target_id=supplier_id,
            details={
                'seller_id': seller_id,
                'pattern': pattern,
                'total_processed': len(imported_products),
                'updated': updated,
                'stock_only': stock_only,
            },
            request=request
        )

        flash(f'Артикулы пересобраны: {updated} обновлено из {len(imported_products)} обработанных', 'success')
        return redirect(url_for('admin_supplier_sellers', supplier_id=supplier_id))

    @app.route('/admin/suppliers/<int:supplier_id>/sellers/<int:seller_id>/disconnect', methods=['POST'])
    @login_required
    @admin_required
    def admin_supplier_disconnect_seller(supplier_id, seller_id):
        SupplierService.disconnect_seller(seller_id, supplier_id)

        log_admin_action(
            admin_user_id=current_user.id,
            action='disconnect_seller_from_supplier',
            target_type='supplier',
            target_id=supplier_id,
            details={'seller_id': seller_id},
            request=request
        )

        flash('Продавец отключён', 'success')
        return redirect(url_for('admin_supplier_sellers', supplier_id=supplier_id))

    # ===================================================================
    # КАТАЛОГ ПОСТАВЩИКА ДЛЯ ПРОДАВЦА
    # ===================================================================

    @app.route('/supplier-catalog')
    @login_required
    @seller_required
    def supplier_catalog():
        """Список доступных поставщиков для продавца"""
        seller = current_user.seller
        connections = SupplierService.get_seller_suppliers(seller.id)
        supplier_ids = [c.supplier_id for c in connections]

        # Прогреваем identity map одним SELECT — lazy conn.supplier дальше
        # не ходит в БД; счётчики импорта берём одним group_by вместо
        # COUNT-запроса на каждого поставщика.
        if supplier_ids:
            Supplier.query.filter(Supplier.id.in_(supplier_ids)).all()
            imported_counts = dict(
                db.session.query(
                    ImportedProduct.supplier_id,
                    db.func.count(ImportedProduct.id),
                ).filter(
                    ImportedProduct.seller_id == seller.id,
                    ImportedProduct.supplier_id.in_(supplier_ids),
                ).group_by(ImportedProduct.supplier_id).all()
            )
        else:
            imported_counts = {}

        suppliers_data = []
        for conn in connections:
            supplier = conn.supplier
            stats = SupplierService.get_product_stats(supplier.id)
            suppliers_data.append({
                'supplier': supplier,
                'connection': conn,
                'stats': stats,
                'imported_count': imported_counts.get(supplier.id, 0),
            })

        return render_template('supplier_catalog.html',
                               suppliers_data=suppliers_data)

    @app.route('/supplier-catalog/<int:supplier_id>/products')
    @login_required
    @seller_required
    def supplier_catalog_products(supplier_id):
        """Каталог товаров поставщика для продавца"""
        seller = current_user.seller

        # Проверяем подключение
        conn = SellerSupplier.query.filter_by(
            seller_id=seller.id, supplier_id=supplier_id, is_active=True
        ).first()
        if not conn:
            flash('У вас нет доступа к этому поставщику', 'danger')
            return redirect(url_for('supplier_catalog'))

        supplier = SupplierService.get_supplier(supplier_id)
        if not supplier:
            flash('Поставщик не найден', 'danger')
            return redirect(url_for('supplier_catalog'))

        select_all_requested = request.args.get('select_all') == '1'
        page = 1 if select_all_requested else request.args.get('page', 1, type=int)
        search = request.args.get('search', '').strip()
        show_imported = request.args.get('show_imported', '0') == '1'
        updates_only = request.args.get('updates_only', '0') == '1'
        # По умолчанию показываем только товары в наличии
        stock_status = request.args.get('stock_status', 'in_stock').strip()
        if stock_status not in ('in_stock', 'out_of_stock', 'all'):
            stock_status = 'in_stock'
        effective_stock = stock_status if stock_status != 'all' else None
        # Фильтр по WB: on_wb / not_on_wb / all
        wb_filter = request.args.get('wb_filter', '').strip()
        if wb_filter not in ('on_wb', 'not_on_wb'):
            wb_filter = None
        # Фильтр по AI-парсингу: ai_parsed / not_parsed / all
        ai_filter = request.args.get('ai_filter', '').strip()
        if ai_filter not in ('ai_parsed', 'not_parsed'):
            ai_filter = None

        # on_wb подразумевает show_imported=True (товары на WB всегда импортированы)
        # not_on_wb — не подразумевает, чтобы скрывать импортированные но не на WB
        effective_show_imported = (
            show_imported or wb_filter == 'on_wb' or updates_only
        )

        stale_supplier_copy_exists = db.session.query(
            ImportedProduct.id
        ).filter(
            ImportedProduct.seller_id == seller.id,
            ImportedProduct.supplier_product_id == SupplierProduct.id,
            ImportedProduct.supplier_content_revision
            < SupplierProduct.content_revision,
        ).exists()
        supplier_update_count = SupplierProduct.query.filter(
            SupplierProduct.supplier_id == supplier_id,
            stale_supplier_copy_exists,
        ).count()

        # Определение «уже на WB»: по связи ImportedProduct → Product и по
        # совпадению вычисленного артикула. Полный проход по каталогу нужен
        # только когда wb-фильтр реально фильтрует выборку; для бейджей
        # достаточно товаров текущей страницы.
        from services.pricing_engine import resolve_vendor_code_settings, generate_vendor_code

        vc_pattern, vc_supplier_code, _ = resolve_vendor_code_settings(
            seller.id, supplier_id
        )

        def _linked_wb_sp_ids(page_ids=None):
            """SP этого поставщика, уже связанные с WB-карточкой продавца."""
            q = db.session.query(ImportedProduct.supplier_product_id).join(
                SupplierProduct,
                SupplierProduct.id == ImportedProduct.supplier_product_id,
            ).filter(
                ImportedProduct.seller_id == seller.id,
                ImportedProduct.product_id.isnot(None),
                SupplierProduct.supplier_id == supplier_id,
            )
            if page_ids is not None:
                q = q.filter(ImportedProduct.supplier_product_id.in_(page_ids))
            return {row[0] for row in q.all()}

        def _vendor_code_wb_sp_ids(exclude_ids, page_ids=None):
            """SP, чей вычисленный артикул уже существует среди Product WB."""
            q = db.session.query(
                SupplierProduct.id,
                SupplierProduct.external_id,
                SupplierProduct.vendor_code,
            ).filter(SupplierProduct.supplier_id == supplier_id)
            if page_ids is not None:
                q = q.filter(SupplierProduct.id.in_(page_ids))
            if exclude_ids:
                q = q.filter(~SupplierProduct.id.in_(exclude_ids))
            vc_to_sp = {}
            for sp_id, ext_id, ext_vc in q.all():
                vc = generate_vendor_code(
                    pattern=vc_pattern,
                    supplier_code=vc_supplier_code,
                    external_id=ext_id,
                    external_vendor_code=str(ext_vc or ''),
                    supplier=supplier,
                )
                if vc:
                    vc_to_sp[vc] = sp_id
            if not vc_to_sp:
                return set()
            existing_vc = {
                row[0] for row in db.session.query(Product.vendor_code).filter(
                    Product.seller_id == seller.id,
                    Product.vendor_code.in_(list(vc_to_sp.keys()))
                ).all()
            }
            return {vc_to_sp[vc] for vc in existing_vc}

        wb_existing_sp_ids = set()
        if wb_filter:
            wb_existing_sp_ids = _linked_wb_sp_ids()
            wb_existing_sp_ids |= _vendor_code_wb_sp_ids(wb_existing_sp_ids)

        page_size = MAX_BULK_PRODUCTS + 1 if select_all_requested else 50
        pagination = SupplierService.get_available_products_for_seller(
            seller.id, supplier_id,
            page=page, per_page=page_size,
            search=search, show_imported=effective_show_imported,
            stock_status=effective_stock,
            wb_filter=wb_filter,
            ai_filter=ai_filter,
            updates_only=updates_only,
            wb_existing_sp_ids=wb_existing_sp_ids
        )
        selection_review = False
        selection_block_reason = None
        selection_block_total = None
        if select_all_requested:
            selection_block_total = pagination.total
            if pagination.total > MAX_BULK_PRODUCTS:
                selection_block_reason = 'limit'
            elif len(pagination.items) != pagination.total:
                selection_block_reason = 'changed'
            else:
                selection_review = True
            if selection_block_reason:
                # Never render or submit a truncated all-filtered result. Keep
                # the standard 50-row page so the seller can narrow or select
                # an explicit subset instead.
                pagination = SupplierService.get_available_products_for_seller(
                    seller.id, supplier_id,
                    page=1, per_page=50,
                    search=search, show_imported=effective_show_imported,
                    stock_status=effective_stock,
                    wb_filter=wb_filter,
                    ai_filter=ai_filter,
                    updates_only=updates_only,
                    wb_existing_sp_ids=wb_existing_sp_ids,
                )
        page_sp_ids = [p.id for p in pagination.items]
        if not wb_filter and page_sp_ids:
            wb_existing_sp_ids = _linked_wb_sp_ids(page_sp_ids)
            wb_existing_sp_ids |= _vendor_code_wb_sp_ids(
                wb_existing_sp_ids, page_sp_ids)

        # Статусы импорта/обновлений нужны только строкам текущей страницы
        imported_revision_by_sp = {
            row[0]: int(row[1] or 0)
            for row in db.session.query(
                ImportedProduct.supplier_product_id,
                ImportedProduct.supplier_content_revision,
            ).filter(
                ImportedProduct.seller_id == seller.id,
                ImportedProduct.supplier_product_id.in_(page_sp_ids),
            ).all()
        } if page_sp_ids else {}
        imported_sp_ids = set(imported_revision_by_sp)
        supplier_update_sp_ids = {
            product.id for product in pagination.items
            if product.id in imported_revision_by_sp
            and int(product.content_revision or 1)
            > imported_revision_by_sp[product.id]
        }
        selected_import_ids = [
            product.id for product in pagination.items
            if selection_review
            and product.id not in imported_sp_ids
            and product.id not in supplier_update_sp_ids
            and product.id not in wb_existing_sp_ids
        ]

        stats = SupplierService.get_product_stats(supplier_id)
        price_stock_stats = SupplierService.get_price_stock_stats(supplier_id)

        # Каналы для блока «Подготовить Ozon» в форме импорта: активные
        # кабинеты + отметка, какие уже покрыты авто-публикацией (для них
        # черновики создаются при импорте автоматически).
        ozon_import_accounts = []
        ozon_auto_account_ids = set()
        if current_app.config.get('MARKETPLACE_OZON_ENABLED', False):
            ozon_import_accounts = SellerMarketplaceAccount.query.join(
                Marketplace
            ).filter(
                SellerMarketplaceAccount.seller_id == seller.id,
                SellerMarketplaceAccount.is_active.is_(True),
                Marketplace.code == 'ozon',
                Marketplace.is_active.is_(True),
            ).order_by(
                SellerMarketplaceAccount.is_default.desc(),
                SellerMarketplaceAccount.label.asc(),
            ).all()
            if ozon_import_accounts:
                from models import AutoPublishSettings
                ozon_auto_account_ids = {
                    row[0]
                    for row in db.session.query(
                        AutoPublishSettings.account_id
                    ).filter(
                        AutoPublishSettings.seller_id == seller.id,
                        AutoPublishSettings.marketplace_code == 'ozon',
                        AutoPublishSettings.is_enabled.is_(True),
                        AutoPublishSettings.account_id.in_(
                            [a.id for a in ozon_import_accounts]
                        ),
                    ).all()
                }

        # Индикатор «есть Ozon-черновик» для карточек страницы каталога
        ozon_draft_sp_ids = set()
        if ozon_import_accounts:
            page_sp_ids = [p.id for p in pagination.items]
            if page_sp_ids:
                ozon_draft_sp_ids = {
                    row[0]
                    for row in db.session.query(
                        ImportedProduct.supplier_product_id
                    ).join(
                        MarketplaceProductDraft,
                        MarketplaceProductDraft.imported_product_id
                        == ImportedProduct.id,
                    ).filter(
                        ImportedProduct.seller_id == seller.id,
                        MarketplaceProductDraft.seller_id == seller.id,
                        ImportedProduct.supplier_product_id.in_(page_sp_ids),
                    ).all()
                }

        catalog_filter_args = {
            'page': 1,
            'search': search,
            'show_imported': '1' if show_imported else None,
            'updates_only': '1' if updates_only else None,
            'stock_status': stock_status,
            'wb_filter': wb_filter or None,
            'ai_filter': ai_filter or None,
        }
        select_all_url = url_for(
            'supplier_catalog_products', supplier_id=supplier_id,
            select_all='1', **catalog_filter_args,
        )
        cancel_select_all_url = url_for(
            'supplier_catalog_products', supplier_id=supplier_id,
            **catalog_filter_args,
        )
        return render_template('supplier_catalog_products.html',
                               supplier=supplier, pagination=pagination,
                               stats=stats, search=search,
                               selection_review=selection_review,
                               selection_block_reason=selection_block_reason,
                               selection_block_total=selection_block_total,
                               selected_import_ids=selected_import_ids,
                               select_all_url=select_all_url,
                               cancel_select_all_url=cancel_select_all_url,
                               show_imported=show_imported,
                               updates_only=updates_only,
                               supplier_update_count=supplier_update_count,
                               stock_status=stock_status,
                               wb_filter=wb_filter or '',
                               ai_filter=ai_filter or '',
                               price_stock_stats=price_stock_stats,
                               imported_sp_ids=imported_sp_ids,
                               supplier_update_sp_ids=supplier_update_sp_ids,
                               wb_existing_sp_ids=wb_existing_sp_ids,
                               connection=conn,
                               ozon_import_accounts=ozon_import_accounts,
                               ozon_auto_account_ids=ozon_auto_account_ids,
                               ozon_draft_sp_ids=ozon_draft_sp_ids)

    @app.route('/supplier-catalog/<int:supplier_id>/products/<int:product_id>')
    @login_required
    @seller_required
    def supplier_catalog_product_detail(supplier_id, product_id):
        """Детали товара поставщика (просмотр для продавца)"""
        seller = current_user.seller
        conn = SellerSupplier.query.filter_by(
            seller_id=seller.id, supplier_id=supplier_id, is_active=True
        ).first()
        if not conn:
            flash('У вас нет доступа к этому поставщику', 'danger')
            return redirect(url_for('supplier_catalog'))

        supplier = SupplierService.get_supplier(supplier_id)
        product = SupplierService.get_product(product_id)
        if not supplier or not product or product.supplier_id != supplier_id:
            flash('Товар не найден', 'danger')
            return redirect(url_for('supplier_catalog_products', supplier_id=supplier_id))

        # Проверяем импортирован ли уже
        existing_import = ImportedProduct.query.filter_by(
            seller_id=seller.id, supplier_product_id=product_id
        ).first()

        product_fact_groups = SupplierService.get_product_detail_fact_groups(
            product
        )

        return render_template('supplier_catalog_product_detail.html',
                               supplier=supplier, product=product,
                               existing_import=existing_import,
                               product_fact_groups=product_fact_groups)

    # -------------------------------------------------------------------
    # Импорт товаров к продавцу
    # -------------------------------------------------------------------
    @app.route('/supplier-catalog/import', methods=['POST'])
    @login_required
    @seller_required
    def supplier_catalog_import():
        """Импорт выбранных товаров к продавцу"""
        seller = current_user.seller
        try:
            supplier_id, product_ids = _supplier_import_form_selection(
                seller.id, request.form,
            )
        except ValueError as exc:
            flash(str(exc), 'warning')
            return _redirect_seller_supplier_catalog(
                request.form.get('supplier_id', type=int)
            )

        if not product_ids:
            flash('Не выбраны товары для импорта', 'warning')
            return _redirect_seller_supplier_catalog(supplier_id)

        draft_account_ids = [
            int(v) for v in request.form.getlist('draft_account_ids')
            if v.isdigit()
        ]
        result = SupplierService.import_to_seller(
            seller.id,
            product_ids,
            draft_account_ids=draft_account_ids or None,
        )

        if result.success:
            marketplace_note = ''
            if result.marketplace_drafts_created:
                marketplace_note += (
                    f', Ozon-черновиков: {result.marketplace_drafts_created}'
                )
            if result.marketplace_draft_errors:
                marketplace_note += (
                    f', Ozon требуют внимания: {result.marketplace_draft_errors}'
                )
            summary = (
                f'Импортировано: {result.imported}, '
                f'пропущено (дубли): {result.skipped}, '
                f'ошибок: {result.errors}{marketplace_note}.'
            )
            category = (
                'success'
                if result.errors == 0 and result.marketplace_draft_errors == 0
                else 'warning'
            )
            if supplier_id is not None:
                zero_import_summary = (
                    f'Новые карточки не добавлены: импортировано 0, '
                    f'пропущено (дубли): {result.skipped}, '
                    f'ошибок: {result.errors}{marketplace_note}. '
                    'Выберите карточки для повторного импорта.'
                )
                zero_import_category = (
                    'warning'
                    if result.errors or result.marketplace_draft_errors
                    else 'info'
                )
                return _redirect_after_supplier_import(
                    seller_id=seller.id,
                    user_id=current_user.id,
                    supplier_id=supplier_id,
                    supplier_product_ids=product_ids,
                    imported_count=result.imported,
                    imported_product_ids=result.imported_product_ids,
                    summary=summary,
                    category=category,
                    zero_import_summary=zero_import_summary,
                    zero_import_category=zero_import_category,
                )

            flash(
                f'{summary} Проверьте карточки и отправьте их на WB.',
                category,
            )
            # Legacy forms without a declared supplier keep the classic destination.
            return redirect(url_for('seller_my_products'))
        flash(f'Ошибка импорта: {"; ".join(result.error_messages[:3])}', 'danger')
        return _redirect_seller_supplier_catalog(supplier_id)

    # -------------------------------------------------------------------
    # Мои импортированные товары — просмотр и управление
    # -------------------------------------------------------------------
    @app.route('/my-products/beta')
    @login_required
    @seller_required
    def seller_my_products_beta():
        """Экран массовых операций: выбор → предупреждение → прогресс → итог."""
        seller = current_user.seller
        ozon_enabled = bool(
            current_app.config.get('MARKETPLACE_OZON_ENABLED', False)
        )
        ozon_accounts = []
        if ozon_enabled:
            ozon_accounts = [
                {
                    'id': account.id,
                    'label': account.label,
                    'connection_status': account.connection_status,
                }
                for account in SellerMarketplaceAccount.query.join(Marketplace).filter(
                    SellerMarketplaceAccount.seller_id == seller.id,
                    SellerMarketplaceAccount.is_active.is_(True),
                    Marketplace.code == 'ozon',
                ).all()
            ]
        suppliers_payload = [
            {'id': row.id, 'name': row.name}
            for row in Supplier.query.join(
                SellerSupplier, SellerSupplier.supplier_id == Supplier.id,
            ).filter(SellerSupplier.seller_id == seller.id).all()
        ]
        try:
            ozon_uploads_url = url_for('ozon_bulk_uploads.index')
        except Exception:
            ozon_uploads_url = '/marketplaces/ozon/uploads/'
        response = render_template(
            'my_products_beta.html',
            wb_connected=bool(seller.has_valid_api_key()),
            ozon_enabled=ozon_enabled,
            ozon_accounts=ozon_accounts,
            suppliers_payload=suppliers_payload,
            ozon_uploads_url=ozon_uploads_url,
            import_batch=_bounded_import_batch_template_value(),
        )
        return _no_store_supplier_import_batch_response(response)

    @app.route('/api/my-products')
    @login_required
    @seller_required
    def api_my_products_list():
        """Списочный read-model «Моих товаров» для интерфейса на Vue."""
        from services.my_products_feed import (
            MyProductsFeed, MyProductsFeedError, MyProductsFeedNotFound,
        )

        seller = current_user.seller
        if 'import_batch' in request.args:
            try:
                imported_product_ids = _load_supplier_import_batch_ids(
                    user_id=current_user.id,
                    seller_id=seller.id,
                )
            except SupplierImportBatchTokenError as exc:
                return _no_store_supplier_import_batch_response((jsonify({
                    'success': False,
                    'error': str(exc),
                }), exc.status_code))
            try:
                payload = MyProductsFeed.list_products_by_ids(
                    seller_id=seller.id,
                    imported_product_ids=imported_product_ids,
                )
            except MyProductsFeedNotFound:
                return _no_store_supplier_import_batch_response((jsonify({
                    'success': False,
                    'error': 'Набор импорта недоступен',
                }), 404))
            except MyProductsFeedError:
                return _no_store_supplier_import_batch_response((jsonify({
                    'success': False,
                    'error': 'Набор импорта недействителен',
                }), 400))
            return _no_store_supplier_import_batch_response(
                jsonify({'success': True, **payload})
            )

        try:
            payload = MyProductsFeed.list_products(
                seller_id=seller.id,
                status=request.args.get('status') or None,
                search=request.args.get('search') or None,
                supplier=request.args.get('supplier') or None,
                brand=request.args.get('brand') or None,
                wb_category=request.args.get('wb_category') or None,
                has_photos=request.args.get('has_photos') or None,
                stock=request.args.get('stock') or None,
                updates_only=request.args.get('updates') == '1',
                sort=request.args.get('sort') or None,
                page=request.args.get('page', 1, type=int) or 1,
                per_page=request.args.get('per_page', 50, type=int) or 50,
            )
        except MyProductsFeedError as exc:
            return jsonify({'success': False, 'error': str(exc)}), 400
        return _no_store_supplier_import_batch_response(
            jsonify({'success': True, **payload})
        )

    @app.route('/api/my-products/facets')
    @login_required
    @seller_required
    def api_my_products_facets():
        """Счётчики вкладок одним агрегатом."""
        from services.my_products_feed import MyProductsFeed

        return jsonify({
            'success': True,
            **MyProductsFeed.facets(seller_id=current_user.seller.id),
        })

    @app.route('/my-products')
    @login_required
    @seller_required
    def seller_my_products():
        """Список импортированных товаров продавца с WB-статусами."""
        seller = current_user.seller
        selected_ozon_account = None
        if request.args.get('account_id'):
            from services.marketplace_accounts import MarketplaceAccountService, MarketplaceAccountError
            account_id = _coerce_positive_int(request.args.get('account_id'))
            if account_id is None or len(request.args.getlist('account_id')) != 1:
                return 'Некорректный кабинет Ozon', 400
            try:
                selected_ozon_account = MarketplaceAccountService.get_owned_account(
                    seller_id=seller.id, account_id=account_id, marketplace_code='ozon',
                )
            except MarketplaceAccountError as exc:
                return str(exc), exc.status_code
        page = request.args.get('page', 1, type=int)
        status = request.args.get('status', '').strip()
        if status not in MY_PRODUCTS_STATUSES:
            status = ''
        search = request.args.get('search', '').strip()
        supplier_filter = request.args.get('supplier', '').strip()
        brand_filter = request.args.get('brand', '').strip()
        wb_category_filter = request.args.get('wb_category', '').strip()
        has_photos = request.args.get('has_photos', '').strip()
        stock = request.args.get('stock', '').strip()
        price_min = request.args.get('price_min', '').strip()
        price_max = request.args.get('price_max', '').strip()
        sort = request.args.get('sort', '').strip()

        updates_filter = request.args.get('updates', '').strip() == '1'

        query = ImportedProduct.query.filter_by(seller_id=seller.id)
        if status:
            query = query.filter_by(import_status=status)
        elif not updates_filter and selected_ozon_account is None:
            # По умолчанию скрываем товары уже загруженные на WB;
            # фильтр «обновления поставщика» показывает и опубликованные —
            # именно их продавец дообогащает на маркетплейсе
            query = query.filter(ImportedProduct.import_status != 'imported')
        if updates_filter:
            query = query.join(
                SupplierProduct,
                SupplierProduct.id == ImportedProduct.supplier_product_id,
            ).filter(
                SupplierProduct.content_revision
                > ImportedProduct.supplier_content_revision,
            )
        if search:
            query = query.filter(
                db.or_(
                    ImportedProduct.title.ilike(f'%{search}%'),
                    ImportedProduct.brand.ilike(f'%{search}%'),
                    ImportedProduct.external_id.ilike(f'%{search}%'),
                )
            )

        if supplier_filter == 'none':
            query = query.filter(ImportedProduct.supplier_id.is_(None))
        elif supplier_filter:
            supplier_id = _coerce_positive_int(supplier_filter)
            if supplier_id:
                query = query.filter(ImportedProduct.supplier_id == supplier_id)

        if brand_filter == 'none':
            query = query.filter(
                db.or_(ImportedProduct.brand.is_(None), ImportedProduct.brand == '')
            )
        elif brand_filter:
            query = query.filter(ImportedProduct.brand == brand_filter)

        if wb_category_filter == 'none':
            query = query.filter(
                db.or_(
                    ImportedProduct.mapped_wb_category.is_(None),
                    ImportedProduct.mapped_wb_category == '',
                )
            )
        elif wb_category_filter:
            query = query.filter(ImportedProduct.mapped_wb_category == wb_category_filter)

        if has_photos == 'yes':
            query = query.filter(
                ImportedProduct.photo_urls.isnot(None),
                ImportedProduct.photo_urls != '',
                ImportedProduct.photo_urls != '[]',
            )
        elif has_photos == 'no':
            query = query.filter(
                db.or_(
                    ImportedProduct.photo_urls.is_(None),
                    ImportedProduct.photo_urls == '',
                    ImportedProduct.photo_urls == '[]',
                )
            )

        if stock == 'in_stock':
            query = query.filter(ImportedProduct.supplier_quantity > 0)
        elif stock == 'out_of_stock':
            query = query.filter(ImportedProduct.supplier_quantity == 0)
        elif stock == 'unknown':
            query = query.filter(ImportedProduct.supplier_quantity.is_(None))

        if price_min:
            try:
                query = query.filter(ImportedProduct.supplier_price >= float(price_min))
            except (ValueError, TypeError):
                pass
        if price_max:
            try:
                query = query.filter(ImportedProduct.supplier_price <= float(price_max))
            except (ValueError, TypeError):
                pass

        sort_map = {
            'oldest': ImportedProduct.created_at.asc(),
            'price_asc': ImportedProduct.supplier_price.asc(),
            'price_desc': ImportedProduct.supplier_price.desc(),
            'title': ImportedProduct.title.asc(),
        }
        query = query.order_by(sort_map.get(sort, ImportedProduct.created_at.desc()))
        per_page = (
            MY_PRODUCTS_UPDATES_PAGE_SIZE
            if updates_filter else MY_PRODUCTS_PAGE_SIZE
        )
        pagination = query.paginate(
            page=page, per_page=per_page, error_out=False,
        )

        # Статистика
        base_q = ImportedProduct.query.filter_by(seller_id=seller.id)
        stats = {
            'total': base_q.count(),
            'pending': base_q.filter_by(import_status='pending').count(),
            'validated': base_q.filter_by(import_status='validated').count(),
            'imported': base_q.filter_by(import_status='imported').count(),
            'failed': base_q.filter_by(import_status='failed').count(),
        }

        # Карточки, у которых общая карточка поставщика новее локальной копии
        supplier_updates_count = db.session.query(
            db.func.count(ImportedProduct.id),
        ).join(
            SupplierProduct,
            SupplierProduct.id == ImportedProduct.supplier_product_id,
        ).filter(
            ImportedProduct.seller_id == seller.id,
            SupplierProduct.content_revision
            > ImportedProduct.supplier_content_revision,
        ).scalar() or 0

        # Флаг обновления для строк текущей страницы + явный дифф:
        # что именно изменилось в общей карточке (категория, характеристики)
        page_supplier_ids = {
            p.supplier_product_id for p in pagination.items
            if p.supplier_product_id
        }
        updates_map = {}
        updates_info = {}
        if page_supplier_ids:
            fresh_rows = db.session.query(
                SupplierProduct.id,
                SupplierProduct.content_revision,
                SupplierProduct.wb_subject_name,
                SupplierProduct.ai_marketplace_json,
            ).filter(SupplierProduct.id.in_(page_supplier_ids)).all()
            fresh_map = {row[0]: row for row in fresh_rows}
            for p in pagination.items:
                row = fresh_map.get(p.supplier_product_id)
                if row is None:
                    updates_map[p.id] = False
                    continue
                _, sp_revision, sp_subject, sp_marketplace_json = row
                has_update = bool(
                    sp_revision is not None
                    and sp_revision > (p.supplier_content_revision or 0)
                )
                updates_map[p.id] = has_update
                if not has_update:
                    continue
                chars_count = 0
                if sp_marketplace_json:
                    try:
                        parsed = json.loads(sp_marketplace_json)
                        if isinstance(parsed, dict):
                            chars_count = len(
                                [k for k in parsed if not str(k).startswith('_')]
                            )
                    except (TypeError, ValueError):
                        pass
                updates_info[p.id] = {
                    'new_category': (
                        sp_subject
                        if sp_subject
                        and sp_subject != (p.mapped_wb_category or '')
                        else None
                    ),
                    'chars_count': chars_count,
                }

        # Последняя WB-ревизия связанных опубликованных карточек страницы:
        # бейдж расхождений в списке (see services/wb_card_audit.py)
        wb_audit_map = {}
        page_product_ids = {
            p.product_id for p in pagination.items if p.product_id
        }
        if page_product_ids:
            audit_rows = db.session.query(
                Product.id, Product.wb_audit_json,
            ).filter(
                Product.id.in_(page_product_ids),
                Product.seller_id == seller.id,
                Product.wb_audit_json.isnot(None),
            ).all()
            audit_by_product = {}
            for prod_id, audit_json in audit_rows:
                try:
                    parsed = json.loads(audit_json)
                except (TypeError, ValueError):
                    continue
                if isinstance(parsed, dict):
                    audit_by_product[prod_id] = parsed
            for p in pagination.items:
                if p.product_id in audit_by_product:
                    wb_audit_map[p.id] = audit_by_product[p.product_id]

        # Данные для выпадающих фильтров (в рамках текущего продавца)
        suppliers_list = db.session.query(
            Supplier.id, Supplier.name, db.func.count(ImportedProduct.id)
        ).join(
            ImportedProduct, ImportedProduct.supplier_id == Supplier.id
        ).filter(
            ImportedProduct.seller_id == seller.id
        ).group_by(Supplier.id, Supplier.name).order_by(Supplier.name).all()
        no_supplier_count = base_q.filter(ImportedProduct.supplier_id.is_(None)).count()

        brands_list = [r[0] for r in db.session.query(
            ImportedProduct.brand
        ).filter(
            ImportedProduct.seller_id == seller.id,
            ImportedProduct.brand.isnot(None),
            ImportedProduct.brand != '',
        ).distinct().order_by(ImportedProduct.brand).all()]

        wb_categories_list = [r[0] for r in db.session.query(
            ImportedProduct.mapped_wb_category
        ).filter(
            ImportedProduct.seller_id == seller.id,
            ImportedProduct.mapped_wb_category.isnot(None),
            ImportedProduct.mapped_wb_category != '',
        ).distinct().order_by(ImportedProduct.mapped_wb_category).all()]

        # Только канонические query-параметры для пагинации, форм действий и
        # вкладок. Обычные status-вкладки и supplier-updates взаимоисключающие:
        # переход между ними не должен сохранять скрытый `updates=1`.
        page_args = _my_products_return_args(request.args)
        status_link_args = {
            k: v for k, v in page_args.items()
            if k not in ('status', 'updates')
        }
        reset_args = (
            {'updates': 1}
            if updates_filter else
            ({'status': status} if status else {})
        )
        if selected_ozon_account is not None:
            reset_args['account_id'] = selected_ozon_account.id
        active_filters = sum(1 for k in (
            'supplier', 'brand', 'wb_category', 'has_photos',
            'stock', 'price_min', 'price_max',
        ) if page_args.get(k))

        # Недавние загрузки (последние 10 импортированных или с ошибкой)
        recent_imports = ImportedProduct.query.filter(
            ImportedProduct.seller_id == seller.id,
            ImportedProduct.import_status.in_(['imported', 'failed']),
            ImportedProduct.updated_at.isnot(None)
        ).order_by(ImportedProduct.updated_at.desc()).limit(10).all()

        # Предзагрузка статуса бренда по категории для товаров на странице
        brand_category_map = {}  # product_id -> True/False/None
        products_with_brand = [
            p for p in pagination.items
            if p.resolved_brand_id and p.wb_subject_id
        ]
        if products_with_brand:
            try:
                from models import MarketplaceBrand, BrandCategoryLink
                brand_ids = list({p.resolved_brand_id for p in products_with_brand})
                mp_brands = MarketplaceBrand.query.filter(
                    MarketplaceBrand.brand_id.in_(brand_ids)
                ).all()
                mp_brand_map = {mb.brand_id: mb.id for mb in mp_brands}

                # Собираем все пары (mp_brand_id, category_id) для проверки
                check_pairs = []
                for p in products_with_brand:
                    mp_id = mp_brand_map.get(p.resolved_brand_id)
                    if mp_id:
                        check_pairs.append((p.id, mp_id, p.wb_subject_id))

                if check_pairs:
                    mp_ids = list({pair[1] for pair in check_pairs})
                    cat_ids = list({pair[2] for pair in check_pairs})
                    links = BrandCategoryLink.query.filter(
                        BrandCategoryLink.marketplace_brand_id.in_(mp_ids),
                        BrandCategoryLink.category_id.in_(cat_ids),
                    ).all()
                    link_map = {(l.marketplace_brand_id, l.category_id): l.is_available for l in links}

                    for pid, mp_id, cat_id in check_pairs:
                        brand_category_map[pid] = link_map.get((mp_id, cat_id))
            except Exception:
                pass  # Таблица может не существовать

        marketplace_accounts = []
        default_marketplace_account = None
        ozon_upload_accounts = []
        default_ozon_upload_account = None
        if current_app.config.get('MARKETPLACE_OZON_ENABLED', False):
            marketplace_accounts = SellerMarketplaceAccount.query.join(
                Marketplace
            ).filter(
                SellerMarketplaceAccount.seller_id == seller.id,
                SellerMarketplaceAccount.is_active.is_(True),
                Marketplace.code == 'ozon',
                Marketplace.is_active.is_(True),
            ).order_by(
                SellerMarketplaceAccount.is_default.desc(),
                SellerMarketplaceAccount.label.asc(),
            ).all()
            default_marketplace_account = next(
                (
                    account for account in marketplace_accounts
                    if account.is_default
                ),
                marketplace_accounts[0]
                if len(marketplace_accounts) == 1 else None,
            )
            now = datetime.utcnow()
            ozon_upload_accounts = [
                account for account in marketplace_accounts
                if (
                    account.connection_status == 'connected'
                    and account.has_credentials
                    and account.public_settings.get('default_vat') is not None
                    and (
                        account.credential_expires_at is None
                        or account.credential_expires_at > now
                    )
                )
            ]
            default_ozon_upload_account = next(
                (
                    account for account in ozon_upload_accounts
                    if account.is_default
                ),
                ozon_upload_accounts[0]
                if len(ozon_upload_accounts) == 1 else None,
            )
            if selected_ozon_account is not None:
                default_marketplace_account = selected_ozon_account
                default_ozon_upload_account = next(
                    (account for account in ozon_upload_accounts if account.id == selected_ozon_account.id),
                    None,
                )
            # channel bar (mp_nav) переиспользует уже загруженные кабинеты
            from services.marketplace_nav import prime_ozon_accounts_cache
            prime_ozon_accounts_cache(marketplace_accounts)

        # Ozon-черновики карточек текущей страницы (для колонки «Каналы»)
        # и уже связанные Ozon-листинги. Два bounded bulk SELECT на страницу.
        ozon_drafts_map = {}
        ozon_listings_map = {}
        ozon_drafts_total = 0
        if marketplace_accounts:
            account_ids = [account.id for account in marketplace_accounts]
            account_labels = {
                account.id: account.label for account in marketplace_accounts
            }
            page_ids = [p.id for p in pagination.items]
            if page_ids:
                draft_rows = MarketplaceProductDraft.query.filter(
                    MarketplaceProductDraft.seller_id == seller.id,
                    MarketplaceProductDraft.account_id.in_(account_ids),
                    MarketplaceProductDraft.imported_product_id.in_(page_ids),
                ).order_by(MarketplaceProductDraft.account_id.asc()).all()
                for draft in draft_rows:
                    ozon_drafts_map.setdefault(
                        draft.imported_product_id, []
                    ).append({
                        'id': draft.id,
                        'account_id': draft.account_id,
                        'account_label': account_labels.get(
                            draft.account_id, 'Ozon'
                        ),
                        'status': draft.status,
                        'validation_status': draft.validation_status,
                        'published_listing_id': draft.published_listing_id,
                    })
                listing_rows = MarketplaceListing.query.filter(
                    MarketplaceListing.seller_id == seller.id,
                    MarketplaceListing.account_id.in_(account_ids),
                    MarketplaceListing.imported_product_id.in_(page_ids),
                ).order_by(
                    MarketplaceListing.account_id.asc(),
                    MarketplaceListing.id.asc(),
                ).all()
                for listing in listing_rows:
                    ozon_listings_map.setdefault(
                        listing.imported_product_id, []
                    ).append({
                        'id': listing.id,
                        'account_id': listing.account_id,
                        'account_label': account_labels.get(
                            listing.account_id, 'Ozon'
                        ),
                        'status': listing.normalized_status,
                        'is_available': listing.is_available,
                    })
            ozon_drafts_total = MarketplaceProductDraft.query.filter(
                MarketplaceProductDraft.seller_id == seller.id,
                MarketplaceProductDraft.account_id.in_(account_ids),
            ).count()

        return render_template(
            'seller_my_products.html',
            pagination=pagination,
            stats=stats,
            status=status,
            search=search,
            supplier_filter=supplier_filter,
            brand_filter=brand_filter,
            wb_category_filter=wb_category_filter,
            has_photos=has_photos,
            stock=stock,
            price_min=price_min,
            price_max=price_max,
            sort=sort,
            suppliers_list=suppliers_list,
            no_supplier_count=no_supplier_count,
            brands_list=brands_list,
            wb_categories_list=wb_categories_list,
            page_args=page_args,
            status_link_args=status_link_args,
            reset_args=reset_args,
            active_filters=active_filters,
            has_wb_key=seller.has_valid_api_key(),
            recent_imports=recent_imports,
            brand_category_map=brand_category_map,
            marketplace_accounts=marketplace_accounts,
            ozon_prepare_request_key=(secrets.token_urlsafe(24)
                                      if current_app.config.get('MARKETPLACE_OZON_ENABLED', False)
                                      else None),
            default_marketplace_account=default_marketplace_account,
            selected_ozon_account=selected_ozon_account,
            ozon_upload_accounts=ozon_upload_accounts,
            default_ozon_upload_account=default_ozon_upload_account,
            ozon_drafts_map=ozon_drafts_map,
            ozon_listings_map=ozon_listings_map,
            ozon_drafts_total=ozon_drafts_total,
            supplier_updates_count=supplier_updates_count,
            updates_filter=updates_filter,
            updates_map=updates_map,
            updates_info=updates_info,
            wb_audit_map=wb_audit_map,
            wb_cta_ids=(
                session.get('supplier_update_wb_ids') or []
                if request.args.get('wb_ready', type=int) else []
            ),
        )

    # -------------------------------------------------------------------
    # WB Card Preview — превью карточки в формате WB
    # -------------------------------------------------------------------
    @app.route('/my-products/<int:product_id>/wb-preview')
    @login_required
    @seller_required
    def seller_product_wb_preview(product_id):
        """Превью карточки товара в формате WB."""
        seller = current_user.seller
        product = ImportedProduct.query.filter_by(
            id=product_id, seller_id=seller.id
        ).first_or_404()

        from services.wb_product_importer import WBProductImporter
        importer = WBProductImporter(seller)
        preview = importer.build_wb_card_preview(product)

        ozon_enabled = bool(
            current_app.config.get('MARKETPLACE_OZON_ENABLED', False)
        )
        ozon_accounts = []
        ozon_drafts_by_account = {}
        if ozon_enabled:
            ozon_accounts = SellerMarketplaceAccount.query.join(
                Marketplace
            ).filter(
                SellerMarketplaceAccount.seller_id == seller.id,
                SellerMarketplaceAccount.is_active.is_(True),
                Marketplace.code == 'ozon',
                Marketplace.is_active.is_(True),
            ).order_by(
                SellerMarketplaceAccount.is_default.desc(),
                SellerMarketplaceAccount.label.asc(),
                SellerMarketplaceAccount.id.asc(),
            ).all()
            account_ids = [account.id for account in ozon_accounts]
            if account_ids:
                drafts = MarketplaceProductDraft.query.filter(
                    MarketplaceProductDraft.seller_id == seller.id,
                    MarketplaceProductDraft.imported_product_id == product.id,
                    MarketplaceProductDraft.account_id.in_(account_ids),
                ).all()
                ozon_drafts_by_account = {
                    draft.account_id: draft for draft in drafts
                }

        supplier_product = product.supplier_product
        if supplier_product and (
            supplier_product.ai_parsed_at is not None
            or bool(supplier_product.ai_parsed_data_json)
        ):
            shared_ai_source = 'supplier_product_cache'
        elif any((
            product.ai_analysis_at,
            product.ai_keywords,
            product.ai_attributes,
            product.ai_seo_title,
        )):
            shared_ai_source = 'imported_product_cache'
        else:
            shared_ai_source = None

        from services.marketplace_fact_pack import MarketplaceFactPackBuilder
        wb_projection_drift = MarketplaceFactPackBuilder.wb_projection_drift(
            product
        )
        linked_wb_nm_id = wb_projection_drift.get('wb_nm_id')
        if linked_wb_nm_id is None and product.wb_nm_id is not None:
            linked_wb_nm_id = str(product.wb_nm_id)

        # Последняя WB-ревизия связанной опубликованной карточки (если была)
        wb_audit = None
        wb_audited_at = None
        if product.product_id:
            linked_product = Product.query.filter_by(
                id=product.product_id, seller_id=seller.id
            ).first()
            if linked_product and linked_product.wb_audit_json:
                try:
                    wb_audit = json.loads(linked_product.wb_audit_json)
                    wb_audited_at = linked_product.wb_audited_at
                except (TypeError, ValueError):
                    wb_audit = None

        return render_template(
            'seller_wb_card_preview.html',
            product=product,
            preview=preview,
            has_wb_key=seller.has_valid_api_key(),
            ozon_enabled=ozon_enabled,
            ozon_accounts=ozon_accounts,
            ozon_drafts_by_account=ozon_drafts_by_account,
            shared_ai_source=shared_ai_source,
            linked_wb_nm_id=linked_wb_nm_id,
            wb_projection_drift=wb_projection_drift,
            wb_audit=wb_audit,
            wb_audited_at=wb_audited_at,
        )

    # -------------------------------------------------------------------
    # WB Card Preview API (JSON)
    # -------------------------------------------------------------------
    @app.route('/my-products/<int:product_id>/wb-preview.json')
    @login_required
    @seller_required
    def seller_product_wb_preview_json(product_id):
        """JSON превью карточки WB (для AJAX)."""
        seller = current_user.seller
        product = ImportedProduct.query.filter_by(
            id=product_id, seller_id=seller.id
        ).first()
        if not product:
            return jsonify({'error': 'Товар не найден'}), 404

        try:
            from services.wb_product_importer import WBProductImporter
            importer = WBProductImporter(seller)
            preview = importer.build_wb_card_preview(product)
            return jsonify(preview)
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    # -------------------------------------------------------------------
    # Push to WB — единичный импорт товара на WB (AJAX)
    # -------------------------------------------------------------------
    @app.route('/my-products/<int:product_id>/push-to-wb', methods=['POST'])
    @login_required
    @seller_required
    def seller_push_to_wb(product_id):
        """Импорт одного товара в магазин WB продавца.
        Поддерживает как AJAX (JSON), так и обычный form POST (redirect)."""
        seller = current_user.seller
        product = ImportedProduct.query.filter_by(
            id=product_id, seller_id=seller.id
        ).first_or_404()

        is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest' or request.accept_mimetypes.best == 'application/json'

        if not seller.has_valid_api_key():
            if is_ajax:
                return jsonify({'success': False, 'error': 'API ключ WB не настроен'}), 400
            flash('API ключ WB не настроен', 'danger')
            return redirect(url_for('seller_product_wb_preview', product_id=product_id))

        # Авто-валидация если товар не validated
        if product.import_status not in ('validated', 'imported'):
            from services.upload_readiness_validator import validate_product_upload_readiness
            vr = validate_product_upload_readiness(product, seller=seller)
            if vr['is_ready']:
                product.import_status = 'validated'
                db.session.commit()
            else:
                error_msgs = [i['message'] for i in vr['issues'] if i['level'] == 'error']
                err = f"Товар не прошёл валидацию: {'; '.join(error_msgs[:3])}"
                if is_ajax:
                    return jsonify({'success': False, 'error': err}), 400
                flash(err, 'danger')
                return redirect(url_for('seller_product_wb_preview', product_id=product_id))

        from services.wb_product_importer import WBProductImporter
        importer = WBProductImporter(seller)
        success, error, created = importer.import_product_to_wb(product)

        # Создаём уведомление
        if success:
            n = Notification(
                seller_id=seller.id,
                category='success',
                title='Товар отправлен на WB',
                message=f'{(product.title or "")[:60]} — карточка создана',
                link=f'/my-products/{product.id}/wb-preview',
            )
            db.session.add(n)
            db.session.commit()
        else:
            n = Notification(
                seller_id=seller.id,
                category='error',
                title='Ошибка импорта на WB',
                message=f'{(product.title or "")[:40]}: {(error or "")[:120]}',
                link=f'/my-products/{product.id}/wb-preview',
            )
            db.session.add(n)
            db.session.commit()

        if is_ajax:
            return jsonify({
                'success': success,
                'error': error,
                'product_id': created.id if hasattr(created, 'id') else created,
            })

        if success:
            flash(f'Товар "{product.title}" отправлен на WB', 'success')
        else:
            flash(f'Ошибка: {error}', 'danger')

        return redirect(url_for('seller_product_wb_preview', product_id=product_id))

    # -------------------------------------------------------------------
    # Push to WB — массовый импорт товаров на WB
    # -------------------------------------------------------------------
    @app.route('/my-products/push-to-wb-bulk', methods=['POST'])
    @login_required
    @seller_required
    def seller_push_to_wb_bulk():
        """Массовый импорт товаров в магазин WB — фоновая задача."""
        import uuid
        import threading

        seller = current_user.seller

        if not seller.has_valid_api_key():
            return jsonify({'success': False, 'error': 'API ключ WB не настроен'}), 400

        data = request.get_json(silent=True) or {}
        try:
            product_ids = _bulk_product_ids(data.get('product_ids'))
        except ValueError as exc:
            return jsonify({'success': False, 'error': str(exc)}), 400

        # Создаём фоновую задачу
        job_uid = f"bulk_wb_{uuid.uuid4().hex[:12]}"
        job = BackgroundJob(
            job_uid=job_uid,
            seller_id=seller.id,
            job_type='bulk_wb_import',
            status='pending',
            total=len(product_ids),
            processed=0,
            succeeded=0,
            failed_count=0,
        )
        job.set_progress({'product_ids': product_ids, 'items': []})
        db.session.add(job)
        db.session.commit()

        # Запускаем обработку в фоновом потоке
        from seller_platform import app as flask_app

        def run_bulk_import(app, job_uid, seller_id, product_ids):
            """Батчевый импорт карточек WB — 1 API-вызов на создание, 1 на цены."""
            with app.app_context():
                validated_count = 0
                items_progress = []
                try:
                    from services.wb_product_importer import WBProductImporter
                    from services.upload_readiness_validator import validate_product_upload_readiness

                    job = BackgroundJob.query.filter_by(job_uid=job_uid).first()
                    if not job:
                        return
                    job.status = 'running'
                    db.session.commit()

                    seller = Seller.query.get(seller_id)
                    importer = WBProductImporter(seller)

                    # Авто-валидация pending/failed
                    products_to_validate = ImportedProduct.query.filter(
                        ImportedProduct.id.in_(product_ids),
                        ImportedProduct.seller_id == seller_id,
                        ImportedProduct.import_status.in_(['pending', 'failed'])
                    ).all()

                    validated_count = 0
                    for p in products_to_validate:
                        try:
                            vr = validate_product_upload_readiness(p, seller=seller)
                            if vr['is_ready']:
                                p.import_status = 'validated'
                                validated_count += 1
                        except Exception:
                            pass
                    if products_to_validate:
                        db.session.commit()

                    # Загружаем все ImportedProduct для батч-импорта
                    imported_products = ImportedProduct.query.filter(
                        ImportedProduct.id.in_(product_ids),
                        ImportedProduct.seller_id == seller_id,
                    ).all()
                    # Сохраняем порядок из product_ids
                    id_to_product = {p.id: p for p in imported_products}
                    ordered_products = [id_to_product[pid] for pid in product_ids if pid in id_to_product]

                    # Callback для обновления прогресса job
                    cancelled = [False]

                    import time as _progress_time
                    _last_progress_update = [0]

                    def on_progress(phase, detail):
                        """Обновляет BackgroundJob из batch callback.
                        Throttled: не чаще 1 раза в 2 секунды (снижает SQLite lock contention)."""
                        if cancelled[0]:
                            raise Exception('__cancelled__')

                        now = _progress_time.time()
                        # Throttle: обновляем не чаще раза в 2с, кроме смены фазы и завершения
                        if phase not in ('done', 'creating', 'polling', 'pricing', 'saving'):
                            if now - _last_progress_update[0] < 2.0:
                                return
                        _last_progress_update[0] = now

                        try:
                            j = BackgroundJob.query.filter_by(job_uid=job_uid).first()
                            if not j or j.status == 'cancelled':
                                cancelled[0] = True
                                raise Exception('__cancelled__')

                            batch_items = detail.get('items', [])
                            succeeded = sum(1 for it in batch_items if it.get('status') == 'success')
                            failed = sum(1 for it in batch_items if it.get('status') in ('failed', 'skipped'))
                            processed = succeeded + failed

                            j.processed = processed
                            j.succeeded = succeeded
                            j.failed_count = failed
                            j.set_progress({
                                'product_ids': product_ids,
                                'phase': phase,
                                'items': batch_items,
                            })
                            db.session.commit()
                        except Exception as cb_err:
                            if '__cancelled__' in str(cb_err):
                                raise
                            try:
                                db.session.rollback()
                            except Exception:
                                db.session.remove()

                    # ─── Батчевый импорт ───
                    try:
                        batch_results = importer.batch_import_products_to_wb(
                            ordered_products, progress_callback=on_progress
                        )
                    except Exception as batch_err:
                        if '__cancelled__' in str(batch_err):
                            # Отмена пользователем — job уже cancelled
                            batch_results = []
                        else:
                            raise

                    items_progress = batch_results

                    # Завершение
                    try:
                        db.session.refresh(job)
                    except Exception:
                        db.session.rollback()
                        job = BackgroundJob.query.filter_by(job_uid=job_uid).first()

                    if job and job.status != 'cancelled':
                        job.status = 'completed'

                    succeeded = sum(1 for it in items_progress if it.get('status') == 'success')
                    failed_cnt = sum(1 for it in items_progress if it.get('status') in ('failed', 'skipped'))
                    if job:
                        job.succeeded = succeeded
                        job.failed_count = failed_cnt
                        job.processed = len(items_progress)
                        job.set_result({
                            'imported': succeeded,
                            'failed': failed_cnt,
                            'total': job.total,
                            'auto_validated': validated_count,
                            'errors': [
                                f"{it.get('title', '')}: {it.get('error', '')}"
                                for it in items_progress
                                if it.get('status') != 'success' and it.get('error')
                            ],
                        })
                        db.session.commit()

                    # Уведомление
                    if job and job.status != 'cancelled':
                        if succeeded > 0:
                            cat = 'success' if failed_cnt == 0 else 'warning'
                            n = Notification(
                                seller_id=seller_id, category=cat,
                                title='Массовый импорт на WB завершён',
                                message=f'Импортировано {succeeded} из {job.total} товаров'
                                        + (f', ошибок: {failed_cnt}' if failed_cnt else ''),
                                link='/my-products?status=imported',
                            )
                        else:
                            n = Notification(
                                seller_id=seller_id, category='error',
                                title='Массовый импорт на WB не удался',
                                message=f'Ни один из {job.total} товаров не был импортирован',
                                link='/my-products',
                            )
                        db.session.add(n)
                        db.session.commit()

                except Exception as e:
                    db.session.rollback()
                    try:
                        job = BackgroundJob.query.filter_by(job_uid=job_uid).first()
                        if job and job.status not in ('completed', 'cancelled'):
                            job.status = 'failed'
                            job.error_message = str(e)[:500]
                            db.session.commit()
                        n = Notification(
                            seller_id=seller_id, category='error',
                            title='Ошибка массового импорта',
                            message=str(e)[:200], link='/my-products',
                        )
                        db.session.add(n)
                        db.session.commit()
                    except Exception:
                        db.session.rollback()
                finally:
                    try:
                        job = BackgroundJob.query.filter_by(job_uid=job_uid).first()
                        if job and job.status in ('running', 'pending'):
                            job.status = 'failed'
                            job.error_message = (job.error_message or 'Процесс прерван неожиданно')[:500]
                            job.set_result({
                                'imported': job.succeeded or 0,
                                'failed': job.failed_count or 0,
                                'total': job.total or 0,
                                'auto_validated': validated_count,
                                'errors': [
                                    f"{it.get('title', '')}: {it.get('error', '')}"
                                    for it in items_progress
                                    if it.get('status') != 'success' and it.get('error')
                                ],
                            })
                            db.session.commit()
                    except Exception:
                        db.session.rollback()

        t = threading.Thread(
            target=run_bulk_import,
            args=(flask_app, job_uid, seller.id, product_ids),
            daemon=True,
        )
        t.start()

        return jsonify({'success': True, 'job_uid': job_uid})

    # -------------------------------------------------------------------
    # Cancel bulk WB import — отмена массового импорта
    # -------------------------------------------------------------------
    @app.route('/api/jobs/<job_uid>/cancel', methods=['POST'])
    @login_required
    @seller_required
    def cancel_bulk_job(job_uid):
        """Отмена фоновой задачи массового импорта."""
        seller = current_user.seller
        job = BackgroundJob.query.filter_by(
            job_uid=job_uid, seller_id=seller.id
        ).first_or_404()

        if job.status in ('pending', 'running'):
            job.status = 'cancelled'
            job.error_message = 'Отменено пользователем'
            db.session.commit()
            return jsonify({'success': True, 'message': 'Задача отменена'})
        else:
            return jsonify({'success': False, 'error': f'Задача уже в статусе: {job.status}'}), 400

    # -------------------------------------------------------------------
    # Validate product for WB — пометить как валидированный
    # -------------------------------------------------------------------
    @app.route('/my-products/<int:product_id>/validate', methods=['POST'])
    @login_required
    @seller_required
    def seller_validate_product(product_id):
        """Пометить товар как validated (готов к пушу на WB).
        Использует глубокую валидацию: цены, фото, характеристики, баркоды."""
        seller = current_user.seller
        product = ImportedProduct.query.filter_by(
            id=product_id, seller_id=seller.id
        ).first_or_404()

        from services.upload_readiness_validator import validate_product_upload_readiness
        result = validate_product_upload_readiness(product, seller=seller)

        if not result['is_ready']:
            error_msgs = [i['message'] for i in result['issues'] if i['level'] == 'error']
            flash(f'Невозможно валидировать: {"; ".join(error_msgs[:5])}', 'danger')
        else:
            product.import_status = 'validated'
            db.session.commit()
            if result['warnings_count'] > 0:
                warn_msgs = [i['message'] for i in result['issues'] if i['level'] == 'warning']
                flash(f'Товар готов к импорту на WB (предупреждений: {len(warn_msgs)})', 'warning')
            else:
                flash('Товар готов к импорту на WB', 'success')

        return redirect(url_for('seller_product_wb_preview', product_id=product_id))

    # -------------------------------------------------------------------
    # Deep validation API (JSON) — полная проверка готовности
    # -------------------------------------------------------------------
    @app.route('/my-products/<int:product_id>/check-readiness')
    @login_required
    @seller_required
    def seller_check_readiness(product_id):
        """API: глубокая проверка готовности товара к загрузке на WB."""
        seller = current_user.seller
        product = ImportedProduct.query.filter_by(
            id=product_id, seller_id=seller.id
        ).first()
        if not product:
            return jsonify({'error': 'Товар не найден'}), 404

        from services.upload_readiness_validator import validate_product_upload_readiness
        result = validate_product_upload_readiness(product, seller=seller)
        return jsonify(result)

    # -------------------------------------------------------------------
    # Удалить импортированный товар — сбросить для повторного импорта
    # -------------------------------------------------------------------
    @app.route('/my-products/<int:product_id>/delete', methods=['POST'])
    @login_required
    @seller_required
    def seller_delete_imported_product(product_id):
        """Удалить запись импортированного товара.
        Товар станет снова доступен для импорта из каталога поставщика."""
        seller = current_user.seller
        product = ImportedProduct.query.filter_by(
            id=product_id, seller_id=seller.id
        ).first_or_404()

        title = product.title or product.external_id
        db.session.delete(product)
        db.session.commit()

        flash(f'Товар «{title}» удалён и доступен для повторного импорта', 'success')
        return _redirect_seller_my_products(request.args)

    @app.route('/my-products/delete-bulk', methods=['POST'])
    @login_required
    @seller_required
    def seller_delete_imported_products_bulk():
        """Массовое удаление импортированных товаров."""
        seller = current_user.seller
        data = request.get_json() or {}
        try:
            product_ids = _bulk_product_ids(data.get('product_ids'))
        except ValueError as exc:
            return jsonify({'error': str(exc)}), 400

        # Удаляем связанные agent_change_snapshots перед удалением товаров
        AgentChangeSnapshot.query.filter(
            AgentChangeSnapshot.imported_product_id.in_(product_ids)
        ).delete(synchronize_session=False)

        deleted = ImportedProduct.query.filter(
            ImportedProduct.id.in_(product_ids),
            ImportedProduct.seller_id == seller.id
        ).delete(synchronize_session=False)
        db.session.commit()

        return jsonify({'deleted': deleted, 'message': f'Удалено {deleted} товаров'})

    # -------------------------------------------------------------------
    # Проверка карточки на WB — фото, цены, статус
    # -------------------------------------------------------------------
    @app.route('/my-products/<int:product_id>/wb-check', methods=['POST'])
    @login_required
    @seller_required
    def seller_wb_check_card(product_id):
        """Проверить состояние карточки на WB: наличие фото, цен, ошибок."""
        seller = current_user.seller
        imp = ImportedProduct.query.filter_by(
            id=product_id, seller_id=seller.id
        ).first_or_404()

        if imp.import_status != 'imported' or not imp.product_id:
            return jsonify({'error': 'Товар ещё не загружен на WB'}), 400

        product = Product.query.filter_by(
            id=imp.product_id,
            seller_id=seller.id,
        ).first()
        if not product or not product.nm_id:
            return jsonify({'error': 'Карточка WB не найдена в базе'}), 400

        if not seller.has_valid_api_key():
            return jsonify({'error': 'API ключ WB не настроен'}), 400

        from services.wb_api_client import WildberriesAPIClient
        api = WildberriesAPIClient(seller.wb_api_key)

        result = {
            'nm_id': product.nm_id,
            'vendor_code': product.vendor_code,
            'title': product.title,
            'has_photos': False,
            'photos_count': 0,
            'has_price': False,
            'price': None,
            'discount': None,
            'issues': [],
        }

        try:
            card = api.get_card_by_nm_id(product.nm_id, seller_id=seller.id)
            if not card:
                result['issues'].append('Карточка не найдена на WB')
                return jsonify(result)

            # Проверяем фото
            photos = card.get('photos', [])
            result['photos_count'] = len(photos)
            result['has_photos'] = len(photos) > 0
            if not photos:
                result['issues'].append('Нет фотографий на WB')

            # Проверяем цены через sizes
            sizes = card.get('sizes', [])
            has_any_price = False
            for size in sizes:
                price_info = size.get('price', {})
                if price_info and price_info.get('total', 0) > 0:
                    has_any_price = True
                    result['price'] = price_info.get('total')
                    result['discount'] = price_info.get('discount')
                    break

            result['has_price'] = has_any_price
            if not has_any_price:
                result['issues'].append('Цена не установлена или равна 0')

            # Проверяем title и description
            if not card.get('title'):
                result['issues'].append('Отсутствует название карточки')
            if not card.get('description'):
                result['issues'].append('Отсутствует описание')

            # Проверяем характеристики
            charcs = card.get('characteristics', [])
            result['characteristics_count'] = len(charcs)

        except Exception as e:
            result['issues'].append(f'Ошибка при запросе к WB API: {str(e)[:200]}')

        return jsonify(result)

    # -------------------------------------------------------------------
    # Повторная загрузка фото на WB
    # -------------------------------------------------------------------
    @app.route('/my-products/<int:product_id>/wb-reupload-photos', methods=['POST'])
    @login_required
    @seller_required
    def seller_wb_reupload_photos(product_id):
        """Безопасно дозагрузить отсутствующие фото, сохранив live-галерею."""
        seller = current_user.seller
        imp = ImportedProduct.query.filter_by(
            id=product_id, seller_id=seller.id
        ).first_or_404()

        if imp.import_status != 'imported' or not imp.product_id:
            return jsonify({'success': False, 'error': 'Товар ещё не загружен на WB'}), 400

        product = Product.query.filter_by(
            id=imp.product_id,
            seller_id=seller.id,
        ).first()
        if not product or not product.nm_id:
            return jsonify({'success': False, 'error': 'Карточка WB не найдена'}), 400

        if not seller.has_valid_api_key():
            return jsonify({'success': False, 'error': 'API ключ WB не настроен'}), 400

        from services.supplier_enrichment import get_enrichment_service
        from services.wb_api_client import WildberriesAPIClient
        api = None
        try:
            api = WildberriesAPIClient(seller.wb_api_key)
            result = get_enrichment_service().apply_enrichment(
                product,
                imp,
                ['photos'],
                'smart_merge',
                seller,
                api,
            )
            photos = result.get('photos') or {}
            if result.get('deferred') or result.get('reconciliation_pending'):
                return jsonify({
                    **result,
                    'message': (
                        'Дозагрузка отложена до завершения предыдущей '
                        'операции WB'
                        if result.get('deferred') else
                        'Фото отправлены и сверяются с live-галереей WB'
                    ),
                }), 202
            if not result.get('success'):
                return jsonify(result), 409
            if photos.get('skipped'):
                message = (
                    'Live-галерея WB сохранена; новых отсутствующих фото нет'
                )
            else:
                message = 'Отсутствующие фото дозагружены без замены live-слотов'
            return jsonify({**result, 'message': message}), 200
        except Exception as e:
            return jsonify({'success': False, 'error': f'Ошибка загрузки фото: {str(e)[:200]}'}), 500
        finally:
            close = getattr(api, 'close', None)
            if callable(close):
                close()

    # -------------------------------------------------------------------
    # Повторная установка цены на WB
    # -------------------------------------------------------------------
    @app.route('/my-products/<int:product_id>/wb-reupload-price', methods=['POST'])
    @login_required
    @seller_required
    def seller_wb_reupload_price(product_id):
        """Повторно установить цену товара на WB."""
        seller = current_user.seller
        imp = ImportedProduct.query.filter_by(
            id=product_id, seller_id=seller.id
        ).first_or_404()

        if imp.import_status != 'imported' or not imp.product_id:
            return jsonify({'success': False, 'error': 'Товар ещё не загружен на WB'}), 400

        product = Product.query.get(imp.product_id)
        if not product or not product.nm_id:
            return jsonify({'success': False, 'error': 'Карточка WB не найдена'}), 400

        if not seller.has_valid_api_key():
            return jsonify({'success': False, 'error': 'API ключ WB не настроен'}), 400

        from services.wb_product_importer import WBProductImporter
        importer = WBProductImporter(seller)

        try:
            final_price, discount_price, price_before = importer._set_price_for_card(
                product.nm_id, imp
            )
            if final_price:
                # Обновляем цену в локальной базе
                product.price = price_before
                product.discount_price = final_price
                db.session.commit()
                return jsonify({
                    'success': True,
                    'message': f'Цена установлена: {final_price} руб.',
                    'price': final_price,
                    'price_before_discount': price_before,
                })
            else:
                return jsonify({
                    'success': False,
                    'error': 'Не удалось рассчитать цену (нет supplier_price или настроек ценообразования)'
                }), 400
        except Exception as e:
            return jsonify({'success': False, 'error': f'Ошибка установки цены: {str(e)[:200]}'}), 500

    # -------------------------------------------------------------------
    # Обогащение карточки WB данными из AI-парсинга поставщика
    # -------------------------------------------------------------------
    @app.route('/my-products/<int:product_id>/wb-enrich', methods=['POST'])
    @login_required
    @seller_required
    def seller_wb_enrich_card(product_id):
        """Обогатить карточку WB данными из AI-парсинга базы поставщика.
        Обновляет: title, description, характеристики, габариты."""
        import json as json_mod
        seller = current_user.seller
        imp = ImportedProduct.query.filter_by(
            id=product_id, seller_id=seller.id
        ).first_or_404()

        if imp.import_status != 'imported' or not imp.product_id:
            return jsonify({'success': False, 'error': 'Товар ещё не загружен на WB'}), 400

        product = Product.query.get(imp.product_id)
        if not product or not product.nm_id:
            return jsonify({'success': False, 'error': 'Карточка WB не найдена'}), 400

        if not seller.has_valid_api_key():
            return jsonify({'success': False, 'error': 'API ключ WB не настроен'}), 400

        # Получаем AI-данные из SupplierProduct
        sp = None
        if imp.supplier_product_id:
            sp = SupplierProduct.query.get(imp.supplier_product_id)

        if not sp:
            return jsonify({'success': False, 'error': 'Не найден товар поставщика для обогащения'}), 400

        # Старый CTA теперь использует тот же live-aware service contract, что
        # одиночный и массовый экраны. SupplierProduct остаётся свежим source,
        # но ни один путь больше не вызывает raw full-card replacement.
        from types import SimpleNamespace

        ai_title = sp.ai_seo_title or imp.ai_seo_title
        ai_desc = sp.ai_description or imp.description
        try:
            marketplace_data = json_mod.loads(sp.ai_marketplace_json) if sp.ai_marketplace_json else None
        except (json_mod.JSONDecodeError, TypeError):
            marketplace_data = None

        charcs = None
        if marketplace_data and isinstance(marketplace_data, dict):
            charcs = marketplace_data.get('characteristics', [])
            meta = marketplace_data.get('_meta')
            if (
                not charcs
                and isinstance(meta, dict)
                and meta.get('source') == 'supplier_catalog_enrichment'
            ):
                # The durable shared-catalog parser stores the current WB
                # schema as a flat exact-name map. The API client will map it
                # to charc_id and validate dictionaries against the live
                # card's own subject before any provider write.
                charcs = {
                    name: value
                    for name, value in marketplace_data.items()
                    if not str(name).startswith('_')
                    and value not in (None, '', [])
                }
        if not charcs:
            charcs = sp.characteristics_json or imp.characteristics

        # Габариты — из AI или sp. Сбор через общий WB helper, чтобы
        # weightBrutto оставался внутри dimensions и в килограммах.
        from services.wb_content_payload import extract_dimensions

        dimension_sources = []
        try:
            ai_dims = json_mod.loads(sp.dimensions_json) if sp.dimensions_json else None
            if ai_dims:
                dimension_sources.append(ai_dims)
        except (json_mod.JSONDecodeError, TypeError):
            pass

        try:
            ai_dims = json_mod.loads(imp.ai_dimensions) if imp.ai_dimensions else None
            if ai_dims:
                dimension_sources.append(ai_dims)
        except (json_mod.JSONDecodeError, TypeError):
            pass

        if marketplace_data and isinstance(marketplace_data, dict):
            if marketplace_data.get('package_dimensions'):
                dimension_sources.append(marketplace_data['package_dimensions'])

        dimensions = {}
        for source in dimension_sources:
            dimensions.update(extract_dimensions(source))

        selected_fields = []
        if ai_title and len(ai_title) >= 5:
            selected_fields.append('title')
        if ai_desc and len(ai_desc) >= 50:
            selected_fields.append('description')
        if charcs:
            selected_fields.append('characteristics')
        if dimensions:
            selected_fields.append('dimensions')

        if not selected_fields:
            return jsonify({
                'success': False,
                'error': 'Нет AI-данных для обогащения. Сначала выполните AI-парсинг товара у поставщика.'
            }), 400

        source = SimpleNamespace(
            id=imp.id,
            seller_id=imp.seller_id,
            product_id=imp.product_id,
            supplier_id=imp.supplier_id,
            supplier_product_id=imp.supplier_product_id,
            external_id=imp.external_id,
            source_type=imp.source_type,
            title=(ai_title[:60] if ai_title and len(ai_title) >= 5 else None),
            ai_seo_title=None,
            brand=imp.brand,
            ai_detected_brand=None,
            description=(
                ai_desc[:5000] if ai_desc and len(ai_desc) >= 50 else None
            ),
            characteristics=charcs or '',
            materials=sp.materials_json or imp.materials,
            gender=sp.gender or imp.gender,
            ai_dimensions=(
                json_mod.dumps(dimensions, ensure_ascii=False)
                if dimensions else None
            ),
            photo_urls=imp.photo_urls,
            created_at=imp.created_at,
        )

        from services.wb_api_client import WildberriesAPIClient
        from services.supplier_enrichment import get_enrichment_service
        api = WildberriesAPIClient(seller.wb_api_key)

        try:
            result = get_enrichment_service().apply_enrichment(
                product,
                source,
                selected_fields,
                'smart_merge',
                seller,
                api,
            )
            if not result.get('success'):
                if result.get('reconciliation_pending'):
                    return jsonify({
                        **result,
                        'message': (
                            'Отправка отложена до завершения предыдущей '
                            'операции WB; новая запись ещё не выполнялась'
                            if result.get('deferred') else
                            'Исход отправки уточняется по live WB; '
                            'автоматический повтор не выполняется'
                        ),
                    }), 202
                return jsonify(result), 409
            labels = {
                'title': 'название',
                'description': 'описание',
                'characteristics': 'характеристики',
                'dimensions': 'габариты',
            }
            applied = list(result.get('fields_applied') or [])
            response = {
                **result,
                'message': (
                    'Изменения отправлены на WB и ожидают сверки: '
                    + ', '.join(labels.get(field, field) for field in applied)
                    if applied else
                    'Более полные live-данные WB сохранены без изменений'
                ),
                'updated_fields': [labels.get(field, field) for field in applied],
            }
            return jsonify(response), (
                202 if result.get('reconciliation_pending') else 200
            )
        except Exception as e:
            return jsonify({
                'success': False,
                'error': f'Ошибка обновления карточки WB: {str(e)[:200]}'
            }), 500
        finally:
            close = getattr(api, 'close', None)
            if callable(close):
                close()

    # -------------------------------------------------------------------
    # Rich-контент (инфографика) для продавца
    # -------------------------------------------------------------------
    @app.route('/my-products/<int:product_id>/rich-content', methods=['POST'])
    @login_required
    @seller_required
    def seller_rich_content(product_id):
        """Генерация Rich-контента (инфографика) для товара продавца."""
        seller = current_user.seller
        imp = ImportedProduct.query.filter_by(
            id=product_id, seller_id=seller.id
        ).first_or_404()

        # Получаем SupplierProduct для доступа к AI
        sp = SupplierProduct.query.get(imp.supplier_product_id) if imp.supplier_product_id else None
        if not sp:
            return jsonify({'success': False, 'error': 'Не найден товар поставщика'}), 400

        # Если у SupplierProduct уже есть rich content — копируем
        if sp.ai_rich_content_json:
            import json as json_mod
            from services.infographic_content import validate_fact_safe_rich_content
            cached = json_mod.loads(sp.ai_rich_content_json)
            valid, _errors = validate_fact_safe_rich_content(cached)
            if valid:
                imp.ai_rich_content = sp.ai_rich_content_json
                db.session.commit()
                return jsonify({'success': True, 'data': cached, 'source': 'supplier'})

        # Генерируем новый
        result = SupplierService.ai_generate_rich_content(sp.id)
        if result.get('success'):
            # Копируем в ImportedProduct
            sp_refreshed = SupplierProduct.query.get(sp.id)
            if sp_refreshed and sp_refreshed.ai_rich_content_json:
                imp.ai_rich_content = sp_refreshed.ai_rich_content_json
                db.session.commit()
        return jsonify(result)

    @app.route('/my-products/<int:product_id>/render-infographic', methods=['POST'])
    @login_required
    @seller_required
    def seller_render_infographic(product_id):
        """Production-safe render; AI background may fall back to a safe template."""
        seller = current_user.seller
        imp = ImportedProduct.query.filter_by(
            id=product_id, seller_id=seller.id
        ).first_or_404()

        sp = SupplierProduct.query.get(imp.supplier_product_id) if imp.supplier_product_id else None
        if not sp:
            return jsonify({'success': False, 'error': 'Не найден товар поставщика'}), 400

        # Берём rich content из ImportedProduct или SupplierProduct
        rich_json = imp.ai_rich_content or (sp.ai_rich_content_json if sp else None)
        if not rich_json:
            return jsonify({'success': False, 'error': 'Сначала сгенерируйте Rich-контент'}), 400

        data = request.get_json() or {}
        slide_index = data.get('slide_index')

        result = SupplierService.ai_render_hybrid_infographic(sp.id)
        if slide_index is not None and result.get('results'):
            selected = [item for item in result['results']
                        if item.get('slide_number') == slide_index + 1]
            result['results'] = selected
            result['total_slides'] = len(selected)
            result['successful'] = sum(1 for item in selected if item.get('success'))
            result['failed'] = len(selected) - result['successful']
        return jsonify(result)

    @app.route('/my-products/<int:product_id>/render-hybrid', methods=['POST'])
    @login_required
    @seller_required
    def seller_render_hybrid(product_id):
        """Гибридный рендер: AI-фон + Playwright текст (качественная инфографика)."""
        seller = current_user.seller
        imp = ImportedProduct.query.filter_by(
            id=product_id, seller_id=seller.id
        ).first_or_404()

        sp = SupplierProduct.query.get(imp.supplier_product_id) if imp.supplier_product_id else None
        if not sp:
            return jsonify({'success': False, 'error': 'Не найден товар поставщика'}), 400

        rich_json = imp.ai_rich_content or (sp.ai_rich_content_json if sp else None)
        if not rich_json:
            return jsonify({'success': False, 'error': 'Сначала сгенерируйте Rich-контент'}), 400

        result = SupplierService.ai_render_hybrid_infographic(sp.id)
        return jsonify(result)

    @app.route('/my-products/<int:product_id>/rich-content-slides')
    @login_required
    @seller_required
    def seller_rich_content_slides(product_id):
        """Просмотр слайдов Rich-контента."""
        import json as json_mod
        seller = current_user.seller
        imp = ImportedProduct.query.filter_by(
            id=product_id, seller_id=seller.id
        ).first_or_404()

        sp = SupplierProduct.query.get(imp.supplier_product_id) if imp.supplier_product_id else None
        rich_json = imp.ai_rich_content or (sp.ai_rich_content_json if sp else None)
        if not rich_json:
            return jsonify({'success': False, 'error': 'Rich-контент не найден'}), 404

        try:
            data = json_mod.loads(rich_json) if isinstance(rich_json, str) else rich_json
            slides = data.get('slides', [])
            return jsonify({
                'success': True,
                'slides': slides,
                'design': data.get('design_recommendations', {}),
                'total': len(slides)
            })
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500

    # -------------------------------------------------------------------
    # WB-ревизия: live-сверка карточек с фиксацией расхождений
    # -------------------------------------------------------------------
    @app.route('/my-products/wb-audit', methods=['POST'])
    @login_required
    @seller_required
    def seller_wb_audit_start():
        """WB-ревизия выбранных карточек: live-сверка с фиксацией расхождений.

        Принимает seller-owned ImportedProduct IDs (как остальные bulk-кнопки
        «Моих товаров»), детерминированно переводит их в exact product_id и
        запускает фоновую сверку до 200 карточек. Только read-вызовы WB.
        """
        import threading
        import uuid as uuid_mod

        from models import BackgroundJob
        from services.wb_card_audit import (
            AUDIT_JOB_TYPE, MAX_AUDIT_BATCH, run_audit_job,
        )

        seller = current_user.seller
        if not seller.has_valid_api_key():
            return jsonify({'success': False,
                            'error': 'API ключ WB не настроен'}), 400

        data = request.get_json(silent=True) or {}
        raw_ids = data.get('product_ids') or []
        if not isinstance(raw_ids, list) or not raw_ids:
            return jsonify({'success': False,
                            'error': 'Не выбраны товары'}), 400
        try:
            raw_ids = [int(x) for x in raw_ids]
        except (TypeError, ValueError):
            return jsonify({'success': False,
                            'error': 'Некорректные product_ids'}), 400
        if len(raw_ids) > MAX_AUDIT_BATCH:
            return jsonify({
                'success': False,
                'error': f'Не больше {MAX_AUDIT_BATCH} карточек за один запуск',
            }), 400

        imported = ImportedProduct.query.filter(
            ImportedProduct.id.in_(raw_ids),
            ImportedProduct.seller_id == seller.id,
            ImportedProduct.product_id.isnot(None),
        ).all()
        product_ids = sorted({imp.product_id for imp in imported})
        if not product_ids:
            return jsonify({
                'success': False,
                'error': 'Среди выбранных нет опубликованных на WB карточек',
            }), 400

        active = (BackgroundJob.query
                  .filter_by(seller_id=seller.id, job_type=AUDIT_JOB_TYPE)
                  .filter(BackgroundJob.status.in_(('pending', 'running')))
                  .first())
        if active:
            return jsonify({'success': False, 'job_uid': active.job_uid,
                            'error': 'Ревизия уже выполняется'}), 409

        job_uid = uuid_mod.uuid4().hex
        job = BackgroundJob(
            job_uid=job_uid, seller_id=seller.id, job_type=AUDIT_JOB_TYPE,
            status='pending', total=len(product_ids),
        )
        db.session.add(job)
        db.session.commit()

        flask_app = current_app._get_current_object()
        thread = threading.Thread(
            target=run_audit_job,
            args=(flask_app, job_uid, seller.id, product_ids),
            daemon=True,
        )
        thread.start()
        return jsonify({'success': True, 'job_uid': job_uid,
                        'total': len(product_ids)})

    # -------------------------------------------------------------------
    # Массовая проверка карточек на WB
    # -------------------------------------------------------------------
    @app.route('/my-products/wb-check-bulk', methods=['POST'])
    @login_required
    @seller_required
    def seller_wb_check_bulk():
        """Массовая проверка состояния карточек на WB."""
        seller = current_user.seller

        if not seller.has_valid_api_key():
            return jsonify({'error': 'API ключ WB не настроен'}), 400

        data = request.get_json(silent=True) or {}
        try:
            # Здесь на каждую карточку идёт отдельный вызов WB, поэтому
            # ограничение особенно важно: без него один клик держит поток
            # и упирается в rate limit площадки.
            product_ids = _bulk_product_ids(data.get('product_ids'), limit=50)
        except ValueError as exc:
            return jsonify({'error': str(exc)}), 400

        # Берём только импортированные товары
        imported = ImportedProduct.query.filter(
            ImportedProduct.id.in_(product_ids),
            ImportedProduct.seller_id == seller.id,
            ImportedProduct.import_status == 'imported',
            ImportedProduct.product_id.isnot(None)
        ).all()

        if not imported:
            return jsonify({'error': 'Нет загруженных на WB товаров среди выбранных'}), 400

        # Собираем nm_ids
        product_map = {}
        for imp in imported:
            prod = Product.query.get(imp.product_id)
            if prod and prod.nm_id:
                product_map[prod.nm_id] = {'imp': imp, 'product': prod}

        if not product_map:
            return jsonify({'error': 'Не удалось найти nmID для выбранных товаров'}), 400

        from services.wb_api_client import WildberriesAPIClient
        api = WildberriesAPIClient(seller.wb_api_key)

        results = []
        no_photos = 0
        no_price = 0

        for nm_id, info in product_map.items():
            item = {
                'product_id': info['imp'].id,
                'nm_id': nm_id,
                'title': info['product'].title or info['imp'].title,
                'has_photos': None,
                'has_price': None,
                'issues': [],
            }
            try:
                card = api.get_card_by_nm_id(nm_id, seller_id=seller.id)
                if not card:
                    item['issues'].append('Не найдена на WB')
                    results.append(item)
                    continue

                photos = card.get('photos', [])
                item['has_photos'] = len(photos) > 0
                item['photos_count'] = len(photos)
                if not photos:
                    item['issues'].append('Нет фото')
                    no_photos += 1

                sizes = card.get('sizes', [])
                has_price = False
                for size in sizes:
                    price_info = size.get('price', {})
                    if price_info and price_info.get('total', 0) > 0:
                        has_price = True
                        break
                item['has_price'] = has_price
                if not has_price:
                    item['issues'].append('Нет цены')
                    no_price += 1

            except Exception as e:
                item['issues'].append(f'Ошибка API: {str(e)[:100]}')

            results.append(item)

        return jsonify({
            'total': len(results),
            'no_photos': no_photos,
            'no_price': no_price,
            'with_issues': sum(1 for r in results if r['issues']),
            'results': results,
        })

    # -------------------------------------------------------------------
    # Обновление существующих товаров из базы поставщика
    # -------------------------------------------------------------------
    @app.route('/supplier-catalog/update/review', methods=['POST'])
    @login_required
    @seller_required
    def supplier_catalog_update_review():
        """Read and sign a seller-local supplier update before applying it."""
        seller = current_user.seller
        try:
            supplier_id, source_ids = _supplier_catalog_update_scope_from_form(
                request.form,
            )
        except ValueError as exc:
            supplier_id = request.form.get('supplier_id', type=int)
            flash(str(exc), 'warning')
            if supplier_id and supplier_id > 0:
                return redirect(_safe_supplier_catalog_update_return(
                    supplier_id, request.form.get('return_to'),
                ))
            return 'Некорректный выбор поставщика', 400

        supplier = Supplier.query.filter_by(id=supplier_id).first()
        if supplier is None:
            abort(404)
        active_connection = SellerSupplier.query.filter_by(
            seller_id=seller.id,
            supplier_id=supplier_id,
            is_active=True,
        ).first()
        if active_connection is None:
            abort(404)
        return_to = _safe_supplier_catalog_update_return(
            supplier_id, request.form.get('return_to'),
        )
        try:
            preview = SupplierService.preview_supplier_products(
                seller.id,
                supplier_id,
                source_ids,
                details_limit=50,
            )
        except ValueError as exc:
            flash(str(exc), 'warning')
            return redirect(return_to)

        review_token = None
        if preview.get('changed', 0) > 0:
            review_token = _issue_supplier_catalog_update_review_token(
                user_id=current_user.id,
                seller_id=seller.id,
                supplier_id=supplier_id,
                source_ids=source_ids,
                review_digest=preview.get('review_digest'),
            )
            if not review_token:
                return _render_supplier_catalog_update_review(
                    supplier=supplier,
                    source_ids=source_ids,
                    preview=preview,
                    review_token=None,
                    return_to=return_to,
                    error='Не удалось подготовить проверку. Обновите страницу и повторите выбор.',
                    status=500,
                )

        return _render_supplier_catalog_update_review(
            supplier=supplier,
            source_ids=source_ids,
            preview=preview,
            review_token=review_token,
            return_to=return_to,
        )

    @app.route('/supplier-catalog/update/confirm', methods=['POST'])
    @login_required
    @seller_required
    def supplier_catalog_update_confirm():
        """Recheck and apply only the exact reviewed seller-local source scope."""
        seller = current_user.seller
        try:
            supplier_id, source_ids = _supplier_catalog_update_scope_from_form(
                request.form,
            )
        except ValueError as exc:
            supplier_id = request.form.get('supplier_id', type=int)
            if supplier_id and supplier_id > 0:
                supplier = Supplier.query.filter_by(id=supplier_id).first()
                if supplier is not None:
                    active_connection = SellerSupplier.query.filter_by(
                        seller_id=seller.id,
                        supplier_id=supplier_id,
                        is_active=True,
                    ).first()
                    if active_connection is None:
                        abort(404)
                    return _render_supplier_catalog_update_review(
                        supplier=supplier,
                        source_ids=None,
                        preview=None,
                        review_token=None,
                        return_to=_safe_supplier_catalog_update_return(
                            supplier_id, request.form.get('return_to'),
                        ),
                        error=str(exc),
                        status=400,
                        allow_recheck=False,
                    )
            return 'Некорректная проверка обновления', 400

        supplier = Supplier.query.filter_by(id=supplier_id).first()
        if supplier is None:
            abort(404)
        active_connection = SellerSupplier.query.filter_by(
            seller_id=seller.id,
            supplier_id=supplier_id,
            is_active=True,
        ).first()
        if active_connection is None:
            abort(404)
        return_to = _safe_supplier_catalog_update_return(
            supplier_id, request.form.get('return_to'),
        )
        token_values = request.form.getlist('review_token')
        if len(token_values) != 1:
            return _render_supplier_catalog_update_review(
                supplier=supplier,
                source_ids=source_ids,
                preview=None,
                review_token=None,
                return_to=return_to,
                error='Проверка обновления недоступна. Откройте её заново.',
                status=409,
            )
        try:
            review = _load_supplier_catalog_update_review_token(
                token_values[0],
                user_id=current_user.id,
                seller_id=seller.id,
                supplier_id=supplier_id,
            )
        except SupplierCatalogUpdateReviewTokenError as exc:
            return _render_supplier_catalog_update_review(
                supplier=supplier,
                source_ids=source_ids,
                preview=None,
                review_token=None,
                return_to=return_to,
                error=str(exc),
                status=exc.status_code,
            )

        submitted_scope = 'all' if source_ids is None else 'source_ids'
        if review['scope'] != submitted_scope or review['source_ids'] != source_ids:
            return _render_supplier_catalog_update_review(
                supplier=supplier,
                source_ids=source_ids,
                preview=None,
                review_token=None,
                return_to=return_to,
                error='Выбор товаров не совпадает с проверкой. Откройте её заново.',
                status=409,
            )

        try:
            result = SupplierService.update_supplier_products(
                seller.id,
                supplier_id,
                source_ids,
                expected_review_digest=review['review_digest'],
            )
        except SupplierRefreshReviewStale:
            try:
                current_preview = SupplierService.preview_supplier_products(
                    seller.id,
                    supplier_id,
                    source_ids,
                    details_limit=50,
                )
            except ValueError:
                current_preview = None
            return _render_supplier_catalog_update_review(
                supplier=supplier,
                source_ids=source_ids,
                preview=current_preview,
                review_token=None,
                return_to=return_to,
                error='Данные или доступ к поставщику изменились после проверки. Проверьте обновление заново.',
                status=409,
            )
        except ValueError as exc:
            return _render_supplier_catalog_update_review(
                supplier=supplier,
                source_ids=source_ids,
                preview=None,
                review_token=None,
                return_to=return_to,
                error=f'Проверка больше не подходит: {exc}',
                status=409,
            )
        except Exception as exc:
            db.session.rollback()
            logger.exception(
                'Reviewed supplier catalog update did not commit for seller %s (%s)',
                seller.id,
                type(exc).__name__,
            )
            return _render_supplier_catalog_update_review(
                supplier=supplier,
                source_ids=source_ids,
                preview=None,
                review_token=None,
                return_to=return_to,
                error='Локальное обновление не сохранено. Откройте проверку заново.',
                status=500,
            )

        flash(
            f'Локально обновлено: {result.imported}, пропущено: '
            f'{result.skipped}, ошибок: {result.errors}. '
            'Карточки WB и Ozon не менялись.',
            'success' if result.errors == 0 and result.skipped == 0 else 'warning',
        )
        return redirect(return_to)

    @app.route('/supplier-catalog/update', methods=['POST'])
    @login_required
    @seller_required
    def supplier_catalog_update():
        """Обновить существующие товары из базы поставщика"""
        seller = current_user.seller
        supplier_id = request.form.get('supplier_id', type=int)

        selected_ids_raw = request.form.getlist('update_product_ids')
        fallback_ids_raw = request.form.getlist('product_ids')
        update_all = request.form.get('update_all_for_supplier') == '1'
        if selected_ids_raw and fallback_ids_raw:
            flash('В форме есть два разных списка карточек. Обновите страницу.', 'warning')
            return _redirect_seller_supplier_catalog(supplier_id)
        product_ids_raw = selected_ids_raw or fallback_ids_raw
        try:
            product_ids = _form_bulk_product_ids(product_ids_raw)
        except ValueError as exc:
            flash(str(exc), 'warning')
            return _redirect_seller_supplier_catalog(supplier_id)

        if update_all and product_ids:
            flash(
                'Выберите либо отмеченные карточки, либо весь каталог поставщика.',
                'warning',
            )
            return _redirect_seller_supplier_catalog(supplier_id)
        if not supplier_id or supplier_id <= 0:
            flash('Не указан поставщик для обновления', 'warning')
            return _redirect_seller_supplier_catalog(supplier_id)

        if not product_ids and not update_all:
            flash('Не выбраны карточки для обновления', 'warning')
            return _redirect_seller_supplier_catalog(supplier_id)

        try:
            result = SupplierService.update_supplier_products(
                seller.id,
                supplier_id,
                None if update_all else product_ids,
            )
        except ValueError:
            flash(
                'Не удалось проверить выбранные карточки. Обновите список и повторите выбор.',
                'warning',
            )
            return _redirect_seller_supplier_catalog(supplier_id)
        except Exception as exc:
            db.session.rollback()
            logger.exception(
                'Supplier catalog refresh did not commit for seller %s (%s)',
                seller.id,
                type(exc).__name__,
            )
            flash(
                'Обновление не сохранено. Обновите страницу и повторите действие.',
                'danger',
            )
            return _redirect_seller_supplier_catalog(supplier_id)

        if update_all and result.total_requested == 0:
            flash(
                'У этого поставщика пока нет импортированных карточек',
                'warning',
            )
            return _redirect_seller_supplier_catalog(supplier_id)
        flash(
            f'Локально обновлено: {result.imported}, пропущено: '
            f'{result.skipped}, ошибок: {result.errors}. '
            'Карточки WB не менялись; опубликованные товары можно отдельно '
            'обогатить в разделе «Мои товары».',
            'success' if result.errors == 0 and result.skipped == 0 else 'warning'
        )

        return _redirect_seller_supplier_catalog(supplier_id)

    @app.route('/my-products/<int:product_id>/supplier-diff')
    @login_required
    @seller_required
    def my_products_supplier_diff(product_id):
        """Наглядный диф «что подтянется при „Обновить из каталога"».

        Read-only: сравнивает текущие характеристики локальной карточки с
        актуальным набором общего каталога поставщика. Питает ховер-попап
        бейджа «+N характеристик» в «Моих товарах».
        """
        from services.supplier_service import _supplier_characteristics_payload

        seller = current_user.seller
        imp = ImportedProduct.query.filter_by(
            id=product_id, seller_id=seller.id
        ).first_or_404()
        sp = (
            SupplierProduct.query.get(imp.supplier_product_id)
            if imp.supplier_product_id else None
        )
        if not sp:
            return jsonify({'success': False,
                            'error': 'Нет связи с каталогом поставщика'}), 404

        def _short(value):
            if isinstance(value, list):
                text = ', '.join(str(v) for v in value)
            else:
                text = str(value if value is not None else '')
            return text[:120]

        def _named_map(raw):
            result = {}
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw)
                except (TypeError, ValueError):
                    return result
            if isinstance(raw, dict):
                for name, value in raw.items():
                    if not str(name).startswith('_'):
                        result[str(name)] = value
            elif isinstance(raw, list):
                for item in raw:
                    if isinstance(item, dict) and item.get('name'):
                        result[str(item['name'])] = item.get('value')
            return result

        current_map = _named_map(imp.characteristics)
        fresh_map = _named_map(_supplier_characteristics_payload(sp))

        added = []
        changed = []
        for name, value in fresh_map.items():
            if name not in current_map:
                added.append({'name': name, 'value': _short(value)})
            elif _short(current_map[name]) != _short(value):
                changed.append({
                    'name': name,
                    'old': _short(current_map[name]),
                    'new': _short(value),
                })
        unchanged = len([
            n for n in fresh_map
            if n in current_map and _short(current_map[n]) == _short(fresh_map[n])
        ])

        new_category = None
        if sp.wb_subject_name and sp.wb_subject_name != (imp.mapped_wb_category or ''):
            new_category = {
                'current': imp.mapped_wb_category or None,
                'new': sp.wb_subject_name,
            }

        return jsonify({
            'success': True,
            'category': new_category,
            'added': added[:30],
            'changed': changed[:30],
            'unchanged_count': unchanged,
        })

    @app.route('/api/my-products/supplier-refresh-preview', methods=['POST'])
    @_supplier_refresh_result_no_store
    @login_required
    @seller_required
    def api_my_products_supplier_refresh_preview():
        """Read and sign an exact bounded supplier refresh review."""
        if (
            request.content_length is not None
            and request.content_length > SUPPLIER_REFRESH_REVIEW_MAX_BODY_BYTES
        ):
            return jsonify({
                'success': False,
                'error': 'Список для проверки слишком большой',
            }), 413
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or set(payload) != {'product_ids'}:
            return jsonify({
                'success': False,
                'error': 'Ожидается точный список выбранных карточек',
            }), 400
        try:
            ids = _bulk_product_ids(payload.get('product_ids'))
            if any(product_id > MAX_SIGNED_SQLITE_ID for product_id in ids):
                raise ValueError('Список карточек содержит некорректный ID')
            preview = SupplierService.preview_imported_products(
                current_user.seller.id, ids,
            )
        except ValueError as exc:
            return jsonify({'success': False, 'error': str(exc)}), 400
        review_token = _issue_supplier_refresh_review_token(
            user_id=current_user.id,
            seller_id=current_user.seller.id,
            ids=ids,
            digest=preview.get('review_digest'),
        )
        if not review_token:
            return jsonify({
                'success': False,
                'error': 'Не удалось сохранить проверку выбранных карточек',
            }), 500
        response_payload = {
            'success': True,
            'requested': preview['requested'],
            'eligible': preview['eligible'],
            'changed': preview['changed'],
            'skipped': preview['skipped'],
            'items': preview['items'],
            'review_token': review_token,
        }
        if len(json.dumps(response_payload, ensure_ascii=False).encode('utf-8')) > SUPPLIER_REFRESH_REVIEW_MAX_RESPONSE_BYTES:
            return jsonify({
                'success': False,
                'error': 'Слишком много данных для показа проверки; сократите выбор',
            }), 413
        return jsonify(response_payload)

    @app.route('/my-products/refresh-from-supplier', methods=['POST'])
    @login_required
    @seller_required
    def my_products_refresh_from_supplier():
        """Обновить выбранные локальные карточки из общего каталога поставщика.

        Локальное действие: WB/Ozon не вызываются. Опубликованным карточкам
        после обновления предлагается отдельный подтверждаемый переход в
        массовое обогащение характеристик."""
        seller = current_user.seller
        # Страница на Vue шлёт JSON, классическая форма — selected_ids[].
        # Без этой ветки JSON-клиент получал redirect и трактовал его как
        # истёкшую сессию.
        wants_json = request.is_json
        review_digest = None
        if wants_json:
            if (
                request.content_length is not None
                and request.content_length > SUPPLIER_REFRESH_REVIEW_MAX_BODY_BYTES
            ):
                return jsonify({
                    'success': False,
                    'error': 'Список для обновления слишком большой',
                }), 413
            payload = request.get_json(silent=True)
            if not isinstance(payload, dict):
                return jsonify({
                    'success': False,
                    'error': 'Ожидается объект с выбранными карточками',
                }), 400
            try:
                ids = _bulk_product_ids(payload.get('product_ids'))
                if any(product_id > MAX_SIGNED_SQLITE_ID for product_id in ids):
                    raise ValueError('Список карточек содержит некорректный ID')
                if 'review_token' in payload:
                    if set(payload) != {'product_ids', 'review_token'}:
                        raise ValueError('Проверка обновления недействительна')
                    review = _load_supplier_refresh_review_token(
                        payload.get('review_token'),
                        user_id=current_user.id,
                        seller_id=seller.id,
                    )
                    if review['ids'] != ids:
                        raise ValueError('Выбранные карточки не совпадают с проверкой')
                    review_digest = review['digest']
            except SupplierRefreshReviewTokenError as exc:
                return jsonify({
                    'success': False,
                    'error': str(exc),
                    'code': exc.code,
                }), exc.status_code
            except ValueError as exc:
                return jsonify({'success': False, 'error': str(exc)}), 400
        else:
            raw_ids = request.form.getlist('selected_ids')
            try:
                ids = _form_bulk_product_ids(raw_ids)
            except ValueError:
                flash('Некорректный выбор карточек.', 'warning')
                return _redirect_seller_my_products(request.args)
            if not ids:
                flash('Выберите от 1 до 200 карточек.', 'warning')
                return _redirect_seller_my_products(request.args)

        try:
            if review_digest is None:
                result = SupplierService.update_imported_products(seller.id, ids)
            else:
                result = SupplierService.update_imported_products(
                    seller.id, ids, expected_review_digest=review_digest,
                )
        except SupplierRefreshReviewStale:
            return jsonify({
                'success': False,
                'error': 'Данные изменились после просмотра. Обновите проверку.',
                'code': 'supplier_refresh_review_stale',
            }), 409
        except ValueError as exc:
            if wants_json:
                return jsonify({
                    'success': False,
                    'error': str(exc),
                }), 400
            flash(
                'Не удалось проверить выбранные карточки. Обновите список.',
                'warning',
            )
            return _redirect_seller_my_products(request.args)
        except Exception as exc:
            db.session.rollback()
            logger.exception(
                'Supplier refresh did not commit for seller %s (%s)',
                seller.id,
                type(exc).__name__,
            )
            if wants_json:
                return jsonify({
                    'success': False,
                    'error': 'Обновление не сохранено. Повторите действие позже.',
                }), 500
            flash(
                'Обновление не сохранено. Повторите действие позже.',
                'danger',
            )
            return _redirect_seller_my_products(request.args)

        refreshed = result.imported
        skipped = result.skipped
        errors = result.errors

        # Опубликованные на WB карточки из выбора — кандидаты на
        # дообогащение характеристик отдельным подтверждаемым экраном. Перед
        # передачей читаем их заново, и только для успешно обновлённых exact IDs.
        updated_ids = [
            item['product_id'] for item in result.item_results
            if item['status'] == 'updated'
        ]
        wb_product_by_imported_id = {}
        if updated_ids:
            owned_wb_rows = db.session.query(
                ImportedProduct.id,
                Product.id,
            ).join(
                Product, Product.id == ImportedProduct.product_id,
            ).filter(
                ImportedProduct.seller_id == seller.id,
                ImportedProduct.id.in_(updated_ids),
                ImportedProduct.import_status == 'imported',
                Product.seller_id == seller.id,
                Product.nm_id > 0,
            ).all()
            wb_product_by_imported_id = {
                imported_id: product_id
                for imported_id, product_id in owned_wb_rows
            }
        wb_targets = []
        seen_wb_product_ids = set()
        for imported_id in updated_ids:
            product_id = wb_product_by_imported_id.get(imported_id)
            if product_id and product_id not in seen_wb_product_ids:
                wb_targets.append({
                    'imported_product_id': imported_id,
                    'product_id': product_id,
                })
                seen_wb_product_ids.add(product_id)
        wb_product_ids = [target['product_id'] for target in wb_targets]
        session['supplier_update_wb_ids'] = wb_product_ids[:200]

        message = f'Обновлено локальных карточек: {refreshed}.'
        if skipped:
            message += f' Без связи с каталогом поставщика: {skipped}.'
        if errors:
            message += f' Не удалось обновить: {errors}.'
        if wants_json:
            response_payload = {
                'success': True,
                'status': (
                    'completed_with_errors' if errors
                    else 'completed'
                ),
                'requested': result.total_requested,
                'updated': refreshed,
                'skipped': skipped,
                'errors': errors,
                'items': result.item_results,
                'message': message,
                'wb_ready': len(wb_product_ids),
                'wb_targets': wb_targets,
            }
            receipt = None
            if getattr(result, 'success', None) is True:
                receipt = _issue_supplier_refresh_receipt(
                    user_id=current_user.id,
                    seller_id=seller.id,
                    requested_ids=ids,
                    requested=getattr(result, 'total_requested', None),
                    updated=refreshed,
                    skipped=skipped,
                    errors=errors,
                    item_results=result.item_results,
                    wb_targets=wb_targets,
                )
            if receipt:
                response_payload['resume_receipt'] = receipt
            response = jsonify(response_payload)
            if receipt:
                response.headers['Cache-Control'] = 'no-store'
                response.headers['Pragma'] = 'no-cache'
                response.headers['Expires'] = '0'
            return response
        flash(message, 'warning' if errors else 'success')
        return _redirect_seller_my_products(
            request.args,
            refreshed=refreshed,
            wb_ready=len(wb_product_ids),
        )

    @app.route('/api/my-products/supplier-refresh-result', methods=['GET'])
    @_supplier_refresh_result_no_store
    @login_required
    @seller_required
    def api_my_products_supplier_refresh_result():
        """Resume a verified supplier refresh receipt without repeating work."""
        header_values = request.headers.getlist(SUPPLIER_REFRESH_RECEIPT_HEADER)
        if request.query_string or len(header_values) != 1:
            return jsonify({
                'success': False,
                'error': 'Итог обновления недоступен',
            }), 400
        token = header_values[0]
        if (
            not token
            or ',' in token
            or len(token.encode('utf-8')) > SUPPLIER_REFRESH_RECEIPT_MAX_TOKEN_BYTES
        ):
            return jsonify({
                'success': False,
                'error': 'Итог обновления недоступен',
            }), 400
        try:
            receipt = _load_supplier_refresh_receipt(
                token,
                user_id=current_user.id,
                seller_id=current_user.seller.id,
            )
        except SupplierRefreshReceiptError as exc:
            return jsonify({
                'success': False,
                'error': str(exc),
            }), exc.status_code

        ids = receipt['ids']
        imported_rows = ImportedProduct.query.filter(
            ImportedProduct.seller_id == current_user.seller.id,
            ImportedProduct.id.in_(ids),
        ).all()
        imported_by_id = {row.id: row for row in imported_rows}
        if len(imported_by_id) != len(ids):
            return jsonify({
                'success': False,
                'error': 'Итог обновления недоступен',
            }), 404

        labels = [
            {
                'productId': product_id,
                'label': supplier_refresh_product_label(imported_by_id[product_id]),
            }
            for product_id in ids
        ]
        statuses = receipt['statuses']
        counts = {
            'updated': statuses.count('updated'),
            'skipped': statuses.count('skipped'),
            'error': statuses.count('error'),
        }
        status_reason = {
            'updated': None,
            'skipped': 'no_supplier_link',
            'error': 'update_failed',
        }
        items = [
            {
                'product_id': product_id,
                'status': item_status,
                'reason': status_reason[item_status],
            }
            for product_id, item_status in zip(ids, statuses)
        ]

        original_targets = receipt['wb_targets']
        current_target_rows = []
        if original_targets:
            original_imported_ids = [
                target['imported_product_id'] for target in original_targets
            ]
            current_target_rows = db.session.query(
                ImportedProduct.id,
                Product.id,
            ).join(
                Product, Product.id == ImportedProduct.product_id,
            ).filter(
                ImportedProduct.seller_id == current_user.seller.id,
                ImportedProduct.id.in_(original_imported_ids),
                ImportedProduct.import_status == 'imported',
                Product.seller_id == current_user.seller.id,
                Product.nm_id > 0,
            ).all()
        current_product_by_imported_id = {
            imported_id: product_id
            for imported_id, product_id in current_target_rows
        }
        wb_targets = [
            target for target in original_targets
            if current_product_by_imported_id.get(
                target['imported_product_id'],
            ) == target['product_id']
        ]
        wb_unavailable = len(original_targets) - len(wb_targets)
        message = f'Обновлено локальных карточек: {counts["updated"]}.'
        if counts['skipped']:
            message += f' Без связи с каталогом поставщика: {counts["skipped"]}.'
        if counts['error']:
            message += f' Не удалось обновить: {counts["error"]}.'
        return jsonify({
            'success': True,
            'status': 'completed_with_errors' if counts['error'] else 'completed',
            'requested': len(ids),
            'updated': counts['updated'],
            'skipped': counts['skipped'],
            'errors': counts['error'],
            'items': items,
            'message': message,
            'wb_ready': len(wb_targets),
            'wb_targets': wb_targets,
            'wb_unavailable': wb_unavailable,
            'labels': labels,
        })

    # -------------------------------------------------------------------
    # Дашборд качества парсинга (HTML)
    # -------------------------------------------------------------------
    @app.route('/admin/suppliers/<int:supplier_id>/parsing-quality/dashboard')
    @login_required
    @admin_required
    def admin_supplier_parsing_quality_dashboard(supplier_id):
        """HTML дашборд качества парсинга."""
        supplier = SupplierService.get_supplier(supplier_id)
        if not supplier:
            flash('Поставщик не найден', 'danger')
            return redirect(url_for('admin_suppliers'))
        return render_template(
            'admin_supplier_parsing_quality.html',
            supplier=supplier,
        )

    # -------------------------------------------------------------------
    # API: Качество парсинга
    # -------------------------------------------------------------------
    @app.route('/admin/suppliers/<int:supplier_id>/parsing-quality')
    @login_required
    @admin_required
    def admin_supplier_parsing_quality(supplier_id):
        """Дашборд качества парсинга для поставщика (JSON API)."""
        from models import ParsingLog
        from services.parsing_confidence import ParsingConfidenceScorer
        from services.ai_parsing_cache import AIParsingCache

        supplier = SupplierService.get_supplier(supplier_id)
        if not supplier:
            return jsonify({'error': 'Supplier not found'}), 404

        # Распределение по качеству
        quality_dist = ParsingConfidenceScorer.get_quality_distribution(supplier_id)

        # Статистика AI-кэша
        cache_stats = AIParsingCache.get_cache_stats(supplier_id)

        # Последние логи парсинга
        recent_logs = ParsingLog.query.filter_by(
            supplier_id=supplier_id
        ).order_by(ParsingLog.created_at.desc()).limit(10).all()

        logs_data = []
        for log in recent_logs:
            logs_data.append({
                'event_type': log.event_type,
                'created_at': log.created_at.isoformat() if log.created_at else None,
                'total_products': log.total_products,
                'processed_successfully': log.processed_successfully,
                'errors_count': log.errors_count,
                'duration_seconds': log.duration_seconds,
                'field_fill_rates': log.field_fill_rates,
                'ai_cache_hits': log.ai_cache_hits,
                'ai_cache_misses': log.ai_cache_misses,
            })

        # Средняя заполненность по полям (из последнего лога)
        field_fill = {}
        if recent_logs and recent_logs[0].field_fill_rates:
            field_fill = recent_logs[0].field_fill_rates

        # Маркетплейсовые характеристики — считаем из БД
        marketplace_fill = {}
        all_sp = SupplierProduct.query.filter_by(supplier_id=supplier_id).limit(5000).all()
        if all_sp:
            total = len(all_sp)
            # WB категория
            marketplace_fill['wb_subject_id'] = round(
                sum(1 for p in all_sp if p.wb_subject_id) / total, 3)
            marketplace_fill['wb_subject_name'] = round(
                sum(1 for p in all_sp if p.wb_subject_name) / total, 3)
            # Уверенность маппинга (средняя по тем, у кого есть)
            confs = [p.category_confidence for p in all_sp if p.category_confidence and p.category_confidence > 0]
            marketplace_fill['category_confidence_avg'] = round(
                sum(confs) / len(confs), 3) if confs else 0.0
            # Характеристики WB
            marketplace_fill['characteristics'] = round(
                sum(1 for p in all_sp if p.characteristics_json and p.characteristics_json not in ('[]', '{}', '')) / total, 3)
            # Размеры
            marketplace_fill['sizes'] = round(
                sum(1 for p in all_sp if p.sizes_json and p.sizes_json not in ('[]', '{}', '')) / total, 3)
            # Габариты
            marketplace_fill['dimensions'] = round(
                sum(1 for p in all_sp if p.dimensions_json and p.dimensions_json not in ('[]', '{}', '')) / total, 3)
            # AI-контент
            marketplace_fill['ai_seo_title'] = round(
                sum(1 for p in all_sp if p.ai_seo_title) / total, 3)
            marketplace_fill['ai_description'] = round(
                sum(1 for p in all_sp if p.ai_description) / total, 3)
            marketplace_fill['ai_keywords'] = round(
                sum(1 for p in all_sp if p.ai_keywords_json and p.ai_keywords_json not in ('[]', '')) / total, 3)
            marketplace_fill['ai_bullets'] = round(
                sum(1 for p in all_sp if p.ai_bullets_json and p.ai_bullets_json not in ('[]', '')) / total, 3)
            # Маркетплейс-специфичные поля
            marketplace_fill['marketplace_data'] = round(
                sum(1 for p in all_sp if p.ai_marketplace_json and p.ai_marketplace_json not in ('{}', '')) / total, 3)
            # Статус валидации
            valid_count = sum(1 for p in all_sp if p.marketplace_validation_status == 'valid')
            partial_count = sum(1 for p in all_sp if p.marketplace_validation_status == 'partial')
            invalid_count = sum(1 for p in all_sp if p.marketplace_validation_status == 'invalid')
            marketplace_fill['validation_stats'] = {
                'valid': valid_count,
                'partial': partial_count,
                'invalid': invalid_count,
                'not_checked': total - valid_count - partial_count - invalid_count,
            }

        return jsonify({
            'supplier': {'id': supplier.id, 'name': supplier.name, 'code': supplier.code},
            'quality_distribution': quality_dist,
            'ai_cache': cache_stats,
            'field_fill_rates': field_fill,
            'marketplace_fill_rates': marketplace_fill,
            'recent_logs': logs_data,
            'total_products': supplier.total_products,
        })

    # -------------------------------------------------------------------
    # API: Проверка фото URL
    # -------------------------------------------------------------------
    @app.route('/admin/suppliers/<int:supplier_id>/verify-photos', methods=['POST'])
    @login_required
    @admin_required
    def admin_supplier_verify_photos(supplier_id):
        """Проверка доступности URL фотографий товаров поставщика."""
        from services.photo_url_verifier import PhotoURLVerifier

        supplier = SupplierService.get_supplier(supplier_id)
        if not supplier:
            return jsonify({'error': 'Supplier not found'}), 404

        limit = request.json.get('limit', 100) if request.is_json else 100
        result = PhotoURLVerifier.verify_supplier_photos(
            supplier_id, limit=min(limit, 500)
        )

        broken_details = []
        for detail in result.details[:50]:
            broken_details.append({
                'product_id': detail.product_id,
                'total_urls': detail.total_urls,
                'valid_urls': detail.valid_urls,
                'broken_urls': detail.broken_urls[:5],
                'errors': dict(list(detail.errors.items())[:5]),
            })

        return jsonify({
            'total_products': result.total_products,
            'products_checked': result.products_checked,
            'products_with_broken_photos': result.products_with_broken_photos,
            'total_urls_checked': result.total_urls_checked,
            'total_broken_urls': result.total_broken_urls,
            'duration_seconds': round(result.duration_seconds, 2),
            'broken_details': broken_details,
        })

    # -------------------------------------------------------------------
    # API: Предпросмотр CSV перед синхронизацией
    # -------------------------------------------------------------------
    @app.route('/admin/suppliers/<int:supplier_id>/csv-preview', methods=['POST'])
    @login_required
    @admin_required
    def admin_supplier_csv_preview(supplier_id):
        """Предпросмотр CSV файла перед синхронизацией."""
        from services.supplier_service import SupplierCSVParser

        supplier = SupplierService.get_supplier(supplier_id)
        if not supplier:
            return jsonify({'error': 'Supplier not found'}), 404

        parser = SupplierCSVParser(supplier)

        # Пробуем скачать raw для предвалидации
        raw_bytes = parser.fetch_csv_raw()
        if not raw_bytes:
            return jsonify({'error': 'Failed to download CSV'}), 400

        _, pre_result = parser.preflight_raw(raw_bytes)

        return jsonify({
            'is_valid': pre_result.is_valid,
            'encoding_detected': pre_result.encoding_detected,
            'encoding_confidence': pre_result.encoding_confidence,
            'delimiter_detected': pre_result.delimiter_detected,
            'total_rows': pre_result.total_rows,
            'columns_count': pre_result.columns_count,
            'empty_rows': pre_result.empty_rows,
            'duplicate_ids': pre_result.duplicate_ids,
            'sample_products': pre_result.sample_products,
            'warnings': pre_result.warnings,
            'errors': pre_result.errors,
            'field_fill_rates': pre_result.field_fill_rates,
        })

    # -------------------------------------------------------------------
    # Страница Smart Parser — дашборд умного парсинга
    # -------------------------------------------------------------------
    @app.route('/admin/suppliers/<int:supplier_id>/smart-parser')
    @login_required
    @admin_required
    def admin_supplier_smart_parser(supplier_id):
        """Дашборд умного парсинга товаров поставщика."""
        supplier = SupplierService.get_supplier(supplier_id)
        if not supplier:
            abort(404)

        # Статистика
        total = SupplierProduct.query.filter_by(supplier_id=supplier_id).count()
        brand_resolved = SupplierProduct.query.filter(
            SupplierProduct.supplier_id == supplier_id,
            SupplierProduct.resolved_brand_id.isnot(None),
        ).count()
        brand_unresolved = SupplierProduct.query.filter(
            SupplierProduct.supplier_id == supplier_id,
            SupplierProduct.brand.isnot(None),
            SupplierProduct.brand != '',
            SupplierProduct.resolved_brand_id.is_(None),
        ).count()
        category_mapped = SupplierProduct.query.filter(
            SupplierProduct.supplier_id == supplier_id,
            SupplierProduct.wb_subject_id.isnot(None),
        ).count()
        in_stock = SupplierProduct.query.filter(
            SupplierProduct.supplier_id == supplier_id,
            SupplierProduct.supplier_quantity.isnot(None),
            SupplierProduct.supplier_quantity > 0,
        ).count()

        # Средняя готовность
        avg_q = db.session.query(
            db.func.avg(SupplierProduct.parsing_confidence)
        ).filter(
            SupplierProduct.supplier_id == supplier_id,
            SupplierProduct.parsing_confidence.isnot(None),
        ).scalar()
        avg_readiness = round((avg_q or 0) * 100)

        stats = {
            'total': total,
            'brand_resolved': brand_resolved,
            'brand_unresolved': brand_unresolved,
            'category_mapped': category_mapped,
            'avg_readiness': avg_readiness,
            'in_stock': in_stock,
        }

        return render_template(
            'admin_supplier_smart_parser.html',
            supplier=supplier,
            stats=stats,
        )

    # -------------------------------------------------------------------
    # API: Smart Product Parser — умный парсинг товаров
    # -------------------------------------------------------------------
    @app.route('/admin/suppliers/<int:supplier_id>/smart-parse', methods=['POST'])
    @login_required
    @admin_required
    def admin_supplier_smart_parse(supplier_id):
        """
        Запуск умного парсинга для выбранных товаров.
        Для больших наборов (>2000) запускает фоновую задачу.
        """
        try:
            from services.smart_product_parser import SmartProductParser

            supplier = SupplierService.get_supplier(supplier_id)
            if not supplier:
                return jsonify({'error': 'Supplier not found'}), 404

            data = request.get_json(silent=True) or {}
            product_ids = data.get('product_ids', [])

            # Если не указаны конкретные — берём все товары поставщика
            if not product_ids:
                scope = data.get('scope', 'all')  # all, unparsed, draft
                query = SupplierProduct.query.filter_by(supplier_id=supplier_id)
                if scope == 'unparsed':
                    query = query.filter(
                        db.or_(
                            SupplierProduct.resolved_brand_id.is_(None),
                            SupplierProduct.parsing_confidence.is_(None),
                            SupplierProduct.parsing_confidence < 0.5,
                        )
                    )
                elif scope == 'draft':
                    query = query.filter_by(status='draft')

                product_ids = [p.id for p in query.all()]

            if not product_ids:
                return jsonify({'error': 'Нет товаров для парсинга'}), 400

            # Для больших наборов — фоновая задача
            if len(product_ids) > 2000:
                result = SmartProductParser.start_background_job(
                    supplier_id=supplier_id,
                    product_ids=product_ids,
                    admin_user_id=current_user.id,
                    marketplace_id=data.get('marketplace_id'),
                    scope=data.get('scope', 'all'),
                )
                log_admin_action(
                    admin_user_id=current_user.id,
                    action='smart_parse_job_start',
                    details=f'supplier={supplier.code}, products={len(product_ids)}, job={result["job_id"][:8]}'
                )
                return jsonify({'background': True, **result})

            # Для небольших — синхронно
            parser = SmartProductParser(
                supplier_id=supplier_id,
                marketplace_id=data.get('marketplace_id'),
            )
            result = parser.parse_and_apply_bulk(product_ids)

            log_admin_action(
                admin_user_id=current_user.id,
                action='smart_parse',
                details=f'supplier={supplier.code}, products={len(product_ids)}, '
                        f'brands={result.brand_resolved_count}, '
                        f'cats={result.category_mapped_count}'
            )

            return jsonify(result.to_dict())
        except Exception as e:
            logger.exception(f"Smart parse error for supplier {supplier_id}")
            return jsonify({'error': f'Ошибка парсинга: {str(e)}'}), 500

    @app.route('/admin/suppliers/<int:supplier_id>/smart-parse/<int:product_id>', methods=['POST'])
    @login_required
    @admin_required
    def admin_supplier_smart_parse_single(supplier_id, product_id):
        """Умный парсинг одного товара."""
        try:
            from services.smart_product_parser import SmartProductParser

            supplier = SupplierService.get_supplier(supplier_id)
            if not supplier:
                return jsonify({'error': 'Supplier not found'}), 404

            parser = SmartProductParser(supplier_id=supplier_id)
            result = parser.parse_and_apply_single(product_id)

            return jsonify(result)
        except Exception as e:
            logger.exception(f"Smart parse single error: product {product_id}")
            return jsonify({'error': f'Ошибка: {str(e)}'}), 500

    # -------------------------------------------------------------------
    # API: Smart Parse — статус и отмена фоновой задачи
    # -------------------------------------------------------------------
    @app.route('/admin/suppliers/<int:supplier_id>/smart-parse-job/<job_id>/status')
    @login_required
    @admin_required
    def admin_supplier_smart_parse_job_status(supplier_id, job_id):
        """Статус фоновой задачи Smart Parse (поллинг)."""
        from services.smart_product_parser import SmartProductParser
        data = SmartProductParser.get_job_status(job_id)
        if not data:
            return jsonify({'error': 'Задача не найдена'}), 404
        return jsonify(data)

    @app.route('/admin/suppliers/<int:supplier_id>/smart-parse-job/<job_id>/cancel', methods=['POST'])
    @login_required
    @admin_required
    def admin_supplier_smart_parse_job_cancel(supplier_id, job_id):
        """Отмена фоновой задачи Smart Parse."""
        from services.smart_product_parser import SmartProductParser
        ok = SmartProductParser.cancel_job(job_id)
        return jsonify({'cancelled': ok})

    # -------------------------------------------------------------------
    # API: Валидация характеристик
    # -------------------------------------------------------------------
    @app.route('/admin/suppliers/<int:supplier_id>/validate-characteristics', methods=['POST'])
    @login_required
    @admin_required
    def admin_supplier_validate_characteristics(supplier_id):
        """
        Валидация и автокоррекция AI-извлечённых характеристик.
        Проверяет по справочнику WB, исправляет fuzzy-совпадения.
        """
        try:
            from services.smart_product_parser import CharacteristicsValidator

            supplier = SupplierService.get_supplier(supplier_id)
            if not supplier:
                return jsonify({'error': 'Supplier not found'}), 404

            data = request.get_json(silent=True) or {}
            product_ids = data.get('product_ids', [])
            auto_correct = data.get('auto_correct', True)

            # Если не указаны — берём все с AI-данными
            if not product_ids:
                query = SupplierProduct.query.filter(
                    SupplierProduct.supplier_id == supplier_id,
                    SupplierProduct.ai_marketplace_json.isnot(None),
                )
                product_ids = [p.id for p in query.all()]

            if not product_ids:
                return jsonify({'error': 'Нет товаров для валидации'}), 400

            result = CharacteristicsValidator.validate_bulk(
                product_ids, auto_correct=auto_correct
            )

            log_admin_action(
                admin_user_id=current_user.id,
                action='validate_characteristics',
                details=f'supplier={supplier.code}, products={len(product_ids)}, '
                        f'valid={result["valid"]}, errors={result["with_errors"]}, '
                        f'corrections={result["corrections_applied"]}'
            )

            return jsonify(result)
        except Exception as e:
            logger.exception(f"Characteristics validation error for supplier {supplier_id}")
            return jsonify({'error': f'Ошибка валидации: {str(e)}'}), 500

    @app.route('/admin/suppliers/<int:supplier_id>/validate-characteristics/<int:product_id>')
    @login_required
    @admin_required
    def admin_supplier_validate_single(supplier_id, product_id):
        """Валидация характеристик одного товара (GET для просмотра)."""
        from services.smart_product_parser import CharacteristicsValidator

        result = CharacteristicsValidator.validate_product(
            product_id, auto_correct=False
        )
        return jsonify(result.to_dict())

    # -------------------------------------------------------------------
    # API: Валидация брендов на маркетплейсе
    # -------------------------------------------------------------------
    @app.route('/admin/suppliers/<int:supplier_id>/validate-brands', methods=['POST'])
    @login_required
    @admin_required
    def admin_supplier_validate_brands(supplier_id):
        """
        Массовая валидация брендов поставщика на маркетплейсе.
        Проверяет fuzzy matching с брендами WB.
        """
        try:
            from services.smart_product_parser import SmartProductParser, SmartParseResult

            supplier = SupplierService.get_supplier(supplier_id)
            if not supplier:
                return jsonify({'error': 'Supplier not found'}), 404

            data = request.get_json(silent=True) or {}
            marketplace_id = data.get('marketplace_id')

            # Получаем уникальные бренды поставщика
            query = db.session.query(
                SupplierProduct.brand, db.func.count(SupplierProduct.id).label('cnt')
            ).filter(
                SupplierProduct.supplier_id == supplier_id,
                SupplierProduct.brand.isnot(None),
                SupplierProduct.brand != '',
            ).group_by(SupplierProduct.brand).order_by(db.desc('cnt'))

            brand_rows = query.all()

            parser = SmartProductParser(
                supplier_id=supplier_id,
                marketplace_id=marketplace_id,
            )

            results = []
            resolved = 0
            unresolved = 0

            for brand_name, count in brand_rows:
                result = SmartParseResult()
                parser._resolve_brand(result, brand_name, '')
                parser._validate_brand_on_marketplace(result)

                status = 'valid' if result.brand_marketplace_valid else (
                    'uncertain' if result.brand_marketplace_valid is None else 'invalid'
                )

                if result.brand_marketplace_valid:
                    resolved += 1
                else:
                    unresolved += 1

                results.append({
                    'supplier_brand': brand_name,
                    'products_count': count,
                    'resolved': result.brand_resolved,
                    'canonical': result.brand_canonical,
                    'confidence': result.brand_confidence,
                    'marketplace_status': status,
                    'marketplace_name': result.brand_marketplace_name,
                    'marketplace_id': result.brand_marketplace_id,
                    'suggestions': result.brand_marketplace_suggestions[:5],
                    'warnings': result.warnings[:3],
                })

            return jsonify({
                'total_brands': len(brand_rows),
                'resolved_on_marketplace': resolved,
                'unresolved_on_marketplace': unresolved,
                'brands': results,
            })
        except Exception as e:
            logger.exception(f"Brand validation error for supplier {supplier_id}")
            return jsonify({'error': f'Ошибка валидации брендов: {str(e)}'}), 500

    # -------------------------------------------------------------------
    # API: Применить результаты валидации брендов
    # -------------------------------------------------------------------
    @app.route('/admin/suppliers/<int:supplier_id>/apply-brands', methods=['POST'])
    @login_required
    @admin_required
    def admin_supplier_apply_brands(supplier_id):
        """
        Применить валидацию брендов: обновить resolved_brand_id у товаров,
        создать MarketplaceBrand записи для найденных совпадений.
        """
        try:
            from services.smart_product_parser import SmartProductParser

            supplier = SupplierService.get_supplier(supplier_id)
            if not supplier:
                return jsonify({'error': 'Supplier not found'}), 404

            data = request.get_json(silent=True) or {}
            marketplace_id = data.get('marketplace_id')

            parser = SmartProductParser(
                supplier_id=supplier_id,
                marketplace_id=marketplace_id,
            )
            result = parser.apply_brand_validation(
                supplier_id=supplier_id,
                marketplace_id=marketplace_id,
            )

            log_admin_action(
                admin_user_id=current_user.id,
                action='apply_brand_validation',
                details=f'supplier={supplier.code}, brands={result["total_brands"]}, '
                        f'resolved={result["resolved"]}, '
                        f'marketplace={result["marketplace_matched"]}, '
                        f'products_updated={result["products_updated"]}'
            )

            return jsonify(result)
        except Exception as e:
            logger.exception(f"Apply brand validation error for supplier {supplier_id}")
            return jsonify({'error': f'Ошибка: {str(e)}'}), 500

    # -------------------------------------------------------------------
    # API: Smart Import к продавцу (обогащение + импорт)
    # -------------------------------------------------------------------
    @app.route('/supplier-catalog/smart-import', methods=['POST'])
    @login_required
    @seller_required
    def supplier_catalog_smart_import():
        """
        Умный импорт: SmartParse + импорт к продавцу.
        Обогащает данные перед копированием в ImportedProduct.
        """
        from services.smart_product_parser import SmartProductParser

        seller = current_user.seller
        try:
            supplier_id, product_ids = _supplier_import_form_selection(
                seller.id, request.form,
            )
        except ValueError as exc:
            flash(str(exc), 'warning')
            return _redirect_seller_supplier_catalog(
                request.form.get('supplier_id', type=int)
            )

        if not product_ids:
            flash('Не выбраны товары для импорта', 'warning')
            return _redirect_seller_supplier_catalog(supplier_id)

        draft_account_ids = [
            int(v) for v in request.form.getlist('draft_account_ids')
            if v.isdigit()
        ]
        parser = SmartProductParser(supplier_id=supplier_id)
        result = parser.smart_import_to_seller(
            seller.id,
            product_ids,
            draft_account_ids=draft_account_ids or None,
        )

        if result['success']:
            imp = result['import_result']
            parse = result['parse_result']
            marketplace_note = ''
            if imp.get('marketplace_drafts_created'):
                marketplace_note += (
                    f', Ozon-черновиков: {imp["marketplace_drafts_created"]}'
                )
            if imp.get('marketplace_draft_errors'):
                marketplace_note += (
                    f', Ozon требуют внимания: {imp["marketplace_draft_errors"]}'
                )
            parse_note = (
                f', бренды определены: {parse["brand_resolved_count"]}'
                f', категории: {parse["category_mapped_count"]}'
                f', готовность: {parse["avg_readiness_score"]:.0f}%'
            )
            summary = (
                f'Импортировано: {imp["imported"]}, '
                f'пропущено: {imp["skipped"]}, '
                f'ошибок: {imp["errors"]}{parse_note}{marketplace_note}.'
            )
            category = (
                'success'
                if imp.get('errors', 0) == 0
                and imp.get('marketplace_draft_errors', 0) == 0
                else 'warning'
            )
            if supplier_id is not None:
                zero_import_summary = (
                    f'Новые карточки не добавлены: импортировано 0, '
                    f'пропущено: {imp["skipped"]}, ошибок: {imp["errors"]}'
                    f'{parse_note}{marketplace_note}. '
                    'Выберите карточки для повторного импорта.'
                )
                zero_import_category = (
                    'warning'
                    if imp.get('errors', 0)
                    or imp.get('marketplace_draft_errors', 0)
                    else 'info'
                )
                return _redirect_after_supplier_import(
                    seller_id=seller.id,
                    user_id=current_user.id,
                    supplier_id=supplier_id,
                    supplier_product_ids=product_ids,
                    imported_count=imp['imported'],
                    imported_product_ids=imp.get('imported_product_ids'),
                    summary=summary,
                    category=category,
                    zero_import_summary=zero_import_summary,
                    zero_import_category=zero_import_category,
                )

            flash(
                f'{summary} Проверьте карточки и отправьте их на WB.',
                category,
            )
            # Legacy forms without a declared supplier keep the classic destination.
            return redirect(url_for('seller_my_products'))
        flash(
            f'Ошибка импорта: {"; ".join(result.get("import_result", {}).get("error_messages", [])[:3])}',
            'danger'
        )
        return _redirect_seller_supplier_catalog(supplier_id)

    # -------------------------------------------------------------------
    # API: Поиск категорий WB (для автокомплита)
    # -------------------------------------------------------------------
    @app.route('/api/wb-subjects/search')
    @login_required
    @seller_required
    def api_wb_subjects_search():
        """Поиск категорий WB по названию.
        Сначала ищем в локальной базе (MarketplaceCategory),
        при недостатке результатов — в WB API."""
        from models import MarketplaceCategory

        q = request.args.get('q', '').strip()
        if not q or len(q) < 2:
            return jsonify({'success': True, 'subjects': []})

        # 1) Поиск в локальной базе
        local_results = MarketplaceCategory.query.filter(
            MarketplaceCategory.subject_name.ilike(f'%{q}%')
        ).order_by(MarketplaceCategory.subject_name).limit(30).all()

        subjects = []
        seen_ids = set()
        for cat in local_results:
            if cat.subject_id not in seen_ids:
                seen_ids.add(cat.subject_id)
                display = cat.subject_name
                if cat.parent_name:
                    display = f"{cat.parent_name} > {cat.subject_name}"
                subjects.append({
                    'subject_id': cat.subject_id,
                    'subject_name': cat.subject_name,
                    'parent_name': cat.parent_name or '',
                    'display': display,
                })

        # 2) Если мало результатов и у продавца есть WB API key — дополняем из API
        if len(subjects) < 5:
            seller = current_user.seller
            if seller and seller.has_valid_api_key():
                try:
                    from services.wb_api_client import WildberriesAPIClient
                    api = WildberriesAPIClient(seller.wb_api_key)
                    result = api.get_subjects_list(name=q, limit=50)
                    for item in result.get('data', []):
                        sid = item.get('subjectID')
                        if sid and sid not in seen_ids:
                            seen_ids.add(sid)
                            parent = item.get('parentName', '')
                            sname = item.get('subjectName', '')
                            display = f"{parent} > {sname}" if parent else sname
                            subjects.append({
                                'subject_id': sid,
                                'subject_name': sname,
                                'parent_name': parent,
                                'display': display,
                            })
                except Exception as e:
                    logger.warning(f"WB API subject search failed: {e}")

        return jsonify({'success': True, 'subjects': subjects[:30]})

    # -------------------------------------------------------------------
    # API: Обновить категорию WB на товаре поставщика
    # -------------------------------------------------------------------
    @app.route('/supplier-catalog/<int:supplier_id>/products/<int:product_id>/update-category', methods=['POST'])
    @login_required
    @seller_required
    def supplier_catalog_update_category(supplier_id, product_id):
        """Обновить wb_subject_id и wb_category_name на SupplierProduct."""
        seller = current_user.seller
        conn = SellerSupplier.query.filter_by(
            seller_id=seller.id, supplier_id=supplier_id, is_active=True
        ).first()
        if not conn:
            return jsonify({'success': False, 'error': 'Нет доступа'}), 403

        product = SupplierProduct.query.get(product_id)
        if not product or product.supplier_id != supplier_id:
            return jsonify({'success': False, 'error': 'Товар не найден'}), 404

        data = request.get_json() or {}
        new_subject_id = data.get('subject_id')
        new_subject_name = data.get('subject_name', '')
        new_display = data.get('display', '')

        if not new_subject_id:
            return jsonify({'success': False, 'error': 'subject_id обязателен'}), 400

        product.wb_subject_id = int(new_subject_id)
        product.wb_subject_name = new_subject_name
        product.wb_category_name = new_display or new_subject_name

        db.session.commit()
        logger.info(
            f"Category updated: product={product_id}, "
            f"subject_id={new_subject_id}, name={new_display}"
        )

        return jsonify({
            'success': True,
            'wb_subject_id': product.wb_subject_id,
            'wb_category_name': product.wb_category_name,
        })


# ============================================================================
# HELPERS
# ============================================================================

def _extract_supplier_form_data(form) -> dict:
    """Извлечь данные поставщика из формы"""
    data = {}
    text_fields = [
        'description', 'website', 'csv_source_url', 'csv_delimiter',
        'csv_encoding', 'api_endpoint', 'auth_login', 'auth_password',
        'price_file_url', 'price_file_inf_url', 'price_file_delimiter',
        'price_file_encoding',
        'description_file_url', 'description_file_delimiter', 'description_file_encoding',
        'ai_provider', 'ai_api_key', 'ai_api_base_url', 'ai_model',
        'ai_client_id', 'ai_client_secret',
        'image_background_color',
        'ai_category_instruction', 'ai_size_instruction',
        'ai_seo_title_instruction', 'ai_keywords_instruction',
        'ai_description_instruction', 'ai_analysis_instruction',
        'ai_parsing_instruction',
        'external_id_pattern', 'default_vendor_code_pattern',
    ]
    for f in text_fields:
        val = form.get(f, '').strip()
        if val:
            data[f] = val

    # Числовые поля
    for f in ('ai_temperature', 'ai_max_tokens', 'ai_timeout',
              'default_markup_percent', 'image_target_size',
              'auto_sync_interval_minutes'):
        val = form.get(f)
        if val:
            try:
                data[f] = float(val) if '.' in str(val) else int(val)
            except (ValueError, TypeError):
                pass

    # Булевы поля
    data['ai_enabled'] = form.get('ai_enabled') == 'on'
    data['ai_proxy_enabled'] = form.get('ai_proxy_enabled') == 'on'
    data['is_active'] = form.get('is_active') == 'on'
    data['resize_images'] = form.get('resize_images') == 'on'
    data['auto_sync_prices'] = form.get('auto_sync_prices') == 'on'
    data['csv_has_header'] = form.get('csv_has_header') == 'on'

    # JSON поля
    csv_column_mapping = form.get('csv_column_mapping', '').strip()
    if csv_column_mapping:
        try:
            data['csv_column_mapping'] = json.loads(csv_column_mapping)
        except (json.JSONDecodeError, TypeError):
            pass

    return data


def _calc_product_completeness(product) -> dict:
    """Расчёт заполненности полей товара по группам"""
    import json

    def _has(val):
        if val is None:
            return False
        if isinstance(val, str):
            val = val.strip()
            if not val or val in ('[]', '{}', '""'):
                return False
            # JSON arrays/objects — check for content
            if val.startswith('[') or val.startswith('{'):
                try:
                    parsed = json.loads(val)
                    return bool(parsed)
                except Exception:
                    pass
            return True
        if isinstance(val, (int, float)):
            return True
        return bool(val)

    groups = {
        'basic': {
            'label': 'Основные',
            'fields': {
                'Название': _has(product.title),
                'Описание': _has(product.description),
                'Бренд': _has(product.brand),
                'Категория': _has(product.category),
                'Артикул': _has(product.vendor_code),
                'Штрихкод': _has(product.barcode),
            }
        },
        'price': {
            'label': 'Цены и остатки',
            'fields': {
                'Цена поставщика': _has(product.supplier_price),
                'Остаток': _has(product.supplier_quantity),
                'РРЦ': _has(product.recommended_retail_price),
                'Статус наличия': _has(product.supplier_status),
            }
        },
        'characteristics': {
            'label': 'Характеристики',
            'fields': {
                'Пол': _has(product.gender),
                'Страна': _has(product.country),
                'Сезон': _has(product.season),
                'Возрастная группа': _has(product.age_group),
                'Цвета': _has(product.colors_json),
                'Материалы': _has(product.materials_json),
                'Размеры': _has(product.sizes_json),
                'Габариты': _has(product.dimensions_json),
                'Характеристики': _has(product.characteristics_json),
            }
        },
        'ai': {
            'label': 'AI данные',
            'fields': {
                'SEO заголовок': _has(product.ai_seo_title),
                'AI описание': _has(product.ai_description),
                'Ключевые слова': _has(product.ai_keywords_json),
                'Буллеты': _has(product.ai_bullets_json),
                'AI парсинг': _has(product.ai_parsed_data_json),
            }
        },
        'marketplace': {
            'label': 'Маркетплейс',
            'fields': {
                'Категория WB': _has(product.wb_category_name),
                'WB Subject ID': _has(product.wb_subject_id),
                'Поля маркетплейса': _has(product.marketplace_fields_json),
                'Валидация': product.marketplace_validation_status == 'valid',
            }
        },
        'media': {
            'label': 'Медиа',
            'fields': {
                'Фотографии': bool(product.get_photos()),
                'Видео': _has(product.video_url),
            }
        },
    }

    # Считаем по каждой группе
    total_filled = 0
    total_fields = 0
    for key, group in groups.items():
        filled = sum(1 for v in group['fields'].values() if v)
        count = len(group['fields'])
        group['filled'] = filled
        group['total'] = count
        group['pct'] = round(filled / count * 100) if count else 0
        total_filled += filled
        total_fields += count

    return {
        'groups': {k: {
            'label': g['label'],
            'pct': g['pct'],
            'filled': g['filled'],
            'total': g['total'],
            'fields': g['fields'],
        } for k, g in groups.items()},
        'total_pct': round(total_filled / total_fields * 100) if total_fields else 0,
        'total_filled': total_filled,
        'total_fields': total_fields,
    }


def _extract_product_form_data(form) -> dict:
    """Извлечь данные товара поставщика из формы"""
    data = {}
    text_fields = [
        'title', 'description', 'brand', 'category', 'vendor_code',
        'wb_category_name', 'wb_subject_name', 'gender', 'country',
        'season', 'age_group', 'status', 'ai_seo_title', 'ai_description',
    ]
    for f in text_fields:
        val = form.get(f)
        if val is not None:
            data[f] = val.strip()

    # Числовые
    for f in ('wb_subject_id', 'supplier_price', 'supplier_quantity'):
        val = form.get(f)
        if val:
            try:
                data[f] = float(val) if '.' in str(val) else int(val)
            except (ValueError, TypeError):
                pass

    return data
