# -*- coding: utf-8 -*-
"""Тесты полного приёма фида поставщика andrey (sex-opt.ru).

Покрывают: merge-категории из нескольких колонок, полный список штрихкодов,
РРЦ-fallback из retail_price_minsk, видео, dict-габариты, сбор несмаппленных
колонок в raw_extra, освежение original_data_json и доводку полей до
ImportedProduct.
"""

import json
import unittest
from types import SimpleNamespace

from migrations.migrate_andrey_feed_full_ingest import ANDREY_CSV_COLUMN_MAPPING
from services.supplier_service import (
    SupplierCSVParser,
    _copy_to_imported_product,
    _update_imported_from_supplier,
    _update_supplier_product,
)
from models import ImportedProduct, SupplierProduct


FEED_HEADER = [
    'code', 'article', 'title', 'group_code', 'group_title', 'category_code',
    'category_title', 'tmn', 'msk', 'nsk', 'start_price', 'price', 'discount',
    'image', 'image1', 'image2', 'material', 'size', 'length', 'width',
    'color', 'weight', 'battery', 'waterproof', 'country', 'manufacturer',
    'barcode', 'new', 'hit', 'description', 'collection', 'video', 'url',
    'rst', 'spb', 'fixed_price', 'pieces', 'brand_code', 'brand_title',
    'created', '3d', 'width_packed', 'height_packed', 'length_packed',
    'weight_packed', 'modification_code', 'images', 'retail_price', 'kdr',
    'category_new_code', 'category_new_title', 'embed3d', 'minsk', 'ast',
    'barcodes', 'retail_price_minsk', 'marked',
]

ROW_1 = {
    'code': '0T-00000877',
    'article': '44032',
    'title': 'Лубрикант на водной основе HOT Exxtreme Glide, 100 мл',
    'group_code': '0T-00000842',
    'group_title': 'HOT',
    'category_code': '67',
    'category_title': 'Лубриканты/Анальные',
    'tmn': '2', 'msk': '1', 'nsk': '',
    'start_price': '765.00', 'price': '596.70', 'discount': '',
    'image': 'https://old.sex-opt.ru/images/aaa.jpg',
    'image1': '', 'image2': '',
    'material': '', 'size': '', 'length': '', 'width': '',
    'color': '', 'weight': '',
    'battery': 'LR44', 'waterproof': 'Да',
    'country': 'Германия', 'manufacturer': 'HOT Productions',
    'barcode': '4042342000719', 'new': '', 'hit': '',
    'description': 'Анальный лубрикант с маслом зверобоя.',
    'collection': '',
    'video': 'https://old.sex-opt.ru/download/video/embed/108',
    'url': 'https://old.sex-opt.ru/catalogue/item/5',
    'rst': '', 'spb': '9', 'fixed_price': '', 'pieces': '',
    'brand_code': '000000023', 'brand_title': 'HOT',
    'created': '2012-11-07 15:37:50', '3d': '',
    'width_packed': '3.5', 'height_packed': '14.0',
    'length_packed': '6.0', 'weight_packed': '0.123',
    'modification_code': 'MOD-1',
    'images': 'https://old.sex-opt.ru/images/aaa.jpg,https://old.sex-opt.ru/images/bbb.jpg',
    'retail_price': '', 'kdr': '3',
    'category_new_code': '480',
    'category_new_title': 'Гели, смазки и лубриканты/Гели и смазки для анального секса',
    'embed3d': '', 'minsk': '', 'ast': '',
    'barcodes': '4042342000719,4042342000720',
    'retail_price_minsk': '1050.00', 'marked': '1',
}


def _csv_line(values):
    return ';'.join('"%s"' % v for v in values)


def _build_csv(rows):
    lines = [_csv_line(FEED_HEADER)]
    for row in rows:
        lines.append(_csv_line([row.get(col, '') for col in FEED_HEADER]))
    return '\n'.join(lines)


def _make_parser(mapping=None):
    supplier = SimpleNamespace(
        code='andrey',
        csv_column_mapping=mapping if mapping is not None else ANDREY_CSV_COLUMN_MAPPING,
        csv_has_header=True,
        csv_delimiter=';',
        csv_encoding='utf-8',
    )
    return SupplierCSVParser(supplier)


class TestAndreyFeedParsing(unittest.TestCase):
    def _parse_one(self):
        products = _make_parser().parse(_build_csv([ROW_1]))
        self.assertEqual(len(products), 1)
        return products[0]

    def test_categories_merged_new_tree_first(self):
        product = self._parse_one()
        self.assertEqual(product['category'], 'Гели, смазки и лубриканты')
        self.assertEqual(product['all_categories'], [
            'Гели, смазки и лубриканты',
            'Гели и смазки для анального секса',
            'Лубриканты',
            'Анальные',
        ])

    def test_full_barcodes_list(self):
        product = self._parse_one()
        self.assertEqual(product['barcodes'], ['4042342000719', '4042342000720'])

    def test_rrp_fallback_parsed(self):
        product = self._parse_one()
        self.assertIsNone(product['recommended_retail_price'])
        self.assertEqual(product['recommended_retail_price_fallback'], 1050.0)

    def test_video_url_parsed(self):
        product = self._parse_one()
        self.assertEqual(
            product['video_url'],
            'https://old.sex-opt.ru/download/video/embed/108',
        )

    def test_dimensions_are_dict(self):
        product = self._parse_one()
        self.assertEqual(product['dimensions'], {
            'Ширина упаковки, см': '3.5',
            'Высота упаковки, см': '14.0',
            'Длина упаковки, см': '6.0',
            'Вес упаковки, кг': '0.123',
        })

    def test_characteristics_include_manufacturer_and_battery(self):
        product = self._parse_one()
        chars = {c['name']: c['value'] for c in product['characteristics']}
        self.assertEqual(chars.get('Производитель'), 'HOT Productions')
        self.assertEqual(chars.get('Тип батареек'), 'LR44')
        self.assertEqual(chars.get('Водонепроницаемость'), 'Да')

    def test_stock_sum_includes_kdr(self):
        product = self._parse_one()
        # tmn=2 + msk=1 + spb=9 + kdr=3
        self.assertEqual(product['supplier_quantity'], 15)

    def test_photo_urls_deduplicated(self):
        product = self._parse_one()
        urls = [p['original'] for p in product['photo_urls']]
        self.assertEqual(urls, [
            'https://old.sex-opt.ru/images/aaa.jpg',
            'https://old.sex-opt.ru/images/bbb.jpg',
        ])

    def test_raw_extra_collects_unmapped_columns(self):
        product = self._parse_one()
        raw_extra = product.get('raw_extra')
        self.assertIsInstance(raw_extra, dict)
        self.assertEqual(raw_extra['marked'], '1')
        self.assertEqual(raw_extra['modification_code'], 'MOD-1')
        self.assertEqual(raw_extra['start_price'], '765.00')
        self.assertEqual(raw_extra['group_title'], 'HOT')
        self.assertEqual(raw_extra['url'], 'https://old.sex-opt.ru/catalogue/item/5')
        self.assertEqual(raw_extra['barcode'], '4042342000719')
        # Пустые ячейки не сохраняются
        self.assertNotIn('nsk', raw_extra)
        self.assertNotIn('discount', raw_extra)
        # Смаппленные колонки не дублируются
        self.assertNotIn('code', raw_extra)
        self.assertNotIn('category_title', raw_extra)
        self.assertNotIn('category_new_title', raw_extra)
        self.assertNotIn('manufacturer', raw_extra)

    def test_raw_extra_disabled_without_flag(self):
        mapping = {
            k: v for k, v in ANDREY_CSV_COLUMN_MAPPING.items()
            if k != '_include_unmapped'
        }
        products = _make_parser(mapping).parse(_build_csv([ROW_1]))
        self.assertEqual(len(products), 1)
        self.assertNotIn('raw_extra', products[0])


class TestUpdateSupplierProduct(unittest.TestCase):
    def _data(self, **overrides):
        data = {
            'external_id': '0T-00000877',
            'vendor_code': '44032',
            'title': 'Лубрикант HOT',
            'description': 'Описание',
            'brand': 'HOT',
            'category': 'Гели, смазки и лубриканты',
            'all_categories': ['Гели, смазки и лубриканты'],
            'barcodes': ['4042342000719', '4042342000720'],
            'supplier_price': 596.7,
            'recommended_retail_price': None,
            'recommended_retail_price_fallback': 1050.0,
            'video_url': 'https://old.sex-opt.ru/download/video/embed/108',
            'raw_extra': {'marked': '1'},
        }
        data.update(overrides)
        return data

    def test_dimension_names_routed_out_of_characteristics(self):
        # Габариты упаковки из фида не должны оседать среди характеристик:
        # они переезжают в dimensions_json под исходными именами.
        sp = SupplierProduct(supplier_id=1)
        _update_supplier_product(sp, self._data(characteristics={
            'Ширина упаковки, см': '10',
            'Вес упаковки, кг': '0.3',
            'Питание': 'батарейки',
        }))
        self.assertEqual(
            json.loads(sp.characteristics_json),
            {'Питание': 'батарейки'},
        )
        dims = json.loads(sp.dimensions_json)
        self.assertEqual(dims['Ширина упаковки, см'], '10')
        self.assertEqual(dims['Вес упаковки, кг'], '0.3')

    def test_fresh_empty_characteristics_clear_stale_json(self):
        # Ранее условная перезапись оставляла загрязнённый JSON навсегда.
        sp = SupplierProduct(supplier_id=1)
        sp.characteristics_json = json.dumps(
            {'Длина упаковки, см': '6'}, ensure_ascii=False)
        _update_supplier_product(sp, self._data(characteristics={}))
        self.assertIsNone(sp.characteristics_json)

    def test_full_barcodes_saved(self):
        sp = SupplierProduct(supplier_id=1)
        _update_supplier_product(sp, self._data())
        self.assertEqual(sp.barcode, '4042342000719')
        self.assertEqual(
            json.loads(sp.barcodes_json),
            ['4042342000719', '4042342000720'],
        )

    def test_rrp_fallback_used_when_primary_missing(self):
        sp = SupplierProduct(supplier_id=1)
        _update_supplier_product(sp, self._data())
        self.assertEqual(sp.recommended_retail_price, 1050.0)

    def test_rrp_primary_wins(self):
        sp = SupplierProduct(supplier_id=1)
        _update_supplier_product(
            sp, self._data(recommended_retail_price=990.0)
        )
        self.assertEqual(sp.recommended_retail_price, 990.0)

    def test_video_url_saved(self):
        sp = SupplierProduct(supplier_id=1)
        _update_supplier_product(sp, self._data())
        self.assertEqual(
            sp.video_url, 'https://old.sex-opt.ru/download/video/embed/108'
        )

    def test_original_data_refreshed_each_sync(self):
        sp = SupplierProduct(supplier_id=1)
        _update_supplier_product(sp, self._data())
        first = json.loads(sp.original_data_json)
        self.assertEqual(first['raw_extra'], {'marked': '1'})

        _update_supplier_product(
            sp,
            self._data(title='Новое название', raw_extra={'marked': ''}),
        )
        second = json.loads(sp.original_data_json)
        self.assertEqual(second['title'], 'Новое название')
        self.assertEqual(second['raw_extra'], {'marked': ''})


class TestImportedProductCopy(unittest.TestCase):
    def _supplier_product(self):
        sp = SupplierProduct(supplier_id=1)
        sp.id = 10
        sp.external_id = '0T-00000877'
        sp.title = 'Лубрикант HOT'
        sp.barcode = '4042342000719'
        sp.barcodes_json = json.dumps(['4042342000719', '4042342000720'])
        sp.recommended_retail_price = 1050.0
        sp.original_data_json = json.dumps({'title': 'Лубрикант HOT'})
        sp.content_revision = 1
        return sp

    def test_copy_full_barcodes_and_rrp(self):
        imp = _copy_to_imported_product(seller_id=1, sp=self._supplier_product())
        self.assertEqual(
            json.loads(imp.barcodes), ['4042342000719', '4042342000720']
        )
        self.assertEqual(imp.recommended_retail_price, 1050.0)

    def test_copy_falls_back_to_single_barcode(self):
        sp = self._supplier_product()
        sp.barcodes_json = None
        imp = _copy_to_imported_product(seller_id=1, sp=sp)
        self.assertEqual(json.loads(imp.barcodes), ['4042342000719'])

    def test_update_refreshes_barcodes_rrp_and_original(self):
        sp = self._supplier_product()
        imp = ImportedProduct(seller_id=1)
        imp.barcodes = json.dumps(['old'])
        imp.original_data = json.dumps({'title': 'старое'})
        _update_imported_from_supplier(imp, sp)
        self.assertEqual(
            json.loads(imp.barcodes), ['4042342000719', '4042342000720']
        )
        self.assertEqual(imp.recommended_retail_price, 1050.0)
        self.assertEqual(json.loads(imp.original_data)['title'], 'Лубрикант HOT')

    def test_update_keeps_existing_when_supplier_empty(self):
        sp = self._supplier_product()
        sp.barcode = None
        sp.barcodes_json = None
        sp.recommended_retail_price = None
        sp.original_data_json = None
        imp = ImportedProduct(seller_id=1)
        imp.barcodes = json.dumps(['old'])
        imp.recommended_retail_price = 500.0
        imp.original_data = json.dumps({'title': 'старое'})
        _update_imported_from_supplier(imp, sp)
        self.assertEqual(json.loads(imp.barcodes), ['old'])
        self.assertEqual(imp.recommended_retail_price, 500.0)
        self.assertEqual(json.loads(imp.original_data)['title'], 'старое')


class TestCharAliases(unittest.TestCase):
    def test_battery_type_alias_present(self):
        from services.wb_product_importer import CHAR_ALIASES
        # WB использует разные имена в разных категориях (live-схема
        # 2026-07-18: Вибраторы 5067 — «Питание»); первый найденный
        # в схеме кандидат выигрывает
        self.assertEqual(
            CHAR_ALIASES['тип батареек'],
            ['тип элемента питания', 'питание'],
        )

    def test_alias_values_are_str_or_list_of_str(self):
        from services.wb_product_importer import CHAR_ALIASES
        for our_name, target in CHAR_ALIASES.items():
            if isinstance(target, str):
                continue
            self.assertIsInstance(target, list, our_name)
            self.assertTrue(
                all(isinstance(t, str) for t in target), our_name
            )


if __name__ == '__main__':
    unittest.main()
