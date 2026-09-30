"""Connection-to-catalog workflow: real SQLite state, synthetic provider only."""

from datetime import datetime, timedelta
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

from cryptography.fernet import Fernet
from flask import Flask
import pytest

from models import BackgroundJob, Marketplace, MarketplaceCatalogSync, MarketplaceListing, Seller, User, db
from services.marketplace_accounts import MarketplaceAccountService, MarketplaceAccountNotFound
from services.marketplace_adapters import ConnectionCheck
from services.marketplace_listings import MarketplaceListingService
from services.marketplace_operation_locks import try_account_operation_lock
from services.ozon_account_sync import (
    JOB_TYPE, MAX_CALLS_PER_TICK, _advance, _BudgetedClient, enqueue_account_sync,
    get_account_job, latest_account_jobs, run_account_sync_tick,
)
from services.ozon_api_client import OzonAPIError, OzonRateLimitError
from tests.test_marketplace_listing_service import SYNTHETIC_CREDENTIALS, SyntheticCatalogAdapter


class Catalog(SyntheticCatalogAdapter):
    def __init__(self, *, active_pages=1, empty=False, failure=None, check_ok=True):
        super().__init__()
        self.active_pages = active_pages
        self.empty = empty
        self.failure = failure
        self.check_ok = check_ok
        self.checks = 0
        self.calls = 0

    def _before_io(self):
        self.calls += 1
        assert not db.session.connection().connection.driver_connection.in_transaction

    def check_connection(self, credentials):
        self._before_io()
        self.checks += 1
        return ConnectionCheck(ok=self.check_ok, status='connected' if self.check_ok else 'invalid',
                               external_account_id=credentials.external_account_id,
                               capabilities=('connection_check', 'catalog_read'))

    def list_products(self, credentials, payload):
        self._before_io()
        self.list_payloads.append(payload)
        if self.failure:
            raise self.failure
        if payload['filter']['visibility'] == 'ARCHIVED' or self.empty:
            return {'result': {'items': [], 'total': 0, 'last_id': ''}}
        cursor = int(payload['last_id'] or '0')
        product_id = 101 + cursor
        return {'result': {'items': [{
            'product_id': product_id, 'offer_id': f'offer-{product_id}',
            'archived': False, 'has_fbo_stocks': True, 'has_fbs_stocks': False,
        }], 'total': self.active_pages, 'last_id': str(cursor + 1)}}

    def get_products(self, credentials, payload):
        self._before_io()
        data = super().get_products(credentials, payload)
        for item in data['items']:
            item['offer_id'] = f"offer-{item['id']}"
        return data

    def get_product_attributes(self, credentials, payload):
        self._before_io()
        data = super().get_product_attributes(credentials, payload)
        for item in data['result']:
            item['offer_id'] = f"offer-{item['id']}"
        return data

    def read_prices(self, credentials, payload):
        self._before_io()
        data = super().read_prices(credentials, payload)
        for item in data['items']:
            item['offer_id'] = f"offer-{item['product_id']}"
        return data

    def read_stocks(self, credentials, payload):
        self._before_io()
        data = super().read_stocks(credentials, payload)
        for item in data['items']:
            item['offer_id'] = f"offer-{item['product_id']}"
        return data


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setenv('ENCRYPTION_KEY', Fernet.generate_key().decode())
    app = Flask(__name__)
    app.config.update(TESTING=True, SQLALCHEMY_DATABASE_URI='sqlite://',
                      SQLALCHEMY_TRACK_MODIFICATIONS=False, MARKETPLACE_OZON_ENABLED=True)
    db.init_app(app)
    with app.app_context():
        db.create_all()
        user = User(username='onboarding', email='onboarding@example.test', password_hash='synthetic')
        seller = Seller(user=user, company_name='Synthetic')
        marketplace = Marketplace(code='ozon', name='Ozon', adapter_code='ozon', is_active=True)
        db.session.add_all([seller, marketplace])
        db.session.commit()
        account = MarketplaceAccountService.save_ozon_account(
            seller_id=seller.id, external_account_id=SYNTHETIC_CREDENTIALS.external_account_id,
            label='Test Ozon', api_key=SYNTHETIC_CREDENTIALS.api_key,
        )
        yield SimpleNamespace(app=app, seller_id=seller.id, account_id=account.id, account=account)
        db.session.remove()
        db.drop_all()


def enqueue(setup, **kwargs):
    result = enqueue_account_sync(seller_id=setup.seller_id, account_id=setup.account_id, **kwargs)
    return BackgroundJob.query.filter_by(job_uid=result['job_uid']).one()


def advance(setup, job_id, adapter, **kwargs):
    return _advance(job_id, setup.seller_id, adapter_factory=lambda: adapter, **kwargs)


def connected(setup):
    setup.account.connection_status = 'connected'
    db.session.commit()


def test_connect_is_local_deduplicated_and_can_omit_publication_vat(setup):
    with patch('services.ozon_account_sync._BoundedAdapter', side_effect=AssertionError('request I/O')):
        first = enqueue(setup, check_connection=True)
        second = enqueue(setup, check_connection=True)
    assert first.id == second.id
    assert BackgroundJob.query.count() == 1
    assert setup.account.public_settings.get('default_vat') is None
    assert 'credential_fingerprint' in first.get_progress()
    assert 'credential_fingerprint' not in json.dumps(first.to_dict())
    assert SYNTHETIC_CREDENTIALS.api_key not in json.dumps(first.to_dict())
    assert latest_account_jobs(seller_id=setup.seller_id, account_ids=[setup.account_id])[setup.account_id]['status'] == 'pending'


def test_rotated_key_can_complete_roles_check_while_old_write_remains_uncertain(setup):
    from tests.test_marketplace_accounts import MarketplaceAccountsTest
    from models import MarketplaceOperation
    operation = MarketplaceAccountsTest._operation(setup.account, status='uncertain', attempt_count=1)
    operation_id = operation.id
    old_job_id = enqueue(setup, check_connection=True).id
    MarketplaceAccountService.rotate_ozon_key(
        seller_id=setup.seller_id, account_id=setup.account_id,
        external_account_id=setup.account.external_account_id, api_key='rotated-synthetic-key', expected_version=setup.account.version)
    new_job_id = enqueue(setup, check_connection=True, force_restart=True).id
    assert new_job_id != old_job_id
    assert db.session.get(BackgroundJob, old_job_id).status == 'failed'
    adapter = Catalog()
    db.session.remove()
    assert advance(setup, new_job_id, adapter)
    assert adapter.checks == 1 and not adapter.list_payloads
    new_job = db.session.get(BackgroundJob, new_job_id)
    assert new_job.get_progress()['phase'] == 'catalog'
    operation = db.session.get(MarketplaceOperation, operation_id)
    assert (operation.status, operation.attempt_count) == ('uncertain', 1)


def test_one_click_continues_more_than_five_pages_and_survives_session_restart(setup):
    job_id = enqueue(setup, check_connection=True).id
    adapter = Catalog(active_pages=6)
    for _ in range(8):  # one check + six active pages + observed empty archive
        db.session.remove()
        assert advance(setup, job_id, adapter)
    job = db.session.get(BackgroundJob, job_id)
    assert job.status == 'completed'
    assert job.processed == job.total == 6
    assert adapter.checks == 1
    assert len(adapter.list_payloads) == 7
    assert MarketplaceListing.query.count() == 6
    assert MarketplaceCatalogSync.query.one().status == 'completed'
    assert get_account_job(seller_id=setup.seller_id, account_id=setup.account_id)['code'] == 'completed'


def test_empty_success_is_distinct_from_failure_and_not_yet_loaded(setup):
    job_id = enqueue(setup, check_connection=True).id
    adapter = Catalog(empty=True)
    for _ in range(3):
        advance(setup, job_id, adapter)
    result = get_account_job(seller_id=setup.seller_id, account_id=setup.account_id)
    assert result['status'] == 'completed'
    assert result['code'] == 'empty'
    assert result['processed'] == 0
    assert setup.account.connection_status == 'connected'


def test_invalid_key_never_starts_catalog(setup):
    job_id = enqueue(setup, check_connection=True).id
    adapter = Catalog(check_ok=False)
    advance(setup, job_id, adapter)
    assert setup.account.connection_status == 'invalid'
    assert db.session.get(BackgroundJob, job_id).status == 'failed'
    assert get_account_job(seller_id=setup.seller_id, account_id=setup.account_id)['code'] == 'ozon_auth_error'
    assert adapter.list_payloads == []


def test_foreign_scope_rejected_before_any_job_or_network(setup):
    with pytest.raises(MarketplaceAccountNotFound):
        enqueue_account_sync(seller_id=setup.seller_id + 1, account_id=setup.account_id, check_connection=True)
    with pytest.raises(MarketplaceAccountNotFound):
        get_account_job(seller_id=setup.seller_id + 1, account_id=setup.account_id)
    assert BackgroundJob.query.count() == 0
    assert latest_account_jobs(seller_id=setup.seller_id + 1, account_ids=[setup.account_id]) == {}


@pytest.mark.parametrize('mutation', ['rotate', 'disconnect'])
def test_credential_change_stops_old_work_before_adapter_creation(setup, mutation):
    job_id = enqueue(setup, check_connection=True).id
    if mutation == 'rotate':
        setup.account.set_credentials({'api_key': 'replacement-secret'})
    else:
        setup.account.clear_credentials()
        setup.account.is_active = False
    db.session.commit()
    factory = Mock(side_effect=AssertionError('unexpected provider'))
    _advance(job_id, setup.seller_id, adapter_factory=factory)
    factory.assert_not_called()
    assert db.session.get(BackgroundJob, job_id).status == 'failed'


def test_429_is_durable_and_never_shortens_retry_after(setup):
    connected(setup)
    job_id = enqueue(setup).id
    now = datetime.utcnow()
    limited = Catalog(failure=OzonRateLimitError('sensitive provider body', code='ozon_rate_limited',
                                               status_code=429, retry_after=5000, retriable=True))
    advance(setup, job_id, limited, now=now)
    result = get_account_job(seller_id=setup.seller_id, account_id=setup.account_id)
    assert result['status'] == 'pending' and result['code'] == 'ozon_rate_limited'
    assert datetime.fromisoformat(result['next_retry_at'].removesuffix('Z')) >= now + timedelta(seconds=5000)
    assert 'sensitive provider body' not in json.dumps(result)
    factory = Mock(side_effect=AssertionError('cooldown HTTP'))
    assert not _advance(job_id, setup.seller_id, now=now + timedelta(seconds=4999), adapter_factory=factory)
    factory.assert_not_called()
    assert advance(setup, job_id, Catalog(), now=now + timedelta(seconds=5001))
    assert db.session.get(BackgroundJob, job_id).processed == 1


def test_catalog_403_does_not_disable_authenticated_account(setup):
    connected(setup)
    job_id = enqueue(setup).id
    advance(setup, job_id, Catalog(failure=OzonAPIError('denied', status_code=403)))
    assert setup.account.connection_status == 'connected'
    assert get_account_job(seller_id=setup.seller_id, account_id=setup.account_id)['code'] == 'ozon_catalog_access_denied'


def test_live_account_claim_defers_without_catalog_calls(setup):
    connected(setup)
    job_id = enqueue(setup).id
    claim = try_account_operation_lock(setup.account_id)
    assert claim is not None
    adapter = Catalog()
    try:
        advance(setup, job_id, adapter)
    finally:
        claim.close()
    assert adapter.calls == 0
    assert get_account_job(seller_id=setup.seller_id, account_id=setup.account_id)['code'] == 'account_busy'


def test_crashed_running_checkpoint_resumes_only_when_catalog_claim_is_free(setup):
    connected(setup)
    job_id = enqueue(setup).id
    adapter = Catalog(active_pages=2)
    advance(setup, job_id, adapter)
    run = MarketplaceCatalogSync.query.one()
    run.status = 'running'  # process died after committing a page, before pause
    run.heartbeat_at = datetime.utcnow()
    db.session.commit()
    claim = MarketplaceListingService._try_claim(setup.account_id)
    assert claim is not None
    try:
        advance(setup, job_id, adapter)
    finally:
        MarketplaceListingService._release_claim(claim)
    assert len(adapter.list_payloads) == 1
    job = db.session.get(BackgroundJob, job_id)
    due = datetime.fromisoformat(job.get_result()['next_retry_at'].removesuffix('Z'))
    advance(setup, job_id, adapter, now=due + timedelta(seconds=1))
    assert len(adapter.list_payloads) == 2
    assert adapter.list_payloads[-1]['last_id'] == '1'
    assert MarketplaceCatalogSync.query.count() == 1


def test_completed_page_checkpoint_is_not_restarted_after_worker_crash(setup):
    connected(setup)
    job_id = enqueue(setup).id
    adapter = Catalog(empty=True)
    advance(setup, job_id, adapter)
    run = MarketplaceListingService.sync_ozon_account(
        seller_id=setup.seller_id, account_id=setup.account_id, adapter=adapter,
        credentials=SYNTHETIC_CREDENTIALS, max_pages=1,
    )
    assert run.status == 'completed'
    factory = Mock(side_effect=AssertionError('already observed completion'))
    _advance(job_id, setup.seller_id, adapter_factory=factory)
    factory.assert_not_called()
    assert db.session.get(BackgroundJob, job_id).status == 'completed'
    assert MarketplaceCatalogSync.query.count() == 1


def test_scheduler_flag_stops_work_and_future_retry_does_not_call_provider(setup):
    job = enqueue(setup, check_connection=True)
    setup.app.config['MARKETPLACE_OZON_ENABLED'] = False
    with patch('services.ozon_account_sync._advance') as step:
        run_account_sync_tick(setup.app)
    step.assert_not_called()
    setup.app.config['MARKETPLACE_OZON_ENABLED'] = True
    result = job.get_result()
    result['next_retry_at'] = (datetime.utcnow() + timedelta(hours=1)).isoformat() + 'Z'
    job.set_result(result)
    db.session.commit()
    with patch('services.ozon_account_sync._advance') as step:
        run_account_sync_tick(setup.app)
    step.assert_not_called()


def test_read_client_has_physical_budget_and_rejects_writes():
    client = _BudgetedClient(SYNTHETIC_CREDENTIALS)
    try:
        assert client.read_retries == 0
        assert client.timeout == (3.0, 6.0)
        with patch.object(client.session, 'request') as request:
            with pytest.raises(ValueError):
                client.request('product_import', {'items': []})
            client.calls = MAX_CALLS_PER_TICK
            with pytest.raises(OzonAPIError):
                client.get_roles()
        request.assert_not_called()
    finally:
        client.session.close()


def test_malformed_catalog_page_preserves_last_complete_snapshot(setup):
    connected(setup)
    first = enqueue(setup).id
    for _ in range(3):
        advance(setup, first, Catalog())
    assert db.session.get(BackgroundJob, first).status == 'completed'
    before = [(row.id, row.sync_fingerprint, row.is_available) for row in MarketplaceListing.query.all()]
    second = enqueue(setup, force_restart=True).id
    adapter = Catalog()
    adapter.get_products = lambda credentials, payload: {'items': {'not': 'a list'}}
    advance(setup, second, adapter)
    result = get_account_job(seller_id=setup.seller_id, account_id=setup.account_id)
    assert result['status'] == 'failed' and result['code'] == 'invalid_snapshot'
    assert before == [(row.id, row.sync_fingerprint, row.is_available) for row in MarketplaceListing.query.all()]


def test_expired_job_and_exhausted_retry_are_terminal_without_secret(setup):
    connected(setup)
    job = enqueue(setup)
    now = datetime.utcnow()
    job.created_at = now - timedelta(hours=25)
    db.session.commit()
    factory = Mock(side_effect=AssertionError('expired job HTTP'))
    assert _advance(job.id, setup.seller_id, adapter_factory=factory, now=now)
    assert job.get_result()['code'] == 'job_expired'
    job = enqueue(setup)
    state = job.get_progress()
    state['failures'] = 7
    job.set_progress(state)
    db.session.commit()
    advance(setup, job.id, Catalog(failure=OzonAPIError('secret-body', status_code=503, retriable=True)))
    assert job.status == 'failed' and job.get_result()['code'] == 'retry_exhausted'
    assert 'secret-body' not in json.dumps(job.to_dict())


def test_future_jobs_cannot_starve_ready_seller_before_candidate_limit(setup):
    for number in range(101):
        waiting = BackgroundJob(seller_id=setup.seller_id, job_uid=f'oc:{number + 200}:synthetic',
                                job_type=JOB_TYPE, status='pending')
        waiting.set_result({'next_retry_at': (datetime.utcnow() + timedelta(hours=1)).isoformat() + 'Z'})
        db.session.add(waiting)
    db.session.commit()
    due_id = enqueue(setup, check_connection=True).id
    with patch('services.ozon_account_sync._advance', return_value=True) as step:
        run_account_sync_tick(setup.app)
    step.assert_called_once_with(due_id, setup.seller_id)
