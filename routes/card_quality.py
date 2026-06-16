# -*- coding: utf-8 -*-
"""Роуты фичи «Качество карточек»: кокпит, деталь карточки, AI-анализ, обновление."""
import json
import logging

from flask import render_template, request, redirect, url_for, flash, jsonify
from flask_login import login_required, current_user

from models import db, Product, CardRatingHistory
from services.card_quality_scorer import card_quality_detail
from services import agent_service

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

        scored = [p.quality_score for p in pagination.items if p.quality_score is not None]
        ratings = [p.nm_rating for p in pagination.items if p.nm_rating is not None]
        summary = {
            'avg_quality': round(sum(scored) / len(scored), 1) if scored else None,
            'avg_wb_rating': round(sum(ratings) / len(ratings), 1) if ratings else None,
            'total': pagination.total,
        }
        return jsonify({'success': True, 'items': items, 'summary': summary,
                        'page': page, 'pages': pagination.pages})

    @app.route('/api/card-quality/<int:product_id>')
    @login_required
    def api_card_quality_detail(product_id):
        if not current_user.seller or not current_user.seller.has_valid_api_key():
            return jsonify({'error': 'API ключ WB не настроен'}), 403
        product = Product.query.filter_by(id=product_id, seller_id=current_user.seller.id).first()
        if not product:
            return jsonify({'error': 'Карточка не найдена'}), 404
        detail = card_quality_detail(product)
        trend = CardRatingHistory.query.filter_by(product_id=product_id)\
            .order_by(CardRatingHistory.captured_at.asc()).limit(90).all()
        detail['trend'] = [{
            'captured_at': h.captured_at.isoformat() if h.captured_at else None,
            'wb_product_rating': h.wb_product_rating,
            'quality_score': h.quality_score,
        } for h in trend]
        return jsonify({'success': True, 'data': detail})

    @app.route('/api/card-quality/<int:product_id>/ai-analyze', methods=['POST'])
    @login_required
    def api_card_quality_ai_analyze(product_id):
        if not current_user.seller:
            return jsonify({'error': 'Нет профиля продавца'}), 403
        product = Product.query.filter_by(id=product_id, seller_id=current_user.seller.id).first()
        if not product:
            return jsonify({'error': 'Карточка не найдена'}), 404

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

    @app.route('/api/card-quality/refresh', methods=['POST'])
    @login_required
    def api_card_quality_refresh():
        if not current_user.seller or not current_user.seller.has_valid_api_key():
            return jsonify({'error': 'API ключ WB не настроен'}), 403
        from services.product_sync_scheduler import sync_card_ratings_all_sellers
        import threading
        from flask import current_app
        app_obj = current_app._get_current_object()
        threading.Thread(target=sync_card_ratings_all_sellers, args=(app_obj,), daemon=True).start()
        return jsonify({'success': True, 'message': 'Обновление рейтингов запущено'})
