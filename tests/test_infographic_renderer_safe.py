# -*- coding: utf-8 -*-
import io
import unittest

from PIL import Image

from services.infographic_renderer import (
    _build_overlay_html,
    _build_slide_html,
    _template_background_bytes,
    render_hybrid_slides,
)


class InfographicRendererSafetyTests(unittest.TestCase):
    def test_markup_escapes_copy_and_rejects_css_color_injection(self):
        slide = {
            "type": "hero",
            "title": '<script>alert("x")</script>',
            "subtitle": '<img src=x onerror="alert(1)">',
            "bullets": ["<b>unsafe</b>"],
        }
        design = {"color_palette": ["red;position:fixed", "#112233"]}
        for markup in (
            _build_overlay_html(slide, design),
            _build_slide_html(slide, design),
        ):
            self.assertNotIn("<script>", markup)
            self.assertNotIn("<img src=x", markup)
            self.assertNotIn("red;position:fixed", markup)
            self.assertIn("&lt;script&gt;", markup)

    def test_template_background_has_exact_contract(self):
        data = _template_background_bytes({})
        image = Image.open(io.BytesIO(data))
        self.assertEqual(image.size, (900, 1200))
        self.assertEqual(image.format, "PNG")

    def test_unverified_rich_content_fails_before_generation(self):
        class UnexpectedService:
            def generate_background(self, _scene):
                raise AssertionError("provider must not be called")

        result = render_hybrid_slides(
            {"slides": [{"title": "invented", "subtitle": "claim"}]},
            UnexpectedService(),
        )
        self.assertFalse(result[0]["success"])
        self.assertEqual(result[0]["quality"]["status"], "rejected")


if __name__ == "__main__":
    unittest.main()
