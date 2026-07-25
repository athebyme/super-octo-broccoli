# -*- coding: utf-8 -*-
"""Mass supplier enrichment: exact source, per-card isolation and honest counts."""

import json
import os
import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

from flask import Flask

from models import (
    BulkEditHistory,
    CardEditHistory,
    EnrichmentJob,
    ImportedProduct,
    Product,
    Seller,
    Supplier,
    SupplierProduct,
    db,
)
from services.marketplace_operation_locks import (
    release_wb_seller_media_lock,
    try_wb_seller_media_lock,
)
from services.supplier_enrichment import (
    EnrichmentJobAlreadyActive,
    EnrichmentService,
    WbEnrichmentAwaitingReconciliation,
    WbMediaOperationBusy,
)


def _make_app():
    app = Flask(__name__)
    app.config.update(
        SQLALCHEMY_DATABASE_URI='sqlite:///:memory:',
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SECRET_KEY='bulk-enrichment-test',
    )
    db.init_app(app)
    return app


class SupplierEnrichmentBulkTestCase(unittest.TestCase):
    def setUp(self):
        os.environ.setdefault('SECRET_KEY', 'bulk-enrichment-test')
        self.app = _make_app()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

        seller = Seller(
            id=1,
            user_id=1,
            company_name='Bulk seller',
        )
        seller.wb_api_key = 'test-key'
        db.session.add(seller)
        for product_id in (11, 12, 13):
            db.session.add(Product(
                id=product_id,
                seller_id=1,
                nm_id=5000 + product_id,
                vendor_code=f'VC-{product_id}',
                title=f'Card {product_id}',
                is_active=True,
            ))
            db.session.add(ImportedProduct(
                id=1000 + product_id,
                seller_id=1,
                product_id=product_id,
                external_id=f'EXT-{product_id}',
                photo_urls=json.dumps([
                    {'original': f'https://supplier.test/{product_id}.jpg'}
                ]),
            ))
        db.session.add(EnrichmentJob(
            id='bulk-job',
            seller_id=1,
            status='pending',
            total=3,
            fields_config='["photos"]',
            photo_strategy='replace',
            results='[]',
        ))
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_one_card_failure_does_not_stop_remaining_cards(self):
        service = EnrichmentService()
        success = {
            'success': True,
            'fields_applied': ['photos'],
            'photos': {'uploaded': 1},
            'error': None,
            'wb_sync': True,
        }

        with (
            patch(
                'services.wb_api_client.WildberriesAPIClient',
                autospec=True,
            ),
            patch.object(
                service,
                'apply_enrichment',
                side_effect=[success, RuntimeError('provider detail'), success],
            ) as apply_mock,
            patch('services.supplier_enrichment.logger.exception'),
        ):
            service._run_bulk_job(
                'bulk-job',
                [11, 12, 13],
                ['photos'],
                'replace',
                1,
                self.app,
            )

        job = db.session.get(EnrichmentJob, 'bulk-job')
        self.assertEqual(job.status, 'done')
        self.assertEqual(job.processed, 3)
        self.assertEqual(job.succeeded, 2)
        self.assertEqual(job.failed, 1)
        self.assertEqual(job.skipped, 0)
        self.assertEqual(apply_mock.call_count, 3)

        rows = json.loads(job.results)
        self.assertEqual([row['status'] for row in rows], [
            'success', 'failed', 'success',
        ])
        self.assertEqual(rows[1]['error'], 'Не удалось обработать карточку')
        self.assertNotIn('provider detail', job.results)

        history = BulkEditHistory.query.one()
        self.assertEqual(history.status, 'completed')
        self.assertEqual(history.success_count, 2)
        self.assertEqual(history.error_count, 1)

    def test_service_atomically_rejects_second_active_bulk_job(self):
        service = EnrichmentService()
        seller = db.session.get(Seller, 1)

        with self.assertRaises(EnrichmentJobAlreadyActive) as raised:
            service.start_bulk_enrichment(
                [11], ['photos'], 'smart_merge', seller,
            )

        self.assertEqual(raised.exception.job_id, 'bulk-job')
        self.assertEqual(EnrichmentJob.query.count(), 1)
        self.assertEqual(BulkEditHistory.query.count(), 0)

    def test_per_product_fields_are_persisted_with_exact_union(self):
        db.session.delete(db.session.get(EnrichmentJob, 'bulk-job'))
        db.session.commit()
        service = EnrichmentService()
        seller = db.session.get(Seller, 1)

        with patch('services.supplier_enrichment.threading.Thread') as thread:
            job_id = service.start_bulk_enrichment(
                [11, 12],
                ['title', 'description'],
                'smart_merge',
                seller,
                fields_by_product={
                    11: ['title'],
                    12: ['description'],
                },
            )

        job = db.session.get(EnrichmentJob, job_id)
        self.assertEqual(json.loads(job.fields_config), {
            'version': 1,
            'default': ['title', 'description'],
            'by_product': {
                '11': ['title'],
                '12': ['description'],
            },
        })
        thread.return_value.start.assert_called_once_with()

        with self.assertRaisesRegex(ValueError, 'exactly equal'):
            service.start_bulk_enrichment(
                [11, 12],
                ['title', 'description', 'photos'],
                'smart_merge',
                seller,
                fields_by_product={
                    11: ['title'],
                    12: ['description'],
                },
            )

    def test_worker_uses_exact_fields_for_each_durable_row(self):
        job = db.session.get(EnrichmentJob, 'bulk-job')
        job.total = 2
        job.product_ids_json = json.dumps([11, 12])
        job.fields_config = json.dumps({
            'version': 1,
            'default': ['title', 'description'],
            'by_product': {
                '11': ['title'],
                '12': ['description'],
            },
        })
        db.session.commit()
        service = EnrichmentService()

        def applied(_product, _imp, item_fields, *_args, **_kwargs):
            return {
                'success': True,
                'fields_applied': list(item_fields),
                'photos': {'skipped': True},
                'error': None,
                'wb_sync': True,
            }

        with (
            patch('services.wb_api_client.WildberriesAPIClient'),
            patch.object(
                service, 'apply_enrichment', side_effect=applied,
            ) as apply_mock,
        ):
            service._run_bulk_job(
                'bulk-job', [], [], 'smart_merge', 1, self.app,
            )

        self.assertEqual(
            [call.args[2] for call in apply_mock.call_args_list],
            [['title'], ['description']],
        )
        job = db.session.get(EnrichmentJob, 'bulk-job')
        self.assertEqual(job.status, 'done')
        self.assertEqual(job.succeeded, 2)

    def test_link_existing_card_uses_append_only_enrichment(self):
        from services.wb_product_importer import WBProductImporter

        importer = WBProductImporter.__new__(WBProductImporter)
        importer.seller = db.session.get(Seller, 1)
        importer.api_client = MagicMock()
        importer.api_client.get_card_by_nm_id.return_value = {
            'nmID': 5011,
            'title': 'Live card',
            'brand': 'Live brand',
        }
        imported = db.session.get(ImportedProduct, 1011)
        imported.product_id = None
        service = MagicMock()
        service.apply_enrichment.return_value = {
            'success': True,
            'fields_applied': [],
            'photos': {'skipped': True},
            'wb_sync': False,
        }

        with (
            patch(
                'services.supplier_enrichment.get_enrichment_service',
                return_value=service,
            ),
            patch.object(importer, '_upload_photos_for_card') as legacy_upload,
        ):
            result = importer._link_existing_card(imported, 'VC-11', 5011)

        self.assertTrue(result[0])
        self.assertEqual(imported.product_id, 11)
        self.assertEqual(service.apply_enrichment.call_args.args[2:4], (
            ['photos'], 'smart_merge',
        ))
        legacy_upload.assert_not_called()

    def test_ambiguous_exact_import_link_fails_closed(self):
        duplicate = ImportedProduct(
            seller_id=1,
            product_id=11,
            external_id='DUPLICATE-11',
            photo_urls='[]',
        )
        db.session.add(duplicate)
        db.session.commit()

        source = EnrichmentService().find_supplier_data(
            db.session.get(Product, 11), 1,
        )

        self.assertIsNone(source)

    def test_ambiguous_legacy_external_id_fallback_fails_closed(self):
        product = Product(
            id=20,
            seller_id=1,
            nm_id=5020,
            vendor_code='id-25268-supplier',
            title='Legacy card',
            is_active=True,
        )
        db.session.add(product)
        db.session.add_all([
            ImportedProduct(
                seller_id=1,
                external_id='25268',
                photo_urls='[]',
            ),
            ImportedProduct(
                seller_id=1,
                external_id='25268',
                photo_urls='[]',
            ),
        ])
        db.session.commit()

        source = EnrichmentService().find_supplier_data(product, 1)

        self.assertIsNone(source)

    def test_photo_noop_is_skipped_not_success(self):
        service = EnrichmentService()
        noop = {
            'success': True,
            'fields_applied': [],
            'photos': {'skipped': True, 'reason': 'already_has_photos'},
            'error': None,
            'wb_sync': False,
        }

        with (
            patch('services.wb_api_client.WildberriesAPIClient'),
            patch.object(service, 'apply_enrichment', return_value=noop),
        ):
            service._run_bulk_job(
                'bulk-job', [11], ['photos'], 'only_if_empty', 1, self.app
            )

        job = db.session.get(EnrichmentJob, 'bulk-job')
        self.assertEqual(job.succeeded, 0)
        self.assertEqual(job.skipped, 1)
        self.assertEqual(json.loads(job.results)[0]['reason'], 'already_has_photos')

    def test_bulk_result_keeps_bounded_field_and_merge_summary(self):
        service = EnrichmentService()
        merged = {
            'success': True,
            'fields_applied': ['title'],
            'photos': {'skipped': True, 'reason': 'photo_match_unavailable'},
            'error': None,
            'wb_sync': False,
            'merge_decisions': {
                'content': {
                    'fields': {
                        'title': {
                            'decision': 'update',
                            'existing_length': 4,
                            'candidate_length': 12,
                        },
                        'characteristics': {
                            'decision': 'preserve_live',
                            'accepted': 0,
                            'preserved': 1,
                        },
                    },
                    'characteristics': {
                        'counts': {
                            'added': 0,
                            'replaced_more_complete': 0,
                            'preserved_existing': 1,
                            'unchanged': 0,
                            'skipped_empty': 0,
                        },
                    },
                },
                'photos': {
                    'matching_status': 'blocked',
                    'live_count': 2,
                    'counts': {'skipped_match_unavailable': 1},
                },
            },
        }

        with (
            patch('services.wb_api_client.WildberriesAPIClient'),
            patch.object(service, 'apply_enrichment', return_value=merged),
        ):
            service._run_bulk_job(
                'bulk-job', [11], ['title', 'characteristics', 'photos'],
                'smart_merge', 1, self.app,
            )

        row = json.loads(
            db.session.get(EnrichmentJob, 'bulk-job').results
        )[0]
        self.assertEqual(row['status'], 'success')
        self.assertEqual(
            row['merge_summary']['fields']['title'],
            {
                'decision': 'update',
                'existing_length': 4,
                'candidate_length': 12,
            },
        )
        self.assertEqual(
            row['merge_summary']['characteristics']['preserved_existing'],
            1,
        )
        self.assertEqual(
            row['merge_summary']['photos']['matching_status'], 'blocked'
        )

    def test_corrupt_durable_ids_fail_before_wb_client(self):
        service = EnrichmentService()
        job = db.session.get(EnrichmentJob, 'bulk-job')
        job.total = 1
        job.product_ids_json = '["11"]'
        job.fields_config = '["photos"]'
        db.session.commit()

        with patch(
            'services.wb_api_client.WildberriesAPIClient',
        ) as client:
            service._run_bulk_job(
                'bulk-job', [], [], 'smart_merge', 1, self.app,
            )

        job = db.session.get(EnrichmentJob, 'bulk-job')
        self.assertEqual(job.status, 'failed')
        self.assertEqual(job.last_error, 'invalid_durable_job_payload')
        client.assert_not_called()

    def test_foreign_bulk_history_is_not_mutated_by_corrupt_job_link(self):
        service = EnrichmentService()
        db.session.add(Seller(
            id=2,
            user_id=2,
            company_name='Other seller',
        ))
        foreign_bulk = BulkEditHistory(
            seller_id=2,
            operation_type='supplier_enrichment',
            status='in_progress',
            total_products=1,
        )
        db.session.add(foreign_bulk)
        db.session.flush()
        job = db.session.get(EnrichmentJob, 'bulk-job')
        job.total = 1
        job.product_ids_json = '[11]'
        job.fields_config = '["photos"]'
        job.bulk_edit_id = foreign_bulk.id
        db.session.commit()

        service._run_bulk_job(
            'bulk-job', [], [], 'smart_merge', 1, self.app,
        )

        db.session.refresh(foreign_bulk)
        job = db.session.get(EnrichmentJob, 'bulk-job')
        self.assertEqual(job.status, 'failed')
        self.assertEqual(foreign_bulk.status, 'in_progress')
        self.assertIsNone(foreign_bulk.completed_at)

    def test_legacy_photo_write_uses_shared_seller_media_lock(self):
        service = EnrichmentService()
        client = MagicMock()
        claim = try_wb_seller_media_lock(1)
        self.assertIsNotNone(claim)
        try:
            with self.assertRaises(WbMediaOperationBusy):
                service.upload_photos_to_card_locked(
                    client,
                    seller_id=1,
                    nm_id=5011,
                    photo_paths=['/tmp/synthetic.jpg'],
                )
            client.upload_photos_to_card.assert_not_called()
        finally:
            release_wb_seller_media_lock(claim)

        client.upload_photos_to_card.return_value = [{'success': True}]
        result = service.upload_photos_to_card_locked(
            client,
            seller_id=1,
            nm_id=5011,
            photo_paths=['/tmp/synthetic.jpg'],
        )
        self.assertEqual(result, [{'success': True}])
        client.upload_photos_to_card.assert_called_once_with(
            5011,
            ['/tmp/synthetic.jpg'],
            seller_id=1,
        )

    def test_exact_supplier_product_gallery_wins_over_stale_import_copy(self):
        supplier = Supplier(id=4, name='Fresh supplier', code='fresh')
        supplier_product = SupplierProduct(
            id=44,
            supplier_id=4,
            external_id='SP-44',
            title='Fresh gallery',
            photo_urls_json=json.dumps([
                {'original': 'https://supplier.test/fresh-1.jpg'},
                {'original': 'https://supplier.test/fresh-2.jpg'},
            ]),
        )
        imported = db.session.get(ImportedProduct, 1011)
        imported.supplier_id = 4
        imported.supplier_product_id = 44
        imported.photo_urls = json.dumps([
            {'original': 'https://supplier.test/stale.jpg'}
        ])
        db.session.add_all([supplier, supplier_product])
        db.session.commit()

        photos, supplier_type, external_id = EnrichmentService()._photo_source(
            imported
        )

        self.assertEqual(len(photos), 2)
        self.assertEqual(photos[0]['original'], 'https://supplier.test/fresh-1.jpg')
        self.assertEqual(supplier_type, 'fresh')
        self.assertEqual(external_id, 'SP-44')

    def test_exact_empty_supplier_gallery_does_not_resurrect_stale_copy(self):
        supplier = Supplier(id=5, name='Empty supplier', code='empty')
        supplier_product = SupplierProduct(
            id=45,
            supplier_id=5,
            external_id='SP-45',
            title='No current photos',
            photo_urls_json='[]',
        )
        imported = db.session.get(ImportedProduct, 1011)
        imported.supplier_id = 5
        imported.supplier_product_id = 45
        imported.photo_urls = json.dumps([
            {'original': 'https://supplier.test/stale.jpg'}
        ])
        db.session.add_all([supplier, supplier_product])
        db.session.commit()

        photos, supplier_type, external_id = EnrichmentService()._photo_source(
            imported
        )

        self.assertEqual(photos, [])
        self.assertEqual(supplier_type, 'empty')
        self.assertEqual(external_id, 'SP-45')

    def test_missing_exact_supplier_product_does_not_use_stale_copy(self):
        imported = db.session.get(ImportedProduct, 1011)
        imported.supplier_product_id = 987654
        imported.photo_urls = json.dumps([
            {'original': 'https://supplier.test/stale.jpg'}
        ])
        db.session.commit()

        photos, _supplier_type, _external_id = (
            EnrichmentService()._photo_source(imported)
        )

        self.assertEqual(photos, [])

    def test_selective_source_drift_stops_before_cache_or_wb(self):
        service = EnrichmentService()
        product = db.session.get(Product, 11)
        imported = db.session.get(ImportedProduct, 1011)
        seller = db.session.get(Seller, 1)
        client = MagicMock()

        with patch(
            'services.photo_cache.get_photo_cache',
        ) as photo_cache:
            result = service.apply_selective_photos(
                product,
                imported,
                [0],
                'smart_merge',
                seller,
                client,
                expected_source_urls=['https://supplier.test/old.jpg'],
            )

        self.assertFalse(result['success'])
        self.assertTrue(result['definitely_not_sent'])
        self.assertEqual(result['reason'], 'supplier_photo_source_drift')
        photo_cache.assert_not_called()
        client.get_card_by_nm_id.assert_not_called()

    def test_bounded_tick_resumes_from_durable_cursor(self):
        service = EnrichmentService()
        result = {
            'success': True,
            'fields_applied': ['photos'],
            'photos': {'uploaded': 1},
            'error': None,
            'wb_sync': True,
        }
        with (
            patch('services.wb_api_client.WildberriesAPIClient'),
            patch.object(service, 'apply_enrichment', return_value=result) as apply,
        ):
            service._run_bulk_job(
                'bulk-job', [11, 12, 13], ['photos'], 'smart_merge', 1,
                self.app, item_limit=1,
            )
            first = db.session.get(EnrichmentJob, 'bulk-job')
            self.assertEqual(first.status, 'pending')
            self.assertEqual(first.processed, 1)
            self.assertIsNone(first.claim_token)

            service._run_bulk_job(
                'bulk-job', [], [], 'smart_merge', 1,
                self.app, item_limit=5,
            )

        db.session.expire_all()
        job = db.session.get(EnrichmentJob, 'bulk-job')
        self.assertEqual(job.status, 'done')
        self.assertEqual(job.processed, 3)
        self.assertEqual(apply.call_count, 3)

    def test_unreconciled_receipt_blocks_only_the_same_write_kind(self):
        service = EnrichmentService()
        content = CardEditHistory(
            product_id=11,
            seller_id=1,
            action='update',
            changed_fields=['title'],
            snapshot_before={'title': 'Card 11'},
            snapshot_after={'title': 'Card 11 richer'},
            wb_synced=False,
            wb_sync_status='submitted',
            wb_reconcile_due_at=datetime.utcnow(),
        )
        db.session.add(content)
        db.session.commit()

        with self.assertRaises(WbEnrichmentAwaitingReconciliation):
            service._assert_no_unreconciled_write(
                seller_id=1,
                nm_id=5011,
                operation_kind='content',
            )
        service._assert_no_unreconciled_write(
            seller_id=1,
            nm_id=5011,
            operation_kind='photos',
        )

        photo = CardEditHistory(
            product_id=11,
            seller_id=1,
            action='update',
            changed_fields=['photos'],
            snapshot_before={'photos': []},
            snapshot_after={'photos': ['enrichment-sha256:planned']},
            wb_synced=False,
            wb_sync_status='uncertain',
            wb_reconcile_due_at=datetime.utcnow(),
        )
        db.session.add(photo)
        db.session.commit()

        with self.assertRaises(WbEnrichmentAwaitingReconciliation):
            service._assert_no_unreconciled_write(
                seller_id=1,
                nm_id=5011,
                operation_kind='photos',
            )

        photo.wb_sync_status = 'partial'
        photo.wb_reconcile_due_at = None
        photo.wb_reconciled_at = datetime.utcnow()
        db.session.commit()
        service._assert_no_unreconciled_write(
            seller_id=1,
            nm_id=5011,
            operation_kind='photos',
        )

    def test_deferred_item_keeps_cursor_and_replans_on_next_tick(self):
        service = EnrichmentService()
        deferred = {
            'success': False,
            'fields_applied': [],
            'photos': {
                'skipped': True,
                'reason': 'previous_photo_write_pending',
            },
            'error': 'Предыдущая отправка фото ещё проверяется',
            'wb_sync': False,
            'reconciliation_pending': True,
            'deferred': True,
        }
        success = {
            'success': True,
            'fields_applied': ['photos'],
            'photos': {'uploaded': 1},
            'error': None,
            'wb_sync': True,
        }

        with (
            patch('services.wb_api_client.WildberriesAPIClient'),
            patch.object(
                service,
                'apply_enrichment',
                side_effect=[deferred, success],
            ) as apply,
        ):
            service._run_bulk_job(
                'bulk-job', [11], ['photos'], 'smart_merge', 1, self.app,
            )
            waiting = db.session.get(EnrichmentJob, 'bulk-job')
            self.assertEqual(waiting.status, 'pending')
            self.assertEqual(waiting.processed, 0)
            self.assertEqual(waiting.current_product_id, 11)

            service._run_bulk_job(
                'bulk-job', [], [], 'smart_merge', 1, self.app,
            )

        db.session.expire_all()
        job = db.session.get(EnrichmentJob, 'bulk-job')
        self.assertEqual(job.status, 'done')
        self.assertEqual(job.processed, 1)
        self.assertEqual(job.succeeded, 1)
        self.assertEqual(apply.call_count, 2)

    def test_pending_presend_receipt_is_recovered_without_replay(self):
        service = EnrichmentService()
        bulk = BulkEditHistory(
            seller_id=1,
            operation_type='supplier_enrichment',
            status='in_progress',
            total_products=1,
        )
        db.session.add(bulk)
        db.session.flush()
        started = datetime.utcnow() - timedelta(seconds=5)
        job = db.session.get(EnrichmentJob, 'bulk-job')
        job.total = 1
        job.product_ids_json = '[11]'
        job.fields_config = '["photos"]'
        job.bulk_edit_id = bulk.id
        job.current_product_id = 11
        job.current_item_started_at = started
        db.session.add(CardEditHistory(
            product_id=11,
            seller_id=1,
            bulk_edit_id=bulk.id,
            action='update',
            changed_fields=['photos'],
            snapshot_before={'photos': []},
            snapshot_after={'photos': ['enrichment-sha256:planned']},
            merge_decisions={'photos': {'live_count': 0}},
            wb_synced=False,
            wb_sync_status='pending',
            wb_reconcile_due_at=datetime.utcnow(),
            created_at=datetime.utcnow(),
        ))
        db.session.commit()

        with (
            patch('services.wb_api_client.WildberriesAPIClient'),
            patch.object(service, 'apply_enrichment') as apply,
        ):
            service._run_bulk_job(
                'bulk-job', [], [], 'smart_merge', 1, self.app,
            )

        job = db.session.get(EnrichmentJob, 'bulk-job')
        self.assertEqual(job.status, 'done')
        self.assertEqual(job.succeeded, 1)
        apply.assert_not_called()

    def test_crashed_photo_noop_is_recovered_as_skipped_not_replayed(self):
        service = EnrichmentService()
        bulk = BulkEditHistory(
            seller_id=1,
            operation_type='supplier_enrichment',
            status='in_progress',
            total_products=1,
        )
        db.session.add(bulk)
        db.session.flush()
        started = datetime.utcnow() - timedelta(seconds=5)
        job = db.session.get(EnrichmentJob, 'bulk-job')
        job.total = 1
        job.product_ids_json = '[11]'
        job.fields_config = '["photos"]'
        job.bulk_edit_id = bulk.id
        job.current_product_id = 11
        job.current_item_started_at = started
        db.session.add(CardEditHistory(
            product_id=11,
            seller_id=1,
            bulk_edit_id=bulk.id,
            action='update',
            changed_fields=[],
            snapshot_before={'photos': []},
            snapshot_after={'photos': []},
            merge_decisions={'photos': {
                'live_count': 0,
                'append_indices': [],
            }},
            wb_synced=False,
            wb_sync_status='skipped',
            wb_reconcile_due_at=None,
            wb_reconciled_at=datetime.utcnow(),
            created_at=datetime.utcnow(),
        ))
        db.session.commit()

        with (
            patch('services.wb_api_client.WildberriesAPIClient'),
            patch.object(service, 'apply_enrichment') as apply,
        ):
            service._run_bulk_job(
                'bulk-job', [], [], 'smart_merge', 1, self.app,
            )

        job = db.session.get(EnrichmentJob, 'bulk-job')
        self.assertEqual(job.status, 'done')
        self.assertEqual(job.skipped, 1)
        self.assertEqual(job.failed, 0)
        self.assertEqual(json.loads(job.results)[0]['reason'], 'recovered_noop')
        apply.assert_not_called()

    def test_content_then_photo_resume_waits_for_reconciliation(self):
        service = EnrichmentService()
        bulk = BulkEditHistory(
            seller_id=1,
            operation_type='supplier_enrichment',
            status='in_progress',
            total_products=1,
        )
        db.session.add(bulk)
        db.session.flush()
        started = datetime.utcnow() - timedelta(seconds=5)
        job = db.session.get(EnrichmentJob, 'bulk-job')
        job.total = 1
        job.product_ids_json = '[11]'
        job.fields_config = '["title", "photos"]'
        job.bulk_edit_id = bulk.id
        job.current_product_id = 11
        job.current_item_started_at = started
        history = CardEditHistory(
            product_id=11,
            seller_id=1,
            bulk_edit_id=bulk.id,
            action='update',
            changed_fields=['title'],
            snapshot_before={'title': 'Card 11'},
            snapshot_after={'title': 'Card 11 richer'},
            merge_decisions={'policy': 'wb-enrichment-preserve-v2'},
            wb_synced=False,
            wb_sync_status='submitted',
            wb_reconcile_due_at=datetime.utcnow(),
            created_at=datetime.utcnow(),
        )
        db.session.add(history)
        db.session.commit()

        photo_result = {
            'success': True,
            'fields_applied': ['photos'],
            'photos': {'uploaded': 1},
            'error': None,
            'wb_sync': True,
        }
        with (
            patch('services.wb_api_client.WildberriesAPIClient'),
            patch.object(
                service, 'apply_enrichment', return_value=photo_result,
            ) as apply,
        ):
            service._run_bulk_job(
                'bulk-job', [], [], 'smart_merge', 1, self.app,
            )
            waiting = db.session.get(EnrichmentJob, 'bulk-job')
            self.assertEqual(waiting.status, 'pending')
            self.assertEqual(waiting.processed, 0)
            apply.assert_not_called()

            history.wb_sync_status = 'success'
            history.wb_synced = True
            history.wb_reconcile_due_at = None
            db.session.commit()
            service._run_bulk_job(
                'bulk-job', [], [], 'smart_merge', 1, self.app,
            )

        job = db.session.get(EnrichmentJob, 'bulk-job')
        self.assertEqual(job.status, 'done')
        apply.assert_called_once()
        self.assertEqual(apply.call_args.args[2], ['photos'])

    def test_terminal_partial_content_receipt_does_not_stall_photo_followup(self):
        service = EnrichmentService()
        bulk = BulkEditHistory(
            seller_id=1,
            operation_type='supplier_enrichment',
            status='in_progress',
            total_products=1,
        )
        db.session.add(bulk)
        db.session.flush()
        started = datetime.utcnow() - timedelta(seconds=5)
        job = db.session.get(EnrichmentJob, 'bulk-job')
        job.total = 1
        job.product_ids_json = '[11]'
        job.fields_config = '["title", "photos"]'
        job.bulk_edit_id = bulk.id
        job.current_product_id = 11
        job.current_item_started_at = started
        db.session.add(CardEditHistory(
            product_id=11,
            seller_id=1,
            bulk_edit_id=bulk.id,
            action='update',
            changed_fields=['title'],
            snapshot_before={'title': 'Card 11'},
            snapshot_after={'title': 'Card 11 richer'},
            merge_decisions={'policy': 'wb-enrichment-preserve-v3'},
            wb_synced=True,
            wb_sync_status='partial',
            wb_reconcile_due_at=None,
            wb_reconciled_at=datetime.utcnow(),
            created_at=datetime.utcnow(),
        ))
        db.session.commit()

        photo_result = {
            'success': True,
            'fields_applied': ['photos'],
            'photos': {'uploaded': 1},
            'error': None,
            'wb_sync': True,
        }
        with (
            patch('services.wb_api_client.WildberriesAPIClient'),
            patch.object(
                service, 'apply_enrichment', return_value=photo_result,
            ) as apply,
        ):
            service._run_bulk_job(
                'bulk-job', [], [], 'smart_merge', 1, self.app,
            )

        job = db.session.get(EnrichmentJob, 'bulk-job')
        self.assertEqual(job.status, 'done')
        apply.assert_called_once()
        self.assertEqual(apply.call_args.args[2], ['photos'])


if __name__ == '__main__':
    unittest.main()
