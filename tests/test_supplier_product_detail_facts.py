import unittest
from types import SimpleNamespace

from services.supplier_service import SupplierService


class TestSupplierProductDetailFacts(unittest.TestCase):
    @staticmethod
    def _product():
        return SimpleNamespace(
            dimensions_json=(
                '{"Ширина упаковки, см": "15", "Вес упаковки, кг": "1.44"}'
            ),
            get_characteristics=lambda: [
                {'name': 'Ширина упаковки, см', 'value': '15'},
                {'name': 'Высота упаковки, см', 'value': '21'},
            ],
            get_ai_parsed_data=lambda: {
                'Формат листов': ['A5'],
                'Комплектация': ['10 журналов'],
                'Страна производства': [],
                '_meta': {'filled_fields': 2},
            },
            get_ai_marketplace_data=lambda: {
                'characteristics': {
                    'Материал': 'Бумага',
                    'Комплектация': 'duplicate must not win',
                },
            },
        )

    def test_groups_observed_and_ai_facts_without_mixing_provenance(self):
        result = SupplierService.get_product_detail_fact_groups(
            self._product()
        )

        self.assertEqual(result['observed_count'], 3)
        self.assertEqual(
            [fact['name'] for fact in result['observed']],
            [
                'Ширина упаковки, см',
                'Высота упаковки, см',
                'Вес упаковки, кг',
            ],
        )
        self.assertEqual(result['suggested_count'], 3)
        self.assertEqual(
            [fact['name'] for fact in result['suggested']],
            ['Формат листов', 'Комплектация', 'Материал'],
        )
        self.assertEqual(result['suggested'][1]['value'], '10 журналов')

    def test_ignores_malformed_or_empty_values(self):
        product = self._product()
        product.dimensions_json = 'not-json'
        product.get_characteristics = lambda: [None, {'name': '', 'value': 1}]
        product.get_ai_parsed_data = lambda: {'_meta': {'x': 1}, 'Empty': []}
        product.get_ai_marketplace_data = lambda: {'characteristics': []}

        result = SupplierService.get_product_detail_fact_groups(product)

        self.assertEqual(result['observed'], [])
        self.assertEqual(result['suggested'], [])


if __name__ == '__main__':
    unittest.main()
