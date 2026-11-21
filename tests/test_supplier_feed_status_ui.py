"""Focused rendering checks for seller-visible supplier feed freshness."""

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
import unittest

from flask import Flask
from jinja2 import ChoiceLoader, DictLoader, FileSystemLoader


ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ("supplier_catalog.html", "supplier_catalog_products.html")


def supplier(**overrides):
    values = {
        "csv_column_mapping": None,
        "last_sync_at": datetime(2026, 9, 21, 10, 15),
        "last_sync_status": "success",
        "price_file_url": "https://private.example/feed.csv?token=hidden",
        "auto_sync_prices": True,
        "last_price_sync_at": datetime(2026, 9, 20, 8, 30),
        "last_price_sync_status": "success",
        "last_price_sync_attempt_at": datetime(2026, 9, 20, 8, 30),
        "last_price_sync_error": "provider response must stay private",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class SupplierFeedStatusRenderingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Flask(__name__, template_folder=str(ROOT / "templates"))
        # Load the real page templates and their macros, but avoid rendering the
        # full application shell when exercising the isolated status macro.
        cls.app.jinja_loader = ChoiceLoader([
            DictLoader({"base.html": "{# isolated template test #}"}),
            FileSystemLoader(str(ROOT / "templates")),
        ])

    def render_status(self, template_name, source):
        template = self.app.jinja_env.get_template(template_name)
        return str(template.module.supplier_feed_freshness(source))

    def test_separate_feed_shows_success_attempt_setting_and_utc_without_private_details(self):
        source = supplier(
            last_sync_status="failed",
            last_price_sync_status="failed",
            last_price_sync_attempt_at=datetime(2026, 9, 22, 11, 45),
        )
        for template_name in TEMPLATES:
            with self.subTest(template=template_name):
                html = self.render_status(template_name, source)
                self.assertIn("Каталог", html)
                self.assertIn("Цены и остатки · отдельный файл", html)
                self.assertIn("Автообновление: включено", html)
                self.assertIn('datetime="2026-09-21T10:15:00Z"', html)
                self.assertIn("21.09.2026 10:15 UTC", html)
                self.assertIn('datetime="2026-09-20T08:30:00Z"', html)
                self.assertIn('datetime="2026-09-22T11:45:00Z"', html)
                self.assertEqual(html.count("Последняя попытка завершилась ошибкой"), 2)
                self.assertNotIn("private.example", html)
                self.assertNotIn("token=hidden", html)
                self.assertNotIn("provider response must stay private", html)

    def test_failed_first_price_attempt_is_not_rendered_as_a_success_or_running_job(self):
        source = supplier(
            last_price_sync_at=None,
            last_price_sync_status="failed",
            last_price_sync_attempt_at=datetime(2026, 9, 22, 11, 45),
            auto_sync_prices=False,
        )
        html = self.render_status(TEMPLATES[0], source)
        self.assertIn("Автообновление: выключено", html)
        self.assertIn("Успешная загрузка не подтверждена", html)
        self.assertIn("Последняя попытка завершилась ошибкой", html)
        self.assertIn('datetime="2026-09-22T11:45:00Z"', html)
        self.assertNotIn("Обновление выполняется", html)

    def test_unconfirmed_attempt_is_not_claimed_as_running(self):
        source = supplier(
            last_price_sync_status="running",
            last_price_sync_attempt_at=datetime(2026, 9, 22, 11, 45),
        )
        html = self.render_status(TEMPLATES[0], source)
        self.assertIn("Результат последней попытки не подтверждён", html)
        self.assertNotIn("Синхронизация выполняется", html)

    def test_combined_feed_uses_catalog_time_and_unconfigured_price_feed_is_omitted(self):
        combined = supplier(
            price_file_url=None,
            csv_column_mapping={
                "supplier_price": {"column": 3, "type": "number"},
                "supplier_quantity": {"column": 4, "type": "number"},
            },
        )
        html = self.render_status(TEMPLATES[1], combined)
        self.assertIn("Цена и остатки · в каталоге", html)
        self.assertIn("Обновляются вместе с каталогом", html)
        self.assertIn('datetime="2026-09-21T10:15:00Z"', html)
        self.assertNotIn('datetime="2026-09-20T08:30:00Z"', html)
        self.assertNotIn("Автообновление:", html)

        no_price_feed = supplier(price_file_url=None, csv_column_mapping=None)
        omitted = self.render_status(TEMPLATES[0], no_price_feed)
        self.assertIn("Каталог", omitted)
        self.assertNotIn("Цены и остатки", omitted)
        self.assertNotIn("Автообновление:", omitted)


if __name__ == "__main__":
    unittest.main()
