# -*- coding: utf-8 -*-
"""Роуты фичи «Качество карточек»: кокпит, деталь карточки, AI-анализ, обновление."""
import logging
import threading

from flask import render_template, request, redirect, url_for, flash, jsonify, current_app
from flask_login import login_required, current_user

from models import db, Product, CardRatingHistory, AgentTask
from services.card_quality_scorer import card_quality_detail, compute_quality_summary
from services import agent_service
from services.wb_api_client import WildberriesAPIClient
from services.card_improver import (ALLOWED_FIELDS, apply_card_updates,
                                     collect_weak_dimensions, build_proposal_from_tasks)
from services.supplier_enrichment import get_enrichment_service

logger = logging.getLogger('card_quality')


def register_card_quality_routes(app):
    """Регистрация роутов качества карточек."""

    @app.route('/card-quality')
    @login_required
    def card_quality_page():
        if not current_user.seller or not current_user.seller.has_valid_api_key():
            flash('Для оценки качества карточек необходимо настроить API ключ WB', 'warning')
            return redirect(url_for('api_settings'))
        return render_template('card_quality.html')

    @app.route('/api/card-quality/list')
    @login_required
    def api_card_quality_list():
        if not current_user.seller or not current_user.seller.has_valid_api_key():
            return jsonify({'error': 'API ключ WB не настроен'}), 403
        try:
            sort = request.args.get('sort', 'quality_score')
            order = request.args.get('order', 'asc')
            page = request.args.get('page', 1, type=int)
            per_page = min(request.args.get('per_page', 50, type=int), 200)

            q = Product.query.filter_by(seller_id=current_user.seller.id, is_active=True)
            col = {'quality_score': Product.quality_score, 'nm_rating': Product.nm_rating,
                   'wb_feedback_rating': Product.wb_feedback_rating}.get(sort, Product.quality_score)
            q = q.order_by(col.asc() if order == 'asc' else col.desc())
            pagination = q.paginate(page=page, per_page=per_page, error_out=False)
            items = [card_quality_detail(p) for p in pagination.items]

            summary = compute_quality_summary(current_user.seller.id)
            summary['total'] = pagination.total
            return jsonify({'success': True, 'items': items, 'summary': summary,
                            'page': page, 'pages': pagination.pages})
        except Exception as e:
            logger.exception('Ошибка в api_card_quality_list: %s', e)
            return jsonify({'error': 'Внутренняя ошибка'}), 500

    @app.route('/api/card-quality/summary')
    @login_required
    def api_card_quality_summary():
        if not current_user.seller:
            return jsonify({'error': 'Нет профиля продавца'}), 403
        try:
            data = compute_quality_summary(current_user.seller.id)
            return jsonify({'success': True, 'data': data})
        except Exception as e:
            logger.exception('Ошибка в api_card_quality_summary: %s', e)
            return jsonify({'error': 'Внутренняя ошибка'}), 500

    @app.route('/api/card-quality/<int:product_id>')
    @login_required
    def api_card_quality_detail(product_id):
        if not current_user.seller or not current_user.seller.has_valid_api_key():
            return jsonify({'error': 'API ключ WB не настроен'}), 403
        product = Product.query.filter_by(id=product_id, seller_id=current_user.seller.id).first()
        if not product:
            return jsonify({'error': 'Карточка не найдена'}), 404
        try:
            detail = card_quality_detail(product)
            trend = CardRatingHistory.query.filter_by(product_id=product.id)\
                .order_by(CardRatingHistory.captured_at.asc()).limit(90).all()
            detail['trend'] = [{
                'captured_at': h.captured_at.isoformat() if h.captured_at else None,
                'wb_product_rating': h.wb_product_rating,
                'quality_score': h.quality_score,
            } for h in trend]
            return jsonify({'success': True, 'data': detail})
        except Exception as e:
            logger.exception('Ошибка в api_card_quality_detail: %s', e)
            return jsonify({'error': 'Внутренняя ошибка'}), 500

    @app.route('/api/card-quality/<int:product_id>/ai-analyze', methods=['POST'])
    @login_required
    def api_card_quality_ai_analyze(product_id):
        if not current_user.seller:
            return jsonify({'error': 'Нет профиля продавца'}), 403
        product = Product.query.filter_by(id=product_id, seller_id=current_user.seller.id).first()
        if not product:
            return jsonify({'error': 'Карточка не найдена'}), 404
        try:
            task_ids = {}
            for agent_name, task_type in (('card-doctor', 'diagnose_single'),
                                          ('photo-optimizer', 'optimize_single')):
                agent = agent_service.get_agent_by_name(agent_name)
                if not agent or getattr(agent, 'status', None) != 'online':
                    continue
                task = agent_service.create_task(
                    agent_id=agent.id,
                    seller_id=current_user.seller.id,
                    task_type=task_type,
                    title=f'AI-анализ карточки {product.nm_id}',
                    input_data={'product_id': product.id},
                )
                task_ids[agent_name] = task.id

            if not task_ids:
                return jsonify({'error': 'AI-агенты сейчас офлайн'}), 409
            return jsonify({'success': True, 'task_ids': task_ids})
        except Exception as e:
            logger.exception('Ошибка в api_card_quality_ai_analyze: %s', e)
            return jsonify({'error': 'Внутренняя ошибка'}), 500

    @app.route('/api/card-quality/refresh', methods=['POST'])
    @login_required
    def api_card_quality_refresh():
        if not current_user.seller or not current_user.seller.has_valid_api_key():
            return jsonify({'error': 'API ключ WB не настроен'}), 403
        from services.product_sync_scheduler import sync_card_ratings_for_seller
        app_obj = current_app._get_current_object()
        seller_id = current_user.seller.id
        threading.Thread(target=sync_card_ratings_for_seller, args=(app_obj, seller_id), daemon=True).start()
        return jsonify({'success': True, 'message': 'Обновление рейтингов запущено'})

    @app.route('/api/card-quality/<int:product_id>/improve', methods=['POST'])
    @login_required
    def api_card_quality_improve(product_id):
        if not current_user.seller or not current_user.seller.has_valid_api_key():
            return jsonify({'error': 'API ключ WB не настроен'}), 403
        product = Product.query.filter_by(id=product_id, seller_id=current_user.seller.id).first()
        if not product:
            return jsonify({'error': 'Карточка не найдена'}), 404
        try:
            detail = card_quality_detail(product)
            weak_dims = collect_weak_dimensions(detail)

            # (a) данные поставщика → готовый дифф
            supplier_diff = None
            es = get_enrichment_service()
            imp = es.find_supplier_data(product, current_user.seller.id)
            if imp:
                supplier_diff = es.build_preview(product, imp)

            # (b)/(c) диагностические агенты по product_id (если online)
            task_ids = {}
            for agent_name, task_type in (('photo-optimizer', 'optimize_single'),
                                          ('card-doctor', 'diagnose_single')):
                agent = agent_service.get_agent_by_name(agent_name)
                if not agent or getattr(agent, 'status', None) != 'online':
                    continue
                task = agent_service.create_task(
                    agent_id=agent.id,
                    seller_id=current_user.seller.id,
                    task_type=task_type,
                    title=f'Улучшение карточки {product.nm_id}',
                    input_data={'product_id': product.id},
                )
                task_ids[agent_name] = task.id

            return jsonify({'success': True, 'weak_dims': weak_dims,
                            'supplier_diff': supplier_diff, 'task_ids': task_ids})
        except Exception as e:
            logger.exception('Ошибка в api_card_quality_improve: %s', e)
            return jsonify({'error': 'Внутренняя ошибка'}), 500

    @app.route('/api/card-quality/<int:product_id>/proposal', methods=['POST'])
    @login_required
    def api_card_quality_proposal(product_id):
        if not current_user.seller or not current_user.seller.has_valid_api_key():
            return jsonify({'error': 'API ключ WB не настроен'}), 403
        product = Product.query.filter_by(id=product_id, seller_id=current_user.seller.id).first()
        if not product:
            return jsonify({'error': 'Карточка не найдена'}), 404
        try:
            body = request.get_json(silent=True) or {}
            task_ids = body.get('task_ids') or {}

            task_results = []
            for agent_name, task_id in task_ids.items():
                task = AgentTask.query.filter_by(id=task_id, seller_id=current_user.seller.id).first()
                if task and task.status == 'completed':
                    task_results.append({'agent': agent_name, 'result': task.get_result()})

            proposal = build_proposal_from_tasks(product, task_results)

            supplier_diff = None
            es = get_enrichment_service()
            imp = es.find_supplier_data(product, current_user.seller.id)
            if imp:
                supplier_diff = es.build_preview(product, imp)

            return jsonify({'success': True, 'proposal': proposal, 'supplier_diff': supplier_diff})
        except Exception as e:
            logger.exception('Ошибка в api_card_quality_proposal: %s', e)
            return jsonify({'error': 'Внутренняя ошибка'}), 500

    @app.route('/api/card-quality/<int:product_id>/apply', methods=['POST'])
    @login_required
    def api_card_quality_apply(product_id):
        if not current_user.seller or not current_user.seller.has_valid_api_key():
            return jsonify({'error': 'API ключ WB не настроен'}), 403
        product = Product.query.filter_by(id=product_id, seller_id=current_user.seller.id).first()
        if not product:
            return jsonify({'error': 'Карточка не найдена'}), 404

        body = request.get_json(silent=True) or {}
        raw_updates = body.get('updates') or {}
        updates = {k: v for k, v in raw_updates.items() if k in ALLOWED_FIELDS}
        if not updates:
            return jsonify({'error': 'Нет допустимых полей для применения'}), 400

        try:
            wb_client = WildberriesAPIClient(current_user.seller.wb_api_key)
            res = apply_card_updates(product, updates, current_user.seller, wb_client,
                                     source='card-quality')
            status = 200 if res.get('success') else 422
            return jsonify({
                'success': res.get('success', False),
                'fields_applied': res.get('fields_applied', []),
                'old_quality': res.get('old_quality'),
                'new_quality': res.get('new_quality'),
                'wb_sync': res.get('wb_sync', False),
                'error': res.get('error'),
            }), status
        except Exception as e:
            logger.exception('Ошибка в api_card_quality_apply: %s', e)
            return jsonify({'error': 'Внутренняя ошибка'}), 500
