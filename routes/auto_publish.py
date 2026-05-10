# -*- coding: utf-8 -*-
"""
Роуты автопубликации товаров на маркетплейсы.
Настройки, запуски, история, ручной trigger, retry.
"""
import json
import logging
import threading
from datetime import datetime

from flask import render_template, request, jsonify, redirect, url_for, flash, current_app
from flask_login import login_required, current_user

from models import (
    db, AutoPublishSettings, AutoPublishRun, AutoPublishItem,
    ImportedProduct, SellerSupplier, Supplier, Notification,
)

logger = logging.getLogger('auto_publish_routes')


def _parse_bool(v):
    """Безопасный парсинг булевых из form-data/JSON.
    Решает проблему bool('false') == True для строковых значений из form."""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if v is None:
        return False
    return str(v).strip().lower() in ('1', 'true', 'on', 'yes')


def _parse_int(data, key, default, min_value, max_value):
    try:
        value = int(data.get(key, default))
    except (TypeError, ValueError):
        value = default
    return max(min_value, min(max_value, value))


def register_auto_publish_routes(app):
    """Регистрация роутов автопубликации"""

    def _get_seller():
        if current_user.seller:
            return current_user.seller
        return None

    def _get_or_create_settings(seller_id):
        settings = AutoPublishSettings.query.filter_by(seller_id=seller_id).first()
        if not settings:
            settings = AutoPublishSettings(seller_id=seller_id)
            db.session.add(settings)
            db.session.commit()
        return settings

    # ========================= СТРАНИЦА НАСТРОЕК =========================

    @app.route('/auto-publish')
    @login_required
    def auto_publish_settings():
        """Страница настроек и истории автопубликации"""
        seller = _get_seller()
        if not seller:
            flash('Необходимо настроить магазин', 'warning')
            return redirect(url_for('dashboard'))

        settings = _get_or_create_settings(seller.id)

        # Привязанные поставщики
        seller_suppliers = SellerSupplier.query.filter_by(seller_id=seller.id).all()
        suppliers = []
        for ss in seller_suppliers:
            s = Supplier.query.get(ss.supplier_id)
            if s:
                suppliers.append({'id': s.id, 'name': s.name, 'code': s.code})

        # Последние запуски
        runs_query = AutoPublishRun.query.filter_by(
            seller_id=seller.id
        ).order_by(AutoPublishRun.created_at.desc()).limit(20).all()
        runs_data = [r.to_dict() for r in runs_query]

        # Статистика
        pending_count = ImportedProduct.query.filter_by(
            seller_id=seller.id, import_status='validated'
        ).count()

        return render_template(
            'auto_publish.html',
            settings=settings,
            suppliers=suppliers,
            runs_data=runs_data,
            pending_count=pending_count,
        )

    # ========================= API: НАСТРОЙКИ =========================

    @app.route('/api/auto-publish/settings', methods=['GET'])
    @login_required
    def api_auto_publish_get_settings():
        seller = _get_seller()
        if not seller:
            return jsonify({'error': 'Seller not found'}), 404

        settings = _get_or_create_settings(seller.id)
        return jsonify(settings.to_dict())

    @app.route('/api/auto-publish/settings', methods=['POST'])
    @login_required
    def api_auto_publish_save_settings():
        seller = _get_seller()
        if not seller:
            return jsonify({'error': 'Seller not found'}), 404

        settings = _get_or_create_settings(seller.id)
        data = request.get_json() or request.form

        # Обновляем поля
        if 'marketplace_code' in data:
            settings.marketplace_code = data['marketplace_code']
        if 'check_interval_minutes' in data:
            settings.check_interval_minutes = _parse_int(data, 'check_interval_minutes', 30, 15, 180)
        if 'batch_size' in data:
            settings.batch_size = _parse_int(data, 'batch_size', 10, 1, 50)
        if 'max_daily_publishes' in data:
            settings.max_daily_publishes = _parse_int(data, 'max_daily_publishes', 100, 1, 500)
        if 'validation_mode' in data:
            if data['validation_mode'] in ('strict', 'lenient'):
                settings.validation_mode = data['validation_mode']
        if 'max_retries_per_product' in data:
            settings.max_retries_per_product = _parse_int(data, 'max_retries_per_product', 3, 0, 10)
        if 'retry_delay_minutes' in data:
            settings.retry_delay_minutes = _parse_int(data, 'retry_delay_minutes', 60, 15, 360)
        if 'failure_threshold' in data:
            settings.failure_threshold = _parse_int(data, 'failure_threshold', 5, 1, 20)
        if 'supplier_ids' in data:
            supplier_ids = data['supplier_ids']
            if supplier_ids and isinstance(supplier_ids, list) and len(supplier_ids) > 0:
                cleaned_supplier_ids = [int(sid) for sid in supplier_ids if str(sid).isdigit()]
                settings.supplier_ids_json = json.dumps(cleaned_supplier_ids) if cleaned_supplier_ids else None
            else:
                settings.supplier_ids_json = None
        if 'notify_on_success' in data:
            settings.notify_on_success = _parse_bool(data['notify_on_success'])
        if 'notify_on_failure' in data:
            settings.notify_on_failure = _parse_bool(data['notify_on_failure'])
        if 'notify_on_pause' in data:
            settings.notify_on_pause = _parse_bool(data['notify_on_pause'])

        settings.updated_at = datetime.utcnow()
        db.session.commit()

        return jsonify({'ok': True, 'settings': settings.to_dict()})

    @app.route('/api/auto-publish/toggle', methods=['POST'])
    @login_required
    def api_auto_publish_toggle():
        seller = _get_seller()
        if not seller:
            return jsonify({'error': 'Seller not found'}), 404

        settings = _get_or_create_settings(seller.id)

        # Проверяем наличие WB API ключа
        has_api_key = seller.has_valid_api_key() if hasattr(seller, 'has_valid_api_key') else bool(seller.wb_api_key)
        if not settings.is_enabled and not has_api_key:
            return jsonify({'error': 'Сначала настройте API ключ WB'}), 400

        settings.is_enabled = not settings.is_enabled
        if settings.is_enabled:
            settings.is_paused = False
            settings.paused_reason = None
            settings.paused_at = None
        settings.updated_at = datetime.utcnow()
        db.session.commit()

        return jsonify({
            'ok': True,
            'is_enabled': settings.is_enabled,
        })

    @app.route('/api/auto-publish/pause', methods=['POST'])
    @login_required
    def api_auto_publish_pause():
        seller = _get_seller()
        if not seller:
            return jsonify({'error': 'Seller not found'}), 404

        settings = _get_or_create_settings(seller.id)
        settings.is_paused = True
        settings.paused_reason = 'Приостановлено вручную'
        settings.paused_at = datetime.utcnow()
        db.session.commit()

        return jsonify({'ok': True})

    @app.route('/api/auto-publish/resume', methods=['POST'])
    @login_required
    def api_auto_publish_resume():
        seller = _get_seller()
        if not seller:
            return jsonify({'error': 'Seller not found'}), 404

        settings = _get_or_create_settings(seller.id)
        settings.is_paused = False
        settings.paused_reason = None
        settings.paused_at = None
        settings.updated_at = datetime.utcnow()
        db.session.commit()

        return jsonify({'ok': True})

    # ========================= API: ЗАПУСКИ =========================

    @app.route('/api/auto-publish/runs', methods=['GET'])
    @login_required
    def api_auto_publish_runs():
        seller = _get_seller()
        if not seller:
            return jsonify({'error': 'Seller not found'}), 404

        page = request.args.get('page', 1, type=int)
        per_page = min(request.args.get('per_page', 20, type=int), 50)
        status = request.args.get('status')

        query = AutoPublishRun.query.filter_by(seller_id=seller.id)
        if status:
            query = query.filter_by(status=status)

        runs = query.order_by(AutoPublishRun.created_at.desc()).paginate(
            page=page, per_page=per_page, error_out=False
        )

        return jsonify({
            'runs': [r.to_dict() for r in runs.items],
            'total': runs.total,
            'page': runs.page,
            'pages': runs.pages,
        })

    @app.route('/api/auto-publish/runs/<int:run_id>', methods=['GET'])
    @login_required
    def api_auto_publish_run_detail(run_id):
        seller = _get_seller()
        if not seller:
            return jsonify({'error': 'Seller not found'}), 404

        run = AutoPublishRun.query.filter_by(
            id=run_id, seller_id=seller.id
        ).first()
        if not run:
            return jsonify({'error': 'Run not found'}), 404

        return jsonify(run.to_dict(include_items=True))

    @app.route('/api/auto-publish/runs/<int:run_id>/items', methods=['GET'])
    @login_required
    def api_auto_publish_run_items(run_id):
        seller = _get_seller()
        if not seller:
            return jsonify({'error': 'Seller not found'}), 404

        run = AutoPublishRun.query.filter_by(
            id=run_id, seller_id=seller.id
        ).first()
        if not run:
            return jsonify({'error': 'Run not found'}), 404

        status = request.args.get('status')
        query = AutoPublishItem.query.filter_by(run_id=run.id)
        if status:
            query = query.filter_by(status=status)

        items = query.order_by(AutoPublishItem.id.asc()).all()
        return jsonify({
            'items': [i.to_dict() for i in items],
            'total': len(items),
        })

    # ========================= API: РУЧНОЙ TRIGGER =========================

    @app.route('/api/auto-publish/trigger', methods=['POST'])
    @login_required
    def api_auto_publish_trigger():
        seller = _get_seller()
        if not seller:
            return jsonify({'error': 'Seller not found'}), 404

        has_api_key = seller.has_valid_api_key() if hasattr(seller, 'has_valid_api_key') else bool(seller.wb_api_key)
        if not has_api_key:
            return jsonify({'error': 'API ключ WB не настроен'}), 400

        settings = _get_or_create_settings(seller.id)

        if settings._run_lock_token:
            return jsonify({'error': 'Уже есть активный запуск'}), 409

        # Проверяем нет ли активного запуска
        active = AutoPublishRun.query.filter_by(
            seller_id=seller.id, status='running'
        ).first()
        if active:
            return jsonify({'error': 'Уже есть активный запуск', 'run_id': active.id}), 409

        # Запускаем в отдельном потоке
        flask_app = current_app._get_current_object()
        thread = threading.Thread(
            target=_manual_trigger,
            args=(flask_app, seller.id, settings.id),
            name=f'auto-publish-manual-{seller.id}',
            daemon=True,
        )
        thread.start()

        return jsonify({'ok': True, 'message': 'Запуск начат'})

    @app.route('/api/auto-publish/runs/<int:run_id>/cancel', methods=['POST'])
    @login_required
    def api_auto_publish_cancel(run_id):
        seller = _get_seller()
        if not seller:
            return jsonify({'error': 'Seller not found'}), 404

        run = AutoPublishRun.query.filter_by(
            id=run_id, seller_id=seller.id
        ).first()
        if not run:
            return jsonify({'error': 'Run not found'}), 404

        if run.status != 'running':
            return jsonify({'error': 'Запуск не активен'}), 400

        run.status = 'cancelled'
        run.completed_at = datetime.utcnow()
        if run.started_at:
            run.duration_seconds = (run.completed_at - run.started_at).total_seconds()

        # Разблокируем ещё не начатые items. Processing item не прерываем
        # посередине WB-запроса; сервис увидит cancelled перед следующим item.
        pending_items = AutoPublishItem.query.filter_by(run_id=run.id, status='pending').all()
        for item in pending_items:
            item.status = 'skipped'
            item.step = 'skipped'
            item.error_message = 'Отменено пользователем'
            item.completed_at = datetime.utcnow()
            product = ImportedProduct.query.get(item.imported_product_id)
            if product and product.import_status == 'publishing':
                product.import_status = 'validated'
            run.total_skipped += 1

        db.session.commit()
        return jsonify({'ok': True})

    # ========================= API: RETRY =========================

    @app.route('/api/auto-publish/items/<int:item_id>/retry', methods=['POST'])
    @login_required
    def api_auto_publish_retry_item(item_id):
        seller = _get_seller()
        if not seller:
            return jsonify({'error': 'Seller not found'}), 404

        item = AutoPublishItem.query.filter_by(
            id=item_id, seller_id=seller.id
        ).first()
        if not item:
            return jsonify({'error': 'Item not found'}), 404

        if item.status not in ('failed', 'skipped'):
            return jsonify({'error': 'Повторить можно только failed/skipped элементы'}), 400

        # Сбрасываем товар в validated для подхвата следующим запуском
        product = ImportedProduct.query.get(item.imported_product_id)
        if product:
            product.import_status = 'validated'
            product.import_error = None

        # Сбрасываем retry счётчик
        item.retry_count = 0
        item.next_retry_at = None
        db.session.commit()

        return jsonify({'ok': True, 'message': 'Товар будет обработан в следующем запуске'})

    # ========================= API: СТАТУС =========================

    @app.route('/api/auto-publish/status', methods=['GET'])
    @login_required
    def api_auto_publish_status():
        seller = _get_seller()
        if not seller:
            return jsonify({'error': 'Seller not found'}), 404

        settings = AutoPublishSettings.query.filter_by(seller_id=seller.id).first()

        last_run = AutoPublishRun.query.filter_by(
            seller_id=seller.id
        ).order_by(AutoPublishRun.created_at.desc()).first()

        pending_count = ImportedProduct.query.filter_by(
            seller_id=seller.id, import_status='validated'
        ).count()

        return jsonify({
            'enabled': settings.is_enabled if settings else False,
            'is_paused': settings.is_paused if settings else False,
            'paused_reason': settings.paused_reason if settings else None,
            'last_run': last_run.to_dict() if last_run else None,
            'daily_published': settings.daily_published_count if settings else 0,
            'daily_limit': settings.max_daily_publishes if settings else 100,
            'next_run_at': settings.next_run_at.isoformat() if settings and settings.next_run_at else None,
            'pending_candidates': pending_count,
        })


def _manual_trigger(flask_app, seller_id, settings_id):
    """Выполняет ручной запуск автопубликации."""
    with flask_app.app_context():
        try:
            from models import Seller
            from services.auto_publish_service import AutoPublishService

            seller = db.session.get(Seller, seller_id)
            settings = db.session.get(AutoPublishSettings, settings_id)
            if not seller or not settings:
                return

            service = AutoPublishService(seller, settings)
            service.execute_run(triggered_by='manual')
        except Exception as e:
            logger.error(f"Ошибка ручного запуска для seller {seller_id}: {e}", exc_info=True)
