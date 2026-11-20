"""Persistence-boundary regressions for the background WB catalog sync.

All provider data and credentials in this module are synthetic.  The tests run
the real ``_perform_product_sync_task`` against an isolated SQLite database
and inject database failures at SQLAlchemy's cursor boundary.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import os
import sqlite3

os.environ.setdefault('SKIP_SCHEDULER', '1')
os.environ.setdefault('DISABLE_SECURE_COOKIE', '1')

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.pool import StaticPool

import seller_platform
from models import APILog, Notification, Product, ProductSyncSettings, Seller, User, db
from services import wb_stock_sync


SAFE_CATALOG_SAVE_ERROR = (
    'Не удалось сохранить полный каталог WB в базе данных. Последняя '
    'подтверждённая синхронизация и её счётчики сохранены, повторите '
    'попытку позже.'
)


@pytest.fixture
def catalog_app(request):
    app = seller_platform.app
    config = {
        key: app.config.get(key)
        for key in (
            'SQLALCHEMY_DATABASE_URI', 'SQLALCHEMY_TRACK_MODIFICATIONS',
            'SQLALCHEMY_ENGINE_OPTIONS', 'WTF_CSRF_ENABLED', 'TESTING',
            'MARKETPLACE_OZON_ENABLED', 'MARKETPLACE_WB_COMMON_READ_ENABLED',
        )
    }
    app.config.update(
        SQLALCHEMY_DATABASE_URI='sqlite:///:memory:',
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SQLALCHEMY_ENGINE_OPTIONS={},
        WTF_CSRF_ENABLED=False,
        TESTING=True,
        MARKETPLACE_OZON_ENABLED=False,
        MARKETPLACE_WB_COMMON_READ_ENABLED=False,
    )
    engine = sa.create_engine(
        'sqlite:///:memory:',
        connect_args={'check_same_thread': False},
        poolclass=StaticPool,
    )
    previous_engines = db._app_engines.get(app)
    db._app_engines[app] = {None: engine}

    previous_success = datetime(2026, 11, 19, 10, 15, 0)
    previous_next = datetime(2026, 11, 20, 10, 15, 0)
    with app.app_context():
        db.session.remove()
        if getattr(request, 'param', False):
            # Model a database created before the current uniqueness guard:
            # ORM mappings stay untouched, while only this private schema copy
            # omits the copied Product uniqueness constraint.
            legacy_metadata = sa.MetaData()
            for table in db.metadata.sorted_tables:
                table.to_metadata(legacy_metadata)
            legacy_product_table = legacy_metadata.tables[Product.__tablename__]
            legacy_unique = next(
                constraint
                for constraint in legacy_product_table.constraints
                if isinstance(constraint, sa.UniqueConstraint)
                and constraint.name == 'uq_seller_nm_id'
            )
            legacy_product_table.constraints.remove(legacy_unique)
            legacy_metadata.create_all(engine)
        else:
            db.create_all()
        users = [
            User(
                username='wb-catalog-owner-a',
                email='wb-catalog-a@example.test',
                password_hash='synthetic',
            ),
            User(
                username='wb-catalog-owner-b',
                email='wb-catalog-b@example.test',
                password_hash='synthetic',
            ),
        ]
        db.session.add_all(users)
        db.session.flush()
        sellers = [
            Seller(
                user_id=users[0].id,
                company_name='Synthetic WB seller A',
                wb_seller_id='synthetic-a',
                api_last_sync=previous_success,
                api_sync_status='success',
            ),
            Seller(
                user_id=users[1].id,
                company_name='Synthetic WB seller B',
                wb_seller_id='synthetic-b',
                api_last_sync=previous_success - timedelta(days=1),
                api_sync_status='success',
            ),
        ]
        for seller in sellers:
            seller.wb_api_key = 'synthetic-not-a-real-provider-key'
        db.session.add_all(sellers)
        db.session.flush()

        settings = [
            ProductSyncSettings(
                seller_id=sellers[0].id,
                is_enabled=True,
                sync_interval_minutes=90,
                sync_products=True,
                sync_stocks=True,
                last_sync_at=previous_success,
                next_sync_at=previous_next,
                last_sync_status='success',
                last_sync_error=None,
                last_sync_duration=12.5,
                products_synced=41,
                products_added=7,
                products_updated=34,
            ),
            ProductSyncSettings(
                seller_id=sellers[1].id,
                is_enabled=True,
                sync_interval_minutes=75,
                sync_products=True,
                sync_stocks=True,
                last_sync_at=previous_success - timedelta(days=1),
                next_sync_at=previous_next - timedelta(days=1),
                last_sync_status='success',
                last_sync_duration=5.0,
                products_synced=9,
                products_added=2,
                products_updated=7,
            ),
        ]
        db.session.add_all(settings)
        db.session.flush()

        # One current owned card, one absent-but-still-active card, and one
        # legacy placeholder exercise the three write/reconciliation scopes.
        db.session.add_all([
            Product(
                seller_id=sellers[0].id,
                nm_id=1000,
                vendor_code='OLD-1000',
                title='Previous title',
                is_active=True,
                price=10,
            ),
            Product(
                seller_id=sellers[0].id,
                nm_id=900000,
                vendor_code='MISSING-900000',
                title='Missing card',
                is_active=True,
            ),
            Product(
                seller_id=sellers[0].id,
                nm_id=0,
                vendor_code='LEGACY-PLACEHOLDER',
                title='Unconfirmed legacy row',
                is_active=True,
            ),
            # Identical nmID under another seller must never be changed by A's
            # successful reconciliation.
            Product(
                seller_id=sellers[1].id,
                nm_id=1000,
                vendor_code='OTHER-1000',
                title='Seller B title',
                is_active=True,
            ),
        ])
        db.session.commit()
        ids = {
            'user_a': users[0].id,
            'seller_a': sellers[0].id,
            'seller_b': sellers[1].id,
        }
        db.session.remove()

    yield app, engine, ids

    with app.app_context():
        db.session.remove()
        db.drop_all()
        db.session.remove()
    engine.dispose()
    if previous_engines is None:
        db._app_engines.pop(app, None)
    else:
        db._app_engines[app] = previous_engines
    for key, value in config.items():
        if value is None:
            app.config.pop(key, None)
        else:
            app.config[key] = value


def _card(nm_id: int) -> dict:
    return {
        'nmID': nm_id,
        'vendorCode': f'SYNTH-{nm_id}',
        'title': f'Synthetic catalog card {nm_id}',
        'brand': 'Synthetic brand',
        'subjectName': 'Synthetic subject',
        'photos': ['https://example.test/synthetic.webp'],
        'sizes': [],
        'characteristics': [],
        'dimensions': {},
        'mediaFiles': [],
    }


class _FakeCatalogClient:
    def __init__(self, cards):
        self.cards = list(cards)
        self.card_read_calls = []
        self.price_read_calls = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def get_all_cards(self, **kwargs):
        self.card_read_calls.append(kwargs)
        return list(self.cards)

    def get_all_goods_prices(self, **kwargs):
        self.price_read_calls += 1
        return []


def _run_with_fake_provider(monkeypatch, seller_id, cards):
    client = _FakeCatalogClient(cards)
    monkeypatch.setattr(
        seller_platform,
        'WildberriesAPIClient',
        lambda _api_key: client,
    )
    seller_platform._perform_product_sync_task(seller_id, seller_platform.app)
    # The worker enters its own Flask app context and therefore uses a
    # distinct scoped SQLAlchemy session from the test observer context.
    # Expire cached entities so following assertions read committed state.
    db.session.expire_all()
    return client


def _seed_success_snapshot(seller_id):
    seller = db.session.get(Seller, seller_id)
    settings = ProductSyncSettings.query.filter_by(seller_id=seller_id).one()
    return {
        'seller_last_sync': seller.api_last_sync,
        'settings_last_sync': settings.last_sync_at,
        'settings_next_sync': settings.next_sync_at,
        'settings_duration': settings.last_sync_duration,
        'products_synced': settings.products_synced,
        'products_added': settings.products_added,
        'products_updated': settings.products_updated,
    }


def _assert_success_snapshot_unchanged(seller_id, before):
    seller = db.session.get(Seller, seller_id)
    settings = ProductSyncSettings.query.filter_by(seller_id=seller_id).one()
    assert seller.api_last_sync == before['seller_last_sync']
    assert settings.last_sync_at == before['settings_last_sync']
    assert settings.next_sync_at == before['settings_next_sync']
    assert settings.last_sync_duration == before['settings_duration']
    assert settings.products_synced == before['products_synced']
    assert settings.products_added == before['products_added']
    assert settings.products_updated == before['products_updated']


def _assert_safe_error_status(app, user_id):
    client = app.test_client()
    with client.session_transaction() as session:
        session['_user_id'] = str(user_id)
        session['_fresh'] = True
    response = client.get('/api/products/sync-status')
    assert response.status_code == 200
    status = response.get_json()
    assert status['last_sync_status'] == 'error'
    assert status['status_message'] == 'Ошибка при последней синхронизации'
    assert status['last_sync_error'] == SAFE_CATALOG_SAVE_ERROR
    assert 'database is locked' not in status['last_sync_error'].lower()


def _install_sqlite_lock(engine, predicate):
    state = {'fired': False}

    def fail_once(_conn, _cursor, statement, parameters, _context, _executemany):
        if not state['fired'] and predicate(str(statement).upper()):
            state['fired'] = True
            raise sa.exc.OperationalError(
                statement,
                parameters,
                sqlite3.OperationalError('database is locked'),
            )

    event.listen(engine, 'before_cursor_execute', fail_once)
    return state, fail_once


def _stop_sqlite_fault(engine, listener):
    event.remove(engine, 'before_cursor_execute', listener)


def _install_no_side_effect_observers(monkeypatch):
    stock_calls = []
    notification_calls = []
    monkeypatch.setattr(
        wb_stock_sync,
        'enqueue_stock_sync',
        lambda seller_id: stock_calls.append(seller_id),
    )
    monkeypatch.setattr(
        seller_platform,
        'create_notification',
        lambda **kwargs: notification_calls.append(kwargs),
    )
    return stock_calls, notification_calls


def _assert_no_false_success_side_effects(seller_id, stock_calls, notification_calls):
    assert stock_calls == []
    assert notification_calls == []
    assert APILog.query.filter_by(
        seller_id=seller_id,
        endpoint='/content/v2/get/cards/list',
        success=True,
    ).count() == 0
    assert Notification.query.filter_by(seller_id=seller_id).count() == 0


def test_later_catalog_batch_lock_persists_error_and_explicit_retry_finishes_exact_catalog(
    catalog_app, monkeypatch,
):
    app, engine, ids = catalog_app
    cards = [_card(nm_id) for nm_id in range(1000, 1201)]
    stock_calls, notification_calls = _install_no_side_effect_observers(monkeypatch)

    with app.app_context():
        before = _seed_success_snapshot(ids['seller_a'])
        insert_count = {'value': 0}

        def fail_in_second_bounded_batch(statement):
            if statement.startswith('INSERT INTO PRODUCTS'):
                insert_count['value'] += 1
                # The first 100-card transaction has committed. Fail after
                # several cards have entered transaction two.
                return insert_count['value'] == 150
            return False

        failure, listener = _install_sqlite_lock(engine, fail_in_second_bounded_batch)
        try:
            failed_client = _run_with_fake_provider(
                monkeypatch, ids['seller_a'], cards,
            )
        finally:
            _stop_sqlite_fault(engine, listener)

        assert failure['fired'] is True
        assert failed_client.card_read_calls == [{
            'batch_size': 100,
            'retry_rate_limited_pages': True,
        }]
        assert failed_client.price_read_calls == 1
        seller = db.session.get(Seller, ids['seller_a'])
        settings = ProductSyncSettings.query.filter_by(seller_id=ids['seller_a']).one()
        assert seller.api_sync_status == 'error'
        assert settings.last_sync_status == 'error'
        assert settings.last_sync_error == SAFE_CATALOG_SAVE_ERROR
        _assert_success_snapshot_unchanged(ids['seller_a'], before)

        # A prior bounded batch may remain; the failed transaction and later
        # reconciliation must not be represented as a successful sweep.
        assert Product.query.filter_by(
            seller_id=ids['seller_a'], nm_id=1000,
        ).one().title == 'Synthetic catalog card 1000'
        assert Product.query.filter(
            Product.seller_id == ids['seller_a'],
            Product.nm_id.between(1001, 1200),
        ).count() == 99
        assert Product.query.filter_by(
            seller_id=ids['seller_a'], nm_id=900000,
        ).one().is_active is True
        assert Product.query.filter_by(
            seller_id=ids['seller_a'], nm_id=0,
        ).one().is_active is True
        _assert_no_false_success_side_effects(
            ids['seller_a'], stock_calls, notification_calls,
        )
        _assert_safe_error_status(app, ids['user_a'])

        # The retry is a new, explicit worker run against the same complete
        # fake catalog. It accepts the already persisted first batch as input
        # state and completes the exact same unique catalog.
        retry_client = _run_with_fake_provider(
            monkeypatch, ids['seller_a'], cards,
        )
        assert retry_client.card_read_calls == [{
            'batch_size': 100,
            'retry_rate_limited_pages': True,
        }]
        assert retry_client.price_read_calls == 1
        seller = db.session.get(Seller, ids['seller_a'])
        settings = ProductSyncSettings.query.filter_by(seller_id=ids['seller_a']).one()
        assert seller.api_sync_status == 'success'
        assert seller.api_last_sync != before['seller_last_sync']
        assert settings.last_sync_status == 'success'
        assert settings.last_sync_error is None
        assert settings.last_sync_at != before['settings_last_sync']
        assert settings.products_synced == 201
        assert settings.products_added == 101
        assert settings.products_updated == 100
        assert Product.query.filter(
            Product.seller_id == ids['seller_a'],
            Product.nm_id.between(1000, 1200),
        ).count() == 201
        assert Product.query.filter_by(
            seller_id=ids['seller_a'], nm_id=900000,
        ).one().is_active is False
        assert Product.query.filter_by(
            seller_id=ids['seller_a'], nm_id=0,
        ).one().is_active is False
        assert APILog.query.filter_by(
            seller_id=ids['seller_a'],
            endpoint='/content/v2/get/cards/list',
            success=True,
        ).count() == 1
        assert len(stock_calls) == 1
        assert len(notification_calls) >= 2

        other_seller = db.session.get(Seller, ids['seller_b'])
        other_settings = ProductSyncSettings.query.filter_by(
            seller_id=ids['seller_b'],
        ).one()
        other_card = Product.query.filter_by(
            seller_id=ids['seller_b'], nm_id=1000,
        ).one()
        assert other_seller.api_sync_status == 'success'
        assert other_seller.api_last_sync == before['seller_last_sync'] - timedelta(days=1)
        assert other_settings.last_sync_at == before['settings_last_sync'] - timedelta(days=1)
        assert other_settings.products_synced == 9
        assert other_card.title == 'Seller B title'


@pytest.mark.parametrize('catalog_app', [True], indirect=True)
def test_dedup_persistence_lock_fails_closed_then_retry_deduplicates_and_syncs(
    catalog_app, monkeypatch,
):
    app, engine, ids = catalog_app
    stock_calls, notification_calls = _install_no_side_effect_observers(monkeypatch)
    with app.app_context():
        duplicate_rows = [
            Product(
                seller_id=ids['seller_a'], nm_id=5000,
                vendor_code=f'DUP-{index}', title=f'Duplicate row {index}',
                is_active=True,
            )
            for index in range(2)
        ]
        db.session.add_all(duplicate_rows)
        db.session.commit()
        before = _seed_success_snapshot(ids['seller_a'])
        failure, listener = _install_sqlite_lock(
            engine,
            lambda statement: statement.startswith('DELETE FROM PRODUCTS'),
        )
        try:
            failed_client = _run_with_fake_provider(
                monkeypatch, ids['seller_a'], [_card(5000)],
            )
        finally:
            _stop_sqlite_fault(engine, listener)

        assert failure['fired'] is True
        assert len(failed_client.card_read_calls) == 1
        seller = db.session.get(Seller, ids['seller_a'])
        settings = ProductSyncSettings.query.filter_by(seller_id=ids['seller_a']).one()
        assert seller.api_sync_status == 'error'
        assert settings.last_sync_status == 'error'
        assert settings.last_sync_error == SAFE_CATALOG_SAVE_ERROR
        _assert_success_snapshot_unchanged(ids['seller_a'], before)
        assert Product.query.filter_by(
            seller_id=ids['seller_a'], nm_id=5000,
        ).count() == 2
        assert Product.query.filter_by(
            seller_id=ids['seller_a'], nm_id=900000,
        ).one().is_active is True
        assert Product.query.filter_by(
            seller_id=ids['seller_a'], nm_id=0,
        ).one().is_active is True
        _assert_no_false_success_side_effects(
            ids['seller_a'], stock_calls, notification_calls,
        )
        _assert_safe_error_status(app, ids['user_a'])

        retry_client = _run_with_fake_provider(
            monkeypatch, ids['seller_a'], [_card(5000)],
        )
        assert len(retry_client.card_read_calls) == 1
        assert Product.query.filter_by(
            seller_id=ids['seller_a'], nm_id=5000,
        ).count() == 1
        assert Product.query.filter_by(
            seller_id=ids['seller_a'], nm_id=900000,
        ).one().is_active is False
        assert Product.query.filter_by(
            seller_id=ids['seller_a'], nm_id=0,
        ).one().is_active is False
        assert seller.api_sync_status == 'success'
        assert settings.last_sync_status == 'success'
        assert settings.products_synced == 1
        assert settings.products_added == 0
        assert settings.products_updated == 1
        assert APILog.query.filter_by(
            seller_id=ids['seller_a'],
            endpoint='/content/v2/get/cards/list',
            success=True,
        ).count() == 1
        assert len(stock_calls) == 1


def test_final_reconciliation_and_success_commit_failure_roll_back_as_a_unit(
    catalog_app, monkeypatch,
):
    app, engine, ids = catalog_app
    stock_calls, notification_calls = _install_no_side_effect_observers(monkeypatch)
    with app.app_context():
        before = _seed_success_snapshot(ids['seller_a'])
        failure, listener = _install_sqlite_lock(
            engine,
            lambda statement: statement.startswith('INSERT INTO API_LOGS'),
        )
        try:
            failed_client = _run_with_fake_provider(
                monkeypatch, ids['seller_a'], [_card(1000)],
            )
        finally:
            _stop_sqlite_fault(engine, listener)

        assert failure['fired'] is True
        assert failed_client.card_read_calls == [{
            'batch_size': 100,
            'retry_rate_limited_pages': True,
        }]
        seller = db.session.get(Seller, ids['seller_a'])
        settings = ProductSyncSettings.query.filter_by(seller_id=ids['seller_a']).one()
        assert seller.api_sync_status == 'error'
        assert settings.last_sync_status == 'error'
        assert settings.last_sync_error == SAFE_CATALOG_SAVE_ERROR
        _assert_success_snapshot_unchanged(ids['seller_a'], before)
        assert Product.query.filter_by(
            seller_id=ids['seller_a'], nm_id=900000,
        ).one().is_active is True
        assert Product.query.filter_by(
            seller_id=ids['seller_a'], nm_id=0,
        ).one().is_active is True
        _assert_no_false_success_side_effects(
            ids['seller_a'], stock_calls, notification_calls,
        )
        _assert_safe_error_status(app, ids['user_a'])

        # The transaction failure is not a provider failure. A clean retry
        # applies both missing/unconfirmed reconciliation and success state.
        retry_client = _run_with_fake_provider(
            monkeypatch, ids['seller_a'], [_card(1000)],
        )
        assert len(retry_client.card_read_calls) == 1
        assert db.session.get(Seller, ids['seller_a']).api_sync_status == 'success'
        assert ProductSyncSettings.query.filter_by(
            seller_id=ids['seller_a'],
        ).one().last_sync_status == 'success'
        assert Product.query.filter_by(
            seller_id=ids['seller_a'], nm_id=900000,
        ).one().is_active is False
        assert Product.query.filter_by(
            seller_id=ids['seller_a'], nm_id=0,
        ).one().is_active is False
        assert APILog.query.filter_by(
            seller_id=ids['seller_a'],
            endpoint='/content/v2/get/cards/list',
            success=True,
        ).count() == 1
        assert len(stock_calls) == 1
        assert len(notification_calls) >= 1


def test_stock_enqueue_failure_is_ancillary_to_a_successful_catalog_commit(
    catalog_app, monkeypatch,
):
    app, _engine, ids = catalog_app
    notification_calls = []
    stock_calls = []
    def fail_stock_enqueue(seller_id):
        stock_calls.append(seller_id)
        raise RuntimeError('synthetic stock queue failure')

    monkeypatch.setattr(wb_stock_sync, 'enqueue_stock_sync', fail_stock_enqueue)
    monkeypatch.setattr(
        seller_platform,
        'create_notification',
        lambda **kwargs: notification_calls.append(kwargs),
    )

    with app.app_context():
        before = _seed_success_snapshot(ids['seller_a'])
        client = _run_with_fake_provider(
            monkeypatch, ids['seller_a'], [_card(1000), _card(1001)],
        )
        seller = db.session.get(Seller, ids['seller_a'])
        settings = ProductSyncSettings.query.filter_by(seller_id=ids['seller_a']).one()
        assert client.card_read_calls == [{
            'batch_size': 100,
            'retry_rate_limited_pages': True,
        }]
        assert seller.api_sync_status == 'success'
        assert seller.api_last_sync != before['seller_last_sync']
        assert settings.last_sync_status == 'success'
        assert settings.last_sync_error is None
        assert settings.products_synced == 2
        assert settings.products_added == 1
        assert settings.products_updated == 1
        assert Product.query.filter_by(
            seller_id=ids['seller_a'], nm_id=1001,
        ).count() == 1
        assert len(stock_calls) == 1
        assert len(notification_calls) >= 1
        assert APILog.query.filter_by(
            seller_id=ids['seller_a'],
            endpoint='/content/v2/get/cards/list',
            success=True,
        ).count() == 1
