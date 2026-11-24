# -*- coding: utf-8 -*-
"""Кнопка «Обновить данные» в каталоге поставщика.

Форма на карточке поставщика не передаёт список ID — она просит обновить все
импортированные карточки этого поставщика. До появления явного флага роут
всегда отвечал «Не выбраны карточки для обновления», то есть кнопка не работала
вообще. Набор ID всегда собирается seller-scoped на сервере.
"""

import os
import re
import unittest
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch


class SupplierCatalogUpdateAllTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ['DISABLE_SECURE_COOKIE'] = '1'
        os.environ.setdefault('SKIP_SCHEDULER', '1')
        import sqlalchemy as _sa
        from sqlalchemy.pool import StaticPool
        import seller_platform  # noqa
        from models import db
        cls.app = seller_platform.app
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

    def setUp(self):
        from models import ImportedProduct, Seller, SellerSupplier, Supplier, User
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.db.create_all()
        self.client = self.app.test_client()

        user = User(username='cat-owner', email='cat@test.local', is_active=True)
        user.set_password('synthetic-password')
        seller = Seller(user=user, company_name='Каталожник')
        other_user = User(
            username='cat-foreign',
            email='foreign@test.local',
            is_active=True,
        )
        other_user.set_password('synthetic-password')
        other_seller = Seller(user=other_user, company_name='Чужой')
        supplier = Supplier(name='Опт', code='opt')
        self.db.session.add_all([seller, other_seller, supplier])
        self.db.session.flush()
        self.db.session.add(SellerSupplier(
            seller_id=seller.id,
            supplier_id=supplier.id,
            is_active=True,
        ))

        own = [
            ImportedProduct(
                seller_id=seller.id,
                supplier_id=supplier.id,
                external_id=f'own-{index}',
                source_type='synthetic',
                title=f'Товар {index}',
            )
            for index in range(3)
        ]
        foreign = ImportedProduct(
            seller_id=other_seller.id,
            supplier_id=supplier.id,
            external_id='foreign-1',
            source_type='synthetic',
            title='Чужой товар',
        )
        self.db.session.add_all(own + [foreign])
        self.db.session.commit()

        self.seller_id = seller.id
        self.supplier_id = supplier.id
        self.own_ids = sorted(row.id for row in own)
        self.foreign_id = foreign.id
        self.other_user = SimpleNamespace(
            id=other_user.id,
            username=other_user.username,
            seller=other_seller,
            is_authenticated=True,
            is_active=True,
            is_admin=False,
        )
        self.user = SimpleNamespace(
            id=user.id,
            username=user.username,
            seller=seller,
            is_authenticated=True,
            is_active=True,
            is_admin=False,
        )

    def tearDown(self):
        self.db.session.remove()
        self.db.drop_all()
        self.ctx.pop()

    def _post(
        self, data, *, path='/supplier-catalog/update', user=None, headers=None,
    ):
        user = user or self.user
        with patch('flask_login.utils._get_user', return_value=user), \
                patch('routes.suppliers.current_user', user):
            return self.client.post(
                path,
                data=data,
                follow_redirects=False,
                headers=headers,
            )

    def _add_source_copy(self, suffix='1', *, commit=True):
        from models import ImportedProduct, SupplierProduct

        source = SupplierProduct(
            supplier_id=self.supplier_id,
            external_id=f'source-{suffix}',
            vendor_code=f'SOURCE-{suffix}',
            title=f'Новое имя {suffix}',
            supplier_price=149,
            supplier_quantity=5,
            currency='RUB',
            supplier_status='in_stock',
            status='ready',
            content_revision=2,
        )
        self.db.session.add(source)
        self.db.session.flush()
        imported = ImportedProduct(
            seller_id=self.seller_id,
            supplier_id=self.supplier_id,
            supplier_product_id=source.id,
            supplier_content_revision=1,
            external_id=f'copy-{suffix}',
            external_vendor_code=f'COPY-{suffix}',
            source_type='synthetic',
            title=f'Старое имя {suffix}',
            supplier_price=99,
            supplier_quantity=1,
            import_status='validated',
        )
        self.db.session.add(imported)
        if commit:
            self.db.session.commit()
        return source, imported

    def _review_post(self, data, *, user=None):
        return self._post(
            data,
            path='/supplier-catalog/update/review',
            user=user,
        )

    def _confirm_post(self, data, *, user=None):
        return self._post(
            data,
            path='/supplier-catalog/update/confirm',
            user=user,
        )

    @staticmethod
    def _review_token(response):
        match = re.search(
            rb'name="review_token" value="([^"]+)"',
            response.data,
        )
        if not match:
            raise AssertionError('review page did not render a confirmation token')
        return match.group(1).decode('ascii')

    @staticmethod
    def _review_count(response, field):
        match = re.search(
            rb'data-' + field.encode('ascii') + rb'[^>]*>\s*([0-9]+)\s*<',
            response.data,
        )
        if not match:
            raise AssertionError(f'review page did not render {field} count')
        return int(match.group(1))

    def _confirm_values(self, source_ids, token, *, return_to=None):
        data = {
            'supplier_id': str(self.supplier_id),
            'review_token': token,
            'return_to': return_to or f'/supplier-catalog/{self.supplier_id}/products',
        }
        if source_ids is None:
            data['update_all_for_supplier'] = '1'
        else:
            data['update_product_ids'] = [str(value) for value in source_ids]
        return data

    def _post_legacy(self, data):
        return self._post(data)

    def test_update_all_collects_only_own_products(self):
        captured = {}

        def _fake_update(seller_id, supplier_id, source_ids):
            captured['seller_id'] = seller_id
            captured['supplier_id'] = supplier_id
            captured['source_ids'] = source_ids
            return SimpleNamespace(
                total_requested=len(self.own_ids),
                imported=len(self.own_ids),
                skipped=0,
                errors=0,
            )

        with patch(
            'services.supplier_service.SupplierService.update_supplier_products',
            side_effect=_fake_update,
        ):
            response = self._post_legacy({
                'supplier_id': str(self.supplier_id),
                'update_all_for_supplier': '1',
            })

        self.assertEqual(response.status_code, 302)
        self.assertEqual(captured['seller_id'], self.seller_id)
        self.assertEqual(captured['supplier_id'], self.supplier_id)
        self.assertIsNone(captured['source_ids'])
        self.assertEqual(len(self.own_ids), 3)
        self.assertNotIn(self.foreign_id, self.own_ids)

    def test_without_flag_and_ids_nothing_is_updated(self):
        with patch(
            'services.supplier_service.SupplierService.update_supplier_products',
        ) as updater:
            response = self._post_legacy({'supplier_id': str(self.supplier_id)})
        self.assertEqual(response.status_code, 302)
        updater.assert_not_called()

    def test_exact_review_is_read_only_until_signed_confirmation(self):
        source, imported = self._add_source_copy()
        old_values = (imported.title, imported.supplier_price, imported.supplier_quantity)
        return_to = f'/supplier-catalog/{self.supplier_id}/products?search=fixture'

        with patch(
            'services.supplier_service.SupplierService.update_supplier_products',
            wraps=__import__(
                'services.supplier_service', fromlist=['SupplierService'],
            ).SupplierService.update_supplier_products,
        ) as updater:
            response = self._review_post({
                'supplier_id': str(self.supplier_id),
                'product_ids': str(source.id),
                'return_to': return_to,
            })
            self.assertEqual(response.status_code, 200)
            updater.assert_not_called()

        self.assertIn('Проверка обновления'.encode('utf-8'), response.data)
        self.assertIn('Старое имя 1'.encode('utf-8'), response.data)
        self.assertIn('Новое имя 1'.encode('utf-8'), response.data)
        self.assertIn(
            'Сохранение не отправляет данные в WB или Ozon'.encode('utf-8'),
            response.data,
        )
        self.assertIn('no-store', response.headers.get('Cache-Control', '').lower())
        self.db.session.refresh(imported)
        self.assertEqual(
            (imported.title, imported.supplier_price, imported.supplier_quantity),
            old_values,
        )

        token = self._review_token(response)
        confirmation = self._confirm_post(self._confirm_values(
            [source.id], token, return_to=return_to,
        ))
        self.assertEqual(confirmation.status_code, 302)
        self.assertEqual(confirmation.headers['Location'], return_to)
        self.db.session.refresh(imported)
        self.assertEqual(imported.title, 'Новое имя 1')
        self.assertEqual(imported.supplier_price, 149)
        self.assertEqual(imported.supplier_quantity, 5)

    def test_equal_revision_category_drift_is_selectable_and_reviewed_locally(self):
        import json

        from models import ImportedProduct, SupplierProduct

        source = SupplierProduct(
            supplier_id=self.supplier_id,
            external_id='category-drift-sku',
            vendor_code='CATEGORY-DRIFT-VENDOR',
            title='Новое имя из каталога',
            category='Новая категория',
            original_data_json=json.dumps({
                'category': 'Новая категория',
            }, ensure_ascii=False),
            supplier_price=149,
            supplier_quantity=5,
            currency='RUB',
            status='ready',
            content_revision=5,
        )
        self.db.session.add(source)
        self.db.session.flush()
        manual_overrides = {
            'schema_version': 1,
            'fields': {
                'title': {
                    'value': 'Ручное название продавца',
                    'inherited_value': 'Старое имя из каталога',
                    'inherited_origin': 'source',
                    'edited_by_user_id': self.user.id,
                    'edited_at': '2026-11-24T00:00:00',
                    'edit_version': 1,
                },
            },
        }
        imported = ImportedProduct(
            seller_id=self.seller_id,
            supplier_id=self.supplier_id,
            supplier_product_id=source.id,
            supplier_content_revision=5,
            external_id=source.external_id,
            external_vendor_code=source.vendor_code,
            source_type='opt',
            title='Ручное название продавца',
            category='Старая категория',
            original_data=json.dumps({
                'category': 'Старая категория',
            }, ensure_ascii=False),
            content_overrides_json=json.dumps(
                manual_overrides,
                ensure_ascii=False,
                sort_keys=True,
                separators=(',', ':'),
            ),
            supplier_price=99,
            supplier_quantity=1,
            import_status='validated',
        )
        self.db.session.add(imported)
        self.db.session.commit()

        with patch('flask_login.utils._get_user', return_value=self.user), \
                patch('routes.suppliers.current_user', self.user):
            listing = self.client.get(
                f'/supplier-catalog/{self.supplier_id}/products'
                '?updates_only=1&select_all=updates&stock_status=all',
                headers={'Accept': 'application/json'},
            )
        self.assertEqual(listing.status_code, 200)
        catalog = listing.get_json()
        self.assertEqual(catalog['mode'], 'updates')
        self.assertEqual(catalog['total'], 1)
        self.assertEqual(catalog['eligible_count'], 1)
        self.assertEqual(catalog['selection'], [str(source.id)])
        self.assertEqual(
            [item['id'] for item in catalog['items'] if item['updatable']],
            [str(source.id)],
        )

        review = self._post(
            {
                'supplier_id': str(self.supplier_id),
                'update_product_ids': [str(source.id)],
            },
            path='/supplier-catalog/update/review',
            headers={'Accept': 'application/json'},
        )
        self.assertEqual(review.status_code, 200, review.get_json())
        preview = review.get_json()
        self.assertTrue(preview['success'])
        self.assertEqual(preview['source_ids'], [str(source.id)])
        self.assertEqual(preview['requested'], 1)
        self.assertEqual(preview['eligible'], 1)
        self.assertEqual(preview['changed'], 1)
        self.assertEqual(preview['items'][0]['state'], 'changed')

        confirmation = self._post(
            self._confirm_values([source.id], preview['review_token']),
            path='/supplier-catalog/update/confirm',
            headers={'Accept': 'application/json'},
        )
        self.assertEqual(confirmation.status_code, 200, confirmation.get_json())
        result = confirmation.get_json()
        self.assertTrue(result['success'])
        self.assertEqual(result['updated'], 1)
        self.assertFalse(result['wb_changed'])
        self.assertFalse(result['ozon_changed'])
        self.db.session.refresh(imported)
        self.assertEqual(imported.category, 'Новая категория')
        self.assertEqual(imported.supplier_content_revision, 5)
        self.assertEqual(imported.title, 'Ручное название продавца')
        refreshed_overrides = json.loads(imported.content_overrides_json)
        self.assertEqual(
            refreshed_overrides['fields']['title']['value'],
            'Ручное название продавца',
        )
        self.assertEqual(
            refreshed_overrides['fields']['title']['inherited_value'],
            'Новое имя из каталога',
        )

    def test_filtered_update_review_return_clears_auto_select_and_keeps_filters(self):
        source, imported = self._add_source_copy('filtered-return')
        return_to = (
            f'/supplier-catalog/{self.supplier_id}/products?page=1'
            '&search=fixture&show_imported=1&updates_only=1'
            '&stock_status=all&wb_filter=not_on_wb&ai_filter=ai_parsed'
            '&select_all=updates'
        )
        review = self._review_post({
            'supplier_id': str(self.supplier_id),
            'update_product_ids': [str(source.id)],
            'return_to': return_to,
        })
        self.assertEqual(review.status_code, 200)
        match = re.search(
            rb'name="return_to" value="([^"]+)"',
            review.data,
        )
        self.assertIsNotNone(match)
        import html
        safe_return = html.unescape(match.group(1).decode('utf-8'))
        parsed_return = urlsplit(safe_return)
        self.assertEqual(
            parsed_return.path,
            f'/supplier-catalog/{self.supplier_id}/products',
        )
        self.assertEqual(parse_qs(parsed_return.query), {
            'page': ['1'], 'search': ['fixture'], 'show_imported': ['1'],
            'updates_only': ['1'], 'stock_status': ['all'],
            'wb_filter': ['not_on_wb'], 'ai_filter': ['ai_parsed'],
        })
        self.assertNotIn('select_all', parse_qs(parsed_return.query))
        self.assertEqual(imported.title, f'Старое имя filtered-return')

        confirmation = self._confirm_post(self._confirm_values(
            [source.id], self._review_token(review), return_to=return_to,
        ))
        self.assertEqual(confirmation.status_code, 302)
        self.assertEqual(confirmation.headers['Location'], safe_return)
        self.db.session.refresh(imported)
        self.assertEqual(imported.title, source.title)

    def test_supplier_wide_review_is_explicit_and_tokenless_legacy_still_applies(self):
        source, imported = self._add_source_copy('all')
        old_title = imported.title
        response = self._review_post({
            'supplier_id': str(self.supplier_id),
            'update_all_for_supplier': '1',
            'return_to': '/supplier-catalog',
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn(
            'Все связанные локальные карточки этого поставщика.'.encode('utf-8'),
            response.data,
        )
        self.assertEqual(self._review_count(response, 'review-requested'), 4)
        self.assertEqual(self._review_count(response, 'review-changed'), 1)
        self.assertEqual(self._review_count(response, 'review-unlinked'), 3)
        self.db.session.refresh(imported)
        self.assertEqual(imported.title, old_title)

        token = self._review_token(response)
        confirmation = self._confirm_post(self._confirm_values(
            None, token, return_to='/supplier-catalog',
        ))
        self.assertEqual(confirmation.status_code, 302)
        self.assertEqual(confirmation.headers['Location'], '/supplier-catalog')
        self.db.session.refresh(imported)
        self.assertEqual(imported.title, source.title)

        legacy_source, legacy_copy = self._add_source_copy('legacy')
        legacy_response = self._post_legacy({
            'supplier_id': str(self.supplier_id),
            'product_ids': str(legacy_source.id),
        })
        self.assertEqual(legacy_response.status_code, 302)
        self.db.session.refresh(legacy_copy)
        self.assertEqual(legacy_copy.title, legacy_source.title)

    def test_supplier_wide_review_caps_details_but_confirms_all_201_changed_rows(self):
        copies = []
        for index in range(201):
            _, imported = self._add_source_copy(
                f'large-{index}',
                commit=False,
            )
            copies.append(imported)
        self.db.session.commit()

        response = self._review_post({
            'supplier_id': str(self.supplier_id),
            'update_all_for_supplier': '1',
            'return_to': '/supplier-catalog',
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._review_count(response, 'review-requested'), 204)
        self.assertEqual(self._review_count(response, 'review-changed'), 201)
        self.assertEqual(self._review_count(response, 'review-unlinked'), 3)
        self.assertIn('Показаны первые 50 из 204'.encode('utf-8'), response.data)
        self.assertEqual(response.data.count(b'data-supplier-review-row '), 50)
        self.db.session.expire_all()
        self.assertEqual(
            self.db.session.get(type(copies[0]), copies[0].id).title,
            f'Старое имя large-0',
        )

        confirmation = self._confirm_post(self._confirm_values(
            None,
            self._review_token(response),
            return_to='/supplier-catalog',
        ))
        self.assertEqual(confirmation.status_code, 302)
        refreshed = [
            self.db.session.get(type(copies[0]), copy.id)
            for copy in copies
        ]
        self.assertTrue(all(copy.title.startswith('Новое имя large-') for copy in refreshed))

    def test_review_scope_mismatch_and_foreign_user_fail_closed(self):
        source, imported = self._add_source_copy('scope-a')
        other_source, other_imported = self._add_source_copy('scope-b')
        response = self._review_post({
            'supplier_id': str(self.supplier_id),
            'update_product_ids': [str(source.id)],
        })
        self.assertEqual(response.status_code, 200)
        token = self._review_token(response)

        mismatch = self._confirm_post(self._confirm_values(
            [other_source.id], token,
        ))
        self.assertEqual(mismatch.status_code, 409)
        self.db.session.refresh(imported)
        self.db.session.refresh(other_imported)
        self.assertEqual(imported.title, 'Старое имя scope-a')
        self.assertEqual(other_imported.title, 'Старое имя scope-b')

        foreign = self._confirm_post(
            self._confirm_values([source.id], token),
            user=self.other_user,
        )
        self.assertEqual(foreign.status_code, 404)
        self.db.session.refresh(imported)
        self.assertEqual(imported.title, 'Старое имя scope-a')

    def test_stale_catalog_source_returns_conflict_without_updating_local_copy(self):
        source, imported = self._add_source_copy('stale')
        response = self._review_post({
            'supplier_id': str(self.supplier_id),
            'product_ids': str(source.id),
        })
        self.assertEqual(response.status_code, 200)
        token = self._review_token(response)
        with self.app.app_context():
            changed_source = self.db.session.get(type(source), source.id)
            changed_source.title = 'Изменено после проверки'
            self.db.session.commit()

        confirmation = self._confirm_post(self._confirm_values([source.id], token))
        self.assertEqual(confirmation.status_code, 409)
        self.assertIn('изменились после проверки', confirmation.data.decode('utf-8'))
        self.db.session.refresh(imported)
        self.assertEqual(imported.title, 'Старое имя stale')

    def test_review_rejects_more_than_200_ids_without_preview_or_write(self):
        source, imported = self._add_source_copy('limit')
        oversized_ids = [str(index) for index in range(1000, 1201)]
        with patch(
            'services.supplier_service.SupplierService.preview_supplier_products',
        ) as preview, patch(
            'services.supplier_service.SupplierService.update_supplier_products',
        ) as updater:
            response = self._review_post({
                'supplier_id': str(self.supplier_id),
                'update_product_ids': oversized_ids,
                'return_to': f'/supplier-catalog/{self.supplier_id}/products',
            })
            preview.assert_not_called()
            updater.assert_not_called()
        self.assertEqual(response.status_code, 302)
        self.db.session.refresh(imported)
        self.assertEqual(imported.title, 'Старое имя limit')

    def test_malformed_confirmation_does_not_offer_broader_all_retry(self):
        source, imported = self._add_source_copy('malformed')
        review = self._review_post({
            'supplier_id': str(self.supplier_id),
            'product_ids': str(source.id),
        })
        self.assertEqual(review.status_code, 200)

        confirmation = self._confirm_post({
            'supplier_id': str(self.supplier_id),
            'review_token': self._review_token(review),
        })
        self.assertEqual(confirmation.status_code, 400)
        self.assertNotIn(b'data-supplier-review-recheck', confirmation.data)
        self.assertNotIn(b'name="update_all_for_supplier"', confirmation.data)
        self.db.session.refresh(imported)
        self.assertEqual(imported.title, 'Старое имя malformed')

    def test_json_review_is_pure_and_confirm_updates_all_exact_source_copies(self):
        from models import ImportedProduct

        source, first_copy = self._add_source_copy('inline-selected')
        sibling_copy = ImportedProduct(
            seller_id=self.seller_id,
            supplier_id=self.supplier_id,
            supplier_product_id=source.id,
            supplier_content_revision=1,
            external_id='copy-inline-sibling',
            external_vendor_code='COPY-INLINE-SIBLING',
            source_type='synthetic',
            title='Старое имя sibling',
            supplier_price=99,
            supplier_quantity=1,
            import_status='validated',
        )
        _, unrelated_copy = self._add_source_copy(
            'inline-other-source',
        )
        self.db.session.add(sibling_copy)
        self.db.session.commit()
        unrelated_title = unrelated_copy.title
        before = (first_copy.title, sibling_copy.title, unrelated_copy.title)

        with patch(
            'services.supplier_service.SupplierService.update_supplier_products',
            wraps=__import__(
                'services.supplier_service', fromlist=['SupplierService'],
            ).SupplierService.update_supplier_products,
        ) as updater:
            preview_response = self._post(
                {
                    'supplier_id': str(self.supplier_id),
                    'update_product_ids': [str(source.id)],
                    'return_to': f'/supplier-catalog/{self.supplier_id}/products',
                    'csrf_token': 'synthetic-csrf',
                },
                path='/supplier-catalog/update/review',
                headers={'Accept': 'application/json'},
            )
            updater.assert_not_called()

        self.assertEqual(preview_response.status_code, 200)
        self.assertIn('no-store', preview_response.headers['Cache-Control'])
        preview = preview_response.get_json()
        self.assertTrue(preview['success'])
        self.assertEqual(preview['scope'], 'source_ids')
        self.assertEqual(preview['source_ids'], [str(source.id)])
        self.assertEqual(preview['source_requested'], 1)
        self.assertEqual(preview['requested'], 2)
        self.assertEqual(preview['eligible'], 2)
        self.assertEqual(preview['changed'], 2)
        self.assertFalse(preview['items_truncated'])
        self.assertTrue(preview['review_token'])
        self.assertEqual(
            (first_copy.title, sibling_copy.title, unrelated_copy.title),
            before,
        )

        confirm_response = self._post(
            self._confirm_values([source.id], preview['review_token']),
            path='/supplier-catalog/update/confirm',
            headers={'Accept': 'application/json'},
        )
        self.assertEqual(confirm_response.status_code, 200)
        self.assertIn('no-store', confirm_response.headers['Cache-Control'])
        result = confirm_response.get_json()
        self.assertTrue(result['success'])
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['requested'], 2)
        self.assertEqual(result['updated'], 2)
        self.assertEqual(result['skipped'], 0)
        self.assertEqual(result['errors'], 0)
        self.assertEqual(
            {row['product_id'] for row in result['items']},
            {str(first_copy.id), str(sibling_copy.id)},
        )
        self.assertFalse(result['wb_changed'])
        self.assertFalse(result['ozon_changed'])
        self.db.session.refresh(first_copy)
        self.db.session.refresh(sibling_copy)
        self.db.session.refresh(unrelated_copy)
        self.assertEqual(first_copy.title, source.title)
        self.assertEqual(sibling_copy.title, source.title)
        self.assertEqual(unrelated_copy.title, unrelated_title)

    def test_json_confirm_denies_review_drift_without_partial_write(self):
        source, imported = self._add_source_copy('inline-drift')
        old_title = imported.title
        preview = self._post(
            {
                'supplier_id': str(self.supplier_id),
                'update_product_ids': [str(source.id)],
            },
            path='/supplier-catalog/update/review',
            headers={'Accept': 'application/json'},
        ).get_json()
        self.assertTrue(preview['success'])
        with self.app.app_context():
            changed_source = self.db.session.get(type(source), source.id)
            changed_source.title = 'Изменено после JSON проверки'
            self.db.session.commit()

        response = self._post(
            self._confirm_values([source.id], preview['review_token']),
            path='/supplier-catalog/update/confirm',
            headers={'Accept': 'application/json'},
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.get_json()['code'], 'supplier_update_review_stale',
        )
        self.db.session.refresh(imported)
        self.assertEqual(imported.title, old_title)


if __name__ == '__main__':
    unittest.main()
