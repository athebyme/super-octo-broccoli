# -*- coding: utf-8 -*-
import unittest

from services.infographic_content import (
    build_fact_pack,
    build_fact_safe_rich_content,
    validate_fact_safe_rich_content,
    visible_texts,
)


class FactSafeContentTests(unittest.TestCase):
    def test_builds_only_available_facts_without_filler(self):
        pack = build_fact_pack(
            title="Футболка хлопковая",
            brand="Бренд",
            category="Футболки",
            characteristics={"Материал": "Хлопок", "Цвет": "Чёрный"},
        )
        content = build_fact_safe_rich_content(pack)
        self.assertEqual(content["total_slides"], 2)
        self.assertEqual(content["slides"][1]["type"], "fact_grid")
        self.assertEqual(len(content["slides"][1]["facts"]), 2)
        ok, errors = validate_fact_safe_rich_content(content)
        self.assertTrue(ok, errors)
        self.assertIn("Хлопок", visible_texts(content))
        self.assertNotIn("ХИТ", visible_texts(content))

    def test_tampered_copy_rejected(self):
        content = build_fact_safe_rich_content(build_fact_pack(title="Товар"))
        content["slides"][0]["title"] = "Лучший товар"
        ok, errors = validate_fact_safe_rich_content(content)
        self.assertFalse(ok)
        self.assertTrue(any("не совпадает" in error for error in errors))

    def test_unverified_promo_in_stored_title_rejected(self):
        with self.assertRaises(ValueError):
            build_fact_safe_rich_content(build_fact_pack(title="ХИТ продаж"))

    def test_too_long_copy_fails_closed_instead_of_clipping(self):
        with self.assertRaises(ValueError):
            build_fact_safe_rich_content(build_fact_pack(title="А" * 161))

    def test_groups_concise_facts_and_deduplicates_semantic_twins(self):
        pack = build_fact_pack(
            title="Товар",
            brand="Lola",
            characteristics={
                "Бренд": "Lola",
                "Материал": "Силикон",
                "Состав": "Силикон",
                "Цвет": "Чёрный",
                "Страна": "Германия",
            },
        )
        characteristic_facts = [
            fact for fact in pack["facts"]
            if fact["kind"] == "characteristic"
        ]
        self.assertEqual(
            [fact["label"] for fact in characteristic_facts],
            ["Материал", "Страна", "Цвет"],
        )

        content = build_fact_safe_rich_content(pack)
        self.assertEqual(content["policy"], "fact_safe_v2")
        self.assertEqual(len(content["slides"]), 2)
        self.assertEqual(len(content["slides"][1]["facts"]), 3)

    def test_tampered_fact_card_is_rejected(self):
        content = build_fact_safe_rich_content(build_fact_pack(
            title="Товар",
            characteristics={"Материал": "Хлопок"},
        ))
        content["slides"][1]["facts"][0]["value"] = "Лучший хлопок"

        ok, errors = validate_fact_safe_rich_content(content)

        self.assertFalse(ok)
        self.assertTrue(any("fact 1 value" in error for error in errors))

    def test_imported_product_provenance_is_explicit(self):
        pack = build_fact_pack(
            title="Крем",
            characteristics={"Объём": "50 мл"},
            source_prefix="imported_product",
        )
        self.assertEqual(pack["facts"][0]["source"], "imported_product.title")
        self.assertEqual(
            pack["facts"][1]["source"],
            "imported_product.characteristics.Объём",
        )


if __name__ == "__main__":
    unittest.main()
