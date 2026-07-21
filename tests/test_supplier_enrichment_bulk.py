# -*- coding: utf-8 -*-
"""Mass supplier enrichment: exact source, per-card isolation and honest counts."""

import json
import os
import unittest
from unittest.mock import MagicMock, patch

from flask import Flask

from models import (
    BulkEditHistory,
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
    EnrichmentService,
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


if __name__ == '__main__':
    unittest.main()
