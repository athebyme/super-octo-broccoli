# -*- coding: utf-8 -*-
"""
Тесты bounded-поведения доставки фото:

- auth-cookies поставщика кэшируются с TTL и не порождают логин-шторм;
- transport photo-cache укладывается в общий wall-clock дедлайн.

Инцидент 2026-07-20: логин-POST на каждый промах кэша + отсутствие общего
дедлайна забивали все gunicorn-слоты и платформа висела.
"""
import io
import time
import unittest
from email.message import Message
from types import SimpleNamespace
from urllib.parse import urlsplit
from unittest import mock

import requests
from PIL import Image
from requests.adapters import BaseAdapter
from requests.models import Response
from requests.structures import CaseInsensitiveDict

from routes import photos
import services.photo_cache as photo_cache
from services.photo_cache import PhotoCacheManager


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
        session = mock.MagicMock()
        with mock.patch(
            'services.url_security.validate_external_url', return_value=None,
        ):
            result = PhotoCacheManager._download_image_bytes(
                session, 'https://example.com/a.jpg', {}, {},
                deadline=time.monotonic() - 1,
            )
        self.assertIsNone(result)
        session.get.assert_not_called()

    def test_normal_download_returns_content(self):
        resp = self._mock_response([b'a' * 2048])
        session = mock.MagicMock()
        session.cookies = requests.cookies.RequestsCookieJar()
        session.get.return_value = resp
        with mock.patch(
            'services.url_security.validate_external_url', return_value=None,
        ):
            result = PhotoCacheManager._download_image_bytes(
                session, 'https://example.com/a.jpg', {}, {},
                deadline=time.monotonic() + 10,
            )
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
        session = mock.MagicMock()
        session.cookies = requests.cookies.RequestsCookieJar()
        session.get.return_value = resp
        with mock.patch(
            'services.url_security.validate_external_url', return_value=None,
        ):
            result = PhotoCacheManager._download_image_bytes(
                session, 'https://example.com/a.jpg', {}, {},
                deadline=time.monotonic() + 0.6,
            )
        self.assertIsNone(result)
        resp.close.assert_called_once()

    def test_oversized_response_rejected(self):
        resp = self._mock_response([b'a' * 1024] * 3)
        session = mock.MagicMock()
        session.cookies = requests.cookies.RequestsCookieJar()
        session.get.return_value = resp
        with (
            mock.patch(
                'services.url_security.validate_external_url',
                return_value=None,
            ),
            mock.patch('services.photo_cache.DOWNLOAD_MAX_BYTES', 2048),
        ):
            result = PhotoCacheManager._download_image_bytes(
                session, 'https://example.com/a.jpg', {}, {},
                deadline=time.monotonic() + 10,
            )
        self.assertIsNone(result)

    def test_non_image_small_response_rejected(self):
        resp = self._mock_response([b'<html>err</html>'],
                                   content_type='text/html')
        session = mock.MagicMock()
        session.cookies = requests.cookies.RequestsCookieJar()
        session.get.return_value = resp
        with mock.patch(
            'services.url_security.validate_external_url', return_value=None,
        ):
            result = PhotoCacheManager._download_image_bytes(
                session, 'https://example.com/a.jpg', {}, {},
                deadline=time.monotonic() + 10,
            )
        self.assertIsNone(result)

    def test_invalid_session_cookie_jar_fails_closed_for_untrusted_origin(self):
        session = mock.MagicMock()
        session.cookies = {'supplier_session': 'synthetic_only'}
        with mock.patch(
            'services.url_security.validate_external_url', return_value=None,
        ):
            result = PhotoCacheManager._download_image_bytes(
                session,
                'https://public.example.test/photo.jpg',
                {},
                {},
                deadline=time.monotonic() + 10,
            )
        self.assertIsNone(result)
        session.get.assert_not_called()
        session.send.assert_not_called()


class _SyntheticPhotoRaw(io.BytesIO):
    def __init__(self, body, headers):
        super().__init__(body)
        message = Message()
        for name, value in headers.items():
            message.add_header(name, value)
        self._original_response = SimpleNamespace(msg=message)


class _SyntheticPhotoAdapter(BaseAdapter):
    """In-memory HTTP adapter that exercises Requests preparation without I/O."""

    def __init__(self, router):
        self.router = router
        self.requests = []

    def send(self, request, **kwargs):
        parsed = urlsplit(request.url)
        spec = self.router(parsed.scheme, parsed.hostname, parsed.port, parsed.path)
        self.requests.append({
            'scheme': parsed.scheme,
            'host': parsed.hostname,
            'port': parsed.port,
            'path': parsed.path,
            'cookie': request.headers.get('Cookie'),
            'referer': request.headers.get('Referer'),
        })
        response = Response()
        response.status_code = spec['status']
        response.headers = CaseInsensitiveDict(spec.get('headers', {}))
        response.url = request.url
        response.request = request
        response.reason = spec.get('reason', 'Synthetic')
        response.raw = _SyntheticPhotoRaw(
            spec.get('body', b''), spec.get('headers', {}),
        )
        return response

    def close(self):
        return None


class SupplierCookieOriginTest(unittest.TestCase):
    COOKIE = 'synthetic_supplier_cookie_only'
    COOKIE_NAME = 'supplier_session'
    AUTH = {'supplier_session': COOKIE}
    REFERER = 'https://sexoptovik.ru/admin/'

    def _session(self, adapter):
        session = requests.Session()
        session.mount('https://', adapter)
        session.mount('http://', adapter)
        return session

    def _jpeg_2x2(self):
        image = Image.new('RGB', (2, 2), (12, 34, 56))
        output = io.BytesIO()
        image.save(output, format='JPEG')
        image.close()
        return output.getvalue()

    def test_supplier_cookie_follows_only_exact_https_origin_redirects(self):
        def route(scheme, host, port, path):
            if host == 'sexoptovik.ru' and path == '/source.jpg':
                return {'status': 302, 'headers': {'Location': '/same-origin.jpg'}}
            if host == 'sexoptovik.ru' and path == '/same-origin.jpg':
                return {
                    'status': 200,
                    'headers': {'Content-Type': 'image/jpeg'},
                    'body': b'same-origin-synthetic-jpeg',
                }
            if host == 'sexoptovik.ru' and path == '/cross-origin.jpg':
                return {
                    'status': 302,
                    'headers': {'Location': 'https://cdn.example.test/photo.jpg'},
                }
            if host == 'sexoptovik.ru' and path == '/downgrade.jpg':
                return {
                    'status': 302,
                    'headers': {'Location': 'http://http-redirect.example.test/photo.jpg'},
                }
            if host == 'cdn.example.test' and path == '/photo.jpg':
                return {
                    'status': 200,
                    'headers': {'Content-Type': 'image/jpeg'},
                    'body': b'synthetic-jpeg',
                }
            if host == 'http-redirect.example.test' and path == '/photo.jpg':
                return {
                    'status': 200,
                    'headers': {'Content-Type': 'image/jpeg'},
                    'body': b'http-downgrade-synthetic-jpeg',
                }
            raise AssertionError('unexpected synthetic destination')

        adapter = _SyntheticPhotoAdapter(route)
        session = self._session(adapter)
        headers = {'Accept': 'image/jpeg', 'Referer': self.REFERER}
        headers_before = dict(headers)
        same_origin_urls = [
            'https://sexoptovik.ru:443/source.jpg',
            'https://sexoptovik.ru:443/same-origin.jpg',
        ]
        cross_origin_urls = [
            'https://sexoptovik.ru/cross-origin.jpg',
            'https://cdn.example.test/photo.jpg',
        ]
        downgrade_urls = [
            'https://sexoptovik.ru/downgrade.jpg',
            'http://http-redirect.example.test/photo.jpg',
        ]
        with mock.patch(
            'services.url_security.validate_external_url', return_value=None,
        ) as validate:
            same_origin_content = PhotoCacheManager._download_image_bytes(
                session, same_origin_urls[0], headers, self.AUTH,
                deadline=time.monotonic() + 10,
            )
            cross_origin_content = PhotoCacheManager._download_image_bytes(
                session, cross_origin_urls[0], headers, self.AUTH,
                deadline=time.monotonic() + 10,
            )
            downgrade_content = PhotoCacheManager._download_image_bytes(
                session, downgrade_urls[0], headers, self.AUTH,
                deadline=time.monotonic() + 10,
            )
        session.close()

        self.assertEqual(same_origin_content, b'same-origin-synthetic-jpeg')
        self.assertEqual(cross_origin_content, b'synthetic-jpeg')
        self.assertEqual(downgrade_content, b'http-downgrade-synthetic-jpeg')
        self.assertEqual(headers, headers_before)
        self.assertEqual([row['cookie'] for row in adapter.requests], [
            f'{self.COOKIE_NAME}={self.COOKIE}',
            f'{self.COOKIE_NAME}={self.COOKIE}',
            f'{self.COOKIE_NAME}={self.COOKIE}',
            None,
            f'{self.COOKIE_NAME}={self.COOKIE}',
            None,
        ])
        self.assertEqual([row['referer'] for row in adapter.requests], [
            self.REFERER, self.REFERER,
            self.REFERER, None,
            self.REFERER, None,
        ])
        self.assertEqual(
            [call.args[0] for call in validate.call_args_list],
            same_origin_urls + cross_origin_urls + downgrade_urls,
        )

    def test_supplier_session_jar_cookie_is_filtered_on_same_host_untrusted_hops(self):
        destinations = [
            'http://sexoptovik.ru/capture',
            'https://sexoptovik.ru:444/capture',
        ]
        for destination in destinations:
            with self.subTest(destination=destination):
                dest = urlsplit(destination)

                def route(scheme, host, port, path):
                    if (
                        scheme == 'https' and host == 'sexoptovik.ru'
                        and port in (None, 443) and path == '/source.jpg'
                    ):
                        return {
                            'status': 302,
                            'headers': {
                                'Location': destination,
                                # Host-only and deliberately non-Secure, as
                                # Requests would accept from this HTTPS hop.
                                'Set-Cookie': (
                                    'rotated_supplier=synthetic_rotated_only; Path=/'
                                ),
                            },
                        }
                    if (
                        scheme == dest.scheme and host == dest.hostname
                        and port == dest.port and path == dest.path
                    ):
                        return {
                            'status': 200,
                            'headers': {'Content-Type': 'image/jpeg'},
                            'body': b'synthetic-jpeg',
                        }
                    raise AssertionError('unexpected synthetic destination')

                adapter = _SyntheticPhotoAdapter(route)
                session = self._session(adapter)
                session.cookies.set(
                    'preexisting_supplier',
                    'synthetic_existing_only',
                    domain='sexoptovik.ru',
                    path='/',
                )
                with mock.patch(
                    'services.url_security.validate_external_url', return_value=None,
                ):
                    content = PhotoCacheManager._download_image_bytes(
                        session,
                        'https://sexoptovik.ru/source.jpg',
                        {},
                        self.AUTH,
                        deadline=time.monotonic() + 10,
                    )

                self.assertEqual(content, b'synthetic-jpeg')
                self.assertEqual(len(adapter.requests), 2)
                self.assertIn(
                    f'{self.COOKIE_NAME}={self.COOKIE}',
                    adapter.requests[0]['cookie'],
                )
                self.assertIsNone(adapter.requests[1]['cookie'])
                self.assertTrue(any(
                    cookie.name == 'rotated_supplier'
                    and cookie.domain == 'sexoptovik.ru'
                    and cookie.value == 'synthetic_rotated_only'
                    for cookie in session.cookies
                ))
                session.close()

    def test_fallback_keeps_public_http_image_unauthenticated_and_session_cookie_scoped(self):
        jpeg = self._jpeg_2x2()

        def route(scheme, host, port, path):
            if host == 'sexoptovik.ru':
                return {'status': 404, 'reason': 'Not Found'}
            if host == 'fallback-a.example.test':
                return {
                    'status': 404,
                    'reason': 'Not Found',
                    'headers': {
                        'Set-Cookie': 'foreign_session=synthetic_foreign_only; Path=/',
                    },
                }
            if host == 'fallback-b.example.test':
                return {
                    'status': 200,
                    'headers': {'Content-Type': 'image/jpeg'},
                    'body': jpeg,
                }
            raise AssertionError('unexpected synthetic destination')

        adapter = _SyntheticPhotoAdapter(route)
        session = self._session(adapter)
        manager = PhotoCacheManager.__new__(PhotoCacheManager)
        with (
            mock.patch.object(photo_cache.requests, 'Session', return_value=session),
            mock.patch(
                'services.url_security.validate_external_url', return_value=None,
            ) as validate,
            mock.patch.object(PhotoCacheManager, 'is_cached', return_value=False),
            mock.patch.object(PhotoCacheManager, 'save_to_cache', return_value=True) as save,
        ):
            saved = manager._download_and_save(
                'sexoptovik',
                'synthetic-product',
                'https://sexoptovik.ru/source.jpg',
                self.AUTH,
                (2, 2),
                'white',
                [
                    'https://fallback-a.example.test/a.jpg',
                    'http://fallback-b.example.test/b.jpg',
                ],
            )

        self.assertTrue(saved)
        save.assert_called_once()
        self.assertEqual([row['host'] for row in adapter.requests], [
            'sexoptovik.ru', 'fallback-a.example.test', 'fallback-b.example.test',
        ])
        self.assertEqual(adapter.requests[0]['cookie'], f'{self.COOKIE_NAME}={self.COOKIE}')
        self.assertIsNone(adapter.requests[1]['cookie'])
        self.assertIsNone(adapter.requests[2]['cookie'])
        self.assertTrue(any(
            cookie.name == 'foreign_session'
            and cookie.domain == 'fallback-a.example.test'
            and cookie.value == 'synthetic_foreign_only'
            for cookie in session.cookies
        ))
        self.assertEqual(adapter.requests[0]['referer'], self.REFERER)
        self.assertIsNone(adapter.requests[1]['referer'])
        self.assertIsNone(adapter.requests[2]['referer'])
        self.assertEqual([call.args[0] for call in validate.call_args_list], [
            'https://sexoptovik.ru/source.jpg',
            'https://fallback-a.example.test/a.jpg',
            'http://fallback-b.example.test/b.jpg',
        ])

    def test_auth_cookie_rejected_for_lookalike_ports_userinfo_and_downgrade(self):
        destinations = [
            'http://sexoptovik.ru/photo.jpg',
            'https://sexoptovik.ru:444/photo.jpg',
            'https://sexoptovik.ru.evil.test/photo.jpg',
            'https://sexoptovik.ru./photo.jpg',
            'https://user@sexoptovik.ru/photo.jpg',
            'https://sexoptovik.ru@evil.test/photo.jpg',
        ]
        for url in destinations:
            with self.subTest(origin=url):
                adapter = _SyntheticPhotoAdapter(
                    lambda scheme, host, port, path: {
                        'status': 200,
                        'headers': {'Content-Type': 'image/jpeg'},
                        'body': b'synthetic-jpeg',
                    },
                )
                session = self._session(adapter)
                headers = {'Referer': self.REFERER, 'X-Test': 'preserve'}
                headers_before = dict(headers)
                with mock.patch(
                    'services.url_security.validate_external_url', return_value=None,
                ):
                    content = PhotoCacheManager._download_image_bytes(
                        session, url, headers, self.AUTH,
                        deadline=time.monotonic() + 10,
                    )
                session.close()
                self.assertEqual(content, b'synthetic-jpeg')
                self.assertEqual(len(adapter.requests), 1)
                self.assertIsNone(adapter.requests[0]['cookie'])
                self.assertIsNone(adapter.requests[0]['referer'])
                self.assertEqual(headers, headers_before)


if __name__ == '__main__':
    unittest.main()
