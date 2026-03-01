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
    ImportedProduct, Seller, AIHistory, log_admin_action
)
from services.supplier_service import SupplierService

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
        price_stock_stats = SupplierService.get_price_stock_stats(supplier_id)
        sellers = SupplierService.get_supplier_sellers(supplier_id)
        return render_template('admin_supplier_form.html',
                               supplier=supplier, mode='edit',
                               stats=stats, price_stock_stats=price_stock_stats,
                               sellers=sellers)

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
    # Синхронизация цен и остатков
    # -------------------------------------------------------------------
    @app.route('/admin/suppliers/<int:supplier_id>/sync-prices', methods=['POST'])
    @login_required
    @admin_required
    def admin_supplier_sync_prices(supplier_id):
        supplier = SupplierService.get_supplier(supplier_id)
        if not supplier:
            flash('Поставщик не найден', 'danger')
            return redirect(url_for('admin_suppliers'))

        if not supplier.price_file_url:
            flash('URL файла цен не задан для этого поставщика', 'warning')
            return redirect(url_for('admin_supplier_edit', supplier_id=supplier_id))

        force = request.form.get('force', '0') == '1'
        result = SupplierService.sync_prices_and_stock(supplier_id, force=force)

        # Каскадное обновление к продавцам
        cascade_result = None
        if result.success and result.updated > 0:
            cascade_result = SupplierService.cascade_prices_to_sellers(supplier_id)

        log_admin_action(
            admin_user_id=current_user.id,
            action='sync_supplier_prices',
            target_type='supplier',
            target_id=supplier_id,
            details={
                'updated': result.updated,
                'errors': result.errors,
                'duration': round(result.duration_seconds, 1),
                'cascade_updated': cascade_result['updated'] if cascade_result else 0,
            },
            request=request
        )

        if result.success:
            msg = (f'Синхронизация цен: {result.updated} обновлено, '
                   f'{result.errors} ошибок ({result.duration_seconds:.1f}с)')
            if cascade_result and cascade_result['updated'] > 0:
                msg += f' | Обновлено у продавцов: {cascade_result["updated"]}'
            flash(msg, 'success')
        else:
            flash(f'Ошибка синхронизации цен: {"; ".join(result.error_messages[:3])}', 'danger')

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
        stock_status = request.args.get('stock_status', '').strip()
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
            ai_validated=ai_val, stock_status=stock_status or None,
            sort_by=sort_by, sort_dir=sort_dir
        )

        stats = SupplierService.get_product_stats(supplier_id)
        price_stock_stats = SupplierService.get_price_stock_stats(supplier_id)

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
                               stats=stats, price_stock_stats=price_stock_stats,
                               brands=brands, categories=categories,
                               search=search, current_status=status,
                               current_brand=brand, current_category=category,
                               ai_validated=ai_validated,
                               stock_status=stock_status,
                               sort_by=sort_by, sort_dir=sort_dir)

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

        # Расчёт розничной цены (если есть закупочная)
        price_calc = None
        if product.supplier_price and product.supplier_price > 0:
            try:
                from services.pricing_engine import calculate_price
                price_calc = calculate_price(product.supplier_price)
            except Exception:
                pass

        return render_template('admin_supplier_product_detail.html',
                               supplier=supplier, product=product,
                               import_count=import_count,
                               price_calc=price_calc)

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
            result = SupplierService.ai_validate_bulk(supplier_id, product_ids)
            flash(f'AI валидация: {result.get("validated", 0)} успешно, {result.get("errors", 0)} ошибок', 'success')

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
    # AI операции с товарами поставщика
    # -------------------------------------------------------------------
    @app.route('/admin/suppliers/<int:supplier_id>/products/<int:product_id>/validate_marketplace', methods=['POST'])
    @login_required
    @admin_required
    def admin_supplier_product_validate_marketplace(supplier_id, product_id):
        """Интерактивная валидация JSON данных товара для маркетплейса"""
        supplier = SupplierService.get_supplier(supplier_id)
        product = SupplierService.get_product(product_id)
        if not supplier or not product or product.supplier_id != supplier_id:
            return jsonify({'success': False, 'error': 'Product not found'}), 404
            
        data = request.json
        fields = data.get('marketplace_fields', {})
        
        from services.marketplace_validator import MarketplaceValidator
        
        # Determine marketplace and category
        marketplace_id = data.get('marketplace_id') or 1 # default WB
        subject_id = data.get('subject_id') or product.wb_subject_id
        
        if not subject_id:
            return jsonify({
                'success': False, 
                'error': 'Category (subject_id) is required for validation.'
            }), 400
            
        validator = MarketplaceValidator(marketplace_id)
        validation_result = validator.validate_product_data(subject_id, fields)
        
        # Optional: Save back to product
        return jsonify({
            'success': True,
            'is_valid': validation_result['is_valid'],
            'fill_percentage': validation_result['fill_percentage'],
            'errors': validation_result['errors']
        })

    @app.route('/admin/suppliers/<int:supplier_id>/marketplace_bulk_validate', methods=['POST'])
    @login_required
    @admin_required
    def admin_supplier_marketplace_bulk_validate(supplier_id):
        """Bulk auto-map and validation of marketplace fields for supplier products"""
        product_ids_raw = request.form.getlist('product_ids')
        product_ids = [int(pid) for pid in product_ids_raw if pid.isdigit()]

        if not product_ids:
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return jsonify({'error': 'Не указаны товары'}), 400
            flash('Не указаны товары', 'warning')
            return redirect(url_for('admin_supplier_products', supplier_id=supplier_id))

        result = SupplierService.start_bulk_marketplace_validation(
            supplier_id, product_ids,
            admin_user_id=current_user.id
        )

        if result.get('error'):
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return jsonify({'error': result['error']}), 400
            flash(result['error'], 'danger')
            return redirect(url_for('admin_supplier_products', supplier_id=supplier_id))

        log_admin_action(
            admin_user_id=current_user.id,
            action='start_marketplace_bulk_validation',
            target_type='supplier',
            target_id=supplier_id,
            details={'job_id': result['job_id'], 'count': result['total']},
            request=request
        )

        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify(result)

        flash(f'Bulk marketplace mapping & validation started in background ({result["total"]} products)', 'success')
        return redirect(url_for('admin_supplier_ai_parser', supplier_id=supplier_id))


    @app.route('/admin/suppliers/<int:supplier_id>/ai/validate', methods=['POST'])
    @login_required
    @admin_required
    def admin_supplier_ai_validate(supplier_id):
        """AI валидация товаров поставщика"""
        product_ids_raw = request.form.getlist('product_ids')
        product_ids = [int(pid) for pid in product_ids_raw if pid.isdigit()]
        single_product_id = request.form.get('product_id', type=int)

        if single_product_id:
            result = SupplierService.ai_validate_product(single_product_id)
            log_admin_action(
                admin_user_id=current_user.id,
                action='ai_validate_supplier_product',
                target_type='supplier_product',
                target_id=single_product_id,
                details={'supplier_id': supplier_id, 'success': result.get('success'), 'score': result.get('score')},
                request=request
            )
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return jsonify(result)
            if result.get('success'):
                flash(f'AI валидация завершена. Оценка: {result.get("score", 0):.0f}%', 'success')
            else:
                flash(f'Ошибка AI валидации: {result.get("error", "?")}', 'danger')
            return redirect(url_for('admin_supplier_product_detail',
                                    supplier_id=supplier_id, product_id=single_product_id))

        elif product_ids:
            result = SupplierService.ai_validate_bulk(supplier_id, product_ids)
            log_admin_action(
                admin_user_id=current_user.id,
                action='ai_validate_bulk_supplier_products',
                target_type='supplier',
                target_id=supplier_id,
                details={'count': len(product_ids), 'validated': result.get('validated'), 'errors': result.get('errors')},
                request=request
            )
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return jsonify(result)
            flash(f'AI валидация: {result.get("validated", 0)} успешно, {result.get("errors", 0)} ошибок', 'success')
            return redirect(url_for('admin_supplier_products', supplier_id=supplier_id))

        flash('Не выбраны товары', 'warning')
        return redirect(url_for('admin_supplier_products', supplier_id=supplier_id))

    @app.route('/admin/suppliers/<int:supplier_id>/ai/generate-seo', methods=['POST'])
    @login_required
    @admin_required
    def admin_supplier_ai_seo(supplier_id):
        """AI генерация SEO заголовка"""
        product_id = request.form.get('product_id', type=int)
        if not product_id:
            flash('Не указан товар', 'warning')
            return redirect(url_for('admin_supplier_products', supplier_id=supplier_id))

        result = SupplierService.ai_generate_seo(product_id)
        log_admin_action(
            admin_user_id=current_user.id,
            action='ai_generate_seo_supplier_product',
            target_type='supplier_product',
            target_id=product_id,
            details={'supplier_id': supplier_id, 'success': result.get('success')},
            request=request
        )

        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify(result)

        if result.get('success'):
            flash(f'SEO заголовок сгенерирован: {result.get("title", "")}', 'success')
        else:
            flash(f'Ошибка генерации SEO: {result.get("error", "?")}', 'danger')
        return redirect(url_for('admin_supplier_product_detail',
                                supplier_id=supplier_id, product_id=product_id))

    @app.route('/admin/suppliers/<int:supplier_id>/ai/generate-desc', methods=['POST'])
    @login_required
    @admin_required
    def admin_supplier_ai_desc(supplier_id):
        """AI генерация описания"""
        product_id = request.form.get('product_id', type=int)
        if not product_id:
            flash('Не указан товар', 'warning')
            return redirect(url_for('admin_supplier_products', supplier_id=supplier_id))

        result = SupplierService.ai_generate_description(product_id)
        log_admin_action(
            admin_user_id=current_user.id,
            action='ai_generate_desc_supplier_product',
            target_type='supplier_product',
            target_id=product_id,
            details={'supplier_id': supplier_id, 'success': result.get('success')},
            request=request
        )

        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify(result)

        if result.get('success'):
            flash('AI описание сгенерировано', 'success')
        else:
            flash(f'Ошибка генерации описания: {result.get("error", "?")}', 'danger')
        return redirect(url_for('admin_supplier_product_detail',
                                supplier_id=supplier_id, product_id=product_id))

    @app.route('/admin/suppliers/<int:supplier_id>/ai/analyze', methods=['POST'])
    @login_required
    @admin_required
    def admin_supplier_ai_analyze(supplier_id):
        """AI анализ товара"""
        product_id = request.form.get('product_id', type=int)
        if not product_id:
            flash('Не указан товар', 'warning')
            return redirect(url_for('admin_supplier_products', supplier_id=supplier_id))

        result = SupplierService.ai_analyze_product(product_id)
        log_admin_action(
            admin_user_id=current_user.id,
            action='ai_analyze_supplier_product',
            target_type='supplier_product',
            target_id=product_id,
            details={'supplier_id': supplier_id, 'success': result.get('success'), 'score': result.get('score')},
            request=request
        )

        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify(result)

        if result.get('success'):
            flash(f'AI анализ завершён. Оценка: {result.get("score", 0):.0f}%', 'success')
        else:
            flash(f'Ошибка AI анализа: {result.get("error", "?")}', 'danger')
        return redirect(url_for('admin_supplier_product_detail',
                                supplier_id=supplier_id, product_id=product_id))

    @app.route('/admin/suppliers/<int:supplier_id>/ai/enrich', methods=['POST'])
    @login_required
    @admin_required
    def admin_supplier_ai_enrich(supplier_id):
        """Полное AI обогащение товара (SEO + описание + ключевые слова + анализ)"""
        product_id = request.form.get('product_id', type=int)
        if not product_id:
            flash('Не указан товар', 'warning')
            return redirect(url_for('admin_supplier_products', supplier_id=supplier_id))

        result = SupplierService.ai_full_enrich(product_id)
        log_admin_action(
            admin_user_id=current_user.id,
            action='ai_full_enrich_supplier_product',
            target_type='supplier_product',
            target_id=product_id,
            details={'supplier_id': supplier_id, 'success': result.get('success')},
            request=request
        )

        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify(result)

        if result.get('success'):
            flash('AI обогащение завершено', 'success')
        else:
            errors_str = '; '.join(result.get('errors', []))
            flash(f'AI обогащение частично завершено: {errors_str}', 'warning')
        return redirect(url_for('admin_supplier_product_detail',
                                supplier_id=supplier_id, product_id=product_id))

    @app.route('/admin/suppliers/<int:supplier_id>/ai/history')
    @login_required
    @admin_required
    def admin_supplier_ai_history(supplier_id):
        """История AI операций для товаров поставщика"""
        supplier = SupplierService.get_supplier(supplier_id)
        if not supplier:
            flash('Поставщик не найден', 'danger')
            return redirect(url_for('admin_suppliers'))

        page = request.args.get('page', 1, type=int)

        # Получаем ID всех ImportedProduct, связанных с этим поставщиком
        subq = db.session.query(ImportedProduct.id).filter(
            ImportedProduct.supplier_id == supplier_id
        ).subquery()

        history_query = AIHistory.query.filter(
            AIHistory.imported_product_id.in_(subq)
        ).order_by(AIHistory.created_at.desc())

        pagination = history_query.paginate(page=page, per_page=50, error_out=False)

        return render_template('admin_supplier_ai_history.html',
                               supplier=supplier, pagination=pagination)

    # -------------------------------------------------------------------
    # AI ПОЛНЫЙ ПАРСИНГ ТОВАРА
    # -------------------------------------------------------------------

    @app.route('/admin/suppliers/<int:supplier_id>/ai/parse', methods=['POST'])
    @login_required
    @admin_required
    def admin_supplier_ai_parse(supplier_id):
        """AI парсинг одного или нескольких товаров — запуск в фоне"""
        product_id = request.form.get('product_id', type=int)
        product_ids_raw = request.form.getlist('product_ids')
        product_ids = [int(pid) for pid in product_ids_raw if pid.isdigit()]

        # Одиночный товар
        if product_id and not product_ids:
            product_ids = [product_id]

        if not product_ids:
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return jsonify({'error': 'Не указаны товары'}), 400
            flash('Не указаны товары', 'warning')
            return redirect(url_for('admin_supplier_products', supplier_id=supplier_id))

        max_workers = request.form.get('max_workers', 4, type=int)
        max_workers = max(1, min(max_workers, 8))

        result = SupplierService.start_ai_parse_job(
            supplier_id, product_ids,
            admin_user_id=current_user.id,
            max_workers=max_workers,
        )

        if result.get('error'):
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return jsonify({'error': result['error']}), 400
            flash(result['error'], 'danger')
            return redirect(url_for('admin_supplier_products', supplier_id=supplier_id))

        log_admin_action(
            admin_user_id=current_user.id,
            action='start_ai_parse_job',
            target_type='supplier',
            target_id=supplier_id,
            details={'job_id': result['job_id'], 'count': result['total']},
            request=request
        )

        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify(result)

        flash(f'AI парсинг запущен в фоне ({result["total"]} товаров)', 'success')
        return redirect(url_for('admin_supplier_ai_parser', supplier_id=supplier_id))

    @app.route('/admin/suppliers/<int:supplier_id>/ai/parse-job/<job_id>/status')
    @login_required
    @admin_required
    def admin_supplier_ai_parse_status(supplier_id, job_id):
        """API: статус фоновой задачи AI парсинга (поллинг)"""
        data = SupplierService.get_ai_parse_job(job_id)
        if not data:
            return jsonify({'error': 'Задача не найдена'}), 404
        return jsonify(data)

    @app.route('/admin/suppliers/<int:supplier_id>/ai/parse-job/<job_id>/cancel', methods=['POST'])
    @login_required
    @admin_required
    def admin_supplier_ai_parse_cancel(supplier_id, job_id):
        """API: отмена фоновой задачи AI парсинга"""
        ok = SupplierService.cancel_ai_parse_job(job_id)
        return jsonify({'cancelled': ok})

    @app.route('/admin/suppliers/<int:supplier_id>/products/<int:product_id>/raw-json')
    @login_required
    @admin_required
    def admin_supplier_product_raw_json(supplier_id, product_id):
        """Полный JSON дамп товара для анализа"""
        supplier = SupplierService.get_supplier(supplier_id)
        product = SupplierService.get_product(product_id)
        if not supplier or not product or product.supplier_id != supplier_id:
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return jsonify({'error': 'Товар не найден'}), 404
            flash('Товар не найден', 'danger')
            return redirect(url_for('admin_supplier_products', supplier_id=supplier_id))

        raw_data = SupplierService.get_product_raw_json(product_id)

        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify(raw_data)

        return render_template('admin_supplier_product_raw_json.html',
                               supplier=supplier, product=product,
                               raw_data=json.dumps(raw_data, ensure_ascii=False, indent=2))

    # -------------------------------------------------------------------
    # СИНХРОНИЗАЦИЯ ОПИСАНИЙ (фон)
    # -------------------------------------------------------------------

    @app.route('/admin/suppliers/<int:supplier_id>/marketplaces', methods=['GET'])
    @login_required
    @admin_required
    def admin_supplier_marketplaces(supplier_id):
        """Интеграции с маркетплейсами для поставщика"""
        supplier = Supplier.query.get_or_404(supplier_id)
        from models import Marketplace, MarketplaceConnection, MarketplaceCategory
        marketplaces = Marketplace.query.all()
        connections_qs = MarketplaceConnection.query.filter_by(supplier_id=supplier.id).all()
        connections = {c.marketplace_id: c for c in connections_qs}

        # Pre-load only enabled categories per marketplace for dropdown (avoid loading 10k+ categories)
        enabled_categories = {}
        for mp in marketplaces:
            enabled_categories[mp.id] = MarketplaceCategory.query.filter_by(
                marketplace_id=mp.id, is_enabled=True
            ).order_by(MarketplaceCategory.subject_name).all()

        return render_template('admin_supplier_marketplaces.html',
                               supplier=supplier,
                               marketplaces=marketplaces,
                               connections=connections,
                               enabled_categories=enabled_categories)

    @app.route('/admin/suppliers/<int:supplier_id>/marketplaces/update', methods=['POST'])
    @login_required
    @admin_required
    def admin_supplier_marketplaces_update(supplier_id):
        """Обновление настроек интеграции"""
        supplier = Supplier.query.get_or_404(supplier_id)
        data = request.json
        marketplace_id = data.get('marketplace_id')
        if not marketplace_id:
            return jsonify({"success": False, "error": "Missing marketplace_id"}), 400
            
        from models import MarketplaceConnection, db
        conn = MarketplaceConnection.query.filter_by(supplier_id=supplier.id, marketplace_id=marketplace_id).first()
        if not conn:
            conn = MarketplaceConnection(supplier_id=supplier.id, marketplace_id=marketplace_id)
            db.session.add(conn)
            
        if 'is_active' in data:
            conn.is_active = bool(data['is_active'])
        if 'auto_map_categories' in data:
            conn.auto_map_categories = bool(data['auto_map_categories'])
        if 'default_category_id' in data:
            try:
                conn.default_category_id = int(data['default_category_id']) if data['default_category_id'] else None
            except (ValueError, TypeError):
                conn.default_category_id = None
                
        db.session.commit()
        return jsonify({"success": True})


    @app.route('/admin/suppliers/<int:supplier_id>/sync-descriptions', methods=['POST'])
    @login_required
    @admin_required
    def admin_supplier_sync_descriptions(supplier_id):
        """Синхронизация описаний — запуск в фоне"""
        supplier = SupplierService.get_supplier(supplier_id)
        if not supplier:
            flash('Поставщик не найден', 'danger')
            return redirect(url_for('admin_suppliers'))

        if not supplier.description_file_url:
            flash('URL файла описаний не задан', 'warning')
            return redirect(url_for('admin_supplier_edit', supplier_id=supplier_id))

        result = SupplierService.start_description_sync_job(
            supplier_id, admin_user_id=current_user.id
        )

        log_admin_action(
            admin_user_id=current_user.id,
            action='sync_supplier_descriptions',
            target_type='supplier',
            target_id=supplier_id,
            details={'job_id': result.get('job_id')},
            request=request
        )

        if result.get('error'):
            flash(f'Ошибка: {result["error"]}', 'danger')
        else:
            flash('Синхронизация описаний запущена в фоне', 'success')

        return redirect(url_for('admin_supplier_ai_parser', supplier_id=supplier_id))

    # -------------------------------------------------------------------
    # AI ПАРСЕР — СТРАНИЦА
    # -------------------------------------------------------------------

    @app.route('/admin/suppliers/<int:supplier_id>/ai/parser')
    @login_required
    @admin_required
    def admin_supplier_ai_parser(supplier_id):
        """Страница AI парсера с выбором товаров"""
        supplier = SupplierService.get_supplier(supplier_id)
        if not supplier:
            flash('Поставщик не найден', 'danger')
            return redirect(url_for('admin_suppliers'))

        page = request.args.get('page', 1, type=int)
        search = request.args.get('search', '').strip()
        stock_status = request.args.get('stock_status', '').strip()

        query = SupplierProduct.query.filter_by(supplier_id=supplier_id)
        if search:
            search_term = f'%{search}%'
            query = query.filter(
                db.or_(
                    SupplierProduct.title.ilike(search_term),
                    SupplierProduct.external_id.ilike(search_term),
                    SupplierProduct.brand.ilike(search_term),
                )
            )
        if stock_status == 'in_stock':
            query = query.filter(SupplierProduct.supplier_status == 'in_stock')
        elif stock_status == 'out_of_stock':
            query = query.filter(SupplierProduct.supplier_status == 'out_of_stock')

        pagination = query.order_by(SupplierProduct.title).paginate(
            page=page, per_page=50, error_out=False
        )

        stats = SupplierService.get_product_stats(supplier_id)

        parsed_count = SupplierProduct.query.filter(
            SupplierProduct.supplier_id == supplier_id,
            SupplierProduct.ai_parsed_data_json.isnot(None)
        ).count()

        active_jobs = SupplierService.get_active_ai_parse_jobs(supplier_id)
        recent_jobs = SupplierService.get_recent_ai_parse_jobs(supplier_id, limit=5)

        return render_template('admin_supplier_ai_parser.html',
                               supplier=supplier, pagination=pagination,
                               stats=stats, search=search,
                               stock_status=stock_status,
                               parsed_count=parsed_count,
                               active_jobs=active_jobs,
                               recent_jobs=recent_jobs)

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
        # По умолчанию показываем только товары в наличии
        stock_status = request.args.get('stock_status', 'in_stock').strip()
        if stock_status not in ('in_stock', 'out_of_stock', 'all'):
            stock_status = 'in_stock'
        effective_stock = stock_status if stock_status != 'all' else None

        pagination = SupplierService.get_available_products_for_seller(
            seller.id, supplier_id,
            page=page, per_page=50,
            search=search, show_imported=show_imported,
            stock_status=effective_stock
        )

        # Получаем ID уже импортированных товаров
        imported_sp_ids = set(
            row[0] for row in db.session.query(ImportedProduct.supplier_product_id).filter(
                ImportedProduct.seller_id == seller.id,
                ImportedProduct.supplier_product_id.isnot(None)
            ).all()
        )

        stats = SupplierService.get_product_stats(supplier_id)
        price_stock_stats = SupplierService.get_price_stock_stats(supplier_id)

        return render_template('supplier_catalog_products.html',
                               supplier=supplier, pagination=pagination,
                               stats=stats, search=search,
                               show_imported=show_imported,
                               stock_status=stock_status,
                               price_stock_stats=price_stock_stats,
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
        'price_file_url', 'price_file_inf_url', 'price_file_delimiter',
        'price_file_encoding',
        'description_file_url', 'description_file_delimiter', 'description_file_encoding',
        'ai_provider', 'ai_api_key', 'ai_api_base_url', 'ai_model',
        'ai_client_id', 'ai_client_secret',
        'image_background_color',
        'ai_category_instruction', 'ai_size_instruction',
        'ai_seo_title_instruction', 'ai_keywords_instruction',
        'ai_description_instruction', 'ai_analysis_instruction',
        'ai_parsing_instruction',
    ]
    for f in text_fields:
        val = form.get(f, '').strip()
        if val:
            data[f] = val

    # Числовые поля
    for f in ('ai_temperature', 'ai_max_tokens', 'ai_timeout',
              'default_markup_percent', 'image_target_size',
              'auto_sync_interval_minutes'):
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
    data['auto_sync_prices'] = form.get('auto_sync_prices') == 'on'

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
