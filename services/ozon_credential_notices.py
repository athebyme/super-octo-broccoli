"""Bounded in-app expiry notices using observed facts, never provider I/O."""
from datetime import datetime, timedelta
import json
import logging
import time

from sqlalchemy import and_, case, or_, select, text
from sqlalchemy.exc import SQLAlchemyError

from models import db, Marketplace, SellerMarketplaceAccount as Account, MarketplaceCredentialNotice as Notice, Notification
from services.marketplace_credential_expiry import expiry_notice
from services.marketplace_operation_locks import _try_operation_lock, try_account_operation_lock

logger = logging.getLogger(__name__)
DISCOVERY_LIMIT = 100
NOTICE_LIMIT = 25
TICK_SECONDS = 5


def _due(now):
    rank = case((Account.credential_expires_at <= now, 4),
                (Account.credential_expires_at <= now + timedelta(days=1), 3),
                (Account.credential_expires_at <= now + timedelta(days=7), 2), else_=1)
    return select(Account.id).join(Marketplace, Marketplace.id == Account.marketplace_id).outerjoin(
        Notice, Notice.account_id == Account.id).where(
        Marketplace.code == 'ozon', Marketplace.is_active.is_(True), Account.is_active.is_(True),
        Account.connection_status != 'disconnected',
        Account._credentials_encrypted.isnot(None), Account._credentials_encrypted != '',
        Account.credential_version > 0, Account.credential_expires_at.isnot(None),
        Account.credential_expires_at <= now + timedelta(days=14),
        or_(Notice.account_id.is_(None), and_(Notice.seller_id == Account.seller_id,
            Notice.marketplace_id == Account.marketplace_id,
            or_(Notice.credential_version != Account.credential_version,
                Notice.expires_at != Account.credential_expires_at, Notice.highest_stage < rank))),
    ).order_by(Account.credential_expires_at, Account.id)


def _write_notice_transaction(account_id, now):
    """Caller owns the account flock; acquire a short DB writer before re-read."""
    # No caller mutations may be silently committed or rolled back by this tick.
    if db.session.new or db.session.dirty or db.session.deleted:
        raise RuntimeError('credential_notice_dirty_session')
    if db.engine.dialect.name == 'sqlite':
        raw = db.session.connection().connection.driver_connection
        old_timeout = raw.execute('PRAGMA busy_timeout').fetchone()[0]
        try:
            raw.execute('PRAGMA busy_timeout=200')
            db.session.execute(text('BEGIN IMMEDIATE'))
        finally:
            # Restore while this session still owns the connection. Restoring
            # after commit could alter a connection already reused by a request.
            raw.execute(f'PRAGMA busy_timeout={int(old_timeout)}')
    identity = db.session.execute(_due(now).where(Account.id == account_id)).scalar_one_or_none()
    if identity is None:
        db.session.rollback()
        return False
    account = db.session.get(Account, identity)
    notice = expiry_notice(account.credential_expires_at, now=now)
    if not notice['needs_attention']:
        db.session.rollback()
        return False
    previous = db.session.get(Notice, identity)
    if previous is None:
        previous = Notice(account_id=identity, seller_id=account.seller_id,
                          marketplace_id=account.marketplace_id)
        db.session.add(previous)
    previous.credential_version = account.credential_version
    previous.expires_at = account.credential_expires_at
    previous.highest_stage = notice['stage']
    previous.notified_at = now
    db.session.add(Notification(
        seller_id=account.seller_id, category='error' if notice['stage'] == 4 else 'warning',
        title='Проверьте срок ключа Ozon',
        message=f'Магазин «{account.label}». На момент проверки: {notice["message"]}',
        link=f'/marketplaces/accounts/?account_id={identity}&action=replace-key#ozon-account-{identity}',
        metadata_json=json.dumps({'source': 'ozon_credential_expiry', 'account_id': identity,
            'credential_version': account.credential_version, 'expires_at': notice['expires_at'],
            'stage': notice['stage'], 'observed_at': now.isoformat() + 'Z'}, ensure_ascii=False),
        created_at=now,
    ))
    db.session.commit()
    return True


def _write_notice(account_id, now):
    if db.session.new or db.session.dirty or db.session.deleted:
        raise RuntimeError('credential_notice_dirty_session')
    db.session.rollback()
    return _write_notice_transaction(account_id, now)


def notify_due_credentials(*, now=None):
    if db.session.new or db.session.dirty or db.session.deleted:
        raise RuntimeError('credential_notice_dirty_session')
    now = now or datetime.utcnow()
    lock = _try_operation_lock('ozon-credential-notices', 1)
    if lock is None:
        return 0
    emitted = 0
    deadline = time.monotonic() + TICK_SECONDS
    try:
        candidates = list(db.session.execute(_due(now).limit(DISCOVERY_LIMIT)).scalars())
        db.session.rollback()
        for account_id in candidates:
            if emitted >= NOTICE_LIMIT or time.monotonic() >= deadline:
                break
            claim = try_account_operation_lock(account_id)
            if claim is None:
                continue
            try:
                emitted += int(_write_notice(account_id, now))
            except SQLAlchemyError:
                db.session.rollback()
                logger.warning('Ozon credential notice deferred after local database contention')
                break
            finally:
                claim.close()
        return emitted
    finally:
        lock.close()


def run_credential_notice_tick(flask_app):
    if not flask_app.config.get('MARKETPLACE_OZON_ENABLED', False):
        return 0
    with flask_app.app_context():
        try:
            return notify_due_credentials()
        except Exception:
            db.session.rollback()
            logger.warning('Ozon credential expiry notices failed safely')
            return 0
        finally:
            db.session.remove()
