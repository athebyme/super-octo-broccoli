"""Material revisions produced by the normal supplier CSV sync path."""

import copy
import json
import time
import unittest
from contextlib import contextmanager
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

from flask import Flask

from models import (
    ImportedProduct,
    Marketplace,
    MarketplaceListing,
    MarketplaceListingSnapshot,
    MarketplaceOperation,
    Product,
    Seller,
    SellerMarketplaceAccount,
    SellerSupplier,
    Supplier,
    SupplierProduct,
    User,
    db,
)
from services.smart_product_parser import SmartParseResult, SmartProductParser
from services.supplier_service import (
    SupplierCSVParser,
    SupplierService,
    _copy_to_imported_product,
    _supplier_catalog_sync_lock,
    supplier_catalog_config_digest,
)
from services.supplier_price_sync import sync_supplier_price_feed


class SupplierFeedContentRevisionTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(
            TESTING=True,
            SECRET_KEY='supplier-feed-revision-test',
            SQLALCHEMY_DATABASE_URI='sqlite://',
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
        )
        db.init_app(self.app)
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()

        user = User(username='feed-revision', email='feed-revision@test.local')
        user.set_password('synthetic-password')
        seller = Seller(user=user, company_name='Revision fixture')
        supplier = Supplier(
            name='Fixture supplier',
            code='fixture-revision',
            csv_source_url='fixture://catalog',
        )
        db.session.add_all([seller, supplier])
        db.session.flush()
        db.session.add(SellerSupplier(
            seller_id=seller.id,
            supplier_id=supplier.id,
            is_active=True,
        ))
        db.session.commit()
        self.seller_id = seller.id
        self.supplier_id = supplier.id
        self.user_id = user.id

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    @staticmethod
    def _row(**overrides):
        row = {
            'external_id': 'fixture-1',
            'vendor_code': 'SKU-1',
            'title': 'Initial item',
            'description': 'Initial description',
            'brand': 'Fixture brand',
            'category': 'Household',
            'all_categories': ['Household'],
            'barcodes': ['4600000000001'],
            'country': 'Россия',
            'characteristics': {'Material': 'Cotton'},
            'colors': [],
            'materials': [],
            'sizes_raw': '',
            'dimensions': {'Width': '10'},
            'photo_urls': [{'original': 'https://supplier.invalid/first.jpg'}],
            'video_url': None,
            'supplier_price': 100.0,
            'supplier_quantity': 2,
            'recommended_retail_price': 150.0,
        }
        row.update(overrides)
        return row

    @contextmanager
    def _sync_patches(self, row, *, parser_failure=False):
        def parse_product(_parser, product_data, existing_product=None):
            result = SmartParseResult(readiness_score=72.0)
            if 'blue' in (product_data.get('title') or '').casefold():
                result.extracted_colors = ['Blue']
            return result

        patches = [
            patch.object(
                SupplierCSVParser,
                'fetch_csv_raw',
                return_value=b'synthetic',
            ),
            patch.object(
                SupplierCSVParser,
                'preflight_raw',
                return_value=(
                    [copy.deepcopy(row)],
                    SimpleNamespace(is_valid=True, warnings=[], errors=[]),
                ),
            ),
            patch(
                'services.smart_category_mapper.SmartCategoryMapper.map_category',
                return_value=(None, None, 0.0),
            ),
            patch(
                'services.parsing_confidence.ParsingConfidenceScorer.score_product',
                return_value=0.72,
            ),
            patch(
                'services.photo_cache.bulk_download_supplier_photos',
                return_value={
                    'total_photos': 0,
                    'already_cached': 0,
                    'queued': 0,
                },
            ),
        ]
        if parser_failure:
            patches.append(patch.object(
                SmartProductParser,
                'parse_and_apply_bulk',
                side_effect=RuntimeError('synthetic parser failure'),
            ))
        else:
            patches.append(patch.object(
                SmartProductParser,
                'parse_product',
                new=parse_product,
            ))

        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            yield

    def _sync(self, row, *, parser_failure=False):
        with self._sync_patches(row, parser_failure=parser_failure):
            return SupplierService.sync_from_csv(self.supplier_id)

    @staticmethod
    def _legacy_row(external_id='123', title='Legacy item'):
        row = [''] * 18
        row[0] = external_id
        row[1] = 'SKU'
        row[2] = title
        return ';'.join(row)

    def test_sexoptovik_preflight_accepts_exact_18_column_headerless_rows(self):
        supplier = SimpleNamespace(
            code='sexoptovik',
            csv_column_mapping=None,
            csv_has_header=False,
            csv_delimiter=';',
            csv_encoding='utf-8',
        )
        products, report = SupplierCSVParser(supplier).preflight_raw(
            (self._legacy_row() + '\n').encode('utf-8'),
        )
        self.assertTrue(report.is_valid, report.errors)
        self.assertEqual(len(products), 1)
        self.assertEqual(products[0]['external_id'], '123')

    def test_sexoptovik_preflight_rejects_invalid_identity_and_row_shape(self):
        supplier = SimpleNamespace(
            code='sexoptovik',
            csv_column_mapping=None,
            csv_has_header=False,
            csv_delimiter=';',
            csv_encoding='utf-8',
        )
        parser = SupplierCSVParser(supplier)
        invalid_feeds = (
            self._legacy_row(external_id='not-numeric'),
            self._legacy_row(external_id='000123'),
            self._legacy_row(title=''),
            self._legacy_row()[:-1],
            self._legacy_row(external_id='001') + '\n' + self._legacy_row(external_id='1'),
        )
        for feed in invalid_feeds:
            with self.subTest(feed=feed[:30]):
                products, report = parser.preflight_raw(feed.encode('utf-8'))
                self.assertFalse(report.is_valid)
                self.assertEqual(products, [])

    def test_mapped_preflight_keeps_header_names_and_optional_short_tail(self):
        supplier = SimpleNamespace(
            code='mapped-fixture',
            csv_column_mapping={
                'external_id': {'column': 'id'},
                'title': {'column': 'name'},
                'vendor_code': {'column': 3},
            },
            csv_has_header=True,
            csv_delimiter=';',
            csv_encoding='utf-8',
        )
        products, report = SupplierCSVParser(supplier).preflight_raw(
            b'id;name;optional\nSKU-1;Mapped item\nSKU-2;Second item;X\n',
        )
        self.assertTrue(report.is_valid, report.errors)
        self.assertEqual([row['external_id'] for row in products], ['SKU-1', 'SKU-2'])

    def test_invalid_source_does_not_advance_catalog_freshness(self):
        supplier = Supplier.query.get(self.supplier_id)
        supplier.code = 'sexoptovik'
        supplier.csv_encoding = 'utf-8'
        previous_sync = datetime(2026, 10, 1, 12, 0)
        supplier.last_sync_at = previous_sync
        supplier.last_sync_status = 'success'
        db.session.commit()

        with patch.object(
            SupplierCSVParser,
            'fetch_csv_raw',
            return_value=(self._legacy_row(external_id='bad-id') + '\n').encode(),
        ):
            result = SupplierService.sync_from_csv(self.supplier_id)

        self.assertFalse(result.success)
        self.assertEqual(SupplierProduct.query.filter_by(
            supplier_id=self.supplier_id,
        ).count(), 0)
        db.session.expire_all()
        supplier = db.session.get(Supplier, self.supplier_id)
        self.assertEqual(supplier.last_sync_at, previous_sync)
        self.assertEqual(supplier.last_sync_status, 'failed')

    def test_supplier_disable_during_provider_read_blocks_raw_apply(self):
        supplier = Supplier.query.get(self.supplier_id)
        previous_sync = datetime(2026, 10, 1, 12, 0)
        supplier.last_sync_at = previous_sync
        supplier.last_sync_status = 'success'
        db.session.commit()

        def read_after_admin_disable():
            current = db.session.get(Supplier, self.supplier_id)
            current.is_active = False
            db.session.commit()
            return b'synthetic'

        with self._sync_patches(self._row()), patch.object(
            SupplierCSVParser,
            'fetch_csv_raw',
            side_effect=read_after_admin_disable,
        ):
            result = SupplierService.sync_from_csv(self.supplier_id)

        self.assertFalse(result.success)
        self.assertEqual(SupplierProduct.query.filter_by(
            supplier_id=self.supplier_id,
        ).count(), 0)
        db.session.expire_all()
        supplier = db.session.get(Supplier, self.supplier_id)
        self.assertFalse(supplier.is_active)
        self.assertEqual(supplier.last_sync_at, previous_sync)
        self.assertEqual(supplier.last_sync_status, 'failed')

    def test_manual_sync_still_runs_for_previously_disabled_supplier(self):
        supplier = Supplier.query.get(self.supplier_id)
        supplier.is_active = False
        db.session.commit()

        result = self._sync(self._row())

        self.assertTrue(result.success)
        self.assertEqual(SupplierProduct.query.filter_by(
            supplier_id=self.supplier_id,
        ).count(), 1)

    def test_catalog_apply_failure_rolls_back_all_raw_rows_and_timestamp(self):
        initial = self._row(title='Keep current title')
        self.assertTrue(self._sync(initial).success)
        source = SupplierProduct.query.filter_by(
            supplier_id=self.supplier_id,
            external_id='fixture-1',
        ).one()
        previous_revision = source.content_revision
        previous_sync = datetime(2026, 10, 2, 12, 0)
        supplier = Supplier.query.get(self.supplier_id)
        supplier.last_sync_at = previous_sync
        db.session.commit()

        from services.supplier_service import _update_supplier_product
        original_update = _update_supplier_product

        def update_then_fail(product, data, price_data=None):
            original_update(product, data, price_data)
            raise RuntimeError('synthetic apply failure')

        with self._sync_patches(self._row(title='Must not commit')), patch(
            'services.supplier_service._update_supplier_product',
            side_effect=update_then_fail,
        ):
            result = SupplierService.sync_from_csv(self.supplier_id)

        self.assertFalse(result.success)
        self.assertEqual(result.added, 0)
        self.assertEqual(result.updated, 0)
        db.session.expire_all()
        source = SupplierProduct.query.filter_by(
            supplier_id=self.supplier_id,
            external_id='fixture-1',
        ).one()
        supplier = db.session.get(Supplier, self.supplier_id)
        self.assertEqual(source.title, 'Keep current title')
        self.assertEqual(source.content_revision, previous_revision)
        self.assertEqual(supplier.last_sync_at, previous_sync)
        self.assertEqual(supplier.last_sync_status, 'failed')

    def test_catalog_refresh_returns_busy_without_starting_second_read(self):
        with _supplier_catalog_sync_lock(self.supplier_id) as acquired:
            self.assertTrue(acquired)
            with patch.object(SupplierCSVParser, 'fetch_csv_raw') as fetch_raw:
                result = SupplierService.sync_from_csv(self.supplier_id)
        self.assertFalse(result.success)
        self.assertTrue(result.busy)
        fetch_raw.assert_not_called()

    def test_provider_read_has_no_open_database_transaction_or_photo_warm(self):
        transaction_states = []

        def read_catalog():
            transaction_states.append(db.session().in_transaction())
            return b'synthetic'

        with self._sync_patches(self._row()), patch(
            'services.photo_cache.bulk_download_supplier_photos',
        ) as photo_warm:
            with patch.object(
                SupplierCSVParser,
                'fetch_csv_raw',
                side_effect=read_catalog,
            ):
                result = SupplierService.sync_from_csv(self.supplier_id)

        self.assertTrue(result.success)
        self.assertEqual(transaction_states, [False])
        photo_warm.assert_not_called()

    def test_expected_config_change_prevents_catalog_get(self):
        with patch.object(SupplierCSVParser, 'fetch_csv_raw') as fetch_raw:
            result = SupplierService._sync_from_csv_locked(
                self.supplier_id,
                start_time=time.time(),
                expected_config_digest='0' * 64,
            )

        self.assertFalse(result.success)
        self.assertEqual(result.catalog_error_code, 'source_changed')
        self.assertEqual(result.price_followup_status, 'skipped')
        fetch_raw.assert_not_called()

    def test_config_change_at_pre_http_recheck_blocks_catalog_get(self):
        initial_digest = supplier_catalog_config_digest(
            db.session.get(Supplier, self.supplier_id),
        )
        original_digest = supplier_catalog_config_digest
        digest_calls = []

        def change_config_during_recheck(supplier):
            digest_calls.append(True)
            if len(digest_calls) == 2:
                supplier.csv_source_url = 'fixture://changed'
                db.session.commit()
            return original_digest(supplier)

        with patch(
            'services.supplier_service.supplier_catalog_config_digest',
            side_effect=change_config_during_recheck,
        ), patch.object(SupplierCSVParser, 'fetch_csv_raw') as fetch_raw:
            result = SupplierService._sync_from_csv_locked(
                self.supplier_id,
                start_time=time.time(),
                expected_config_digest=initial_digest,
            )

        self.assertFalse(result.success)
        self.assertEqual(result.catalog_error_code, 'source_changed')
        self.assertEqual(len(digest_calls), 2)
        fetch_raw.assert_not_called()

    def test_raw_checkpoint_runs_atomically_and_failure_rolls_back_catalog(self):
        previous_sync = datetime(2026, 10, 3, 12, 0)
        supplier = db.session.get(Supplier, self.supplier_id)
        supplier.last_sync_at = previous_sync
        db.session.commit()

        callback_observations = []

        def checkpoint(result, raw_at):
            callback_observations.append((
                db.session().in_transaction(),
                result.added,
                raw_at,
                SupplierProduct.query.filter_by(
                    supplier_id=self.supplier_id,
                ).count(),
            ))
            current = db.session.get(Supplier, self.supplier_id)
            self.assertEqual(current.last_sync_at, raw_at)
            current.name = 'must roll back'
            raise RuntimeError('synthetic checkpoint failure')

        with self._sync_patches(self._row()):
            result = SupplierService._sync_from_csv_locked(
                self.supplier_id,
                start_time=time.time(),
                raw_checkpoint=checkpoint,
            )

        self.assertFalse(result.success)
        self.assertEqual(result.catalog_error_code, 'raw_checkpoint_failed')
        self.assertEqual(result.price_followup_status, 'skipped')
        self.assertEqual(len(callback_observations), 1)
        self.assertEqual(callback_observations[0][0:2], (True, 1))
        self.assertIsInstance(callback_observations[0][2], datetime)
        self.assertEqual(callback_observations[0][3], 1)
        db.session.expire_all()
        supplier = db.session.get(Supplier, self.supplier_id)
        self.assertEqual(supplier.name, 'Fixture supplier')
        self.assertEqual(supplier.last_sync_at, previous_sync)
        self.assertEqual(supplier.last_sync_status, 'failed')
        self.assertEqual(SupplierProduct.query.filter_by(
            supplier_id=self.supplier_id,
        ).count(), 0)

    def test_catalog_price_followup_uses_original_effective_config(self):
        supplier = db.session.get(Supplier, self.supplier_id)
        supplier.price_file_url = 'https://prices.invalid/original.csv'
        supplier.price_file_encoding = 'utf-8'
        supplier.price_file_delimiter = '|'
        db.session.commit()

        with self._sync_patches(self._row()), patch.object(
            SupplierService,
            'sync_prices_and_stock',
            return_value=SimpleNamespace(
                success=True,
                updated=1,
                conflicting_source_rows=2,
                error_messages=[],
                busy=False,
            ),
        ) as price_followup:
            result = SupplierService.sync_from_csv(self.supplier_id)

        self.assertTrue(result.success)
        self.assertEqual(result.price_followup_status, 'partial')
        self.assertEqual(result.price_followup_conflicts, 2)
        self.assertEqual(
            price_followup.call_args.kwargs['expected_config'],
            {
                'price_file_url': 'https://prices.invalid/original.csv',
                'encoding': 'utf-8',
                'delimiter': '|',
            },
        )

    def test_price_writer_rejects_changed_expected_config_before_get(self):
        supplier = db.session.get(Supplier, self.supplier_id)
        supplier.price_file_url = 'https://prices.invalid/current.csv'
        supplier.price_file_encoding = 'utf-8'
        supplier.price_file_delimiter = '|'
        db.session.commit()
        expected = {
            'price_file_url': 'https://prices.invalid/original.csv',
            'encoding': 'utf-8',
            'delimiter': '|',
        }

        with patch(
            'services.supplier_price_sync.read_supplier_price_feed',
        ) as read_feed:
            result = sync_supplier_price_feed(
                self.supplier_id,
                expected_config=expected,
            )

        self.assertFalse(result.success)
        self.assertEqual(result.error_messages, ['source_changed'])
        read_feed.assert_not_called()

    def test_config_digest_is_available_on_module_and_service(self):
        supplier = db.session.get(Supplier, self.supplier_id)
        digest = supplier_catalog_config_digest(supplier)
        self.assertRegex(digest, r'^[0-9a-f]{64}$')
        self.assertEqual(SupplierService.supplier_catalog_config_digest(supplier), digest)

    def _make_imported_copy_and_channel_snapshots(self, source):
        wb_product = Product(
            seller_id=self.seller_id,
            nm_id=123456789,
            title='WB title snapshot',
            wb_audit_json='{"observed":"wb-snapshot"}',
        )
        db.session.add(wb_product)
        db.session.flush()

        imported = _copy_to_imported_product(self.seller_id, source)
        imported.product_id = wb_product.id
        imported.wb_nm_id = wb_product.nm_id
        imported.title = 'Seller-picked title'
        imported.content_overrides_json = json.dumps({
            'schema_version': 1,
            'fields': {
                'title': {
                    'value': 'Seller-picked title',
                    'inherited_value': source.title,
                    'inherited_origin': 'source',
                    'edited_by_user_id': self.user_id,
                    'edited_at': datetime.utcnow().isoformat(),
                    'edit_version': 1,
                },
            },
        }, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        db.session.add(imported)
        db.session.flush()

        marketplace = Marketplace(name='Ozon fixture', code='ozon')
        db.session.add(marketplace)
        db.session.flush()
        account = SellerMarketplaceAccount(
            seller_id=self.seller_id,
            marketplace_id=marketplace.id,
            external_account_id='synthetic-account',
            label='Synthetic account',
        )
        db.session.add(account)
        db.session.flush()
        listing = MarketplaceListing(
            seller_id=self.seller_id,
            marketplace_id=marketplace.id,
            account_id=account.id,
            imported_product_id=imported.id,
            offer_id='synthetic-offer',
            external_product_id='987654321',
            normalized_status='active',
            sync_fingerprint='d' * 64,
        )
        db.session.add(listing)
        db.session.flush()
        operation = MarketplaceOperation(
            seller_id=self.seller_id,
            marketplace_id=marketplace.id,
            account_id=account.id,
            listing_id=listing.id,
            operation_kind='product_update',
            status='succeeded',
            idempotency_key='feed-revision-fixture-op',
            request_fingerprint='a' * 64,
            contract_version='fixture-v1',
        )
        db.session.add(operation)
        db.session.flush()
        snapshot = MarketplaceListingSnapshot(
            seller_id=self.seller_id,
            marketplace_id=marketplace.id,
            account_id=account.id,
            operation_id=operation.id,
            listing_id=listing.id,
            snapshot_kind='product_update',
            source_fingerprint='b' * 64,
            submitted_fingerprint='c' * 64,
            submitted_state_json='{"fixture":"submitted"}',
            confirmed_state_json='{"fixture":"confirmed"}',
        )
        db.session.add(snapshot)
        db.session.commit()
        return imported.id, wb_product.id, listing.id, snapshot.id

    def test_material_change_is_one_revision_then_stale_refresh_is_exact(self):
        first = self._row()
        self.assertTrue(self._sync(first).success)
        source = SupplierProduct.query.filter_by(
            supplier_id=self.supplier_id,
            external_id='fixture-1',
        ).one()
        initial_revision = source.content_revision
        self.assertGreaterEqual(initial_revision, 1)
        imported_id, wb_product_id, listing_id, snapshot_id = (
            self._make_imported_copy_and_channel_snapshots(source)
        )
        copied_revision = ImportedProduct.query.get(imported_id).supplier_content_revision

        changed = self._row(
            title='Blue item',
            description='Changed description',
            characteristics={'Material': 'Linen'},
            photo_urls=[{'original': 'https://supplier.invalid/second.jpg'}],
            dimensions={'Width': '12'},
            supplier_price=125.0,
            supplier_quantity=7,
            recommended_retail_price=175.0,
        )
        self.assertTrue(self._sync(changed).success)
        source = db.session.get(SupplierProduct, source.id)
        self.assertEqual(source.content_revision, initial_revision + 1)
        self.assertEqual(json.loads(source.colors_json), ['Blue'])
        self.assertEqual(json.loads(source.characteristics_json)['Цвет'], 'Blue')
        self.assertEqual(json.loads(source.characteristics_json)['Material'], 'Linen')
        self.assertEqual(json.loads(source.photo_urls_json)[0]['original'],
                         'https://supplier.invalid/second.jpg')
        self.assertEqual(json.loads(source.dimensions_json)['Width'], '12')

        stale = SupplierService.get_available_products_for_seller(
            self.seller_id,
            self.supplier_id,
            show_imported=True,
            updates_only=True,
        )
        self.assertEqual([row.id for row in stale.items], [source.id])

        imported_before = ImportedProduct.query.get(imported_id)
        override_before = imported_before.content_overrides_json
        self.assertEqual(imported_before.supplier_content_revision, copied_revision)
        result = SupplierService.update_imported_products(
            self.seller_id,
            [imported_id],
        )
        self.assertEqual(result.imported, 1)
        imported_after = ImportedProduct.query.get(imported_id)
        self.assertEqual(imported_after.supplier_content_revision, initial_revision + 1)
        self.assertEqual(imported_after.title, 'Seller-picked title')
        overrides = json.loads(imported_after.content_overrides_json)
        self.assertEqual(
            overrides['fields']['title']['value'],
            json.loads(override_before)['fields']['title']['value'],
        )
        self.assertEqual(imported_after.wb_nm_id, 123456789)
        self.assertEqual(Product.query.get(wb_product_id).wb_audit_json,
                         '{"observed":"wb-snapshot"}')
        listing = db.session.get(MarketplaceListing, listing_id)
        self.assertEqual(listing.imported_product_id, imported_id)
        snapshot = db.session.get(MarketplaceListingSnapshot, snapshot_id)
        self.assertEqual(snapshot.confirmed_state_json, '{"fixture":"confirmed"}')

        # The same material snapshot plus independent price/stock changes must
        # not repeatedly make the copy stale.
        unchanged_material = self._row(
            title='Blue item',
            description='Changed description',
            characteristics={'Material': 'Linen'},
            photo_urls=[{'original': 'https://supplier.invalid/second.jpg'}],
            dimensions={'Width': '12'},
            supplier_price=160.0,
            supplier_quantity=9,
            recommended_retail_price=210.0,
        )
        self.assertTrue(self._sync(unchanged_material).success)
        source = db.session.get(SupplierProduct, source.id)
        self.assertEqual(source.content_revision, initial_revision + 1)
        stale = SupplierService.get_available_products_for_seller(
            self.seller_id,
            self.supplier_id,
            show_imported=True,
            updates_only=True,
        )
        self.assertEqual(stale.total, 0)

    def test_committed_material_change_survives_parser_failure(self):
        self.assertTrue(self._sync(self._row()).success)
        source = SupplierProduct.query.filter_by(
            supplier_id=self.supplier_id,
            external_id='fixture-1',
        ).one()
        initial_revision = source.content_revision
        self.assertGreaterEqual(initial_revision, 1)

        changed = self._row(
            title='Updated before parser failure',
            description='The raw feed phase committed this change',
        )
        self.assertTrue(self._sync(changed, parser_failure=True).success)
        source = db.session.get(SupplierProduct, source.id)
        self.assertEqual(source.title, changed['title'])
        self.assertEqual(source.content_revision, initial_revision + 1)


if __name__ == '__main__':
    unittest.main()
