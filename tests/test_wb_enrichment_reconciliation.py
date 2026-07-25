# -*- coding: utf-8 -*-
"""Durable confirmation tests for asynchronous WB enrichment writes."""
import json
import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

from flask import Flask

from models import CardEditHistory, Product, Seller, User, db
from services.wb_enrichment_reconciliation import (
    process_due_reconciliations,
    reconciliation_max_attempts,
)


class WBEnrichmentReconciliationTestCase(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(
            SQLALCHEMY_DATABASE_URI='sqlite:///:memory:',
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            SECRET_KEY='reconciliation-test',
        )
        db.init_app(self.app)
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()

        user = User(
            id=1,
            username='reconcile-user',
            email='reconcile@example.test',
            password_hash='test',
        )
        seller = Seller(id=1, user_id=1, company_name='Reconcile seller')
        seller.wb_api_key = 'test-key'
        product = Product(
            id=10,
            seller_id=1,
            nm_id=900010,
            vendor_code='VC-10',
            title='Старое название',
            brand='Brand',
            description='Описание',
            characteristics_json='[]',
            dimensions_json='{}',
            photos_json='[]',
        )
        db.session.add_all([user, seller, product])
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def _client(self, live_card, errors=None):
        client = MagicMock()
        client.fetch_cards_by_nm_ids.return_value = {900010: live_card}
        client.get_cards_error_list.return_value = errors or []
        return client

    def _content_history(self, *, status='submitted'):
        before = {
            'nmID': 900010,
            'vendorCode': 'VC-10',
            'title': 'Старое название',
        }
        after = dict(before, title='Старое название с дополнением')
        history = CardEditHistory(
            product_id=10,
            seller_id=1,
            action='update',
            changed_fields=['title'],
            snapshot_before=before,
            snapshot_after=after,
            wb_synced=False,
            wb_sync_status=status,
            wb_reconcile_due_at=datetime.utcnow() - timedelta(seconds=1),
            wb_reconcile_attempts=0,
        )
        db.session.add(history)
        db.session.commit()
        return history, before, after

    def test_submitted_content_is_mirrored_only_after_exact_live_confirmation(self):
        history, _before, after = self._content_history()
        client = self._client(after)

        summary = process_due_reconciliations(
            now=datetime.utcnow(),
            client_factory=lambda _key: client,
        )

        db.session.refresh(history)
        product = db.session.get(Product, 10)
        self.assertEqual(summary['confirmed'], 1)
        self.assertTrue(history.wb_synced)
        self.assertEqual(history.wb_sync_status, 'success')
        self.assertEqual(history.wb_reconcile_code, 'content_confirmed')
        self.assertEqual(product.title, after['title'])

    def test_provider_error_is_terminal_and_never_replayed(self):
        history, before, _after = self._content_history(status='pending')
        client = self._client(before, errors=[{
            'nmID': 900010,
            'vendorCode': 'VC-10',
            'errors': ['Недопустимое значение'],
            'updatedAt': datetime.utcnow().isoformat(),
        }])

        process_due_reconciliations(
            now=datetime.utcnow(),
            client_factory=lambda _key: client,
        )

        db.session.refresh(history)
        self.assertFalse(history.wb_synced)
        self.assertEqual(history.wb_sync_status, 'failed')
        self.assertEqual(history.wb_reconcile_code, 'provider_rejected')
        self.assertIsNone(history.wb_reconcile_due_at)

    def test_mixed_content_requires_two_observations_before_conflict(self):
        history, _before, _after = self._content_history()
        mixed = {
            'nmID': 900010,
            'vendorCode': 'VC-10',
            'title': 'Ручное третье значение',
        }
        client = self._client(mixed)
        now = datetime.utcnow()

        process_due_reconciliations(
            now=now,
            client_factory=lambda _key: client,
        )
        db.session.refresh(history)
        self.assertEqual(history.wb_sync_status, 'submitted')
        self.assertEqual(history.wb_reconcile_attempts, 1)
        self.assertIsNotNone(history.wb_reconcile_due_at)

        history.wb_reconcile_due_at = now
        db.session.commit()
        process_due_reconciliations(
            now=now + timedelta(seconds=1),
            client_factory=lambda _key: client,
        )
        db.session.refresh(history)
        self.assertEqual(history.wb_sync_status, 'conflict')
        self.assertEqual(history.wb_reconcile_code, 'content_conflict')

    def test_later_receipt_exception_does_not_reschedule_confirmed_sibling(self):
        first, _before, _after = self._content_history()
        second = CardEditHistory(
            product_id=10,
            seller_id=1,
            action='update',
            changed_fields=['description'],
            snapshot_before={'description': 'Описание'},
            snapshot_after={'description': 'Описание с дополнением'},
            wb_synced=False,
            wb_sync_status='submitted',
            wb_reconcile_due_at=datetime.utcnow() - timedelta(seconds=1),
            wb_reconcile_attempts=0,
        )
        db.session.add(second)
        db.session.commit()
        client = self._client({
            'nmID': 900010,
            'title': 'Старое название с дополнением',
            'description': 'Описание',
        })

        def reconcile(history, _product, _live, now, **_kwargs):
            if history.id == first.id:
                history.wb_synced = True
                history.wb_sync_status = 'success'
                history.wb_reconcile_due_at = None
                history.wb_reconciled_at = now
                history.wb_reconcile_code = 'content_confirmed'
                return
            raise RuntimeError('malformed sibling receipt')

        with patch(
            'services.wb_enrichment_reconciliation._reconcile_content',
            side_effect=reconcile,
        ):
            process_due_reconciliations(
                now=datetime.utcnow(),
                client_factory=lambda _key: client,
            )

        db.session.refresh(first)
        db.session.refresh(second)
        self.assertEqual(first.wb_sync_status, 'success')
        self.assertIsNone(first.wb_reconcile_due_at)
        self.assertEqual(first.wb_reconcile_attempts, 1)
        self.assertEqual(second.wb_sync_status, 'submitted')
        self.assertIsNotNone(second.wb_reconcile_due_at)
        self.assertEqual(second.wb_reconcile_attempts, 1)
        self.assertEqual(second.wb_reconcile_code, 'provider_read_failed')

    def test_photo_hash_tokens_become_exact_provider_urls(self):
        old_url = 'https://basket-01.wbbasket.ru/old.webp'
        new_url = 'https://basket-01.wbbasket.ru/new.webp'
        old_fp = {'pixel_sha': 'old', 'dhash': 1, 'ahash': 1}
        new_fp = {'pixel_sha': 'new', 'dhash': 100, 'ahash': 100}
        history = CardEditHistory(
            product_id=10,
            seller_id=1,
            action='update',
            changed_fields=['photos'],
            snapshot_before={'photos': [old_url]},
            snapshot_after={
                'photos': [old_url, 'enrichment-sha256:new'],
            },
            merge_decisions={'photos': {
                'live_count': 1,
                'live_position_fingerprints': [old_fp],
                'expected_append_fingerprints': [new_fp],
                'upload': {'uploaded': 1, 'uncertain': 0, 'failed': 0},
            }},
            wb_synced=False,
            wb_sync_status='submitted',
            wb_reconcile_due_at=datetime.utcnow() - timedelta(seconds=1),
        )
        db.session.add(history)
        db.session.commit()
        client = self._client({
            'nmID': 900010,
            'photos': [{'tm': old_url}, {'tm': new_url}],
        })

        with patch(
            'services.wb_enrichment_reconciliation.fingerprint_remote_photo',
            side_effect=[old_fp, new_fp],
        ):
            process_due_reconciliations(
                now=datetime.utcnow(),
                client_factory=lambda _key: client,
            )

        db.session.refresh(history)
        product = db.session.get(Product, 10)
        self.assertEqual(history.wb_sync_status, 'success')
        self.assertEqual(history.snapshot_after['photos'], [old_url, new_url])
        self.assertEqual(json.loads(product.photos_json), [old_url, new_url])

    def test_square_variant_confirms_source_when_portrait_thumbnail_differs(self):
        old_tm = 'https://basket-01.wbbasket.ru/old-tm.webp'
        old_square = 'https://basket-01.wbbasket.ru/old-square.webp'
        new_tm = 'https://basket-01.wbbasket.ru/new-tm.webp'
        new_square = 'https://basket-01.wbbasket.ru/new-square.webp'
        old_fp = {'pixel_sha': 'old', 'dhash': 1, 'ahash': 1}
        new_fp = {'pixel_sha': 'new', 'dhash': 100, 'ahash': 100}
        history = CardEditHistory(
            product_id=10,
            seller_id=1,
            action='update',
            changed_fields=['photos'],
            snapshot_before={'photos': [old_tm]},
            snapshot_after={
                'photos': [old_tm, 'enrichment-sha256:new'],
            },
            merge_decisions={'photos': {
                'policy': 'wb-enrichment-preserve-v3',
                'live_count': 1,
                'live_position_fingerprints': [old_fp],
                'expected_append_fingerprints': [new_fp],
                'upload': {'uploaded': 1, 'uncertain': 0, 'failed': 0},
            }},
            wb_synced=False,
            wb_sync_status='submitted',
            wb_reconcile_due_at=datetime.utcnow() - timedelta(seconds=1),
        )
        db.session.add(history)
        db.session.commit()
        client = self._client({
            'nmID': 900010,
            'photos': [
                {'tm': old_tm, 'square': old_square},
                {'tm': new_tm, 'square': new_square},
            ],
        })

        fingerprints = {
            old_tm: old_fp,
            new_square: new_fp,
        }
        with patch(
            'services.wb_enrichment_reconciliation.fingerprint_remote_photo',
            side_effect=lambda url: fingerprints[url],
        ) as fingerprint:
            process_due_reconciliations(
                now=datetime.utcnow(),
                client_factory=lambda _key: client,
            )

        db.session.refresh(history)
        self.assertEqual(
            history.wb_sync_status,
            'success',
            (
                history.wb_reconcile_code,
                [call.args[0] for call in fingerprint.call_args_list],
            ),
        )
        self.assertEqual(history.wb_reconcile_code, 'photo_confirmed')
        self.assertEqual(
            [call.args[0] for call in fingerprint.call_args_list],
            [old_tm, new_square],
        )

    def test_manual_photo_after_expected_positions_is_preserved_not_hashed(self):
        old_url = 'https://basket-01.wbbasket.ru/old.webp'
        new_url = 'https://basket-01.wbbasket.ru/new.webp'
        manual_url = 'https://basket-01.wbbasket.ru/manual-extra.webp'
        old_fp = {'pixel_sha': 'old', 'dhash': 1, 'ahash': 1}
        new_fp = {'pixel_sha': 'new', 'dhash': 100, 'ahash': 100}
        history = CardEditHistory(
            product_id=10,
            seller_id=1,
            action='update',
            changed_fields=['photos'],
            snapshot_before={'photos': [old_url]},
            snapshot_after={
                'photos': [old_url, 'enrichment-sha256:new'],
            },
            merge_decisions={'photos': {
                'live_count': 1,
                'live_position_fingerprints': [old_fp],
                'expected_append_fingerprints': [new_fp],
                'upload': {'uploaded': 1, 'uncertain': 0, 'failed': 0},
            }},
            wb_synced=False,
            wb_sync_status='submitted',
            wb_reconcile_due_at=datetime.utcnow() - timedelta(seconds=1),
        )
        db.session.add(history)
        db.session.commit()
        client = self._client({
            'nmID': 900010,
            'photos': [
                {'tm': old_url}, {'tm': new_url}, {'tm': manual_url},
            ],
        })

        with patch(
            'services.wb_enrichment_reconciliation.fingerprint_remote_photo',
            side_effect=[old_fp, new_fp],
        ) as fingerprint:
            process_due_reconciliations(
                now=datetime.utcnow(),
                client_factory=lambda _key: client,
            )

        db.session.refresh(history)
        product = db.session.get(Product, 10)
        self.assertEqual(fingerprint.call_count, 2)
        self.assertEqual(history.wb_sync_status, 'success')
        self.assertEqual(
            history.snapshot_after['photos'],
            [old_url, new_url, manual_url],
        )
        self.assertEqual(
            json.loads(product.photos_json),
            [old_url, new_url, manual_url],
        )

    def test_unobserved_ambiguous_photo_exhausts_without_write_retry(self):
        old_url = 'https://basket-01.wbbasket.ru/old.webp'
        old_fp = {'pixel_sha': 'old', 'dhash': 1, 'ahash': 1}
        new_fp = {'pixel_sha': 'new', 'dhash': 100, 'ahash': 100}
        history = CardEditHistory(
            product_id=10,
            seller_id=1,
            action='update',
            changed_fields=['photos'],
            snapshot_before={'photos': [old_url]},
            snapshot_after={
                'photos': [old_url, 'enrichment-sha256:new'],
            },
            merge_decisions={'photos': {
                'live_count': 1,
                'live_position_fingerprints': [old_fp],
                'expected_append_fingerprints': [new_fp],
                'upload': {'uploaded': 0, 'uncertain': 1, 'failed': 0},
            }},
            wb_synced=False,
            wb_sync_status='uncertain',
            wb_reconcile_due_at=datetime.utcnow() - timedelta(seconds=1),
            wb_reconcile_attempts=reconciliation_max_attempts() - 1,
        )
        db.session.add(history)
        db.session.commit()
        client = self._client({
            'nmID': 900010,
            'photos': [{'tm': old_url}],
        })

        with patch(
            'services.wb_enrichment_reconciliation.fingerprint_remote_photo',
            return_value=old_fp,
        ):
            process_due_reconciliations(
                now=datetime.utcnow(),
                client_factory=lambda _key: client,
            )

        db.session.refresh(history)
        self.assertEqual(history.wb_sync_status, 'failed')
        self.assertEqual(history.wb_reconcile_code, 'photo_not_applied')
        self.assertIsNone(history.wb_reconcile_due_at)


if __name__ == '__main__':
    unittest.main()
