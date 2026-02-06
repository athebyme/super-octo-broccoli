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
import hashlib
from datetime import datetime

from models import db, AutoImportSettings, ImportedProduct, CategoryMapping, AIHistory
from auto_import_manager import AutoImportManager, ImageProcessor

logger = logging.getLogger(__name__)


def compute_content_hash(product):
    """Вычисляет хеш контента карточки для отслеживания изменений"""
    content = f"{product.title or ''}{product.description or ''}{product.characteristics or ''}"
    return hashlib.md5(content.encode('utf-8')).hexdigest()


def save_ai_history(
    seller_id,
    product_id,
    action_type,
    input_data,
    result_data,
    success=True,
    error_message=None,
    ai_provider=None,
    ai_model=None,
    system_prompt=None,
    user_prompt=None,
    raw_response=None,
    response_time_ms=0,
    tokens_used=0,
    source_module='auto_import'
):
    """
    Сохраняет действие AI в историю с расширенной информацией

    Args:
        seller_id: ID продавца
        product_id: ID товара (опционально)
        action_type: Тип действия (seo_title, keywords, etc.)
        input_data: Входные данные (dict)
        result_data: Результат (dict)
        success: Успешен ли запрос
        error_message: Сообщение об ошибке
        ai_provider: Провайдер AI (cloudru, openai, custom)
        ai_model: Модель AI
        system_prompt: Системный промпт
        user_prompt: Пользовательский промпт
        raw_response: Сырой ответ AI
        response_time_ms: Время ответа в мс
        tokens_used: Использовано токенов
        source_module: Модуль-источник запроса
    """
    try:
        history = AIHistory(
            seller_id=seller_id,
            imported_product_id=product_id,
            action_type=action_type,
            input_data=json.dumps(input_data, ensure_ascii=False) if input_data else None,
            result_data=json.dumps(result_data, ensure_ascii=False) if result_data else None,
            success=success,
            error_message=error_message,
            ai_provider=ai_provider,
            ai_model=ai_model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            raw_response=raw_response,
            response_time_ms=response_time_ms,
            tokens_used=tokens_used,
            source_module=source_module
        )
        db.session.add(history)
        db.session.commit()
        return history
    except Exception as e:
        logger.error(f"Error saving AI history: {e}")
        db.session.rollback()
        return None


def ai_request_logger_callback(**kwargs):
    """Callback для централизованного логирования AI запросов из ai_service"""
    save_ai_history(
        seller_id=kwargs.get('seller_id', 0),
        product_id=kwargs.get('product_id'),
        action_type=kwargs.get('action_type', 'unknown'),
        input_data=None,
        result_data=None,
        success=kwargs.get('success', True),
        error_message=kwargs.get('error_message'),
        ai_provider=kwargs.get('provider'),
        ai_model=kwargs.get('model'),
        system_prompt=kwargs.get('system_prompt'),
        user_prompt=kwargs.get('user_prompt'),
        raw_response=kwargs.get('response'),
        response_time_ms=kwargs.get('response_time_ms', 0),
        tokens_used=kwargs.get('tokens_used', 0),
        source_module=kwargs.get('source_module', 'ai_service')
    )


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

            # Кастомные инструкции AI для каждой функции
            settings.ai_category_instruction = request.form.get('ai_category_instruction', '').strip() or None
            settings.ai_size_instruction = request.form.get('ai_size_instruction', '').strip() or None
            settings.ai_seo_title_instruction = request.form.get('ai_seo_title_instruction', '').strip() or None
            settings.ai_keywords_instruction = request.form.get('ai_keywords_instruction', '').strip() or None
            settings.ai_bullets_instruction = request.form.get('ai_bullets_instruction', '').strip() or None
            settings.ai_description_instruction = request.form.get('ai_description_instruction', '').strip() or None
            settings.ai_rich_content_instruction = request.form.get('ai_rich_content_instruction', '').strip() or None
            settings.ai_analysis_instruction = request.form.get('ai_analysis_instruction', '').strip() or None

            # Настройки генерации изображений
            settings.image_gen_enabled = request.form.get('image_gen_enabled') == 'on'
            settings.image_gen_provider = request.form.get('image_gen_provider', 'together_flux').strip()
            settings.openai_api_key = request.form.get('openai_api_key', '').strip()
            settings.replicate_api_key = request.form.get('replicate_api_key', '').strip()
            settings.together_api_key = request.form.get('together_api_key', '').strip()

            try:
                settings.image_gen_width = int(request.form.get('image_gen_width', 1440))
            except ValueError:
                settings.image_gen_width = 1440

            try:
                settings.image_gen_height = int(request.form.get('image_gen_height', 810))
            except ValueError:
                settings.image_gen_height = 810

            settings.openai_image_quality = request.form.get('openai_image_quality', 'standard').strip()
            settings.openai_image_style = request.form.get('openai_image_style', 'vivid').strip()

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
        """Список импортированных товаров с расширенными фильтрами"""
        if not current_user.seller:
            flash('Для работы с автоимпортом обратитесь к администратору.', 'warning')
            return redirect(url_for('dashboard'))

        seller = current_user.seller

        # Фильтры
        status_filter = request.args.get('status', '')
        search_query = request.args.get('q', '').strip()
        category_filter = request.args.get('category', '')
        brand_filter = request.args.get('brand', '')
        has_ai_filter = request.args.get('has_ai', '')  # 'yes', 'no', ''
        sort_by = request.args.get('sort', 'created_at')  # created_at, title, category
        sort_order = request.args.get('order', 'desc')  # asc, desc
        page = int(request.args.get('page', 1))
        per_page = int(request.args.get('per_page', 50))
        per_page = min(max(per_page, 10), 100)  # От 10 до 100

        query = ImportedProduct.query.filter_by(seller_id=seller.id)

        # Применяем фильтры
        if status_filter:
            query = query.filter_by(import_status=status_filter)

        if search_query:
            search_pattern = f"%{search_query}%"
            query = query.filter(
                db.or_(
                    ImportedProduct.title.ilike(search_pattern),
                    ImportedProduct.external_id.ilike(search_pattern),
                    ImportedProduct.external_vendor_code.ilike(search_pattern),
                    ImportedProduct.description.ilike(search_pattern),
                    ImportedProduct.brand.ilike(search_pattern)
                )
            )

        if category_filter:
            query = query.filter(
                db.or_(
                    ImportedProduct.category.ilike(f"%{category_filter}%"),
                    ImportedProduct.mapped_wb_category.ilike(f"%{category_filter}%")
                )
            )

        if brand_filter:
            query = query.filter(ImportedProduct.brand.ilike(f"%{brand_filter}%"))

        if has_ai_filter == 'yes':
            query = query.filter(
                db.or_(
                    ImportedProduct.ai_seo_title.isnot(None),
                    ImportedProduct.ai_keywords.isnot(None),
                    ImportedProduct.ai_bullets.isnot(None),
                    ImportedProduct.ai_rich_content.isnot(None)
                )
            )
        elif has_ai_filter == 'no':
            query = query.filter(
                ImportedProduct.ai_seo_title.is_(None),
                ImportedProduct.ai_keywords.is_(None),
                ImportedProduct.ai_bullets.is_(None),
                ImportedProduct.ai_rich_content.is_(None)
            )

        # Сортировка
        sort_column = getattr(ImportedProduct, sort_by, ImportedProduct.created_at)
        if sort_order == 'asc':
            query = query.order_by(sort_column.asc())
        else:
            query = query.order_by(sort_column.desc())

        pagination = query.paginate(page=page, per_page=per_page, error_out=False)
        products = pagination.items

        # Получаем уникальные значения для фильтров
        all_categories = db.session.query(ImportedProduct.category).filter_by(
            seller_id=seller.id
        ).filter(ImportedProduct.category.isnot(None)).distinct().all()
        categories = sorted(set(c[0] for c in all_categories if c[0]))

        all_brands = db.session.query(ImportedProduct.brand).filter_by(
            seller_id=seller.id
        ).filter(ImportedProduct.brand.isnot(None)).distinct().all()
        brands = sorted(set(b[0] for b in all_brands if b[0]))

        # Статистика
        stats = {
            'total': ImportedProduct.query.filter_by(seller_id=seller.id).count(),
            'pending': ImportedProduct.query.filter_by(seller_id=seller.id, import_status='pending').count(),
            'validated': ImportedProduct.query.filter_by(seller_id=seller.id, import_status='validated').count(),
            'imported': ImportedProduct.query.filter_by(seller_id=seller.id, import_status='imported').count(),
            'failed': ImportedProduct.query.filter_by(seller_id=seller.id, import_status='failed').count(),
        }

        return render_template(
            'auto_import_products.html',
            products=products,
            pagination=pagination,
            status_filter=status_filter,
            search_query=search_query,
            category_filter=category_filter,
            brand_filter=brand_filter,
            has_ai_filter=has_ai_filter,
            sort_by=sort_by,
            sort_order=sort_order,
            categories=categories,
            brands=brands,
            stats=stats
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

        # Парсим characteristics для отображения дополнительных характеристик
        try:
            chars = json.loads(product.characteristics) if product.characteristics else {}
            # Фильтруем служебные ключи (начинаются с _)
            product.characteristics_dict = {k: v for k, v in chars.items() if not k.startswith('_')}
        except:
            product.characteristics_dict = {}

        # Безопасный доступ к AI-полям (могут не существовать до миграции)
        product.has_ai_data = False
        product.ai_keywords_list = None
        product.ai_bullets_list = None
        product.ai_rich_content_data = None
        product.ai_analysis_data = None

        try:
            # Парсим AI JSON поля
            ai_keywords_raw = getattr(product, 'ai_keywords', None)
            ai_bullets_raw = getattr(product, 'ai_bullets', None)
            ai_rich_content_raw = getattr(product, 'ai_rich_content', None)
            ai_analysis_raw = getattr(product, 'ai_analysis', None)
            ai_seo_title = getattr(product, 'ai_seo_title', None)

            if ai_keywords_raw:
                try:
                    product.ai_keywords_list = json.loads(ai_keywords_raw)
                except:
                    product.ai_keywords_list = None

            if ai_bullets_raw:
                try:
                    product.ai_bullets_list = json.loads(ai_bullets_raw)
                except:
                    product.ai_bullets_list = None

            if ai_rich_content_raw:
                try:
                    product.ai_rich_content_data = json.loads(ai_rich_content_raw)
                except:
                    product.ai_rich_content_data = None

            if ai_analysis_raw:
                try:
                    product.ai_analysis_data = json.loads(ai_analysis_raw)
                except:
                    product.ai_analysis_data = None

            product.has_ai_data = bool(
                product.ai_keywords_list or
                product.ai_bullets_list or
                product.ai_rich_content_data or
                product.ai_analysis_data or
                ai_seo_title
            )
        except Exception as e:
            logger.warning(f"Ошибка парсинга AI полей: {e}")

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
                # Вместо ошибки 500 возвращаем placeholder - это не блокирует UI
                logger.debug(f"Фото недоступно, возвращаем placeholder: {photo_url[:50]}...")
                # Создаём простой серый placeholder
                from PIL import Image
                from io import BytesIO
                placeholder = Image.new('RGB', (200, 200), color=(243, 244, 246))
                buffer = BytesIO()
                placeholder.save(buffer, format='JPEG', quality=80)
                buffer.seek(0)
                return send_file(buffer, mimetype='image/jpeg')

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
            # Вместо ошибки 500 возвращаем placeholder
            logger.debug(f"Ошибка загрузки фото, возвращаем placeholder: {str(e)[:100]}")
            from PIL import Image
            from io import BytesIO
            placeholder = Image.new('RGB', (200, 200), color=(243, 244, 246))
            buffer = BytesIO()
            placeholder.save(buffer, format='JPEG', quality=80)
            buffer.seek(0)
            return send_file(buffer, mimetype='image/jpeg')


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

                    # Получаем список характеристик для категории WB
                    category_chars = []
                    if product.wb_subject_id:
                        try:
                            from wb_api_client import WBApiClient
                            # Используем WB API клиент селлера
                            wb_client = WBApiClient(seller.api_key)
                            chars_config = wb_client.get_card_characteristics_config(product.wb_subject_id)
                            if chars_config and chars_config.get('data'):
                                # Извлекаем названия характеристик (особенно размерные)
                                size_keywords = ['длина', 'ширина', 'высота', 'диаметр', 'глубина', 'размер', 'вес', 'объем']
                                for char in chars_config['data']:
                                    char_name = char.get('name', '')
                                    # Добавляем размерные характеристики
                                    if any(kw in char_name.lower() for kw in size_keywords):
                                        category_chars.append(char_name)
                                logger.info(f"Загружено {len(category_chars)} размерных характеристик для категории {product.wb_subject_id}")
                        except Exception as e:
                            logger.warning(f"Не удалось загрузить характеристики категории: {e}")

                    success, parsed_data, error = ai_service.parse_sizes(
                        sizes_text=sizes_text,
                        product_title=product.title or '',
                        description=product.description or '',
                        category_characteristics=category_chars if category_chars else None
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

    @app.route('/auto-import/ai/test-raw', methods=['POST', 'GET'])
    def auto_import_ai_test_raw():
        """
        Тестирует подключение к AI API напрямую (как curl)
        GET: использует ключ из настроек (требует авторизацию)
        POST с json: {"api_key": "..."} - тест с переданным ключом
        """
        import requests as req

        # Получаем api_key из запроса или из настроек
        if request.method == 'POST' and request.json and request.json.get('api_key'):
            api_key = request.json.get('api_key')
            logger.info(f"🧪 RAW TEST: используем ключ из запроса")
        elif current_user.is_authenticated and current_user.seller:
            settings = AutoImportSettings.query.filter_by(seller_id=current_user.seller.id).first()
            if not settings or not settings.ai_api_key:
                return jsonify({'success': False, 'error': 'API ключ не настроен в настройках'}), 400
            api_key = settings.ai_api_key
            logger.info(f"🧪 RAW TEST: используем ключ из настроек")
        else:
            return jsonify({'success': False, 'error': 'Передайте api_key в JSON или авторизуйтесь'}), 400
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

    # ============================================================================
    # AI ENHANCED ENDPOINTS - Новые AI функции для улучшения карточки
    # ============================================================================

    @app.route('/auto-import/ai/seo-title', methods=['POST'])
    @login_required
    def auto_import_ai_seo_title():
        """Генерация SEO-оптимизированного заголовка"""
        if not current_user.seller:
            return jsonify({'success': False, 'error': 'Seller not found'}), 403

        data = request.get_json() or {}
        product_id = data.get('product_id')

        if not product_id:
            return jsonify({'success': False, 'error': 'product_id required'}), 400

        seller = current_user.seller
        settings = AutoImportSettings.query.filter_by(seller_id=seller.id).first()

        if not settings or not settings.ai_enabled:
            return jsonify({'success': False, 'error': 'AI не настроен'}), 400

        product = ImportedProduct.query.filter_by(
            id=product_id, seller_id=seller.id
        ).first()

        if not product:
            return jsonify({'success': False, 'error': 'Товар не найден'}), 404

        try:
            from ai_service import AIConfig, AIService

            config = AIConfig.from_settings(settings)
            if not config:
                return jsonify({'success': False, 'error': 'AI не настроен'}), 400

            ai_service = AIService(config)
            success, result, error = ai_service.generate_seo_title(
                title=product.title or '',
                category=product.mapped_wb_category or '',
                brand=product.brand or '',
                description=product.description or ''
            )

            if success:
                # Сохраняем результат в кэш продукта
                if result.get('title'):
                    product.ai_seo_title = result['title']
                    product.content_hash = compute_content_hash(product)
                    db.session.commit()

                # Сохраняем в историю
                save_ai_history(
                    seller_id=seller.id,
                    product_id=product.id,
                    action_type='seo_title',
                    input_data={'title': product.title, 'category': product.mapped_wb_category},
                    result_data=result,
                    ai_provider=settings.ai_provider,
                    ai_model=settings.ai_model
                )

                return jsonify({
                    'success': True,
                    'data': result,
                    'original_title': product.title
                })
            else:
                save_ai_history(seller.id, product.id, 'seo_title', None, None, False, error, ai_provider=settings.ai_provider, ai_model=settings.ai_model)
                return jsonify({'success': False, 'error': error}), 500

        except Exception as e:
            logger.error(f"AI SEO title error: {e}")
            return jsonify({'success': False, 'error': str(e)}), 500

    @app.route('/auto-import/ai/keywords', methods=['POST'])
    @login_required
    def auto_import_ai_keywords():
        """Генерация ключевых слов для товара"""
        if not current_user.seller:
            return jsonify({'success': False, 'error': 'Seller not found'}), 403

        data = request.get_json() or {}
        product_id = data.get('product_id')

        if not product_id:
            return jsonify({'success': False, 'error': 'product_id required'}), 400

        seller = current_user.seller
        settings = AutoImportSettings.query.filter_by(seller_id=seller.id).first()

        if not settings or not settings.ai_enabled:
            return jsonify({'success': False, 'error': 'AI не настроен'}), 400

        product = ImportedProduct.query.filter_by(
            id=product_id, seller_id=seller.id
        ).first()

        if not product:
            return jsonify({'success': False, 'error': 'Товар не найден'}), 404

        try:
            from ai_service import AIConfig, AIService

            config = AIConfig.from_settings(settings)
            ai_service = AIService(config)
            success, result, error = ai_service.generate_keywords(
                title=product.title or '',
                category=product.mapped_wb_category or '',
                description=product.description or ''
            )

            if success:
                # Сохраняем в кэш продукта
                product.ai_keywords = json.dumps(result, ensure_ascii=False)
                product.ai_analysis_at = datetime.utcnow()
                product.content_hash = compute_content_hash(product)
                db.session.commit()

                # Сохраняем в историю
                save_ai_history(seller.id, product.id, 'keywords',
                    {'title': product.title, 'category': product.mapped_wb_category}, result,
                    ai_provider=settings.ai_provider, ai_model=settings.ai_model)

                return jsonify({'success': True, 'data': result})
            else:
                save_ai_history(seller.id, product.id, 'keywords', None, None, False, error, ai_provider=settings.ai_provider, ai_model=settings.ai_model)
                return jsonify({'success': False, 'error': error}), 500

        except Exception as e:
            logger.error(f"AI keywords error: {e}")
            return jsonify({'success': False, 'error': str(e)}), 500

    @app.route('/auto-import/ai/bullet-points', methods=['POST'])
    @login_required
    def auto_import_ai_bullet_points():
        """Генерация bullet points (преимуществ) товара"""
        if not current_user.seller:
            return jsonify({'success': False, 'error': 'Seller not found'}), 403

        data = request.get_json() or {}
        product_id = data.get('product_id')

        if not product_id:
            return jsonify({'success': False, 'error': 'product_id required'}), 400

        seller = current_user.seller
        settings = AutoImportSettings.query.filter_by(seller_id=seller.id).first()

        if not settings or not settings.ai_enabled:
            return jsonify({'success': False, 'error': 'AI не настроен'}), 400

        product = ImportedProduct.query.filter_by(
            id=product_id, seller_id=seller.id
        ).first()

        if not product:
            return jsonify({'success': False, 'error': 'Товар не найден'}), 404

        try:
            from ai_service import AIConfig, AIService

            config = AIConfig.from_settings(settings)
            ai_service = AIService(config)

            # Получаем характеристики если есть
            characteristics = {}
            if product.characteristics:
                try:
                    characteristics = json.loads(product.characteristics) if isinstance(product.characteristics, str) else product.characteristics
                except:
                    pass

            success, result, error = ai_service.generate_bullet_points(
                title=product.title or '',
                description=product.description or '',
                characteristics=characteristics
            )

            if success:
                # Сохраняем в кэш продукта
                product.ai_bullets = json.dumps(result, ensure_ascii=False)
                product.ai_analysis_at = datetime.utcnow()
                product.content_hash = compute_content_hash(product)
                db.session.commit()

                # Сохраняем в историю
                save_ai_history(seller.id, product.id, 'bullets',
                    {'title': product.title}, result,
                    ai_provider=settings.ai_provider, ai_model=settings.ai_model)

                return jsonify({'success': True, 'data': result})
            else:
                save_ai_history(seller.id, product.id, 'bullets', None, None, False, error,
                    ai_provider=settings.ai_provider, ai_model=settings.ai_model)
                return jsonify({'success': False, 'error': error}), 500

        except Exception as e:
            logger.error(f"AI bullet points error: {e}")
            return jsonify({'success': False, 'error': str(e)}), 500

    @app.route('/auto-import/ai/enhance-description', methods=['POST'])
    @login_required
    def auto_import_ai_enhance_description():
        """Улучшение описания товара"""
        if not current_user.seller:
            return jsonify({'success': False, 'error': 'Seller not found'}), 403

        data = request.get_json() or {}
        product_id = data.get('product_id')

        if not product_id:
            return jsonify({'success': False, 'error': 'product_id required'}), 400

        seller = current_user.seller
        settings = AutoImportSettings.query.filter_by(seller_id=seller.id).first()

        if not settings or not settings.ai_enabled:
            return jsonify({'success': False, 'error': 'AI не настроен'}), 400

        product = ImportedProduct.query.filter_by(
            id=product_id, seller_id=seller.id
        ).first()

        if not product:
            return jsonify({'success': False, 'error': 'Товар не найден'}), 404

        if not product.description:
            return jsonify({'success': False, 'error': 'Описание отсутствует'}), 400

        try:
            from ai_service import AIConfig, AIService

            config = AIConfig.from_settings(settings)
            ai_service = AIService(config)
            success, result, error = ai_service.enhance_description(
                title=product.title or '',
                description=product.description,
                category=product.mapped_wb_category or ''
            )

            if success:
                return jsonify({
                    'success': True,
                    'data': result,
                    'original_description': product.description
                })
            else:
                return jsonify({'success': False, 'error': error}), 500

        except Exception as e:
            logger.error(f"AI enhance description error: {e}")
            return jsonify({'success': False, 'error': str(e)}), 500

    @app.route('/auto-import/ai/analyze', methods=['POST'])
    @login_required
    def auto_import_ai_analyze():
        """Анализ карточки товара с рекомендациями"""
        if not current_user.seller:
            return jsonify({'success': False, 'error': 'Seller not found'}), 403

        data = request.get_json() or {}
        product_id = data.get('product_id')

        if not product_id:
            return jsonify({'success': False, 'error': 'product_id required'}), 400

        seller = current_user.seller
        settings = AutoImportSettings.query.filter_by(seller_id=seller.id).first()

        if not settings or not settings.ai_enabled:
            return jsonify({'success': False, 'error': 'AI не настроен'}), 400

        product = ImportedProduct.query.filter_by(
            id=product_id, seller_id=seller.id
        ).first()

        if not product:
            return jsonify({'success': False, 'error': 'Товар не найден'}), 404

        try:
            from ai_service import AIConfig, AIService

            config = AIConfig.from_settings(settings)
            ai_service = AIService(config)

            # Получаем характеристики
            characteristics = {}
            if product.characteristics:
                try:
                    characteristics = json.loads(product.characteristics) if isinstance(product.characteristics, str) else product.characteristics
                except:
                    pass

            # Считаем фото
            photos_count = 0
            if product.photo_urls:
                try:
                    photos = json.loads(product.photo_urls) if isinstance(product.photo_urls, str) else product.photo_urls
                    photos_count = len(photos) if photos else 0
                except:
                    pass

            success, result, error = ai_service.analyze_card(
                title=product.title or '',
                description=product.description or '',
                category=product.mapped_wb_category or '',
                characteristics=characteristics,
                photos_count=photos_count,
                price=float(getattr(product, 'price', 0) or 0)
            )

            if success:
                # Сохраняем анализ в кэш продукта
                product.ai_analysis = json.dumps(result, ensure_ascii=False)
                product.ai_analysis_at = datetime.utcnow()
                product.content_hash = compute_content_hash(product)
                db.session.commit()

                # Сохраняем в историю
                save_ai_history(seller.id, product.id, 'analysis',
                    {'title': product.title, 'photos_count': photos_count}, result,
                    ai_provider=settings.ai_provider, ai_model=settings.ai_model)

                return jsonify({'success': True, 'data': result})
            else:
                save_ai_history(seller.id, product.id, 'analysis', None, None, False, error, ai_provider=settings.ai_provider, ai_model=settings.ai_model)
                return jsonify({'success': False, 'error': error}), 500

        except Exception as e:
            logger.error(f"AI analyze error: {e}")
            return jsonify({'success': False, 'error': str(e)}), 500

    @app.route('/auto-import/ai/rich-content', methods=['POST'])
    @login_required
    def auto_import_ai_rich_content():
        """Генерация продающего rich контента для карточки"""
        if not current_user.seller:
            return jsonify({'success': False, 'error': 'Seller not found'}), 403

        data = request.get_json() or {}
        product_id = data.get('product_id')

        if not product_id:
            return jsonify({'success': False, 'error': 'product_id required'}), 400

        seller = current_user.seller
        settings = AutoImportSettings.query.filter_by(seller_id=seller.id).first()

        if not settings or not settings.ai_enabled:
            return jsonify({'success': False, 'error': 'AI не настроен'}), 400

        product = ImportedProduct.query.filter_by(
            id=product_id, seller_id=seller.id
        ).first()

        if not product:
            return jsonify({'success': False, 'error': 'Товар не найден'}), 404

        try:
            from ai_service import AIConfig, AIService

            config = AIConfig.from_settings(settings)
            ai_service = AIService(config)

            # Получаем характеристики
            characteristics = {}
            if product.characteristics:
                try:
                    characteristics = json.loads(product.characteristics) if isinstance(product.characteristics, str) else product.characteristics
                except:
                    pass

            success, result, error = ai_service.generate_rich_content(
                title=product.title or '',
                description=product.description or '',
                category=product.mapped_wb_category or '',
                brand=product.brand or '',
                characteristics=characteristics,
                price=float(getattr(product, 'price', 0) or 0)
            )

            if success:
                # Сохраняем rich контент в кэш продукта
                product.ai_rich_content = json.dumps(result, ensure_ascii=False)
                product.ai_analysis_at = datetime.utcnow()
                product.content_hash = compute_content_hash(product)
                db.session.commit()

                # Сохраняем в историю
                save_ai_history(seller.id, product.id, 'rich_content',
                    {'title': product.title, 'category': product.mapped_wb_category}, result,
                    ai_provider=settings.ai_provider, ai_model=settings.ai_model)

                return jsonify({
                    'success': True,
                    'data': result,
                    'original_description': product.description
                })
            else:
                save_ai_history(seller.id, product.id, 'rich_content', None, None, False, error, ai_provider=settings.ai_provider, ai_model=settings.ai_model)
                return jsonify({'success': False, 'error': error}), 500

        except Exception as e:
            logger.error(f"AI rich content error: {e}")
            return jsonify({'success': False, 'error': str(e)}), 500

    @app.route('/auto-import/ai/full-optimize', methods=['POST'])
    @login_required
    def auto_import_ai_full_optimize():
        """Полная AI-оптимизация карточки товара"""
        if not current_user.seller:
            return jsonify({'success': False, 'error': 'Seller not found'}), 403

        data = request.get_json() or {}
        product_id = data.get('product_id')

        if not product_id:
            return jsonify({'success': False, 'error': 'product_id required'}), 400

        seller = current_user.seller
        settings = AutoImportSettings.query.filter_by(seller_id=seller.id).first()

        if not settings or not settings.ai_enabled:
            return jsonify({'success': False, 'error': 'AI не настроен'}), 400

        product = ImportedProduct.query.filter_by(
            id=product_id, seller_id=seller.id
        ).first()

        if not product:
            return jsonify({'success': False, 'error': 'Товар не найден'}), 404

        try:
            from ai_service import AIConfig, AIService

            config = AIConfig.from_settings(settings)
            ai_service = AIService(config)

            # Получаем характеристики
            characteristics = {}
            if product.characteristics:
                try:
                    characteristics = json.loads(product.characteristics) if isinstance(product.characteristics, str) else product.characteristics
                except:
                    pass

            # Считаем фото
            photos_count = 0
            if product.photo_urls:
                try:
                    photos = json.loads(product.photo_urls) if isinstance(product.photo_urls, str) else product.photo_urls
                    photos_count = len(photos) if photos else 0
                except:
                    pass

            result = ai_service.full_optimize(
                title=product.title or '',
                description=product.description or '',
                category=product.mapped_wb_category or '',
                brand=product.brand or '',
                characteristics=characteristics,
                photos_count=photos_count,
                price=float(getattr(product, 'price', 0) or 0)
            )

            return jsonify({'success': True, 'data': result})

        except Exception as e:
            logger.error(f"AI full optimize error: {e}")
            return jsonify({'success': False, 'error': str(e)}), 500

    @app.route('/auto-import/ai/apply', methods=['POST'])
    @login_required
    def auto_import_ai_apply():
        """Применяет AI-улучшения к товару"""
        if not current_user.seller:
            return jsonify({'success': False, 'error': 'Seller not found'}), 403

        data = request.get_json() or {}
        product_id = data.get('product_id')
        updates = data.get('updates', {})

        if not product_id:
            return jsonify({'success': False, 'error': 'product_id required'}), 400

        seller = current_user.seller
        product = ImportedProduct.query.filter_by(
            id=product_id, seller_id=seller.id
        ).first()

        if not product:
            return jsonify({'success': False, 'error': 'Товар не найден'}), 404

        try:
            applied = []

            if 'title' in updates and updates['title']:
                product.title = updates['title']
                applied.append('title')

            if 'description' in updates and updates['description']:
                product.description = updates['description']
                applied.append('description')

            if 'keywords' in updates and updates['keywords']:
                # Сохраняем ключевые слова в отдельное поле или добавляем к характеристикам
                existing_chars = {}
                if product.characteristics:
                    try:
                        existing_chars = json.loads(product.characteristics) if isinstance(product.characteristics, str) else product.characteristics
                    except:
                        pass
                existing_chars['_keywords'] = updates['keywords']
                product.characteristics = json.dumps(existing_chars, ensure_ascii=False)
                applied.append('keywords')

            if 'bullet_points' in updates and updates['bullet_points']:
                existing_chars = {}
                if product.characteristics:
                    try:
                        existing_chars = json.loads(product.characteristics) if isinstance(product.characteristics, str) else product.characteristics
                    except:
                        pass
                existing_chars['_bullet_points'] = updates['bullet_points']
                product.characteristics = json.dumps(existing_chars, ensure_ascii=False)
                applied.append('bullet_points')

            # Размеры и габариты
            if 'sizes' in updates and updates['sizes']:
                product.ai_clothing_sizes = json.dumps(updates['sizes'], ensure_ascii=False)
                applied.append('sizes')

            if 'dimensions' in updates and updates['dimensions']:
                product.ai_dimensions = json.dumps(updates['dimensions'], ensure_ascii=False)
                applied.append('dimensions')

            # Сохраняем размеры в поле sizes если есть
            if 'sizes_text' in updates and updates['sizes_text']:
                product.sizes = updates['sizes_text']
                applied.append('sizes_text')

            # Характеристики из WB API - сохраняем в characteristics для отображения
            if 'wb_characteristics' in updates and updates['wb_characteristics']:
                wb_chars = updates['wb_characteristics']

                # Сохраняем в ai_dimensions как backup
                product.ai_dimensions = json.dumps(wb_chars, ensure_ascii=False)

                # Также добавляем в основные characteristics для отображения в карточке
                existing_chars = {}
                try:
                    if product.characteristics:
                        existing_chars = json.loads(product.characteristics) if isinstance(product.characteristics, str) else product.characteristics
                except:
                    existing_chars = {}

                # Маппинг WB характеристик в поля
                dimension_mapping = {
                    'объем': 'volume',
                    'объём': 'volume',
                    'вес': 'weight',
                    'масса': 'weight',
                    'длина': 'length',
                    'ширина': 'width',
                    'высота': 'height',
                    'глубина': 'depth',
                    'диаметр': 'diameter'
                }

                # Структура для sizes.dimensions
                sizes_data = {}
                try:
                    if product.sizes:
                        sizes_data = json.loads(product.sizes) if isinstance(product.sizes, str) else product.sizes
                        if isinstance(sizes_data, list):
                            sizes_data = {'simple_sizes': sizes_data}
                except:
                    sizes_data = {}

                if 'dimensions' not in sizes_data:
                    sizes_data['dimensions'] = {}

                # Извлекаем значения из wb_chars
                if 'extracted_values' in wb_chars:
                    for key, value in wb_chars['extracted_values'].items():
                        # Добавляем в characteristics
                        existing_chars[key] = value

                        # Также маппим на dimensions если это размерная характеристика
                        key_lower = key.lower()
                        for rus_name, eng_name in dimension_mapping.items():
                            if rus_name in key_lower and 'запас' not in key_lower:
                                try:
                                    val = float(str(value).replace(',', '.'))
                                    if eng_name not in sizes_data['dimensions']:
                                        sizes_data['dimensions'][eng_name] = []
                                    if val not in sizes_data['dimensions'][eng_name]:
                                        sizes_data['dimensions'][eng_name].append(val)
                                except:
                                    pass
                                break

                # Сохраняем
                product.characteristics = json.dumps(existing_chars, ensure_ascii=False)
                product.sizes = json.dumps(sizes_data, ensure_ascii=False)
                applied.append('wb_characteristics')

            # Все характеристики (all_characteristics) - аналогично wb_characteristics
            if 'all_characteristics' in updates and updates['all_characteristics']:
                all_chars = updates['all_characteristics']

                # Загружаем существующие characteristics
                existing_chars = {}
                try:
                    if product.characteristics:
                        existing_chars = json.loads(product.characteristics) if isinstance(product.characteristics, str) else product.characteristics
                except:
                    existing_chars = {}

                # Добавляем все извлечённые характеристики
                for key, value in all_chars.items():
                    if 'запас' not in key.lower():  # Пропускаем значения с запасом
                        existing_chars[key] = value

                product.characteristics = json.dumps(existing_chars, ensure_ascii=False)
                product.ai_attributes = json.dumps(all_chars, ensure_ascii=False)
                applied.append('all_characteristics')

            if applied:
                db.session.commit()

            return jsonify({
                'success': True,
                'applied': applied,
                'message': f"Применено: {', '.join(applied)}" if applied else "Нет изменений"
            })

        except Exception as e:
            db.session.rollback()
            logger.error(f"AI apply error: {e}")
            return jsonify({'success': False, 'error': str(e)}), 500

    @app.route('/auto-import/ai/generate-slide-image', methods=['POST'])
    @login_required
    def auto_import_ai_generate_slide_image():
        """Генерация изображения для слайда Rich-контента"""
        if not current_user.seller:
            return jsonify({'success': False, 'error': 'Seller not found'}), 403

        data = request.get_json() or {}
        product_id = data.get('product_id')
        slide_index = data.get('slide_index', 0)

        if not product_id:
            return jsonify({'success': False, 'error': 'product_id required'}), 400

        seller = current_user.seller
        settings = AutoImportSettings.query.filter_by(seller_id=seller.id).first()

        if not settings:
            return jsonify({'success': False, 'error': 'Настройки не найдены'}), 400

        # Проверяем настройки генерации изображений
        image_gen_enabled = getattr(settings, 'image_gen_enabled', False)
        if not image_gen_enabled:
            return jsonify({'success': False, 'error': 'Генерация изображений не включена. Настройте провайдер в настройках автоимпорта.'}), 400

        product = ImportedProduct.query.filter_by(
            id=product_id, seller_id=seller.id
        ).first()

        if not product:
            return jsonify({'success': False, 'error': 'Товар не найден'}), 404

        # Проверяем наличие Rich content
        if not product.ai_rich_content:
            return jsonify({'success': False, 'error': 'Сначала сгенерируйте Rich-контент'}), 400

        try:
            rich_content = json.loads(product.ai_rich_content)
            slides = rich_content.get('slides', [])

            if not slides:
                return jsonify({'success': False, 'error': 'Нет слайдов в Rich-контенте'}), 400

            if slide_index >= len(slides):
                return jsonify({'success': False, 'error': f'Слайд {slide_index} не найден'}), 400

            slide = slides[slide_index]

            # Получаем фотографии товара
            product_photos = []
            if product.photo_urls:
                try:
                    product_photos = json.loads(product.photo_urls) if isinstance(product.photo_urls, str) else product.photo_urls
                except:
                    pass

            # Импортируем сервис генерации изображений
            from image_generation_service import ImageGenerationConfig, ImageGenerationService, ImageProvider

            # Создаем конфигурацию
            provider_str = getattr(settings, 'image_gen_provider', 'together_flux') or 'together_flux'
            try:
                provider = ImageProvider(provider_str)
            except ValueError:
                provider = ImageProvider.TOGETHER_FLUX

            api_key = ""
            replicate_key = ""
            together_key = ""

            if provider == ImageProvider.OPENAI_DALLE:
                api_key = getattr(settings, 'openai_api_key', '') or ''
                if not api_key:
                    return jsonify({'success': False, 'error': 'OpenAI API ключ не настроен'}), 400
            elif provider == ImageProvider.TOGETHER_FLUX:
                together_key = getattr(settings, 'together_api_key', '') or ''
                if not together_key:
                    return jsonify({'success': False, 'error': 'Together AI API ключ не настроен. Получите бесплатно на api.together.xyz'}), 400
            else:
                replicate_key = getattr(settings, 'replicate_api_key', '') or ''
                if not replicate_key:
                    return jsonify({'success': False, 'error': 'Replicate API ключ не настроен'}), 400

            config = ImageGenerationConfig(
                provider=provider,
                api_key=api_key,
                replicate_api_key=replicate_key,
                together_api_key=together_key,
                openai_quality=getattr(settings, 'openai_image_quality', 'standard') or 'standard',
                openai_style=getattr(settings, 'openai_image_style', 'vivid') or 'vivid',
                default_width=getattr(settings, 'image_gen_width', 1440) or 1440,
                default_height=getattr(settings, 'image_gen_height', 810) or 810
            )

            service = ImageGenerationService(config)

            # Генерируем изображение
            success, image_bytes, error = service.generate_slide_image(
                slide_data=slide,
                product_photos=product_photos,
                product_title=product.title or ''
            )

            if not success:
                return jsonify({'success': False, 'error': error}), 500

            # Конвертируем в base64
            import base64
            image_b64 = base64.b64encode(image_bytes).decode('utf-8')

            return jsonify({
                'success': True,
                'slide_index': slide_index,
                'slide_type': slide.get('type', 'unknown'),
                'image_base64': image_b64,
                'image_size': len(image_bytes),
                'provider': provider.value
            })

        except Exception as e:
            logger.error(f"Image generation error: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return jsonify({'success': False, 'error': str(e)}), 500

    @app.route('/auto-import/ai/generate-all-slide-images', methods=['POST'])
    @login_required
    def auto_import_ai_generate_all_slide_images():
        """Генерация изображений для всех слайдов Rich-контента"""
        if not current_user.seller:
            return jsonify({'success': False, 'error': 'Seller not found'}), 403

        data = request.get_json() or {}
        product_id = data.get('product_id')

        if not product_id:
            return jsonify({'success': False, 'error': 'product_id required'}), 400

        seller = current_user.seller
        settings = AutoImportSettings.query.filter_by(seller_id=seller.id).first()

        if not settings:
            return jsonify({'success': False, 'error': 'Настройки не найдены'}), 400

        image_gen_enabled = getattr(settings, 'image_gen_enabled', False)
        if not image_gen_enabled:
            return jsonify({'success': False, 'error': 'Генерация изображений не включена'}), 400

        product = ImportedProduct.query.filter_by(
            id=product_id, seller_id=seller.id
        ).first()

        if not product:
            return jsonify({'success': False, 'error': 'Товар не найден'}), 404

        if not product.ai_rich_content:
            return jsonify({'success': False, 'error': 'Сначала сгенерируйте Rich-контент'}), 400

        try:
            rich_content = json.loads(product.ai_rich_content)
            slides = rich_content.get('slides', [])

            if not slides:
                return jsonify({'success': False, 'error': 'Нет слайдов'}), 400

            # Получаем фотографии товара
            product_photos = []
            if product.photo_urls:
                try:
                    product_photos = json.loads(product.photo_urls) if isinstance(product.photo_urls, str) else product.photo_urls
                except:
                    pass

            from image_generation_service import ImageGenerationConfig, ImageGenerationService, ImageProvider

            provider_str = getattr(settings, 'image_gen_provider', 'together_flux') or 'together_flux'
            try:
                provider = ImageProvider(provider_str)
            except ValueError:
                provider = ImageProvider.TOGETHER_FLUX

            api_key = ""
            replicate_key = ""
            together_key = ""

            if provider == ImageProvider.OPENAI_DALLE:
                api_key = getattr(settings, 'openai_api_key', '') or ''
            elif provider == ImageProvider.TOGETHER_FLUX:
                together_key = getattr(settings, 'together_api_key', '') or ''
            else:
                replicate_key = getattr(settings, 'replicate_api_key', '') or ''

            if not api_key and not replicate_key and not together_key:
                return jsonify({'success': False, 'error': 'API ключ не настроен'}), 400

            config = ImageGenerationConfig(
                provider=provider,
                api_key=api_key,
                replicate_api_key=replicate_key,
                together_api_key=together_key,
                openai_quality=getattr(settings, 'openai_image_quality', 'standard') or 'standard',
                openai_style=getattr(settings, 'openai_image_style', 'vivid') or 'vivid'
            )

            service = ImageGenerationService(config)

            # Генерируем все изображения
            results = service.generate_all_slides(
                slides=slides,
                product_photos=product_photos,
                product_title=product.title or ''
            )

            # Конвертируем в base64
            import base64
            output = []
            for r in results:
                item = {
                    'slide_number': r['slide_number'],
                    'slide_type': r['slide_type'],
                    'success': r['success'],
                    'error': r.get('error', '')
                }
                if r['success'] and r['image_bytes']:
                    item['image_base64'] = base64.b64encode(r['image_bytes']).decode('utf-8')
                    item['image_size'] = len(r['image_bytes'])
                output.append(item)

            successful = sum(1 for r in results if r['success'])

            return jsonify({
                'success': True,
                'total_slides': len(slides),
                'successful': successful,
                'failed': len(slides) - successful,
                'results': output,
                'provider': provider.value
            })

        except Exception as e:
            logger.error(f"Image generation error: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return jsonify({'success': False, 'error': str(e)}), 500

    @app.route('/auto-import/ai/image-providers', methods=['GET'])
    @login_required
    def auto_import_ai_image_providers():
        """Получение списка доступных провайдеров генерации изображений"""
        try:
            from image_generation_service import get_available_providers
            providers = get_available_providers()
            return jsonify({'success': True, 'providers': providers})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500

    # =====================================
    # AI History Endpoints
    # =====================================

    @app.route('/auto-import/ai/history', methods=['GET'])
    @login_required
    def auto_import_ai_history():
        """
        Получение истории AI запросов

        Query params:
            page: номер страницы (default: 1)
            per_page: записей на страницу (default: 20, max: 100)
            action_type: фильтр по типу действия
            product_id: фильтр по товару
            success: фильтр по успешности (true/false)
        """
        if not current_user.seller:
            return jsonify({'success': False, 'error': 'Seller not found'}), 403

        seller = current_user.seller
        page = request.args.get('page', 1, type=int)
        per_page = min(request.args.get('per_page', 20, type=int), 100)
        action_type = request.args.get('action_type')
        product_id = request.args.get('product_id', type=int)
        success_filter = request.args.get('success')

        # Строим запрос
        query = AIHistory.query.filter_by(seller_id=seller.id)

        if action_type:
            query = query.filter_by(action_type=action_type)
        if product_id:
            query = query.filter_by(imported_product_id=product_id)
        if success_filter is not None:
            query = query.filter_by(success=success_filter.lower() == 'true')

        # Сортировка по дате (новые первые)
        query = query.order_by(AIHistory.created_at.desc())

        # Пагинация
        pagination = query.paginate(page=page, per_page=per_page, error_out=False)

        # Формируем ответ
        history_items = []
        for item in pagination.items:
            item_dict = item.to_dict(include_prompts=False)
            item_dict['action_type_display'] = AIHistory.get_action_type_display(item.action_type)
            # Добавляем название товара если есть
            if item.imported_product:
                item_dict['product_title'] = item.imported_product.title
            history_items.append(item_dict)

        return jsonify({
            'success': True,
            'history': history_items,
            'pagination': {
                'page': page,
                'per_page': per_page,
                'total': pagination.total,
                'pages': pagination.pages,
                'has_next': pagination.has_next,
                'has_prev': pagination.has_prev
            }
        })

    @app.route('/auto-import/ai/history/<int:history_id>', methods=['GET'])
    @login_required
    def auto_import_ai_history_detail(history_id):
        """Получение детальной информации о записи AI истории"""
        if not current_user.seller:
            return jsonify({'success': False, 'error': 'Seller not found'}), 403

        seller = current_user.seller
        history = AIHistory.query.filter_by(
            id=history_id,
            seller_id=seller.id
        ).first()

        if not history:
            return jsonify({'success': False, 'error': 'Запись не найдена'}), 404

        item_dict = history.to_dict(include_prompts=True)
        item_dict['action_type_display'] = AIHistory.get_action_type_display(history.action_type)
        if history.imported_product:
            item_dict['product_title'] = history.imported_product.title

        return jsonify({
            'success': True,
            'history': item_dict
        })

    @app.route('/auto-import/ai/history/stats', methods=['GET'])
    @login_required
    def auto_import_ai_history_stats():
        """Статистика AI запросов"""
        if not current_user.seller:
            return jsonify({'success': False, 'error': 'Seller not found'}), 403

        seller = current_user.seller

        # Общая статистика
        total_requests = AIHistory.query.filter_by(seller_id=seller.id).count()
        successful_requests = AIHistory.query.filter_by(seller_id=seller.id, success=True).count()
        failed_requests = total_requests - successful_requests

        # Статистика по типам действий
        from sqlalchemy import func
        action_stats = db.session.query(
            AIHistory.action_type,
            func.count(AIHistory.id).label('count'),
            func.sum(AIHistory.tokens_used).label('total_tokens')
        ).filter_by(seller_id=seller.id).group_by(AIHistory.action_type).all()

        action_stats_list = []
        for action_type, count, total_tokens in action_stats:
            action_stats_list.append({
                'action_type': action_type,
                'action_type_display': AIHistory.get_action_type_display(action_type),
                'count': count,
                'total_tokens': total_tokens or 0
            })

        # Последние 10 запросов
        recent = AIHistory.query.filter_by(seller_id=seller.id).order_by(
            AIHistory.created_at.desc()
        ).limit(10).all()

        recent_list = []
        for item in recent:
            recent_list.append({
                'id': item.id,
                'action_type': item.action_type,
                'action_type_display': AIHistory.get_action_type_display(item.action_type),
                'success': item.success,
                'created_at': item.created_at.isoformat() if item.created_at else None
            })

        return jsonify({
            'success': True,
            'stats': {
                'total_requests': total_requests,
                'successful_requests': successful_requests,
                'failed_requests': failed_requests,
                'success_rate': round(successful_requests / total_requests * 100, 1) if total_requests > 0 else 0,
                'by_action_type': action_stats_list
            },
            'recent': recent_list
        })

    @app.route('/auto-import/ai/history/clear', methods=['POST'])
    @login_required
    def auto_import_ai_history_clear():
        """Очистка истории AI запросов (старше 30 дней)"""
        if not current_user.seller:
            return jsonify({'success': False, 'error': 'Seller not found'}), 403

        seller = current_user.seller
        days = request.get_json().get('days', 30) if request.is_json else 30

        from datetime import timedelta
        cutoff_date = datetime.utcnow() - timedelta(days=days)

        deleted = AIHistory.query.filter(
            AIHistory.seller_id == seller.id,
            AIHistory.created_at < cutoff_date
        ).delete()

        db.session.commit()

        return jsonify({
            'success': True,
            'deleted': deleted,
            'message': f'Удалено {deleted} записей старше {days} дней'
        })

    # =========================================================================
    # НОВЫЕ AI МЕТОДЫ ДЛЯ РАСШИРЕННОГО АНАЛИЗА ТОВАРОВ
    # =========================================================================

    @app.route('/auto-import/ai/extract-dimensions', methods=['POST'])
    @login_required
    def auto_import_ai_extract_dimensions():
        """Извлечение физических габаритов товара"""
        if not current_user.seller:
            return jsonify({'success': False, 'error': 'Seller not found'}), 403

        data = request.get_json() or {}
        product_id = data.get('product_id')

        if not product_id:
            return jsonify({'success': False, 'error': 'product_id required'}), 400

        seller = current_user.seller
        settings = AutoImportSettings.query.filter_by(seller_id=seller.id).first()

        if not settings or not settings.ai_enabled:
            return jsonify({'success': False, 'error': 'AI не настроен'}), 400

        product = ImportedProduct.query.filter_by(
            id=product_id, seller_id=seller.id
        ).first()

        if not product:
            return jsonify({'success': False, 'error': 'Товар не найден'}), 404

        try:
            from ai_service import AIConfig, AIService

            config = AIConfig.from_settings(settings)
            ai_service = AIService(config)

            characteristics = {}
            if product.characteristics:
                try:
                    characteristics = json.loads(product.characteristics) if isinstance(product.characteristics, str) else product.characteristics
                except:
                    pass

            success, result, error = ai_service.extract_dimensions(
                title=product.title or '',
                description=product.description or '',
                characteristics=characteristics,
                sizes_text=product.sizes or ''
            )

            if success:
                product.ai_dimensions = json.dumps(result, ensure_ascii=False)
                db.session.commit()

                save_ai_history(seller.id, product.id, 'dimensions', {'title': product.title}, result, ai_provider=settings.ai_provider, ai_model=settings.ai_model)
                return jsonify({'success': True, 'data': result})
            else:
                save_ai_history(seller.id, product.id, 'dimensions', None, None, False, error, ai_provider=settings.ai_provider, ai_model=settings.ai_model)
                return jsonify({'success': False, 'error': error}), 500

        except Exception as e:
            logger.error(f"AI extract dimensions error: {e}")
            return jsonify({'success': False, 'error': str(e)}), 500

    @app.route('/auto-import/ai/wb-category-characteristics', methods=['POST'])
    @login_required
    def auto_import_ai_wb_category_characteristics():
        """Получить характеристики категории WB и извлечь размеры на их основе"""
        if not current_user.seller:
            return jsonify({'success': False, 'error': 'Seller not found'}), 403

        data = request.get_json() or {}
        product_id = data.get('product_id')
        weight_margin_percent = data.get('weight_margin', 20)  # Запас по весу в % (по умолчанию 20%)

        if not product_id:
            return jsonify({'success': False, 'error': 'product_id required'}), 400

        seller = current_user.seller
        settings = AutoImportSettings.query.filter_by(seller_id=seller.id).first()

        if not settings or not settings.ai_enabled:
            return jsonify({'success': False, 'error': 'AI не настроен'}), 400

        product = ImportedProduct.query.filter_by(
            id=product_id, seller_id=seller.id
        ).first()

        if not product:
            return jsonify({'success': False, 'error': 'Товар не найден'}), 404

        # Получаем характеристики категории из WB API
        wb_characteristics = []
        size_characteristics = []
        category_id = product.wb_subject_id

        if category_id and seller.wb_api_key:
            try:
                from wb_api_client import WildberriesAPIClient
                with WildberriesAPIClient(seller.wb_api_key) as wb_client:
                    chars_config = wb_client.get_card_characteristics_config(int(category_id))
                    wb_characteristics = chars_config.get('data', [])

                    # Фильтруем характеристики связанные с размерами и весом
                    size_keywords = [
                        'длина', 'ширина', 'высота', 'глубина', 'диаметр',
                        'размер', 'вес', 'масса', 'объем', 'толщина',
                        'обхват', 'рабочая', 'максимальн', 'минимальн'
                    ]
                    for char in wb_characteristics:
                        char_name = char.get('name', '').lower()
                        if any(kw in char_name for kw in size_keywords):
                            size_characteristics.append({
                                'id': char.get('charcID'),
                                'name': char.get('name'),
                                'required': char.get('required', False),
                                'unit': char.get('unitName', ''),
                                'type': char.get('charcType'),
                                'maxCount': char.get('maxCount', 1)
                            })
            except Exception as e:
                logger.warning(f"Не удалось получить характеристики WB: {e}")

        try:
            from ai_service import AIConfig, AIService

            config = AIConfig.from_settings(settings)
            ai_service = AIService(config)

            # Подготавливаем характеристики товара
            product_characteristics = {}
            if product.characteristics:
                try:
                    product_characteristics = json.loads(product.characteristics) if isinstance(product.characteristics, str) else product.characteristics
                except:
                    pass

            # Получаем оригинальные данные поставщика как дополнительный источник
            original_data = {}
            original_description = ''
            original_sizes = {}
            if product.original_data:
                try:
                    original_data = json.loads(product.original_data) if isinstance(product.original_data, str) else product.original_data
                    original_description = original_data.get('description', '')
                    original_sizes = original_data.get('sizes', {})
                    original_chars = original_data.get('characteristics', {})
                    # Объединяем оригинальные характеристики с текущими
                    for k, v in original_chars.items():
                        if k not in product_characteristics:
                            product_characteristics[k] = v
                except:
                    pass

            # Формируем список характеристик для AI
            chars_list = []
            if size_characteristics:
                for sc in size_characteristics:
                    unit_str = f" ({sc['unit']})" if sc['unit'] else ""
                    required_str = " [ОБЯЗАТЕЛЬНО]" if sc['required'] else ""
                    chars_list.append(f"- {sc['name']}{unit_str}{required_str}")
            else:
                # Базовый список если нет данных из WB
                chars_list = [
                    "- Длина (см)",
                    "- Ширина (см)",
                    "- Высота (см)",
                    "- Глубина (см)",
                    "- Диаметр (см)",
                    "- Вес (г)",
                    "- Объем (мл)",
                    "- Рабочая длина (см)",
                    "- Максимальная длина (см)",
                    "- Толщина (см)"
                ]

            # Комбинируем описание - текущее + оригинальное (если отличается)
            combined_description = product.description or ''
            if original_description and original_description != combined_description:
                combined_description = f"{combined_description}\n\n=== ОРИГИНАЛЬНЫЕ ДАННЫЕ ПОСТАВЩИКА ===\n{original_description}"

            # Добавляем оригинальные размеры если есть
            sizes_text = product.sizes or ''
            if original_sizes:
                original_sizes_str = json.dumps(original_sizes, ensure_ascii=False) if isinstance(original_sizes, dict) else str(original_sizes)
                if original_sizes_str not in sizes_text:
                    sizes_text = f"{sizes_text}\n\nОригинальные размеры: {original_sizes_str}"

            # Вызываем AI с характеристиками категории
            success, result, error = ai_service.extract_category_dimensions(
                title=product.title or '',
                description=combined_description,
                characteristics=product_characteristics,
                sizes_text=sizes_text,
                category_characteristics=chars_list
            )

            if success:
                import re
                import math
                extracted = result.get('extracted_values', {})
                suggestions = result.get('suggestions', {})

                # Строим расширенный маппинг ВСЕХ размерных WB характеристик
                # wb_dim_chars - список ВСЕХ характеристик связанных с размерами/весом
                wb_dim_chars = {}  # name -> {char_info, type, is_pack}
                wb_char_map = {}   # type -> [list of char names] (для заполнения всех похожих)

                # Ключевые слова для размерных характеристик
                dimension_keywords = [
                    'длина', 'ширина', 'высота', 'глубина', 'диаметр', 'толщина',
                    'обхват', 'размер', 'вес', 'масса', 'объем', 'объём', 'радиус',
                    'минимальн', 'максимальн', 'рабочая', 'общая', 'внутренн', 'внешн'
                ]
                # Единицы измерения
                unit_keywords = ['см', 'мм', 'м', 'г', 'кг', 'мл', 'л']

                for char in size_characteristics:
                    char_name = char['name']
                    char_lower = char_name.lower()

                    # Проверяем является ли характеристика размерной
                    is_dimensional = any(kw in char_lower for kw in dimension_keywords)
                    has_unit = any(f'({u})' in char_lower or f' {u}' in char_lower for u in unit_keywords)

                    if is_dimensional or has_unit:
                        is_pack = 'упаков' in char_lower

                        # Определяем тип характеристики
                        char_type = None
                        if 'вес' in char_lower or 'масса' in char_lower:
                            char_type = 'weight_packed' if is_pack else 'weight'
                        elif 'длина' in char_lower:
                            if is_pack:
                                char_type = 'pack_length'
                            elif 'рабочая' in char_lower or 'рабоч' in char_lower:
                                char_type = 'working_length'
                            elif 'минимальн' in char_lower:
                                char_type = 'min_length'
                            elif 'максимальн' in char_lower:
                                char_type = 'max_length'
                            elif 'общая' in char_lower:
                                char_type = 'total_length'
                            elif 'внутренн' in char_lower:
                                char_type = 'inner_length'
                            else:
                                char_type = 'length'
                        elif 'ширина' in char_lower:
                            char_type = 'pack_width' if is_pack else 'width'
                        elif 'высота' in char_lower:
                            char_type = 'pack_height' if is_pack else 'height'
                        elif 'диаметр' in char_lower:
                            if 'минимальн' in char_lower:
                                char_type = 'min_diameter'
                            elif 'максимальн' in char_lower:
                                char_type = 'max_diameter'
                            elif 'внутренн' in char_lower:
                                char_type = 'inner_diameter'
                            else:
                                char_type = 'diameter'
                        elif 'глубина' in char_lower:
                            char_type = 'depth'
                        elif 'толщина' in char_lower:
                            char_type = 'thickness'
                        elif 'обхват' in char_lower:
                            char_type = 'circumference'
                        elif 'объем' in char_lower or 'объём' in char_lower:
                            char_type = 'volume'
                        elif 'радиус' in char_lower:
                            char_type = 'radius'
                        else:
                            char_type = 'other_dimension'

                        wb_dim_chars[char_name] = {
                            'char': char,
                            'type': char_type,
                            'is_pack': is_pack
                        }

                        # Добавляем в маппинг типов (один тип может иметь несколько имён)
                        if char_type not in wb_char_map:
                            wb_char_map[char_type] = []
                        wb_char_map[char_type].append(char_name)

                # Результат будет содержать только WB характеристики
                wb_extracted = {}

                # Добавляем предположения AI для обязательных полей
                for key, value in suggestions.items():
                    if key not in extracted:
                        numbers = re.findall(r'(\d+(?:[.,]\d+)?)', str(value))
                        if numbers:
                            try:
                                num_value = float(numbers[0].replace(',', '.'))
                                extracted[key] = num_value
                            except:
                                pass

                # Функция для сопоставления AI ключа с WB характеристикой
                def match_ai_key_to_wb(ai_key):
                    """Находит WB характеристику по ключу AI (fuzzy match)"""
                    ai_key_lower = ai_key.lower()
                    ai_key_normalized = re.sub(r'\s*\([^)]*\)\s*', '', ai_key_lower).strip()

                    matches = []
                    for wb_name, info in wb_dim_chars.items():
                        wb_lower = wb_name.lower()
                        wb_normalized = re.sub(r'\s*\([^)]*\)\s*', '', wb_lower).strip()

                        # Точное совпадение
                        if ai_key_lower == wb_lower or ai_key == wb_name:
                            return [(wb_name, 1.0)]

                        # Совпадение без единиц измерения
                        if ai_key_normalized == wb_normalized:
                            matches.append((wb_name, 0.95))
                            continue

                        # AI ключ содержится в WB имени или наоборот
                        if ai_key_normalized in wb_normalized:
                            matches.append((wb_name, 0.8))
                        elif wb_normalized in ai_key_normalized:
                            matches.append((wb_name, 0.7))

                    return sorted(matches, key=lambda x: -x[1]) if matches else []

                # Характеристики которые должны быть ТЕКСТОВЫМИ (не преобразовывать в числа)
                text_only_keywords = [
                    'наименование', 'название', 'описание', 'комплектация',
                    'артикул', 'бренд', 'модель', 'серия', 'коллекция',
                    'страна', 'производитель', 'состав', 'материал', 'цвет',
                    'особенности', 'назначение', 'применение', 'инструкция',
                    'противопоказания', 'предупреждения', 'гарантия', 'тип',
                    'вид', 'форма', 'функци', 'режим', 'питание', 'особенност'
                ]

                def is_text_only_char(char_name):
                    char_lower = char_name.lower()
                    return any(kw in char_lower for kw in text_only_keywords)

                # Извлекаем числовые значения из extracted и сопоставляем с WB
                length_val = None
                diameter_val = None
                weight_val = None
                width_val = None
                height_val = None

                for key, value in extracted.items():
                    key_lower = key.lower()
                    try:
                        # Сопоставляем с WB характеристиками
                        wb_matches = match_ai_key_to_wb(key)

                        # Определяем целевое имя WB характеристики
                        target_wb_name = wb_matches[0][0] if wb_matches else None

                        # Если это текстовая характеристика - сохраняем как текст
                        if target_wb_name and is_text_only_char(target_wb_name):
                            if target_wb_name not in wb_extracted:
                                wb_extracted[target_wb_name] = str(value).strip()
                            continue

                        nums = re.findall(r'(\d+(?:[.,]\d+)?)', str(value))
                        if nums:
                            val = float(nums[0].replace(',', '.'))

                            # Сохраняем значения для расчётов
                            if 'длина' in key_lower and 'упаков' not in key_lower:
                                if length_val is None:
                                    length_val = val
                            if 'диаметр' in key_lower and 'упаков' not in key_lower:
                                if diameter_val is None:
                                    diameter_val = val
                            if ('вес' in key_lower or 'масса' in key_lower) and 'упаков' not in key_lower:
                                if weight_val is None:
                                    weight_val = val
                            if 'ширина' in key_lower and 'упаков' not in key_lower:
                                if width_val is None:
                                    width_val = val
                            if 'высота' in key_lower and 'упаков' not in key_lower:
                                if height_val is None:
                                    height_val = val

                            # Сопоставляем с WB характеристиками
                            for wb_name, score in wb_matches:
                                if wb_name not in wb_extracted:
                                    wb_extracted[wb_name] = val
                                    break  # Берём только лучшее совпадение
                    except:
                        pass

                # Расчёт веса если не найден (на основе объёма цилиндра)
                if not weight_val and length_val and diameter_val:
                    radius = diameter_val / 2
                    volume_cm3 = math.pi * (radius ** 2) * length_val
                    weight_val = round(volume_cm3 * 1.1 * 0.6, 0)
                    weight_val = max(weight_val, 50)

                # Заполняем ВСЕ характеристики каждого типа
                def fill_all_chars_of_type(char_type, value, apply_margin=False):
                    """Заполняет все WB характеристики данного типа"""
                    if char_type in wb_char_map and value is not None:
                        final_value = value
                        if apply_margin:
                            final_value = int(round(value * (1 + weight_margin_percent / 100), 0))
                        for wb_name in wb_char_map[char_type]:
                            if wb_name not in wb_extracted:
                                wb_extracted[wb_name] = final_value

                # Заполняем базовые размеры
                fill_all_chars_of_type('length', int(length_val) if length_val else None)
                fill_all_chars_of_type('total_length', int(length_val) if length_val else None)
                fill_all_chars_of_type('max_length', int(length_val) if length_val else None)
                fill_all_chars_of_type('working_length', int(length_val * 0.8) if length_val else None)  # Примерно 80% от общей
                fill_all_chars_of_type('diameter', diameter_val)
                fill_all_chars_of_type('max_diameter', diameter_val)
                fill_all_chars_of_type('width', width_val or diameter_val)
                fill_all_chars_of_type('height', height_val or diameter_val)
                fill_all_chars_of_type('thickness', diameter_val)  # Толщина часто равна диаметру
                fill_all_chars_of_type('weight', weight_val, apply_margin=True)

                # Расчёт размеров упаковки
                if length_val:
                    pack_margin = 4
                    pack_length = int(min(max(length_val + pack_margin * 2, 10), 40))
                    pack_width = int(min(max((diameter_val or width_val or 5) + pack_margin * 2, 8), 25))
                    pack_height = int(min(max((diameter_val or height_val or 5) + pack_margin * 2, 5), 20))

                    fill_all_chars_of_type('pack_length', pack_length)
                    fill_all_chars_of_type('pack_width', pack_width)
                    fill_all_chars_of_type('pack_height', pack_height)

                    # Вес с упаковкой
                    if weight_val:
                        weight_with_margin = int(round(weight_val * (1 + weight_margin_percent / 100), 0))
                        fill_all_chars_of_type('weight_packed', weight_with_margin + 30)

                # Добавляем остальные извлечённые AI значения (если совпадают с WB характеристиками)
                for key, value in extracted.items():
                    wb_matches = match_ai_key_to_wb(key)
                    for wb_name, score in wb_matches:
                        if wb_name not in wb_extracted:
                            # Если характеристика текстовая - сохраняем как есть
                            if is_text_only_char(wb_name):
                                wb_extracted[wb_name] = str(value).strip()
                            else:
                                try:
                                    nums = re.findall(r'(\d+(?:[.,]\d+)?)', str(value))
                                    if nums:
                                        wb_extracted[wb_name] = float(nums[0].replace(',', '.'))
                                except:
                                    wb_extracted[wb_name] = value
                            break

                # === ДЕФОЛТЫ И ПРИНУДИТЕЛЬНОЕ ЗАПОЛНЕНИЕ ===
                for char in size_characteristics:
                    char_name = char['name']
                    char_lower = char_name.lower()

                    # Упаковка - дефолт "Анонимная непрозрачная"
                    if char_lower == 'упаковка' or 'тип упаковки' in char_lower:
                        if char_name not in wb_extracted:
                            wb_extracted[char_name] = 'Анонимная непрозрачная'

                    # Страна производства - дефолт "Китай"
                    if 'страна производ' in char_lower:
                        if char_name not in wb_extracted:
                            wb_extracted[char_name] = 'Китай'

                    # Принудительно заполняем вес если есть размеры но вес не заполнен
                    if ('вес' in char_lower or 'масса' in char_lower) and 'упаков' not in char_lower:
                        if char_name not in wb_extracted and weight_val:
                            weight_with_margin = int(round(weight_val * (1 + weight_margin_percent / 100), 0))
                            wb_extracted[char_name] = weight_with_margin

                    # Вес с упаковкой
                    if ('вес' in char_lower or 'масса' in char_lower) and 'упаков' in char_lower:
                        if char_name not in wb_extracted and weight_val:
                            weight_with_margin = int(round(weight_val * (1 + weight_margin_percent / 100), 0))
                            wb_extracted[char_name] = weight_with_margin + 30

                    # Размеры упаковки
                    if length_val:
                        pack_margin = 4
                        if 'длина' in char_lower and 'упаков' in char_lower:
                            if char_name not in wb_extracted:
                                wb_extracted[char_name] = int(min(max(length_val + pack_margin * 2, 10), 40))
                        if 'ширина' in char_lower and 'упаков' in char_lower:
                            if char_name not in wb_extracted:
                                wb_extracted[char_name] = int(min(max((diameter_val or width_val or 5) + pack_margin * 2, 8), 25))
                        if 'высота' in char_lower and 'упаков' in char_lower:
                            if char_name not in wb_extracted:
                                wb_extracted[char_name] = int(min(max((diameter_val or height_val or 5) + pack_margin * 2, 5), 20))

                result['extracted_values'] = wb_extracted
                result['_debug_raw_extracted'] = extracted  # Для отладки

                # Сохраняем результат
                result['wb_category_id'] = category_id
                result['wb_characteristics_count'] = len(size_characteristics)
                result['weight_margin_percent'] = weight_margin_percent

                product.ai_dimensions = json.dumps(result, ensure_ascii=False)
                db.session.commit()

                save_ai_history(seller.id, product.id, 'category_dimensions',
                              {'title': product.title, 'category_id': category_id}, result,
                              ai_provider=settings.ai_provider, ai_model=settings.ai_model)

                return jsonify({
                    'success': True,
                    'data': result,
                    'wb_characteristics': size_characteristics
                })
            else:
                save_ai_history(seller.id, product.id, 'category_dimensions', None, None, False, error, ai_provider=settings.ai_provider, ai_model=settings.ai_model)
                return jsonify({'success': False, 'error': error}), 500

        except Exception as e:
            logger.error(f"AI extract category dimensions error: {e}")
            import traceback
            traceback.print_exc()
            return jsonify({'success': False, 'error': str(e)}), 500

    @app.route('/auto-import/ai/extract-all-characteristics', methods=['POST'])
    @login_required
    def auto_import_ai_extract_all_characteristics():
        """Извлечь ВСЕ характеристики категории WB (обязательные + необязательные)"""
        if not current_user.seller:
            return jsonify({'success': False, 'error': 'Seller not found'}), 403

        data = request.get_json() or {}
        product_id = data.get('product_id')
        weight_margin_percent = data.get('weight_margin', 10)

        if not product_id:
            return jsonify({'success': False, 'error': 'product_id required'}), 400

        seller = current_user.seller
        settings = AutoImportSettings.query.filter_by(seller_id=seller.id).first()

        if not settings or not settings.ai_enabled:
            return jsonify({'success': False, 'error': 'AI не настроен'}), 400

        product = ImportedProduct.query.filter_by(
            id=product_id, seller_id=seller.id
        ).first()

        if not product:
            return jsonify({'success': False, 'error': 'Товар не найден'}), 404

        # Получаем ВСЕ характеристики категории из WB API
        all_wb_characteristics = []
        required_characteristics = []
        optional_characteristics = []
        category_id = product.wb_subject_id

        if category_id and seller.wb_api_key:
            try:
                from wb_api_client import WildberriesAPIClient
                with WildberriesAPIClient(seller.wb_api_key) as wb_client:
                    chars_config = wb_client.get_card_characteristics_config(int(category_id))
                    raw_chars = chars_config.get('data', [])

                    # Список характеристик которые WB помечает required но они НЕ нужны для заполнения
                    # (это служебные поля, которые заполняются автоматически или не нужны)
                    false_required_keywords = [
                        'артикул ozon', 'артикул озон', 'sku', 'икпу', 'код упаковки',
                        'номер декларации', 'номер сертификата', 'дата регистрации',
                        'дата окончания', 'ставка ндс', 'штрих', 'barcode', 'ean',
                        'уин', 'gtin', 'код тнвэд'
                    ]

                    for char in raw_chars:
                        char_name = char.get('name', '')
                        char_name_lower = char_name.lower()

                        # Проверяем, является ли это "ложной обязательной"
                        is_false_required = any(kw in char_name_lower for kw in false_required_keywords)

                        char_info = {
                            'id': char.get('charcID'),
                            'name': char_name,
                            'required': char.get('required', False) and not is_false_required,
                            'unit': char.get('unitName', ''),
                            'type': char.get('charcType'),
                            'maxCount': char.get('maxCount', 1),
                            'dictionary': char.get('dictionary', [])  # Допустимые значения
                        }
                        all_wb_characteristics.append(char_info)

                        if char_info['required']:
                            required_characteristics.append(char_info)
                        else:
                            optional_characteristics.append(char_info)

                    logger.info(f"Загружено {len(all_wb_characteristics)} характеристик для категории {category_id}: {len(required_characteristics)} обязательных, {len(optional_characteristics)} необязательных")
            except Exception as e:
                logger.warning(f"Не удалось получить характеристики WB: {e}")

        if not all_wb_characteristics:
            return jsonify({'success': False, 'error': 'Не удалось получить характеристики категории WB'}), 400

        try:
            from ai_service import AIConfig, AIService

            config = AIConfig.from_settings(settings)
            ai_service = AIService(config)

            # Подготавливаем данные товара
            product_characteristics = {}
            if product.characteristics:
                try:
                    product_characteristics = json.loads(product.characteristics) if isinstance(product.characteristics, str) else product.characteristics
                except:
                    pass

            # Получаем оригинальные данные поставщика
            original_data = {}
            original_description = ''
            if product.original_data:
                try:
                    original_data = json.loads(product.original_data) if isinstance(product.original_data, str) else product.original_data
                    original_description = original_data.get('description', '')
                    original_chars = original_data.get('characteristics', {})
                    for k, v in original_chars.items():
                        if k not in product_characteristics:
                            product_characteristics[k] = v
                except:
                    pass

            # Формируем список характеристик для AI
            chars_for_ai = []

            # Сначала обязательные
            if required_characteristics:
                chars_for_ai.append("=== ОБЯЗАТЕЛЬНЫЕ ХАРАКТЕРИСТИКИ (нужно заполнить!) ===")
                for char in required_characteristics:
                    unit_str = f" ({char['unit']})" if char['unit'] else ""
                    dict_values = char.get('dictionary', [])
                    if dict_values and len(dict_values) <= 20:
                        values_str = f" [допустимые: {', '.join(str(v) for v in dict_values[:10])}{'...' if len(dict_values) > 10 else ''}]"
                    else:
                        values_str = ""
                    chars_for_ai.append(f"- {char['name']}{unit_str}{values_str}")

            # Затем необязательные
            if optional_characteristics:
                chars_for_ai.append("\n=== НЕОБЯЗАТЕЛЬНЫЕ ХАРАКТЕРИСТИКИ (заполнить по возможности) ===")
                for char in optional_characteristics:
                    unit_str = f" ({char['unit']})" if char['unit'] else ""
                    dict_values = char.get('dictionary', [])
                    if dict_values and len(dict_values) <= 20:
                        values_str = f" [допустимые: {', '.join(str(v) for v in dict_values[:10])}{'...' if len(dict_values) > 10 else ''}]"
                    else:
                        values_str = ""
                    chars_for_ai.append(f"- {char['name']}{unit_str}{values_str}")

            # Комбинируем описание
            combined_description = product.description or ''
            if original_description and original_description != combined_description:
                combined_description = f"{combined_description}\n\n=== ОРИГИНАЛЬНЫЕ ДАННЫЕ ПОСТАВЩИКА ===\n{original_description}"

            # Собираем все доступные данные о товаре
            product_info = {
                'title': product.title or '',
                'description': combined_description,
                'brand': product.brand or '',
                'category': product.category or '',
                'colors': [],
                'materials': [],
                'sizes': {}
            }

            try:
                if product.colors:
                    product_info['colors'] = json.loads(product.colors) if isinstance(product.colors, str) else product.colors
            except:
                pass

            try:
                if product.materials:
                    product_info['materials'] = json.loads(product.materials) if isinstance(product.materials, str) else product.materials
            except:
                pass

            try:
                if product.sizes:
                    product_info['sizes'] = json.loads(product.sizes) if isinstance(product.sizes, str) else product.sizes
            except:
                pass

            # Вызываем AI для извлечения всех характеристик
            success, result, error = ai_service.extract_all_characteristics(
                product_info=product_info,
                existing_characteristics=product_characteristics,
                category_characteristics=chars_for_ai,
                original_data=original_data
            )

            if success and result:
                import re
                import math
                extracted = result.get('extracted_values', {})
                all_chars = required_characteristics + optional_characteristics

                # Строим расширенный маппинг ВСЕХ размерных WB характеристик
                wb_dim_chars = {}  # name -> {char_info, type, is_pack}
                wb_char_map = {}   # type -> [list of char names]

                # Ключевые слова для размерных характеристик
                dimension_keywords = [
                    'длина', 'ширина', 'высота', 'глубина', 'диаметр', 'толщина',
                    'обхват', 'размер', 'вес', 'масса', 'объем', 'объём', 'радиус',
                    'минимальн', 'максимальн', 'рабочая', 'общая', 'внутренн', 'внешн'
                ]
                unit_keywords = ['см', 'мм', 'м', 'г', 'кг', 'мл', 'л']

                for char in all_chars:
                    char_name = char['name']
                    char_lower = char_name.lower()

                    is_dimensional = any(kw in char_lower for kw in dimension_keywords)
                    has_unit = any(f'({u})' in char_lower or f' {u}' in char_lower for u in unit_keywords)

                    if is_dimensional or has_unit:
                        is_pack = 'упаков' in char_lower

                        char_type = None
                        if 'вес' in char_lower or 'масса' in char_lower:
                            char_type = 'weight_packed' if is_pack else 'weight'
                        elif 'длина' in char_lower:
                            if is_pack:
                                char_type = 'pack_length'
                            elif 'рабочая' in char_lower or 'рабоч' in char_lower:
                                char_type = 'working_length'
                            elif 'минимальн' in char_lower:
                                char_type = 'min_length'
                            elif 'максимальн' in char_lower:
                                char_type = 'max_length'
                            elif 'общая' in char_lower:
                                char_type = 'total_length'
                            elif 'внутренн' in char_lower:
                                char_type = 'inner_length'
                            else:
                                char_type = 'length'
                        elif 'ширина' in char_lower:
                            char_type = 'pack_width' if is_pack else 'width'
                        elif 'высота' in char_lower:
                            char_type = 'pack_height' if is_pack else 'height'
                        elif 'диаметр' in char_lower:
                            if 'минимальн' in char_lower:
                                char_type = 'min_diameter'
                            elif 'максимальн' in char_lower:
                                char_type = 'max_diameter'
                            elif 'внутренн' in char_lower:
                                char_type = 'inner_diameter'
                            else:
                                char_type = 'diameter'
                        elif 'глубина' in char_lower:
                            char_type = 'depth'
                        elif 'толщина' in char_lower:
                            char_type = 'thickness'
                        elif 'обхват' in char_lower:
                            char_type = 'circumference'
                        elif 'объем' in char_lower or 'объём' in char_lower:
                            char_type = 'volume'
                        elif 'радиус' in char_lower:
                            char_type = 'radius'
                        else:
                            char_type = 'other_dimension'

                        wb_dim_chars[char_name] = {
                            'char': char,
                            'type': char_type,
                            'is_pack': is_pack
                        }

                        if char_type not in wb_char_map:
                            wb_char_map[char_type] = []
                        wb_char_map[char_type].append(char_name)

                wb_extracted = {}

                # Функция для сопоставления AI ключа с WB характеристикой
                def match_ai_key_to_wb(ai_key):
                    ai_key_lower = ai_key.lower()
                    ai_key_normalized = re.sub(r'\s*\([^)]*\)\s*', '', ai_key_lower).strip()

                    matches = []
                    for wb_name, info in wb_dim_chars.items():
                        wb_lower = wb_name.lower()
                        wb_normalized = re.sub(r'\s*\([^)]*\)\s*', '', wb_lower).strip()

                        if ai_key_lower == wb_lower or ai_key == wb_name:
                            return [(wb_name, 1.0)]
                        if ai_key_normalized == wb_normalized:
                            matches.append((wb_name, 0.95))
                            continue
                        if ai_key_normalized in wb_normalized:
                            matches.append((wb_name, 0.8))
                        elif wb_normalized in ai_key_normalized:
                            matches.append((wb_name, 0.7))

                    return sorted(matches, key=lambda x: -x[1]) if matches else []

                # Также проверяем точное совпадение с любой характеристикой WB (не только размерной)
                def match_any_wb_char(ai_key):
                    ai_key_lower = ai_key.lower()
                    ai_key_normalized = re.sub(r'\s*\([^)]*\)\s*', '', ai_key_lower).strip()

                    for char in all_chars:
                        wb_name = char['name']
                        wb_lower = wb_name.lower()
                        wb_normalized = re.sub(r'\s*\([^)]*\)\s*', '', wb_lower).strip()

                        if ai_key_lower == wb_lower or ai_key == wb_name:
                            return wb_name
                        if ai_key_normalized == wb_normalized:
                            return wb_name
                    return None

                # Характеристики которые должны быть ТЕКСТОВЫМИ (не преобразовывать в числа)
                text_only_keywords = [
                    'наименование', 'название', 'описание', 'комплектация',
                    'артикул', 'бренд', 'модель', 'серия', 'коллекция',
                    'страна', 'производитель', 'состав', 'материал', 'цвет',
                    'особенности', 'назначение', 'применение', 'инструкция',
                    'противопоказания', 'предупреждения', 'гарантия', 'тип',
                    'вид', 'форма', 'функци', 'режим', 'питание', 'особенност'
                ]

                def is_text_only_char(char_name):
                    """Проверяет, является ли характеристика текстовой (не числовой)"""
                    char_lower = char_name.lower()
                    return any(kw in char_lower for kw in text_only_keywords)

                # Извлекаем значения и сопоставляем с WB
                length_val = None
                diameter_val = None
                weight_val = None
                width_val = None
                height_val = None

                for key, value in extracted.items():
                    key_lower = key.lower()
                    try:
                        # Сначала пробуем сопоставить с WB характеристикой
                        wb_matches = match_ai_key_to_wb(key)
                        wb_exact = match_any_wb_char(key)

                        # Определяем целевое имя WB характеристики
                        target_wb_name = None
                        if wb_matches:
                            target_wb_name = wb_matches[0][0]
                        elif wb_exact:
                            target_wb_name = wb_exact

                        # Если это текстовая характеристика - сохраняем как текст
                        if target_wb_name and is_text_only_char(target_wb_name):
                            if target_wb_name not in wb_extracted:
                                wb_extracted[target_wb_name] = str(value).strip()
                            continue

                        nums = re.findall(r'(\d+(?:[.,]\d+)?)', str(value))
                        if nums:
                            val = float(nums[0].replace(',', '.'))

                            # Сохраняем значения для расчётов
                            if 'длина' in key_lower and 'упаков' not in key_lower:
                                if length_val is None:
                                    length_val = val
                            if 'диаметр' in key_lower and 'упаков' not in key_lower:
                                if diameter_val is None:
                                    diameter_val = val
                            if ('вес' in key_lower or 'масса' in key_lower) and 'упаков' not in key_lower:
                                if weight_val is None:
                                    weight_val = val
                            if 'ширина' in key_lower and 'упаков' not in key_lower:
                                if width_val is None:
                                    width_val = val
                            if 'высота' in key_lower and 'упаков' not in key_lower:
                                if height_val is None:
                                    height_val = val

                            # Сопоставляем с WB
                            if wb_matches:
                                for wb_name, score in wb_matches:
                                    if wb_name not in wb_extracted:
                                        wb_extracted[wb_name] = val
                                        break
                            elif wb_exact and wb_exact not in wb_extracted:
                                wb_extracted[wb_exact] = val
                        else:
                            # Нечисловое значение
                            if wb_exact and wb_exact not in wb_extracted:
                                wb_extracted[wb_exact] = value
                    except:
                        pass

                # Расчёт веса если не найден
                if not weight_val and length_val and diameter_val:
                    radius = diameter_val / 2
                    volume_cm3 = math.pi * (radius ** 2) * length_val
                    weight_val = round(volume_cm3 * 1.1 * 0.6, 0)
                    weight_val = max(weight_val, 50)

                # Заполняем ВСЕ характеристики каждого типа
                def fill_all_chars_of_type(char_type, value, apply_margin=False):
                    if char_type in wb_char_map and value is not None:
                        final_value = value
                        if apply_margin:
                            final_value = int(round(value * (1 + weight_margin_percent / 100), 0))
                        for wb_name in wb_char_map[char_type]:
                            if wb_name not in wb_extracted:
                                wb_extracted[wb_name] = final_value

                # Заполняем базовые размеры
                fill_all_chars_of_type('length', int(length_val) if length_val else None)
                fill_all_chars_of_type('total_length', int(length_val) if length_val else None)
                fill_all_chars_of_type('max_length', int(length_val) if length_val else None)
                fill_all_chars_of_type('working_length', int(length_val * 0.8) if length_val else None)
                fill_all_chars_of_type('diameter', diameter_val)
                fill_all_chars_of_type('max_diameter', diameter_val)
                fill_all_chars_of_type('width', width_val or diameter_val)
                fill_all_chars_of_type('height', height_val or diameter_val)
                fill_all_chars_of_type('thickness', diameter_val)
                fill_all_chars_of_type('weight', weight_val, apply_margin=True)

                # Размеры упаковки
                if length_val:
                    pack_margin = 4
                    pack_length = int(min(max(length_val + pack_margin * 2, 10), 40))
                    pack_width = int(min(max((diameter_val or width_val or 5) + pack_margin * 2, 8), 25))
                    pack_height = int(min(max((diameter_val or height_val or 5) + pack_margin * 2, 5), 20))

                    fill_all_chars_of_type('pack_length', pack_length)
                    fill_all_chars_of_type('pack_width', pack_width)
                    fill_all_chars_of_type('pack_height', pack_height)

                    if weight_val:
                        weight_with_margin = int(round(weight_val * (1 + weight_margin_percent / 100), 0))
                        fill_all_chars_of_type('weight_packed', weight_with_margin + 30)

                # Добавляем остальные характеристики из AI (если совпадают с WB)
                for key, value in extracted.items():
                    wb_exact = match_any_wb_char(key)
                    if wb_exact and wb_exact not in wb_extracted:
                        # Если характеристика текстовая - сохраняем как есть
                        if is_text_only_char(wb_exact):
                            wb_extracted[wb_exact] = str(value).strip()
                        else:
                            try:
                                nums = re.findall(r'(\d+(?:[.,]\d+)?)', str(value))
                                if nums:
                                    wb_extracted[wb_exact] = float(nums[0].replace(',', '.'))
                                else:
                                    wb_extracted[wb_exact] = value
                            except:
                                wb_extracted[wb_exact] = value

                # === ДЕФОЛТЫ И ПРИНУДИТЕЛЬНОЕ ЗАПОЛНЕНИЕ ===

                # Ищем характеристику "Упаковка" и ставим дефолт если не заполнена
                for char in all_chars:
                    char_name = char['name']
                    char_lower = char_name.lower()

                    # Упаковка - дефолт "Анонимная непрозрачная"
                    if char_lower == 'упаковка' or 'тип упаковки' in char_lower:
                        if char_name not in wb_extracted:
                            wb_extracted[char_name] = 'Анонимная непрозрачная'

                    # Страна производства - дефолт "Китай"
                    if 'страна производ' in char_lower:
                        if char_name not in wb_extracted:
                            wb_extracted[char_name] = 'Китай'

                    # Принудительно заполняем вес если есть размеры но вес не заполнен
                    if ('вес' in char_lower or 'масса' in char_lower) and 'упаков' not in char_lower:
                        if char_name not in wb_extracted and weight_val:
                            weight_with_margin = int(round(weight_val * (1 + weight_margin_percent / 100), 0))
                            wb_extracted[char_name] = weight_with_margin

                    # Вес с упаковкой
                    if ('вес' in char_lower or 'масса' in char_lower) and 'упаков' in char_lower:
                        if char_name not in wb_extracted and weight_val:
                            weight_with_margin = int(round(weight_val * (1 + weight_margin_percent / 100), 0))
                            wb_extracted[char_name] = weight_with_margin + 30

                    # Размеры упаковки
                    if length_val:
                        pack_margin = 4
                        if 'длина' in char_lower and 'упаков' in char_lower:
                            if char_name not in wb_extracted:
                                wb_extracted[char_name] = int(min(max(length_val + pack_margin * 2, 10), 40))
                        if 'ширина' in char_lower and 'упаков' in char_lower:
                            if char_name not in wb_extracted:
                                wb_extracted[char_name] = int(min(max((diameter_val or width_val or 5) + pack_margin * 2, 8), 25))
                        if 'высота' in char_lower and 'упаков' in char_lower:
                            if char_name not in wb_extracted:
                                wb_extracted[char_name] = int(min(max((diameter_val or height_val or 5) + pack_margin * 2, 5), 20))

                result['extracted_values'] = wb_extracted

                # === ВАЛИДАЦИЯ БРЕНДА ЧЕРЕЗ WB API ===
                brand_validation = None
                if product.brand and seller.wb_api_key:
                    try:
                        from wb_api_client import WildberriesAPIClient
                        with WildberriesAPIClient(seller.wb_api_key) as wb_client:
                            brand_result = wb_client.validate_brand(product.brand)
                            brand_validation = {
                                'original_brand': product.brand,
                                'valid': brand_result.get('valid', False),
                                'exact_match': brand_result.get('exact_match'),
                                'suggestions': brand_result.get('suggestions', [])[:5]
                            }
                            # Если бренд найден - используем точное имя из WB
                            if brand_validation['valid'] and brand_validation['exact_match']:
                                wb_brand = brand_validation['exact_match'].get('name')
                                if wb_brand:
                                    product.brand = wb_brand
                                    brand_validation['corrected_to'] = wb_brand
                    except Exception as e:
                        logger.warning(f"Brand validation failed: {e}")
                        brand_validation = {'error': str(e)}

                result['brand_validation'] = brand_validation

                # Считаем статистику
                extracted_count = len(result.get('extracted_values', {}))
                required_filled = sum(1 for char in required_characteristics
                                    if char['name'] in result.get('extracted_values', {}))

                result['statistics'] = {
                    'total_characteristics': len(all_wb_characteristics),
                    'required_count': len(required_characteristics),
                    'optional_count': len(optional_characteristics),
                    'extracted_count': extracted_count,
                    'required_filled': required_filled,
                    'fill_rate': round(extracted_count / len(all_wb_characteristics) * 100, 1) if all_wb_characteristics else 0
                }

                result['wb_category_id'] = category_id
                result['weight_margin_percent'] = weight_margin_percent

                # Сохраняем результат
                product.ai_attributes = json.dumps(result, ensure_ascii=False)
                db.session.commit()

                save_ai_history(seller.id, product.id, 'extract_all_characteristics',
                              {'title': product.title, 'category_id': category_id}, result,
                              ai_provider=settings.ai_provider, ai_model=settings.ai_model)

                return jsonify({
                    'success': True,
                    'data': result,
                    'required_characteristics': required_characteristics,
                    'optional_characteristics': optional_characteristics[:50],  # Ограничиваем для UI
                    'brand_validation': brand_validation
                })
            else:
                save_ai_history(seller.id, product.id, 'extract_all_characteristics',
                              None, None, False, error,
                              ai_provider=settings.ai_provider, ai_model=settings.ai_model)
                return jsonify({'success': False, 'error': error}), 500

        except Exception as e:
            logger.error(f"AI extract all characteristics error: {e}")
            import traceback
            traceback.print_exc()
            return jsonify({'success': False, 'error': str(e)}), 500

    @app.route('/auto-import/ai/parse-clothing-sizes', methods=['POST'])
    @login_required
    def auto_import_ai_parse_clothing_sizes():
        """Парсинг и стандартизация размеров одежды"""
        if not current_user.seller:
            return jsonify({'success': False, 'error': 'Seller not found'}), 403

        data = request.get_json() or {}
        product_id = data.get('product_id')

        if not product_id:
            return jsonify({'success': False, 'error': 'product_id required'}), 400

        seller = current_user.seller
        settings = AutoImportSettings.query.filter_by(seller_id=seller.id).first()

        if not settings or not settings.ai_enabled:
            return jsonify({'success': False, 'error': 'AI не настроен'}), 400

        product = ImportedProduct.query.filter_by(
            id=product_id, seller_id=seller.id
        ).first()

        if not product:
            return jsonify({'success': False, 'error': 'Товар не найден'}), 404

        try:
            from ai_service import AIConfig, AIService

            config = AIConfig.from_settings(settings)
            ai_service = AIService(config)

            success, result, error = ai_service.parse_clothing_sizes(
                title=product.title or '',
                sizes_text=product.sizes or '',
                description=product.description or '',
                category=product.mapped_wb_category or ''
            )

            if success:
                product.ai_clothing_sizes = json.dumps(result, ensure_ascii=False)
                db.session.commit()

                save_ai_history(seller.id, product.id, 'clothing_sizes', {'title': product.title, 'sizes': product.sizes}, result, ai_provider=settings.ai_provider, ai_model=settings.ai_model)
                return jsonify({'success': True, 'data': result})
            else:
                save_ai_history(seller.id, product.id, 'clothing_sizes', None, None, False, error, ai_provider=settings.ai_provider, ai_model=settings.ai_model)
                return jsonify({'success': False, 'error': error}), 500

        except Exception as e:
            logger.error(f"AI parse clothing sizes error: {e}")
            return jsonify({'success': False, 'error': str(e)}), 500

    @app.route('/auto-import/ai/detect-brand', methods=['POST'])
    @login_required
    def auto_import_ai_detect_brand():
        """Автоматическое определение бренда товара"""
        if not current_user.seller:
            return jsonify({'success': False, 'error': 'Seller not found'}), 403

        data = request.get_json() or {}
        product_id = data.get('product_id')

        if not product_id:
            return jsonify({'success': False, 'error': 'product_id required'}), 400

        seller = current_user.seller
        settings = AutoImportSettings.query.filter_by(seller_id=seller.id).first()

        if not settings or not settings.ai_enabled:
            return jsonify({'success': False, 'error': 'AI не настроен'}), 400

        product = ImportedProduct.query.filter_by(
            id=product_id, seller_id=seller.id
        ).first()

        if not product:
            return jsonify({'success': False, 'error': 'Товар не найден'}), 404

        try:
            from ai_service import AIConfig, AIService

            config = AIConfig.from_settings(settings)
            ai_service = AIService(config)

            characteristics = {}
            if product.characteristics:
                try:
                    characteristics = json.loads(product.characteristics) if isinstance(product.characteristics, str) else product.characteristics
                except:
                    pass

            success, result, error = ai_service.detect_brand(
                title=product.title or '',
                description=product.description or '',
                characteristics=characteristics,
                category=product.mapped_wb_category or ''
            )

            if success:
                product.ai_detected_brand = json.dumps(result, ensure_ascii=False)
                if result.get('confidence', 0) >= 0.7 and result.get('brand_normalized'):
                    product.brand = result['brand_normalized']
                db.session.commit()

                save_ai_history(seller.id, product.id, 'brand_detection', {'title': product.title}, result, ai_provider=settings.ai_provider, ai_model=settings.ai_model)
                return jsonify({'success': True, 'data': result})
            else:
                save_ai_history(seller.id, product.id, 'brand_detection', None, None, False, error, ai_provider=settings.ai_provider, ai_model=settings.ai_model)
                return jsonify({'success': False, 'error': error}), 500

        except Exception as e:
            logger.error(f"AI detect brand error: {e}")
            return jsonify({'success': False, 'error': str(e)}), 500

    @app.route('/auto-import/ai/detect-materials', methods=['POST'])
    @login_required
    def auto_import_ai_detect_materials():
        """Определение материалов и состава товара"""
        if not current_user.seller:
            return jsonify({'success': False, 'error': 'Seller not found'}), 403

        data = request.get_json() or {}
        product_id = data.get('product_id')

        if not product_id:
            return jsonify({'success': False, 'error': 'product_id required'}), 400

        seller = current_user.seller
        settings = AutoImportSettings.query.filter_by(seller_id=seller.id).first()

        if not settings or not settings.ai_enabled:
            return jsonify({'success': False, 'error': 'AI не настроен'}), 400

        product = ImportedProduct.query.filter_by(
            id=product_id, seller_id=seller.id
        ).first()

        if not product:
            return jsonify({'success': False, 'error': 'Товар не найден'}), 404

        try:
            from ai_service import AIConfig, AIService

            config = AIConfig.from_settings(settings)
            ai_service = AIService(config)

            characteristics = {}
            if product.characteristics:
                try:
                    characteristics = json.loads(product.characteristics) if isinstance(product.characteristics, str) else product.characteristics
                except:
                    pass

            success, result, error = ai_service.detect_materials(
                title=product.title or '',
                description=product.description or '',
                characteristics=characteristics,
                category=product.mapped_wb_category or ''
            )

            if success:
                product.ai_materials = json.dumps(result, ensure_ascii=False)
                db.session.commit()

                save_ai_history(seller.id, product.id, 'materials', {'title': product.title}, result, ai_provider=settings.ai_provider, ai_model=settings.ai_model)
                return jsonify({'success': True, 'data': result})
            else:
                save_ai_history(seller.id, product.id, 'materials', None, None, False, error, ai_provider=settings.ai_provider, ai_model=settings.ai_model)
                return jsonify({'success': False, 'error': error}), 500

        except Exception as e:
            logger.error(f"AI detect materials error: {e}")
            return jsonify({'success': False, 'error': str(e)}), 500

    @app.route('/auto-import/ai/detect-color', methods=['POST'])
    @login_required
    def auto_import_ai_detect_color():
        """Определение цвета товара"""
        if not current_user.seller:
            return jsonify({'success': False, 'error': 'Seller not found'}), 403

        data = request.get_json() or {}
        product_id = data.get('product_id')

        if not product_id:
            return jsonify({'success': False, 'error': 'product_id required'}), 400

        seller = current_user.seller
        settings = AutoImportSettings.query.filter_by(seller_id=seller.id).first()

        if not settings or not settings.ai_enabled:
            return jsonify({'success': False, 'error': 'AI не настроен'}), 400

        product = ImportedProduct.query.filter_by(
            id=product_id, seller_id=seller.id
        ).first()

        if not product:
            return jsonify({'success': False, 'error': 'Товар не найден'}), 404

        try:
            from ai_service import AIConfig, AIService

            config = AIConfig.from_settings(settings)
            ai_service = AIService(config)

            characteristics = {}
            if product.characteristics:
                try:
                    characteristics = json.loads(product.characteristics) if isinstance(product.characteristics, str) else product.characteristics
                except:
                    pass

            success, result, error = ai_service.detect_color(
                title=product.title or '',
                description=product.description or '',
                characteristics=characteristics
            )

            if success:
                product.ai_colors = json.dumps(result, ensure_ascii=False)
                db.session.commit()

                save_ai_history(seller.id, product.id, 'color', {'title': product.title}, result, ai_provider=settings.ai_provider, ai_model=settings.ai_model)
                return jsonify({'success': True, 'data': result})
            else:
                save_ai_history(seller.id, product.id, 'color', None, None, False, error, ai_provider=settings.ai_provider, ai_model=settings.ai_model)
                return jsonify({'success': False, 'error': error}), 500

        except Exception as e:
            logger.error(f"AI detect color error: {e}")
            return jsonify({'success': False, 'error': str(e)}), 500

    @app.route('/auto-import/ai/detect-category', methods=['POST'])
    @login_required
    def auto_import_ai_detect_category():
        """Определение категории WB с помощью AI"""
        if not current_user.seller:
            return jsonify({'success': False, 'error': 'Seller not found'}), 403

        data = request.get_json() or {}
        product_id = data.get('product_id')

        if not product_id:
            return jsonify({'success': False, 'error': 'product_id required'}), 400

        seller = current_user.seller
        settings = AutoImportSettings.query.filter_by(seller_id=seller.id).first()

        if not settings or not settings.ai_enabled:
            return jsonify({'success': False, 'error': 'AI не настроен'}), 400

        product = ImportedProduct.query.filter_by(
            id=product_id, seller_id=seller.id
        ).first()

        if not product:
            return jsonify({'success': False, 'error': 'Товар не найден'}), 404

        try:
            from ai_service import AIConfig, AIService
            from wb_categories_mapping import WB_ADULT_CATEGORIES

            config = AIConfig.from_settings(settings)
            ai_service = AIService(config)
            ai_service.set_categories(WB_ADULT_CATEGORIES)

            all_categories = []
            if product.all_categories:
                try:
                    all_categories = json.loads(product.all_categories) if isinstance(product.all_categories, str) else product.all_categories
                except:
                    pass

            category_id, category_name, confidence, reasoning = ai_service.detect_category(
                product_title=product.title or '',
                source_category=product.category or '',
                all_categories=all_categories,
                brand=product.brand or '',
                description=product.description or ''
            )

            result = {
                'category_id': category_id,
                'category_name': category_name,
                'confidence': confidence,
                'reasoning': reasoning,
                'original_category': product.category,
                'current_wb_category': product.mapped_wb_category
            }

            if category_id and category_name:
                product.mapped_wb_category = category_name
                product.wb_subject_id = category_id
                product.category_confidence = confidence
                db.session.commit()

                save_ai_history(seller.id, product.id, 'category_detection',
                              {'title': product.title, 'source_category': product.category}, result,
                              ai_provider=settings.ai_provider, ai_model=settings.ai_model)
                return jsonify({'success': True, 'data': result})
            else:
                save_ai_history(seller.id, product.id, 'category_detection', None, None, False, reasoning, ai_provider=settings.ai_provider, ai_model=settings.ai_model)
                return jsonify({'success': False, 'error': reasoning}), 500

        except Exception as e:
            logger.error(f"AI detect category error: {e}")
            return jsonify({'success': False, 'error': str(e)}), 500

    @app.route('/auto-import/ai/extract-all-attributes', methods=['POST'])
    @login_required
    def auto_import_ai_extract_all_attributes():
        """Комплексное извлечение всех атрибутов товара за один запрос"""
        if not current_user.seller:
            return jsonify({'success': False, 'error': 'Seller not found'}), 403

        data = request.get_json() or {}
        product_id = data.get('product_id')

        if not product_id:
            return jsonify({'success': False, 'error': 'product_id required'}), 400

        seller = current_user.seller
        settings = AutoImportSettings.query.filter_by(seller_id=seller.id).first()

        if not settings or not settings.ai_enabled:
            return jsonify({'success': False, 'error': 'AI не настроен'}), 400

        product = ImportedProduct.query.filter_by(
            id=product_id, seller_id=seller.id
        ).first()

        if not product:
            return jsonify({'success': False, 'error': 'Товар не найден'}), 404

        try:
            from ai_service import AIConfig, AIService

            config = AIConfig.from_settings(settings)
            ai_service = AIService(config)

            characteristics = {}
            if product.characteristics:
                try:
                    characteristics = json.loads(product.characteristics) if isinstance(product.characteristics, str) else product.characteristics
                except:
                    pass

            success, result, error = ai_service.extract_all_attributes(
                title=product.title or '',
                description=product.description or '',
                characteristics=characteristics,
                category=product.mapped_wb_category or '',
                sizes_text=product.sizes or ''
            )

            if success:
                product.ai_attributes = json.dumps(result, ensure_ascii=False)

                if result.get('brand', {}).get('name') and result['brand'].get('confidence', 0) >= 0.7:
                    product.brand = result['brand'].get('normalized') or result['brand'].get('name')

                if result.get('gender'):
                    product.ai_gender = result['gender']

                if result.get('age_group'):
                    product.ai_age_group = result['age_group']

                if result.get('season'):
                    product.ai_season = result['season']

                if result.get('country'):
                    product.ai_country = result['country']

                if result.get('colors'):
                    product.ai_colors = json.dumps(result['colors'], ensure_ascii=False)

                if result.get('materials'):
                    product.ai_materials = json.dumps(result['materials'], ensure_ascii=False)

                if result.get('dimensions'):
                    product.ai_dimensions = json.dumps(result['dimensions'], ensure_ascii=False)

                if result.get('clothing_size'):
                    product.ai_clothing_sizes = json.dumps(result['clothing_size'], ensure_ascii=False)

                db.session.commit()

                save_ai_history(seller.id, product.id, 'all_attributes', {'title': product.title}, result, ai_provider=settings.ai_provider, ai_model=settings.ai_model)
                return jsonify({'success': True, 'data': result})
            else:
                save_ai_history(seller.id, product.id, 'all_attributes', None, None, False, error, ai_provider=settings.ai_provider, ai_model=settings.ai_model)
                return jsonify({'success': False, 'error': error}), 500

        except Exception as e:
            logger.error(f"AI extract all attributes error: {e}")
            return jsonify({'success': False, 'error': str(e)}), 500

    @app.route('/auto-import/validate-brand', methods=['POST'])
    @login_required
    def auto_import_validate_brand():
        """
        Валидация бренда через WB API

        Проверяет существует ли бренд в справочнике WB и предлагает похожие.

        Request:
            {"brand": "Nike"}

        Response:
            {
                "success": true,
                "valid": true,
                "exact_match": {"id": 1234, "name": "Nike"},
                "suggestions": []
            }
        """
        if not current_user.seller:
            return jsonify({'success': False, 'error': 'Seller not found'}), 403

        data = request.get_json() or {}
        brand_name = data.get('brand', '').strip()

        if not brand_name:
            return jsonify({'success': False, 'error': 'brand required'}), 400

        seller = current_user.seller

        if not seller.wb_api_key:
            return jsonify({'success': False, 'error': 'WB API ключ не настроен'}), 400

        try:
            from wb_api_client import WildberriesAPIClient

            with WildberriesAPIClient(seller.wb_api_key) as wb_client:
                result = wb_client.validate_brand(brand_name)

                return jsonify({
                    'success': True,
                    'valid': result.get('valid', False),
                    'exact_match': result.get('exact_match'),
                    'suggestions': result.get('suggestions', []),
                    'searched_brand': brand_name
                })

        except Exception as e:
            logger.error(f"Brand validation error: {e}")
            return jsonify({'success': False, 'error': str(e)}), 500

    @app.route('/auto-import/search-brands', methods=['GET'])
    @login_required
    def auto_import_search_brands():
        """
        Поиск брендов в справочнике WB

        Query params:
            q: строка поиска
            limit: максимум результатов (по умолчанию 20)

        Response:
            {
                "success": true,
                "brands": [{"id": 1234, "name": "Nike"}, ...]
            }
        """
        if not current_user.seller:
            return jsonify({'success': False, 'error': 'Seller not found'}), 403

        query = request.args.get('q', '').strip()
        limit = min(int(request.args.get('limit', 20)), 100)

        if not query or len(query) < 2:
            return jsonify({'success': False, 'error': 'Минимум 2 символа для поиска'}), 400

        seller = current_user.seller

        if not seller.wb_api_key:
            return jsonify({'success': False, 'error': 'WB API ключ не настроен'}), 400

        try:
            from wb_api_client import WildberriesAPIClient

            with WildberriesAPIClient(seller.wb_api_key) as wb_client:
                result = wb_client.search_brands(query, top=limit)
                brands = result.get('data', [])

                return jsonify({
                    'success': True,
                    'brands': brands,
                    'count': len(brands),
                    'query': query
                })

        except Exception as e:
            logger.error(f"Brand search error: {e}")
            return jsonify({'success': False, 'error': str(e)}), 500

    @app.route('/auto-import/match-brand', methods=['POST'])
    @login_required
    def auto_import_match_brand():
        """
        Матчинг бренда через кэш с fuzzy matching.

        Использует локальный кэш брендов WB для быстрого поиска.
        Возвращает статус: exact, confident, uncertain, not_found.

        Request:
            {"brand": "Lovetoys"}

        Response:
            {
                "success": true,
                "status": "confident",
                "match": {"id": 123, "name": "Lovetoy"},
                "confidence": 0.85,
                "suggestions": [...]
            }
        """
        if not current_user.seller:
            return jsonify({'success': False, 'error': 'Seller not found'}), 403

        data = request.get_json() or {}
        brand_name = data.get('brand', '').strip()

        if not brand_name:
            return jsonify({'success': False, 'error': 'brand required'}), 400

        seller = current_user.seller

        if not seller.wb_api_key:
            return jsonify({'success': False, 'error': 'WB API ключ не настроен'}), 400

        try:
            from brand_cache import get_brand_cache
            from wb_api_client import WildberriesAPIClient

            cache = get_brand_cache()

            # Синхронизируем кэш если пустой
            if not cache.brands:
                # Запускаем синхронизацию в фоне
                cache.sync_async(seller.wb_api_key)

                # Пока кэш пустой - используем прямой поиск API
                with WildberriesAPIClient(seller.wb_api_key) as wb_client:
                    result = wb_client.validate_brand(brand_name)
                    return jsonify({
                        'success': True,
                        'status': 'exact' if result.get('valid') else 'uncertain',
                        'match': result.get('exact_match'),
                        'confidence': 1.0 if result.get('valid') else 0.5,
                        'suggestions': result.get('suggestions', []),
                        'cache_syncing': True
                    })

            # Матчим через кэш
            result = cache.match_brand(brand_name)

            return jsonify({
                'success': True,
                'status': result['status'],
                'match': result['match'],
                'confidence': result['confidence'],
                'suggestions': result['suggestions'],
                'cache_stats': cache.get_stats()
            })

        except Exception as e:
            logger.error(f"Brand match error: {e}")
            return jsonify({'success': False, 'error': str(e)}), 500

    @app.route('/auto-import/detect-brand-ai', methods=['POST'])
    @login_required
    def auto_import_detect_brand_ai():
        """
        Определение бренда с помощью AI.

        Использует AI для определения бренда из названия, описания и характеристик.
        После определения пытается сматчить с WB брендами.

        Request:
            {
                "product_id": 123,
                "title": "...",
                "description": "...",
                "characteristics": {...}
            }

        Response:
            {
                "success": true,
                "detected_brand": "Lovetoy",
                "confidence": 0.9,
                "wb_match": {"id": 123, "name": "Lovetoy"},
                "reasoning": "..."
            }
        """
        if not current_user.seller:
            return jsonify({'success': False, 'error': 'Seller not found'}), 403

        data = request.get_json() or {}
        product_id = data.get('product_id')
        title = data.get('title', '')
        description = data.get('description', '')
        characteristics = data.get('characteristics', {})
        category = data.get('category', '')

        if not title:
            return jsonify({'success': False, 'error': 'title required'}), 400

        seller = current_user.seller
        settings = get_or_create_auto_import_settings(seller.id)

        if not settings.ai_api_key:
            return jsonify({'success': False, 'error': 'AI API ключ не настроен'}), 400

        try:
            from ai_service import create_ai_client, BrandDetectionTask, TaskPriority
            from brand_cache import get_brand_cache

            # Создаем AI клиент и выполняем задачу
            client = create_ai_client(
                provider=settings.ai_provider,
                api_key=settings.ai_api_key,
                model=settings.ai_model
            )

            task = BrandDetectionTask(priority=TaskPriority.HIGH)
            result, error = client.execute_task(
                task,
                title=title,
                description=description,
                characteristics=characteristics,
                category=category
            )

            if error:
                return jsonify({'success': False, 'error': error}), 500

            if not result:
                return jsonify({'success': False, 'error': 'AI не смог определить бренд'}), 500

            detected_brand = result.get('brand', '') or result.get('brand_normalized', '')
            confidence = result.get('confidence', 0.5)
            reasoning = result.get('reasoning', '')

            # Пытаемся сматчить с WB брендами
            wb_match = None
            if detected_brand:
                cache = get_brand_cache()
                if cache.brands:
                    match_result = cache.match_brand(detected_brand)
                    if match_result['status'] in ('exact', 'confident'):
                        wb_match = match_result['match']

            # Если нет матча через кэш - пробуем API
            if not wb_match and detected_brand and seller.wb_api_key:
                try:
                    from wb_api_client import WildberriesAPIClient
                    with WildberriesAPIClient(seller.wb_api_key) as wb_client:
                        api_result = wb_client.validate_brand(detected_brand)
                        if api_result.get('valid'):
                            wb_match = api_result.get('exact_match')
                        elif api_result.get('suggestions'):
                            # Берем первое предложение если уверенность AI высокая
                            if confidence >= 0.7:
                                wb_match = api_result['suggestions'][0]
                except Exception as e:
                    logger.warning(f"WB API brand validation failed: {e}")

            # Сохраняем результат если указан product_id
            if product_id and wb_match:
                try:
                    product = AutoImportProduct.query.filter_by(
                        id=product_id,
                        seller_id=seller.id
                    ).first()
                    if product:
                        product.brand = wb_match['name']
                        db.session.commit()
                        logger.info(f"Updated product {product_id} brand to: {wb_match['name']}")
                except Exception as e:
                    logger.warning(f"Failed to update product brand: {e}")

            return jsonify({
                'success': True,
                'detected_brand': detected_brand,
                'confidence': confidence,
                'reasoning': reasoning,
                'wb_match': wb_match,
                'alternative_names': result.get('alternative_names', []),
                'brand_type': result.get('brand_type', 'unknown')
            })

        except Exception as e:
            logger.error(f"AI brand detection error: {e}")
            return jsonify({'success': False, 'error': str(e)}), 500

    @app.route('/auto-import/sync-brands', methods=['POST'])
    @login_required
    def auto_import_sync_brands():
        """
        Запустить синхронизацию кэша брендов.

        Response:
            {
                "success": true,
                "status": "started" | "already_syncing",
                "stats": {...}
            }
        """
        if not current_user.seller:
            return jsonify({'success': False, 'error': 'Seller not found'}), 403

        seller = current_user.seller

        if not seller.wb_api_key:
            return jsonify({'success': False, 'error': 'WB API ключ не настроен'}), 400

        try:
            from brand_cache import get_brand_cache

            cache = get_brand_cache()

            if cache.is_syncing:
                return jsonify({
                    'success': True,
                    'status': 'already_syncing',
                    'stats': cache.get_stats()
                })

            cache.sync_async(seller.wb_api_key)

            return jsonify({
                'success': True,
                'status': 'started',
                'stats': cache.get_stats()
            })

        except Exception as e:
            logger.error(f"Brand sync error: {e}")
            return jsonify({'success': False, 'error': str(e)}), 500

    @app.route('/auto-import/brands-stats', methods=['GET'])
    @login_required
    def auto_import_brands_stats():
        """
        Получить статистику кэша брендов.

        Response:
            {
                "success": true,
                "stats": {
                    "brands_count": 1234,
                    "last_sync": 1234567890,
                    "is_syncing": false
                }
            }
        """
        try:
            from brand_cache import get_brand_cache

            cache = get_brand_cache()
            return jsonify({
                'success': True,
                'stats': cache.get_stats()
            })

        except Exception as e:
            logger.error(f"Brand stats error: {e}")
            return jsonify({'success': False, 'error': str(e)}), 500


# Пример использования:
# from auto_import_routes import register_auto_import_routes
# register_auto_import_routes(app)
