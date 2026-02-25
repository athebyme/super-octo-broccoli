# -*- coding: utf-8 -*-
"""
Маршруты обогащения WB-карточек данными от поставщика.
"""
import json
import logging

from flask import render_template, request, redirect, url_for, flash, jsonify, abort
from flask_login import login_required, current_user

from models import db, Product, ImportedProduct, EnrichmentJob
from supplier_enrichment import get_enrichment_service

logger = logging.getLogger(__name__)


def register_enrichment_routes(app):
    """Регистрирует маршруты обогащения в Flask-приложении"""

    # =========================================================================
    # ОДНА КАРТОЧКА
    # =========================================================================

    @app.route('/products/<int:product_id>/enrich', methods=['GET'])
    @login_required
    def product_enrich(product_id):
        """Страница сравнения и применения данных поставщика"""
        if not current_user.seller:
            flash('У вас нет профиля продавца', 'danger')
            return redirect(url_for('dashboard'))

        product = Product.query.get_or_404(product_id)
        if product.seller_id != current_user.seller.id:
            abort(403)

        service = get_enrichment_service()
        imp = service.find_supplier_data(product, current_user.seller.id)

        if not imp:
            flash(
                'Данные поставщика для этой карточки не найдены. '
                'Убедитесь, что товар был импортирован из поставщика.',
                'warning'
            )
            return redirect(url_for('product_detail', product_id=product_id))

        preview = service.build_preview(product, imp)

        return render_template(
            'product_enrich.html',
            product=product,
            imported_product=imp,
            preview=preview,
        )

    @app.route('/api/products/<int:product_id>/enrich/preview', methods=['POST'])
    @login_required
    def api_product_enrich_preview(product_id):
        """JSON превью diff между WB-карточкой и данными поставщика"""
        if not current_user.seller:
            return jsonify({'error': 'No seller profile'}), 403

        product = Product.query.get_or_404(product_id)
        if product.seller_id != current_user.seller.id:
            return jsonify({'error': 'Access denied'}), 403

        service = get_enrichment_service()
        imp = service.find_supplier_data(product, current_user.seller.id)

        if not imp:
            return jsonify({'error': 'Supplier data not found', 'matched': False}), 404

        preview = service.build_preview(product, imp)
        return jsonify({'matched': True, 'preview': preview})

    @app.route('/api/products/<int:product_id>/enrich/apply', methods=['POST'])
    @login_required
    def api_product_enrich_apply(product_id):
        """Применить выбранные поля из данных поставщика к WB-карточке"""
        if not current_user.seller:
            return jsonify({'error': 'No seller profile'}), 403

        if not current_user.seller.has_valid_api_key():
            return jsonify({'error': 'WB API key not configured'}), 400

        product = Product.query.get_or_404(product_id)
        if product.seller_id != current_user.seller.id:
            return jsonify({'error': 'Access denied'}), 403

        data = request.get_json(silent=True) or {}
        fields = data.get('fields', [])
        photo_strategy = data.get('photo_strategy', 'replace')
        supplier_id = data.get('supplier_id')

        if not fields:
            return jsonify({'error': 'No fields selected'}), 400

        # Валидация допустимых полей
        allowed_fields = {'title', 'brand', 'description', 'characteristics', 'dimensions', 'photos'}
        fields = [f for f in fields if f in allowed_fields]
        if not fields:
            return jsonify({'error': 'No valid fields selected'}), 400

        service = get_enrichment_service()

        # Находим ImportedProduct (можно переопределить через supplier_id)
        if supplier_id:
            imp = ImportedProduct.query.filter_by(
                id=supplier_id, seller_id=current_user.seller.id
            ).first()
        else:
            imp = service.find_supplier_data(product, current_user.seller.id)

        if not imp:
            return jsonify({'error': 'Supplier data not found'}), 404

        from wb_api_client import WildberriesAPIClient
        try:
            wb_client = WildberriesAPIClient(current_user.seller.get_wb_api_key())
        except Exception as e:
            return jsonify({'error': f'WB client error: {e}'}), 500

        try:
            result = service.apply_enrichment(
                product, imp, fields, photo_strategy,
                current_user.seller, wb_client
            )
            return jsonify(result)
        except Exception as e:
            logger.error(f"[Enrich] apply_enrichment error for product {product_id}: {e}", exc_info=True)
            return jsonify({'success': False, 'error': str(e)}), 500

    @app.route('/api/products/<int:product_id>/supplier-photos', methods=['GET'])
    @login_required
    def api_product_supplier_photos(product_id):
        """Список фото поставщика для данной карточки"""
        if not current_user.seller:
            return jsonify({'error': 'No seller profile'}), 403

        product = Product.query.get_or_404(product_id)
        if product.seller_id != current_user.seller.id:
            return jsonify({'error': 'Access denied'}), 403

        service = get_enrichment_service()
        imp = service.find_supplier_data(product, current_user.seller.id)

        if not imp:
            return jsonify({'photos': [], 'matched': False})

        photos = service._get_supplier_photo_list(imp)
        return jsonify({'photos': photos, 'matched': True, 'supplier_id': imp.id})

    # =========================================================================
    # МАССОВОЕ ОБОГАЩЕНИЕ
    # =========================================================================

    @app.route('/products/enrich-bulk', methods=['POST'])
    @login_required
    def products_enrich_bulk():
        """
        Запуск страницы массового обогащения.
        POST из списка товаров: selected_ids + redirect на страницу настройки.
        """
        if not current_user.seller:
            flash('У вас нет профиля продавца', 'danger')
            return redirect(url_for('dashboard'))

        selected_ids = request.form.getlist('selected_ids')
        if not selected_ids:
            flash('Не выбрано ни одной карточки', 'warning')
            return redirect(url_for('products_list'))

        # Валидируем что ids принадлежат этому продавцу
        seller_id = current_user.seller.id
        product_ids = []
        for pid in selected_ids:
            try:
                pid_int = int(pid)
                p = Product.query.filter_by(id=pid_int, seller_id=seller_id).first()
                if p:
                    product_ids.append(pid_int)
            except (ValueError, TypeError):
                pass

        if not product_ids:
            flash('Выбранные карточки не найдены', 'warning')
            return redirect(url_for('products_list'))

        return render_template(
            'products_enrich_bulk.html',
            product_ids=product_ids,
            product_ids_json=json.dumps(product_ids),
            count=len(product_ids),
        )

    @app.route('/api/products/enrich-bulk/start', methods=['POST'])
    @login_required
    def api_enrich_bulk_start():
        """Запуск фоновой задачи массового обогащения"""
        if not current_user.seller:
            return jsonify({'error': 'No seller profile'}), 403

        if not current_user.seller.has_valid_api_key():
            return jsonify({'error': 'WB API key not configured'}), 400

        data = request.get_json(silent=True) or {}
        product_ids = data.get('product_ids', [])
        fields = data.get('fields', [])
        photo_strategy = data.get('photo_strategy', 'replace')

        if not product_ids or not fields:
            return jsonify({'error': 'product_ids and fields are required'}), 400

        # Валидируем поля
        allowed_fields = {'title', 'brand', 'description', 'characteristics', 'dimensions', 'photos'}
        fields = [f for f in fields if f in allowed_fields]
        if not fields:
            return jsonify({'error': 'No valid fields'}), 400

        # Проверяем принадлежность товаров продавцу
        seller_id = current_user.seller.id
        valid_ids = [
            pid for pid in product_ids
            if Product.query.filter_by(id=pid, seller_id=seller_id).first()
        ]

        if not valid_ids:
            return jsonify({'error': 'No valid products found'}), 400

        from wb_api_client import WildberriesAPIClient
        try:
            wb_client = WildberriesAPIClient(current_user.seller.get_wb_api_key())
        except Exception as e:
            return jsonify({'error': f'WB client error: {e}'}), 500

        service = get_enrichment_service()
        job_id = service.start_bulk_enrichment(
            valid_ids, fields, photo_strategy,
            current_user.seller, wb_client
        )

        return jsonify({'job_id': job_id, 'total': len(valid_ids)})

    @app.route('/api/products/enrich-bulk/<job_id>/status', methods=['GET'])
    @login_required
    def api_enrich_bulk_status(job_id):
        """Прогресс фоновой задачи обогащения"""
        if not current_user.seller:
            return jsonify({'error': 'No seller profile'}), 403

        job = EnrichmentJob.query.get_or_404(job_id)
        if job.seller_id != current_user.seller.id:
            return jsonify({'error': 'Access denied'}), 403

        results = []
        if job.results:
            try:
                results = json.loads(job.results)
            except (json.JSONDecodeError, TypeError):
                pass

        return jsonify({
            'job_id': job.id,
            'status': job.status,
            'total': job.total,
            'processed': job.processed,
            'succeeded': job.succeeded,
            'failed': job.failed,
            'skipped': job.skipped,
            'progress_pct': round(job.processed / job.total * 100) if job.total else 0,
            'results': results[-50:],  # Последние 50 для отображения в таблице
        })
