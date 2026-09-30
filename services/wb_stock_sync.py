"""Durable seller-scoped FBW stock pages; web paths enqueue, never fetch.

An exact batch is exposed only after observing its pagination end. Failures,
duplicates, access denial and partial pages preserve all previous stock facts.
The existing BackgroundJob holds only the current bounded batch, not a catalog.
"""

from collections import defaultdict
from datetime import datetime, timedelta
import hashlib
import logging
import uuid

from sqlalchemy import delete, func, update
from sqlalchemy.orm import load_only

from models import BackgroundJob, Product, ProductStock, Seller, db
from services.marketplace_operation_locks import _try_operation_lock
from services.wb_api_client import WBAuthException, WBRateLimitException, WildberriesAPIClient
from services.wb_credentials import token_is_expired, token_needs_stock_upgrade
from services.wb_stock_contracts import PAGE_SIZE, WBStockContractError, combine_stock_pages, normalize_stock_page


logger = logging.getLogger(__name__)
JOB_TYPE = 'wb_warehouse_stocks'
BATCH_SIZE = 100
ACCESS_MESSAGE = ('Для остатков WB нужен персональный или сервисный ключ с доступом к аналитике. '
                  'Проверьте тип и права ключа в настройках API. Последние остатки сохранены.')


class WBStockSyncError(RuntimeError):
    code = 'wb_stock_sync_error'
    status_code = 409


class WBStockAccessError(WBStockSyncError):
    code = 'wb_stock_access_required'
    status_code = 403


def _owned_key(seller_id):
    if type(seller_id) is not int or seller_id <= 0:
        raise WBStockSyncError('Некорректный продавец')
    seller = db.session.get(Seller, seller_id)
    if seller is None or not seller.wb_api_key:
        raise WBStockAccessError('Настройте API ключ Wildberries.')
    key = seller.wb_api_key
    if token_is_expired(key):
        raise WBStockAccessError('Срок действия API ключа Wildberries истёк. Замените его в настройках API.')
    if token_needs_stock_upgrade(key):
        raise WBStockAccessError(ACCESS_MESSAGE)
    return key


def enqueue_stock_sync(seller_id):
    """Idempotent local request; never provider I/O or a new thread."""
    _owned_key(seller_id)
    claim = _try_operation_lock('wb-stock-seller', seller_id)
    if claim is None:
        raise WBStockSyncError('Обновление остатков уже выполняется. Статус доступен в фоновых задачах.')
    try:
        existing = BackgroundJob.query.filter_by(seller_id=seller_id, job_type=JOB_TYPE).filter(
            BackgroundJob.status.in_(['pending', 'running']),
        ).first()
        if existing:
            return public_job(existing)
        last_id, count = db.session.query(func.max(Product.id), func.count(Product.id)).filter(
            Product.seller_id == seller_id, Product.nm_id > 0,
        ).one()
        job = BackgroundJob(
            job_uid=uuid.uuid4().hex, seller_id=seller_id, job_type=JOB_TYPE,
            status='pending' if count else 'completed', total=count, processed=0,
        )
        job.set_progress({'cursor': 0, 'last_id': last_id or 0})
        db.session.add(job)
        db.session.commit()
        return public_job(job)
    finally:
        claim.close()


def public_job(job):
    if job is None:
        return None
    return {
        'job_uid': job.job_uid, 'status': job.status,
        'processed': job.processed, 'total': job.total,
        'message': job.error_message,
        'updated_at': job.updated_at.isoformat() if job.updated_at else None,
        'source': 'wb_analytics_warehouses',
    }


def latest_stock_sync(seller_id):
    job = BackgroundJob.query.options(load_only(
        BackgroundJob.job_uid, BackgroundJob.status, BackgroundJob.processed,
        BackgroundJob.total, BackgroundJob.error_message, BackgroundJob.updated_at,
    )).filter_by(seller_id=seller_id, job_type=JOB_TYPE).order_by(BackgroundJob.id.desc()).first()
    return public_job(job)


def _replace_complete_batch(seller_id, targets, rows, now):
    """One short transaction replaces a rebuildable FBW projection, not history."""
    ids = [target[0] for target in targets]
    current = db.session.query(Product.id, Product.nm_id).filter(
        Product.seller_id == seller_id, Product.id.in_(ids),
    ).order_by(Product.id).all()
    if [list(pair) for pair in current] != targets:
        raise WBStockContractError('WB stock target changed during sync')
    nm_to_id = {nm: pid for pid, nm in current}
    if len(nm_to_id) != len(current):
        raise WBStockContractError('Ambiguous local WB identity')
    aggregated = {}
    totals = defaultdict(int)
    for row in rows:
        key = (nm_to_id[row['nmId']], row['warehouseId'])
        if key not in aggregated:
            aggregated[key] = dict(
                product_id=key[0], warehouse_id=key[1], warehouse_name=row['warehouseName'],
                quantity=0, in_way_to_client=0, in_way_from_client=0,
                # This field was removed from the current API. NULL is unknown,
                # not zero and not an invented sum of incompatible quantities.
                quantity_full=None, created_at=now, updated_at=now,
            )
        stock = aggregated[key]
        if stock['warehouse_name'] != row['warehouseName']:
            raise WBStockContractError('Conflicting WB warehouse identity')
        for source, target in [('quantity', 'quantity'), ('inWayToClient', 'in_way_to_client'), ('inWayFromClient', 'in_way_from_client')]:
            stock[target] += row[source]
        totals[key[0]] += row['quantity']
    # No other table points to product_stocks; only this recoverable read model
    # is replaced. Name-derived legacy IDs disappear only after a complete read.
    db.session.execute(delete(ProductStock).where(ProductStock.product_id.in_(ids)))
    if aggregated:
        db.session.execute(ProductStock.__table__.insert(), list(aggregated.values()))
    db.session.execute(
        update(Product), [{'id': pid, 'quantity': totals[pid]} for pid in ids],
    )


def run_stock_sync_tick(flask_app):
    """One physical page per minute globally; no sleep, external write or LLM."""
    with flask_app.app_context():
        candidates = db.session.query(BackgroundJob.id, BackgroundJob.seller_id).filter(
            BackgroundJob.job_type == JOB_TYPE,
            BackgroundJob.status.in_(['pending', 'running']),
        ).order_by(
            # Finish a bounded partial batch inside its freshness window, then
            # yield to the oldest pending seller. Do not starve slow pages by
            # round-robining hundreds of accounts until every buffer expires.
            (BackgroundJob.status == 'running').desc(),
            BackgroundJob.updated_at, BackgroundJob.id,
        ).limit(20).all()
        for job_id, seller_id in candidates:
            claim = _try_operation_lock('wb-stock-seller', seller_id)
            if claim is None:
                continue
            try:
                handled = _advance(job_id, seller_id)
            finally:
                claim.close()
                db.session.remove()
            if handled:
                return


def _advance(job_id, seller_id):
    now = datetime.utcnow()
    job = BackgroundJob.query.filter_by(id=job_id, seller_id=seller_id, job_type=JOB_TYPE).first()
    if job is None or job.status not in ('pending', 'running'):
        return False
    progress = job.get_progress()
    retry_at = progress.get('retry_at')
    if retry_at and datetime.fromisoformat(retry_at) > now:
        # Let other sellers progress; retry_at does not trigger blocking sleep.
        job.updated_at = now
        db.session.commit()
        return False
    try:
        key = _owned_key(seller_id)
        key_hash = hashlib.sha256(key.encode()).hexdigest()
        if progress.get('targets') and (
            progress.get('key_hash') != key_hash
            or datetime.fromisoformat(progress['batch_started']) < now - timedelta(minutes=15)
        ):
            progress = {name: progress[name] for name in ('cursor', 'last_id')}
        if not progress.get('targets'):
            targets = db.session.query(Product.id, Product.nm_id).filter(
                Product.seller_id == seller_id, Product.nm_id > 0,
                Product.id > progress['cursor'], Product.id <= progress['last_id'],
            ).order_by(Product.id).limit(BATCH_SIZE).all()
            if not targets:
                job.status = 'completed'
                job.set_progress({'cursor': progress['last_id'], 'last_id': progress['last_id']})
                job.updated_at = now
                db.session.commit()
                return True
            progress.update(targets=[list(pair) for pair in targets], rows=[], key_hash=key_hash, batch_started=now.isoformat())
        targets = progress['targets']
        nm_ids = [pair[1] for pair in targets]
        previous = progress.get('rows', [])
        job.status = 'running'
        job.error_message = None
        job.set_progress(progress)
        db.session.commit()  # Claim/progress durable; no write lock through I/O.

        with WildberriesAPIClient(key, max_retries=0, timeout=15) as client:
            page = client.get_stocks_page(nm_ids, offset=len(previous))
        # Validate even synthetic adapters and buffered rows on resume.
        page = normalize_stock_page({'data': {'items': page}}, nm_ids=nm_ids)
        previous = normalize_stock_page({'data': {'items': previous}}, nm_ids=nm_ids, limit=25000)
        rows = combine_stock_pages(previous, page)

        db.session.expire_all()
        if hashlib.sha256(_owned_key(seller_id).encode()).hexdigest() != key_hash:
            raise WBStockSyncError('Ключ WB изменился во время обновления. Запустите синхронизацию заново.')
        job = BackgroundJob.query.filter_by(id=job_id, seller_id=seller_id, status='running').one()
        if len(page) < PAGE_SIZE:
            _replace_complete_batch(seller_id, targets, rows, now)
            job.processed += len(targets)
            job.succeeded += len(targets)
            progress = {'cursor': targets[-1][0], 'last_id': progress['last_id']}
            if progress['cursor'] >= progress['last_id']:
                job.status = 'completed'
            else:
                job.status = 'pending'
        else:
            progress['rows'] = rows
            progress.pop('retry_at', None)
        job.set_progress(progress)
        job.updated_at = now
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        job = BackgroundJob.query.filter_by(id=job_id, seller_id=seller_id, job_type=JOB_TYPE).first()
        if job is None:
            return
        if isinstance(exc, WBRateLimitException):
            progress['retry_at'] = (now + timedelta(seconds=max(60, min(exc.retry_after or 60, 3600)))).isoformat()
            job.set_progress(progress)
            job.error_message = 'Wildberries ограничил частоту запросов. Обновление продолжится автоматически; последние остатки сохранены.'
        else:
            job.status = 'failed'
            job.failed_count = 1
            if isinstance(exc, WBStockAccessError):
                job.error_message = str(exc)
            elif isinstance(exc, WBAuthException):
                job.error_message = ACCESS_MESSAGE
            elif isinstance(exc, WBStockSyncError):
                job.error_message = str(exc)
            else:
                job.error_message = 'Не удалось подтвердить полный ответ WB по остаткам. Последние данные сохранены; повторите обновление позже.'
            job.set_progress({name: progress.get(name, 0) for name in ('cursor', 'last_id')})
            job.set_result({'code': 'wb_stock_access_required' if isinstance(exc, (WBStockAccessError, WBAuthException)) else 'wb_stock_read_failed'})
            logger.info('WB stocks stopped for seller=%s: %s', seller_id, type(exc).__name__)
        job.updated_at = now
        db.session.commit()
    return True
