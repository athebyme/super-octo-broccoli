# -*- coding: utf-8 -*-
"""
Роуты аналитики продаж.
API-эндпоинты для получения KPI, графиков, детализации по товарам.
"""
import logging
from flask import render_template, request, jsonify, redirect, url_for, flash
from flask_login import login_required, current_user

from models import db
from services.analytics_service import AnalyticsService
from utils.safe_error import safe_error_message

logger = logging.getLogger('analytics_routes')


def register_analytics_routes(app):
    """Регистрация роутов аналитики"""

    @app.route('/analytics')
    @login_required
    def analytics_page():
        """Страница аналитики продаж"""
        if not current_user.seller or not current_user.seller.has_valid_api_key():
            flash('Для просмотра аналитики необходимо настроить API ключ WB', 'warning')
            return redirect(url_for('api_settings'))
        return render_template('analytics.html')

    @app.route('/api/analytics/summary')
    @login_required
    def api_analytics_summary():
        """
        API: Получить агрегированную аналитику за период.

        Query params:
            period: '7d', '30d', '90d', '1y' (default: '30d')
            force: '1' для принудительного обновления кэша
        """
        if not current_user.seller or not current_user.seller.has_valid_api_key():
            return jsonify({'error': 'API ключ WB не настроен'}), 403

        period = request.args.get('period', '30d')
        if period not in ('7d', '30d', '90d', '1y'):
            period = '30d'
        force = request.args.get('force', '0') == '1'

        try:
            data = AnalyticsService.fetch_and_cache_snapshot(
                seller=current_user.seller,
                period_code=period,
                force=force,
            )
            return jsonify({'success': True, 'data': data})
        except Exception as e:
            logger.exception(f"Error fetching analytics summary: {e}")
            return jsonify({'error': safe_error_message(e)}), 500

    @app.route('/api/analytics/daily')
    @login_required
    def api_analytics_daily():
        """
        API: Получить дневную статистику для графиков.

        Query params:
            period: '7d', '30d', '90d', '1y'
        """
        if not current_user.seller or not current_user.seller.has_valid_api_key():
            return jsonify({'error': 'API ключ WB не настроен'}), 403

        period = request.args.get('period', '30d')
        if period not in ('7d', '30d', '90d', '1y'):
            period = '30d'

        try:
            daily = AnalyticsService.update_snapshot_daily_data(
                seller=current_user.seller,
                period_code=period,
            )
            return jsonify({'success': True, 'data': daily or []})
        except Exception as e:
            logger.exception(f"Error fetching daily analytics: {e}")
            return jsonify({'error': safe_error_message(e)}), 500

    @app.route('/api/analytics/products')
    @login_required
    def api_analytics_products():
        """
        API: Получить аналитику по отдельным товарам.

        Query params:
            period: '7d', '30d', '90d', '1y'
            sort_by: Поле сортировки (orders_sum, orders_count, buyouts_sum, etc.)
            sort_dir: 'asc' / 'desc'
            search: Поиск по названию, артикулу, бренду
            page: Номер страницы
            per_page: Количество на странице
        """
        if not current_user.seller:
            return jsonify({'error': 'Нет привязки к продавцу'}), 403

        period = request.args.get('period', '30d')
        sort_by = request.args.get('sort_by', 'orders_sum')
        sort_dir = request.args.get('sort_dir', 'desc')
        search = request.args.get('search', '').strip()
        page = request.args.get('page', 1, type=int)
        per_page = request.args.get('per_page', 20, type=int)

        allowed_sorts = [
            'orders_sum', 'orders_count', 'buyouts_sum', 'buyouts_count',
            'open_card_count', 'add_to_cart_count', 'cancel_count',
        ]
        if sort_by not in allowed_sorts:
            sort_by = 'orders_sum'
        if sort_dir not in ('asc', 'desc'):
            sort_dir = 'desc'

        try:
            data = AnalyticsService.get_product_analytics_list(
                seller_id=current_user.seller.id,
                period_code=period,
                sort_by=sort_by,
                sort_dir=sort_dir,
                search=search,
                page=page,
                per_page=per_page,
            )
            return jsonify({'success': True, 'data': data})
        except Exception as e:
            logger.exception(f"Error fetching product analytics: {e}")
            return jsonify({'error': safe_error_message(e)}), 500

    @app.route('/api/analytics/refresh', methods=['POST'])
    @login_required
    def api_analytics_refresh():
        """
        API: Принудительно обновить аналитику из WB API.

        Body JSON:
            period: '7d', '30d', '90d', '1y'
        """
        if not current_user.seller or not current_user.seller.has_valid_api_key():
            return jsonify({'error': 'API ключ WB не настроен'}), 403

        body = request.get_json(silent=True) or {}
        period = body.get('period', '30d')
        if period not in ('7d', '30d', '90d', '1y'):
            period = '30d'

        try:
            data = AnalyticsService.fetch_and_cache_snapshot(
                seller=current_user.seller,
                period_code=period,
                force=True,
            )
            return jsonify({'success': True, 'data': data})
        except Exception as e:
            logger.exception(f"Error refreshing analytics: {e}")
            return jsonify({'error': safe_error_message(e)}), 500
