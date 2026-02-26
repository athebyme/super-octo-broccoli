# -*- coding: utf-8 -*-
"""
Роуты для работы с заблокированными/скрытыми карточками и экспорта данных.
Данные читаются из БД (кэш), обновляются планировщиком каждые 10 минут.
"""
import json
import logging
from datetime import datetime

from flask import render_template, request, redirect, url_for, flash, jsonify
from flask_login import login_required, current_user
from sqlalchemy import or_

from models import (
    db, Product, APILog, BlockedCard, ShadowedCard, BlockedCardsSyncSettings,
    BulkEditHistory, CardEditHistory,
)
from services.wb_api_client import WildberriesAPIClient, WBAPIException, WBAuthException
from services.data_export import (
    export_data, get_available_columns,
    BLOCKED_CARD_COLUMNS, SHADOWED_CARD_COLUMNS, PRODUCT_COLUMNS,
    BULK_EDIT_COLUMNS, COLUMN_SETS,
)

logger = logging.getLogger('blocked_cards')


def register_blocked_cards_routes(app):
    """Регистрация роутов для заблокированных карточек и экспорта данных"""

    def _get_wb_client(seller):
        """Создать WB API клиент для продавца"""
        return WildberriesAPIClient(
            api_key=seller.wb_api_key,
            db_logger_callback=lambda **kwargs: APILog.log_request(**kwargs)
        )

    # ==================== ЗАБЛОКИРОВАННЫЕ КАРТОЧКИ ====================

    @app.route('/blocked-cards')
    @login_required
    def blocked_cards():
        """Страница заблокированных и скрытых карточек (из БД-кэша)"""
        if not current_user.seller or not current_user.seller.has_valid_api_key():
            flash('Для просмотра заблокированных карточек необходимо настроить API ключ WB', 'warning')
            return redirect(url_for('api_settings'))

        seller = current_user.seller
        tab = request.args.get('tab', 'blocked')
        search = request.args.get('search', '').strip()
        filter_brand = request.args.get('brand', '').strip()
        filter_reason = request.args.get('reason', '').strip()
        date_from = request.args.get('date_from', '').strip()
        date_to = request.args.get('date_to', '').strip()
        sort = request.args.get('sort', 'first_seen_at')
        order = request.args.get('order', 'desc')
        page = request.args.get('page', 1, type=int)
        per_page = request.args.get('per_page', 50, type=int)
        per_page = min(per_page, 200)
        show_resolved = request.args.get('show_resolved', '') in ['1', 'on']

        # Статус синхронизации
        sync_settings = BlockedCardsSyncSettings.query.filter_by(
            seller_id=seller.id
        ).first()

        # --- Заблокированные ---
        blocked_pagination = None
        blocked_brands = []
        blocked_reasons = []

        if tab in ('blocked', 'all'):
            q = BlockedCard.query.filter_by(seller_id=seller.id)
            if not show_resolved:
                q = q.filter_by(is_active=True)

            if search:
                pattern = f'%{search}%'
                q = q.filter(or_(
                    BlockedCard.nm_id.cast(db.String).ilike(pattern),
                    BlockedCard.vendor_code.ilike(pattern),
                    BlockedCard.title.ilike(pattern),
                    BlockedCard.brand.ilike(pattern),
                    BlockedCard.reason.ilike(pattern),
                ))
            if filter_brand:
                q = q.filter(BlockedCard.brand == filter_brand)
            if filter_reason:
                q = q.filter(BlockedCard.reason.ilike(f'%{filter_reason}%'))
            if date_from:
                try:
                    q = q.filter(BlockedCard.first_seen_at >= datetime.strptime(date_from, '%Y-%m-%d'))
                except ValueError:
                    pass
            if date_to:
                try:
                    q = q.filter(BlockedCard.first_seen_at <= datetime.strptime(date_to + ' 23:59:59', '%Y-%m-%d %H:%M:%S'))
                except ValueError:
                    pass

            # Сортировка
            sort_col = {
                'nm_id': BlockedCard.nm_id,
                'vendor_code': BlockedCard.vendor_code,
                'title': BlockedCard.title,
                'brand': BlockedCard.brand,
                'reason': BlockedCard.reason,
                'first_seen_at': BlockedCard.first_seen_at,
            }.get(sort, BlockedCard.first_seen_at)
            q = q.order_by(sort_col.desc() if order == 'desc' else sort_col.asc())

            blocked_pagination = q.paginate(page=page, per_page=per_page, error_out=False)

            # Уникальные бренды и причины для фильтров
            base_q = BlockedCard.query.filter_by(seller_id=seller.id, is_active=True)
            blocked_brands = [r[0] for r in base_q.with_entities(BlockedCard.brand).filter(
                BlockedCard.brand.isnot(None), BlockedCard.brand != ''
            ).distinct().order_by(BlockedCard.brand).all()]
            blocked_reasons = [r[0] for r in base_q.with_entities(BlockedCard.reason).filter(
                BlockedCard.reason.isnot(None), BlockedCard.reason != ''
            ).distinct().order_by(BlockedCard.reason).all()]

        # --- Скрытые ---
        shadowed_pagination = None
        shadowed_brands = []

        if tab in ('shadowed', 'all'):
            q = ShadowedCard.query.filter_by(seller_id=seller.id)
            if not show_resolved:
                q = q.filter_by(is_active=True)

            if search:
                pattern = f'%{search}%'
                q = q.filter(or_(
                    ShadowedCard.nm_id.cast(db.String).ilike(pattern),
                    ShadowedCard.vendor_code.ilike(pattern),
                    ShadowedCard.title.ilike(pattern),
                    ShadowedCard.brand.ilike(pattern),
                ))
            if filter_brand:
                q = q.filter(ShadowedCard.brand == filter_brand)
            if date_from:
                try:
                    q = q.filter(ShadowedCard.first_seen_at >= datetime.strptime(date_from, '%Y-%m-%d'))
                except ValueError:
                    pass
            if date_to:
                try:
                    q = q.filter(ShadowedCard.first_seen_at <= datetime.strptime(date_to + ' 23:59:59', '%Y-%m-%d %H:%M:%S'))
                except ValueError:
                    pass

            sort_col_s = {
                'nm_id': ShadowedCard.nm_id,
                'vendor_code': ShadowedCard.vendor_code,
                'title': ShadowedCard.title,
                'brand': ShadowedCard.brand,
                'nm_rating': ShadowedCard.nm_rating,
                'first_seen_at': ShadowedCard.first_seen_at,
            }.get(sort, ShadowedCard.first_seen_at)
            q = q.order_by(sort_col_s.desc() if order == 'desc' else sort_col_s.asc())

            shadowed_pagination = q.paginate(page=page, per_page=per_page, error_out=False)

            base_q = ShadowedCard.query.filter_by(seller_id=seller.id, is_active=True)
            shadowed_brands = [r[0] for r in base_q.with_entities(ShadowedCard.brand).filter(
                ShadowedCard.brand.isnot(None), ShadowedCard.brand != ''
            ).distinct().order_by(ShadowedCard.brand).all()]

        # Общие счётчики
        total_blocked = BlockedCard.query.filter_by(seller_id=seller.id, is_active=True).count()
        total_shadowed = ShadowedCard.query.filter_by(seller_id=seller.id, is_active=True).count()

        return render_template(
            'blocked_cards.html',
            tab=tab,
            blocked_pagination=blocked_pagination,
            shadowed_pagination=shadowed_pagination,
            total_blocked=total_blocked,
            total_shadowed=total_shadowed,
            search=search,
            filter_brand=filter_brand,
            filter_reason=filter_reason,
            date_from=date_from,
            date_to=date_to,
            sort=sort,
            order=order,
            per_page=per_page,
            show_resolved=show_resolved,
            blocked_brands=blocked_brands,
            blocked_reasons=blocked_reasons,
            shadowed_brands=shadowed_brands,
            sync_settings=sync_settings,
            blocked_columns=get_available_columns('blocked'),
            shadowed_columns=get_available_columns('shadowed'),
        )

    # ==================== РУЧНОЕ ОБНОВЛЕНИЕ ====================

    @app.route('/blocked-cards/refresh', methods=['POST'])
    @login_required
    def blocked_cards_refresh():
        """Принудительное обновление заблокированных карточек из API"""
        if not current_user.seller or not current_user.seller.has_valid_api_key():
            flash('API ключ не настроен', 'warning')
            return redirect(url_for('api_settings'))

        seller = current_user.seller
        try:
            from services.product_sync_scheduler import _upsert_blocked_cards, _upsert_shadowed_cards
            client = _get_wb_client(seller)

            blocked_api = client.get_blocked_cards(
                sort='nmId', order='asc',
                log_to_db=True, seller_id=seller.id
            )
            _upsert_blocked_cards(seller.id, blocked_api, db)

            shadowed_api = client.get_shadowed_cards(
                sort='nmId', order='asc',
                log_to_db=True, seller_id=seller.id
            )
            _upsert_shadowed_cards(seller.id, shadowed_api, db)

            # Обновляем статус синка
            sync_settings = BlockedCardsSyncSettings.query.filter_by(
                seller_id=seller.id
            ).first()
            if not sync_settings:
                sync_settings = BlockedCardsSyncSettings(seller_id=seller.id)
                db.session.add(sync_settings)
            sync_settings.last_sync_at = datetime.utcnow()
            sync_settings.last_sync_status = 'success'
            sync_settings.last_sync_error = None
            sync_settings.blocked_count = len(blocked_api)
            sync_settings.shadowed_count = len(shadowed_api)
            db.session.commit()

            if len(blocked_api) == 0 and len(shadowed_api) == 0:
                flash(
                    'API вернул 0 заблокированных и 0 скрытых карточек. '
                    'Убедитесь, что API-ключ имеет права категории «Аналитика» (contentanalytics).',
                    'warning'
                )
            else:
                flash(
                    f'Данные обновлены: {len(blocked_api)} заблокированных, '
                    f'{len(shadowed_api)} скрытых карточек',
                    'success'
                )
        except WBAuthException:
            flash(
                'Ошибка авторизации. Проверьте API-ключ и убедитесь, что он '
                'включает права категории «Аналитика» (contentanalytics).',
                'danger'
            )
        except WBAPIException as e:
            error_msg = str(e)
            if '403' in error_msg or 'Forbidden' in error_msg:
                flash(
                    f'Доступ запрещён (403). API-ключ не имеет прав для раздела аналитики. '
                    f'Создайте новый ключ с категорией «Аналитика» в личном кабинете WB.',
                    'danger'
                )
            else:
                flash(f'Ошибка API: {error_msg}', 'danger')
        except Exception as e:
            logger.exception(f'Manual refresh error for seller {seller.id}')
            flash(f'Ошибка обновления: {str(e)}', 'danger')

        return redirect(url_for('blocked_cards', tab=request.args.get('tab', 'blocked')))

    # ==================== ЭКСПОРТ ЗАБЛОКИРОВАННЫХ ====================

    @app.route('/blocked-cards/export')
    @login_required
    def blocked_cards_export():
        """Экспорт заблокированных или скрытых карточек из БД"""
        if not current_user.seller:
            flash('Нет профиля продавца', 'warning')
            return redirect(url_for('dashboard'))

        seller = current_user.seller
        tab = request.args.get('tab', 'blocked')
        fmt = request.args.get('format', 'csv')
        columns = request.args.getlist('columns')
        separator = request.args.get('separator', ', ')
        text_column = request.args.get('text_column', '')

        if tab == 'shadowed':
            cards = ShadowedCard.query.filter_by(
                seller_id=seller.id, is_active=True
            ).order_by(ShadowedCard.nm_id.asc()).all()
            data = [
                {
                    'nmId': c.nm_id, 'vendorCode': c.vendor_code,
                    'title': c.title, 'brand': c.brand,
                    'nmRating': c.nm_rating,
                }
                for c in cards
            ]
            column_defs = SHADOWED_CARD_COLUMNS
            prefix = 'shadowed_cards'
        else:
            cards = BlockedCard.query.filter_by(
                seller_id=seller.id, is_active=True
            ).order_by(BlockedCard.nm_id.asc()).all()
            data = [
                {
                    'nmId': c.nm_id, 'vendorCode': c.vendor_code,
                    'title': c.title, 'brand': c.brand,
                    'reason': c.reason,
                }
                for c in cards
            ]
            column_defs = BLOCKED_CARD_COLUMNS
            prefix = 'blocked_cards'

        if not columns:
            columns = list(column_defs.keys())

        return export_data(
            data=data,
            columns=columns,
            column_defs=column_defs,
            fmt=fmt,
            filename_prefix=prefix,
            separator=separator,
            single_column_for_text=text_column if text_column else None,
        )

    # ==================== УНИВЕРСАЛЬНЫЙ ЭКСПОРТ ДАННЫХ ====================

    @app.route('/export/products')
    @login_required
    def export_products():
        """Экспорт карточек товаров с выбором колонок и формата"""
        if not current_user.seller:
            flash('У вас нет профиля продавца', 'danger')
            return redirect(url_for('dashboard'))

        seller = current_user.seller
        fmt = request.args.get('format', 'csv')
        columns = request.args.getlist('columns')
        separator = request.args.get('separator', ', ')
        text_column = request.args.get('text_column', '')
        search = request.args.get('search', '').strip()
        filter_brand = request.args.get('brand', '').strip()
        active_only = request.args.get('active_only', '') in ['1', 'true', 'on']

        query = Product.query.filter_by(seller_id=seller.id)

        if active_only:
            query = query.filter_by(is_active=True)
        if filter_brand:
            query = query.filter_by(brand=filter_brand)
        if search:
            search_pattern = f'%{search}%'
            query = query.filter(
                or_(
                    Product.vendor_code.ilike(search_pattern),
                    Product.title.ilike(search_pattern),
                    Product.brand.ilike(search_pattern),
                    Product.nm_id.cast(db.String).ilike(search_pattern),
                )
            )

        products = query.order_by(Product.nm_id.asc()).all()

        data = []
        for p in products:
            data.append({
                'nm_id': p.nm_id,
                'vendor_code': p.vendor_code,
                'title': p.title,
                'brand': p.brand,
                'object_name': p.object_name,
                'price': p.price,
                'sizes': json.loads(p.sizes_json) if p.sizes_json else [],
            })

        if not columns:
            columns = list(PRODUCT_COLUMNS.keys())

        return export_data(
            data=data,
            columns=columns,
            column_defs=PRODUCT_COLUMNS,
            fmt=fmt,
            filename_prefix='products',
            separator=separator,
            single_column_for_text=text_column if text_column else None,
        )

    # ==================== ЭКСПОРТ ИЗ МАССОВЫХ ОПЕРАЦИЙ ====================

    @app.route('/bulk-history/<int:bulk_id>/export')
    @login_required
    def bulk_edit_export(bulk_id):
        """Экспорт товаров из массовой операции"""
        if not current_user.seller:
            flash('Нет профиля продавца', 'warning')
            return redirect(url_for('dashboard'))

        seller = current_user.seller
        bulk_op = BulkEditHistory.query.filter_by(
            id=bulk_id, seller_id=seller.id
        ).first_or_404()

        fmt = request.args.get('format', 'csv')
        columns = request.args.getlist('columns')
        separator = request.args.get('separator', ', ')
        text_column = request.args.get('text_column', '')

        changes = CardEditHistory.query.filter_by(
            bulk_edit_id=bulk_id
        ).all()

        field_labels = {
            'vendor_code': 'Артикул', 'title': 'Название',
            'description': 'Описание', 'brand': 'Бренд',
            'characteristics': 'Характеристики',
        }

        data = []
        for ch in changes:
            product = ch.product
            status = 'Откачено' if ch.reverted else (
                'Синхронизировано' if ch.wb_synced else 'Ошибка'
            )
            changed = ', '.join(
                field_labels.get(f, f) for f in (ch.changed_fields or [])
            )
            data.append({
                'nm_id': product.nm_id if product else '',
                'vendor_code': product.vendor_code if product else '',
                'title': product.title if product else '',
                'brand': product.brand if product else '',
                'changed_fields_str': changed,
                'status': status,
                'error': ch.wb_error_message or '',
            })

        if not columns:
            columns = list(BULK_EDIT_COLUMNS.keys())

        desc = bulk_op.description or f'bulk_op_{bulk_id}'
        safe_prefix = 'bulk_edit_' + str(bulk_id)

        return export_data(
            data=data,
            columns=columns,
            column_defs=BULK_EDIT_COLUMNS,
            fmt=fmt,
            filename_prefix=safe_prefix,
            separator=separator,
            single_column_for_text=text_column if text_column else None,
        )

    # ==================== API: ДОСТУПНЫЕ КОЛОНКИ ====================

    @app.route('/api/export/columns/<column_set>')
    @login_required
    def api_export_columns(column_set):
        """API для получения доступных колонок набора данных"""
        cols = get_available_columns(column_set)
        if not cols:
            return jsonify({'error': f'Unknown column set: {column_set}'}), 404
        return jsonify({'columns': cols})
