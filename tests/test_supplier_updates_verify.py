# -*- coding: utf-8 -*-
"""Тесты сверки карточек с WB в хабе обновлений (verify_cards_on_wb + route)."""

import json
import os
import unittest
from unittest.mock import patch, MagicMock


class TestSupplierUpdatesVerify(unittest.TestCase):
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
        user = User(username='vf_seller', email='vf@example.com', password_hash='x')
        self.db.session.add(user)
        self.db.session.flush()
        self.user_id = user.id
        self.seller = Seller(user_id=user.id, company_name='Сверка', wb_seller_id='555')
        self.seller.wb_api_key = 'test-api-key'
        self.db.session.add(self.seller)
        self.db.session.commit()

    def tearDown(self):
        self.db.session.remove()
        self.db.drop_all()
        self.ctx.pop()

    def _product(self, nm_id, vendor_code, photos_json='[]'):
        from models import Product
        p = Product(
            seller_id=self.seller.id, nm_id=nm_id, vendor_code=vendor_code,
            title=f'Карточка {nm_id}', is_active=True, photos_json=photos_json,
        )
        self.db.session.add(p)
        self.db.session.commit()
        return p

    def _verify(self, product_ids, cards, wb_errors, expected_photos=None):
        from services import supplier_update_hub as hub
        mock_client = MagicMock()
        mock_client.fetch_cards_by_nm_ids.return_value = cards
        mock_client.get_cards_error_list.return_value = wb_errors
        patches = [patch.object(hub, 'WildberriesAPIClient', return_value=mock_client)]
        if expected_photos is not None:
            patches.append(patch.object(
                hub, 'build_target_photo_set',
                return_value=[f'http://x/{i}.jpg' for i in range(expected_photos)],
            ))
        with patches[0]:
            if len(patches) > 1:
                with patches[1]:
                    return hub.verify_cards_on_wb(self.seller, product_ids)
            return hub.verify_cards_on_wb(self.seller, product_ids)

    def test_ok_card_updates_local_photos(self):
        p = self._product(1001, 'VC-1001')
        card = {'photos': [{'big': 'http://wb/1.jpg'}, {'big': 'http://wb/2.jpg'}]}
        report = self._verify([p.id], {1001: card}, [])

        self.assertEqual(report['summary']['ok'], 1)
        self.assertEqual(report['items'][0]['status'], 'ok')
        self.assertEqual(report['items'][0]['wb_photos'], 2)
        self.db.session.refresh(p)
        self.assertEqual(json.loads(p.photos_json),
                         ['http://wb/1.jpg', 'http://wb/2.jpg'])

    def test_error_from_wb_error_list(self):
        p = self._product(1002, 'VC-1002')
        report = self._verify(
            [p.id],
            {1002: {'photos': [{'big': 'http://wb/1.jpg'}]}},
            [{'nmID': 1002, 'errors': ['Недопустимое фото']}],
        )
        self.assertEqual(report['summary']['error'], 1)
        self.assertEqual(report['items'][0]['status'], 'error')
        self.assertIn('Недопустимое фото', report['items'][0]['errors'])

    def test_error_from_real_wb_format_by_vendor_code(self):
        """Реальный формат error/list: dict errors по vendorCode, без nmID."""
        p = self._product(1005, 'VC-1005')
        report = self._verify(
            [p.id],
            {1005: {'photos': [{'big': 'http://wb/1.jpg'}]}},
            [{
                'batchUUID': 'y',
                'vendorCodes': ['VC-1005'],
                'errors': {'VC-1005': ['Измените значения полей «Артикул продавца»']},
                'updatedAt': '2026-07-13T00:00:00Z',
            }],
        )
        self.assertEqual(report['summary']['error'], 1)
        self.assertEqual(report['items'][0]['status'], 'error')
        self.assertIn('Артикул продавца', report['items'][0]['errors'][0])

    def test_missing_card_reported_not_found(self):
        p = self._product(1003, 'VC-1003')
        report = self._verify([p.id], {}, [])
        self.assertEqual(report['summary']['not_found'], 1)
        self.assertEqual(report['items'][0]['status'], 'not_found')

    def test_pending_when_wb_has_fewer_photos_than_target(self):
        from models import ImportedProduct, Supplier, SupplierProduct
        supplier = Supplier(name='Опт', code='opt-vf')
        self.db.session.add(supplier)
        self.db.session.flush()
        sp = SupplierProduct(
            supplier_id=supplier.id, external_id='E-1', title='Т',
            photo_urls_json=json.dumps(['http://s/1.jpg'] * 5),
        )
        self.db.session.add(sp)
        self.db.session.flush()
        p = self._product(1004, 'VC-1004')
        imp = ImportedProduct(
            seller_id=self.seller.id, product_id=p.id,
            supplier_id=supplier.id, supplier_product_id=sp.id,
            import_status='imported',
        )
        self.db.session.add(imp)
        self.db.session.commit()

        card = {'photos': [{'big': 'http://wb/1.jpg'}, {'big': 'http://wb/2.jpg'}]}
        report = self._verify([p.id], {1004: card}, [], expected_photos=5)

        self.assertEqual(report['summary']['pending'], 1)
        item = report['items'][0]
        self.assertEqual(item['status'], 'pending')
        self.assertEqual(item['wb_photos'], 2)
        self.assertEqual(item['expected_photos'], 5)

    def test_tenant_scope_excludes_foreign_products(self):
        from models import User, Seller, Product
        other_user = User(username='vf_other', email='vfo@example.com', password_hash='x')
        self.db.session.add(other_user)
        self.db.session.flush()
        other_seller = Seller(user_id=other_user.id, company_name='Чужой', wb_seller_id='556')
        self.db.session.add(other_seller)
        self.db.session.flush()
        foreign = Product(
            seller_id=other_seller.id, nm_id=2001, vendor_code='F-1',
            title='Чужая', is_active=True,
        )
        self.db.session.add(foreign)
        self.db.session.commit()

        report = self._verify([foreign.id], {2001: {'photos': []}}, [])
        self.assertEqual(report['items'], [])

    # ── Route ────────────────────────────────────────────────────

    def _client(self):
        client = self.app.test_client()
        with client.session_transaction() as sess:
            sess['_user_id'] = str(self.user_id)
            sess['_fresh'] = True
        return client

    def test_verify_start_creates_job(self):
        from models import BackgroundJob
        from services.supplier_update_hub import VERIFY_JOB_TYPE
        p = self._product(3001, 'VC-3001')
        with patch('routes.supplier_updates.run_verify_job'):
            resp = self._client().post(
                '/api/supplier-updates/verify/start',
                json={'product_ids': [p.id]},
            )
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data['success'])
        job = BackgroundJob.query.filter_by(
            seller_id=self.seller.id, job_type=VERIFY_JOB_TYPE).first()
        self.assertIsNotNone(job)
        self.assertEqual(job.total, 1)

    def test_verify_start_rejects_foreign_ids(self):
        from models import User, Seller, Product
        other_user = User(username='vf_other2', email='vfo2@example.com', password_hash='x')
        self.db.session.add(other_user)
        self.db.session.flush()
        other_seller = Seller(user_id=other_user.id, company_name='Чужой2', wb_seller_id='557')
        self.db.session.add(other_seller)
        self.db.session.flush()
        foreign = Product(
            seller_id=other_seller.id, nm_id=3002, vendor_code='F-2',
            title='Чужая', is_active=True,
        )
        self.db.session.add(foreign)
        self.db.session.commit()

        with patch('routes.supplier_updates.run_verify_job'):
            resp = self._client().post(
                '/api/supplier-updates/verify/start',
                json={'product_ids': [foreign.id]},
            )
        self.assertEqual(resp.status_code, 400)


if __name__ == '__main__':
    unittest.main()
