# -*- coding: utf-8 -*-
"""Раздел «Обновление карточек»: карточки продавца по поставщикам + дозагрузка фото на WB.

Страница показывает связанные с поставщиками карточки, дельту «фото на WB vs
у поставщика» и запускает фоновую дозагрузку фото (media/save) по выбранным.
"""
import logging
import threading
import uuid

from flask import render_template, request, jsonify, current_app
from flask_login import login_required, current_user

from models import db, Product, BackgroundJob
from services.supplier_update_hub import (
    JOB_TYPE, query_update_rows, get_supplier_chips,
    expand_filter_to_ids, run_photos_job,
)

logger = logging.getLogger(__name__)

ACTIVE_STATUSES = ('pending', 'running')


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

        chips = get_supplier_chips(seller.id)
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

        # Один активный job на продавца
        existing = (BackgroundJob.query
                    .filter_by(seller_id=seller.id, job_type=JOB_TYPE)
                    .filter(BackgroundJob.status.in_(ACTIVE_STATUSES))
                    .first())
        if existing:
            return jsonify({'success': False, 'job_uid': existing.job_uid,
                            'error': 'Обновление фото уже выполняется'}), 409

        if body.get('select_all'):
            supplier_id = body.get('supplier_id') or None
            only_new = bool(body.get('only_new', True))
            search = (body.get('search') or '').strip()
            product_ids = expand_filter_to_ids(
                seller.id, supplier_id=supplier_id,
                only_new=only_new, search=search,
            )
        else:
            raw_ids = body.get('product_ids') or []
            if not isinstance(raw_ids, list):
                return jsonify({'success': False, 'error': 'product_ids должен быть списком'}), 400
            try:
                raw_ids = [int(x) for x in raw_ids]
            except (TypeError, ValueError):
                return jsonify({'success': False, 'error': 'Некорректные product_ids'}), 400
            # Только карточки текущего продавца
            product_ids = [
                p.id for p in Product.query
                .filter(Product.id.in_(raw_ids), Product.seller_id == seller.id)
                .all()
            ]

        if not product_ids:
            return jsonify({'success': False, 'error': 'Нет карточек для обновления'}), 400

        job_uid = uuid.uuid4().hex
        job = BackgroundJob(
            job_uid=job_uid, seller_id=seller.id, job_type=JOB_TYPE,
            status='pending', total=len(product_ids),
        )
        db.session.add(job)
        db.session.commit()

        flask_app = current_app._get_current_object()
        t = threading.Thread(
            target=run_photos_job,
            args=(flask_app, job_uid, seller.id, product_ids),
            daemon=True,
        )
        t.start()

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
