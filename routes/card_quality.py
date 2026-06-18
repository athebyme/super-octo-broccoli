# -*- coding: utf-8 -*-
"""Роуты фичи «Качество карточек»: кокпит, деталь карточки, AI-анализ, обновление."""
import json
import logging
import threading
from datetime import datetime as _dt

from flask import render_template, request, redirect, url_for, flash, jsonify, current_app
from flask_login import login_required, current_user

from models import db, Product, CardRatingHistory, AgentTask, BulkEditHistory, CardEditHistory
from models import get_standard_media, get_min_photos
from services.card_quality_scorer import card_quality_detail, compute_quality_summary
from services import agent_service
from services.wb_api_client import WildberriesAPIClient
from services.card_improver import (ALLOWED_FIELDS, apply_card_updates,
                                     collect_weak_dimensions, build_proposal_from_tasks)
from services.supplier_enrichment import get_enrichment_service
from services.standard_photos import compose_card_photo_urls

logger = logging.getLogger('card_quality')

BULK_IMPROVE_LIMIT = 30
STANDARD_PHOTOS_BULK_LIMIT = 30


def get_sparse_photo_candidates(seller, db_session, limit: int = STANDARD_PHOTOS_BULK_LIMIT):
    """Возвращает (candidates, total_M) для «Дополнить фото» слабым карточкам.

    candidates — list[(Product, composed_urls)] длиной <= limit, отсортированный
                 по количеству собственных фото (asc, меньше фото → выше).
    total_M    — полное число sparse-карточек с непустой композицией (до обрезки limit).

    Карточка считается «sparse», если len(own_photos) < get_min_photos(seller.id).
    В список попадает только если compose_card_photo_urls(...) вернул непустой список.
    """
    min_photos = get_min_photos(seller.id)

    # Загружаем все активные карточки продавца
    active_products = Product.query.filter_by(
        seller_id=seller.id, is_active=True
    ).all()

    all_candidates = []  # (photo_count, product, composed_urls)
    for product in active_products:
        try:
            own = json.loads(product.photos_json) if product.photos_json else []
        except (ValueError, TypeError):
            own = []

        # Пропускаем не-sparse карточки
        if len(own) >= min_photos:
            continue

        # Получаем стандартные медиа для категории
        media = get_standard_media(seller.id, product.subject_id)

        # Пробуем скомпоновать
        composed = compose_card_photo_urls(own, media, seller.id, min_photos)
        if not composed:
            continue  # нечего добавлять

        all_candidates.append((len(own), product, composed))

    # Сортируем по количеству своих фото (меньше → в начало)
    all_candidates.sort(key=lambda t: t[0])

    total_m = len(all_candidates)
    top_n = all_candidates[:limit]

    candidates = [(product, composed) for (_, product, composed) in top_n]
    return candidates, total_m


def _collect_bulk_candidates(seller_id: int, limit: int = BULK_IMPROVE_LIMIT) -> dict:
    """Собирает top-N слабых карточек продавца с весами и (если есть) диффом поставщика."""
    base = Product.query.filter(
        Product.seller_id == seller_id, Product.is_active == True
    ).filter(
        (Product.quality_score < 50) | (Product.nm_rating < 6)
    )

    total_weak = base.count()
    rows = base.order_by(Product.quality_score.asc()).limit(limit).all()

    es = get_enrichment_service()
    candidates = []
    for p in rows:
        detail = card_quality_detail(p)
        weak_dims = collect_weak_dimensions(detail)
        supplier_diff = None
        imp = es.find_supplier_data(p, seller_id)
        if imp:
            supplier_diff = es.build_preview(p, imp)
        candidates.append({
            'product_id': detail['product_id'],
            'nm_id': detail['nm_id'],
            'vendor_code': detail.get('vendor_code'),
            'title': detail.get('title'),
            'quality_score': detail.get('quality_score'),
            'weak_dims': weak_dims,
            'has_supplier': bool(supplier_diff),
            'supplier_diff': supplier_diff,
        })
    return {'candidates': candidates, 'total_weak': total_weak, 'shown': len(candidates)}


def register_card_quality_routes(app):
    """Регистрация роутов качества карточек."""

    @app.route('/card-quality')
    @login_required
    def card_quality_page():
        if not current_user.seller or not current_user.seller.has_valid_api_key():
            flash('Для оценки качества карточек необходимо настроить API ключ WB', 'warning')
            return redirect(url_for('api_settings'))
        return render_template('card_quality.html')

    @app.route('/api/card-quality/list')
    @login_required
    def api_card_quality_list():
        if not current_user.seller or not current_user.seller.has_valid_api_key():
            return jsonify({'error': 'API ключ WB не настроен'}), 403
        try:
            sort = request.args.get('sort', 'quality_score')
            order = request.args.get('order', 'asc')
            page = request.args.get('page', 1, type=int)
            per_page = min(request.args.get('per_page', 50, type=int), 200)

            q = Product.query.filter_by(seller_id=current_user.seller.id, is_active=True)
            col = {'quality_score': Product.quality_score, 'nm_rating': Product.nm_rating,
                   'wb_feedback_rating': Product.wb_feedback_rating}.get(sort, Product.quality_score)
            q = q.order_by(col.asc() if order == 'asc' else col.desc())
            pagination = q.paginate(page=page, per_page=per_page, error_out=False)
            items = [card_quality_detail(p) for p in pagination.items]

            summary = compute_quality_summary(current_user.seller.id)
            summary['total'] = pagination.total
            return jsonify({'success': True, 'items': items, 'summary': summary,
                            'page': page, 'pages': pagination.pages})
        except Exception as e:
            logger.exception('Ошибка в api_card_quality_list: %s', e)
            return jsonify({'error': 'Внутренняя ошибка'}), 500

    @app.route('/api/card-quality/summary')
    @login_required
    def api_card_quality_summary():
        if not current_user.seller:
            return jsonify({'error': 'Нет профиля продавца'}), 403
        try:
            data = compute_quality_summary(current_user.seller.id)
            return jsonify({'success': True, 'data': data})
        except Exception as e:
            logger.exception('Ошибка в api_card_quality_summary: %s', e)
            return jsonify({'error': 'Внутренняя ошибка'}), 500

    @app.route('/api/card-quality/<int:product_id>')
    @login_required
    def api_card_quality_detail(product_id):
        if not current_user.seller or not current_user.seller.has_valid_api_key():
            return jsonify({'error': 'API ключ WB не настроен'}), 403
        product = Product.query.filter_by(id=product_id, seller_id=current_user.seller.id).first()
        if not product:
            return jsonify({'error': 'Карточка не найдена'}), 404
        try:
            detail = card_quality_detail(product)
            trend = CardRatingHistory.query.filter_by(product_id=product.id)\
                .order_by(CardRatingHistory.captured_at.asc()).limit(90).all()
            detail['trend'] = [{
                'captured_at': h.captured_at.isoformat() if h.captured_at else None,
                'wb_product_rating': h.wb_product_rating,
                'quality_score': h.quality_score,
            } for h in trend]
            return jsonify({'success': True, 'data': detail})
        except Exception as e:
            logger.exception('Ошибка в api_card_quality_detail: %s', e)
            return jsonify({'error': 'Внутренняя ошибка'}), 500

    @app.route('/api/card-quality/<int:product_id>/ai-analyze', methods=['POST'])
    @login_required
    def api_card_quality_ai_analyze(product_id):
        if not current_user.seller:
            return jsonify({'error': 'Нет профиля продавца'}), 403
        product = Product.query.filter_by(id=product_id, seller_id=current_user.seller.id).first()
        if not product:
            return jsonify({'error': 'Карточка не найдена'}), 404
        try:
            task_ids = {}
            for agent_name, task_type in (('card-doctor', 'diagnose_single'),
                                          ('photo-optimizer', 'optimize_single')):
                agent = agent_service.get_agent_by_name(agent_name)
                if not agent or getattr(agent, 'status', None) != 'online':
                    continue
                task = agent_service.create_task(
                    agent_id=agent.id,
                    seller_id=current_user.seller.id,
                    task_type=task_type,
                    title=f'AI-анализ карточки {product.nm_id}',
                    input_data={'product_id': product.id},
                )
                task_ids[agent_name] = task.id

            if not task_ids:
                return jsonify({'error': 'AI-агенты сейчас офлайн'}), 409
            return jsonify({'success': True, 'task_ids': task_ids})
        except Exception as e:
            logger.exception('Ошибка в api_card_quality_ai_analyze: %s', e)
            return jsonify({'error': 'Внутренняя ошибка'}), 500

    @app.route('/api/card-quality/refresh', methods=['POST'])
    @login_required
    def api_card_quality_refresh():
        if not current_user.seller or not current_user.seller.has_valid_api_key():
            return jsonify({'error': 'API ключ WB не настроен'}), 403
        from services.product_sync_scheduler import sync_card_ratings_for_seller
        app_obj = current_app._get_current_object()
        seller_id = current_user.seller.id
        threading.Thread(target=sync_card_ratings_for_seller, args=(app_obj, seller_id), daemon=True).start()
        return jsonify({'success': True, 'message': 'Обновление рейтингов запущено'})

    @app.route('/api/card-quality/<int:product_id>/improve', methods=['POST'])
    @login_required
    def api_card_quality_improve(product_id):
        if not current_user.seller or not current_user.seller.has_valid_api_key():
            return jsonify({'error': 'API ключ WB не настроен'}), 403
        product = Product.query.filter_by(id=product_id, seller_id=current_user.seller.id).first()
        if not product:
            return jsonify({'error': 'Карточка не найдена'}), 404
        try:
            detail = card_quality_detail(product)
            weak_dims = collect_weak_dimensions(detail)

            # (a) данные поставщика → готовый дифф
            supplier_diff = None
            es = get_enrichment_service()
            imp = es.find_supplier_data(product, current_user.seller.id)
            if imp:
                supplier_diff = es.build_preview(product, imp)

            # (b)/(c) диагностические агенты по product_id (если online)
            task_ids = {}
            for agent_name, task_type in (('photo-optimizer', 'optimize_single'),
                                          ('card-doctor', 'diagnose_single')):
                agent = agent_service.get_agent_by_name(agent_name)
                if not agent or getattr(agent, 'status', None) != 'online':
                    continue
                task = agent_service.create_task(
                    agent_id=agent.id,
                    seller_id=current_user.seller.id,
                    task_type=task_type,
                    title=f'Улучшение карточки {product.nm_id}',
                    input_data={'product_id': product.id, 'seller_id': current_user.seller.id},
                )
                task_ids[agent_name] = task.id

            # (d) генеративные агенты в режиме propose для слабых измерений
            _gen_input = {'product_id': product.id, 'seller_id': current_user.seller.id, 'mode': 'propose'}

            # seo-writer: запускается один раз при наличии слабого title ИЛИ description
            if 'title' in weak_dims or 'description' in weak_dims:
                agent = agent_service.get_agent_by_name('seo-writer')
                if agent and getattr(agent, 'status', None) == 'online':
                    task = agent_service.create_task(
                        agent_id=agent.id,
                        seller_id=current_user.seller.id,
                        task_type='seo_single',
                        title=f'SEO-текст для карточки {product.nm_id}',
                        input_data=_gen_input,
                    )
                    task_ids['seo-writer'] = task.id

            # brand-resolver: при слабом brand
            if 'brand' in weak_dims:
                agent = agent_service.get_agent_by_name('brand-resolver')
                if agent and getattr(agent, 'status', None) == 'online':
                    task = agent_service.create_task(
                        agent_id=agent.id,
                        seller_id=current_user.seller.id,
                        task_type='resolve_single',
                        title=f'Уточнение бренда для карточки {product.nm_id}',
                        input_data=_gen_input,
                    )
                    task_ids['brand-resolver'] = task.id

            # category-mapper: при слабой category
            if 'category' in weak_dims:
                agent = agent_service.get_agent_by_name('category-mapper')
                if agent and getattr(agent, 'status', None) == 'online':
                    task = agent_service.create_task(
                        agent_id=agent.id,
                        seller_id=current_user.seller.id,
                        task_type='map_single',
                        title=f'Маппинг категории для карточки {product.nm_id}',
                        input_data=_gen_input,
                    )
                    task_ids['category-mapper'] = task.id

            # characteristics-filler: при слабых characteristics
            if 'characteristics' in weak_dims:
                agent = agent_service.get_agent_by_name('characteristics-filler')
                if agent and getattr(agent, 'status', None) == 'online':
                    task = agent_service.create_task(
                        agent_id=agent.id,
                        seller_id=current_user.seller.id,
                        task_type='fill_single',
                        title=f'Заполнение характеристик для карточки {product.nm_id}',
                        input_data=_gen_input,
                    )
                    task_ids['characteristics-filler'] = task.id

            return jsonify({'success': True, 'weak_dims': weak_dims,
                            'supplier_diff': supplier_diff, 'task_ids': task_ids})
        except Exception as e:
            logger.exception('Ошибка в api_card_quality_improve: %s', e)
            return jsonify({'error': 'Внутренняя ошибка'}), 500

    @app.route('/api/card-quality/<int:product_id>/proposal', methods=['POST'])
    @login_required
    def api_card_quality_proposal(product_id):
        if not current_user.seller or not current_user.seller.has_valid_api_key():
            return jsonify({'error': 'API ключ WB не настроен'}), 403
        product = Product.query.filter_by(id=product_id, seller_id=current_user.seller.id).first()
        if not product:
            return jsonify({'error': 'Карточка не найдена'}), 404
        try:
            body = request.get_json(silent=True) or {}
            task_ids = body.get('task_ids') or {}

            task_results = []
            for agent_name, task_id in task_ids.items():
                task = AgentTask.query.filter_by(id=task_id, seller_id=current_user.seller.id).first()
                if task and task.status == 'completed':
                    task_results.append({'agent': agent_name, 'result': task.get_result()})

            proposal = build_proposal_from_tasks(product, task_results)

            # Предложение стандартных фото: compose собственных URL + глобальное медиа продавца
            try:
                own = json.loads(product.photos_json) if product.photos_json else []
            except (ValueError, TypeError):
                own = []
            try:
                media = get_standard_media(current_user.seller.id, product.subject_id)
                composed = compose_card_photo_urls(own, media, current_user.seller.id,
                                                   get_min_photos(current_user.seller.id))
                if composed:
                    # Стандартные фото приоритетнее реордера агента (photo-optimizer):
                    # они реально добавляют фото и поднимают измерение photos,
                    # тогда как реордер агента не меняет их количество. Намеренно
                    # перезаписываем proposal['photos'], если он был построен из задач агента.
                    proposal['photos'] = {
                        'current': own,
                        'proposed': composed,
                        'dimension': 'photos',
                        'source': 'standard-photos',
                    }
            except Exception as _e:
                logger.warning('standard-photos proposal skipped: %s', _e)

            supplier_diff = None
            es = get_enrichment_service()
            imp = es.find_supplier_data(product, current_user.seller.id)
            if imp:
                supplier_diff = es.build_preview(product, imp)

            return jsonify({'success': True, 'proposal': proposal, 'supplier_diff': supplier_diff})
        except Exception as e:
            logger.exception('Ошибка в api_card_quality_proposal: %s', e)
            return jsonify({'error': 'Внутренняя ошибка'}), 500

    @app.route('/api/card-quality/<int:product_id>/apply', methods=['POST'])
    @login_required
    def api_card_quality_apply(product_id):
        if not current_user.seller or not current_user.seller.has_valid_api_key():
            return jsonify({'error': 'API ключ WB не настроен'}), 403
        product = Product.query.filter_by(id=product_id, seller_id=current_user.seller.id).first()
        if not product:
            return jsonify({'error': 'Карточка не найдена'}), 404

        body = request.get_json(silent=True) or {}
        raw_updates = body.get('updates') or {}
        updates = {k: v for k, v in raw_updates.items() if k in ALLOWED_FIELDS}
        if not updates:
            return jsonify({'error': 'Нет допустимых полей для применения'}), 400

        try:
            wb_client = WildberriesAPIClient(current_user.seller.wb_api_key)
            res = apply_card_updates(product, updates, current_user.seller, wb_client,
                                     source='card-quality')
            status = 200 if res.get('success') else 422
            return jsonify({
                'success': res.get('success', False),
                'fields_applied': res.get('fields_applied', []),
                'old_quality': res.get('old_quality'),
                'new_quality': res.get('new_quality'),
                'wb_sync': res.get('wb_sync', False),
                'error': res.get('error'),
            }), status
        except Exception as e:
            logger.exception('Ошибка в api_card_quality_apply: %s', e)
            return jsonify({'error': 'Внутренняя ошибка'}), 500

    @app.route('/card-quality/bulk-improve', methods=['GET'])
    @login_required
    def card_quality_bulk_improve_page():
        if not current_user.seller or not current_user.seller.has_valid_api_key():
            flash('Для массового улучшения необходимо настроить API ключ WB', 'warning')
            return redirect(url_for('api_settings'))
        data = _collect_bulk_candidates(current_user.seller.id, BULK_IMPROVE_LIMIT)
        return render_template('card_quality_bulk_confirm.html',
                               candidates=data['candidates'],
                               total_weak=data['total_weak'],
                               shown=data['shown'],
                               limit=BULK_IMPROVE_LIMIT)

    @app.route('/card-quality/bulk-improve', methods=['POST'])
    @login_required
    def card_quality_bulk_improve_apply():
        if not current_user.seller or not current_user.seller.has_valid_api_key():
            return jsonify({'error': 'API ключ WB не настроен'}), 403
        action = request.form.get('action')
        if action == 'reject':
            flash('Массовое улучшение отклонено', 'info')
            return redirect(url_for('card_quality_page'))

        # Карта product_id -> список выбранных полей
        selections = {}
        for key in request.form:
            if key.startswith('apply_'):
                # apply_<pid>_<field>
                parts = key.split('_', 2)
                if len(parts) == 3:
                    _, pid, field = parts
                    try:
                        selections.setdefault(int(pid), []).append(field)
                    except ValueError:
                        pass

        if not selections:
            flash('Не выбрано ни одного изменения', 'warning')
            return redirect(url_for('card_quality_bulk_improve_page'))

        bulk = BulkEditHistory(
            seller_id=current_user.seller.id,
            operation_type='card_quality_bulk_improve',
            operation_params={'product_ids': list(selections.keys())},
            description=f'Массовое улучшение {len(selections)} карточек',
            total_products=len(selections),
            status='in_progress',
        )
        db.session.add(bulk)
        db.session.commit()

        wb_client = WildberriesAPIClient(current_user.seller.wb_api_key)
        es = get_enrichment_service()
        success, errors = 0, 0
        for pid, fields in selections.items():
            product = Product.query.filter_by(id=pid, seller_id=current_user.seller.id).first()
            if not product:
                errors += 1
                continue
            updates = {}
            imp = es.find_supplier_data(product, current_user.seller.id)
            if imp:
                preview = es.build_preview(product, imp)
                if 'title' in fields and preview.get('title', {}).get('supplier'):
                    updates['title'] = preview['title']['supplier']
                if 'brand' in fields and preview.get('brand', {}).get('supplier'):
                    updates['brand'] = preview['brand']['supplier']
                if 'description' in fields and preview.get('description', {}).get('supplier'):
                    updates['description'] = preview['description']['supplier']
                if 'dimensions' in fields and preview.get('dimensions', {}).get('supplier'):
                    updates['dimensions'] = preview['dimensions']['supplier']
            if not updates:
                continue
            try:
                res = apply_card_updates(product, updates, current_user.seller, wb_client,
                                         source='card-quality-bulk')
                if res.get('success'):
                    success += 1
                else:
                    errors += 1
            except Exception as e:
                logger.exception('bulk improve pid=%s: %s', pid, e)
                errors += 1

        bulk.success_count = success
        bulk.error_count = errors
        bulk.status = 'completed'
        bulk.wb_synced = success > 0
        bulk.completed_at = _dt.utcnow()
        db.session.commit()

        flash(f'Улучшено карточек: {success}, ошибок: {errors}', 'success' if success else 'warning')
        return redirect(url_for('card_quality_page'))

    @app.route('/card-quality/standard-photos-bulk', methods=['GET'])
    @login_required
    def card_quality_standard_photos_bulk_page():
        if not current_user.seller or not current_user.seller.has_valid_api_key():
            flash('Для дополнения фото необходимо настроить API ключ WB', 'warning')
            return redirect(url_for('api_settings'))
        candidates, total_m = get_sparse_photo_candidates(
            current_user.seller, db.session, limit=STANDARD_PHOTOS_BULK_LIMIT
        )
        # Подготовим данные для шаблона: добавим удобные поля
        min_photos = get_min_photos(current_user.seller.id)
        candidate_list = []
        for product, composed in candidates:
            try:
                own = json.loads(product.photos_json) if product.photos_json else []
            except (ValueError, TypeError):
                own = []
            candidate_list.append({
                'product_id': product.id,
                'nm_id': product.nm_id,
                'vendor_code': product.vendor_code,
                'title': product.title,
                'quality_score': product.quality_score,
                'own_photo_count': len(own),
                'min_photos': min_photos,
                'composed_count': len(composed),
                'composed_preview': composed[:3],  # первые 3 URL для превью
            })
        return render_template(
            'card_quality_standard_photos_bulk.html',
            candidates=candidate_list,
            total_m=total_m,
            shown=len(candidate_list),
            limit=STANDARD_PHOTOS_BULK_LIMIT,
        )

    @app.route('/card-quality/standard-photos-bulk/apply', methods=['POST'])
    @login_required
    def card_quality_standard_photos_bulk_apply():
        if not current_user.seller or not current_user.seller.has_valid_api_key():
            return jsonify({'error': 'API ключ WB не настроен'}), 403
        action = request.form.get('action')
        if action == 'reject':
            flash('Дополнение фото отклонено', 'info')
            return redirect(url_for('card_quality_page'))

        # Получаем выбранные product_id из чекбоксов
        selected_ids = []
        for key in request.form:
            if key.startswith('product_'):
                try:
                    pid = int(key[len('product_'):])
                    selected_ids.append(pid)
                except ValueError:
                    pass

        if not selected_ids:
            flash('Не выбрано ни одной карточки', 'warning')
            return redirect(url_for('card_quality_standard_photos_bulk_page'))

        # Полное число sparse-карточек (M) приходит из формы; падаем на N, если нет
        try:
            total_m = int(request.form.get('total_m', len(selected_ids)))
        except (ValueError, TypeError):
            total_m = len(selected_ids)

        bulk = BulkEditHistory(
            seller_id=current_user.seller.id,
            operation_type='standard_photos_bulk',
            operation_params={'product_ids': selected_ids},
            description=f'Дополнение стандартных фото: {len(selected_ids)} карточек',
            total_products=len(selected_ids),
            status='in_progress',
        )
        db.session.add(bulk)
        db.session.commit()

        wb_client = WildberriesAPIClient(current_user.seller.wb_api_key)
        min_photos = get_min_photos(current_user.seller.id)
        success, errors = 0, 0

        for pid in selected_ids:
            product = Product.query.filter_by(id=pid, seller_id=current_user.seller.id).first()
            if not product:
                errors += 1
                continue
            try:
                own = json.loads(product.photos_json) if product.photos_json else []
            except (ValueError, TypeError):
                own = []
            media = get_standard_media(current_user.seller.id, product.subject_id)
            composed = compose_card_photo_urls(own, media, current_user.seller.id, min_photos)
            if not composed:
                errors += 1
                continue
            try:
                res = apply_card_updates(
                    product, {'photos': composed}, current_user.seller, wb_client,
                    source='standard-photos-bulk'
                )
                if res.get('success'):
                    success += 1
                else:
                    errors += 1
            except Exception as e:
                logger.exception('standard-photos-bulk pid=%s: %s', pid, e)
                errors += 1

        bulk.success_count = success
        bulk.error_count = errors
        bulk.status = 'completed'
        bulk.wb_synced = success > 0
        bulk.completed_at = _dt.utcnow()
        db.session.commit()

        flash(
            f'Дополнено фото: {success}, ошибок: {errors} '
            f'(выбрано {len(selected_ids)} из {total_m} слабых)',
            'success' if success else 'warning'
        )
        return redirect(url_for('card_quality_page'))

    @app.route('/api/card-quality/<int:product_id>/history')
    @login_required
    def api_card_quality_history(product_id):
        if not current_user.seller:
            return jsonify({'success': False, 'error': 'Нет продавца'}), 403
        product = Product.query.filter_by(id=product_id, seller_id=current_user.seller.id).first()
        if not product:
            return jsonify({'success': False, 'error': 'Карточка не найдена'}), 404
        try:
            rows = CardEditHistory.query.filter_by(
                product_id=product.id,
                seller_id=current_user.seller.id
            ).order_by(CardEditHistory.created_at.desc()).limit(50).all()
            items = [
                {
                    'created_at': row.created_at.isoformat(),
                    'action': row.action,
                    'changed_fields': row.changed_fields or [],
                    'wb_synced': row.wb_synced,
                    'wb_sync_status': row.wb_sync_status,
                    'user_comment': row.user_comment,
                    'changes': {
                        field: {k: v for k, v in fld.items() if k not in ('before_raw', 'after_raw')}
                        for field, fld in (row.get_changes_summary() or {}).items()
                    },
                }
                for row in rows
            ]
            return jsonify({'success': True, 'items': items})
        except Exception as e:
            current_app.logger.error('api_card_quality_history error: %s', e, exc_info=True)
            return jsonify({'success': False, 'error': 'Ошибка сервера при загрузке истории'}), 500
