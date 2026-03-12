# -*- coding: utf-8 -*-
"""
Internal API — эндпоинты для сервисных агентов (Go-микросервисы).

Все эндпоинты требуют аутентификации через заголовок X-Agent-Key.
Prefix: /internal/v1/
"""
import json
import logging
from functools import wraps

from flask import request, jsonify, abort
from werkzeug.security import check_password_hash

from models import db, ServiceAgent, Product, ImportedProduct, SupplierProduct, Seller
from services import agent_service

logger = logging.getLogger(__name__)


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


def register_internal_api_routes(app):
    """Регистрирует Internal API маршруты."""

    # ── Heartbeat ───────────────────────────────────────────────────

    @app.route('/internal/v1/heartbeat', methods=['POST'])
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

    @app.route('/internal/v1/tasks/poll', methods=['GET'])
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

    @app.route('/internal/v1/tasks/<task_id>/start', methods=['POST'])
    @_authenticate_agent
    def internal_start_task(task_id):
        """Агент берёт задачу в работу."""
        task = agent_service.start_task(task_id)
        if not task:
            return jsonify({'error': 'Task not found or not in queued state'}), 404
        return jsonify({'ok': True, 'task': task.to_dict()})

    @app.route('/internal/v1/tasks/<task_id>/progress', methods=['POST'])
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

    @app.route('/internal/v1/tasks/<task_id>/complete', methods=['POST'])
    @_authenticate_agent
    def internal_complete_task(task_id):
        """Агент завершает задачу успешно."""
        data = request.get_json(silent=True) or {}
        task = agent_service.complete_task(task_id, result_data=data.get('result'))
        if not task:
            return jsonify({'error': 'Task not found'}), 404
        return jsonify({'ok': True, 'task': task.to_dict()})

    @app.route('/internal/v1/tasks/<task_id>/fail', methods=['POST'])
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

    @app.route('/internal/v1/tasks/<task_id>/steps', methods=['POST'])
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

    @app.route('/internal/v1/tasks/<task_id>/steps', methods=['GET'])
    @_authenticate_agent
    def internal_get_steps(task_id):
        """Получить шаги задачи."""
        steps = agent_service.get_task_steps(task_id)
        return jsonify({'steps': [s.to_dict() for s in steps]})

    # ── Данные: товары ──────────────────────────────────────────────

    @app.route('/internal/v1/sellers/<int:seller_id>/products', methods=['GET'])
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

    @app.route('/internal/v1/sellers/<int:seller_id>/products/<int:product_id>', methods=['GET'])
    @_authenticate_agent
    def internal_get_product(seller_id, product_id):
        """Получить конкретный товар."""
        product = Product.query.filter_by(id=product_id, seller_id=seller_id).first()
        if not product:
            return jsonify({'error': 'Product not found'}), 404
        return jsonify({'product': _product_to_dict(product)})

    @app.route('/internal/v1/sellers/<int:seller_id>/products/<int:product_id>', methods=['PATCH'])
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
        ]
        for field in allowed_fields:
            if field in data:
                setattr(product, field, data[field])

        product.updated_at = datetime.utcnow()
        db.session.commit()
        return jsonify({'ok': True, 'product': _product_to_dict(product)})

    @app.route('/internal/v1/sellers/<int:seller_id>/imported-products', methods=['GET'])
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

    # ── Данные: продавцы ────────────────────────────────────────────

    @app.route('/internal/v1/sellers/<int:seller_id>', methods=['GET'])
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


def _product_to_dict(p):
    """Сериализация Product для Internal API."""
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
        'photos': getattr(p, 'photos', None),
    }


def _imported_product_to_dict(p):
    """Сериализация ImportedProduct для Internal API."""
    return {
        'id': p.id,
        'external_id': p.external_id,
        'title': p.title,
        'description': p.description,
        'brand': p.brand,
        'category': p.category,
        'price': p.price,
        'photo_urls': p.photo_urls,
        'characteristics': p.characteristics,
        'supplier_id': p.supplier_id,
    }


# Нужен datetime для update_product
from datetime import datetime
