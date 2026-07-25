# -*- coding: utf-8 -*-
"""Истёкшая сессия отвечает по-разному браузеру и JSON-клиенту.

До появления обработчика fetch получал 200 с HTML страницы логина, JSON-парсинг
падал, и интерфейс показывал «данных нет» вместо «сессия истекла». При этом
обычная навигация и клиенты с «Accept: */*» обязаны и дальше получать редирект.
"""

import os
import unittest


class UnauthorizedResponseShapeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ['DISABLE_SECURE_COOKIE'] = '1'
        os.environ.setdefault('SKIP_SCHEDULER', '1')
        import seller_platform  # noqa
        cls.app = seller_platform.app
        cls.app.config['TESTING'] = True

    def setUp(self):
        self.client = self.app.test_client()

    def test_json_client_gets_401_code(self):
        response = self.client.get(
            '/marketplaces/listings/api',
            headers={'Accept': 'application/json'},
        )
        self.assertEqual(response.status_code, 401)
        payload = response.get_json()
        self.assertEqual(payload['code'], 'auth_required')
        self.assertFalse(payload['success'])

    def test_xhr_client_gets_401_code(self):
        response = self.client.get(
            '/marketplaces/listings/api',
            headers={'X-Requested-With': 'XMLHttpRequest'},
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()['code'], 'auth_required')

    def test_browser_navigation_still_redirects_to_login(self):
        response = self.client.get(
            '/marketplaces/listings/',
            headers={
                'Accept': (
                    'text/html,application/xhtml+xml,application/xml;q=0.9,'
                    '*/*;q=0.8'
                )
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login', response.headers['Location'])

    def test_wildcard_accept_client_still_redirects(self):
        response = self.client.get(
            '/marketplaces/listings/',
            headers={'Accept': '*/*'},
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login', response.headers['Location'])


if __name__ == '__main__':
    unittest.main()
