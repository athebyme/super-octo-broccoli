# -*- coding: utf-8 -*-
"""
Маршруты обогащения WB-карточек данными от поставщика.
"""
import json
import logging
from flask import render_template, request, redirect, url_for, flash, jsonify, abort, Response, send_file
from flask_login import login_required, current_user

from models import Product, ImportedProduct, EnrichmentJob
from services.supplier_enrichment import (
    EnrichmentJobAlreadyActive,
    SAFE_ENRICHMENT_PHOTO_STRATEGIES,
    WbMediaOperationBusy,
    get_enrichment_service,
)

logger = logging.getLogger(__name__)
MAX_SUPPLIER_BULK_PRODUCTS = 200

# Технические коды остановки задачи → что это значит для продавца и что делать.
# Неизвестный код показывается как есть: врать про причину нельзя.
_JOB_ERROR_TEXTS = {
    'invalid_durable_job_payload': (
        'Не удалось прочитать список выбранных карточек — выберите товары '
        'заново и запустите отправку ещё раз.'
    ),
    'seller_lock_busy': (
        'Для этого магазина уже идёт другая отправка на WB. Дождитесь её '
        'завершения и повторите.'
    ),
    'worker_interrupted': (
        'Обработка прервалась при перезапуске сервиса. Уже отправленные '
        'карточки сохранены, остальные можно отправить повторно.'
    ),
    'wb_api_key_missing': (
        'Не найден ключ Wildberries. Проверьте подключение в «Настройках API».'
    ),
    'supplier_source_unavailable': (
        'Нет данных поставщика для этих карточек — обновите каталог поставщика '
        'и повторите.'
    ),
}


def _human_job_error(code):
    """Человеческая расшифровка кода остановки задачи."""
    if not code:
        return None
    text = _JOB_ERROR_TEXTS.get(str(code).strip())
    return text or str(code)


def _bounded_unique_product_ids(raw_ids, limit=MAX_SUPPLIER_BULK_PRODUCTS):
    if not isinstance(raw_ids, list) or not raw_ids:
        raise ValueError('product_ids must be a non-empty array')
    if len(raw_ids) > limit:
        raise ValueError(f'Maximum {limit} product_ids per request')
    normalized = []
    seen = set()
    for index, value in enumerate(raw_ids):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(
                f'product_ids[{index}] must be a positive integer'
            )
        if value in seen:
            raise ValueError('product_ids must not contain duplicates')
        normalized.append(value)
        seen.add(value)
    return normalized


def _parse_form_product_ids(raw_ids):
    """Strict bounded parser for the HTML bulk-selection handoff."""
    if not raw_ids:
        raise ValueError('Не выбрано ни одной карточки')
    if len(raw_ids) > MAX_SUPPLIER_BULK_PRODUCTS:
        raise ValueError(
            f'За один запуск можно выбрать не более '
            f'{MAX_SUPPLIER_BULK_PRODUCTS} карточек'
        )

    parsed = []
    for index, raw_value in enumerate(raw_ids):
        if (
            not isinstance(raw_value, str)
            or not raw_value.isascii()
            or not raw_value.isdigit()
        ):
            raise ValueError(f'Некорректный ID карточки в позиции {index + 1}')
        value = int(raw_value)
        if value <= 0:
            raise ValueError(f'Некорректный ID карточки в позиции {index + 1}')
        parsed.append(value)
    return _bounded_unique_product_ids(parsed)


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

        seller_id = current_user.seller.id
        product = Product.query.filter_by(
            id=product_id,
            seller_id=seller_id,
        ).first_or_404()

        # Пробуем найти данные поставщика автоматически
        service = get_enrichment_service()
        imp = service.find_supplier_data(product, current_user.seller.id)

        # Пользователь может явно указать supplier_id через ?supplier_id=X
        manual_supplier_id = request.args.get('supplier_id', type=int)
        if manual_supplier_id:
            manual_imp = ImportedProduct.query.filter_by(
                id=manual_supplier_id,
                seller_id=seller_id,
            ).first()
            if manual_imp:
                imp = manual_imp

        preview = service.build_preview(product, imp) if imp else None

        # Предполагаемый external_id из vendor_code для подсказки в форме
        from services.pricing_engine import extract_supplier_product_id
        suggested_ext_id = extract_supplier_product_id(product.vendor_code or '')

        # Сигнал шаблону, что данных мало
        low_data = imp and not any([
            imp.photo_urls, imp.description, imp.characteristics,
            imp.materials, imp.gender, imp.ai_seo_title, imp.ai_dimensions
        ])

        # Текущие фото WB для передачи в шаблон
        current_wb_photos = []
        if product.nm_id:
            from seller_platform import wb_photo_url
            for i in range(1, 11):
                current_wb_photos.append({
                    'index': i,
                    'url': wb_photo_url(product.nm_id, i),
                })

        return render_template(
            'product_enrich.html',
            product=product,
            imported_product=imp,
            preview=preview,
            suggested_ext_id=suggested_ext_id,
            low_data=low_data,
            current_wb_photos=current_wb_photos,
        )

    @app.route('/api/products/<int:product_id>/enrich/preview', methods=['POST'])
    @login_required
    def api_product_enrich_preview(product_id):
        """JSON превью diff между WB-карточкой и данными поставщика"""
        if not current_user.seller:
            return jsonify({'error': 'No seller profile'}), 403

        product = Product.query.filter_by(
            id=product_id,
            seller_id=current_user.seller.id,
        ).first_or_404()

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

        product = Product.query.filter_by(
            id=product_id,
            seller_id=current_user.seller.id,
        ).first_or_404()

        data = request.get_json(silent=True) or {}
        fields = data.get('fields', [])
        photo_strategy = data.get('photo_strategy', 'smart_merge')
        photo_indices = data.get('photo_indices')  # Новое: выборочные фото
        supplier_id = data.get('supplier_id')

        if not isinstance(fields, list) or not fields:
            return jsonify({'error': 'No fields selected'}), 400
        if photo_strategy not in SAFE_ENRICHMENT_PHOTO_STRATEGIES:
            return jsonify({'error': 'Invalid photo_strategy'}), 400

        allowed_fields = {
            'title', 'brand', 'description',
            'characteristics', 'dimensions', 'photos',
        }
        if (
            len(fields) > len(allowed_fields)
            or any(
                not isinstance(field, str) or field not in allowed_fields
                for field in fields
            )
            or len(set(fields)) != len(fields)
        ):
            return jsonify({'error': 'Invalid or duplicate fields'}), 400
        if supplier_id is not None and (
            not isinstance(supplier_id, int)
            or isinstance(supplier_id, bool)
            or supplier_id <= 0
        ):
            return jsonify({'error': 'supplier_id must be a positive integer'}), 400
        if photo_indices is not None:
            from services.wb_api_client import MAX_WB_MEDIA_FILES

            if 'photos' not in fields:
                return jsonify({
                    'error': 'photo_indices requires the photos field',
                }), 400
            if (
                not isinstance(photo_indices, list)
                or not 1 <= len(photo_indices) <= MAX_WB_MEDIA_FILES
                or any(
                    not isinstance(index, int)
                    or isinstance(index, bool)
                    or index < 0
                    for index in photo_indices
                )
                or len(set(photo_indices)) != len(photo_indices)
            ):
                return jsonify({
                    'error': (
                        'photo_indices must contain 1..30 unique '
                        'non-negative integers'
                    ),
                }), 400

        service = get_enrichment_service()

        # ImportedProduct всегда принадлежит текущему seller. Числовой ID без
        # tenant scope нельзя использовать ни для чтения, ни для привязки.
        if supplier_id:
            imp = ImportedProduct.query.filter_by(
                id=supplier_id,
                seller_id=current_user.seller.id,
            ).first()
        else:
            imp = service.find_supplier_data(product, current_user.seller.id)

        if not imp:
            return jsonify({'error': 'Supplier data not found'}), 404

        # Validate exact source positions before a text/content write. A bad
        # selective-photo request must not leave a partially applied content
        # side effect merely because its media input was rejected afterwards.
        selected_photo_source_urls = None
        if photo_indices is not None:
            source_photos, _source_type, _external_id = service._photo_source(
                imp
            )
            if any(index >= len(source_photos) for index in photo_indices):
                return jsonify({
                    'error': 'photo_indices contains an unavailable position',
                }), 400
            selected_photo_source_urls = [
                service._photo_url(source_photos[index])
                for index in photo_indices
            ]
            if any(not value for value in selected_photo_source_urls):
                return jsonify({
                    'error': 'photo_indices contains an invalid photo source',
                }), 400

        from services.wb_api_client import WildberriesAPIClient
        try:
            wb_client = WildberriesAPIClient(current_user.seller.wb_api_key)
        except Exception as e:
            return jsonify({'error': f'WB client error: {e}'}), 500

        try:
            # Если есть выборочные фото — используем selective стратегию
            if photo_indices is not None and 'photos' in fields:
                # Content и media — разные операции/receipts. Не передаём
                # `photos` в content-flow: при uncertain content выбранные
                # индексы нигде durable не хранятся и не должны ошибочно
                # отображаться как автоматически ожидающий photo follow-up.
                content_fields = [
                    field for field in fields if field != 'photos'
                ]
                if content_fields:
                    text_result = service.apply_enrichment(
                        product, imp, content_fields, 'smart_merge',
                        current_user.seller, wb_client
                    )
                else:
                    text_result = {
                        'success': True,
                        'fields_applied': [],
                        'error': None,
                        'wb_sync': False,
                        'reconciliation_pending': False,
                        'fields_pending': [],
                    }

                # Fail closed до отдельного photo side effect. Иначе crafted
                # request мог получить success по фото после того, как
                # характеристики были отклонены обязательным WB-словарём.
                if text_result.get('error'):
                    text_result['photos'] = {
                        'skipped': True,
                        'reason': 'content_update_not_settled',
                        'definitely_not_sent': True,
                    }
                    return jsonify(text_result), (
                        202 if text_result.get('reconciliation_pending') else 409
                    )

                # Отдельно применяем выбранные фото
                photo_result = service.apply_selective_photos(
                    product, imp, photo_indices, photo_strategy,
                    current_user.seller, wb_client,
                    expected_source_urls=selected_photo_source_urls,
                )

                # Объединяем результаты
                combined_fields = list(text_result.get('fields_applied', []))
                if photo_result.get('uploaded', 0) > 0:
                    combined_fields.append('photos')

                text_fields_requested = any(
                    field != 'photos' for field in fields
                )
                text_success = (
                    text_result.get('success', False)
                    if text_fields_requested else True
                )
                combined = {
                    'success': bool(
                        text_success and photo_result.get('success', False)
                    ),
                    'fields_applied': combined_fields,
                    'photos': photo_result,
                    'error': text_result.get('error') or photo_result.get('error'),
                    'wb_sync': bool(
                        text_result.get('wb_sync', False)
                        or int(photo_result.get('uploaded') or 0) > 0
                    ),
                    'wb_confirmed': False,
                    'deferred': bool(
                        text_result.get('deferred')
                        or photo_result.get('deferred')
                    ),
                    'reconciliation_pending': bool(
                        text_result.get('reconciliation_pending')
                        or photo_result.get('reconciliation_pending')
                    ),
                    'fields_pending': sorted(set(
                        list(text_result.get('fields_pending') or [])
                        + (
                            ['photos']
                            if photo_result.get('reconciliation_pending')
                            else []
                        )
                    )),
                }
                if combined['reconciliation_pending']:
                    status = 202
                else:
                    status = 200 if combined['success'] else 409
                return jsonify(combined), status
            else:
                result = service.apply_enrichment(
                    product, imp, fields, photo_strategy,
                    current_user.seller, wb_client
                )
                return jsonify(result), (
                    202 if result.get('reconciliation_pending') else 200
                )
        except Exception as e:
            logger.error(f"[Enrich] apply_enrichment error for product {product_id}: {e}", exc_info=True)
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            close = getattr(wb_client, 'close', None)
            if callable(close):
                close()

    @app.route('/api/products/<int:product_id>/supplier-photos', methods=['GET'])
    @login_required
    def api_product_supplier_photos(product_id):
        """Список фото поставщика для данной карточки"""
        if not current_user.seller:
            return jsonify({'error': 'No seller profile'}), 403

        product = Product.query.filter_by(
            id=product_id,
            seller_id=current_user.seller.id,
        ).first_or_404()

        service = get_enrichment_service()
        imp = service.find_supplier_data(product, current_user.seller.id)

        if not imp:
            return jsonify({'photos': [], 'matched': False})

        photos = service._get_supplier_photo_list(imp)
        return jsonify({'photos': photos, 'matched': True, 'supplier_id': imp.id})

    @app.route('/api/enrich/photo-proxy/<int:imported_product_id>/<int:photo_idx>')
    @login_required
    def api_enrich_photo_proxy(imported_product_id, photo_idx):
        """
        Прокси для фото поставщика: отдаёт из кэша или скачивает у поставщика.
        """
        import requests as _requests
        from io import BytesIO
        from PIL import Image as _Image
        from services.photo_cache import get_photo_cache

        if not current_user.seller:
            abort(403)

        imp = ImportedProduct.query.filter_by(
            id=imported_product_id,
            seller_id=current_user.seller.id,
        ).first_or_404()

        if not imp.photo_urls:
            abort(404)

        try:
            photos = json.loads(imp.photo_urls)
        except (json.JSONDecodeError, TypeError):
            abort(404)

        if photo_idx < 0 or photo_idx >= len(photos):
            abort(404)

        ph = photos[photo_idx]
        url = ph.get('sexoptovik') or ph.get('original') or ph.get('blur')
        if not url:
            abort(404)

        supplier_type = imp.source_type or 'unknown'
        external_id = imp.external_id or ''
        cache = get_photo_cache()

        # Если уже закэшировано — отдаём из кэша
        if cache.is_cached(supplier_type, external_id, url):
            cache_path = cache.get_cache_path(supplier_type, external_id, url)
            response = send_file(cache_path, mimetype='image/jpeg', conditional=True)
            response.cache_control.max_age = 86400
            response.cache_control.private = True
            return response

        # Скачиваем с поставщика
        service = get_enrichment_service()
        auth_cookies = None
        if supplier_type == 'sexoptovik':
            try:
                auth_cookies = service._get_sexoptovik_auth(current_user.seller)
            except Exception:
                pass

        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'image/*,*/*;q=0.8',
        }
        if 'sexoptovik.ru' in url:
            headers['Referer'] = 'https://sexoptovik.ru/admin/'

        fallbacks = []
        if ph.get('blur') and ph['blur'] != url:
            fallbacks.append(ph['blur'])
        if ph.get('original') and ph['original'] != url:
            fallbacks.append(ph['original'])

        for current_url in [url] + fallbacks:
            try:
                resp = _requests.get(
                    current_url, headers=headers, cookies=auth_cookies,
                    timeout=15, allow_redirects=True
                )
                resp.raise_for_status()

                content_type = resp.headers.get('Content-Type', '')
                if not content_type.startswith('image/') and len(resp.content) < 1024:
                    continue

                img = _Image.open(BytesIO(resp.content))
                output = BytesIO()
                if img.mode != 'RGB':
                    img = img.convert('RGB')
                img.save(output, format='JPEG', quality=95)
                image_bytes = output.getvalue()

                # Сохраняем в кэш
                cache.save_to_cache(supplier_type, external_id, url, image_bytes)

                response = Response(image_bytes, mimetype='image/jpeg')
                response.cache_control.max_age = 86400
                response.cache_control.private = True
                return response

            except Exception as e:
                logger.debug(f"[PhotoProxy] Failed {current_url[:60]}: {e}")
                continue

        # Возвращаем placeholder вместо 404
        return _generate_placeholder_image()

    @app.route('/api/enrich/search-supplier', methods=['GET'])
    @login_required
    def api_enrich_search_supplier():
        """
        Поиск ImportedProduct по артикулу поставщика или названию.
        GET ?q=25268   или   ?q=Массажная+свеча
        """
        if not current_user.seller:
            return jsonify({'error': 'No seller profile'}), 403

        q = (request.args.get('q') or '').strip()
        if not q:
            return jsonify({'results': []})

        from sqlalchemy import or_

        # Поиск по external_id (точное), external_vendor_code (точное) или title (ilike)
        query = ImportedProduct.query.filter(
            ImportedProduct.seller_id == current_user.seller.id,
            or_(
                ImportedProduct.external_id == q,
                ImportedProduct.external_vendor_code == q,
                ImportedProduct.title.ilike(f'%{q}%'),
            )
        ).order_by(ImportedProduct.id.desc()).limit(20)

        results = []
        for imp in query.all():
            has_data = any([
                imp.photo_urls, imp.description, imp.characteristics,
                imp.materials, imp.gender, imp.ai_seo_title, imp.ai_dimensions
            ])
            photo_count = 0
            if imp.photo_urls:
                try:
                    photo_count = len(json.loads(imp.photo_urls))
                except Exception:
                    pass
            results.append({
                'id': imp.id,
                'external_id': imp.external_id,
                'title': imp.title or '—',
                'brand': imp.brand or '',
                'source_type': imp.source_type or '',
                'photo_count': photo_count,
                'has_data': has_data,
                'already_linked': imp.product_id is not None,
                'import_status': imp.import_status,
            })

        return jsonify({'results': results})

    @app.route('/api/products/<int:product_id>/enrich/debug', methods=['GET'])
    @login_required
    def api_product_enrich_debug(product_id):
        """Debug: показывает детали матчинга для диагностики"""
        if not current_user.seller:
            return jsonify({'error': 'No seller profile'}), 403

        product = Product.query.filter_by(
            id=product_id,
            seller_id=current_user.seller.id,
        ).first_or_404()

        from services.pricing_engine import extract_supplier_product_id
        import re as _re

        seller_id = current_user.seller.id
        vendor_code = product.vendor_code or ''

        pid_num = extract_supplier_product_id(vendor_code)
        candidates = []
        if pid_num:
            candidates.extend([str(pid_num), f'id-{pid_num}'])
        vc_match = _re.match(r'^(id-\w+)-', vendor_code)
        if vc_match:
            candidates.append(vc_match.group(1))
        candidates = list(dict.fromkeys(candidates))

        db_results = {}
        for c in candidates:
            imp = ImportedProduct.query.filter_by(external_id=c, seller_id=seller_id).first()
            db_results[c] = {'found': imp is not None, 'imp_id': imp.id if imp else None}

        recent = ImportedProduct.query.filter_by(seller_id=seller_id).order_by(
            ImportedProduct.id.desc()
        ).limit(5).all()

        fk_imp = ImportedProduct.query.filter_by(
            product_id=product.id,
            seller_id=seller_id,
        ).first()

        return jsonify({
            'product_id': product.id,
            'nm_id': product.nm_id,
            'vendor_code': vendor_code,
            'supplier_vendor_code': product.supplier_vendor_code,
            'seller_id': seller_id,
            'numeric_pid_extracted': pid_num,
            'candidate_external_ids': candidates,
            'db_search_results': db_results,
            'fk_match': {'found': fk_imp is not None, 'imp_id': fk_imp.id if fk_imp else None},
            'recent_imported_external_ids': [
                {'id': imp.id, 'external_id': imp.external_id, 'product_id': imp.product_id}
                for imp in recent
            ],
        })

    # =========================================================================
    # МАССОВОЕ ОБОГАЩЕНИЕ
    # =========================================================================

    @app.route('/products/enrich-bulk', methods=['POST'])
    @login_required
    def products_enrich_bulk():
        """Страница подтверждения массового обновления карточек."""
        if not current_user.seller:
            flash('У вас нет профиля продавца', 'danger')
            return redirect(url_for('dashboard'))

        seller_id = current_user.seller.id
        try:
            requested_ids = _parse_form_product_ids(
                request.form.getlist('selected_ids')
            )
        except ValueError as exc:
            flash(str(exc), 'warning')
            return redirect(url_for('products_list'))

        owned_ids = {
            product.id
            for product in Product.query.filter(
                Product.seller_id == seller_id,
                Product.id.in_(requested_ids),
            ).all()
        }
        if owned_ids != set(requested_ids):
            flash(
                'Часть выбранных карточек недоступна. '
                'Обновите список и повторите выбор.',
                'warning',
            )
            return redirect(url_for('products_list'))

        # Сохраняем порядок выбора и одним bounded preflight определяем,
        # для скольких карточек доступны данные/фото поставщика.
        product_ids = list(requested_ids)
        service = get_enrichment_service()
        availability = service.check_enrichment_availability(
            product_ids, seller_id
        )
        matched_count = sum(
            1 for product_id in product_ids
            if availability.get(product_id, {}).get('available')
        )
        photo_ready_count = sum(
            1 for product_id in product_ids
            if availability.get(product_id, {}).get('photo_count', 0) > 0
        )

        # Явный preset из CTA «Отправить характеристики»; default экрана
        # (только фото) не меняется
        preset = request.form.get('preset', '').strip()
        if preset not in ('characteristics',):
            preset = ''

        return render_template(
            'products_enrich_bulk.html',
            product_ids=product_ids,
            product_ids_json=json.dumps(product_ids),
            count=len(product_ids),
            matched_count=matched_count,
            photo_ready_count=photo_ready_count,
            preset=preset,
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
        raw_product_ids = data.get('product_ids', [])
        fields = data.get('fields', [])
        photo_strategy = data.get('photo_strategy', 'smart_merge')

        if not raw_product_ids or not fields:
            return jsonify({'error': 'product_ids and fields are required'}), 400

        try:
            product_ids = _bounded_unique_product_ids(raw_product_ids)
        except ValueError as exc:
            return jsonify({'error': str(exc)}), 400

        allowed_fields = {
            'title', 'brand', 'description',
            'characteristics', 'dimensions', 'photos',
        }
        if not isinstance(fields, list):
            return jsonify({'error': 'fields must be an array'}), 400
        if not fields or any(
            not isinstance(field, str) or field not in allowed_fields
            for field in fields
        ):
            return jsonify({'error': 'fields contains an unsupported value'}), 400
        if len(set(fields)) != len(fields):
            return jsonify({'error': 'fields must not contain duplicates'}), 400
        if photo_strategy not in SAFE_ENRICHMENT_PHOTO_STRATEGIES:
            return jsonify({'error': 'Invalid photo_strategy'}), 400

        seller_id = current_user.seller.id
        available_ids = {
            product.id
            for product in Product.query.filter(
                Product.seller_id == seller_id,
                Product.id.in_(product_ids),
            ).all()
        }
        if available_ids != set(product_ids):
            return jsonify({
                'error': 'Some selected products are unavailable; refresh the list and try again'
            }), 409

        active_job = (
            EnrichmentJob.query
            .filter(
                EnrichmentJob.seller_id == seller_id,
                EnrichmentJob.status.in_(('pending', 'running')),
            )
            .order_by(EnrichmentJob.created_at.desc())
            .first()
        )
        if active_job:
            return jsonify({
                'error': 'Массовое обновление уже выполняется',
                'job_id': active_job.id,
            }), 409

        service = get_enrichment_service()
        try:
            job_id = service.start_bulk_enrichment(
                product_ids, fields, photo_strategy,
                current_user.seller,
            )
        except EnrichmentJobAlreadyActive as exc:
            payload = {'error': str(exc)}
            if exc.job_id:
                payload['job_id'] = exc.job_id
            return jsonify(payload), 409

        return jsonify({'job_id': job_id, 'total': len(product_ids)})

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
            'confirmed': job.confirmed or 0,
            'conflicted': job.conflicted or 0,
            'reconciliation_pending': max(
                0,
                int(job.succeeded or 0)
                - int(job.confirmed or 0)
                - int(job.conflicted or 0),
            ),
            'last_error': job.last_error,
            'last_error_text': _human_job_error(job.last_error),
            'progress_pct': round(job.processed / job.total * 100) if job.total else 0,
            # Запуск ограничен 200 карточками, поэтому весь bounded отчёт можно
            # вернуть сразу: пользователь видит результат каждой выбранной строки.
            'results': results[:MAX_SUPPLIER_BULK_PRODUCTS],
        })

    # =========================================================================
    # ПРОВЕРКА ДОСТУПНОСТИ ОБОГАЩЕНИЯ (ДЛЯ СПИСКА КАРТОЧЕК)
    # =========================================================================

    @app.route('/api/products/enrich-availability', methods=['POST'])
    @login_required
    def api_enrich_availability():
        """
        Проверка наличия данных поставщика для списка карточек.
        POST: {"product_ids": [1, 2, 3]}
        Возвращает: {"results": {product_id: {available, photo_count, ...}}}
        """
        if not current_user.seller:
            return jsonify({'error': 'No seller profile'}), 403

        data = request.get_json(silent=True) or {}
        product_ids = data.get('product_ids', [])

        if not product_ids:
            return jsonify({'results': {}})

        # Ограничиваем количество для производительности
        product_ids = product_ids[:200]

        # Фильтруем только карточки текущего продавца
        seller_id = current_user.seller.id
        valid_products = Product.query.filter(
            Product.id.in_(product_ids),
            Product.seller_id == seller_id
        ).with_entities(Product.id).all()
        valid_ids = [p.id for p in valid_products]

        service = get_enrichment_service()
        results = service.check_enrichment_availability(valid_ids, seller_id)

        return jsonify({'results': {str(k): v for k, v in results.items()}})

    @app.route('/api/products/<int:product_id>/enrich/photo-compare', methods=['GET'])
    @login_required
    def api_product_photo_compare(product_id):
        """
        Полное сравнение фото WB-карточки и поставщика с деталями.
        Возвращает обе галереи для side-by-side UI.
        """
        if not current_user.seller:
            return jsonify({'error': 'No seller profile'}), 403

        product = Product.query.filter_by(
            id=product_id,
            seller_id=current_user.seller.id,
        ).first_or_404()

        service = get_enrichment_service()
        imp = service.find_supplier_data(product, current_user.seller.id)

        # Текущие фото WB
        from seller_platform import wb_photo_url
        wb_photos = []
        if product.nm_id:
            for i in range(1, 11):
                wb_photos.append({
                    'index': i,
                    'url': wb_photo_url(product.nm_id, i),
                })

        # Фото поставщика
        supplier_photos = []
        supplier_meta = None
        if imp:
            supplier_photos = service._get_supplier_photo_list(imp)
            supplier_meta = {
                'id': imp.id,
                'external_id': imp.external_id,
                'source_type': imp.source_type,
                'title': imp.title,
            }

        return jsonify({
            'wb_photos': wb_photos,
            'wb_photo_count': len(
                service._safe_json_loads(product.photos_json, []) or []
            ),
            'supplier_photos': [
                {
                    'index': i,
                    'proxy_url': f'/api/enrich/photo-proxy/{imp.id}/{i}' if imp else None,
                    'cached': p.get('cached', False),
                    'has_original': p.get('has_original', False),
                }
                for i, p in enumerate(supplier_photos)
            ],
            'supplier_photo_count': len(supplier_photos),
            'supplier_meta': supplier_meta,
            'matched': imp is not None,
        })

    # =========================================================================
    # УПРАВЛЕНИЕ ФОТО КАРТОЧКИ
    # =========================================================================

    @app.route('/api/products/<int:product_id>/photos/reorder', methods=['POST'])
    @login_required
    def api_product_photos_reorder(product_id):
        """Изменить порядок фото карточки"""
        if not current_user.seller:
            return jsonify({'error': 'No seller profile'}), 403

        product = Product.query.filter_by(
            id=product_id,
            seller_id=current_user.seller.id,
        ).first_or_404()

        data = request.get_json(silent=True) or {}
        new_order = data.get('order', [])  # Список индексов в новом порядке

        if not new_order:
            return jsonify({'error': 'No order specified'}), 400

        return jsonify({'success': True, 'message': 'Photo order updated'})

    @app.route('/api/products/<int:product_id>/photos/add-supplier', methods=['POST'])
    @login_required
    def api_product_photos_add_supplier(product_id):
        """Добавить выбранные фото поставщика к карточке WB"""
        if not current_user.seller:
            return jsonify({'error': 'No seller profile'}), 403

        if not current_user.seller.has_valid_api_key():
            return jsonify({'error': 'WB API key not configured'}), 400

        product = Product.query.filter_by(
            id=product_id,
            seller_id=current_user.seller.id,
        ).first_or_404()

        data = request.get_json(silent=True) or {}
        supplier_id = data.get('supplier_id')
        photo_indices = data.get('photo_indices', [])
        strategy = data.get('strategy', 'smart_merge')

        if (
            not isinstance(supplier_id, int)
            or isinstance(supplier_id, bool)
            or supplier_id <= 0
            or not isinstance(photo_indices, list)
            or not photo_indices
        ):
            return jsonify({'error': 'supplier_id and photo_indices required'}), 400
        if strategy not in SAFE_ENRICHMENT_PHOTO_STRATEGIES:
            return jsonify({'error': 'Invalid photo strategy'}), 400

        imp = ImportedProduct.query.filter_by(
            id=supplier_id,
            seller_id=current_user.seller.id,
        ).first()
        if not imp:
            return jsonify({'error': 'Supplier product not found'}), 404

        from services.wb_api_client import WildberriesAPIClient
        wb_client = None
        try:
            wb_client = WildberriesAPIClient(current_user.seller.wb_api_key)
            service = get_enrichment_service()
            result = service.apply_selective_photos(
                product,
                imp,
                photo_indices,
                strategy,
                current_user.seller,
                wb_client,
            )
            if result.get('reconciliation_pending') or result.get('deferred'):
                status = 202
            else:
                status = 200 if result.get('success') else 400
            return jsonify(result), status
        except WbMediaOperationBusy as e:
            return jsonify({'success': False, 'error': str(e)}), 409
        except Exception as e:
            logger.error(f"[Enrich] Photo upload error: {e}")
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            close = getattr(wb_client, 'close', None)
            if callable(close):
                close()


def _generate_placeholder_image():
    """Генерирует placeholder изображение (серый квадрат с иконкой)"""
    from PIL import Image as _Image, ImageDraw as _ImageDraw
    from io import BytesIO

    img = _Image.new('RGB', (200, 200), '#f3f4f6')
    draw = _ImageDraw.Draw(img)
    # Рисуем простой крестик
    draw.line([(80, 80), (120, 120)], fill='#d1d5db', width=2)
    draw.line([(120, 80), (80, 120)], fill='#d1d5db', width=2)
    draw.rectangle([(60, 60), (140, 140)], outline='#d1d5db', width=1)

    output = BytesIO()
    img.save(output, format='JPEG', quality=80)
    output.seek(0)

    response = Response(output.getvalue(), mimetype='image/jpeg')
    response.cache_control.max_age = 300
    response.cache_control.private = True
    return response
