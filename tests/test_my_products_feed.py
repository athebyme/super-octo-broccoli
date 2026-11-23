# -*- coding: utf-8 -*-
"""Лента «Моих товаров»: контекст для решения и tenant scope.

Экран массовых операций должен давать ответ «стоит ли выбирать эту строку»:
качество, спрос и фактические цены канала. И не должен показывать чужое.
"""

import json
import os
import unittest
from types import SimpleNamespace
from urllib.parse import urlencode
from unittest.mock import patch


class MyProductsFeedTest(unittest.TestCase):
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
        from models import (
            ImportedProduct, Product, Seller, Supplier, SupplierProduct, User,
        )
        self.ctx = self.app.app_context()
        self.ctx.push()
        # Движок общий на класс (StaticPool), поэтому чистим схему явно перед
        # каждым тестом: иначе строки предыдущего случая ломают уникальность.
        self.db.session.remove()
        self.db.drop_all()
        self.db.create_all()
        self.client = self.app.test_client()

        user = User(username='feed-owner', email='feed@test.local', is_active=True)
        user.set_password('synthetic-password')
        seller = Seller(user=user, company_name='Лента')
        other_user = User(username='feed-alien', email='alien@test.local', is_active=True)
        other_user.set_password('synthetic-password')
        other_seller = Seller(user=other_user, company_name='Чужой')
        supplier = Supplier(name='Опт', code='opt')
        self.db.session.add_all([seller, other_seller, supplier])
        self.db.session.flush()

        wb_product = Product(
            seller_id=seller.id,
            nm_id=880001,
            title='Свеча',
            is_active=True,
            quality_score=41,
            quality_impact=52.0,
            attention_reasons='few_photos,weak_chars',
            wb_views_30d=310,
            wb_orders_30d=2,
            wb_price=1500,
            wb_discounted_price=1290,
            quantity=6,
            photos_json=json.dumps([1, 2]),
        )
        self.db.session.add(wb_product)
        self.db.session.flush()

        source = SupplierProduct(
            supplier_id=supplier.id,
            external_id='sp-1',
            title='Свеча',
            content_revision=5,
        )
        self.db.session.add(source)
        self.db.session.flush()

        published = ImportedProduct(
            seller_id=seller.id,
            supplier_id=supplier.id,
            supplier_product_id=source.id,
            supplier_content_revision=3,
            product_id=wb_product.id,
            external_id='imp-1',
            external_vendor_code='id-7725-1',
            source_type='synthetic',
            title='Свеча ароматическая',
            import_status='imported',
            supplier_price=700,
            supplier_quantity=12,
            photo_urls=json.dumps(['https://cdn.test/a.jpg']),
        )
        draft_row = ImportedProduct(
            seller_id=seller.id,
            external_id='imp-2',
            source_type='synthetic',
            title='Плед',
            import_status='validated',
        )
        foreign = ImportedProduct(
            seller_id=other_seller.id,
            external_id='imp-alien',
            source_type='synthetic',
            title='Чужой товар',
            import_status='validated',
        )
        self.db.session.add_all([published, draft_row, foreign])
        self.db.session.commit()

        self.seller_id = seller.id
        self.other_seller_id = other_seller.id
        self.supplier_id = supplier.id
        self.published_id = published.id
        self.draft_id = draft_row.id
        self.foreign_id = foreign.id
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

    def _auth(self):
        return patch('flask_login.utils._get_user', return_value=self.user)

    def test_feed_carries_quality_demand_and_channel_facts(self):
        with self._auth():
            response = self.client.get(
                '/api/my-products',
                headers={'Accept': 'application/json'},
            )
        self.assertEqual(response.status_code, 200)
        items = {item['id']: item for item in response.get_json()['items']}
        published = items[self.published_id]
        self.assertEqual(published['wb']['quality_score'], 41)
        self.assertIn('few_photos', published['wb']['attention_reasons'])
        self.assertEqual(published['wb']['views_30d'], 310)
        self.assertEqual(published['wb']['orders_30d'], 2)
        self.assertEqual(published['wb']['price'], 1290.0)
        self.assertEqual(published['supplier_price'], 700.0)
        self.assertTrue(published['has_supplier_update'])
        # Непубликованная строка не выдумывает канальных фактов
        self.assertIsNone(items[self.draft_id]['wb'])

    def test_ozon_listing_facts_are_exact_and_same_owner_marketplace_scoped(self):
        from models import (
            Marketplace, MarketplaceListing, MarketplaceProductDraft,
            SellerMarketplaceAccount,
        )

        ozon = Marketplace(name='Ozon', code='ozon')
        wb = Marketplace(name='WB', code='wb')
        self.db.session.add_all([ozon, wb])
        self.db.session.flush()

        owned_ozon = SellerMarketplaceAccount(
            seller_id=self.seller_id,
            marketplace_id=ozon.id,
            external_account_id='ozon-owned',
            label='Мой Ozon',
            is_active=True,
        )
        inactive_ozon = SellerMarketplaceAccount(
            seller_id=self.seller_id,
            marketplace_id=ozon.id,
            external_account_id='ozon-inactive',
            label='Неактивный Ozon',
            is_active=False,
        )
        owned_wb = SellerMarketplaceAccount(
            seller_id=self.seller_id,
            marketplace_id=wb.id,
            external_account_id='wb-owned',
            label='Мой WB',
            is_active=True,
        )
        foreign_ozon = SellerMarketplaceAccount(
            seller_id=self.other_seller_id,
            marketplace_id=ozon.id,
            external_account_id='ozon-foreign',
            label='Чужой Ozon',
            is_active=True,
        )
        foreign_seller_ozon = SellerMarketplaceAccount(
            seller_id=self.other_seller_id,
            marketplace_id=ozon.id,
            external_account_id='ozon-foreign-seller',
            label='Чужой Ozon 2',
            is_active=True,
        )
        self.db.session.add_all([
            owned_ozon, inactive_ozon, owned_wb, foreign_ozon,
            foreign_seller_ozon,
        ])
        self.db.session.flush()

        common = dict(
            seller_id=self.seller_id,
            imported_product_id=self.published_id,
            normalized_status='active',
            link_status='linked',
            sync_fingerprint='synthetic',
        )
        self.db.session.add_all([
            MarketplaceListing(
                **common,
                marketplace_id=ozon.id,
                account_id=owned_ozon.id,
                offer_id='owned-ozon-offer',
                external_product_id='owned-ozon-product',
                is_available=True,
                is_archived=False,
            ),
            MarketplaceListing(
                **common,
                marketplace_id=ozon.id,
                account_id=inactive_ozon.id,
                offer_id='inactive-ozon-offer',
                external_product_id='inactive-ozon-product',
                is_available=False,
                is_archived=True,
            ),
            # A linked WB projection must not be mislabeled as an Ozon row.
            MarketplaceListing(
                **common,
                marketplace_id=wb.id,
                account_id=owned_wb.id,
                offer_id='owned-wb-offer',
                external_product_id='owned-wb-product',
            ),
            # Even a seller_id-matching projection is excluded if its account
            # belongs to another seller.
            MarketplaceListing(
                **common,
                marketplace_id=ozon.id,
                account_id=foreign_ozon.id,
                offer_id='foreign-account-offer',
                external_product_id='foreign-account-product',
            ),
            # Unlinked ambiguity cannot be assigned to this ImportedProduct.
            MarketplaceListing(
                seller_id=self.seller_id,
                marketplace_id=ozon.id,
                account_id=owned_ozon.id,
                offer_id='ambiguous-offer',
                external_product_id='ambiguous-product',
                normalized_status='unknown',
                link_status='ambiguous',
                sync_fingerprint='synthetic',
            ),
            # Rows owned by another seller cannot enter this seller's feed.
            MarketplaceListing(
                seller_id=self.other_seller_id,
                marketplace_id=ozon.id,
                account_id=foreign_seller_ozon.id,
                imported_product_id=self.published_id,
                offer_id='foreign-seller-offer',
                external_product_id='foreign-seller-product',
                sync_fingerprint='synthetic',
            ),
        ])
        self.db.session.add_all([
            MarketplaceProductDraft(
                seller_id=self.seller_id,
                marketplace_id=ozon.id,
                account_id=owned_ozon.id,
                imported_product_id=self.draft_id,
                offer_id='owned-ozon-draft',
                status='draft',
                source_fact_hash='synthetic',
            ),
            MarketplaceProductDraft(
                seller_id=self.seller_id,
                marketplace_id=ozon.id,
                account_id=foreign_ozon.id,
                imported_product_id=self.draft_id,
                offer_id='foreign-account-draft',
                status='draft',
                source_fact_hash='synthetic',
            ),
            MarketplaceProductDraft(
                seller_id=self.seller_id,
                marketplace_id=wb.id,
                account_id=owned_wb.id,
                imported_product_id=self.draft_id,
                offer_id='cross-store-draft',
                status='draft',
                source_fact_hash='synthetic',
            ),
        ])
        self.db.session.commit()

        with self._auth():
            response = self.client.get('/api/my-products')
        self.assertEqual(response.status_code, 200)
        items = {
            row['id']: row for row in response.get_json()['items']
        }
        item = items[self.published_id]
        self.assertEqual(item['wb']['price'], 1290.0)
        self.assertEqual(
            [
                draft['account_id']
                for draft in items[self.draft_id]['ozon']['drafts']
            ],
            [owned_ozon.id],
        )
        listings = item['ozon']['listings']
        self.assertEqual(
            {listing['account_id'] for listing in listings},
            {owned_ozon.id, inactive_ozon.id},
        )
        self.assertTrue(all(
            listing['marketplace_code'] == 'ozon'
            and listing['link_status'] == 'linked'
            for listing in listings
        ))
        active_row = next(
            listing for listing in listings
            if listing['account_id'] == owned_ozon.id
        )
        self.assertEqual(active_row['account_label'], 'Мой Ozon')
        self.assertTrue(active_row['account_is_active'])
        self.assertTrue(active_row['is_available'])
        self.assertFalse(active_row['is_archived'])
        inactive_row = next(
            listing for listing in listings
            if listing['account_id'] == inactive_ozon.id
        )
        self.assertFalse(inactive_row['account_is_active'])
        self.assertFalse(inactive_row['is_available'])
        self.assertTrue(inactive_row['is_archived'])
        self.assertNotIn('Чужой Ozon', json.dumps(item, ensure_ascii=False))

    def _batch_token(self, *, user_id, seller_id, ids):
        from itsdangerous import URLSafeTimedSerializer

        return URLSafeTimedSerializer(
            self.app.secret_key,
            salt='supplier-import-workspace-v1',
        ).dumps({
            'v': 1,
            'user_id': user_id,
            'seller_id': seller_id,
            'ids': ids,
        })

    def test_import_batch_returns_exact_rows_in_receipt_order_and_ignores_filters(self):
        from models import ImportedProduct, SupplierProduct

        source_a = SupplierProduct(
            supplier_id=self.supplier_id, external_id='batch-source-a',
            title='Batch A', content_revision=1,
        )
        source_b = SupplierProduct(
            supplier_id=self.supplier_id, external_id='batch-source-b',
            title='Batch B', content_revision=1,
        )
        self.db.session.add_all([source_a, source_b])
        self.db.session.flush()
        row_a = ImportedProduct(
            seller_id=self.seller_id, supplier_id=self.supplier_id,
            supplier_product_id=source_a.id, external_id='batch-import-a',
            source_type='synthetic', title='Batch A', import_status='validated',
        )
        row_b = ImportedProduct(
            seller_id=self.seller_id, supplier_id=self.supplier_id,
            supplier_product_id=source_b.id, external_id='batch-import-b',
            source_type='synthetic', title='Batch B', import_status='failed',
        )
        self.db.session.add_all([row_a, row_b])
        self.db.session.commit()
        self.assertNotEqual(row_a.id, source_a.id)
        self.assertNotEqual(row_b.id, source_b.id)

        token = self._batch_token(
            user_id=self.user.id,
            seller_id=self.seller_id,
            ids=[row_b.id, row_a.id],
        )
        query = urlencode({
            'import_batch': token,
            'status': 'imported',
            'search': 'does-not-exist',
            'supplier': 'none',
            'brand': 'unmatched',
            'wb_category': 'unmatched',
            'has_photos': 'yes',
            'stock': 'in_stock',
            'updates': '1',
            'sort': 'title',
            'page': '999',
            'per_page': '999',
        })
        with self._auth():
            response = self.client.get('/api/my-products?' + query)

        self.assertEqual(response.status_code, 200)
        self.assertIn('no-store', response.headers['Cache-Control'])
        payload = response.get_json()
        self.assertEqual([item['id'] for item in payload['items']], [row_b.id, row_a.id])
        self.assertEqual(payload['import_batch_count'], 2)
        self.assertEqual(payload['pagination'], {
            'page': 1,
            'per_page': 200,
            'pages': 1,
            'total': 2,
            'has_next': False,
        })

    def test_import_batch_rejects_tampered_expired_foreign_and_missing_sets(self):
        from models import ImportedProduct, User
        from routes.suppliers import _issue_supplier_import_batch_token

        valid = _issue_supplier_import_batch_token(
            user_id=self.user.id,
            seller_id=self.seller_id,
            imported_product_ids=[self.draft_id],
        )
        token_body, token_signature = valid.rsplit('.', 1)
        tampered_signature = ('a' if token_signature[0] != 'a' else 'b') + token_signature[1:]
        tampered = f'{token_body}.{tampered_signature}'
        foreign_user = User.query.filter_by(username='feed-alien').one()
        foreign = _issue_supplier_import_batch_token(
            user_id=foreign_user.id,
            seller_id=self.other_seller_id,
            imported_product_ids=[ImportedProduct.query.filter_by(
                seller_id=self.other_seller_id,
            ).one().id],
        )
        missing = _issue_supplier_import_batch_token(
            user_id=self.user.id,
            seller_id=self.seller_id,
            imported_product_ids=[self.published_id],
        )
        ImportedProduct.query.filter_by(id=self.published_id).delete()
        self.db.session.commit()

        cases = [
            (tampered, 400),
            (foreign, 404),
            (missing, 404),
        ]
        for token, expected_status in cases:
            with self.subTest(expected_status=expected_status), self._auth():
                response = self.client.get(
                    '/api/my-products?' + urlencode({'import_batch': token}),
                )
            self.assertEqual(response.status_code, expected_status)
            self.assertIn('no-store', response.headers['Cache-Control'])
            self.assertNotIn('items', response.get_json())

        with patch('routes.suppliers.SUPPLIER_IMPORT_BATCH_MAX_AGE_SECONDS', -1):
            with self._auth():
                expired = self.client.get(
                    '/api/my-products?' + urlencode({'import_batch': valid}),
                )
        self.assertEqual(expired.status_code, 410)
        self.assertIn('no-store', expired.headers['Cache-Control'])

        # Duplicate tokens are rejected rather than widening or falling back.
        with self._auth():
            duplicate = self.client.get(
                '/api/my-products?' + urlencode([
                    ('import_batch', valid), ('import_batch', valid),
                ]),
            )
        self.assertEqual(duplicate.status_code, 400)
        self.assertIn('no-store', duplicate.headers['Cache-Control'])

    def test_beta_page_passes_bounded_import_batch_and_disables_caching(self):
        from routes.suppliers import _issue_supplier_import_batch_token

        token = _issue_supplier_import_batch_token(
            user_id=self.user.id,
            seller_id=self.seller_id,
            imported_product_ids=[self.draft_id],
        )
        with self._auth(), patch(
            'routes.suppliers.render_template', return_value='rendered-page',
        ) as render:
            response = self.client.get(
                '/my-products/beta?' + urlencode({'import_batch': token}),
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn('no-store', response.headers['Cache-Control'])
        self.assertEqual(render.call_args.kwargs['import_batch'], token)

        oversized = 'x' * (8 * 1024 + 1)
        with self._auth(), patch(
            'routes.suppliers.render_template', return_value='rendered-page',
        ) as render:
            response = self.client.get(
                '/my-products/beta?' + urlencode({'import_batch': oversized}),
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn('no-store', response.headers['Cache-Control'])
        self.assertEqual(render.call_args.kwargs['import_batch'], '')

        with self._auth(), patch(
            'routes.suppliers.render_template', return_value='rendered-page',
        ) as render:
            self.client.get('/my-products/beta')
        self.assertIsNone(render.call_args.kwargs['import_batch'])

    def test_feed_is_tenant_scoped(self):
        with self._auth():
            response = self.client.get(
                '/api/my-products',
                headers={'Accept': 'application/json'},
            )
        titles = [item['title'] for item in response.get_json()['items']]
        self.assertNotIn('Чужой товар', titles)

    def test_search_matches_cyrillic_in_any_case(self):
        with self._auth():
            lower = self.client.get('/api/my-products?search=свеча')
            upper = self.client.get('/api/my-products?search=СВЕЧА')
        self.assertEqual(len(lower.get_json()['items']), 1)
        self.assertEqual(len(upper.get_json()['items']), 1)

    def test_facets_count_statuses_and_supplier_updates(self):
        with self._auth():
            response = self.client.get('/api/my-products/facets')
        statuses = response.get_json()['statuses']
        self.assertEqual(statuses['all'], 2)
        self.assertEqual(statuses['imported'], 1)
        self.assertEqual(statuses['validated'], 1)
        self.assertEqual(statuses['supplier_updates'], 1)

    def test_per_page_is_bounded(self):
        with self._auth():
            response = self.client.get('/api/my-products?per_page=500')
        self.assertEqual(response.status_code, 400)

    def test_selection_review_returns_exact_owned_rows_in_request_order(self):
        with self._auth():
            response = self.client.post(
                '/api/my-products/selection',
                json={'ids': [self.draft_id, self.published_id]},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json().keys(), {'success', 'items'})
        payload = response.get_json()
        self.assertTrue(payload['success'])
        self.assertEqual(
            [item['id'] for item in payload['items']],
            [self.draft_id, self.published_id],
        )
        self.assertIn('private', response.headers['Cache-Control'])
        self.assertIn('no-store', response.headers['Cache-Control'])
        self.assertEqual(response.headers['Pragma'], 'no-cache')
        self.assertEqual(response.headers['Expires'], '0')
        self.assertFalse(self.db.session.new)
        self.assertFalse(self.db.session.dirty)
        self.assertFalse(self.db.session.deleted)

    def test_selection_review_rejects_invalid_sets_as_a_whole(self):
        cases = [
            {},
            {'ids': []},
            {'ids': [True]},
            {'ids': [1.0]},
            {'ids': [str(self.draft_id)]},
            {'ids': [self.draft_id, self.draft_id]},
            {'ids': [(1 << 53)]},
            {'ids': list(range(1, 202))},
            {'ids': [self.draft_id], 'extra': True},
        ]
        with self._auth():
            responses = [
                self.client.post('/api/my-products/selection', json=body)
                for body in cases
            ]
        for response in responses:
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.get_json(), {
                'success': False,
                'error': 'Выбранный список недействителен',
            })
            self.assertIn('private', response.headers['Cache-Control'])
            self.assertIn('no-store', response.headers['Cache-Control'])

        oversized = b'{"ids":[' + b'1,' * 9000 + b'1]}'
        with self._auth():
            response = self.client.post(
                '/api/my-products/selection',
                data=oversized,
                content_type='application/json',
            )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.get_json()['error'], 'Выбранный список недействителен',
        )

    def test_selection_review_coalesces_missing_and_foreign_rows(self):
        missing_id = 987654321
        with self._auth():
            foreign = self.client.post(
                '/api/my-products/selection',
                json={'ids': [self.foreign_id]},
            )
            mixed = self.client.post(
                '/api/my-products/selection',
                json={'ids': [self.draft_id, self.foreign_id]},
            )
            missing = self.client.post(
                '/api/my-products/selection',
                json={'ids': [missing_id]},
            )

        self.assertEqual(
            [foreign.status_code, mixed.status_code, missing.status_code],
            [404, 404, 404],
        )
        self.assertEqual(foreign.get_json(), mixed.get_json())
        self.assertEqual(foreign.get_json(), missing.get_json())
        self.assertEqual(foreign.get_json(), {
            'success': False,
            'error': 'Выбранный список недоступен',
        })

    def test_selection_review_accepts_maximum_safe_javascript_id(self):
        from models import ImportedProduct

        max_safe_id = (1 << 53) - 1
        row = ImportedProduct(
            id=max_safe_id,
            seller_id=self.seller_id,
            external_id='imp-max-safe-id',
            source_type='synthetic',
            title='Safe integer boundary',
            import_status='validated',
        )
        self.db.session.add(row)
        self.db.session.commit()

        with self._auth():
            accepted = self.client.post(
                '/api/my-products/selection', json={'ids': [max_safe_id]},
            )
            rejected = self.client.post(
                '/api/my-products/selection', json={'ids': [max_safe_id + 1]},
            )

        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(accepted.get_json()['items'][0]['id'], max_safe_id)
        self.assertEqual(rejected.status_code, 400)
        self.assertEqual(
            rejected.get_json()['error'], 'Выбранный список недействителен',
        )

    def test_selection_review_requires_csrf_and_never_caches_framework_errors(self):
        previous = self.app.config.get('WTF_CSRF_ENABLED')
        self.app.config['WTF_CSRF_ENABLED'] = True
        try:
            with self._auth():
                response = self.client.post(
                    '/api/my-products/selection',
                    json={'ids': [self.draft_id]},
                )
        finally:
            self.app.config['WTF_CSRF_ENABLED'] = previous

        self.assertEqual(response.status_code, 400)
        self.assertIn('private', response.headers['Cache-Control'])
        self.assertIn('no-store', response.headers['Cache-Control'])


if __name__ == '__main__':
    unittest.main()


class MyProductsBetaPageTest(MyProductsFeedTest):
    """Страница массовых операций рендерится и знает про каналы продавца."""

    def test_beta_bootstrap_keeps_disconnected_ozon_account_for_existing_flow(self):
        from models import Marketplace, SellerMarketplaceAccount

        marketplace = Marketplace(name='Ozon', code='ozon')
        self.db.session.add(marketplace)
        self.db.session.flush()
        account = SellerMarketplaceAccount(
            seller_id=self.seller_id,
            marketplace_id=marketplace.id,
            external_account_id='ozon-disconnected',
            label='Отключённый магазин',
            connection_status='disconnected',
        )
        self.db.session.add(account)
        self.db.session.commit()

        previous = self.app.config.get('MARKETPLACE_OZON_ENABLED')
        self.app.config['MARKETPLACE_OZON_ENABLED'] = True
        try:
            with self._auth():
                response = self.client.get('/my-products/beta')
        finally:
            if previous is None:
                self.app.config.pop('MARKETPLACE_OZON_ENABLED', None)
            else:
                self.app.config['MARKETPLACE_OZON_ENABLED'] = previous

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        payload = json.loads(
            body.split('id="mp-bootstrap">', 1)[1].split('</script>', 1)[0]
        )
        self.assertEqual(payload['ozonAccounts'], [{
            'id': account.id,
            'label': 'Отключённый магазин',
            'connection_status': 'disconnected',
        }])

    def test_beta_page_renders_with_bootstrap(self):
        with self._auth():
            response = self.client.get('/my-products/beta')
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn('my-products-app', body)
        self.assertIn('sh-vue-kit.js', body)
        self.assertIn('mp-bootstrap', body)

    def test_classic_page_links_to_beta(self):
        with self._auth():
            response = self.client.get('/my-products')
        self.assertEqual(response.status_code, 200)
        self.assertIn('/my-products/beta', response.get_data(as_text=True))
