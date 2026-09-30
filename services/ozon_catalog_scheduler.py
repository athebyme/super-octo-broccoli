"""Bounded periodic catalog discovery using the existing durable sync queue.

The latest complete catalog is the freshness fact; active/failed queue rows
own retry timing. Discovery never decrypts credentials or calls the provider.
"""
from datetime import datetime, timedelta
import logging

from sqlalchemy import String, cast, case, func, literal, or_, select

from models import BackgroundJob, Marketplace, MarketplaceCatalogSync, SellerMarketplaceAccount, db
from services.marketplace_accounts import MarketplaceAccountError
from services.marketplace_operation_locks import _try_operation_lock
from services.ozon_account_sync import ACTIVE, JOB_TYPE, enqueue_account_sync


logger = logging.getLogger(__name__)
CATALOG_INTERVAL = timedelta(hours=1)
FAILED_RETRY_INTERVAL = timedelta(hours=24)
DISCOVERY_LIMIT = 100
ENQUEUE_LIMIT = 3


def _due_accounts(now):
    account = SellerMarketplaceAccount
    job_scope = (
        BackgroundJob.seller_id == account.seller_id,
        BackgroundJob.job_type == JOB_TYPE,
        BackgroundJob.job_uid.like(literal('oc:') + cast(account.id, String) + literal(':%')),
    )
    def latest(column):
        return select(column).where(*job_scope).order_by(BackgroundJob.id.desc()).limit(1).correlate(account).scalar_subquery()

    last_attempt = latest(BackgroundJob.updated_at)
    last_status = latest(BackgroundJob.status)
    # A provider delay longer than a job's lifetime must survive its failure.
    not_before = latest(case((func.json_valid(BackgroundJob.progress_data),
        func.json_extract(BackgroundJob.progress_data, '$.auto_retry_not_before')), else_=None))
    completed = select(func.max(MarketplaceCatalogSync.completed_at)).where(
        MarketplaceCatalogSync.seller_id == account.seller_id,
        MarketplaceCatalogSync.account_id == account.id,
        MarketplaceCatalogSync.marketplace_id == account.marketplace_id,
        MarketplaceCatalogSync.status == 'completed',
    ).correlate(account).scalar_subquery()
    active = select(BackgroundJob.id).where(*job_scope,
        BackgroundJob.status.in_(ACTIVE)).correlate(account).exists()
    return account.query.join(Marketplace).filter(
        Marketplace.code == 'ozon', Marketplace.is_active.is_(True),
        account.is_active.is_(True), account.connection_status == 'connected',
        account._credentials_encrypted.isnot(None), account._credentials_encrypted != '',
        or_(account.credential_expires_at.is_(None), account.credential_expires_at > now),
        ~active,
        or_(completed.is_(None), completed <= now - CATALOG_INTERVAL),
        or_(last_status.is_(None), last_status != 'failed',
            last_attempt <= now - FAILED_RETRY_INTERVAL),
        or_(not_before.is_(None), not_before <= now.isoformat() + 'Z'),
    ).order_by(last_attempt.asc(), account.id)


def enqueue_due_catalogs(*, now=None):
    """Enroll at most three due accounts, with filtering before the limit."""
    now = now or datetime.utcnow()
    claim = _try_operation_lock('ozon-catalog-discovery', 1)
    if claim is None:
        return 0
    queued = 0
    try:
        candidates = _due_accounts(now).with_entities(
            SellerMarketplaceAccount.id, SellerMarketplaceAccount.seller_id,
        ).limit(DISCOVERY_LIMIT).all()
        for account_id, seller_id in candidates:
            try:
                # Re-ground after previous short commits and before enqueue.
                if _due_accounts(now).filter(SellerMarketplaceAccount.id == account_id).first() is None:
                    continue
                enqueue_account_sync(seller_id=seller_id, account_id=account_id)
                queued += 1
                if queued >= ENQUEUE_LIMIT:
                    break
            except MarketplaceAccountError:
                db.session.rollback()
                # A manual request, key rotation or publication can win the claim.
                continue
        return queued
    finally:
        claim.close()


def run_catalog_discovery_tick(flask_app):
    if not flask_app.config.get('MARKETPLACE_OZON_ENABLED', False):
        return 0
    with flask_app.app_context():
        try:
            return enqueue_due_catalogs()
        except Exception:
            db.session.rollback()
            logger.warning('Ozon periodic catalog discovery failed safely')
            return 0
        finally:
            db.session.remove()
