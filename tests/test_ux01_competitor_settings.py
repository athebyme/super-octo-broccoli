"""Regression coverage for the explicit per-cycle competitor limit setting."""

import json
import os
from pathlib import Path
import re
import subprocess
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault('SKIP_SCHEDULER', '1')

from flask import Flask
from flask_login import LoginManager

from models import CompetitorMonitorSettings, Seller, User, db
from routes.competitors import register_competitor_routes


ROOT = Path(__file__).resolve().parents[1]


class CompetitorSettingsTestBase(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__, template_folder=str(ROOT / 'templates'))
        self.app.config.update(
            TESTING=True,
            SECRET_KEY='ux01-synthetic-settings',
            SQLALCHEMY_DATABASE_URI='sqlite://',
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
        )
        self.login_manager = LoginManager()
        self.login_manager.init_app(self.app)
        db.init_app(self.app)
        register_competitor_routes(self.app)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.seller = self._make_seller('ux-shop-one', 'ux-one@test.local')
        self.other_seller = self._make_seller('ux-shop-two', 'ux-two@test.local')
        self.client = self.app.test_client()
        self.user = MagicMock()
        self.user.is_authenticated = True
        self.user.seller = self.seller

    def _make_seller(self, username, email):
        user = User(username=username, email=email, is_active=True)
        user.set_password('synthetic-password')
        seller = Seller(user=user, company_name=username)
        db.session.add(seller)
        db.session.commit()
        return seller

    def _settings(self, seller, **overrides):
        values = {
            'seller_id': seller.id,
            'max_products': 100000,
            'sync_interval_minutes': 60,
            'price_change_alert_percent': 5.0,
            'discount_alert_pp': 5.0,
        }
        values.update(overrides)
        row = CompetitorMonitorSettings(**values)
        db.session.add(row)
        db.session.commit()
        return row

    def _as_user(self, user=None):
        return patch('flask_login.utils._get_user', return_value=user or self.user)

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()


class CompetitorSettingsRouteContractTests(CompetitorSettingsTestBase):
    def test_unrelated_save_preserves_legacy_setting_and_get_marks_it_as_saved(self):
        row = self._settings(self.seller, max_products=100000)
        with self._as_user():
            response = self.client.put('/api/competitors/settings', json={
                'sync_interval_minutes': 120,
                'discount_alert_pp': 8,
            })
            fetched = self.client.get('/api/competitors/settings')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(fetched.status_code, 200)
        self.assertEqual(fetched.get_json()['max_products'], 100000)
        self.assertNotIn('effective_max_products', fetched.get_json())
        db.session.refresh(row)
        self.assertEqual(row.max_products, 100000)
        self.assertEqual(row.sync_interval_minutes, 120)
        self.assertEqual(row.discount_alert_pp, 8)

    def test_explicit_valid_limit_changes_accept_only_integer_boundaries(self):
        row = self._settings(self.seller, max_products=100000)
        for value in (1, 1000):
            with self.subTest(value=value), self._as_user():
                response = self.client.put(
                    '/api/competitors/settings', json={'max_products': value})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json()['max_products'], value)
            db.session.refresh(row)
            self.assertEqual(row.max_products, value)

    def test_invalid_explicit_limit_returns_400_without_any_setting_commit(self):
        row = self._settings(
            self.seller, max_products=100000, sync_interval_minutes=60)
        invalid_values = (True, 1000.5, '1000', 0, 1001)
        for value in invalid_values:
            with self.subTest(value=value), self._as_user():
                response = self.client.put('/api/competitors/settings', json={
                    'sync_interval_minutes': 240,
                    'max_products': value,
                })
            self.assertEqual(response.status_code, 400)
            self.assertIn('целым числом от 1 до 1000', response.get_json()['error'])
            db.session.refresh(row)
            self.assertEqual(row.max_products, 100000)
            self.assertEqual(row.sync_interval_minutes, 60)

    def test_invalid_limit_does_not_create_default_row(self):
        with self._as_user():
            response = self.client.put('/api/competitors/settings', json={
                'max_products': 1001,
            })
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            CompetitorMonitorSettings.query.filter_by(
                seller_id=self.seller.id).count(), 0)

    def test_unauthenticated_request_cannot_read_or_change_either_seller(self):
        own = self._settings(self.seller, max_products=100000)
        foreign = self._settings(self.other_seller, max_products=700)
        anonymous = MagicMock()
        anonymous.is_authenticated = False
        anonymous.seller = None
        with self._as_user(anonymous):
            read = self.client.get('/api/competitors/settings')
            write = self.client.put('/api/competitors/settings', json={
                'seller_id': self.other_seller.id,
                'max_products': 1,
            })
        self.assertEqual(read.status_code, 401)
        self.assertEqual(write.status_code, 401)
        db.session.refresh(own)
        db.session.refresh(foreign)
        self.assertEqual(own.max_products, 100000)
        self.assertEqual(foreign.max_products, 700)


class CompetitorSettingsBrowserStateTest(unittest.TestCase):
    def test_page_payload_omits_legacy_limit_until_explicit_edit(self):
        template = (ROOT / 'templates/competitors_settings.html').read_text(encoding='utf-8')
        marker = 'function competitorSettings()'
        start = template.index(marker)
        end = template.index('</script>', start)
        component = template[start:end]
        component = re.sub(
            r'\{\{\s*settings\.to_dict\(\)\|tojson\s*\}\}',
            json.dumps({
                'id': 17, 'seller_id': 1, 'is_enabled': False,
                'price_change_alert_percent': 5.0, 'max_products': 100000,
                'sync_interval_minutes': 60, 'next_sync_due_at': None,
                'discount_alert_pp': 5.0,
                'proxy': {'is_set': False, 'masked': None, 'has_credentials': False},
                'last_sync_at': None, 'last_sync_status': 'never',
                'last_sync_error': None, 'last_full_cycle_duration': None,
                'total_products_monitored': 0, 'total_cycles_completed': 0,
            }),
            component,
        )
        self.assertNotIn('{{ settings.to_dict()|tojson }}', component)

        program = r'''
let saved = {
  id: 17, seller_id: 1, is_enabled: false, price_change_alert_percent: 5,
  max_products: 100000, sync_interval_minutes: 60, next_sync_due_at: null,
  discount_alert_pp: 5, proxy: {is_set: false, masked: null, has_credentials: false},
  last_sync_at: null, last_sync_status: 'never', last_sync_error: null,
  last_full_cycle_duration: null, total_products_monitored: 0, total_cycles_completed: 0
};
const sent = [];
global.document = {querySelector: () => ({content: 'synthetic-csrf'})};
global.fetch = async (_url, options) => {
  const payload = JSON.parse(options.body);
  sent.push(payload);
  saved = {...saved, ...payload};
  return {ok: true, json: async () => ({...saved})};
};
'''
        program += component + r'''
(async () => {
  const state = competitorSettings();
  state.$store = {toasts: {success() {}, error() {}}};
  state.init();
  await state.save();
  const unrelatedHasLimit = Object.prototype.hasOwnProperty.call(sent[0], 'max_products');
  state.maxProductsDraft = 1000;
  state.maxProductsEdited = true;
  await state.save();
  const explicitLimit = sent[1].max_products;
  const beforeInvalid = sent.length;
  state.maxProductsDraft = 1001;
  state.maxProductsEdited = true;
  await state.save();
  console.log(JSON.stringify({
    unrelatedHasLimit, explicitLimit, invalidSent: sent.length !== beforeInvalid,
    clientError: state.maxProductsError, saved: state.savedMaxProducts,
    effective: state.effectiveMaxProducts()
  }));
})().catch(error => { console.error(error); process.exitCode = 1; });
'''
        result = subprocess.run(
            ['node', '-'], input=program, text=True, capture_output=True, check=True)
        state = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertFalse(state['unrelatedHasLimit'])
        self.assertEqual(state['explicitLimit'], 1000)
        self.assertFalse(state['invalidSent'])
        self.assertIn('целое число от 1 до 1000', state['clientError'])
        self.assertEqual(state['saved'], 1000)
        self.assertEqual(state['effective'], 1000)

    def test_ui_distinguishes_saved_value_from_effective_per_cycle_cap(self):
        template = (ROOT / 'templates/competitors_settings.html').read_text(encoding='utf-8')
        self.assertIn('лимит обработки за цикл', template.lower())
        self.assertIn('Сохранённое значение настройки:', template)
        self.assertIn('за один цикл', template)
        self.assertIn('серверный предел — 1000', template)
        self.assertNotIn('До 1000 товаров суммарно по всем группам', template)
