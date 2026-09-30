"""Durable, seller-scoped, read-only Ozon warehouse and exact FBS refreshes.

HTTP only enqueues. One existing requested-read scheduler slot advances one
bounded page. Staging is private; last-good projections change only at the
observed end of pagination, in the same transaction as job completion.
"""

from datetime import datetime, timedelta
import hashlib
import json
import math
import time
import uuid
from types import SimpleNamespace

from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError

from models import (
    Marketplace, MarketplaceListing, MarketplaceWarehouse,
    MarketplaceWarehouseReadItem, MarketplaceWarehouseReadJob,
    MarketplaceWarehouseStock, MarketplaceWarehouseSync,
    SellerMarketplaceAccount, db,
)
from services.marketplace_accounts import MarketplaceAccountNotFound, MarketplaceAccountService
from services.marketplace_adapters import MarketplaceCredentials
from services.marketplace_adapters.ozon import OzonAdapter
from services.marketplace_credential_identity import ozon_credential_fingerprint
from services.marketplace_operation_locks import try_account_operation_lock
from services.marketplace_warehouses import MarketplaceWarehouseService
from services.ozon_api_client import (
    OZON_ENDPOINTS, OzonAPIError, OzonAuthError, OzonProtocolError,
    OzonRateLimitError, OzonSellerAPIClient,
)
from services.ozon_read_response import bound_read_responses, OzonReadResponseTooLarge
from services.ozon_commercial_contracts import (
    OzonCommercialContractError, OzonStockContract, OzonWarehouseContract,
)


ACTIVE = ('queued', 'running', 'waiting_provider')
TERMINAL = ('waiting_access', 'completed', 'failed', 'cancelled')
MAX_CALLS_PER_STEP = 12
CALL_START_SECONDS = 45
LEASE_SECONDS = 120
MAX_PAGES = 100
MAX_ROWS = 10_000
MAX_STAGED_BYTES = 8 * 1024 * 1024
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_ACTIVE_JOBS = 1000
MAX_STAGING_JOBS = 8  # 8 x per-job 8 MiB / 10k rows => 64 MiB / 80k rows
MAX_FAILURES = 8
MAX_AGE = timedelta(hours=24)
MAX_BACKOFF_SECONDS = 6 * 3600

MESSAGES = {
    'queued': 'Обновление поставлено в очередь. Последние полные данные остаются доступны.',
    'running': 'Читаем страницы Ozon. Последние полные данные остаются доступны.',
    'waiting_provider': 'Чтение отложено; продолжим после указанного времени.',
    'waiting_access': 'Ozon не разрешил чтение. Проверьте права и ключ кабинета.',
    'completed': 'Полный снимок получен и применён.',
    'failed': 'Не удалось получить полный снимок. Последние полные данные сохранены.',
    'cancelled': 'Локальное продолжение отменено. Последние полные данные сохранены.',
    'provider_rate_limited': 'Ozon ограничил частоту. Продолжим после указанного времени.',
    'provider_unavailable': 'Ozon временно недоступен. Продолжим автоматически.',
    'read_budget_exhausted': 'Бюджет чтения исчерпан. Продолжим позже.',
    'read_capacity_busy': 'Очередь чтения занята. Повторим без потери последнего снимка.',
    'account_busy': 'Кабинет занят другой операцией. Продолжим позже.',
    'access_denied': 'Ozon отклонил доступ к чтению. Проверьте права API key.',
    'credentials_changed': 'Ключ кабинета изменился. Запустите новое обновление.',
    'account_unavailable': 'Кабинет отключён или подключение не подтверждено.',
    'identity_changed': 'Идентификатор товара изменился. Запустите новое обновление.',
    'unknown_warehouse': 'Ozon вернул склад вне сохранённого списка. Обновите склады.',
    'invalid_snapshot': 'Ozon вернул неполный или противоречивый ответ.',
    'response_too_large': 'Ответ Ozon превысил безопасный предел.',
    'job_expired': 'Обновление не завершилось за сутки. Запустите новое.',
    'retry_exhausted': 'Автоматические попытки исчерпаны. Запустите новое обновление.',
    'retry_delay_out_of_range': 'Ozon указал непредставимую паузу. Повторите позже.',
    'provider_cooldown_exceeds_job_age': 'Пауза Ozon длиннее срока этой заявки. Повторите после указанного времени.',
    'warehouse_catalog_required': 'Сначала загрузите список складов этого кабинета.',
}


class WarehouseReadError(RuntimeError):
    status_code = 400
    code = 'invalid_marketplace_warehouse_request'

    def __init__(self, message=None, *, next_attempt_at=None):
        super().__init__(message or MESSAGES.get(self.code, MESSAGES['failed']))
        self.next_attempt_at = next_attempt_at


class WarehouseReadNotFound(WarehouseReadError):
    status_code = 404
    code = 'marketplace_warehouse_not_found'


class WarehouseReadConflict(WarehouseReadError):
    status_code = 409
    code = 'warehouse_catalog_required'


class WarehouseReadCooldown(WarehouseReadError):
    status_code = 429
    code = 'provider_rate_limited'


class WarehouseReadCapacity(WarehouseReadError):
    status_code = 503
    code = 'read_capacity_busy'


class _PageFailure(RuntimeError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


class _ReadClient(OzonSellerAPIClient):
    """Use the existing rate ledger and only allow bounded read endpoints."""

    def __init__(self, credentials):
        super().__init__(credentials, timeout=(3.0, 6.0), read_retries=0)
        self.calls = 0
        self.deadline = time.monotonic() + CALL_START_SECONDS
        bound_read_responses(self.session, maximum_bytes=MAX_RESPONSE_BYTES)

    def request(self, endpoint_name, payload):
        spec = OZON_ENDPOINTS.get(endpoint_name)
        if endpoint_name not in ('warehouses', 'product_stocks_by_warehouse_fbs') or spec is None or spec.retry_class != 'read':
            raise _PageFailure('invalid_snapshot')
        if self.calls >= MAX_CALLS_PER_STEP or time.monotonic() >= self.deadline:
            raise _PageFailure('read_budget_exhausted')
        self.calls += 1
        return super().request(endpoint_name, payload)


class _ReadAdapter(OzonAdapter):
    def __init__(self, credentials):
        super().__init__()
        self.client = _ReadClient(credentials)

    def _client(self, credentials):
        if credentials != self.client._credentials:
            raise _PageFailure('credentials_changed')
        return self.client

    def close(self):
        self.client.session.close()


def _now(now):
    return now or datetime.utcnow()


def _iso(value):
    return value.isoformat() + 'Z' if value else None


def _scope_query(seller_id, kind, account_id, listing_id=None):
    return MarketplaceWarehouseReadJob.query.filter_by(
        seller_id=seller_id, kind=kind, account_id=account_id, listing_id=listing_id,
    )


def _owned_account(seller_id, account_id):
    try:
        return MarketplaceAccountService.get_owned_account(
            seller_id=seller_id, account_id=account_id, marketplace_code='ozon',
        )
    except MarketplaceAccountNotFound:
        raise WarehouseReadNotFound('Кабинет Ozon не найден') from None


def _owned_listing(seller_id, listing_id):
    listing = MarketplaceListing.query.join(Marketplace).filter(
        MarketplaceListing.id == listing_id,
        MarketplaceListing.seller_id == seller_id,
        Marketplace.code == 'ozon',
    ).first()
    if listing is None or listing.account_id is None:
        raise WarehouseReadNotFound('Товар Ozon не найден')
    return listing


def _account_ready(account, now):
    return bool(
        account.is_active and account.marketplace.is_active
        and account.connection_status == 'connected' and account.has_credentials
        and (account.credential_expires_at is None or account.credential_expires_at > now)
    )


def _last_good(kind, seller_id, account_id, listing_id):
    prior = _scope_query(seller_id, kind, account_id, listing_id).filter_by(status='completed').order_by(
        MarketplaceWarehouseReadJob.id.desc(),
    ).first()
    observed = prior.completed_at if prior else None
    if kind == 'warehouses':
        legacy = db.session.query(func.max(MarketplaceWarehouseSync.completed_at)).filter_by(
            seller_id=seller_id, account_id=account_id, status='completed',
        ).scalar()
    else:
        legacy = db.session.query(func.max(MarketplaceWarehouseStock.observed_at)).filter_by(
            seller_id=seller_id, account_id=account_id, listing_id=listing_id,
        ).scalar()
    return max((v for v in (observed, legacy) if v is not None), default=None)


def public_job(job):
    if job is None:
        return None
    code = job.error_code if job.error_code in MESSAGES else None
    return {
        'id': job.id,
        'kind': job.kind,
        'account_id': job.account_id,
        'listing_id': job.listing_id if job.kind == 'fbs_stock' else None,
        'status': job.status,
        'code': code,
        'message': MESSAGES.get(code or job.status, MESSAGES['failed']),
        'active': job.status in ACTIVE,
        'next_attempt_at': _iso(job.next_due_at if job.status in ACTIVE else job.cooldown_until),
        'pages_loaded': job.page_count,
        'requested_at': _iso(job.requested_at),
        'completed_at': _iso(job.completed_at),
        'last_completed_at': _iso(job.last_completed_at),
        'snapshot_id': (job.warehouse_sync_id if job.kind == 'warehouses' else job.id)
        if job.status == 'completed' else None,
    }


def _latest(seller_id, kind, account_id, listing_id=None):
    return _scope_query(seller_id, kind, account_id, listing_id).order_by(
        MarketplaceWarehouseReadJob.id.desc(),
    ).first()


def status_for_scope(*, seller_id, kind, account_id=None, listing_id=None):
    if kind == 'warehouses':
        _owned_account(seller_id, account_id)
    elif kind == 'fbs_stock':
        listing = _owned_listing(seller_id, listing_id)
        if account_id is not None and account_id != listing.account_id:
            raise WarehouseReadNotFound()
        account_id = listing.account_id
    else:
        raise WarehouseReadError()
    return public_job(_latest(seller_id, kind, account_id, listing_id))


def status_for_job(*, seller_id, job_id):
    if type(job_id) is not int or job_id <= 0:
        raise WarehouseReadNotFound()
    row = MarketplaceWarehouseReadJob.query.filter_by(seller_id=seller_id, id=job_id).first()
    if row is None:
        raise WarehouseReadNotFound()
    return public_job(row)


def _cooldown(account_id, now):
    return db.session.query(func.max(MarketplaceWarehouseReadJob.cooldown_until)).filter(
        MarketplaceWarehouseReadJob.account_id == account_id,
        MarketplaceWarehouseReadJob.cooldown_until > now,
    ).scalar()


def enqueue_refresh(*, seller_id, kind, account_id=None, listing_id=None, now=None):
    """Never decrypt credentials or call Ozon on the HTTP path."""
    now = _now(now)
    if type(seller_id) is not int or seller_id <= 0:
        raise WarehouseReadNotFound()
    if kind == 'warehouses':
        if type(account_id) is not int or account_id <= 0 or listing_id is not None:
            raise WarehouseReadError()
        account = _owned_account(seller_id, account_id)
        listing = None
    elif kind == 'fbs_stock':
        if type(listing_id) is not int or listing_id <= 0:
            raise WarehouseReadError()
        listing = _owned_listing(seller_id, listing_id)
        account_id = listing.account_id
        account = _owned_account(seller_id, account_id)
        has_catalog = MarketplaceWarehouseSync.query.filter_by(
            seller_id=seller_id, account_id=account_id, status='completed',
        ).first() is not None or MarketplaceWarehouse.query.filter_by(
            seller_id=seller_id, account_id=account_id,
        ).first() is not None
        if not has_catalog:
            raise WarehouseReadConflict()
    else:
        raise WarehouseReadError()
    if not _account_ready(account, now):
        error = WarehouseReadError(MESSAGES['account_unavailable'])
        error.status_code = 409
        error.code = 'account_unavailable'
        raise error
    marker = ozon_credential_fingerprint(account)
    existing = _scope_query(seller_id, kind, account_id, listing_id).filter(
        MarketplaceWarehouseReadJob.status.in_(ACTIVE),
    ).first()
    if existing and existing.credential_fingerprint == marker and (
        listing is None or (existing.offer_id == listing.offer_id and existing.external_product_id == listing.external_product_id)
    ):
        return public_job(existing)
    if existing:
        existing.status = 'failed'
        existing.error_code = ('credentials_changed' if existing.credential_fingerprint != marker else 'identity_changed')
        existing.lease_token = None
        existing.lease_expires_at = None
        existing.completed_at = now
        db.session.flush()
    cooldown = _cooldown(account_id, now)
    if cooldown:
        db.session.commit()
        raise WarehouseReadCooldown(next_attempt_at=_iso(cooldown))
    job = MarketplaceWarehouseReadJob(
        seller_id=seller_id, marketplace_id=account.marketplace_id,
        account_id=account_id, kind=kind, listing_id=listing_id,
        external_account_id=account.external_account_id,
        offer_id=listing.offer_id if listing else None,
        external_product_id=listing.external_product_id if listing else None,
        credential_fingerprint=marker, status='queued', next_due_at=now,
        last_completed_at=_last_good(kind, seller_id, account_id, listing_id),
        requested_at=now, created_at=now, updated_at=now,
    )
    db.session.add(job)
    try:
        # Flush takes the SQLite writer slot before the global count. Concurrent
        # enqueues cannot both admit the 1001st active job.
        db.session.flush()
    except IntegrityError:
        db.session.rollback()
        current = _scope_query(seller_id, kind, account_id, listing_id).filter(
            MarketplaceWarehouseReadJob.status.in_(ACTIVE),
        ).first()
        if current is None:
            raise
        return public_job(current)
    active_count = db.session.query(func.count(MarketplaceWarehouseReadJob.id)).filter(
        MarketplaceWarehouseReadJob.status.in_(ACTIVE),
    ).scalar()
    if active_count > MAX_ACTIVE_JOBS:
        db.session.rollback()
        raise WarehouseReadCapacity()
    # Retain a bounded seller/scope history. Staging itself is removed on every
    # terminal transition; old terminal job rows are safe to prune.
    old_ids = [row.id for row in _scope_query(seller_id, kind, account_id, listing_id).filter(
        MarketplaceWarehouseReadJob.status.in_(TERMINAL),
    ).order_by(MarketplaceWarehouseReadJob.id.desc()).offset(20).limit(100).all()]
    if old_ids:
        MarketplaceWarehouseReadJob.query.filter(MarketplaceWarehouseReadJob.id.in_(old_ids)).delete(synchronize_session=False)
    aged_ids = [row.id for row in MarketplaceWarehouseReadJob.query.filter(
        MarketplaceWarehouseReadJob.status.in_(TERMINAL),
        MarketplaceWarehouseReadJob.completed_at < now - timedelta(days=30),
    ).order_by(MarketplaceWarehouseReadJob.id).limit(100).all()]
    if aged_ids:
        MarketplaceWarehouseReadJob.query.filter(MarketplaceWarehouseReadJob.id.in_(aged_ids)).delete(synchronize_session=False)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        current = _scope_query(seller_id, kind, account_id, listing_id).filter(
            MarketplaceWarehouseReadJob.status.in_(ACTIVE),
        ).first()
        if current is None:
            raise
        return public_job(current)
    return public_job(job)


def cancel_refresh(*, seller_id, job_id, now=None):
    status_for_job(seller_id=seller_id, job_id=job_id)
    now = _now(now)
    changed = MarketplaceWarehouseReadJob.query.filter_by(id=job_id, seller_id=seller_id).filter(
        MarketplaceWarehouseReadJob.status.in_(ACTIVE),
    ).update({
        'status': 'cancelled', 'error_code': None, 'lease_token': None,
        'lease_expires_at': None, 'completed_at': now, 'updated_at': now,
    }, synchronize_session=False)
    if changed:
        MarketplaceWarehouseReadItem.query.filter_by(job_id=job_id).delete(synchronize_session=False)
    db.session.commit()
    return status_for_job(seller_id=seller_id, job_id=job_id)


def due_candidate(*, now=None, exclude_account_ids=()):
    """SQL due filter precedes LIMIT; returns only public scheduling coordinates."""
    now = _now(now)
    query = MarketplaceWarehouseReadJob.query.filter(
        MarketplaceWarehouseReadJob.status.in_(ACTIVE),
        MarketplaceWarehouseReadJob.next_due_at <= now,
        or_(MarketplaceWarehouseReadJob.cooldown_until.is_(None), MarketplaceWarehouseReadJob.cooldown_until <= now),
        or_(MarketplaceWarehouseReadJob.lease_expires_at.is_(None), MarketplaceWarehouseReadJob.lease_expires_at <= now),
    )
    if exclude_account_ids:
        query = query.filter(~MarketplaceWarehouseReadJob.account_id.in_(list(exclude_account_ids)))
    row = query.order_by(
        MarketplaceWarehouseReadJob.next_due_at.asc(),
        MarketplaceWarehouseReadJob.last_attempt_at.asc(),
        MarketplaceWarehouseReadJob.id.asc(),
    ).with_entities(
        MarketplaceWarehouseReadJob.id,
        MarketplaceWarehouseReadJob.account_id,
        MarketplaceWarehouseReadJob.next_due_at,
        MarketplaceWarehouseReadJob.last_attempt_at,
    ).first()
    return dict(id=row.id, account_id=row.account_id, next_due_at=row.next_due_at,
                last_attempt_at=row.last_attempt_at) if row else None


def _claim(job_id, now):
    token = uuid.uuid4().hex
    changed = MarketplaceWarehouseReadJob.query.filter(
        MarketplaceWarehouseReadJob.id == job_id,
        MarketplaceWarehouseReadJob.status.in_(ACTIVE),
        MarketplaceWarehouseReadJob.next_due_at <= now,
        or_(MarketplaceWarehouseReadJob.cooldown_until.is_(None), MarketplaceWarehouseReadJob.cooldown_until <= now),
        or_(MarketplaceWarehouseReadJob.lease_expires_at.is_(None), MarketplaceWarehouseReadJob.lease_expires_at <= now),
    ).update({
        'status': 'running', 'lease_token': token,
        'lease_expires_at': now + timedelta(seconds=LEASE_SECONDS),
        'next_due_at': now + timedelta(seconds=LEASE_SECONDS),
        'last_attempt_at': now, 'updated_at': now,
    }, synchronize_session=False)
    db.session.commit()
    return token if changed == 1 else None


def _lease_query(job_id, token):
    return MarketplaceWarehouseReadJob.query.filter_by(id=job_id, lease_token=token, status='running')


def _capacity_available(*, current_job, now):
    """Under a SQLite writer transaction, evict only a long-paused private stage.

    The victim's provider deadline, failure count and last-good projection stay
    unchanged. Its next due run restarts from page one with the same credentials.
    """
    if current_job.staged_count > 0:
        return True
    holders = db.session.query(func.count(MarketplaceWarehouseReadJob.id)).filter(
        MarketplaceWarehouseReadJob.status.in_(ACTIVE),
        MarketplaceWarehouseReadJob.staged_count > 0,
    ).scalar()
    if holders < MAX_STAGING_JOBS:
        return True
    victims = MarketplaceWarehouseReadJob.query.filter(
        MarketplaceWarehouseReadJob.id != current_job.id,
        MarketplaceWarehouseReadJob.account_id != current_job.account_id,
        MarketplaceWarehouseReadJob.status == 'waiting_provider',
        MarketplaceWarehouseReadJob.staged_count > 0,
        MarketplaceWarehouseReadJob.next_due_at > now + timedelta(minutes=5),
        or_(MarketplaceWarehouseReadJob.lease_expires_at.is_(None),
            MarketplaceWarehouseReadJob.lease_expires_at <= now),
    ).order_by(
        MarketplaceWarehouseReadJob.next_due_at.desc(),
        MarketplaceWarehouseReadJob.id.asc(),
    ).limit(MAX_STAGING_JOBS).all()
    for victim in victims:
        account_claim = try_account_operation_lock(victim.account_id)
        if account_claim is None:
            continue
        try:
            changed = MarketplaceWarehouseReadJob.query.filter(
                MarketplaceWarehouseReadJob.id == victim.id,
                MarketplaceWarehouseReadJob.status == 'waiting_provider',
                MarketplaceWarehouseReadJob.staged_count == victim.staged_count,
                MarketplaceWarehouseReadJob.staged_bytes == victim.staged_bytes,
                MarketplaceWarehouseReadJob.page_count == victim.page_count,
                MarketplaceWarehouseReadJob.next_due_at == victim.next_due_at,
                MarketplaceWarehouseReadJob.lease_token == victim.lease_token,
                or_(MarketplaceWarehouseReadJob.lease_expires_at.is_(None),
                    MarketplaceWarehouseReadJob.lease_expires_at <= now),
            ).update({
                'staged_count': 0, 'staged_bytes': 0, 'page_count': 0,
                'next_cursor': None, 'seen_cursor_hashes_json': '[]',
                'lease_token': None, 'lease_expires_at': None, 'updated_at': now,
            }, synchronize_session=False)
            if changed == 1:
                MarketplaceWarehouseReadItem.query.filter_by(job_id=victim.id).delete(synchronize_session=False)
                return True
        finally:
            account_claim.close()
    return False


def _preflight_capacity(job_id, token, now):
    """Avoid a provider read when the bounded staging pool cannot accept it."""
    db.session.rollback()
    db.session.connection().exec_driver_sql('BEGIN IMMEDIATE')
    try:
        job = _lease_query(job_id, token).first()
        available = bool(job and _capacity_available(current_job=job, now=now))
        db.session.commit()
        return available
    except Exception:
        db.session.rollback()
        raise


def _finish(job_id, token, now, *, status, code=None, due=None, cooldown=None, failures=None):
    values = {
        'status': status, 'error_code': code,
        'next_due_at': due or now, 'cooldown_until': cooldown,
        'lease_token': None, 'lease_expires_at': None, 'updated_at': now,
    }
    if status in TERMINAL:
        values['completed_at'] = now
    if failures is not None:
        values['failure_count'] = failures
    changed = _lease_query(job_id, token).update(values, synchronize_session=False)
    if changed and status in TERMINAL:
        MarketplaceWarehouseReadItem.query.filter_by(job_id=job_id).delete(synchronize_session=False)
    db.session.commit()
    return changed == 1


def _reground(job, now):
    db.session.expire_all()
    account = _owned_account(job.seller_id, job.account_id)
    if not _account_ready(account, now):
        raise _PageFailure('account_unavailable')
    if account.external_account_id != job.external_account_id or ozon_credential_fingerprint(account) != job.credential_fingerprint:
        raise _PageFailure('credentials_changed')
    listing = None
    if job.kind == 'fbs_stock':
        try:
            listing = _owned_listing(job.seller_id, job.listing_id)
        except WarehouseReadNotFound:
            raise _PageFailure('identity_changed') from None
        if (listing.account_id != job.account_id or listing.marketplace_id != job.marketplace_id
                or listing.offer_id != job.offer_id or listing.external_product_id != job.external_product_id):
            raise _PageFailure('identity_changed')
    return account, listing


def _page(job, adapter, credentials):
    cursor = job.next_cursor or ''
    if job.kind == 'warehouses':
        return OzonWarehouseContract.normalize_page(adapter.read_warehouses(
            credentials, OzonWarehouseContract.request_payload(cursor=cursor),
        )), 'warehouses'
    return OzonStockContract.normalize_fbs_page(adapter.read_stocks_by_warehouse_fbs(
        credentials, {'limit': 100, 'cursor': cursor, 'offer_id': [job.offer_id]},
    )), 'products'


def _checkpoint(job_id, token, page, item_key, now):
    # The page was normalized outside any DB transaction. BEGIN IMMEDIATE
    # serializes the global staging admission check across different accounts.
    db.session.rollback()
    db.session.connection().exec_driver_sql('BEGIN IMMEDIATE')
    job = _lease_query(job_id, token).first()
    if job is None:
        db.session.rollback()
        return False, False
    if job.page_count >= MAX_PAGES:
        raise _PageFailure('response_too_large')
    items = page[item_key]
    if job.staged_count + len(items) > MAX_ROWS:
        raise _PageFailure('response_too_large')
    serialized = [(item['warehouse_id'], MarketplaceWarehouseService._stable_json(item)) for item in items]
    added_bytes = sum(len(payload.encode('utf-8')) for _, payload in serialized)
    if job.staged_bytes + added_bytes > MAX_STAGED_BYTES:
        raise _PageFailure('response_too_large')
    if job.staged_count == 0 and items and not _capacity_available(current_job=job, now=now):
        raise _PageFailure('read_capacity_busy')
    ids = [external_id for external_id, _ in serialized]
    if ids and MarketplaceWarehouseReadItem.query.filter(
        MarketplaceWarehouseReadItem.job_id == job_id,
        MarketplaceWarehouseReadItem.external_warehouse_id.in_(ids),
    ).first() is not None:
        raise _PageFailure('invalid_snapshot')
    try:
        seen = json.loads(job.seen_cursor_hashes_json or '[]')
    except (TypeError, ValueError):
        raise _PageFailure('invalid_snapshot') from None
    if not isinstance(seen, list) or len(seen) > MAX_PAGES:
        raise _PageFailure('invalid_snapshot')
    if page['has_next']:
        cursor = page['cursor']
        digest = hashlib.sha256(cursor.encode('utf-8')).hexdigest()
        if cursor == (job.next_cursor or '') or digest in seen:
            raise _PageFailure('invalid_snapshot')
        if job.page_count + 1 >= MAX_PAGES:
            raise _PageFailure('response_too_large')
        seen.append(digest)
    else:
        cursor = None
    for external_id, payload in serialized:
        db.session.add(MarketplaceWarehouseReadItem(
            job_id=job_id, external_warehouse_id=external_id,
            item_kind=job.kind, normalized_json=payload,
            normalized_bytes=len(payload.encode('utf-8')),
            fingerprint=hashlib.sha256(payload.encode('utf-8')).hexdigest(),
            observed_at=now,
        ))
    changed = _lease_query(job_id, token).update({
        'page_count': job.page_count + 1,
        'staged_count': job.staged_count + len(items),
        'staged_bytes': job.staged_bytes + added_bytes,
        'next_cursor': cursor,
        'seen_cursor_hashes_json': json.dumps(seen, separators=(',', ':')),
        'next_due_at': now,
        'updated_at': now,
    }, synchronize_session=False)
    if changed != 1:
        db.session.rollback()
        return False, False
    db.session.commit()
    return True, not page['has_next']


def _staged(job):
    rows = MarketplaceWarehouseReadItem.query.filter_by(job_id=job.id).order_by(
        MarketplaceWarehouseReadItem.id,
    ).all()
    if len(rows) != job.staged_count:
        raise _PageFailure('invalid_snapshot')
    result = {}
    total = 0
    for row in rows:
        if row.item_kind != job.kind:
            raise _PageFailure('invalid_snapshot')
        payload = row.normalized_json
        if row.normalized_bytes != len(payload.encode('utf-8')) or row.fingerprint != hashlib.sha256(payload.encode('utf-8')).hexdigest():
            raise _PageFailure('invalid_snapshot')
        try:
            item = json.loads(payload)
        except (TypeError, ValueError):
            raise _PageFailure('invalid_snapshot') from None
        if (not isinstance(item, dict) or item.get('warehouse_id') != row.external_warehouse_id
                or row.external_warehouse_id in result or row.observed_at is None):
            raise _PageFailure('invalid_snapshot')
        result[row.external_warehouse_id] = (item, row.observed_at)
        total += row.normalized_bytes
    if total != job.staged_bytes or len(result) > MAX_ROWS or total > MAX_STAGED_BYTES:
        raise _PageFailure('invalid_snapshot')
    return result


def _apply_warehouses(job, items, now):
    existing = {row.external_warehouse_id: row for row in MarketplaceWarehouse.query.filter_by(
        seller_id=job.seller_id, marketplace_id=job.marketplace_id, account_id=job.account_id,
    ).all()}
    run = MarketplaceWarehouseSync(
        seller_id=job.seller_id, marketplace_id=job.marketplace_id, account_id=job.account_id,
        status='completed', page_count=job.page_count, seen_count=len(items),
        started_at=job.requested_at, completed_at=now, created_at=now, updated_at=now,
    )
    db.session.add(run)
    created = updated = unavailable = 0
    for external_id, (item, observed_at) in items.items():
        row = existing.get(external_id)
        fingerprint = MarketplaceWarehouseService._fingerprint(item)
        if row is None:
            row = MarketplaceWarehouse(
                seller_id=job.seller_id, marketplace_id=job.marketplace_id,
                account_id=job.account_id, external_warehouse_id=external_id,
                name=item['name'], sync_fingerprint=fingerprint,
                last_seen_at=observed_at, last_synced_at=observed_at,
            )
            db.session.add(row)
            created += 1
        else:
            updated += int(row.sync_fingerprint != fingerprint or not row.is_available)
        row.name = item['name']
        row.status = item.get('status')
        row.warehouse_type = item.get('warehouse_type')
        row.carriage_label_type = item.get('carriage_label_type')
        row.flags_json = MarketplaceWarehouseService._stable_json(item.get('flags', {}))
        row.limits_json = MarketplaceWarehouseService._stable_json(item.get('limits', {}))
        row.is_available = True
        row.sync_fingerprint = fingerprint
        row.last_seen_at = observed_at
        row.last_synced_at = observed_at
    for external_id, row in existing.items():
        if external_id not in items and row.is_available:
            row.is_available = False
            row.last_synced_at = now  # absence is established only at complete pagination
            unavailable += 1
    run.created_count = created
    run.updated_count = updated
    run.unavailable_count = unavailable
    db.session.flush()
    return run.id


def _apply_fbs(job, items, now):
    warehouses = {row.external_warehouse_id: row for row in MarketplaceWarehouse.query.filter_by(
        seller_id=job.seller_id, marketplace_id=job.marketplace_id,
        account_id=job.account_id, is_available=True,
    ).all()}
    if set(items) - set(warehouses):
        raise _PageFailure('unknown_warehouse')
    existing_rows = db.session.query(
        MarketplaceWarehouseStock, MarketplaceWarehouse.external_warehouse_id,
    ).join(
        MarketplaceWarehouse, MarketplaceWarehouseStock.warehouse_id == MarketplaceWarehouse.id,
    ).filter(
        MarketplaceWarehouseStock.seller_id == job.seller_id,
        MarketplaceWarehouseStock.marketplace_id == job.marketplace_id,
        MarketplaceWarehouseStock.account_id == job.account_id,
        MarketplaceWarehouseStock.listing_id == job.listing_id,
    ).all()
    existing = {external_id: row for row, external_id in existing_rows}
    for external_id, (item, observed_at) in items.items():
        if item.get('offer_id') != job.offer_id or item.get('product_id') != job.external_product_id:
            raise _PageFailure('invalid_snapshot')
        row = existing.get(external_id)
        if row is None:
            row = MarketplaceWarehouseStock(
                seller_id=job.seller_id, marketplace_id=job.marketplace_id,
                account_id=job.account_id, listing_id=job.listing_id,
                warehouse_id=warehouses[external_id].id, offer_id=job.offer_id,
                external_product_id=job.external_product_id,
                sku=item['sku'], present=item['present'], reserved=item['reserved'],
                free_stock=item['free_stock'], is_available=True,
                sync_fingerprint=MarketplaceWarehouseService._fingerprint(item),
                observed_at=observed_at,
            )
            db.session.add(row)
        else:
            row.offer_id = job.offer_id
            row.external_product_id = job.external_product_id
            row.sku = item['sku']
            row.present = item['present']
            row.reserved = item['reserved']
            row.free_stock = item['free_stock']
            row.is_available = True
            row.sync_fingerprint = MarketplaceWarehouseService._fingerprint(item)
            row.observed_at = observed_at
    for external_id, row in existing.items():
        if external_id not in items:
            row.is_available = False
            row.observed_at = now  # complete empty/missing set observed at final boundary


def _finalize(job_id, token, now):
    job = _lease_query(job_id, token).first()
    if job is None:
        db.session.rollback()
        return False
    _reground(job, now)
    items = _staged(job)
    if job.kind == 'warehouses':
        sync_id = _apply_warehouses(job, items, now)
    else:
        _apply_fbs(job, items, now)
        sync_id = None
    changed = _lease_query(job_id, token).update({
        'status': 'completed', 'error_code': None, 'completed_at': now,
        'last_completed_at': now, 'warehouse_sync_id': sync_id,
        'lease_token': None, 'lease_expires_at': None,
        'cooldown_until': None, 'updated_at': now,
    }, synchronize_session=False)
    if changed != 1:
        db.session.rollback()
        return False
    MarketplaceWarehouseReadItem.query.filter_by(job_id=job_id).delete(synchronize_session=False)
    db.session.commit()
    return True


def _failure(job_id, token, now, error):
    db.session.rollback()
    job = _lease_query(job_id, token).first()
    if job is None:
        return 'skipped'
    if isinstance(error, _PageFailure):
        code = error.code
    elif isinstance(error, OzonReadResponseTooLarge):
        code = 'response_too_large'
    elif isinstance(error, OzonAuthError) or isinstance(error, OzonAPIError) and error.status_code in (401, 403):
        code = 'access_denied'
    elif isinstance(error, OzonRateLimitError) or isinstance(error, OzonAPIError) and error.status_code == 429:
        code = 'provider_rate_limited'
    elif isinstance(error, OzonAPIError) and error.code == 'ozon_read_budget':
        code = 'read_budget_exhausted'
    elif isinstance(error, OzonAPIError) and error.retriable:
        code = 'provider_unavailable'
    elif isinstance(error, (OzonProtocolError, OzonCommercialContractError, OzonAPIError, ValueError, IntegrityError)):
        code = 'invalid_snapshot'
    else:
        code = 'provider_unavailable'
    if code in ('credentials_changed', 'account_unavailable', 'identity_changed', 'unknown_warehouse',
                'invalid_snapshot', 'response_too_large', 'retry_delay_out_of_range', 'job_expired'):
        _finish(job_id, token, now, status='failed', code=code)
        return 'failed'
    if code == 'access_denied':
        _finish(job_id, token, now, status='waiting_access', code=code)
        return 'waiting_access'
    if code == 'read_budget_exhausted':
        _finish(job_id, token, now, status='waiting_provider', code=code, due=now + timedelta(seconds=60))
        return 'waiting_provider'
    if code == 'read_capacity_busy':
        _finish(job_id, token, now, status='waiting_provider', code=code, due=now + timedelta(seconds=60))
        return 'waiting_provider'
    failures = job.failure_count + 1
    if code == 'provider_rate_limited':
        delay = getattr(error, 'retry_after', None)
        if delay is None:
            delay = 60
        if not isinstance(delay, (int, float)) or not math.isfinite(delay) or delay < 0 or delay > 10 * 365 * 86400:
            _finish(job_id, token, now, status='failed', code='retry_delay_out_of_range')
            return 'failed'
        due = now + timedelta(seconds=max(1, delay))
    else:
        # Stable jitter does not turn the 60s base retry into an early call.
        base = min(MAX_BACKOFF_SECONDS, 60 * (2 ** min(failures - 1, 8)))
        jitter = int(hashlib.sha256(f'{job_id}:{failures}'.encode()).hexdigest()[:4], 16) % 13
        due = now + timedelta(seconds=base + jitter)
    if code == 'provider_rate_limited' and due > job.requested_at + MAX_AGE:
        _finish(job_id, token, now, status='failed', code='provider_cooldown_exceeds_job_age',
                due=due, cooldown=due, failures=failures)
        return 'failed'
    if failures >= MAX_FAILURES or now >= job.requested_at + MAX_AGE:
        _finish(job_id, token, now, status='failed', code='retry_exhausted' if failures >= MAX_FAILURES else 'job_expired',
                due=due, cooldown=due if code == 'provider_rate_limited' else None, failures=failures)
        return 'failed'
    _finish(job_id, token, now, status='waiting_provider', code=code,
            due=due, cooldown=due if code == 'provider_rate_limited' else None, failures=failures)
    return 'waiting_provider'


def run_read_step(job_id, *, now=None, adapter_factory=None):
    """One page per 10s scheduler slot; no SQL write transaction spans HTTP."""
    supplied_now = now is not None
    now = _now(now)
    started = time.monotonic()
    if type(job_id) is not int or job_id <= 0:
        return {'selected': 0, 'outcome': 'skipped'}
    token = _claim(job_id, now)
    if token is None:
        return {'selected': 0, 'outcome': 'skipped'}
    job = MarketplaceWarehouseReadJob.query.filter_by(id=job_id).first()
    account_claim = try_account_operation_lock(job.account_id)
    if account_claim is None:
        _finish(job_id, token, now, status='waiting_provider', code='account_busy', due=now + timedelta(seconds=60))
        return {'selected': 0, 'outcome': 'skipped', 'code': 'account_busy'}
    adapter = None
    try:
        if now >= job.requested_at + MAX_AGE:
            raise _PageFailure('job_expired')
        account, _listing = _reground(job, now)
        # A completed final page is durable before projection apply. A crash in
        # that gap resumes from staging, without repeating page one or HTTP.
        if job.page_count > 0 and job.next_cursor is None:
            finished = (now + timedelta(seconds=time.monotonic() - started)) if supplied_now else datetime.utcnow()
            completed = _finalize(job_id, token, finished)
            return {'selected': int(completed), 'outcome': 'completed' if completed else 'skipped'}
        if job.staged_count == 0 and not _preflight_capacity(job_id, token, now):
            raise _PageFailure('read_capacity_busy')
        secret = account.get_credentials()
        credentials = MarketplaceCredentials(external_account_id=account.external_account_id, api_key=secret['api_key'])
        del secret
        adapter = adapter_factory(credentials) if adapter_factory else _ReadAdapter(credentials)
        page_job = SimpleNamespace(kind=job.kind, next_cursor=job.next_cursor, offer_id=job.offer_id)
        db.session.rollback()  # release any read transaction before network I/O
        page, key = _page(page_job, adapter, credentials)
        finished = (now + timedelta(seconds=time.monotonic() - started)) if supplied_now else datetime.utcnow()
        db.session.expire_all()
        job = _lease_query(job_id, token).first()
        if job is None:
            return {'selected': 0, 'outcome': 'skipped'}
        _reground(job, finished)
        if job.kind == 'fbs_stock':
            if any(item['offer_id'] != job.offer_id or item['product_id'] != job.external_product_id for item in page[key]):
                raise _PageFailure('invalid_snapshot')
        checkpointed, complete = _checkpoint(job_id, token, page, key, finished)
        if not checkpointed:
            return {'selected': 0, 'outcome': 'skipped'}
        if complete:
            completed = _finalize(job_id, token, finished)
            return {'selected': int(completed), 'outcome': 'completed' if completed else 'skipped'}
        _finish(job_id, token, finished, status='queued', due=finished)
        return {'selected': 1, 'outcome': 'running'}
    except Exception as exc:
        failure_time = (now + timedelta(seconds=time.monotonic() - started)) if supplied_now else datetime.utcnow()
        outcome = _failure(job_id, token, failure_time, exc)
        return {'selected': int(outcome != 'skipped'), 'outcome':
                'unavailable' if outcome in ('waiting_provider', 'waiting_access') else outcome,
                'code': getattr(exc, 'code', None)}
    finally:
        if adapter is not None:
            try:
                adapter.close()
            except Exception:
                pass
        account_claim.close()
