# -*- coding: utf-8 -*-
"""
Internal API — эндпоинты для сервисных AI-агентов (Python ADK).

Все эндпоинты требуют аутентификации через заголовок X-Agent-Key.
Prefix: /internal/v1/

Blueprint исключён из CSRF-защиты (агенты аутентифицируются через X-Agent-Key).
"""
import json
import logging
from functools import wraps
from datetime import datetime

from flask import request, jsonify, abort, Blueprint
from werkzeug.security import check_password_hash

from models import db, ServiceAgent, Product, ImportedProduct, SupplierProduct, Seller, MarketplaceCategory
from services import agent_service

logger = logging.getLogger(__name__)

internal_api_bp = Blueprint('internal_api', __name__, url_prefix='/internal/v1')


def _authenticate_agent(f):
    """Декоратор: аутентификация агента по X-Agent-Id + X-Agent-Key."""
    @wraps(f)
    def decorated(*args, **kwargs):
        agent_id = request.headers.get('X-Agent-Id', '')
        agent_key = request.headers.get('X-Agent-Key', '')

        if not agent_id or not agent_key:
            return jsonify({'error': 'Missing X-Agent-Id or X-Agent-Key header'}), 401

        agent = ServiceAgent.query.get(agent_id)
        if not agent:
            return jsonify({'error': 'Unknown agent'}), 401

        if not agent.api_key_hash or not check_password_hash(agent.api_key_hash, agent_key):
            return jsonify({'error': 'Invalid agent key'}), 401

        request._agent = agent
        return f(*args, **kwargs)
    return decorated


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
    task = agent_service.start_task(task_id)
    if not task:
        return jsonify({'error': 'Task not found or not in queued state'}), 404
    return jsonify({'ok': True, 'task': task.to_dict()})


@internal_api_bp.route('/tasks/<task_id>/progress', methods=['POST'])
@_authenticate_agent
def internal_update_progress(task_id):
    """Агент обновляет прогресс задачи."""
    data = request.get_json(silent=True) or {}
    task = agent_service.update_task_progress(
        task_id,
        completed_steps=data.get('completed_steps', 0),
        current_step_label=data.get('current_step_label'),
        total_steps=data.get('total_steps'),
    )
    if not task:
        return jsonify({'error': 'Task not found'}), 404
    return jsonify({'ok': True, 'task': task.to_dict()})


@internal_api_bp.route('/tasks/<task_id>/complete', methods=['POST'])
@_authenticate_agent
def internal_complete_task(task_id):
    """Агент завершает задачу успешно."""
    data = request.get_json(silent=True) or {}
    task = agent_service.complete_task(task_id, result_data=data.get('result'))
    if not task:
        return jsonify({'error': 'Task not found'}), 404
    return jsonify({'ok': True, 'task': task.to_dict()})


@internal_api_bp.route('/tasks/<task_id>/fail', methods=['POST'])
@_authenticate_agent
def internal_fail_task(task_id):
    """Агент сообщает об ошибке."""
    data = request.get_json(silent=True) or {}
    task = agent_service.fail_task(
        task_id,
        error_message=data.get('error', 'Unknown error'),
        result_data=data.get('result'),
    )
    if not task:
        return jsonify({'error': 'Task not found'}), 404
    return jsonify({'ok': True, 'task': task.to_dict()})


# ── Шаги задач ──────────────────────────────────────────────────

@internal_api_bp.route('/tasks/<task_id>/steps', methods=['POST'])
@_authenticate_agent
def internal_add_step(task_id):
    """Агент логирует шаг выполнения задачи."""
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
    steps = agent_service.get_task_steps(task_id)
    return jsonify({'steps': [s.to_dict() for s in steps]})


# ── Данные: товары ──────────────────────────────────────────────

@internal_api_bp.route('/sellers/<int:seller_id>/products', methods=['GET'])
@_authenticate_agent
def internal_list_products(seller_id):
    """Получить товары продавца."""
    page = request.args.get('page', 1, type=int)
    per_page = min(request.args.get('per_page', 50, type=int), 200)
    status = request.args.get('status')

    q = Product.query.filter_by(seller_id=seller_id)
    if status:
        q = q.filter_by(wb_status=status)

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
    product = Product.query.filter_by(id=product_id, seller_id=seller_id).first()
    if not product:
        return jsonify({'error': 'Product not found'}), 404
    return jsonify({'product': _product_to_dict(product)})


@internal_api_bp.route('/sellers/<int:seller_id>/products/<int:product_id>', methods=['PATCH'])
@_authenticate_agent
def internal_update_product(seller_id, product_id):
    """Агент обновляет данные товара."""
    product = Product.query.filter_by(id=product_id, seller_id=seller_id).first()
    if not product:
        return jsonify({'error': 'Product not found'}), 404

    data = request.get_json(silent=True) or {}
    allowed_fields = [
        'title', 'description', 'brand', 'vendor_code',
        'characteristics', 'tags', 'ai_seo_title',
        'wb_category_id', 'wb_category_name',
    ]
    for field in allowed_fields:
        if field in data:
            # Маппинг полей агентов на поля модели
            if field == 'wb_category_id':
                if hasattr(product, 'subject_id'):
                    product.subject_id = data[field]
                elif hasattr(product, 'wb_category_id'):
                    product.wb_category_id = data[field]
            elif field == 'wb_category_name':
                if hasattr(product, 'category'):
                    product.category = data[field]
            else:
                setattr(product, field, data[field])

    product.updated_at = datetime.utcnow()
    db.session.commit()
    return jsonify({'ok': True, 'product': _product_to_dict(product)})


@internal_api_bp.route('/sellers/<int:seller_id>/imported-products', methods=['GET'])
@_authenticate_agent
def internal_list_imported_products(seller_id):
    """Получить импортированные товары (от поставщика)."""
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


@internal_api_bp.route('/imported-products/<int:product_id>', methods=['GET'])
@_authenticate_agent
def internal_get_imported_product(product_id):
    """Получить одну импортированную запись по ID."""
    p = ImportedProduct.query.get(product_id)
    if not p:
        return jsonify({'error': 'Imported product not found'}), 404
    return jsonify({'product': _imported_product_to_dict(p)})


@internal_api_bp.route('/imported-products/<int:product_id>', methods=['PATCH'])
@_authenticate_agent
def internal_update_imported_product(product_id):
    """Агент обновляет данные импортированного товара."""
    p = ImportedProduct.query.get(product_id)
    if not p:
        return jsonify({'error': 'Imported product not found'}), 404

    data = request.get_json(silent=True) or {}
    allowed_fields = [
        'title', 'description', 'brand', 'mapped_wb_category',
        'wb_subject_id', 'category_confidence',
        'ai_seo_title', 'ai_keywords', 'ai_bullets',
    ]
    for field in allowed_fields:
        if field in data:
            setattr(p, field, data[field])

    # Также принимаем wb_category_id/wb_category_name от агентов
    if 'wb_category_id' in data:
        p.wb_subject_id = data['wb_category_id']
    if 'wb_category_name' in data:
        p.mapped_wb_category = data['wb_category_name']

    p.updated_at = datetime.utcnow()
    db.session.commit()
    return jsonify({'ok': True, 'product': _imported_product_to_dict(p)})


# ── Задачи: создание подзадач (для оркестратора) ───────────────

@internal_api_bp.route('/tasks/create', methods=['POST'])
@_authenticate_agent
def internal_create_task():
    """Агент-оркестратор создаёт подзадачу для другого агента."""
    data = request.get_json(silent=True) or {}

    agent_name = data.get('agent_name')
    if not agent_name:
        return jsonify({'error': 'agent_name is required'}), 400

    target_agent = ServiceAgent.query.filter_by(name=agent_name).first()
    if not target_agent:
        return jsonify({'error': f'Agent "{agent_name}" not found'}), 404

    seller_id = data.get('seller_id')
    if not seller_id:
        return jsonify({'error': 'seller_id is required'}), 400

    task = agent_service.create_task(
        agent_id=target_agent.id,
        seller_id=seller_id,
        task_type=data.get('task_type', 'unknown'),
        title=data.get('title', f'Подзадача: {agent_name}'),
        input_data=data.get('input_data', {}),
        priority=data.get('priority', 0),
        parent_task_id=data.get('parent_task_id'),
    )
    return jsonify({'ok': True, 'task': task.to_dict()})


@internal_api_bp.route('/tasks/<task_id>', methods=['GET'])
@_authenticate_agent
def internal_get_task(task_id):
    """Получить статус задачи (для оркестратора)."""
    task = agent_service.get_task(task_id)
    if not task:
        return jsonify({'error': 'Task not found'}), 404
    return jsonify({'task': task.to_dict()})


# ── Данные: продавцы ────────────────────────────────────────────

@internal_api_bp.route('/sellers/<int:seller_id>', methods=['GET'])
@_authenticate_agent
def internal_get_seller(seller_id):
    """Информация о продавце."""
    seller = Seller.query.get(seller_id)
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


# ── Справочник категорий WB ─────────────────────────────────────

@internal_api_bp.route('/categories/search', methods=['GET'])
@_authenticate_agent
def internal_search_categories():
    """Поиск по локальному справочнику категорий WB (MarketplaceCategory).

    Параметры:
        q: поисковый запрос (подстрока названия категории)
        limit: макс. количество результатов (по умолчанию 20)
    """
    q = request.args.get('q', '').strip()
    limit = min(request.args.get('limit', 20, type=int), 50)

    if not q or len(q) < 2:
        return jsonify({'error': 'Parameter q is required (min 2 chars)'}), 400

    # Ищем только включённые категории по subject_name
    categories = MarketplaceCategory.query.filter(
        MarketplaceCategory.is_enabled == True,
        MarketplaceCategory.subject_name.ilike(f'%{q}%')
    ).order_by(
        MarketplaceCategory.subject_name
    ).limit(limit).all()

    return jsonify({
        'categories': [
            {
                'subject_id': c.subject_id,
                'subject_name': c.subject_name,
                'parent_name': c.parent_name,
                'is_enabled': c.is_enabled,
            }
            for c in categories
        ],
        'count': len(categories),
    })


def _product_to_dict(p):
    """Сериализация Product для Internal API (агенты).

    Только поля, нужные агентам. Без photo URLs (экономия токенов).
    """
    return {
        'id': p.id,
        'nm_id': p.nm_id,
        'imt_id': p.imt_id,
        'title': p.title,
        'brand': p.brand,
        'vendor_code': p.vendor_code,
        'description': getattr(p, 'description', None),
        'barcode': p.barcode,
        'category': getattr(p, 'category', None),
        'wb_status': getattr(p, 'wb_status', None),
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
