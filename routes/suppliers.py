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

        completeness = _calc_product_completeness(product)

        return render_template('admin_supplier_product_detail.html',
                               supplier=supplier, product=product,
                               import_count=import_count,
                               price_calc=price_calc,
                               completeness=completeness)

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
        model_override = request.form.get('model_override', '').strip() or None

        result = SupplierService.start_ai_parse_job(
            supplier_id, product_ids,
            admin_user_id=current_user.id,
            max_workers=max_workers,
            model_override=model_override,
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

    @app.route('/admin/suppliers/<int:supplier_id>/ai/parse-by-filter', methods=['POST'])
    @login_required
    @admin_required
    def admin_supplier_ai_parse_by_filter(supplier_id):
        """Массовый AI парсинг: собрать все товары по фильтрам и запустить job."""
        supplier = SupplierService.get_supplier(supplier_id)
        if not supplier:
            return jsonify({'error': 'Поставщик не найден'}), 404

        # Фильтры из формы
        search = request.form.get('search', '').strip()
        stock_status = request.form.get('stock_status', '').strip()
        parse_status = request.form.get('parse_status', '').strip()
        fill_max = request.form.get('fill_max', '').strip()
        limit = request.form.get('limit', 0, type=int)  # 0 = без лимита
        max_workers = request.form.get('max_workers', 4, type=int)
        max_workers = max(1, min(max_workers, 8))
        model_override = request.form.get('model_override', '').strip() or None

        # Строим запрос с фильтрами (аналогично admin_supplier_ai_parser)
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

        if parse_status == 'not_parsed':
            query = query.filter(SupplierProduct.ai_parsed_data_json.is_(None))
        elif parse_status == 'parsed':
            query = query.filter(SupplierProduct.ai_parsed_data_json.isnot(None))
        elif parse_status == 'fill_below' and fill_max:
            try:
                fill_threshold = float(fill_max)
                query = query.filter(
                    db.or_(
                        SupplierProduct.marketplace_fill_pct.is_(None),
                        SupplierProduct.marketplace_fill_pct < fill_threshold
                    )
                )
            except (ValueError, TypeError):
                pass

        # Собираем ID (с опциональным лимитом)
        q = query.with_entities(SupplierProduct.id).order_by(SupplierProduct.title)
        if limit > 0:
            q = q.limit(limit)
        product_ids = [row[0] for row in q.all()]

        if not product_ids:
            return jsonify({'error': 'По фильтрам не найдено товаров'}), 400

        # Ограничение — не больше 10000 за раз
        if len(product_ids) > 10000:
            product_ids = product_ids[:10000]

        result = SupplierService.start_ai_parse_job(
            supplier_id, product_ids,
            admin_user_id=current_user.id,
            max_workers=max_workers,
            model_override=model_override,
        )

        if result.get('error'):
            return jsonify({'error': result['error']}), 400

        log_admin_action(
            admin_user_id=current_user.id,
            action='start_ai_parse_by_filter',
            target_type='supplier',
            target_id=supplier_id,
            details={
                'job_id': result['job_id'],
                'count': result['total'],
                'filters': {
                    'search': search, 'stock_status': stock_status,
                    'parse_status': parse_status, 'fill_max': fill_max,
                    'limit': limit,
                },
            },
            request=request
        )

        return jsonify(result)

    @app.route('/admin/suppliers/<int:supplier_id>/products/<int:product_id>/refresh-data')
    @login_required
    @admin_required
    def admin_supplier_product_refresh_data(supplier_id, product_id):
        """API: свежие данные товара для динамического обновления карточки"""
        product = SupplierService.get_product(product_id)
        if not product or product.supplier_id != supplier_id:
            return jsonify({'error': 'Товар не найден'}), 404

        parsed = product.get_ai_parsed_data()
        pm = parsed.get('parsing_meta', {})
        mp_fields = product.get_marketplace_fields()

        # Собираем статистику заполненности
        completeness = _calc_product_completeness(product)

        return jsonify({
            'title': product.title or '',
            'brand': product.brand or '',
            'description': product.description or '',
            'category': product.category or '',
            'wb_category_name': product.wb_category_name or '',
            'wb_subject_id': product.wb_subject_id or '',
            'gender': product.gender or '',
            'country': product.country or '',
            'ai_seo_title': product.ai_seo_title or '',
            'ai_description': product.ai_description or '',
            'ai_keywords_json': product.ai_keywords_json or '',
            'ai_parsed_data_json': product.ai_parsed_data_json or '',
            'ai_model_used': product.ai_model_used or '',
            'ai_parsed_at': product.ai_parsed_at.strftime('%d.%m.%Y %H:%M') if product.ai_parsed_at else '',
            'parsing_meta': pm,
            'marketplace_fields': mp_fields,
            'marketplace_validation_status': product.marketplace_validation_status or '',
            'marketplace_fill_pct': product.marketplace_fill_pct or 0,
            'completeness': completeness,
        })

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
        parse_status = request.args.get('parse_status', '').strip()
        fill_max = request.args.get('fill_max', '', type=str).strip()
        per_page = request.args.get('per_page', 50, type=int)
        per_page = max(10, min(per_page, 500))
        auto_select = request.args.get('auto_select', '').strip()

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

        # Фильтр по статусу AI парсинга
        if parse_status == 'not_parsed':
            query = query.filter(SupplierProduct.ai_parsed_data_json.is_(None))
        elif parse_status == 'parsed':
            query = query.filter(SupplierProduct.ai_parsed_data_json.isnot(None))
        elif parse_status == 'fill_below' and fill_max:
            try:
                fill_threshold = float(fill_max)
                # Товары, которые не спарсены ИЛИ у которых fill_pct < threshold
                query = query.filter(
                    db.or_(
                        SupplierProduct.marketplace_fill_pct.is_(None),
                        SupplierProduct.marketplace_fill_pct < fill_threshold
                    )
                )
            except (ValueError, TypeError):
                pass

        pagination = query.order_by(SupplierProduct.title).paginate(
            page=page, per_page=per_page, error_out=False
        )

        stats = SupplierService.get_product_stats(supplier_id)

        parsed_count = SupplierProduct.query.filter(
            SupplierProduct.supplier_id == supplier_id,
            SupplierProduct.ai_parsed_data_json.isnot(None)
        ).count()

        active_jobs = SupplierService.get_active_ai_parse_jobs(supplier_id)
        recent_jobs = SupplierService.get_recent_ai_parse_jobs(supplier_id, limit=5)

        # Модели для выбора в парсере
        from services.ai_service import get_available_models
        available_models = get_available_models(supplier.ai_provider or 'cloudru')

        return render_template('admin_supplier_ai_parser.html',
                               supplier=supplier, pagination=pagination,
                               stats=stats, search=search,
                               stock_status=stock_status,
                               parse_status=parse_status,
                               fill_max=fill_max,
                               per_page=per_page,
                               auto_select=auto_select,
                               parsed_count=parsed_count,
                               active_jobs=active_jobs,
                               recent_jobs=recent_jobs,
                               available_models=available_models)

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
    # Мои импортированные товары — просмотр и управление
    # -------------------------------------------------------------------
    @app.route('/my-products')
    @login_required
    @seller_required
    def seller_my_products():
        """Список импортированных товаров продавца с WB-статусами."""
        seller = current_user.seller
        page = request.args.get('page', 1, type=int)
        status = request.args.get('status', '').strip()
        search = request.args.get('search', '').strip()

        query = ImportedProduct.query.filter_by(seller_id=seller.id)
        if status:
            query = query.filter_by(import_status=status)
        else:
            # По умолчанию скрываем товары уже загруженные на WB
            query = query.filter(ImportedProduct.import_status != 'imported')
        if search:
            query = query.filter(
                db.or_(
                    ImportedProduct.title.ilike(f'%{search}%'),
                    ImportedProduct.brand.ilike(f'%{search}%'),
                    ImportedProduct.external_id.ilike(f'%{search}%'),
                )
            )

        query = query.order_by(ImportedProduct.created_at.desc())
        pagination = query.paginate(page=page, per_page=40, error_out=False)

        # Статистика
        base_q = ImportedProduct.query.filter_by(seller_id=seller.id)
        stats = {
            'total': base_q.count(),
            'pending': base_q.filter_by(import_status='pending').count(),
            'validated': base_q.filter_by(import_status='validated').count(),
            'imported': base_q.filter_by(import_status='imported').count(),
            'failed': base_q.filter_by(import_status='failed').count(),
        }

        return render_template(
            'seller_my_products.html',
            pagination=pagination,
            stats=stats,
            status=status,
            search=search,
            has_wb_key=seller.has_valid_api_key(),
        )

    # -------------------------------------------------------------------
    # WB Card Preview — превью карточки в формате WB
    # -------------------------------------------------------------------
    @app.route('/my-products/<int:product_id>/wb-preview')
    @login_required
    @seller_required
    def seller_product_wb_preview(product_id):
        """Превью карточки товара в формате WB."""
        seller = current_user.seller
        product = ImportedProduct.query.filter_by(
            id=product_id, seller_id=seller.id
        ).first_or_404()

        from services.wb_product_importer import WBProductImporter
        importer = WBProductImporter(seller)
        preview = importer.build_wb_card_preview(product)

        return render_template(
            'seller_wb_card_preview.html',
            product=product,
            preview=preview,
            has_wb_key=seller.has_valid_api_key(),
        )

    # -------------------------------------------------------------------
    # WB Card Preview API (JSON)
    # -------------------------------------------------------------------
    @app.route('/my-products/<int:product_id>/wb-preview.json')
    @login_required
    @seller_required
    def seller_product_wb_preview_json(product_id):
        """JSON превью карточки WB (для AJAX)."""
        seller = current_user.seller
        product = ImportedProduct.query.filter_by(
            id=product_id, seller_id=seller.id
        ).first()
        if not product:
            return jsonify({'error': 'Товар не найден'}), 404

        try:
            from services.wb_product_importer import WBProductImporter
            importer = WBProductImporter(seller)
            preview = importer.build_wb_card_preview(product)
            return jsonify(preview)
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    # -------------------------------------------------------------------
    # Push to WB — единичный импорт товара на WB
    # -------------------------------------------------------------------
    @app.route('/my-products/<int:product_id>/push-to-wb', methods=['POST'])
    @login_required
    @seller_required
    def seller_push_to_wb(product_id):
        """Импорт одного товара в магазин WB продавца."""
        seller = current_user.seller
        product = ImportedProduct.query.filter_by(
            id=product_id, seller_id=seller.id
        ).first_or_404()

        if not seller.has_valid_api_key():
            flash('API ключ WB не настроен', 'danger')
            return redirect(url_for('seller_product_wb_preview', product_id=product_id))

        from services.wb_product_importer import WBProductImporter
        importer = WBProductImporter(seller)
        success, error, created = importer.import_product_to_wb(product)

        if success:
            flash(f'Товар "{product.title}" отправлен на WB', 'success')
        else:
            flash(f'Ошибка: {error}', 'danger')

        return redirect(url_for('seller_product_wb_preview', product_id=product_id))

    # -------------------------------------------------------------------
    # Push to WB — массовый импорт товаров на WB
    # -------------------------------------------------------------------
    @app.route('/my-products/push-to-wb-bulk', methods=['POST'])
    @login_required
    @seller_required
    def seller_push_to_wb_bulk():
        """Массовый импорт товаров в магазин WB."""
        seller = current_user.seller

        if not seller.has_valid_api_key():
            return jsonify({'error': 'API ключ WB не настроен'}), 400

        data = request.get_json(silent=True) or {}
        product_ids = data.get('product_ids', [])

        if not product_ids:
            return jsonify({'error': 'Не выбраны товары'}), 400

        try:
            from services.wb_product_importer import WBProductImporter
            importer = WBProductImporter(seller)
            result = importer.import_multiple_products(product_ids)
            return jsonify(result)
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    # -------------------------------------------------------------------
    # Validate product for WB — пометить как валидированный
    # -------------------------------------------------------------------
    @app.route('/my-products/<int:product_id>/validate', methods=['POST'])
    @login_required
    @seller_required
    def seller_validate_product(product_id):
        """Пометить товар как validated (готов к пушу на WB)."""
        seller = current_user.seller
        product = ImportedProduct.query.filter_by(
            id=product_id, seller_id=seller.id
        ).first_or_404()

        # Проверяем минимальные требования
        issues = []
        if not product.title:
            issues.append('Нет названия')
        if not product.brand:
            issues.append('Нет бренда')
        if not product.wb_subject_id:
            issues.append('Нет категории WB')

        if issues:
            flash(f'Невозможно валидировать: {", ".join(issues)}', 'danger')
        else:
            product.import_status = 'validated'
            db.session.commit()
            flash('Товар готов к импорту на WB', 'success')

        return redirect(url_for('seller_product_wb_preview', product_id=product_id))

    # -------------------------------------------------------------------
    # Удалить импортированный товар — сбросить для повторного импорта
    # -------------------------------------------------------------------
    @app.route('/my-products/<int:product_id>/delete', methods=['POST'])
    @login_required
    @seller_required
    def seller_delete_imported_product(product_id):
        """Удалить запись импортированного товара.
        Товар станет снова доступен для импорта из каталога поставщика."""
        seller = current_user.seller
        product = ImportedProduct.query.filter_by(
            id=product_id, seller_id=seller.id
        ).first_or_404()

        title = product.title or product.external_id
        db.session.delete(product)
        db.session.commit()

        flash(f'Товар «{title}» удалён и доступен для повторного импорта', 'success')
        return redirect(url_for('seller_my_products'))

    @app.route('/my-products/delete-bulk', methods=['POST'])
    @login_required
    @seller_required
    def seller_delete_imported_products_bulk():
        """Массовое удаление импортированных товаров."""
        seller = current_user.seller
        data = request.get_json() or {}
        product_ids = data.get('product_ids', [])

        if not product_ids:
            return jsonify({'error': 'Не выбраны товары'}), 400

        deleted = ImportedProduct.query.filter(
            ImportedProduct.id.in_(product_ids),
            ImportedProduct.seller_id == seller.id
        ).delete(synchronize_session=False)
        db.session.commit()

        return jsonify({'deleted': deleted, 'message': f'Удалено {deleted} товаров'})

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

    # -------------------------------------------------------------------
    # Дашборд качества парсинга (HTML)
    # -------------------------------------------------------------------
    @app.route('/admin/suppliers/<int:supplier_id>/parsing-quality/dashboard')
    @login_required
    @admin_required
    def admin_supplier_parsing_quality_dashboard(supplier_id):
        """HTML дашборд качества парсинга."""
        supplier = SupplierService.get_supplier(supplier_id)
        if not supplier:
            flash('Поставщик не найден', 'danger')
            return redirect(url_for('admin_suppliers'))
        return render_template(
            'admin_supplier_parsing_quality.html',
            supplier=supplier,
        )

    # -------------------------------------------------------------------
    # API: Качество парсинга
    # -------------------------------------------------------------------
    @app.route('/admin/suppliers/<int:supplier_id>/parsing-quality')
    @login_required
    @admin_required
    def admin_supplier_parsing_quality(supplier_id):
        """Дашборд качества парсинга для поставщика (JSON API)."""
        from models import ParsingLog
        from services.parsing_confidence import ParsingConfidenceScorer
        from services.ai_parsing_cache import AIParsingCache

        supplier = SupplierService.get_supplier(supplier_id)
        if not supplier:
            return jsonify({'error': 'Supplier not found'}), 404

        # Распределение по качеству
        quality_dist = ParsingConfidenceScorer.get_quality_distribution(supplier_id)

        # Статистика AI-кэша
        cache_stats = AIParsingCache.get_cache_stats(supplier_id)

        # Последние логи парсинга
        recent_logs = ParsingLog.query.filter_by(
            supplier_id=supplier_id
        ).order_by(ParsingLog.created_at.desc()).limit(10).all()

        logs_data = []
        for log in recent_logs:
            logs_data.append({
                'event_type': log.event_type,
                'created_at': log.created_at.isoformat() if log.created_at else None,
                'total_products': log.total_products,
                'processed_successfully': log.processed_successfully,
                'errors_count': log.errors_count,
                'duration_seconds': log.duration_seconds,
                'field_fill_rates': log.field_fill_rates,
                'ai_cache_hits': log.ai_cache_hits,
                'ai_cache_misses': log.ai_cache_misses,
            })

        # Средняя заполненность по полям (из последнего лога)
        field_fill = {}
        if recent_logs and recent_logs[0].field_fill_rates:
            field_fill = recent_logs[0].field_fill_rates

        # Маркетплейсовые характеристики — считаем из БД
        marketplace_fill = {}
        all_sp = SupplierProduct.query.filter_by(supplier_id=supplier_id).limit(5000).all()
        if all_sp:
            total = len(all_sp)
            # WB категория
            marketplace_fill['wb_subject_id'] = round(
                sum(1 for p in all_sp if p.wb_subject_id) / total, 3)
            marketplace_fill['wb_subject_name'] = round(
                sum(1 for p in all_sp if p.wb_subject_name) / total, 3)
            # Уверенность маппинга (средняя по тем, у кого есть)
            confs = [p.category_confidence for p in all_sp if p.category_confidence and p.category_confidence > 0]
            marketplace_fill['category_confidence_avg'] = round(
                sum(confs) / len(confs), 3) if confs else 0.0
            # Характеристики WB
            marketplace_fill['characteristics'] = round(
                sum(1 for p in all_sp if p.characteristics_json and p.characteristics_json not in ('[]', '{}', '')) / total, 3)
            # Размеры
            marketplace_fill['sizes'] = round(
                sum(1 for p in all_sp if p.sizes_json and p.sizes_json not in ('[]', '{}', '')) / total, 3)
            # Габариты
            marketplace_fill['dimensions'] = round(
                sum(1 for p in all_sp if p.dimensions_json and p.dimensions_json not in ('[]', '{}', '')) / total, 3)
            # AI-контент
            marketplace_fill['ai_seo_title'] = round(
                sum(1 for p in all_sp if p.ai_seo_title) / total, 3)
            marketplace_fill['ai_description'] = round(
                sum(1 for p in all_sp if p.ai_description) / total, 3)
            marketplace_fill['ai_keywords'] = round(
                sum(1 for p in all_sp if p.ai_keywords_json and p.ai_keywords_json not in ('[]', '')) / total, 3)
            marketplace_fill['ai_bullets'] = round(
                sum(1 for p in all_sp if p.ai_bullets_json and p.ai_bullets_json not in ('[]', '')) / total, 3)
            # Маркетплейс-специфичные поля
            marketplace_fill['marketplace_data'] = round(
                sum(1 for p in all_sp if p.ai_marketplace_json and p.ai_marketplace_json not in ('{}', '')) / total, 3)
            # Статус валидации
            valid_count = sum(1 for p in all_sp if p.marketplace_validation_status == 'valid')
            partial_count = sum(1 for p in all_sp if p.marketplace_validation_status == 'partial')
            invalid_count = sum(1 for p in all_sp if p.marketplace_validation_status == 'invalid')
            marketplace_fill['validation_stats'] = {
                'valid': valid_count,
                'partial': partial_count,
                'invalid': invalid_count,
                'not_checked': total - valid_count - partial_count - invalid_count,
            }

        return jsonify({
            'supplier': {'id': supplier.id, 'name': supplier.name, 'code': supplier.code},
            'quality_distribution': quality_dist,
            'ai_cache': cache_stats,
            'field_fill_rates': field_fill,
            'marketplace_fill_rates': marketplace_fill,
            'recent_logs': logs_data,
            'total_products': supplier.total_products,
        })

    # -------------------------------------------------------------------
    # API: Проверка фото URL
    # -------------------------------------------------------------------
    @app.route('/admin/suppliers/<int:supplier_id>/verify-photos', methods=['POST'])
    @login_required
    @admin_required
    def admin_supplier_verify_photos(supplier_id):
        """Проверка доступности URL фотографий товаров поставщика."""
        from services.photo_url_verifier import PhotoURLVerifier

        supplier = SupplierService.get_supplier(supplier_id)
        if not supplier:
            return jsonify({'error': 'Supplier not found'}), 404

        limit = request.json.get('limit', 100) if request.is_json else 100
        result = PhotoURLVerifier.verify_supplier_photos(
            supplier_id, limit=min(limit, 500)
        )

        broken_details = []
        for detail in result.details[:50]:
            broken_details.append({
                'product_id': detail.product_id,
                'total_urls': detail.total_urls,
                'valid_urls': detail.valid_urls,
                'broken_urls': detail.broken_urls[:5],
                'errors': dict(list(detail.errors.items())[:5]),
            })

        return jsonify({
            'total_products': result.total_products,
            'products_checked': result.products_checked,
            'products_with_broken_photos': result.products_with_broken_photos,
            'total_urls_checked': result.total_urls_checked,
            'total_broken_urls': result.total_broken_urls,
            'duration_seconds': round(result.duration_seconds, 2),
            'broken_details': broken_details,
        })

    # -------------------------------------------------------------------
    # API: Предпросмотр CSV перед синхронизацией
    # -------------------------------------------------------------------
    @app.route('/admin/suppliers/<int:supplier_id>/csv-preview', methods=['POST'])
    @login_required
    @admin_required
    def admin_supplier_csv_preview(supplier_id):
        """Предпросмотр CSV файла перед синхронизацией."""
        from services.csv_pre_validator import CSVPreValidator
        from services.supplier_service import SupplierCSVParser

        supplier = SupplierService.get_supplier(supplier_id)
        if not supplier:
            return jsonify({'error': 'Supplier not found'}), 404

        parser = SupplierCSVParser(supplier)

        # Пробуем скачать raw для предвалидации
        raw_bytes = parser.fetch_csv_raw()
        if not raw_bytes:
            return jsonify({'error': 'Failed to download CSV'}), 400

        pre_result = CSVPreValidator.validate_raw(
            raw_bytes,
            expected_delimiter=supplier.csv_delimiter,
            expected_encoding=supplier.csv_encoding,
            column_mapping=supplier.csv_column_mapping,
        )

        return jsonify({
            'is_valid': pre_result.is_valid,
            'encoding_detected': pre_result.encoding_detected,
            'encoding_confidence': pre_result.encoding_confidence,
            'delimiter_detected': pre_result.delimiter_detected,
            'total_rows': pre_result.total_rows,
            'columns_count': pre_result.columns_count,
            'empty_rows': pre_result.empty_rows,
            'duplicate_ids': pre_result.duplicate_ids,
            'sample_products': pre_result.sample_products,
            'warnings': pre_result.warnings,
            'errors': pre_result.errors,
            'field_fill_rates': pre_result.field_fill_rates,
        })

    # -------------------------------------------------------------------
    # Страница Smart Parser — дашборд умного парсинга
    # -------------------------------------------------------------------
    @app.route('/admin/suppliers/<int:supplier_id>/smart-parser')
    @login_required
    @admin_required
    def admin_supplier_smart_parser(supplier_id):
        """Дашборд умного парсинга товаров поставщика."""
        supplier = SupplierService.get_supplier(supplier_id)
        if not supplier:
            abort(404)

        # Статистика
        total = SupplierProduct.query.filter_by(supplier_id=supplier_id).count()
        brand_resolved = SupplierProduct.query.filter(
            SupplierProduct.supplier_id == supplier_id,
            SupplierProduct.resolved_brand_id.isnot(None),
        ).count()
        brand_unresolved = SupplierProduct.query.filter(
            SupplierProduct.supplier_id == supplier_id,
            SupplierProduct.brand.isnot(None),
            SupplierProduct.brand != '',
            SupplierProduct.resolved_brand_id.is_(None),
        ).count()
        category_mapped = SupplierProduct.query.filter(
            SupplierProduct.supplier_id == supplier_id,
            SupplierProduct.wb_subject_id.isnot(None),
        ).count()
        in_stock = SupplierProduct.query.filter(
            SupplierProduct.supplier_id == supplier_id,
            SupplierProduct.supplier_quantity.isnot(None),
            SupplierProduct.supplier_quantity > 0,
        ).count()

        # Средняя готовность
        avg_q = db.session.query(
            db.func.avg(SupplierProduct.parsing_confidence)
        ).filter(
            SupplierProduct.supplier_id == supplier_id,
            SupplierProduct.parsing_confidence.isnot(None),
        ).scalar()
        avg_readiness = round((avg_q or 0) * 100)

        stats = {
            'total': total,
            'brand_resolved': brand_resolved,
            'brand_unresolved': brand_unresolved,
            'category_mapped': category_mapped,
            'avg_readiness': avg_readiness,
            'in_stock': in_stock,
        }

        return render_template(
            'admin_supplier_smart_parser.html',
            supplier=supplier,
            stats=stats,
        )

    # -------------------------------------------------------------------
    # API: Smart Product Parser — умный парсинг товаров
    # -------------------------------------------------------------------
    @app.route('/admin/suppliers/<int:supplier_id>/smart-parse', methods=['POST'])
    @login_required
    @admin_required
    def admin_supplier_smart_parse(supplier_id):
        """
        Запуск умного парсинга для выбранных товаров.
        Для больших наборов (>2000) запускает фоновую задачу.
        """
        try:
            from services.smart_product_parser import SmartProductParser

            supplier = SupplierService.get_supplier(supplier_id)
            if not supplier:
                return jsonify({'error': 'Supplier not found'}), 404

            data = request.get_json(silent=True) or {}
            product_ids = data.get('product_ids', [])

            # Если не указаны конкретные — берём все товары поставщика
            if not product_ids:
                scope = data.get('scope', 'all')  # all, unparsed, draft
                query = SupplierProduct.query.filter_by(supplier_id=supplier_id)
                if scope == 'unparsed':
                    query = query.filter(
                        db.or_(
                            SupplierProduct.resolved_brand_id.is_(None),
                            SupplierProduct.parsing_confidence.is_(None),
                            SupplierProduct.parsing_confidence < 0.5,
                        )
                    )
                elif scope == 'draft':
                    query = query.filter_by(status='draft')

                product_ids = [p.id for p in query.all()]

            if not product_ids:
                return jsonify({'error': 'Нет товаров для парсинга'}), 400

            # Для больших наборов — фоновая задача
            if len(product_ids) > 2000:
                result = SmartProductParser.start_background_job(
                    supplier_id=supplier_id,
                    product_ids=product_ids,
                    admin_user_id=current_user.id,
                    marketplace_id=data.get('marketplace_id'),
                    scope=data.get('scope', 'all'),
                )
                log_admin_action(
                    admin_user_id=current_user.id,
                    action='smart_parse_job_start',
                    details=f'supplier={supplier.code}, products={len(product_ids)}, job={result["job_id"][:8]}'
                )
                return jsonify({'background': True, **result})

            # Для небольших — синхронно
            parser = SmartProductParser(
                supplier_id=supplier_id,
                marketplace_id=data.get('marketplace_id'),
            )
            result = parser.parse_and_apply_bulk(product_ids)

            log_admin_action(
                admin_user_id=current_user.id,
                action='smart_parse',
                details=f'supplier={supplier.code}, products={len(product_ids)}, '
                        f'brands={result.brand_resolved_count}, '
                        f'cats={result.category_mapped_count}'
            )

            return jsonify(result.to_dict())
        except Exception as e:
            logger.exception(f"Smart parse error for supplier {supplier_id}")
            return jsonify({'error': f'Ошибка парсинга: {str(e)}'}), 500

    @app.route('/admin/suppliers/<int:supplier_id>/smart-parse/<int:product_id>', methods=['POST'])
    @login_required
    @admin_required
    def admin_supplier_smart_parse_single(supplier_id, product_id):
        """Умный парсинг одного товара."""
        try:
            from services.smart_product_parser import SmartProductParser

            supplier = SupplierService.get_supplier(supplier_id)
            if not supplier:
                return jsonify({'error': 'Supplier not found'}), 404

            parser = SmartProductParser(supplier_id=supplier_id)
            result = parser.parse_and_apply_single(product_id)

            return jsonify(result)
        except Exception as e:
            logger.exception(f"Smart parse single error: product {product_id}")
            return jsonify({'error': f'Ошибка: {str(e)}'}), 500

    # -------------------------------------------------------------------
    # API: Smart Parse — статус и отмена фоновой задачи
    # -------------------------------------------------------------------
    @app.route('/admin/suppliers/<int:supplier_id>/smart-parse-job/<job_id>/status')
    @login_required
    @admin_required
    def admin_supplier_smart_parse_job_status(supplier_id, job_id):
        """Статус фоновой задачи Smart Parse (поллинг)."""
        from services.smart_product_parser import SmartProductParser
        data = SmartProductParser.get_job_status(job_id)
        if not data:
            return jsonify({'error': 'Задача не найдена'}), 404
        return jsonify(data)

    @app.route('/admin/suppliers/<int:supplier_id>/smart-parse-job/<job_id>/cancel', methods=['POST'])
    @login_required
    @admin_required
    def admin_supplier_smart_parse_job_cancel(supplier_id, job_id):
        """Отмена фоновой задачи Smart Parse."""
        from services.smart_product_parser import SmartProductParser
        ok = SmartProductParser.cancel_job(job_id)
        return jsonify({'cancelled': ok})

    # -------------------------------------------------------------------
    # API: Валидация характеристик
    # -------------------------------------------------------------------
    @app.route('/admin/suppliers/<int:supplier_id>/validate-characteristics', methods=['POST'])
    @login_required
    @admin_required
    def admin_supplier_validate_characteristics(supplier_id):
        """
        Валидация и автокоррекция AI-извлечённых характеристик.
        Проверяет по справочнику WB, исправляет fuzzy-совпадения.
        """
        try:
            from services.smart_product_parser import CharacteristicsValidator

            supplier = SupplierService.get_supplier(supplier_id)
            if not supplier:
                return jsonify({'error': 'Supplier not found'}), 404

            data = request.get_json(silent=True) or {}
            product_ids = data.get('product_ids', [])
            auto_correct = data.get('auto_correct', True)

            # Если не указаны — берём все с AI-данными
            if not product_ids:
                query = SupplierProduct.query.filter(
                    SupplierProduct.supplier_id == supplier_id,
                    SupplierProduct.ai_marketplace_json.isnot(None),
                )
                product_ids = [p.id for p in query.all()]

            if not product_ids:
                return jsonify({'error': 'Нет товаров для валидации'}), 400

            result = CharacteristicsValidator.validate_bulk(
                product_ids, auto_correct=auto_correct
            )

            log_admin_action(
                admin_user_id=current_user.id,
                action='validate_characteristics',
                details=f'supplier={supplier.code}, products={len(product_ids)}, '
                        f'valid={result["valid"]}, errors={result["with_errors"]}, '
                        f'corrections={result["corrections_applied"]}'
            )

            return jsonify(result)
        except Exception as e:
            logger.exception(f"Characteristics validation error for supplier {supplier_id}")
            return jsonify({'error': f'Ошибка валидации: {str(e)}'}), 500

    @app.route('/admin/suppliers/<int:supplier_id>/validate-characteristics/<int:product_id>')
    @login_required
    @admin_required
    def admin_supplier_validate_single(supplier_id, product_id):
        """Валидация характеристик одного товара (GET для просмотра)."""
        from services.smart_product_parser import CharacteristicsValidator

        result = CharacteristicsValidator.validate_product(
            product_id, auto_correct=False
        )
        return jsonify(result.to_dict())

    # -------------------------------------------------------------------
    # API: Валидация брендов на маркетплейсе
    # -------------------------------------------------------------------
    @app.route('/admin/suppliers/<int:supplier_id>/validate-brands', methods=['POST'])
    @login_required
    @admin_required
    def admin_supplier_validate_brands(supplier_id):
        """
        Массовая валидация брендов поставщика на маркетплейсе.
        Проверяет fuzzy matching с брендами WB.
        """
        try:
            from services.smart_product_parser import SmartProductParser, SmartParseResult

            supplier = SupplierService.get_supplier(supplier_id)
            if not supplier:
                return jsonify({'error': 'Supplier not found'}), 404

            data = request.get_json(silent=True) or {}
            marketplace_id = data.get('marketplace_id')

            # Получаем уникальные бренды поставщика
            query = db.session.query(
                SupplierProduct.brand, db.func.count(SupplierProduct.id).label('cnt')
            ).filter(
                SupplierProduct.supplier_id == supplier_id,
                SupplierProduct.brand.isnot(None),
                SupplierProduct.brand != '',
            ).group_by(SupplierProduct.brand).order_by(db.desc('cnt'))

            brand_rows = query.all()

            parser = SmartProductParser(
                supplier_id=supplier_id,
                marketplace_id=marketplace_id,
            )

            results = []
            resolved = 0
            unresolved = 0

            for brand_name, count in brand_rows:
                result = SmartParseResult()
                parser._resolve_brand(result, brand_name, '')
                parser._validate_brand_on_marketplace(result)

                status = 'valid' if result.brand_marketplace_valid else (
                    'uncertain' if result.brand_marketplace_valid is None else 'invalid'
                )

                if result.brand_marketplace_valid:
                    resolved += 1
                else:
                    unresolved += 1

                results.append({
                    'supplier_brand': brand_name,
                    'products_count': count,
                    'resolved': result.brand_resolved,
                    'canonical': result.brand_canonical,
                    'confidence': result.brand_confidence,
                    'marketplace_status': status,
                    'marketplace_name': result.brand_marketplace_name,
                    'marketplace_id': result.brand_marketplace_id,
                    'suggestions': result.brand_marketplace_suggestions[:5],
                    'warnings': result.warnings[:3],
                })

            return jsonify({
                'total_brands': len(brand_rows),
                'resolved_on_marketplace': resolved,
                'unresolved_on_marketplace': unresolved,
                'brands': results,
            })
        except Exception as e:
            logger.exception(f"Brand validation error for supplier {supplier_id}")
            return jsonify({'error': f'Ошибка валидации брендов: {str(e)}'}), 500

    # -------------------------------------------------------------------
    # API: Применить результаты валидации брендов
    # -------------------------------------------------------------------
    @app.route('/admin/suppliers/<int:supplier_id>/apply-brands', methods=['POST'])
    @login_required
    @admin_required
    def admin_supplier_apply_brands(supplier_id):
        """
        Применить валидацию брендов: обновить resolved_brand_id у товаров,
        создать MarketplaceBrand записи для найденных совпадений.
        """
        try:
            from services.smart_product_parser import SmartProductParser

            supplier = SupplierService.get_supplier(supplier_id)
            if not supplier:
                return jsonify({'error': 'Supplier not found'}), 404

            data = request.get_json(silent=True) or {}
            marketplace_id = data.get('marketplace_id')

            parser = SmartProductParser(
                supplier_id=supplier_id,
                marketplace_id=marketplace_id,
            )
            result = parser.apply_brand_validation(
                supplier_id=supplier_id,
                marketplace_id=marketplace_id,
            )

            log_admin_action(
                admin_user_id=current_user.id,
                action='apply_brand_validation',
                details=f'supplier={supplier.code}, brands={result["total_brands"]}, '
                        f'resolved={result["resolved"]}, '
                        f'marketplace={result["marketplace_matched"]}, '
                        f'products_updated={result["products_updated"]}'
            )

            return jsonify(result)
        except Exception as e:
            logger.exception(f"Apply brand validation error for supplier {supplier_id}")
            return jsonify({'error': f'Ошибка: {str(e)}'}), 500

    # -------------------------------------------------------------------
    # API: Smart Import к продавцу (обогащение + импорт)
    # -------------------------------------------------------------------
    @app.route('/supplier-catalog/smart-import', methods=['POST'])
    @login_required
    @seller_required
    def supplier_catalog_smart_import():
        """
        Умный импорт: SmartParse + импорт к продавцу.
        Обогащает данные перед копированием в ImportedProduct.
        """
        from services.smart_product_parser import SmartProductParser

        seller = current_user.seller
        supplier_id = request.form.get('supplier_id', type=int)
        product_ids_raw = request.form.getlist('product_ids')
        product_ids = [int(pid) for pid in product_ids_raw if pid.isdigit()]

        if not product_ids:
            flash('Не выбраны товары для импорта', 'warning')
            return redirect(url_for('supplier_catalog_products', supplier_id=supplier_id))

        parser = SmartProductParser(supplier_id=supplier_id)
        result = parser.smart_import_to_seller(seller.id, product_ids)

        if result['success']:
            imp = result['import_result']
            parse = result['parse_result']
            flash(
                f'Импортировано: {imp["imported"]}, '
                f'пропущено: {imp["skipped"]}, '
                f'бренды определены: {parse["brand_resolved_count"]}, '
                f'категории: {parse["category_mapped_count"]}, '
                f'готовность: {parse["avg_readiness_score"]:.0f}%',
                'success'
            )
        else:
            flash(
                f'Ошибка импорта: {"; ".join(result.get("import_result", {}).get("error_messages", [])[:3])}',
                'danger'
            )

        return redirect(url_for('supplier_catalog_products', supplier_id=supplier_id))


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
    data['csv_has_header'] = form.get('csv_has_header') == 'on'

    # JSON поля
    csv_column_mapping = form.get('csv_column_mapping', '').strip()
    if csv_column_mapping:
        try:
            data['csv_column_mapping'] = json.loads(csv_column_mapping)
        except (json.JSONDecodeError, TypeError):
            pass

    return data


def _calc_product_completeness(product) -> dict:
    """Расчёт заполненности полей товара по группам"""
    import json

    def _has(val):
        if val is None:
            return False
        if isinstance(val, str):
            val = val.strip()
            if not val or val in ('[]', '{}', '""'):
                return False
            # JSON arrays/objects — check for content
            if val.startswith('[') or val.startswith('{'):
                try:
                    parsed = json.loads(val)
                    return bool(parsed)
                except Exception:
                    pass
            return True
        if isinstance(val, (int, float)):
            return True
        return bool(val)

    groups = {
        'basic': {
            'label': 'Основные',
            'fields': {
                'Название': _has(product.title),
                'Описание': _has(product.description),
                'Бренд': _has(product.brand),
                'Категория': _has(product.category),
                'Артикул': _has(product.vendor_code),
                'Штрихкод': _has(product.barcode),
            }
        },
        'price': {
            'label': 'Цены и остатки',
            'fields': {
                'Цена поставщика': _has(product.supplier_price),
                'Остаток': _has(product.supplier_quantity),
                'РРЦ': _has(product.recommended_retail_price),
                'Статус наличия': _has(product.supplier_status),
            }
        },
        'characteristics': {
            'label': 'Характеристики',
            'fields': {
                'Пол': _has(product.gender),
                'Страна': _has(product.country),
                'Сезон': _has(product.season),
                'Возрастная группа': _has(product.age_group),
                'Цвета': _has(product.colors_json),
                'Материалы': _has(product.materials_json),
                'Размеры': _has(product.sizes_json),
                'Габариты': _has(product.dimensions_json),
                'Характеристики': _has(product.characteristics_json),
            }
        },
        'ai': {
            'label': 'AI данные',
            'fields': {
                'SEO заголовок': _has(product.ai_seo_title),
                'AI описание': _has(product.ai_description),
                'Ключевые слова': _has(product.ai_keywords_json),
                'Буллеты': _has(product.ai_bullets_json),
                'AI парсинг': _has(product.ai_parsed_data_json),
            }
        },
        'marketplace': {
            'label': 'Маркетплейс',
            'fields': {
                'Категория WB': _has(product.wb_category_name),
                'WB Subject ID': _has(product.wb_subject_id),
                'Поля маркетплейса': _has(product.marketplace_fields_json),
                'Валидация': product.marketplace_validation_status == 'valid',
            }
        },
        'media': {
            'label': 'Медиа',
            'fields': {
                'Фотографии': bool(product.get_photos()),
                'Видео': _has(product.video_url),
            }
        },
    }

    # Считаем по каждой группе
    total_filled = 0
    total_fields = 0
    for key, group in groups.items():
        filled = sum(1 for v in group['fields'].values() if v)
        count = len(group['fields'])
        group['filled'] = filled
        group['total'] = count
        group['pct'] = round(filled / count * 100) if count else 0
        total_filled += filled
        total_fields += count

    return {
        'groups': {k: {
            'label': g['label'],
            'pct': g['pct'],
            'filled': g['filled'],
            'total': g['total'],
            'fields': g['fields'],
        } for k, g in groups.items()},
        'total_pct': round(total_filled / total_fields * 100) if total_fields else 0,
        'total_filled': total_filled,
        'total_fields': total_fields,
    }


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
