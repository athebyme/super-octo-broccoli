# -*- coding: utf-8 -*-
"""Exact, punctuation-tolerant Ozon forbidden-brand policy."""

import unittest

from services.ozon_brand_policy import (
    OZON_FORBIDDEN_BRAND_NAMES,
    match_forbidden_ozon_brand,
)


class OzonBrandPolicyTest(unittest.TestCase):
    def test_every_configured_brand_matches_its_canonical_name(self):
        self.assertEqual(len(OZON_FORBIDDEN_BRAND_NAMES), 76)
        for brand in OZON_FORBIDDEN_BRAND_NAMES:
            with self.subTest(brand=brand):
                self.assertEqual(
                    match_forbidden_ozon_brand(brand),
                    brand,
                )

    def test_case_space_punctuation_and_yo_variants_are_blocked(self):
        self.assertEqual(
            match_forbidden_ozon_brand("  b vibe "),
            "B-VIBE",
        )
        self.assertEqual(
            match_forbidden_ozon_brand("my size"),
            "MY.SIZE",
        )
        self.assertEqual(
            match_forbidden_ozon_brand("штучки — дрючки"),
            "Штучки-дрючки",
        )
        self.assertEqual(
            match_forbidden_ozon_brand("ёska"),
            "ЁSKA",
        )

    def test_substrings_are_not_blocked(self):
        for brand in (
            "HOT WHEELS",
            "ORION FOODS",
            "ON THE GO",
            "Springfield",
            "Private Label Allowed",
        ):
            with self.subTest(brand=brand):
                self.assertIsNone(match_forbidden_ozon_brand(brand))


if __name__ == "__main__":
    unittest.main()
