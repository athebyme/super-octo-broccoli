# -*- coding: utf-8 -*-
import importlib.util
import json
import os
import unittest
from pathlib import Path

os.environ.setdefault("SKIP_SCHEDULER", "1")

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "infographic_pilot.py"


def _load():
    spec = importlib.util.spec_from_file_location("infographic_pilot", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class VariantParsingTests(unittest.TestCase):
    def test_parse_variants(self):
        m = _load()
        variants = m.parse_variants("gen_api:flux-2:B,aitunnel:seedream-4.5:A")
        self.assertEqual(len(variants), 2)
        self.assertEqual(variants[0].provider, "gen_api")
        self.assertEqual(variants[0].model, "flux-2")
        self.assertEqual(variants[0].mode, "B")
        self.assertEqual(variants[1].mode, "A")

    def test_invalid_mode_rejected(self):
        m = _load()
        with self.assertRaises(ValueError):
            m.parse_variants("gen_api:flux-2:X")

    def test_unknown_provider_rejected(self):
        m = _load()
        with self.assertRaises(ValueError):
            m.parse_variants("magic:flux-2:A")

    def test_midjourney_edit_mode_rejected(self):
        # MJ не умеет product-preserving edit — только фоны (режим B)
        m = _load()
        with self.assertRaises(ValueError):
            m.parse_variants("gen_api:midjourney:A")
        variants = m.parse_variants("gen_api:midjourney:B")
        self.assertEqual(variants[0].mode, "B")


class CostEstimateTests(unittest.TestCase):
    def test_estimate_uses_price_table(self):
        m = _load()
        variants = m.parse_variants("gen_api:flux-2:A,aitunnel:gpt-image-2:B")
        cost = m.estimate_cost_rub(variants, n_products=10)
        expected = (m.PRICE_TABLE_RUB["gen_api:flux-2"]
                    + m.PRICE_TABLE_RUB["aitunnel:gpt-image-2"]) * 10
        self.assertAlmostEqual(cost, expected)

    def test_unknown_model_uses_default_price(self):
        m = _load()
        variants = m.parse_variants("gen_api:new-model:A")
        cost = m.estimate_cost_rub(variants, n_products=2)
        self.assertAlmostEqual(cost, m.DEFAULT_PRICE_RUB * 2)


class PhotoUrlTests(unittest.TestCase):
    def test_first_photo_from_json_list(self):
        m = _load()
        url = m.first_photo_url(json.dumps(["http://a/1.jpg", "http://a/2.jpg"]))
        self.assertEqual(url, "http://a/1.jpg")

    def test_list_input_supported(self):
        m = _load()
        self.assertEqual(m.first_photo_url(["http://a/1.jpg"]), "http://a/1.jpg")

    def test_dict_format_from_production(self):
        m = _load()
        raw = json.dumps([{"original": "http://a/1.jpg"}, {"original": "http://a/2.jpg"}])
        self.assertEqual(m.first_photo_url(raw), "http://a/1.jpg")
        self.assertIsNone(m.first_photo_url(json.dumps([{"broken": True}])))

    def test_empty_or_broken_json_gives_none(self):
        m = _load()
        self.assertIsNone(m.first_photo_url(None))
        self.assertIsNone(m.first_photo_url("not-json"))
        self.assertIsNone(m.first_photo_url("[]"))


class ReportTests(unittest.TestCase):
    def _rows(self):
        return [
            {"product_id": 1, "title": "Товар А", "variant": "gen_api:flux-2",
             "mode": "B", "status": "ok", "latency_s": 4.2, "cost_rub": 3.3,
             "output": "1_gen_api_flux-2_B.png", "error": ""},
            {"product_id": 1, "title": "Товар А", "variant": "aitunnel:seedream-4.5",
             "mode": "A", "status": "nsfw", "latency_s": 2.0, "cost_rub": 6.8,
             "output": None, "error": "NSFW: blocked"},
        ]

    def test_report_contains_grid_and_badges(self):
        m = _load()
        html = m.build_report_html(self._rows())
        self.assertIn("1_gen_api_flux-2_B.png", html)
        self.assertIn("NSFW", html)
        self.assertIn("Товар А", html)
        self.assertIn("6.8", html)

    def test_report_merges_extra_results(self):
        import tempfile
        m = _load()
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False,
                                         encoding="utf-8") as f:
            f.write(json.dumps({
                "product_id": 1, "title": "Товар А", "variant": "gpu:qwen-edit",
                "mode": "A", "status": "ok", "latency_s": 4.6, "cost_rub": 0.6,
                "output": "1_gpu_qwen-edit_A.png", "error": ""},
                ensure_ascii=False) + "\n")
            path = f.name
        html = m.build_report_html(self._rows(), extra_results_path=path)
        self.assertIn("gpu:qwen-edit", html)
        # пути картинок extra-прогона префиксуются папкой его results.jsonl
        parent = Path(path).resolve().parent.name
        self.assertIn(f"{parent}/1_gpu_qwen-edit_A.png", html)
        os.unlink(path)

    def test_summary_counts_by_variant(self):
        m = _load()
        html = m.build_report_html(self._rows())
        self.assertIn("gen_api:flux-2", html)
        self.assertIn("aitunnel:seedream-4.5", html)

    def test_report_has_price_projections(self):
        m = _load()
        html = m.build_report_html(self._rows())
        self.assertIn("₽/попытку", html)
        self.assertIn("₽/auto pass", html)
        self.assertIn("Прогноз", html)
        self.assertIn("1000 карточек", html)
        # 500 hero-only для flux-2: 500 × 3.3 × 1.2 = 1980
        self.assertIn("1980", html)


class GpuBundleTests(unittest.TestCase):
    def test_manifest_structure(self):
        m = _load()
        manifest = m.build_gpu_manifest(
            products=[(7, "Товар", "http://a/1.jpg")],
            presets=["boudoir", "neon"],
            text_files=[("text/phrase_01.png", "Гипоаллергенный силикон")],
        )
        self.assertEqual(manifest["products"], [{"id": 7, "photo": "photos/7.png"}])
        self.assertIn("boudoir", manifest["presets"])
        self.assertIn("edit_prompt", manifest["presets"]["boudoir"])
        self.assertIn("background_prompt", manifest["presets"]["neon"])
        self.assertEqual(manifest["text_samples"][0]["phrase"],
                         "Гипоаллергенный силикон")
        self.assertGreaterEqual(len(manifest["short_texts"]), 4)


class ReportOriginalsTests(unittest.TestCase):
    def test_originals_column_rendered(self):
        m = _load()
        rows = [{"product_id": 5, "title": "Т", "variant": "v", "mode": "A",
                 "status": "ok", "latency_s": 1.0, "cost_rub": 1.0,
                 "output": "5_v_A.png", "error": ""}]
        html = m.build_report_html(rows, originals={5: "orig/5.png"})
        self.assertIn("<th>Оригинал</th>", html)
        self.assertIn("orig/5.png", html)
        # без mapping колонки нет
        self.assertNotIn("Оригинал", m.build_report_html(rows))


class ProductsJsonTests(unittest.TestCase):
    def test_load_products_json_filters_invalid(self):
        import tempfile
        m = _load()
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                         encoding="utf-8") as f:
            json.dump([
                {"id": 5, "title": "Ок", "photo": "http://a/5.jpg"},
                {"id": "bad", "title": "Строка-ID", "photo": "http://a/6.jpg"},
                {"id": 7, "title": "Без фото", "photo": ""},
            ], f, ensure_ascii=False)
            path = f.name
        products = m.load_products_json(path)
        self.assertEqual(products, [(5, "Ок", "http://a/5.jpg")])
        os.unlink(path)


class ComposeTests(unittest.TestCase):
    def test_compose_places_product_on_background(self):
        import io

        from PIL import Image

        m = _load()

        def fake_cutout(product_bytes):
            img = Image.new("RGBA", (100, 200), (255, 0, 0, 0))
            from PIL import ImageDraw
            ImageDraw.Draw(img).rectangle((10, 10, 89, 189), fill=(255, 0, 0, 255))
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return buf.getvalue()

        bg = Image.new("RGB", (900, 1200), (10, 10, 10))
        bg_buf = io.BytesIO()
        bg.save(bg_buf, format="PNG")
        source = Image.new("RGB", (100, 200), (255, 0, 0))
        source_buf = io.BytesIO()
        source.save(source_buf, format="PNG")
        out = m.compose_product_on_background(
            bg_buf.getvalue(), source_buf.getvalue(), cutout=fake_cutout)
        result = Image.open(io.BytesIO(out))
        self.assertEqual(result.size, (900, 1200))
        # товар (красный) появился в нижней трети по центру
        r, g, b = result.getpixel((450, 1000))[:3]
        self.assertGreater(r, 150)
        self.assertLess(g, 100)

    def test_try_compose_swallows_errors(self):
        m = _load()
        self.assertIsNone(
            m.try_compose_mode_b(b"bg", "http://nonexistent.invalid/x.png"))


if __name__ == "__main__":
    unittest.main()
