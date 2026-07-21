# -*- coding: utf-8 -*-
"""Роуты конкурентов v2: строгая валидация, tenant scope, bounded WB."""
import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch

from flask import Flask

from models import (
    CompetitorGroup, CompetitorMonitorSettings, CompetitorProduct,
    Product, Seller, User, db,
)
from routes.competitors import register_competitor_routes
from services.competitor_fetch import WBRateLimitedError


class RoutesTestBase(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(
            TESTING=True, SECRET_KEY='t',
            SQLALCHEMY_DATABASE_URI='sqlite://',
            SQLALCHEMY_TRACK_MODIFICATIONS=False)
        db.init_app(self.app)
        register_competitor_routes(self.app)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.seller = self._mk_seller('shop1', 'r1@test.local')
        self.other = self._mk_seller('shop2', 'r2@test.local')
        self.group = CompetitorGroup(seller_id=self.seller.id, name='G')
        db.session.add(self.group)
        db.session.commit()
        self.client = self.app.test_client()
        self.user = MagicMock()
        self.user.is_authenticated = True
        self.user.seller = self.seller

    def _mk_seller(self, name, email):
        user = User(username=name, email=email, is_active=True)
        user.set_password('synthetic-password')
        seller = Seller(user=user, company_name=name)
        db.session.add(seller)
        db.session.commit()
        return seller

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _as_user(self):
        return patch('flask_login.utils._get_user', return_value=self.user)


class AddProductsTest(RoutesTestBase):
    def test_add_by_nm_ids_no_wb_calls(self):
        with self._as_user(), \
             patch('routes.competitors.CompetitorFetchService') as svc:
            resp = self.client.post('/api/competitors/products', json={
                'group_id': self.group.id, 'nm_ids': [111, 222]})
            self.assertEqual(resp.status_code, 200)
            svc.assert_not_called()
        self.assertEqual(resp.get_json()['added'], 2)
        self.assertTrue(resp.get_json()['scheduled'])
        rows = CompetitorProduct.query.filter_by(group_id=self.group.id).all()
        self.assertEqual({r.nm_id for r in rows}, {111, 222})
        settings = CompetitorMonitorSettings.query.filter_by(
            seller_id=self.seller.id).first()
        self.assertIsNotNone(settings.next_sync_due_at)

    def test_rejects_bool_float_string_and_dups(self):
        with self._as_user():
            for bad in ([True, 5], [1.5], ['123'], [111, 111], [0], [-3]):
                resp = self.client.post('/api/competitors/products', json={
                    'group_id': self.group.id, 'nm_ids': bad})
                self.assertEqual(resp.status_code, 400, f'nm_ids={bad}')
        self.assertEqual(CompetitorProduct.query.count(), 0)

    def test_cap_300(self):
        with self._as_user():
            resp = self.client.post('/api/competitors/products', json={
                'group_id': self.group.id,
                'nm_ids': list(range(1, 302))})
        self.assertEqual(resp.status_code, 400)

    def test_foreign_group_404(self):
        foreign = CompetitorGroup(seller_id=self.other.id, name='F')
        db.session.add(foreign)
        db.session.commit()
        with self._as_user():
            resp = self.client.post('/api/competitors/products', json={
                'group_id': foreign.id, 'nm_ids': [111]})
        self.assertEqual(resp.status_code, 404)

    def test_supplier_import_request_sets_flag(self):
        with self._as_user():
            resp = self.client.post('/api/competitors/products', json={
                'group_id': self.group.id, 'wb_supplier_id': 332183})
        self.assertEqual(resp.status_code, 200)
        db.session.refresh(self.group)
        self.assertTrue(self.group.import_requested)
        self.assertEqual(self.group.auto_source, 'seller')
        self.assertEqual(self.group.auto_source_value, '332183')

    def test_reactivates_inactive_duplicate(self):
        row = CompetitorProduct(
            seller_id=self.seller.id, group_id=self.group.id, nm_id=111,
            is_active=False, fetch_error_count=20, price_miss_count=7)
        db.session.add(row)
        db.session.commit()
        with self._as_user():
            resp = self.client.post('/api/competitors/products', json={
                'group_id': self.group.id, 'nm_ids': [111]})
        self.assertEqual(resp.get_json()['reactivated'], 1)
        db.session.refresh(row)
        self.assertTrue(row.is_active)
        self.assertEqual(row.fetch_error_count, 0)
        self.assertEqual(row.price_miss_count, 0)


class SearchBoundedTest(RoutesTestBase):
    def test_429_returns_503(self):
        with self._as_user(), \
             patch('routes.competitors.CompetitorFetchService') as SvcCls:
            SvcCls.return_value.search_products.side_effect = (
                WBRateLimitedError('429'))
            resp = self.client.get('/api/competitors/search?q=носки')
        self.assertEqual(resp.status_code, 503)
        self.assertIn('WB', resp.get_json()['error'])

    def test_search_ok(self):
        with self._as_user(), \
             patch('routes.competitors.CompetitorFetchService') as SvcCls:
            SvcCls.return_value.search_products.return_value = [
                {'nm_id': 1, 'title': 'X'}]
            resp = self.client.get('/api/competitors/search?q=носки')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.get_json()), 1)


class SettingsTest(RoutesTestBase):
    def test_put_normalizes_and_schedules_on_enable(self):
        with self._as_user():
            resp = self.client.put('/api/competitors/settings', json={
                'is_enabled': True, 'sync_interval_minutes': 5,
                'max_products': 99999, 'discount_alert_pp': 200})
        data = resp.get_json()
        self.assertEqual(data['sync_interval_minutes'], 30)   # clamp снизу
        self.assertEqual(data['max_products'], 1000)          # clamp cap
        self.assertEqual(data['discount_alert_pp'], 50)       # clamp
        self.assertIsNotNone(data['next_sync_due_at'])

    def test_proxy_masked_in_response(self):
        import os
        from cryptography.fernet import Fernet
        old = os.environ.get('ENCRYPTION_KEY')
        os.environ['ENCRYPTION_KEY'] = Fernet.generate_key().decode('ascii')
        try:
            with self._as_user():
                resp = self.client.put('/api/competitors/settings', json={
                    'proxy_url': 'http://u:pw@p.example.com:1080'})
            data = resp.get_json()
            self.assertNotIn('proxy_url', data)
            self.assertNotIn('pw', str(data))
            self.assertEqual(data['proxy']['masked'],
                             'http://p.example.com:1080')
        finally:
            if old is None:
                os.environ.pop('ENCRYPTION_KEY', None)
            else:
                os.environ['ENCRYPTION_KEY'] = old

    def test_force_sync_schedules(self):
        with self._as_user():
            resp = self.client.post('/api/competitors/sync')
        self.assertTrue(resp.get_json()['scheduled'])


class GroupOwnProductTest(RoutesTestBase):
    def test_foreign_own_product_rejected(self):
        foreign_product = Product(
            seller_id=self.other.id, nm_id=999, title='Чужой')
        db.session.add(foreign_product)
        db.session.commit()
        with self._as_user():
            resp = self.client.put(
                f'/api/competitors/groups/{self.group.id}',
                json={'own_product_id': foreign_product.id})
        self.assertEqual(resp.status_code, 400)

    def test_own_product_accepted_and_compare_position(self):
        own = Product(seller_id=self.seller.id, nm_id=1000, title='Мой',
                      price=2000, discount_price=1500)
        db.session.add(own)
        db.session.flush()
        for i, sale in enumerate([1000, 1400, 1600, 2000]):
            db.session.add(CompetitorProduct(
                seller_id=self.seller.id, group_id=self.group.id,
                nm_id=2000 + i, current_sale_price=sale))
        db.session.commit()
        with self._as_user():
            resp = self.client.put(
                f'/api/competitors/groups/{self.group.id}',
                json={'own_product_id': own.id})
            self.assertEqual(resp.status_code, 200)
            resp = self.client.get(f'/api/competitors/compare/{self.group.id}')
        data = resp.get_json()
        # own 1500: дешевле него 1000 и 1400 => позиция 3 из 5
        self.assertEqual(data['own_product']['position'], 3)
        self.assertEqual(data['own_product']['total_with_own'], 5)
        self.assertEqual(data['own_product']['vs_min_percent'], 50.0)


if __name__ == '__main__':
    unittest.main()
