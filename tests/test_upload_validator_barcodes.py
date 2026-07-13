# -*- coding: utf-8 -*-
"""Тесты поиска конфликтов баркодов в upload_readiness_validator."""

import json
import os
import unittest


class TestBarcodeConflicts(unittest.TestCase):
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
        user = User(username='bc_seller', email='bc@example.com', password_hash='x')
        self.db.session.add(user)
        self.db.session.flush()
        self.seller = Seller(user_id=user.id, company_name='Баркоды', wb_seller_id='888')
        self.db.session.add(self.seller)
        self.db.session.commit()

    def tearDown(self):
        self.db.session.remove()
        self.db.drop_all()
        self.ctx.pop()

    def _candidate(self, barcodes):
        from models import ImportedProduct
        p = ImportedProduct(
            seller_id=self.seller.id, title='Кандидат',
            import_status='validated',
            barcodes=json.dumps(barcodes),
        )
        self.db.session.add(p)
        self.db.session.commit()
        return p

    def test_conflict_with_imported_product(self):
        """Регрессия: раньше ветка искала несуществующий статус 'completed'
        и конфликты с уже загруженными на WB товарами не находились."""
        from models import ImportedProduct
        from services.upload_readiness_validator import _find_barcode_conflicts
        existing = ImportedProduct(
            seller_id=self.seller.id, title='Уже на WB',
            import_status='imported', wb_nm_id=111222,
            barcodes=json.dumps(['69595323']),
        )
        self.db.session.add(existing)
        self.db.session.commit()

        candidate = self._candidate(['69595323'])
        conflicts = _find_barcode_conflicts(candidate, ['69595323'])
        self.assertEqual(conflicts, [('69595323', 111222)])

    def test_conflict_with_product_sizes(self):
        from models import Product
        from services.upload_readiness_validator import _find_barcode_conflicts
        wb_product = Product(
            seller_id=self.seller.id, nm_id=333444, vendor_code='VC-333',
            sizes_json=json.dumps([{'techSize': '0', 'skus': ['4600000000077']}]),
        )
        self.db.session.add(wb_product)
        self.db.session.commit()

        candidate = self._candidate(['4600000000077'])
        conflicts = _find_barcode_conflicts(candidate, ['4600000000077'])
        self.assertEqual(conflicts, [('4600000000077', 333444)])

    def test_no_conflict_for_unique_barcodes(self):
        from models import ImportedProduct
        from services.upload_readiness_validator import _find_barcode_conflicts
        existing = ImportedProduct(
            seller_id=self.seller.id, title='Уже на WB',
            import_status='imported', wb_nm_id=111222,
            barcodes=json.dumps(['1111111111111']),
        )
        self.db.session.add(existing)
        self.db.session.commit()

        candidate = self._candidate(['2222222222222'])
        self.assertEqual(_find_barcode_conflicts(candidate, ['2222222222222']), [])

    def test_conflict_scoped_to_seller(self):
        """Баркоды другого продавца не считаются конфликтом."""
        from models import User, Seller, ImportedProduct
        from services.upload_readiness_validator import _find_barcode_conflicts
        other_user = User(username='bc_other', email='bco@example.com', password_hash='x')
        self.db.session.add(other_user)
        self.db.session.flush()
        other_seller = Seller(user_id=other_user.id, company_name='Чужой', wb_seller_id='999')
        self.db.session.add(other_seller)
        self.db.session.flush()
        foreign = ImportedProduct(
            seller_id=other_seller.id, title='Чужой товар',
            import_status='imported', wb_nm_id=777888,
            barcodes=json.dumps(['3333333333333']),
        )
        self.db.session.add(foreign)
        self.db.session.commit()

        candidate = self._candidate(['3333333333333'])
        self.assertEqual(_find_barcode_conflicts(candidate, ['3333333333333']), [])

    def test_candidate_not_conflicting_with_itself(self):
        from services.upload_readiness_validator import _find_barcode_conflicts
        candidate = self._candidate(['4444444444444'])
        candidate.import_status = 'imported'
        candidate.wb_nm_id = 123123
        self.db.session.commit()
        self.assertEqual(_find_barcode_conflicts(candidate, ['4444444444444']), [])


if __name__ == '__main__':
    unittest.main()
