"""Durable, fair scheduling for Ozon analytics, fulfillment and finance reads.

The schedule owns attempts/cooldowns, including failures before a domain run
exists. Domain services still own page transactions and last-good snapshots.
No credential, provider body or arbitrary exception text enters this table.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
import math
import time
import uuid

from sqlalchemy import DateTime, and_, case, func, literal, or_, select
from sqlalchemy.dialects.sqlite import insert

from models import Marketplace, MarketplaceReadRequest, MarketplaceReadSchedule, SellerMarketplaceAccount, db
from services.ozon_read_requests import ACTIVE, record_request, worker_request
from services.marketplace_adapters.ozon import OzonAdapter
from services.marketplace_adapters.types import MarketplaceCredentials
from services.marketplace_operation_locks import _try_operation_lock, try_account_operation_lock
from services.ozon_api_client import OZON_ENDPOINTS, OzonAPIError, OzonSellerAPIClient


logger = logging.getLogger(__name__)
DISCOVERY_LIMIT = 200
CANDIDATE_LIMIT = 100
LEASE_SECONDS = 120
MAX_CALLS_PER_ACCOUNT = 12
CALL_START_BUDGET_SECONDS = 45
MAX_RETRY_SECONDS = 6 * 3600


@dataclass(frozen=True)
class Domain:
    service: object
    model: object
    max_pages: int
    default_period: str = '30d'
    source_kind: str = None
    capability: str = None

    @property
    def run_scope(self):
        return {'source_kind': self.source_kind} if self.source_kind else {}


def _domain(name):
    if name == 'analytics':
        from models import MarketplaceAnalyticsSync
        from services.marketplace_analytics import MarketplaceAnalyticsService
        return Domain(MarketplaceAnalyticsService, MarketplaceAnalyticsSync, 2)
    if name == 'fulfillment':
        from models import MarketplaceFulfillmentSync
        from services.marketplace_fulfillment import MarketplaceFulfillmentService
        return Domain(MarketplaceFulfillmentService, MarketplaceFulfillmentSync, 5)
    if name == 'finance':
        from models import MarketplaceFinanceSync
        from services.marketplace_finance import MarketplaceFinanceService
        return Domain(MarketplaceFinanceService, MarketplaceFinanceSync, 5)
    if name in ('reviews', 'questions'):
        from models import MarketplaceInboxSync
        from services.ozon_inbox_reads import OzonReviewReadService, OzonQuestionReadService
        return Domain(OzonReviewReadService if name == 'reviews' else OzonQuestionReadService,
                      MarketplaceInboxSync, 3, '90d',
                      'review' if name == 'reviews' else 'question', name + '_read')
    raise ValueError('Unsupported Ozon read domain')


class _ReadClient(OzonSellerAPIClient):
    def __init__(self, credentials):
        super().__init__(credentials, timeout=(3.0, 6.0), read_retries=0)
        self.calls = 0
        self.deadline = time.monotonic() + CALL_START_BUDGET_SECONDS
        self.last_error = None

    def request(self, endpoint_name, payload):
        spec = OZON_ENDPOINTS.get(endpoint_name)
        if spec is None or spec.retry_class != 'read':
            raise ValueError('Ozon scheduled reads cannot write to the provider')
        try:
            if self.calls >= MAX_CALLS_PER_ACCOUNT or time.monotonic() >= self.deadline:
                raise OzonAPIError('Read budget exhausted', code='ozon_read_budget', retriable=True)
            self.calls += 1
            return super().request(endpoint_name, payload)
        except OzonAPIError as exc:
            self.last_error = exc
            raise


class _ReadAdapter(OzonAdapter):
    def __init__(self, credentials):
        super().__init__()
        self.client = _ReadClient(credentials)

    def _client(self, credentials):
        if credentials != self.client._credentials:
            raise ValueError('Ozon read credentials changed within an attempt')
        return self.client

    @property
    def provider_error(self):
        return self.client.last_error

    def close(self):
        self.client.session.close()


def _eligible_accounts(now):
    return SellerMarketplaceAccount.query.join(Marketplace).filter(
        Marketplace.code == 'ozon', Marketplace.is_active.is_(True),
        SellerMarketplaceAccount.is_active.is_(True),
        SellerMarketplaceAccount.connection_status == 'connected',
        SellerMarketplaceAccount._credentials_encrypted.isnot(None),
        SellerMarketplaceAccount._credentials_encrypted != '',
        or_(SellerMarketplaceAccount.credential_expires_at.is_(None),
            SellerMarketplaceAccount.credential_expires_at > now),
    )


def _capability_condition(domain):
    """Exact JSON array membership before LIMIT; invalid/revoked roles cannot starve others."""
    raw = SellerMarketplaceAccount.capabilities_json
    valid = case((func.json_valid(raw), raw), else_='[]')
    array = case((func.json_type(valid) == 'array', valid), else_='[]')
    roles = func.json_each(array).table_valued('value', 'type')
    def has(value):
        return select(literal(1)).select_from(roles).where(
            roles.c.type == 'text', roles.c.value == value).exists()
    return or_(and_(domain == 'reviews', has('reviews_read')),
               and_(domain == 'questions', has('questions_read')),
               and_(domain != 'reviews', domain != 'questions'))


def _discover(domain, now):
    """Enroll a bounded missing set; existing accounts never occupy the limit."""
    absent = ~select(MarketplaceReadSchedule.id).where(
        MarketplaceReadSchedule.account_id == SellerMarketplaceAccount.id,
        MarketplaceReadSchedule.domain == domain,
    ).exists()
    eligible = _eligible_accounts(now).filter(absent, _capability_condition(domain))
    spec = _domain(domain)
    if spec.source_kind:
        from services.marketplace_inbox import MarketplaceInboxService
        for account in eligible.order_by(SellerMarketplaceAccount.id).limit(DISCOVERY_LIMIT).all():
            due = MarketplaceInboxService.access_denied_retry_after(
                seller_id=account.seller_id, account_id=account.id,
                source_kind=spec.source_kind, now=now)
            db.session.execute(insert(MarketplaceReadSchedule).values(
                seller_id=account.seller_id, marketplace_id=account.marketplace_id,
                account_id=account.id, domain=domain, status='waiting' if due else 'pending',
                next_due_at=due or now, cooldown_until=due,
                last_error_code='inbox_access_denied' if due else None,
                created_at=now, updated_at=now, consecutive_failures=0,
            ).on_conflict_do_nothing(index_elements=['account_id', 'domain']))
        db.session.commit()
        return
    source = eligible.order_by(
        SellerMarketplaceAccount.id,
    ).limit(DISCOVERY_LIMIT).with_entities(
        SellerMarketplaceAccount.seller_id,
        SellerMarketplaceAccount.marketplace_id,
        SellerMarketplaceAccount.id,
        literal(domain), literal('pending'), literal(now), literal(now), literal(now),
        literal(0),
    ).statement
    statement = insert(MarketplaceReadSchedule).from_select(
        ['seller_id', 'marketplace_id', 'account_id', 'domain', 'status',
         'next_due_at', 'created_at', 'updated_at', 'consecutive_failures'],
        source, include_defaults=False,
    ).on_conflict_do_nothing(index_elements=['account_id', 'domain'])
    db.session.execute(statement)
    db.session.commit()


def _due_conditions(now):
    return (
        MarketplaceReadSchedule.next_due_at <= now,
        or_(MarketplaceReadSchedule.cooldown_until.is_(None),
            MarketplaceReadSchedule.cooldown_until <= now),
        or_(MarketplaceReadSchedule.lease_expires_at.is_(None),
            MarketplaceReadSchedule.lease_expires_at <= now),
    )


def _scope(row):
    return MarketplaceReadSchedule.query.filter_by(
        id=row.id, seller_id=row.seller_id, marketplace_id=row.marketplace_id,
        account_id=row.account_id, domain=row.domain,
    )


def _claim(row, now):
    token = uuid.uuid4().hex
    claimed = _scope(row).filter(*_due_conditions(now)).update({
        'lease_token': token, 'lease_expires_at': now + timedelta(seconds=LEASE_SECONDS),
        'last_attempt_at': now, 'next_due_at': now + timedelta(seconds=LEASE_SECONDS),
        'status': 'running', 'updated_at': now,
    }, synchronize_session=False)
    db.session.commit()
    return token if claimed == 1 else None


def _finish(row, token, now, **values):
    # The HTTP enqueue can happen while this worker is reading Ozon. Evaluate
    # pending intent in the UPDATE itself so neither ordering can lose its due.
    active = select(MarketplaceReadRequest.id).where(
        MarketplaceReadRequest.seller_id == row.seller_id,
        MarketplaceReadRequest.marketplace_id == row.marketplace_id,
        MarketplaceReadRequest.account_id == row.account_id,
        MarketplaceReadRequest.domain == row.domain,
        MarketplaceReadRequest.status.in_(ACTIVE),
    ).exists()
    due = values.get('next_due_at', MarketplaceReadSchedule.next_due_at)
    cooldown = (literal(values['cooldown_until'], type_=DateTime)
                if 'cooldown_until' in values else MarketplaceReadSchedule.cooldown_until)
    values['next_due_at'] = case(
        (cooldown > now, cooldown),
        (active, func.min(due, now + timedelta(seconds=60))),
        else_=due,
    )
    changed = _scope(row).filter(MarketplaceReadSchedule.lease_token == token).update({
        'lease_token': None, 'lease_expires_at': None, 'updated_at': now, **values,
    }, synchronize_session=False)
    db.session.commit()
    return changed == 1


def _current_run(spec, row):
    return spec.model.query.filter_by(
        seller_id=row.seller_id, marketplace_id=row.marketplace_id,
        account_id=row.account_id, status='running', **spec.run_scope,
    ).order_by(spec.model.id.desc()).first()


def _cached(spec, row, now, request=None):
    if request is not None and request.force:
        return None
    period = request.period_code if request is not None else spec.default_period
    end = request.period_end if request is not None else now.date()
    start = request.period_start if request is not None else spec.service._period(period, today=end)[1]
    if row.domain == 'analytics':
        return spec.service._fresh_cached_sync(
            seller_id=row.seller_id, account_id=row.account_id,
            period_code=period, now=now, today=end,
        )
    return spec.service._fresh_completed(
        seller_id=row.seller_id, account_id=row.account_id, period_code=period,
        period_start=start, period_end=end, now=now,
    )


def _success(row, token, spec, run, now):
    completed = getattr(run, 'completed_at', None) or now
    next_due = min(
        completed + spec.service.CACHE_TTL,
        datetime.combine(now.date() + timedelta(days=1), datetime.min.time()),
    )
    # A resumed 7d/old-window run does not make the automatic 30d scope fresh.
    if (getattr(run, 'period_code', spec.default_period) != spec.default_period
            or getattr(run, 'period_end', now.date()) != now.date()):
        next_due = now + timedelta(seconds=60)
    _finish(row, token, now, status='idle', next_due_at=max(next_due, now),
            cooldown_until=None, consecutive_failures=0, last_error_code=None,
            last_success_at=completed, last_run_id=getattr(run, 'id', None))


def _provider_error(exc, adapter):
    observed = getattr(adapter, 'provider_error', None) if adapter is not None else None
    if isinstance(observed, OzonAPIError):
        return observed
    # Services wrap provider errors for HTTP compatibility. Only inspect typed
    # exceptions; never persist messages, bodies or arbitrary provider codes.
    for _ in range(5):
        if isinstance(exc, OzonAPIError):
            return exc
        exc = getattr(exc, '__cause__', None) or getattr(exc, '__context__', None)
        if exc is None:
            break
    return None


def _failed(row, token, spec, exc, adapter, now, request=None):
    provider = _provider_error(exc, adapter)
    failures = min(row.consecutive_failures + 1, 1_000_000)
    delay = min(60 * 2 ** min(failures - 1, 9), MAX_RETRY_SECONDS)
    code = 'sync_failed'
    retry_after = None
    if provider is not None:
        retry_after = provider.retry_after
        if provider.status_code == 429:
            code = 'provider_rate_limited'
        elif spec.source_kind and not provider.retriable and str(provider.code).strip() == '7':
            code, delay = 'inbox_access_denied', 24 * 3600
        elif provider.status_code in (401, 403):
            code = 'access_denied'
            delay = 24 * 3600
        elif provider.code == 'ozon_read_budget':
            code = 'read_budget_exhausted'
        else:
            code = 'provider_unavailable'
    elif spec.source_kind and getattr(exc, 'code', '') == 'ozon_inbox_access_denied':
        code, delay = 'inbox_access_denied', 24 * 3600
    elif getattr(exc, 'code', '') in {
        'marketplace_analytics_busy', 'marketplace_finance_busy', 'marketplace_fulfillment_busy', 'marketplace_inbox_busy',
    }:
        code, delay, failures = 'account_busy', 60, row.consecutive_failures
    else:
        # Permanent protocol/configuration failures should not be hammered.
        delay = max(delay, 600)
    if isinstance(retry_after, (float, int)) and not isinstance(retry_after, bool):
        if math.isfinite(retry_after) and retry_after > 0:
            delay = max(delay, math.ceil(retry_after))
    try:
        due = now + timedelta(seconds=delay)
    except OverflowError:
        # Unrepresentable finite provider delay must never turn into an early
        # retry. Leave the scope visibly stopped until an operator investigates.
        due, code = datetime.max, 'retry_delay_out_of_range'
    running = _current_run(spec, row)
    record_request(row, request, running, now, error_code=code,
                   retryable=bool(provider and provider.retriable) or code == 'account_busy')
    _finish(row, token, now, status='waiting', next_due_at=due, cooldown_until=due,
            consecutive_failures=failures, last_error_code=code,
            last_run_id=running.id if running else row.last_run_id)
    return code


def _advance(row, spec, now, adapter_factory, *, requested_only=False):
    """Hold host claims across I/O, but commit the durable lease beforehand."""
    claim = _try_operation_lock('ozon-read-' + row.domain, row.account_id)
    if claim is None:
        return 'skipped'
    account_claim = None
    adapter = None
    token = None
    request = None
    started = time.monotonic()
    try:
        token = _claim(row, now)
        if token is None:
            return 'skipped'
        account_claim = try_account_operation_lock(row.account_id)
        if account_claim is None:
            _finish(row, token, now, status='waiting', next_due_at=now + timedelta(seconds=60),
                    last_error_code='account_busy')
            return 'skipped'
        db.session.expire_all()
        account = _eligible_accounts(now).filter(
            SellerMarketplaceAccount.id == row.account_id,
            SellerMarketplaceAccount.seller_id == row.seller_id,
            SellerMarketplaceAccount.marketplace_id == row.marketplace_id,
            _capability_condition(row.domain),
        ).first()
        if account is None:
            _finish(row, token, now, status='waiting', next_due_at=now + timedelta(minutes=10),
                    last_error_code='account_unavailable')
            return 'skipped'
        request = worker_request(row, account, now)
        if requested_only and request is None:
            _finish(row, token, now, status='idle', next_due_at=now + timedelta(minutes=10))
            return 'skipped'
        running = _current_run(spec, row)
        cached = _cached(spec, row, now, request) if running is None else None
        if cached is not None:
            record_request(row, request, cached, now)
            _success(row, token, spec, cached, now)
            return 'skipped'
        secret = account.get_credentials()
        credentials = MarketplaceCredentials(external_account_id=account.external_account_id,
                                             api_key=secret['api_key'])
        del secret
        adapter = adapter_factory(credentials)
        run = spec.service.sync_account(
            seller_id=row.seller_id, account_id=row.account_id,
            period_code=(running.period_code if running is not None else
                         request.period_code if request is not None else spec.default_period),
            force=running is not None or bool(request and request.force), max_pages=spec.max_pages,
            adapter=adapter, credentials=credentials, recover_abandoned=True,
            now=now, today=(running.period_end if running is not None else
                            request.period_end if request is not None else now.date()),
        )
        finished = now + timedelta(seconds=time.monotonic() - started)
        record_request(row, request, run, finished)
        if run.status == 'completed':
            _success(row, token, spec, run, finished)
            return 'completed'
        if run.status != 'running':
            raise ValueError('Unexpected read sync result')
        _finish(row, token, finished, status='pending', next_due_at=finished + timedelta(seconds=60),
                cooldown_until=None, consecutive_failures=0, last_error_code=None,
                last_run_id=run.id)
        return 'running'
    except Exception as exc:
        db.session.rollback()
        failure_code = None
        if token is not None:
            failure_code = _failed(row, token, spec, exc, adapter,
                    now + timedelta(seconds=time.monotonic() - started), request=request)
        logger.info('Ozon scheduled read deferred domain=%s account=%s kind=%s',
                    row.domain, row.account_id, type(exc).__name__)
        return 'unavailable' if failure_code == 'inbox_access_denied' else 'failed'
    finally:
        if adapter is not None:
            try:
                adapter.close()
            except Exception as exc:
                logger.info('Ozon read pool cleanup failed kind=%s', type(exc).__name__)
        if account_claim is not None:
            account_claim.close()
        claim.close()


def run_due_reads(*, domain, limit, now=None, adapter_factory=None):
    return _run_due_domains((domain,), limit=limit, now=now, adapter_factory=adapter_factory)


def run_due_inbox_reads(*, limit=2, now=None, adapter_factory=None):
    return _run_due_domains(('reviews', 'questions'), limit=limit, now=now, adapter_factory=adapter_factory)


def _run_due_domains(domains, *, limit, now=None, adapter_factory=None):
    """Called in the singleton scheduler's app context; never a provider write."""
    specs = {name: _domain(name) for name in domains}
    if type(limit) is not int or not 1 <= limit <= 10:
        raise ValueError('Read scheduler limit must be between 1 and 10')
    now = now or datetime.utcnow()
    for domain in domains:
        _discover(domain, now)
    candidates = _eligible_accounts(now).join(MarketplaceReadSchedule, and_(
        MarketplaceReadSchedule.account_id == SellerMarketplaceAccount.id,
        MarketplaceReadSchedule.seller_id == SellerMarketplaceAccount.seller_id,
        MarketplaceReadSchedule.marketplace_id == SellerMarketplaceAccount.marketplace_id,
    )).filter(
        MarketplaceReadSchedule.domain.in_(domains), *_due_conditions(now),
        _capability_condition(MarketplaceReadSchedule.domain),
    ).with_entities(MarketplaceReadSchedule).order_by(
        MarketplaceReadSchedule.next_due_at, MarketplaceReadSchedule.last_attempt_at,
        MarketplaceReadSchedule.id,
    ).limit(CANDIDATE_LIMIT).all()
    result = {'selected': 0, 'completed': 0, 'running': 0, 'failed': 0, 'unavailable': 0}
    for row in candidates:
        outcome = _advance(row, specs[row.domain], now, adapter_factory or _ReadAdapter)
        if outcome != 'skipped':
            result['selected'] += 1
            result[outcome] += 1
        if result['selected'] >= limit:
            break
    return result


def _requested_candidate(now, exclude_account_ids=()):
    """Earliest eligible period request, with exclusions applied before LIMIT."""
    active = select(MarketplaceReadRequest.id).where(
        MarketplaceReadRequest.seller_id == MarketplaceReadSchedule.seller_id,
        MarketplaceReadRequest.marketplace_id == MarketplaceReadSchedule.marketplace_id,
        MarketplaceReadRequest.account_id == MarketplaceReadSchedule.account_id,
        MarketplaceReadRequest.domain == MarketplaceReadSchedule.domain,
        MarketplaceReadRequest.status.in_(ACTIVE),
    ).exists()
    candidates = _eligible_accounts(now).join(MarketplaceReadSchedule, and_(
        MarketplaceReadSchedule.account_id == SellerMarketplaceAccount.id,
        MarketplaceReadSchedule.seller_id == SellerMarketplaceAccount.seller_id,
        MarketplaceReadSchedule.marketplace_id == SellerMarketplaceAccount.marketplace_id,
    )).filter(active, *_due_conditions(now), _capability_condition(MarketplaceReadSchedule.domain))
    if exclude_account_ids:
        candidates = candidates.filter(SellerMarketplaceAccount.id.notin_(exclude_account_ids))
    return candidates.with_entities(MarketplaceReadSchedule).order_by(
        MarketplaceReadSchedule.next_due_at, MarketplaceReadSchedule.last_attempt_at,
        MarketplaceReadSchedule.id,
    ).first()


def run_requested_reads(*, limit=1, now=None, adapter_factory=None,
                        warehouse_adapter_factory=None):
    """Share the existing frequent slot fairly between period and stock reads.

    Each lane selects its earliest eligible account before LIMIT. Compare their
    durable due/attempt times, then exclude a visited account in both lanes so
    a busy cabinet cannot hide another one or spend two per-account budgets.
    A skipped claim consumes no execution slot; probing is bounded separately.
    """
    if type(limit) is not int or not 1 <= limit <= 10:
        raise ValueError('Read scheduler limit must be between 1 and 10')
    from services.ozon_warehouse_reads import due_candidate, run_read_step
    now = now or datetime.utcnow()
    result = {'selected': 0, 'completed': 0, 'running': 0, 'failed': 0, 'unavailable': 0}
    visited_accounts = set()
    for _ in range(CANDIDATE_LIMIT):
        row = _requested_candidate(now, visited_accounts)
        warehouse = due_candidate(now=now, exclude_account_ids=visited_accounts)
        candidates = []
        if row is not None:
            candidates.append((row.next_due_at, row.last_attempt_at or datetime.min,
                               0, row.id, row.account_id))
        if warehouse is not None:
            candidates.append((warehouse['next_due_at'], warehouse['last_attempt_at'] or datetime.min,
                               1, warehouse['id'], warehouse['account_id']))
        if not candidates:
            break
        _, _, lane, identity, account_id = min(candidates)
        visited_accounts.add(account_id)
        if lane == 0:
            outcome = _advance(row, _domain(row.domain), now, adapter_factory or _ReadAdapter,
                               requested_only=True)
        else:
            step = run_read_step(identity, now=now, adapter_factory=warehouse_adapter_factory)
            outcome = step['outcome'] if step['selected'] else 'skipped'
        if outcome != 'skipped':
            result['selected'] += 1
            result[outcome] += 1
        if result['selected'] >= limit:
            break
    return result
