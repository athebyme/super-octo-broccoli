from datetime import datetime, timedelta, timezone
import json
from unittest.mock import Mock

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from models import db, SellerMarketplaceAccount as Account, MarketplaceCredentialNotice as Notice, Notification, Marketplace, User, Seller
from services.marketplace_credential_expiry import expiry_notice
from services import ozon_credential_notices as worker
from services.marketplace_operation_locks import try_account_operation_lock
from tests.test_ozon_read_scheduler import scope, NOW


@pytest.mark.parametrize('seconds,stage', [(14*86400+1,0),(14*86400,1),(7*86400+1,1),
    (7*86400,2),(86400+1,2),(86400,3),(1,3),(0,4),(-1,4)])
def test_exact_utc_boundaries(seconds, stage):
    value=expiry_notice(NOW+timedelta(seconds=seconds),now=NOW)
    assert value['stage']==stage and value['needs_attention']==(stage>0)
    assert value['expires_at'].endswith('Z')


def test_unknown_inactive_and_aware_dates_do_not_invent_expiry():
    assert expiry_notice(None,now=NOW)['state']=='unknown'
    assert expiry_notice('2026-09-25',now=NOW)['expires_at'] is None
    assert expiry_notice(NOW,active=False,now=NOW)['state']=='inactive'
    aware=(NOW+timedelta(hours=3)).replace(tzinfo=timezone(timedelta(hours=3)))
    assert expiry_notice(aware,now=NOW)['state']=='expired'


def expire(scope, days=10):
    a=db.session.get(Account,scope.ids[0]);a.credential_expires_at=NOW+timedelta(days=days)
    db.session.commit()
    return a.id


def test_stages_are_atomic_deduplicated_and_deleted_notification_stays_deleted(scope,monkeypatch):
    aid=expire(scope)
    decrypt=Mock(side_effect=AssertionError('no decryption'))
    monkeypatch.setattr(Account,'get_credentials',decrypt)
    for now,stage in [(NOW,1),(NOW+timedelta(days=3),2),(NOW+timedelta(days=9),3),(NOW+timedelta(days=10),4)]:
        assert worker.notify_due_credentials(now=now)==1
        row=Notification.query.one();metadata=json.loads(row.metadata_json)
        assert row.seller_id==scope.seller_id and metadata['stage']==stage
        assert str(aid) in row.link and 'action=replace-key' in row.link
        assert 'secret' not in row.message+row.metadata_json
        assert db.session.get(Notice,aid).highest_stage==stage
        row.is_read=True;db.session.commit()
        assert worker.notify_due_credentials(now=now)==0
        Notification.query.delete();db.session.commit();db.session.remove()
        assert worker.notify_due_credentials(now=now)==0
    decrypt.assert_not_called()


def test_long_downtime_emits_only_current_stage_and_old_clock_does_not_repeat(scope):
    aid=expire(scope,days=-2)
    assert worker.notify_due_credentials(now=NOW)==1
    assert Notification.query.count()==1 and db.session.get(Notice,aid).highest_stage==4
    assert worker.notify_due_credentials(now=NOW-timedelta(days=3))==0


def test_rotation_and_new_expiry_start_new_series_but_unknown_does_not(scope):
    aid=expire(scope)
    assert worker.notify_due_credentials(now=NOW)==1
    a=db.session.get(Account,aid);a.credential_version+=1;a.credential_expires_at=None;db.session.commit()
    assert worker.notify_due_credentials(now=NOW)==0
    a=db.session.get(Account,aid);a.credential_expires_at=NOW+timedelta(days=12);db.session.commit()
    assert worker.notify_due_credentials(now=NOW)==1
    a=db.session.get(Account,aid);a.credential_expires_at=NOW+timedelta(days=13);db.session.commit()
    assert worker.notify_due_credentials(now=NOW)==1
    assert Notification.query.count()==3 and Notice.query.count()==1


@pytest.mark.parametrize('change', ['unknown','future','disabled','missing','disconnected','wb','market_disabled'])
def test_non_due_and_unsupported_accounts_never_notify(scope,change):
    aid=expire(scope);a=db.session.get(Account,aid)
    if change=='unknown':a.credential_expires_at=None
    elif change=='future':a.credential_expires_at=NOW+timedelta(days=15)
    elif change=='disabled':a.is_active=False
    elif change=='missing':a._credentials_encrypted=None
    elif change=='disconnected':a.connection_status='disconnected'
    elif change=='market_disabled':a.marketplace.is_active=False
    else:a.marketplace.code='wb'
    db.session.commit()
    assert worker.notify_due_credentials(now=NOW)==0 and Notification.query.count()==0


def test_concurrent_rotation_rechecked_after_claim_and_busy_account_skipped(scope,monkeypatch):
    aid=expire(scope)
    claim=try_account_operation_lock(aid)
    try:assert worker.notify_due_credentials(now=NOW)==0
    finally:claim.close()
    original=worker.try_account_operation_lock
    def changed(identity):
        a=db.session.get(Account,identity);a.credential_version+=1;a.credential_expires_at=None;db.session.commit()
        return original(identity)
    monkeypatch.setattr(worker,'try_account_operation_lock',changed)
    assert worker.notify_due_credentials(now=NOW)==0 and Notice.query.count()==0


def test_notification_failure_rolls_back_journal_and_releases_account(scope,monkeypatch):
    aid=expire(scope)
    original=db.session.commit
    fail=Mock(side_effect=IntegrityError('synthetic',{},Exception('synthetic')))
    monkeypatch.setattr(db.session,'commit',fail)
    assert worker.notify_due_credentials(now=NOW)==0
    monkeypatch.setattr(db.session,'commit',original)
    assert Notice.query.count()==0 and Notification.query.count()==0
    assert worker.notify_due_credentials(now=NOW)==1


def test_busy_timeout_restored_before_connection_returns_to_pool(scope,monkeypatch):
    expire(scope)
    timeout=db.session.execute(text('PRAGMA busy_timeout')).scalar_one()
    db.session.rollback()
    original=db.session.commit
    observed=[]
    def checked_commit():
        observed.append(db.session.execute(text('PRAGMA busy_timeout')).scalar_one())
        return original()
    monkeypatch.setattr(db.session,'commit',checked_commit)
    assert worker.notify_due_credentials(now=NOW)==1
    assert observed==[timeout]


def test_many_not_due_accounts_do_not_starve_exact_seller_and_tick_is_bounded(scope):
    for n in range(101):scope.add(credential_expires_at=NOW+timedelta(days=90))
    aid=expire(scope)
    assert worker.notify_due_credentials(now=NOW)==1
    assert Notification.query.one().seller_id==scope.seller_id
    assert db.session.get(Notice,aid) is not None
    for n in range(30):scope.add(credential_expires_at=NOW+timedelta(days=2))
    assert worker.notify_due_credentials(now=NOW)==25
    assert worker.notify_due_credentials(now=NOW)==5
    assert worker.notify_due_credentials(now=NOW)==0


def test_existing_foreign_notice_cannot_redirect_delivery(scope):
    aid=expire(scope)
    user=User(username='notice-other',email='other@local.test',password_hash='x')
    seller=Seller(user=user,company_name='Other');db.session.add(seller);db.session.flush()
    db.session.add(Notice(account_id=aid,seller_id=seller.id,marketplace_id=scope.marketplace_id,
        credential_version=1,expires_at=NOW,highest_stage=1,notified_at=NOW));db.session.commit()
    assert worker.notify_due_credentials(now=NOW)==0 and Notification.query.count()==0


def test_disabled_feature_and_dirty_session_do_not_mutate(scope):
    aid=expire(scope)
    app=Mock(config={'MARKETPLACE_OZON_ENABLED':False})
    assert worker.run_credential_notice_tick(app)==0
    a=db.session.get(Account,aid);a.label='unsaved'
    with pytest.raises(RuntimeError,match='dirty_session'):
        worker.notify_due_credentials(now=NOW)
    assert a.label=='unsaved' and a in db.session.dirty
    db.session.rollback()


def test_rotation_requires_viewed_version_and_preserves_newer_key(scope):
    from services.marketplace_accounts import MarketplaceAccountService,MarketplaceAccountVersionConflict,MarketplaceAccountValidationError
    aid=expire(scope)
    a=db.session.get(Account,aid);version=a.version;external=a.external_account_id
    kwargs=dict(seller_id=scope.seller_id,account_id=aid,external_account_id=external)
    for invalid in (None,True,0,'1'):
        with pytest.raises(MarketplaceAccountValidationError):
            MarketplaceAccountService.rotate_ozon_key(**kwargs,api_key='candidate-key',expected_version=invalid)
    new=MarketplaceAccountService.rotate_ozon_key(**kwargs,api_key='first-reviewed-key',expected_version=version)
    current=new.version
    for _ in range(2):
        with pytest.raises(MarketplaceAccountVersionConflict):
            MarketplaceAccountService.rotate_ozon_key(**kwargs,api_key='old-tab-key',expected_version=version)
    a=db.session.get(Account,aid)
    assert a.version==current and a.get_credentials()['api_key']=='first-reviewed-key'
    assert a.credential_expires_at is None
