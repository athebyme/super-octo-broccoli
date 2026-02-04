# -*- coding: utf-8 -*-
"""
Роуты для автоимпорта товаров
Эти роуты нужно добавить в seller_platform.py
"""
from flask import render_template, redirect, url_for, flash, request, jsonify, send_file
from flask_login import login_required, current_user
import json
import threading
import logging
import time

from models import db, AutoImportSettings, ImportedProduct, CategoryMapping
from auto_import_manager import AutoImportManager, ImageProcessor

logger = logging.getLogger(__name__)


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
            settings.csv_delimiter = request.form.get('csv_delimiter', ';').strip() or ';'
            settings.import_only_new = request.form.get('import_only_new') == 'on'
            settings.auto_enable_products = request.form.get('auto_enable_products') == 'on'
            settings.use_blurred_images = request.form.get('use_blurred_images') == 'on'
            settings.resize_images_to_1200 = request.form.get('resize_images_to_1200') == 'on'
            settings.image_background_color = request.form.get('image_background_color', 'white').strip()

            # Авторизация Sexoptovik
            settings.sexoptovik_login = request.form.get('sexoptovik_login', '').strip()
            settings.sexoptovik_password = request.form.get('sexoptovik_password', '').strip()

            # AI настройки
            settings.ai_enabled = request.form.get('ai_enabled') == 'on'
            settings.ai_provider = request.form.get('ai_provider', 'cloudru')
            settings.ai_api_key = request.form.get('ai_api_key', '').strip()
            settings.ai_api_base_url = request.form.get('ai_api_base_url', '').strip()
            settings.ai_model = request.form.get('ai_model', 'openai/gpt-oss-120b').strip()
            settings.ai_use_for_categories = request.form.get('ai_use_for_categories') == 'on'
            settings.ai_use_for_sizes = request.form.get('ai_use_for_sizes') == 'on'
            # Cloud.ru OAuth2 credentials
            settings.ai_client_id = request.form.get('ai_client_id', '').strip()
            settings.ai_client_secret = request.form.get('ai_client_secret', '').strip()

            try:
                settings.ai_temperature = float(request.form.get('ai_temperature', 0.3))
            except ValueError:
                settings.ai_temperature = 0.3

            try:
                settings.ai_max_tokens = int(request.form.get('ai_max_tokens', 2000))
            except ValueError:
                settings.ai_max_tokens = 2000

            try:
                settings.ai_timeout = int(request.form.get('ai_timeout', 60))
            except ValueError:
                settings.ai_timeout = 60

            try:
                settings.ai_category_confidence_threshold = float(request.form.get('ai_category_confidence_threshold', 0.7))
            except ValueError:
                settings.ai_category_confidence_threshold = 0.7

            # Дополнительные AI параметры
            try:
                settings.ai_top_p = float(request.form.get('ai_top_p', 0.95))
            except ValueError:
                settings.ai_top_p = 0.95

            try:
                settings.ai_presence_penalty = float(request.form.get('ai_presence_penalty', 0.0))
            except ValueError:
                settings.ai_presence_penalty = 0.0

            try:
                settings.ai_frequency_penalty = float(request.form.get('ai_frequency_penalty', 0.0))
            except ValueError:
                settings.ai_frequency_penalty = 0.0

            # Кастомные инструкции AI
            settings.ai_category_instruction = request.form.get('ai_category_instruction', '').strip() or None
            settings.ai_size_instruction = request.form.get('ai_size_instruction', '').strip() or None

            # Сбрасываем AI сервис при изменении настроек
            if settings.ai_enabled:
                try:
                    from ai_service import reset_ai_service
                    reset_ai_service()
                except ImportError:
                    pass

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

        # Получаем список всех WB категорий для dropdown
        from wb_categories_mapping import WB_ADULT_CATEGORIES
        wb_categories = WB_ADULT_CATEGORIES

        return render_template('auto_import_product_detail.html', product=product, wb_categories=wb_categories)

    @app.route('/auto-import/validate', methods=['GET'])
    @login_required
    def auto_import_validate():
        """Страница валидации товаров с низкой уверенностью определения категории"""
        if not current_user.seller:
            flash('Для работы с автоимпортом обратитесь к администратору.', 'warning')
            return redirect(url_for('dashboard'))

        seller = current_user.seller

        # Параметры пагинации и фильтрации
        page = request.args.get('page', 1, type=int)
        per_page = request.args.get('per_page', 20, type=int)
        min_confidence = float(request.args.get('min_confidence', 0.9))
        max_confidence = float(request.args.get('max_confidence', 1.0))
        category_filter = request.args.get('category', '')

        # Базовый запрос
        query = ImportedProduct.query.filter(
            ImportedProduct.seller_id == seller.id,
            ImportedProduct.category_confidence < min_confidence
        )

        # Дополнительные фильтры
        if max_confidence < 1.0:
            query = query.filter(ImportedProduct.category_confidence <= max_confidence)

        if category_filter:
            query = query.filter(ImportedProduct.mapped_wb_category.like(f'%{category_filter}%'))

        # Подсчет общего количества
        total_count = query.count()

        # Получаем товары с пагинацией
        pagination = query.order_by(ImportedProduct.category_confidence.asc()).paginate(
            page=page, per_page=per_page, error_out=False
        )

        low_confidence_products = pagination.items

        # Парсим JSON поля для каждого товара
        for product in low_confidence_products:
            try:
                product.all_categories_list = json.loads(product.all_categories) if product.all_categories else []
            except:
                product.all_categories_list = []

        # Получаем список всех WB категорий для dropdown
        from wb_categories_mapping import WB_ADULT_CATEGORIES
        wb_categories = WB_ADULT_CATEGORIES

        return render_template('auto_import_validate.html',
                             products=low_confidence_products,
                             wb_categories=wb_categories,
                             min_confidence=min_confidence,
                             max_confidence=max_confidence,
                             category_filter=category_filter,
                             total_count=total_count,
                             pagination=pagination,
                             page=page,
                             per_page=per_page)

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

    @app.route('/auto-import/import-to-wb', methods=['POST'])
    @login_required
    def auto_import_to_wb():
        """Массовый импорт товаров в WB"""
        if not current_user.seller:
            return jsonify({'success': False, 'error': 'Seller not found'}), 403

        seller = current_user.seller

        # Получаем список товаров для импорта
        product_ids_str = request.form.get('product_ids', '')
        if not product_ids_str:
            return jsonify({'success': False, 'error': 'No products selected'}), 400

        try:
            product_ids = [int(pid) for pid in product_ids_str.split(',')]
        except ValueError:
            return jsonify({'success': False, 'error': 'Invalid product IDs'}), 400

        # Импортируем товары
        from wb_product_importer import import_products_batch
        result = import_products_batch(seller.id, product_ids)

        if result.get('success'):
            message = f"Импортировано: {result['success']}, Ошибок: {result['failed']}, Пропущено: {result['skipped']}"
            flash(message, 'success' if result['failed'] == 0 else 'warning')
            return jsonify(result)
        else:
            return jsonify(result), 500

    @app.route('/auto-import/product/<int:product_id>/import', methods=['POST'])
    @login_required
    def auto_import_single_to_wb(product_id):
        """Импорт одного товара в WB"""
        if not current_user.seller:
            return jsonify({'success': False, 'error': 'Seller not found'}), 403

        seller = current_user.seller
        imported_product = ImportedProduct.query.filter_by(
            id=product_id,
            seller_id=seller.id
        ).first()

        if not imported_product:
            return jsonify({'success': False, 'error': 'Product not found'}), 404

        # Импортируем товар
        from wb_product_importer import WBProductImporter
        importer = WBProductImporter(seller)
        success, error, product = importer.import_product_to_wb(imported_product)

        if success:
            flash(f'Товар "{imported_product.title}" успешно импортирован в WB', 'success')
            return jsonify({
                'success': True,
                'product_id': product.id if product else None
            })
        else:
            flash(f'Ошибка импорта: {error}', 'danger')
            return jsonify({'success': False, 'error': error}), 500

    @app.route('/auto-import/product/<int:product_id>/delete', methods=['POST'])
    @login_required
    def auto_import_delete_product(product_id):
        """Удаление одного товара"""
        if not current_user.seller:
            return jsonify({'success': False, 'error': 'Seller not found'}), 403

        seller = current_user.seller
        product = ImportedProduct.query.filter_by(
            id=product_id,
            seller_id=seller.id
        ).first()

        if not product:
            return jsonify({'success': False, 'error': 'Product not found'}), 404

        title = product.title
        db.session.delete(product)
        db.session.commit()

        flash(f'Товар "{title}" удален', 'success')
        return jsonify({'success': True})

    @app.route('/auto-import/products/delete', methods=['POST'])
    @login_required
    def auto_import_delete_products():
        """Массовое удаление товаров"""
        if not current_user.seller:
            return jsonify({'success': False, 'error': 'Seller not found'}), 403

        seller = current_user.seller

        # Получаем список товаров для удаления
        product_ids_str = request.form.get('product_ids', '')
        if not product_ids_str:
            return jsonify({'success': False, 'error': 'No products selected'}), 400

        try:
            product_ids = [int(pid) for pid in product_ids_str.split(',')]
        except ValueError:
            return jsonify({'success': False, 'error': 'Invalid product IDs'}), 400

        # Удаляем товары
        deleted_count = ImportedProduct.query.filter(
            ImportedProduct.id.in_(product_ids),
            ImportedProduct.seller_id == seller.id
        ).delete(synchronize_session=False)

        db.session.commit()

        flash(f'Удалено товаров: {deleted_count}', 'success')
        return jsonify({'success': True, 'deleted': deleted_count})

    @app.route('/auto-import/products/delete-all', methods=['POST'])
    @login_required
    def auto_import_delete_all():
        """Удаление всех товаров"""
        if not current_user.seller:
            return jsonify({'success': False, 'error': 'Seller not found'}), 403

        seller = current_user.seller

        # Получаем фильтр статуса (опционально)
        status_filter = request.form.get('status', None)

        # Удаляем товары
        query = ImportedProduct.query.filter_by(seller_id=seller.id)

        if status_filter:
            query = query.filter_by(import_status=status_filter)

        deleted_count = query.delete(synchronize_session=False)
        db.session.commit()

        message = f'Удалено товаров: {deleted_count}'
        if status_filter:
            message += f' (статус: {status_filter})'

        flash(message, 'success')
        return jsonify({'success': True, 'deleted': deleted_count})

    @app.route('/auto-import/product/<int:product_id>/correct-category', methods=['POST'])
    @login_required
    def auto_import_correct_category(product_id):
        """Сохраняет ручное исправление категории для товара"""
        if not current_user.seller:
            return jsonify({'success': False, 'error': 'Seller not found'}), 403

        seller = current_user.seller
        product = ImportedProduct.query.filter_by(
            id=product_id,
            seller_id=seller.id
        ).first()

        if not product:
            return jsonify({'success': False, 'error': 'Product not found'}), 404

        # Получаем новую категорию
        data = request.get_json()
        new_wb_subject_id = data.get('wb_subject_id')

        if not new_wb_subject_id:
            return jsonify({'success': False, 'error': 'Category ID is required'}), 400

        try:
            # Получаем название категории
            from wb_categories_mapping import WB_ADULT_CATEGORIES
            new_wb_subject_name = WB_ADULT_CATEGORIES.get(new_wb_subject_id)

            if not new_wb_subject_name:
                return jsonify({'success': False, 'error': 'Invalid category ID'}), 400

            # Проверяем, есть ли уже исправление для этого товара
            from models import ProductCategoryCorrection
            correction = ProductCategoryCorrection.query.filter_by(
                external_id=product.external_id,
                source_type=product.source_type
            ).first()

            if correction:
                # Обновляем существующее исправление
                correction.corrected_wb_subject_id = new_wb_subject_id
                correction.corrected_wb_subject_name = new_wb_subject_name
                correction.corrected_by_user_id = current_user.id
                correction.product_title = product.title
                correction.original_category = product.category
                from datetime import datetime
                correction.updated_at = datetime.utcnow()
            else:
                # Создаем новое исправление
                correction = ProductCategoryCorrection(
                    imported_product_id=product.id,
                    external_id=product.external_id,
                    source_type=product.source_type,
                    product_title=product.title,
                    original_category=product.category,
                    corrected_wb_subject_id=new_wb_subject_id,
                    corrected_wb_subject_name=new_wb_subject_name,
                    corrected_by_user_id=current_user.id
                )
                db.session.add(correction)

            # Обновляем категорию в самом товаре
            product.wb_subject_id = new_wb_subject_id
            product.mapped_wb_category = new_wb_subject_name
            product.category_confidence = 1.0  # Максимальная уверенность для ручных исправлений

            db.session.commit()

            flash(f'Категория товара обновлена на "{new_wb_subject_name}"', 'success')
            return jsonify({
                'success': True,
                'new_category_id': new_wb_subject_id,
                'new_category_name': new_wb_subject_name
            })

        except Exception as e:
            db.session.rollback()
            return jsonify({'success': False, 'error': str(e)}), 500

    @app.route('/auto-import/product/<int:product_id>/update', methods=['POST'])
    @login_required
    def auto_import_update_product(product_id):
        """Обновляет данные товара и перезапускает валидацию"""
        if not current_user.seller:
            return jsonify({'success': False, 'error': 'Seller not found'}), 403

        seller = current_user.seller
        product = ImportedProduct.query.filter_by(
            id=product_id,
            seller_id=seller.id
        ).first()

        if not product:
            return jsonify({'success': False, 'error': 'Product not found'}), 404

        try:
            data = request.get_json()

            # Обновляем поля
            if 'title' in data:
                product.title = data['title']

            if 'brand' in data:
                product.brand = data['brand']

            if 'barcodes' in data:
                barcodes = data['barcodes']
                if isinstance(barcodes, str):
                    barcodes = [b.strip() for b in barcodes.split(',') if b.strip()]
                product.barcodes = json.dumps(barcodes, ensure_ascii=False)

            if 'wb_subject_id' in data:
                from wb_categories_mapping import WB_ADULT_CATEGORIES
                new_id = data['wb_subject_id']
                if new_id in WB_ADULT_CATEGORIES:
                    product.wb_subject_id = new_id
                    product.mapped_wb_category = WB_ADULT_CATEGORIES[new_id]
                    product.category_confidence = 1.0

            # Перезапускаем валидацию
            from auto_import_manager import ProductValidator

            # Собираем данные для валидации
            product_data = {
                'title': product.title,
                'external_vendor_code': product.external_vendor_code,
                'category': product.category,
                'brand': product.brand,
                'barcodes': json.loads(product.barcodes) if product.barcodes else [],
                'photo_urls': json.loads(product.photo_urls) if product.photo_urls else [],
                'colors': json.loads(product.colors) if product.colors else [],
                'sizes': json.loads(product.sizes) if product.sizes else [],
                'wb_subject_id': product.wb_subject_id
            }

            is_valid, errors = ProductValidator.validate_product(product_data)

            if is_valid:
                product.import_status = 'validated'
                product.validation_errors = None
            else:
                product.import_status = 'failed'
                product.validation_errors = json.dumps(errors, ensure_ascii=False)

            db.session.commit()

            return jsonify({
                'success': True,
                'is_valid': is_valid,
                'errors': errors if not is_valid else [],
                'new_status': product.import_status
            })

        except Exception as e:
            db.session.rollback()
            return jsonify({'success': False, 'error': str(e)}), 500

    @app.route('/auto-import/recalculate-categories', methods=['POST'])
    @login_required
    def auto_import_recalculate_categories():
        """
        Пересчитывает категории для всех товаров с учетом ручных исправлений
        Применяет все исправления из ProductCategoryCorrection к остальным товарам
        """
        if not current_user.seller:
            return jsonify({'success': False, 'error': 'Seller not found'}), 403

        seller = current_user.seller

        try:
            from wb_categories_mapping import get_best_category_match

            # Получаем все товары с низкой уверенностью (< 95%)
            products_to_recalculate = ImportedProduct.query.filter(
                ImportedProduct.seller_id == seller.id,
                ImportedProduct.category_confidence < 0.95
            ).all()

            updated_count = 0
            improved_count = 0

            for product in products_to_recalculate:
                # Парсим все категории
                try:
                    all_categories = json.loads(product.all_categories) if product.all_categories else []
                except:
                    all_categories = []

                # Заново определяем категорию (get_best_category_match автоматически
                # проверит таблицу ProductCategoryCorrection и применит исправления)
                new_wb_id, new_wb_name, new_confidence = get_best_category_match(
                    csv_category=product.category,
                    product_title=product.title,
                    all_categories=all_categories,
                    external_id=product.external_id,
                    source_type=product.source_type
                )

                # Проверяем, изменилась ли категория или уверенность
                old_confidence = product.category_confidence or 0.0
                category_changed = (new_wb_id != product.wb_subject_id)
                confidence_improved = (new_confidence > old_confidence)

                if category_changed or confidence_improved:
                    product.wb_subject_id = new_wb_id
                    product.mapped_wb_category = new_wb_name
                    product.category_confidence = new_confidence
                    updated_count += 1

                    if confidence_improved:
                        improved_count += 1

            db.session.commit()

            return jsonify({
                'success': True,
                'total_checked': len(products_to_recalculate),
                'updated_count': updated_count,
                'improved_count': improved_count
            })

        except Exception as e:
            db.session.rollback()
            return jsonify({'success': False, 'error': str(e)}), 500

    # Простой файловый кэш для картинок
    import hashlib
    import os
    PHOTO_CACHE_DIR = os.path.join(os.path.dirname(__file__), 'photo_cache')
    os.makedirs(PHOTO_CACHE_DIR, exist_ok=True)

    def get_photo_cache_path(url: str) -> str:
        """Генерирует путь к кэшированному файлу"""
        url_hash = hashlib.md5(url.encode()).hexdigest()
        return os.path.join(PHOTO_CACHE_DIR, f"{url_hash}.jpg")

    @app.route('/auto-import/photo/padded', methods=['GET'])
    def auto_import_photo_padded():
        """
        Возвращает фото с примененным padding до 1200x1200
        Query params:
            url: URL исходного фото
            bg_color: Цвет фона для padding (по умолчанию 'white')
            fallback_blur: URL альтернативного изображения (blur)
            fallback_original: URL альтернативного изображения (original)
        """
        photo_url = request.args.get('url')
        bg_color = request.args.get('bg_color', 'white')
        fallback_blur = request.args.get('fallback_blur')
        fallback_original = request.args.get('fallback_original')

        if not photo_url:
            return jsonify({'error': 'URL параметр обязателен'}), 400

        # Проверяем кэш
        cache_path = get_photo_cache_path(photo_url)
        if os.path.exists(cache_path):
            # Проверяем возраст кэша (24 часа)
            cache_age = time.time() - os.path.getmtime(cache_path)
            if cache_age < 86400:  # 24 часа
                logger.info(f"📦 Кэш найден для: {photo_url[:50]}...")
                return send_file(cache_path, mimetype='image/jpeg')

        try:
            logger.info(f"🖼️  Запрос обработки фото: {photo_url}")

            # Собираем fallback URLs
            fallback_urls = []
            if fallback_blur:
                fallback_urls.append(fallback_blur)
            if fallback_original:
                fallback_urls.append(fallback_original)

            # Автоматически формируем fallback URLs для sexoptovik
            if 'sexoptovik.ru' in photo_url and not fallback_urls:
                # Извлекаем ID и номер из URL
                import re
                match = re.search(r'/(\d+)/(\d+)_(\d+)_1200\.jpg', photo_url)
                if match:
                    product_id, _, photo_num = match.groups()
                    fallback_urls = [
                        f"https://x-story.ru/mp/_project/img_sx0_1200/{product_id}_{photo_num}_1200.jpg",
                        f"https://x-story.ru/mp/_project/img_sx_1200/{product_id}_{photo_num}_1200.jpg"
                    ]
                    logger.info(f"📋 Автоматические fallback URLs: {fallback_urls}")

            # Получаем настройки автоимпорта для получения credentials sexoptovik
            seller = current_user.seller if current_user.is_authenticated else None
            logger.info(f"👤 Current user authenticated: {current_user.is_authenticated}, seller: {seller is not None}")
            auth_cookies = None

            if seller and seller.auto_import_settings:
                settings = seller.auto_import_settings
                logger.info(f"⚙️  Настройки найдены. Проверяем URL...")

                # Если URL от sexoptovik и есть логин/пароль - авторизуемся
                if 'sexoptovik.ru' in photo_url:
                    logger.info(f"🌐 URL от sexoptovik.ru обнаружен")
                    logger.info(f"🔑 Login: {settings.sexoptovik_login}, Password: {'***' if settings.sexoptovik_password else None}")

                    if settings.sexoptovik_login and settings.sexoptovik_password:
                        logger.info(f"🔐 Авторизация на sexoptovik с логином: {settings.sexoptovik_login}")
                        from auto_import_manager import SexoptovikAuth
                        auth_cookies = SexoptovikAuth.get_auth_cookies(
                            settings.sexoptovik_login,
                            settings.sexoptovik_password
                        )
                        if not auth_cookies:
                            logger.warning(f"⚠️  Авторизация не удалась, пробуем fallback URLs")
                            # Не возвращаем ошибку, пробуем fallback
                        else:
                            logger.info(f"✅ Авторизация успешна, получены cookies")
                    else:
                        logger.warning(f"⚠️  Нет credentials для sexoptovik, пробуем fallback URLs")
                else:
                    logger.info(f"ℹ️  URL не от sexoptovik.ru, авторизация не требуется")
            else:
                logger.warning(f"⚠️  Настройки не найдены, пробуем без авторизации или fallback")

            # Скачиваем и обрабатываем фото с retry и fallback
            logger.info(f"⬇️  Скачивание и обработка изображения...")
            processed_image = ImageProcessor.download_and_process_image(
                photo_url,
                target_size=(1200, 1200),
                background_color=bg_color,
                auth_cookies=auth_cookies,
                fallback_urls=fallback_urls if fallback_urls else None
            )

            if not processed_image:
                error_msg = "Не удалось скачать или обработать изображение после всех попыток."
                logger.error(f"❌ {error_msg} URL: {photo_url}, Fallbacks: {fallback_urls}")
                return jsonify({
                    'error': error_msg,
                    'details': f'URL: {photo_url}',
                    'fallback_urls': fallback_urls
                }), 500

            logger.info(f"✅ Изображение успешно обработано")

            # Сохраняем в кэш
            try:
                processed_image.seek(0)
                with open(cache_path, 'wb') as f:
                    f.write(processed_image.read())
                processed_image.seek(0)
                logger.info(f"💾 Сохранено в кэш: {cache_path}")
            except Exception as cache_err:
                logger.warning(f"⚠️  Ошибка сохранения в кэш: {cache_err}")

            # Возвращаем обработанное изображение
            return send_file(
                processed_image,
                mimetype='image/jpeg',
                as_attachment=False,
                download_name='padded_photo.jpg'
            )

        except Exception as e:
            import traceback
            error_trace = traceback.format_exc()
            logger.error(f"❌ Критическая ошибка при обработке фото:\n{error_trace}")
            return jsonify({
                'error': f'Ошибка обработки изображения: {str(e)}',
                'details': error_trace.split('\n')[-2] if error_trace else str(e),
                'url': photo_url
            }), 500


    @app.route('/auto-import/ai-update', methods=['GET'])
    @login_required
    def auto_import_ai_update():
        """Страница AI обновления товаров"""
        if not current_user.seller:
            flash('Для работы с автоимпортом обратитесь к администратору.', 'warning')
            return redirect(url_for('dashboard'))

        seller = current_user.seller
        settings = AutoImportSettings.query.filter_by(seller_id=seller.id).first()

        # Проверяем, настроен ли AI
        ai_enabled = settings and settings.ai_enabled and settings.ai_api_key

        # Пагинация
        page = request.args.get('page', 1, type=int)
        per_page = 50

        # Получаем товары (исключаем уже импортированные)
        query = ImportedProduct.query.filter(
            ImportedProduct.seller_id == seller.id,
            ImportedProduct.import_status.in_(['pending', 'validated', 'failed'])
        )

        pagination = query.order_by(ImportedProduct.created_at.desc()).paginate(
            page=page, per_page=per_page, error_out=False
        )

        return render_template(
            'auto_import_ai_update.html',
            products=pagination.items,
            pagination=pagination,
            ai_enabled=ai_enabled,
            settings=settings
        )

    @app.route('/auto-import/ai-process', methods=['POST'])
    @login_required
    def auto_import_ai_process_single():
        """
        Обработка одного товара с AI

        POST JSON:
        {
            "product_id": int,
            "operations": ["category", "dimensions", "description", "sizes"]
        }
        """
        if not current_user.seller:
            return jsonify({'success': False, 'error': 'Seller not found'}), 403

        seller = current_user.seller
        settings = AutoImportSettings.query.filter_by(seller_id=seller.id).first()

        if not settings or not settings.ai_enabled or not settings.ai_api_key:
            return jsonify({'success': False, 'error': 'AI не настроен'}), 400

        data = request.get_json()
        product_id = data.get('product_id')
        operations = data.get('operations', [])

        if not product_id:
            return jsonify({'success': False, 'error': 'Product ID is required'}), 400

        if not operations:
            return jsonify({'success': False, 'error': 'No operations specified'}), 400

        # Получаем товар
        product = ImportedProduct.query.filter_by(
            id=product_id,
            seller_id=seller.id
        ).first()

        if not product:
            return jsonify({'success': False, 'error': 'Product not found'}), 404

        try:
            from ai_service import get_ai_service, AIConfig
            ai_service = get_ai_service(settings)

            if not ai_service:
                return jsonify({'success': False, 'error': 'Не удалось инициализировать AI сервис'}), 500

            results = {}
            updated_fields = []

            # Парсим JSON поля товара
            try:
                all_categories = json.loads(product.all_categories) if product.all_categories else []
            except:
                all_categories = []

            # Определение категории
            if 'category' in operations:
                try:
                    cat_id, cat_name, confidence, reasoning = ai_service.detect_category(
                        product_title=product.title or '',
                        source_category=product.category or '',
                        all_categories=all_categories,
                        brand=product.brand or '',
                        description=product.description or ''
                    )

                    if cat_id:
                        product.wb_subject_id = cat_id
                        product.mapped_wb_category = cat_name
                        product.category_confidence = confidence
                        updated_fields.append('category')
                        results['category'] = {
                            'id': cat_id,
                            'name': cat_name,
                            'confidence': confidence,
                            'reasoning': reasoning
                        }
                        logger.info(f"AI определил категорию для {product.id}: {cat_name} ({confidence*100:.0f}%)")
                except Exception as e:
                    logger.error(f"Ошибка AI определения категории: {e}")
                    results['category_error'] = str(e)

            # Парсинг размеров и габаритов
            if 'dimensions' in operations or 'sizes' in operations:
                try:
                    # Собираем текст для парсинга
                    sizes_text = ''
                    try:
                        sizes_list = json.loads(product.sizes) if product.sizes else []
                        sizes_text = ', '.join(str(s) for s in sizes_list)
                    except:
                        pass

                    success, parsed_data, error = ai_service.parse_sizes(
                        sizes_text=sizes_text,
                        product_title=product.title or '',
                        description=product.description or ''
                    )

                    if success and parsed_data:
                        # Сохраняем характеристики
                        existing_chars = {}
                        try:
                            existing_chars = json.loads(product.characteristics) if product.characteristics else {}
                        except:
                            existing_chars = {}

                        # Обновляем характеристики из AI
                        if parsed_data.get('characteristics'):
                            existing_chars.update(parsed_data['characteristics'])
                            product.characteristics = json.dumps(existing_chars, ensure_ascii=False)
                            updated_fields.append('characteristics')

                        results['sizes'] = parsed_data
                        logger.info(f"AI распарсил размеры для {product.id}: {parsed_data}")
                except Exception as e:
                    logger.error(f"Ошибка AI парсинга размеров: {e}")
                    results['sizes_error'] = str(e)

            # Генерация описания (TODO: отдельная задача в ai_service)
            if 'description' in operations:
                try:
                    # Простая генерация описания через chat completion
                    from ai_service import AIClient, AIConfig as AIC
                    config = AIC.from_settings(settings)
                    if config:
                        client = AIClient(config)
                        prompt = f"""Напиши краткое SEO-оптимизированное описание товара для маркетплейса Wildberries.

Название: {product.title}
Категория: {product.mapped_wb_category or product.category}
Бренд: {product.brand or 'Не указан'}

Требования:
- 2-3 предложения
- Без воды и общих фраз
- Упомяни ключевые особенности товара
- Подходит для карточки товара на Wildberries

Ответь ТОЛЬКО текстом описания, без заголовков и пояснений."""

                        response = client.chat_completion([
                            {"role": "user", "content": prompt}
                        ], max_tokens=500)

                        if response:
                            product.description = response.strip()
                            updated_fields.append('description')
                            results['description'] = response.strip()[:200] + '...' if len(response) > 200 else response.strip()
                            logger.info(f"AI сгенерировал описание для {product.id}")

                        client.close()
                except Exception as e:
                    logger.error(f"Ошибка AI генерации описания: {e}")
                    results['description_error'] = str(e)

            # Сохраняем изменения
            if updated_fields:
                db.session.commit()
                return jsonify({
                    'success': True,
                    'updated_fields': updated_fields,
                    'results': results
                })
            else:
                return jsonify({
                    'success': False,
                    'skipped': True,
                    'message': 'Нет данных для обновления',
                    'results': results
                })

        except Exception as e:
            import traceback
            logger.error(f"Ошибка AI обработки товара {product_id}: {traceback.format_exc()}")
            db.session.rollback()
            return jsonify({'success': False, 'error': str(e)}), 500

    @app.route('/auto-import/ai/models', methods=['GET'])
    @login_required
    def auto_import_ai_models():
        """Возвращает список доступных AI моделей для провайдера"""
        provider = request.args.get('provider', 'cloudru')

        try:
            from ai_service import get_available_models
            models = get_available_models(provider)
            return jsonify({
                'success': True,
                'provider': provider,
                'models': models
            })
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500

    @app.route('/auto-import/ai/test-raw', methods=['POST'])
    @login_required
    def auto_import_ai_test_raw():
        """Тестирует подключение к AI API напрямую (как curl)"""
        if not current_user.seller:
            return jsonify({'success': False, 'error': 'Seller not found'}), 403

        seller = current_user.seller
        settings = AutoImportSettings.query.filter_by(seller_id=seller.id).first()

        if not settings or not settings.ai_api_key:
            return jsonify({'success': False, 'error': 'API ключ не настроен'}), 400

        import requests as req

        api_key = settings.ai_api_key
        url = "https://foundation-models.api.cloud.ru/v1/chat/completions"

        logger.info(f"🧪 RAW TEST: api_key={api_key[:20]}... (len={len(api_key)})")
        logger.info(f"🧪 RAW TEST: url={url}")

        headers = {
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json'
        }

        payload = {
            "model": "openai/gpt-oss-120b",
            "messages": [{"role": "user", "content": "Ответь одним словом: работает"}],
            "temperature": 0.7,
            "max_tokens": 50
        }

        logger.info(f"🧪 RAW TEST: Authorization header = Bearer {api_key[:20]}...")

        try:
            response = req.post(url, json=payload, headers=headers, timeout=30)
            logger.info(f"🧪 RAW TEST: status={response.status_code}")
            logger.info(f"🧪 RAW TEST: response={response.text[:500]}")

            if response.status_code == 200:
                return jsonify({'success': True, 'message': 'RAW тест успешен!', 'response': response.json()})
            else:
                return jsonify({'success': False, 'error': f'HTTP {response.status_code}: {response.text}'})
        except Exception as e:
            logger.error(f"🧪 RAW TEST ERROR: {e}")
            return jsonify({'success': False, 'error': str(e)})

    @app.route('/auto-import/ai/test', methods=['POST'])
    @login_required
    def auto_import_ai_test():
        """Тестирует подключение к AI API"""
        if not current_user.seller:
            return jsonify({'success': False, 'error': 'Seller not found'}), 403

        seller = current_user.seller
        settings = AutoImportSettings.query.filter_by(seller_id=seller.id).first()

        if not settings:
            return jsonify({'success': False, 'error': 'Настройки не найдены'}), 400

        # Все провайдеры используют API ключ
        if not settings.ai_api_key:
            return jsonify({'success': False, 'error': 'API ключ не настроен. Сохраните настройки перед тестированием.'}), 400

        try:
            from ai_service import get_ai_service, reset_ai_service

            # Логируем какой ключ используется
            logger.info(f"🔑 AI Test: provider={settings.ai_provider}")
            logger.info(f"🔑 API Key: {settings.ai_api_key[:20] if settings.ai_api_key else 'None'}... (длина: {len(settings.ai_api_key) if settings.ai_api_key else 0})")
            logger.info(f"🔑 Base URL: {settings.ai_api_base_url or 'DEFAULT'}")
            logger.info(f"🔑 Model: {settings.ai_model or 'DEFAULT'}")

            # Сбрасываем кэш чтобы использовать свежие настройки
            reset_ai_service()
            ai_service = get_ai_service(settings)

            if not ai_service:
                return jsonify({'success': False, 'error': 'Не удалось инициализировать AI сервис'}), 500

            success, message = ai_service.test_connection()

            return jsonify({
                'success': success,
                'message': message
            })
        except Exception as e:
            import traceback
            app.logger.error(f"AI test error: {traceback.format_exc()}")
            return jsonify({'success': False, 'error': str(e)}), 500

    @app.route('/auto-import/ai/instructions', methods=['GET'])
    @login_required
    def auto_import_ai_instructions():
        """Возвращает дефолтные инструкции для редактирования"""
        try:
            from ai_service import get_default_instructions
            instructions = get_default_instructions()
            return jsonify({
                'success': True,
                'instructions': instructions
            })
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500


# Пример использования:
# from auto_import_routes import register_auto_import_routes
# register_auto_import_routes(app)
