from flask import render_template, jsonify, request, redirect, url_for, flash
from flask_login import login_required, current_user
import requests
import logging
from datetime import datetime

from models import db
from utils.safe_error import safe_error_message

logger = logging.getLogger(__name__)

# In-memory alert history log (per seller_id)
_alert_history = {}
MAX_HISTORY = 50


class TelegramAlertConfig(db.Model):
    """Telegram alert configuration per seller."""
    __tablename__ = 'telegram_alert_configs'

    id = db.Column(db.Integer, primary_key=True)
    seller_id = db.Column(db.Integer, db.ForeignKey('sellers.id'), nullable=False, unique=True)
    telegram_chat_id = db.Column(db.String(100))
    telegram_bot_token = db.Column(db.String(200))
    is_active = db.Column(db.Boolean, default=False)
    # Alert types
    alert_negative_reviews = db.Column(db.Boolean, default=True)
    alert_low_stock = db.Column(db.Boolean, default=True)
    alert_sales_drop = db.Column(db.Boolean, default=True)
    alert_price_change = db.Column(db.Boolean, default=True)
    alert_order_cancel = db.Column(db.Boolean, default=True)
    # Thresholds
    low_stock_threshold = db.Column(db.Integer, default=5)
    sales_drop_percent = db.Column(db.Integer, default=30)
    # Timestamps
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


ALERT_TYPE_FIELDS = {
    'negative_reviews': 'alert_negative_reviews',
    'low_stock': 'alert_low_stock',
    'sales_drop': 'alert_sales_drop',
    'price_change': 'alert_price_change',
    'order_cancel': 'alert_order_cancel',
}

ALERT_TYPE_LABELS = {
    'negative_reviews': 'Негативные отзывы',
    'low_stock': 'Низкие остатки',
    'sales_drop': 'Падение продаж',
    'price_change': 'Изменения цен',
    'order_cancel': 'Отмены заказов',
}


def _log_alert(seller_id, alert_type, message, status='sent'):
    """Log an alert send to in-memory history."""
    if seller_id not in _alert_history:
        _alert_history[seller_id] = []
    entry = {
        'date': datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'),
        'type': ALERT_TYPE_LABELS.get(alert_type, alert_type),
        'message': message[:200],
        'status': status,
    }
    _alert_history[seller_id].insert(0, entry)
    _alert_history[seller_id] = _alert_history[seller_id][:MAX_HISTORY]


def send_telegram_alert(seller_id, alert_type, message):
    """Send a Telegram alert for the given seller if configured and enabled.

    Returns True if the message was sent, False otherwise.
    """
    config = TelegramAlertConfig.query.filter_by(seller_id=seller_id).first()
    if not config or not config.is_active:
        return False

    # Check if this alert type is enabled
    field = ALERT_TYPE_FIELDS.get(alert_type)
    if field and not getattr(config, field, False):
        return False

    if not config.telegram_bot_token or not config.telegram_chat_id:
        return False

    try:
        url = f"https://api.telegram.org/bot{config.telegram_bot_token}/sendMessage"
        resp = requests.post(url, json={
            'chat_id': config.telegram_chat_id,
            'text': message,
            'parse_mode': 'HTML',
        }, timeout=10)
        resp.raise_for_status()
        _log_alert(seller_id, alert_type, message, 'sent')
        logger.info(f"Telegram alert sent to seller {seller_id}: {alert_type}")
        return True
    except Exception as e:
        logger.error(f"Failed to send Telegram alert to seller {seller_id}: {e}")
        _log_alert(seller_id, alert_type, message, 'error')
        return False


def register_telegram_alerts_routes(app):
    """Register Telegram alerts routes."""

    @app.route('/telegram-alerts')
    @login_required
    def telegram_alerts_page():
        """Telegram alerts configuration page."""
        return render_template('telegram_alerts.html')

    @app.route('/api/telegram-alerts/config', methods=['GET'])
    @login_required
    def api_telegram_alerts_config_get():
        """Return current Telegram alert config for the logged-in seller."""
        if not current_user.seller:
            return jsonify({'error': 'Продавец не найден'}), 403

        config = TelegramAlertConfig.query.filter_by(seller_id=current_user.seller.id).first()
        if not config:
            return jsonify({
                'telegram_chat_id': '',
                'telegram_bot_token': '',
                'is_active': False,
                'alert_negative_reviews': True,
                'alert_low_stock': True,
                'alert_sales_drop': True,
                'alert_price_change': True,
                'alert_order_cancel': True,
                'low_stock_threshold': 5,
                'sales_drop_percent': 30,
            })

        return jsonify({
            'telegram_chat_id': config.telegram_chat_id or '',
            'telegram_bot_token': config.telegram_bot_token or '',
            'is_active': config.is_active,
            'alert_negative_reviews': config.alert_negative_reviews,
            'alert_low_stock': config.alert_low_stock,
            'alert_sales_drop': config.alert_sales_drop,
            'alert_price_change': config.alert_price_change,
            'alert_order_cancel': config.alert_order_cancel,
            'low_stock_threshold': config.low_stock_threshold,
            'sales_drop_percent': config.sales_drop_percent,
        })

    @app.route('/api/telegram-alerts/config', methods=['POST'])
    @login_required
    def api_telegram_alerts_config_save():
        """Save Telegram alert config."""
        if not current_user.seller:
            return jsonify({'error': 'Продавец не найден'}), 403

        data = request.get_json(silent=True)
        if not data:
            return jsonify({'error': 'Нет данных'}), 400

        seller_id = current_user.seller.id
        config = TelegramAlertConfig.query.filter_by(seller_id=seller_id).first()
        if not config:
            config = TelegramAlertConfig(seller_id=seller_id)
            db.session.add(config)

        config.telegram_chat_id = data.get('telegram_chat_id', '').strip()
        config.telegram_bot_token = data.get('telegram_bot_token', '').strip()
        config.is_active = bool(data.get('is_active', False))
        config.alert_negative_reviews = bool(data.get('alert_negative_reviews', True))
        config.alert_low_stock = bool(data.get('alert_low_stock', True))
        config.alert_sales_drop = bool(data.get('alert_sales_drop', True))
        config.alert_price_change = bool(data.get('alert_price_change', True))
        config.alert_order_cancel = bool(data.get('alert_order_cancel', True))
        config.low_stock_threshold = int(data.get('low_stock_threshold', 5))
        config.sales_drop_percent = int(data.get('sales_drop_percent', 30))

        try:
            db.session.commit()
            return jsonify({'success': True, 'message': 'Настройки сохранены'})
        except Exception as e:
            db.session.rollback()
            logger.error(f"Error saving telegram alert config: {e}")
            return jsonify({'error': 'Ошибка сохранения'}), 500

    @app.route('/api/telegram-alerts/test', methods=['POST'])
    @login_required
    def api_telegram_alerts_test():
        """Send a test Telegram message."""
        if not current_user.seller:
            return jsonify({'error': 'Продавец не найден'}), 403

        data = request.get_json(silent=True)
        bot_token = (data or {}).get('bot_token', '').strip()
        chat_id = (data or {}).get('chat_id', '').strip()

        if not bot_token or not chat_id:
            # Try from saved config
            config = TelegramAlertConfig.query.filter_by(seller_id=current_user.seller.id).first()
            if config:
                bot_token = bot_token or config.telegram_bot_token
                chat_id = chat_id or config.telegram_chat_id

        if not bot_token or not chat_id:
            return jsonify({'error': 'Укажите Bot Token и Chat ID'}), 400

        try:
            url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
            resp = requests.post(url, json={
                'chat_id': chat_id,
                'text': '\u2705 Тестовое сообщение от WB Platform',
                'parse_mode': 'HTML',
            }, timeout=10)

            if resp.status_code == 200:
                _log_alert(current_user.seller.id, 'test', 'Тестовое сообщение', 'sent')
                return jsonify({'success': True, 'message': 'Сообщение отправлено'})
            else:
                error_data = resp.json() if resp.headers.get('content-type', '').startswith('application/json') else {}
                description = error_data.get('description', resp.text[:200])
                return jsonify({'error': f'Telegram API ошибка: {description}'}), 400
        except requests.exceptions.Timeout:
            return jsonify({'error': 'Таймаут при отправке сообщения'}), 504
        except Exception as e:
            logger.error(f"Telegram test message error: {e}")
            return jsonify({'error': safe_error_message(e)}), 500

    @app.route('/api/telegram-alerts/history', methods=['GET'])
    @login_required
    def api_telegram_alerts_history():
        """Return last 50 alert log entries for the current seller."""
        if not current_user.seller:
            return jsonify({'error': 'Продавец не найден'}), 403

        history = _alert_history.get(current_user.seller.id, [])
        return jsonify({'history': history[:MAX_HISTORY]})
