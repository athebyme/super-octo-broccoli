# -*- coding: utf-8 -*-
"""Доступ и поведение админского экрана compliance-дефолтов Ozon."""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import unittest

from flask import Flask
from flask_login import LoginManager

from models import (
    Marketplace,
    MarketplaceProductDraft,
    MarketplaceProductType,
    OzonMarkingRegistryVersion,
    User,
    db,
)
from routes.admin_ozon_compliance import admin_ozon_compliance_bp


class ComplianceRouteAuthTestCase(unittest.TestCase):
    def test_non_admin_is_rejected(self):
        from routes.admin_ozon_compliance import _admin_required

        wrapped = _admin_required(lambda: 'ok')
        user = MagicMock()
        user.is_authenticated = True
        user.is_admin = False
        with patch('routes.admin_ozon_compliance.current_user', user):
            with self.assertRaises(Exception):
                wrapped()


class AdminOzonComplianceRoutesTestCase(unittest.TestCase):
    """Роуты не должны сами трогать БД в обход сервиса — сервис мокается,
    а вызов проверяется по позиционным/именованным аргументам, чтобы поймать
    как раз то, что бриф считает критичным: ``user_id`` берётся только из
    ``current_user.id``, а не из тела запроса.
    """

    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(
            TESTING=True,
            SECRET_KEY="admin-ozon-compliance-routes",
            SQLALCHEMY_DATABASE_URI="sqlite://",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
        )
        db.init_app(self.app)
        LoginManager(self.app)
        self.app.register_blueprint(admin_ozon_compliance_bp)
        self.client = self.app.test_client()
        with self.app.app_context():
            db.create_all()
            marketplace = Marketplace(
                name="Ozon", code="ozon", adapter_code="ozon", is_active=True,
            )
            db.session.add(marketplace)
            user = User(
                username="admin", email="admin@example.com", password_hash="x",
            )
            db.session.add(user)
            db.session.commit()
            self.marketplace_id = marketplace.id
            self.user_id = user.id

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.drop_all()

    @staticmethod
    def _user(*, admin=True, user_id=1):
        return SimpleNamespace(
            id=user_id, is_authenticated=True, is_active=True, is_admin=admin,
        )

    def _auth(self, *, admin=True, user_id=1):
        user = self._user(admin=admin, user_id=user_id)
        return (
            patch("routes.admin_ozon_compliance.current_user", user),
            patch("flask_login.utils._get_user", return_value=user),
        )

    def _make_product_type(self, external_type_id="1609"):
        with self.app.app_context():
            product_type = MarketplaceProductType(
                marketplace_id=self.marketplace_id,
                category_id=1,
                external_type_id=external_type_id,
                name="Тип",
            )
            db.session.add(product_type)
            db.session.commit()
            return product_type.id

    def _make_draft(self, product_type_id, offer_id):
        with self.app.app_context():
            draft = MarketplaceProductDraft(
                seller_id=1,
                marketplace_id=self.marketplace_id,
                account_id=1,
                imported_product_id=1,
                product_type_id=product_type_id,
                offer_id=offer_id,
                source_fact_hash="fingerprint",
            )
            db.session.add(draft)
            db.session.commit()

    # ── Авторизация ────────────────────────────────────────────────

    def test_non_admin_is_denied_on_index(self):
        user_patch, login_patch = self._auth(admin=False)
        with user_patch, login_patch:
            response = self.client.get("/admin/ozon/compliance/")
        self.assertEqual(response.status_code, 403)

    def test_non_admin_is_denied_on_decision_post(self):
        user_patch, login_patch = self._auth(admin=False)
        with user_patch, login_patch, patch(
            "routes.admin_ozon_compliance.save_decision"
        ) as save:
            response = self.client.post(
                "/admin/ozon/compliance/decision", data={},
            )
        self.assertEqual(response.status_code, 403)
        save.assert_not_called()

    def test_non_admin_is_denied_on_registry_create(self):
        user_patch, login_patch = self._auth(admin=False)
        with user_patch, login_patch, patch(
            "routes.admin_ozon_compliance.create_registry_version"
        ) as create:
            response = self.client.post(
                "/admin/ozon/compliance/registry", data={},
            )
        self.assertEqual(response.status_code, 403)
        create.assert_not_called()

    def test_non_admin_is_denied_on_registry_activate(self):
        user_patch, login_patch = self._auth(admin=False)
        with user_patch, login_patch, patch(
            "routes.admin_ozon_compliance.activate_registry_version"
        ) as activate:
            response = self.client.post(
                "/admin/ozon/compliance/registry/activate", data={},
            )
        self.assertEqual(response.status_code, 403)
        activate.assert_not_called()

    def test_non_admin_is_denied_on_registry_preview(self):
        user_patch, login_patch = self._auth(admin=False)
        with user_patch, login_patch, patch(
            "routes.admin_ozon_compliance.preview_registry_switch"
        ) as preview:
            response = self.client.get(
                "/admin/ozon/compliance/registry/1/preview",
            )
        self.assertEqual(response.status_code, 403)
        preview.assert_not_called()

    # ── index() ────────────────────────────────────────────────────

    def test_index_passes_type_rows_and_registry_versions_to_template(self):
        product_type_id = self._make_product_type()
        self._make_draft(product_type_id, "offer-1")
        with self.app.app_context():
            version = OzonMarkingRegistryVersion(
                label="v1", is_complete=True,
                declared_by_user_id=self.user_id, status="active",
            )
            db.session.add(version)
            db.session.commit()

        user_patch, login_patch = self._auth()
        with user_patch, login_patch, patch(
            "routes.admin_ozon_compliance.render_template",
            return_value="rendered",
        ) as render:
            response = self.client.get("/admin/ozon/compliance/")

        self.assertEqual(response.status_code, 200)
        kwargs = render.call_args.kwargs
        self.assertEqual(len(kwargs["type_rows"]), 1)
        self.assertEqual(
            kwargs["type_rows"][0]["product_type_id"], product_type_id,
        )
        self.assertEqual(
            [v.label for v in kwargs["registry_versions"]], ["v1"],
        )

    # ── save() decision ────────────────────────────────────────────

    def test_decision_save_uses_authenticated_user_id_not_form(self):
        product_type_id = self._make_product_type()
        user_patch, login_patch = self._auth(user_id=77)
        with user_patch, login_patch, patch(
            "routes.admin_ozon_compliance.save_decision"
        ) as save:
            response = self.client.post("/admin/ozon/compliance/decision", data={
                "product_type_id": str(product_type_id),
                "tnved_code": "3307900008",
                "rationale": "test",
                "user_id": "999",  # обязано быть проигнорировано роутом
            })
        self.assertEqual(response.status_code, 302)
        save.assert_called_once_with(
            product_type_id=str(product_type_id),
            tnved_code="3307900008",
            rationale="test",
            user_id=77,
        )

    def test_decision_save_error_is_flashed_not_raised(self):
        from services.ozon_compliance_admin import OzonComplianceAdminError

        user_patch, login_patch = self._auth()
        with user_patch, login_patch, patch(
            "routes.admin_ozon_compliance.save_decision",
            side_effect=OzonComplianceAdminError("человекочитаемая ошибка"),
        ):
            response = self.client.post(
                "/admin/ozon/compliance/decision", data={},
            )
        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as session:
            flashes = session.get("_flashes", [])
        messages = [message for _category, message in flashes]
        self.assertIn("человекочитаемая ошибка", messages)

    # ── create_registry() ──────────────────────────────────────────

    def test_registry_create_uses_authenticated_user_id_and_checkbox_bool(self):
        user_patch, login_patch = self._auth(user_id=42)
        with user_patch, login_patch, patch(
            "routes.admin_ozon_compliance.create_registry_version"
        ) as create:
            response = self.client.post("/admin/ozon/compliance/registry", data={
                "label": "v2",
                "is_complete": "on",
                "rules_text": "6402",
            })
        self.assertEqual(response.status_code, 302)
        create.assert_called_once_with(
            label="v2", is_complete=True, rules_text="6402", user_id=42,
        )

    def test_registry_create_without_checkbox_is_false(self):
        user_patch, login_patch = self._auth()
        with user_patch, login_patch, patch(
            "routes.admin_ozon_compliance.create_registry_version"
        ) as create:
            self.client.post("/admin/ozon/compliance/registry", data={
                "label": "v2", "rules_text": "6402",
            })
        self.assertFalse(create.call_args.kwargs["is_complete"])

    def test_registry_create_error_is_flashed_not_raised(self):
        from services.ozon_compliance_admin import OzonComplianceAdminError

        user_patch, login_patch = self._auth()
        with user_patch, login_patch, patch(
            "routes.admin_ozon_compliance.create_registry_version",
            side_effect=OzonComplianceAdminError("Строка 2: код должен ..."),
        ):
            response = self.client.post(
                "/admin/ozon/compliance/registry",
                data={"label": "v2", "rules_text": ";"},
            )
        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as session:
            flashes = session.get("_flashes", [])
        messages = [message for _category, message in flashes]
        self.assertTrue(any("Строка 2" in message for message in messages))

    # ── activate_registry() ────────────────────────────────────────

    def test_registry_activate_uses_authenticated_user_id(self):
        user_patch, login_patch = self._auth(user_id=13)
        with user_patch, login_patch, patch(
            "routes.admin_ozon_compliance.activate_registry_version"
        ) as activate:
            response = self.client.post(
                "/admin/ozon/compliance/registry/activate",
                data={"version_id": "5"},
            )
        self.assertEqual(response.status_code, 302)
        activate.assert_called_once_with(version_id="5", user_id=13)

    # ── registry_preview() ─────────────────────────────────────────

    def test_registry_preview_returns_service_result_as_json(self):
        user_patch, login_patch = self._auth()
        with user_patch, login_patch, patch(
            "routes.admin_ozon_compliance.preview_registry_switch",
            return_value={
                "to_true": 1, "to_false": 0, "to_unresolved": 0, "unchanged": 2,
            },
        ) as preview:
            response = self.client.get(
                "/admin/ozon/compliance/registry/7/preview",
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["to_true"], 1)
        preview.assert_called_once_with(7)

    def test_registry_preview_error_returns_400(self):
        from services.ozon_compliance_admin import OzonComplianceAdminError

        user_patch, login_patch = self._auth()
        with user_patch, login_patch, patch(
            "routes.admin_ozon_compliance.preview_registry_switch",
            side_effect=OzonComplianceAdminError("версия не найдена"),
        ):
            response = self.client.get(
                "/admin/ozon/compliance/registry/999/preview",
            )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "версия не найдена")


if __name__ == "__main__":
    unittest.main()
