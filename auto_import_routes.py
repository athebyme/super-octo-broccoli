# -*- coding: utf-8 -*-
"""
Роуты для автоимпорта товаров
Эти роуты нужно добавить в seller_platform.py
"""
from flask import render_template, redirect, url_for, flash, request, jsonify
from flask_login import login_required, current_user
import json
import threading

from models import db, AutoImportSettings, ImportedProduct, CategoryMapping
from auto_import_manager import AutoImportManager


def register_auto_import_routes(app):
    """
    Регистрирует роуты для автоимпорта в Flask приложении

    Args:
        app: Flask приложение
    """

    @app.route('/auto-import', methods=['GET'])
    @login_required
    def auto_import_dashboard():
        """Дашборд автоимпорта"""
        if not current_user.seller:
            flash('Для работы с автоимпортом обратитесь к администратору.', 'warning')
            return redirect(url_for('dashboard'))

        seller = current_user.seller

        # Получаем или создаем настройки
        settings = AutoImportSettings.query.filter_by(seller_id=seller.id).first()
        if not settings:
            settings = AutoImportSettings(seller_id=seller.id)
            db.session.add(settings)
            db.session.commit()

        # Статистика импортированных товаров
        total_imported = ImportedProduct.query.filter_by(
            seller_id=seller.id,
            import_status='imported'
        ).count()

        pending_import = ImportedProduct.query.filter_by(
            seller_id=seller.id,
            import_status='validated'
        ).count()

        failed_import = ImportedProduct.query.filter_by(
            seller_id=seller.id,
            import_status='failed'
        ).count()

        # Последние импортированные товары
        recent_products = ImportedProduct.query.filter_by(
            seller_id=seller.id
        ).order_by(ImportedProduct.created_at.desc()).limit(10).all()

        return render_template(
            'auto_import_dashboard.html',
            settings=settings,
            total_imported=total_imported,
            pending_import=pending_import,
            failed_import=failed_import,
            recent_products=recent_products
        )

    @app.route('/auto-import/settings', methods=['GET', 'POST'])
    @login_required
    def auto_import_settings():
        """Страница настроек автоимпорта"""
        if not current_user.seller:
            flash('Для работы с автоимпортом обратитесь к администратору.', 'warning')
            return redirect(url_for('dashboard'))

        seller = current_user.seller
        settings = AutoImportSettings.query.filter_by(seller_id=seller.id).first()

        if not settings:
            settings = AutoImportSettings(seller_id=seller.id)
            db.session.add(settings)
            db.session.commit()

        if request.method == 'POST':
            # Обновляем настройки
            settings.is_enabled = request.form.get('is_enabled') == 'on'
            settings.supplier_code = request.form.get('supplier_code', '').strip()
            settings.vendor_code_pattern = request.form.get('vendor_code_pattern', 'id-{product_id}-{supplier_code}').strip()
            settings.csv_source_url = request.form.get('csv_source_url', '').strip()
            settings.csv_source_type = request.form.get('csv_source_type', 'sexoptovik')
            settings.import_only_new = request.form.get('import_only_new') == 'on'
            settings.auto_enable_products = request.form.get('auto_enable_products') == 'on'
            settings.use_blurred_images = request.form.get('use_blurred_images') == 'on'
            settings.resize_images_to_1200 = request.form.get('resize_images_to_1200') == 'on'
            settings.image_background_color = request.form.get('image_background_color', 'white').strip()

            try:
                settings.auto_import_interval_hours = int(request.form.get('auto_import_interval_hours', 24))
            except ValueError:
                settings.auto_import_interval_hours = 24

            db.session.commit()
            flash('Настройки автоимпорта сохранены', 'success')
            return redirect(url_for('auto_import_dashboard'))

        return render_template('auto_import_settings.html', settings=settings)

    @app.route('/auto-import/products', methods=['GET'])
    @login_required
    def auto_import_products():
        """Список импортированных товаров"""
        if not current_user.seller:
            flash('Для работы с автоимпортом обратитесь к администратору.', 'warning')
            return redirect(url_for('dashboard'))

        seller = current_user.seller

        # Фильтры
        status_filter = request.args.get('status', '')
        page = int(request.args.get('page', 1))
        per_page = 50

        query = ImportedProduct.query.filter_by(seller_id=seller.id)

        if status_filter:
            query = query.filter_by(import_status=status_filter)

        pagination = query.order_by(ImportedProduct.created_at.desc()).paginate(
            page=page, per_page=per_page, error_out=False
        )

        products = pagination.items

        return render_template(
            'auto_import_products.html',
            products=products,
            pagination=pagination,
            status_filter=status_filter
        )

    @app.route('/auto-import/run', methods=['POST'])
    @login_required
    def auto_import_run():
        """Запуск импорта вручную"""
        if not current_user.seller:
            return jsonify({'success': False, 'error': 'Seller not found'}), 403

        seller = current_user.seller
        settings = AutoImportSettings.query.filter_by(seller_id=seller.id).first()

        if not settings:
            return jsonify({'success': False, 'error': 'Settings not configured'}), 400

        if not settings.csv_source_url:
            return jsonify({'success': False, 'error': 'CSV source URL not configured'}), 400

        if not settings.supplier_code:
            return jsonify({'success': False, 'error': 'Supplier code not configured'}), 400

        # Проверяем, не идет ли импорт уже
        if settings.last_import_status == 'running':
            return jsonify({'success': False, 'error': 'Import is already running'}), 400

        # Запускаем импорт в фоновом потоке
        def run_import_background():
            from seller_platform import app
            with app.app_context():
                manager = AutoImportManager(seller, settings)
                manager.run_import()

        thread = threading.Thread(target=run_import_background)
        thread.daemon = True
        thread.start()

        flash('Импорт запущен. Процесс может занять несколько минут.', 'info')
        return jsonify({'success': True, 'message': 'Import started'})

    @app.route('/auto-import/product/<int:product_id>', methods=['GET'])
    @login_required
    def auto_import_product_detail(product_id):
        """Детали импортированного товара"""
        if not current_user.seller:
            flash('Для работы с автоимпортом обратитесь к администратору.', 'warning')
            return redirect(url_for('dashboard'))

        seller = current_user.seller
        product = ImportedProduct.query.filter_by(
            id=product_id,
            seller_id=seller.id
        ).first_or_404()

        # Парсим JSON поля
        try:
            product.colors_list = json.loads(product.colors) if product.colors else []
        except:
            product.colors_list = []

        try:
            product.sizes_list = json.loads(product.sizes) if product.sizes else []
        except:
            product.sizes_list = []

        try:
            product.materials_list = json.loads(product.materials) if product.materials else []
        except:
            product.materials_list = []

        try:
            product.photo_urls_list = json.loads(product.photo_urls) if product.photo_urls else []
        except:
            product.photo_urls_list = []

        try:
            product.barcodes_list = json.loads(product.barcodes) if product.barcodes else []
        except:
            product.barcodes_list = []

        try:
            product.validation_errors_list = json.loads(product.validation_errors) if product.validation_errors else []
        except:
            product.validation_errors_list = []

        return render_template('auto_import_product_detail.html', product=product)

    @app.route('/auto-import/categories', methods=['GET'])
    @login_required
    def auto_import_categories():
        """Управление маппингом категорий"""
        if not current_user.is_admin:
            flash('Только администраторы могут управлять маппингом категорий', 'danger')
            return redirect(url_for('auto_import_dashboard'))

        mappings = CategoryMapping.query.order_by(
            CategoryMapping.source_type,
            CategoryMapping.source_category
        ).all()

        return render_template('auto_import_categories.html', mappings=mappings)


# Пример использования:
# from auto_import_routes import register_auto_import_routes
# register_auto_import_routes(app)
