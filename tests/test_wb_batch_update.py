# -*- coding: utf-8 -*-
"""Тесты батчевого обновления карточек WB.

WB даёт cards/update всего 10 запросов/мин (у нас 8), но до 3000 карточек
в одном запросе — массовые правки обязаны батчиться, а не слать по одной.
"""

import unittest
from unittest.mock import patch, MagicMock

from services.wb_api_client import (
    WildberriesAPIClient, WBAPIException, WBRateLimitException,
    WBTransportUncertainException,
)


def _card(nm_id, vendor_code=None, **extra):
    card = {'nmID': nm_id, 'vendorCode': vendor_code or f'VC-{nm_id}',
            'title': f'Товар {nm_id}', 'brand': 'Old',
            'subjectID': 777, 'characteristics': [],
            'sizes': [{'skus': ['123']}]}
    card.update(extra)
    from services.wb_validators import _mark_wb_card_as_fetched
    return _mark_wb_card_as_fetched(card)


class TestFetchCardsByNmIds(unittest.TestCase):
    def setUp(self):
        self.client = WildberriesAPIClient('token-batch-fetch')

    def test_small_set_fetches_per_id(self):
        with patch.object(self.client, 'get_card_by_nm_id',
                          side_effect=lambda nm, **kw: _card(nm)) as mock_get, \
             patch.object(self.client, 'get_cards_list') as mock_list:
            found = self.client.fetch_cards_by_nm_ids([1, 2, 3])
        self.assertEqual(set(found), {1, 2, 3})
        self.assertEqual(mock_get.call_count, 3)
        mock_list.assert_not_called()

    def test_large_set_sweeps_with_cursor(self):
        pages = [
            {'cards': [_card(i) for i in range(1, 101)],
             'cursor': {'updatedAt': 't1', 'nmID': 100, 'total': 100}},
            {'cards': [_card(i) for i in range(101, 151)],
             'cursor': {'updatedAt': 't2', 'nmID': 150, 'total': 50}},
        ]
        with patch.object(self.client, 'get_cards_list',
                          side_effect=pages) as mock_list, \
             patch.object(self.client, 'get_card_by_nm_id') as mock_get:
            targets = list(range(1, 121))  # 120 карточек — больше порога
            found = self.client.fetch_cards_by_nm_ids(targets)
        self.assertEqual(len(found), 120)
        mock_get.assert_not_called()
        self.assertEqual(mock_list.call_count, 2)

    def test_missing_ids_are_absent(self):
        with patch.object(self.client, 'get_card_by_nm_id', return_value=None):
            found = self.client.fetch_cards_by_nm_ids([7])
        self.assertEqual(found, {})


class TestUpdateCardsMerged(unittest.TestCase):
    def setUp(self):
        self.client = WildberriesAPIClient('token-batch-merged')

    def _run(self, nm_updates, cards_map, batch_side_effect=None):
        with patch.object(self.client, 'fetch_cards_by_nm_ids',
                          return_value=cards_map), \
             patch.object(self.client, 'update_cards_batch',
                          side_effect=batch_side_effect or (lambda cards, **kw: {'error': False})) as mock_batch:
            result = self.client.update_cards_merged(nm_updates)
        return result, mock_batch

    def test_merges_updates_into_full_cards_and_sends_one_request(self):
        cards_map = {1: _card(1), 2: _card(2)}
        result, mock_batch = self._run(
            {1: {'brand': 'New'}, 2: {'title': 'Новый заголовок'}}, cards_map)
        self.assertEqual(set(result['sent']), {1, 2})
        self.assertEqual(result['requests'], 1)
        self.assertEqual(mock_batch.call_count, 1)
        sent_cards = mock_batch.call_args.args[0]
        by_nm = {c['nmID']: c for c in sent_cards}
        self.assertEqual(by_nm[1]['brand'], 'New')
        self.assertEqual(by_nm[1]['vendorCode'], 'VC-1')  # полная карточка
        self.assertEqual(by_nm[2]['title'], 'Новый заголовок')

    def test_missing_cards_reported_not_sent(self):
        result, mock_batch = self._run({1: {'brand': 'New'}, 99: {'brand': 'X'}},
                                       {1: _card(1)})
        self.assertEqual(result['sent'], [1])
        self.assertEqual(result['missing'], [99])

    def test_invalid_card_skipped_with_error(self):
        # без vendorCode карточка не проходит валидацию
        bad = _card(5)
        del bad['vendorCode']
        result, mock_batch = self._run({5: {'brand': 'New'}}, {5: bad})
        self.assertEqual(result['sent'], [])
        self.assertIn(5, result['invalid'])
        mock_batch.assert_not_called()

    def test_chunking_by_count(self):
        n = 7
        cards_map = {i: _card(i) for i in range(1, n + 1)}
        nm_updates = {i: {'brand': 'New'} for i in range(1, n + 1)}
        with patch.object(self.client, 'fetch_cards_by_nm_ids',
                          return_value=cards_map), \
             patch.object(self.client, 'update_cards_batch',
                          return_value={'error': False}) as mock_batch:
            result = self.client.update_cards_merged(nm_updates, chunk_size=3)
        self.assertEqual(mock_batch.call_count, 3)  # 3+3+1
        self.assertEqual(result['requests'], 3)
        self.assertEqual(len(result['sent']), 7)

    def test_failed_chunk_bisects_to_isolate_bad_card(self):
        """Одна плохая карточка не валит остальные: бисекция до одиночной."""
        cards_map = {i: _card(i) for i in range(1, 5)}
        nm_updates = {i: {'brand': 'New'} for i in range(1, 5)}

        def batch_side_effect(cards, **kw):
            if any(c['nmID'] == 3 for c in cards):
                raise WBAPIException('WB отклонил запрос')
            return {'error': False}

        with patch.object(self.client, 'fetch_cards_by_nm_ids',
                          return_value=cards_map), \
             patch.object(self.client, 'update_cards_batch',
                          side_effect=batch_side_effect):
            result = self.client.update_cards_merged(nm_updates, chunk_size=10)

        self.assertEqual(set(result['sent']), {1, 2, 4})
        self.assertEqual(set(result['failed']), {3})
        self.assertIn('WB отклонил', result['failed'][3])

    def test_uncertain_transport_failure_is_never_bisected_or_retried(self):
        cards_map = {i: _card(i) for i in range(1, 5)}
        nm_updates = {i: {'brand': 'New'} for i in range(1, 5)}
        error = WBTransportUncertainException(
            'timeout after request body', request_may_have_been_applied=True,
        )

        with patch.object(
            self.client, 'fetch_cards_by_nm_ids', return_value=cards_map,
        ), patch.object(
            self.client, 'update_cards_batch', side_effect=error,
        ) as mock_batch:
            with self.assertRaises(WBTransportUncertainException):
                self.client.update_cards_merged(nm_updates, chunk_size=10)

        self.assertEqual(mock_batch.call_count, 1)

    def test_rate_limit_is_batch_wide_and_never_bisected(self):
        cards_map = {i: _card(i) for i in range(1, 5)}
        nm_updates = {i: {'brand': 'New'} for i in range(1, 5)}

        with patch.object(
            self.client, 'fetch_cards_by_nm_ids', return_value=cards_map,
        ), patch.object(
            self.client, 'update_cards_batch',
            side_effect=WBRateLimitException('rate limit', retry_after=30),
        ) as mock_batch:
            with self.assertRaises(WBRateLimitException):
                self.client.update_cards_merged(nm_updates, chunk_size=10)

        self.assertEqual(mock_batch.call_count, 1)

    def test_partial_progress_survives_later_chunk_failure(self):
        """Падение позднего чанка не теряет уже отправленные карточки."""
        cards_map = {i: _card(i) for i in range(1, 6)}
        nm_updates = {i: {'brand': 'New'} for i in range(1, 6)}

        def batch_side_effect(cards, **kw):
            if any(c['nmID'] == 5 for c in cards):
                raise WBAPIException('boom')
            return {'error': False}

        with patch.object(self.client, 'fetch_cards_by_nm_ids',
                          return_value=cards_map), \
             patch.object(self.client, 'update_cards_batch',
                          side_effect=batch_side_effect):
            result = self.client.update_cards_merged(nm_updates, chunk_size=2)

        self.assertEqual(set(result['sent']), {1, 2, 3, 4})
        self.assertEqual(set(result['failed']), {5})

    def test_http200_with_error_body_is_failure_not_success(self):
        """WB может вернуть 200 с error:true — это отказ, а не успех."""
        cards_map = {1: _card(1)}
        result, _ = self._run({1: {'brand': 'New'}}, cards_map,
                              batch_side_effect=lambda cards, **kw: {
                                  'error': True, 'errorText': 'карточка забанена'})
        self.assertEqual(result['sent'], [])
        self.assertIn(1, result['failed'])
        self.assertIn('забанена', result['failed'][1])

    def test_chunk_size_accounting_matches_wire_format(self):
        """Бюджет чанка считается по ASCII-escaped JSON (как реально шлёт requests)."""
        # Кириллица: ~6 байт на символ в escaped-виде против ~2 в utf-8.
        big_text = 'Ы' * 200_000  # ~1.2 МБ на проводе в escaped-виде
        cards_map = {i: _card(i, bigfield=big_text) for i in range(1, 11)}
        nm_updates = {i: {'brand': 'New'} for i in range(1, 11)}
        sizes = []
        from services.wb_validators import WB_PREPARED_CONTEXT_KEY

        def record_wire_size(cards, **_kwargs):
            wire_cards = [
                {
                    key: value for key, value in card.items()
                    if key != WB_PREPARED_CONTEXT_KEY
                }
                for card in cards
            ]
            sizes.append(len(__import__('json').dumps(wire_cards)))
            return {'error': False}

        with patch.object(self.client, 'fetch_cards_by_nm_ids',
                          return_value=cards_map), \
             patch.object(self.client, 'update_cards_batch',
                          side_effect=record_wire_size):
            self.client.update_cards_merged(nm_updates, chunk_size=1000)
        # Ни один отправленный чанк не должен превышать 8 МБ в wire-формате
        self.assertTrue(all(s <= 8 * 1024 * 1024 for s in sizes), sizes)
        self.assertGreater(len(sizes), 1)  # ~12 МБ материала → минимум 2 чанка

    def test_sweep_stops_on_stalled_cursor(self):
        """Повторяющийся курсор WB не должен зацикливать обход."""
        page = {'cards': [_card(1)],
                'cursor': {'updatedAt': 'same', 'nmID': 1, 'total': 100}}
        with patch.object(self.client, 'get_cards_list',
                          return_value=page) as mock_list:
            found = self.client.fetch_cards_by_nm_ids(list(range(1, 200)))
        self.assertLessEqual(mock_list.call_count, 3)
        self.assertEqual(set(found), {1})

    def test_photos_never_leak_into_batch(self):
        """prepare_card_for_update вырезает нередактируемые поля (photos и пр.)."""
        card = _card(1, photos=[{'big': 'url'}], imtID=555)
        result, mock_batch = self._run({1: {'brand': 'New'}}, {1: card})
        sent = mock_batch.call_args.args[0][0]
        self.assertNotIn('photos', sent)
        self.assertNotIn('imtID', sent)


if __name__ == '__main__':
    unittest.main()
