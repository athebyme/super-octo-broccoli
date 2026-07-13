# -*- coding: utf-8 -*-
"""Тесты отбора кандидатов автопубликации: SQL-предфильтры, backfill цены,
эскалация кулдауна скипов и сверка с WB cards/error/list."""

import json
import os
import unittest
from datetime import datetime
from unittest.mock import patch, MagicMock


class TestAutoPublishCandidates(unittest.TestCase):
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
        from models import User, Seller, Supplier, AutoPublishSettings
        user = User(username='ap_seller', email='ap@example.com', password_hash='x')
        self.db.session.add(user)
        self.db.session.flush()
        self.seller = Seller(user_id=user.id, company_name='АвтоПаб', wb_seller_id='777')
        self.seller.wb_api_key = 'test-api-key'
        self.db.session.add(self.seller)
        self.db.session.flush()
        self.supplier = Supplier(name='Опт', code='opt')
        self.db.session.add(self.supplier)
        self.db.session.flush()
        self.settings = AutoPublishSettings(
            seller_id=self.seller.id, is_enabled=True,
            batch_size=10, max_daily_publishes=100,
        )
        self.db.session.add(self.settings)
        self.db.session.commit()

    def tearDown(self):
        self.db.session.remove()
        self.db.drop_all()
        self.ctx.pop()

    def _make_product(self, **kw):
        from models import ImportedProduct
        defaults = dict(
            seller_id=self.seller.id,
            supplier_id=self.supplier.id,
            import_status='validated',
            title='Товар',
            supplier_price=100.0,
            wb_subject_id=123,
            photo_urls='["http://x/1.jpg"]',
            barcodes='["4600000000001"]',
        )
        defaults.update(kw)
        p = ImportedProduct(**defaults)
        self.db.session.add(p)
        self.db.session.commit()
        return p

    def _service(self):
        from services.auto_publish_service import AutoPublishService
        return AutoPublishService(self.seller, self.settings)

    def _run(self):
        from models import AutoPublishRun
        run = AutoPublishRun(
            seller_id=self.seller.id, run_uid='test-run',
            status='running', started_at=datetime.utcnow(),
        )
        self.db.session.add(run)
        self.db.session.commit()
        return run

    # ── SQL-предфильтры ──────────────────────────────────────────

    def test_candidates_exclude_products_without_price(self):
        good = self._make_product(external_id='GOOD')
        self._make_product(external_id='NO-PRICE', supplier_price=None)
        self._make_product(external_id='ZERO-PRICE', supplier_price=0)

        items = self._service()._select_candidates(self._run())
        ids = {i.imported_product_id for i in items}
        self.assertEqual(ids, {good.id})

    def test_candidates_exclude_products_without_photos(self):
        good = self._make_product(external_id='GOOD')
        self._make_product(external_id='NO-PHOTO', photo_urls=None)
        self._make_product(external_id='EMPTY-PHOTO', photo_urls='[]')

        items = self._service()._select_candidates(self._run())
        ids = {i.imported_product_id for i in items}
        self.assertEqual(ids, {good.id})

    def test_candidates_exclude_products_without_wb_category(self):
        good = self._make_product(external_id='GOOD')
        self._make_product(external_id='NO-SUBJ', wb_subject_id=None)

        items = self._service()._select_candidates(self._run())
        ids = {i.imported_product_id for i in items}
        self.assertEqual(ids, {good.id})

    # ── Backfill цены из каталога поставщика ─────────────────────

    def test_backfill_price_from_supplier_product(self):
        from models import SupplierProduct
        sp = SupplierProduct(
            supplier_id=self.supplier.id, external_id='EXT-1',
            title='Товар', supplier_price=250.0, supplier_quantity=7,
        )
        self.db.session.add(sp)
        self.db.session.flush()
        product = self._make_product(
            external_id='LATE-PRICE', supplier_price=None,
            supplier_product_id=sp.id, supplier_quantity=None,
        )

        items = self._service()._select_candidates(self._run())
        ids = {i.imported_product_id for i in items}

        self.db.session.refresh(product)
        self.assertEqual(product.supplier_price, 250.0)
        self.assertEqual(product.supplier_quantity, 7)
        self.assertIn(product.id, ids)

    def test_backfill_skips_products_without_catalog_price(self):
        from models import SupplierProduct
        sp = SupplierProduct(
            supplier_id=self.supplier.id, external_id='EXT-2',
            title='Товар', supplier_price=None,
        )
        self.db.session.add(sp)
        self.db.session.flush()
        product = self._make_product(
            external_id='STILL-NO-PRICE', supplier_price=None,
            supplier_product_id=sp.id,
        )

        items = self._service()._select_candidates(self._run())
        ids = {i.imported_product_id for i in items}

        self.db.session.refresh(product)
        self.assertIsNone(product.supplier_price)
        self.assertNotIn(product.id, ids)

    # ── Эскалация кулдауна скипов ─────────────────────────────────

    def test_skip_cooldown_escalates_with_history(self):
        from models import AutoPublishItem
        product = self._make_product(external_id='SKIPPY')
        run = self._run()
        # Две прошлые skip-попытки по валидации
        for _ in range(2):
            self.db.session.add(AutoPublishItem(
                run_id=run.id, imported_product_id=product.id,
                seller_id=self.seller.id, status='skipped',
                step='skipped', error_step='validating',
            ))
        current = AutoPublishItem(
            run_id=run.id, imported_product_id=product.id,
            seller_id=self.seller.id, status='skipped',
            step='skipped', error_step='validating',
        )
        self.db.session.add(current)
        self.db.session.commit()

        service = self._service()
        base = max(self.settings.retry_delay_minutes, 15)
        cooldown = service._validation_skip_cooldown_minutes(current)
        self.assertEqual(cooldown, min(base * 4, 24 * 60))

    def test_skip_cooldown_first_skip_uses_base(self):
        from models import AutoPublishItem
        product = self._make_product(external_id='FIRST-SKIP')
        run = self._run()
        current = AutoPublishItem(
            run_id=run.id, imported_product_id=product.id,
            seller_id=self.seller.id, status='skipped',
            step='skipped', error_step='validating',
        )
        self.db.session.add(current)
        self.db.session.commit()

        base = max(self.settings.retry_delay_minutes, 15)
        self.assertEqual(
            self._service()._validation_skip_cooldown_minutes(current), base
        )

    def test_skip_cooldown_capped_at_24h(self):
        from models import AutoPublishItem
        product = self._make_product(external_id='OLD-SKIPPY')
        run = self._run()
        for _ in range(10):
            self.db.session.add(AutoPublishItem(
                run_id=run.id, imported_product_id=product.id,
                seller_id=self.seller.id, status='skipped',
                step='skipped', error_step='validating',
            ))
        current = AutoPublishItem(
            run_id=run.id, imported_product_id=product.id,
            seller_id=self.seller.id, status='skipped',
            step='skipped', error_step='validating',
        )
        self.db.session.add(current)
        self.db.session.commit()

        self.assertEqual(
            self._service()._validation_skip_cooldown_minutes(current), 24 * 60
        )

    # ── Сверка с WB cards/error/list ──────────────────────────────

    def test_wb_processing_errors_flag_completed_items(self):
        from models import AutoPublishItem
        product = self._make_product(external_id='WB-REJECTED')
        run = self._run()
        item = AutoPublishItem(
            run_id=run.id, imported_product_id=product.id,
            seller_id=self.seller.id, status='completed',
            step='completed', wb_nm_id=555001,
        )
        ok_item = AutoPublishItem(
            run_id=run.id, imported_product_id=product.id,
            seller_id=self.seller.id, status='completed',
            step='completed', wb_nm_id=555002,
        )
        self.db.session.add_all([item, ok_item])
        self.db.session.commit()

        mock_client = MagicMock()
        mock_client.get_cards_error_list.return_value = [
            {'nmID': 555001, 'vendorCode': 'VC-1',
             'errors': ['Баркод уже используется']},
        ]
        with patch('services.wb_api_client.WildberriesAPIClient',
                   return_value=mock_client):
            flagged = self._service()._check_wb_processing_errors(run)

        self.assertEqual(flagged, 1)
        self.db.session.refresh(item)
        self.db.session.refresh(ok_item)
        self.assertEqual(item.step, 'wb_processing_error')
        self.assertIn('Баркод уже используется', item.error_message)
        self.assertEqual(ok_item.step, 'completed')

    def test_wb_processing_errors_real_wb_format_matched_by_vendor_code(self):
        """Реальный формат error/list: errors — dict {vendorCode: [msgs]},
        nmID отсутствует — матчинг идёт по vendor_code связанного Product."""
        from models import AutoPublishItem, Product
        product = self._make_product(external_id='WB-REAL-FMT')
        wb_product = Product(
            seller_id=self.seller.id, nm_id=555010,
            vendor_code='id-999-1366', title='Т', is_active=True,
        )
        self.db.session.add(wb_product)
        self.db.session.flush()
        run = self._run()
        item = AutoPublishItem(
            run_id=run.id, imported_product_id=product.id,
            seller_id=self.seller.id, status='completed',
            step='completed', wb_nm_id=None, product_id=wb_product.id,
        )
        self.db.session.add(item)
        self.db.session.commit()

        mock_client = MagicMock()
        mock_client.get_cards_error_list.return_value = [{
            'batchUUID': 'x',
            'vendorCodes': ['id-999-1366'],
            'errors': {'id-999-1366': ['Недопустимое значение цвета "мягкий"']},
            'updatedAt': '2026-07-12T21:58:05Z',
        }]
        with patch('services.wb_api_client.WildberriesAPIClient',
                   return_value=mock_client):
            flagged = self._service()._check_wb_processing_errors(run)

        self.assertEqual(flagged, 1)
        self.db.session.refresh(item)
        self.assertEqual(item.step, 'wb_processing_error')
        self.assertIn('Недопустимое значение цвета', item.error_message)

    def test_wb_processing_errors_noop_without_errors(self):
        from models import AutoPublishItem
        product = self._make_product(external_id='WB-OK')
        run = self._run()
        item = AutoPublishItem(
            run_id=run.id, imported_product_id=product.id,
            seller_id=self.seller.id, status='completed',
            step='completed', wb_nm_id=555003,
        )
        self.db.session.add(item)
        self.db.session.commit()

        mock_client = MagicMock()
        mock_client.get_cards_error_list.return_value = []
        with patch('services.wb_api_client.WildberriesAPIClient',
                   return_value=mock_client):
            flagged = self._service()._check_wb_processing_errors(run)

        self.assertEqual(flagged, 0)
        self.db.session.refresh(item)
        self.assertEqual(item.step, 'completed')


if __name__ == '__main__':
    unittest.main()
