# -*- coding: utf-8 -*-
"""Роуты конкурентов v2: строгая валидация, tenant scope, bounded WB."""
import json
import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch

from flask import Flask

from models import (
    BackgroundJob, CompetitorGroup, CompetitorMonitorSettings,
    CompetitorProduct, CompetitorProductMatch, Product, Seller,
    SellerCompetitorMatchReview, Supplier, SupplierProduct, User, db,
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
        self.assertEqual(
            {item['nm_id'] for item in resp.get_json()['products']},
            {111, 222})
        self.assertTrue(all(item['id'] for item in resp.get_json()['products']))
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
        self.assertEqual(resp.get_json()['wb_supplier_id'], 332183)

    def test_cancel_supplier_import_is_tenant_scoped(self):
        self.group.import_requested = True
        foreign = CompetitorGroup(
            seller_id=self.other.id, name='Foreign', import_requested=True)
        db.session.add(foreign)
        db.session.commit()

        with self._as_user():
            resp = self.client.delete(
                f'/api/competitors/groups/{self.group.id}/import')
            foreign_resp = self.client.delete(
                f'/api/competitors/groups/{foreign.id}/import')

        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.get_json()['import_requested'])
        self.assertEqual(foreign_resp.status_code, 404)
        db.session.refresh(self.group)
        db.session.refresh(foreign)
        self.assertFalse(self.group.import_requested)
        self.assertTrue(foreign.import_requested)

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
        self.assertEqual(resp.get_json()['code'], 'wb_rate_limited')
        self.assertTrue(resp.get_json()['retryable'])

    def test_search_ok(self):
        with self._as_user(), \
             patch('routes.competitors.CompetitorFetchService') as SvcCls:
            SvcCls.return_value.search_products.return_value = [
                {'nm_id': 1, 'title': 'X'}]
            resp = self.client.get('/api/competitors/search?q=носки')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.get_json()), 1)

    def test_seller_catalog_preview_is_one_bounded_page(self):
        preview = [{
            'nm_id': 11, 'title': 'Товар', 'supplier_name': 'Магазин',
            'wb_supplier_id': 332183,
        }]
        with self._as_user(), \
             patch('routes.competitors.CompetitorFetchService') as SvcCls:
            SvcCls.return_value.fetch_seller_catalog_page.return_value = preview
            resp = self.client.get(
                '/api/competitors/seller-catalog?supplier_id=332183&page=1')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), preview)
        SvcCls.return_value.fetch_seller_catalog_page.assert_called_once_with(
            332183, page=1)

    def test_seller_catalog_429_returns_honest_503(self):
        with self._as_user(), \
             patch('routes.competitors.CompetitorFetchService') as SvcCls:
            SvcCls.return_value.fetch_seller_catalog_page.side_effect = (
                WBRateLimitedError('429'))
            resp = self.client.get(
                '/api/competitors/seller-catalog?supplier_id=332183')
        self.assertEqual(resp.status_code, 503)
        self.assertIn('WB', resp.get_json()['error'])
        self.assertEqual(resp.get_json()['code'], 'wb_rate_limited')
        self.assertTrue(resp.get_json()['retryable'])

    def test_seller_preview_uses_configured_proxy_server_side(self):
        settings = CompetitorMonitorSettings(
            seller_id=self.seller.id,
            _proxy_url='http://proxy.example.test:8080')
        db.session.add(settings)
        db.session.commit()
        with self._as_user(), \
             patch('routes.competitors.CompetitorFetchService') as SvcCls:
            SvcCls.return_value.fetch_seller_catalog_page.return_value = []
            resp = self.client.get(
                '/api/competitors/seller-catalog?supplier_id=332183')
        self.assertEqual(resp.status_code, 200)
        SvcCls.assert_called_once_with(
            proxy_url='http://proxy.example.test:8080')

    def test_seller_catalog_rejects_missing_or_invalid_id(self):
        with self._as_user():
            for query in ('', '?supplier_id=0', '?supplier_id=not-a-number'):
                resp = self.client.get('/api/competitors/seller-catalog' + query)
                self.assertEqual(resp.status_code, 400)


class MatchingRoutesTest(RoutesTestBase):
    def setUp(self):
        super().setUp()
        self.user.id = self.seller.user_id
        self.competitor = CompetitorProduct(
            seller_id=self.seller.id, group_id=self.group.id,
            nm_id=551122, title='Наблюдаемый товар WB')
        db.session.add(self.competitor)
        db.session.commit()

    def _supplier_product(self):
        supplier = Supplier(name='Feed', code='route-feed')
        db.session.add(supplier)
        db.session.flush()
        product = SupplierProduct(
            supplier_id=supplier.id, external_id='x-1',
            title='Исходный товар поставщика',
            ai_seo_title='Не должно попасть в API',
            original_data_json='{"title":"Исходный товар поставщика"}',
        )
        db.session.add(product)
        db.session.commit()
        return product

    def test_run_endpoint_only_enqueues_bounded_background_job(self):
        with self._as_user(), \
             patch('services.competitor_matching._run_llm') as llm, \
             patch('services.competitor_matching._image_similarity') as image:
            response = self.client.post(
                f'/api/competitors/groups/{self.group.id}/matches/run',
                json={})
        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.get_json()['shared_cache'])
        llm.assert_not_called()
        image.assert_not_called()
        job = BackgroundJob.query.one()
        self.assertEqual(job.job_type, 'competitor_matching')
        self.assertEqual(job.total, 1)
        self.assertEqual(CompetitorProductMatch.query.count(), 1)

    def test_job_status_and_match_review_are_tenant_scoped(self):
        supplier_product = self._supplier_product()
        shared = CompetitorProductMatch(
            nm_id=self.competitor.nm_id,
            suggested_supplier_product_id=supplier_product.id,
            processing_status='completed', predicted_match_type='same',
            text_score=80, deterministic_score=75, final_score=80,
            algorithm_version='supplier-observed-v1', llm_status='completed')
        db.session.add(shared)
        db.session.commit()
        with self._as_user():
            response = self.client.put(
                f'/api/competitors/matches/{shared.id}/review', json={
                    'action': 'confirm',
                    'supplier_product_id': supplier_product.id,
                    'match_type': 'same',
                })
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.get_json()['shared_suggestion_changed'])
        self.assertEqual(SellerCompetitorMatchReview.query.one().seller_id,
                         self.seller.id)

        foreign_user = MagicMock()
        foreign_user.is_authenticated = True
        foreign_user.seller = self.other
        foreign_user.id = self.other.user_id
        with patch('flask_login.utils._get_user', return_value=foreign_user):
            denied = self.client.put(
                f'/api/competitors/matches/{shared.id}/review', json={
                    'action': 'reject'})
        self.assertEqual(denied.status_code, 404)
        db.session.refresh(shared)
        self.assertEqual(shared.suggested_supplier_product_id,
                         supplier_product.id)

    def test_supplier_search_returns_only_observed_card_fields(self):
        product = self._supplier_product()
        with self._as_user():
            response = self.client.get(
                '/api/competitors/supplier-products/search?q=Исходный')
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data['source_scope'], 'supplier_observed_only')
        self.assertEqual(data['items'][0]['id'], product.id)
        self.assertNotIn('ai_seo_title', data['items'][0])
        self.assertNotIn('Не должно попасть', json.dumps(
            data, ensure_ascii=False))
        with self._as_user():
            ai_only = self.client.get(
                '/api/competitors/supplier-products/search?q=Не%20должно')
        self.assertEqual(ai_only.status_code, 200)
        self.assertEqual(ai_only.get_json()['items'], [])


class SettingsTest(RoutesTestBase):
    def test_put_normalizes_and_schedules_on_enable(self):
        with self._as_user():
            resp = self.client.put('/api/competitors/settings', json={
                'is_enabled': True, 'sync_interval_minutes': 5,
                'max_products': 1000, 'discount_alert_pp': 200})
        data = resp.get_json()
        self.assertEqual(data['sync_interval_minutes'], 30)   # clamp снизу
        self.assertEqual(data['max_products'], 1000)          # valid explicit cap
        self.assertEqual(data['discount_alert_pp'], 50)       # clamp
        self.assertIsNotNone(data['next_sync_due_at'])

    def test_put_rejects_over_cap_without_committing_other_settings(self):
        settings = CompetitorMonitorSettings(
            seller_id=self.seller.id, is_enabled=False,
            sync_interval_minutes=60, max_products=100000)
        db.session.add(settings)
        db.session.commit()
        with self._as_user():
            resp = self.client.put('/api/competitors/settings', json={
                'is_enabled': True, 'sync_interval_minutes': 120,
                'max_products': 99999,
            })

        self.assertEqual(resp.status_code, 400)
        self.assertIn('целым числом от 1 до 1000', resp.get_json()['error'])
        db.session.refresh(settings)
        self.assertFalse(settings.is_enabled)
        self.assertEqual(settings.sync_interval_minutes, 60)
        self.assertEqual(settings.max_products, 100000)
        self.assertIsNone(settings.next_sync_due_at)

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

    def test_force_sync_schedules_when_monitoring_enabled(self):
        with self._as_user():
            self.client.put(
                '/api/competitors/settings',
                json={'is_enabled': True},
            )
            resp = self.client.post('/api/competitors/sync')
        self.assertTrue(resp.get_json()['scheduled'])

    def test_force_sync_is_honest_when_monitoring_disabled(self):
        """Планировщик пропускает выключенных продавцов — обещать запуск нельзя."""
        with self._as_user():
            self.client.put(
                '/api/competitors/settings',
                json={'is_enabled': False},
            )
            resp = self.client.post('/api/competitors/sync')
        payload = resp.get_json()
        self.assertEqual(resp.status_code, 409)
        self.assertFalse(payload['scheduled'])
        self.assertEqual(payload['code'], 'monitoring_disabled')


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
