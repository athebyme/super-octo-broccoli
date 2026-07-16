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
from sqlalchemy.exc import IntegrityError

from models import (
    db, AutoPublishSettings, AutoPublishRun, AutoPublishItem,
    ImportedProduct, Marketplace, MarketplaceOperation,
    SellerMarketplaceAccount,
    SellerSupplier, Supplier, Notification,
)

logger = logging.getLogger('auto_publish_routes')


class _AutoPublishScopeError(RuntimeError):
    pass


class _AutoPublishValidationError(RuntimeError):
    pass


def _parse_bool(v):
    """Strict boolean parser for JSON and form-data."""
    if isinstance(v, bool):
        return v
    if isinstance(v, int) and not isinstance(v, bool) and v in (0, 1):
        return bool(v)
    if isinstance(v, str):
        normalized = v.strip().lower()
        if normalized in ('1', 'true', 'on', 'yes'):
            return True
        if normalized in ('0', 'false', 'off', 'no'):
            return False
    raise _AutoPublishValidationError('Boolean-поле имеет неизвестный формат')


def _parse_int(data, key, default, min_value, max_value):
    raw = data.get(key, default)
    if isinstance(raw, bool) or isinstance(raw, float):
        raise _AutoPublishValidationError(
            f'{key} должен быть целым числом'
        )
    if isinstance(raw, int):
        value = raw
    elif isinstance(raw, str) and raw.isascii() and raw.isdigit():
        value = int(raw)
    else:
        raise _AutoPublishValidationError(
            f'{key} должен быть целым числом'
        )
    if not min_value <= value <= max_value:
        raise _AutoPublishValidationError(
            f'{key} должен быть от {min_value} до {max_value}'
        )
    return value


def register_auto_publish_routes(app):
    """Регистрация роутов автопубликации"""

    def _get_seller():
        if current_user.seller:
            return current_user.seller
        return None

    def _ozon_auto_write_enabled():
        return bool(
            current_app.config.get('MARKETPLACE_OZON_ENABLED', False)
            and current_app.config.get(
                'MARKETPLACE_OZON_PUBLICATION_ENABLED', False
            )
            and current_app.config.get(
                'MARKETPLACE_OZON_AUTO_PUBLISH_ENABLED', False
            )
        )

    def _available_targets(seller):
        targets = [{
            'marketplace_code': 'wb',
            'account_id': None,
            'label': 'Wildberries · основной магазин',
            'connection_status': (
                'connected' if seller.has_valid_api_key() else 'not_configured'
            ),
        }]
        if not current_app.config.get('MARKETPLACE_OZON_ENABLED', False):
            return targets
        accounts = SellerMarketplaceAccount.query.join(Marketplace).filter(
            SellerMarketplaceAccount.seller_id == seller.id,
            Marketplace.code == 'ozon',
            Marketplace.is_active.is_(True),
        ).order_by(
            SellerMarketplaceAccount.is_default.desc(),
            SellerMarketplaceAccount.label.asc(),
            SellerMarketplaceAccount.id.asc(),
        ).all()
        targets.extend({
            'marketplace_code': 'ozon',
            'account_id': account.id,
            'label': f'Ozon · {account.label}',
            'connection_status': account.connection_status,
        } for account in accounts)
        return targets

    def _scope_from_request():
        marketplace_code = (request.args.get('marketplace') or 'wb').strip().lower()
        if marketplace_code not in {'wb', 'ozon'}:
            raise _AutoPublishScopeError('Неизвестный маркетплейс')
        raw_account_id = request.args.get('account_id')
        if marketplace_code == 'wb':
            if raw_account_id not in (None, ''):
                raise _AutoPublishScopeError('WB scope не принимает account_id')
            return 'wb', None
        if not current_app.config.get('MARKETPLACE_OZON_ENABLED', False):
            raise _AutoPublishScopeError('Ozon выключен rollout-флагом')
        if not raw_account_id or not raw_account_id.isascii() or not raw_account_id.isdigit():
            raise _AutoPublishScopeError('Для Ozon выберите кабинет')
        account_id = int(raw_account_id)
        if account_id <= 0:
            raise _AutoPublishScopeError('Для Ozon выберите кабинет')
        return 'ozon', account_id

    def _get_or_create_settings(seller, marketplace_code, account_id):
        query = AutoPublishSettings.query.filter_by(
            seller_id=seller.id,
            marketplace_code=marketplace_code,
        )
        if account_id is None:
            query = query.filter(AutoPublishSettings.account_id.is_(None))
        else:
            account = SellerMarketplaceAccount.query.join(Marketplace).filter(
                SellerMarketplaceAccount.id == account_id,
                SellerMarketplaceAccount.seller_id == seller.id,
                Marketplace.code == 'ozon',
                Marketplace.is_active.is_(True),
            ).first()
            if account is None:
                raise _AutoPublishScopeError('Кабинет Ozon не найден')
            query = query.filter(AutoPublishSettings.account_id == account.id)
        settings = query.first()
        if not settings:
            settings = AutoPublishSettings(
                seller_id=seller.id,
                marketplace_code=marketplace_code,
                account_id=account_id,
            )
            db.session.add(settings)
            try:
                db.session.commit()
            except IntegrityError:
                db.session.rollback()
                settings = query.first()
                if settings is None:
                    raise
        return settings

    def _settings_for_request(seller):
        marketplace_code, account_id = _scope_from_request()
        return _get_or_create_settings(
            seller,
            marketplace_code,
            account_id,
        )

    def _runs_for_scope(seller, settings):
        query = AutoPublishRun.query.filter(
            AutoPublishRun.seller_id == seller.id,
            AutoPublishRun.settings_id == settings.id,
            AutoPublishRun.marketplace_code == settings.marketplace_code,
        )
        if settings.account_id is None:
            return query.filter(AutoPublishRun.account_id.is_(None))
        return query.filter(AutoPublishRun.account_id == settings.account_id)

    def _items_for_run_scope(seller, settings, run):
        query = AutoPublishItem.query.filter(
            AutoPublishItem.run_id == run.id,
            AutoPublishItem.seller_id == seller.id,
            AutoPublishItem.marketplace_code == settings.marketplace_code,
        )
        if settings.account_id is None:
            return query.filter(AutoPublishItem.account_id.is_(None))
        return query.filter(AutoPublishItem.account_id == settings.account_id)

    # ========================= СТРАНИЦА НАСТРОЕК =========================

    @app.route('/auto-publish')
    @login_required
    def auto_publish_settings():
        """Страница настроек и истории автопубликации"""
        seller = _get_seller()
        if not seller:
            flash('Необходимо настроить магазин', 'warning')
            return redirect(url_for('dashboard'))

        try:
            marketplace_code, account_id = _scope_from_request()
            settings = _get_or_create_settings(
                seller, marketplace_code, account_id
            )
        except _AutoPublishScopeError as exc:
            flash(str(exc), 'warning')
            return redirect(url_for('auto_publish_settings'))

        # Привязанные поставщики
        seller_suppliers = SellerSupplier.query.filter_by(seller_id=seller.id).all()
        suppliers = []
        for ss in seller_suppliers:
            s = Supplier.query.get(ss.supplier_id)
            if s:
                suppliers.append({'id': s.id, 'name': s.name, 'code': s.code})

        # Последние запуски
        runs_query = _runs_for_scope(seller, settings).order_by(
            AutoPublishRun.created_at.desc()
        ).limit(20).all()
        runs_data = [r.to_dict() for r in runs_query]

        # Статистика
        if settings.marketplace_code == 'ozon':
            from services.marketplace_auto_publish import (
                MarketplaceAutoPublishError,
                OzonAutoPublishService,
            )

            try:
                pending_count = OzonAutoPublishService(
                    seller, settings
                ).pending_candidate_count()
            except MarketplaceAutoPublishError as exc:
                pending_count = 0
                flash(str(exc), 'warning')
        else:
            pending_count = ImportedProduct.query.filter_by(
                seller_id=seller.id, import_status='validated'
            ).count()

        return render_template(
            'auto_publish.html',
            settings=settings,
            suppliers=suppliers,
            runs_data=runs_data,
            pending_count=pending_count,
            targets=_available_targets(seller),
            selected_marketplace=marketplace_code,
            selected_account_id=account_id,
            ozon_auto_publish_enabled=_ozon_auto_write_enabled(),
        )

    # ========================= API: НАСТРОЙКИ =========================

    @app.route('/api/auto-publish/settings', methods=['GET'])
    @login_required
    def api_auto_publish_get_settings():
        seller = _get_seller()
        if not seller:
            return jsonify({'error': 'Seller not found'}), 404

        try:
            settings = _settings_for_request(seller)
        except _AutoPublishScopeError as exc:
            return jsonify({'error': str(exc)}), 404
        return jsonify(settings.to_dict())

    @app.route('/api/auto-publish/settings', methods=['POST'])
    @login_required
    def api_auto_publish_save_settings():
        seller = _get_seller()
        if not seller:
            return jsonify({'error': 'Seller not found'}), 404

        try:
            settings = _settings_for_request(seller)
        except _AutoPublishScopeError as exc:
            return jsonify({'error': str(exc)}), 404
        data = request.get_json() if request.is_json else request.form
        if not hasattr(data, 'keys'):
            return jsonify({'error': 'Body должен быть JSON object'}), 400
        allowed_fields = {
            'check_interval_minutes', 'batch_size', 'max_daily_publishes',
            'validation_mode', 'max_retries_per_product',
            'retry_delay_minutes', 'failure_threshold', 'supplier_ids',
            'notify_on_success', 'notify_on_failure', 'notify_on_pause',
        }
        unknown_fields = set(data.keys()) - allowed_fields
        if 'marketplace_code' in unknown_fields or 'account_id' in unknown_fields:
            return jsonify({
                'error': 'marketplace/account задаются только selector scope'
            }), 400
        if unknown_fields:
            return jsonify({
                'error': 'Body содержит неизвестные поля',
                'fields': sorted(unknown_fields),
            }), 400
        try:
            if 'check_interval_minutes' in data:
                settings.check_interval_minutes = _parse_int(
                    data, 'check_interval_minutes', 30, 15, 180
                )
            if 'batch_size' in data:
                settings.batch_size = _parse_int(
                    data, 'batch_size', 10, 1, 50
                )
            if 'max_daily_publishes' in data:
                settings.max_daily_publishes = _parse_int(
                    data, 'max_daily_publishes', 100, 1, 500
                )
            if 'validation_mode' in data:
                validation_mode = data['validation_mode']
                if validation_mode not in ('strict', 'lenient'):
                    raise _AutoPublishValidationError(
                        'validation_mode имеет неизвестное значение'
                    )
                if settings.marketplace_code == 'ozon' and validation_mode != 'strict':
                    raise _AutoPublishValidationError(
                        'Ozon auto-publish всегда использует strict validation'
                    )
                settings.validation_mode = validation_mode
            if 'max_retries_per_product' in data:
                settings.max_retries_per_product = _parse_int(
                    data, 'max_retries_per_product', 3, 0, 10
                )
            if 'retry_delay_minutes' in data:
                settings.retry_delay_minutes = _parse_int(
                    data, 'retry_delay_minutes', 60, 15, 360
                )
            if 'failure_threshold' in data:
                settings.failure_threshold = _parse_int(
                    data, 'failure_threshold', 5, 1, 20
                )
            if 'supplier_ids' in data:
                supplier_ids = data['supplier_ids']
                if not isinstance(supplier_ids, list):
                    raise _AutoPublishValidationError(
                        'supplier_ids должен быть массивом'
                    )
                if len(supplier_ids) > 200:
                    raise _AutoPublishValidationError(
                        'Слишком много поставщиков'
                    )
                if any(
                    not isinstance(sid, int)
                    or isinstance(sid, bool)
                    or sid <= 0
                    for sid in supplier_ids
                ) or len(set(supplier_ids)) != len(supplier_ids):
                    raise _AutoPublishValidationError(
                        'supplier_ids должен содержать уникальные integer ID'
                    )
                owned = {
                    row.supplier_id for row in SellerSupplier.query.filter(
                        SellerSupplier.seller_id == seller.id,
                        SellerSupplier.supplier_id.in_(supplier_ids),
                    ).all()
                }
                if owned != set(supplier_ids):
                    db.session.rollback()
                    return jsonify({
                        'error': 'Поставщик не подключён к seller'
                    }), 404
                settings.supplier_ids_json = (
                    json.dumps(supplier_ids) if supplier_ids else None
                )
            if 'notify_on_success' in data:
                settings.notify_on_success = _parse_bool(
                    data['notify_on_success']
                )
            if 'notify_on_failure' in data:
                settings.notify_on_failure = _parse_bool(
                    data['notify_on_failure']
                )
            if 'notify_on_pause' in data:
                settings.notify_on_pause = _parse_bool(
                    data['notify_on_pause']
                )
        except _AutoPublishValidationError as exc:
            db.session.rollback()
            return jsonify({'error': str(exc)}), 400

        active_run = _runs_for_scope(seller, settings).filter(
            AutoPublishRun.status.in_(('running', 'waiting', 'cancelling'))
        ).first()
        if active_run is not None:
            db.session.rollback()
            return jsonify({
                'error': 'Настройки нельзя менять во время активного запуска',
                'run_id': active_run.id,
            }), 409

        settings.updated_at = datetime.utcnow()
        db.session.commit()

        return jsonify({'ok': True, 'settings': settings.to_dict()})

    @app.route('/api/auto-publish/toggle', methods=['POST'])
    @login_required
    def api_auto_publish_toggle():
        seller = _get_seller()
        if not seller:
            return jsonify({'error': 'Seller not found'}), 404

        try:
            settings = _settings_for_request(seller)
        except _AutoPublishScopeError as exc:
            return jsonify({'error': str(exc)}), 404

        if not settings.is_enabled:
            if settings.marketplace_code == 'wb':
                has_api_key = (
                    seller.has_valid_api_key()
                    if hasattr(seller, 'has_valid_api_key')
                    else bool(seller.wb_api_key)
                )
                if not has_api_key:
                    return jsonify({'error': 'Сначала настройте API ключ WB'}), 400
            else:
                account = settings.account
                if (
                    account is None
                    or not account.is_active
                    or not account.has_credentials
                    or account.connection_status != 'connected'
                ):
                    return jsonify({
                        'error': 'Сначала подключите и проверьте выбранный кабинет Ozon'
                    }), 400
                if not _ozon_auto_write_enabled():
                    return jsonify({
                        'error': 'Ozon auto-publish rollout flag пока выключен'
                    }), 409

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

        try:
            settings = _settings_for_request(seller)
        except _AutoPublishScopeError as exc:
            return jsonify({'error': str(exc)}), 404
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

        try:
            settings = _settings_for_request(seller)
        except _AutoPublishScopeError as exc:
            return jsonify({'error': str(exc)}), 404
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

        page = max(1, request.args.get('page', 1, type=int) or 1)
        per_page = max(
            1,
            min(request.args.get('per_page', 20, type=int) or 20, 50),
        )
        status = request.args.get('status')

        try:
            settings = _settings_for_request(seller)
        except _AutoPublishScopeError as exc:
            return jsonify({'error': str(exc)}), 404
        query = _runs_for_scope(seller, settings)
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

        try:
            settings = _settings_for_request(seller)
        except _AutoPublishScopeError as exc:
            return jsonify({'error': str(exc)}), 404
        run = _runs_for_scope(seller, settings).filter(
            AutoPublishRun.id == run_id
        ).first()
        if not run:
            return jsonify({'error': 'Run not found'}), 404

        payload = run.to_dict()
        payload['items'] = [
            item.to_dict()
            for item in _items_for_run_scope(
                seller, settings, run
            ).order_by(AutoPublishItem.id.asc()).all()
        ]
        return jsonify(payload)

    @app.route('/api/auto-publish/runs/<int:run_id>/items', methods=['GET'])
    @login_required
    def api_auto_publish_run_items(run_id):
        seller = _get_seller()
        if not seller:
            return jsonify({'error': 'Seller not found'}), 404

        try:
            settings = _settings_for_request(seller)
        except _AutoPublishScopeError as exc:
            return jsonify({'error': str(exc)}), 404
        run = _runs_for_scope(seller, settings).filter(
            AutoPublishRun.id == run_id
        ).first()
        if not run:
            return jsonify({'error': 'Run not found'}), 404

        status = request.args.get('status')
        query = _items_for_run_scope(seller, settings, run)
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

        try:
            settings = _settings_for_request(seller)
        except _AutoPublishScopeError as exc:
            return jsonify({'error': str(exc)}), 404
        if not settings.is_enabled or settings.is_paused:
            return jsonify({
                'error': 'Сначала включите и возобновите выбранный scope'
            }), 409
        if settings.marketplace_code == 'wb':
            has_api_key = (
                seller.has_valid_api_key()
                if hasattr(seller, 'has_valid_api_key')
                else bool(seller.wb_api_key)
            )
            if not has_api_key:
                return jsonify({'error': 'API ключ WB не настроен'}), 400
        else:
            if not _ozon_auto_write_enabled():
                return jsonify({
                    'error': 'Ozon auto-publish rollout flag выключен'
                }), 409
            account = settings.account
            if (
                account is None
                or not account.is_active
                or not account.has_credentials
                or account.connection_status != 'connected'
            ):
                return jsonify({'error': 'Кабинет Ozon не готов к публикации'}), 409

        if settings._run_lock_token:
            return jsonify({'error': 'Уже есть активный запуск'}), 409

        # Проверяем нет ли активного запуска
        active = _runs_for_scope(seller, settings).filter(
            AutoPublishRun.status.in_(('running', 'waiting', 'cancelling'))
        ).first()
        if active:
            return jsonify({'error': 'Уже есть активный запуск', 'run_id': active.id}), 409

        # Запускаем в отдельном потоке
        flask_app = current_app._get_current_object()
        thread = threading.Thread(
            target=_manual_trigger,
            args=(flask_app, seller.id, settings.id),
            name=(
                f'auto-publish-manual-{settings.marketplace_code}-'
                f'{seller.id}-{settings.id}'
            ),
            daemon=True,
        )
        thread.start()

        return jsonify({
            'ok': True,
            'message': 'Запуск начат',
            'marketplace_code': settings.marketplace_code,
            'account_id': settings.account_id,
        })

    @app.route('/api/auto-publish/runs/<int:run_id>/cancel', methods=['POST'])
    @login_required
    def api_auto_publish_cancel(run_id):
        seller = _get_seller()
        if not seller:
            return jsonify({'error': 'Seller not found'}), 404

        try:
            settings = _settings_for_request(seller)
        except _AutoPublishScopeError as exc:
            return jsonify({'error': str(exc)}), 404
        run = _runs_for_scope(seller, settings).filter(
            AutoPublishRun.id == run_id
        ).first()
        if not run:
            return jsonify({'error': 'Run not found'}), 404

        if run.status not in ('running', 'waiting', 'cancelling'):
            return jsonify({'error': 'Запуск не активен'}), 400

        now = datetime.utcnow()
        if run.marketplace_code == 'ozon':
            # Commit cancellation intent before inspecting items. The worker's
            # atomic submit claim checks this row, so whichever transaction
            # commits first defines the honest no-write boundary.
            run.status = 'cancelling'
            db.session.commit()

        # Отменяются только items без provider write. Уже созданная Ozon
        # operation продолжает read-only reconciliation и не выдаётся за
        # отменённую на стороне провайдера.
        pending_items = AutoPublishItem.query.filter_by(
            run_id=run.id,
            seller_id=seller.id,
            marketplace_code=run.marketplace_code,
            account_id=run.account_id,
            status='pending',
        ).all()
        for item in pending_items:
            item.status = 'skipped'
            item.step = 'skipped'
            item.error_message = 'Отменено пользователем'
            item.completed_at = now
            if run.marketplace_code == 'wb':
                product = ImportedProduct.query.filter_by(
                    id=item.imported_product_id,
                    seller_id=seller.id,
                ).first()
                if product and product.import_status == 'publishing':
                    product.import_status = 'validated'
            run.total_skipped += 1

        active_provider_items = 0
        if run.marketplace_code == 'ozon':
            processing_items = AutoPublishItem.query.filter_by(
                run_id=run.id,
                seller_id=seller.id,
                marketplace_code='ozon',
                account_id=run.account_id,
                status='processing',
            ).all()
            for item in processing_items:
                operation = None
                if item.operation_id:
                    operation = MarketplaceOperation.query.filter_by(
                        id=item.operation_id,
                        seller_id=seller.id,
                        account_id=run.account_id,
                        draft_id=item.draft_id,
                        operation_kind='product_import',
                    ).first()
                elif item.idempotency_key:
                    operation = MarketplaceOperation.query.filter_by(
                        seller_id=seller.id,
                        account_id=run.account_id,
                        draft_id=item.draft_id,
                        operation_kind='product_import',
                        idempotency_key=item.idempotency_key,
                    ).first()
                    if operation is not None:
                        item.operation_id = operation.id
                if operation is not None:
                    active_provider_items += 1
                elif item.idempotency_key or item.step == 'submitting':
                    # The durable submit boundary may have won immediately
                    # before cancellation. Keep reconciliation honest until a
                    # local operation appears or the worker proves no write.
                    active_provider_items += 1
                else:
                    item.status = 'skipped'
                    item.step = 'cancelled_before_write'
                    item.error_message = 'Отменено до создания provider operation'
                    item.completed_at = now
                    run.total_skipped += 1
        if run.marketplace_code == 'ozon' and active_provider_items:
            run.status = 'cancelling'
        else:
            run.status = 'cancelled'
            run.completed_at = now
            if run.started_at:
                run.duration_seconds = (now - run.started_at).total_seconds()

        db.session.commit()
        return jsonify({'ok': True})

    # ========================= API: RETRY =========================

    @app.route('/api/auto-publish/items/<int:item_id>/retry', methods=['POST'])
    @login_required
    def api_auto_publish_retry_item(item_id):
        seller = _get_seller()
        if not seller:
            return jsonify({'error': 'Seller not found'}), 404

        try:
            settings = _settings_for_request(seller)
        except _AutoPublishScopeError as exc:
            return jsonify({'error': str(exc)}), 404
        item_query = AutoPublishItem.query.join(AutoPublishRun).filter(
            AutoPublishItem.id == item_id,
            AutoPublishItem.seller_id == seller.id,
            AutoPublishItem.marketplace_code == settings.marketplace_code,
            AutoPublishRun.seller_id == seller.id,
            AutoPublishRun.settings_id == settings.id,
            AutoPublishRun.marketplace_code == settings.marketplace_code,
        )
        if settings.account_id is None:
            item_query = item_query.filter(
                AutoPublishItem.account_id.is_(None),
                AutoPublishRun.account_id.is_(None),
            )
        else:
            item_query = item_query.filter(
                AutoPublishItem.account_id == settings.account_id,
                AutoPublishRun.account_id == settings.account_id,
            )
        item = item_query.first()
        if not item:
            return jsonify({'error': 'Item not found'}), 404

        if item.status not in ('failed', 'skipped', 'deferred', 'uncertain'):
            return jsonify({
                'error': 'Повторить можно только завершённый проблемный элемент'
            }), 400

        # Сбрасываем товар в validated для подхвата следующим запуском
        if item.marketplace_code == 'wb':
            product = ImportedProduct.query.filter_by(
                id=item.imported_product_id,
                seller_id=seller.id,
            ).first()
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

        try:
            settings = _settings_for_request(seller)
        except _AutoPublishScopeError as exc:
            return jsonify({'error': str(exc)}), 404

        last_run = _runs_for_scope(seller, settings).order_by(
            AutoPublishRun.created_at.desc()
        ).first()

        if settings.marketplace_code == 'ozon':
            from services.marketplace_auto_publish import (
                MarketplaceAutoPublishError,
                OzonAutoPublishService,
            )

            try:
                pending_count = OzonAutoPublishService(
                    seller, settings
                ).pending_candidate_count()
            except MarketplaceAutoPublishError as exc:
                return jsonify({'error': str(exc)}), 409
        else:
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
            'marketplace_code': settings.marketplace_code,
            'account_id': settings.account_id,
        })


def _manual_trigger(flask_app, seller_id, settings_id):
    """Выполняет ручной запуск автопубликации."""
    with flask_app.app_context():
        try:
            from models import Seller
            seller = db.session.get(Seller, seller_id)
            settings = db.session.get(AutoPublishSettings, settings_id)
            if not seller or not settings:
                return

            if settings.marketplace_code == 'ozon':
                from services.marketplace_auto_publish import (
                    OzonAutoPublishService,
                )

                service = OzonAutoPublishService(seller, settings)
            else:
                from services.auto_publish_service import AutoPublishService

                service = AutoPublishService(seller, settings)
            service.execute_run(triggered_by='manual')
        except Exception as exc:
            logger.error(
                "Ошибка ручного запуска для seller %s (%s)",
                seller_id,
                type(exc).__name__,
            )
