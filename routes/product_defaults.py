# -*- coding: utf-8 -*-
"""
Роуты для настройки дефолтных габаритов/веса и глобального медиа товаров
"""
import json
import os
import uuid
import logging
from datetime import datetime
from flask import render_template, redirect, url_for, flash, request, jsonify, send_file, current_app
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename

from models import db, ProductDefaults

logger = logging.getLogger(__name__)

ALLOWED_EXTENSIONS = {'jpg', 'jpeg', 'png', 'webp', 'mp4', 'mov', 'avi'}
MAX_FILE_SIZE = 20 * 1024 * 1024  # 20MB


def _allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def _get_media_dir(seller_id):
    """Директория для хранения глобального медиа продавца"""
    base = os.path.join(current_app.root_path, 'data', 'global_media', str(seller_id))
    os.makedirs(base, exist_ok=True)
    return base


def _file_type(filename):
    ext = filename.rsplit('.', 1)[1].lower() if '.' in filename else ''
    if ext in ('mp4', 'mov', 'avi'):
        return 'video'
    return 'photo'


def register_product_defaults_routes(app):

    @app.route('/settings/product-defaults')
    @login_required
    def product_defaults_page():
        """Страница настроек дефолтных габаритов и глобального медиа"""
        if not current_user.seller:
            flash('Необходимо быть продавцом.', 'warning')
            return redirect(url_for('dashboard'))

        seller = current_user.seller

        # Глобальное правило
        global_rule = ProductDefaults.query.filter_by(
            seller_id=seller.id, rule_type='global'
        ).first()

        # Категорийные правила
        category_rules = ProductDefaults.query.filter_by(
            seller_id=seller.id, rule_type='category'
        ).order_by(ProductDefaults.wb_category_name).all()

        # Получаем список доступных WB категорий для автокомплита
        from models import MarketplaceCategory
        wb_categories = MarketplaceCategory.query.filter_by(
            is_enabled=True
        ).order_by(MarketplaceCategory.subject_name).all()

        # Глобальные дефолтные характеристики
        global_chars = global_rule.get_default_characteristics() if global_rule else {}

        # Категорийные характеристики
        category_chars = {}
        for rule in category_rules:
            category_chars[rule.id] = rule.get_default_characteristics()

        return render_template('product_defaults.html',
                               global_rule=global_rule,
                               category_rules=category_rules,
                               wb_categories=wb_categories,
                               global_chars=global_chars,
                               category_chars=category_chars)

    @app.route('/settings/product-defaults/save-global', methods=['POST'])
    @login_required
    def product_defaults_save_global():
        """Сохранить глобальные дефолты"""
        if not current_user.seller:
            return jsonify({'success': False, 'error': 'Forbidden'}), 403

        seller = current_user.seller

        rule = ProductDefaults.query.filter_by(
            seller_id=seller.id, rule_type='global'
        ).first()

        if not rule:
            rule = ProductDefaults(seller_id=seller.id, rule_type='global', wb_subject_id=None)
            db.session.add(rule)

        try:
            rule.length_cm = float(request.form.get('length_cm') or 0) or None
            rule.width_cm = float(request.form.get('width_cm') or 0) or None
            rule.height_cm = float(request.form.get('height_cm') or 0) or None
            rule.weight_kg = float(request.form.get('weight_kg') or 0) or None
        except (ValueError, TypeError):
            return jsonify({'success': False, 'error': 'Некорректные значения'}), 400

        db.session.commit()
        flash('Глобальные дефолты сохранены', 'success')
        return jsonify({'success': True})

    @app.route('/settings/product-defaults/save-category', methods=['POST'])
    @login_required
    def product_defaults_save_category():
        """Сохранить/обновить категорийное правило"""
        if not current_user.seller:
            return jsonify({'success': False, 'error': 'Forbidden'}), 403

        seller = current_user.seller

        rule_id = request.form.get('rule_id')
        wb_subject_id = request.form.get('wb_subject_id')
        wb_category_name = request.form.get('wb_category_name', '').strip()

        if not wb_subject_id:
            return jsonify({'success': False, 'error': 'Выберите категорию'}), 400

        try:
            wb_subject_id = int(wb_subject_id)
        except ValueError:
            return jsonify({'success': False, 'error': 'Некорректный ID категории'}), 400

        if rule_id:
            rule = ProductDefaults.query.filter_by(id=int(rule_id), seller_id=seller.id).first()
            if not rule:
                return jsonify({'success': False, 'error': 'Правило не найдено'}), 404
        else:
            # Проверяем уникальность
            existing = ProductDefaults.query.filter_by(
                seller_id=seller.id, rule_type='category', wb_subject_id=wb_subject_id
            ).first()
            if existing:
                rule = existing
            else:
                rule = ProductDefaults(
                    seller_id=seller.id, rule_type='category',
                    wb_subject_id=wb_subject_id, wb_category_name=wb_category_name,
                    priority=10
                )
                db.session.add(rule)

        rule.wb_category_name = wb_category_name
        rule.wb_subject_id = wb_subject_id

        try:
            rule.length_cm = float(request.form.get('length_cm') or 0) or None
            rule.width_cm = float(request.form.get('width_cm') or 0) or None
            rule.height_cm = float(request.form.get('height_cm') or 0) or None
            rule.weight_kg = float(request.form.get('weight_kg') or 0) or None
        except (ValueError, TypeError):
            return jsonify({'success': False, 'error': 'Некорректные значения'}), 400

        db.session.commit()
        flash(f'Правило для "{wb_category_name}" сохранено', 'success')
        return jsonify({'success': True, 'rule_id': rule.id})

    @app.route('/settings/product-defaults/delete-category/<int:rule_id>', methods=['POST'])
    @login_required
    def product_defaults_delete_category(rule_id):
        """Удалить категорийное правило"""
        if not current_user.seller:
            return jsonify({'success': False, 'error': 'Forbidden'}), 403

        rule = ProductDefaults.query.filter_by(
            id=rule_id, seller_id=current_user.seller.id, rule_type='category'
        ).first()
        if not rule:
            return jsonify({'success': False, 'error': 'Правило не найдено'}), 404

        db.session.delete(rule)
        db.session.commit()
        flash('Правило удалено', 'success')
        return jsonify({'success': True})

    # ==================== ДЕФОЛТНЫЕ ХАРАКТЕРИСТИКИ ====================

    @app.route('/settings/product-defaults/save-characteristics', methods=['POST'])
    @login_required
    def product_defaults_save_characteristics():
        """Сохранить дефолтные характеристики (глобальные)"""
        if not current_user.seller:
            return jsonify({'success': False, 'error': 'Forbidden'}), 403

        seller = current_user.seller

        rule = ProductDefaults.query.filter_by(
            seller_id=seller.id, rule_type='global'
        ).first()

        if not rule:
            rule = ProductDefaults(seller_id=seller.id, rule_type='global', wb_subject_id=None)
            db.session.add(rule)

        try:
            data = request.get_json()
            if not data:
                return jsonify({'success': False, 'error': 'Нет данных'}), 400

            chars = data.get('characteristics', {})
            # Очищаем пустые значения
            clean_chars = {}
            for key, val in chars.items():
                key = key.strip()
                if isinstance(val, str):
                    val = val.strip()
                if key and val:
                    clean_chars[key] = val
            rule.set_default_characteristics(clean_chars)
            db.session.commit()
            return jsonify({'success': True, 'count': len(clean_chars)})
        except Exception as e:
            logger.error(f"Ошибка сохранения характеристик: {e}")
            db.session.rollback()
            return jsonify({'success': False, 'error': str(e)}), 500

    @app.route('/settings/product-defaults/save-category-characteristics', methods=['POST'])
    @login_required
    def product_defaults_save_category_characteristics():
        """Сохранить дефолтные характеристики для категории"""
        if not current_user.seller:
            return jsonify({'success': False, 'error': 'Forbidden'}), 403

        seller = current_user.seller

        try:
            data = request.get_json()
            if not data:
                return jsonify({'success': False, 'error': 'Нет данных'}), 400

            rule_id = data.get('rule_id')
            if not rule_id:
                return jsonify({'success': False, 'error': 'rule_id обязателен'}), 400

            rule = ProductDefaults.query.filter_by(
                id=int(rule_id), seller_id=seller.id, rule_type='category'
            ).first()
            if not rule:
                return jsonify({'success': False, 'error': 'Правило не найдено'}), 404

            chars = data.get('characteristics', {})
            clean_chars = {}
            for key, val in chars.items():
                key = key.strip()
                if isinstance(val, str):
                    val = val.strip()
                if key and val:
                    clean_chars[key] = val
            rule.set_default_characteristics(clean_chars)
            db.session.commit()
            return jsonify({'success': True, 'count': len(clean_chars)})
        except Exception as e:
            logger.error(f"Ошибка сохранения характеристик категории: {e}")
            db.session.rollback()
            return jsonify({'success': False, 'error': str(e)}), 500

    # ==================== ГЛОБАЛЬНОЕ МЕДИА ====================

    @app.route('/settings/product-defaults/upload-media', methods=['POST'])
    @login_required
    def product_defaults_upload_media():
        """Загрузить глобальное медиа (фото/видео)"""
        if not current_user.seller:
            return jsonify({'success': False, 'error': 'Forbidden'}), 403

        seller = current_user.seller

        if 'file' not in request.files:
            return jsonify({'success': False, 'error': 'Файл не выбран'}), 400

        file = request.files['file']
        if not file.filename or not _allowed_file(file.filename):
            return jsonify({'success': False, 'error': 'Недопустимый формат файла'}), 400

        # Проверяем размер
        file.seek(0, 2)
        size = file.tell()
        file.seek(0)
        if size > MAX_FILE_SIZE:
            return jsonify({'success': False, 'error': 'Файл слишком большой (макс. 20 МБ)'}), 400

        # Сохраняем файл
        media_dir = _get_media_dir(seller.id)
        ext = file.filename.rsplit('.', 1)[1].lower()
        unique_name = f"{uuid.uuid4().hex[:12]}.{ext}"
        filepath = os.path.join(media_dir, unique_name)
        file.save(filepath)

        # Получаем или создаём глобальное правило
        rule = ProductDefaults.query.filter_by(
            seller_id=seller.id, rule_type='global'
        ).first()
        if not rule:
            rule = ProductDefaults(seller_id=seller.id, rule_type='global', wb_subject_id=None)
            db.session.add(rule)
            db.session.flush()

        # Добавляем файл в список медиа
        media_list = rule.get_global_media_list()
        media_list.append({
            'filename': unique_name,
            'original_name': secure_filename(file.filename),
            'type': _file_type(file.filename),
            'size': size,
            'uploaded_at': datetime.utcnow().isoformat()
        })
        rule.global_media = json.dumps(media_list, ensure_ascii=False)
        db.session.commit()

        logger.info(f"Seller {seller.id}: uploaded global media {unique_name} ({_file_type(file.filename)})")

        return jsonify({
            'success': True,
            'file': {
                'filename': unique_name,
                'original_name': secure_filename(file.filename),
                'type': _file_type(file.filename),
                'size': size
            }
        })

    @app.route('/settings/product-defaults/delete-media/<filename>', methods=['POST'])
    @login_required
    def product_defaults_delete_media(filename):
        """Удалить глобальное медиа"""
        if not current_user.seller:
            return jsonify({'success': False, 'error': 'Forbidden'}), 403

        seller = current_user.seller

        rule = ProductDefaults.query.filter_by(
            seller_id=seller.id, rule_type='global'
        ).first()
        if not rule:
            return jsonify({'success': False, 'error': 'Not found'}), 404

        media_list = rule.get_global_media_list()
        media_list = [m for m in media_list if m.get('filename') != filename]
        rule.global_media = json.dumps(media_list, ensure_ascii=False)
        db.session.commit()

        # Удаляем файл с диска (защита от path traversal)
        safe_name = secure_filename(filename)
        if not safe_name:
            return jsonify({'success': False, 'error': 'Invalid filename'}), 400
        media_dir = _get_media_dir(seller.id)
        filepath = os.path.join(media_dir, safe_name)
        # Проверяем что путь не выходит за пределы media_dir
        if not os.path.abspath(filepath).startswith(os.path.abspath(media_dir)):
            return jsonify({'success': False, 'error': 'Invalid filename'}), 400
        if os.path.exists(filepath):
            os.remove(filepath)

        return jsonify({'success': True})

    @app.route('/settings/product-defaults/media/<filename>')
    @login_required
    def product_defaults_serve_media(filename):
        """Отдать файл глобального медиа"""
        if not current_user.seller:
            return '', 403

        # Безопасность: только имя файла без путей
        safe_name = secure_filename(filename)
        filepath = os.path.join(_get_media_dir(current_user.seller.id), safe_name)
        if not os.path.exists(filepath):
            return '', 404
        return send_file(filepath)

    # ==================== API: получить дефолты для товара ====================

    @app.route('/api/product-defaults/for-product/<int:product_id>')
    @login_required
    def api_product_defaults_for_product(product_id):
        """Получить применимые дефолты для конкретного товара"""
        if not current_user.seller:
            return jsonify({'success': False}), 403

        from models import ImportedProduct
        product = ImportedProduct.query.filter_by(
            id=product_id, seller_id=current_user.seller.id
        ).first()
        if not product:
            return jsonify({'success': False, 'error': 'Not found'}), 404

        defaults = get_defaults_for_product(current_user.seller.id, product.wb_subject_id)
        return jsonify({'success': True, 'defaults': defaults})


def get_defaults_for_product(seller_id, wb_subject_id=None):
    """
    Получить дефолтные значения для товара.
    Приоритет: категорийное правило > глобальное правило > захардкоженные дефолты.

    Returns:
        dict с ключами: length, width, height, weightBrutto, global_media, default_characteristics
    """
    result = {
        'length': 10,
        'width': 10,
        'height': 5,
        'weightBrutto': 0.1
    }
    global_media = []
    default_chars = {}

    # 1. Глобальное правило
    global_rule = ProductDefaults.query.filter_by(
        seller_id=seller_id, rule_type='global', is_active=True
    ).first()

    if global_rule:
        dims = global_rule.get_dimensions_dict()
        result.update(dims)
        global_media = global_rule.get_global_media_list()
        default_chars = global_rule.get_default_characteristics()

    # 2. Категорийное правило (перезаписывает глобальные)
    if wb_subject_id:
        cat_rule = ProductDefaults.query.filter_by(
            seller_id=seller_id, rule_type='category',
            wb_subject_id=wb_subject_id, is_active=True
        ).first()
        if cat_rule:
            if cat_rule.has_dimensions():
                dims = cat_rule.get_dimensions_dict()
                result.update(dims)
            # Категорийные характеристики перезаписывают глобальные
            cat_chars = cat_rule.get_default_characteristics()
            if cat_chars:
                default_chars.update(cat_chars)

    result['global_media'] = global_media
    result['default_characteristics'] = default_chars
    return result
