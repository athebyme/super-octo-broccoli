# -*- coding: utf-8 -*-
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from flask import Flask
from flask_login import LoginManager

from routes.image_lab import register_image_lab_routes
from services.admin_sales_intelligence import AdminSalesIntelligenceError


class ImageLabRecommendationRouteTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(TESTING=True, SECRET_KEY="image-lab-recommendations")
        LoginManager(self.app)
        register_image_lab_routes(self.app)
        self.client = self.app.test_client()

    @staticmethod
    def _user():
        return SimpleNamespace(
            id=17,
            seller=SimpleNamespace(id=71),
            is_authenticated=True,
            is_active=True,
            is_admin=False,
        )

    def _auth(self):
        user = self._user()
        return (
            patch("routes.image_lab.current_user", user),
            patch("flask_login.utils._get_user", return_value=user),
        )

    def test_review_uses_authenticated_seller_and_rejects_scope_smuggling(self):
        recommendation = SimpleNamespace(id=9, status="completed")
        user_patch, login_patch = self._auth()
        with user_patch, login_patch, patch(
            "routes.image_lab.AdminSalesIntelligenceService.review_recommendation",
            return_value=recommendation,
        ) as review:
            response = self.client.post(
                "/image-lab/api/recommendations/9/status",
                json={"status": "completed"},
            )
            smuggled = self.client.post(
                "/image-lab/api/recommendations/9/status",
                json={"status": "completed", "seller_id": 999},
            )
        self.assertEqual(response.status_code, 200)
        review.assert_called_once_with(
            seller_id=71,
            recommendation_id=9,
            user_id=17,
            status="completed",
        )
        self.assertEqual(smuggled.status_code, 400)

    def test_foreign_recommendation_is_hidden(self):
        user_patch, login_patch = self._auth()
        with user_patch, login_patch, patch(
            "routes.image_lab.AdminSalesIntelligenceService.review_recommendation",
            side_effect=AdminSalesIntelligenceError("Рекомендация не найдена"),
        ):
            response = self.client.post(
                "/image-lab/api/recommendations/404/status",
                json={"status": "dismissed"},
            )
        self.assertEqual(response.status_code, 404)
        self.assertNotIn("seller", response.get_data(as_text=True).lower())


if __name__ == "__main__":
    unittest.main()
