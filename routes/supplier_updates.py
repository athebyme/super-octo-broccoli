# -*- coding: utf-8 -*-
"""Раздел «Обновление карточек»: карточки продавца по поставщикам + дозагрузка фото на WB.

Страница показывает связанные с поставщиками карточки, дельту «фото на WB vs
у поставщика» и запускает фоновую дозагрузку фото (media/save) по выбранным.
"""
import logging
import threading

from flask import render_template, request, jsonify, current_app
from flask_login import login_required, current_user

from models import db, Product, BackgroundJob
from services.supplier_update_hub import (
    JOB_TYPE, MAX_SUPPLIER_UPDATE_PRODUCTS, VERIFY_JOB_TYPE,
    SupplierUpdateJobAlreadyActive, create_supplier_update_job,
    query_update_rows, get_supplier_chips, expand_filter_to_ids,
    run_photos_job, run_verify_job,
)

logger = logging.getLogger(__name__)

ACTIVE_STATUSES = ('pending', 'running')


def _strict_product_ids(raw_ids):
    if not isinstance(raw_ids, list) or not raw_ids:
        raise ValueError('product_ids должен быть непустым списком')
    if len(raw_ids) > MAX_SUPPLIER_UPDATE_PRODUCTS:
        raise ValueError(
            f'За один запуск можно выбрать не более '
            f'{MAX_SUPPLIER_UPDATE_PRODUCTS} карточек'
        )
    if any(
        not isinstance(value, int)
        or isinstance(value, bool)
        or value <= 0
        for value in raw_ids
    ):
        raise ValueError('product_ids должны быть positive integer')
    if len(set(raw_ids)) != len(raw_ids):
        raise ValueError('product_ids не должны повторяться')
    return list(raw_ids)


def _filtered_product_ids(body, seller_id):
    """Resolve one bounded exact seller-owned target set from JSON."""
    if body.get('select_all') is True:
        supplier_id = body.get('supplier_id')
        if supplier_id is not None and (
            not isinstance(supplier_id, int)
            or isinstance(supplier_id, bool)
            or supplier_id <= 0
        ):
            raise ValueError('supplier_id должен быть positive integer')
        only_new = body.get('only_new', True)
        if not isinstance(only_new, bool):
            raise ValueError('only_new должен быть boolean')
        search = body.get('search', '')
        if not isinstance(search, str) or len(search) > 200:
            raise ValueError('search должен быть строкой до 200 символов')
        product_ids = expand_filter_to_ids(
            seller_id,
            supplier_id=supplier_id,
            only_new=only_new,
            search=search.strip(),
            limit=MAX_SUPPLIER_UPDATE_PRODUCTS + 1,
        )
        if len(product_ids) > MAX_SUPPLIER_UPDATE_PRODUCTS:
            raise ValueError(
                f'По фильтру найдено больше '
                f'{MAX_SUPPLIER_UPDATE_PRODUCTS} карточек; сузьте фильтр'
            )
        return product_ids
    if body.get('select_all') not in (None, False):
        raise ValueError('select_all должен быть boolean')

    requested = _strict_product_ids(body.get('product_ids'))
    owned = {
        product.id
        for product in Product.query.filter(
            Product.id.in_(requested),
            Product.seller_id == seller_id,
        ).all()
    }
    if owned != set(requested):
        raise ValueError(
            'Часть выбранных карточек недоступна; обновите список'
        )
    return requested


def _current_seller_or_none():
    return current_user.seller if getattr(current_user, 'seller', None) else None


def _parse_filters(args):
    supplier_id = args.get('supplier_id', type=int)
    only_new = args.get('only_new', '1').strip() not in ('0', 'false', 'False', '')
    search = (args.get('search') or '').strip()
    return supplier_id, only_new, search


def register_supplier_updates_routes(app):

    @app.route('/supplier-updates')
    @login_required
    def supplier_updates_page():
        seller = _current_seller_or_none()
        if not seller:
            from flask import flash, redirect, url_for
            flash('У вас нет профиля продавца', 'danger')
            return redirect(url_for('dashboard'))

        supplier_id, only_new, search = _parse_filters(request.args)
        page = request.args.get('page', 1, type=int)
        per_page = min(request.args.get('per_page', 50, type=int), 200)

        # Тяжёлые json_array_length-агрегаты по всем карточкам — кеш 60с;
        # инвалидируется по завершении фото-джобы (run_photos_job)
        from services.ttl_cache import cache
        chips = cache.get_or_load(
            f'supdates-chips:{seller.id}', 60,
            lambda: get_supplier_chips(seller.id))
        rows, total = query_update_rows(
            seller.id, supplier_id=supplier_id, only_new=only_new,
            search=search, page=page, per_page=per_page,
        )

        active_job = (BackgroundJob.query
                      .filter_by(seller_id=seller.id, job_type=JOB_TYPE)
                      .filter(BackgroundJob.status.in_(ACTIVE_STATUSES))
                      .order_by(BackgroundJob.id.desc())
                      .first())

        total_pages = max(1, (total + per_page - 1) // per_page)
        return render_template(
            'supplier_updates.html',
            rows=rows, total=total, chips=chips,
            supplier_id=supplier_id, only_new=only_new, search=search,
            page=page, per_page=per_page, total_pages=total_pages,
            active_job=active_job,
        )

    @app.route('/api/supplier-updates/photos/start', methods=['POST'])
    @login_required
    def supplier_updates_photos_start():
        seller = _current_seller_or_none()
        if not seller:
            return jsonify({'success': False, 'error': 'Нет профиля продавца'}), 403
        if not seller.has_valid_api_key():
            return jsonify({'success': False,
                            'error': 'Не задан API ключ Wildberries'}), 403

        body = request.get_json(silent=True) or {}

        try:
            product_ids = _filtered_product_ids(body, seller.id)
        except ValueError as exc:
            return jsonify({'success': False, 'error': str(exc)}), 400

        if not product_ids:
            return jsonify({'success': False, 'error': 'Нет карточек для обновления'}), 400

        try:
            job = create_supplier_update_job(
                seller_id=seller.id,
                job_type=JOB_TYPE,
                product_ids=product_ids,
                progress={
                    'skipped': 0,
                    'errors': [],
                    'current_product_id': None,
                    'current_item_started_at': None,
                },
            )
        except SupplierUpdateJobAlreadyActive as exc:
            payload = {
                'success': False,
                'error': 'Обновление фото уже выполняется',
            }
            if exc.job_uid:
                payload['job_uid'] = exc.job_uid
            return jsonify(payload), 409
        job_uid = job.job_uid

        flask_app = current_app._get_current_object()
        t = threading.Thread(
            target=run_photos_job,
            args=(flask_app, job_uid, seller.id, product_ids),
            daemon=True,
        )
        try:
            t.start()
        except Exception:
            # Durable pending job will be picked up by the singleton scheduler.
            logger.exception('Could not kick supplier photo job %s', job_uid)

        return jsonify({'success': True, 'job_uid': job_uid,
                        'total': len(product_ids)})

    @app.route('/api/supplier-updates/verify/start', methods=['POST'])
    @login_required
    def supplier_updates_verify_start():
        """Сверка карточек с WB: применились ли обновления, нет ли ошибок обработки."""
        seller = _current_seller_or_none()
        if not seller:
            return jsonify({'success': False, 'error': 'Нет профиля продавца'}), 403
        if not seller.has_valid_api_key():
            return jsonify({'success': False,
                            'error': 'Не задан API ключ Wildberries'}), 403

        body = request.get_json(silent=True) or {}

        try:
            product_ids = _filtered_product_ids(body, seller.id)
        except ValueError as exc:
            return jsonify({'success': False, 'error': str(exc)}), 400

        if not product_ids:
            return jsonify({'success': False, 'error': 'Нет карточек для проверки'}), 400

        try:
            job = create_supplier_update_job(
                seller_id=seller.id,
                job_type=VERIFY_JOB_TYPE,
                product_ids=product_ids,
                progress={'attempts': 0},
            )
        except SupplierUpdateJobAlreadyActive as exc:
            payload = {
                'success': False,
                'error': 'Проверка уже выполняется',
            }
            if exc.job_uid:
                payload['job_uid'] = exc.job_uid
            return jsonify(payload), 409
        job_uid = job.job_uid

        flask_app = current_app._get_current_object()
        t = threading.Thread(
            target=run_verify_job,
            args=(flask_app, job_uid, seller.id, product_ids),
            daemon=True,
        )
        try:
            t.start()
        except Exception:
            logger.exception('Could not kick supplier verify job %s', job_uid)

        return jsonify({'success': True, 'job_uid': job_uid,
                        'total': len(product_ids)})

    @app.route('/api/supplier-updates/jobs/<job_uid>/status')
    @login_required
    def supplier_updates_job_status(job_uid):
        seller = _current_seller_or_none()
        if not seller:
            return jsonify({'success': False, 'error': 'Нет профиля продавца'}), 403
        job = BackgroundJob.query.filter_by(
            job_uid=job_uid, seller_id=seller.id).first()
        if not job:
            return jsonify({'success': False, 'error': 'Задача не найдена'}), 404
        return jsonify({
            'success': True,
            'status': job.status,
            'total': job.total,
            'processed': job.processed,
            'succeeded': job.succeeded,
            'failed': job.failed_count,
            'progress': job.get_progress(),
            'result': job.get_result(),
        })

    @app.route('/api/supplier-updates/jobs/<job_uid>/cancel', methods=['POST'])
    @login_required
    def supplier_updates_job_cancel(job_uid):
        seller = _current_seller_or_none()
        if not seller:
            return jsonify({'success': False, 'error': 'Нет профиля продавца'}), 403
        job = BackgroundJob.query.filter_by(
            job_uid=job_uid, seller_id=seller.id).first()
        if not job:
            return jsonify({'success': False, 'error': 'Задача не найдена'}), 404
        if job.status in ACTIVE_STATUSES:
            job.status = 'cancelled'
            job.error_message = 'Отменено пользователем'
            db.session.commit()
            return jsonify({'success': True})
        return jsonify({'success': False,
                        'error': f'Задача уже в статусе: {job.status}'}), 400
