"""Synthetic settings/quality checks for the UX-01 seller shell changes."""

from __future__ import annotations

import base64
from datetime import datetime
from html import unescape
import os
from pathlib import Path
import re
import socket
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch


class SellerWorkspaceSettingsQualityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._environment_keys = (
            "DATABASE_URL", "SKIP_SCHEDULER", "DISABLE_SECURE_COOKIE", "SECRET_KEY",
        )
        cls._previous_environment = {
            key: os.environ.get(key) for key in cls._environment_keys
        }
        cls._temporary = tempfile.TemporaryDirectory(prefix="ux01-settings-quality-")
        os.environ.update({
            "DATABASE_URL": "sqlite:///" + str(Path(cls._temporary.name) / "test.sqlite"),
            "SKIP_SCHEDULER": "1",
            "DISABLE_SECURE_COOKIE": "1",
            "SECRET_KEY": "synthetic-ux01-settings-quality",
        })

        import sqlalchemy as sa
        from sqlalchemy.pool import StaticPool
        import seller_platform
        from models import db

        cls.app = seller_platform.app
        cls._config_keys = (
            "SQLALCHEMY_DATABASE_URI", "SQLALCHEMY_TRACK_MODIFICATIONS",
            "SQLALCHEMY_ENGINE_OPTIONS", "WTF_CSRF_ENABLED", "TESTING",
            "MARKETPLACE_OZON_ENABLED", "MARKETPLACE_OZON_PUBLICATION_ENABLED",
            "MARKETPLACE_OZON_AUTO_PUBLISH_ENABLED",
        )
        cls._previous_config = {
            key: cls.app.config.get(key) for key in cls._config_keys
        }
        cls.app.config.update(
            SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            SQLALCHEMY_ENGINE_OPTIONS={},
            WTF_CSRF_ENABLED=False,
            TESTING=True,
            MARKETPLACE_OZON_ENABLED=True,
            MARKETPLACE_OZON_PUBLICATION_ENABLED=True,
            MARKETPLACE_OZON_AUTO_PUBLISH_ENABLED=False,
        )
        cls._engine = sa.create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        cls._previous_engine_map = db._app_engines.get(cls.app)
        db._app_engines[cls.app] = {None: cls._engine}
        cls.db = db

        import requests

        def forbid_network(*_args, **_kwargs):
            raise AssertionError("Provider network is forbidden in UX-01 tests")

        cls._network_patches = [
            patch.object(requests.sessions.Session, "request", new=forbid_network),
            patch.object(socket, "create_connection", new=forbid_network),
        ]
        for network_patch in cls._network_patches:
            network_patch.start()

    @classmethod
    def tearDownClass(cls):
        with cls.app.app_context():
            cls.db.session.remove()
        cls._engine.dispose()
        if cls._previous_engine_map is None:
            cls.db._app_engines.pop(cls.app, None)
        else:
            cls.db._app_engines[cls.app] = cls._previous_engine_map
        for key, value in cls._previous_config.items():
            if value is None:
                cls.app.config.pop(key, None)
            else:
                cls.app.config[key] = value
        for network_patch in reversed(cls._network_patches):
            network_patch.stop()
        for key, value in cls._previous_environment.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        cls._temporary.cleanup()

    def setUp(self):
        from models import Marketplace, Seller, SellerMarketplaceAccount, User

        self.context = self.app.app_context()
        self.context.push()
        self.db.drop_all()
        self.db.create_all()
        self.client = self.app.test_client()

        self.user_row = User(
            username="ux01-synthetic",
            email="ux01-synthetic@example.test",
            is_active=True,
        )
        self.user_row.set_password("synthetic-only")
        self.seller = Seller(user=self.user_row, company_name="Synthetic seller")
        self.db.session.add(self.seller)
        self.db.session.flush()
        self.ozon = Marketplace(name="Ozon", code="ozon", is_active=True)
        self.db.session.add(self.ozon)
        self.db.session.flush()
        self.account = SellerMarketplaceAccount(
            seller_id=self.seller.id,
            marketplace_id=self.ozon.id,
            external_account_id="synthetic-account-41",
            label="Synthetic Ozon account",
            is_active=True,
            is_default=True,
            connection_status="error",
            connection_checked_at=datetime(2026, 9, 30, 12, 0),
            credential_expires_at=datetime(2026, 9, 1, 12, 0),
            last_error_code="synthetic_read_failed",
            last_error_message="Synthetic connection check failed",
        )
        self.db.session.add(self.account)
        self.db.session.commit()
        self.user = type(
            "SyntheticUser",
            (),
            {
                "id": self.user_row.id,
                "username": self.user_row.username,
                "seller": self.seller,
                "is_authenticated": True,
                "is_active": True,
                "is_admin": False,
            },
        )()

    def tearDown(self):
        self.db.session.remove()
        self.db.drop_all()
        self.context.pop()

    def _auth(self):
        return patch("flask_login.utils._get_user", return_value=self.user)

    @staticmethod
    def _expired_jwt():
        payload = base64.urlsafe_b64encode(b'{"exp":1}').decode().rstrip("=")
        return "synthetic-header." + payload + ".synthetic-signature"

    def test_auto_publish_shows_wb_expiry_hint_without_exposing_credential(self):
        self.seller.wb_api_key = self._expired_jwt()
        self.db.session.commit()

        with self._auth():
            response = self.client.get("/auto-publish")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("срок JWT истёк", body)
        self.assertIn("распознан истёкший срок", body)
        self.assertIn("не подтверждение личности, доступности методов", body)
        self.assertNotIn(self._expired_jwt(), body)

    def test_ozon_count_failure_is_unknown_and_stays_account_scoped(self):
        from services.marketplace_auto_publish import (
            MarketplaceAutoPublishError,
            OzonAutoPublishService,
        )

        with patch.object(
            OzonAutoPublishService,
            "pending_candidate_count",
            side_effect=MarketplaceAutoPublishError(
                "Synthetic candidate count unavailable"
            ),
        ), self._auth():
            response = self.client.get(
                f"/auto-publish?marketplace=ozon&account_id={self.account.id}"
            )

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Synthetic Ozon account · состояние подключения", body)
        self.assertIn("Количество неизвестно", body)
        self.assertIn("pendingCount: null", body)
        self.assertNotIn("pendingCount: 0", body)
        self.assertIn("Synthetic connection check failed", body)
        self.assertIn("01.09.2026 12:00 UTC", body)
        self.assertIn("Срок ключа истёк", body)
        self.assertIn("Автопубликация Ozon отключена настройкой платформы", body)
        self.assertIn("разрешение конкретного метода", body)
        self.assertIn("Synthetic candidate count unavailable", body)

    def _settings_auto_publish_target(self, response):
        href = self._settings_nav_href(response, "Автопубликация")
        parsed = urlsplit(href)
        self.assertEqual(parsed.path, "/auto-publish")
        return parse_qs(parsed.query)

    def _settings_nav_href(self, response, label):
        body = response.get_data(as_text=True)
        nav_start = body.find('class="seller-workspace-settings-nav"')
        self.assertGreaterEqual(nav_start, 0, "settings navigation must render")
        nav_end = body.find("</nav>", nav_start)
        self.assertGreater(nav_end, nav_start)
        match = re.search(
            r'<a href="([^"]+)"[^>]*>' + re.escape(label) + r'</a>',
            body[nav_start:nav_end],
        )
        self.assertIsNotNone(match, f"settings navigation must retain {label}")
        return unescape(match.group(1))

    def test_settings_navigation_preserves_explicit_ozon_account_context_only(self):
        from flask import url_for
        from models import Marketplace, Seller, SellerMarketplaceAccount, User

        # Readiness renders both channels and requires the WB reference row.
        # Keep this explicit test seed local instead of weakening its route.
        self.db.session.add(Marketplace(
            name="Synthetic Wildberries",
            code="wb",
            adapter_code="wb",
            is_active=True,
        ))
        inactive = SellerMarketplaceAccount(
            seller_id=self.seller.id,
            marketplace_id=self.ozon.id,
            external_account_id="synthetic-inactive-account",
            label="Inactive synthetic Ozon account",
            is_active=False,
            connection_status="error",
        )
        foreign_user = User(
            username="ux01-foreign-account-owner",
            email="ux01-foreign-account-owner@example.test",
            is_active=True,
        )
        foreign_user.set_password("synthetic-only")
        foreign_seller = Seller(user=foreign_user, company_name="Foreign synthetic seller")
        self.db.session.add_all([inactive, foreign_seller])
        self.db.session.flush()
        foreign_account = SellerMarketplaceAccount(
            seller_id=foreign_seller.id,
            marketplace_id=self.ozon.id,
            external_account_id="synthetic-foreign-account",
            label="Foreign synthetic account",
            is_active=True,
            connection_status="connected",
        )
        self.db.session.add(foreign_account)
        self.db.session.commit()

        with self.app.test_request_context():
            health_url = url_for(
                "ozon_account_health.page", account_id=inactive.id
            )
            accounts_url = url_for(
                "marketplace_accounts.index", account_id=inactive.id
            )
            readiness_url = url_for(
                "marketplace_readiness.index", account_id=inactive.id
            )
            foreign_url = url_for(
                "marketplace_accounts.index", account_id=foreign_account.id
            )
            stale_url = url_for(
                "marketplace_accounts.index", account_id=999999
            )

        with self._auth():
            health = self.client.get(health_url)
            accounts = self.client.get(accounts_url)
            explicitly_ozon = self.client.get(
                f"/api-settings?marketplace=ozon&account_id={self.account.id}"
            )
            generic_readiness = self.client.get(readiness_url)
            foreign_request = self.client.get(foreign_url)
            stale_request = self.client.get(stale_url)

        for response in (
            health, accounts, explicitly_ozon, generic_readiness,
            foreign_request, stale_request,
        ):
            self.assertEqual(response.status_code, 200)

        expected_id = str(self.account.id)
        inactive_id = str(inactive.id)
        self.assertEqual(
            self._settings_auto_publish_target(health),
            {"marketplace": ["ozon"], "account_id": [inactive_id]},
        )
        self.assertEqual(
            self._settings_auto_publish_target(accounts),
            {"marketplace": ["ozon"], "account_id": [inactive_id]},
        )
        health_href = urlsplit(unescape(self._settings_nav_href(
            accounts, "Состояние кабинета Ozon"
        )))
        self.assertEqual(
            parse_qs(health_href.query), {"account_id": [inactive_id]}
        )
        self.assertEqual(
            self._settings_auto_publish_target(explicitly_ozon),
            {"marketplace": ["ozon"], "account_id": [expected_id]},
        )
        # Readiness is shared across channels: a sticky Ozon account id alone
        # must not silently turn its WB auto-publish destination into Ozon.
        self.assertEqual(
            self._settings_auto_publish_target(generic_readiness),
            {"marketplace": ["wb"]},
        )
        # Foreign/stale IDs never enter a destination URL; the page resolves
        # to this seller's validated active default account instead.
        for response in (foreign_request, stale_request):
            self.assertEqual(
                self._settings_auto_publish_target(response),
                {"marketplace": ["ozon"], "account_id": [expected_id]},
            )

    def test_workspace_style_keeps_readable_labels_focus_and_mobile_targets(self):
        from pathlib import Path

        css = Path(__file__).resolve().parents[1].joinpath(
            "static/seller-workspace.css"
        ).read_text(encoding="utf-8")
        base = Path(__file__).resolve().parents[1].joinpath(
            "templates/base.html"
        ).read_text(encoding="utf-8")

        self.assertIn("--bg-sidebar-hover: #1a1a1a", base)
        self.assertEqual(base.count("--text-sidebar-muted: #a3a3a3;"), 2)
        self.assertIn("outline: 2px solid var(--text-sidebar)", css)
        self.assertIn("font-size: 12px", css)
        self.assertNotIn("text-transform: uppercase", css)
        self.assertNotRegex(css, r"font-size:\s*(?:10|11)px")
        self.assertRegex(css, r"@media \(max-width: 639px\)[\s\S]*?min-height: 44px")
        self.assertNotIn("background: #1a1a1a", css)

    def test_quality_classic_beta_filters_and_bulk_actions_survive(self):
        from pathlib import Path

        templates = Path(__file__).resolve().parents[1] / "templates"
        classic = (templates / "card_quality.html").read_text(encoding="utf-8")
        beta = (templates / "card_quality_beta.html").read_text(encoding="utf-8")
        for control_id in (
            "cq-search", "cq-brand-filter", "cq-category-filter",
            "cq-quality-filter", "cq-supplier-filter",
        ):
            self.assertIn(f'id="{control_id}"', classic)
        self.assertRegex(classic, r"card_quality_bulk_improve_page")
        self.assertRegex(classic, r"card_quality_standard_photos_bulk_page")
        self.assertIn('id="card-quality-app"', beta)
        self.assertIn("seller_workspace_quality_media.html", beta)


if __name__ == "__main__":
    unittest.main()
