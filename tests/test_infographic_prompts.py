# -*- coding: utf-8 -*-
import os
import unittest

os.environ.setdefault("SKIP_SCHEDULER", "1")

from services.infographic_prompts import (
    ATMOSPHERE_PRESETS,
    SHORT_TEXT_SAMPLES,
    TEXT_SAMPLE_PHRASES,
    build_background_prompt,
    build_edit_prompt,
    build_native_scene_prompt,
    sanitize_prompt,
)


class PresetTests(unittest.TestCase):
    def test_all_presets_present(self):
        self.assertEqual(
            set(ATMOSPHERE_PRESETS.keys()), {"boudoir", "neon", "luxury", "spa"}
        )

    def test_edit_prompt_keeps_product_and_bans_text(self):
        p = build_edit_prompt("boudoir")
        self.assertIn("Preserve the complete foreground", p)
        self.assertIn("do not generate text", p.lower())
        self.assertIn("do not add people", p.lower())
        self.assertIn("including any person already present", p.lower())

    def test_background_prompt_has_no_product_words(self):
        p = build_background_prompt("neon")
        self.assertIn("empty", p.lower())
        self.assertIn("no people", p.lower())

    def test_native_scene_prompt_forbids_duplicate_and_requires_review(self):
        p = build_native_scene_prompt("spa")
        self.assertIn("exactly one", p.lower())
        self.assertIn("second copy", p.lower())
        self.assertIn("one coherent photograph", p.lower())
        self.assertIn("pasted cutout", p.lower())
        self.assertIn("human identity review", p.lower())

    def test_unknown_preset_raises(self):
        with self.assertRaises(KeyError):
            build_edit_prompt("disco")


class SanitizerTests(unittest.TestCase):
    def test_risky_terms_replaced(self):
        out = sanitize_prompt("вибратор для взрослых, эротический стиль")
        low = out.lower()
        self.assertNotIn("вибратор", low)
        self.assertNotIn("эрот", low)

    def test_neutral_text_unchanged(self):
        self.assertEqual(sanitize_prompt("гель на водной основе"),
                         "гель на водной основе")


class TextSampleTests(unittest.TestCase):
    def test_phrase_pool_sizes(self):
        self.assertGreaterEqual(len(TEXT_SAMPLE_PHRASES), 12)
        self.assertGreaterEqual(len(SHORT_TEXT_SAMPLES), 4)
        # уровни текста 2–3: длинные фразы и короткие слова разделены
        self.assertTrue(all(len(s) <= 12 for s in SHORT_TEXT_SAMPLES))


if __name__ == "__main__":
    unittest.main()
