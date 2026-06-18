# -*- coding: utf-8 -*-
"""
Тесты для services/card_improver.py :: apply_card_updates.
TDD: сначала тест — потом реализация.
"""
import json
import unittest
from unittest.mock import patch


class FakeWBClient:
    """Фейковый WB-клиент — записывает вызовы, не ходит в сеть."""
    def __init__(self):
        self.calls = []

    def update_card(self, nm_id, updates, merge_with_existing=True, seller_id=None):
        self.calls.append({
            'nm_id': nm_id,
            'updates': updates,
            'merge_with_existing': merge_with_existing,
            'seller_id': seller_id,
        })
        return {'data': {}, 'error': False}

    def upload_photos_by_url(self, nm_id, photo_urls, seller_id=None):
        # Task 5: фото теперь уходят в WB через media/save
        self.calls.append({
            'method': 'upload_photos_by_url',
            'nm_id': nm_id,
            'photo_urls': list(photo_urls),
            'seller_id': seller_id,
        })
        return {'data': {}, 'error': False}


class FakeWBClientRaises:
    """Фейковый WB-клиент, который бросает исключение при любом WB-вызове."""
    def __init__(self):
        self.calls = []

    def update_card(self, nm_id, updates, merge_with_existing=True, seller_id=None):
        self.calls.append({'nm_id': nm_id})
        raise RuntimeError('WB API недоступен')

    def upload_photos_by_url(self, nm_id, photo_urls, seller_id=None):
        self.calls.append({'nm_id': nm_id})
        raise RuntimeError('WB API недоступен')


class FakeSeller:
    id = 7


class FakeProduct:
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


class ApplyCardUpdatesTest(unittest.TestCase):
    def setUp(self):
        # Снимки/история/recompute не должны ходить в реальную БД
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
    # Путь успеха: текстовые поля + история + wb_sync
    # ------------------------------------------------------------------
    def test_applies_text_fields_and_records_history(self):
        """apply_card_updates обновляет title/description, вызывает WB API, пишет историю."""
        from services.card_improver import apply_card_updates
        product = FakeProduct()
        wb = FakeWBClient()
        seller = FakeSeller()

        new_desc = 'д' * 450
        res = apply_card_updates(
            product,
            {'title': 'Новый длинный заголовок про платье', 'description': new_desc},
            seller, wb, source='card-quality',
        )

        self.assertTrue(res['success'])
        self.assertIn('title', res['fields_applied'])
        self.assertIn('description', res['fields_applied'])
        self.assertTrue(res['wb_sync'])
        self.assertEqual(res['old_quality'], 40.0)
        self.assertIsNotNone(res['new_quality'])
        self.assertGreater(res['new_quality'], res['old_quality'])
        # WB-клиент вызван с merge и seller_id
        self.assertEqual(len(wb.calls), 1)
        self.assertEqual(wb.calls[0]['nm_id'], 555111)
        self.assertTrue(wb.calls[0]['merge_with_existing'])
        self.assertEqual(wb.calls[0]['seller_id'], 7)
        # Локальный продукт обновлён
        self.assertEqual(product.title, 'Новый длинный заголовок про платье')
        self.assertEqual(product.description, new_desc)
        # История создана и закоммичена
        self.assertEqual(len(self.history_records), 1)
        h = self.history_records[0]
        self.assertEqual(h.action, 'update')
        self.assertEqual(sorted(h.changed_fields), ['description', 'title'])
        self.assertTrue(h.wb_synced)
        self.assertEqual(h.wb_sync_status, 'success')
        self.assertTrue(self.committed)

    # ------------------------------------------------------------------
    # Неизвестные поля игнорируются, разрешённые применяются
    # ------------------------------------------------------------------
    def test_ignores_unknown_fields(self):
        """Поля вне ALLOWED_FIELDS игнорируются; допустимые — применяются."""
        from services.card_improver import apply_card_updates
        product = FakeProduct()
        wb = FakeWBClient()
        res = apply_card_updates(product, {'foobar': 'x', 'brand': 'NewBrand'},
                                 FakeSeller(), wb, source='card-quality')
        self.assertEqual(res['fields_applied'], ['brand'])
        self.assertEqual(product.brand, 'NewBrand')

    # ------------------------------------------------------------------
    # Пустой / нет известных полей → нет WB-вызова, success=False
    # ------------------------------------------------------------------
    def test_no_known_fields_skips_wb_call(self):
        """Если нет ни одного допустимого поля — WB не вызывается, success=False."""
        from services.card_improver import apply_card_updates
        product = FakeProduct()
        wb = FakeWBClient()
        res = apply_card_updates(product, {'foobar': 'x'}, FakeSeller(), wb)
        self.assertFalse(res['success'])
        self.assertEqual(res['fields_applied'], [])
        self.assertFalse(res['wb_sync'])
        self.assertEqual(len(wb.calls), 0)

    # ------------------------------------------------------------------
    # Путь ошибки: WB API бросает → success=False, wb_sync=False, error заполнен
    # ------------------------------------------------------------------
    def test_wb_api_failure_returns_error_and_records_failed_history(self):
        """При ошибке WB API: success=False, wb_sync=False, error установлен, история со статусом 'failed'."""
        from services.card_improver import apply_card_updates
        product = FakeProduct()
        wb = FakeWBClientRaises()
        seller = FakeSeller()

        res = apply_card_updates(
            product,
            {'title': 'Заголовок после ошибки'},
            seller, wb, source='card-quality',
        )

        self.assertFalse(res['success'])
        self.assertFalse(res['wb_sync'])
        self.assertIsNotNone(res['error'])
        self.assertIn('WB API', res['error'])
        # Продукт локально НЕ обновлён (обновление — только после успеха WB)
        self.assertEqual(product.title, 'Старый заголовок')
        # История всё равно создана — с wb_sync_status='failed'
        self.assertEqual(len(self.history_records), 1)
        h = self.history_records[0]
        self.assertFalse(h.wb_synced)
        self.assertEqual(h.wb_sync_status, 'failed')
        self.assertIsNotNone(h.wb_error_message)
        self.assertTrue(self.committed)


    # ------------------------------------------------------------------
    # Task 5: photos уходят в WB через upload_photos_by_url (media/save), не update_card
    # ------------------------------------------------------------------
    def test_photos_uploaded_via_media_save(self):
        """photos отправляются в WB через upload_photos_by_url и сохраняются в photos_json."""
        from services.card_improver import apply_card_updates
        product = FakeProduct()
        wb = FakeWBClient()

        res = apply_card_updates(
            product,
            {'photos': ['url1', 'url2']},
            FakeSeller(), wb, source='card-quality',
        )

        # photos должны быть в fields_applied
        self.assertIn('photos', res['fields_applied'])
        # photos_json должен содержать переданные URL
        stored = json.loads(product.photos_json)
        self.assertEqual(stored, ['url1', 'url2'])
        # wb_sync=True — upload_photos_by_url вызван успешно
        self.assertTrue(res['wb_sync'])
        # upload_photos_by_url вызван с правильными аргументами; update_card НЕ вызывался
        photo_calls = [c for c in wb.calls if c.get('method') == 'upload_photos_by_url']
        self.assertEqual(len(photo_calls), 1)
        self.assertEqual(photo_calls[0]['photo_urls'], ['url1', 'url2'])
        update_calls = [c for c in wb.calls if 'updates' in c]
        self.assertEqual(len(update_calls), 0, "update_card не должен вызываться для photos")

    # ------------------------------------------------------------------
    # Fix 2 (обновлён для Task 5): wb_sync_status при только-локальных полях
    # subject_id — локальный (skipped), photos — через WB media/save (success)
    # ------------------------------------------------------------------
    def test_wb_sync_status_skipped_when_only_subject_id(self):
        """Если применяется только subject_id (локальный, без WB), wb_sync_status == 'skipped'."""
        from services.card_improver import apply_card_updates
        product = FakeProduct()
        wb = FakeWBClient()
        apply_card_updates(product, {'subject_id': 42}, FakeSeller(), wb, source='card-quality')

        self.assertEqual(len(self.history_records), 1)
        h = self.history_records[0]
        self.assertEqual(h.wb_sync_status, 'skipped')

    def test_wb_sync_status_success_when_only_photos(self):
        """photos отправляются через WB media/save, поэтому wb_sync_status == 'success'."""
        from services.card_improver import apply_card_updates
        product = FakeProduct()
        wb = FakeWBClient()
        apply_card_updates(product, {'photos': ['url1']}, FakeSeller(), wb, source='card-quality')

        self.assertEqual(len(self.history_records), 1)
        h = self.history_records[0]
        self.assertEqual(h.wb_sync_status, 'success')

    def test_wb_sync_status_success_when_subject_id_and_photos(self):
        """subject_id + photos: photos уходят в WB → wb_sync_status == 'success'."""
        from services.card_improver import apply_card_updates
        product = FakeProduct()
        wb = FakeWBClient()
        apply_card_updates(product, {'subject_id': 42, 'photos': ['url1']}, FakeSeller(), wb,
                           source='card-quality')

        self.assertEqual(len(self.history_records), 1)
        h = self.history_records[0]
        self.assertEqual(h.wb_sync_status, 'success')


if __name__ == '__main__':
    unittest.main()
