# -*- coding: utf-8 -*-
"""mp_nav global + channel_bar: renders via non-context macro import."""

from types import SimpleNamespace
from unittest.mock import patch
import unittest

from flask import Flask, render_template_string
from flask_login import LoginManager

from models import (
    Marketplace,
    Seller,
    SellerMarketplaceAccount,
    User,
    db,
)
from services.marketplace_nav import register_marketplace_nav

# Импорт намеренно БЕЗ `with context`: mp_nav обязан быть Jinja-глобалом,
# иначе channel_bar молча не рендерится в шаблонах с обычным импортом.
TEMPLATE = (
    '{% from "macros/components.html" import channel_bar %}'
    "{{ channel_bar(active=active, wb_href='/wb-page', "
    "ozon_href='/ozon-page', account_id=account_id) }}"
)


class MarketplaceNavTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__, template_folder="../templates")
        self.app.config.update(
            TESTING=True,
            SECRET_KEY="marketplace-nav-test",
            SQLALCHEMY_DATABASE_URI="sqlite://",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            MARKETPLACE_OZON_ENABLED=True,
        )
        db.init_app(self.app)
        LoginManager(self.app)
        register_marketplace_nav(self.app)

        @self.app.route("/api-settings")
        def api_settings():
            return ""

        @self.app.route("/marketplaces/nav-echo")
        def nav_echo():
            return self.app.jinja_env.globals["mp_nav"]()

        with self.app.app_context():
            db.create_all()
            user = User(
                username="nav-user", email="nav@test.local", is_active=True
            )
            user.set_password("synthetic-password")
            seller = Seller(user=user, company_name="nav-user")
            ozon = Marketplace(
                name="Ozon", code="ozon", adapter_code="ozon", is_active=True
            )
            db.session.add_all([seller, ozon])
            db.session.flush()
            account = SellerMarketplaceAccount(
                seller_id=seller.id,
                marketplace_id=ozon.id,
                external_account_id="client-nav",
                label="Основной",
                is_active=True,
                is_default=True,
                connection_status="connected",
            )
            inactive = SellerMarketplaceAccount(
                seller_id=seller.id,
                marketplace_id=ozon.id,
                external_account_id="client-off",
                label="Выключенный",
                is_active=False,
                connection_status="disconnected",
            )
            db.session.add_all([account, inactive])
            db.session.commit()
            self.seller_id = seller.id
            self.account_id = account.id

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.drop_all()

    def _render(self, active="wb", account_id=None, seller_id=None):
        user = SimpleNamespace(
            is_authenticated=True,
            is_active=True,
            seller=SimpleNamespace(id=seller_id or self.seller_id),
        )
        with patch("flask_login.utils._get_user", return_value=user):
            with self.app.test_request_context("/"):
                return render_template_string(
                    TEMPLATE, active=active, account_id=account_id
                )

    def test_renders_via_plain_import_with_account_tabs(self):
        html = self._render(active="wb")
        self.assertIn("sh-channel-bar", html)
        self.assertIn("Wildberries", html)
        self.assertIn(f"/ozon-page?account_id={self.account_id}", html)
        self.assertNotIn("Выключенный", html)

    def test_active_ozon_tab_marked_current(self):
        html = self._render(active="ozon", account_id=self.account_id)
        self.assertIn("sh-channel-tab--active", html)
        self.assertIn('aria-current="page"', html)

    def test_flag_off_renders_nothing(self):
        self.app.config["MARKETPLACE_OZON_ENABLED"] = False
        html = self._render(active="wb")
        self.assertEqual(html.strip(), "")

    def test_anonymous_renders_nothing(self):
        anonymous = SimpleNamespace(is_authenticated=False, seller=None)
        with patch("flask_login.utils._get_user", return_value=anonymous):
            with self.app.test_request_context("/"):
                html = render_template_string(
                    TEMPLATE, active="wb", account_id=None
                )
        self.assertEqual(html.strip(), "")

    def test_last_account_sticks_in_session_and_is_validated(self):
        user = SimpleNamespace(
            is_authenticated=True,
            is_active=True,
            seller=SimpleNamespace(id=self.seller_id),
        )
        client = self.app.test_client()
        with patch("flask_login.utils._get_user", return_value=user):
            first = client.get(
                f"/marketplaces/nav-echo?account_id={self.account_id}"
            )
            self.assertEqual(
                first.get_json()["last_account_id"], self.account_id
            )
            # Без параметра — кабинет «липнет» из session
            second = client.get("/marketplaces/nav-echo")
            self.assertEqual(
                second.get_json()["last_account_id"], self.account_id
            )
            # Неизвестный/чужой id запоминается в session, но не всплывает
            third = client.get("/marketplaces/nav-echo?account_id=99999")
            self.assertIsNone(third.get_json()["last_account_id"])

    def test_no_accounts_offers_connect_ghost_tab(self):
        with self.app.app_context():
            SellerMarketplaceAccount.query.delete()
            db.session.commit()
        html = self._render(active="wb")
        self.assertIn("Подключить Ozon", html)
        self.assertIn("/api-settings", html)


if __name__ == "__main__":
    unittest.main()
