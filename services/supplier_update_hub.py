# -*- coding: utf-8 -*-
"""Обновление карточек от поставщика: выборка дельт по фото и массовая дозагрузка.

Карточка продавца (Product) связана с каталогом поставщика цепочкой
Product ← ImportedProduct → SupplierProduct → Supplier. Актуальный набор фото
берётся напрямую из SupplierProduct.photo_urls_json (staging-копия
ImportedProduct.photo_urls может устареть).

«Дозагрузить фото» использует общий live-aware smart merge: существующие слоты
WB и их порядок сохраняются, supplier-файлы сравниваются perceptual hash и
только доказанно отсутствующие добавляются после live-галереи. Durable cursor и
pre-send history не допускают слепого replay после рестарта.
"""
import json
import logging
import uuid
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

from sqlalchemy import and_, case, func, or_

from models import (
    db, Product, ImportedProduct, Supplier, SupplierProduct, Seller,
    BackgroundJob, BulkEditHistory, Notification,
    get_standard_media, get_min_photos,
)
from services.standard_photos import compose_card_photo_urls, WB_MAX_PHOTOS
from services.wb_api_client import WildberriesAPIClient, normalize_cards_error_list

logger = logging.getLogger(__name__)

JOB_TYPE = 'supplier_photos_update'
VERIFY_JOB_TYPE = 'supplier_updates_verify'
MAX_SUPPLIER_UPDATE_PRODUCTS = 200
_PHOTO_JOB_CLAIM_KEY = '_photo_job_claim_token'
_ACTIVE_JOB_STATUSES = ('pending', 'running')


class SupplierUpdateJobAlreadyActive(RuntimeError):
    """The seller already owns this kind of supplier-update job."""

    def __init__(self, job_uid: Optional[str] = None):
        self.job_uid = job_uid
        super().__init__('Задача этого типа уже выполняется')


def create_supplier_update_job(
    *,
    seller_id: int,
    job_type: str,
    product_ids: List[int],
    progress: dict,
) -> BackgroundJob:
    """Atomically check and create one durable supplier-update job.

    SQLite has no portable partial unique constraint in the legacy schema, so
    a host-shared seller/type lock closes the HTTP check→insert race.
    """
    if (
        not isinstance(seller_id, int)
        or isinstance(seller_id, bool)
        or seller_id <= 0
        or job_type not in {JOB_TYPE, VERIFY_JOB_TYPE}
        or not isinstance(product_ids, list)
        or not 1 <= len(product_ids) <= MAX_SUPPLIER_UPDATE_PRODUCTS
        or any(
            not isinstance(value, int)
            or isinstance(value, bool)
            or value <= 0
            for value in product_ids
        )
        or len(set(product_ids)) != len(product_ids)
        or not isinstance(progress, dict)
    ):
        raise ValueError('Invalid supplier update job payload')

    from services.marketplace_operation_locks import (
        release_wb_seller_supplier_job_lock,
        try_wb_seller_supplier_photo_job_lock,
        try_wb_seller_supplier_verify_job_lock,
    )

    lock_factory = (
        try_wb_seller_supplier_photo_job_lock
        if job_type == JOB_TYPE
        else try_wb_seller_supplier_verify_job_lock
    )
    claim = lock_factory(seller_id)
    if claim is None:
        raise SupplierUpdateJobAlreadyActive()
    try:
        existing = BackgroundJob.query.filter_by(
            seller_id=seller_id,
            job_type=job_type,
        ).filter(
            BackgroundJob.status.in_(_ACTIVE_JOB_STATUSES),
        ).order_by(BackgroundJob.id.desc()).first()
        if existing is not None:
            raise SupplierUpdateJobAlreadyActive(existing.job_uid)

        job = BackgroundJob(
            job_uid=uuid.uuid4().hex,
            seller_id=seller_id,
            job_type=job_type,
            status='pending',
            total=len(product_ids),
        )
        durable_progress = dict(progress)
        durable_progress['product_ids'] = list(product_ids)
        job.set_progress(durable_progress)
        db.session.add(job)
        db.session.commit()
        return job
    except Exception:
        db.session.rollback()
        raise
    finally:
        release_wb_seller_supplier_job_lock(claim)


def _load_owned_photo_job(job_uid: str, seller_id: int, claim_token: str):
    """Reload a photo job and prove this worker still owns its generation."""
    db.session.expire_all()
    job = BackgroundJob.query.filter_by(
        job_uid=job_uid,
        seller_id=seller_id,
        job_type=JOB_TYPE,
    ).first()
    if job is None:
        return None, None
    progress = job.get_progress() or {}
    if progress.get(_PHOTO_JOB_CLAIM_KEY) != claim_token:
        return None, None
    return job, progress


def _release_photo_job_claim(progress: dict, claim_token: str) -> None:
    if progress.get(_PHOTO_JOB_CLAIM_KEY) == claim_token:
        progress.pop(_PHOTO_JOB_CLAIM_KEY, None)

def _json_len(column):
    """SQL-выражение: длина JSON-массива в колонке (0 для NULL/мусора)."""
    return case(
        (func.json_valid(column), func.json_array_length(column)),
        else_=0,
    )


def _base_query(seller_id: int, supplier_id: Optional[int] = None,
                only_new: bool = True, search: str = ''):
    wb_count = _json_len(Product.photos_json).label('wb_count')
    sp_count = _json_len(SupplierProduct.photo_urls_json).label('sp_count')

    q = (
        db.session.query(Product, ImportedProduct, Supplier, wb_count, sp_count)
        .join(ImportedProduct, ImportedProduct.product_id == Product.id)
        .join(SupplierProduct, SupplierProduct.id == ImportedProduct.supplier_product_id)
        .join(Supplier, Supplier.id == ImportedProduct.supplier_id)
        .filter(
            Product.seller_id == seller_id,
            ImportedProduct.seller_id == seller_id,
            Product.is_active.is_(True),
            Product.nm_id.isnot(None),
        )
        .group_by(Product.id)
    )
    if supplier_id:
        q = q.filter(ImportedProduct.supplier_id == supplier_id)
    if only_new:
        q = q.filter(sp_count > wb_count)
    if search:
        like = f'%{search}%'
        filters = [Product.title.ilike(like), Product.vendor_code.ilike(like)]
        if search.isdigit():
            filters.append(Product.nm_id == int(search))
        q = q.filter(or_(*filters))
    return q, wb_count, sp_count


def query_update_rows(seller_id: int, supplier_id: Optional[int] = None,
                      only_new: bool = True, search: str = '',
                      page: int = 1, per_page: int = 50) -> Tuple[List[dict], int]:
    """Строки хаба: карточки продавца со счётчиками фото WB/поставщик."""
    q, wb_count, sp_count = _base_query(seller_id, supplier_id, only_new, search)
    total = q.count()
    q = q.order_by((sp_count - wb_count).desc(), Product.id.asc())
    items = q.offset((page - 1) * per_page).limit(per_page).all()

    rows = []
    for product, imp, supplier, wb_n, sp_n in items:
        rows.append({
            'product': product,
            'imported_product_id': imp.id,
            'supplier_product_id': imp.supplier_product_id,
            'supplier_id': supplier.id,
            'supplier_name': supplier.name,
            'wb_count': int(wb_n or 0),
            'supplier_count': int(sp_n or 0),
            'delta': int(sp_n or 0) - int(wb_n or 0),
        })
    return rows, total


def expand_filter_to_ids(seller_id: int, supplier_id: Optional[int] = None,
                         only_new: bool = True, search: str = '',
                         limit: int = MAX_SUPPLIER_UPDATE_PRODUCTS) -> List[int]:
    """Развернуть фильтр в список product_id (для «выбрать всё по фильтру»)."""
    if (
        not isinstance(limit, int)
        or isinstance(limit, bool)
        or not 1 <= limit <= MAX_SUPPLIER_UPDATE_PRODUCTS + 1
    ):
        raise ValueError('invalid supplier update filter limit')
    q, _, sp_count = _base_query(seller_id, supplier_id, only_new, search)
    wb = _json_len(Product.photos_json)
    q = q.order_by((sp_count - wb).desc(), Product.id.asc())
    return [product.id for product, *_ in q.limit(limit).all()]


def get_supplier_chips(seller_id: int) -> List[dict]:
    """Сводка по поставщикам продавца: всего связанных карточек / с новыми фото."""
    wb_count = _json_len(Product.photos_json)
    sp_count = _json_len(SupplierProduct.photo_urls_json)
    rows = (
        db.session.query(
            Supplier.id, Supplier.name, Supplier.code,
            func.count(func.distinct(Product.id)),
            func.sum(case((sp_count > wb_count, 1), else_=0)),
        )
        .select_from(Product)
        .join(ImportedProduct, ImportedProduct.product_id == Product.id)
        .join(SupplierProduct, SupplierProduct.id == ImportedProduct.supplier_product_id)
        .join(Supplier, Supplier.id == ImportedProduct.supplier_id)
        .filter(
            Product.seller_id == seller_id,
            ImportedProduct.seller_id == seller_id,
            Product.is_active.is_(True),
            Product.nm_id.isnot(None),
        )
        .group_by(Supplier.id, Supplier.name, Supplier.code)
        .all()
    )
    return [
        {'supplier_id': sid, 'name': name, 'code': code,
         'total': int(total or 0), 'with_new': int(with_new or 0)}
        for sid, name, code, total, with_new in rows
    ]


def build_target_photo_set(supplier_product: SupplierProduct, product: Product,
                           seller_id: int) -> List[str]:
    """Целевой набор URL для media/save: пины продавца + вся галерея поставщика."""
    photos = supplier_product.get_photos() if supplier_product else []
    if not photos:
        return []

    from routes.photos import generate_public_photo_url
    supplier_urls = [
        generate_public_photo_url(supplier_product.id, idx)
        for idx in range(len(photos))
    ]

    media = get_standard_media(seller_id, getattr(product, 'subject_id', None))
    composed = compose_card_photo_urls(
        supplier_urls, media, seller_id, get_min_photos(seller_id))
    # Композер возвращает [] когда пинов нет — тогда просто галерея поставщика
    return composed if composed else supplier_urls[:WB_MAX_PHOTOS]


def verify_cards_on_wb(seller, product_ids: List[int]) -> dict:
    """Сверка «долетели ли обновления до WB» по списку карточек продавца.

    Два источника истины WB:
      1. /content/v2/cards/error/list — асинхронные ошибки обработки
         (WB отвечает 200/202 на save, а реальные отказы видны только здесь);
      2. фактическая карточка (fetch_cards_by_nm_ids) — сравниваем число фото
         на WB с целевым набором поставщика.

    Статусы per-card: ok | pending (WB ещё обрабатывает) | error (ошибки WB)
    | not_found (карточка не найдена на WB).
    Попутно обновляет Product.photos_json фактическими URL с WB, чтобы
    счётчики дельты хаба отражали реальность.
    """
    products = (
        Product.query
        .filter(
            Product.id.in_(product_ids),
            Product.seller_id == seller.id,
            Product.nm_id.isnot(None),
        )
        .all()
    )
    summary = {'ok': 0, 'pending': 0, 'error': 0, 'not_found': 0}
    if not products:
        return {'items': [], 'summary': summary}

    client = WildberriesAPIClient(seller.wb_api_key)
    try:
        cards = client.fetch_cards_by_nm_ids(
            [int(p.nm_id) for p in products], seller_id=seller.id
        )
        wb_errors = client.get_cards_error_list(seller_id=seller.id)
    finally:
        close = getattr(client, 'close', None)
        if callable(close):
            close()
    errors_by_nm, errors_by_vendor = normalize_cards_error_list(wb_errors)

    # Целевое число фото: пины продавца + галерея поставщика
    imps = {
        imp.product_id: imp
        for imp in ImportedProduct.query.filter(
            ImportedProduct.seller_id == seller.id,
            ImportedProduct.product_id.in_([p.id for p in products]),
            ImportedProduct.supplier_product_id.isnot(None),
        ).all()
    }
    sp_ids = [imp.supplier_product_id for imp in imps.values()]
    sps_by_id = {
        sp.id: sp
        for sp in SupplierProduct.query.filter(
            SupplierProduct.id.in_(sp_ids)
        ).all()
    } if sp_ids else {}

    items = []
    for p in products:
        nm_id = int(p.nm_id)
        entry = {
            'product_id': p.id,
            'nm_id': nm_id,
            'title': (p.title or '')[:80],
            'vendor_code': p.vendor_code,
            'wb_photos': None,
            'expected_photos': None,
            'errors': [],
        }

        msgs = errors_by_nm.get(nm_id) or errors_by_vendor.get(str(p.vendor_code or ''))
        card = cards.get(nm_id)

        expected = None
        imp = imps.get(p.id)
        if imp:
            sp = sps_by_id.get(imp.supplier_product_id)
            if sp:
                expected = len(build_target_photo_set(sp, p, seller.id))
        entry['expected_photos'] = expected

        if msgs:
            entry['status'] = 'error'
            entry['errors'] = [str(m) for m in msgs[:5]]
            summary['error'] += 1
        elif not card:
            entry['status'] = 'not_found'
            summary['not_found'] += 1
        else:
            photos = card.get('photos') or []
            entry['wb_photos'] = len(photos)
            # Синхронизируем локальный набор фото с фактом WB — дельта хаба
            # пересчитается честно. Пустое состояние тоже факт: карточка без
            # фото обязана вернуться в хаб дозагрузки, а не скрываться за
            # старым локальным списком. Если read model WB запаздывает,
            # следующая сверка поправит счётчик обратно.
            urls = []
            for ph in photos:
                if isinstance(ph, dict):
                    url = ph.get('big') or ph.get('c516x688') or ph.get('square')
                else:
                    url = ph if isinstance(ph, str) else None
                if url:
                    urls.append(url)
            p.photos_json = json.dumps(urls, ensure_ascii=False)
            if expected is not None and len(photos) < expected:
                entry['status'] = 'pending'
                summary['pending'] += 1
            else:
                entry['status'] = 'ok'
                summary['ok'] += 1
        items.append(entry)

    db.session.commit()

    # Дельты изменились — чипы пересчитаются на следующем показе
    try:
        from services.ttl_cache import cache
        cache.invalidate(f'supdates-chips:{seller.id}')
    except Exception:
        pass

    return {'items': items, 'summary': summary}


def run_verify_job(
    flask_app,
    job_uid: str,
    seller_id: int,
    product_ids: Optional[List[int]] = None,
) -> None:
    """Run a restart-safe read-only WB verification job."""
    with flask_app.app_context():
        now = datetime.utcnow()
        claimed = BackgroundJob.query.filter(
            BackgroundJob.job_uid == job_uid,
            BackgroundJob.seller_id == seller_id,
            BackgroundJob.job_type == VERIFY_JOB_TYPE,
            or_(
                BackgroundJob.status == 'pending',
                and_(
                    BackgroundJob.status == 'running',
                    BackgroundJob.updated_at <= now - timedelta(minutes=15),
                ),
            ),
        ).update({
            BackgroundJob.status: 'running',
            BackgroundJob.updated_at: now,
        }, synchronize_session=False)
        db.session.commit()
        if claimed != 1:
            return

        job = BackgroundJob.query.filter_by(job_uid=job_uid).first()
        progress = job.get_progress() or {}
        durable_ids = progress.get('product_ids')
        if not isinstance(durable_ids, list) or not durable_ids:
            durable_ids = list(product_ids or [])
            progress['product_ids'] = durable_ids
        if (
            not durable_ids
            or len(durable_ids) > MAX_SUPPLIER_UPDATE_PRODUCTS
            or any(
                not isinstance(value, int) or isinstance(value, bool) or value <= 0
                for value in durable_ids
            )
            or len(set(durable_ids)) != len(durable_ids)
        ):
            job.status = 'failed'
            job.error_message = 'Повреждён durable список карточек для сверки'
            db.session.commit()
            return
        job.total = len(durable_ids)
        job.set_progress(progress)
        db.session.commit()

        try:
            seller = db.session.get(Seller, seller_id)
            if seller is None:
                raise RuntimeError('Продавец не найден')
            report = verify_cards_on_wb(seller, durable_ids)

            job = BackgroundJob.query.filter_by(job_uid=job_uid).first()
            if job.status != 'cancelled':
                job.status = 'completed'
            job.processed = len(report['items'])
            job.succeeded = report['summary']['ok']
            job.failed_count = report['summary']['error']
            job.set_result(report)
            db.session.commit()
            logger.info(f"[SupplierVerify] job {job_uid} done: {report['summary']}")
        except Exception as e:
            logger.error(f"[SupplierVerify] job {job_uid} failed: {e}", exc_info=True)
            db.session.rollback()
            job = BackgroundJob.query.filter_by(job_uid=job_uid).first()
            if job:
                progress = job.get_progress() or {}
                attempts = int(progress.get('attempts') or 0) + 1
                progress['attempts'] = attempts
                job.set_progress(progress)
                job.status = 'failed' if attempts >= 3 else 'pending'
                job.error_message = (
                    str(e)[:500]
                    if attempts >= 3 else
                    'Временная ошибка live-сверки; задача будет продолжена'
                )
                db.session.commit()


def run_photos_job(
    flask_app,
    job_uid: str,
    seller_id: int,
    product_ids: Optional[List[int]] = None,
    *,
    item_limit: Optional[int] = None,
) -> None:
    """Advance a restart-safe photo hub job without replaying in-flight media."""
    with flask_app.app_context():
        from models import CardEditHistory
        from services.supplier_enrichment import EnrichmentService

        now = datetime.utcnow()
        stale_cutoff = now - timedelta(minutes=15)
        candidate = BackgroundJob.query.filter(
            BackgroundJob.job_uid == job_uid,
            BackgroundJob.seller_id == seller_id,
            BackgroundJob.job_type == JOB_TYPE,
            or_(
                BackgroundJob.status == 'pending',
                and_(
                    BackgroundJob.status == 'running',
                    BackgroundJob.updated_at <= stale_cutoff,
                ),
            ),
        ).first()
        if candidate is None:
            return

        # BackgroundJob is shared by legacy workflows and has no dedicated
        # lease columns. Use an exact JSON compare-and-set generation token:
        # a worker that outlives the 15-minute stale window may finish its
        # provider I/O, but it cannot advance counters/cursor after takeover.
        raw_progress = candidate.progress_data
        progress = candidate.get_progress() or {}
        claim_token = uuid.uuid4().hex
        progress[_PHOTO_JOB_CLAIM_KEY] = claim_token
        claimed_progress = json.dumps(progress, ensure_ascii=False)
        progress_match = (
            BackgroundJob.progress_data.is_(None)
            if raw_progress is None
            else BackgroundJob.progress_data == raw_progress
        )
        claimed = BackgroundJob.query.filter(
            BackgroundJob.id == candidate.id,
            BackgroundJob.seller_id == seller_id,
            BackgroundJob.job_type == JOB_TYPE,
            progress_match,
            or_(
                BackgroundJob.status == 'pending',
                and_(
                    BackgroundJob.status == 'running',
                    BackgroundJob.updated_at <= stale_cutoff,
                ),
            ),
        ).update({
            BackgroundJob.status: 'running',
            BackgroundJob.progress_data: claimed_progress,
            BackgroundJob.updated_at: now,
        }, synchronize_session=False)
        db.session.commit()
        if claimed != 1:
            return

        job, progress = _load_owned_photo_job(
            job_uid, seller_id, claim_token,
        )
        if job is None:
            return
        durable_ids = progress.get('product_ids')
        if not isinstance(durable_ids, list) or not durable_ids:
            durable_ids = list(product_ids or [])
            progress['product_ids'] = durable_ids
        if (
            not durable_ids
            or len(durable_ids) > MAX_SUPPLIER_UPDATE_PRODUCTS
            or any(
                not isinstance(value, int) or isinstance(value, bool) or value <= 0
                for value in durable_ids
            )
            or len(set(durable_ids)) != len(durable_ids)
        ):
            job.status = 'failed'
            job.error_message = 'Повреждён durable список карточек'
            _release_photo_job_claim(progress, claim_token)
            job.set_progress(progress)
            db.session.commit()
            return

        job.total = len(durable_ids)
        seller = db.session.get(Seller, seller_id)
        if seller is None:
            job.status = 'failed'
            job.error_message = 'Продавец не найден'
            _release_photo_job_claim(progress, claim_token)
            job.set_progress(progress)
            db.session.commit()
            return

        bulk_edit_id = progress.get('bulk_edit_id')
        bulk_history = (
            db.session.get(BulkEditHistory, bulk_edit_id)
            if isinstance(bulk_edit_id, int) else None
        )
        if bulk_history is None:
            bulk_history = BulkEditHistory(
                seller_id=seller_id,
                operation_type=JOB_TYPE,
                operation_params={'job_uid': job_uid, 'field': 'photos'},
                description='Дозагрузка фото из каталога поставщика',
                status='in_progress',
                total_products=len(durable_ids),
                success_count=0,
                error_count=0,
                errors_details=[],
                wb_synced=False,
            )
            db.session.add(bulk_history)
            db.session.flush()
            progress['bulk_edit_id'] = bulk_history.id
            job.set_progress(progress)
            db.session.commit()

        wb_client = None
        try:
            wb_client = WildberriesAPIClient(seller.wb_api_key)
        except Exception as exc:
            db.session.rollback()
            job, progress = _load_owned_photo_job(
                job_uid, seller_id, claim_token,
            )
            if job is not None:
                attempts = int(progress.get('worker_attempts') or 0) + 1
                progress['worker_attempts'] = attempts
                _release_photo_job_claim(progress, claim_token)
                job.set_progress(progress)
                job.status = 'failed' if attempts >= 3 else 'pending'
                job.error_message = (
                    str(exc)[:500]
                    if attempts >= 3 else
                    'Временная ошибка WB-клиента; задача будет продолжена'
                )
                db.session.commit()
            return
        enrichment_service = EnrichmentService()
        succeeded = int(job.succeeded or 0)
        failed = int(job.failed_count or 0)
        skipped = int(progress.get('skipped') or 0)
        item_errors = progress.get('errors')
        if not isinstance(item_errors, list):
            item_errors = []
        handled = 0

        try:
            for idx in range(int(job.processed or 0), len(durable_ids)):
                if item_limit is not None and handled >= item_limit:
                    break
                job, progress = _load_owned_photo_job(
                    job_uid, seller_id, claim_token,
                )
                if job is None:
                    return
                if job.status == 'cancelled':
                    break
                pid = durable_ids[idx]
                resumed_inflight = progress.get('current_product_id') == pid
                if not resumed_inflight:
                    progress['current_product_id'] = pid
                    progress['current_item_started_at'] = datetime.utcnow().isoformat()
                    job.set_progress(progress)
                    db.session.commit()

                outcome_error = None
                recovered = False
                if resumed_inflight:
                    try:
                        started_at = datetime.fromisoformat(
                            progress.get('current_item_started_at') or ''
                        )
                    except (TypeError, ValueError):
                        started_at = job.created_at
                    receipts = CardEditHistory.query.filter(
                        CardEditHistory.seller_id == seller_id,
                        CardEditHistory.product_id == pid,
                        CardEditHistory.bulk_edit_id == bulk_history.id,
                        CardEditHistory.created_at >= started_at,
                    ).all()
                    recovered = any(
                        set(row.changed_fields or []) == {'photos'}
                        and row.wb_sync_status in {
                            'pending', 'submitted', 'uncertain',
                            'partial', 'success',
                        }
                        for row in receipts
                    )
                    recovered_terminal_failure = any(
                        set(row.changed_fields or []) == {'photos'}
                        and row.wb_sync_status in {'failed', 'conflict'}
                        for row in receipts
                    )
                    recovered_noop = any(
                        row.wb_sync_status == 'skipped'
                        and isinstance(row.merge_decisions, dict)
                        and isinstance(
                            row.merge_decisions.get('photos'), dict
                        )
                        for row in receipts
                    )
                else:
                    recovered_terminal_failure = False
                    recovered_noop = False

                defer_current = False
                deferred_reason = None
                try:
                    product = db.session.get(Product, pid)
                    imp = None
                    if product and product.seller_id == seller_id:
                        imp = (ImportedProduct.query
                               .filter_by(seller_id=seller_id, product_id=pid)
                               .filter(ImportedProduct.supplier_product_id.isnot(None))
                               .first())
                    if not product or not imp or not product.nm_id:
                        skipped += 1
                    elif recovered_terminal_failure:
                        failed += 1
                        outcome_error = (
                            'Предыдущая отправка фото завершилась конфликтом '
                            'или ошибкой; автоматический replay запрещён'
                        )
                    elif recovered:
                        succeeded += 1
                    elif recovered_noop:
                        skipped += 1
                    else:
                        sp = db.session.get(SupplierProduct, imp.supplier_product_id)
                        if not sp:
                            skipped += 1
                        else:
                            result = enrichment_service.apply_enrichment(
                                product,
                                imp,
                                ['photos'],
                                'smart_merge',
                                seller,
                                wb_client,
                                bulk_edit_id=bulk_history.id,
                                is_bulk=True,
                            )
                            if result.get('deferred'):
                                defer_current = True
                                deferred_reason = str(
                                    result.get('error')
                                    or 'Ожидание предыдущей отправки фото WB'
                                )[:500]
                            elif result.get('wb_sync'):
                                succeeded += 1
                            elif (result.get('photos') or {}).get('skipped'):
                                skipped += 1
                            else:
                                failed += 1
                                outcome_error = result.get('error') or 'не отправлено'
                except Exception as exc:
                    db.session.rollback()
                    failed += 1
                    outcome_error = 'Не удалось обработать карточку'
                    logger.warning(
                        '[SupplierPhotos] product %s: %s', pid, exc,
                        exc_info=True,
                    )

                # Provider/photo-cache I/O can outlive the stale cutoff. A
                # replacement worker may already own the durable cursor; in
                # that case this generation leaves its receipt for recovery
                # and must not publish counters or advance the row.
                job, progress = _load_owned_photo_job(
                    job_uid, seller_id, claim_token,
                )
                if job is None:
                    return

                if defer_current:
                    # Preserve current_product_id/current_item_started_at and
                    # leave processed unchanged. A later scheduler tick will
                    # rebuild the live-aware plan after the older receipt has
                    # reached a terminal state.
                    progress['deferred_reason'] = deferred_reason
                    job.set_progress(progress)
                    db.session.commit()
                    break

                if outcome_error and len(item_errors) < 50:
                    item_errors.append({
                        'product_id': pid,
                        'error': str(outcome_error)[:200],
                    })
                progress.update({
                    'product_ids': durable_ids,
                    'bulk_edit_id': bulk_history.id,
                    'errors': item_errors,
                    'skipped': skipped,
                    'current_product_id': None,
                    'current_item_started_at': None,
                    'deferred_reason': None,
                })
                job.processed = idx + 1
                job.succeeded = succeeded
                job.failed_count = failed
                job.set_progress(progress)
                db.session.commit()
                handled += 1

            job, progress = _load_owned_photo_job(
                job_uid, seller_id, claim_token,
            )
            if job is None:
                return
            completed = int(job.processed or 0) >= len(durable_ids)
            if job.status != 'cancelled':
                job.status = 'completed' if completed else 'pending'
            bulk_history = db.session.get(BulkEditHistory, bulk_history.id)
            bulk_history.status = (
                'completed' if completed else (
                    'cancelled' if job.status == 'cancelled' else 'in_progress'
                )
            )
            bulk_history.success_count = succeeded
            bulk_history.error_count = failed + skipped
            bulk_history.errors_details = item_errors[:20]
            bulk_history.wb_synced = False
            bulk_history.completed_at = datetime.utcnow() if completed else None
            job.set_result({
                'succeeded': succeeded,
                'failed': failed,
                'skipped': skipped,
                'total': len(durable_ids),
                'reconciliation_pending': succeeded,
                'dispatch_deferred': bool(
                    not completed and progress.get('current_product_id')
                ),
            })
            _release_photo_job_claim(progress, claim_token)
            job.set_progress(progress)
            db.session.commit()

            if completed and succeeded:
                db.session.add(Notification(
                    seller_id=seller_id,
                    category='info',
                    title='Фото от поставщика отправлены',
                    message=(
                        f'Отправлено карточек: {succeeded} из {len(durable_ids)}; '
                        'фактическое состояние проверяется по live-галерее WB'
                    ),
                    link='/supplier-updates',
                ))
                db.session.commit()

            try:
                from services.ttl_cache import cache
                cache.invalidate(f'supdates-chips:{seller_id}')
            except Exception:
                pass
        except Exception as exc:
            db.session.rollback()
            logger.exception(
                '[SupplierPhotos] worker %s interrupted', job_uid,
            )
            job, progress = _load_owned_photo_job(
                job_uid, seller_id, claim_token,
            )
            if job is not None:
                attempts = int(progress.get('worker_attempts') or 0) + 1
                progress['worker_attempts'] = attempts
                _release_photo_job_claim(progress, claim_token)
                job.set_progress(progress)
                job.status = 'failed' if attempts >= 3 else 'pending'
                job.error_message = (
                    str(exc)[:500]
                    if attempts >= 3 else
                    'Временная ошибка worker; задача будет продолжена'
                )
                db.session.commit()
        finally:
            close = getattr(wb_client, 'close', None)
            if callable(close):
                close()


def process_due_photo_jobs(flask_app, *, max_jobs: int = 1) -> int:
    """Resume bounded photo-hub cursors after thread/container interruption."""
    with flask_app.app_context():
        cutoff = datetime.utcnow() - timedelta(minutes=15)
        rows = BackgroundJob.query.filter(
            BackgroundJob.job_type == JOB_TYPE,
            or_(
                BackgroundJob.status == 'pending',
                and_(
                    BackgroundJob.status == 'running',
                    BackgroundJob.updated_at <= cutoff,
                ),
            ),
        ).order_by(
            BackgroundJob.created_at.asc(), BackgroundJob.id.asc(),
        ).limit(max(1, min(3, int(max_jobs)))).all()
        targets = [(row.job_uid, row.seller_id) for row in rows]
        db.session.remove()

    for job_uid, seller_id in targets:
        run_photos_job(
            flask_app,
            job_uid,
            seller_id,
            None,
            item_limit=3,
        )
    return len(targets)


def process_due_verify_jobs(flask_app, *, max_jobs: int = 1) -> int:
    """Resume read-only supplier verification after worker/container restart."""
    with flask_app.app_context():
        cutoff = datetime.utcnow() - timedelta(minutes=15)
        rows = BackgroundJob.query.filter(
            BackgroundJob.job_type == VERIFY_JOB_TYPE,
            or_(
                BackgroundJob.status == 'pending',
                and_(
                    BackgroundJob.status == 'running',
                    BackgroundJob.updated_at <= cutoff,
                ),
            ),
        ).order_by(
            BackgroundJob.created_at.asc(), BackgroundJob.id.asc(),
        ).limit(max(1, min(3, int(max_jobs)))).all()
        targets = [(row.job_uid, row.seller_id) for row in rows]
        db.session.remove()

    for job_uid, seller_id in targets:
        run_verify_job(flask_app, job_uid, seller_id, None)
    return len(targets)
