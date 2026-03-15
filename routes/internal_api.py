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

from models import (
    db, ServiceAgent, Product, ImportedProduct, SupplierProduct, Seller,
    MarketplaceCategory, MarketplaceCategoryCharacteristic,
    MarketplaceDirectory, PricingSettings, ProhibitedWord,
    Brand, BrandAlias, MarketplaceBrand, BrandCategoryLink,
    AgentChangeSnapshot,
)
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


@internal_api_bp.route('/imported-products/brief', methods=['POST'])
@_authenticate_agent
def internal_get_imported_products_brief():
    """Пакетное получение краткой информации о товарах (экономия токенов).

    Возвращает только id, title, brand, category — минимум для маппинга.
    Максимум 50 товаров за раз.

    Body: { "product_ids": [1, 2, 3] }
    """
    data = request.get_json(silent=True) or {}
    product_ids = data.get('product_ids', [])

    if not product_ids or not isinstance(product_ids, list):
        return jsonify({'error': 'product_ids array is required'}), 400

    product_ids = product_ids[:50]

    products = ImportedProduct.query.filter(
        ImportedProduct.id.in_(product_ids)
    ).all()

    return jsonify({
        'products': [
            {
                'id': p.id,
                'title': p.title or '',
                'brand': p.brand or '',
                'category': p.category or '',
                'mapped_wb_category': p.mapped_wb_category or '',
                'wb_subject_id': p.wb_subject_id,
            }
            for p in products
        ],
        'count': len(products),
    })


@internal_api_bp.route('/imported-products/<int:product_id>', methods=['PATCH'])
@_authenticate_agent
def internal_update_imported_product(product_id):
    """Агент обновляет данные импортированного товара.

    БЕЗОПАСНОСТЬ ЦЕН: Если агент устанавливает calculated_price,
    проверяем что цена >= supplier_price * (1 + min_price_margin_pct/100).
    По умолчанию min_price_margin_pct = 20% (настраивается в PricingSettings).
    """
    p = ImportedProduct.query.get(product_id)
    if not p:
        return jsonify({'error': 'Imported product not found'}), 404

    data = request.get_json(silent=True) or {}

    # ── Защита цен: агент НЕ может установить цену ниже порога ──
    price_fields = ('calculated_price', 'calculated_discount_price',
                    'calculated_price_before_discount')
    for pf in price_fields:
        if pf in data and data[pf] is not None:
            try:
                new_price = float(data[pf])
            except (ValueError, TypeError):
                return jsonify({'error': f'Invalid {pf} value'}), 400

            if p.supplier_price and p.supplier_price > 0:
                # Получаем минимальный порог из настроек продавца
                min_margin_pct = 20.0  # fallback по умолчанию
                ps = PricingSettings.query.filter_by(seller_id=p.seller_id).first()
                if ps and ps.min_profit is not None:
                    min_margin_pct = ps.min_profit

                min_allowed = p.supplier_price * (1 + min_margin_pct / 100)

                if new_price < p.supplier_price:
                    return jsonify({
                        'error': f'ЗАПРЕЩЕНО: цена {new_price} ниже закупочной {p.supplier_price}',
                        'min_allowed': round(min_allowed, 2),
                        'supplier_price': p.supplier_price,
                    }), 400

                if new_price < min_allowed:
                    return jsonify({
                        'error': (
                            f'ЗАПРЕЩЕНО: цена {new_price} ниже минимального порога '
                            f'{round(min_allowed, 2)} (закупка {p.supplier_price} + {min_margin_pct}%)'
                        ),
                        'min_allowed': round(min_allowed, 2),
                        'supplier_price': p.supplier_price,
                        'min_margin_pct': min_margin_pct,
                    }), 400

    allowed_fields = [
        'title', 'description', 'brand', 'mapped_wb_category',
        'wb_subject_id', 'category_confidence',
        'ai_seo_title', 'ai_keywords', 'ai_bullets',
        'characteristics', 'sizes', 'gender', 'country',
        'calculated_price', 'calculated_discount_price',
        'calculated_price_before_discount',
    ]

    # Также принимаем wb_category_id/wb_category_name от агентов
    if 'wb_category_id' in data:
        data['wb_subject_id'] = data.pop('wb_category_id')
    if 'wb_category_name' in data:
        data['mapped_wb_category'] = data.pop('wb_category_name')

    # ── Сохраняем снимок предыдущих значений для отката ──
    previous_values = {}
    new_values = {}
    for field in allowed_fields:
        if field in data:
            old_val = getattr(p, field, None)
            new_val = data[field]
            # Записываем только реально изменённые поля
            if str(old_val) != str(new_val):
                previous_values[field] = old_val
                new_values[field] = new_val

    # Применяем изменения
    for field in allowed_fields:
        if field in data:
            setattr(p, field, data[field])

    p.updated_at = datetime.utcnow()

    # Сохраняем снимок если были реальные изменения
    if previous_values:
        task_id = request.headers.get('X-Task-Id')
        snapshot = AgentChangeSnapshot(
            task_id=task_id,
            imported_product_id=p.id,
            agent_id=request._agent.id if hasattr(request, '_agent') else None,
            previous_values=json.dumps(previous_values, ensure_ascii=False, default=str),
            new_values=json.dumps(new_values, ensure_ascii=False, default=str),
        )
        db.session.add(snapshot)

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

    Ищет ТОЛЬКО конечные (leaf) категории — именно их принимает WB API.
    Поиск выполняется и по subject_name (дочерняя), и по parent_name
    (родительская) — чтобы запрос "Товары для взрослых" вернул все
    дочерние leaf-категории этого раздела.

    Параметры:
        q: поисковый запрос (подстрока названия категории)
        limit: макс. количество результатов (по умолчанию 20)
    """
    q = request.args.get('q', '').strip()
    limit = min(request.args.get('limit', 20, type=int), 50)

    if not q or len(q) < 2:
        return jsonify({'error': 'Parameter q is required (min 2 chars)'}), 400

    # Ищем ТОЛЬКО включённые LEAF-категории (конечные предметы).
    # Родительские категории (is_leaf=False) WB API не принимает.
    # Ищем и по subject_name, и по parent_name — чтобы поиск
    # "Товары для взрослых" вернул все leaf-категории этого раздела.
    categories = MarketplaceCategory.query.filter(
        MarketplaceCategory.is_enabled == True,
        MarketplaceCategory.is_leaf == True,
        db.or_(
            MarketplaceCategory.subject_name.ilike(f'%{q}%'),
            MarketplaceCategory.parent_name.ilike(f'%{q}%'),
        )
    ).order_by(
        # Точное совпадение по subject_name выше
        db.case(
            (MarketplaceCategory.subject_name.ilike(f'%{q}%'), 0),
            else_=1
        ),
        MarketplaceCategory.subject_name
    ).limit(limit).all()

    return jsonify({
        'categories': [
            {
                'subject_id': c.subject_id,
                'subject_name': c.subject_name,
                'parent_name': c.parent_name,
                'is_leaf': c.is_leaf,
            }
            for c in categories
        ],
        'count': len(categories),
    })


# ── Характеристики категории ──────────────────────────────────

@internal_api_bp.route('/categories/<int:subject_id>/characteristics', methods=['GET'])
@_authenticate_agent
def internal_get_category_characteristics(subject_id):
    """Получить характеристики категории WB (обязательные/рекомендованные).

    Возвращает список характеристик с типами, допустимыми значениями
    и AI-инструкциями. НЕ раскрывает конфиденциальные данные.

    Параметры:
        required_only: если true — вернуть только обязательные (default: false)
    """
    category = MarketplaceCategory.query.filter_by(subject_id=subject_id).first()
    if not category:
        return jsonify({'error': f'Category {subject_id} not found'}), 404

    required_only = request.args.get('required_only', 'false').lower() == 'true'

    q = MarketplaceCategoryCharacteristic.query.filter_by(
        category_id=category.id,
        is_enabled=True,
    )
    if required_only:
        q = q.filter_by(required=True)

    charcs = q.order_by(
        MarketplaceCategoryCharacteristic.required.desc(),
        MarketplaceCategoryCharacteristic.display_order,
    ).all()

    return jsonify({
        'subject_id': subject_id,
        'subject_name': category.subject_name,
        'characteristics': [
            {
                'charc_id': c.charc_id,
                'name': c.name,
                'type': c.type_label,
                'required': c.required,
                'unit_name': c.unit_name or '',
                'max_count': c.max_count,
                'popular': c.popular,
                'dictionary': json.loads(c.dictionary_json) if c.dictionary_json else None,
                'ai_instruction': c.ai_instruction or '',
                'ai_example_value': c.ai_example_value or '',
            }
            for c in charcs
        ],
        'count': len(charcs),
    })


# ── Справочники (цвета, страны, сезоны) ─────────────────────

@internal_api_bp.route('/directories/<directory_type>', methods=['GET'])
@_authenticate_agent
def internal_get_directory(directory_type):
    """Получить справочник WB (colors, countries, kinds, seasons).

    Параметры:
        q: поисковый запрос для фильтрации (опционально)
        limit: максимум записей (default: 50)
    """
    allowed_types = ('colors', 'countries', 'kinds', 'seasons', 'vat', 'tnved')
    if directory_type not in allowed_types:
        return jsonify({'error': f'Unknown directory type. Allowed: {", ".join(allowed_types)}'}), 400

    directory = MarketplaceDirectory.query.filter_by(
        directory_type=directory_type,
    ).first()

    if not directory or not directory.data_json:
        return jsonify({'error': f'Directory "{directory_type}" not found or empty'}), 404

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

    category_id = request.args.get('category_id', type=int)

    # Точный поиск по алиасам
    normalized = brand_name.strip().lower()
    alias = BrandAlias.query.filter(
        db.func.lower(BrandAlias.alias_normalized) == normalized
    ).first()

    if alias and alias.brand:
        brand = alias.brand
        result = {
            'status': 'found',
            'brand_name': brand.name,
            'confidence': 1.0,
            'source': 'exact_match',
        }

        # Проверяем привязку к маркетплейсу
        mp_brand = MarketplaceBrand.query.filter_by(brand_id=brand.id).first()
        if mp_brand:
            result['marketplace_brand_name'] = mp_brand.marketplace_brand_name
            result['marketplace_brand_id'] = mp_brand.marketplace_brand_id

            # Проверяем доступность в категории
            if category_id:
                link = BrandCategoryLink.query.filter_by(
                    marketplace_brand_id=mp_brand.id,
                    category_id=category_id,
                ).first()
                result['category_available'] = link.is_available if link else None

        return jsonify({'result': result})

    # Нечёткий поиск: ищем похожие бренды
    suggestions = []
    all_aliases = BrandAlias.query.join(Brand).filter(
        Brand.status != 'rejected',
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
        }
    })


# ── Настройки ценообразования ────────────────────────────────

@internal_api_bp.route('/sellers/<int:seller_id>/pricing', methods=['GET'])
@_authenticate_agent
def internal_get_pricing_settings(seller_id):
    """Получить настройки ценообразования продавца.

    БЕЗОПАСНОСТЬ: НЕ возвращает URL файлов поставщика, хеши, user IDs.
    Только формулы и коэффициенты, нужные для расчёта цен.
    """
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
    p = ImportedProduct.query.get(product_id)
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
