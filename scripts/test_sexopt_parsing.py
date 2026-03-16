#!/usr/bin/env python3
"""
Тест парсинга CSV sex-opt.ru через новый header-based csv_column_mapping.
Скачивает первые 10 строк CSV и прогоняет через парсер.
"""

import sys
import os
import json
import csv
from io import StringIO

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Тестовый CSV (первые строки из реального файла)
TEST_CSV = '''"code";"article";"title";"group_code";"group_title";"category_code";"category_title";"tmn";"msk";"nsk";"start_price";"price";"discount";"image";"image1";"image2";"material";"size";"length";"width";"color";"weight";"battery";"waterproof";"country";"manufacturer";"barcode";"new";"hit";"description";"collection";"video";"url";"rst";"spb";"fixed_price";"pieces";"brand_code";"brand_title";"created";"3d";"width_packed";"height_packed";"length_packed";"weight_packed";"modification_code";"images";"retail_price";"kdr";"category_new_code";"category_new_title";"embed3d";"minsk";"ast";"barcodes";"retail_price_minsk";"marked"
"0T-00000877";"44032";"Лубрикант на водной основе HOT Exxtreme Glide, 100 мл";"0T-00000842";"HOT";"67";"Лубриканты/Анальные";"";"11";"";"765.00";"596.70";"";"https://old.sex-opt.ru/images/26108dde8cb181a91b73b291a56f2cc4.jpg";"";"";"";"";"";"";"";"";"";"";"";"HOT Productions";"4042342000719";"";"";"Описание товара";"";"";"https://old.sex-opt.ru/catalogue/item/5";"";"11";"";"";"000000023";"HOT";"2012-11-07 15:37:50";"";"3.5";"14.0";"6.0";"0.123";"";"https://old.sex-opt.ru/images/26108dde8cb181a91b73b291a56f2cc4.jpg";"";"";"480";"Гели, смазки и лубриканты/Гели и смазки для анального секса";"";"";"";"4042342000719";"1050.00";""
"0T-00000863";"44033";"Лубрикант на водной основе HOT Exxtreme Glide, 30 мл";"0T-00000842";"HOT";"67";"Лубриканты/Анальные";"1";"11";"";"475.00";"370.50";"";"https://old.sex-opt.ru/images/d6f3543a5be1eb55d0fd6636efcbfc79.jpg";"https://old.sex-opt.ru/images/extra1.jpg";"";"";"";"";"";"";"";"";"";"";"HOT Productions";"4042342000702";"";"";"Описание 2";"";"";"https://old.sex-opt.ru/catalogue/item/6";"";"5";"";"";"000000023";"HOT";"2012-11-07 15:37:50";"";"3.0";"10.0";"5.5";"0.043";"";"https://old.sex-opt.ru/images/d6f3543a5be1eb55d0fd6636efcbfc79.jpg";"1500.00";"";"480";"Гели, смазки и лубриканты/Гели и смазки для анального секса";"";"3";"";"4042342000702";"649.60";""'''


MAPPING = {
    "external_id": {"column": "code", "type": "string"},
    "vendor_code": {"column": "article", "type": "string"},
    "title": {"column": "title", "type": "string"},
    "brand": {"column": "brand_title", "type": "string"},
    "categories": {"column": "category_title", "type": "list", "separator": "/"},
    "description": {"column": "description", "type": "string"},
    "country": {"column": "country", "type": "string"},
    "supplier_price": {"column": "price", "type": "number"},
    "recommended_retail_price": {"column": "retail_price", "type": "number"},
    "colors": {"column": "color", "type": "list", "separator": ","},
    "materials": {"column": "material", "type": "list", "separator": ","},
    "sizes_raw": {"column": "size", "type": "string"},
    "barcodes": {"column": "barcodes", "type": "list", "separator": ","},
    "supplier_quantity": {
        "columns": ["msk", "spb", "tmn", "rst", "nsk", "ast"],
        "type": "stock_sum"
    },
    "photo_urls": {
        "columns": ["image", "image1", "image2"],
        "type": "photo_urls"
    },
    "characteristics": {
        "columns": {
            "length": "Длина, см",
            "width": "Ширина, см",
            "weight": "Вес, кг",
            "battery": "Тип батареек",
            "waterproof": "Водонепроницаемость"
        },
        "type": "characteristics"
    },
    "dimensions": {
        "columns": {
            "width_packed": "Ширина упаковки, см",
            "height_packed": "Высота упаковки, см",
            "length_packed": "Длина упаковки, см",
            "weight_packed": "Вес упаковки, кг"
        },
        "type": "characteristics"
    },
}


def test_header_resolve():
    """Test: resolve header names to indices"""
    reader = csv.reader(StringIO(TEST_CSV), delimiter=';', quotechar='"')
    header_row = next(reader)
    header_index = {col.strip().strip('"'): idx for idx, col in enumerate(header_row)}

    print("=== Header Resolution ===")
    print(f"Total headers: {len(header_index)}")
    print(f"Sample: code={header_index.get('code')}, title={header_index.get('title')}, "
          f"msk={header_index.get('msk')}, brand_title={header_index.get('brand_title')}")

    # Verify all mapping columns exist
    errors = []
    for field, config in MAPPING.items():
        col = config.get('column')
        if isinstance(col, str) and col not in header_index:
            errors.append(f"  MISSING: '{col}' (field '{field}')")
        cols = config.get('columns')
        if isinstance(cols, list):
            for c in cols:
                if isinstance(c, str) and c not in header_index:
                    errors.append(f"  MISSING: '{c}' (field '{field}')")
        if isinstance(cols, dict):
            for c in cols:
                if isinstance(c, str) and c not in header_index:
                    errors.append(f"  MISSING: '{c}' (field '{field}')")

    if errors:
        print("ERRORS:")
        for e in errors:
            print(e)
        return False
    else:
        print("All mapping columns found in headers!")
        return True


def test_parsing():
    """Test: full parsing through SupplierCSVParser (standalone, no Flask)"""
    from unittest.mock import MagicMock

    mock_supplier = MagicMock()
    mock_supplier.code = 'sexopt'
    mock_supplier.csv_column_mapping = MAPPING
    mock_supplier.csv_has_header = True

    # Import parser directly
    from services.supplier_service import SupplierCSVParser
    parser = SupplierCSVParser.__new__(SupplierCSVParser)
    parser.supplier = mock_supplier
    parser.delimiter = ';'
    parser.encoding = 'utf-8'

    products = parser._parse_with_mapping(TEST_CSV)

    print(f"\n=== Parsing Results ===")
    print(f"Products parsed: {len(products)}")

    for i, p in enumerate(products):
        print(f"\n--- Product {i+1} ---")
        print(f"  external_id: {p.get('external_id')}")
        print(f"  vendor_code: {p.get('vendor_code')}")
        print(f"  title: {p.get('title')[:60]}...")
        print(f"  brand: {p.get('brand')}")
        print(f"  category: {p.get('category')}")
        print(f"  all_categories: {p.get('all_categories')}")
        print(f"  supplier_price: {p.get('supplier_price')}")
        print(f"  recommended_retail_price: {p.get('recommended_retail_price')}")
        print(f"  supplier_quantity: {p.get('supplier_quantity')}")
        print(f"  photo_urls: {len(p.get('photo_urls', []))} photos")
        for ph in p.get('photo_urls', []):
            print(f"    {ph.get('original', '')[:80]}")
        print(f"  barcode(s): {p.get('barcodes')}")
        print(f"  description: {p.get('description', '')[:60]}...")
        print(f"  country: {p.get('country')}")
        print(f"  characteristics: {p.get('characteristics', [])}")

    # Validations
    assert len(products) == 2, f"Expected 2 products, got {len(products)}"
    p1 = products[0]
    assert p1['external_id'] == '0T-00000877', f"Wrong external_id: {p1['external_id']}"
    assert p1['vendor_code'] == '44032', f"Wrong vendor_code: {p1['vendor_code']}"
    assert p1['brand'] == 'HOT', f"Wrong brand: {p1['brand']}"
    assert p1['supplier_price'] == 596.70, f"Wrong price: {p1['supplier_price']}"
    assert p1['supplier_quantity'] == 22, f"Wrong quantity: {p1['supplier_quantity']}"
    assert len(p1['photo_urls']) >= 1, "No photos parsed"
    assert p1['all_categories'] == ['Лубриканты', 'Анальные'], f"Wrong categories: {p1['all_categories']}"

    p2 = products[1]
    assert p2['supplier_quantity'] == 17, f"Wrong quantity p2: {p2['supplier_quantity']}"
    assert p2['recommended_retail_price'] == 1500.0, f"Wrong RRP: {p2['recommended_retail_price']}"
    assert len(p2['photo_urls']) == 2, f"Expected 2 photos, got {len(p2['photo_urls'])}"

    print("\n=== ALL TESTS PASSED ===")
    return True


if __name__ == '__main__':
    ok = test_header_resolve()
    if ok:
        test_parsing()
