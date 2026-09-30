"""HTTP enqueue/status boundaries for durable Ozon warehouse reads."""

from contextlib import ExitStack
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch
import os
import unittest

from cryptography.fernet import Fernet
from flask import Flask
from flask_login import LoginManager
from flask_wtf.csrf import CSRFProtect, generate_csrf

from models import (Marketplace, MarketplaceListing, MarketplaceWarehouse,
                    MarketplaceWarehouseReadJob, Seller, SellerMarketplaceAccount, User, db)
from routes.marketplace_commercial import register_marketplace_commercial_routes


class WarehouseReadRoutesTest(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {'ENCRYPTION_KEY': Fernet.generate_key().decode()})
        self.env.start()
        self.app = Flask(__name__)
        self.app.config.update(TESTING=True, SECRET_KEY='synthetic-warehouse-route',
                               SQLALCHEMY_DATABASE_URI='sqlite://',
                               SQLALCHEMY_TRACK_MODIFICATIONS=False,
                               WTF_CSRF_ENABLED=True, MARKETPLACE_OZON_ENABLED=True)
        db.init_app(self.app)
        LoginManager(self.app)
        CSRFProtect(self.app)
        register_marketplace_commercial_routes(self.app)
        self.app.add_url_rule('/test-csrf', view_func=lambda: generate_csrf())
        self.client = self.app.test_client()
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        marketplace = Marketplace(name='Ozon', code='ozon', adapter_code='ozon', is_active=True)
        owners = []
        for suffix in ('one', 'two'):
            user = User(username='warehouse-route-' + suffix,
                        email='warehouse-route-' + suffix + '@test.local', is_active=True)
            user.set_password('synthetic-password')
            owners.append(Seller(user=user, company_name='Warehouse Route ' + suffix))
        db.session.add_all([marketplace, *owners])
        db.session.flush()
        accounts, listings = [], []
        for index, seller in enumerate(owners):
            account = SellerMarketplaceAccount(
                seller_id=seller.id, marketplace_id=marketplace.id,
                external_account_id='synthetic-client-' + str(index),
                label='Ozon ' + str(index), is_active=True, connection_status='connected')
            account.set_credentials({'api_key': 'synthetic-secret-' + str(index)})
            db.session.add(account)
            db.session.flush()
            listing = MarketplaceListing(
                seller_id=seller.id, marketplace_id=marketplace.id,
                account_id=account.id, offer_id='offer-' + str(index),
                external_product_id=str(100 + index), primary_sku=str(900 + index),
                normalized_status='active', is_available=True, sync_fingerprint='a' * 64)
            db.session.add(listing)
            accounts.append(account)
            listings.append(listing)
        db.session.commit()
        self.sellers = [seller.id for seller in owners]
        self.users = [seller.user_id for seller in owners]
        self.accounts = [account.id for account in accounts]
        self.listings = [listing.id for listing in listings]

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()
        self.env.stop()

    def as_seller(self, index):
        user = SimpleNamespace(id=self.users[index], seller=SimpleNamespace(id=self.sellers[index]),
                               is_authenticated=True, is_active=True, is_admin=False)
        stack = ExitStack()
        stack.enter_context(patch('routes.marketplace_commercial.current_user', user))
        stack.enter_context(patch('flask_login.utils._get_user', return_value=user))
        return stack

    def token(self):
        return self.client.get('/test-csrf').data.decode()

    def post(self, path, body=None, token=None):
        return self.client.post(path, json={} if body is None else body,
                                headers={'X-CSRFToken': token or self.token()})

    def test_csrf_dedup_location_get_cancel_and_zero_provider_calls(self):
        path = f'/marketplaces/commercial/accounts/{self.accounts[0]}/warehouses/sync'
        with self.as_seller(0), patch('requests.sessions.Session.request', side_effect=AssertionError('provider read in HTTP')):
            denied = self.client.post(path, json={})
            unknown = self.post(path, {'seller_id': self.sellers[1]})
            first = self.post(path)
            second = self.post(path)
            self.assertEqual(denied.status_code, 400)
            self.assertEqual(unknown.status_code, 400)
            self.assertEqual(first.status_code, 202, first.get_data(as_text=True))
            self.assertEqual(second.status_code, 202)
            self.assertEqual(first.json['refresh']['id'], second.json['refresh']['id'])
            self.assertEqual(first.headers['Location'],
                             f"/marketplaces/commercial/refreshes/{first.json['refresh']['id']}")
            status = self.client.get(first.headers['Location'])
            self.assertEqual(status.status_code, 200)
            self.assertEqual(status.json['refresh']['status'], 'queued')
            encoded = status.get_data(as_text=True)
            self.assertNotIn('synthetic-secret', encoded)
            self.assertNotIn('credential_fingerprint', encoded)
            cancelled = self.post(first.headers['Location'] + '/cancel')
            self.assertEqual(cancelled.status_code, 200)
            self.assertEqual(cancelled.json['refresh']['status'], 'cancelled')
        self.assertEqual(MarketplaceWarehouseReadJob.query.count(), 1)

    def test_foreign_scope_and_fbs_catalog_prerequisite(self):
        own_account = self.accounts[0]
        foreign_account = self.accounts[1]
        own_listing = self.listings[0]
        foreign_listing = self.listings[1]
        with self.as_seller(0), patch('requests.sessions.Session.request', side_effect=AssertionError('provider read in HTTP')):
            self.assertEqual(self.post(f'/marketplaces/commercial/accounts/{foreign_account}/warehouses/sync').status_code, 404)
            self.assertEqual(self.client.get(f'/marketplaces/commercial/accounts/{foreign_account}/warehouses/refresh').status_code, 404)
            self.assertEqual(self.post(f'/marketplaces/commercial/listings/{foreign_listing}/stocks/refresh').status_code, 404)
            missing = self.post(f'/marketplaces/commercial/listings/{own_listing}/stocks/refresh')
            self.assertEqual(missing.status_code, 409)
            self.assertEqual(missing.json['code'], 'warehouse_catalog_required')
            account = db.session.get(SellerMarketplaceAccount, own_account)
            db.session.add(MarketplaceWarehouse(
                seller_id=self.sellers[0], marketplace_id=account.marketplace_id,
                account_id=own_account, external_warehouse_id='7001', name='Склад',
                sync_fingerprint='b' * 64, last_seen_at=datetime.utcnow(),
                last_synced_at=datetime.utcnow()))
            db.session.commit()
            accepted = self.post(f'/marketplaces/commercial/listings/{own_listing}/stocks/refresh')
            self.assertEqual(accepted.status_code, 202, accepted.get_data(as_text=True))
            self.assertEqual(accepted.json['refresh']['listing_id'], own_listing)
            self.assertEqual(accepted.json['refresh']['account_id'], own_account)


if __name__ == '__main__':
    unittest.main()
