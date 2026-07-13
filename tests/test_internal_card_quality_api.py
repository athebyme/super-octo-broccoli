# -*- coding: utf-8 -*-
"""Tests for the read-only internal card-quality brief endpoint."""
import unittest

from flask import Flask
from werkzeug.security import generate_password_hash

from models import db, AgentTask, Product, Seller, ServiceAgent, User
from routes.internal_api import internal_api_bp


class InternalCardQualityBriefTestCase(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        self.app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
        db.init_app(self.app)
        self.app.register_blueprint(internal_api_bp)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

        user1 = User(username='seller-1', email='seller1@example.com', password_hash='x')
        user2 = User(username='seller-2', email='seller2@example.com', password_hash='x')
        db.session.add_all([user1, user2])
        db.session.flush()
        self.seller1 = Seller(user_id=user1.id, company_name='Seller One')
        self.seller2 = Seller(user_id=user2.id, company_name='Seller Two')
        db.session.add_all([self.seller1, self.seller2])
        db.session.flush()

        self.agent1 = ServiceAgent(
            id='agent-1', name='agent-one', display_name='Agent One',
            api_key_hash=generate_password_hash('key-1'),
        )
        db.session.add(self.agent1)
        db.session.flush()

        self.task1 = AgentTask(
            id='task-1', agent_id=self.agent1.id, seller_id=self.seller1.id,
            task_type='quality_audit', title='Owned task', status='running',
        )
        db.session.add(self.task1)

        self.product1 = Product(
            seller_id=self.seller1.id, nm_id=111, vendor_code='SKU-1',
            title='Проблемная карточка 1', price=500, quantity=10,
            quality_score=40.0, quality_impact=30.0,
            attention_reasons='few_photos,weak_chars',
            nm_rating=3.5, wb_views_30d=5, wb_orders_30d=0,
            wb_cart_conv=1.0, wb_buyout_rate=50.0,
        )
        self.product2 = Product(
            seller_id=self.seller1.id, nm_id=222, vendor_code='SKU-2',
            title='Проблемная карточка 2', price=700, quantity=3,
            quality_score=70.0, quality_impact=10.0,
            attention_reasons='no_views',
            nm_rating=4.8, wb_views_30d=2, wb_orders_30d=0,
            wb_cart_conv=0.0, wb_buyout_rate=None,
        )
        # Healthy card with no attention reasons must never show up by default.
        self.product3 = Product(
            seller_id=self.seller1.id, nm_id=333, vendor_code='SKU-3',
            title='Здоровая карточка', price=300, quantity=8,
            quality_score=95.0, quality_impact=None, attention_reasons=None,
        )
        db.session.add_all([self.product1, self.product2, self.product3])
        db.session.commit()
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    @property
    def auth(self):
        return {'X-Agent-Id': 'agent-1', 'X-Agent-Key': 'key-1'}

    def task_headers(self, task_id='task-1'):
        return {**self.auth, 'X-Task-Id': task_id}

    def _post(self, seller_id, headers=None, **kwargs):
        return self.client.post(
            f'/internal/v1/sellers/{seller_id}/products/quality-brief',
            headers=headers, **kwargs,
        )

    def test_missing_agent_key_is_rejected(self):
        response = self._post(self.seller1.id)
        self.assertIn(response.status_code, (401, 403))

    def test_foreign_seller_scope_is_rejected(self):
        response = self._post(self.seller2.id, headers=self.task_headers())
        self.assertEqual(response.status_code, 403)

    def test_default_selection_returns_products_ordered_by_impact(self):
        response = self._post(self.seller1.id, headers=self.task_headers())
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload['total'], 2)
        products = payload['products']
        self.assertEqual(
            [p['id'] for p in products], [self.product1.id, self.product2.id],
        )
        first = products[0]
        self.assertEqual(first['attention_reasons'], ['few_photos', 'weak_chars'])
        self.assertEqual(first['quality_impact'], 30.0)
        self.assertEqual(first['wb_views_30d'], 5)
        self.assertIsInstance(first['recommendations'], list)
        self.assertIn('reason_labels', payload)
        self.assertEqual(payload['reason_labels']['few_photos'], 'Мало фото')

    def test_explicit_product_ids_filter(self):
        response = self._post(
            self.seller1.id, headers=self.task_headers(),
            json={'product_ids': [self.product2.id]},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(
            [p['id'] for p in payload['products']], [self.product2.id],
        )

    def test_reason_filter(self):
        response = self.client.post(
            f'/internal/v1/sellers/{self.seller1.id}/products/quality-brief'
            '?reason=few_photos',
            headers=self.task_headers(),
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(
            [p['id'] for p in payload['products']], [self.product1.id],
        )

        invalid = self.client.post(
            f'/internal/v1/sellers/{self.seller1.id}/products/quality-brief'
            '?reason=abrakadabra',
            headers=self.task_headers(),
        )
        self.assertEqual(invalid.status_code, 400)

    def test_response_excludes_protected_fields(self):
        response = self._post(self.seller1.id, headers=self.task_headers())
        payload = response.get_json()
        forbidden_keys = {
            'price', 'quantity', 'supplier_price', 'wb_api_key',
            'api_key', 'credentials',
        }
        for product in payload['products']:
            self.assertTrue(forbidden_keys.isdisjoint(product.keys()))


if __name__ == '__main__':
    unittest.main()
