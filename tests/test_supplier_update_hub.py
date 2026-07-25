# -*- coding: utf-8 -*-
"""Тесты сервисного слоя «Обновление карточек от поставщика».

Реальная in-memory SQLite, как в остальных тестах платформы.
"""

import json
import unittest
from unittest.mock import patch, MagicMock

from flask import Flask

from models import (
    db, Product, ImportedProduct, Supplier, SupplierProduct, Seller,
    BackgroundJob, BulkEditHistory, Notification,
)

import os as _os
_PROJECT_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))


def _make_app():
    app = Flask(__name__, template_folder=_os.path.join(_PROJECT_ROOT, 'templates'))
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    app.config['SECRET_KEY'] = 'test-secret'
    app.config['PUBLIC_BASE_URL'] = 'https://platform.example'
    db.init_app(app)
    return app


class HubDBTestCase(unittest.TestCase):
    def setUp(self):
        self.app = _make_app()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self._seed()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _seed(self):
        db.session.add(Seller(id=1, user_id=1, company_name='Владимир'))
        db.session.add(Seller(id=2, user_id=2, company_name='Другой'))
        db.session.add(Supplier(id=2, name='Андрей (sex-opt.ru)', code='andrey'))
        db.session.add(Supplier(id=1, name='Sexoptovik', code='sexoptovik'))

        def sp(id_, supplier_id, n_photos, external_id=None):
            return SupplierProduct(
                id=id_, supplier_id=supplier_id,
                external_id=external_id or f'EXT-{id_}',
                title=f'Товар {id_}',
                photo_urls_json=json.dumps(
                    [{'original': f'http://sup/{id_}/{i}.jpg'} for i in range(n_photos)]),
            )

        def product(id_, seller_id, nm_id, wb_photos, active=True, title='Товар', vc=None):
            return Product(
                id=id_, seller_id=seller_id, nm_id=nm_id,
                vendor_code=vc or f'VC-{id_}', title=title,
                photos_json=json.dumps(list(range(1, wb_photos + 1))),
                is_active=active,
            )

        def link(id_, seller_id, product_id, sp_id, supplier_id):
            return ImportedProduct(
                id=id_, seller_id=seller_id, product_id=product_id,
                supplier_product_id=sp_id, supplier_id=supplier_id,
            )

        db.session.add_all([
            # seller 1, andrey: карточка с 3 фото на WB, у поставщика 17 → delta +14
            sp(101, 2, 17), product(11, 1, 5001, 3, title='Гель для тела'),
            link(1001, 1, 11, 101, 2),
            # seller 1, andrey: WB 5, поставщик 5 → нет новых
            sp(102, 2, 5), product(12, 1, 5002, 5), link(1002, 1, 12, 102, 2),
            # seller 1, andrey: WB 4, поставщик 2 → delta отрицательная
            sp(103, 2, 2), product(13, 1, 5003, 4), link(1003, 1, 13, 103, 2),
            # seller 1, sexoptovik: WB 1, поставщик 9 → новые есть, но другой supplier
            sp(104, 1, 9), product(14, 1, 5004, 1), link(1004, 1, 14, 104, 1),
            # seller 1, andrey: неактивная карточка — исключается
            sp(105, 2, 20), product(15, 1, 5005, 3, active=False), link(1005, 1, 15, 105, 2),
            # seller 2, andrey: чужой продавец
            sp(106, 2, 15), product(16, 2, 5006, 3), link(1006, 2, 16, 106, 2),
        ])
        db.session.commit()


class TestQueryUpdateRows(HubDBTestCase):
    def _rows(self, **kw):
        from services.supplier_update_hub import query_update_rows
        kw.setdefault('seller_id', 1)
        return query_update_rows(**kw)

    def test_only_new_returns_cards_with_more_supplier_photos(self):
        rows, total = self._rows(only_new=True)
        ids = [r['product'].id for r in rows]
        self.assertEqual(total, 2)
        self.assertEqual(set(ids), {11, 14})

    def test_counts_and_delta(self):
        rows, _ = self._rows(only_new=True, supplier_id=2)
        row = rows[0]
        self.assertEqual(row['product'].id, 11)
        self.assertEqual(row['wb_count'], 3)
        self.assertEqual(row['supplier_count'], 17)
        self.assertEqual(row['delta'], 14)
        self.assertEqual(row['supplier_name'], 'Андрей (sex-opt.ru)')

    def test_supplier_filter(self):
        rows, total = self._rows(only_new=False, supplier_id=2)
        self.assertEqual(total, 3)  # 11, 12, 13 (15 неактивна, 16 чужая)
        self.assertTrue(all(r['supplier_id'] == 2 for r in rows))

    def test_all_filter_includes_cards_without_new_photos(self):
        rows, total = self._rows(only_new=False)
        self.assertEqual(total, 4)  # 11..14

    def test_seller_isolation(self):
        rows, total = self._rows(seller_id=2, only_new=True)
        self.assertEqual([r['product'].id for r in rows], [16])

    def test_search_by_title_and_vendor_code(self):
        rows, _ = self._rows(only_new=False, search='Гель')
        self.assertEqual([r['product'].id for r in rows], [11])
        rows, _ = self._rows(only_new=False, search='VC-13')
        self.assertEqual([r['product'].id for r in rows], [13])

    def test_orders_by_delta_desc(self):
        rows, _ = self._rows(only_new=True)
        deltas = [r['delta'] for r in rows]
        self.assertEqual(deltas, sorted(deltas, reverse=True))

    def test_pagination(self):
        rows, total = self._rows(only_new=False, page=1, per_page=2)
        self.assertEqual(total, 4)
        self.assertEqual(len(rows), 2)

    def test_expand_filter_matches_query(self):
        from services.supplier_update_hub import expand_filter_to_ids
        ids = expand_filter_to_ids(seller_id=1, only_new=True)
        self.assertEqual(set(ids), {11, 14})


class TestSupplierChips(HubDBTestCase):
    def test_chips_totals(self):
        from services.supplier_update_hub import get_supplier_chips
        chips = {c['supplier_id']: c for c in get_supplier_chips(1)}
        self.assertEqual(chips[2]['total'], 3)
        self.assertEqual(chips[2]['with_new'], 1)
        self.assertEqual(chips[1]['total'], 1)
        self.assertEqual(chips[1]['with_new'], 1)


class TestSupplierUpdateJobCreation(HubDBTestCase):
    def test_check_and_insert_is_single_active_job_per_seller_and_type(self):
        from services.supplier_update_hub import (
            JOB_TYPE,
            SupplierUpdateJobAlreadyActive,
            create_supplier_update_job,
        )

        created = create_supplier_update_job(
            seller_id=1,
            job_type=JOB_TYPE,
            product_ids=[11, 14],
            progress={'skipped': 0},
        )
        self.assertEqual(created.status, 'pending')
        self.assertEqual(created.get_progress()['product_ids'], [11, 14])

        with self.assertRaises(SupplierUpdateJobAlreadyActive) as raised:
            create_supplier_update_job(
                seller_id=1,
                job_type=JOB_TYPE,
                product_ids=[12],
                progress={'skipped': 0},
            )
        self.assertEqual(raised.exception.job_uid, created.job_uid)
        self.assertEqual(BackgroundJob.query.count(), 1)

    def test_photo_and_verify_creation_scopes_are_independent(self):
        from services.supplier_update_hub import (
            JOB_TYPE,
            VERIFY_JOB_TYPE,
            create_supplier_update_job,
        )

        photo = create_supplier_update_job(
            seller_id=1,
            job_type=JOB_TYPE,
            product_ids=[11],
            progress={},
        )
        verify = create_supplier_update_job(
            seller_id=1,
            job_type=VERIFY_JOB_TYPE,
            product_ids=[11],
            progress={'attempts': 0},
        )
        self.assertNotEqual(photo.job_uid, verify.job_uid)
        self.assertEqual(BackgroundJob.query.count(), 2)

    def test_job_payload_validation_is_strict(self):
        from services.supplier_update_hub import (
            JOB_TYPE,
            create_supplier_update_job,
        )

        for product_ids in ([True], ['11'], [0], [11, 11], []):
            with self.subTest(product_ids=product_ids):
                with self.assertRaises(ValueError):
                    create_supplier_update_job(
                        seller_id=1,
                        job_type=JOB_TYPE,
                        product_ids=product_ids,
                        progress={},
                    )


class TestBuildTargetPhotoSet(HubDBTestCase):
    def _sp(self, id_):
        return db.session.get(SupplierProduct, id_)

    def _product(self, id_):
        return db.session.get(Product, id_)

    def test_composes_supplier_urls_with_standard_pins(self):
        from services.supplier_update_hub import build_target_photo_set
        media = [{'filename': 'logo.jpg', 'type': 'photo',
                  'position': 'first', 'mode': 'pin', 'order': 0}]
        with patch('services.supplier_update_hub.get_standard_media', return_value=media), \
             patch('services.supplier_update_hub.get_min_photos', return_value=4):
            target = build_target_photo_set(self._sp(101), self._product(11), seller_id=1)
        self.assertEqual(len(target), 18)  # 1 пин + 17 фото поставщика
        self.assertIn('/media/standard/1/logo.jpg', target[0])
        self.assertIn('/photos/public/101/0.jpg', target[1])
        self.assertIn('/photos/public/101/16.jpg', target[-1])

    def test_falls_back_to_supplier_urls_without_standard_media(self):
        from services.supplier_update_hub import build_target_photo_set
        with patch('services.supplier_update_hub.get_standard_media', return_value=[]), \
             patch('services.supplier_update_hub.get_min_photos', return_value=4):
            target = build_target_photo_set(self._sp(101), self._product(11), seller_id=1)
        self.assertEqual(len(target), 17)
        self.assertIn('/photos/public/101/0.jpg', target[0])

    def test_caps_at_30(self):
        from services.supplier_update_hub import build_target_photo_set
        sp = self._sp(101)
        sp.photo_urls_json = json.dumps(
            [{'original': f'http://sup/x/{i}.jpg'} for i in range(35)])
        with patch('services.supplier_update_hub.get_standard_media', return_value=[]), \
             patch('services.supplier_update_hub.get_min_photos', return_value=4):
            target = build_target_photo_set(sp, self._product(11), seller_id=1)
        self.assertEqual(len(target), 30)

    def test_empty_when_supplier_has_no_photos(self):
        from services.supplier_update_hub import build_target_photo_set
        sp = self._sp(101)
        sp.photo_urls_json = '[]'
        with patch('services.supplier_update_hub.get_standard_media', return_value=[]), \
             patch('services.supplier_update_hub.get_min_photos', return_value=4):
            self.assertEqual(build_target_photo_set(sp, self._product(11), 1), [])


class TestRunPhotosJob(HubDBTestCase):
    def _make_job(self, product_ids):
        job = BackgroundJob(job_uid='j-test', seller_id=1,
                            job_type='supplier_photos_update',
                            status='pending', total=len(product_ids))
        db.session.add(job)
        db.session.commit()
        return job

    def _run(self, product_ids, apply_result=None):
        from services.supplier_update_hub import run_photos_job
        apply_result = apply_result or {
            'success': True, 'fields_applied': ['photos'],
            'photos': {'uploaded': 2}, 'wb_sync': True, 'error': None,
        }
        self._make_job(product_ids)
        with patch('services.supplier_update_hub.WildberriesAPIClient'), \
             patch('services.supplier_update_hub.verify_cards_on_wb',
                   return_value={'items': [], 'summary': {}}), \
             patch('services.supplier_enrichment.EnrichmentService.apply_enrichment',
                   return_value=apply_result) as mock_apply:
            run_photos_job(self.app, 'j-test', 1, product_ids)
        return mock_apply

    def test_success_counters_and_history(self):
        mock_apply = self._run([11, 12])
        job = BackgroundJob.query.filter_by(job_uid='j-test').first()
        self.assertEqual(job.status, 'completed')
        self.assertEqual(job.processed, 2)
        self.assertEqual(job.succeeded, 2)
        self.assertEqual(job.failed_count, 0)
        self.assertEqual(mock_apply.call_count, 2)
        self.assertNotIn('_photo_job_claim_token', job.get_progress())
        # запись в историю массовых операций
        h = BulkEditHistory.query.first()
        self.assertIsNotNone(h)

    def test_skips_foreign_and_missing_products(self):
        self._run([16, 99999])  # чужой продавец и несуществующий id
        job = BackgroundJob.query.filter_by(job_uid='j-test').first()
        self.assertEqual(job.succeeded, 0)
        self.assertEqual(job.processed, 2)

    def test_photo_noop_counts_as_skipped(self):
        self._run([11], apply_result={
            'success': True,
            'fields_applied': [],
            'photos': {'skipped': True, 'reason': 'empty_photo_list'},
            'wb_sync': False,
            'error': None,
        })
        job = BackgroundJob.query.filter_by(job_uid='j-test').first()
        self.assertEqual(job.succeeded, 0)
        self.assertEqual(job.failed_count, 0)
        self.assertEqual(job.get_result()['skipped'], 1)
        self.assertEqual(job.status, 'completed')

    def test_failed_apply_counts_as_failed(self):
        self._run([11], apply_result={
            'success': False, 'fields_applied': [], 'old_quality': 40,
            'new_quality': 40, 'wb_sync': False, 'error': 'WB rejected'})
        job = BackgroundJob.query.filter_by(job_uid='j-test').first()
        self.assertEqual(job.failed_count, 1)
        self.assertEqual(job.status, 'completed')

    def test_pending_previous_photo_write_defers_without_advancing_cursor(self):
        from services.supplier_update_hub import run_photos_job

        self._make_job([11])
        deferred = {
            'success': False,
            'fields_applied': [],
            'photos': {
                'skipped': True,
                'reason': 'previous_photo_write_pending',
            },
            'wb_sync': False,
            'error': 'Предыдущая отправка фото ещё проверяется',
            'reconciliation_pending': True,
            'deferred': True,
        }
        success = {
            'success': True,
            'fields_applied': ['photos'],
            'photos': {'uploaded': 1},
            'wb_sync': True,
            'error': None,
        }
        with (
            patch('services.supplier_update_hub.WildberriesAPIClient'),
            patch(
                'services.supplier_enrichment.EnrichmentService.apply_enrichment',
                side_effect=[deferred, success],
            ) as apply,
        ):
            run_photos_job(self.app, 'j-test', 1, [11])
            waiting = BackgroundJob.query.filter_by(job_uid='j-test').first()
            self.assertEqual(waiting.status, 'pending')
            self.assertEqual(waiting.processed, 0)
            self.assertEqual(
                waiting.get_progress().get('current_product_id'), 11,
            )
            self.assertNotIn(
                '_photo_job_claim_token', waiting.get_progress(),
            )

            run_photos_job(self.app, 'j-test', 1, [])

        db.session.expire_all()
        job = BackgroundJob.query.filter_by(job_uid='j-test').first()
        self.assertEqual(job.status, 'completed')
        self.assertEqual(job.processed, 1)
        self.assertEqual(job.succeeded, 1)
        self.assertEqual(apply.call_count, 2)

    def test_bulk_uses_multipart_even_with_public_base_url(self):
        from services.supplier_update_hub import run_photos_job

        self._make_job([11, 12])
        self.app.config['PUBLIC_BASE_URL'] = 'https://platform.example'
        result = {
            'success': True,
            'fields_applied': ['photos'],
            'photos': {'uploaded': 2},
            'wb_sync': True,
            'error': None,
        }
        with patch('services.supplier_update_hub.WildberriesAPIClient'), \
             patch('services.supplier_update_hub.build_target_photo_set') as build_target, \
             patch('services.supplier_update_hub.verify_cards_on_wb',
                   return_value={'items': [], 'summary': {}}), \
             patch('services.supplier_enrichment.EnrichmentService.apply_enrichment',
                   return_value=result) as apply_enrichment:
            run_photos_job(self.app, 'j-test', 1, [11, 12])

        job = BackgroundJob.query.filter_by(job_uid='j-test').first()
        self.assertEqual(job.status, 'completed')
        self.assertEqual(job.succeeded, 2)
        self.assertEqual(apply_enrichment.call_count, 2)
        build_target.assert_not_called()
        self.assertEqual(
            apply_enrichment.call_args.args[2:4],
            (['photos'], 'smart_merge'),
        )

    def test_worker_that_loses_claim_cannot_advance_cursor_after_io(self):
        from services.supplier_update_hub import run_photos_job

        self._make_job([11])

        def steal_claim(*_args, **_kwargs):
            job = BackgroundJob.query.filter_by(job_uid='j-test').first()
            progress = job.get_progress()
            self.assertIn('_photo_job_claim_token', progress)
            progress['_photo_job_claim_token'] = 'replacement-worker'
            job.set_progress(progress)
            job.status = 'running'
            db.session.commit()
            return {
                'success': True,
                'fields_applied': ['photos'],
                'photos': {'uploaded': 1},
                'wb_sync': True,
                'error': None,
            }

        with (
            patch('services.supplier_update_hub.WildberriesAPIClient'),
            patch(
                'services.supplier_enrichment.EnrichmentService.apply_enrichment',
                side_effect=steal_claim,
            ),
        ):
            run_photos_job(self.app, 'j-test', 1, [11])

        db.session.expire_all()
        job = BackgroundJob.query.filter_by(job_uid='j-test').first()
        self.assertEqual(job.status, 'running')
        self.assertEqual(job.processed, 0)
        self.assertEqual(job.succeeded, 0)
        self.assertEqual(
            job.get_progress()['_photo_job_claim_token'],
            'replacement-worker',
        )

    def test_client_construction_failure_releases_claim_for_retry(self):
        from services.supplier_update_hub import run_photos_job

        self._make_job([11])
        with patch(
            'services.supplier_update_hub.WildberriesAPIClient',
            side_effect=RuntimeError('temporary client failure'),
        ):
            run_photos_job(self.app, 'j-test', 1, [11])

        job = BackgroundJob.query.filter_by(job_uid='j-test').first()
        self.assertEqual(job.status, 'pending')
        self.assertEqual(job.processed, 0)
        self.assertNotIn('_photo_job_claim_token', job.get_progress())
        self.assertEqual(job.get_progress()['worker_attempts'], 1)

    def test_cancelled_job_stops_processing(self):
        from services.supplier_update_hub import run_photos_job
        job = self._make_job([11, 12])
        job.status = 'cancelled'
        db.session.commit()
        with patch('services.supplier_update_hub.WildberriesAPIClient'), \
             patch('services.supplier_enrichment.EnrichmentService.apply_enrichment') as mock_apply:
            run_photos_job(self.app, 'j-test', 1, [11, 12])
        self.assertEqual(mock_apply.call_count, 0)


if __name__ == '__main__':
    unittest.main()
