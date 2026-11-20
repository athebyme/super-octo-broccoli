# -*- coding: utf-8 -*-
"""Exact seller/supplier scope and terminal feedback for supplier refresh."""

import os
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

    def _post(self, path, data=None, *, json=None):
        with patch('flask_login.utils._get_user', return_value=self.user), \
                patch('routes.suppliers.current_user', self.user):
            return self.client.post(path, data=data, json=json)

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
        self.db.session.refresh(selected)
        self.db.session.refresh(other_copy)
        self.assertEqual(selected.title, 'fresh title')
        self.assertEqual(other_copy.title, 'old unselected')

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
        with self.client.session_transaction() as client_session:
            self.assertEqual(client_session['supplier_update_wb_ids'], [])

    def test_terminal_partial_and_all_error_outcomes_are_explicit_and_safe(self):
        source = self._source(self.supplier.id, 'good', 'fresh good')
        good = self._copy(source, title='old good')
        failed_source = self._source(self.supplier.id, 'bad', 'fresh bad')
        failed = self._copy(failed_source, title='old bad')
        unlinked = self._copy(None, title='unlinked')
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
