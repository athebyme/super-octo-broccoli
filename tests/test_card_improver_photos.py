# -*- coding: utf-8 -*-
"""
TDD-тесты для Task 5: apply_card_updates — загрузка фото через WB media/save.

Проверяем, что при наличии непустого списка URL в updates['photos']:
 - wb_client.upload_photos_by_url вызывается с упорядоченным списком;
 - product.photos_json обновляется;
 - 'photos' попадает в fields_applied;
 - wb_sync=True;
 - создаётся одна запись CardEditHistory с changed_fields=['photos'].

При ошибке upload_photos_by_url:
 - success=False, wb_sync=False, error установлен, статус 'failed'.
"""
import json
import unittest
from unittest.mock import patch


class FakeWBClient:
    """Фейковый WB-клиент — записывает вызовы, не ходит в сеть."""
    def __init__(self):
        self.calls = []

    def update_card(self, nm_id, updates, merge_with_existing=True, seller_id=None):
        self.calls.append(('update_card', nm_id, updates))
        return {'data': {}, 'error': False}

    def upload_photos_by_url(self, nm_id, photo_urls, seller_id=None):
        self.calls.append(('upload_photos_by_url', nm_id, list(photo_urls), seller_id))
        return {'data': {}, 'error': False}


class FakeWBClientRaisesOnPhotos:
    """Фейковый WB-клиент, который бросает исключение при upload_photos_by_url."""
    def __init__(self):
        self.calls = []

    def update_card(self, nm_id, updates, merge_with_existing=True, seller_id=None):
        self.calls.append(('update_card', nm_id, updates))
        return {'data': {}, 'error': False}

    def upload_photos_by_url(self, nm_id, photo_urls, seller_id=None):
        self.calls.append(('upload_photos_by_url', nm_id))
        raise RuntimeError('WB media/save недоступен')


class FakeSeller:
    id = 7


class FakeProduct:
    """Зеркальная фикстура из tests/test_card_improver_apply.py."""
    def __init__(self):
        self.id = 101
        self.nm_id = 555111
        self.vendor_code = 'VC-1'
        self.title = 'Старый заголовок'
        self.brand = 'OldBrand'
        self.description = 'кратко'
        self.object_name = 'Платье'
        self.subject_id = 999
        self.price = 1990
        self.discount_price = 1490
        self.quantity = 5
        self.characteristics_json = json.dumps([{'id': 1, 'name': 'Цвет', 'value': 'синий'}],
                                               ensure_ascii=False)
        self.dimensions_json = json.dumps({'length': 10, 'width': 5, 'height': 2}, ensure_ascii=False)
        self.photos_json = json.dumps(['a.jpg', 'b.jpg'])
        self.sizes_json = None
        self.is_active = True
        self.quality_score = 40.0
        self.quality_breakdown_json = None
        self.quality_checked_at = None
        self.nm_rating = 7.0
        self.wb_feedback_rating = 4.2
        self.nm_rating_checked_at = None
        self.updated_at = None


class PhotosViaMediaSaveTest(unittest.TestCase):
    """Тесты загрузки фото через WB media/save (Task 5)."""

    def setUp(self):
        self.history_records = []
        self.committed = False

        def fake_snapshot(product):
            return {'title': product.title, 'brand': product.brand,
                    'description': product.description}

        def fake_recompute(product, capture_history=True):
            from services.card_quality_scorer import compute_card_quality, product_to_card_input
            cq = compute_card_quality(product_to_card_input(product))
            product.quality_score = cq['score']
            product.quality_breakdown_json = json.dumps(cq['dimensions'], ensure_ascii=False)
            return cq

        class FakeHistory:
            def __init__(self, **kw):
                self.__dict__.update(kw)

        outer = self

        class FakeSession:
            def add(self, obj):
                outer.history_records.append(obj)

            def commit(self):
                outer.committed = True

        self.p1 = patch('services.card_improver._create_product_snapshot', side_effect=fake_snapshot)
        self.p2 = patch('services.card_improver.recompute_and_persist', side_effect=fake_recompute)
        self.p3 = patch('services.card_improver.CardEditHistory', FakeHistory)
        self.p4 = patch('services.card_improver.db')
        self.p1.start()
        self.p2.start()
        self.p3.start()
        mock_db = self.p4.start()
        mock_db.session = FakeSession()

    def tearDown(self):
        for p in (self.p1, self.p2, self.p3, self.p4):
            p.stop()

    # ------------------------------------------------------------------
    # Успешный путь: фото загружаются через upload_photos_by_url (media/save)
    # ------------------------------------------------------------------
    def test_photos_uploaded_via_media_save_ordered(self):
        """upload_photos_by_url вызывается с упорядоченным списком URL."""
        from services.card_improver import apply_card_updates
        product = FakeProduct()
        wb = FakeWBClient()
        seller = FakeSeller()

        urls = ['u_first', 'own1', 'own2', 'u_last']
        res = apply_card_updates(
            product,
            {'photos': urls},
            seller, wb, source='card-quality',
        )

        # upload_photos_by_url должен быть вызван с правильными аргументами
        photo_calls = [c for c in wb.calls if c[0] == 'upload_photos_by_url']
        self.assertEqual(len(photo_calls), 1, "upload_photos_by_url должен быть вызван ровно 1 раз")
        _, nm_id_called, urls_called, seller_id_called = photo_calls[0]
        self.assertEqual(nm_id_called, product.nm_id)
        self.assertEqual(urls_called, urls, "URL должны быть переданы в исходном порядке")
        self.assertEqual(seller_id_called, seller.id)

        # photos должны быть в fields_applied
        self.assertIn('photos', res['fields_applied'])

        # product.photos_json обновлён
        stored = json.loads(product.photos_json)
        self.assertEqual(stored, urls)

        # wb_sync=True
        self.assertTrue(res['wb_sync'])
        self.assertTrue(res['success'])

    def test_photos_json_updated_after_media_save(self):
        """product.photos_json == json.dumps(urls) после успешного media/save."""
        from services.card_improver import apply_card_updates
        product = FakeProduct()
        wb = FakeWBClient()
        urls = ['http://cdn.example.com/1.jpg', 'http://cdn.example.com/2.jpg']

        apply_card_updates(product, {'photos': urls}, FakeSeller(), wb)

        self.assertEqual(product.photos_json, json.dumps(urls))

    def test_photos_creates_card_edit_history_with_photos_field(self):
        """Создаётся одна запись CardEditHistory с 'photos' в changed_fields."""
        from services.card_improver import apply_card_updates
        product = FakeProduct()
        wb = FakeWBClient()
        urls = ['http://example.com/a.jpg']

        apply_card_updates(product, {'photos': urls}, FakeSeller(), wb)

        self.assertEqual(len(self.history_records), 1)
        h = self.history_records[0]
        self.assertIn('photos', h.changed_fields)
        self.assertTrue(self.committed)

    def test_photos_wb_sync_status_success(self):
        """При успешном media/save wb_sync_status='success'."""
        from services.card_improver import apply_card_updates
        product = FakeProduct()
        wb = FakeWBClient()

        apply_card_updates(product, {'photos': ['u1']}, FakeSeller(), wb)

        h = self.history_records[0]
        self.assertEqual(h.wb_sync_status, 'success')
        self.assertTrue(h.wb_synced)

    # ------------------------------------------------------------------
    # Путь ошибки: upload_photos_by_url бросает → failure-path (как текстовые поля)
    # ------------------------------------------------------------------
    def test_photos_upload_failure_returns_error(self):
        """При ошибке upload_photos_by_url: success=False, wb_sync=False, error установлен."""
        from services.card_improver import apply_card_updates
        product = FakeProduct()
        wb = FakeWBClientRaisesOnPhotos()
        seller = FakeSeller()

        res = apply_card_updates(
            product,
            {'photos': ['url1', 'url2']},
            seller, wb, source='card-quality',
        )

        self.assertFalse(res['success'])
        self.assertFalse(res['wb_sync'])
        self.assertIsNotNone(res['error'])
        self.assertIn('media/save', res['error'])

        # product.photos_json НЕ обновлён (обновление — только при успехе)
        stored = json.loads(product.photos_json)
        self.assertEqual(stored, ['a.jpg', 'b.jpg'], "photos_json не должен меняться при ошибке WB")

        # 'photos' не в fields_applied
        self.assertNotIn('photos', res['fields_applied'])

    def test_photos_upload_failure_history_status_failed(self):
        """При ошибке upload_photos_by_url история записывается со статусом 'failed'."""
        from services.card_improver import apply_card_updates
        product = FakeProduct()
        wb = FakeWBClientRaisesOnPhotos()

        apply_card_updates(product, {'photos': ['url1']}, FakeSeller(), wb)

        # История всё равно создаётся
        self.assertEqual(len(self.history_records), 1)
        h = self.history_records[0]
        self.assertFalse(h.wb_synced)
        self.assertEqual(h.wb_sync_status, 'failed')
        self.assertIsNotNone(h.wb_error_message)
        self.assertTrue(self.committed)

    # ------------------------------------------------------------------
    # Пустой список фото — не вызываем upload_photos_by_url
    # ------------------------------------------------------------------
    def test_empty_photos_list_skipped(self):
        """Пустой список photos пропускается (отфильтрован в clean); WB не вызывается."""
        from services.card_improver import apply_card_updates
        product = FakeProduct()
        wb = FakeWBClient()

        res = apply_card_updates(product, {'photos': []}, FakeSeller(), wb)

        self.assertFalse(res['success'])
        photo_calls = [c for c in wb.calls if c[0] == 'upload_photos_by_url']
        self.assertEqual(len(photo_calls), 0)


if __name__ == '__main__':
    unittest.main()
