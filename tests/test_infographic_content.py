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
        self.assertEqual(content["total_slides"], 3)
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
            build_fact_safe_rich_content(build_fact_pack(title="А" * 121))


if __name__ == "__main__":
    unittest.main()
