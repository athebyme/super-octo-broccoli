"""Real ORM regressions for the bounded, read-only WB stock pipeline."""

import base64
from datetime import datetime, timedelta
import json
from unittest.mock import Mock

from flask import Flask
import pytest

from models import BackgroundJob, Product, ProductStock, Seller, User, db
from services import wb_stock_sync as sync
from services.wb_api_client import WBAuthException, WBRateLimitException


def row(nm=101, size=1, warehouse=507, quantity=3):
    return dict(nmId=nm, chrtId=size, warehouseId=warehouse, warehouseName='Коледино',
                quantity=quantity, inWayToClient=2, inWayFromClient=1)


@pytest.fixture
def stock_db(monkeypatch):
    app = Flask(__name__)
    app.config.update(SQLALCHEMY_DATABASE_URI='sqlite://', TESTING=True)
    db.init_app(app)
    with app.app_context():
        db.create_all()
        sellers = [Seller(company_name=f'Synthetic {i}', user=User(
            username=f'stock-{i}', email=f'stock-{i}@example.test', password_hash='synthetic',
        )) for i in (1, 2)]
        for seller in sellers:
            seller.wb_api_key = 'synthetic-opaque-key'
        db.session.add_all(sellers)
        db.session.flush()
        products = [Product(seller_id=sellers[0].id, nm_id=101, quantity=99),
                    Product(seller_id=sellers[0].id, nm_id=102, quantity=88),
                    Product(seller_id=sellers[1].id, nm_id=101, quantity=77)]
        db.session.add_all(products)
        db.session.flush()
        db.session.add_all([ProductStock(product_id=p.id, warehouse_id=9999, warehouse_name='Legacy name ID',
                                       quantity=p.quantity, quantity_full=p.quantity) for p in products])
        db.session.commit()
        ids = [p.id for p in products]
        seller_ids = [seller.id for seller in sellers]
        client = Mock()
        def read(*args, **kwargs):
            assert not db.session().in_transaction(), 'DB transaction held through provider read'
            return [row()]
        client.get_stocks_page.side_effect = read
        factory = Mock()
        factory.return_value.__enter__ = Mock(return_value=client)
        factory.return_value.__exit__ = Mock(return_value=False)
        monkeypatch.setattr(sync, 'WildberriesAPIClient', factory)
        yield app, seller_ids, ids, client, factory
        db.session.remove()
        db.drop_all()


def enqueue(stock_db):
    _, seller_ids, _, _, _ = stock_db
    state = sync.enqueue_stock_sync(seller_ids[0])
    job = BackgroundJob.query.filter_by(job_uid=state['job_uid']).one()
    return job.id, seller_ids[0]


def test_enqueue_is_idempotent_and_has_no_network(stock_db):
    _, sellers, _, _, factory = stock_db
    one = sync.enqueue_stock_sync(sellers[0])
    assert sync.enqueue_stock_sync(sellers[0]) == one
    assert one['total'] == 2
    assert BackgroundJob.query.count() == 1
    factory.assert_not_called()


def test_complete_read_replaces_legacy_ids_aggregates_sizes_and_clears_absent(stock_db):
    _, _, products, client, _ = stock_db
    job_id, seller = enqueue(stock_db)
    def read(*args, **kwargs):
        assert not db.session().in_transaction()
        return [row(size=1), row(size=2, quantity=4), row(size=1, warehouse=508, quantity=5)]
    client.get_stocks_page.side_effect = read
    sync._advance(job_id, seller)
    assert db.session.get(BackgroundJob, job_id).status == 'completed'
    stocks = ProductStock.query.filter_by(product_id=products[0]).order_by(ProductStock.warehouse_id).all()
    assert [(s.warehouse_id, s.quantity, s.quantity_full) for s in stocks] == [(507, 7, None), (508, 5, None)]
    assert stocks[0].in_way_to_client == 4
    assert db.session.get(Product, products[0]).quantity == 12
    assert db.session.get(Product, products[1]).quantity == 0
    assert ProductStock.query.filter_by(product_id=products[1]).count() == 0
    assert db.session.get(Product, products[2]).quantity == 77


def test_empty_completed_batch_is_observed_zero_not_fetch_miss(stock_db):
    _, _, products, client, _ = stock_db
    job_id, seller = enqueue(stock_db)
    client.get_stocks_page.side_effect = lambda *a, **kw: []
    sync._advance(job_id, seller)
    assert db.session.get(BackgroundJob, job_id).status == 'completed'
    assert db.session.get(Product, products[0]).quantity == 0
    assert db.session.get(Product, products[2]).quantity == 77


@pytest.mark.parametrize('payload', [[row(nm=999)], [row(), row()], [dict(row(), quantity=None)], [dict(row(), warehouseId=0)]])
def test_unobserved_or_malformed_read_preserves_existing_facts(stock_db, payload):
    _, _, products, client, _ = stock_db
    job_id, seller = enqueue(stock_db)
    client.get_stocks_page.side_effect = lambda *a, **kw: payload
    sync._advance(job_id, seller)
    assert db.session.get(BackgroundJob, job_id).status == 'failed'
    assert db.session.get(Product, products[0]).quantity == 99
    assert ProductStock.query.filter_by(product_id=products[0]).one().warehouse_id == 9999


def test_partial_page_is_durable_and_not_visible_until_pagination_end(stock_db, monkeypatch):
    _, _, products, client, _ = stock_db
    monkeypatch.setattr(sync, 'PAGE_SIZE', 2)
    job_id, seller = enqueue(stock_db)
    client.get_stocks_page.side_effect = [[row(size=1), row(size=2)], []]
    sync._advance(job_id, seller)
    assert db.session.get(BackgroundJob, job_id).status == 'running'
    assert db.session.get(BackgroundJob, job_id).to_dict()['progress'] == {}
    assert db.session.get(Product, products[0]).quantity == 99
    db.session.remove()
    sync._advance(job_id, seller)
    assert client.get_stocks_page.call_args.kwargs['offset'] == 2
    assert db.session.get(Product, products[0]).quantity == 6
    assert db.session.get(BackgroundJob, job_id).get_progress().get('rows') is None


def test_repeated_page_fails_without_exposing_partial_stock(stock_db, monkeypatch):
    monkeypatch.setattr(sync, 'PAGE_SIZE', 1)
    _, _, products, _, _ = stock_db
    job_id, seller = enqueue(stock_db)
    sync._advance(job_id, seller)
    sync._advance(job_id, seller)
    assert db.session.get(BackgroundJob, job_id).status == 'failed'
    assert db.session.get(Product, products[0]).quantity == 99


def test_429_defers_without_sleep_or_losing_buffer(stock_db, monkeypatch):
    _, _, products, client, _ = stock_db
    job_id, seller = enqueue(stock_db)
    client.get_stocks_page.side_effect = WBRateLimitException('synthetic-private-provider-text', retry_after=120)
    sync._advance(job_id, seller)
    job = db.session.get(BackgroundJob, job_id)
    assert job.status == 'running'
    assert datetime.fromisoformat(job.get_progress()['retry_at']) > datetime.utcnow()
    assert 'synthetic-private' not in job.error_message
    assert sync._advance(job_id, seller) is False
    client.get_stocks_page.assert_called_once()
    assert db.session.get(Product, products[0]).quantity == 99


def test_access_denial_is_actionable_without_leaking_provider_text(stock_db):
    _, _, products, client, _ = stock_db
    job_id, seller = enqueue(stock_db)
    client.get_stocks_page.side_effect = WBAuthException('synthetic-private-provider-text')
    sync._advance(job_id, seller)
    job = db.session.get(BackgroundJob, job_id)
    assert job.status == 'failed'
    assert job.error_message == sync.ACCESS_MESSAGE
    assert db.session.get(Product, products[0]).quantity == 99


def test_basic_key_is_rejected_before_enqueuing_or_network(stock_db):
    _, sellers, _, _, factory = stock_db
    body = base64.urlsafe_b64encode(json.dumps({'acc': 1, 'exp': 4000000000}).encode()).decode().rstrip('=')
    db.session.get(Seller, sellers[0]).wb_api_key = f'synthetic.{body}.synthetic'
    db.session.commit()
    with pytest.raises(sync.WBStockAccessError, match='персональный'):
        sync.enqueue_stock_sync(sellers[0])
    factory.assert_not_called()
    assert BackgroundJob.query.count() == 0


def test_credential_change_during_read_never_applies_old_account_facts(stock_db):
    _, _, products, client, _ = stock_db
    job_id, seller = enqueue(stock_db)
    def read(*args, **kwargs):
        db.session.get(Seller, seller).wb_api_key = 'synthetic-replaced-key'
        db.session.commit()
        return [row()]
    client.get_stocks_page.side_effect = read
    sync._advance(job_id, seller)
    assert db.session.get(BackgroundJob, job_id).status == 'failed'
    assert db.session.get(Product, products[0]).quantity == 99


def test_expired_partial_batch_restarts_instead_of_merging_stale_observations(stock_db, monkeypatch):
    _, _, _, client, _ = stock_db
    monkeypatch.setattr(sync, 'PAGE_SIZE', 1)
    job_id, seller = enqueue(stock_db)
    sync._advance(job_id, seller)
    job = db.session.get(BackgroundJob, job_id)
    progress = job.get_progress()
    progress['batch_started'] = (datetime.utcnow() - timedelta(minutes=16)).isoformat()
    job.set_progress(progress)
    db.session.commit()
    sync._advance(job_id, seller)
    assert client.get_stocks_page.call_args.kwargs['offset'] == 0
