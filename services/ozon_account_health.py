"""Bounded local observations for one seller-owned Ozon account. No provider I/O.

This is diagnostics, never a publication permission or a replacement for domain
preflight. Partial runs, waiting queues and observed freshness stay separate.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import time

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.exc import SQLAlchemyError

from services.marketplace_credential_expiry import expiry_notice
from models import (Marketplace, SellerMarketplaceAccount, MarketplaceCatalogSync,
                    MarketplaceReadSchedule, MarketplaceReadRequest, MarketplaceOperation,
                    BackgroundJob, db)
from services import scheduler_heartbeat

READ_SECONDS = 3
OPERATION_LIMIT = 25
DOMAIN_NAMES = ('catalog','analytics','fulfillment','finance','reviews','questions')
LABELS = {'catalog':'Каталог', 'analytics':'Аналитика', 'fulfillment':'Заказы и возвраты',
          'finance':'Финансы', 'reviews':'Отзывы', 'questions':'Вопросы'}
PATHS = {'catalog':'listings', 'analytics':'analytics', 'fulfillment':'orders',
         'finance':'finance', 'reviews':'reviews', 'questions':'reviews'}
CODES = {'provider_rate_limited','provider_unavailable','access_denied','inbox_access_denied',
         'read_budget_exhausted','account_busy','account_unavailable','credentials_changed',
         'request_expired','retry_exhausted','retry_delay_out_of_range','sync_failed',
         'ozon_inbox_access_denied','ozon_rate_limited','ozon_timeout','ozon_network_error'}


class HealthError(Exception):
    status_code = 503
    code = 'ozon_health_unavailable'


class HealthNotFound(HealthError):
    status_code = 404
    code = 'ozon_account_not_found'


def _iso(value):
    if value is None: return None
    if isinstance(value, datetime): return value.isoformat() + 'Z'
    return value.isoformat()


def _age(value, now):
    return max(0, int((now-value).total_seconds())) if value and value <= now+timedelta(seconds=5) else None


def _code(value):
    return value if isinstance(value,str) and value in CODES else ('sync_failed' if value else None)


def _timestamp(value):
    if not isinstance(value, str) or len(value)>40: return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z','+00:00'))
        return parsed.astimezone(timezone.utc).replace(tzinfo=None) if parsed.tzinfo else parsed
    except ValueError: return None


@contextmanager
def _reader(engine):
    """One short consistent snapshot; progress handler is always removed on pool return."""
    with engine.connect() as connection:
        raw = connection.connection.driver_connection
        if connection.dialect.name != 'sqlite':
            raise HealthError('Проверка состояния пока поддерживает только текущую SQLite-конфигурацию.')
        deadline = time.monotonic()+READ_SECONDS
        busy_timeout = raw.execute('PRAGMA busy_timeout').fetchone()[0]
        try:
            raw.execute('PRAGMA busy_timeout=200')
            raw.set_progress_handler(lambda: int(time.monotonic()>deadline), 1000)
            connection.exec_driver_sql('BEGIN')
            yield connection
        except SQLAlchemyError:
            raise HealthError('Не удалось проверить состояние за отведённое время. Повторите позже.') from None
        finally:
            raw.set_progress_handler(None, 0)
            connection.rollback()
            raw.execute(f'PRAGMA busy_timeout={int(busy_timeout)}')


def _scope(model, account):
    return (model.seller_id==account['seller_id'], model.marketplace_id==account['marketplace_id'],
            model.account_id==account['id'])


def _snapshot(connection, model, scope, *, completed=False, extra=()):
    columns = [model.id,model.status,model.started_at,model.completed_at,model.page_count,model.error_code]
    if model is not MarketplaceCatalogSync:
        columns += [model.period_start,model.period_end,model.contract_version,model.request_fingerprint]
    query = select(*columns).where(*scope,*extra)
    if completed: query=query.where(model.status=='completed').order_by(model.completed_at.desc(),model.id.desc())
    else: query=query.order_by(model.id.desc())
    return connection.execute(query.limit(1)).mappings().first()


def _valid_snapshot(spec, row):
    if row is None: return False
    from services.marketplace_inbox import MarketplaceInboxService
    from services.ozon_analytics_contracts import request_fingerprint
    service = MarketplaceInboxService if spec.source_kind else spec.service
    if row['contract_version'] != service.CONTRACT_VERSION: return False
    start,end = row['period_start'],row['period_end']
    if not start or not end or start>end: return False
    if spec.source_kind:
        fingerprint=service._run_fingerprint(source_kind=spec.source_kind,period_start=start,period_end=end)
    elif spec.default_period and spec.model.__tablename__ == 'marketplace_analytics_syncs':
        fingerprint=request_fingerprint(period_start=start,period_end=end)
    else: fingerprint=service._run_fingerprint(start,end)
    return row['request_fingerprint']==fingerprint and (end-start).days==(89 if spec.source_kind else 29)


def _catalog_queue(connection, account, now):
    job=BackgroundJob
    valid = case((func.json_valid(job.result_data),job.result_data),else_='{}')
    row=connection.execute(select(job.id,job.status,job.created_at,job.updated_at,
        func.json_extract(valid,'$.code').label('code'),
        func.json_extract(valid,'$.next_retry_at').label('due'),
        func.json_extract(valid,'$.account_id').label('account_id'),
    ).where(job.seller_id==account['seller_id'],job.job_type=='ozon_account_sync',
            job.job_uid.like(f"oc:{account['id']}:%")).order_by(job.id.desc()).limit(1)).mappings().first()
    if not row or row['account_id'] != account['id']: return None
    due = _timestamp(row['due'])
    code = row['code']
    if code in ('queued','catalog_queued','checking','catalog','completed','empty'): code=None
    elif isinstance(code,str):
        code={'ozon_auth_error':'access_denied','ozon_catalog_access_denied':'access_denied',
              'ozon_temporarily_unavailable':'provider_unavailable','job_expired':'request_expired',
              'account_disconnected':'account_unavailable'}.get(code,code)
    return {'active':row['status'] in ('pending','running'), 'status':row['status'],
            'age_seconds':_age(row['created_at'],now), 'next_attempt_at':_iso(due),
            'cooldown_until':_iso(due) if due and due>now else None,'error_code':_code(code)}


def _domain(connection, account, name, now):
    from services.ozon_read_scheduler import _domain as read_domain
    from services.ozon_catalog_scheduler import CATALOG_INTERVAL
    spec = None if name=='catalog' else read_domain(name)
    model = MarketplaceCatalogSync if spec is None else spec.model
    scope = _scope(model,account)
    if spec and spec.source_kind: scope += (model.source_kind==spec.source_kind,)
    # Automatic scope is deliberately 30d, independent from a newer manual 7d run.
    extra = (model.period_code==spec.default_period,) if spec and not spec.source_kind else ()
    last = _snapshot(connection,model,scope,extra=extra)
    good = _snapshot(connection,model,scope,completed=True,extra=extra)
    ttl = int((CATALOG_INTERVAL if spec is None else spec.service.CACHE_TTL).total_seconds())
    valid = good is not None and (spec is None or _valid_snapshot(spec,good))
    age = _age(good['completed_at'],now) if valid else None
    window_current = spec is None or good is not None and good['period_end']==now.date()
    freshness = 'unknown' if age is None else 'fresh' if age<=ttl and window_current else 'stale'
    queue=None; schedule=None; overdue=None
    if spec:
        s=MarketplaceReadSchedule
        schedule=connection.execute(select(s.status,s.next_due_at,s.cooldown_until,s.last_error_code,
            s.consecutive_failures,s.last_attempt_at,s.lease_expires_at).where(*_scope(s,account),s.domain==name)).mappings().first()
        r=MarketplaceReadRequest
        active=connection.execute(select(r.id,r.status,r.period_code,r.requested_at,r.credential_version).where(
            *_scope(r,account),r.domain==name,r.status.in_(('pending','running'))
        ).order_by(r.requested_at,r.id).limit(3)).mappings().all()
        if active:
            first=active[0]
            paused = first['credential_version']!=account['credential_version'] or now-first['requested_at']>timedelta(hours=24)
            queue={'active':not paused, 'status':'paused' if paused else first['status'],
                   'age_seconds':_age(first['requested_at'],now), 'period':first['period_code'],
                   'pending_count':len(active),'truncated':len(active)>2,
                   'next_attempt_at':_iso(schedule['next_due_at']) if schedule else None}
        if schedule and schedule['next_due_at'] and schedule['next_due_at']<=now:
            if not schedule['cooldown_until'] or schedule['cooldown_until']<=now:
                overdue = _age(schedule['next_due_at'],now)
    else: queue=_catalog_queue(connection,account,now)
    cooldown = schedule['cooldown_until'] if schedule else _timestamp(queue.get('cooldown_until')) if queue else None
    code = (_code(schedule['last_error_code']) if schedule and schedule['last_error_code'] else
            queue['error_code'] if name=='catalog' and queue and queue['error_code'] else
            _code(last['error_code']) if last else None)
    denied = code in ('access_denied','inbox_access_denied','ozon_inbox_access_denied')
    roles = account['capabilities']
    activity = ('not_enabled' if not account['read_enabled'] else
                'account_unavailable' if account['state']!='connected' else
                'access_unconfirmed' if spec and spec.capability and spec.capability not in roles else
                'access_denied' if denied else
                'waiting' if cooldown and cooldown>now else
                'paused' if queue and queue['status']=='paused' else
                'running' if (queue and queue['active'] and queue['status']=='running'
                    or last and last['status']=='running'
                    or schedule and schedule['lease_expires_at'] and schedule['lease_expires_at']>now) else
                'pending' if queue and queue['active'] else
                'failed' if (last and last['status']=='failed' or queue and queue['status']=='failed') else 'idle')
    warning = activity in ('account_unavailable','access_unconfirmed','access_denied','paused','failed') or freshness!='fresh' or bool(overdue and overdue>900)
    snapshot=None
    if good:
        snapshot={'id':good['id'],'completed_at':_iso(good['completed_at']), 'age_seconds':age,
                  'pages':int(good['page_count'] or 0),'contract_confirmed':valid}
        if spec: snapshot.update(period=spec.default_period, period_start=_iso(good['period_start']),period_end=_iso(good['period_end']))
    return {'domain':name,'label':LABELS[name], 'url':f"/marketplaces/{PATHS[name]}?account_id={account['id']}"+('&source_kind=question' if name=='questions' else ''),
            'freshness':freshness,'fresh_for_seconds':ttl,'activity':activity,'needs_attention':warning,
            'last_complete':snapshot,'latest_attempt':{'id':last['id'],'status':last['status'],
                'started_at':_iso(last['started_at']),'error_code':_code(last['error_code'])} if last else None,
            'queue':queue,'cooldown_until':_iso(cooldown) if cooldown and cooldown>now else None,
            'due_lag_seconds':overdue,'consecutive_failures':schedule['consecutive_failures'] if schedule else 0,
            'last_error_code':code}


def observe_account(*, seller_id, account_id, config, engine=None, now=None, scheduler_state=None):
    if any(type(v) is not int or v<=0 for v in (seller_id,account_id)):
        raise HealthNotFound('Магазин Ozon не найден.')
    now = now or datetime.utcnow()
    engine = engine or db.engine
    with _reader(engine) as connection:
        a=SellerMarketplaceAccount;m=Marketplace
        row=connection.execute(select(a.id,a.seller_id,a.marketplace_id,a.label,a.is_active,
            a.connection_status,a.connection_checked_at,a.credential_expires_at,a.credential_version,
            and_(a._credentials_encrypted.isnot(None),a._credentials_encrypted!='').label('has_credentials'),
            a.capabilities_json,m.is_active.label('marketplace_active')).join(m,a.marketplace_id==m.id).where(
            a.id==account_id,a.seller_id==seller_id,m.code=='ozon').limit(1)).mappings().first()
        if row is None: raise HealthNotFound('Магазин Ozon не найден.')
        account=dict(row)
        try: roles=json.loads(account.pop('capabilities_json') or '[]')
        except (ValueError,TypeError): roles=[]
        account['capabilities']=[v for v in roles if isinstance(v,str)] if isinstance(roles,list) else []
        account['read_enabled']=bool(config.get('MARKETPLACE_OZON_ENABLED',False))
        account['state']=('disabled' if not account['is_active'] or not account['marketplace_active'] else
            'credentials_missing' if not account['has_credentials'] else
            'expired' if account['credential_expires_at'] and account['credential_expires_at']<=now else
            'connected' if account['connection_status']=='connected' else 'connection_unconfirmed')
        domains=[_domain(connection,account,name,now) for name in DOMAIN_NAMES]
        op=MarketplaceOperation
        operations=connection.execute(select(op.id,op.operation_kind,op.status,op.attempt_count,op.created_at,
            op.updated_at,op.next_poll_at,op.deadline_at).where(*_scope(op,account),op.status.in_(('uncertain','submitting','submitted','polling'))
            ).order_by(op.created_at,op.id).limit(OPERATION_LIMIT+1)).mappings().all()
        relevant=[]
        for item in operations[:OPERATION_LIMIT]:
            check_state = ('expired' if item['deadline_at'] and item['deadline_at']<=now else
                           'submitting' if item['status']=='submitting' else
                           'scheduled' if item['next_poll_at'] is not None else
                           'stopped' if item['status']=='uncertain' else 'not_scheduled')
            relevant.append({'id':item['id'],'kind':item['operation_kind'],'status':item['status'],
                'attempts':item['attempt_count'],'age_seconds':_age(item['created_at'],now),
                'automatic_check_stopped':check_state in ('expired','stopped'), 'check_state':check_state,
                'next_check_at':_iso(item['next_poll_at']), 'url':f"/marketplaces/operations/{item['id']}"})
    heartbeat = scheduler_state if scheduler_state is not None else scheduler_heartbeat.observe()
    credential_expiry = expiry_notice(account['credential_expires_at'], now=now,
        active=bool(account['is_active'] and account['has_credentials'] and account['marketplace_active']))
    attention = credential_expiry['needs_attention'] or account['state']!='connected' or not account['read_enabled'] or heartbeat['state']!='healthy' or bool(relevant) or any(d['needs_attention'] for d in domains)
    return {'scope':{'marketplace':'ozon','account_id':account_id}, 'observed_at':_iso(now),
            'observation_only':True,'needs_attention':attention,
            'account':{'id':account_id,'label':account['label'],'state':account['state'],
                'checked_at':_iso(account['connection_checked_at']),'expires_at':_iso(account['credential_expires_at']),
                'credential_expiry':credential_expiry,'read_enabled':account['read_enabled'],'settings_url':f'/marketplaces/accounts?account_id={account_id}'},
            'scheduler':heartbeat,'domains':domains,'operations':relevant,'operations_truncated':len(operations)>OPERATION_LIMIT,
            'publication_permission_evaluated':False}
