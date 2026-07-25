# -*- coding: utf-8 -*-
"""Regression tests for non-destructive supplier enrichment merge policy."""
import hashlib
import unittest
from unittest.mock import MagicMock, mock_open, patch

from services.supplier_enrichment import (
    EnrichmentService,
    WbLiveMediaDrift,
)
from services.wb_api_client import (
    WBAPIException,
    WBLiveCardDrift,
    WBTransportUncertainException,
    WildberriesAPIClient,
)
from services.wb_enrichment_merge import (
    WBEnrichmentMergeError,
    live_wb_photo_match_urls,
    live_wb_photo_urls,
    plan_characteristic_merge,
    plan_dimensions_merge,
    plan_photo_merge,
    plan_scalar_field_merge,
)


def _fp(value):
    return {
        'pixel_sha': value,
        'dhash': 0 if value == 'same' else 2,
        'ahash': 0 if value == 'same' else 2,
    }


class WBEnrichmentMergePolicyTestCase(unittest.TestCase):
    def test_live_photo_urls_prefer_square_rendition(self):
        card = {
            'photos': [{
                'tm': 'https://basket-01.wbbasket.ru/tm.webp',
                'c246x328': 'https://basket-01.wbbasket.ru/c246x328.webp',
                'square': 'https://basket-01.wbbasket.ru/square.webp',
                'big': 'https://basket-01.wbbasket.ru/big.webp',
            }],
        }

        self.assertEqual(
            live_wb_photo_urls(card),
            ['https://basket-01.wbbasket.ru/square.webp'],
        )

    def test_live_photo_urls_keep_tm_as_legacy_fallback(self):
        card = {
            'photos': [{
                'tm': 'https://basket-01.wbbasket.ru/tm.webp',
            }],
        }

        self.assertEqual(
            live_wb_photo_urls(card),
            ['https://basket-01.wbbasket.ru/tm.webp'],
        )

    def test_characteristic_ids_reject_numeric_coercion(self):
        invalid_ids = ('10', 10.0, True, 0, -1)
        for raw_id in invalid_ids:
            with self.subTest(existing_id=raw_id):
                with self.assertRaises(WBEnrichmentMergeError):
                    plan_characteristic_merge(
                        [{'id': raw_id, 'value': ['Live']}],
                        [],
                    )
            with self.subTest(candidate_id=raw_id):
                with self.assertRaises(WBEnrichmentMergeError):
                    plan_characteristic_merge(
                        [],
                        [{'id': raw_id, 'value': ['Supplier']}],
                    )

    def test_characteristics_only_add_or_replace_with_strictly_more_information(self):
        result = plan_characteristic_merge(
            [
                {'id': 1, 'name': 'Материал', 'value': ['Хлопок и полиэстер']},
                {'id': 2, 'name': 'Комплектация', 'value': ['Чехол']},
                {'id': 3, 'name': 'Цвет', 'value': ['Красный']},
            ],
            [
                {'id': 1, 'value': ['Хлопок']},
                {'id': 2, 'value': ['Чехол, инструкция и крепление']},
                {'id': 3, 'value': ['  красный  ']},
                {'id': 4, 'value': ['Новое поле']},
            ],
        )

        self.assertEqual(result['accepted_patch'], [
            {'id': 2, 'value': ['Чехол, инструкция и крепление']},
            {'id': 4, 'value': ['Новое поле']},
        ])
        self.assertEqual(result['counts']['preserved_existing'], 1)
        self.assertEqual(result['counts']['replaced_more_complete'], 1)
        self.assertEqual(result['counts']['unchanged'], 1)
        self.assertEqual(result['counts']['added'], 1)

    def test_photo_plan_matches_existing_and_only_appends_missing(self):
        same = _fp('same')
        novel = {
            'pixel_sha': 'novel',
            'dhash': (1 << 64) - 1,
            'ahash': (1 << 64) - 1,
        }
        result = plan_photo_merge(
            [same],
            [same, novel, novel],
            max_images=30,
        )

        self.assertEqual(result['append_indices'], [1])
        self.assertEqual(result['counts']['already_present'], 1)
        self.assertEqual(result['counts']['append'], 1)
        self.assertEqual(result['counts']['duplicate_candidate'], 1)
        self.assertEqual(result['items'][1]['target_or_match_position'], 2)

    def test_square_wb_variant_is_preferred_for_photo_matching(self):
        card = {
            'photos': [{
                'tm': 'https://basket-01.wbbasket.ru/photo-tm.webp',
                'square': 'https://basket-01.wbbasket.ru/photo-square.webp',
            }],
        }

        self.assertEqual(live_wb_photo_match_urls(card), [
            'https://basket-01.wbbasket.ru/photo-square.webp',
        ])

    def test_longer_but_semantically_different_characteristic_is_preserved(self):
        result = plan_characteristic_merge(
            [{'id': 1, 'value': ['Натуральный хлопок']}],
            [{'id': 1, 'value': ['Искусственный полиэстер премиум класса']}],
        )

        self.assertEqual(result['accepted_patch'], [])
        self.assertEqual(result['counts']['preserved_existing'], 1)

    def test_longer_formatting_without_new_semantic_fact_is_preserved(self):
        result = plan_characteristic_merge(
            [{'id': 1, 'value': ['Красный']}],
            [{'id': 1, 'value': ['Красный, красный!!!']}],
        )

        self.assertEqual(result['accepted_patch'], [])
        self.assertEqual(result['counts']['preserved_existing'], 1)

    def test_numeric_and_negated_values_never_win_by_length(self):
        numeric = plan_characteristic_merge(
            [{'id': 1, 'value': [10]}],
            [{'id': 1, 'value': ['10.000']}],
        )
        negated = plan_characteristic_merge(
            [{'id': 2, 'value': ['Красный']}],
            [{'id': 2, 'value': ['Не красный цвет']}],
        )

        self.assertEqual(numeric['accepted_patch'], [])
        self.assertEqual(negated['accepted_patch'], [])

    def test_number_with_units_never_wins_by_added_length(self):
        result = plan_characteristic_merge(
            [{'id': 1, 'value': ['10 см']}],
            [{'id': 1, 'value': ['10 см, дополнительно 20 см']}],
        )

        self.assertEqual(result['accepted_patch'], [])
        self.assertEqual(result['counts']['preserved_existing'], 1)

    def test_semantic_reversal_without_standalone_not_is_preserved(self):
        absent = plan_characteristic_merge(
            [{'id': 1, 'value': ['Красный']}],
            [{'id': 1, 'value': ['Красный цвет отсутствует']}],
        )
        excluded = plan_scalar_field_merge(
            'description',
            'Подходит для детей',
            'Подходит для детей, кроме детей младшего возраста',
        )

        self.assertEqual(absent['accepted_patch'], [])
        self.assertFalse(excluded['accepted'])

    def test_scalar_and_dimension_policy_preserves_manual_facts(self):
        richer_title = plan_scalar_field_merge(
            'title', 'Чехол', 'Чехол для телефона с ремешком',
        )
        unrelated_title = plan_scalar_field_merge(
            'title', 'Чехол из кожи', 'Премиальный кошелёк большого размера',
        )
        brand = plan_scalar_field_merge('brand', 'Seller Brand', 'Longer Brand')
        dimensions = plan_dimensions_merge(
            {'length': 10, 'width': None},
            {'length': 100, 'width': 20, 'height': 30},
        )

        self.assertTrue(richer_title['accepted'])
        self.assertFalse(unrelated_title['accepted'])
        self.assertFalse(brand['accepted'])
        self.assertEqual(dimensions['accepted_patch'], {
            'width': 20,
            'height': 30,
        })

    def test_dimension_policy_fills_invalid_zero_with_positive_observed_fact(self):
        dimensions = plan_dimensions_merge(
            {'length': 10, 'weightBrutto': 0},
            {'length': 100, 'weightBrutto': 0.18},
        )

        self.assertEqual(dimensions['accepted_patch'], {
            'weightBrutto': 0.18,
        })
        self.assertEqual(dimensions['counts']['filled_missing'], 1)
        self.assertEqual(dimensions['counts']['preserved_existing'], 1)

    def test_photo_plan_never_replaces_when_gallery_is_full(self):
        existing = [
            {'pixel_sha': f'existing-{index}', 'dhash': index, 'ahash': index}
            for index in range(30)
        ]
        candidate = {
            'pixel_sha': 'novel',
            'dhash': (1 << 64) - 1,
            'ahash': (1 << 64) - 1,
        }

        result = plan_photo_merge(existing, [candidate], max_images=30)

        self.assertEqual(result['append_indices'], [])
        self.assertEqual(result['counts']['skipped_capacity'], 1)

    def test_photo_plan_fails_closed_when_a_live_photo_cannot_be_matched(self):
        candidate = {
            'pixel_sha': 'novel',
            'dhash': (1 << 64) - 1,
            'ahash': (1 << 64) - 1,
        }

        result = plan_photo_merge([None], [candidate], max_images=30)

        self.assertEqual(result['append_indices'], [])
        self.assertEqual(result['matching_status'], 'blocked')
        self.assertEqual(result['counts']['skipped_match_unavailable'], 1)

    def test_unknown_photo_strategy_is_rejected_before_live_read(self):
        client = MagicMock()

        with self.assertRaises(ValueError):
            EnrichmentService.merge_photos_to_card_locked(
                client,
                seller_id=123,
                nm_id=456,
                photo_paths=['candidate.jpg'],
                strategy='destructive_replace',
            )

        client.get_card_by_nm_id.assert_not_called()

    @patch(
        'services.marketplace_validator.validate_wb_full_card_dictionary_values',
        return_value={'valid': True, 'issues': []},
    )
    @patch(
        'services.marketplace_validator.build_wb_characteristic_patch',
        side_effect=lambda _subject, patch: patch,
    )
    def test_update_card_skips_network_when_live_characteristic_is_richer(
        self, _build_patch, _validate_full,
    ):
        client = WildberriesAPIClient('test-key')
        client.get_card_by_nm_id = MagicMock(return_value={
            'nmID': 1001,
            'subjectID': 77,
            'vendorCode': 'VC-1',
            'title': 'Товар',
            'brand': 'Brand',
            'description': 'Описание',
            'sizes': [{'chrtID': 1, 'skus': ['1234567890123']}],
            'characteristics': [{
                'id': 10,
                'name': 'Материал',
                'value': ['Хлопок и полиэстер'],
            }],
        })
        client._make_request = MagicMock()
        context = {}

        result = client.update_card(
            1001,
            {'characteristics': [{'id': 10, 'value': ['Хлопок']}]},
            snapshot_context=context,
            preserve_richer_enrichment=True,
        )

        self.assertTrue(result['skipped'])
        self.assertFalse(context['write_required'])
        self.assertEqual(context['before'], context['after'])
        self.assertEqual(
            context['merge_decisions']['characteristics']['counts'][
                'preserved_existing'
            ],
            1,
        )
        client._make_request.assert_not_called()

    @patch(
        'services.marketplace_validator.validate_wb_full_card_dictionary_values',
        return_value={'valid': True, 'issues': []},
    )
    @patch(
        'services.marketplace_validator.build_wb_characteristic_patch',
        side_effect=lambda _subject, patch: patch,
    )
    def test_update_card_keeps_unmentioned_live_fields_and_accepts_richer_patch(
        self, _build_patch, _validate_full,
    ):
        client = WildberriesAPIClient('test-key')
        client.get_card_by_nm_id = MagicMock(return_value={
            'nmID': 1002,
            'subjectID': 77,
            'vendorCode': 'VC-2',
            'title': 'Товар',
            'brand': 'Brand',
            'description': 'Описание',
            'sizes': [{'chrtID': 1, 'skus': ['1234567890123']}],
            'characteristics': [
                {'id': 10, 'name': 'Комплектация', 'value': ['Чехол']},
                {'id': 99, 'name': 'Ручное поле', 'value': ['Не удалять']},
            ],
        })
        response = MagicMock()
        response.json.return_value = {'error': False}
        client._make_request = MagicMock(return_value=response)

        client.update_card(
            1002,
            {'characteristics': [
                {'id': 10, 'value': ['Чехол и инструкция']},
                {'id': 11, 'value': ['Новое значение']},
            ]},
            preserve_richer_enrichment=True,
            validate=False,
        )

        sent = client._make_request.call_args.kwargs['json'][0]
        self.assertEqual(sent['characteristics'], [
            {'id': 10, 'value': ['Чехол и инструкция']},
            {'id': 99, 'value': ['Не удалять']},
            {'id': 11, 'value': ['Новое значение']},
        ])

    @patch(
        'services.marketplace_validator.validate_wb_full_card_dictionary_values',
        return_value={'valid': True, 'issues': []},
    )
    def test_update_card_accepts_reordered_live_characteristics(
        self, _validate_full,
    ):
        base = {
            'nmID': 1003,
            'subjectID': 77,
            'vendorCode': 'VC-3',
            'title': 'Чехол',
            'brand': 'Brand',
            'description': 'Описание продавца',
            'sizes': [{'chrtID': 1, 'skus': ['1234567890123']}],
            'characteristics': [
                {'id': 10, 'name': 'Материал', 'value': ['Хлопок']},
                {'id': 20, 'name': 'Цвет', 'value': ['Черный']},
            ],
        }
        reordered = {
            **base,
            'characteristics': list(reversed(base['characteristics'])),
        }
        response = MagicMock()
        response.json.return_value = {'error': False}
        client = WildberriesAPIClient('test-key')
        client.get_card_by_nm_id = MagicMock(
            side_effect=[base, reordered, base],
        )
        client._make_request = MagicMock(return_value=response)

        client.update_card(
            1003,
            {'title': 'Чехол для телефона'},
            preserve_richer_enrichment=True,
            validate=False,
        )

        client._make_request.assert_called_once()

    @patch(
        'services.marketplace_validator.validate_wb_full_card_dictionary_values',
        return_value={'valid': True, 'issues': []},
    )
    def test_update_card_still_blocks_live_characteristic_value_drift(
        self, _validate_full,
    ):
        base = {
            'nmID': 1003,
            'subjectID': 77,
            'vendorCode': 'VC-3',
            'title': 'Чехол',
            'brand': 'Brand',
            'description': 'Описание продавца',
            'sizes': [{'chrtID': 1, 'skus': ['1234567890123']}],
            'characteristics': [{
                'id': 10,
                'name': 'Материал',
                'value': ['Хлопок'],
            }],
        }
        changed = {
            **base,
            'characteristics': [{
                'id': 10,
                'name': 'Материал',
                'value': ['Шелк'],
            }],
        }
        client = WildberriesAPIClient('test-key')
        client.get_card_by_nm_id = MagicMock(
            side_effect=[base, changed],
        )
        client._make_request = MagicMock()

        with self.assertRaises(WBLiveCardDrift):
            client.update_card(
                1003,
                {'title': 'Чехол для телефона'},
                preserve_richer_enrichment=True,
                validate=False,
            )

        client._make_request.assert_not_called()

    @patch(
        'services.marketplace_validator.validate_wb_full_card_dictionary_values',
        return_value={'valid': True, 'issues': []},
    )
    def test_update_card_final_preflight_blocks_live_drift(
        self, _validate_full,
    ):
        base = {
            'nmID': 1003,
            'subjectID': 77,
            'vendorCode': 'VC-3',
            'title': 'Чехол',
            'brand': 'Brand',
            'description': 'Описание продавца',
            'sizes': [{'chrtID': 1, 'skus': ['1234567890123']}],
            'characteristics': [],
        }
        drifted = dict(base)
        drifted['description'] = 'Ручное описание, изменённое параллельно'
        client = WildberriesAPIClient('test-key')
        client.get_card_by_nm_id = MagicMock(
            side_effect=[dict(base), dict(base), drifted],
        )
        client._make_request = MagicMock()
        receipt = MagicMock()

        with self.assertRaises(WBLiveCardDrift):
            client.update_card(
                1003,
                {'title': 'Чехол для телефона'},
                preserve_richer_enrichment=True,
                validate=False,
                before_send_callback=receipt,
            )

        receipt.assert_called_once()
        client._make_request.assert_not_called()

    @patch(
        'services.marketplace_validator.validate_wb_full_card_dictionary_values',
        return_value={'valid': True, 'issues': []},
    )
    def test_preserve_update_never_fabricates_missing_dimensions(
        self, _validate_full,
    ):
        live = {
            'nmID': 1004,
            'subjectID': 77,
            'vendorCode': 'VC-4',
            'title': 'Чехол',
            'brand': 'Brand',
            'description': 'Описание',
            'sizes': [{'chrtID': 1, 'skus': ['1234567890123']}],
            'characteristics': [],
        }
        response = MagicMock()
        response.json.return_value = {'error': False}
        client = WildberriesAPIClient('test-key')
        client.get_card_by_nm_id = MagicMock(return_value=dict(live))
        client._make_request = MagicMock(return_value=response)

        client.update_card(
            1004,
            {'title': 'Чехол для телефона'},
            preserve_richer_enrichment=True,
            validate=False,
        )

        sent = client._make_request.call_args.kwargs['json'][0]
        self.assertNotIn('dimensions', sent)

    @patch(
        'services.marketplace_validator.validate_wb_full_card_dictionary_values',
        return_value={'valid': True, 'issues': []},
    )
    def test_preserve_update_keeps_partial_live_dimensions_without_defaults(
        self, _validate_full,
    ):
        live = {
            'nmID': 1005,
            'subjectID': 77,
            'vendorCode': 'VC-5',
            'title': 'Чехол',
            'brand': 'Brand',
            'description': 'Описание',
            'sizes': [{'chrtID': 1, 'skus': ['1234567890123']}],
            'characteristics': [],
            'dimensions': {'length': 17, 'isValid': True},
        }
        response = MagicMock()
        response.json.return_value = {'error': False}
        client = WildberriesAPIClient('test-key')
        client.get_card_by_nm_id = MagicMock(return_value=dict(live))
        client._make_request = MagicMock(return_value=response)

        client.update_card(
            1005,
            {'title': 'Чехол для телефона'},
            preserve_richer_enrichment=True,
            validate=False,
        )

        sent = client._make_request.call_args.kwargs['json'][0]
        self.assertEqual(sent['dimensions'], {'length': 17})

    @patch(
        'services.marketplace_validator.validate_wb_full_card_dictionary_values',
        return_value={'valid': True, 'issues': []},
    )
    def test_preserve_update_does_not_reinterpret_heavy_live_weight_as_grams(
        self, _validate_full,
    ):
        live = {
            'nmID': 1007,
            'subjectID': 77,
            'vendorCode': 'VC-7',
            'title': 'Тяжёлый товар',
            'brand': 'Brand',
            'description': 'Описание',
            'sizes': [{'chrtID': 1, 'skus': ['1234567890123']}],
            'characteristics': [],
            'dimensions': {'weightBrutto': 50},
        }
        response = MagicMock()
        response.json.return_value = {'error': False}
        client = WildberriesAPIClient('test-key')
        client.get_card_by_nm_id = MagicMock(return_value=dict(live))
        client._make_request = MagicMock(return_value=response)

        client.update_card(
            1007,
            {'title': 'Тяжёлый товар с усиленной упаковкой'},
            preserve_richer_enrichment=True,
            validate=False,
        )

        sent = client._make_request.call_args.kwargs['json'][0]
        self.assertEqual(sent['dimensions'], {'weightBrutto': 50})

    @patch(
        'services.marketplace_validator.validate_wb_full_card_dictionary_values',
        return_value={'valid': True, 'issues': []},
    )
    def test_invalid_live_weight_blocks_preserve_write_instead_of_deleting_it(
        self, _validate_full,
    ):
        live = {
            'nmID': 1008,
            'subjectID': 77,
            'vendorCode': 'VC-8',
            'title': 'Товар',
            'brand': 'Brand',
            'description': 'Описание',
            'sizes': [{'chrtID': 1, 'skus': ['1234567890123']}],
            'characteristics': [],
            'dimensions': {'length': 10, 'weightBrutto': 'bad-live-value'},
        }
        client = WildberriesAPIClient('test-key')
        client.get_card_by_nm_id = MagicMock(return_value=dict(live))
        client._make_request = MagicMock()

        with self.assertRaises(WBAPIException) as raised:
            client.update_card(
                1008,
                {'title': 'Товар с дополнением'},
                preserve_richer_enrichment=True,
            )

        self.assertIn("weightBrutto", str(raised.exception))
        self.assertIn("фактический вес упаковки", str(raised.exception))
        self.assertNotIn("Validation failed", str(raised.exception))
        client._make_request.assert_not_called()

    @patch(
        'services.marketplace_validator.validate_wb_full_card_dictionary_values',
        return_value={'valid': True, 'issues': []},
    )
    def test_positive_supplier_weight_fills_invalid_zero_live_weight(
        self, _validate_full,
    ):
        live = {
            'nmID': 1009,
            'subjectID': 77,
            'vendorCode': 'VC-9',
            'title': 'Товар',
            'brand': 'Brand',
            'description': 'Описание',
            'sizes': [{'chrtID': 1, 'skus': ['1234567890123']}],
            'characteristics': [],
            'dimensions': {
                'length': 10,
                'width': 8,
                'height': 4,
                'weightBrutto': 0,
            },
        }
        response = MagicMock()
        response.json.return_value = {'error': False}
        client = WildberriesAPIClient('test-key')
        client.get_card_by_nm_id = MagicMock(return_value=dict(live))
        client._make_request = MagicMock(return_value=response)

        client.update_card(
            1009,
            {'dimensions': {'weightBrutto': 0.18}},
            preserve_richer_enrichment=True,
        )

        sent = client._make_request.call_args.kwargs['json'][0]
        self.assertEqual(sent['dimensions']['weightBrutto'], 0.18)

    def test_preserve_update_does_not_hide_legacy_characteristic_removal(self):
        live = {
            'nmID': 1006,
            'subjectID': 77,
            'vendorCode': 'VC-6',
            'title': 'Чехол',
            'brand': 'Brand',
            'description': 'Описание',
            'sizes': [{'chrtID': 1, 'skus': ['1234567890123']}],
            'characteristics': [{
                'id': 88952,
                'name': 'Вес с упаковкой',
                'value': ['250 г'],
            }],
        }
        client = WildberriesAPIClient('test-key')
        client.get_card_by_nm_id = MagicMock(return_value=dict(live))
        client._make_request = MagicMock()

        with self.assertRaises(WBAPIException):
            client.update_card(
                1006,
                {'title': 'Чехол для телефона'},
                preserve_richer_enrichment=True,
                validate=False,
            )

        client._make_request.assert_not_called()

    def test_legacy_replace_photo_strategy_appends_after_live_slots(self):
        client = MagicMock()
        client.get_card_by_nm_id.return_value = {
            'nmID': 2002,
            'photos': [{
                'tm': 'https://basket-01.wbbasket.ru/live-thumb.webp',
            }],
        }
        client.upload_photos_to_card.return_value = [{
            'photo_number': 2,
            'success': True,
        }]
        existing_fp = _fp('same')
        novel_fp = {
            'pixel_sha': 'novel',
            'dhash': (1 << 64) - 1,
            'ahash': (1 << 64) - 1,
        }
        receipts = []

        with (
            patch(
                'services.wb_enrichment_merge.fingerprint_remote_photo',
                return_value=existing_fp,
            ),
            patch(
                'services.wb_enrichment_merge.fingerprint_local_photo',
                side_effect=[existing_fp, novel_fp, novel_fp],
            ),
        ):
            result = EnrichmentService.merge_photos_to_card_locked(
                client,
                seller_id=987654,
                nm_id=2002,
                photo_paths=['already.jpg', 'new.jpg'],
                strategy='replace',
                before_upload_callback=receipts.append,
            )

        self.assertEqual(result['uploaded'], 1)
        client.upload_photos_to_card.assert_called_once_with(
            2002,
            ['new.jpg'],
            seller_id=987654,
            start_photo_number=2,
        )
        self.assertEqual(receipts[0]['report']['counts']['already_present'], 1)
        self.assertEqual(receipts[0]['live_urls_before'], [
            'https://basket-01.wbbasket.ru/live-thumb.webp',
        ])

    def test_photo_merge_uses_square_variant_to_avoid_false_duplicate(self):
        client = MagicMock()
        client.get_card_by_nm_id.return_value = {
            'nmID': 2008,
            'photos': [{
                'tm': 'https://basket-01.wbbasket.ru/live-thumb.webp',
                'square': 'https://basket-01.wbbasket.ru/live-square.webp',
            }],
        }
        square_fp = _fp('same')
        portrait_fp = {
            'pixel_sha': 'portrait-crop',
            'dhash': (1 << 64) - 1,
            'ahash': (1 << 64) - 1,
        }

        def remote_fingerprint(url):
            return square_fp if 'square' in url else portrait_fp

        with (
            patch(
                'services.wb_enrichment_merge.fingerprint_remote_photo',
                side_effect=remote_fingerprint,
            ) as remote,
            patch(
                'services.wb_enrichment_merge.fingerprint_local_photo',
                return_value=square_fp,
            ),
        ):
            result = EnrichmentService.merge_photos_to_card_locked(
                client,
                seller_id=987658,
                nm_id=2008,
                photo_paths=['same-source.jpg'],
                strategy='smart_merge',
            )

        self.assertTrue(result['skipped'])
        self.assertEqual(result['reason'], 'all_supplier_photos_already_present')
        remote.assert_called_once_with(
            'https://basket-01.wbbasket.ru/live-square.webp',
        )
        client.upload_photos_to_card.assert_not_called()

    def test_photo_append_final_preflight_blocks_gallery_drift(self):
        client = MagicMock()
        initial = {
            'nmID': 2003,
            'photos': [{
                'tm': 'https://basket-01.wbbasket.ru/live-1.webp',
            }],
        }
        drifted = {
            'nmID': 2003,
            'photos': initial['photos'] + [{
                'tm': 'https://basket-01.wbbasket.ru/manual-2.webp',
            }],
        }
        client.get_card_by_nm_id.side_effect = [initial, drifted]
        novel = {
            'pixel_sha': 'novel',
            'dhash': (1 << 64) - 1,
            'ahash': (1 << 64) - 1,
        }
        receipt = MagicMock()

        with (
            patch(
                'services.wb_enrichment_merge.fingerprint_remote_photo',
                return_value=_fp('same'),
            ),
            patch(
                'services.wb_enrichment_merge.fingerprint_local_photo',
                return_value=novel,
            ),
            self.assertRaises(WbLiveMediaDrift),
        ):
            EnrichmentService.merge_photos_to_card_locked(
                client,
                seller_id=987655,
                nm_id=2003,
                photo_paths=['new.jpg'],
                strategy='smart_merge',
                before_upload_callback=receipt,
            )

        receipt.assert_called_once()
        client.upload_photos_to_card.assert_not_called()

    def test_content_pending_guard_runs_inside_lock_before_live_read(self):
        client = WildberriesAPIClient('test-key')
        client.get_card_by_nm_id = MagicMock()
        guard = MagicMock(side_effect=RuntimeError('pending receipt'))

        with self.assertRaisesRegex(RuntimeError, 'pending receipt'):
            client.update_card(
                2005,
                {'title': 'Новое название'},
                seller_id=987656,
                before_live_read_callback=guard,
            )

        guard.assert_called_once_with()
        client.get_card_by_nm_id.assert_not_called()

    def test_photo_pending_guard_runs_inside_lock_before_live_read(self):
        client = MagicMock()
        guard = MagicMock(side_effect=RuntimeError('pending photo receipt'))

        with self.assertRaisesRegex(RuntimeError, 'pending photo receipt'):
            EnrichmentService.merge_photos_to_card_locked(
                client,
                seller_id=987657,
                nm_id=2006,
                photo_paths=['new.jpg'],
                strategy='smart_merge',
                before_live_read_callback=guard,
            )

        guard.assert_called_once_with()
        client.get_card_by_nm_id.assert_not_called()

    def test_multipart_stops_after_ambiguous_slot(self):
        client = WildberriesAPIClient('test-key')
        client._make_request = MagicMock(side_effect=(
            WBTransportUncertainException(
                'timeout', request_may_have_been_applied=True,
            )
        ))

        with patch('builtins.open', mock_open(read_data=b'image')):
            result = client.upload_photos_to_card(
                2004,
                ['one.jpg', 'two.jpg'],
                seller_id=1,
                start_photo_number=3,
            )

        self.assertEqual(len(result), 1)
        self.assertTrue(result[0]['request_may_have_been_applied'])
        self.assertEqual(client._make_request.call_count, 1)

    def test_multipart_rejects_source_bytes_changed_after_plan(self):
        client = WildberriesAPIClient('test-key')
        client._make_request = MagicMock()
        planned = hashlib.sha256(b'planned-image').hexdigest()

        with patch('builtins.open', mock_open(read_data=b'changed-image')):
            result = client.upload_photos_to_card(
                2007,
                ['one.jpg'],
                seller_id=1,
                expected_source_sha256=[planned],
            )

        self.assertEqual(result[0]['error'], 'source_photo_changed')
        self.assertFalse(result[0]['request_may_have_been_applied'])
        client._make_request.assert_not_called()

    def test_multipart_sends_the_exact_bytes_pinned_by_plan(self):
        client = WildberriesAPIClient('test-key')
        payload = b'pinned-image-bytes'
        response = MagicMock(content=b'')

        def send(*_args, **kwargs):
            upload = kwargs['files']['uploadfile'][1]
            self.assertEqual(upload.read(), payload)
            self.assertIsNone(kwargs['headers']['Content-Type'])
            self.assertEqual(client.session.headers['Content-Type'], 'application/json')
            return response

        client._make_request = MagicMock(side_effect=send)
        with patch('builtins.open', mock_open(read_data=payload)):
            result = client.upload_photos_to_card(
                2008,
                ['one.jpg'],
                seller_id=1,
                expected_source_sha256=[hashlib.sha256(payload).hexdigest()],
            )

        self.assertTrue(result[0]['success'])
        self.assertEqual(client._make_request.call_count, 1)


if __name__ == '__main__':
    unittest.main()
