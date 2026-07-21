# -*- coding: utf-8 -*-
from types import SimpleNamespace
from pathlib import Path
import unittest
from unittest.mock import patch

from flask import Flask
from flask_login import LoginManager

from routes.admin_sales_intelligence import register_admin_sales_intelligence_routes


class AdminSalesIntelligenceRouteTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(
            TESTING=True,
            SECRET_KEY="admin-sales-routes",
            MARKETPLACE_OZON_ENABLED=True,
        )
        LoginManager(self.app)

        @self.app.get("/dashboard", endpoint="dashboard")
        def dashboard():
            return "dashboard"

        register_admin_sales_intelligence_routes(self.app)
        self.client = self.app.test_client()

    @staticmethod
    def _user(*, admin):
        return SimpleNamespace(
            id=41,
            is_authenticated=True,
            is_active=True,
            is_admin=admin,
        )

    def _auth(self, *, admin=True):
        user = self._user(admin=admin)
        return (
            patch("routes.admin_sales_intelligence.current_user", user),
            patch("flask_login.utils._get_user", return_value=user),
        )

    def test_admin_bulk_handoff_passes_server_filters_and_authenticated_user(self):
        user_patch, login_patch = self._auth(admin=True)
        with user_patch, login_patch, patch(
            "routes.admin_sales_intelligence.AdminSalesIntelligenceService.recommend",
            return_value={
                "total": 2, "created": 2, "updated": 0, "skipped": 0,
            },
        ) as recommend:
            response = self.client.post("/admin/sales/recommendations", data={
                "period": "30d",
                "marketplace": "all",
                "seller_id": "7",
                "sort": "opportunity",
                "ready": "1",
                "page": "1",
                "per_page": "50",
                "row_keys": ["wb:product:10", "ozon:account:4:listing:9"],
            })
        self.assertEqual(response.status_code, 302)
        kwargs = recommend.call_args.kwargs
        self.assertEqual(kwargs["admin_user_id"], 41)
        self.assertEqual(kwargs["row_keys"], [
            "wb:product:10", "ozon:account:4:listing:9",
        ])
        self.assertEqual(kwargs["filters"].seller_id, 7)
        self.assertTrue(kwargs["filters"].ready_only)
        self.assertTrue(kwargs["include_ozon"])

    def test_non_admin_is_denied_before_dashboard_service(self):
        user_patch, login_patch = self._auth(admin=False)
        with user_patch, login_patch, patch(
            "routes.admin_sales_intelligence.AdminSalesIntelligenceService.dashboard",
        ) as dashboard:
            response = self.client.get("/admin/sales/")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith("/dashboard"))
        dashboard.assert_not_called()

    def test_alpine_selection_json_is_html_attribute_escaped(self):
        template_path = (
            Path(__file__).resolve().parents[1]
            / "templates"
            / "admin_sales_intelligence.html"
        )
        source = template_path.read_text(encoding="utf-8")
        self.assertIn("selectable_keys|tojson|forceescape", source)
        self.assertNotIn("selectable_keys|tojson }}", source)


if __name__ == "__main__":
    unittest.main()
