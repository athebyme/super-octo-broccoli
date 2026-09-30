"""Durable, account-scoped local quality recomputation; no provider/LLM I/O."""
from datetime import datetime, timedelta
import logging
import uuid

from sqlalchemy import func
from models import BackgroundJob, MarketplaceListing, db
from services.marketplace_quality import (MarketplaceQualityService, MarketplaceQualityBusy,
    MarketplaceQualityValidationError)
from services.marketplace_operation_locks import _try_operation_lock

JOB_TYPE = 'ozon_quality_recompute'
ACTIVE = ('pending', 'running')
BATCH_SIZE = 200
MAX_AGE = timedelta(hours=24)
MESSAGES = {
    'queued': 'Пересчёт оценок в очереди. Страницу можно закрыть.',
    'running': 'Пересчитываем оценки по сохранённым данным. Страницу можно закрыть.',
    'completed': 'Пересчёт завершён. Откройте обновлённые оценки.',
    'expired': 'Пересчёт не завершился за сутки. Запустите его снова.',
    'account_unavailable': 'Магазин отключён. Сохранённые оценки доступны.',
    'failed': 'Не удалось завершить пересчёт. Сохранённые оценки доступны; повторите позже.',
}
logger = logging.getLogger(__name__)


def _account(seller_id, account_id):
    return MarketplaceQualityService._account(seller_id=seller_id, account_id=account_id)


def _query(seller_id, account_id):
    return BackgroundJob.query.filter(BackgroundJob.seller_id == seller_id,
        BackgroundJob.job_type == JOB_TYPE, BackgroundJob.job_uid.like(f'oq:{account_id}:%'))


def _listings(account):
    return MarketplaceListing.query.filter_by(seller_id=account.seller_id,
        marketplace_id=account.marketplace_id, account_id=account.id,
        is_available=True, is_archived=False)


def _public(job, account, now):
    if job is None:
        return None
    result = job.get_result()
    if result.get('account_id') != account.id or result.get('marketplace_id') != account.marketplace_id:
        raise MarketplaceQualityValidationError('Не удалось подтвердить магазин пересчёта.')
    code = result.get('code')
    if code not in MESSAGES:
        code = 'failed'
    status = job.status
    if status in ACTIVE and (not account.is_active or not account.marketplace.is_active):
        status, code = 'paused', 'account_unavailable'
    elif status in ACTIVE and job.created_at < now - MAX_AGE:
        status, code = 'paused', 'expired'
    return {'job_uid':job.job_uid, 'account_id':account.id, 'marketplace_code':'ozon',
        'status':status, 'active':status in ACTIVE, 'code':code, 'message':MESSAGES[code],
        'processed':job.processed or 0, 'initial_total':job.total or 0,
        'scored':job.succeeded or 0,
        'requested_at':job.created_at.isoformat()+'Z',
        'updated_at':job.updated_at.isoformat()+'Z'}


def latest_quality_job(*, seller_id, account_id, now=None):
    account = _account(seller_id, account_id)
    job = _query(seller_id, account_id).order_by(BackgroundJob.id.desc()).first()
    return _public(job, account, now or datetime.utcnow())


def _result(job, account, code):
    job.set_result({'account_id':account.id, 'marketplace_id':account.marketplace_id, 'code':code})
    job.error_message = MESSAGES[code] if job.status == 'failed' else None


def enqueue_quality(*, seller_id, account_id, now=None):
    now = now or datetime.utcnow()
    account = _account(seller_id, account_id)
    if not account.is_active or not account.marketplace.is_active:
        raise MarketplaceQualityValidationError(MESSAGES['account_unavailable'])
    claim = _try_operation_lock('ozon-quality-job', account.id)
    if claim is None:
        existing = _query(seller_id, account_id).filter(BackgroundJob.status.in_(ACTIVE)).first()
        if existing:
            return _public(existing, account, now)
        raise MarketplaceQualityBusy('Пересчёт уже ставится в очередь. Проверьте его состояние.')
    try:
        db.session.expire_all()
        account = _account(seller_id, account_id)
        if not account.is_active or not account.marketplace.is_active:
            raise MarketplaceQualityValidationError(MESSAGES['account_unavailable'])
        existing = _query(seller_id, account_id).filter(BackgroundJob.status.in_(ACTIVE)).first()
        if existing and existing.created_at >= now - MAX_AGE:
            return _public(existing, account, now)
        # Read the catalog boundary before any writes; no OFFSET cursor drift.
        total, upper = _listings(account).with_entities(func.count(MarketplaceListing.id),
            func.max(MarketplaceListing.id)).one()
        if existing:
            existing.status = 'failed'
            _result(existing, account, 'expired')
        job = BackgroundJob(job_uid=f'oq:{account.id}:{uuid.uuid4().hex}',seller_id=seller_id,
            job_type=JOB_TYPE,status='pending',total=total,processed=0,succeeded=0,
            created_at=now,updated_at=now)
        job.set_progress({'account_id':account.id,'marketplace_id':account.marketplace_id,
            'after_id':0,'upper_id':upper or 0})
        _result(job, account, 'queued')
        db.session.add(job)
        db.session.commit()
        return _public(job, account, now)
    except Exception:
        db.session.rollback()
        raise
    finally:
        claim.close()


def _state(job):
    state = job.get_progress()
    required = {'account_id','marketplace_id','after_id','upper_id'}
    if (not isinstance(state,dict) or set(state)!=required
        or any(type(v) is not int or v < 0 for v in state.values())
        or state['account_id']<=0 or state['marketplace_id']<=0
        or state['after_id']>state['upper_id']
        or not job.job_uid.startswith(f"oq:{state['account_id']}:")):
        raise ValueError('invalid quality checkpoint')
    return state


def run_quality_tick(app, *, now=None):
    """Advance at most one batch; oldest-updated job rotates between accounts."""
    with app.app_context():
        if not app.config.get('MARKETPLACE_OZON_ENABLED',False):
            return {'processed':0,'disabled':True}
        now = now or datetime.utcnow()
        candidates = BackgroundJob.query.filter(BackgroundJob.job_type==JOB_TYPE,
            BackgroundJob.status.in_(ACTIVE)).order_by(BackgroundJob.updated_at,BackgroundJob.id).limit(20).all()
        for candidate in candidates:
            try:
                state = _state(candidate)
            except ValueError:
                candidate.status='failed';candidate.error_message=MESSAGES['failed']
                candidate.set_progress({});candidate.set_result({'code':'failed'})
                db.session.commit()
                return {'processed':0,'failed':True}
            claim = _try_operation_lock('ozon-quality-job',state['account_id'])
            if claim is None:
                continue
            job_id = candidate.id
            try:
                db.session.expire_all()
                job = db.session.get(BackgroundJob,job_id)
                if job.status not in ACTIVE:
                    continue
                state = _state(job)
                account = _account(job.seller_id,state['account_id'])
                if account.marketplace_id != state['marketplace_id']:
                    raise ValueError('quality scope changed')
                if not account.is_active or not account.marketplace.is_active or job.created_at<now-MAX_AGE:
                    job.status='failed';job.updated_at=now
                    _result(job,account,'expired' if job.created_at<now-MAX_AGE else 'account_unavailable')
                    db.session.commit()
                    return {'processed':0,'failed':True}
                ids = [r[0] for r in _listings(account).with_entities(MarketplaceListing.id).filter(
                    MarketplaceListing.id>state['after_id'],MarketplaceListing.id<=state['upper_id']
                ).order_by(MarketplaceListing.id).limit(BATCH_SIZE).all()]
                job.status='running';job.updated_at=now;_result(job,account,'running');db.session.commit()
                result = MarketplaceQualityService.recompute_account(seller_id=account.seller_id,
                    account_id=account.id,listing_ids=ids,limit=len(ids),now=now) if ids else {'scored':0}
                # Pure local recompute may repeat after a crash before this checkpoint.
                job = db.session.get(BackgroundJob,job_id)
                job.processed += len(ids);job.succeeded += int(result.get('scored') or 0)
                state['after_id'] = ids[-1] if ids else state['upper_id']
                job.set_progress(state);job.updated_at=now
                if len(ids)<BATCH_SIZE or state['after_id']==state['upper_id']:
                    job.status='completed';_result(job,account,'completed')
                db.session.commit()
                return {'processed':len(ids),'completed':job.status=='completed'}
            except MarketplaceQualityBusy:
                db.session.rollback()
                job=db.session.get(BackgroundJob,job_id);job.updated_at=now;db.session.commit()
                return {'processed':0,'busy':True}
            except Exception as error:
                db.session.rollback()
                job=db.session.get(BackgroundJob,job_id)
                if job and job.status in ACTIVE:
                    job.status='failed';job.updated_at=now;job.error_message=MESSAGES['failed']
                    result=job.get_result();result['code']='failed';job.set_result(result);db.session.commit()
                logger.error('Local Ozon quality recompute failed: %s',type(error).__name__)
                return {'processed':0,'failed':True}
            finally:
                claim.close()
        return {'processed':0}
