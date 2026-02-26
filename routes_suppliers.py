# -*- coding: utf-8 -*-
"""
Маршруты для управления поставщиками (админ) и каталога (продавец)
"""
import json
import re
import logging
from datetime import datetime
from functools import wraps

from flask import (
    Blueprint, render_template, redirect, url_for, flash,
    request, abort, jsonify
)
from flask_login import login_required, current_user

from models import (
    db, Supplier, SupplierProduct, SellerSupplier,
    ImportedProduct, Seller, log_admin_action
)
from supplier_service import SupplierService

logger = logging.getLogger(__name__)


# ============================================================================
# DECORATORS
# ============================================================================

def admin_required(f):
    """Декоратор для проверки прав администратора"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            flash('У вас нет прав для доступа к этой странице', 'danger')
            return redirect(url_for('dashboard'))
        return f(*args, **kwargs)
    return decorated_function


def seller_required(f):
    """Декоратор — пользователь должен быть продавцом"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.seller:
            flash('У вас нет профиля продавца', 'danger')
            return redirect(url_for('dashboard'))
        return f(*args, **kwargs)
    return decorated_function


# ============================================================================
# ADMIN: CRUD поставщиков
# ============================================================================

def register_supplier_routes(app):
    """Регистрирует все маршруты поставщиков в приложении Flask"""

    # -------------------------------------------------------------------
    # Список поставщиков
    # -------------------------------------------------------------------
    @app.route('/admin/suppliers')
    @login_required
    @admin_required
    def admin_suppliers():
        suppliers = SupplierService.list_suppliers()
        stats = {}
        for s in suppliers:
            stats[s.id] = SupplierService.get_product_stats(s.id)
            stats[s.id]['sellers_count'] = s.get_connected_sellers_count()
        return render_template('admin_suppliers.html',
                               suppliers=suppliers, stats=stats)

    # -------------------------------------------------------------------
    # Создание поставщика
    # -------------------------------------------------------------------
    @app.route('/admin/suppliers/add', methods=['GET', 'POST'])
    @login_required
    @admin_required
    def admin_supplier_add():
        if request.method == 'POST':
            code = request.form.get('code', '').strip().lower()
            code = re.sub(r'[^a-z0-9_-]', '', code)
            name = request.form.get('name', '').strip()

            if not name or not code:
                flash('Название и код обязательны', 'warning')
                return render_template('admin_supplier_form.html', supplier=None, mode='add')

            # Проверка уникальности кода
            if Supplier.query.filter_by(code=code).first():
                flash(f'Поставщик с кодом "{code}" уже существует', 'danger')
                return render_template('admin_supplier_form.html', supplier=None, mode='add')

            data = _extract_supplier_form_data(request.form)
            data['name'] = name
            data['code'] = code

            supplier = SupplierService.create_supplier(data, created_by_user_id=current_user.id)

            log_admin_action(
                admin_user_id=current_user.id,
                action='create_supplier',
                target_type='supplier',
                target_id=supplier.id,
                details={'name': name, 'code': code},
                request=request
            )

            flash(f'Поставщик "{name}" успешно создан', 'success')
            return redirect(url_for('admin_supplier_edit', supplier_id=supplier.id))

        return render_template('admin_supplier_form.html', supplier=None, mode='add')

    # -------------------------------------------------------------------
    # Редактирование поставщика
    # -------------------------------------------------------------------
    @app.route('/admin/suppliers/<int:supplier_id>/edit', methods=['GET', 'POST'])
    @login_required
    @admin_required
    def admin_supplier_edit(supplier_id):
        supplier = SupplierService.get_supplier(supplier_id)
        if not supplier:
            flash('Поставщик не найден', 'danger')
            return redirect(url_for('admin_suppliers'))

        if request.method == 'POST':
            data = _extract_supplier_form_data(request.form)
            data['name'] = request.form.get('name', supplier.name).strip()

            SupplierService.update_supplier(supplier_id, data)

            log_admin_action(
                admin_user_id=current_user.id,
                action='update_supplier',
                target_type='supplier',
                target_id=supplier_id,
                details={'name': data['name']},
                request=request
            )

            flash('Данные поставщика обновлены', 'success')
            return redirect(url_for('admin_supplier_edit', supplier_id=supplier_id))

        stats = SupplierService.get_product_stats(supplier_id)
        sellers = SupplierService.get_supplier_sellers(supplier_id)
        return render_template('admin_supplier_form.html',
                               supplier=supplier, mode='edit',
                               stats=stats, sellers=sellers)

    # -------------------------------------------------------------------
    # Удаление поставщика
    # -------------------------------------------------------------------
    @app.route('/admin/suppliers/<int:supplier_id>/delete', methods=['POST'])
    @login_required
    @admin_required
    def admin_supplier_delete(supplier_id):
        supplier = SupplierService.get_supplier(supplier_id)
        if not supplier:
            flash('Поставщик не найден', 'danger')
            return redirect(url_for('admin_suppliers'))

        name = supplier.name
        SupplierService.delete_supplier(supplier_id)

        log_admin_action(
            admin_user_id=current_user.id,
            action='delete_supplier',
            target_type='supplier',
            target_id=supplier_id,
            details={'name': name},
            request=request
        )

        flash(f'Поставщик "{name}" удалён', 'success')
        return redirect(url_for('admin_suppliers'))

    # -------------------------------------------------------------------
    # Переключение статуса
    # -------------------------------------------------------------------
    @app.route('/admin/suppliers/<int:supplier_id>/toggle-active', methods=['POST'])
    @login_required
    @admin_required
    def admin_supplier_toggle(supplier_id):
        supplier = SupplierService.get_supplier(supplier_id)
        if not supplier:
            flash('Поставщик не найден', 'danger')
            return redirect(url_for('admin_suppliers'))

        supplier.is_active = not supplier.is_active
        db.session.commit()

        status = 'активирован' if supplier.is_active else 'деактивирован'
        flash(f'Поставщик "{supplier.name}" {status}', 'success')
        return redirect(url_for('admin_suppliers'))

    # -------------------------------------------------------------------
    # Синхронизация каталога из CSV
    # -------------------------------------------------------------------
    @app.route('/admin/suppliers/<int:supplier_id>/sync', methods=['POST'])
    @login_required
    @admin_required
    def admin_supplier_sync(supplier_id):
        supplier = SupplierService.get_supplier(supplier_id)
        if not supplier:
            flash('Поставщик не найден', 'danger')
            return redirect(url_for('admin_suppliers'))

        if not supplier.csv_source_url:
            flash('URL CSV не задан для этого поставщика', 'warning')
            return redirect(url_for('admin_supplier_edit', supplier_id=supplier_id))

        result = SupplierService.sync_from_csv(supplier_id)

        log_admin_action(
            admin_user_id=current_user.id,
            action='sync_supplier_csv',
            target_type='supplier',
            target_id=supplier_id,
            details={
                'added': result.added,
                'updated': result.updated,
                'errors': result.errors,
                'duration': round(result.duration_seconds, 1)
            },
            request=request
        )

        if result.success:
            flash(
                f'Синхронизация завершена: +{result.added} новых, '
                f'~{result.updated} обновлено, {result.errors} ошибок '
                f'({result.duration_seconds:.1f}с)',
                'success'
            )
        else:
            flash(f'Ошибка синхронизации: {"; ".join(result.error_messages[:3])}', 'danger')

        return redirect(url_for('admin_supplier_products', supplier_id=supplier_id))

    # -------------------------------------------------------------------
    # Товары поставщика (админ)
    # -------------------------------------------------------------------
    @app.route('/admin/suppliers/<int:supplier_id>/products')
    @login_required
    @admin_required
    def admin_supplier_products(supplier_id):
        supplier = SupplierService.get_supplier(supplier_id)
        if not supplier:
            flash('Поставщик не найден', 'danger')
            return redirect(url_for('admin_suppliers'))

        page = request.args.get('page', 1, type=int)
        per_page = request.args.get('per_page', 50, type=int)
        search = request.args.get('search', '').strip()
        status = request.args.get('status', '').strip()
        brand = request.args.get('brand', '').strip()
        category = request.args.get('category', '').strip()
        ai_validated = request.args.get('ai_validated')
        sort_by = request.args.get('sort_by', 'created_at')
        sort_dir = request.args.get('sort_dir', 'desc')

        ai_val = None
        if ai_validated == '1':
            ai_val = True
        elif ai_validated == '0':
            ai_val = False

        pagination = SupplierService.get_products(
            supplier_id, page=page, per_page=per_page,
            search=search, status=status or None,
            category=category or None, brand=brand or None,
            ai_validated=ai_val, sort_by=sort_by, sort_dir=sort_dir
        )

        stats = SupplierService.get_product_stats(supplier_id)

        # Получить список брендов и категорий для фильтров
        brands = db.session.query(SupplierProduct.brand).filter(
            SupplierProduct.supplier_id == supplier_id,
            SupplierProduct.brand.isnot(None),
            SupplierProduct.brand != ''
        ).distinct().order_by(SupplierProduct.brand).all()
        brands = [b[0] for b in brands]

        categories = db.session.query(SupplierProduct.category).filter(
            SupplierProduct.supplier_id == supplier_id,
            SupplierProduct.category.isnot(None),
            SupplierProduct.category != ''
        ).distinct().order_by(SupplierProduct.category).all()
        categories = [c[0] for c in categories]

        return render_template('admin_supplier_products.html',
                               supplier=supplier, pagination=pagination,
                               stats=stats, brands=brands, categories=categories,
                               search=search, current_status=status,
                               current_brand=brand, current_category=category,
                               ai_validated=ai_validated, sort_by=sort_by, sort_dir=sort_dir)

    # -------------------------------------------------------------------
    # Детали / редактирование товара поставщика
    # -------------------------------------------------------------------
    @app.route('/admin/suppliers/<int:supplier_id>/products/<int:product_id>', methods=['GET', 'POST'])
    @login_required
    @admin_required
    def admin_supplier_product_detail(supplier_id, product_id):
        supplier = SupplierService.get_supplier(supplier_id)
        product = SupplierService.get_product(product_id)
        if not supplier or not product or product.supplier_id != supplier_id:
            flash('Товар не найден', 'danger')
            return redirect(url_for('admin_supplier_products', supplier_id=supplier_id))

        if request.method == 'POST':
            data = _extract_product_form_data(request.form)
            SupplierService.update_product(product_id, data)

            log_admin_action(
                admin_user_id=current_user.id,
                action='update_supplier_product',
                target_type='supplier_product',
                target_id=product_id,
                details={'supplier_id': supplier_id, 'title': data.get('title')},
                request=request
            )

            flash('Товар обновлён', 'success')
            return redirect(url_for('admin_supplier_product_detail',
                                    supplier_id=supplier_id, product_id=product_id))

        # Кол-во продавцов, импортировавших этот товар
        import_count = ImportedProduct.query.filter_by(supplier_product_id=product_id).count()

        return render_template('admin_supplier_product_detail.html',
                               supplier=supplier, product=product,
                               import_count=import_count)

    # -------------------------------------------------------------------
    # Массовые действия с товарами
    # -------------------------------------------------------------------
    @app.route('/admin/suppliers/<int:supplier_id>/products/bulk-action', methods=['POST'])
    @login_required
    @admin_required
    def admin_supplier_products_bulk(supplier_id):
        action = request.form.get('action', '')
        product_ids_raw = request.form.getlist('product_ids')
        product_ids = [int(pid) for pid in product_ids_raw if pid.isdigit()]

        if not product_ids:
            flash('Не выбраны товары', 'warning')
            return redirect(url_for('admin_supplier_products', supplier_id=supplier_id))

        if action == 'delete':
            count = SupplierService.delete_products(product_ids)
            # Обновляем total_products
            supplier = SupplierService.get_supplier(supplier_id)
            if supplier:
                supplier.total_products = SupplierProduct.query.filter_by(supplier_id=supplier_id).count()
                db.session.commit()
            flash(f'Удалено {count} товаров', 'success')

        elif action == 'set_status':
            new_status = request.form.get('new_status', 'draft')
            for pid in product_ids:
                p = SupplierProduct.query.get(pid)
                if p and p.supplier_id == supplier_id:
                    p.status = new_status
            db.session.commit()
            flash(f'Статус обновлён для {len(product_ids)} товаров', 'success')

        elif action == 'ai_validate':
            # Перенаправляем на AI страницу
            flash(f'AI валидация для {len(product_ids)} товаров запланирована', 'info')

        log_admin_action(
            admin_user_id=current_user.id,
            action=f'bulk_{action}_supplier_products',
            target_type='supplier',
            target_id=supplier_id,
            details={'product_count': len(product_ids), 'action': action},
            request=request
        )

        return redirect(url_for('admin_supplier_products', supplier_id=supplier_id))

    # -------------------------------------------------------------------
    # Управление подключёнными продавцами
    # -------------------------------------------------------------------
    @app.route('/admin/suppliers/<int:supplier_id>/sellers')
    @login_required
    @admin_required
    def admin_supplier_sellers(supplier_id):
        supplier = SupplierService.get_supplier(supplier_id)
        if not supplier:
            flash('Поставщик не найден', 'danger')
            return redirect(url_for('admin_suppliers'))

        connections = SupplierService.get_supplier_sellers(supplier_id, active_only=False)
        # Все продавцы для выбора подключения
        all_sellers = Seller.query.join(Seller.user).order_by(Seller.company_name).all()
        connected_seller_ids = {c.seller_id for c in connections if c.is_active}

        return render_template('admin_supplier_sellers.html',
                               supplier=supplier, connections=connections,
                               all_sellers=all_sellers,
                               connected_seller_ids=connected_seller_ids)

    @app.route('/admin/suppliers/<int:supplier_id>/sellers/connect', methods=['POST'])
    @login_required
    @admin_required
    def admin_supplier_connect_seller(supplier_id):
        seller_id = request.form.get('seller_id', type=int)
        supplier_code = request.form.get('supplier_code', '').strip()

        if not seller_id:
            flash('Выберите продавца', 'warning')
            return redirect(url_for('admin_supplier_sellers', supplier_id=supplier_id))

        SupplierService.connect_seller(seller_id, supplier_id, supplier_code=supplier_code)

        log_admin_action(
            admin_user_id=current_user.id,
            action='connect_seller_to_supplier',
            target_type='supplier',
            target_id=supplier_id,
            details={'seller_id': seller_id},
            request=request
        )

        flash('Продавец подключён', 'success')
        return redirect(url_for('admin_supplier_sellers', supplier_id=supplier_id))

    @app.route('/admin/suppliers/<int:supplier_id>/sellers/<int:seller_id>/disconnect', methods=['POST'])
    @login_required
    @admin_required
    def admin_supplier_disconnect_seller(supplier_id, seller_id):
        SupplierService.disconnect_seller(seller_id, supplier_id)

        log_admin_action(
            admin_user_id=current_user.id,
            action='disconnect_seller_from_supplier',
            target_type='supplier',
            target_id=supplier_id,
            details={'seller_id': seller_id},
            request=request
        )

        flash('Продавец отключён', 'success')
        return redirect(url_for('admin_supplier_sellers', supplier_id=supplier_id))

    # ===================================================================
    # КАТАЛОГ ПОСТАВЩИКА ДЛЯ ПРОДАВЦА
    # ===================================================================

    @app.route('/supplier-catalog')
    @login_required
    @seller_required
    def supplier_catalog():
        """Список доступных поставщиков для продавца"""
        seller = current_user.seller
        connections = SupplierService.get_seller_suppliers(seller.id)

        suppliers_data = []
        for conn in connections:
            supplier = conn.supplier
            stats = SupplierService.get_product_stats(supplier.id)
            imported_count = ImportedProduct.query.filter_by(
                seller_id=seller.id, supplier_id=supplier.id
            ).count()
            suppliers_data.append({
                'supplier': supplier,
                'connection': conn,
                'stats': stats,
                'imported_count': imported_count,
            })

        return render_template('supplier_catalog.html',
                               suppliers_data=suppliers_data)

    @app.route('/supplier-catalog/<int:supplier_id>/products')
    @login_required
    @seller_required
    def supplier_catalog_products(supplier_id):
        """Каталог товаров поставщика для продавца"""
        seller = current_user.seller

        # Проверяем подключение
        conn = SellerSupplier.query.filter_by(
            seller_id=seller.id, supplier_id=supplier_id, is_active=True
        ).first()
        if not conn:
            flash('У вас нет доступа к этому поставщику', 'danger')
            return redirect(url_for('supplier_catalog'))

        supplier = SupplierService.get_supplier(supplier_id)
        if not supplier:
            flash('Поставщик не найден', 'danger')
            return redirect(url_for('supplier_catalog'))

        page = request.args.get('page', 1, type=int)
        search = request.args.get('search', '').strip()
        show_imported = request.args.get('show_imported', '0') == '1'

        pagination = SupplierService.get_available_products_for_seller(
            seller.id, supplier_id,
            page=page, per_page=50,
            search=search, show_imported=show_imported
        )

        # Получаем ID уже импортированных товаров
        imported_sp_ids = set(
            row[0] for row in db.session.query(ImportedProduct.supplier_product_id).filter(
                ImportedProduct.seller_id == seller.id,
                ImportedProduct.supplier_product_id.isnot(None)
            ).all()
        )

        stats = SupplierService.get_product_stats(supplier_id)

        return render_template('supplier_catalog_products.html',
                               supplier=supplier, pagination=pagination,
                               stats=stats, search=search,
                               show_imported=show_imported,
                               imported_sp_ids=imported_sp_ids,
                               connection=conn)

    @app.route('/supplier-catalog/<int:supplier_id>/products/<int:product_id>')
    @login_required
    @seller_required
    def supplier_catalog_product_detail(supplier_id, product_id):
        """Детали товара поставщика (просмотр для продавца)"""
        seller = current_user.seller
        conn = SellerSupplier.query.filter_by(
            seller_id=seller.id, supplier_id=supplier_id, is_active=True
        ).first()
        if not conn:
            flash('У вас нет доступа к этому поставщику', 'danger')
            return redirect(url_for('supplier_catalog'))

        supplier = SupplierService.get_supplier(supplier_id)
        product = SupplierService.get_product(product_id)
        if not supplier or not product or product.supplier_id != supplier_id:
            flash('Товар не найден', 'danger')
            return redirect(url_for('supplier_catalog_products', supplier_id=supplier_id))

        # Проверяем импортирован ли уже
        existing_import = ImportedProduct.query.filter_by(
            seller_id=seller.id, supplier_product_id=product_id
        ).first()

        return render_template('supplier_catalog_product_detail.html',
                               supplier=supplier, product=product,
                               existing_import=existing_import)

    # -------------------------------------------------------------------
    # Импорт товаров к продавцу
    # -------------------------------------------------------------------
    @app.route('/supplier-catalog/import', methods=['POST'])
    @login_required
    @seller_required
    def supplier_catalog_import():
        """Импорт выбранных товаров к продавцу"""
        seller = current_user.seller
        supplier_id = request.form.get('supplier_id', type=int)
        product_ids_raw = request.form.getlist('product_ids')
        product_ids = [int(pid) for pid in product_ids_raw if pid.isdigit()]

        if not product_ids:
            flash('Не выбраны товары для импорта', 'warning')
            return redirect(url_for('supplier_catalog_products', supplier_id=supplier_id))

        result = SupplierService.import_to_seller(seller.id, product_ids)

        if result.success:
            flash(
                f'Импортировано: {result.imported}, '
                f'пропущено (дубли): {result.skipped}, '
                f'ошибок: {result.errors}',
                'success' if result.errors == 0 else 'warning'
            )
        else:
            flash(f'Ошибка импорта: {"; ".join(result.error_messages[:3])}', 'danger')

        return redirect(url_for('supplier_catalog_products', supplier_id=supplier_id))

    # -------------------------------------------------------------------
    # Обновление существующих товаров из базы поставщика
    # -------------------------------------------------------------------
    @app.route('/supplier-catalog/update', methods=['POST'])
    @login_required
    @seller_required
    def supplier_catalog_update():
        """Обновить существующие товары из базы поставщика"""
        seller = current_user.seller
        supplier_id = request.form.get('supplier_id', type=int)

        product_ids_raw = request.form.getlist('product_ids')
        product_ids = [int(pid) for pid in product_ids_raw if pid.isdigit()] if product_ids_raw else None

        result = SupplierService.update_seller_products(seller.id, product_ids)

        flash(
            f'Обновлено: {result.imported}, ошибок: {result.errors}',
            'success' if result.errors == 0 else 'warning'
        )

        if supplier_id:
            return redirect(url_for('supplier_catalog_products', supplier_id=supplier_id))
        return redirect(url_for('supplier_catalog'))


# ============================================================================
# HELPERS
# ============================================================================

def _extract_supplier_form_data(form) -> dict:
    """Извлечь данные поставщика из формы"""
    data = {}
    text_fields = [
        'description', 'website', 'csv_source_url', 'csv_delimiter',
        'csv_encoding', 'api_endpoint', 'auth_login', 'auth_password',
        'ai_provider', 'ai_api_key', 'ai_api_base_url', 'ai_model',
        'ai_client_id', 'ai_client_secret',
        'image_background_color',
        'ai_category_instruction', 'ai_size_instruction',
        'ai_seo_title_instruction', 'ai_keywords_instruction',
        'ai_description_instruction', 'ai_analysis_instruction',
    ]
    for f in text_fields:
        val = form.get(f, '').strip()
        if val:
            data[f] = val

    # Числовые поля
    for f in ('ai_temperature', 'ai_max_tokens', 'ai_timeout',
              'default_markup_percent', 'image_target_size'):
        val = form.get(f)
        if val:
            try:
                data[f] = float(val) if '.' in str(val) else int(val)
            except (ValueError, TypeError):
                pass

    # Булевы поля
    data['ai_enabled'] = form.get('ai_enabled') == 'on'
    data['is_active'] = form.get('is_active') == 'on'
    data['resize_images'] = form.get('resize_images') == 'on'

    return data


def _extract_product_form_data(form) -> dict:
    """Извлечь данные товара поставщика из формы"""
    data = {}
    text_fields = [
        'title', 'description', 'brand', 'category', 'vendor_code',
        'wb_category_name', 'wb_subject_name', 'gender', 'country',
        'season', 'age_group', 'status', 'ai_seo_title', 'ai_description',
    ]
    for f in text_fields:
        val = form.get(f)
        if val is not None:
            data[f] = val.strip()

    # Числовые
    for f in ('wb_subject_id', 'supplier_price', 'supplier_quantity'):
        val = form.get(f)
        if val:
            try:
                data[f] = float(val) if '.' in str(val) else int(val)
            except (ValueError, TypeError):
                pass

    return data
