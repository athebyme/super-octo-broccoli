# -*- coding: utf-8 -*-
"""Регрессии photo storm: UI request не выполняет внешний network I/O."""

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from flask import Flask

import services.photo_cache as photo_cache_module
import routes.photos as photo_routes
from routes.photos import register_photo_routes
from services.photo_cache import PhotoCacheManager


class PhotoRouteNonBlockingTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(TESTING=True, SECRET_KEY='photo-route-test')
        register_photo_routes(self.app)
        self.client = self.app.test_client()
        self.user = SimpleNamespace(
            is_authenticated=True,
            seller=SimpleNamespace(id=7),
        )

    @staticmethod
    def _cache_miss():
        cache = MagicMock()
        cache.is_cached.return_value = False
        cache.queue_download.return_value = True
        return cache

    def test_supplier_cache_miss_queues_without_auth_or_network(self):
        supplier = SimpleNamespace(
            code='sexoptovik',
            auth_login='login',
            auth_password='password',
        )
        product = SimpleNamespace(
            photo_urls_json=json.dumps([{
                'sexoptovik': 'https://example.com/source.jpg',
                'blur': 'https://example.com/preview.jpg',
            }]),
            supplier=supplier,
            external_id='source-1',
        )
        fake_model = SimpleNamespace(
            query=SimpleNamespace(get_or_404=MagicMock(return_value=product)),
        )
        cache = self._cache_miss()

        started = time.monotonic()
        with patch('flask_login.utils._get_user', return_value=self.user), \
             patch('models.SupplierProduct', fake_model), \
             patch('services.photo_cache.get_photo_cache', return_value=cache), \
             patch(
                 'routes.photos._get_supplier_auth_cookies',
                 side_effect=AssertionError('auth must run only in worker'),
             ), patch(
                 'requests.sessions.Session.request',
                 side_effect=AssertionError('network in UI request'),
             ):
            response = self.client.get(
                '/api/photos/supplier-product/10/0?deferred=1',
            )
        elapsed = time.monotonic() - started

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.headers['X-Photo-Cache'], 'pending')
        self.assertEqual(response.headers['X-Photo-Queue'], 'queued')
        self.assertLess(elapsed, 1.0)
        cache.queue_download.assert_called_once()
        kwargs = cache.queue_download.call_args.kwargs
        self.assertEqual(kwargs['supplier_type'], 'sexoptovik')
        self.assertEqual(
            kwargs['fallback_urls'],
            ['https://example.com/preview.jpg'],
        )
        self.assertTrue(callable(kwargs['auth_cookies_provider']))

    def test_imported_supplier_photo_has_no_redirect_chain(self):
        supplier = SimpleNamespace(
            code='synthetic', auth_login=None, auth_password=None,
        )
        supplier_product = SimpleNamespace(
            photo_urls_json=json.dumps([
                'https://example.com/supplier.jpg',
            ]),
            supplier=supplier,
            external_id='supplier-source-2',
        )
        imported = SimpleNamespace(
            id=20,
            external_id='imported-20',
            supplier_product_id=30,
            supplier=supplier,
            photo_urls=json.dumps(['https://example.com/imported.jpg']),
        )
        imported_model = SimpleNamespace(
            query=SimpleNamespace(
                filter_by=MagicMock(return_value=SimpleNamespace(
                    first_or_404=MagicMock(return_value=imported),
                )),
            ),
        )
        supplier_model = SimpleNamespace()
        fake_db = SimpleNamespace(
            session=SimpleNamespace(get=MagicMock(return_value=supplier_product)),
        )
        cache = self._cache_miss()

        with patch('flask_login.utils._get_user', return_value=self.user), \
             patch('models.ImportedProduct', imported_model), \
             patch('models.SupplierProduct', supplier_model), \
             patch('models.db', fake_db), \
             patch('services.photo_cache.get_photo_cache', return_value=cache):
            response = self.client.get(
                '/api/photos/imported-product/20/0?deferred=1',
            )

        self.assertEqual(response.status_code, 202)
        self.assertNotIn('Location', response.headers)
        imported_model.query.filter_by.assert_called_once_with(
            id=20, seller_id=7,
        )
        cache.queue_download.assert_called_once()
        kwargs = cache.queue_download.call_args.kwargs
        self.assertEqual(kwargs['supplier_type'], 'synthetic')
        self.assertEqual(kwargs['external_id'], 'supplier-source-2')
        self.assertEqual(kwargs['url'], 'https://example.com/supplier.jpg')

    def test_public_cache_warm_fails_fast_when_bulkhead_is_busy(self):
        cache = MagicMock()
        with patch.object(
            photo_routes._photo_public_fetch_slots,
            'acquire',
            return_value=False,
        ):
            status = photo_routes._warm_public_photo(
                cache,
                'supplier',
                'sku',
                'https://example.com/image.jpg',
                [],
                None,
            )

        self.assertEqual(status, 'busy')
        cache.download_now.assert_not_called()


class PhotoCacheBoundedQueueTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_instance = PhotoCacheManager._instance
        self.original_global = photo_cache_module._photo_cache
        PhotoCacheManager._instance = None
        photo_cache_module._photo_cache = None
        self.patches = [
            patch.object(
                photo_cache_module, 'PHOTO_CACHE_DIR', self.temp_dir.name,
            ),
            patch.object(PhotoCacheManager, 'start_workers'),
        ]
        for item in self.patches:
            item.start()
        self.cache = PhotoCacheManager()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        PhotoCacheManager._instance = self.original_instance
        photo_cache_module._photo_cache = self.original_global
        self.temp_dir.cleanup()

    def test_duplicate_cache_miss_occupies_one_queue_slot(self):
        first = self.cache.queue_download(
            'supplier', 'sku-1', 'https://example.com/image.jpg',
        )
        second = self.cache.queue_download(
            'supplier', 'sku-1', 'https://example.com/image.jpg',
        )

        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(self.cache._download_queue.qsize(), 1)
        self.assertEqual(self.cache.get_stats()['downloads_queued'], 1)
        self.assertEqual(self.cache.get_stats()['downloads_deduplicated'], 1)

    def test_worker_releases_dedupe_claim_after_completion(self):
        provider = MagicMock(return_value={'session': 'cookie'})
        self.assertTrue(self.cache.queue_download(
            'supplier',
            'sku-worker',
            'https://example.com/worker.jpg',
            auth_cookies_provider=provider,
        ))
        self.cache._download_queue.put_nowait(None)
        self.cache._running = True

        with patch.object(
            self.cache, '_download_and_save', return_value=True,
        ) as download:
            self.cache._download_worker()
        self.cache._running = False

        provider.assert_called_once_with()
        download.assert_called_once()
        self.assertEqual(self.cache._pending_downloads, set())
        self.assertEqual(self.cache.get_stats()['downloads_completed'], 1)

        self.assertTrue(self.cache.queue_download(
            'supplier', 'sku-worker', 'https://example.com/worker.jpg',
        ))

    def test_cache_write_is_atomic(self):
        with patch.object(self.cache, '_schedule_cache_maintenance'):
            saved = self.cache.save_to_cache(
                'supplier', 'sku-2', 'https://example.com/image.jpg', b'jpeg',
            )

        cache_path = Path(self.cache.get_cache_path(
            'supplier', 'sku-2', 'https://example.com/image.jpg',
        ))
        self.assertTrue(saved)
        self.assertEqual(cache_path.read_bytes(), b'jpeg')
        self.assertEqual(list(cache_path.parent.glob('*.tmp-*')), [])

    def test_maintenance_deletes_oldest_files_to_low_water_mark(self):
        root = Path(self.temp_dir.name)
        paths = []
        for index in range(3):
            path = root / 'supplier' / str(index) / f'{index}.jpg'
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(bytes([index]) * 40)
            os.utime(path, (100 + index, 100 + index))
            paths.append(path)

        disk_usage = SimpleNamespace(free=10_000)
        with patch.object(photo_cache_module, 'PHOTO_CACHE_MAX_BYTES', 100), \
             patch.object(photo_cache_module, 'PHOTO_CACHE_PRUNE_TO_BYTES', 50), \
             patch.object(photo_cache_module, 'PHOTO_CACHE_MIN_FREE_BYTES', 1), \
             patch.object(photo_cache_module.shutil, 'disk_usage', return_value=disk_usage):
            self.cache._prune_cache_files()

        self.assertFalse(paths[0].exists())
        self.assertFalse(paths[1].exists())
        self.assertTrue(paths[2].exists())
        self.assertEqual(
            self.cache.get_stats()['maintenance_deleted_bytes'], 80,
        )


class MyProductsPhotoTemplateContractTest(unittest.TestCase):
    def test_product_thumbnails_are_lazy_and_retry_async_cache_warm(self):
        template = Path('templates/seller_my_products.html').read_text(
            encoding='utf-8',
        )

        self.assertIn('deferred=1', template)
        self.assertIn('loading="lazy"', template)
        self.assertIn('fetchpriority="low"', template)
        self.assertIn('scheduleProductPhotoRetry(this)', template)


if __name__ == '__main__':
    unittest.main()
