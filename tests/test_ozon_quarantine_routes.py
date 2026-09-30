"""Real Flask/CSRF boundary for reviewed local Ozon quarantine decisions."""
from contextlib import ExitStack
from unittest.mock import patch
import unittest

from flask_wtf.csrf import generate_csrf

from models import db, Seller, MarketplaceWriteQuarantine as Hold, MarketplaceWriteQuarantineEvent as Event
from routes.marketplace_operations import register_marketplace_operation_routes
from tests import test_marketplace_accounts as fixtures
from tests import test_ozon_write_quarantine as service_fixtures


class QuarantineRoutesTest(unittest.TestCase):
    setUp = fixtures.MarketplaceAccountsTest.setUp
    tearDown = fixtures.MarketplaceAccountsTest.tearDown
    _create_seller = staticmethod(fixtures.MarketplaceAccountsTest._create_seller)
    _save = fixtures.MarketplaceAccountsTest._save
    operation = service_fixtures.WriteQuarantineTest.operation

    def _ready(self):
        self.app.config['WTF_CSRF_ENABLED'] = True
        register_marketplace_operation_routes(self.app)
        self.app.add_url_rule('/test-csrf', view_func=lambda: generate_csrf())
        with self.app.app_context():
            op = self.operation()
            return op.id, op.account_id

    def _client_as(self, seller_id):
        user_id = None
        with self.app.app_context():
            user_id = db.session.get(Seller, seller_id).user_id
        user = fixtures.SimpleNamespace(id=user_id, seller=fixtures.SimpleNamespace(id=seller_id),
                                        is_authenticated=True, is_active=True, is_admin=False)
        stack = ExitStack()
        stack.enter_context(patch('routes.marketplace_operations.current_user', user))
        stack.enter_context(patch('flask_login.utils._get_user', return_value=user))
        return stack

    def _token(self):
        return self.client.get('/test-csrf').data.decode()

    def _post(self, path, body, token=None):
        return self.client.post(path, json=body, headers={'X-CSRFToken': token or self._token()})

    def test_product_preview_place_lost_response_and_original_outcome(self):
        oid, _ = self._ready()
        with self._client_as(self.seller1_id):
            path = f'/marketplaces/operations/api/{oid}/review'
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            viewed = response.json['review']
            self.assertEqual(viewed['scope']['kind'], 'product')
            self.assertEqual(viewed['scope']['offer_id'], 'EXACT-01')
            self.assertTrue(viewed['can_place'])
            self.assertNotIn('synthetic-ozon-key', response.get_data(as_text=True))
            body = {'expected_version': viewed['operation_version'], 'scope_token': viewed['scope_token'],
                    'reason': 'Проверка результата требует остановки.', 'confirm_scope': True}
            write = self._post(f'/marketplaces/operations/api/{oid}/quarantine', body)
            self.assertEqual(write.status_code, 200, write.get_data(as_text=True))
            # A client that lost the POST response must discover the decision with GET.
            readback = self.client.get(path).json['review']
            self.assertEqual(readback['outcome'], 'uncertain')
            self.assertEqual(readback['hold']['status'], 'active')
            self.assertEqual(readback['events'][0]['action'], 'placed')
            duplicate = self._post(f'/marketplaces/operations/api/{oid}/quarantine', body)
            self.assertEqual(duplicate.status_code, 409)
            with self.app.app_context():
                self.assertEqual(Hold.query.count(), 1)
                self.assertEqual(Event.query.count(), 1)

    def test_csrf_owner_json_types_and_unknown_fields(self):
        oid, _ = self._ready()
        route = f'/marketplaces/operations/api/{oid}/quarantine'
        with self._client_as(self.seller2_id):
            self.assertEqual(self.client.get(f'/marketplaces/operations/api/{oid}/review').status_code, 404)
        with self._client_as(self.seller1_id):
            viewed = self.client.get(f'/marketplaces/operations/api/{oid}/review').json['review']
            body = {'expected_version': viewed['operation_version'], 'scope_token': viewed['scope_token'],
                    'reason': 'Останавливаем до проверки Ozon.', 'confirm_scope': True}
            self.assertEqual(self.client.post(route, json=body).status_code, 400)
            for extra in ({'expected_version': True}, {'expected_version': '1'}, {'confirm_scope': 1},
                          {'scope_kind': 'product'}, {'origin_type': 'media_operation'}):
                with self.subTest(extra=extra):
                    invalid = self._post(route, dict(body, **extra))
                    self.assertEqual(invalid.status_code, 400)
            self.assertEqual(self._post(route, [body]).status_code, 400)
            with self.app.app_context():
                self.assertEqual(Hold.query.count(), 0)

    def test_note_version_release_gate_and_bounded_journal(self):
        oid, _ = self._ready()
        api = f'/marketplaces/operations/api/{oid}/review'
        with self._client_as(self.seller1_id):
            viewed = self.client.get(api).json['review']
            placed = self._post(f'/marketplaces/operations/api/{oid}/quarantine',
                {'expected_version': viewed['operation_version'], 'scope_token': viewed['scope_token'],
                 'reason': 'Неизвестный результат требует остановки.', 'confirm_scope': True})
            self.assertEqual(placed.status_code, 200)
            row = placed.json['review']
            path = f'/marketplaces/operations/api/{oid}/quarantine/decision'
            values = {'expected_version': row['hold']['version'],
                      'expected_operation_version': row['operation_version'],
                      'action': 'note_added', 'reason': 'Проверили состояние без новой отправки.',
                      'confirm_release': False}
            note = self._post(path, values)
            self.assertEqual(note.status_code, 200, note.get_data(as_text=True))
            self.assertEqual(self._post(path, values).status_code, 409)
            current = note.json['review']
            denied = self._post(path, dict(values, expected_version=current['hold']['version'],
                                           action='released', confirm_release=True))
            self.assertEqual(denied.status_code, 409)
            self.assertEqual(self.client.get(api+'?before_id=bad').status_code, 400)
            self.assertEqual(self.client.get(api+'?before_id='+'9'*5000).status_code, 400)
            self.assertEqual(self.client.get(api+'?before_id=1&before_id=2').status_code, 400)
            self.assertEqual(self.client.get(api+'?future=1').status_code, 400)
            with self.app.app_context():
                self.assertEqual(Event.query.count(), 2)
                self.assertEqual(Hold.query.one().status, 'active')

    def test_html_review_is_scoped_and_links_exact_operation(self):
        oid, account_id = self._ready()
        with self._client_as(self.seller1_id):
            with patch('routes.marketplace_operations.render_template', return_value='rendered') as render:
                response = self.client.get(f'/marketplaces/operations/{oid}/review')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(render.call_args.args[0], 'ozon_operation_review.html')
            self.assertEqual(render.call_args.kwargs['account_id'], account_id)
        with self._client_as(self.seller2_id):
            with patch('routes.marketplace_operations.render_template', return_value='not found'):
                self.assertEqual(self.client.get(f'/marketplaces/operations/{oid}/review').status_code, 404)

    def test_authenticated_user_without_seller_gets_403(self):
        oid, _ = self._ready()
        user = fixtures.SimpleNamespace(id=999, seller=None,
            is_authenticated=True, is_active=True, is_admin=False)
        with patch('routes.marketplace_operations.current_user', user), \
             patch('flask_login.utils._get_user', return_value=user):
            response = self.client.get(f'/marketplaces/operations/api/{oid}/review')
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json['code'], 'write_quarantine_forbidden')
