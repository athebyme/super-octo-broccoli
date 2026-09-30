"""Shared-worker inbox contracts. All provider I/O is synthetic and counted."""
from datetime import timedelta
from unittest.mock import Mock
import json

import pytest

from models import db, MarketplaceReadSchedule, MarketplaceReadRequest, MarketplaceInboxSync, SellerMarketplaceAccount
from services import ozon_read_scheduler as worker
from services.ozon_read_requests import enqueue_read, read_status, record_request
from services.marketplace_inbox import MarketplaceInboxService as Inbox
from services.ozon_api_client import OzonAPIError, OzonRateLimitError, OzonSellerAPIClient
from services.marketplace_accounts import MarketplaceAccountValidationError
from tests.test_ozon_read_scheduler import scope, NOW


@pytest.fixture
def inbox(scope):
    account = db.session.get(SellerMarketplaceAccount, scope.ids[0])
    account.capabilities_json = json.dumps(['reviews_read','questions_read'])
    db.session.commit()
    return scope


def enqueue(scope, domain='reviews', **kwargs):
    return enqueue_read(seller_id=scope.seller_id, account_id=scope.ids[0], domain=domain,
                        period_code='90d', force=True, now=kwargs.get('now', NOW))


def status(scope, domain='reviews', now=NOW):
    return read_status(seller_id=scope.seller_id, account_id=scope.ids[0], domain=domain,
                       period_code='90d', now=now)


def empty(client, endpoint, payload):
    assert not db.session.connection().connection.driver_connection.in_transaction
    return {'reviews' if 'review' in endpoint else 'questions': [], 'has_next':False, 'last_id':None}


@pytest.mark.parametrize('domain,kind', [('reviews','review'),('questions','question')])
def test_exact_kind_and_90_days_enqueue_dedup_and_three_pages_complete(inbox, monkeypatch, domain, kind):
    first = enqueue(inbox, domain)
    assert enqueue(inbox, domain)['id'] == first['id']
    OzonSellerAPIClient.request.assert_not_called()
    read = Mock(side_effect=empty)
    monkeypatch.setattr(OzonSellerAPIClient, 'request', lambda self,e,p: read(self,e,p))
    db.session.remove()
    assert worker.run_requested_reads(now=NOW)['completed'] == 1
    result = status(inbox, domain)
    assert result['id'] == first['id'] and result['status'] == 'completed' and not result['active']
    assert result['pages_loaded'] == 3 and read.call_count == 3
    run = db.session.get(MarketplaceInboxSync, result['snapshot_id'])
    assert run.source_kind == kind and run.period_code == '90d'
    assert run.period_start == NOW.date() - timedelta(days=89) and run.period_end == NOW.date()
    assert not {'api_key','credential_version','lease_token','cursor'} & result.keys()


def test_review_and_question_have_separate_requests_runs_and_lifetime(inbox, monkeypatch):
    review, question = enqueue(inbox), enqueue(inbox, 'questions')
    assert review['id'] != question['id']
    read = Mock(side_effect=empty)
    monkeypatch.setattr(OzonSellerAPIClient, 'request', lambda self,e,p: read(self,e,p))
    first = worker.run_requested_reads(now=NOW, limit=2)
    assert first['selected'] == first['completed'] == 1
    assert read.call_count == 3
    assert status(inbox)['status'] == 'completed'
    assert status(inbox, 'questions')['status'] == 'pending'
    assert status(inbox, 'questions')['snapshot_id'] is None
    assert MarketplaceInboxSync.query.count() == 1

    second = worker.run_requested_reads(now=NOW+timedelta(seconds=10), limit=2)
    assert second['selected'] == second['completed'] == 1
    assert read.call_count == 6
    review_status, question_status = status(inbox), status(inbox, 'questions')
    assert review_status['status'] == question_status['status'] == 'completed'
    assert review_status['id'] == review['id'] and question_status['id'] == question['id']
    assert review_status['snapshot_id'] != question_status['snapshot_id']
    assert {run.source_kind for run in MarketplaceInboxSync.query.all()} == {'review', 'question'}

    renewed = enqueue(inbox, now=NOW+timedelta(seconds=11))
    assert renewed['id'] != review['id']
    assert status(inbox, now=NOW+timedelta(days=2))['error_code'] == 'request_expired'
    assert status(inbox, 'questions', now=NOW+timedelta(days=2))['snapshot_id'] == question_status['snapshot_id']
    read = Mock(side_effect=AssertionError('expired request cannot call provider'))
    monkeypatch.setattr(OzonSellerAPIClient,'request',read)
    assert worker.run_requested_reads(now=NOW+timedelta(days=2))['selected'] == 0
    assert MarketplaceReadRequest.query.filter_by(id=renewed['id']).one().error_code == 'request_expired'
    assert MarketplaceReadRequest.query.filter_by(id=question['id']).one().status == 'completed'
    read.assert_not_called()


def test_transient_retry_keeps_cursor_and_resumes_after_restart_beyond_old_stale_timeout(inbox, monkeypatch):
    enqueue(inbox)
    calls = []
    def read(client, endpoint, payload):
        assert not db.session.connection().connection.driver_connection.in_transaction
        calls.append((payload['filters']['status'],payload.get('last_id')))
        if len(calls) == 1:
            return {'reviews':[{'id':'synthetic-page-one','sku':101,'text':'Тестовый отзыв','rating':5,'status':'NEW',
                'published_at':'2026-09-24T10:00:00Z','comments_amount':0,'photos_amount':0,'videos_amount':0}],
                'has_next':True,'last_id':'next-synthetic-page'}
        if len(calls) == 2:
            raise OzonRateLimitError('synthetic',status_code=429,retry_after=5000,retriable=True)
        return empty(client,endpoint,payload)
    monkeypatch.setattr(OzonSellerAPIClient,'request',read)
    assert worker.run_requested_reads(now=NOW)['failed'] == 1
    run = MarketplaceInboxSync.query.one()
    assert run.status == 'running' and run.page_count == 1 and run.next_cursor == 'next-synthetic-page'
    identity = run.id
    due = MarketplaceReadSchedule.query.one().cooldown_until
    assert due >= NOW+timedelta(seconds=5000)
    assert enqueue(inbox, now=NOW+timedelta(seconds=1))['status'] == 'waiting'
    assert worker.run_requested_reads(now=NOW+timedelta(seconds=4999))['selected'] == 0
    db.session.remove()
    assert worker.run_requested_reads(now=due)['completed'] == 1
    assert calls[1] == calls[2] == ('NEW','next-synthetic-page')
    assert len(calls) == 5 and MarketplaceInboxSync.query.one().id == identity
    assert status(inbox, now=due)['status'] == 'completed'


def test_endpoint_denial_is_terminal_request_and_daily_automatic_pause_manual_recheck_only(inbox, monkeypatch):
    enqueue(inbox)
    read = Mock(side_effect=OzonAPIError('synthetic',code='7',status_code=400,retriable=False))
    monkeypatch.setattr(OzonSellerAPIClient,'request',read)
    assert worker.run_requested_reads(now=NOW)['unavailable'] == 1
    result = status(inbox)
    assert result['status'] == 'failed' and result['error_code'] == 'inbox_access_denied'
    row = MarketplaceReadSchedule.query.one()
    assert row.cooldown_until >= NOW+timedelta(days=1)
    assert MarketplaceInboxSync.query.one().error_code == Inbox.ACCESS_DENIED_ERROR_CODE
    assert worker.run_due_reads(domain='reviews',limit=1,now=NOW+timedelta(hours=1))['selected'] == 0
    read.assert_called_once()
    enqueue(inbox,now=NOW+timedelta(hours=1))
    assert MarketplaceReadSchedule.query.one().cooldown_until is None
    monkeypatch.setattr(OzonSellerAPIClient,'request',empty)
    assert worker.run_requested_reads(now=NOW+timedelta(hours=1))['completed'] == 1
    assert MarketplaceReadSchedule.query.one().last_error_code is None


def test_discovery_imports_only_latest_legacy_denial_and_never_probes_before_due(inbox, monkeypatch):
    account = db.session.get(SellerMarketplaceAccount,inbox.ids[0])
    for kind in ('review','question'):
        run = Inbox._create_run(account=account, source_kind=kind,
            period_start=NOW.date()-timedelta(days=89),period_end=NOW.date(),now=NOW)
        run.status='failed';run.error_code=Inbox.ACCESS_DENIED_ERROR_CODE;run.completed_at=NOW
    db.session.commit()
    assert worker.run_due_inbox_reads(now=NOW+timedelta(hours=1))['selected'] == 0
    assert MarketplaceReadSchedule.query.count() == 2
    assert all(r.cooldown_until == NOW+timedelta(days=1) for r in MarketplaceReadSchedule.query.all())
    OzonSellerAPIClient.request.assert_not_called()


@pytest.mark.parametrize('capabilities', ['[]','not-json','{"reviews_read":true}','["not_reviews_read"]'])
def test_missing_or_malformed_capability_cannot_enroll_or_enqueue(inbox, capabilities):
    account = db.session.get(SellerMarketplaceAccount,inbox.ids[0]);account.capabilities_json=capabilities;db.session.commit()
    assert worker.run_due_inbox_reads(now=NOW)['selected'] == 0
    with pytest.raises(MarketplaceAccountValidationError): enqueue(inbox)
    assert MarketplaceReadSchedule.query.count() == 0
    OzonSellerAPIClient.request.assert_not_called()


def test_revoked_roles_excluded_before_candidate_limit_and_pair_budget_is_shared(inbox, monkeypatch):
    for _ in range(102):
        inbox.add(capabilities_json='["reviews_read","questions_read"]')
    worker._discover('reviews',NOW);worker._discover('questions',NOW)
    SellerMarketplaceAccount.query.filter(SellerMarketplaceAccount.id != inbox.ids[-1]).update({'capabilities_json':'[]'})
    db.session.commit()
    monkeypatch.setattr(OzonSellerAPIClient,'request',empty)
    result = worker.run_due_inbox_reads(limit=2,now=NOW)
    assert result['selected'] == result['completed'] == 2
    assert {r.account_id for r in MarketplaceInboxSync.query.all()} == {inbox.ids[-1]}
    assert {r.source_kind for r in MarketplaceInboxSync.query.all()} == {'review','question'}


def test_credential_rotation_and_capability_revocation_pause_without_io(inbox):
    enqueue(inbox)
    account = db.session.get(SellerMarketplaceAccount,inbox.ids[0])
    account.capabilities_json='[]';db.session.commit()
    assert status(inbox)['status'] == 'paused'
    assert worker.run_requested_reads(now=NOW)['selected'] == 0
    account.capabilities_json='["reviews_read"]';account.credential_version+=1;db.session.commit()
    assert status(inbox)['error_code'] == 'credentials_changed'
    assert worker.run_requested_reads(now=NOW)['selected'] == 0
    OzonSellerAPIClient.request.assert_not_called()


def test_foreign_kind_run_cannot_complete_request_or_leak_progress(inbox, monkeypatch):
    enqueue(inbox);enqueue(inbox,'questions')
    monkeypatch.setattr(OzonSellerAPIClient,'request',empty)
    # Finish only the review; a corrupt question run pointer must not expose its pages.
    worker.run_requested_reads(now=NOW,limit=1)
    review = MarketplaceInboxSync.query.one()
    question = MarketplaceReadRequest.query.filter_by(domain='questions').one()
    schedule = MarketplaceReadSchedule.query.filter_by(domain='questions').one()
    record_request(schedule,question,review,NOW)
    assert question.status != 'completed'
    question.run_id=review.id;db.session.commit()
    assert status(inbox,'questions')['pages_loaded'] == 0
    assert status(inbox,'questions')['snapshot_id'] is None
