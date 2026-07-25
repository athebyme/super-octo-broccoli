# -*- coding: utf-8 -*-
"""
Контракт заявленных продавцом габаритов упаковки WB.

WB может отдавать живую карточку с ``weightBrutto: 0`` и ``isValid: false``.
Такой карточке заблокировано любое full-replacement обновление, включая
обновление одних только фото. Чинить это разрешено ровно одним способом —
фактом, который продавец сам заявил в дефолтах товаров. Захардкоженный
``DEFAULT_DIMENSIONS`` (0.1 кг) в WB уходить не должен: вес упаковки определяет
логистический тариф.
"""
import unittest
from unittest.mock import MagicMock, patch

from services.wb_api_client import WBAPIException, WildberriesAPIClient
from services.wb_package_dimensions import (
    is_usable_dimension_value,
    plan_declared_dimension_repair,
    repair_card_dimensions_in_place,
)


class DeclaredDimensionPlanTestCase(unittest.TestCase):
    def test_valid_live_value_is_never_overwritten(self):
        plan = plan_declared_dimension_repair(
            {'length': 20, 'width': 11, 'height': 17, 'weightBrutto': 1.5},
            {'length': 30, 'weightBrutto': 0.3},
        )
        self.assertEqual(plan['patch'], {})
        self.assertEqual(plan['invalid_live_keys'], [])
        self.assertEqual(plan['unresolved_keys'], [])

    def test_invalid_live_weight_is_repaired_from_declared_fact(self):
        plan = plan_declared_dimension_repair(
            {'length': 20, 'width': 11, 'height': 17, 'weightBrutto': 0},
            {'length': 30, 'width': 30, 'height': 30, 'weightBrutto': 0.3},
        )
        # Чиним только сломанный ключ: валидные 20/11/17 остаются живым фактом.
        self.assertEqual(plan['patch'], {'weightBrutto': 0.3})
        self.assertEqual(plan['invalid_live_keys'], ['weightBrutto'])
        self.assertEqual(plan['unresolved_keys'], [])

    def test_missing_live_key_is_never_fabricated(self):
        plan = plan_declared_dimension_repair(
            {'length': 20, 'width': 11, 'height': 17},
            {'weightBrutto': 0.3},
        )
        self.assertEqual(plan['patch'], {})
        self.assertEqual(plan['invalid_live_keys'], [])

    def test_without_declared_fact_the_key_stays_unresolved(self):
        plan = plan_declared_dimension_repair(
            {'length': 20, 'width': 0, 'height': 17, 'weightBrutto': 0},
            {},
        )
        self.assertEqual(plan['patch'], {})
        self.assertEqual(plan['invalid_live_keys'], ['width', 'weightBrutto'])
        self.assertEqual(plan['unresolved_keys'], ['width', 'weightBrutto'])

    def test_non_numeric_and_boolean_values_are_not_dimensions(self):
        for value in (True, False, '0.3', None, [], float('nan'), float('inf')):
            with self.subTest(value=value):
                self.assertFalse(is_usable_dimension_value(value))
                plan = plan_declared_dimension_repair(
                    {'weightBrutto': 0},
                    {'weightBrutto': value},
                )
                self.assertEqual(plan['patch'], {})
                self.assertEqual(plan['unresolved_keys'], ['weightBrutto'])

    def test_declared_weight_is_rounded_to_wb_precision(self):
        plan = plan_declared_dimension_repair(
            {'weightBrutto': 0},
            {'weightBrutto': 0.123456},
        )
        self.assertEqual(plan['patch'], {'weightBrutto': 0.123})

    def test_declared_weight_that_rounds_to_zero_is_not_a_fact(self):
        plan = plan_declared_dimension_repair(
            {'weightBrutto': 0},
            {'weightBrutto': 0.0004},
        )
        self.assertEqual(plan['patch'], {})
        self.assertEqual(plan['unresolved_keys'], ['weightBrutto'])

    def test_declared_linear_dimensions_become_integers(self):
        plan = plan_declared_dimension_repair(
            {'length': 0, 'weightBrutto': 0},
            {'length': 20.4, 'weightBrutto': 0.3},
        )
        self.assertEqual(plan['patch']['length'], 20)
        self.assertIsInstance(plan['patch']['length'], int)

    def test_heavy_declared_weight_is_not_reinterpreted_as_grams(self):
        # wb_content_payload считает значения >30 граммами по имени поля
        # поставщика. Заявленный продавцом факт уже в килограммах.
        plan = plan_declared_dimension_repair(
            {'weightBrutto': 0},
            {'weightBrutto': 50},
        )
        self.assertEqual(plan['patch'], {'weightBrutto': 50.0})

    def test_repair_in_place_skips_declared_lookup_for_valid_card(self):
        card = {'dimensions': {'weightBrutto': 1.2}}
        with patch(
            'services.wb_package_dimensions.resolve_declared_package_dimensions'
        ) as resolve:
            plan = repair_card_dimensions_in_place(card, seller_id=1)
        resolve.assert_not_called()
        self.assertEqual(plan['invalid_live_keys'], [])
        self.assertEqual(card['dimensions'], {'weightBrutto': 1.2})


def _live_card(nm_id=1001, **extra):
    card = {
        'nmID': nm_id,
        'subjectID': 5064,
        'vendorCode': f'VC-{nm_id}',
        'title': 'Товар',
        'brand': 'Brand',
        'description': 'Описание',
        'sizes': [{'chrtID': 1, 'skus': ['1234567890123']}],
        'characteristics': [{'id': 10, 'name': 'Комплектация', 'value': ['1 шт']}],
        'dimensions': {
            'length': 20, 'width': 11, 'height': 17,
            'weightBrutto': 0, 'isValid': False,
        },
    }
    card.update(extra)
    return card


@patch(
    'services.marketplace_validator.validate_wb_full_card_dictionary_values',
    return_value={'valid': True, 'issues': []},
)
@patch(
    'services.marketplace_validator.build_wb_characteristic_patch',
    side_effect=lambda _subject, patch_value, **_kw: patch_value,
)
class UpdateCardDeclaredRepairTestCase(unittest.TestCase):
    """Preserve-live путь: тот самый, что выдал ошибку по nmID=139303555."""

    def _client(self, card):
        client = WildberriesAPIClient('test-key')
        client.get_card_by_nm_id = MagicMock(return_value=card)
        response = MagicMock()
        response.json.return_value = {'error': False}
        client._make_request = MagicMock(return_value=response)
        return client

    def test_declared_weight_unblocks_card_wb_itself_marked_invalid(
        self, _patch, _validate,
    ):
        client = self._client(_live_card())

        with patch(
            'services.wb_package_dimensions.resolve_declared_package_dimensions',
            return_value={'length': 30, 'width': 30, 'height': 30, 'weightBrutto': 0.3},
        ):
            context = {}
            client.update_card(
                1001,
                {'characteristics': [{'id': 11, 'value': ['Новое значение']}]},
                seller_id=42,
                snapshot_context=context,
                preserve_richer_enrichment=True,
                _content_lock_held=True,
            )

        sent = client._make_request.call_args.kwargs['json'][0]
        self.assertEqual(sent['dimensions']['weightBrutto'], 0.3)
        # Валидные живые габариты не подменяются заявленными 30/30/30.
        self.assertEqual(sent['dimensions']['length'], 20)
        self.assertEqual(sent['dimensions']['width'], 11)
        self.assertEqual(sent['dimensions']['height'], 17)

        repair = context['merge_decisions']['declared_dimension_repair']
        self.assertEqual(repair['source'], 'seller_product_defaults')
        self.assertEqual(repair['invalid_live_keys'], ['weightBrutto'])
        self.assertEqual(repair['applied'], ['weightBrutto'])
        self.assertEqual(repair['unresolved_keys'], [])

    def test_without_declared_fact_write_is_refused_with_actionable_reason(
        self, _patch, _validate,
    ):
        client = self._client(_live_card(nm_id=1002))

        with patch(
            'services.wb_package_dimensions.resolve_declared_package_dimensions',
            return_value={},
        ):
            with self.assertRaises(WBAPIException) as ctx:
                client.update_card(
                    1002,
                    {'characteristics': [{'id': 11, 'value': ['Новое значение']}]},
                    seller_id=42,
                    preserve_richer_enrichment=True,
                    _content_lock_held=True,
                )

        message = str(ctx.exception)
        self.assertIn('weightBrutto', message)
        self.assertIn('Дефолты товаров', message)
        client._make_request.assert_not_called()

    def test_repair_never_turns_a_no_op_into_a_wb_write(self, _patch, _validate):
        card = _live_card(nm_id=1003)
        client = self._client(card)

        with patch(
            'services.wb_package_dimensions.resolve_declared_package_dimensions',
            return_value={'weightBrutto': 0.3},
        ) as resolve:
            result = client.update_card(
                1003,
                # Живое значение богаче — merge-план отвергает патч целиком.
                {'characteristics': [{'id': 10, 'value': ['1']}]},
                seller_id=42,
                preserve_richer_enrichment=True,
                _content_lock_held=True,
            )

        self.assertTrue(result['skipped'])
        self.assertEqual(result['reason'], 'nothing_to_update')
        resolve.assert_not_called()
        client._make_request.assert_not_called()

    def test_valid_live_weight_is_kept_and_defaults_are_not_read(
        self, _patch, _validate,
    ):
        card = _live_card(nm_id=1004)
        card['dimensions'] = {
            'length': 20, 'width': 11, 'height': 17, 'weightBrutto': 1.5,
        }
        client = self._client(card)

        with patch(
            'services.wb_package_dimensions.resolve_declared_package_dimensions',
            return_value={'weightBrutto': 0.3},
        ) as resolve:
            client.update_card(
                1004,
                {'characteristics': [{'id': 11, 'value': ['Новое значение']}]},
                seller_id=42,
                preserve_richer_enrichment=True,
                _content_lock_held=True,
            )

        resolve.assert_not_called()
        sent = client._make_request.call_args.kwargs['json'][0]
        self.assertEqual(sent['dimensions']['weightBrutto'], 1.5)


class MergedBatchDeclaredRepairTestCase(unittest.TestCase):
    """Legacy batch-путь: массовые правки и публикация контента из чата."""

    def setUp(self):
        self.client = WildberriesAPIClient('token-batch')

    def _fetched(self, nm_id, weight):
        from services.wb_validators import _mark_wb_card_as_fetched
        return _mark_wb_card_as_fetched({
            'nmID': nm_id,
            'vendorCode': f'VC-{nm_id}',
            'title': f'Товар {nm_id}',
            'brand': 'Old',
            'subjectID': 5064,
            'characteristics': [],
            'sizes': [{'skus': ['1234567890123']}],
            'dimensions': {
                'length': 20, 'width': 11, 'height': 17,
                'weightBrutto': weight, 'isValid': weight > 0,
            },
        })

    def test_hardcoded_default_weight_no_longer_reaches_wb(self):
        cards_map = {1: self._fetched(1, 0)}
        with patch.object(
            self.client, 'fetch_cards_by_nm_ids', return_value=cards_map,
        ), patch.object(
            self.client, 'update_cards_batch',
            side_effect=lambda cards, **kw: {'error': False},
        ) as batch, patch(
            'services.wb_package_dimensions.resolve_declared_package_dimensions',
            return_value={'weightBrutto': 0.3},
        ):
            result = self.client.update_cards_merged(
                {1: {'brand': 'New'}}, seller_id=42,
            )

        self.assertEqual(result['invalid'], {})
        sent_card = batch.call_args.args[0][0]
        # Раньше здесь молча оказывались захардкоженные 0.1 кг.
        self.assertEqual(sent_card['dimensions']['weightBrutto'], 0.3)

    def test_card_without_declared_fact_is_reported_and_not_sent(self):
        cards_map = {1: self._fetched(1, 0), 2: self._fetched(2, 0.7)}
        with patch.object(
            self.client, 'fetch_cards_by_nm_ids', return_value=cards_map,
        ), patch.object(
            self.client, 'update_cards_batch',
            side_effect=lambda cards, **kw: {'error': False},
        ) as batch, patch(
            'services.wb_package_dimensions.resolve_declared_package_dimensions',
            return_value={},
        ):
            result = self.client.update_cards_merged(
                {1: {'brand': 'New'}, 2: {'brand': 'New'}}, seller_id=42,
            )

        self.assertIn(1, result['invalid'])
        self.assertIn('вес упаковки', result['invalid'][1])
        # Исправная карточка того же батча не блокируется соседкой.
        sent_ids = [card['nmID'] for card in batch.call_args.args[0]]
        self.assertEqual(sent_ids, [2])

    @patch(
        'services.marketplace_validator.validate_wb_full_card_dictionary_values',
        return_value={'valid': True, 'issues': []},
    )
    def test_batch_guard_refuses_prepared_card_without_declared_fact(
        self, _validate,
    ):
        from services.wb_validators import prepare_card_for_update

        prepared = prepare_card_for_update(self._fetched(3, 0), {})
        with patch(
            'services.wb_package_dimensions.resolve_declared_package_dimensions',
            return_value={},
        ):
            with self.assertRaises(WBAPIException) as ctx:
                self.client.update_cards_batch(
                    [prepared], seller_id=42, validate=False,
                    _content_lock_held=True,
                )

        self.assertIn('вес упаковки', str(ctx.exception))


if __name__ == '__main__':
    unittest.main()
