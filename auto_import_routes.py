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

    @app.route('/auto-import/photo/padded', methods=['GET'])
    def auto_import_photo_padded():
        """
        Возвращает фото с примененным padding до 1200x1200
        Query params:
            url: URL исходного фото
            bg_color: Цвет фона для padding (по умолчанию 'white')
        """
        photo_url = request.args.get('url')
        bg_color = request.args.get('bg_color', 'white')

        if not photo_url:
            return jsonify({'error': 'URL параметр обязателен'}), 400

        try:
            logger.info(f"🖼️  Запрос обработки фото: {photo_url}")

            # Получаем настройки автоимпорта для получения credentials sexoptovik
            seller = current_user.seller if current_user.is_authenticated else None
            auth_cookies = None

            if seller and seller.auto_import_settings:
                settings = seller.auto_import_settings
                # Если URL от sexoptovik и есть логин/пароль - авторизуемся
                if 'sexoptovik.ru' in photo_url:
                    if settings.sexoptovik_login and settings.sexoptovik_password:
                        logger.info(f"🔐 Авторизация на sexoptovik с логином: {settings.sexoptovik_login}")
                        from auto_import_manager import SexoptovikAuth
                        auth_cookies = SexoptovikAuth.get_auth_cookies(
                            settings.sexoptovik_login,
                            settings.sexoptovik_password
                        )
                        if not auth_cookies:
                            error_msg = "Не удалось авторизоваться на sexoptovik.ru. Проверьте логин и пароль в настройках."
                            logger.error(f"❌ {error_msg}")
                            return jsonify({'error': error_msg, 'details': 'Авторизация не прошла'}), 401
                        logger.info(f"✅ Авторизация успешна, получены cookies")
                    else:
                        error_msg = "Для доступа к фото sexoptovik.ru нужно указать логин и пароль в настройках автоимпорта"
                        logger.warning(f"⚠️  {error_msg}")
                        return jsonify({'error': error_msg, 'details': 'Отсутствуют учетные данные'}), 403
            else:
                if 'sexoptovik.ru' in photo_url:
                    error_msg = "Для доступа к фото sexoptovik.ru нужно указать логин и пароль в настройках автоимпорта"
                    logger.warning(f"⚠️  {error_msg}")
                    return jsonify({'error': error_msg, 'details': 'Настройки не найдены'}), 403

            # Скачиваем и обрабатываем фото
            logger.info(f"⬇️  Скачивание и обработка изображения...")
            processed_image = ImageProcessor.download_and_process_image(
                photo_url,
                target_size=(1200, 1200),
                background_color=bg_color,
                auth_cookies=auth_cookies
            )

            if not processed_image:
                error_msg = "Не удалось скачать или обработать изображение. Проверьте URL и доступность сервера."
                logger.error(f"❌ {error_msg} URL: {photo_url}")
                return jsonify({'error': error_msg, 'details': f'URL: {photo_url}'}), 500

            logger.info(f"✅ Изображение успешно обработано")
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


# Пример использования:
# from auto_import_routes import register_auto_import_routes
# register_auto_import_routes(app)
