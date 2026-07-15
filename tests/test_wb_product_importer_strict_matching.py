import json
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from services.wb_product_importer import WBProductImporter


class TestWBProductImporterStrictCharacteristicMatching(unittest.TestCase):
    SUBJECT_ID = 777

    @staticmethod
    def _product(characteristics):
        return SimpleNamespace(
            wb_subject_id=TestWBProductImporterStrictCharacteristicMatching.SUBJECT_ID,
            external_id='strict-characteristics',
            characteristics=json.dumps(characteristics, ensure_ascii=False),
        )

    @staticmethod
    def _importer(wb_chars):
        importer = WBProductImporter.__new__(WBProductImporter)
        importer.seller = SimpleNamespace(id=1)
        importer.api_client = SimpleNamespace(
            get_card_characteristics_config=MagicMock(
                return_value={'data': wb_chars},
            ),
        )
        # Production batch preflight fills this shared admin-cache snapshot
        # once per subject before per-card mapping begins.
        importer._chars_config_cache = {
            TestWBProductImporterStrictCharacteristicMatching.SUBJECT_ID: wb_chars,
        }
        importer._category_sizes_cache = {}
        importer._wb_directories_cache = {}
        importer._wb_validation_cache = {}
        importer._assemble_chars_from_fields = MagicMock(return_value={})
        importer._collect_ai_characteristics = MagicMock(return_value={})
        importer._get_category_default_chars = MagicMock(return_value={})
        return importer

    def _build(self, importer, product):
        with (
            patch(
                'routes.product_defaults.get_defaults_for_product',
                return_value={},
            ),
            patch(
                'services.marketplace_validator.build_wb_characteristic_patch',
                side_effect=lambda _subject_id, values, **_kwargs: values,
            ),
        ):
            return importer._build_wb_characteristics(product)

    def test_substring_and_first_word_names_are_not_auto_applied(self):
        cases = (
            (
                'Материал изделия дополнительный',
                'Силикон',
                {'charcID': 10, 'name': 'Материал изделия', 'charcType': 1},
            ),
            (
                'Цвет основной оттенок',
                'Красный',
                {'charcID': 20, 'name': 'Цвет', 'charcType': 1},
            ),
            (
                'Длина товара',
                '20',
                {'charcID': 30, 'name': 'Длина секс игрушки', 'charcType': 4},
            ),
            (
                'Назначение',
                'для взрослых',
                {'charcID': 50, 'name': 'Пол', 'charcType': 1},
            ),
            (
                'Для кого',
                'для пары',
                {'charcID': 50, 'name': 'Пол', 'charcType': 1},
            ),
        )

        for supplied_name, value, wb_char in cases:
            with self.subTest(supplied_name=supplied_name):
                importer = self._importer([wb_char])
                result = self._build(
                    importer,
                    self._product({supplied_name: value}),
                )
                self.assertEqual(result, [])

    def test_explicit_alias_still_requires_exact_target_name(self):
        importer = self._importer([
            {'charcID': 20, 'name': 'Цвет', 'charcType': 1},
        ])

        result = self._build(
            importer,
            self._product({'Цвет товара': 'Красный'}),
        )

        self.assertEqual(result, [{'id': 20, 'value': ['Красный']}])

    def test_build_fails_closed_when_reference_preflight_fails(self):
        importer = self._importer([
            {'charcID': 10, 'name': 'Материал изделия', 'charcType': 1},
        ])
        importer._chars_config_cache = {}

        with patch(
            'services.marketplace_service.MarketplaceService.ensure_wb_references_current',
            return_value={'success': False, 'error': 'upstream unavailable'},
        ):
            with self.assertRaisesRegex(ValueError, 'Не удалось актуализировать'):
                self._build(
                    importer,
                    self._product({'Материал изделия': 'Силикон'}),
                )

    def test_required_coverage_rejects_empty_cached_live_config(self):
        importer = self._importer([])
        importer._chars_config_cache[self.SUBJECT_ID] = []

        with self.assertRaisesRegex(ValueError, 'пустую или некорректную конфигурацию'):
            importer._validate_characteristics_coverage(
                self.SUBJECT_ID,
                [],
                'empty-live-schema',
            )

        importer.api_client.get_card_characteristics_config.assert_not_called()

    def test_compound_country_is_rejected_instead_of_rewritten(self):
        importer = self._importer([
            {
                'charcID': 40,
                'name': 'Страна производства',
                'charcType': 1,
                'maxCount': 1,
                'dictionary': [
                    {'value': 'Россия'},
                    {'value': 'Китай'},
                ],
            },
        ])

        with self.assertRaisesRegex(ValueError, 'Россия-Китай.*отсутствует'):
            self._build(
                importer,
                self._product({'Страна производства': 'Россия-Китай'}),
            )

    def test_country_alias_is_not_auto_rewritten(self):
        importer = self._importer([
            {
                'charcID': 40,
                'name': 'Страна производства',
                'charcType': 1,
                'maxCount': 1,
                'dictionary': [{'value': 'Великобритания'}],
            },
        ])

        with self.assertRaisesRegex(ValueError, 'Англия.*отсутствует'):
            self._build(
                importer,
                self._product({'Страна производства': 'Англия'}),
            )


if __name__ == '__main__':
    unittest.main()
