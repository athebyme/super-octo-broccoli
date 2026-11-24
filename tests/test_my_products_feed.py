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

    def test_supplier_updates_include_only_exact_coherent_equal_revision_category_drift(self):
        from models import ImportedProduct, SupplierProduct

        def add_category_copy(
            suffix, *, copied_category, source_category='Новая категория',
            source_raw_category='Новая категория', source_revision=5,
            copied_revision=5, source_raw=None, copied_raw=None,
            copied_source_type=None, copied_sku=None, copied_vendor_code=None,
            source_vendor_code=None,
        ):
            sku = f'category-drift-{suffix}'
            vendor_code = source_vendor_code or f'VENDOR-{suffix}'
            supplier_raw = (
                json.dumps({'category': source_raw_category}, ensure_ascii=False)
                if source_raw is None else source_raw
            )
            copied_snapshot = (
                {'category': copied_category}
                if copied_raw is None else copied_raw
            )
            copied_snapshot_raw = (
                copied_snapshot
                if isinstance(copied_snapshot, str)
                else json.dumps(copied_snapshot, ensure_ascii=False)
            )
            source = SupplierProduct(
                supplier_id=self.supplier_id,
                external_id=sku,
                vendor_code=vendor_code,
                title=f'Источник {suffix}',
                category=source_category,
                original_data_json=supplier_raw,
                content_revision=source_revision,
            )
            self.db.session.add(source)
            self.db.session.flush()
            copied = ImportedProduct(
                seller_id=self.seller_id,
                supplier_id=self.supplier_id,
                supplier_product_id=source.id,
                supplier_content_revision=copied_revision,
                external_id=copied_sku or sku,
                external_vendor_code=copied_vendor_code or vendor_code,
                source_type=copied_source_type or 'opt',
                title=f'Копия {suffix}',
                category=copied_category,
                original_data=copied_snapshot_raw,
                import_status='validated',
            )
            self.db.session.add(copied)
            return copied

        exact_copy = add_category_copy(
            'exact', copied_category='Старая категория',
        )
        blank_copy = add_category_copy(
            'blank', copied_category='', copied_raw={},
        )
        manual_override = add_category_copy(
            'manual', copied_category='Ручная категория',
            copied_raw={'category': 'Старая категория'},
        )
        malformed_copy = add_category_copy(
            'malformed', copied_category='Старая категория',
            copied_raw='{broken-json',
        )
        malformed_source = add_category_copy(
            'bad-source-raw', copied_category='Старая категория',
            source_raw='{broken-json',
        )
        vendor_mismatch = add_category_copy(
            'vendor-mismatch', copied_category='Старая категория',
            copied_vendor_code='SELLER-CHANGED-VENDOR',
        )
        sku_mismatch = add_category_copy(
            'sku-mismatch', copied_category='Старая категория',
            copied_sku='seller-changed-sku',
        )
        type_mismatch = add_category_copy(
            'type-mismatch', copied_category='Старая категория',
            copied_source_type='other',
        )
        self.db.session.commit()

        with self._auth():
            updates = self.client.get('/api/my-products?updates=1')
            facets = self.client.get('/api/my-products/facets')

        self.assertEqual(updates.status_code, 200)
        self.assertEqual(facets.status_code, 200)
        update_items = updates.get_json()['items']
        update_ids = {item['id'] for item in update_items}
        expected_ids = {
            self.published_id, exact_copy.id, blank_copy.id,
        }
        self.assertEqual(update_ids, expected_ids)
        self.assertEqual(
            facets.get_json()['statuses']['supplier_updates'],
            len(expected_ids),
        )

        with self._auth():
            full_feed = self.client.get('/api/my-products')
        full_items = {
            item['id']: item for item in full_feed.get_json()['items']
        }
        for row_id in expected_ids:
            self.assertTrue(full_items[row_id]['has_supplier_update'])
        for row in (
            manual_override, malformed_copy, malformed_source,
            vendor_mismatch, sku_mismatch, type_mismatch,
        ):
            self.assertFalse(full_items[row.id]['has_supplier_update'])

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

    def test_marketplace_filtering_scopes_exact_provider_states_before_paging(self):
        from models import (
            ImportedProduct, Marketplace, MarketplaceListing,
            MarketplaceProductDraft, Product, SellerMarketplaceAccount,
        )

        ozon = Marketplace(name='Ozon', code='ozon')
        wb = Marketplace(name='WB', code='wb')
        self.db.session.add_all([ozon, wb])
        self.db.session.flush()
        account = SellerMarketplaceAccount(
            seller_id=self.seller_id, marketplace_id=ozon.id,
            external_account_id='filter-ozon-own', label='Ozon', is_active=True,
        )
        inactive_account = SellerMarketplaceAccount(
            seller_id=self.seller_id, marketplace_id=ozon.id,
            external_account_id='filter-ozon-inactive',
            label='Inactive Ozon', is_active=False,
        )
        wrong_owner_account = SellerMarketplaceAccount(
            seller_id=self.other_seller_id, marketplace_id=ozon.id,
            external_account_id='filter-ozon-foreign', label='Foreign Ozon',
            is_active=True,
        )
        self.db.session.add_all([
            account, inactive_account, wrong_owner_account,
        ])
        self.db.session.flush()

        def imported(external_id, status='pending'):
            row = ImportedProduct(
                seller_id=self.seller_id, external_id=external_id,
                source_type='synthetic', title=external_id,
                import_status=status,
            )
            self.db.session.add(row)
            self.db.session.flush()
            return row

        active = imported('ozon-active')
        inactive_account_active_listing = imported('ozon-inactive-account')
        unknown_archived = imported('ozon-unknown-archived')
        archived_moderation = imported('ozon-archived-moderation')
        archived_error = imported('ozon-archived-error')
        unavailable = imported('ozon-unavailable')
        moderation = imported('ozon-moderation')
        errored = imported('ozon-error')
        draft_preparation = imported('ozon-draft-preparation')
        draft_blocked = imported('ozon-draft-blocked')
        wrong_account = imported('ozon-wrong-account')
        wb_pending = imported('wb-pending-sync', 'imported_pending_sync')
        wb_failed = imported('wb-failed', 'failed')
        wb_historical = imported('wb-historical', 'imported')
        foreign_product_source = imported('wb-foreign-product', 'imported')
        imported_without_product = imported('wb-imported-unlinked', 'imported')

        historical_product = Product(
            seller_id=self.seller_id, nm_id=880098,
            title='Inactive owned Product', is_active=False,
        )
        foreign_product = Product(
            seller_id=self.other_seller_id, nm_id=880099,
            title='Foreign Product', is_active=True,
        )
        self.db.session.add_all([historical_product, foreign_product])
        self.db.session.flush()
        wb_historical.product_id = historical_product.id
        foreign_product_source.product_id = foreign_product.id
        imported_without_product.wb_nm_id = 880100

        def listing(row, status, *, available=True, archived=False, owner=account):
            self.db.session.add(MarketplaceListing(
                seller_id=self.seller_id, marketplace_id=ozon.id,
                account_id=owner.id, imported_product_id=row.id,
                offer_id='offer-' + row.external_id,
                external_product_id='product-' + row.external_id,
                normalized_status=status, is_available=available,
                is_archived=archived, sync_fingerprint='filter-fixture',
            ))

        listing(active, 'active')
        listing(inactive_account_active_listing, 'active', owner=inactive_account)
        listing(unknown_archived, 'unknown', available=False, archived=True)
        listing(archived_moderation, 'moderation', archived=True)
        listing(archived_error, 'error', archived=True)
        listing(unavailable, 'active', available=False)
        listing(moderation, 'moderation')
        listing(errored, 'error')
        listing(wrong_account, 'active', owner=wrong_owner_account)
        self.db.session.add_all([
            MarketplaceProductDraft(
                seller_id=self.seller_id, marketplace_id=ozon.id,
                account_id=account.id, imported_product_id=draft_preparation.id,
                offer_id='ozon-draft-prep', status='needs_category',
                source_fact_hash='filter-fixture',
            ),
            MarketplaceProductDraft(
                seller_id=self.seller_id, marketplace_id=ozon.id,
                account_id=account.id, imported_product_id=draft_blocked.id,
                offer_id='ozon-draft-blocked', status='blocked',
                source_fact_hash='filter-fixture',
            ),
        ])
        self.db.session.commit()

        from sqlalchemy import text
        from services.my_products_feed import MyProductsFeed

        for predicate in (
            MyProductsFeed._ozon_association(seller_id=self.seller_id),
            MyProductsFeed._ozon_state(
                seller_id=self.seller_id, state='active',
            ),
        ):
            statement = str(ImportedProduct.query.filter(
                ImportedProduct.seller_id == self.seller_id,
                predicate,
            ).statement.compile(
                dialect=self.db.engine.dialect,
                compile_kwargs={'literal_binds': True},
            ))
            plan = self.db.session.execute(
                text('EXPLAIN QUERY PLAN ' + statement)
            ).all()
            details = '\n'.join(str(row[-1]) for row in plan).upper()
            self.assertNotIn('CORRELATED', details, details)
            self.assertIn('LIST SUBQUERY', details, details)

        previous = self.app.config.get('MARKETPLACE_OZON_ENABLED')
        self.app.config['MARKETPLACE_OZON_ENABLED'] = True
        try:
            with self._auth():
                baseline = self.client.get('/api/my-products').get_json()
                empty_filters = self.client.get(
                    '/api/my-products?marketplace=&marketplace_state=',
                ).get_json()
                ozon_all_response = self.client.get(
                    '/api/my-products?marketplace=ozon',
                )
                ozon_all = ozon_all_response.get_json()
                ozon_active = self.client.get(
                    '/api/my-products?marketplace=ozon&marketplace_state=active',
                ).get_json()
                ozon_preparation = self.client.get(
                    '/api/my-products?marketplace=ozon&marketplace_state=preparation',
                ).get_json()
                ozon_attention = self.client.get(
                    '/api/my-products?marketplace=ozon&marketplace_state=attention',
                ).get_json()
                active_union = self.client.get(
                    '/api/my-products?marketplace_state=active',
                ).get_json()
                wb_active = self.client.get(
                    '/api/my-products?marketplace=wb&marketplace_state=active',
                ).get_json()
                wb_page = self.client.get(
                    '/api/my-products?marketplace=wb&sort=title&per_page=1&page=2',
                ).get_json()
                wb_page_repeat = self.client.get(
                    '/api/my-products?marketplace=wb&sort=title&per_page=1&page=2',
                ).get_json()
                legacy_match = self.client.get(
                    '/api/my-products?marketplace=wb&status=validated&search=Плед',
                ).get_json()
                legacy_miss = self.client.get(
                    '/api/my-products?marketplace=wb&status=validated&search=Обувь',
                ).get_json()
        finally:
            if previous is None:
                self.app.config.pop('MARKETPLACE_OZON_ENABLED', None)
            else:
                self.app.config['MARKETPLACE_OZON_ENABLED'] = previous

        def ids(payload):
            return [row['id'] for row in payload['items']]

        self.assertEqual(empty_filters, baseline)
        cache_directives = {
            part.strip()
            for part in ozon_all_response.headers['Cache-Control'].split(',')
        }
        self.assertTrue({'private', 'no-store'}.issubset(cache_directives))
        self.assertEqual(ozon_all_response.headers['Pragma'], 'no-cache')
        self.assertEqual(ozon_all['pagination']['total'], 10)
        self.assertCountEqual(ids(ozon_all), [
            active.id, inactive_account_active_listing.id,
            unknown_archived.id, archived_moderation.id, archived_error.id,
            unavailable.id, moderation.id, errored.id, draft_preparation.id,
            draft_blocked.id,
        ])
        self.assertNotIn(wrong_account.id, ids(ozon_all))
        self.assertEqual(ids(ozon_active), [active.id])
        self.assertCountEqual(
            ids(ozon_preparation), [moderation.id, draft_preparation.id],
        )
        self.assertCountEqual(ids(ozon_attention), [errored.id, draft_blocked.id])
        self.assertNotIn(archived_moderation.id, ids(ozon_preparation))
        self.assertNotIn(archived_error.id, ids(ozon_attention))
        self.assertCountEqual(ids(active_union), [self.published_id, active.id])

        self.assertEqual(wb_page['pagination']['total'], 5)
        self.assertEqual(wb_page['pagination']['pages'], 5)
        self.assertTrue(wb_page['pagination']['has_next'])
        self.assertEqual(ids(wb_page), ids(wb_page_repeat))
        self.assertEqual(ids(legacy_match), [self.draft_id])
        self.assertEqual(legacy_miss['pagination']['total'], 0)
        self.assertEqual(ids(wb_active), [self.published_id])
        with self._auth():
            wb_all = self.client.get('/api/my-products?marketplace=wb').get_json()
        self.assertIn(wb_historical.id, ids(wb_all))
        self.assertNotIn(foreign_product_source.id, ids(wb_all))
        self.assertNotIn(imported_without_product.id, ids(wb_all))

    def test_marketplace_filters_reject_bad_values_and_respect_ozon_flag(self):
        previous = self.app.config.get('MARKETPLACE_OZON_ENABLED')
        self.app.config['MARKETPLACE_OZON_ENABLED'] = False
        try:
            with self._auth():
                invalid_marketplace = self.client.get(
                    '/api/my-products?marketplace=amazon',
                )
                invalid_state = self.client.get(
                    '/api/my-products?marketplace_state=live',
                )
                duplicate = self.client.get(
                    '/api/my-products?marketplace=wb&marketplace=ozon',
                )
                duplicate_state = self.client.get(
                    '/api/my-products?marketplace_state=active&marketplace_state=attention',
                )
                disabled_ozon = self.client.get(
                    '/api/my-products?marketplace=ozon',
                )
                wb_state_only = self.client.get(
                    '/api/my-products?marketplace_state=active',
                )
        finally:
            if previous is None:
                self.app.config.pop('MARKETPLACE_OZON_ENABLED', None)
            else:
                self.app.config['MARKETPLACE_OZON_ENABLED'] = previous

        self.assertEqual(invalid_marketplace.status_code, 400)
        self.assertEqual(invalid_state.status_code, 400)
        self.assertEqual(duplicate.status_code, 400)
        self.assertEqual(duplicate_state.status_code, 400)
        self.assertEqual(
            invalid_marketplace.get_json()['error'], 'Неизвестная площадка',
        )
        self.assertEqual(
            invalid_state.get_json()['error'], 'Неизвестный статус карточек',
        )
        self.assertEqual(
            duplicate.get_json()['error'], 'Площадка указана несколько раз',
        )
        self.assertEqual(
            duplicate_state.get_json()['error'],
            'Статус карточек указан несколько раз',
        )
        self.assertEqual(disabled_ozon.status_code, 404)
        for response in (invalid_marketplace, invalid_state, disabled_ozon):
            directives = {
                part.strip()
                for part in response.headers['Cache-Control'].split(',')
            }
            self.assertTrue({'private', 'no-store'}.issubset(directives))
        self.assertEqual(
            [row['id'] for row in wb_state_only.get_json()['items']],
            [self.published_id],
        )

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

    def test_catalog_category_filter_uses_the_display_fallback_and_missing(self):
        from models import ImportedProduct

        published = ImportedProduct.query.filter_by(id=self.published_id).one()
        published.mapped_wb_category = 'WB / Свечи'
        published.category = 'Исходная / Свечи'
        draft = ImportedProduct.query.filter_by(id=self.draft_id).one()
        draft.mapped_wb_category = ''
        draft.category = 'Исходная / Пледы'
        missing = ImportedProduct(
            seller_id=self.seller_id,
            external_id='imp-no-category',
            source_type='synthetic',
            title='Без категории',
            import_status='pending',
            mapped_wb_category='',
            category='',
        )
        foreign = ImportedProduct.query.filter_by(id=self.foreign_id).one()
        foreign.mapped_wb_category = 'WB / Свечи'
        self.db.session.add(missing)
        self.db.session.commit()

        with self._auth():
            mapped = self.client.get('/api/my-products?' + urlencode({
                'catalog_category': 'label:WB / Свечи',
            }))
            source_fallback = self.client.get('/api/my-products?' + urlencode({
                'catalog_category': 'label:Исходная / Пледы',
            }))
            uncategorized = self.client.get(
                '/api/my-products?catalog_category=missing',
            )
            legacy_wb_category = self.client.get('/api/my-products?' + urlencode({
                'wb_category': 'WB / Свечи',
            }))

        self.assertEqual(mapped.status_code, 200)
        self.assertEqual(
            [item['id'] for item in mapped.get_json()['items']],
            [self.published_id],
        )
        self.assertEqual(
            mapped.get_json()['items'][0]['category'], 'WB / Свечи',
        )
        self.assertEqual(
            [item['id'] for item in source_fallback.get_json()['items']],
            [self.draft_id],
        )
        self.assertEqual(
            source_fallback.get_json()['items'][0]['category'],
            'Исходная / Пледы',
        )
        self.assertEqual(
            [item['id'] for item in uncategorized.get_json()['items']],
            [missing.id],
        )
        self.assertEqual(
            [item['id'] for item in legacy_wb_category.get_json()['items']],
            [self.published_id],
        )
        self.assertIn('private', mapped.headers['Cache-Control'])
        self.assertIn('no-store', mapped.headers['Cache-Control'])

    def test_catalog_category_filter_rejects_malformed_duplicate_and_control_tokens(self):
        malformed = [
            'unknown',
            'label:',
            'label:' + ('x' * 201),
            'label:category\u0000name',
        ]
        with self._auth():
            responses = [
                self.client.get('/api/my-products?' + urlencode({
                    'catalog_category': value,
                }))
                for value in malformed
            ]
            duplicate = self.client.get('/api/my-products?' + urlencode([
                ('catalog_category', 'label:One'),
                ('catalog_category', 'label:Two'),
            ]))

        for response in responses + [duplicate]:
            self.assertEqual(response.status_code, 400)
            self.assertTrue(response.get_json()['error'])
            self.assertIn('private', response.headers['Cache-Control'])
            self.assertIn('no-store', response.headers['Cache-Control'])

    def test_catalog_category_options_are_paged_and_tenant_scoped(self):
        from models import ImportedProduct

        self.db.session.add_all([
            ImportedProduct(
                seller_id=self.seller_id,
                external_id=f'category-option-{index:03d}',
                source_type='synthetic',
                title=f'Category {index:03d}',
                import_status='pending',
                mapped_wb_category=f'Category {index:03d}',
            )
            for index in range(101)
        ])
        foreign = ImportedProduct.query.filter_by(id=self.foreign_id).one()
        foreign.mapped_wb_category = 'Foreign-only category'
        self.db.session.commit()

        with self._auth():
            first = self.client.get('/api/my-products/categories')
            second = self.client.get('/api/my-products/categories?page=2')
            beyond = self.client.get('/api/my-products/categories?page=3')

        first_payload = first.get_json()
        second_payload = second.get_json()
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(beyond.status_code, 200)
        self.assertTrue(first_payload['success'])
        self.assertEqual(first_payload['page'], 1)
        self.assertTrue(first_payload['has_next'])
        self.assertTrue(first_payload['has_uncategorized'])
        self.assertEqual(len(first_payload['options']), 100)
        self.assertEqual(first_payload['options'][0], {
            'key': 'label:Category 000',
            'label': 'Category 000',
        })
        self.assertEqual(second_payload['page'], 2)
        self.assertFalse(second_payload['has_next'])
        self.assertEqual(second_payload['options'], [{
            'key': 'label:Category 100',
            'label': 'Category 100',
        }])
        self.assertFalse(beyond.get_json()['has_next'])
        self.assertEqual(beyond.get_json()['options'], [])
        self.assertNotIn(
            'Foreign-only category',
            json.dumps([first_payload, second_payload], ensure_ascii=False),
        )
        for response in (first, second, beyond):
            self.assertIn('private', response.headers['Cache-Control'])
            self.assertIn('no-store', response.headers['Cache-Control'])
            self.assertEqual(response.headers['Pragma'], 'no-cache')
            self.assertEqual(response.headers['Expires'], '0')

    def test_catalog_category_options_reject_bad_and_duplicate_pages(self):
        invalid_queries = [
            'page=0',
            'page=1.5',
            'page=99999999999999999',
            'page=1&page=2',
        ]
        with self._auth():
            responses = [
                self.client.get('/api/my-products/categories?' + query)
                for query in invalid_queries
            ]

        for response in responses:
            self.assertEqual(response.status_code, 400)
            self.assertTrue(response.get_json()['error'])
            self.assertIn('private', response.headers['Cache-Control'])
            self.assertIn('no-store', response.headers['Cache-Control'])

    def test_catalog_category_options_skip_unsupported_persisted_labels(self):
        from models import ImportedProduct

        self.db.session.add_all([
            ImportedProduct(
                seller_id=self.seller_id,
                external_id=external_id,
                source_type='synthetic',
                title=external_id,
                import_status='pending',
                category=label,
            )
            for external_id, label in (
                ('category-valid', 'Сохраняемая'),
                ('category-too-long', 'x' * 201),
                ('category-control', 'bad\x01label'),
                ('category-null-control', 'bad\x00label'),
            )
        ])
        self.db.session.commit()

        with self._auth():
            response = self.client.get('/api/my-products/categories')
            too_long_filter = self.client.get('/api/my-products?' + urlencode({
                'catalog_category': 'label:' + ('x' * 201),
            }))
            control_filter = self.client.get('/api/my-products?' + urlencode({
                'catalog_category': 'label:bad\x01label',
            }))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['options'], [{
            'key': 'label:Сохраняемая',
            'label': 'Сохраняемая',
        }])
        self.assertFalse(response.get_json()['has_next'])
        for rejected in (too_long_filter, control_filter):
            self.assertEqual(rejected.status_code, 400)
            self.assertIn('private', rejected.headers['Cache-Control'])
            self.assertIn('no-store', rejected.headers['Cache-Control'])

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
