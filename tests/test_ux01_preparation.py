"""Focused rendering checks for the shared, Ozon-scoped preparation journey."""
from pathlib import Path
import re
import unittest

from flask import Flask, render_template_string


ROOT = Path(__file__).resolve().parents[1]


class PreparationJourneyRenderingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Flask(
            __name__,
            template_folder=str(ROOT / "templates"),
            static_folder=str(ROOT / "static"),
        )
        cls.app.config.update(SECRET_KEY="ux01-preparation-template-test")
        endpoints = {
            "supplier_catalog": "/supplier-catalog",
            "seller_my_products": "/my-products",
            "marketplace_drafts.index": "/marketplaces/drafts/",
            "ozon_bulk_uploads.index": "/marketplaces/ozon/uploads/",
            "marketplace_listings.index": "/marketplaces/listings/",
        }
        for index, (endpoint, path) in enumerate(endpoints.items()):
            cls.app.add_url_rule(
                path,
                endpoint=endpoint,
                view_func=lambda: "",
            )

    def _render(self, body):
        with self.app.test_request_context("/"):
            return render_template_string(
                '{% from "partials/preparation_workspace_journey.html" '
                'import journey, listing_end_state %}' + body
            )

    def test_current_and_next_are_visible_and_full_path_is_accessible_disclosure(self):
        html = self._render(
            "{{ journey('draft', 'Ozon CI 0', 'черновик №7 · внутренний товар №5', "
            "'Сохраните локально и откройте свежую проверку.', "
            "draft_url='/marketplaces/drafts/?account_id=9', "
            "review_url='/marketplaces/ozon/uploads/review?account_id=9&draft_ids=7', "
            "internal_url='/my-products?account_id=9', "
            "result_url='/marketplaces/ozon/uploads/?account_id=9', "
            "listing_url='/marketplaces/listings/?marketplace=ozon&account_id=9', "
            "channel='Ozon') }}"
        )
        self.assertIn("Сейчас: Черновик Ozon", html)
        self.assertIn("<strong>Дальше:</strong> Сохраните локально", html)
        self.assertIn("Магазин:</strong> Ozon CI 0", html)
        self.assertIn("черновик №7 · внутренний товар №5", html)
        self.assertRegex(html, r'<details class="preparation-journey-path">')
        self.assertNotRegex(html, r'<details[^>]*\sopen(?:\s|>)')
        self.assertEqual(html.count('<li class="preparation-journey-step'), 7)
        self.assertIn('aria-current="step"', html)
        self.assertIn('href="/marketplaces/ozon/uploads/review?account_id=9&amp;draft_ids=7"', html)
        self.assertRegex(html, r'href="/my-products\?account_id=9"')
        self.assertIn('href="/marketplaces/listings/?marketplace=ozon&amp;account_id=9"', html)
        self.assertNotIn('href="false"', html)

    def test_missing_actions_explain_the_gate_without_adding_publish_link(self):
        html = self._render(
            "{{ journey('draft', 'Ozon CI 0', '2 exact draft versions', "
            "'Проверьте строки и отдельно подтвердите отправку ниже.', "
            "review_reason='Сначала сохраните черновик и выберите точный набор.', "
            "publish_reason='Подтверждение находится ниже; сервер перепроверит точные версии.', "
            "channel='Ozon') }}"
        )
        self.assertIn("Сейчас: Черновик Ozon", html)
        self.assertIn("2 exact draft versions", html)
        self.assertIn("Сначала сохраните черновик и выберите точный набор.", html)
        self.assertIn("Подтверждение находится ниже; сервер перепроверит точные версии.", html)
        self.assertIn('aria-disabled="true"', html)
        self.assertNotRegex(html, r'href="[^"]*(?:publish|send|confirm)[^"]*"')
        self.assertNotIn("is-complete", html)

    def test_listing_end_state_keeps_actions_scoped_to_account(self):
        html = self._render("{{ listing_end_state(17) }}")
        self.assertIn("Карточка Ozon", html)
        self.assertIn("сама по себе не подтверждает предыдущие этапы", html)
        self.assertIn('href="/marketplaces/drafts/?account_id=17"', html)
        self.assertIn('href="/marketplaces/ozon/uploads/?account_id=17"', html)
        self.assertNotRegex(html, r'href="/marketplaces/(?:drafts|ozon/uploads)/[^\"]*account_id=(?!17)')

    def test_source_and_internal_names_are_distinct_and_escaped(self):
        html = self._render(
            "{{ journey('source', none, '<SupplierProduct 44>', "
            "'Импортируйте источник в отдельный внутренний товар; это не создаёт карточку Ozon.', channel='Ozon') }}"
        )
        self.assertIn("Источник поставщика", html)
        self.assertIn("Внутренний товар", html)
        self.assertIn("&lt;SupplierProduct 44&gt;", html)
        self.assertNotIn("<SupplierProduct 44>", html)
        self.assertIn("не создаёт карточку Ozon", html)

    def test_owned_journey_call_sites_compile_with_the_real_jinja_loader(self):
        templates = (
            "supplier_catalog.html",
            "supplier_catalog_products.html",
            "supplier_catalog_product_detail.html",
            "seller_my_products.html",
            "my_products_beta.html",
            "marketplace_drafts.html",
            "marketplace_drafts_classic.html",
            "marketplace_draft_detail.html",
            "marketplace_draft_detail_classic.html",
            "ozon_upload_review.html",
            "ozon_bulk_uploads.html",
            "ozon_bulk_upload_detail.html",
            "marketplace_listing_detail.html",
            "marketplace_listing_beta_detail.html",
            "partials/preparation_workspace_journey.html",
        )
        for name in templates:
            with self.subTest(template=name):
                self.app.jinja_env.get_template(name)


if __name__ == "__main__":
    unittest.main()
