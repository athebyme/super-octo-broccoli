"""Safety regressions for exact WB vendor-code import prechecks."""

import json
import unittest
from unittest.mock import MagicMock, patch

from flask import Flask

from models import db, ImportedProduct, Product, Seller, User
from services.wb_api_client import (
    WBVendorCardLookupUncertain,
    WBVendorCardNotFound,
)
from services.wb_product_importer import WBProductImporter


class WBImporterIdentityPreflightTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        self.app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
        db.init_app(self.app)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

        user = User(
            username='wb-importer-test',
            email='wb-importer-test@example.test',
            password_hash='x',
        )
        db.session.add(user)
        db.session.flush()
        self.seller = Seller(user_id=user.id, company_name='Seller')
        db.session.add(self.seller)
        db.session.flush()
        self.imported = ImportedProduct(
            seller_id=self.seller.id,
            external_id='supplier-1',
            external_vendor_code='source-1',
            title='Test product',
            description='Description',
            brand='Brand',
            colors='[]',
            sizes='{}',
            barcodes='["460000000001"]',
            materials='[]',
            photo_urls='[]',
            characteristics='{}',
            import_status='validated',
            wb_subject_id=777,
        )
        db.session.add(self.imported)
        db.session.commit()

        self.importer = WBProductImporter.__new__(WBProductImporter)
        self.importer.seller = self.seller
        self.importer.api_client = MagicMock()
        self.importer._category_supports_sizes = MagicMock(return_value=False)
        self.importer._build_wb_characteristics = MagicMock(return_value=[])
        self.importer._validate_characteristics_coverage = MagicMock(return_value=[])
        self.importer._build_wb_dimensions = MagicMock(return_value={})
        self.importer._resolve_brand_for_wb = MagicMock(return_value='Brand')
        self.importer._check_barcode_uniqueness = MagicMock(return_value=[])

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _call_single_import(self):
        with (
            patch(
                'services.prohibited_brands_service.check_brand_for_import',
                return_value=(True, None),
            ),
            patch(
                'services.pricing_engine.resolve_vendor_code_settings',
                return_value=('pattern', 'supplier', None),
            ),
            patch(
                'services.pricing_engine.generate_vendor_code',
                return_value='VC-EXACT-1',
            ),
            patch(
                'routes.photos.generate_public_photo_urls',
                return_value=[],
            ),
        ):
            return self.importer.import_product_to_wb(self.imported)

    def test_uncertain_exact_precheck_stops_before_barcode_or_create(self):
        self.importer.api_client.get_card_by_vendor_code.side_effect = (
            WBVendorCardLookupUncertain('incomplete')
        )

        success, error, _product = self._call_single_import()

        self.assertFalse(success)
        self.assertIn('точному артикулу', error)
        self.assertIn('Создание остановлено', error)
        self.importer._check_barcode_uniqueness.assert_not_called()
        self.importer.api_client.create_product_card.assert_not_called()

    def test_unexpected_none_response_is_not_treated_as_a_miss(self):
        self.importer.api_client.get_card_by_vendor_code.return_value = None

        success, error, _product = self._call_single_import()

        self.assertFalse(success)
        self.assertIn('не подтвердил точную карточку', error)
        self.importer._check_barcode_uniqueness.assert_not_called()
        self.importer.api_client.create_product_card.assert_not_called()

    def test_only_typed_not_found_reaches_barcode_and_create_fallback(self):
        self.importer.api_client.get_card_by_vendor_code.side_effect = [
            WBVendorCardNotFound('not found'),
        ]
        self.importer.api_client.create_product_card.side_effect = RuntimeError(
            'test stops after create attempt'
        )

        success, error, _product = self._call_single_import()

        self.assertFalse(success)
        self.assertTrue(error)
        self.importer._check_barcode_uniqueness.assert_called_once()
        self.importer.api_client.create_product_card.assert_called_once()

    def test_exact_card_is_passed_to_linker_without_nm_id_read(self):
        exact_card = {
            'vendorCode': 'VC-EXACT-1',
            'nmID': 50101,
            'title': 'Exact card',
        }
        self.importer.api_client.get_card_by_vendor_code.return_value = exact_card
        self.importer._link_existing_card = MagicMock(
            return_value=(True, 'linked', 50101),
        )

        result = self._call_single_import()

        self.assertEqual(result, (True, 'linked', 50101))
        self.importer._link_existing_card.assert_called_once_with(
            self.imported,
            'VC-EXACT-1',
            50101,
            existing_card=exact_card,
            require_unlinked_import_source=True,
        )
        self.importer.api_client.get_card_by_nm_id.assert_not_called()
        self.importer.api_client.create_product_card.assert_not_called()

    def test_vendor_used_recovery_also_requires_exact_card_and_passes_it_to_linker(self):
        exact_card = {
            'vendorCode': 'VC-EXACT-1',
            'nmID': 50102,
            'title': 'Exact recovery card',
        }
        self.importer.api_client.get_card_by_vendor_code.side_effect = [
            WBVendorCardNotFound('precheck complete miss'),
            exact_card,
        ]
        self.importer.api_client.create_product_card.side_effect = RuntimeError(
            'vendor code is used'
        )
        self.importer._link_existing_card = MagicMock(
            return_value=(True, 'linked after vendor conflict', 50102),
        )

        result = self._call_single_import()

        self.assertEqual(result, (True, 'linked after vendor conflict', 50102))
        self.importer._link_existing_card.assert_called_once_with(
            self.imported,
            'VC-EXACT-1',
            50102,
            existing_card=exact_card,
            require_unlinked_import_source=True,
        )
        self.importer.api_client.get_card_by_nm_id.assert_not_called()

    def test_exact_link_guard_preserves_legacy_wb_nm_id_only_association(self):
        self.imported.import_status = 'validated'
        other_import = ImportedProduct(
            seller_id=self.seller.id,
            external_id='legacy-source',
            import_status='imported',
            wb_nm_id=50103,
        )
        db.session.add(other_import)
        db.session.commit()

        with self.assertRaisesRegex(ValueError, 'уже связана с другим товаром'):
            self.importer._link_existing_card(
                self.imported,
                'VC-EXACT-1',
                50103,
                existing_card={'vendorCode': 'VC-EXACT-1', 'nmID': 50103},
                require_unlinked_import_source=True,
            )

        db.session.expire_all()
        source = db.session.get(ImportedProduct, self.imported.id)
        self.assertIsNone(source.product_id)
        self.assertIsNone(source.wb_nm_id)
        self.assertIsNone(
            Product.query.filter_by(seller_id=self.seller.id, nm_id=50103).first()
        )

    def test_exact_link_returns_materialized_product_after_atomic_local_save(self):
        service = MagicMock()
        service.apply_enrichment.return_value = {
            'success': True,
            'fields_applied': [],
            'photos': {'skipped': True},
            'wb_sync': False,
        }

        with patch(
            'services.supplier_enrichment.get_enrichment_service',
            return_value=service,
        ):
            result = self.importer._link_existing_card(
                self.imported,
                'VC-EXACT-1',
                50106,
                existing_card={
                    'vendorCode': 'VC-EXACT-1',
                    'nmID': 50106,
                    'title': 'Exact product',
                    'brand': 'Exact brand',
                },
                require_unlinked_import_source=True,
            )

        self.assertTrue(result[0])
        self.assertIsInstance(result[2], Product)
        self.assertEqual(result[2].nm_id, 50106)
        self.importer.api_client.get_card_by_nm_id.assert_not_called()
        service.apply_enrichment.assert_called_once()
        db.session.expire_all()
        source = db.session.get(ImportedProduct, self.imported.id)
        self.assertEqual(source.product_id, result[2].id)
        self.assertEqual(source.wb_nm_id, 50106)

    def test_exact_link_guard_preserves_product_fk_association(self):
        product = Product(
            seller_id=self.seller.id,
            nm_id=50105,
            vendor_code='existing',
            title='Existing product',
        )
        db.session.add(product)
        db.session.flush()
        other_import = ImportedProduct(
            seller_id=self.seller.id,
            product_id=product.id,
            external_id='linked-source',
            import_status='imported',
        )
        db.session.add(other_import)
        db.session.commit()

        with self.assertRaisesRegex(ValueError, 'уже связана с другим товаром'):
            self.importer._link_existing_card(
                self.imported,
                'VC-EXACT-1',
                50105,
                existing_card={'vendorCode': 'VC-EXACT-1', 'nmID': 50105},
                require_unlinked_import_source=True,
            )

        db.session.expire_all()
        source = db.session.get(ImportedProduct, self.imported.id)
        self.assertIsNone(source.product_id)
        self.assertIsNone(source.wb_nm_id)
        self.assertEqual(
            Product.query.filter_by(seller_id=self.seller.id, nm_id=50105).count(),
            1,
        )

    def test_exact_link_commit_failure_rolls_back_product_and_source_association(self):
        self.importer.api_client.get_card_by_nm_id.assert_not_called()
        real_commit = db.session.commit

        def fail_commit():
            raise RuntimeError('simulated local commit failure')

        with patch.object(db.session, 'commit', side_effect=fail_commit):
            with self.assertRaisesRegex(RuntimeError, 'simulated local commit failure'):
                self.importer._link_existing_card(
                    self.imported,
                    'VC-EXACT-1',
                    50104,
                    existing_card={'vendorCode': 'VC-EXACT-1', 'nmID': 50104},
                    require_unlinked_import_source=True,
                )

        # The link helper rolls back before returning control to its caller.
        real_commit()
        db.session.expire_all()
        source = db.session.get(ImportedProduct, self.imported.id)
        self.assertIsNone(source.product_id)
        self.assertIsNone(source.wb_nm_id)
        self.assertIsNone(
            Product.query.filter_by(seller_id=self.seller.id, nm_id=50104).first()
        )

    def test_batch_rejects_all_duplicate_vendor_codes_but_submits_valid_neighbor(self):
        duplicate_a = ImportedProduct(
            seller_id=self.seller.id, external_id='duplicate-a',
            title='Duplicate A', import_status='validated',
        )
        duplicate_b = ImportedProduct(
            seller_id=self.seller.id, external_id='duplicate-b',
            title='Duplicate B', import_status='validated',
        )
        unique = ImportedProduct(
            seller_id=self.seller.id, external_id='valid-neighbor',
            title='Valid neighbor', import_status='validated',
        )
        db.session.add_all([duplicate_a, duplicate_b, unique])
        db.session.commit()

        code_by_external_id = {
            'duplicate-a': 'VC-DUPLICATE',
            'duplicate-b': 'VC-DUPLICATE',
            'valid-neighbor': 'VC-VALID',
        }

        def prepare(ip):
            code = code_by_external_id[ip.external_id]
            return {
                'vendor_code': code,
                'subject_id': 777,
                'variant': {'vendorCode': code},
            }

        sent_batches = []

        def reject_fake_create(**kwargs):
            sent_batches.append(kwargs['cards'])
            raise RuntimeError('fake client stops after observing request')

        self.importer._prepare_card_data = MagicMock(side_effect=prepare)
        self.importer.api_client.create_product_cards_batch.side_effect = reject_fake_create
        self.importer.api_client.get_card_by_vendor_code.reset_mock()

        with patch(
            'services.prohibited_brands_service.check_brand_for_import',
            return_value=(True, None),
        ):
            results = self.importer.batch_import_products_to_wb(
                [duplicate_a, duplicate_b, unique],
            )

        self.assertEqual(len(sent_batches), 1)
        submitted_codes = [
            card['variants'][0]['vendorCode'] for card in sent_batches[0]
        ]
        self.assertEqual(submitted_codes, ['VC-VALID'])
        duplicate_results = [
            item for item in results
            if item['product_id'] in (duplicate_a.id, duplicate_b.id)
        ]
        self.assertEqual(len(duplicate_results), 2)
        self.assertTrue(all(item['status'] == 'failed' for item in duplicate_results))
        self.assertTrue(all('повторяется' in item['error'] for item in duplicate_results))
        self.importer.api_client.get_card_by_vendor_code.assert_not_called()

    def test_accepted_batch_without_confirmed_nmid_stays_pending_without_resubmit(self):
        class AcceptedButUnresolvedWB:
            def __init__(self):
                self.create_calls = []
                self.lookup_calls = 0
                self.errors_calls = 0

            def create_product_cards_batch(self, **kwargs):
                self.create_calls.append(kwargs['cards'])
                return {'accepted': True}

            def get_card_by_vendor_code(self, _vendor_code):
                self.lookup_calls += 1
                raise WBVendorCardNotFound('complete exact-page miss')

            def get_cards_errors_list(self, **_kwargs):
                self.errors_calls += 1
                return {'data': []}

        self.imported.wb_subject_id = None
        self.imported.import_status = 'validated'
        db.session.commit()
        fake_wb = AcceptedButUnresolvedWB()
        self.importer.api_client = fake_wb
        self.importer._prepare_card_data = MagicMock(return_value={
            'vendor_code': 'VC-ACCEPTED-1',
            'subject_id': 777,
            'variant': {'vendorCode': 'VC-ACCEPTED-1'},
            'wb_sizes': [],
            'characteristics': [],
            'media_urls': [],
            'dimensions': {},
        })

        with (
            patch(
                'services.prohibited_brands_service.check_brand_for_import',
                return_value=(True, None),
            ),
            patch('time.sleep'),
        ):
            results = self.importer.batch_import_products_to_wb([self.imported])

        self.assertEqual(len(fake_wb.create_calls), 1)
        self.assertEqual(fake_wb.lookup_calls, 11)
        self.assertEqual(fake_wb.errors_calls, 1)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['status'], 'pending_sync')
        self.assertIsNone(results[0]['nm_id'])
        self.assertEqual(
            results[0]['message'],
            'WB принял запрос. Карточка ожидает подтверждения после синхронизации.',
        )
        self.assertIsNone(results[0]['error'])

        db.session.expire_all()
        source = db.session.get(ImportedProduct, self.imported.id)
        self.assertEqual(source.import_status, 'imported_pending_sync')
        self.assertEqual(source.import_error, results[0]['message'])
        self.assertIsNone(source.product_id)
        self.assertIsNone(source.wb_nm_id)
        self.assertEqual(
            Product.query.filter_by(seller_id=self.seller.id).count(), 0,
        )

    def test_legacy_batch_stats_separate_pending_from_imported_and_failed(self):
        from services.wb_product_importer import import_products_batch

        pending_result = {
            'product_id': self.imported.id,
            'status': 'pending_sync',
            'error': None,
            'nm_id': None,
            'message': 'WB принял запрос. Карточка ожидает подтверждения после синхронизации.',
            'warnings': [],
        }
        with patch('services.wb_product_importer.WBProductImporter') as importer_cls:
            importer_cls.return_value.batch_import_products_to_wb.return_value = [
                pending_result,
            ]

            stats = import_products_batch(self.seller.id, [self.imported.id])

        self.assertEqual(stats['imported'], 0)
        self.assertEqual(stats['failed'], 0)
        self.assertEqual(stats['pending_sync'], 1)
        self.assertEqual(stats['skipped'], 0)
        self.assertEqual(stats['errors'], [])


if __name__ == '__main__':
    unittest.main()
