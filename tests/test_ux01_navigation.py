"""Server-rendered UX-01 seller navigation contracts."""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path
import re
import json
import unittest
from types import SimpleNamespace

from flask import Flask, render_template


REPOSITORY = Path(__file__).resolve().parents[1]
BASELINE_FIXTURE = REPOSITORY / "tests/ux01/baseline-actions.json"


class _NavigationParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.groups = []
        self.links = []
        self._current_link = None

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        classes = set((attributes.get("class") or "").split())
        if tag == "button" and "seller-workspace-group-toggle" in classes:
            self.groups.append(attributes)
        if tag == "a" and "seller-workspace-sublink" in classes:
            self.links.append((attributes, ""))
            self._current_link = len(self.links) - 1

    def handle_data(self, data):
        if self._current_link is not None:
            attributes, text = self.links[self._current_link]
            self.links[self._current_link] = (attributes, text + data)

    def handle_endtag(self, tag):
        if tag == "a":
            if self._current_link is not None:
                attributes, text = self.links[self._current_link]
                self.links[self._current_link] = (attributes, text.strip())
            self._current_link = None


class SellerWorkspaceNavigationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Flask(__name__, template_folder=str(REPOSITORY / "templates"))
        cls.app.config.update(TESTING=True, MARKETPLACE_OZON_ENABLED=True)
        cls.app.jinja_env.globals["url_for"] = (
            lambda endpoint, *args, **kwargs: "/synthetic/" + endpoint
        )
        cls.current_nav_account = [41]
        cls.app.jinja_env.globals["mp_nav"] = lambda: SimpleNamespace(
            last_account_id=cls.current_nav_account[0]
        )

        cls.endpoint_cases = {
            "seller_my_products_beta": ("products", "Внутренние товары"),
            "supplier_catalog_product_detail": ("products", "Каталог поставщиков"),
            "supplier_catalog_export": ("products", "Каталог поставщиков"),
            "marketplace_listings.detail": ("products", "Карточки кабинетов"),
            "marketplace_drafts.detail": ("products", "Черновики Ozon"),
            "marketplace_operations.detail": ("operations", "Операции Ozon"),
            "marketplace_inbox.thread": ("communication", "Отзывы и вопросы Ozon"),
            "marketplace_finance.detail": ("analytics", "Финансы"),
            "marketplace_commercial.detail": ("prices", "Текущие цены и предложения"),
        }

        for endpoint in cls.endpoint_cases:
            path = "/case/" + endpoint.replace(".", "-")
            cls.app.add_url_rule(
                path,
                endpoint=endpoint,
                view_func=cls._render_nav,
            )
        cls.app.add_url_rule(
            "/case/settings-health",
            endpoint="ozon_account_health.page",
            view_func=cls._render_settings_nav,
        )

    @staticmethod
    def _render_nav():
        return render_template(
            "partials/seller_workspace_nav.html",
            config=SellerWorkspaceNavigationTest.app.config,
        )

    @staticmethod
    def _render_settings_nav():
        return render_template(
            "partials/seller_workspace_settings_nav.html",
            config=SellerWorkspaceNavigationTest.app.config,
        )

    def _render(self, endpoint):
        response = self.app.test_client().get(
            "/case/" + endpoint.replace(".", "-")
        )
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        parser = _NavigationParser()
        parser.feed(body)
        return body, parser

    def test_deep_routes_keep_their_group_and_parent_link_active(self):
        for endpoint, (expected_group, expected_link) in self.endpoint_cases.items():
            with self.subTest(endpoint=endpoint):
                body, parser = self._render(endpoint)
                self.assertIn(f'data-active-group="{expected_group}"', body)
                # x-cloak hides panels before Alpine can read persistent
                # sidebar state. Runtime expanded/visibility is covered by
                # the synthetic browser fixture.
                self.assertEqual(
                    sum(button.get("aria-expanded") == "true" for button in parser.groups),
                    0,
                )
                active_links = [
                    text.strip()
                    for attributes, text in parser.links
                    if "active" in (attributes.get("class") or "").split()
                ]
                self.assertTrue(
                    any(expected_link in label for label in active_links),
                    active_links,
                )

    def test_all_eight_groups_start_with_only_the_current_group_expanded(self):
        body, parser = self._render("seller_my_products_beta")
        self.assertEqual(
            [button["id"] for button in parser.groups],
            [
                "seller-workspace-toggle-overview",
                "seller-workspace-toggle-products",
                "seller-workspace-toggle-prices",
                "seller-workspace-toggle-operations",
                "seller-workspace-toggle-communication",
                "seller-workspace-toggle-analytics",
                "seller-workspace-toggle-competitors",
                "seller-workspace-toggle-promotion",
            ],
        )
        self.assertIn('data-active-group="products"', body)
        for label in (
            "Обзор", "Товары", "Цены", "Операции", "Общение", "Аналитика",
            "Конкуренты", "Продвижение",
        ):
            self.assertIn(label, body)

    def test_exact_account_health_link_requires_a_selected_account(self):
        path = "/case/settings-health"
        self.current_nav_account[0] = None
        no_selection = self.app.test_client().get(path).get_data(as_text=True)
        self.assertNotIn("Состояние кабинета Ozon", no_selection)
        self.assertNotIn("ozon_account_health.page", no_selection)

        self.current_nav_account[0] = 41
        selected = self.app.test_client().get(path).get_data(as_text=True)
        self.assertIn("href=\"/synthetic/ozon_account_health.page\"", selected)
        self.assertIn("Состояние кабинета Ozon", selected)
        self.assertIn("is-active", selected)

    def test_baseline_base_destinations_remain_in_shell_or_workspace_menu(self):
        baseline = json.loads(BASELINE_FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(baseline["baseline"], "ba63371")
        self.assertEqual(baseline["source_template"], "templates/base.html")
        self.assertRegex(baseline["source_sha256"], r"^[a-f0-9]{64}$")
        current = "\n".join(
            (REPOSITORY / path).read_text(encoding="utf-8")
            for path in (
                "templates/base.html",
                "templates/partials/seller_workspace_nav.html",
                "templates/partials/seller_workspace_utility.html",
            )
        )
        endpoint = re.compile(r"url_for\(['\"]([^'\"]+)")
        original_destinations = set(baseline["endpoints"])
        current_destinations = set(endpoint.findall(current))
        self.assertTrue(original_destinations)
        self.assertEqual(
            sorted(original_destinations - current_destinations),
            [],
            "UX-01 must preserve every destination from the old shell",
        )


if __name__ == "__main__":
    unittest.main()
