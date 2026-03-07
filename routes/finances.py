# -*- coding: utf-8 -*-
"""
Роуты финансов.
API-эндпоинты для дашборда финансов: баланс, расходы, транзакции.
"""
import logging
from flask import render_template, request, jsonify, redirect, url_for, flash
from flask_login import login_required, current_user

from services.finance_service import FinanceService

logger = logging.getLogger('finance_routes')


def register_finance_routes(app):
    """Регистрация роутов финансов"""

    @app.route('/finances')
    @login_required
    def finances_page():
        """Страница финансов"""
        if not current_user.seller or not current_user.seller.has_valid_api_key():
            flash('Для просмотра финансов необходимо настроить API ключ WB', 'warning')
            return redirect(url_for('api_settings'))
        return render_template('finances.html')

    @app.route('/api/finances/summary')
    @login_required
    def api_finances_summary():
        """
        API: Получить финансовую сводку за период.

        Query params:
            period: '7d', '30d', '90d', '1y' (default: '30d')
            force: '1' для принудительного обновления
        """
        if not current_user.seller or not current_user.seller.has_valid_api_key():
            return jsonify({'error': 'API ключ WB не настроен'}), 403

        period = request.args.get('period', '30d')
        if period not in ('7d', '30d', '90d', '1y'):
            period = '30d'
        force = request.args.get('force', '0') == '1'

        try:
            data = FinanceService.fetch_and_cache(
                seller=current_user.seller,
                period_code=period,
                force=force,
            )
            return jsonify({'success': True, 'data': data})
        except Exception as e:
            logger.exception(f"Error fetching finance summary: {e}")
            return jsonify({'error': str(e)}), 500

    @app.route('/api/finances/refresh', methods=['POST'])
    @login_required
    def api_finances_refresh():
        """API: Принудительно обновить финансы из WB API."""
        if not current_user.seller or not current_user.seller.has_valid_api_key():
            return jsonify({'error': 'API ключ WB не настроен'}), 403

        body = request.get_json(silent=True) or {}
        period = body.get('period', '30d')
        if period not in ('7d', '30d', '90d', '1y'):
            period = '30d'

        try:
            data = FinanceService.fetch_and_cache(
                seller=current_user.seller,
                period_code=period,
                force=True,
            )
            return jsonify({'success': True, 'data': data})
        except Exception as e:
            logger.exception(f"Error refreshing finances: {e}")
            return jsonify({'error': str(e)}), 500
