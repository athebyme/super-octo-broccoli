# -*- coding: utf-8 -*-
"""
Роуты для работы с заблокированными/скрытыми карточками и экспорта данных
"""
import json
import logging

from flask import render_template, request, redirect, url_for, flash, jsonify
from flask_login import login_required, current_user

from models import db, Product, APILog
from wb_api_client import WildberriesAPIClient, WBAPIException, WBAuthException
from data_export import (
    export_data, get_available_columns,
    BLOCKED_CARD_COLUMNS, SHADOWED_CARD_COLUMNS, PRODUCT_COLUMNS, COLUMN_SETS,
)

logger = logging.getLogger('blocked_cards')


def register_blocked_cards_routes(app):
    """Регистрация роутов для заблокированных карточек и экспорта данных"""

    def _get_wb_client(seller):
        """Создать WB API клиент для продавца"""
        return WildberriesAPIClient(
            api_key=seller.wb_api_key,
            db_logger_callback=lambda **kwargs: APILog.log_request(
                seller_id=seller.id, **kwargs
            )
        )

    # ==================== ЗАБЛОКИРОВАННЫЕ КАРТОЧКИ ====================

    @app.route('/blocked-cards')
    @login_required
    def blocked_cards():
        """Страница заблокированных и скрытых карточек"""
        if not current_user.seller or not current_user.seller.has_valid_api_key():
            flash('Для просмотра заблокированных карточек необходимо настроить API ключ WB', 'warning')
            return redirect(url_for('api_settings'))

        seller = current_user.seller
        tab = request.args.get('tab', 'blocked')
        sort = request.args.get('sort', 'nmId')
        order = request.args.get('order', 'asc')
        search = request.args.get('search', '').strip()

        blocked_cards_data = []
        shadowed_cards_data = []
        error_message = None

        try:
            client = _get_wb_client(seller)

            if tab == 'blocked' or tab == 'all':
                blocked_cards_data = client.get_blocked_cards(
                    sort=sort, order=order,
                    log_to_db=True, seller_id=seller.id
                )

            if tab == 'shadowed' or tab == 'all':
                shadowed_sort = sort if sort != 'reason' else 'nmId'
                shadowed_cards_data = client.get_shadowed_cards(
                    sort=shadowed_sort, order=order,
                    log_to_db=True, seller_id=seller.id
                )

        except WBAuthException:
            error_message = 'Ошибка авторизации. Проверьте API ключ.'
        except WBAPIException as e:
            error_message = f'Ошибка API: {str(e)}'
        except Exception as e:
            logger.exception(f'Error loading blocked cards for seller {seller.id}')
            error_message = f'Ошибка загрузки данных: {str(e)}'

        # Фильтрация по поисковому запросу (на стороне сервера, т.к. API не поддерживает поиск)
        if search:
            search_lower = search.lower()
            blocked_cards_data = [
                c for c in blocked_cards_data
                if search_lower in str(c.get('nmId', '')).lower()
                or search_lower in (c.get('vendorCode', '') or '').lower()
                or search_lower in (c.get('title', '') or '').lower()
                or search_lower in (c.get('brand', '') or '').lower()
                or search_lower in (c.get('reason', '') or '').lower()
            ]
            shadowed_cards_data = [
                c for c in shadowed_cards_data
                if search_lower in str(c.get('nmId', '')).lower()
                or search_lower in (c.get('vendorCode', '') or '').lower()
                or search_lower in (c.get('title', '') or '').lower()
                or search_lower in (c.get('brand', '') or '').lower()
            ]

        return render_template(
            'blocked_cards.html',
            blocked_cards=blocked_cards_data,
            shadowed_cards=shadowed_cards_data,
            tab=tab,
            sort=sort,
            order=order,
            search=search,
            error_message=error_message,
            blocked_columns=get_available_columns('blocked'),
            shadowed_columns=get_available_columns('shadowed'),
        )

    # ==================== ЭКСПОРТ ЗАБЛОКИРОВАННЫХ ====================

    @app.route('/blocked-cards/export')
    @login_required
    def blocked_cards_export():
        """Экспорт заблокированных или скрытых карточек"""
        if not current_user.seller or not current_user.seller.has_valid_api_key():
            flash('API ключ не настроен', 'warning')
            return redirect(url_for('api_settings'))

        seller = current_user.seller
        tab = request.args.get('tab', 'blocked')
        fmt = request.args.get('format', 'csv')
        columns = request.args.getlist('columns')
        separator = request.args.get('separator', ', ')
        text_column = request.args.get('text_column', '')

        try:
            client = _get_wb_client(seller)

            if tab == 'shadowed':
                data = client.get_shadowed_cards(
                    sort='nmId', order='asc',
                    log_to_db=True, seller_id=seller.id
                )
                column_defs = SHADOWED_CARD_COLUMNS
                prefix = 'shadowed_cards'
            else:
                data = client.get_blocked_cards(
                    sort='nmId', order='asc',
                    log_to_db=True, seller_id=seller.id
                )
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

        except WBAuthException:
            flash('Ошибка авторизации. Проверьте API ключ.', 'danger')
        except WBAPIException as e:
            flash(f'Ошибка API: {str(e)}', 'danger')
        except Exception as e:
            logger.exception(f'Export error for seller {seller.id}')
            flash(f'Ошибка экспорта: {str(e)}', 'danger')

        return redirect(url_for('blocked_cards'))

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

        # Запрос товаров из БД
        query = Product.query.filter_by(seller_id=seller.id)

        if active_only:
            query = query.filter_by(is_active=True)

        if filter_brand:
            query = query.filter_by(brand=filter_brand)

        if search:
            from sqlalchemy import or_
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

        # Конвертация ORM-объектов в словари
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

    # ==================== API: ДОСТУПНЫЕ КОЛОНКИ ====================

    @app.route('/api/export/columns/<column_set>')
    @login_required
    def api_export_columns(column_set):
        """API для получения доступных колонок набора данных"""
        columns = get_available_columns(column_set)
        if not columns:
            return jsonify({'error': f'Unknown column set: {column_set}'}), 404
        return jsonify({'columns': columns})
