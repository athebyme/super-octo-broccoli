# -*- coding: utf-8 -*-
"""
Тесты bounded-поведения фото-прокси (routes/photos.py):

- auth-cookies поставщика кэшируются с TTL и не порождают логин-шторм;
- скачивание изображения укладывается в общий wall-clock дедлайн.

Инцидент 2026-07-20: логин-POST на каждый промах кэша + отсутствие общего
дедлайна забивали все gunicorn-слоты и платформа висела.
"""
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from routes import photos


def _supplier(**kwargs):
    defaults = dict(code='sexoptovik', auth_login='user', auth_password='pass')
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


class AuthCookieCacheTest(unittest.TestCase):
    def setUp(self):
        photos._auth_cookie_cache.clear()

    def tearDown(self):
        photos._auth_cookie_cache.clear()

    def _mock_session(self, status_code=302, cookies=None):
        session = mock.MagicMock()
        session.post.return_value = SimpleNamespace(status_code=status_code)
        session.cookies = cookies if cookies is not None else {'sid': 'abc'}
        return session

    def test_success_is_cached(self):
        session = self._mock_session()
        with mock.patch('requests.Session', return_value=session):
            first = photos._get_supplier_auth_cookies(_supplier())
            second = photos._get_supplier_auth_cookies(_supplier())
        self.assertEqual(first, {'sid': 'abc'})
        self.assertEqual(second, {'sid': 'abc'})
        self.assertEqual(session.post.call_count, 1)

    def test_failure_is_cached_without_retry_storm(self):
        session = self._mock_session(status_code=403, cookies={})
        with mock.patch('requests.Session', return_value=session):
            first = photos._get_supplier_auth_cookies(_supplier())
            second = photos._get_supplier_auth_cookies(_supplier())
        self.assertEqual(first, {})
        self.assertEqual(second, {})
        self.assertEqual(session.post.call_count, 1)

    def test_expired_entry_triggers_new_login(self):
        session = self._mock_session()
        with mock.patch('requests.Session', return_value=session):
            photos._get_supplier_auth_cookies(_supplier())
            # Протухание TTL: сдвигаем срок в прошлое вручную,
            # чтобы не патчить глобальный time.monotonic.
            cookies, _ = photos._auth_cookie_cache['sexoptovik']
            photos._auth_cookie_cache['sexoptovik'] = (
                cookies, time.monotonic() - 1)
            photos._get_supplier_auth_cookies(_supplier())
        self.assertEqual(session.post.call_count, 2)

    def test_no_credentials_no_login(self):
        with mock.patch('requests.Session') as session_cls:
            result = photos._get_supplier_auth_cookies(
                _supplier(auth_login=None))
        self.assertEqual(result, {})
        session_cls.assert_not_called()

    def test_none_supplier(self):
        self.assertEqual(photos._get_supplier_auth_cookies(None), {})


class DownloadDeadlineTest(unittest.TestCase):
    def _mock_response(self, chunks, content_type='image/jpeg'):
        resp = mock.MagicMock()
        resp.headers = {'Content-Type': content_type}
        resp.raise_for_status.return_value = None
        resp.iter_content.return_value = iter(chunks)
        return resp

    def test_expired_deadline_skips_request(self):
        with mock.patch('requests.get') as get:
            result = photos._download_image_with_deadline(
                'https://example.com/a.jpg', {}, {},
                deadline=time.monotonic() - 1)
        self.assertIsNone(result)
        get.assert_not_called()

    def test_normal_download_returns_content(self):
        resp = self._mock_response([b'a' * 2048])
        with mock.patch('requests.get', return_value=resp):
            result = photos._download_image_with_deadline(
                'https://example.com/a.jpg', {}, {},
                deadline=time.monotonic() + 10)
        self.assertEqual(result, b'a' * 2048)
        resp.close.assert_called_once()

    def test_slow_stream_hits_deadline(self):
        # Дедлайн больше предзапросного порога 0.5с, но меньше паузы
        # между чанками: второй чанк обязан упереться в дедлайн.
        def slow_chunks():
            yield b'a' * 100
            time.sleep(0.8)
            yield b'b' * 100

        resp = self._mock_response(slow_chunks())
        with mock.patch('requests.get', return_value=resp):
            result = photos._download_image_with_deadline(
                'https://example.com/a.jpg', {}, {},
                deadline=time.monotonic() + 0.6)
        self.assertIsNone(result)
        resp.close.assert_called_once()

    def test_oversized_response_rejected(self):
        resp = self._mock_response([b'a' * 1024] * 3)
        with mock.patch('requests.get', return_value=resp):
            result = photos._download_image_with_deadline(
                'https://example.com/a.jpg', {}, {},
                deadline=time.monotonic() + 10, max_bytes=2048)
        self.assertIsNone(result)

    def test_non_image_small_response_rejected(self):
        resp = self._mock_response([b'<html>err</html>'],
                                   content_type='text/html')
        with mock.patch('requests.get', return_value=resp):
            result = photos._download_image_with_deadline(
                'https://example.com/a.jpg', {}, {},
                deadline=time.monotonic() + 10)
        self.assertIsNone(result)


if __name__ == '__main__':
    unittest.main()
