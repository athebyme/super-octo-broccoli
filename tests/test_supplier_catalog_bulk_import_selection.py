# -*- coding: utf-8 -*-
"""Exact bounded bulk selection and seller/supplier scope for catalog import."""

from datetime import datetime
from html.parser import HTMLParser
import json
import os
import re
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
import unittest
from unittest.mock import patch


class _SupplierPageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.import_rows = []
        self.update_rows = []
        self.root_state = None
        self.update_select_href = None
        self.updates_mode_href = None
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
        if tag == 'a' and 'data-supplier-filter-update-select' in attributes:
            self.update_select_href = attributes.get('href')
        if tag == 'a' and 'intent=updates' in attributes.get('href', ''):
            self.updates_mode_href = attributes.get('href')

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
            imported_product_ids=[],
            skipped=0,
            errors=0,
            marketplace_drafts_created=0,
            marketplace_draft_errors=0,
        )

    def test_import_entry_skips_the_list_only_for_one_connected_supplier(self):
        from models import SellerSupplier

        path = '/supplier-catalog'
        self.assertEqual(self.client.get(path + '?action=import').status_code, 200)
        other_connection = SellerSupplier.query.filter_by(
            seller_id=self.seller_id, supplier_id=self.other_supplier_id,
        ).one()
        other_connection.is_active = False
        self.db.session.commit()

        self.assertEqual(self.client.get(path).status_code, 200)
        landing = self.client.get(path + '?action=import')
        self.assertEqual(landing.status_code, 302)
        self.assertEqual(urlsplit(landing.location).path,
                         f'/supplier-catalog/{self.supplier_id}/products')
        self.assertEqual(self.client.get(landing.location).status_code, 200)

        sole_connection = SellerSupplier.query.filter_by(
            seller_id=self.seller_id, supplier_id=self.supplier_id,
        ).one()
        sole_connection.is_active = False
        self.db.session.commit()
        self.assertEqual(self.client.get(path + '?action=import').status_code, 200)

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
        stale_import_out_of_stock = self._product(
            title='BulkChoice stale update out of stock', quantity=0,
        )
        stale_import_out_of_stock.content_revision = 2
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
        self._link_import(stale_import_out_of_stock, stale=True)
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
        self.assertEqual(normal_page.update_rows, [])
        self.assertIsNone(normal_page.update_select_href)
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
        self.assertEqual(review_page.update_rows, [])
        self.assertEqual(checked_ids, free_ids)
        self.assertEqual(sum(row['checked'] for row in review_page.import_rows), 55)
        rendered_titles = {
            f'BulkChoice free {index:03d}' for index in range(55)
        } | {'BulkChoice already imported', 'BulkChoice stale update'}
        self.assertTrue(all(title in review_html for title in rendered_titles))
        self.assertIn('Выбрано 55 из 57', review_html)
        self.assertIn('(55)', review_html)
        self.assertIn('id="supplier-selection-status"', review_html)
        self.assertEqual(review_html.count('id="supplier-selection-count"'), 1)
        self.assertIn('aria-describedby="supplier-selection-count"', review_html)
        self.assertNotIn('id="import-selection-count"', review_html)
        self.assertNotIn('ID каталога:', review_html)
        self.assertIn('BulkChoice already imported', review_html)
        self.assertIn('В «Моих товарах»', review_html)
        self.assertIn('Есть изменения', review_html)
        self.assertIn('name="supplier_id" value="' + str(self.supplier_id) + '"', review_html)
        self.assertNotIn('BulkChoice linked on WB', review_html)
        self.assertNotIn('BulkChoice not parsed', review_html)
        self.assertNotIn('BulkChoice out of stock', review_html)
        self.assertNotIn('BulkChoice from other source', review_html)
        self.assertIn('Снять выбор', review_html)

        state = review_page.root_state or ''
        match = re.search(r'importSelected:\s*(\[.*?\]),\s*updateSelected:', state)
        self.assertIsNotNone(match)
        self.assertEqual(set(json.loads(match.group(1))), free_ids)
        self.assertEqual(ImportedProduct.query.count(), before_import_count)

        # The other intent exposes only stale linked rows. A deep link without
        # an explicit stock filter defaults to all stock states so an out of
        # stock row whose supplier data changed remains available for refresh.
        default_stock_path = (
            f'/supplier-catalog/{self.supplier_id}/products?'
            'search=BulkChoice&ai_filter=ai_parsed&wb_filter=not_on_wb'
            '&show_imported=1'
        )
        default_stock_page = self._parse_supplier_page(
            self.client.get(default_stock_path).get_data(as_text=True),
        )
        self.assertIsNone(default_stock_page.update_select_href)
        self.assertIsNotNone(default_stock_page.updates_mode_href)
        update_intent_path = default_stock_page.updates_mode_href
        update_intent_query = parse_qs(urlsplit(update_intent_path).query)
        self.assertEqual(update_intent_query['search'], ['BulkChoice'])
        self.assertEqual(update_intent_query['stock_status'], ['all'])
        self.assertEqual(update_intent_query['intent'], ['updates'])
        self.assertEqual(update_intent_query['updates_only'], ['1'])
        update_intent_page = self._parse_supplier_page(
            self.client.get(update_intent_path).get_data(as_text=True),
        )
        self.assertEqual(
            {row['id'] for row in update_intent_page.update_rows},
            {str(stale_import.id), str(stale_import_out_of_stock.id)},
        )
        self.assertEqual(update_intent_page.import_rows, [])

    def test_filtered_update_selection_is_exact_bounded_and_separate_from_import(self):
        from models import ImportedProduct

        source_rows = []
        for index in range(55):
            source = self._product(title=f'FilteredUpdate source {index:03d}')
            source.content_revision = 2
            self.db.session.flush()
            if index == 0:
                # The source is eligible because at least one exact seller copy
                # is stale, even though another linked copy is already current.
                self._link_import(source, stale=False)
                self._link_import(source, stale=True)
            else:
                self._link_import(source, stale=True)
            source_rows.append(source)

        mismatched = self._product(title='FilteredUpdate mismatched supplier copy')
        mismatched.content_revision = 2
        self.db.session.flush()
        self.db.session.add(ImportedProduct(
            seller_id=self.seller_id,
            supplier_id=self.other_supplier_id,
            supplier_product_id=mismatched.id,
            supplier_content_revision=1,
            title=mismatched.title,
        ))
        unimported = self._product(title='FilteredUpdate import only')
        foreign_source = self._product(
            supplier_id=self.other_supplier_id,
            title='FilteredUpdate foreign source',
        )
        self.db.session.flush()
        self.db.session.add(ImportedProduct(
            seller_id=self.seller_id,
            supplier_id=self.other_supplier_id,
            supplier_product_id=foreign_source.id,
            supplier_content_revision=1,
            title=foreign_source.title,
        ))
        self.db.session.commit()
        before_count = ImportedProduct.query.count()

        filters = (
            'search=FilteredUpdate&show_imported=1&stock_status=all'
            '&wb_filter=not_on_wb&ai_filter=ai_parsed'
            '&intent=updates&updates_only=1'
        )
        normal_path = f'/supplier-catalog/{self.supplier_id}/products?{filters}'
        normal_response = self.client.get(normal_path)
        self.assertEqual(normal_response.status_code, 200)
        normal_page = self._parse_supplier_page(normal_response.get_data(as_text=True))
        self.assertIsNotNone(normal_page.update_select_href)
        link_query = parse_qs(urlsplit(normal_page.update_select_href).query)
        self.assertEqual(link_query, {
            'page': ['1'], 'search': ['FilteredUpdate'],
            'show_imported': ['1'],
            'stock_status': ['all'], 'wb_filter': ['not_on_wb'],
            'ai_filter': ['ai_parsed'], 'updates_only': ['1'],
            'select_all': ['updates'],
        })

        review_path = f'{normal_path}&select_all=updates'
        review_response = self.client.get(review_path)
        self.assertEqual(review_response.status_code, 200)
        review_html = review_response.get_data(as_text=True)
        review_page = self._parse_supplier_page(review_html)
        self.assertEqual(review_page.catalog_cards, 55)
        self.assertEqual(review_page.update_select_href, None)
        state = review_page.root_state or ''
        selected_update_match = re.search(
            r'updateSelected:\s*(\[[^\]]*\])', state,
        )
        selected_import_match = re.search(
            r'importSelected:\s*(\[[^\]]*\])', state,
        )
        self.assertIsNotNone(selected_update_match)
        self.assertIsNotNone(selected_import_match)
        selected_update_ids = {int(value) for value in json.loads(selected_update_match.group(1))}
        self.assertEqual(
            selected_update_ids,
            {source.id for source in source_rows},
        )
        self.assertEqual(
            {row['id'] for row in review_page.update_rows},
            {str(source.id) for source in source_rows},
        )
        self.assertEqual(json.loads(selected_import_match.group(1)), [])
        self.assertIn(
            'Выбрано 55 из 55',
            review_html,
        )
        self.assertIn('id="supplier-selection-status"', review_html)
        self.assertEqual(review_html.count('id="supplier-selection-count"'), 1)
        self.assertIn('aria-describedby="supplier-selection-count"', review_html)
        self.assertNotIn('id="update-selection-count"', review_html)
        self.assertIn('update_product_ids', review_html)
        self.assertNotIn('update_all_for_supplier', review_html)
        self.assertEqual(ImportedProduct.query.count(), before_count)

    def test_filtered_update_selection_rejects_over_cap_and_incomplete_reads(self):
        from models import ImportedProduct
        from services.supplier_service import SupplierService

        rows = []
        for index in range(201):
            source = self._product(title=f'UpdateLimit source {index:03d}')
            source.content_revision = 2
            self.db.session.flush()
            self._link_import(source, stale=True)
            rows.append(source)
        self.db.session.commit()
        before_count = ImportedProduct.query.count()

        path = (
            f'/supplier-catalog/{self.supplier_id}/products?'
            'search=UpdateLimit&show_imported=1&stock_status=all'
            '&select_all=updates'
        )
        over_limit = self.client.get(path)
        self.assertEqual(over_limit.status_code, 200)
        over_page = self._parse_supplier_page(over_limit.get_data(as_text=True))
        self.assertEqual(over_page.catalog_cards, 50)
        state = over_page.root_state or ''
        selected_match = re.search(r'updateSelected:\s*(\[[^\]]*\])', state)
        self.assertIsNotNone(selected_match)
        self.assertEqual(json.loads(selected_match.group(1)), [])
        over_limit_html = over_limit.get_data(as_text=True)
        self.assertIn('В текущем фильтре 201 товаров каталога.', over_limit_html)
        self.assertIn('Ни одна позиция не была отмечена.', over_limit_html)
        self.assertIsNone(over_page.update_select_href)

        original_get = SupplierService.get_available_products_for_seller

        class _IncompletePagination:
            def __init__(self, wrapped):
                self._wrapped = wrapped
                self.total = wrapped.total + 1

            def __getattr__(self, name):
                return getattr(self._wrapped, name)

        def incomplete_once(*args, **kwargs):
            result = original_get(*args, **kwargs)
            if kwargs.get('per_page') == 201:
                return _IncompletePagination(result)
            return result

        with patch(
            'routes.suppliers.SupplierService.get_available_products_for_seller',
            side_effect=incomplete_once,
        ):
            incomplete = self.client.get(path.replace('UpdateLimit', 'UpdateLimit source 000'))
        self.assertEqual(incomplete.status_code, 200)
        incomplete_html = incomplete.get_data(as_text=True)
        incomplete_page = self._parse_supplier_page(incomplete_html)
        self.assertEqual(incomplete_page.catalog_cards, 1)
        self.assertIn('Список изменился во время подготовки массового выбора.', incomplete_html)
        incomplete_state = incomplete_page.root_state or ''
        incomplete_match = re.search(r'updateSelected:\s*(\[[^\]]*\])', incomplete_state)
        self.assertIsNotNone(incomplete_match)
        self.assertEqual(json.loads(incomplete_match.group(1)), [])
        self.assertEqual(ImportedProduct.query.count(), before_count)

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

    def _imported_row(self, supplier_product, *, external_id):
        from models import ImportedProduct

        row = ImportedProduct(
            seller_id=self.seller_id,
            supplier_id=supplier_product.supplier_id,
            supplier_product_id=supplier_product.id,
            external_id=external_id,
            source_type='synthetic',
            title=supplier_product.title,
            import_status='validated',
        )
        self.db.session.add(row)
        self.db.session.flush()
        return row

    def test_modern_import_redirects_303_with_exact_committed_new_ids(self):
        from itsdangerous import URLSafeTimedSerializer
        from models import ImportedProduct
        from routes.suppliers import SUPPLIER_IMPORT_BATCH_TOKEN_SALT

        source_a = self._product(title='Batch source A', product_id=101)
        source_b = self._product(title='Batch source B', product_id=102)
        self.db.session.add(ImportedProduct(
            seller_id=self.seller_id,
            external_id='batch-id-padding',
            source_type='synthetic',
            title='Existing row to separate ID spaces',
            import_status='validated',
        ))
        self.db.session.add_all([source_a, source_b])
        self.db.session.commit()
        expected_ids = []

        def import_result(seller_id, supplier_product_ids, **kwargs):
            # Commit exactly the rows represented in the returned import result.
            rows_by_source = {
                source_a.id: self._imported_row(
                    source_a, external_id='new-import-a',
                ),
                source_b.id: self._imported_row(
                    source_b, external_id='new-import-b',
                ),
            }
            expected_ids.extend([
                rows_by_source[source_b.id].id,
                rows_by_source[source_a.id].id,
            ])
            self.db.session.commit()
            return SimpleNamespace(
                success=True,
                imported=2,
                skipped=0,
                errors=0,
                imported_product_ids=list(expected_ids),
                marketplace_drafts_created=1,
                marketplace_draft_errors=0,
                error_messages=[],
            )

        with patch(
            'routes.suppliers.SupplierService.import_to_seller',
            side_effect=import_result,
        ) as importer:
            response = self.client.post('/supplier-catalog/import', data={
                'supplier_id': str(self.supplier_id),
                'product_ids': [str(source_a.id), str(source_b.id)],
            })

        self.assertEqual(response.status_code, 303)
        self.assertTrue(response.location.startswith('/my-products/beta?import_batch='))
        token = parse_qs(urlsplit(response.location).query)['import_batch'][0]
        payload = URLSafeTimedSerializer(
            self.app.secret_key,
            salt=SUPPLIER_IMPORT_BATCH_TOKEN_SALT,
        ).loads(token, max_age=3600)
        self.assertEqual(set(payload), {'v', 'user_id', 'seller_id', 'ids'})
        self.assertEqual(payload['v'], 1)
        self.assertEqual(payload['user_id'], self.user.id)
        self.assertEqual(payload['seller_id'], self.seller_id)
        self.assertEqual(payload['ids'], expected_ids)
        self.assertTrue(all(
            imported_id not in {source_a.id, source_b.id}
            for imported_id in payload['ids']
        ))
        importer.assert_called_once_with(
            self.seller_id,
            [source_a.id, source_b.id],
            draft_account_ids=None,
        )
        with self.client.session_transaction() as session:
            message = ' '.join(value for _, value in session.get('_flashes', []))
        self.assertIn('Ozon-черновиков: 1', message)
        self.assertNotIn('на WB', message)

    def test_modern_zero_import_returns_to_selected_supplier_with_truthful_flash(self):
        source = self._product(title='Duplicate source')
        self.db.session.commit()
        result = SimpleNamespace(
            success=True,
            imported=0,
            skipped=1,
            errors=0,
            imported_product_ids=[],
            marketplace_drafts_created=0,
            marketplace_draft_errors=0,
            error_messages=[],
        )
        with patch(
            'routes.suppliers.SupplierService.import_to_seller',
            return_value=result,
        ):
            response = self.client.post('/supplier-catalog/import', data={
                'supplier_id': str(self.supplier_id),
                'product_ids': [str(source.id)],
            })
        self.assertEqual(response.status_code, 302)
        self.assertNotIn('/my-products', response.location)
        self.assertIn(str(self.supplier_id), response.location)
        with self.client.session_transaction() as session:
            message = ' '.join(value for _, value in session.get('_flashes', []))
        self.assertIn('Новые карточки не добавлены: импортировано 0', message)
        self.assertIn('пропущено (дубли): 1', message)

    def test_modern_smart_import_redirects_to_exact_new_batch(self):
        from itsdangerous import URLSafeTimedSerializer
        from models import ImportedProduct
        from routes.suppliers import SUPPLIER_IMPORT_BATCH_TOKEN_SALT

        source = self._product(title='Smart new source')
        self.db.session.add(ImportedProduct(
            seller_id=self.seller_id,
            external_id='smart-id-padding',
            source_type='synthetic',
            title='Existing row to separate ID spaces',
            import_status='validated',
        ))
        self.db.session.flush()
        imported = self._imported_row(source, external_id='smart-new-import')
        self.db.session.commit()
        result = {
            'success': True,
            'parse_result': {
                'brand_resolved_count': 1,
                'category_mapped_count': 1,
                'avg_readiness_score': 82.0,
            },
            'import_result': {
                'imported': 1,
                'imported_product_ids': [imported.id],
                'skipped': 0,
                'errors': 0,
                'error_messages': [],
                'marketplace_drafts_created': 0,
                'marketplace_draft_errors': 0,
            },
        }
        with patch('services.smart_product_parser.SmartProductParser') as parser_cls:
            parser = parser_cls.return_value
            parser.smart_import_to_seller.return_value = result
            response = self.client.post('/supplier-catalog/smart-import', data={
                'supplier_id': str(self.supplier_id),
                'product_ids': [str(source.id)],
            })
        self.assertEqual(response.status_code, 303)
        token = parse_qs(urlsplit(response.location).query)['import_batch'][0]
        payload = URLSafeTimedSerializer(
            self.app.secret_key,
            salt=SUPPLIER_IMPORT_BATCH_TOKEN_SALT,
        ).loads(token, max_age=3600)
        self.assertEqual(payload['ids'], [imported.id])
        self.assertNotIn('WB', response.get_data(as_text=True))

    def test_smart_parser_result_carries_imported_product_ids(self):
        from services.smart_product_parser import SmartProductParser

        source = self._product(title='Parser result source')
        self.db.session.commit()
        imported = self._imported_row(source, external_id='parser-result-import')
        self.db.session.commit()
        import_result = SimpleNamespace(
            success=True,
            imported=1,
            imported_product_ids=[imported.id],
            skipped=0,
            errors=0,
            error_messages=[],
            marketplace_drafts_created=0,
            marketplace_drafts_existing=0,
            marketplace_draft_errors=0,
        )
        parser = SmartProductParser(supplier_id=self.supplier_id)
        parser.parse_and_apply_bulk = lambda product_ids: SimpleNamespace(
            to_dict=lambda: {
                'brand_resolved_count': 0,
                'category_mapped_count': 0,
                'avg_readiness_score': 0.0,
            },
        )
        with patch(
            'services.supplier_service.SupplierService.import_to_seller',
            return_value=import_result,
        ):
            result = parser.smart_import_to_seller(self.seller_id, [source.id])
        self.assertEqual(
            result['import_result']['imported_product_ids'], [imported.id],
        )

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
        self.assertEqual(response.location, '/my-products')
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
