"""One durable, read-only path from Ozon credentials to a complete catalog.

HTTP only enqueues; the singleton scheduler advances one account/page per tick.
Catalog snapshots/cursors remain owned by MarketplaceListingService. No prices,
stocks, publications, global references or LLM writes belong to this workflow.
"""

from datetime import datetime, timedelta
import logging
import math
import time
import uuid

from sqlalchemy import case, func, or_, select, union_all

from models import BackgroundJob, MarketplaceCatalogSync, db
from services.marketplace_accounts import (
    MarketplaceAccountConflict, MarketplaceAccountError, MarketplaceAccountNotFound,
    MarketplaceAccountService, MarketplaceAccountValidationError,
)
from services.marketplace_adapters import MarketplaceCredentials
from services.marketplace_adapters.ozon import OzonAdapter
from services.marketplace_credential_identity import ozon_credential_fingerprint
from services.marketplace_listings import MarketplaceCatalogBusy, MarketplaceListingService
from services.marketplace_operation_locks import _try_operation_lock, try_account_operation_lock
from services.ozon_api_client import OZON_ENDPOINTS, OzonAPIError, OzonSellerAPIClient
from services.ozon_read_response import bound_read_responses


logger = logging.getLogger(__name__)
JOB_TYPE = 'ozon_account_sync'
ACTIVE = ('pending', 'running')
TICK_SECONDS = 10
MAX_CALLS_PER_TICK = 12
CALL_START_BUDGET_SECONDS = 45
MAX_READ_RESPONSE_BYTES = 64 * 1024 * 1024
MAX_TRANSIENT_FAILURES = 8
MAX_JOB_AGE = timedelta(hours=24)

MESSAGES = {
    'queued': 'Проверим подключение и загрузим каталог. Страницу можно закрыть.',
    'catalog_queued': 'Загрузим каталог из Ozon в фоне. Страницу можно закрыть.',
    'checking': 'Проверяем ключ и доступные права в Ozon…',
    'catalog': 'Загружаем товары, цены и остатки из Ozon…',
    'completed': 'Каталог загружен. Можно перейти к товарам или подготовить новые карточки.',
    'empty': 'Кабинет подключён. На Ozon пока нет товаров — можно подготовить первую карточку.',
    'ozon_auth_error': 'Ozon отклонил ключ. Проверьте Client-Id и API key в настройках кабинета.',
    'ozon_catalog_access_denied': 'Ключ принят, но Ozon не разрешает чтение каталога, цен или остатков. Проверьте права API key. Сохранённые товары не изменены.',
    'ozon_rate_limited': 'Ozon ограничил частоту запросов. Продолжим автоматически после паузы; сохранённые товары доступны.',
    'ozon_temporarily_unavailable': 'Ozon временно недоступен. Повторим автоматически; сохранённые товары доступны.',
    'account_busy': 'Кабинет занят другой операцией. Продолжим автоматически после её завершения.',
    'credentials_changed': 'Реквизиты кабинета изменились. Запустите подключение с новым ключом.',
    'account_disconnected': 'Кабинет отключён. Подключите его снова, чтобы загрузить каталог.',
    'connection_required': 'Сначала проверьте подключение кабинета.',
    'invalid_snapshot': 'Ozon вернул неполные или противоречивые данные. Сохранённый каталог не удалён. Повторите загрузку позже.',
    'retry_exhausted': 'Ozon долго не отвечает или ограничивает запросы. Автоматические попытки остановлены; повторите загрузку позже.',
    'job_expired': 'Загрузка не завершилась за сутки. Сохранённые товары доступны; запустите загрузку снова.',
    'sync_failed': 'Не удалось завершить загрузку. Сохранённые товары доступны; повторите попытку.',
}


class OzonAccountSyncError(MarketplaceAccountError):
    code = 'ozon_account_sync_error'


class _BudgetedClient(OzonSellerAPIClient):
    def __init__(self, credentials):
        super().__init__(credentials, timeout=(3.0, 6.0), read_retries=0)
        bound_read_responses(self.session, maximum_bytes=MAX_READ_RESPONSE_BYTES)
        self.calls = 0
        self.deadline = time.monotonic() + CALL_START_BUDGET_SECONDS

    def request(self, endpoint_name, payload):
        spec = OZON_ENDPOINTS.get(endpoint_name)
        if spec is None or spec.retry_class != 'read':
            raise ValueError('Ozon account sync only permits allowlisted reads')
        if self.calls >= MAX_CALLS_PER_TICK or time.monotonic() >= self.deadline:
            raise OzonAPIError('Read budget exhausted', code='ozon_read_budget', retriable=True)
        self.calls += 1
        return super().request(endpoint_name, payload)


class _BoundedAdapter(OzonAdapter):
    """Reuse one connection pool for the at-most-one page of this tick."""
    def __init__(self):
        super().__init__()
        self.client = None

    def _client(self, credentials):
        if self.client is None:
            self.client = _BudgetedClient(credentials)
        elif self.client._credentials != credentials:
            raise ValueError('Account sync credentials changed within a tick')
        return self.client

    def close(self):
        if self.client is not None:
            self.client.session.close()


class _Registry:
    def __init__(self, adapter):
        self.adapter = adapter

    def get(self, code):
        if code != 'ozon':
            raise ValueError('Not an Ozon account')
        return self.adapter


def _prefix(account_id):
    MarketplaceAccountService._positive_integer(account_id, 'account_id')
    return f'oc:{account_id}:'


def _fingerprint(account):
    # Keep the private worker-state alias for existing callers/tests.
    return ozon_credential_fingerprint(account)


def _latest_run(seller_id, account_id):
    return MarketplaceCatalogSync.query.filter_by(
        seller_id=seller_id, account_id=account_id,
    ).order_by(MarketplaceCatalogSync.id.desc()).first()


def _job_query(seller_id, account_id):
    return BackgroundJob.query.filter(
        BackgroundJob.seller_id == seller_id, BackgroundJob.job_type == JOB_TYPE,
        BackgroundJob.job_uid.like(_prefix(account_id) + '%'),
    )


def _owned_account(seller_id, account_id):
    return MarketplaceAccountService.get_owned_account(
        seller_id=seller_id, account_id=account_id, marketplace_code='ozon',
    )


def _public_result(job, state, code):
    job.error_message = MESSAGES.get(code, MESSAGES['sync_failed'])
    job.set_result({
        'account_id': state['account_id'], 'phase': state['phase'], 'code': code,
        'next_retry_at': state.get('next_retry_at') or (
            state.get('auto_retry_not_before') if job.status == 'failed' else None
        ), 'sync_id': state.get('sync_id'),
        'page_count': state.get('page_count', 0),
    })


def public_job(job):
    if job is None:
        return None
    result = job.get_result()
    return {
        'job_uid': job.job_uid, 'status': job.status,
        'account_id': result.get('account_id'), 'phase': result.get('phase'),
        'code': result.get('code'), 'message': job.error_message,
        'processed': job.processed or 0, 'total': job.total or 0,
        'page_count': result.get('page_count', 0), 'sync_id': result.get('sync_id'),
        'next_retry_at': result.get('next_retry_at'),
        'updated_at': job.updated_at.isoformat() + 'Z' if job.updated_at else None,
    }


def latest_account_jobs(*, seller_id, account_ids):
    """One bounded query for up to 10 owned account scopes, without JSON scans."""
    MarketplaceAccountService._positive_integer(seller_id, 'seller_id')
    ids = list(dict.fromkeys(account_ids))
    if len(ids) > MarketplaceAccountService.MAX_ACCOUNTS_PER_MARKETPLACE:
        raise MarketplaceAccountValidationError('Слишком много кабинетов')
    if not ids:
        return {}
    selects = [select(func.max(BackgroundJob.id)).where(
        BackgroundJob.seller_id == seller_id, BackgroundJob.job_type == JOB_TYPE,
        BackgroundJob.job_uid.like(_prefix(account_id) + '%'),
    ) for account_id in ids]
    latest_ids = union_all(*selects).subquery()
    jobs = BackgroundJob.query.filter(BackgroundJob.id.in_(select(latest_ids.c[0]))).all()
    return {job.get_result().get('account_id'): public_job(job) for job in jobs}


def get_account_job(*, seller_id, account_id):
    _owned_account(seller_id, account_id)
    return public_job(_job_query(seller_id, account_id).order_by(BackgroundJob.id.desc()).first())


def enqueue_account_sync(*, seller_id, account_id, check_connection=False, force_restart=False):
    """No decryption, HTTP, thread creation or catalog mutation in a request."""
    if type(check_connection) is not bool or type(force_restart) is not bool:
        raise MarketplaceAccountValidationError('Параметры загрузки должны быть boolean')
    account = _owned_account(seller_id, account_id)
    if not account.is_active or not account.has_credentials or not account.marketplace.is_active:
        raise MarketplaceAccountValidationError(MESSAGES['account_disconnected'])
    if not check_connection and account.connection_status != 'connected':
        raise MarketplaceAccountValidationError(MESSAGES['connection_required'])
    existing = _job_query(seller_id, account_id).filter(BackgroundJob.status.in_(ACTIVE)).first()
    if existing and existing.get_progress().get('credential_fingerprint') == _fingerprint(account):
        return public_job(existing)
    claim = _try_operation_lock('ozon-account-sync', account_id)
    if claim is None:
        raise MarketplaceAccountConflict(MESSAGES['account_busy'])
    try:
        db.session.expire_all()
        account = _owned_account(seller_id, account_id)
        if not account.is_active or not account.has_credentials:
            raise MarketplaceAccountValidationError(MESSAGES['account_disconnected'])
        existing = _job_query(seller_id, account_id).filter(BackgroundJob.status.in_(ACTIVE)).first()
        if existing:
            if existing.get_progress().get('credential_fingerprint') == _fingerprint(account):
                return public_job(existing)
            old_state = existing.get_progress()
            old_state.update(phase='failed', account_id=account_id)
            existing.status = 'failed'
            _public_result(existing, old_state, 'credentials_changed')
        previous = _job_query(seller_id, account_id).order_by(BackgroundJob.id.desc()).first()
        if previous is not None and previous.status == 'failed':
            previous_state = previous.get_progress()
            not_before = previous_state.get('auto_retry_not_before')
            if (previous_state.get('credential_fingerprint') == _fingerprint(account)
                    and isinstance(not_before, str)
                    and not_before > datetime.utcnow().isoformat() + 'Z'):
                error = MarketplaceAccountConflict(
                    'Ozon запросил паузу. Дождитесь указанного времени следующей попытки.'
                )
                error.code = 'ozon_catalog_cooldown'
                raise error
        latest = _latest_run(seller_id, account_id)
        state = {
            'account_id': account_id, 'phase': 'check' if check_connection else 'catalog',
            'credential_fingerprint': _fingerprint(account),
            'force_restart': force_restart, 'baseline_sync_id': latest.id if latest else 0,
            'sync_id': latest.id if latest and latest.status in ('failed', 'paused') and not force_restart else None,
            'failures': 0,
        }
        job = BackgroundJob(
            job_uid=_prefix(account_id) + uuid.uuid4().hex, seller_id=seller_id,
            job_type=JOB_TYPE, status='pending', total=0, processed=0,
        )
        job.set_progress(state)
        _public_result(job, state, 'queued' if check_connection else 'catalog_queued')
        db.session.add(job)
        db.session.commit()
        return public_job(job)
    except Exception:
        db.session.rollback()
        raise
    finally:
        claim.close()


def _store(job, state, code, now, *, status=None):
    if status:
        job.status = status
    job.updated_at = now
    job.set_progress(state)
    _public_result(job, state, code)
    db.session.commit()


def _finish(job, state, code, now):
    state['phase'] = 'completed' if code in ('completed', 'empty') else 'failed'
    state.pop('next_retry_at', None)
    if state['phase'] == 'completed':
        job.total = job.processed or 0
    _store(job, state, code, now, status=state['phase'])


def _defer(job, state, code, now, retry_after=None, *, count_failure=True):
    failures = int(state.get('failures') or 0) + int(count_failure)
    state['failures'] = failures
    if isinstance(retry_after, (float, int)) and not isinstance(retry_after, bool) and math.isfinite(retry_after):
        try:
            not_before = now + timedelta(seconds=math.ceil(max(0, retry_after)))
        except OverflowError:
            not_before = datetime.max
        state['auto_retry_not_before'] = not_before.isoformat() + 'Z'
    if failures >= MAX_TRANSIENT_FAILURES:
        _finish(job, state, 'retry_exhausted', now)
        return
    seconds = min(3600, 60 * 2 ** min(failures, 6)) if count_failure else 30
    if isinstance(retry_after, (float, int)) and not isinstance(retry_after, bool) and math.isfinite(retry_after):
        # Never shorten a provider cooldown. A delay beyond the job lifetime
        # becomes an explicit stopped job, not an early physical retry.
        seconds = max(seconds, math.ceil(max(0, retry_after)))
    if seconds > MAX_JOB_AGE.total_seconds():
        _finish(job, state, 'retry_exhausted', now)
        return
    state['next_retry_at'] = (now + timedelta(seconds=seconds)).isoformat() + 'Z'
    _store(job, state, code, now, status='pending')


def _observe_run(job, state):
    run = _latest_run(job.seller_id, state['account_id'])
    if run is not None and (run.id > state['baseline_sync_id'] or run.id == state.get('sync_id')):
        state.update(sync_id=run.id, page_count=run.page_count, force_restart=False)
        job.processed = run.seen_count
        return run
    return None


def _advance(job_id, seller_id, *, now=None, adapter_factory=None):
    now = now or datetime.utcnow()
    tick_started = time.monotonic()

    def elapsed_now():
        # A Retry-After begins when the 429/transport result is observed, not
        # when this tick began before up to 45 seconds of earlier reads.
        return now + timedelta(seconds=max(0.0, time.monotonic() - tick_started))

    job = BackgroundJob.query.filter_by(id=job_id, seller_id=seller_id, job_type=JOB_TYPE).first()
    if job is None or job.status not in ACTIVE:
        return False
    state = job.get_progress()
    account_id = state.get('account_id')
    if type(account_id) is not int or not job.job_uid.startswith(_prefix(account_id)):
        raise ValueError('Invalid internal Ozon job scope')
    if job.created_at and job.created_at < now - MAX_JOB_AGE:
        _finish(job, state, 'job_expired', now)
        return True
    retry_at = state.get('next_retry_at')
    if retry_at and datetime.fromisoformat(retry_at.removesuffix('Z')) > now:
        return False
    state.pop('next_retry_at', None)
    try:
        account = _owned_account(seller_id, account_id)
    except MarketplaceAccountNotFound:
        _finish(job, state, 'account_disconnected', now)
        return True
    if not account.is_active or not account.has_credentials or not account.marketplace.is_active:
        _finish(job, state, 'account_disconnected', now)
        return True
    if _fingerprint(account) != state.get('credential_fingerprint'):
        _finish(job, state, 'credentials_changed', now)
        return True
    if state['phase'] == 'catalog':
        run = _observe_run(job, state)
        if run is not None and run.status == 'completed':
            _finish(job, state, 'completed' if run.seen_count else 'empty', now)
            return True
    expected_account_version = account.version
    adapter = (adapter_factory or _BoundedAdapter)()
    try:
        _store(job, state, 'checking' if state['phase'] == 'check' else 'catalog', now, status='running')
        if state['phase'] == 'check':
            account, result = MarketplaceAccountService.check_connection(
                seller_id=seller_id, account_id=account_id, registry=_Registry(adapter),
                expected_version=expected_account_version, now=now,
            )
            if not result.ok:
                metadata = result.metadata
                if result.status == 'invalid':
                    _finish(job, state, 'ozon_auth_error', now)
                elif metadata.get('retriable'):
                    _defer(job, state, 'ozon_rate_limited' if metadata.get('http_status') == 429 else 'ozon_temporarily_unavailable',
                           elapsed_now(), metadata.get('retry_after_seconds'))
                else:
                    _finish(job, state, 'sync_failed', now)
                return True
            state.update(phase='catalog', failures=0)
            _store(job, state, 'catalog_queued', now, status='pending')
            return True
        # Catalog reads and their short local commits must not cross a key
        # rotation/disconnect/publication. Re-ground after acquiring the claim.
        account_claim = try_account_operation_lock(account_id)
        if account_claim is None:
            raise MarketplaceAccountConflict(MESSAGES['account_busy'])
        try:
            db.session.expire_all()
            account = _owned_account(seller_id, account_id)
            if _fingerprint(account) != state['credential_fingerprint']:
                _finish(job, state, 'credentials_changed', now)
                return True
            if not account.is_active or not account.has_credentials:
                _finish(job, state, 'account_disconnected', now)
                return True
            secret = account.get_credentials()
            credentials = MarketplaceCredentials(external_account_id=account.external_account_id, api_key=secret['api_key'])
            del secret
            run = MarketplaceListingService.sync_ozon_account(
                seller_id=seller_id, account_id=account_id, max_pages=1,
                force_restart=state['force_restart'], recover_abandoned=True,
                account_claim_held=True,
                adapter=adapter, credentials=credentials, now=now,
            )
            state.update(sync_id=run.id, page_count=run.page_count, force_restart=False, failures=0)
            job.processed = run.seen_count
            if run.status == 'completed':
                _finish(job, state, 'completed' if run.seen_count else 'empty', now)
            else:
                _store(job, state, 'catalog', now, status='pending')
            return True
        finally:
            account_claim.close()
    except (MarketplaceAccountConflict, MarketplaceCatalogBusy):
        db.session.rollback()
        _defer(job, state, 'account_busy', elapsed_now(), count_failure=False)
    except Exception as exc:
        db.session.rollback()
        _observe_run(job, state)
        provider_status = getattr(exc, 'provider_status_code', None)
        if provider_status == 401:
            _finish(job, state, 'ozon_auth_error', now)
        elif provider_status == 403:
            _finish(job, state, 'ozon_catalog_access_denied', now)
        elif getattr(exc, 'retriable', False):
            _defer(job, state, 'ozon_rate_limited' if provider_status == 429 else 'ozon_temporarily_unavailable',
                   elapsed_now(), getattr(exc, 'retry_after', None))
        else:
            code = ('retry_exhausted' if getattr(exc, 'code', '') == 'ozon_catalog_checkpoint_exhausted'
                    else 'invalid_snapshot' if getattr(exc, 'code', '') == 'ozon_catalog_protocol_error'
                    else 'sync_failed')
            _finish(job, state, code, now)
        # Never log arbitrary exception strings/provider bodies/credentials.
        logger.info('Ozon read workflow deferred/stopped seller=%s account=%s kind=%s', seller_id, account_id, type(exc).__name__)
    finally:
        close = getattr(adapter, 'close', None)
        if close is not None:
            close()
    return True


def run_account_sync_tick(flask_app):
    if not flask_app.config.get('MARKETPLACE_OZON_ENABLED', False):
        return
    with flask_app.app_context():
        # Future cooldowns must not occupy the whole bounded candidate window
        # and starve a new seller. Only this job type owns these small JSON rows.
        retry_at = case((
            func.json_valid(BackgroundJob.result_data),
            func.json_extract(BackgroundJob.result_data, '$.next_retry_at'),
        ), else_=None)
        candidates = db.session.query(BackgroundJob.id, BackgroundJob.seller_id, BackgroundJob.job_uid).filter(
            BackgroundJob.job_type == JOB_TYPE, BackgroundJob.status.in_(ACTIVE),
            or_(retry_at.is_(None), retry_at <= datetime.utcnow().isoformat() + 'Z'),
        ).order_by(BackgroundJob.updated_at, BackgroundJob.id).limit(100).all()
        for job_id, seller_id, uid in candidates:
            parts = uid.split(':')
            if len(parts) != 3 or parts[0] != 'oc' or not parts[1].isdigit():
                continue
            claim = _try_operation_lock('ozon-account-sync', int(parts[1]))
            if claim is None:
                continue
            try:
                if _advance(job_id, seller_id):
                    return
            except Exception:
                db.session.rollback()
                logger.warning('Ozon read workflow tick failed safely job=%s seller=%s', job_id, seller_id)
            finally:
                claim.close()
                db.session.remove()
