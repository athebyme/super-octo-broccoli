# -*- coding: utf-8 -*-
"""Тесты WB-ревизии: live-сверка карточек с фиксацией расхождений."""

import json
import os
import unittest
from unittest.mock import patch, MagicMock


class TestWBCardAudit(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ['DISABLE_SECURE_COOKIE'] = '1'
        import sqlalchemy as _sa
        from sqlalchemy.pool import StaticPool
        import seller_platform  # noqa
        from models import db
        cls.app = seller_platform.app
        cls.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        cls.app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
        cls.app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {}
        cls.app.config['WTF_CSRF_ENABLED'] = False
        cls.app.config['TESTING'] = True
        cls._engine = _sa.create_engine(
            'sqlite:///:memory:',
            connect_args={'check_same_thread': False},
            poolclass=StaticPool,
        )
        db._app_engines[cls.app] = {None: cls._engine}
        cls.db = db

    def setUp(self):
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.db.create_all()
        from models import User, Seller
        user = User(username='audit_seller', email='au@example.com',
                    password_hash='x')
        self.db.session.add(user)
        self.db.session.flush()
        self.seller = Seller(user_id=user.id, company_name='Ревизия',
                             wb_seller_id='777')
        self.seller.wb_api_key = 'test-api-key'
        self.db.session.add(self.seller)
        self.db.session.commit()

    def tearDown(self):
        self.db.session.remove()
        self.db.drop_all()
        self.ctx.pop()

    def _product(self, nm_id, characteristics=None, photos_json='[]'):
        from models import Product
        p = Product(
            seller_id=self.seller.id, nm_id=nm_id,
            vendor_code=f'VC-{nm_id}', title=f'Карточка {nm_id}',
            is_active=True, photos_json=photos_json,
            characteristics_json=(
                json.dumps(characteristics, ensure_ascii=False)
                if characteristics is not None else None
            ),
        )
        self.db.session.add(p)
        self.db.session.commit()
        return p

    def _audit(self, product_ids, cards, wb_errors=None):
        from services import wb_card_audit as mod
        mock_client = MagicMock()
        mock_client.fetch_cards_by_nm_ids.return_value = cards
        mock_client.get_cards_error_list.return_value = wb_errors or []
        with patch.object(mod, 'WildberriesAPIClient',
                          return_value=mock_client):
            return mod.audit_cards(self.seller, product_ids)

    def test_ok_card_persists_audit_and_photos(self):
        p = self._product(2001, characteristics=[
            {'id': 14177449, 'name': 'Цвет', 'value': ['мятный']},
        ])
        card = {
            'title': 'Т', 'description': 'Д',
            'photos': [{'big': 'http://wb/1.jpg'}],
            'dimensions': {'length': 20},
            'characteristics': [
                {'id': 14177449, 'name': 'Цвет', 'value': ['мятный']},
            ],
        }
        report = self._audit([p.id], {2001: card})

        self.assertEqual(report['summary']['ok'], 1)
        item = report['items'][0]
        self.assertEqual(item['status'], 'ok')
        self.assertEqual(item['photos_count'], 1)
        self.db.session.refresh(p)
        audit = json.loads(p.wb_audit_json)
        self.assertTrue(audit['exists'])
        self.assertIsNotNone(p.wb_audited_at)
        self.assertEqual(json.loads(p.photos_json), ['http://wb/1.jpg'])

    def test_missing_characteristics_and_empty_photos_flag_divergence(self):
        p = self._product(
            2002,
            characteristics=[
                {'id': 101, 'name': 'Материал изделия', 'value': ['Силикон']},
                {'id': 102, 'name': 'Пол', 'value': ['Унисекс']},
            ],
            photos_json='[1, 2, 3, 4, 5]',
        )
        card = {
            'title': 'Т', 'description': '',
            'photos': [],
            'characteristics': [
                {'id': 101, 'name': 'Материал изделия', 'value': ['Силикон']},
            ],
        }
        report = self._audit([p.id], {2002: card})

        item = report['items'][0]
        self.assertEqual(item['status'], 'diverged')
        chars = item['characteristics']
        self.assertEqual(chars['missing_on_wb'], 1)
        self.assertIn('Пол', chars['missing_on_wb_names'])
        self.assertEqual(item['photos_count'], 0)
        # Фейковый локальный список фото заменён честным пустым состоянием.
        self.db.session.refresh(p)
        self.assertEqual(json.loads(p.photos_json), [])

    def test_not_found_card_reported(self):
        p = self._product(2003)
        report = self._audit([p.id], {})
        self.assertEqual(report['summary']['not_found'], 1)
        self.db.session.refresh(p)
        audit = json.loads(p.wb_audit_json)
        self.assertFalse(audit['exists'])

    def test_tenant_scope_excludes_foreign_products(self):
        from models import User, Seller, Product
        other_user = User(username='audit_other', email='auo@example.com',
                          password_hash='x')
        self.db.session.add(other_user)
        self.db.session.flush()
        other = Seller(user_id=other_user.id, company_name='Чужой')
        self.db.session.add(other)
        self.db.session.flush()
        foreign = Product(
            seller_id=other.id, nm_id=2004, vendor_code='F-1',
            title='Чужая', is_active=True,
        )
        self.db.session.add(foreign)
        self.db.session.commit()

        report = self._audit([foreign.id], {2004: {'photos': []}})
        self.assertEqual(report['items'], [])
        self.db.session.refresh(foreign)
        self.assertIsNone(foreign.wb_audit_json)

    def test_invalid_ids_rejected_without_query(self):
        report = self._audit([True, -5, 'x', 0], {})
        self.assertEqual(report['items'], [])


class TestWBAuditRoute(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        TestWBCardAudit.setUpClass.__func__(cls)

    setUp = TestWBCardAudit.setUp
    tearDown = TestWBCardAudit.tearDown

    def test_route_rejects_foreign_and_unpublished(self):
        from models import User
        user = self.db.session.get(User, self.user_id_or_none())
        client = self.app.test_client()
        with client.session_transaction() as sess:
            sess['_user_id'] = str(user.id)
        resp = client.post('/my-products/wb-audit', json={'product_ids': [999]})
        self.assertEqual(resp.status_code, 400)
        self.assertIn('опубликованных', resp.get_json()['error'])

    def user_id_or_none(self):
        from models import Seller
        return self.db.session.get(Seller, self.seller.id).user_id


if __name__ == '__main__':
    unittest.main()
