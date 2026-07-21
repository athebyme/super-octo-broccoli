import io
import unittest

from PIL import Image

from services.infographic_renderer import (
    WB_HEIGHT,
    WB_WIDTH,
    _build_overlay_html,
    _template_background_bytes,
)


class InfographicCatalogRendererTests(unittest.TestCase):
    DESIGN = {
        "color_palette": ["#173f2a", "#e0a52b", "#edf4e8", "#ffffff"],
        "font_style": "modern",
    }

    def test_fact_grid_renders_grouped_cards_and_escapes_values(self):
        markup = _build_overlay_html({
            "number": 2,
            "type": "fact_grid",
            "facts": [
                {"label": "Материал", "value": "Силикон"},
                {"label": "Цвет", "value": "<Чёрный>"},
            ],
        }, self.DESIGN)

        self.assertIn("Характеристики", markup)
        self.assertEqual(markup.count('class="fact-card"'), 2)
        self.assertIn("&lt;Чёрный&gt;", markup)
        self.assertNotIn("<Чёрный>", markup)

    def test_hero_has_large_catalog_panel_for_long_exact_title(self):
        title = "Вакуумная помпа с эрекционным кольцом Discovery Explorer"
        markup = _build_overlay_html({
            "number": 1,
            "type": "hero",
            "eyebrow": "Товары для взрослых",
            "title": title,
            "subtitle": "Lola toys",
        }, self.DESIGN)

        self.assertIn(title, markup)
        self.assertIn('height:326px', markup)
        self.assertIn('КАТАЛОГ', markup)

    def test_template_background_is_exact_wb_canvas(self):
        data = _template_background_bytes(self.DESIGN, "fact_grid", 1)
        image = Image.open(io.BytesIO(data))

        self.assertEqual(image.size, (WB_WIDTH, WB_HEIGHT))
        self.assertGreater(image.entropy(), 1.0)


if __name__ == "__main__":
    unittest.main()
