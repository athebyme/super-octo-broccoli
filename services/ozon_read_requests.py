"""Seller-authorized enqueue/status for exact-period Ozon refreshes. No I/O."""
from datetime import datetime, timedelta

from sqlalchemy import case, text
from sqlalchemy.dialects.sqlite import insert

from models import MarketplaceReadRequest, MarketplaceReadSchedule, db
from services.marketplace_accounts import (
    MarketplaceAccountService, MarketplaceAccountValidationError,
)
from services.marketplace_credential_identity import ozon_credential_fingerprint


ACTIVE = ('pending', 'running')
MAX_REQUEST_AGE = timedelta(hours=24)
MAX_REQUEST_FAILURES = 8
MESSAGES = {
    'pending': 'Обновление в очереди. Страницу можно закрыть.',
    'running': 'Загружаем данные из Ozon. Страницу можно закрыть.',
    'completed': 'Обновление завершено. Данные за выбранные даты готовы.',
    'failed': 'Обновление не завершилось. Сохранённые данные доступны; повторите позже.',
    'provider_rate_limited': 'Ozon ограничил частоту запросов. Продолжим автоматически после паузы.',
    'provider_unavailable': 'Ozon временно недоступен. Повторим автоматически.',
    'read_budget_exhausted': 'Часть данных загружена. Продолжим автоматически.',
    'account_busy': 'В кабинете выполняется другая операция. Продолжим автоматически.',
    'access_denied': 'Ozon отклонил доступ. Проверьте ключ и его права в настройках кабинета.',
    'inbox_access_denied': 'Ozon отклонил доступ к этому разделу. Автоматические попытки приостановлены на сутки. После изменения доступа можно проверить его вручную.',
    'account_unavailable': 'Для обновления восстановите подключение кабинета в настройках.',
    'credentials_changed': 'Ключ кабинета изменился. Запустите обновление с новым ключом.',
    'credentials_unverified': 'Не удалось подтвердить ключ для прежней заявки. Запустите обновление заново.',
    'request_expired': 'Обновление не завершилось за сутки. Сохранённые данные доступны; запустите его снова.',
    'retry_exhausted': 'Ozon долго не отвечает. Автоматические попытки остановлены; повторите позже.',
    'retry_delay_out_of_range': 'Ozon указал слишком долгую паузу. Обратитесь в поддержку.',
}


def _spec(domain):
    from services.ozon_read_scheduler import _domain
    try:
        return _domain(domain)
    except ValueError:
        raise MarketplaceAccountValidationError('Неизвестный раздел Ozon') from None


def _owned(seller_id, account_id):
    return MarketplaceAccountService.get_owned_account(
        seller_id=seller_id, account_id=account_id, marketplace_code='ozon',
    )


def _available(account, now, spec=None):
    return (account.is_active and account.marketplace.is_active
            and account.connection_status == 'connected' and account.has_credentials
            and (account.credential_expires_at is None or account.credential_expires_at > now)
            and (spec is None or not spec.capability or spec.capability in account.capabilities))


def request_query(row):
    return MarketplaceReadRequest.query.filter_by(
        seller_id=row.seller_id, marketplace_id=row.marketplace_id,
        account_id=row.account_id, domain=row.domain,
    )


def _iso(value):
    return value.isoformat() + 'Z' if value else None


def _credential_error(item, account):
    # Old intents have no proven identity. Never attach today's key to them.
    if not item.credential_fingerprint:
        return 'credentials_unverified'
    if (item.credential_fingerprint != ozon_credential_fingerprint(account)
            or item.credential_version != account.credential_version):
        return 'credentials_changed'
    return None


def serialize_request(item, schedule, account, *, now):
    spec = _spec(item.domain)
    status = item.status
    code = item.error_code
    active = status in ACTIVE
    if active and not _available(account, now, spec):
        status, code, active = 'paused', 'account_unavailable', False
    elif active and _credential_error(item, account):
        status, code, active = 'paused', _credential_error(item, account), False
    elif active and item.requested_at < now - MAX_REQUEST_AGE:
        status, code, active = 'paused', 'request_expired', False
    elif active and schedule and schedule.cooldown_until and schedule.cooldown_until > now:
        status, code = 'waiting', schedule.last_error_code
    run = None
    if item.run_id:
        run = spec.model.query.filter_by(
            id=item.run_id, seller_id=item.seller_id, marketplace_id=item.marketplace_id,
            account_id=item.account_id, **spec.run_scope,
        ).first()
    due = schedule.next_due_at if schedule and active else None
    return {
        'id': item.id, 'account_id': item.account_id, 'domain': item.domain,
        'period': item.period_code, 'period_start': item.period_start.isoformat(),
        'period_end': item.period_end.isoformat(), 'status': status, 'active': active,
        'message': MESSAGES.get(code) or MESSAGES.get(status, MESSAGES['failed']),
        'error_code': code if code in MESSAGES else ('sync_failed' if code else None),
        'requested_at': _iso(item.requested_at), 'completed_at': _iso(item.completed_at),
        'next_attempt_at': _iso(due),
        'pages_loaded': int(getattr(run, 'page_count', 0) or 0),
        'snapshot_id': run.id if run is not None and item.status == 'completed' else None,
    }


def enqueue_read(*, seller_id, account_id, domain, period_code='30d', force=False, now=None):
    """One short transaction. A duplicate active request never resets cooldown."""
    spec = _spec(domain)
    now = now or datetime.utcnow()
    if type(force) is not bool:
        raise MarketplaceAccountValidationError('force должен быть boolean')
    period_code, start, end = spec.service._period(period_code, today=now.date())
    account = _owned(seller_id, account_id)
    if not _available(account, now, spec):
        raise MarketplaceAccountValidationError(MESSAGES['account_unavailable'])
    fingerprint = ozon_credential_fingerprint(account)
    scope = dict(seller_id=account.seller_id, marketplace_id=account.marketplace_id,
                 account_id=account.id, domain=domain)
    db.session.execute(insert(MarketplaceReadSchedule).values(
        **scope, status='pending', next_due_at=now, created_at=now, updated_at=now,
        consecutive_failures=0,
    ).on_conflict_do_nothing(index_elements=['account_id', 'domain']))
    # Retire obsolete manual intents without touching any provider snapshot.
    unknown_key = MarketplaceReadRequest.credential_fingerprint.is_(None)
    changed_key = (
        (MarketplaceReadRequest.credential_fingerprint != fingerprint)
        | (MarketplaceReadRequest.credential_version != account.credential_version)
    )
    MarketplaceReadRequest.query.filter_by(**scope).filter(
        MarketplaceReadRequest.status.in_(ACTIVE),
        unknown_key | changed_key
        | (MarketplaceReadRequest.requested_at < now - MAX_REQUEST_AGE),
    ).update(dict(status='failed', error_code=case(
        (unknown_key, 'credentials_unverified'),
        (changed_key, 'credentials_changed'),
        else_='request_expired',
    ), completed_at=now, updated_at=now), synchronize_session=False)
    created = db.session.execute(insert(MarketplaceReadRequest).values(
        **scope, period_code=period_code, period_start=start, period_end=end,
        force=force, credential_version=account.credential_version, status='pending',
        credential_fingerprint=fingerprint,
        failure_count=0, requested_at=now, updated_at=now,
    ).on_conflict_do_nothing(
        index_elements=['account_id', 'domain', 'period_code'],
        index_where=text("status IN ('pending','running')"),
    )).rowcount == 1
    if created:
        # Only an explicit recheck may shorten the endpoint-level denial pause.
        # Neither a 429 nor the transport's shared Client-Id ledger is cleared.
        if spec.source_kind and force:
            MarketplaceReadSchedule.query.filter_by(**scope, last_error_code='inbox_access_denied').filter(
                MarketplaceReadSchedule.lease_token.is_(None),
            ).update(dict(cooldown_until=None), synchronize_session=False)
        MarketplaceReadSchedule.query.filter_by(**scope).update({
            'next_due_at': case(
                (MarketplaceReadSchedule.cooldown_until > now, MarketplaceReadSchedule.cooldown_until),
                else_=now,
            ),
            'updated_at': now,
        }, synchronize_session=False)
    # Bounded read-request history; domain facts have their own retention rules.
    old = db.session.query(MarketplaceReadRequest.id).filter_by(**scope).filter(
        MarketplaceReadRequest.status.in_(('completed', 'failed')),
    ).order_by(MarketplaceReadRequest.id.desc()).offset(20).limit(100).all()
    if old:
        MarketplaceReadRequest.query.filter(MarketplaceReadRequest.id.in_([r.id for r in old])).delete(synchronize_session=False)
    db.session.commit()
    return read_status(seller_id=seller_id, account_id=account_id, domain=domain,
                       period_code=period_code, now=now)


def read_status(*, seller_id, account_id, domain, period_code='30d', now=None):
    spec = _spec(domain)
    now = now or datetime.utcnow()
    spec.service._period(period_code, today=now.date())
    account = _owned(seller_id, account_id)
    scope = dict(seller_id=account.seller_id, marketplace_id=account.marketplace_id,
                 account_id=account.id, domain=domain)
    item = MarketplaceReadRequest.query.filter_by(**scope, period_code=period_code).order_by(
        MarketplaceReadRequest.id.desc(),
    ).first()
    if item is None:
        return {'id': None, 'account_id': account.id, 'domain': domain,
                'period': period_code, 'status': 'idle', 'active': False, 'message': ''}
    schedule = MarketplaceReadSchedule.query.filter_by(**scope).first()
    return serialize_request(item, schedule, account, now=now)


def worker_request(row, account, now):
    """At most two active periods, selected only under the scheduler claim."""
    items = request_query(row).filter(MarketplaceReadRequest.status.in_(ACTIVE)).order_by(
        MarketplaceReadRequest.requested_at, MarketplaceReadRequest.id,
    ).limit(2).all()
    for item in items:
        code = (_credential_error(item, account) or
                ('request_expired' if item.requested_at < now - MAX_REQUEST_AGE else None))
        if code:
            item.status, item.error_code = 'failed', code
            item.completed_at = item.updated_at = now
            db.session.commit()
            continue
        claimed = request_query(row).filter_by(id=item.id).filter(
            MarketplaceReadRequest.status.in_(ACTIVE),
        ).update(dict(status='running', updated_at=now), synchronize_session=False)
        db.session.commit()
        if claimed:
            db.session.refresh(item)
            return item
    return None


def record_request(row, item, run, now, *, error_code=None, retryable=False):
    if item is None:
        return
    item = request_query(row).filter_by(id=item.id).filter(MarketplaceReadRequest.status.in_(ACTIVE)).first()
    if item is None:
        return  # A concurrent key change/new request cannot be completed by this worker.
    spec = _spec(item.domain)
    matches = run is not None and (not spec.source_kind or run.source_kind == spec.source_kind) and (
        run.period_code == item.period_code and run.period_start == item.period_start
        and run.period_end == item.period_end
    )
    values = {'updated_at': now}
    if matches:
        values['run_id'] = run.id
    if error_code:
        failures = item.failure_count + int(error_code != 'account_busy')
        values.update(failure_count=failures, error_code=error_code)
        if not retryable or failures >= MAX_REQUEST_FAILURES:
            values.update(status='failed', completed_at=now)
            if retryable:
                values['error_code'] = 'retry_exhausted'
    elif matches and run.status == 'completed':
        values.update(status='completed', completed_at=now, error_code=None)
    else:
        values['error_code'] = None
    request_query(row).filter_by(id=item.id).filter(
        MarketplaceReadRequest.status.in_(ACTIVE),
    ).update(values, synchronize_session=False)
    db.session.commit()
