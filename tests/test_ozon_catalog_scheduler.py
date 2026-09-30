"""Periodic catalogs reuse durable jobs and observed freshness, without HTTP."""
from datetime import datetime, timedelta
from unittest.mock import patch
import pytest

from models import BackgroundJob, MarketplaceCatalogSync, SellerMarketplaceAccount, db
from services.ozon_account_sync import _defer, JOB_TYPE, enqueue_account_sync
from services.ozon_catalog_scheduler import enqueue_due_catalogs, run_catalog_discovery_tick
from services.marketplace_accounts import MarketplaceAccountConflict
from tests.test_ozon_account_sync import setup, connected, enqueue, advance, Catalog


def extra_account(setup, number):
    account = SellerMarketplaceAccount(
        seller_id=setup.seller_id, marketplace_id=setup.account.marketplace_id,
        external_account_id=f'synthetic-{number}', label=f'Test {number}',
        is_active=True, connection_status='connected', _credentials_encrypted='synthetic',
    )
    db.session.add(account)
    db.session.flush()
    return account


def test_discovery_queues_three_then_progresses_and_deduplicates_manual(setup):
    connected(setup)
    for number in range(4):
        extra_account(setup, number)
    db.session.commit()
    with patch('requests.sessions.Session.request', side_effect=AssertionError('Discovery HTTP')):
        assert enqueue_due_catalogs() == 3
        assert enqueue_due_catalogs() == 2
        assert enqueue_due_catalogs() == 0
        manual = enqueue_account_sync(seller_id=setup.seller_id, account_id=setup.account_id)
    assert BackgroundJob.query.count() == 5
    assert manual['job_uid'] == BackgroundJob.query.order_by(BackgroundJob.id).first().job_uid


def test_completed_catalog_is_fresh_then_reuses_same_worker_for_new_sweep(setup):
    connected(setup)
    now = datetime.utcnow()
    assert enqueue_due_catalogs(now=now) == 1
    job = BackgroundJob.query.one()
    for _ in range(3):
        advance(setup, job.id, Catalog())
    assert job.status == 'completed'
    run = MarketplaceCatalogSync.query.one()
    assert enqueue_due_catalogs(now=run.completed_at + timedelta(minutes=59)) == 0
    assert enqueue_due_catalogs(now=run.completed_at + timedelta(hours=1, seconds=1)) == 1
    second = BackgroundJob.query.order_by(BackgroundJob.id.desc()).first()
    assert second.id != job.id
    for _ in range(3):
        advance(setup, second.id, Catalog())
    assert second.status == 'completed'
    assert MarketplaceCatalogSync.query.count() == 2


def test_recent_failed_and_future_cooldowns_cannot_starve_due_account(setup):
    connected(setup)
    now = datetime.utcnow()
    # More blocked accounts than the bounded discovery candidate window.
    for number in range(105):
        account = extra_account(setup, number)
        job = BackgroundJob(seller_id=setup.seller_id, job_uid=f'oc:{account.id}:synthetic',
                            job_type=JOB_TYPE, status='failed', updated_at=now-timedelta(days=2))
        job.set_progress({'auto_retry_not_before': (now+timedelta(days=2)).isoformat()+'Z'})
        db.session.add(job)
    db.session.commit()
    assert enqueue_due_catalogs(now=now) == 1
    assert BackgroundJob.query.filter_by(status='pending').one().job_uid.startswith(f'oc:{setup.account_id}:')


def test_failed_job_waits_day_and_preserves_long_provider_retry_after(setup):
    connected(setup)
    now = datetime.utcnow()
    job = enqueue(setup)
    _defer(job, job.get_progress(), 'ozon_rate_limited', now, retry_after=3*86400)
    assert job.status == 'failed'
    assert job.get_result()['next_retry_at'] == (now+timedelta(days=3)).isoformat()+'Z'
    with pytest.raises(MarketplaceAccountConflict) as error:
        enqueue_account_sync(seller_id=setup.seller_id, account_id=setup.account_id,
                             force_restart=True)
    assert error.value.code == 'ozon_catalog_cooldown'
    assert enqueue_due_catalogs(now=now+timedelta(days=2)) == 0
    # Queue checks actual wall time again; exercise the elapsed state explicitly.
    state = job.get_progress()
    state['auto_retry_not_before'] = (now-timedelta(seconds=1)).isoformat()+'Z'
    job.set_progress(state)
    job.updated_at = now-timedelta(days=4)
    db.session.commit()
    assert enqueue_due_catalogs(now=now+timedelta(days=3, seconds=1)) == 1


def test_failed_protocol_does_not_loop_every_minute(setup):
    connected(setup)
    job = enqueue(setup)
    job.status = 'failed'
    job.updated_at = now = datetime.utcnow()
    db.session.commit()
    assert enqueue_due_catalogs(now=now+timedelta(hours=23)) == 0
    assert enqueue_due_catalogs(now=now+timedelta(hours=24, seconds=1)) == 1


def test_disabled_or_expired_accounts_are_not_enrolled(setup):
    connected(setup)
    now = datetime.utcnow()
    setup.account.credential_expires_at = now-timedelta(seconds=1)
    db.session.commit()
    assert enqueue_due_catalogs(now=now) == 0
    setup.account.credential_expires_at = None
    setup.account.is_active = False
    db.session.commit()
    assert enqueue_due_catalogs(now=now) == 0
    setup.account.is_active = True
    setup.account.connection_status = 'invalid'
    db.session.commit()
    assert enqueue_due_catalogs(now=now) == 0
    setup.app.config['MARKETPLACE_OZON_ENABLED'] = False
    assert run_catalog_discovery_tick(setup.app) == 0


def test_active_deferred_job_is_not_duplicated_after_restart(setup):
    connected(setup)
    job = enqueue(setup)
    _defer(job, job.get_progress(), 'ozon_rate_limited', datetime.utcnow(), retry_after=3600)
    job_id = job.id
    db.session.remove()
    assert enqueue_due_catalogs() == 0
    assert BackgroundJob.query.one().id == job_id
