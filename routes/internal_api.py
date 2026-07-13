# -*- coding: utf-8 -*-
"""
Internal API — эндпоинты для сервисных AI-агентов (Python ADK).

Все эндпоинты требуют аутентификации через заголовок X-Agent-Key.
Prefix: /internal/v1/

Blueprint исключён из CSRF-защиты (агенты аутентифицируются через X-Agent-Key).
"""
import json
import html
import logging
import re
from difflib import SequenceMatcher
from functools import wraps
from datetime import datetime, timedelta, timezone

from flask import request, jsonify, abort, Blueprint
from sqlalchemy import func
from sqlalchemy.orm import load_only
from werkzeug.security import check_password_hash

from models import (
    db, ServiceAgent, Product, ImportedProduct, SupplierProduct, Seller,
    Marketplace, MarketplaceCategory, MarketplaceCategoryCharacteristic,
    MarketplaceDirectory, PricingSettings, ProhibitedWord,
    Brand, BrandAlias, MarketplaceBrand, BrandCategoryLink,
    AgentChangeSnapshot, CardEditHistory, SystemSettings, AutoImportSettings,
    AgentTask, ProductDefaults, APILog, AgentReviewProposal,
    Supplier, SellerSupplier,
)
from services import agent_service
from services.brand_reference_service import (
    parse_positive_integer,
    preflight_brand_categories,
    resolve_exact_brand_categories,
)

logger = logging.getLogger(__name__)

internal_api_bp = Blueprint('internal_api', __name__, url_prefix='/internal/v1')


def _owned_task(task_id: str):
    """Возвращает task только если он назначен аутентифицированному агенту."""
    task = db.session.get(AgentTask, task_id)
    if not task or str(task.agent_id) != str(request._agent.id):
        return None
    return task


def _owned_task_or_404(task_id: str):
    task = _owned_task(task_id)
    if not task:
        return None, (jsonify({'error': 'Task not found'}), 404)
    return task, None


def _task_mutation_payload(task: AgentTask) -> dict:
    """Return only the status fields needed after a task mutation.

    Poll/get endpoints intentionally expose the task input or result. Mutation
    endpoints must not echo those potentially large blobs back to the worker on
    every progress update or checkpoint.
    """
    return {
        'id': task.id,
        'status': task.status,
        'total_steps': task.total_steps,
        'completed_steps': task.completed_steps,
        'current_step_label': task.current_step_label,
        'progress_percent': task.progress_percent,
        'duration_seconds': task.duration_seconds,
        'error_message': task.error_message,
        'started_at': task.started_at.isoformat() if task.started_at else None,
        'completed_at': task.completed_at.isoformat() if task.completed_at else None,
    }


def _assigned_task_for_seller(seller_id: int = None):
    """Проверяет X-Task-Id и seller scope для data/tool endpoints."""
    task_id = request.headers.get('X-Task-Id', '').strip()
    if not task_id:
        return None, (jsonify({'error': 'X-Task-Id header is required'}), 403)
    task = _owned_task(task_id)
    if not task:
        return None, (jsonify({'error': 'Assigned task not found'}), 403)
    if task.status not in ('queued', 'running'):
        return None, (jsonify({'error': 'Assigned task is not active'}), 403)
    if seller_id is not None and int(task.seller_id) != int(seller_id):
        return None, (jsonify({'error': 'Seller is outside assigned task scope'}), 403)
    return task, None


def _authenticate_agent(f):
    """Декоратор: аутентификация агента по X-Agent-Id + X-Agent-Key."""
    @wraps(f)
    def decorated(*args, **kwargs):
        agent_id = request.headers.get('X-Agent-Id', '')
        agent_key = request.headers.get('X-Agent-Key', '')

        if not agent_id or not agent_key:
            return jsonify({'error': 'Missing X-Agent-Id or X-Agent-Key header'}), 401

        agent = db.session.get(ServiceAgent, agent_id)
        if not agent:
            return jsonify({'error': 'Unknown agent'}), 401

        if not agent.api_key_hash or not check_password_hash(agent.api_key_hash, agent_key):
            return jsonify({'error': 'Invalid agent key'}), 401

        request._agent = agent
        return f(*args, **kwargs)
    return decorated


# ── LLM Config ─────────────────────────────────────────────────

# Ключи LLM-настроек в SystemSettings
_LLM_CONFIG_KEYS = {
    'llm_provider': 'LLM_PROVIDER',
    'llm_model': 'CLOUDRU_MODEL',
    'llm_base_url': 'CLOUDRU_BASE_URL',
    'fallback_provider': 'FALLBACK_LLM_PROVIDER',
    'fallback_model': 'FALLBACK_LLM_MODEL',
    'step_namer_provider': 'STEP_NAMER_PROVIDER',
    'step_namer_model': 'STEP_NAMER_MODEL',
    'openrouter_model': 'OPENROUTER_MODEL',
    'deepseek_model': 'DEEPSEEK_MODEL',
    'claude_model': 'CLAUDE_MODEL',
    'gemini_model': 'GEMINI_MODEL',
    'llm_temperature': 'LLM_TEMPERATURE',
    'llm_max_tokens': 'LLM_MAX_TOKENS',
}


@internal_api_bp.route('/config/llm', methods=['GET'])
@_authenticate_agent
def internal_llm_config():
    """Return non-secret runtime defaults; task-scoped endpoint owns credentials."""
    config = {}
    for db_key, env_key in _LLM_CONFIG_KEYS.items():
        setting = SystemSettings.query.filter_by(key=f'agent_{db_key}').first()
        if setting and setting.value:
            config[env_key] = setting.get_value()
    return jsonify({'config': config})


# ── Heartbeat ───────────────────────────────────────────────────

@internal_api_bp.route('/heartbeat', methods=['POST'])
@_authenticate_agent
def internal_heartbeat():
    """Агент шлёт heartbeat для подтверждения online-статуса."""
    data = request.get_json(silent=True) or {}
    agent = agent_service.heartbeat(
        request._agent.id,
        status=data.get('status', 'online'),
        error=data.get('error'),
    )
    return jsonify({'ok': True, 'agent': agent.to_dict()})


# ── Задачи: получение очереди ───────────────────────────────────

@internal_api_bp.route('/tasks/poll', methods=['GET'])
@_authenticate_agent
def internal_poll_tasks():
    """Агент запрашивает очередь своих задач."""
    limit = request.args.get('limit', 10, type=int)
    tasks = agent_service.get_pending_tasks(request._agent.id, limit=limit)
    return jsonify({
        'tasks': [t.to_dict() for t in tasks],
        'count': len(tasks),
    })


# ── Задачи: обновление статуса ──────────────────────────────────

@internal_api_bp.route('/tasks/<task_id>/start', methods=['POST'])
@_authenticate_agent
def internal_start_task(task_id):
    """Агент берёт задачу в работу."""
    _, error = _owned_task_or_404(task_id)
    if error:
        return error
    task = agent_service.start_task(task_id)
    if not task:
        return jsonify({'error': 'Task not found or not in queued state'}), 404
    return jsonify({'ok': True, 'task': _task_mutation_payload(task)})


@internal_api_bp.route('/tasks/<task_id>/progress', methods=['POST'])
@_authenticate_agent
def internal_update_progress(task_id):
    """Агент обновляет прогресс задачи."""
    _, error = _owned_task_or_404(task_id)
    if error:
        return error
    data = request.get_json(silent=True) or {}
    task = agent_service.update_task_progress(
        task_id,
        completed_steps=data.get('completed_steps', 0),
        current_step_label=data.get('current_step_label'),
        total_steps=data.get('total_steps'),
    )
    if not task:
        return jsonify({'error': 'Task not found'}), 404
    return jsonify({'ok': True, 'task': _task_mutation_payload(task)})


@internal_api_bp.route('/tasks/<task_id>/checkpoint', methods=['POST'])
@_authenticate_agent
def internal_update_checkpoint(task_id):
    """Persist a skill-boundary checkpoint for crash-safe resume."""
    _, error = _owned_task_or_404(task_id)
    if error:
        return error
    data = request.get_json(silent=True) or {}
    checkpoint = data.get('checkpoint')
    if not isinstance(checkpoint, dict):
        return jsonify({'error': 'checkpoint must be an object'}), 400
    task = agent_service.update_task_checkpoint(task_id, checkpoint)
    if not task:
        return jsonify({'error': 'Task is not active'}), 409
    return jsonify({'ok': True, 'task': _task_mutation_payload(task)})


@internal_api_bp.route('/tasks/<task_id>/complete', methods=['POST'])
@_authenticate_agent
def internal_complete_task(task_id):
    """Агент завершает задачу успешно."""
    _, error = _owned_task_or_404(task_id)
    if error:
        return error
    data = request.get_json(silent=True) or {}
    task = agent_service.complete_task(task_id, result_data=data.get('result'))
    if not task:
        return jsonify({'error': 'Task not found'}), 404
    return jsonify({'ok': True, 'task': _task_mutation_payload(task)})


@internal_api_bp.route('/tasks/<task_id>/fail', methods=['POST'])
@_authenticate_agent
def internal_fail_task(task_id):
    """Агент сообщает об ошибке."""
    _, error = _owned_task_or_404(task_id)
    if error:
        return error
    data = request.get_json(silent=True) or {}
    task = agent_service.fail_task(
        task_id,
        error_message=data.get('error', 'Unknown error'),
        result_data=data.get('result'),
    )
    if not task:
        return jsonify({'error': 'Task not found'}), 404
    return jsonify({'ok': True, 'task': _task_mutation_payload(task)})


# ── Шаги задач ──────────────────────────────────────────────────

@internal_api_bp.route('/tasks/<task_id>/steps', methods=['POST'])
@_authenticate_agent
def internal_add_step(task_id):
    """Агент логирует шаг выполнения задачи."""
    _, error = _owned_task_or_404(task_id)
    if error:
        return error
    data = request.get_json(silent=True) or {}
    step = agent_service.add_task_step(
        task_id=task_id,
        step_type=data.get('step_type', 'action'),
        title=data.get('title', ''),
        detail=data.get('detail'),
        status=data.get('status', 'completed'),
        duration_ms=data.get('duration_ms'),
        metadata=data.get('metadata'),
    )
    if not step:
        return jsonify({'error': 'Task not found'}), 404
    return jsonify({'ok': True, 'step': step.to_dict()})


@internal_api_bp.route('/tasks/<task_id>/steps', methods=['GET'])
@_authenticate_agent
def internal_get_steps(task_id):
    """Получить шаги задачи."""
    _, error = _owned_task_or_404(task_id)
    if error:
        return error
    steps = agent_service.get_task_steps(task_id)
    return jsonify({'steps': [s.to_dict() for s in steps]})


@internal_api_bp.route('/tasks/<task_id>/ai-config', methods=['GET'])
@_authenticate_agent
def internal_task_ai_config(task_id):
    """Возвращает секретный AI profile только агенту-владельцу task."""
    task, error = _owned_task_or_404(task_id)
    if error:
        return error

    settings = AutoImportSettings.query.filter_by(seller_id=task.seller_id).first()
    provider = (settings.ai_provider if settings else None) or 'deepseek'
    model = (settings.ai_model if settings else None) or 'deepseek-v4-pro'
    api_key = settings.ai_api_key if settings else None
    base_url = settings.ai_api_base_url if settings else None
    single_model = getattr(settings, 'agent_single_model', False) if settings else False

    return jsonify({
        'ai_config': {
            'provider': provider,
            'model': model,
            'key': api_key,
            'base_url': base_url,
            'single_model': bool(single_model),
        },
    })


# ── Данные: товары ──────────────────────────────────────────────

@internal_api_bp.route('/sellers/<int:seller_id>/products', methods=['GET'])
@_authenticate_agent
def internal_list_products(seller_id):
    """Получить товары продавца."""
    _, error = _assigned_task_for_seller(seller_id)
    if error:
        return error
    page = request.args.get('page', 1, type=int)
    per_page = min(request.args.get('per_page', 50, type=int), 200)
    status = request.args.get('status')

    q = Product.query.filter_by(seller_id=seller_id)
    if status:
        # Product не имеет wb_status — фильтруем по is_active
        if status in ('active', 'enabled'):
            q = q.filter_by(is_active=True)
        elif status in ('inactive', 'disabled'):
            q = q.filter_by(is_active=False)

    total = q.count()
    products = q.offset((page - 1) * per_page).limit(per_page).all()

    return jsonify({
        'products': [_product_to_dict(p) for p in products],
        'total': total,
        'page': page,
        'per_page': per_page,
    })


@internal_api_bp.route('/sellers/<int:seller_id>/products/<int:product_id>', methods=['GET'])
@_authenticate_agent
def internal_get_product(seller_id, product_id):
    """Получить конкретный товар."""
    _, error = _assigned_task_for_seller(seller_id)
    if error:
        return error
    product = Product.query.filter_by(id=product_id, seller_id=seller_id).first()
    if not product:
        return jsonify({'error': 'Product not found'}), 404
    return jsonify({'product': _product_to_dict(product)})


@internal_api_bp.route('/sellers/<int:seller_id>/products/query', methods=['GET'])
@_authenticate_agent
def internal_query_products(seller_id):
    """One-query deterministic filters for WB Product cards."""
    _, error = _assigned_task_for_seller(seller_id)
    if error:
        return error
    active = (request.args.get('active') or '').strip().lower()
    stock_state = (request.args.get('stock_state') or '').strip().lower()
    quality_max = request.args.get('quality_max', type=float)
    if active and active not in {'yes', 'no'}:
        return jsonify({'error': 'active must be yes or no'}), 400
    if stock_state and stock_state not in {'in_stock', 'out_of_stock'}:
        return jsonify({'error': 'invalid stock_state'}), 400
    if request.args.get('quality_max') is not None and quality_max is None:
        return jsonify({'error': 'quality_max must be numeric'}), 400
    limit = min(max(request.args.get('limit', 100, type=int), 1), 200)
    query = Product.query.filter_by(seller_id=seller_id)
    if active == 'yes':
        query = query.filter(Product.is_active.is_(True))
    elif active == 'no':
        query = query.filter(Product.is_active.is_(False))
    if stock_state == 'out_of_stock':
        query = query.filter(Product.quantity <= 0)
    elif stock_state == 'in_stock':
        query = query.filter(Product.quantity > 0)
    if quality_max is not None:
        query = query.filter(Product.quality_score < quality_max)
    rows = query.options(load_only(
        Product.id, Product.nm_id, Product.title, Product.vendor_code,
        Product.price, Product.discount_price, Product.quantity,
        Product.quality_score, Product.is_active,
    )).add_columns(
        func.count(Product.id).over().label('matched_total'),
    ).order_by(Product.updated_at.desc(), Product.id.desc()).limit(limit).all()
    products = [row[0] for row in rows]
    total = int(rows[0].matched_total) if rows else 0
    return jsonify({
        'total': total,
        'products': [{
            'id': product.id,
            'nm_id': product.nm_id,
            'title': html.unescape(product.title or '')[:180],
            'vendor_code': product.vendor_code,
            'price': product.discount_price or product.price,
            'supplier_quantity': product.quantity,
            'quality_score': product.quality_score,
            'is_active': bool(product.is_active),
        } for product in products],
        'truncated': total > len(products),
    })


@internal_api_bp.route('/sellers/<int:seller_id>/products/quality-brief', methods=['POST'])
@_authenticate_agent
def internal_products_quality_brief(seller_id):
    """Качество карточек для агентного рантайма (read-only).

    Body {'product_ids': [...]} (до 50) — явная выборка; без него — топ
    проблемных по quality_impact, опционально ?reason=<код>. Protected
    fields (цены/остатки/ключи) не возвращаются.
    """
    _, error = _assigned_task_for_seller(seller_id)
    if error:
        return error
    from services.card_quality_scorer import ATTENTION_REASONS, REASON_LABELS

    body = request.get_json(silent=True) or {}
    raw_ids = body.get('product_ids') or []
    if not isinstance(raw_ids, list):
        return jsonify({'error': 'product_ids must be a list'}), 400
    ids = [int(x) for x in raw_ids if str(x).isdigit()][:50]
    reason = (request.args.get('reason') or '').strip()
    if reason and reason not in ATTENTION_REASONS:
        return jsonify({'error': 'unknown reason'}), 400
    limit = min(max(request.args.get('limit', 30, type=int), 1), 50)

    q = Product.query.filter_by(seller_id=seller_id, is_active=True)
    if ids:
        q = q.filter(Product.id.in_(ids))
    else:
        q = q.filter(Product.attention_reasons.isnot(None),
                     Product.attention_reasons != '')
        if reason:
            q = q.filter(Product.attention_reasons.like(f'%{reason}%'))
    rows = q.order_by(Product.quality_impact.desc().nullslast()).limit(limit).all()

    def _top_recommendations(p):
        try:
            dims = json.loads(p.quality_breakdown_json) if p.quality_breakdown_json else {}
        except (ValueError, TypeError):
            dims = {}
        cand = [(d.get('weight', 0) * (100 - d.get('score', 0)), d.get('hint'))
                for d in dims.values() if isinstance(d, dict) and d.get('hint')]
        cand.sort(key=lambda t: -t[0])
        return [hint for _, hint in cand[:3]]

    return jsonify({
        'reason_labels': REASON_LABELS,
        'total': len(rows),
        'products': [{
            'id': p.id,
            'nm_id': p.nm_id,
            'vendor_code': p.vendor_code,
            'title': html.unescape(p.title or '')[:180],
            'quality_score': p.quality_score,
            'quality_impact': p.quality_impact,
            'attention_reasons': [r for r in (p.attention_reasons or '').split(',') if r],
            'wb_rating': p.nm_rating,
            'wb_views_30d': p.wb_views_30d,
            'wb_orders_30d': p.wb_orders_30d,
            'wb_cart_conv': p.wb_cart_conv,
            'wb_buyout_rate': p.wb_buyout_rate,
            'recommendations': _top_recommendations(p),
        } for p in rows],
    })


@internal_api_bp.route('/sellers/<int:seller_id>/products/<int:product_id>', methods=['PATCH'])
@_authenticate_agent
def internal_update_product(seller_id, product_id):
    """Agent update for a WB product with an auditable rollback snapshot."""
    task, error = _assigned_task_for_seller(seller_id)
    if error:
        return error
    product = Product.query.filter_by(id=product_id, seller_id=seller_id).first()
    if not product:
        return jsonify({'error': 'Product not found'}), 404

    data = request.get_json(silent=True) or {}
    protected_fields = sorted(set(data) & _PRODUCT_PROTECTED_FIELDS)
    if protected_fields:
        return jsonify({
            'ok': False,
            'changed': False,
            'requires_manual_review': True,
            'protected_fields': protected_fields,
            'error': (
                'Цены и остатки основной карточки нельзя изменить агентом. '
                'Проверьте и примените их вручную.'
            ),
        }), 409
    reference_error = _validate_product_reference_update(product, data)
    if reference_error:
        return jsonify({
            'ok': False,
            'changed': False,
            'error': reference_error,
            'reference_data_blocked': True,
        }), 409
    changed_fields, snapshot_before, snapshot_after = _apply_product_fields(product, data)

    if changed_fields:
        product.updated_at = datetime.utcnow()
        db.session.add(CardEditHistory(
            product_id=product.id, seller_id=seller_id, action='update',
            changed_fields=changed_fields,
            snapshot_before=snapshot_before, snapshot_after=snapshot_after,
            wb_synced=False, wb_sync_status='pending',
            user_comment=f'agent_task:{task.id}',
        ))
    db.session.commit()
    return jsonify({
        'ok': True, 'changed': bool(changed_fields),
        'changed_fields': changed_fields, 'product': _product_to_dict(product),
    })


_PRODUCT_PROTECTED_FIELDS = {
    'price', 'wb_price', 'discount_price', 'wb_discounted_price',
    'supplier_price', 'quantity', 'stock', 'stocks', 'amount',
}

_PRODUCT_FIELD_MAP = {
    'title': 'title', 'description': 'description', 'brand': 'brand',
    'vendor_code': 'vendor_code', 'characteristics': 'characteristics_json',
    'tags': 'tags_json', 'wb_category_id': 'subject_id',
    'wb_category_name': 'object_name',
}


def _parse_subject_id(raw_value):
    try:
        return parse_positive_integer(raw_value, 'subject_id')
    except ValueError:
        return None


def _brand_reference_cache_key(brand_name, subject_id):
    from services.brand_engine import normalize_for_comparison

    return normalize_for_comparison(brand_name), _parse_subject_id(subject_id)


def _prime_agent_brand_write_cache(pairs, validation_cache):
    """Resolve all explicit brand writes once for a request-sized batch."""
    cache = validation_cache.setdefault('agent_write_brands', {})
    pending = []
    for brand_name, subject_id in pairs:
        key = _brand_reference_cache_key(brand_name, subject_id)
        if key in cache or len(str(brand_name or '').strip()) < 2 or not key[1]:
            continue
        pending.append({
            'request_id': key,
            'brand': str(brand_name).strip(),
            'category_id': key[1],
        })
    # De-duplicate exact normalized pairs while preserving bounded input.
    unique = {item['request_id']: item for item in pending}
    if not unique:
        return
    for result in resolve_exact_brand_categories(list(unique.values())):
        cache[result['request_id']] = result


def _validate_agent_brand_write(
    brand_name, subject_id, validation_cache=None,
):
    """Fail closed and return only the canonical WB marketplace spelling."""
    brand_name = str(brand_name or '').strip()
    if len(brand_name) < 2 or len(brand_name) > 200:
        return None, 'Бренд должен содержать от 2 до 200 символов'
    subject_id = _parse_subject_id(subject_id)
    if not subject_id:
        return None, 'Нельзя записать бренд без категории WB'

    validation_cache = validation_cache if validation_cache is not None else {}
    key = _brand_reference_cache_key(brand_name, subject_id)
    cache = validation_cache.setdefault('agent_write_brands', {})
    if key not in cache:
        _prime_agent_brand_write_cache(
            [(brand_name, subject_id)], validation_cache,
        )
    result = cache.get(key) or {}
    reference_status = result.get('reference_status') or {}
    if not reference_status.get('usable'):
        return None, (
            'Справочник брендов WB для этой категории недоступен или '
            'устарел; запись остановлена'
        )
    canonical = str(result.get('marketplace_brand_name') or '').strip()
    if (
        result.get('status') != 'found'
        or result.get('category_available') is not True
        or not canonical
    ):
        return None, (
            f'Бренд "{brand_name}" не подтверждён в категории WB '
            f'{subject_id}; запись остановлена'
        )
    return canonical, None


def _wb_category_for_agent_write(subject_id, validation_cache=None):
    """Resolve a category only from a fresh, successful WB catalog snapshot."""
    category_cache = None
    if validation_cache is not None:
        category_cache = validation_cache.setdefault('agent_write_categories', {})
        if subject_id in category_cache:
            return category_cache[subject_id]
        if 'agent_write_marketplace' in validation_cache:
            marketplace = validation_cache['agent_write_marketplace']
        else:
            marketplace = Marketplace.query.filter_by(code='wb').first()
            validation_cache['agent_write_marketplace'] = marketplace
    else:
        marketplace = Marketplace.query.filter_by(code='wb').first()
    status = _wb_reference_status(
        'wb_categories',
        marketplace.categories_synced_at if marketplace else None,
        marketplace.categories_sync_status if marketplace else None,
        getattr(marketplace, 'categories_sync_error', None) if marketplace else None,
        available=bool(marketplace and marketplace.is_active),
        has_data=bool(marketplace and marketplace.total_categories),
    )
    if not status['usable']:
        result = (
            None,
            'Справочник категорий WB недоступен или устарел; запись остановлена',
        )
        if category_cache is not None:
            category_cache[subject_id] = result
        return result

    category = MarketplaceCategory.query.filter_by(
        marketplace_id=marketplace.id,
        subject_id=subject_id,
    ).first()
    if not category:
        result = (
            None,
            f'Категория WB с subject_id={subject_id} не найдена в справочнике',
        )
        if category_cache is not None:
            category_cache[subject_id] = result
        return result
    if not getattr(category, 'is_available', True):
        result = (
            None,
            f'Категория "{category.subject_name}" (id={subject_id}) больше недоступна в WB',
        )
        if category_cache is not None:
            category_cache[subject_id] = result
        return result
    if not category.is_leaf:
        result = (
            None,
            f'Категория "{category.subject_name}" (id={subject_id}) не является конечной',
        )
        if category_cache is not None:
            category_cache[subject_id] = result
        return result
    if not category.is_enabled:
        result = (
            None,
            f'Категория "{category.subject_name}" (id={subject_id}) не включена в системе',
        )
        if category_cache is not None:
            category_cache[subject_id] = result
        return result
    result = (category, None)
    if category_cache is not None:
        category_cache[subject_id] = result
    return result


def _decode_agent_characteristics(raw_value):
    value = raw_value
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return None, None, 'Характеристики должны быть валидным JSON'

    names = set()
    charc_ids = set()
    if isinstance(value, dict):
        if not value:
            return None, None, 'Пустой patch характеристик недопустим; очистка возможна только вручную'
        names = {str(key) for key in value}
    elif isinstance(value, list):
        if not value:
            return None, None, 'Пустой patch характеристик недопустим; очистка возможна только вручную'
        for item in value:
            if not isinstance(item, dict):
                return None, None, 'Элементы списка характеристик должны быть объектами'
            raw_id = item.get('charc_id', item.get('id'))
            if raw_id is not None:
                try:
                    charc_ids.add(int(raw_id))
                except (TypeError, ValueError):
                    return None, None, 'Некорректный ID характеристики'
            elif item.get('name'):
                names.add(str(item['name']))
            else:
                return None, None, 'У характеристики нет id или name'
    else:
        return None, None, 'Характеристики должны быть JSON-объектом или списком'
    return names, charc_ids, None


def _validate_agent_characteristics_write(
    subject_id, raw_value, validation_cache=None,
):
    validation_cache = validation_cache if validation_cache is not None else {}
    category, error = _wb_category_for_agent_write(
        subject_id, validation_cache,
    )
    if error:
        return None, error

    status = _wb_reference_status(
        f'wb_category_characteristics:{subject_id}',
        category.characteristics_synced_at,
        getattr(category, 'characteristics_sync_status', None),
        getattr(category, 'characteristics_sync_error', None),
        available=getattr(category, 'is_available', True),
        has_data=bool(category.characteristics_count),
    )
    if not status['usable']:
        return None, (
            f'Схема характеристик категории {subject_id} '
            'недоступна или устарела; запись остановлена'
        )

    _, _, error = _decode_agent_characteristics(raw_value)
    if error:
        return None, error

    schema_key = ('wb', int(subject_id))
    schema_cache = validation_cache.setdefault('schemas', {})
    resolved_schema = schema_cache.get(schema_key)
    if resolved_schema is None:
        allowed = MarketplaceCategoryCharacteristic.query.filter_by(
            category_id=category.id,
            is_enabled=True,
            is_available=True,
        ).all()
        resolved_schema = (
            validation_cache.get('agent_write_marketplace'),
            category,
            allowed,
        )
        schema_cache[schema_key] = resolved_schema
    else:
        allowed = resolved_schema[2]
    if not allowed:
        return None, (
            f'Для категории {subject_id} нет доступных '
            'включённых характеристик'
        )

    validation_value = raw_value
    if isinstance(validation_value, str):
        validation_value = json.loads(validation_value)
    if isinstance(validation_value, list):
        validation_value = [
            {
                **item,
                **(
                    {'id': item.get('charc_id')}
                    if item.get('id') is None and item.get('charc_id') is not None
                    else {}
                ),
            }
            for item in validation_value
        ]
    try:
        from services.marketplace_validator import (
            WBCharacteristicValidationError,
            build_wb_characteristic_patch,
        )
        normalized_patch = build_wb_characteristic_patch(
            subject_id, validation_value, validation_cache=validation_cache,
        )
    except WBCharacteristicValidationError as exc:
        issues = exc.result.get('issues') or []
        details = '; '.join(
            str(issue.get('message') or '') for issue in issues[:5]
        )
        return None, (
            'Значения характеристик не прошли проверку WB: '
            f'{details[:500]}'
        )
    allowed_ids = {item.charc_id for item in allowed}
    unavailable_ids = sorted(
        int(item['id']) for item in normalized_patch
        if int(item['id']) not in allowed_ids
    )
    if unavailable_ids:
        return None, (
            'Характеристики отсутствуют в текущей схеме WB '
            f'(ID: {", ".join(map(str, unavailable_ids[:10]))})'
        )
    return normalized_patch, None


def _decode_stored_agent_characteristics(raw_value):
    """Decode the current card state without silently dropping corrupt rows."""
    if raw_value in (None, ''):
        return [], None
    value = raw_value
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return None, (
                'Текущие характеристики карточки повреждены; '
                'обновление остановлено'
            )
    if isinstance(value, dict):
        return value, None
    if not isinstance(value, list) or any(
        not isinstance(item, dict) for item in value
    ):
        return None, (
            'Текущие характеристики карточки имеют недопустимый '
            'формат; обновление остановлено'
        )
    return value, None


def _merge_agent_characteristics(
    subject_id, stored_value, normalized_patch, validation_cache=None,
):
    existing, error = _decode_stored_agent_characteristics(stored_value)
    if error:
        return None, error
    from services.marketplace_validator import merge_wb_characteristics

    merged = merge_wb_characteristics(
        existing,
        normalized_patch,
        subject_id=subject_id,
        validation_cache=validation_cache,
    )
    return json.dumps(
        merged,
        ensure_ascii=False,
        separators=(',', ':'),
    ), None


def _validate_product_reference_update(product, data, validation_cache=None):
    subject_id = product.subject_id
    if 'wb_category_id' in data or 'wb_category_name' in data:
        if 'wb_category_id' in data:
            subject_id = _parse_subject_id(data.get('wb_category_id'))
        else:
            subject_id = _parse_subject_id(subject_id)
        if not subject_id:
            return 'Нельзя записать название категории без корректного wb_category_id'
        category, error = _wb_category_for_agent_write(
            subject_id, validation_cache,
        )
        if error:
            return error
        if not category.is_leaf:
            return f'Категория "{category.subject_name}" не является конечной'
        if not category.is_enabled:
            return f'Категория "{category.subject_name}" не включена в системе'
        if 'wb_category_id' in data:
            data['wb_category_id'] = subject_id
        data['wb_category_name'] = category.subject_name

    # Validate only an explicit brand patch. A category-only step must not be
    # blocked by the old raw brand before the following brand-normalization step.
    if 'brand' in data:
        canonical_brand, error = _validate_agent_brand_write(
            data['brand'], subject_id, validation_cache,
        )
        if error:
            return error
        data['brand'] = canonical_brand

    if 'characteristics' in data:
        if not subject_id:
            return 'Нельзя записать характеристики без категории WB'
        normalized_patch, error = _validate_agent_characteristics_write(
            subject_id, data['characteristics'], validation_cache,
        )
        if error:
            return error
        merged, error = _merge_agent_characteristics(
            subject_id,
            product.characteristics_json,
            normalized_patch,
            validation_cache,
        )
        if error:
            return error
        data['characteristics'] = merged
    return None


def _apply_product_fields(product: Product, data: dict) -> tuple[list, dict, dict]:
    """Apply only the established safe Product fields and return an audit diff."""
    changed_fields = []
    snapshot_before = {}
    snapshot_after = {}
    for incoming, model_field in _PRODUCT_FIELD_MAP.items():
        if incoming not in data:
            continue
        new_value = data[incoming]
        if model_field in {'characteristics_json', 'tags_json'} and not isinstance(new_value, str):
            new_value = json.dumps(new_value, ensure_ascii=False)
        old_value = getattr(product, model_field, None)
        if str(old_value) == str(new_value):
            continue
        changed_fields.append(model_field)
        snapshot_before[model_field] = old_value
        snapshot_after[model_field] = new_value
        setattr(product, model_field, new_value)
    return changed_fields, snapshot_before, snapshot_after


def _expected_timestamp_matches(product, raw_value) -> bool:
    """Compare an optional ISO timestamp against the currently stored version."""
    if raw_value in (None, ''):
        return True
    try:
        expected = datetime.fromisoformat(str(raw_value).strip().replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return False
    if expected.tzinfo is not None:
        expected = expected.astimezone(timezone.utc).replace(tzinfo=None)
    actual = product.updated_at
    if actual is None:
        return False
    if actual.tzinfo is not None:
        actual = actual.astimezone(timezone.utc).replace(tzinfo=None)
    return actual == expected


@internal_api_bp.route('/sellers/<int:seller_id>/products/batch', methods=['PATCH'])
@_authenticate_agent
def internal_batch_update_products(seller_id):
    """Update up to 50 main WB cards with per-card history and isolation."""
    task, error = _assigned_task_for_seller(seller_id)
    if error:
        return error
    data = request.get_json(silent=True) or {}
    updates = data.get('updates')
    if not isinstance(updates, list) or not updates:
        return jsonify({'error': 'updates array is required'}), 400
    if len(updates) > 50:
        return jsonify({'error': 'Maximum 50 updates per request'}), 400

    product_ids = []
    seen = set()
    for index, item in enumerate(updates):
        if not isinstance(item, dict):
            return jsonify({'error': f'updates[{index}] must be an object'}), 400
        raw_id = item.get('product_id')
        if isinstance(raw_id, bool):
            return jsonify({'error': f'updates[{index}].product_id must be a positive integer'}), 400
        if isinstance(raw_id, int):
            product_id = raw_id
        elif isinstance(raw_id, str) and re.fullmatch(r'[1-9]\d*', raw_id.strip()):
            product_id = int(raw_id)
        else:
            return jsonify({'error': f'updates[{index}].product_id must be a positive integer'}), 400
        if product_id <= 0:
            return jsonify({'error': f'updates[{index}].product_id must be a positive integer'}), 400
        if product_id in seen:
            return jsonify({'error': f'Duplicate product_id: {product_id}'}), 400
        product_ids.append(product_id)
        seen.add(product_id)

    products = Product.query.filter(
        Product.seller_id == seller_id,
        Product.id.in_(product_ids),
    ).all()
    products_by_id = {product.id: product for product in products}
    results = []
    updated_count = 0
    unchanged_count = 0
    failed_count = 0
    reference_validation_cache = {}
    _prime_agent_brand_write_cache([
        (
            item.get('brand'),
            item.get('wb_category_id', product.subject_id),
        )
        for item, product_id in zip(updates, product_ids)
        for product in [products_by_id.get(product_id)]
        if product is not None and 'brand' in item
    ], reference_validation_cache)

    for item, product_id in zip(updates, product_ids):
        product = products_by_id.get(product_id)
        if not product:
            results.append({
                'product_id': product_id, 'status': 'error',
                'error': 'Product not found',
            })
            failed_count += 1
            continue

        protected_fields = sorted(set(item) & _PRODUCT_PROTECTED_FIELDS)
        if protected_fields:
            results.append({
                'product_id': product_id, 'status': 'error',
                'error': 'Price and stock fields require manual review',
                'requires_manual_review': True,
                'protected_fields': protected_fields,
            })
            failed_count += 1
            continue

        unsupported_fields = sorted(
            set(item) - set(_PRODUCT_FIELD_MAP) - {'product_id', 'expected_updated_at'}
        )
        if unsupported_fields:
            results.append({
                'product_id': product_id, 'status': 'error',
                'error': 'Unsupported fields',
                'unsupported_fields': unsupported_fields,
            })
            failed_count += 1
            continue

        if not _expected_timestamp_matches(product, item.get('expected_updated_at')):
            results.append({
                'product_id': product_id, 'status': 'error',
                'error': 'Product changed after the content brief was loaded',
                'conflict': True,
                'updated_at': product.updated_at.isoformat() if product.updated_at else None,
            })
            failed_count += 1
            continue

        reference_error = _validate_product_reference_update(
            product, item, reference_validation_cache,
        )
        if reference_error:
            results.append({
                'product_id': product_id,
                'status': 'error',
                'error': reference_error,
                'reference_data_blocked': True,
            })
            failed_count += 1
            continue

        savepoint = db.session.begin_nested()
        try:
            changed_fields, snapshot_before, snapshot_after = _apply_product_fields(product, item)
            if not changed_fields:
                savepoint.commit()
                results.append({
                    'product_id': product_id, 'status': 'unchanged',
                    'changed_fields': [],
                    'updated_at': product.updated_at.isoformat() if product.updated_at else None,
                })
                unchanged_count += 1
                continue

            product.updated_at = datetime.utcnow()
            db.session.add(CardEditHistory(
                product_id=product.id,
                seller_id=seller_id,
                action='update',
                changed_fields=changed_fields,
                snapshot_before=snapshot_before,
                snapshot_after=snapshot_after,
                wb_synced=False,
                wb_sync_status='pending',
                user_comment=f'agent_task:{task.id}',
            ))
            db.session.flush()
            savepoint.commit()
            results.append({
                'product_id': product_id, 'status': 'updated',
                'changed_fields': changed_fields,
                'updated_at': product.updated_at.isoformat(),
            })
            updated_count += 1
        except Exception as exc:
            savepoint.rollback()
            results.append({
                'product_id': product_id, 'status': 'error',
                'error': str(exc)[:200],
            })
            failed_count += 1

    db.session.commit()
    return jsonify({
        'ok': failed_count == 0,
        'updated': updated_count,
        'unchanged': unchanged_count,
        'failed': failed_count,
        'results': results,
    })


@internal_api_bp.route('/sellers/<int:seller_id>/imported-products', methods=['GET'])
@_authenticate_agent
def internal_list_imported_products(seller_id):
    """Получить импортированные товары (от поставщика)."""
    _, error = _assigned_task_for_seller(seller_id)
    if error:
        return error
    page = request.args.get('page', 1, type=int)
    per_page = min(request.args.get('per_page', 50, type=int), 200)

    q = ImportedProduct.query.filter_by(seller_id=seller_id)
    total = q.count()
    products = q.offset((page - 1) * per_page).limit(per_page).all()

    return jsonify({
        'products': [_imported_product_to_dict(p) for p in products],
        'total': total,
        'page': page,
        'per_page': per_page,
    })


@internal_api_bp.route('/sellers/<int:seller_id>/imported-products/query', methods=['GET'])
@_authenticate_agent
def internal_query_imported_products(seller_id):
    """Compact deterministic catalog filters for simple read-only questions."""
    _, error = _assigned_task_for_seller(seller_id)
    if error:
        return error
    price_min = request.args.get('price_min', type=float)
    price_max = request.args.get('price_max', type=float)
    quantity_min = request.args.get('quantity_min', type=int)
    quantity_max = request.args.get('quantity_max', type=int)
    stock_state = (request.args.get('stock_state') or '').strip().lower()
    missing_field = (request.args.get('missing_field') or '').strip().lower()
    import_status = (request.args.get('import_status') or '').strip().lower()
    published = (request.args.get('published') or '').strip().lower()
    vendor_code = (request.args.get('vendor_code') or '').strip()[:100]
    allowed_missing = {
        'title', 'description', 'brand', 'category', 'photos',
        'characteristics', 'price', 'validation_errors',
    }
    if stock_state and stock_state not in {'in_stock', 'out_of_stock', 'missing'}:
        return jsonify({'error': 'invalid stock_state'}), 400
    if missing_field and missing_field not in allowed_missing:
        return jsonify({'error': 'invalid missing_field'}), 400
    if import_status and import_status not in {'pending', 'validated', 'imported', 'failed'}:
        return jsonify({'error': 'invalid import_status'}), 400
    if published and published not in {'yes', 'no'}:
        return jsonify({'error': 'published must be yes or no'}), 400
    for name, parsed in (
        ('price_min', price_min), ('price_max', price_max),
        ('quantity_min', quantity_min), ('quantity_max', quantity_max),
    ):
        if request.args.get(name) is not None and parsed is None:
            return jsonify({'error': f'{name} must be numeric'}), 400
    limit = min(max(request.args.get('limit', 100, type=int), 1), 200)
    query = ImportedProduct.query.filter_by(seller_id=seller_id)
    if price_min is not None:
        query = query.filter(ImportedProduct.calculated_price > price_min)
    if price_max is not None:
        query = query.filter(ImportedProduct.calculated_price < price_max)
    if quantity_min is not None:
        query = query.filter(ImportedProduct.supplier_quantity > quantity_min)
    if quantity_max is not None:
        query = query.filter(ImportedProduct.supplier_quantity < quantity_max)
    if stock_state == 'out_of_stock':
        query = query.filter(ImportedProduct.supplier_quantity <= 0)
    elif stock_state == 'in_stock':
        query = query.filter(ImportedProduct.supplier_quantity > 0)
    elif stock_state == 'missing':
        query = query.filter(ImportedProduct.supplier_quantity.is_(None))

    empty_text = lambda column: db.or_(
        column.is_(None), func.trim(column) == '', func.trim(column).in_(['[]', '{}', 'null']),
    )
    missing_filters = {
        'title': empty_text(ImportedProduct.title),
        'description': empty_text(ImportedProduct.description),
        'brand': empty_text(ImportedProduct.brand),
        'category': ImportedProduct.wb_subject_id.is_(None),
        'photos': empty_text(ImportedProduct.photo_urls),
        'characteristics': empty_text(ImportedProduct.characteristics),
        'price': ImportedProduct.calculated_price.is_(None),
        'validation_errors': ~empty_text(ImportedProduct.validation_errors),
    }
    if missing_field in missing_filters:
        query = query.filter(missing_filters[missing_field])
    if import_status in {'pending', 'validated', 'imported', 'failed'}:
        query = query.filter(ImportedProduct.import_status == import_status)
    if published == 'yes':
        query = query.filter(ImportedProduct.product_id.is_not(None))
    elif published == 'no':
        query = query.filter(ImportedProduct.product_id.is_(None))
    if vendor_code:
        query = query.filter(db.or_(
            ImportedProduct.external_vendor_code == vendor_code,
            ImportedProduct.external_id == vendor_code,
        ))
    rows = query.options(load_only(
        ImportedProduct.id, ImportedProduct.title, ImportedProduct.external_vendor_code,
        ImportedProduct.calculated_price, ImportedProduct.supplier_price,
        ImportedProduct.supplier_quantity, ImportedProduct.supplier_id,
        ImportedProduct.import_status, ImportedProduct.product_id,
    )).add_columns(
        func.count(ImportedProduct.id).over().label('matched_total'),
    ).order_by(ImportedProduct.calculated_price.desc(), ImportedProduct.id).limit(limit).all()
    products = [row[0] for row in rows]
    total = int(rows[0].matched_total) if rows else 0
    return jsonify({
        'total': total,
        'products': [{
            'id': product.id,
            'title': html.unescape(product.title or '')[:180],
            'vendor_code': product.external_vendor_code,
            'price': product.calculated_price,
            'supplier_price': product.supplier_price,
            'supplier_quantity': product.supplier_quantity,
            'supplier_id': product.supplier_id,
            'import_status': product.import_status,
            'published': product.product_id is not None,
        } for product in products],
        'truncated': total > len(products),
    })


@internal_api_bp.route('/imported-products/<int:product_id>', methods=['GET'])
@_authenticate_agent
def internal_get_imported_product(product_id):
    """Получить одну импортированную запись по ID."""
    task, error = _assigned_task_for_seller()
    if error:
        return error
    p = ImportedProduct.query.filter_by(
        id=product_id, seller_id=task.seller_id,
    ).first()
    if not p:
        return jsonify({'error': 'Imported product not found'}), 404
    return jsonify({'product': _imported_product_to_dict(p)})


@internal_api_bp.route('/imported-products/brief', methods=['POST'])
@_authenticate_agent
def internal_get_imported_products_brief():
    """Пакетное получение краткой информации о товарах (экономия токенов).

    Возвращает только id, title, brand, category — минимум для маппинга.
    Максимум 50 товаров за раз.

    Body: { "product_ids": [1, 2, 3] }
    """
    task, error = _assigned_task_for_seller()
    if error:
        return error
    data = request.get_json(silent=True) or {}
    product_ids = data.get('product_ids', [])

    if not product_ids or not isinstance(product_ids, list):
        return jsonify({'error': 'product_ids array is required'}), 400

    product_ids = product_ids[:50]

    products = ImportedProduct.query.filter(
        ImportedProduct.id.in_(product_ids),
        ImportedProduct.seller_id == task.seller_id,
    ).all()

    # Опционально расширенные поля (для агента характеристик)
    include_description = data.get('include_description', False)

    return jsonify({
        'products': [
            {
                'id': p.id,
                'title': p.title or '',
                'brand': p.brand or '',
                'category': p.category or '',
                'mapped_wb_category': p.mapped_wb_category or '',
                'wb_subject_id': p.wb_subject_id,
                **(
                    {
                        'description': (p.description or '')[:500],
                        'country': p.country or '',
                    } if include_description else {}
                ),
            }
            for p in products
        ],
        'count': len(products),
    })


_IMPORTED_PRODUCT_ALLOWED_FIELDS = [
    'title', 'description', 'brand', 'mapped_wb_category',
    'wb_subject_id', 'category_confidence',
    'ai_seo_title', 'ai_keywords', 'ai_bullets',
    'characteristics', 'sizes', 'gender', 'country',
]

_AGENT_PROTECTED_FIELDS = frozenset({
    'calculated_price', 'calculated_discount_price',
    'calculated_price_before_discount', 'supplier_price',
    'supplier_quantity', 'stock', 'stocks', 'quantity', 'amount',
})


def _stage_protected_agent_changes(product, data: dict, task_id: str):
    """Remove price/stock writes and persist an idempotent review proposal."""
    proposed = {}
    for field in list(data):
        if field not in _AGENT_PROTECTED_FIELDS:
            continue
        new_value = data.pop(field)
        old_value = getattr(product, field, None)
        if str(old_value) != str(new_value):
            proposed[field] = {'old': old_value, 'new': new_value}
    if not proposed:
        return None

    proposal = AgentReviewProposal.query.filter_by(
        task_id=task_id,
        imported_product_id=product.id,
        proposal_type='price_or_stock',
        status='pending',
    ).first()
    if proposal:
        existing = proposal.get_changes()
        existing.update(proposed)
        proposal.changes_json = json.dumps(existing, ensure_ascii=False)
    else:
        proposal = AgentReviewProposal(
            task_id=task_id,
            seller_id=product.seller_id,
            imported_product_id=product.id,
            proposal_type='price_or_stock',
            changes_json=json.dumps(proposed, ensure_ascii=False),
            reason=(
                'Цены и остатки защищены: AI может только предложить значения, '
                'а применить их может пользователь после ручной проверки.'
            ),
        )
        db.session.add(proposal)
        db.session.flush()
    return proposal


def _validate_and_apply_imported_product_update(
    product, data: dict, task_id: str = None, agent_id: str = None,
    validation_cache=None,
) -> tuple:
    """Валидирует и применяет обновление импортированного товара.

    Возвращает (True, None) при успехе или (False, error_string) при ошибке.
    НЕ вызывает db.session.commit() — вызывающий код решает когда коммитить.
    """
    # ── Нормализация алиасов полей ──
    if 'wb_category_id' in data:
        data['wb_subject_id'] = data.pop('wb_category_id')
    if 'wb_category_name' in data:
        data['mapped_wb_category'] = data.pop('wb_category_name')

    # ── Валидация категории ──
    if 'wb_subject_id' in data or 'mapped_wb_category' in data:
        raw_subject_id = data.get('wb_subject_id', product.wb_subject_id)
        subject_id = _parse_subject_id(raw_subject_id)
        if not subject_id:
            return False, 'Нельзя записать название категории без корректного wb_subject_id'
        if 'wb_subject_id' in data:
            data['wb_subject_id'] = subject_id
        cat, reference_error = _wb_category_for_agent_write(
            subject_id, validation_cache,
        )
        if reference_error:
            return False, reference_error
        if not cat.is_leaf:
            return False, (
                f'Категория "{cat.subject_name}" (id={subject_id}) не является конечной (leaf)'
            )
        if not cat.is_enabled:
            return False, (
                f'Категория "{cat.subject_name}" (id={subject_id}) не включена в системе'
            )

        confidence = data.get('category_confidence')
        if confidence is not None:
            try:
                confidence = float(confidence)
            except (ValueError, TypeError):
                confidence = None
            if confidence is not None and confidence < 0.5:
                return False, (
                    f'Уверенность в категории слишком низкая ({confidence}). Минимум 0.5.'
                )

        data['mapped_wb_category'] = cat.subject_name

    if 'brand' in data:
        target_subject_id = _parse_subject_id(
            data.get('wb_subject_id', product.wb_subject_id),
        )
        canonical_brand, reference_error = _validate_agent_brand_write(
            data['brand'], target_subject_id, validation_cache,
        )
        if reference_error:
            return False, reference_error
        data['brand'] = canonical_brand

    if 'characteristics' in data:
        target_subject_id = _parse_subject_id(
            data.get('wb_subject_id', product.wb_subject_id),
        )
        if not target_subject_id:
            return False, 'Нельзя записать характеристики без категории WB'
        normalized_patch, reference_error = (
            _validate_agent_characteristics_write(
                target_subject_id,
                data['characteristics'],
                validation_cache,
            )
        )
        if reference_error:
            return False, reference_error
        merged, reference_error = _merge_agent_characteristics(
            target_subject_id,
            product.characteristics,
            normalized_patch,
            validation_cache,
        )
        if reference_error:
            return False, reference_error
        data['characteristics'] = merged

    # ── Снимок предыдущих значений для отката ──
    previous_values = {}
    new_values = {}
    for field in _IMPORTED_PRODUCT_ALLOWED_FIELDS:
        if field in data:
            old_val = getattr(product, field, None)
            new_val = data[field]
            if str(old_val) != str(new_val):
                previous_values[field] = old_val
                new_values[field] = new_val

    # ── Применяем изменения ──
    for field in _IMPORTED_PRODUCT_ALLOWED_FIELDS:
        if field in data:
            setattr(product, field, data[field])

    product.updated_at = datetime.utcnow()

    # Снимок если были реальные изменения
    if previous_values:
        snapshot = AgentChangeSnapshot(
            task_id=task_id,
            imported_product_id=product.id,
            agent_id=agent_id,
            previous_values=json.dumps(previous_values, ensure_ascii=False, default=str),
            new_values=json.dumps(new_values, ensure_ascii=False, default=str),
        )
        db.session.add(snapshot)

    return True, None


def _is_reference_data_write_error(error):
    text = str(error or '').lower()
    return any(marker in text for marker in (
        'справочник категорий',
        'схема характеристик',
        'больше недоступна в wb',
        'не является конечной',
        'не включена в системе',
        'текущей схеме wb',
        'значения характеристик',
        'доступных включённых характеристик',
        'пустой patch характеристик',
        'текущие характеристики карточки',
        'характеристики без категории wb',
        'название категории без',
        'справочник брендов wb',
        'бренд без категории wb',
        'не подтверждён в категории wb',
    ))


@internal_api_bp.route('/imported-products/<int:product_id>', methods=['PATCH'])
@_authenticate_agent
def internal_update_imported_product(product_id):
    """Агент обновляет данные импортированного товара."""
    task, error = _assigned_task_for_seller()
    if error:
        return error
    p = ImportedProduct.query.filter_by(
        id=product_id, seller_id=task.seller_id,
    ).first()
    if not p:
        return jsonify({'error': 'Imported product not found'}), 404

    data = request.get_json(silent=True) or {}
    task_id = request.headers.get('X-Task-Id')
    agent_id = request._agent.id if hasattr(request, '_agent') else None
    proposal = _stage_protected_agent_changes(p, data, task_id)

    ok, error = _validate_and_apply_imported_product_update(p, data, task_id, agent_id)
    if not ok:
        reference_blocked = _is_reference_data_write_error(error)
        return jsonify({
            'error': error,
            **({'reference_data_blocked': True} if reference_blocked else {}),
        }), 409 if reference_blocked else 400

    db.session.commit()
    return jsonify({
        'ok': True,
        'product': _imported_product_to_dict(p),
        'requires_manual_review': bool(proposal),
        'proposal': proposal.to_dict() if proposal else None,
    })


@internal_api_bp.route('/imported-products/batch', methods=['PATCH'])
@_authenticate_agent
def internal_batch_update_imported_products():
    """Пакетное обновление импортированных товаров (до 50 за запрос).

    Каждый товар обрабатывается независимо — ошибка одного не блокирует остальные.
    Используется агентами для массового сохранения результатов batch-обработки.
    """
    task, error = _assigned_task_for_seller()
    if error:
        return error
    data = request.get_json(silent=True) or {}
    raw_updates = data.get('updates')

    if not isinstance(raw_updates, list) or not raw_updates:
        return jsonify({'error': 'updates array is required'}), 400
    if len(raw_updates) > 50:
        return jsonify({'error': 'Maximum 50 updates per request'}), 400

    updates = []
    product_ids = []
    seen_product_ids = set()
    for index, item in enumerate(raw_updates):
        if not isinstance(item, dict):
            return jsonify({
                'error': f'updates[{index}] must be an object',
            }), 400
        try:
            product_id = parse_positive_integer(
                item.get('product_id'),
                f'updates[{index}].product_id',
            )
        except ValueError as exc:
            return jsonify({'error': str(exc)}), 400
        if product_id in seen_product_ids:
            return jsonify({
                'error': f'Duplicate product_id: {product_id}',
            }), 400
        seen_product_ids.add(product_id)
        product_ids.append(product_id)
        updates.append({**item, 'product_id': product_id})

    task_id = request.headers.get('X-Task-Id')
    agent_id = request._agent.id if hasattr(request, '_agent') else None

    # Предзагрузка всех товаров одним запросом
    products_map = {
        p.id: p
        for p in ImportedProduct.query.filter(
            ImportedProduct.id.in_(product_ids),
            ImportedProduct.seller_id == task.seller_id,
        ).all()
    } if product_ids else {}

    results = []
    updated_count = 0
    failed_count = 0
    reference_validation_cache = {}
    _prime_agent_brand_write_cache([
        (
            item.get('brand'),
            (
                item.get('wb_category_id')
                if 'wb_category_id' in item
                else item.get('wb_subject_id', product.wb_subject_id)
            ),
        )
        for item in updates
        if isinstance(item, dict)
        for product in [products_map.get(item.get('product_id'))]
        if product is not None and 'brand' in item
    ], reference_validation_cache)

    for item in updates:
        pid = item.get('product_id')
        if not pid:
            results.append({'product_id': pid, 'status': 'error', 'error': 'product_id is required'})
            failed_count += 1
            continue

        product = products_map.get(pid)
        if not product:
            results.append({'product_id': pid, 'status': 'error', 'error': 'Product not found'})
            failed_count += 1
            continue

        if not _expected_timestamp_matches(product, item.get('expected_updated_at')):
            results.append({
                'product_id': pid, 'status': 'error',
                'error': 'Product changed after the content brief was loaded',
                'conflict': True,
                'updated_at': product.updated_at.isoformat() if product.updated_at else None,
            })
            failed_count += 1
            continue

        # expected_updated_at is concurrency metadata, never a writable field.
        update_data = {
            k: v for k, v in item.items()
            if k not in {'product_id', 'expected_updated_at'}
        }
        proposal = _stage_protected_agent_changes(product, update_data, task_id)

        # Savepoint для изоляции ошибок отдельных товаров
        savepoint = db.session.begin_nested()
        try:
            ok, error = _validate_and_apply_imported_product_update(
                product, update_data, task_id, agent_id,
                reference_validation_cache,
            )
            if ok:
                savepoint.commit()
                results.append({
                    'product_id': pid,
                    'status': 'review_required' if proposal else 'updated',
                    'proposal_id': proposal.id if proposal else None,
                })
                updated_count += 1
            else:
                savepoint.rollback()
                reference_blocked = _is_reference_data_write_error(error)
                results.append({
                    'product_id': pid,
                    'status': 'error',
                    'error': error,
                    **(
                        {'reference_data_blocked': True}
                        if reference_blocked else {}
                    ),
                })
                failed_count += 1
        except Exception as e:
            savepoint.rollback()
            results.append({'product_id': pid, 'status': 'error', 'error': str(e)[:200]})
            failed_count += 1

    db.session.commit()

    return jsonify({
        'ok': True,
        'updated': updated_count,
        'failed': failed_count,
        'results': results,
    })


# ── Задачи: создание подзадач (для оркестратора) ───────────────

@internal_api_bp.route('/tasks/create', methods=['POST'])
@_authenticate_agent
def internal_create_task():
    """Агент-оркестратор создаёт подзадачу для другого агента."""
    data = request.get_json(silent=True) or {}

    parent_task_id = data.get('parent_task_id')
    if not parent_task_id:
        return jsonify({'error': 'parent_task_id is required'}), 400
    if request.headers.get('X-Task-Id') != parent_task_id:
        return jsonify({'error': 'X-Task-Id must match parent_task_id'}), 403
    parent_task, error = _owned_task_or_404(parent_task_id)
    if error:
        return error
    if parent_task.status != 'running':
        return jsonify({'error': 'Parent task is not running'}), 409

    agent_name = data.get('agent_name')
    if not agent_name:
        return jsonify({'error': 'agent_name is required'}), 400

    target_agent = ServiceAgent.query.filter_by(name=agent_name).first()
    if not target_agent:
        return jsonify({'error': f'Agent "{agent_name}" not found'}), 404

    seller_id = data.get('seller_id')
    if not seller_id:
        return jsonify({'error': 'seller_id is required'}), 400
    try:
        seller_id = int(seller_id)
    except (TypeError, ValueError):
        return jsonify({'error': 'seller_id must be an integer'}), 400
    if seller_id != int(parent_task.seller_id):
        return jsonify({'error': 'Subtask seller must match parent task'}), 403
    input_data = data.get('input_data') or {}
    if not isinstance(input_data, dict):
        return jsonify({'error': 'input_data must be an object'}), 400
    input_seller_id = input_data.get('seller_id')
    if input_seller_id:
        try:
            input_seller_id = int(input_seller_id)
        except (TypeError, ValueError):
            return jsonify({'error': 'input_data seller_id must be an integer'}), 400
        if input_seller_id != int(parent_task.seller_id):
            return jsonify({'error': 'input_data seller must match parent task'}), 403

    task = agent_service.create_task(
        agent_id=target_agent.id,
        seller_id=seller_id,
        task_type=data.get('task_type', 'unknown'),
        title=data.get('title', f'Подзадача: {agent_name}'),
        input_data=input_data,
        priority=data.get('priority', 0),
        parent_task_id=parent_task_id,
    )
    return jsonify({'ok': True, 'task': task.to_dict()})


@internal_api_bp.route('/tasks/<task_id>', methods=['GET'])
@_authenticate_agent
def internal_get_task(task_id):
    """Получить статус задачи (для оркестратора).

    Обрезает input_data чтобы не забивать контекст оркестратора
    (может содержать 10k+ product_ids).
    """
    task, error = _owned_task_or_404(task_id)
    if error:
        return error
    d = task.to_dict()
    # Обрезаем input_data — оркестратору нужен только status/result
    raw = d.get('input_data', '{}')
    if len(raw) > 500:
        d['input_data'] = raw[:500] + '...(truncated)'
    return jsonify({'task': d})


@internal_api_bp.route(
    '/tasks/<parent_task_id>/subtasks/<task_id>', methods=['GET'],
)
@_authenticate_agent
def internal_get_subtask(parent_task_id, task_id):
    """Parent agent может читать только явно связанную дочернюю задачу."""
    parent, error = _owned_task_or_404(parent_task_id)
    if error:
        return error
    task = AgentTask.query.filter_by(
        id=task_id,
        parent_task_id=parent.id,
        seller_id=parent.seller_id,
    ).first()
    if not task:
        return jsonify({'error': 'Subtask not found'}), 404
    d = task.to_dict()
    raw = d.get('input_data', '{}')
    if len(raw) > 500:
        d['input_data'] = raw[:500] + '...(truncated)'
    return jsonify({'task': d})


# ── Данные: продавцы ────────────────────────────────────────────

@internal_api_bp.route('/sellers/<int:seller_id>', methods=['GET'])
@_authenticate_agent
def internal_get_seller(seller_id):
    """Информация о продавце."""
    _, error = _assigned_task_for_seller(seller_id)
    if error:
        return error
    seller = db.session.get(Seller, seller_id)
    if not seller:
        return jsonify({'error': 'Seller not found'}), 404
    return jsonify({
        'seller': {
            'id': seller.id,
            'company_name': seller.company_name,
            'wb_seller_id': seller.wb_seller_id,
            'has_api_key': bool(seller._wb_api_key_encrypted),
        }
    })


@internal_api_bp.route('/sellers/<int:seller_id>/suppliers/resolve', methods=['GET'])
@_authenticate_agent
def internal_resolve_supplier(seller_id):
    """Resolve a seller-connected supplier by a human name or code."""
    _, error = _assigned_task_for_seller(seller_id)
    if error:
        return error
    query = (request.args.get('q') or '').strip().lower()
    if len(query) < 2:
        return jsonify({'error': 'q must contain at least 2 characters'}), 400

    suppliers = Supplier.query.join(
        SellerSupplier, SellerSupplier.supplier_id == Supplier.id,
    ).filter(
        SellerSupplier.seller_id == seller_id,
        SellerSupplier.is_active.is_(True),
        Supplier.is_active.is_(True),
    ).all()
    matches = []
    for supplier in suppliers:
        haystacks = [supplier.name.lower(), supplier.code.lower()]
        score = max(SequenceMatcher(None, query, value).ratio() for value in haystacks)
        if any(query in value or value in query for value in haystacks):
            score = max(score, 0.95)
        elif any(query[:5] and query[:5] in value for value in haystacks):
            score = max(score, 0.78)
        if score >= 0.45:
            matches.append({
                'id': supplier.id, 'name': supplier.name, 'code': supplier.code,
                'score': round(score, 3),
            })
    matches.sort(key=lambda item: item['score'], reverse=True)
    return jsonify({'suppliers': matches[:10], 'count': len(matches)})


def _json_list_length(value) -> int:
    if not value:
        return 0
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
        return len(parsed) if isinstance(parsed, list) else 0
    except (TypeError, ValueError):
        return 0


def _bounded_product_ids(value, limit: int = 200) -> list[int]:
    """Validate a bounded typed selection without dropping or deduplicating IDs."""
    if not isinstance(value, list) or not value:
        raise ValueError('product_ids array is required')
    if len(value) > limit:
        raise ValueError(f'Maximum {limit} product_ids per request')
    result = []
    seen = set()
    for index, raw in enumerate(value):
        if isinstance(raw, bool):
            raise ValueError(f'product_ids[{index}] must be a positive integer')
        if isinstance(raw, int):
            product_id = raw
        elif isinstance(raw, str) and re.fullmatch(r'[1-9]\d*', raw.strip()):
            product_id = int(raw)
        else:
            raise ValueError(f'product_ids[{index}] must be a positive integer')
        if product_id <= 0:
            raise ValueError(f'product_ids[{index}] must be a positive integer')
        if product_id in seen:
            raise ValueError(f'Duplicate product_id: {product_id}')
        result.append(product_id)
        seen.add(product_id)
    return result


def _empty_serialized(value) -> bool:
    return not str(value or '').strip() or str(value).strip() in {'{}', '[]', 'null'}


def _selected_batch_audit(products: list, entity_kind: str, focus_limit: int) -> dict:
    if entity_kind == 'product':
        definitions = (
            ('missing_title', 'Нет названия', 10, lambda p: not (p.title or '').strip()),
            ('long_title', 'Название длиннее 60 символов', 4,
             lambda p: len((p.title or '').strip()) > 60),
            ('missing_category', 'Не определена категория WB', 10, lambda p: not p.subject_id),
            ('missing_brand', 'Не указан бренд', 5, lambda p: not (p.brand or '').strip()),
            ('missing_description', 'Нет описания', 8, lambda p: not (p.description or '').strip()),
            ('short_description', 'Слишком короткое описание', 4,
             lambda p: bool((p.description or '').strip()) and len(p.description.strip()) < 100),
            ('missing_photos', 'Нет фотографий', 10,
             lambda p: _json_list_length(p.photos_json) == 0),
            ('missing_characteristics', 'Не заполнены характеристики', 7,
             lambda p: _empty_serialized(p.characteristics_json)),
            ('missing_price', 'Нет цены', 7,
             lambda p: p.price is None and p.discount_price is None),
            ('out_of_stock', 'Нет остатка', 5, lambda p: int(p.quantity or 0) <= 0),
            ('low_quality', 'Quality Score ниже 50', 5,
             lambda p: p.quality_score is not None and float(p.quality_score) < 50),
            ('inactive', 'Карточка неактивна', 5, lambda p: not bool(p.is_active)),
        )
    else:
        definitions = (
            ('missing_title', 'Нет названия', 10, lambda p: not (p.title or '').strip()),
            ('long_title', 'Название длиннее 60 символов', 4,
             lambda p: len((p.title or '').strip()) > 60),
            ('missing_category', 'Не определена категория WB', 10, lambda p: not p.wb_subject_id),
            ('low_category_confidence', 'Низкая уверенность в категории', 6,
             lambda p: bool(p.wb_subject_id) and float(p.category_confidence or 0) < 0.75),
            ('missing_brand', 'Не указан бренд', 5, lambda p: not (p.brand or '').strip()),
            ('missing_description', 'Нет описания', 8, lambda p: not (p.description or '').strip()),
            ('short_description', 'Слишком короткое описание', 4,
             lambda p: bool((p.description or '').strip()) and len(p.description.strip()) < 100),
            ('missing_photos', 'Нет фотографий', 10,
             lambda p: max(_json_list_length(p.processed_photos), _json_list_length(p.photo_urls)) == 0),
            ('missing_characteristics', 'Не заполнены характеристики', 7,
             lambda p: _empty_serialized(p.characteristics)),
            ('missing_price', 'Нет закупочной цены', 7, lambda p: p.supplier_price is None),
            ('out_of_stock', 'Нет остатка у поставщика', 5,
             lambda p: p.supplier_quantity is not None and p.supplier_quantity <= 0),
            ('validation_errors', 'Есть ошибки валидации', 9,
             lambda p: _json_list_length(p.validation_errors) > 0),
        )

    issue_data = {
        code: {'code': code, 'label': label, 'count': 0, 'examples': []}
        for code, label, _, _ in definitions
    }
    ranked = []
    for product in products:
        score = 0
        codes = []
        labels = []
        for code, label, weight, predicate in definitions:
            if not predicate(product):
                continue
            score += weight
            codes.append(code)
            labels.append(label)
            issue_data[code]['count'] += 1
            if len(issue_data[code]['examples']) < 5:
                issue_data[code]['examples'].append(product.id)
        if codes:
            ranked.append({
                'id': product.id,
                'title': html.unescape(product.title or '')[:180],
                'risk_score': score,
                'issue_codes': codes,
                'issue_labels': labels[:5],
            })

    total = len(products)
    issues = []
    for item in issue_data.values():
        if not item['count']:
            continue
        item['percent'] = round(item['count'] * 100 / total, 1) if total else 0
        issues.append(item)
    issues.sort(key=lambda item: (item['count'], item['code']), reverse=True)
    ranked.sort(key=lambda item: (item['risk_score'], item['id']), reverse=True)
    return {
        'total': total,
        'cards_with_issues': len(ranked),
        'issue_summary': issues,
        'products': ranked[:focus_limit],
        'truncated': len(ranked) > focus_limit,
    }


@internal_api_bp.route('/sellers/<int:seller_id>/products/content-brief', methods=['POST'])
@_authenticate_agent
def internal_products_content_brief(seller_id):
    """Fetch a typed content projection for up to 200 selected cards in one query."""
    _, error = _assigned_task_for_seller(seller_id)
    if error:
        return error
    data = request.get_json(silent=True) or {}
    entity_kind = str(data.get('entity_kind') or '').strip().lower()
    if entity_kind not in {'product', 'imported_product'}:
        return jsonify({'error': 'entity_kind must be product or imported_product'}), 400
    try:
        product_ids = _bounded_product_ids(data.get('product_ids'))
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400

    model = Product if entity_kind == 'product' else ImportedProduct
    products = model.query.filter(
        model.seller_id == seller_id,
        model.id.in_(product_ids),
    ).all()
    if len(products) != len(product_ids):
        return jsonify({
            'error': 'Some selected products are unavailable in this seller scope',
            'unavailable_count': len(product_ids) - len(products),
        }), 409
    by_id = {product.id: product for product in products}
    result = []
    for product_id in product_ids:
        product = by_id[product_id]
        category = (
            product.object_name if entity_kind == 'product'
            else (product.mapped_wb_category or product.category)
        )
        result.append({
            'id': product.id,
            'title': html.unescape(product.title or '')[:180],
            'description': html.unescape(product.description or '')[:1200],
            'brand': html.unescape(product.brand or '')[:200],
            'category': html.unescape(category or '')[:200],
            'updated_at': product.updated_at.isoformat() if product.updated_at else None,
        })
    return jsonify({
        'entity_kind': entity_kind,
        'products': result,
        'count': len(result),
    })


@internal_api_bp.route('/sellers/<int:seller_id>/products/audit-batch', methods=['POST'])
@_authenticate_agent
def internal_products_audit_batch(seller_id):
    """Audit an explicit typed selection in one tenant-scoped database query."""
    _, error = _assigned_task_for_seller(seller_id)
    if error:
        return error
    data = request.get_json(silent=True) or {}
    entity_kind = str(data.get('entity_kind') or '').strip().lower()
    if entity_kind not in {'product', 'imported_product'}:
        return jsonify({'error': 'entity_kind must be product or imported_product'}), 400
    try:
        product_ids = _bounded_product_ids(data.get('product_ids'))
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    try:
        focus_limit = min(max(int(data.get('focus_limit') or 100), 1), 200)
    except (TypeError, ValueError):
        return jsonify({'error': 'focus_limit must be an integer'}), 400

    model = Product if entity_kind == 'product' else ImportedProduct
    products = model.query.filter(
        model.seller_id == seller_id,
        model.id.in_(product_ids),
    ).all()
    if len(products) != len(product_ids):
        return jsonify({
            'error': 'Some selected products are unavailable in this seller scope',
            'unavailable_count': len(product_ids) - len(products),
        }), 409
    result = _selected_batch_audit(products, entity_kind, focus_limit)
    return jsonify({'entity_kind': entity_kind, **result})


@internal_api_bp.route('/sellers/<int:seller_id>/suppliers/<int:supplier_id>/ready-candidates', methods=['GET'])
@_authenticate_agent
def internal_ready_supplier_candidates(seller_id, supplier_id):
    """Return a compact, pre-ranked pool of unpublished upload-ready cards."""
    _, error = _assigned_task_for_seller(seller_id)
    if error:
        return error
    connection = SellerSupplier.query.filter_by(
        seller_id=seller_id, supplier_id=supplier_id, is_active=True,
    ).first()
    if not connection:
        return jsonify({'error': 'Supplier is not connected to this seller'}), 404

    limit = min(max(request.args.get('limit', 60, type=int), 1), 100)
    source = ImportedProduct.query.filter_by(
        seller_id=seller_id, supplier_id=supplier_id,
    ).filter(
        ImportedProduct.product_id.is_(None),
        db.or_(ImportedProduct.supplier_quantity.is_(None), ImportedProduct.supplier_quantity > 0),
    ).order_by(ImportedProduct.updated_at.desc()).limit(500).all()

    from services.upload_readiness_validator import validate_product_upload_readiness
    seller = db.session.get(Seller, seller_id)
    candidates = []
    for product in source:
        readiness = validate_product_upload_readiness(product, seller)
        if not readiness.get('is_ready'):
            continue
        photo_count = max(
            _json_list_length(product.processed_photos),
            _json_list_length(product.photo_urls),
        )
        description_len = len(product.description or '')
        baseline = 50
        baseline += min(photo_count, 6) * 5
        baseline += 8 if description_len >= 300 else 4 if description_len >= 100 else 0
        baseline += 6 if product.brand else 0
        baseline += 6 if product.category_confidence and product.category_confidence >= 0.8 else 0
        baseline += min(max(product.supplier_quantity or 0, 0), 20) / 4
        baseline -= min(readiness.get('warnings_count', 0) * 2, 10)
        candidates.append({
            'id': product.id,
            'title': (product.title or '')[:180],
            'category': product.mapped_wb_category or product.category,
            'brand': product.brand,
            'photo_count': photo_count,
            'description_length': description_len,
            'supplier_price': product.supplier_price,
            'supplier_quantity': product.supplier_quantity,
            'category_confidence': product.category_confidence,
            'warnings_count': readiness.get('warnings_count', 0),
            'baseline_score': round(baseline, 1),
        })
    candidates.sort(key=lambda item: (item['baseline_score'], item['id']), reverse=True)
    return jsonify({
        'supplier_id': supplier_id,
        'ready_total': len(candidates),
        'candidates': candidates[:limit],
    })


@internal_api_bp.route('/sellers/<int:seller_id>/suppliers/<int:supplier_id>/imported-audit', methods=['GET'])
@_authenticate_agent
def internal_supplier_imported_audit(seller_id, supplier_id):
    """Aggregate a supplier catalog without sending the full catalog to an LLM."""
    _, error = _assigned_task_for_seller(seller_id)
    if error:
        return error
    connection = SellerSupplier.query.filter_by(
        seller_id=seller_id, supplier_id=supplier_id, is_active=True,
    ).first()
    supplier = Supplier.query.filter_by(id=supplier_id, is_active=True).first()
    if not connection or not supplier:
        return jsonify({'error': 'Supplier is not connected to this seller'}), 404

    focus_limit = min(max(request.args.get('focus_limit', 100, type=int), 1), 200)
    products = ImportedProduct.query.filter_by(
        seller_id=seller_id, supplier_id=supplier_id,
    ).options(load_only(
        ImportedProduct.id, ImportedProduct.product_id, ImportedProduct.title,
        ImportedProduct.wb_subject_id, ImportedProduct.category_confidence,
        ImportedProduct.brand, ImportedProduct.description,
        ImportedProduct.processed_photos, ImportedProduct.photo_urls,
        ImportedProduct.characteristics, ImportedProduct.supplier_price,
        ImportedProduct.supplier_quantity, ImportedProduct.validation_errors,
        ImportedProduct.updated_at,
    )).order_by(ImportedProduct.updated_at.desc()).all()
    total = len(products)

    definitions = (
        ('missing_title', 'Нет названия', 10, lambda p: not (p.title or '').strip()),
        ('missing_category', 'Не определена категория WB', 10, lambda p: not p.wb_subject_id),
        ('low_category_confidence', 'Низкая уверенность в категории', 6,
         lambda p: bool(p.wb_subject_id) and float(p.category_confidence or 0) < 0.75),
        ('missing_brand', 'Не указан бренд', 5, lambda p: not (p.brand or '').strip()),
        ('missing_description', 'Нет описания', 8, lambda p: not (p.description or '').strip()),
        ('short_description', 'Слишком короткое описание', 4,
         lambda p: bool((p.description or '').strip()) and len(p.description.strip()) < 100),
        ('missing_photos', 'Нет фотографий', 10,
         lambda p: max(_json_list_length(p.processed_photos), _json_list_length(p.photo_urls)) == 0),
        ('missing_characteristics', 'Не заполнены характеристики', 7,
         lambda p: not (p.characteristics or '').strip() or p.characteristics.strip() in {'{}', '[]'}),
        ('missing_price', 'Нет закупочной цены', 7, lambda p: p.supplier_price is None),
        ('out_of_stock', 'Нет остатка у поставщика', 5,
         lambda p: p.supplier_quantity is not None and p.supplier_quantity <= 0),
        ('validation_errors', 'Есть ошибки валидации', 9,
         lambda p: _json_list_length(p.validation_errors) > 0),
    )
    issue_data = {
        code: {'code': code, 'label': label, 'count': 0, 'examples': []}
        for code, label, _, _ in definitions
    }
    ranked = []
    published = 0
    for product in products:
        published += int(product.product_id is not None)
        score = 0
        codes = []
        for code, _, weight, predicate in definitions:
            if predicate(product):
                score += weight
                codes.append(code)
                issue_data[code]['count'] += 1
                if len(issue_data[code]['examples']) < 5:
                    issue_data[code]['examples'].append(product.id)
        if codes:
            ranked.append((score, product.id, codes))

    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    issues = []
    for item in issue_data.values():
        if not item['count']:
            continue
        item['percent'] = round(item['count'] * 100 / total, 1) if total else 0
        issues.append(item)
    issues.sort(key=lambda item: (item['count'], item['code']), reverse=True)

    return jsonify({
        'supplier': {'id': supplier.id, 'name': supplier.name, 'code': supplier.code},
        'total': total,
        'published': published,
        'unpublished': total - published,
        'cards_with_issues': len(ranked),
        'issue_summary': issues,
        'focus_product_ids': [item[1] for item in ranked[:focus_limit]],
        'focus': [
            {'product_id': product_id, 'risk_score': score, 'issue_codes': codes}
            for score, product_id, codes in ranked[:focus_limit]
        ],
    })


@internal_api_bp.route('/sellers/<int:seller_id>/product-defaults', methods=['GET'])
@_authenticate_agent
def internal_get_product_defaults(seller_id):
    """Системные значения товара без media paths и служебных данных."""
    _, error = _assigned_task_for_seller(seller_id)
    if error:
        return error

    subject_id = request.args.get('subject_id', type=int)
    query = ProductDefaults.query.filter_by(seller_id=seller_id, is_active=True)
    if subject_id is not None:
        query = query.filter(db.or_(
            ProductDefaults.rule_type == 'global',
            db.and_(
                ProductDefaults.rule_type == 'category',
                ProductDefaults.wb_subject_id == subject_id,
            ),
        ))
    rules = query.order_by(ProductDefaults.priority.desc()).limit(100).all()

    return jsonify({
        'defaults': [
            {
                'rule_type': rule.rule_type,
                'wb_subject_id': rule.wb_subject_id,
                'wb_category_name': rule.wb_category_name,
                'dimensions': rule.get_dimensions_dict(),
                'default_characteristics': rule.get_default_characteristics(),
                'min_photos': rule.min_photos,
                'priority': rule.priority,
            }
            for rule in rules
        ],
        'count': len(rules),
    })


@internal_api_bp.route('/sellers/<int:seller_id>/api-connection-status', methods=['GET'])
@_authenticate_agent
def internal_get_api_connection_status(seller_id):
    """Статус WB API без возврата ключа или иных credentials."""
    _, error = _assigned_task_for_seller(seller_id)
    if error:
        return error
    seller = db.session.get(Seller, seller_id)
    if not seller:
        return jsonify({'error': 'Seller not found'}), 404

    has_key = bool(seller._wb_api_key_encrypted)
    return jsonify({
        'connection': {
            'has_key': has_key,
            'mask': '****' if has_key else None,
            'status': seller.api_sync_status or ('configured' if has_key else 'not_configured'),
        },
    })


_LOG_SECRET_PATTERNS = (
    (
        re.compile(r'(?i)\bauthorization\s*[:=]?\s*(?:bearer\s+)?[^\s,;]+'),
        'Authorization: [REDACTED]',
    ),
    (re.compile(r'(?i)\bbearer\s+[^\s,;]+'), 'Bearer [REDACTED]'),
    (
        re.compile(r'(?i)\b(api[_-]?key|token)\s*[=:]\s*[^\s,;]+'),
        r'\1=[REDACTED]',
    ),
)


def _sanitize_api_log_error(value: str) -> str | None:
    if not value:
        return None
    cleaned = str(value)
    for pattern, replacement in _LOG_SECRET_PATTERNS:
        cleaned = pattern.sub(replacement, cleaned)
    return cleaned[:300]


def _sanitize_api_log_endpoint(value: str) -> str:
    """Сохраняет route, но удаляет query string, где могут быть credentials."""
    endpoint = str(value or '')
    path, separator, _ = endpoint.partition('?')
    return f'{path}?[REDACTED]' if separator else path


@internal_api_bp.route('/sellers/<int:seller_id>/api-logs', methods=['GET'])
@_authenticate_agent
def internal_get_api_logs(seller_id):
    """Последние API metadata без request/response bodies."""
    _, error = _assigned_task_for_seller(seller_id)
    if error:
        return error
    limit = min(max(request.args.get('limit', 20, type=int), 1), 50)
    logs = APILog.query.filter_by(seller_id=seller_id).order_by(
        APILog.created_at.desc(), APILog.id.desc(),
    ).limit(limit).all()
    return jsonify({
        'logs': [
            {
                'id': log.id,
                'endpoint': _sanitize_api_log_endpoint(log.endpoint),
                'method': log.method,
                'status_code': log.status_code,
                'response_time': log.response_time,
                'success': bool(log.success),
                'error': _sanitize_api_log_error(log.error_message),
                'created_at': log.created_at.isoformat() if log.created_at else None,
            }
            for log in logs
        ],
        'count': len(logs),
    })


# ── Справочник категорий WB ─────────────────────────────────────

_WB_REFERENCE_MAX_AGE = timedelta(hours=48)


def _wb_reference_status(source, synced_at, sync_status=None, error=None,
                         available=True, has_data=True):
    """Build a small fail-closed freshness contract for agent tools."""
    normalized_status = str(sync_status or '').strip().lower()
    if not normalized_status:
        normalized_status = 'success' if synced_at else 'never_synced'

    stale = True
    if synced_at:
        value = synced_at
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        stale = datetime.now(timezone.utc) - value > _WB_REFERENCE_MAX_AGE

    reason = None
    if not available:
        reason = 'upstream_unavailable'
    elif normalized_status != 'success':
        reason = 'sync_not_successful'
    elif stale:
        reason = 'stale_cache'
    elif not has_data:
        reason = 'empty_cache'

    return {
        'source': source,
        'sync_status': normalized_status,
        'synced_at': synced_at.isoformat() if synced_at else None,
        'stale': stale,
        'available': bool(available),
        'usable': reason is None,
        'reason': reason,
        'error': str(error)[:240] if error else None,
        'max_age_hours': int(_WB_REFERENCE_MAX_AGE.total_seconds() // 3600),
    }


def _wb_reference_warning(status, label):
    if status.get('usable'):
        return None
    reason = status.get('reason')
    if reason == 'upstream_unavailable':
        return f'{label} больше недоступен в WB. Не используйте эти данные.'
    if reason == 'stale_cache':
        return f'{label} устарел. Дождитесь синхронизации с WB.'
    if reason == 'empty_cache':
        return f'{label} пуст. Сначала синхронизируйте данные с WB.'
    return f'{label} не готов к использованию. Дождитесь успешной синхронизации с WB.'


_WB_REFERENCE_BATCH_LIMIT = 200
_WB_CATEGORY_QUERY_MAX_CHARS = 300
_WB_CATEGORY_RESULT_LIMIT = 50
_WB_RU_SEARCH_ENDINGS = (
    'ами', 'ями', 'ого', 'его', 'ому', 'ему', 'ной', 'ный', 'ная', 'ное',
    'ые', 'ие', 'ой', 'ей', 'ом', 'ем', 'ов', 'ев', 'ам', 'ям',
    'ах', 'ях', 'ую', 'юю', 'ий', 'ый',
    'а', 'я', 'о', 'е', 'и', 'ы', 'у', 'ю', 'ь', 'й',
)


def _validated_reference_queries(data):
    if not isinstance(data, dict):
        raise ValueError('JSON body must be an object')
    queries = data.get('queries')
    if (
        not isinstance(queries, list)
        or not queries
        or len(queries) > _WB_REFERENCE_BATCH_LIMIT
    ):
        raise ValueError('queries must contain 1..200 entries')

    prepared = []
    seen = set()
    for index, raw in enumerate(queries):
        if not isinstance(raw, str):
            raise ValueError(f'queries[{index}] must be a string')
        query = raw.strip()
        if len(query) < 2 or len(query) > _WB_CATEGORY_QUERY_MAX_CHARS:
            raise ValueError(
                f'queries[{index}] must contain 2..{_WB_CATEGORY_QUERY_MAX_CHARS} chars'
            )
        normalized = query.casefold()
        if normalized in seen:
            raise ValueError(f'Duplicate query: {query}')
        seen.add(normalized)
        prepared.append(query)
    return prepared


def _validated_reference_subject_ids(data):
    if not isinstance(data, dict):
        raise ValueError('JSON body must be an object')
    subject_ids = data.get('subject_ids')
    if (
        not isinstance(subject_ids, list)
        or not subject_ids
        or len(subject_ids) > _WB_REFERENCE_BATCH_LIMIT
    ):
        raise ValueError('subject_ids must contain 1..200 entries')

    prepared = []
    seen = set()
    for index, raw in enumerate(subject_ids):
        if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
            raise ValueError(
                f'subject_ids[{index}] must be a positive integer'
            )
        if raw in seen:
            raise ValueError(f'Duplicate subject_id: {raw}')
        seen.add(raw)
        prepared.append(raw)
    return prepared


def _wb_categories_reference_status(marketplace):
    return _wb_reference_status(
        'wb_categories',
        marketplace.categories_synced_at if marketplace else None,
        marketplace.categories_sync_status if marketplace else None,
        getattr(marketplace, 'categories_sync_error', None)
        if marketplace else None,
        available=bool(marketplace and marketplace.is_active),
        has_data=bool(marketplace and marketplace.total_categories),
    )


def _load_wb_leaf_categories(marketplace):
    if not marketplace:
        return []
    return (
        MarketplaceCategory.query
        .options(load_only(
            MarketplaceCategory.subject_id,
            MarketplaceCategory.subject_name,
            MarketplaceCategory.parent_name,
            MarketplaceCategory.is_leaf,
            MarketplaceCategory.is_enabled,
            MarketplaceCategory.is_available,
            MarketplaceCategory.last_seen_at,
        ))
        .filter(
            MarketplaceCategory.marketplace_id == marketplace.id,
            MarketplaceCategory.is_leaf.is_(True),
            MarketplaceCategory.is_available.is_(True),
        )
        .all()
    )


def _stem_wb_category_word(word):
    if len(word) <= 3:
        return word
    for ending in _WB_RU_SEARCH_ENDINGS:
        if word.endswith(ending) and len(word) - len(ending) >= 3:
            return word[:-len(ending)]
    return word


def _wb_category_search_spec(query):
    normalized = query.casefold()
    stems = tuple(
        _stem_wb_category_word(word)
        for word in normalized.split()
        if len(word) >= 2
    )
    return normalized, stems


def _wb_category_matches(category, normalized, stems):
    subject_name = str(category.subject_name or '').casefold()
    parent_name = str(category.parent_name or '').casefold()
    terms = stems or (normalized,)
    return all(
        term in subject_name or term in parent_name
        for term in terms
    )


def _wb_category_sort_key(category, normalized, stems):
    subject_name = str(category.subject_name or '')
    folded = subject_name.casefold()
    if normalized in folded:
        priority = 0
    elif stems and stems[0] in folded:
        priority = 1
    else:
        priority = 2
    return priority, subject_name


def _serialize_wb_category(category, include_disabled=False):
    return {
        'subject_id': category.subject_id,
        'subject_name': category.subject_name,
        'parent_name': category.parent_name,
        'is_leaf': category.is_leaf,
        'is_available': getattr(category, 'is_available', True),
        'last_seen_at': (
            category.last_seen_at.isoformat()
            if getattr(category, 'last_seen_at', None) else None
        ),
        **(
            {'is_enabled': category.is_enabled}
            if include_disabled else {}
        ),
    }


def _serialize_wb_category_search(
    query, categories, reference_status, limit,
):
    if not reference_status['usable']:
        return {
            'categories': [],
            'count': 0,
            'reference_status': dict(reference_status),
            'warning': _wb_reference_warning(
                reference_status, 'Справочник категорий WB',
            ),
        }

    normalized, stems = _wb_category_search_spec(query)
    matches = [
        category for category in categories
        if _wb_category_matches(category, normalized, stems)
    ]
    enabled = [category for category in matches if category.is_enabled]
    include_disabled = not enabled and bool(matches)
    selected = enabled if enabled else matches
    selected.sort(
        key=lambda category: _wb_category_sort_key(
            category, normalized, stems,
        )
    )
    selected = selected[:limit]
    return {
        'categories': [
            _serialize_wb_category(category, include_disabled)
            for category in selected
        ],
        'count': len(selected),
        'reference_status': dict(reference_status),
        **(
            {
                'warning': (
                    'Нет включённых категорий по запросу. '
                    'Показаны все доступные (включая отключённые). '
                    'Для использования категории её нужно включить в разделе '
                    'Маркетплейсы → Категории.'
                ),
            }
            if include_disabled else {}
        ),
    }


@internal_api_bp.route('/categories/search', methods=['GET'])
@_authenticate_agent
def internal_search_categories():
    """Поиск по локальному справочнику категорий WB (MarketplaceCategory).

    Ищет ТОЛЬКО конечные (leaf) категории — именно их принимает WB API.
    Поиск выполняется и по subject_name (дочерняя), и по parent_name
    (родительская) — чтобы запрос "Товары для взрослых" вернул все
    дочерние leaf-категории этого раздела.

    Параметры:
        q: поисковый запрос (подстрока названия категории)
        limit: макс. количество результатов (по умолчанию 20)
    """
    q = request.args.get('q', '').strip()
    limit = min(max(request.args.get('limit', 20, type=int), 1), 50)
    if not q or len(q) < 2 or len(q) > _WB_CATEGORY_QUERY_MAX_CHARS:
        return jsonify({
            'error': (
                'Parameter q is required '
                f'(2..{_WB_CATEGORY_QUERY_MAX_CHARS} chars)'
            ),
        }), 400

    marketplace = Marketplace.query.filter_by(code='wb').first()
    reference_status = _wb_categories_reference_status(marketplace)
    categories = (
        _load_wb_leaf_categories(marketplace)
        if reference_status['usable'] else []
    )
    return jsonify(_serialize_wb_category_search(
        q, categories, reference_status, limit,
    ))


@internal_api_bp.route('/categories/search-batch', methods=['POST'])
@_authenticate_agent
def internal_search_categories_batch():
    """Search up to 200 category queries from one local WB snapshot."""
    try:
        data = request.get_json(silent=True)
        queries = _validated_reference_queries(data)
        limit = data.get('limit', 20)
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= _WB_CATEGORY_RESULT_LIMIT
        ):
            raise ValueError('limit must be an integer from 1 to 50')
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400

    marketplace = Marketplace.query.filter_by(code='wb').first()
    reference_status = _wb_categories_reference_status(marketplace)
    categories = (
        _load_wb_leaf_categories(marketplace)
        if reference_status['usable'] else []
    )
    results = [
        {
            'query': query,
            **_serialize_wb_category_search(
                query, categories, reference_status, limit,
            ),
        }
        for query in queries
    ]
    return jsonify({
        'results': results,
        'count': len(results),
        'reference_status': reference_status,
    })


# ── Характеристики категории ──────────────────────────────────

def _wb_category_characteristics_status(category, marketplace=None):
    marketplace = marketplace or getattr(category, 'marketplace', None)
    category_status = _wb_categories_reference_status(marketplace)
    if not category_status['usable']:
        status = dict(category_status)
        status.update({
            'source': (
                f'wb_category_characteristics:{category.subject_id}'
            ),
            'category_reference_status': category_status,
        })
        return status

    status = _wb_reference_status(
        f'wb_category_characteristics:{category.subject_id}',
        category.characteristics_synced_at,
        getattr(category, 'characteristics_sync_status', None),
        getattr(category, 'characteristics_sync_error', None),
        available=getattr(category, 'is_available', True),
        has_data=bool(category.characteristics_count),
    )
    if status['usable'] and not category.is_leaf:
        status.update({
            'usable': False,
            'reason': 'non_leaf_category',
            'error': 'WB category is not a leaf subject',
        })
    elif status['usable'] and not category.is_enabled:
        status.update({
            'usable': False,
            'reason': 'category_disabled',
            'error': 'WB category is disabled in the admin reference',
        })
    return status


def _missing_wb_category_characteristics(subject_id):
    status = _wb_reference_status(
        f'wb_category_characteristics:{subject_id}',
        None,
        'not_found',
        f'WB category {subject_id} was not found in the local reference cache',
        available=False,
        has_data=False,
    )
    status.update({
        'sync_status': 'not_found',
        'stale': False,
        'reason': 'not_found',
    })
    return {
        'subject_id': subject_id,
        'subject_name': None,
        'characteristics': [],
        'count': 0,
        'reference_status': status,
        'warning': (
            f'Категория WB {subject_id} не найдена в локальном '
            'справочнике; нужна синхронизация с WB.'
        ),
    }


def _serialize_wb_category_characteristics(
    marketplace, category, charcs, constraint_cache,
):
    subject_id = category.subject_id
    reference_status = _wb_category_characteristics_status(
        category, marketplace,
    )
    if not reference_status['usable']:
        return {
            'subject_id': subject_id,
            'subject_name': category.subject_name,
            'characteristics': [],
            'count': 0,
            'reference_status': reference_status,
            'warning': _wb_reference_warning(
                reference_status,
                f'Схема характеристик категории {subject_id}',
            ),
        }

    characteristics = []
    required_constraint_issues = []
    try:
        from services.marketplace_validator import (
            get_wb_characteristic_constraint,
        )

        for characteristic in charcs:
            constraint = get_wb_characteristic_constraint(
                marketplace,
                characteristic,
                constraint_cache,
            )
            if characteristic.required and not constraint['usable']:
                required_constraint_issues.append({
                    'charc_id': characteristic.charc_id,
                    'name': characteristic.name,
                    **(constraint.get('issue') or {}),
                })
            characteristics.append({
                'charc_id': characteristic.charc_id,
                'name': characteristic.name,
                'type': characteristic.type_label,
                'required': characteristic.required,
                'unit_name': characteristic.unit_name or '',
                'max_count': characteristic.max_count,
                'popular': characteristic.popular,
                'has_filter': getattr(characteristic, 'has_filter', False),
                'is_variable': getattr(characteristic, 'is_variable', False),
                'constraint': constraint,
                'ai_instruction': characteristic.ai_instruction or '',
                'ai_example_value': characteristic.ai_example_value or '',
            })
    except Exception:
        logger.exception(
            'Failed to resolve WB characteristic constraints for subject_id=%s',
            subject_id,
        )
        reference_status.update({
            'usable': False,
            'reason': 'invalid_cache',
            'error': 'Characteristic dictionary cache is invalid',
        })
        return {
            'subject_id': subject_id,
            'subject_name': category.subject_name,
            'characteristics': [],
            'count': 0,
            'reference_status': reference_status,
            'warning': (
                'Локальная схема характеристик повреждена; '
                'нужна повторная синхронизация.'
            ),
        }

    if required_constraint_issues:
        reference_status.update({
            'usable': False,
            'reason': 'required_constraint_unusable',
            'error': '; '.join(
                str(issue.get('message') or issue.get('name') or '')
                for issue in required_constraint_issues[:5]
            )[:1000],
        })
    elif not characteristics:
        reference_status.update({
            'usable': False,
            'reason': 'empty_enabled_schema',
            'error': None,
        })

    return {
        'subject_id': subject_id,
        'subject_name': category.subject_name,
        'characteristics': (
            characteristics if reference_status['usable'] else []
        ),
        'count': len(characteristics) if reference_status['usable'] else 0,
        'reference_status': reference_status,
        **(
            {'constraint_issues': required_constraint_issues[:10]}
            if required_constraint_issues else {}
        ),
        **(
            {
                'warning': (
                    'Обязательное поле WB нельзя проверить '
                    'по актуальным справочникам; заполнение остановлено.'
                    if required_constraint_issues else
                    'В схеме нет включённых доступных характеристик; '
                    'заполнение остановлено.'
                ),
            }
            if not reference_status['usable'] else {}
        ),
    }


def _load_wb_category_characteristics_payloads(
    marketplace, categories, subject_ids, required_only=False,
):
    categories_by_subject = {
        category.subject_id: category for category in categories
    }
    usable_categories = [
        category for category in categories
        if _wb_category_characteristics_status(
            category, marketplace,
        )['usable']
    ]
    charcs = []
    if usable_categories:
        usable_category_ids = [category.id for category in usable_categories]
        query = MarketplaceCategoryCharacteristic.query.filter(
            MarketplaceCategoryCharacteristic.marketplace_id == marketplace.id,
            MarketplaceCategoryCharacteristic.category_id.in_(
                usable_category_ids,
            ),
            MarketplaceCategoryCharacteristic.is_available.is_(True),
            db.or_(
                MarketplaceCategoryCharacteristic.is_enabled.is_(True),
                MarketplaceCategoryCharacteristic.required.is_(True),
            ),
        )
        if required_only:
            query = query.filter(
                MarketplaceCategoryCharacteristic.required.is_(True),
            )
        charcs = query.order_by(
            MarketplaceCategoryCharacteristic.category_id,
            MarketplaceCategoryCharacteristic.required.desc(),
            MarketplaceCategoryCharacteristic.display_order,
            MarketplaceCategoryCharacteristic.charc_id,
        ).all()

    constraint_cache = {}
    if charcs:
        from services.marketplace_validator import (
            prime_wb_characteristic_directory_cache,
        )
        prime_wb_characteristic_directory_cache(
            marketplace, charcs, constraint_cache,
        )

    charcs_by_category = {}
    for characteristic in charcs:
        charcs_by_category.setdefault(characteristic.category_id, []).append(
            characteristic,
        )

    results = []
    for subject_id in subject_ids:
        category = categories_by_subject.get(subject_id)
        if not category:
            results.append(_missing_wb_category_characteristics(subject_id))
            continue
        results.append(_serialize_wb_category_characteristics(
            marketplace,
            category,
            charcs_by_category.get(category.id, []),
            constraint_cache,
        ))
    return results


@internal_api_bp.route('/categories/<int:subject_id>/characteristics', methods=['GET'])
@_authenticate_agent
def internal_get_category_characteristics(subject_id):
    """Получить характеристики категории WB (обязательные/рекомендованные).

    Возвращает список характеристик с типами, допустимыми значениями
    и AI-инструкциями. НЕ раскрывает конфиденциальные данные.

    Параметры:
        required_only: если true — вернуть только обязательные (default: false)
    """
    marketplace = Marketplace.query.filter_by(code='wb').first()
    category = None
    if marketplace:
        category = MarketplaceCategory.query.filter_by(
            marketplace_id=marketplace.id,
            subject_id=subject_id,
        ).first()
    if not category:
        payload = _missing_wb_category_characteristics(subject_id)
        return jsonify(payload), 404

    required_only = (
        request.args.get('required_only', 'false').lower() == 'true'
    )
    payload = _load_wb_category_characteristics_payloads(
        marketplace, [category], [subject_id], required_only,
    )[0]
    return jsonify(payload)


@internal_api_bp.route('/categories/characteristics-batch', methods=['POST'])
@_authenticate_agent
def internal_get_category_characteristics_batch():
    """Load up to 200 typed category schemas with constant query count."""
    try:
        data = request.get_json(silent=True)
        subject_ids = _validated_reference_subject_ids(data)
        required_only = data.get('required_only', False)
        if not isinstance(required_only, bool):
            raise ValueError('required_only must be a boolean')
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400

    marketplace = Marketplace.query.filter_by(code='wb').first()
    categories = []
    if marketplace:
        categories = MarketplaceCategory.query.filter(
            MarketplaceCategory.marketplace_id == marketplace.id,
            MarketplaceCategory.subject_id.in_(subject_ids),
        ).all()
    results = _load_wb_category_characteristics_payloads(
        marketplace, categories, subject_ids, required_only,
    ) if marketplace else [
        _missing_wb_category_characteristics(subject_id)
        for subject_id in subject_ids
    ]
    return jsonify({'results': results, 'count': len(results)})


# ── Справочники (цвета, страны, сезоны) ─────────────────────

@internal_api_bp.route('/directories/<directory_type>', methods=['GET'])
@_authenticate_agent
def internal_get_directory(directory_type):
    """Получить справочник WB (colors, countries, kinds, seasons).

    Параметры:
        q: поисковый запрос для фильтрации (опционально)
        limit: максимум записей (default: 50)
    """
    if directory_type == 'tnved':
        return jsonify({
            'directory_type': directory_type,
            'items': [],
            'count': 0,
            'reference_status': {
                'source': 'wb_directory:tnved',
                'sync_status': 'unsupported_global_scope',
                'synced_at': None,
                'stale': False,
                'available': False,
                'usable': False,
                'reason': 'category_scope_required',
                'error': None,
                'max_age_hours': 48,
            },
            'warning': 'Справочник ТН ВЭД в WB зависит от subject_id. '
                       'Используйте схему конкретной категории.',
        })

    allowed_types = ('colors', 'countries', 'kinds', 'seasons', 'vat')
    if directory_type not in allowed_types:
        return jsonify({'error': f'Unknown directory type. Allowed: {", ".join(allowed_types)}'}), 400

    marketplace = Marketplace.query.filter_by(code='wb').first()
    directory = MarketplaceDirectory.query.filter_by(
        marketplace_id=marketplace.id if marketplace else -1,
        directory_type=directory_type,
    ).first()

    if not directory or not directory.data_json:
        reference_status = _wb_reference_status(
            f'wb_directory:{directory_type}', None,
            getattr(directory, 'sync_status', None) if directory else None,
            getattr(directory, 'sync_error', None) if directory else None,
            available=bool(marketplace and marketplace.is_active),
            has_data=False,
        )
        return jsonify({
            'directory_type': directory_type,
            'items': [],
            'count': 0,
            'reference_status': reference_status,
            'warning': _wb_reference_warning(
                reference_status, f'Справочник WB {directory_type}',
            ),
        })

    reference_status = _wb_reference_status(
        f'wb_directory:{directory_type}', directory.synced_at,
        getattr(directory, 'sync_status', None),
        getattr(directory, 'sync_error', None),
        available=bool(marketplace and marketplace.is_active),
        has_data=bool(directory.items_count),
    )
    if not reference_status['usable']:
        return jsonify({
            'directory_type': directory_type,
            'items': [],
            'count': 0,
            'reference_status': reference_status,
            'warning': _wb_reference_warning(
                reference_status, f'Справочник WB {directory_type}',
            ),
        })

    try:
        items = json.loads(directory.data_json)
    except Exception:
        return jsonify({'error': 'Failed to parse directory data'}), 500

    # Опциональная фильтрация по подстроке
    q = request.args.get('q', '').strip().lower()
    if q and len(q) >= 2:
        filtered = []
        for item in items:
            # Ищем по любому строковому значению в записи
            match = False
            if isinstance(item, dict):
                for v in item.values():
                    if isinstance(v, str) and q in v.lower():
                        match = True
                        break
            elif isinstance(item, str) and q in item.lower():
                match = True
            if match:
                filtered.append(item)
        items = filtered

    limit = min(request.args.get('limit', 50, type=int), 200)
    items = items[:limit]

    return jsonify({
        'directory_type': directory_type,
        'items': items,
        'count': len(items),
        'reference_status': reference_status,
    })


# ── Запрещённые слова ────────────────────────────────────────

@internal_api_bp.route('/prohibited-words', methods=['GET'])
@_authenticate_agent
def internal_get_prohibited_words():
    """Получить список запрещённых слов (глобальные + продавца).

    БЕЗОПАСНОСТЬ: возвращает только слова и замены, без user IDs и метаданных.

    Параметры:
        seller_id: ID продавца для персональных стоп-слов (опционально)
        q: поиск по слову (опционально)
    """
    seller_id = request.args.get('seller_id', type=int)
    if seller_id:
        _, error = _assigned_task_for_seller(seller_id)
        if error:
            return error

    # Глобальные стоп-слова
    q_filter = ProhibitedWord.query.filter_by(is_active=True)

    if seller_id:
        # Глобальные + персональные для этого продавца
        q_filter = q_filter.filter(
            db.or_(
                ProhibitedWord.scope == 'global',
                db.and_(
                    ProhibitedWord.scope == 'seller',
                    ProhibitedWord.seller_id == seller_id,
                ),
            )
        )
    else:
        q_filter = q_filter.filter_by(scope='global')

    # Поиск по подстроке
    search = request.args.get('q', '').strip()
    if search and len(search) >= 2:
        q_filter = q_filter.filter(ProhibitedWord.word.ilike(f'%{search}%'))

    words = q_filter.order_by(ProhibitedWord.word).limit(500).all()

    # БЕЗОПАСНОСТЬ: возвращаем ТОЛЬКО слово и замену, без created_by, seller_id и т.д.
    return jsonify({
        'words': [
            {'word': w.word, 'replacement': w.replacement}
            for w in words
        ],
        'count': len(words),
    })


# ── Проверка текста на стоп-слова ────────────────────────────

@internal_api_bp.route('/prohibited-words/check', methods=['POST'])
@_authenticate_agent
def internal_check_prohibited_words():
    """Проверить текст на запрещённые слова.

    Body: { "text": "...", "seller_id": 123 (optional) }
    """
    data = request.get_json(silent=True) or {}
    text = data.get('text', '')
    if not text:
        return jsonify({'error': 'text is required'}), 400

    seller_id = data.get('seller_id')
    if seller_id:
        try:
            seller_id = int(seller_id)
        except (TypeError, ValueError):
            return jsonify({'error': 'seller_id must be an integer'}), 400
        _, error = _assigned_task_for_seller(seller_id)
        if error:
            return error

    try:
        from services.prohibited_words_filter import get_prohibited_words_filter
        pf = get_prohibited_words_filter(seller_id)
        found = pf.has_prohibited_words(text)
        filtered = pf.filter_text(text)
    except Exception as e:
        logger.error(f"Prohibited words check error: {e}")
        return jsonify({'error': 'Filter unavailable'}), 500

    return jsonify({
        'has_prohibited': len(found) > 0,
        'found_words': found,
        'filtered_text': filtered,
    })


@internal_api_bp.route('/prohibited-words/check-batch', methods=['POST'])
@_authenticate_agent
def internal_check_prohibited_words_batch():
    """Проверить до 50 коротких текстов одним запросом в seller scope."""
    data = request.get_json(silent=True) or {}
    try:
        seller_id = int(data.get('seller_id'))
    except (TypeError, ValueError):
        return jsonify({'error': 'seller_id must be an integer'}), 400
    _, error = _assigned_task_for_seller(seller_id)
    if error:
        return error
    items = data.get('items')
    if not isinstance(items, list) or not items:
        return jsonify({'error': 'items array is required'}), 400
    if len(items) > 50:
        return jsonify({'error': 'Maximum 50 texts per request'}), 400

    from services.prohibited_words_filter import get_prohibited_words_filter
    try:
        prohibited_filter = get_prohibited_words_filter(seller_id)
        results = []
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                return jsonify({'error': f'items[{index}] must be an object'}), 400
            text = str(item.get('text') or '').strip()[:2000]
            if not text:
                return jsonify({'error': f'items[{index}].text is required'}), 400
            found = prohibited_filter.has_prohibited_words(text)
            results.append({
                'product_id': item.get('product_id'),
                'field': item.get('field'),
                'has_prohibited': bool(found),
                'found_words': found,
                'filtered_text': prohibited_filter.filter_text(text),
            })
    except Exception:
        logger.exception('Prohibited words batch check failed')
        return jsonify({'error': 'Filter unavailable'}), 500
    return jsonify({'results': results, 'count': len(results)})


# ── Валидация бренда ─────────────────────────────────────────

@internal_api_bp.route('/brands/validate', methods=['GET'])
@_authenticate_agent
def internal_validate_brand():
    """Проверить бренд по локальному реестру (без обращения к WB API).

    БЕЗОПАСНОСТЬ: НЕ раскрывает API-ключи, внутренние ID пользователей.
    Возвращает только публичные данные бренда.

    Параметры:
        brand: название бренда для проверки (обязательно)
        category_id: subject_id категории для проверки доступности (опционально)
    """
    brand_name = request.args.get('brand', '').strip()
    if not brand_name or len(brand_name) < 2:
        return jsonify({'error': 'brand parameter required (min 2 chars)'}), 400

    raw_category_id = request.args.get('category_id')
    if raw_category_id in (None, ''):
        category_id = None
    else:
        try:
            category_id = parse_positive_integer(
                raw_category_id, 'category_id',
            )
        except ValueError as exc:
            return jsonify({'error': str(exc)}), 400

    wb_marketplace = Marketplace.query.filter_by(code='wb').first()
    from services.brand_engine import normalize_for_comparison
    normalized = normalize_for_comparison(brand_name)
    alias = BrandAlias.query.join(Brand).filter(
        BrandAlias.alias_normalized == normalized,
        BrandAlias.is_active.is_(True),
        Brand.status == 'verified',
    ).first()
    brand = alias.brand if alias and alias.brand else None
    mp_brand = None
    category_link = None
    category_scope_available = category_id is None
    if wb_marketplace and category_id:
        category_scope_available = MarketplaceCategory.query.filter_by(
            marketplace_id=wb_marketplace.id,
            subject_id=category_id,
            is_enabled=True,
            is_available=True,
        ).first() is not None
    if wb_marketplace and brand:
        mp_brand = MarketplaceBrand.query.filter_by(
            brand_id=brand.id,
            marketplace_id=wb_marketplace.id,
            is_available=True,
            status='verified',
        ).first()
        if mp_brand and category_id and category_scope_available:
            category_link = BrandCategoryLink.query.filter_by(
                marketplace_brand_id=mp_brand.id,
                category_id=category_id,
            ).first()

    verified_binding_exists = False
    if wb_marketplace:
        verified_binding_exists = MarketplaceBrand.query.filter_by(
            marketplace_id=wb_marketplace.id,
            status='verified',
            is_available=True,
        ).first() is not None
    reference_status = _wb_reference_status(
        'wb_brands',
        wb_marketplace.brands_synced_at if wb_marketplace else None,
        wb_marketplace.brands_sync_status if wb_marketplace else None,
        getattr(wb_marketplace, 'brands_sync_error', None) if wb_marketplace else None,
        available=bool(wb_marketplace and wb_marketplace.is_active),
        has_data=bool(
            wb_marketplace
            and (wb_marketplace.brands_version or 0) > 0
            and verified_binding_exists
        ),
    )
    reference_status['version'] = int(
        wb_marketplace.brands_version or 0
    ) if wb_marketplace else 0

    # A recent manual/live verification is authoritative for this exact
    # brand/category pair even while a bounded global sweep is still partial.
    if category_id and not category_scope_available:
        reference_status = _wb_reference_status(
            'wb_brands', None, None,
            f'WB category {category_id} is unavailable',
            available=False,
            has_data=False,
        )
        reference_status.update({
            'version': int(wb_marketplace.brands_version or 0)
            if wb_marketplace else 0,
            'scope': 'category',
            'category_id': category_id,
        })
    elif category_id and mp_brand and category_link:
        reference_status = _wb_reference_status(
            'wb_brands',
            category_link.verified_at,
            'success',
            None,
            available=bool(wb_marketplace and wb_marketplace.is_active),
            has_data=True,
        )
        reference_status.update({
            'version': int(wb_marketplace.brands_version or 0),
            'scope': 'category',
            'category_id': category_id,
        })
    if not reference_status['usable']:
        return jsonify({
            'result': {
                'status': 'unavailable',
                'brand_name': None,
                'confidence': 0.0,
                'suggestions': [],
            },
            'reference_status': reference_status,
            'warning': _wb_reference_warning(
                reference_status, 'Справочник брендов WB',
            ),
        })

    # Точный ответ допустим только для verified+available WB binding.
    if brand and mp_brand:
        result = {
            'status': 'found',
            'brand_name': brand.name,
            'confidence': 1.0,
            'source': 'exact_match',
        }

        result['marketplace_brand_name'] = mp_brand.marketplace_brand_name
        result['marketplace_brand_id'] = mp_brand.marketplace_brand_id

        if category_id:
            if category_link:
                result['category_available'] = category_link.is_available
            else:
                result['category_available'] = None
                result['category_warning'] = (
                    f'Нет данных о доступности бренда в категории {category_id}. '
                    f'Бренд НЕ подтверждён в этой категории — wb_registered=false.'
                )
        else:
            result['category_available'] = None
            result['category_warning'] = (
                'category_id не передан — проверка доступности в категории не выполнена. '
                'Бренд найден в реестре, но category_available=null.'
            )

        return jsonify({
            'result': result,
            'reference_status': reference_status,
        })

    # Нечёткий поиск: ищем похожие бренды
    suggestions = []
    all_aliases = BrandAlias.query.join(Brand).join(
        MarketplaceBrand,
        MarketplaceBrand.brand_id == Brand.id,
    ).filter(
        BrandAlias.is_active.is_(True),
        Brand.status != 'rejected',
        MarketplaceBrand.marketplace_id == wb_marketplace.id,
        MarketplaceBrand.status == 'verified',
        MarketplaceBrand.is_available.is_(True),
    ).limit(5000).all()

    from difflib import SequenceMatcher
    for a in all_aliases:
        ratio = SequenceMatcher(None, normalized, a.alias_normalized or '').ratio()
        if ratio >= 0.7:
            suggestions.append({
                'brand_name': a.brand.name if a.brand else a.alias,
                'confidence': round(ratio, 2),
            })

    suggestions.sort(key=lambda x: x['confidence'], reverse=True)

    return jsonify({
        'result': {
            'status': 'not_found' if not suggestions else 'suggestions',
            'brand_name': None,
            'confidence': 0.0,
            'suggestions': suggestions[:5],
        },
        'reference_status': reference_status,
    })


@internal_api_bp.route('/brands/preflight', methods=['POST'])
@_authenticate_agent
def internal_preflight_brand_categories():
    """Check typed WB category scopes without looking up a candidate brand."""
    data = request.get_json(silent=True) or {}
    category_ids = data.get('category_ids')
    if not isinstance(category_ids, list) or not 1 <= len(category_ids) <= 100:
        return jsonify({'error': 'category_ids must contain 1..100 entries'}), 400
    try:
        return jsonify(preflight_brand_categories(category_ids))
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400


@internal_api_bp.route('/brands/validate-batch', methods=['POST'])
@_authenticate_agent
def internal_validate_brands_batch():
    """Validate up to 100 typed brand/category pairs with bounded bulk SQL."""
    data = request.get_json(silent=True) or {}
    items = data.get('items')
    if not isinstance(items, list) or not items or len(items) > 100:
        return jsonify({'error': 'items must contain 1..100 entries'}), 400

    prepared = []
    seen_product_ids = set()
    for item in items:
        if not isinstance(item, dict):
            return jsonify({'error': 'Each item must be an object'}), 400
        try:
            product_id = parse_positive_integer(
                item.get('product_id'), 'product_id',
            )
        except ValueError as exc:
            return jsonify({'error': str(exc)}), 400
        if product_id in seen_product_ids:
            return jsonify({'error': 'product_id values must be unique'}), 400
        seen_product_ids.add(product_id)
        brand_name = str(item.get('brand') or '').strip()
        if len(brand_name) < 2:
            return jsonify({'error': 'brand must contain at least 2 chars'}), 400
        raw_category_id = item.get('category_id')
        if raw_category_id in (None, ''):
            category_id = None
        else:
            try:
                category_id = parse_positive_integer(
                    raw_category_id, 'category_id',
                )
            except ValueError as exc:
                return jsonify({'error': str(exc)}), 400
        prepared.append({
            'request_id': product_id,
            'brand': brand_name,
            'category_id': category_id,
        })
    try:
        resolved = resolve_exact_brand_categories(prepared)
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    results = [
        {
            **{key: value for key, value in result.items() if key != 'request_id'},
            'product_id': result['request_id'],
        }
        for result in resolved
    ]
    return jsonify({'results': results, 'count': len(results)})


# ── Настройки ценообразования ────────────────────────────────

@internal_api_bp.route('/sellers/<int:seller_id>/pricing', methods=['GET'])
@_authenticate_agent
def internal_get_pricing_settings(seller_id):
    """Получить настройки ценообразования продавца.

    БЕЗОПАСНОСТЬ: НЕ возвращает URL файлов поставщика, хеши, user IDs.
    Только формулы и коэффициенты, нужные для расчёта цен.
    """
    _, error = _assigned_task_for_seller(seller_id)
    if error:
        return error
    ps = PricingSettings.query.filter_by(seller_id=seller_id).first()
    if not ps:
        return jsonify({'error': 'Pricing settings not found for this seller'}), 404

    if not ps.is_enabled:
        return jsonify({'error': 'Pricing is not enabled for this seller'}), 404

    # Таблица наценок
    price_ranges = []
    if ps.price_ranges:
        try:
            price_ranges = json.loads(ps.price_ranges)
        except Exception:
            pass

    # БЕЗОПАСНОСТЬ: возвращаем ТОЛЬКО формулы и коэффициенты
    # НЕ возвращаем: supplier_price_url, supplier_price_inf_url,
    # last_price_file_hash и другие внутренние поля
    return jsonify({
        'pricing': {
            'formula_type': ps.formula_type,
            'wb_commission_pct': ps.wb_commission_pct,
            'tax_rate': ps.tax_rate,
            'logistics_cost': ps.logistics_cost,
            'storage_cost': ps.storage_cost,
            'packaging_cost': ps.packaging_cost,
            'acquiring_cost': ps.acquiring_cost,
            'extra_cost': ps.extra_cost,
            'delivery_pct': ps.delivery_pct,
            'delivery_min': ps.delivery_min,
            'delivery_max': ps.delivery_max,
            'profit_column': ps.profit_column,
            'min_profit': ps.min_profit,
            'max_profit': ps.max_profit,
            'spp_pct': ps.spp_pct,
            'spp_min': ps.spp_min,
            'spp_max': ps.spp_max,
            'inflated_multiplier': ps.inflated_multiplier,
            'price_ranges': price_ranges,
        }
    })


# ── Валидация характеристик ─────────────────────────────────

@internal_api_bp.route('/imported-products/<int:product_id>/validate', methods=['POST'])
@_authenticate_agent
def internal_validate_imported_product(product_id):
    """Валидация данных товара перед сохранением.

    Проверяет характеристики, размеры, заголовок, описание по схеме WB.
    Используется агентами для проверки своей работы.

    Body: { "characteristics": {...}, "title": "...", "sizes": {...} }
    """
    task, error = _assigned_task_for_seller()
    if error:
        return error
    p = ImportedProduct.query.filter_by(
        id=product_id, seller_id=task.seller_id,
    ).first()
    if not p:
        return jsonify({'error': 'Imported product not found'}), 404

    data = request.get_json(silent=True) or {}
    errors = []
    warnings = []

    # Validate title
    title = data.get('title', p.title or '')
    if title and len(title) > 60:
        errors.append(f'Заголовок {len(title)} символов (макс. 60)')

    # Validate description
    desc = data.get('description', p.description or '')
    if desc and len(desc) > 5000:
        errors.append(f'Описание {len(desc)} символов (макс. 5000)')

    # Validate characteristics against category schema
    chars = data.get('characteristics')
    if chars and p.wb_subject_id:
        category = MarketplaceCategory.query.filter_by(
            subject_id=p.wb_subject_id
        ).first()
        if category:
            schema_charcs = MarketplaceCategoryCharacteristic.query.filter_by(
                category_id=category.id,
                is_enabled=True,
            ).all()

            if isinstance(chars, str):
                try:
                    chars = json.loads(chars)
                except Exception:
                    errors.append('characteristics: невалидный JSON')
                    chars = {}

            if isinstance(chars, dict):
                schema_names = {c.name.lower(): c for c in schema_charcs}
                filled_required = 0
                total_required = 0

                for c in schema_charcs:
                    if c.charc_type == 0:
                        continue
                    if c.required:
                        total_required += 1
                        if c.name in chars or c.name.lower() in {k.lower() for k in chars}:
                            filled_required += 1
                        else:
                            warnings.append(f'Обязательная характеристика "{c.name}" не заполнена')

                # Check that provided keys match schema
                for key in chars:
                    if key.lower() not in schema_names:
                        warnings.append(f'Характеристика "{key}" не найдена в схеме категории')

                result_chars = {
                    'total_required': total_required,
                    'filled_required': filled_required,
                    'provided_count': len(chars),
                }
            else:
                errors.append('characteristics: должен быть JSON-объект')
                result_chars = {}
        else:
            warnings.append(f'Категория subject_id={p.wb_subject_id} не найдена для валидации')
            result_chars = {}
    else:
        result_chars = {}

    return jsonify({
        'valid': len(errors) == 0,
        'errors': errors,
        'warnings': warnings,
        'characteristics_validation': result_chars,
    })


def _product_to_dict(p):
    """Сериализация Product для Internal API (агенты).

    Только поля, нужные агентам. Без photo URLs (экономия токенов).
    """
    def bounded_json(raw, fallback):
        if not raw:
            return fallback
        try:
            value = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError):
            return fallback
        if isinstance(value, dict):
            return dict(list(value.items())[:40])
        if isinstance(value, list):
            return value[:40]
        return fallback

    photos = bounded_json(getattr(p, 'photos_json', None), [])
    return {
        'id': p.id,
        'nm_id': p.nm_id,
        'imt_id': p.imt_id,
        'title': p.title,
        'brand': p.brand,
        'vendor_code': p.vendor_code,
        'object_name': p.object_name,
        'subject_id': p.subject_id,
        'description': getattr(p, 'description', None),
        'characteristics': bounded_json(getattr(p, 'characteristics_json', None), {}),
        'sizes': bounded_json(getattr(p, 'sizes_json', None), []),
        'photos_count': len(photos),
        'is_active': p.is_active,
    }


def _imported_product_to_dict(p):
    """Сериализация ImportedProduct для Internal API (агенты).

    Только поля, нужные для AI-обработки. Без дублирующих блоков
    (original_data, all_data_for_parsing) и photo URLs — они не нужны
    текстовым агентам и занимают ~75% токенов.
    """
    import json as _json

    # Характеристики — распарсим JSON text
    chars = {}
    if p.characteristics:
        try:
            chars = _json.loads(p.characteristics)
        except Exception:
            pass

    # Размеры
    sizes = {}
    if p.sizes:
        try:
            sizes = _json.loads(p.sizes)
        except Exception:
            pass

    return {
        'id': p.id,
        'external_id': p.external_id,
        'title': p.title,
        'description': p.description or '',
        'brand': p.brand or '',
        'category': p.category or '',
        'mapped_wb_category': p.mapped_wb_category or '',
        'wb_subject_id': p.wb_subject_id,
        'country': p.country or '',
        'gender': p.gender or '',
        'supplier_price': p.supplier_price,
        'characteristics': chars,
        'sizes': sizes,
        'import_status': p.import_status,
        'photos_count': len(_json.loads(p.photo_urls)) if p.photo_urls else 0,
    }
