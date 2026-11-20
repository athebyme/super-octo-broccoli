# -*- coding: utf-8 -*-
"""Exact bounded bulk selection and seller/supplier scope for catalog import."""

from datetime import datetime
from html.parser import HTMLParser
import json
import os
import re
from types import SimpleNamespace
import unittest
from unittest.mock import patch


class _SupplierPageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.import_rows = []
        self.update_rows = []
        self.root_state = None
        self.catalog_cards = 0
        self.text = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == 'input' and attributes.get('form') == 'supplier-import-form':
            if attributes.get('name') == 'product_ids':
                self.import_rows.append({
                    'id': attributes.get('value'),
                    'checked': 'checked' in attributes,
                })
        if tag == 'input' and attributes.get('form') == 'supplier-update-form':
            if attributes.get('name') == 'update_product_ids':
                self.update_rows.append({
                    'id': attributes.get('value'),
                    'checked': 'checked' in attributes,
                })
        if tag == 'div' and 'flex-col' in attributes.get('class', '').split():
            if attributes.get('class', '').split()[0] == 'p-4':
                self.catalog_cards += 1
        if tag == 'div' and 'data-supplier-products' in attributes:
            self.root_state = attributes.get('x-data')

    def handle_data(self, data):
        self.text.append(data)


class SupplierCatalogBulkImportSelectionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ['DISABLE_SECURE_COOKIE'] = '1'
        os.environ.setdefault('SKIP_SCHEDULER', '1')
        import sqlalchemy as _sa
        from sqlalchemy.pool import StaticPool
        import seller_platform  # noqa: E402
        from models import db

        cls.app = seller_platform.app
        cls._config_before = {
            key: cls.app.config.get(key)
            for key in (
                'SQLALCHEMY_DATABASE_URI', 'SQLALCHEMY_TRACK_MODIFICATIONS',
                'SQLALCHEMY_ENGINE_OPTIONS', 'WTF_CSRF_ENABLED', 'TESTING',
            )
        }
        cls._app_engine_before = db._app_engines.get(cls.app)
        cls.app.config.update(
            SQLALCHEMY_DATABASE_URI='sqlite:///:memory:',
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            SQLALCHEMY_ENGINE_OPTIONS={},
            WTF_CSRF_ENABLED=False,
            TESTING=True,
        )
        cls._engine = _sa.create_engine(
            'sqlite:///:memory:',
            connect_args={'check_same_thread': False},
            poolclass=StaticPool,
        )
        db._app_engines[cls.app] = {None: cls._engine}
        cls.db = db

    @classmethod
    def tearDownClass(cls):
        with cls.app.app_context():
            cls.db.session.remove()
        cls._engine.dispose()
        if cls._app_engine_before is None:
            cls.db._app_engines.pop(cls.app, None)
        else:
            cls.db._app_engines[cls.app] = cls._app_engine_before
        cls.app.config.update(cls._config_before)

    def setUp(self):
        from models import Seller, SellerSupplier, Supplier, User

        self.ctx = self.app.app_context()
        self.ctx.push()
        self.db.create_all()
        user = User(
            username='bulk-import-owner',
            email='bulk-import-owner@test.local',
            is_active=True,
        )
        user.set_password('synthetic-password')
        seller = Seller(user=user, company_name='Bulk import owner')
        supplier = Supplier(
            name='Exact source', code='exact-bulk-source', is_active=True,
        )
        other_supplier = Supplier(
            name='Other source', code='other-bulk-source', is_active=True,
        )
        self.db.session.add_all([seller, supplier, other_supplier])
        self.db.session.flush()
        self.db.session.add_all([
            SellerSupplier(
                seller_id=seller.id, supplier_id=supplier.id, is_active=True,
            ),
            # Both sources are valid for this seller. Import still must honor
            # the exact supplier declared by the current form.
            SellerSupplier(
                seller_id=seller.id,
                supplier_id=other_supplier.id,
                is_active=True,
            ),
        ])
        self.db.session.commit()

        self.seller_id = seller.id
        self.supplier_id = supplier.id
        self.other_supplier_id = other_supplier.id
        self.user = user
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session['_user_id'] = str(user.id)
            session['_fresh'] = True

    def tearDown(self):
        self.db.session.remove()
        self.db.drop_all()
        self.ctx.pop()

    def _product(self, *, supplier_id=None, title, quantity=5, parsed=True,
                 product_id=None):
        from models import SupplierProduct

        product = SupplierProduct(
            supplier_id=supplier_id or self.supplier_id,
            id=product_id,
            external_id=f'ext-{title}',
            title=title,
            brand='Bulk brand',
            status='ready',
            supplier_quantity=quantity,
            ai_parsed_at=datetime(2026, 1, 1) if parsed else None,
        )
        self.db.session.add(product)
        return product

    def _link_import(self, supplier_product, *, on_wb=False, stale=False):
        from models import ImportedProduct, Product

        product_id = None
        if on_wb:
            wb_product = Product(
                seller_id=self.seller_id,
                nm_id=999000 + supplier_product.id,
                vendor_code=f'BULK-{supplier_product.id}',
                title=supplier_product.title,
                is_active=True,
            )
            self.db.session.add(wb_product)
            self.db.session.flush()
            product_id = wb_product.id
        self.db.session.add(ImportedProduct(
            seller_id=self.seller_id,
            supplier_id=self.supplier_id,
            supplier_product_id=supplier_product.id,
            product_id=product_id,
            title=supplier_product.title,
            supplier_content_revision=(
                max(1, int(supplier_product.content_revision or 1) - 1)
                if stale else int(supplier_product.content_revision or 1)
            ),
        ))

    @staticmethod
    def _parse_supplier_page(html):
        parser = _SupplierPageParser()
        parser.feed(html)
        return parser

    @staticmethod
    def _ok_import_result():
        return SimpleNamespace(
            success=True,
            imported=1,
            skipped=0,
            errors=0,
            marketplace_drafts_created=0,
            marketplace_draft_errors=0,
        )

    def test_filter_review_renders_complete_exact_selection_and_is_read_only(self):
        from models import ImportedProduct

        free = [
            self._product(
                title=f'BulkChoice free {index:03d}',
                product_id=(9_007_199_254_741_029 if index == 0 else None),
            )
            for index in range(55)
        ]
        imported = self._product(title='BulkChoice already imported')
        stale_import = self._product(title='BulkChoice stale update')
        stale_import.content_revision = 2
        wb_linked = self._product(title='BulkChoice linked on WB')
        non_ai = self._product(title='BulkChoice not parsed', parsed=False)
        out_of_stock = self._product(title='BulkChoice out of stock', quantity=0)
        other_source = self._product(
            supplier_id=self.other_supplier_id,
            title='BulkChoice from other source',
        )
        self.db.session.flush()
        self._link_import(imported)
        self._link_import(stale_import, stale=True)
        self._link_import(wb_linked, on_wb=True)
        self.db.session.commit()
        free_ids = {str(product.id) for product in free}
        self.assertIn('9007199254741029', free_ids)
        before_import_count = ImportedProduct.query.count()

        filters = (
            'search=BulkChoice&stock_status=in_stock&ai_filter=ai_parsed'
            '&wb_filter=not_on_wb&show_imported=1'
        )
        path = f'/supplier-catalog/{self.supplier_id}/products?{filters}'
        normal_response = self.client.get(path)
        self.assertEqual(normal_response.status_code, 200)
        normal_html = normal_response.get_data(as_text=True)
        normal_page = self._parse_supplier_page(normal_html)
        self.assertEqual(normal_page.catalog_cards, 50)
        self.assertEqual(len(normal_page.import_rows), 49)
        self.assertEqual(sum(row['checked'] for row in normal_page.import_rows), 0)
        self.assertIn('select_all=1', normal_html)
        self.assertIn('ai_filter=ai_parsed', normal_html)

        review_response = self.client.get(path + '&select_all=1')
        self.assertEqual(review_response.status_code, 200)
        review_html = review_response.get_data(as_text=True)
        review_page = self._parse_supplier_page(review_html)
        checked_ids = {
            row['id'] for row in review_page.import_rows if row['checked']
        }
        self.assertEqual(len(review_page.import_rows), 55)
        self.assertEqual(review_page.catalog_cards, 57)
        self.assertEqual(
            {row['id'] for row in review_page.update_rows},
            {str(stale_import.id)},
        )
        self.assertEqual(sum(row['checked'] for row in review_page.update_rows), 0)
        self.assertEqual(checked_ids, free_ids)
        self.assertEqual(sum(row['checked'] for row in review_page.import_rows), 55)
        rendered_titles = {
            f'BulkChoice free {index:03d}' for index in range(55)
        } | {'BulkChoice already imported', 'BulkChoice stale update'}
        self.assertTrue(all(title in review_html for title in rendered_titles))
        self.assertIn('Выбрано 55 из 57 по текущему фильтру.', review_html)
        self.assertIn('(55)', review_html)
        self.assertIn('55 выбрано', review_html)
        self.assertNotIn('ID каталога:', review_html)
        self.assertIn('BulkChoice already imported', review_html)
        self.assertIn('Импортирован', review_html)
        self.assertIn('Есть обновление', review_html)
        self.assertIn('name="supplier_id" value="' + str(self.supplier_id) + '"', review_html)
        self.assertNotIn('BulkChoice linked on WB', review_html)
        self.assertNotIn('BulkChoice not parsed', review_html)
        self.assertNotIn('BulkChoice out of stock', review_html)
        self.assertNotIn('BulkChoice from other source', review_html)
        self.assertIn('Отменить массовый выбор', review_html)

        state = review_page.root_state or ''
        match = re.search(r'importSelected:\s*(\[.*?\]),\s*updateSelected:', state)
        self.assertIsNotNone(match)
        self.assertEqual(set(json.loads(match.group(1))), free_ids)
        self.assertEqual(ImportedProduct.query.count(), before_import_count)

    def test_over_limit_filter_falls_back_to_unselected_fifty_rows(self):
        from models import ImportedProduct

        self.db.session.add_all([
            self._product(title=f'Cap target {index:03d}', quantity=3)
            for index in range(205)
        ])
        self.db.session.commit()
        before_import_count = ImportedProduct.query.count()

        response = self.client.get(
            f'/supplier-catalog/{self.supplier_id}/products'
            '?search=Cap%20target&stock_status=all&select_all=1'
        )
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        parsed = self._parse_supplier_page(html)
        self.assertEqual(len(parsed.import_rows), 50)
        self.assertEqual(sum(row['checked'] for row in parsed.import_rows), 0)
        self.assertIn('В текущем фильтре 205 товаров', html)
        self.assertIn('Ни один товар не был отмечен', html)
        self.assertNotIn('importSelected: ["', parsed.root_state or '')
        self.assertEqual(ImportedProduct.query.count(), before_import_count)

    def test_declared_supplier_import_rejects_mixed_invalid_and_over_cap_sets(self):
        from models import SupplierProduct

        own = self._product(
            title='Own declared source',
            product_id=9_007_199_254_741_029,
        )
        foreign = self._product(
            supplier_id=self.other_supplier_id,
            title='Other connected source',
        )
        self.db.session.commit()
        importer_target = 'routes.suppliers.SupplierService.import_to_seller'

        with patch(importer_target, return_value=self._ok_import_result()) as importer:
            valid = self.client.post('/supplier-catalog/import', data={
                'supplier_id': str(self.supplier_id),
                'product_ids': [str(own.id)],
            })
        self.assertEqual(valid.status_code, 302)
        importer.assert_called_once_with(
            self.seller_id, [own.id], draft_account_ids=None,
        )

        invalid_forms = [
            {
                'supplier_id': str(self.supplier_id),
                'product_ids': [str(own.id), str(foreign.id)],
            },
            {
                'supplier_id': str(self.supplier_id),
                'product_ids': [str(own.id), str(own.id + 1_000_000)],
            },
            {
                'supplier_id': str(self.supplier_id),
                'product_ids': [str(own.id), 'not-an-id'],
            },
            {
                'supplier_id': str(self.supplier_id),
                'product_ids': [str(own.id), str(own.id)],
            },
            {
                'supplier_id': str(self.supplier_id),
                'product_ids': [str(index) for index in range(1, 202)],
            },
            {
                'supplier_id': str(1 << 63),
                'product_ids': [str(own.id)],
            },
            {
                'supplier_id': str(self.supplier_id),
                'product_ids': [str(1 << 63)],
            },
        ]
        for data in invalid_forms:
            with self.subTest(data=data), patch(importer_target) as importer:
                response = self.client.post('/supplier-catalog/import', data=data)
            self.assertEqual(response.status_code, 302)
            importer.assert_not_called()

        from models import SellerSupplier
        connection = SellerSupplier.query.filter_by(
            seller_id=self.seller_id,
            supplier_id=self.other_supplier_id,
        ).one()
        connection.is_active = False
        self.db.session.commit()
        with patch(importer_target) as importer:
            response = self.client.post('/supplier-catalog/import', data={
                'supplier_id': str(self.other_supplier_id),
                'product_ids': [str(foreign.id)],
            })
        self.assertEqual(response.status_code, 302)
        importer.assert_not_called()

        self.assertEqual(SupplierProduct.query.count(), 2)

    def test_legacy_missing_supplier_and_smart_import_keep_their_boundaries(self):
        own = self._product(title='Legacy accepted source')
        foreign = self._product(
            supplier_id=self.other_supplier_id,
            title='Smart rejected other source',
        )
        self.db.session.commit()
        importer_target = 'routes.suppliers.SupplierService.import_to_seller'

        # A legacy normal POST without supplier_id retains its old digit
        # filtering contract; service-level callers are unchanged.
        with patch(importer_target, return_value=self._ok_import_result()) as importer:
            response = self.client.post('/supplier-catalog/import', data={
                'product_ids': [str(own.id), 'legacy-invalid-token'],
            })
        self.assertEqual(response.status_code, 302)
        importer.assert_called_once_with(
            self.seller_id, [own.id], draft_account_ids=None,
        )

        # The smart path rejects a mixed declared-supplier set before parsing
        # or any enrichment work starts.
        with patch('services.smart_product_parser.SmartProductParser') as parser:
            response = self.client.post('/supplier-catalog/smart-import', data={
                'supplier_id': str(self.supplier_id),
                'product_ids': [str(own.id), str(foreign.id)],
            })
        self.assertEqual(response.status_code, 302)
        parser.assert_not_called()


if __name__ == '__main__':
    unittest.main()
