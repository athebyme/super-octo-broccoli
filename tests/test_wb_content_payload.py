# -*- coding: utf-8 -*-
"""
Regression tests for WB Content API card payload normalization.
"""

import unittest

from services.wb_content_payload import (
    build_dimensions,
    extract_characteristics,
    normalize_create_cards_payload,
)
from services.wb_validators import (
    WBValidationError,
    clean_characteristics_for_update,
    prepare_card_for_update,
    prepare_create_cards_for_wb,
)


class TestWBContentPayload(unittest.TestCase):
    def test_extract_characteristics_moves_legacy_packed_weight_to_dimensions(self):
        raw = {
            "Цвет": "черный",
            "Вес с упаковкой": "200 г",
            "Длина упаковки, см": "12",
            "Ширина упаковки, см": "8",
            "Высота упаковки, см": "3",
        }

        extracted = extract_characteristics(raw)

        self.assertEqual(extracted.values, {"Цвет": "черный"})
        self.assertEqual(extracted.dimensions, {
            "weightBrutto": 0.2,
            "length": 12,
            "width": 8,
            "height": 3,
        })
        self.assertIn("Вес с упаковкой", extracted.dropped)

    def test_normalize_create_payload_drops_wb_weight_characteristic_id(self):
        payload = [{
            "subjectID": 123,
            "variants": [{
                "vendorCode": "SKU-1",
                "brand": "Brand",
                "title": "Товар",
                "description": "Описание товара",
                "dimensions": {"length": 10, "width": 8, "height": 4, "weightBrutto": 0.1},
                "sizes": [{"price": 1000, "skus": ["2000000000011"]}],
                "characteristics": [
                    {"id": "88952", "value": "250 г"},
                    {"id": 14177449, "value": ["Россия"]},
                ],
            }],
        }]

        normalized = normalize_create_cards_payload(payload)
        variant = normalized[0]["variants"][0]

        self.assertEqual(variant["dimensions"], {
            "length": 10,
            "width": 8,
            "height": 4,
            "weightBrutto": 0.25,
        })
        self.assertEqual(variant["characteristics"], [
            {"id": 14177449, "value": ["Россия"]},
        ])

    def test_normalize_create_payload_keeps_net_weight_as_number(self):
        payload = [{
            "subjectID": 123,
            "variants": [{
                "vendorCode": "SKU-1",
                "brand": "Brand",
                "title": "Товар",
                "description": "Описание товара",
                "dimensions": {"length": 10, "width": 8, "height": 4, "weightBrutto": 0.1},
                "sizes": [{"price": 1000, "skus": ["2000000000011"]}],
                "characteristics": [
                    {
                        "id": 123456,
                        "name": "Вес товара без упаковки (г)",
                        "charcType": "4",
                        "value": "250 г",
                    },
                ],
            }],
        }]

        normalized = normalize_create_cards_payload(payload)
        characteristic = normalized[0]["variants"][0]["characteristics"][0]

        self.assertEqual(characteristic, {"id": 123456, "value": 250})

    def test_prepare_create_cards_for_wb_validates_final_shape(self):
        payload = [{
            "subjectID": 123,
            "variants": [{
                "vendorCode": "SKU-1",
                "brand": "Brand",
                "title": "Товар",
                "sizes": [{"price": 1000, "skus": ["2000000000011"]}],
                "characteristics": [{"id": 14177449, "value": ["Россия"]}],
            }],
        }]

        normalized = prepare_create_cards_for_wb(payload)

        variant = normalized[0]["variants"][0]
        self.assertEqual(set(variant["dimensions"]), {"length", "width", "height", "weightBrutto"})
        self.assertEqual(variant["dimensions"]["weightBrutto"], 0.1)

    def test_prepare_create_cards_for_wb_rejects_missing_barcode(self):
        payload = [{
            "subjectID": 123,
            "variants": [{
                "vendorCode": "SKU-1",
                "brand": "Brand",
                "title": "Товар",
                "sizes": [{"price": 1000, "skus": []}],
            }],
        }]

        with self.assertRaises(WBValidationError) as exc_info:
            prepare_create_cards_for_wb(payload)

        self.assertIn("skus", str(exc_info.exception))

    def test_prepare_card_for_update_merges_dimensions_and_strips_weight_char(self):
        full_card = {
            "nmID": 100,
            "vendorCode": "SKU-1",
            "brand": "Brand",
            "imtID": 200,
            "subjectID": 300,
            "subjectName": "Subject",
            "needKiz": False,
            "isSwatchTryOn": False,
            "sizes": [{"chrtID": 10, "skus": ["2000000000011"]}],
            "dimensions": {
                "length": 10,
                "width": 8,
                "height": 4,
                "weightBrutto": 0.1,
                "isValid": True,
            },
            "characteristics": [
                {"id": "88952", "value": "400 г"},
                {"id": 14177449, "value": "Россия"},
            ],
            "photos": ["https://example.test/photo.jpg"],
            "createdAt": "2026-01-01T00:00:00Z",
            "updatedAt": "2026-01-01T00:00:00Z",
        }

        prepared = prepare_card_for_update(full_card, {"dimensions": {"width": 9}})

        self.assertEqual(prepared["dimensions"], {
            "length": 10,
            "width": 9,
            "height": 4,
            "weightBrutto": 0.4,
        })
        self.assertEqual(prepared["characteristics"], [
            {"id": 14177449, "value": ["Россия"]},
        ])
        self.assertNotIn("photos", prepared)
        self.assertNotIn("imtID", prepared)
        self.assertNotIn("subjectID", prepared)
        self.assertNotIn("subjectName", prepared)
        self.assertNotIn("needKiz", prepared)
        self.assertNotIn("isSwatchTryOn", prepared)
        self.assertNotIn("createdAt", prepared)
        self.assertNotIn("updatedAt", prepared)
        self.assertNotIn("isValid", prepared["dimensions"])

    def test_build_dimensions_converts_mm_and_grams_from_legacy_sources(self):
        dimensions = build_dimensions(
            {"length": 10, "width": 8, "height": 4, "weightBrutto": 0.1},
            {"package_length_mm": 120, "package_weight_g": 350},
        )

        self.assertEqual(dimensions["length"], 12)
        self.assertEqual(dimensions["weightBrutto"], 0.35)

    def test_build_dimensions_later_product_sources_override_fallback_defaults(self):
        dimensions = build_dimensions(
            {"length": 10, "width": 8, "height": 4, "weightBrutto": 0.1},
            {"Вес с упаковкой": "500 г"},
            {"package_weight_g": 250},
        )

        self.assertEqual(dimensions["weightBrutto"], 0.25)

    def test_clean_characteristics_for_update_keeps_net_weight_as_number(self):
        cleaned = clean_characteristics_for_update([
            {
                "id": 123456,
                "name": "Вес товара без упаковки (г)",
                "charcType": "4",
                "value": ["318"],
            },
            {"id": 14177449, "name": "Страна производства", "value": "Россия"},
        ])

        self.assertEqual(cleaned, [
            {"id": 123456, "value": 318},
            {"id": 14177449, "value": ["Россия"]},
        ])


if __name__ == "__main__":
    unittest.main()
