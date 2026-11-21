# -*- coding: utf-8 -*-
"""Exact seller/supplier scope and terminal feedback for supplier refresh."""

import json
import os
import random
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine, event, text


class SupplierBulkExactRefreshTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ['DISABLE_SECURE_COOKIE'] = '1'
        os.environ.setdefault('SKIP_SCHEDULER', '1')
        import seller_platform  # noqa: F401
        from models import db

        cls.app = seller_platform.app
        cls.previous_engine_map = db._app_engines.get(cls.app)
        cls.previous_database_uri = cls.app.config.get('SQLALCHEMY_DATABASE_URI')
        cls.tempdir = tempfile.TemporaryDirectory(
            prefix='supplier-bulk-exact-refresh-',
        )
        cls.sqlite_path = os.path.join(cls.tempdir.name, 'isolated.sqlite')
        cls.app.config.update(
            SQLALCHEMY_DATABASE_URI=f'sqlite:///{cls.sqlite_path}',
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            SQLALCHEMY_ENGINE_OPTIONS={},
            WTF_CSRF_ENABLED=False,
            TESTING=True,
        )
        cls.engine = create_engine(
            f'sqlite:///{cls.sqlite_path}',
            connect_args={'check_same_thread': False},
        )
        db._app_engines[cls.app] = {None: cls.engine}
        cls.db = db

    @classmethod
    def tearDownClass(cls):
        with cls.app.app_context():
            cls.db.session.remove()
        cls.engine.dispose()
        if cls.previous_engine_map is None:
            cls.db._app_engines.pop(cls.app, None)
        else:
            cls.db._app_engines[cls.app] = cls.previous_engine_map
        cls.app.config['SQLALCHEMY_DATABASE_URI'] = cls.previous_database_uri
        cls.tempdir.cleanup()

    def setUp(self):
        from models import (
            ImportedProduct, Product, Seller, SellerSupplier, Supplier,
            SupplierProduct, User,
        )

        self.ImportedProduct = ImportedProduct
        self.Product = Product
        self.SellerSupplier = SellerSupplier
        self.SupplierProduct = SupplierProduct
        self.User = User
        self.Seller = Seller
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.db.create_all()
        self.client = self.app.test_client()

        user = User(
            username=f'exact-refresh-{id(self)}',
            email=f'exact-refresh-{id(self)}@test.local',
            is_active=True,
        )
        user.set_password('synthetic-password')
        self.seller = Seller(user=user, company_name='Exact refresh seller')
        self.supplier = Supplier(
            name='Exact refresh supplier', code=f'exact-refresh-{id(self)}',
        )
        self.other_supplier = Supplier(
            name='Other supplier', code=f'exact-refresh-other-{id(self)}',
        )
        self.db.session.add_all([
            self.seller, self.supplier, self.other_supplier,
        ])
        self.db.session.flush()
        self.db.session.add_all([
            SellerSupplier(
                seller_id=self.seller.id, supplier_id=self.supplier.id,
                is_active=True,
            ),
            SellerSupplier(
                seller_id=self.seller.id, supplier_id=self.other_supplier.id,
                is_active=True,
            ),
        ])
        self.db.session.commit()
        self.user = SimpleNamespace(
            id=user.id,
            username=user.username,
            seller=self.seller,
            is_authenticated=True,
            is_active=True,
            is_admin=False,
        )

    def tearDown(self):
        self.db.session.remove()
        self.db.drop_all()
        self.ctx.pop()

    def _source(self, supplier_id, external_id, title, explicit_id=None):
        values = dict(
            supplier_id=supplier_id,
            external_id=external_id,
            title=title,
            content_revision=2,
        )
        if explicit_id is not None:
            values['id'] = explicit_id
        row = self.SupplierProduct(**values)
        self.db.session.add(row)
        self.db.session.flush()
        return row

    def _copy(
        self, source=None, *, title='old title', explicit_id=None,
        supplier_id=None,
    ):
        values = dict(
            seller_id=self.seller.id,
            supplier_id=(
                supplier_id if supplier_id is not None
                else (source.supplier_id if source is not None else self.supplier.id)
            ),
            supplier_product_id=source.id if source is not None else None,
            external_id=source.external_id if source is not None else 'unlinked',
            title=title,
        )
        if explicit_id is not None:
            values['id'] = explicit_id
        row = self.ImportedProduct(**values)
        self.db.session.add(row)
        self.db.session.flush()
        return row

    def _post(self, path, data=None, *, json=None, user=None):
        active_user = user or self.user
        with patch('flask_login.utils._get_user', return_value=active_user), \
                patch('routes.suppliers.current_user', active_user):
            return self.client.post(path, data=data, json=json)

    def _get(self, path, *, headers=None, user=None, query_string=None):
        active_user = user or self.user
        with patch('flask_login.utils._get_user', return_value=active_user), \
                patch('routes.suppliers.current_user', active_user):
            return self.client.get(
                path, headers=headers, query_string=query_string,
            )

    def _create_refresh_receipt(self):
        source = self._source(
            self.supplier.id, 'receipt-good', 'fresh receipt title',
        )
        copied = self._copy(source, title='old receipt title')
        product = self.Product(
            id=987_654_321,
            seller_id=self.seller.id,
            nm_id=123456799,
            vendor_code='receipt-wb',
            title='Receipt WB product',
        )
        self.db.session.add(product)
        self.db.session.flush()
        copied.import_status = 'imported'
        copied.product_id = product.id
        self.db.session.commit()
        response = self._post(
            '/my-products/refresh-from-supplier',
            json={'product_ids': [copied.id]},
        )
        self.assertEqual(response.status_code, 200)
        token = response.get_json().get('resume_receipt')
        self.assertIsInstance(token, str)
        self.assertNotEqual(copied.id, product.id)
        return token, copied, product

    def test_catalog_all_uses_internal_ids_not_colliding_source_ids(self):
        first_source = self._source(
            self.supplier.id, 'first', 'fresh first', explicit_id=500,
        )
        collision_source = self._source(
            self.supplier.id, 'collision', 'fresh collision', explicit_id=17,
        )
        first_copy = self._copy(
            first_source, title='old first', explicit_id=17,
        )
        collision_copy = self._copy(
            collision_source, title='old collision', explicit_id=29,
        )
        self.db.session.commit()

        response = self._post('/supplier-catalog/update', {
            'supplier_id': str(self.supplier.id),
            'update_all_for_supplier': '1',
        })

        self.assertEqual(response.status_code, 302)
        self.db.session.refresh(first_copy)
        self.db.session.refresh(collision_copy)
        self.assertEqual(first_copy.title, 'fresh first')
        self.assertEqual(collision_copy.title, 'fresh collision')

    def test_catalog_selected_ids_are_confined_to_current_supplier(self):
        other_source = self._source(
            self.other_supplier.id, 'other', 'new other title', explicit_id=701,
        )
        other_copy = self._copy(other_source, title='keep this title')
        self.db.session.commit()

        response = self._post('/supplier-catalog/update', {
            'supplier_id': str(self.supplier.id),
            'update_product_ids': str(other_source.id),
        })

        self.assertEqual(response.status_code, 302)
        self.db.session.refresh(other_copy)
        self.assertEqual(other_copy.title, 'keep this title')

    def test_catalog_rejects_ambiguous_mixed_form_scopes(self):
        source = self._source(self.supplier.id, 'ambiguous', 'fresh title')
        copied = self._copy(source, title='old title')
        self.db.session.commit()

        mixed_lists = self._post('/supplier-catalog/update', {
            'supplier_id': str(self.supplier.id),
            'update_product_ids': str(source.id),
            'product_ids': str(source.id),
        })
        mixed_all = self._post('/supplier-catalog/update', {
            'supplier_id': str(self.supplier.id),
            'update_product_ids': str(source.id),
            'update_all_for_supplier': '1',
        })

        self.assertEqual(mixed_lists.status_code, 302)
        self.assertEqual(mixed_all.status_code, 302)
        self.db.session.refresh(copied)
        self.assertEqual(copied.title, 'old title')

    def test_catalog_legacy_product_ids_field_still_updates_exact_owned_row(self):
        source = self._source(self.supplier.id, 'legacy-field', 'fresh legacy')
        copied = self._copy(source, title='old legacy')
        self.db.session.commit()

        response = self._post('/supplier-catalog/update', {
            'supplier_id': str(self.supplier.id),
            'product_ids': str(source.id),
        })

        self.assertEqual(response.status_code, 302)
        self.db.session.refresh(copied)
        self.assertEqual(copied.title, 'fresh legacy')

    def test_my_products_refresh_preserves_exact_internal_selection(self):
        source = self._source(self.supplier.id, 'duplicate-source', 'fresh title')
        selected = self._copy(source, title='old selected', explicit_id=1201)
        other_copy = self._copy(source, title='old unselected', explicit_id=1202)
        self.db.session.commit()

        response = self._post(
            '/my-products/refresh-from-supplier',
            json={'product_ids': [selected.id]},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload['success'], True)
        self.assertEqual(payload['status'], 'completed')
        self.assertEqual(
            (payload['requested'], payload['updated'], payload['skipped'], payload['errors']),
            (1, 1, 0, 0),
        )
        self.assertEqual(payload['items'], [{
            'product_id': selected.id,
            'status': 'updated',
            'reason': None,
        }])
        self.assertEqual(payload['wb_targets'], [])
        self.assertEqual(payload['wb_ready'], 0)
        self.db.session.refresh(selected)
        self.db.session.refresh(other_copy)
        self.assertEqual(selected.title, 'fresh title')
        self.assertEqual(other_copy.title, 'old unselected')

    def test_json_refresh_returns_ordered_verified_wb_target_subset(self):
        first_source = self._source(
            self.supplier.id, 'wb-first', 'fresh first',
        )
        first = self._copy(first_source, title='old first')
        no_wb_source = self._source(
            self.supplier.id, 'not-published', 'fresh internal only',
        )
        internal_only = self._copy(no_wb_source, title='old internal only')
        pending_source = self._source(
            self.supplier.id, 'pending-wb-link', 'fresh pending',
        )
        pending = self._copy(pending_source, title='old pending')
        no_nm_source = self._source(
            self.supplier.id, 'missing-nm-id', 'fresh no nm id',
        )
        no_nm = self._copy(no_nm_source, title='old no nm id')
        second_source = self._source(
            self.supplier.id, 'wb-second', 'fresh second',
        )
        second = self._copy(second_source, title='old second')

        first_wb = self.Product(
            seller_id=self.seller.id,
            nm_id=123456701,
            vendor_code='first-wb',
            title='First WB product',
        )
        second_wb = self.Product(
            seller_id=self.seller.id,
            nm_id=123456702,
            vendor_code='second-wb',
            title='Second WB product',
        )
        pending_wb = self.Product(
            seller_id=self.seller.id,
            nm_id=123456703,
            vendor_code='pending-wb',
            title='Pending WB product',
        )
        no_nm_wb = self.Product(
            seller_id=self.seller.id,
            nm_id=0,
            vendor_code='no-nm-wb',
            title='Product without WB nm id',
        )
        self.db.session.add_all([first_wb, second_wb, pending_wb, no_nm_wb])
        self.db.session.flush()
        first.import_status = 'imported'
        first.product_id = first_wb.id
        pending.import_status = 'validated'
        pending.product_id = pending_wb.id
        no_nm.import_status = 'imported'
        no_nm.product_id = no_nm_wb.id
        second.import_status = 'imported'
        second.product_id = second_wb.id
        self.db.session.commit()

        response = self._post(
            '/my-products/refresh-from-supplier',
            json={
                'product_ids': [
                    first.id, internal_only.id, pending.id, no_nm.id, second.id,
                ],
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload['status'], 'completed')
        self.assertEqual((payload['requested'], payload['updated']), (5, 5))
        self.assertEqual(payload['wb_targets'], [
            {
                'imported_product_id': first.id,
                'product_id': first_wb.id,
            },
            {
                'imported_product_id': second.id,
                'product_id': second_wb.id,
            },
        ])
        self.assertEqual(payload['wb_ready'], 2)
        with self.client.session_transaction() as client_session:
            self.assertEqual(
                client_session['supplier_update_wb_ids'],
                [first_wb.id, second_wb.id],
            )

    def test_classic_refresh_keeps_legacy_redirect_and_wb_session_handoff(self):
        source = self._source(self.supplier.id, 'classic-wb', 'fresh classic')
        copied = self._copy(source, title='old classic')
        product = self.Product(
            seller_id=self.seller.id,
            nm_id=123456703,
            vendor_code='classic-wb',
            title='Classic WB product',
        )
        self.db.session.add(product)
        self.db.session.flush()
        copied.import_status = 'imported'
        copied.product_id = product.id
        self.db.session.commit()

        response = self._post(
            '/my-products/refresh-from-supplier',
            data={'selected_ids': str(copied.id)},
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn('refreshed=1', response.headers['Location'])
        self.assertIn('wb_ready=1', response.headers['Location'])
        with self.client.session_transaction() as client_session:
            self.assertEqual(
                client_session['supplier_update_wb_ids'], [product.id],
            )

    def test_foreign_legacy_product_link_is_not_offered_for_wb_followup(self):
        source = self._source(self.supplier.id, 'foreign-product-link', 'fresh')
        foreign_user = self.User(
            username='foreign-product-owner',
            email='foreign-product-owner@test.local',
            is_active=True,
        )
        foreign_user.set_password('synthetic-password')
        foreign_seller = self.Seller(
            user=foreign_user,
            company_name='Foreign product owner',
        )
        self.db.session.add(foreign_seller)
        self.db.session.flush()
        foreign_product = self.Product(
            seller_id=foreign_seller.id,
            nm_id=123456789,
            vendor_code='foreign-product',
            title='Foreign WB product',
        )
        self.db.session.add(foreign_product)
        self.db.session.flush()
        copied = self._copy(source, title='old title')
        copied.import_status = 'imported'
        copied.product_id = foreign_product.id
        self.db.session.commit()

        response = self._post(
            '/my-products/refresh-from-supplier',
            json={'product_ids': [copied.id]},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload['updated'], 1)
        self.assertEqual(payload['wb_ready'], 0)
        self.assertEqual(payload['wb_targets'], [])
        with self.client.session_transaction() as client_session:
            self.assertEqual(client_session['supplier_update_wb_ids'], [])

    def test_terminal_partial_and_all_error_outcomes_are_explicit_and_safe(self):
        source = self._source(self.supplier.id, 'good', 'fresh good')
        good = self._copy(source, title='old good')
        failed_source = self._source(self.supplier.id, 'bad', 'fresh bad')
        failed = self._copy(failed_source, title='old bad')
        unlinked = self._copy(None, title='unlinked')
        wb_products = [
            self.Product(
                seller_id=self.seller.id,
                nm_id=123456704 + index,
                vendor_code=f'partial-{index}',
                title=f'Partial WB product {index}',
            )
            for index in range(3)
        ]
        self.db.session.add_all(wb_products)
        self.db.session.flush()
        for imported, product in zip([good, failed, unlinked], wb_products):
            imported.import_status = 'imported'
            imported.product_id = product.id
        self.db.session.commit()

        from services import supplier_service
        original_update = supplier_service._update_imported_from_supplier

        def fail_one(item, supplier_product):
            if item.id == failed.id:
                raise RuntimeError('private exception detail')
            return original_update(item, supplier_product)

        with patch(
            'services.supplier_service._update_imported_from_supplier',
            side_effect=fail_one,
        ):
            response = self._post(
                '/my-products/refresh-from-supplier',
                json={'product_ids': [good.id, failed.id, unlinked.id]},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload['success'])
        self.assertEqual(payload['status'], 'completed_with_errors')
        self.assertIsInstance(payload.get('resume_receipt'), str)
        self.assertEqual(
            (payload['requested'], payload['updated'], payload['skipped'], payload['errors']),
            (3, 1, 1, 1),
        )
        self.assertEqual(
            [(item['product_id'], item['status'], item['reason']) for item in payload['items']],
            [
                (good.id, 'updated', None),
                (failed.id, 'error', 'update_failed'),
                (unlinked.id, 'skipped', 'no_supplier_link'),
            ],
        )
        self.assertEqual(payload['wb_targets'], [
            {
                'imported_product_id': good.id,
                'product_id': wb_products[0].id,
            },
        ])
        self.assertEqual(payload['wb_ready'], 1)
        self.assertNotIn('private exception detail', response.get_data(as_text=True))
        self.db.session.refresh(good)
        self.db.session.refresh(failed)
        self.assertEqual(good.title, 'fresh good')
        self.assertEqual(failed.title, 'old bad')

    def test_all_row_failures_are_still_a_terminal_200_result(self):
        first_source = self._source(self.supplier.id, 'fail-1', 'fresh 1')
        second_source = self._source(self.supplier.id, 'fail-2', 'fresh 2')
        first = self._copy(first_source, title='old 1')
        second = self._copy(second_source, title='old 2')
        self.db.session.commit()

        with patch(
            'services.supplier_service._update_imported_from_supplier',
            side_effect=RuntimeError('private failure detail'),
        ):
            response = self._post(
                '/my-products/refresh-from-supplier',
                json={'product_ids': [first.id, second.id]},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload['success'])
        self.assertEqual(payload['status'], 'completed_with_errors')
        self.assertNotIn('resume_receipt', payload)
        self.assertEqual(
            (payload['requested'], payload['updated'], payload['skipped'], payload['errors']),
            (2, 0, 0, 2),
        )
        self.assertEqual(
            [(item['product_id'], item['status'], item['reason']) for item in payload['items']],
            [
                (first.id, 'error', 'update_failed'),
                (second.id, 'error', 'update_failed'),
            ],
        )
        self.assertNotIn('private failure detail', response.get_data(as_text=True))

    def test_supplier_refresh_receipt_resumes_mixed_result_without_writes(self):
        good_source = self._source(
            self.supplier.id, 'receipt-mixed-good', 'fresh good',
        )
        failed_source = self._source(
            self.supplier.id, 'receipt-mixed-failed', 'fresh failed',
        )
        good = self._copy(good_source, title='old good')
        failed = self._copy(failed_source, title='old failed')
        unlinked = self._copy(None, title='unlinked title')
        wb_product = self.Product(
            id=987_654_322,
            seller_id=self.seller.id,
            nm_id=123456800,
            vendor_code='mixed-wb',
            title='Mixed WB product',
        )
        self.db.session.add(wb_product)
        self.db.session.flush()
        good.import_status = 'imported'
        good.product_id = wb_product.id
        self.db.session.commit()

        from services import supplier_service
        original_update = supplier_service._update_imported_from_supplier

        def fail_one(item, supplier_product):
            if item.id == failed.id:
                raise RuntimeError('private mixed error detail')
            return original_update(item, supplier_product)

        with patch(
            'services.supplier_service._update_imported_from_supplier',
            side_effect=fail_one,
        ):
            post_response = self._post(
                '/my-products/refresh-from-supplier',
                json={'product_ids': [good.id, failed.id, unlinked.id]},
            )
        self.assertEqual(post_response.status_code, 200)
        original = post_response.get_json()
        token = original['resume_receipt']
        self.assertNotIn('private mixed error detail', token)

        good.title = 'Current good title'
        good.external_vendor_code = 'CURRENT-GOOD'
        failed.title = 'Current failed title'
        failed.external_vendor_code = 'CURRENT-FAILED'
        unlinked.title = ''
        unlinked.external_vendor_code = ''
        unlinked.external_id = 'CURRENT-UNLINKED'
        self.db.session.commit()

        statements = []

        def observe_sql(_connection, _cursor, statement, _parameters, _context, _executemany):
            statements.append(statement.lstrip().split(None, 1)[0].upper())

        event.listen(self.engine, 'before_cursor_execute', observe_sql)
        try:
            response = self._get(
                '/api/my-products/supplier-refresh-result',
                headers={'X-Supplier-Refresh-Receipt': token},
            )
        finally:
            event.remove(self.engine, 'before_cursor_execute', observe_sql)

        self.assertEqual(response.status_code, 200)
        self.assertIn('no-store', response.headers['Cache-Control'])
        self.assertEqual(
            {statement for statement in statements},
            {'SELECT'},
        )
        payload = response.get_json()
        self.assertEqual(
            (payload['success'], payload['status'], payload['requested'],
             payload['updated'], payload['skipped'], payload['errors']),
            (True, 'completed_with_errors', 3, 1, 1, 1),
        )
        self.assertEqual(payload['items'], [
            {'product_id': good.id, 'status': 'updated', 'reason': None},
            {'product_id': failed.id, 'status': 'error', 'reason': 'update_failed'},
            {'product_id': unlinked.id, 'status': 'skipped', 'reason': 'no_supplier_link'},
        ])
        self.assertEqual(payload['labels'], [
            {'productId': good.id, 'label': 'Current good title · CURRENT-GOOD'},
            {'productId': failed.id, 'label': 'Current failed title · CURRENT-FAILED'},
            {'productId': unlinked.id, 'label': 'CURRENT-UNLINKED'},
        ])
        self.assertEqual(payload['wb_ready'], 1)
        self.assertEqual(payload['wb_unavailable'], 0)
        self.assertEqual(payload['wb_targets'], [{
            'imported_product_id': good.id,
            'product_id': wb_product.id,
        }])
        self.assertNotIn('private mixed error detail', response.get_data(as_text=True))

    def test_supplier_refresh_receipt_rejects_missing_tampered_foreign_and_expired(self):
        token, _copied, _product = self._create_refresh_receipt()
        missing_header = self._get('/api/my-products/supplier-refresh-result')
        self.assertEqual(missing_header.status_code, 400)

        query_token = self._get(
            '/api/my-products/supplier-refresh-result',
            query_string={'resume_receipt': token},
        )
        self.assertEqual(query_token.status_code, 400)

        duplicate_headers = self._get(
            '/api/my-products/supplier-refresh-result',
            headers=[
                ('X-Supplier-Refresh-Receipt', token),
                ('X-Supplier-Refresh-Receipt', token),
            ],
        )
        self.assertEqual(duplicate_headers.status_code, 400)

        payload_segment, signature_segment = token.rsplit('.', 1)
        altered_signature = (
            ('A' if signature_segment[0] != 'A' else 'B')
            + signature_segment[1:]
        )
        altered_token = f'{payload_segment}.{altered_signature}'
        tampered = self._get(
            '/api/my-products/supplier-refresh-result',
            headers={'X-Supplier-Refresh-Receipt': altered_token},
        )
        self.assertEqual(tampered.status_code, 400)

        foreign_user_model = self.User(
            username='foreign-refresh-receipt',
            email='foreign-refresh-receipt@test.local',
            is_active=True,
        )
        foreign_user_model.set_password('synthetic-password')
        self.db.session.add(foreign_user_model)
        self.db.session.flush()
        foreign_seller = self.Seller(
            user_id=foreign_user_model.id,
            company_name='Foreign receipt seller',
        )
        self.db.session.add(foreign_seller)
        self.db.session.commit()
        foreign_user = SimpleNamespace(
            id=foreign_user_model.id,
            username=foreign_user_model.username,
            seller=foreign_seller,
            is_authenticated=True,
            is_active=True,
            is_admin=False,
        )
        foreign = self._get(
            '/api/my-products/supplier-refresh-result',
            headers={'X-Supplier-Refresh-Receipt': token},
            user=foreign_user,
        )
        self.assertEqual(foreign.status_code, 404)

        from routes import suppliers as suppliers_routes
        with patch.object(
            suppliers_routes, 'SUPPLIER_REFRESH_RECEIPT_MAX_AGE_SECONDS', -1,
        ):
            expired = self._get(
                '/api/my-products/supplier-refresh-result',
                headers={'X-Supplier-Refresh-Receipt': token},
            )
        self.assertEqual(expired.status_code, 410)

    def test_supplier_refresh_receipt_returns_404_if_any_imported_row_is_missing(self):
        token, copied, _product = self._create_refresh_receipt()
        self.db.session.delete(copied)
        self.db.session.commit()

        response = self._get(
            '/api/my-products/supplier-refresh-result',
            headers={'X-Supplier-Refresh-Receipt': token},
        )

        self.assertEqual(response.status_code, 404)
        self.assertFalse(response.get_json()['success'])

    def test_supplier_refresh_receipt_does_not_promote_a_drifted_wb_link(self):
        token, copied, original_product = self._create_refresh_receipt()
        replacement = self.Product(
            seller_id=self.seller.id,
            nm_id=123456801,
            vendor_code='replacement-wb',
            title='Replacement WB product',
        )
        self.db.session.add(replacement)
        self.db.session.flush()
        copied.product_id = replacement.id
        self.db.session.commit()

        response = self._get(
            '/api/my-products/supplier-refresh-result',
            headers={'X-Supplier-Refresh-Receipt': token},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload['wb_ready'], 0)
        self.assertEqual(payload['wb_unavailable'], 1)
        self.assertEqual(payload['wb_targets'], [])
        self.assertNotEqual(payload['wb_targets'], [{
            'imported_product_id': copied.id,
            'product_id': replacement.id,
        }])
        self.assertNotEqual(original_product.id, replacement.id)

    def test_supplier_refresh_receipt_200_high_ids_stays_within_header_limit(self):
        from routes.suppliers import (
            SUPPLIER_REFRESH_RECEIPT_MAX_TOKEN_BYTES,
            _issue_supplier_refresh_receipt,
            _load_supplier_refresh_receipt,
        )

        generator = random.Random(20261121)

        def unique_high_ids():
            values = set()
            ordered = []
            while len(values) < 200:
                candidate = generator.randrange(
                    8_000_000_000_000_000_000,
                    (1 << 63) - 1,
                )
                if candidate not in values:
                    values.add(candidate)
                    ordered.append(candidate)
            return ordered

        imported_ids = unique_high_ids()
        wb_product_ids = unique_high_ids()
        item_results = [
            {'product_id': product_id, 'status': 'updated', 'reason': None}
            for product_id in imported_ids
        ]
        wb_targets = [
            {'imported_product_id': imported_id, 'product_id': wb_product_id}
            for imported_id, wb_product_id in zip(imported_ids, wb_product_ids)
        ]
        with self.app.app_context():
            token = _issue_supplier_refresh_receipt(
                user_id=(1 << 63) - 2,
                seller_id=(1 << 63) - 3,
                requested_ids=imported_ids,
                requested=200,
                updated=200,
                skipped=0,
                errors=0,
                item_results=item_results,
                wb_targets=wb_targets,
            )
            self.assertIsNotNone(token)
            self.assertLessEqual(
                len(token.encode('utf-8')),
                SUPPLIER_REFRESH_RECEIPT_MAX_TOKEN_BYTES,
            )
            loaded = _load_supplier_refresh_receipt(
                token,
                user_id=(1 << 63) - 2,
                seller_id=(1 << 63) - 3,
            )
        self.assertEqual(loaded['ids'], imported_ids)
        self.assertEqual(loaded['wb_targets'], wb_targets)

    def test_refresh_preview_is_ordered_read_only_and_hides_source_payloads(self):
        source = self._source(
            self.supplier.id, 'preview-linked', 'Fresh supplier title',
        )
        source.description = 'Private supplier description'
        source.supplier_price = 0.0
        source.supplier_quantity = 0
        source.photo_urls_json = json.dumps([
            'https://supplier-private.example/photo.jpg',
        ])
        source.characteristics_json = json.dumps([
            {'name': 'Secret attribute', 'value': 'Secret value'},
        ], ensure_ascii=False)
        source.original_data_json = json.dumps({
            'title': 'Fresh supplier title',
            'description': 'Private supplier description',
            'photo_urls': ['https://supplier-private.example/photo.jpg'],
            'raw_secret': 'do-not-return-this-source-body',
        }, ensure_ascii=False)
        linked = self._copy(source, title='Old seller title')
        linked.supplier_price = 7.5
        linked.supplier_quantity = 3
        unlinked = self._copy(None, title='Unlinked seller title')
        self.db.session.commit()
        linked_id, unlinked_id, source_id = linked.id, unlinked.id, source.id

        response = self._post(
            '/api/my-products/supplier-refresh-preview',
            json={'product_ids': [unlinked_id, linked_id]},
        )

        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertIn('no-store', response.headers['Cache-Control'])
        payload = response.get_json()
        self.assertEqual(
            (payload['requested'], payload['eligible'], payload['changed'], payload['skipped']),
            (2, 1, 1, 1),
        )
        self.assertNotIn('review_digest', payload)
        self.assertIsInstance(payload['review_token'], str)
        self.assertLessEqual(len(payload['review_token'].encode('utf-8')), 8192)
        self.assertEqual(
            [(row['product_id'], row['state']) for row in payload['items']],
            [(unlinked_id, 'unlinked'), (linked_id, 'changed')],
        )
        self.assertEqual(payload['items'][0]['changes'], [])
        changes = {item['field']: item for item in payload['items'][1]['changes']}
        self.assertEqual(
            (changes['title']['before'], changes['title']['after']),
            ('Old seller title', 'Fresh supplier title'),
        )
        self.assertEqual(
            (changes['purchase_price']['before'], changes['purchase_price']['after']),
            ('7.5', '0'),
        )
        self.assertEqual(
            (changes['supplier_quantity']['before'], changes['supplier_quantity']['after']),
            ('3', '0'),
        )
        for field in ('description', 'photos', 'characteristics', 'source_snapshot'):
            if field in changes:
                self.assertEqual((changes[field]['before'], changes[field]['after']), (None, None))
        response_text = response.get_data(as_text=True)
        for secret in (
            'Private supplier description',
            'supplier-private.example',
            'Secret attribute',
            'Secret value',
            'do-not-return-this-source-body',
        ):
            self.assertNotIn(secret, response_text)
        self.assertFalse(self.db.session.new, 'projection rows must remain unattached')
        self.db.session.expire_all()
        self.db.session.refresh(linked)
        self.db.session.refresh(source)
        self.assertEqual(linked.title, 'Old seller title')
        self.assertEqual(linked.supplier_price, 7.5)
        self.assertEqual(linked.supplier_quantity, 3)
        self.assertIsNone(linked.original_data)
        self.assertEqual(source.id, source_id)
        self.assertEqual(source.title, 'Fresh supplier title')

    def test_reviewed_refresh_applies_real_projection_and_preserves_manual_override(self):
        source = self._source(
            self.supplier.id, 'review-apply', 'Fresh inherited title',
        )
        source.description = 'Never disclose this description'
        source.description_source = 'csv'
        source.supplier_price = 0.0
        source.supplier_quantity = 0
        source.photo_urls_json = json.dumps([
            'https://supplier-private.example/new-photo.jpg',
        ])
        source.characteristics_json = None
        source.original_data_json = json.dumps({
            'title': source.title,
            'description': source.description,
            'photo_urls': ['https://supplier-private.example/new-photo.jpg'],
        }, ensure_ascii=False)
        source.content_revision = 3
        copied = self._copy(source, title='Seller-owned title')
        copied.description = 'Seller-owned description'
        copied.photo_urls = json.dumps(['https://seller-private.example/old.jpg'])
        copied.characteristics = json.dumps([
            {'name': 'Old field', 'value': 'Old value'},
        ], ensure_ascii=False)
        copied.supplier_price = 12.5
        copied.supplier_quantity = 4
        copied.content_overrides_json = json.dumps({
            'schema_version': 1,
            'fields': {
                'title': {
                    'value': 'Seller-owned title',
                    'inherited_value': 'Old inherited title',
                    'inherited_origin': 'source',
                    'edited_by_user_id': self.user.id,
                    'edited_at': '2026-11-21T00:00:00',
                    'edit_version': 1,
                },
                'description': {
                    'value': 'Seller-owned description',
                    'inherited_value': 'Old supplier description',
                    'inherited_origin': 'source',
                    'edited_by_user_id': self.user.id,
                    'edited_at': '2026-11-21T00:00:00',
                    'edit_version': 1,
                },
            },
        }, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        self.db.session.commit()

        preview = self._post(
            '/api/my-products/supplier-refresh-preview',
            json={'product_ids': [copied.id]},
        )
        self.assertEqual(preview.status_code, 200, preview.get_json())
        token = preview.get_json()['review_token']
        rows = preview.get_json()['items']
        fields = {change['field']: change for change in rows[0]['changes']}
        self.assertEqual(rows[0]['state'], 'changed')
        self.assertNotIn('title', fields, 'effective manual title stays unchanged')
        self.assertEqual(
            (fields['purchase_price']['before'], fields['purchase_price']['after']),
            ('12.5', '0'),
        )
        self.assertEqual(
            (fields['supplier_quantity']['before'], fields['supplier_quantity']['after']),
            ('4', '0'),
        )
        for field in ('photos', 'characteristics', 'source_snapshot'):
            self.assertEqual((fields[field]['before'], fields[field]['after']), (None, None))
        preview_text = preview.get_data(as_text=True)
        self.assertNotIn('Never disclose this description', preview_text)
        self.assertNotIn('supplier-private.example', preview_text)

        applied = self._post(
            '/my-products/refresh-from-supplier',
            json={'product_ids': [copied.id], 'review_token': token},
        )
        self.assertEqual(applied.status_code, 200, applied.get_json())
        payload = applied.get_json()
        self.assertEqual((payload['requested'], payload['updated'], payload['skipped'], payload['errors']), (1, 1, 0, 0))
        self.db.session.refresh(copied)
        self.assertEqual(copied.title, 'Seller-owned title')
        self.assertEqual(copied.description, 'Seller-owned description')
        self.assertEqual(copied.supplier_price, 0.0)
        self.assertEqual(copied.supplier_quantity, 0)
        self.assertIsNone(copied.characteristics)
        self.assertEqual(
            json.loads(copied.photo_urls),
            ['https://supplier-private.example/new-photo.jpg'],
        )
        overrides = json.loads(copied.content_overrides_json)
        self.assertEqual(
            overrides['fields']['title']['inherited_value'],
            'Fresh inherited title',
        )

    def test_reviewed_refresh_stale_source_and_card_return_zero_write_409(self):
        source = self._source(self.supplier.id, 'review-stale', 'Fresh title')
        copied = self._copy(source, title='Old title')
        copied.supplier_price = 5.0
        self.db.session.commit()

        source_preview = self._post(
            '/api/my-products/supplier-refresh-preview',
            json={'product_ids': [copied.id]},
        )
        source_token = source_preview.get_json()['review_token']
        source.title = 'Changed after review'
        source.supplier_price = 99.0
        source.content_revision += 1
        self.db.session.commit()

        stale_source = self._post(
            '/my-products/refresh-from-supplier',
            json={'product_ids': [copied.id], 'review_token': source_token},
        )
        self.assertEqual(stale_source.status_code, 409, stale_source.get_json())
        self.assertEqual(stale_source.get_json()['code'], 'supplier_refresh_review_stale')
        self.db.session.refresh(copied)
        self.assertEqual(copied.title, 'Old title')
        self.assertEqual(copied.supplier_price, 5.0)

        card_preview = self._post(
            '/api/my-products/supplier-refresh-preview',
            json={'product_ids': [copied.id]},
        )
        card_token = card_preview.get_json()['review_token']
        copied.title = 'Seller changed after review'
        copied.content_edit_version += 1
        self.db.session.commit()

        stale_card = self._post(
            '/my-products/refresh-from-supplier',
            json={'product_ids': [copied.id], 'review_token': card_token},
        )
        self.assertEqual(stale_card.status_code, 409, stale_card.get_json())
        self.assertEqual(stale_card.get_json()['code'], 'supplier_refresh_review_stale')
        self.db.session.refresh(copied)
        self.assertEqual(copied.title, 'Seller changed after review')
        self.assertEqual(copied.supplier_price, 5.0)

    def test_reviewed_refresh_rejects_reordered_foreign_and_malformed_tokens(self):
        first_source = self._source(self.supplier.id, 'review-first', 'fresh first')
        second_source = self._source(self.supplier.id, 'review-second', 'fresh second')
        first = self._copy(first_source, title='old first')
        second = self._copy(second_source, title='old second')
        self.db.session.commit()

        preview = self._post(
            '/api/my-products/supplier-refresh-preview',
            json={'product_ids': [first.id, second.id]},
        )
        token = preview.get_json()['review_token']
        reversed_selection = self._post(
            '/my-products/refresh-from-supplier',
            json={'product_ids': [second.id, first.id], 'review_token': token},
        )
        self.assertEqual(reversed_selection.status_code, 400)

        malformed = self._post(
            '/my-products/refresh-from-supplier',
            json={'product_ids': [first.id], 'review_token': 'not-a-signed-token'},
        )
        self.assertEqual(malformed.status_code, 400)

        from models import User, Seller
        foreign_user_model = User(
            username='foreign-refresh-review',
            email='foreign-refresh-review@test.local',
            is_active=True,
        )
        foreign_user_model.set_password('synthetic-password')
        self.db.session.add(foreign_user_model)
        self.db.session.flush()
        foreign_seller = Seller(
            user_id=foreign_user_model.id,
            company_name='Foreign review seller',
        )
        self.db.session.add(foreign_seller)
        self.db.session.flush()
        foreign_user = SimpleNamespace(
            id=foreign_user_model.id,
            username=foreign_user_model.username,
            seller=foreign_seller,
            is_authenticated=True,
            is_active=True,
            is_admin=False,
        )
        self.db.session.commit()
        foreign_token = self._post(
            '/my-products/refresh-from-supplier',
            json={'product_ids': [first.id], 'review_token': token},
            user=foreign_user,
        )
        self.assertEqual(foreign_token.status_code, 404)
        self.db.session.refresh(first)
        self.db.session.refresh(second)
        self.assertEqual(first.title, 'old first')
        self.assertEqual(second.title, 'old second')

    def test_reviewed_refresh_missing_scope_is_stale_and_misbound_source_is_unlinked(self):
        source = self._source(self.supplier.id, 'review-owner', 'should not apply')
        copied = self._copy(source, title='owned title')
        other_source = self._source(
            self.other_supplier.id, 'misbound-source', 'private misbound source title',
        )
        misbound = self._copy(
            other_source, title='misbound local title',
            supplier_id=self.supplier.id,
        )
        self.db.session.commit()

        preview_misbound = self._post(
            '/api/my-products/supplier-refresh-preview',
            json={'product_ids': [misbound.id]},
        )
        self.assertEqual(preview_misbound.status_code, 200, preview_misbound.get_json())
        misbound_payload = preview_misbound.get_json()
        self.assertEqual(misbound_payload['items'][0]['state'], 'unlinked')
        self.assertNotIn('private misbound source title', preview_misbound.get_data(as_text=True))
        misbound_apply = self._post(
            '/my-products/refresh-from-supplier',
            json={
                'product_ids': [misbound.id],
                'review_token': misbound_payload['review_token'],
            },
        )
        self.assertEqual(misbound_apply.status_code, 200, misbound_apply.get_json())
        self.assertEqual(
            (misbound_apply.get_json()['updated'], misbound_apply.get_json()['skipped']),
            (0, 1),
        )
        self.db.session.refresh(misbound)
        self.assertEqual(misbound.title, 'misbound local title')

        preview = self._post(
            '/api/my-products/supplier-refresh-preview',
            json={'product_ids': [copied.id]},
        )
        token = preview.get_json()['review_token']
        copied.seller_id = self.other_supplier.id
        self.db.session.commit()
        stale = self._post(
            '/my-products/refresh-from-supplier',
            json={'product_ids': [copied.id], 'review_token': token},
        )
        self.assertEqual(stale.status_code, 409, stale.get_json())
        self.assertEqual(stale.get_json()['code'], 'supplier_refresh_review_stale')
        self.db.session.refresh(copied)
        self.assertEqual(copied.title, 'owned title')

    def test_invalid_empty_and_foreign_my_products_selections_write_nothing(self):
        source = self._source(self.supplier.id, 'owned', 'fresh')
        copied = self._copy(source, title='old')
        # Use another valid user so the foreign row has a real tenant owner.
        from models import User, Seller
        foreign_user_model = User(
            username='foreign-exact-refresh',
            email='foreign-exact-refresh@test.local',
            is_active=True,
        )
        foreign_user_model.set_password('synthetic-password')
        self.db.session.add(foreign_user_model)
        self.db.session.flush()
        foreign_seller = Seller(
            user_id=foreign_user_model.id,
            company_name='Foreign seller',
        )
        self.db.session.add(foreign_seller)
        self.db.session.flush()
        foreign_copy = self.ImportedProduct(
            seller_id=foreign_seller.id,
            supplier_id=self.supplier.id,
            supplier_product_id=source.id,
            external_id='foreign-copy',
            title='foreign old',
        )
        self.db.session.add(foreign_copy)
        self.db.session.commit()

        empty = self._post(
            '/my-products/refresh-from-supplier', json={'product_ids': []},
        )
        malformed = self._post(
            '/my-products/refresh-from-supplier', json=[1],
        )
        foreign = self._post(
            '/my-products/refresh-from-supplier',
            json={'product_ids': [foreign_copy.id]},
        )

        self.assertEqual(empty.status_code, 400)
        self.assertEqual(malformed.status_code, 400)
        self.assertEqual(foreign.status_code, 400)
        self.assertFalse(empty.get_json()['success'])
        self.assertFalse(malformed.get_json()['success'])
        self.assertFalse(foreign.get_json()['success'])
        self.db.session.refresh(copied)
        self.db.session.refresh(foreign_copy)
        self.assertEqual(copied.title, 'old')
        self.assertEqual(foreign_copy.title, 'foreign old')

    def test_batch_commit_failure_rolls_back_and_returns_terminal_error(self):
        source = self._source(self.supplier.id, 'commit-failure', 'fresh title')
        copied = self._copy(source, title='old title')
        self.db.session.commit()
        copied_id = copied.id
        session = self.db.session()

        def fail_commit(_session):
            raise RuntimeError('private commit detail')

        event.listen(session, 'before_commit', fail_commit)
        try:
            response = self._post(
                '/my-products/refresh-from-supplier',
                json={'product_ids': [copied.id]},
            )
        finally:
            event.remove(session, 'before_commit', fail_commit)

        self.assertEqual(response.status_code, 500)
        self.assertFalse(response.get_json()['success'])
        self.assertNotIn('private commit detail', response.get_data(as_text=True))
        self.db.session.remove()
        with self.engine.connect() as observer:
            persisted_title = observer.execute(
                text('SELECT title FROM imported_products WHERE id = :product_id'),
                {'product_id': copied_id},
            ).scalar_one()
        self.assertEqual(persisted_title, 'old title')

    def test_explicit_all_mode_has_no_selected_batch_cap(self):
        rows = [self.ImportedProduct(
            seller_id=self.seller.id,
            supplier_id=self.supplier.id,
            external_id=f'unlinked-{index}',
            title=f'unlinked {index}',
        ) for index in range(201)]
        self.db.session.add_all(rows)
        self.db.session.commit()

        from services.supplier_service import SupplierService

        result = SupplierService.update_supplier_products(
            self.seller.id, self.supplier.id, None,
        )

        self.assertEqual(result.total_requested, 201)
        self.assertEqual(result.skipped, 201)
        self.assertEqual(result.imported, 0)
        self.assertEqual(len(result.item_results), 201)

    def test_legacy_none_all_and_explicit_empty_service_scopes_remain_distinct(self):
        source = self._source(self.supplier.id, 'legacy-service', 'fresh title')
        copied = self._copy(source, title='old title')
        self.db.session.commit()

        from services.supplier_service import SupplierService

        empty_result = SupplierService.update_seller_products(self.seller.id, [])
        self.db.session.refresh(copied)
        self.assertEqual(empty_result.imported, 0)
        self.assertEqual(copied.title, 'old title')

        all_result = SupplierService.update_seller_products(self.seller.id)
        self.db.session.refresh(copied)
        self.assertEqual(all_result.imported, 1)
        self.assertEqual(copied.title, 'fresh title')


if __name__ == '__main__':
    unittest.main()
