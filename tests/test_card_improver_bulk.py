# -*- coding: utf-8 -*-
"""Тесты apply_card_updates_bulk: тексты — одним батчем cards/update, фото — по карточке."""
import json
import unittest
from unittest.mock import patch


class FakeWBClient:
    def __init__(self, outcome=None, raise_batch=False):
        self.calls = []
        self.outcome = outcome
        self.raise_batch = raise_batch

    def update_cards_merged(self, nm_updates, log_to_db=False, seller_id=None, chunk_size=1000):
        self.calls.append({'method': 'update_cards_merged',
                           'nm_updates': {int(k): dict(v) for k, v in nm_updates.items()},
                           'seller_id': seller_id})
        if self.raise_batch:
            raise RuntimeError('WB API недоступен')
        if self.outcome is not None:
            return self.outcome
        return {'sent': [int(k) for k in nm_updates], 'missing': [], 'invalid': {}, 'requests': 1}

    def update_card(self, *a, **kw):
        self.calls.append({'method': 'update_card'})
        raise AssertionError('bulk-путь не должен звать поштучный update_card')

    def upload_photos_by_url(self, nm_id, photo_urls, seller_id=None):
        self.calls.append({'method': 'upload_photos_by_url', 'nm_id': nm_id,
                           'photo_urls': list(photo_urls)})
        return {'error': False}


class FakeSeller:
    id = 7


def _product(pid, nm_id):
    class P:
        pass
    p = P()
    p.id = pid
    p.nm_id = nm_id
    p.vendor_code = f'VC-{pid}'
    p.title = 'Старый'
    p.brand = 'OldBrand'
    p.description = 'кратко'
    p.object_name = 'Платье'
    p.subject_id = 999
    p.price = 1990
    p.discount_price = 1490
    p.quantity = 5
    p.characteristics_json = json.dumps([{'id': 1, 'value': 'синий'}])
    p.dimensions_json = json.dumps({'length': 10})
    p.photos_json = json.dumps(['a.jpg'])
    p.sizes_json = None
    p.is_active = True
    p.quality_score = 40.0
    p.quality_breakdown_json = None
    p.quality_checked_at = None
    p.nm_rating = 7.0
    p.wb_feedback_rating = 4.2
    p.nm_rating_checked_at = None
    p.updated_at = None
    return p


class BulkApplyTestBase(unittest.TestCase):
    def setUp(self):
        self.history_records = []

        def fake_snapshot(product):
            return {'title': product.title, 'brand': product.brand}

        def fake_recompute(product, capture_history=True):
            return {'score': 60.0, 'dimensions': {}}

        class FakeHistory:
            def __init__(self, **kw):
                self.__dict__.update(kw)

        outer = self

        class FakeSession:
            def add(self, obj):
                outer.history_records.append(obj)

            def commit(self):
                pass

        self.patches = [
            patch('services.card_improver._create_product_snapshot', side_effect=fake_snapshot),
            patch('services.card_improver.recompute_and_persist', side_effect=fake_recompute),
            patch('services.card_improver.CardEditHistory',
                  lambda **kw: FakeHistory(**kw)),
            patch('services.card_improver.db'),
        ]
        started = [p.start() for p in self.patches]
        started[3].session = FakeSession()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def _run(self, items, client=None):
        from services.card_improver import apply_card_updates_bulk
        client = client or FakeWBClient()
        results = apply_card_updates_bulk(items, FakeSeller(), client, source='test-bulk')
        return results, client


class TestBulkTextUpdates(BulkApplyTestBase):
    def test_texts_go_in_single_merged_call(self):
        p1, p2 = _product(1, 111), _product(2, 222)
        results, client = self._run([
            (p1, {'title': 'Новый 1', 'brand': 'NewBrand'}),
            (p2, {'description': 'Новое описание'}),
        ])
        merged_calls = [c for c in client.calls if c['method'] == 'update_cards_merged']
        self.assertEqual(len(merged_calls), 1)
        self.assertEqual(set(merged_calls[0]['nm_updates']), {111, 222})
        self.assertTrue(results[1]['success'])
        self.assertTrue(results[2]['success'])
        self.assertEqual(p1.title, 'Новый 1')
        self.assertEqual(p1.brand, 'NewBrand')
        self.assertEqual(p2.description, 'Новое описание')
        self.assertIn('title', results[1]['fields_applied'])

    def test_missing_card_fails_without_local_apply(self):
        p1 = _product(1, 111)
        client = FakeWBClient(outcome={'sent': [], 'missing': [111], 'invalid': {}, 'requests': 0})
        results, _ = self._run([(p1, {'title': 'Новый'})], client)
        self.assertFalse(results[1]['success'])
        self.assertFalse(results[1]['wb_sync'])
        self.assertEqual(p1.title, 'Старый')
        self.assertIn('не найдена', results[1]['error'])

    def test_invalid_card_error_propagates(self):
        p1 = _product(1, 111)
        client = FakeWBClient(outcome={'sent': [], 'missing': [],
                                       'invalid': {111: 'нет vendorCode'}, 'requests': 0})
        results, _ = self._run([(p1, {'title': 'Новый'})], client)
        self.assertFalse(results[1]['success'])
        self.assertEqual(results[1]['error'], 'нет vendorCode')

    def test_failed_send_from_bisect_propagates_per_card(self):
        """Карточки из failed-карты (бисекция чанка) получают свою ошибку."""
        p1, p2 = _product(1, 111), _product(2, 222)
        client = FakeWBClient(outcome={'sent': [222], 'missing': [], 'invalid': {},
                                       'failed': {111: 'WB отклонил карточку'},
                                       'requests': 2})
        results, _ = self._run([(p1, {'title': 'Новый'}), (p2, {'brand': 'B'})], client)
        self.assertFalse(results[1]['success'])
        self.assertEqual(results[1]['error'], 'WB отклонил карточку')
        self.assertEqual(p1.title, 'Старый')
        self.assertTrue(results[2]['success'])
        self.assertEqual(p2.brand, 'B')

    def test_batch_exception_marks_all_failed(self):
        p1, p2 = _product(1, 111), _product(2, 222)
        client = FakeWBClient(raise_batch=True)
        results, _ = self._run([(p1, {'title': 'Новый'}), (p2, {'brand': 'X'})], client)
        self.assertFalse(results[1]['success'])
        self.assertFalse(results[2]['success'])
        self.assertEqual(p1.title, 'Старый')

    def test_history_written_per_card(self):
        p1, p2 = _product(1, 111), _product(2, 222)
        self._run([(p1, {'title': 'Новый 1'}), (p2, {'brand': 'B'})])
        self.assertEqual(len(self.history_records), 2)


class TestBulkPhotos(BulkApplyTestBase):
    def test_photos_uploaded_per_card_via_media_save(self):
        p1 = _product(1, 111)
        results, client = self._run([(p1, {'photos': ['http://x/1.jpg', 'http://x/2.jpg']})])
        photo_calls = [c for c in client.calls if c['method'] == 'upload_photos_by_url']
        self.assertEqual(len(photo_calls), 1)
        self.assertEqual(photo_calls[0]['nm_id'], 111)
        self.assertTrue(results[1]['success'])
        self.assertEqual(json.loads(p1.photos_json), ['http://x/1.jpg', 'http://x/2.jpg'])
        # текстового батча не было — нечего слать
        self.assertEqual([c for c in client.calls if c['method'] == 'update_cards_merged'], [])


if __name__ == '__main__':
    unittest.main()
