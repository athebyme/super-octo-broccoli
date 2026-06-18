# -*- coding: utf-8 -*-
"""Тест валидации размера фото при загрузке (WB ≥700×900)."""

import io
import os
import unittest


def _make_png(width, height):
    """Генерирует минимальный PNG-файл заданного размера через Pillow."""
    from PIL import Image
    img = Image.new('RGB', (width, height), color=(200, 200, 200))
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    return buf.read()


class TestUploadMediaPhotoValidation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ['DISABLE_SECURE_COOKIE'] = '1'
        import sqlalchemy as _sa
        from sqlalchemy.pool import StaticPool
        import seller_platform  # noqa
        from models import db
        cls.app = seller_platform.app
        cls.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        cls.app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
        cls.app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {}
        cls.app.config['SECRET_KEY'] = 'test-secret-key-upload-validation'
        cls.app.config['WTF_CSRF_ENABLED'] = False
        cls.app.config['TESTING'] = True
        cls._engine = _sa.create_engine(
            'sqlite:///:memory:',
            connect_args={'check_same_thread': False},
            poolclass=StaticPool,
        )
        db._app_engines[cls.app] = {None: cls._engine}
        cls.db = db
        with cls.app.app_context():
            db.create_all()
            cls._seed()

    @classmethod
    def _seed(cls):
        from models import User, Seller
        user = User(username='uploader', email='uploader@example.com', password_hash='x')
        cls.db.session.add(user)
        cls.db.session.flush()
        seller = Seller(user_id=user.id, company_name='ООО Загрузка', wb_seller_id='456')
        seller.wb_api_key = 'test-key'
        cls.db.session.add(seller)
        cls.db.session.flush()
        cls.user_id = user.id
        cls.seller_id = seller.id
        cls.db.session.commit()

    @classmethod
    def tearDownClass(cls):
        with cls.app.app_context():
            cls.db.session.remove()
            cls.db.drop_all()
        cls._engine.dispose()

    def _client_logged_in(self):
        client = self.app.test_client()
        with client.session_transaction() as sess:
            sess['_user_id'] = str(self.user_id)
            sess['_fresh'] = True
        return client

    def _media_dir(self):
        import os
        return os.path.join(self.app.root_path, 'data', 'global_media', str(self.seller_id))

    def _saved_files_before(self):
        d = self._media_dir()
        if not os.path.isdir(d):
            return set()
        return set(os.listdir(d))

    def test_small_photo_is_rejected_with_400(self):
        """POST с PNG 100×100 должен возвращать 400 с русским сообщением об ошибке."""
        png_bytes = _make_png(100, 100)
        client = self._client_logged_in()
        before = self._saved_files_before()

        resp = client.post(
            '/settings/product-defaults/upload-media',
            data={'file': (io.BytesIO(png_bytes), 'small.png')},
            content_type='multipart/form-data',
        )

        self.assertEqual(resp.status_code, 400)
        payload = resp.get_json()
        self.assertIsNotNone(payload)
        self.assertIn('error', payload)
        self.assertIn('700', payload['error'])
        self.assertIn('900', payload['error'])

        # Файл НЕ должен быть сохранён
        after = self._saved_files_before()
        new_files = after - before
        self.assertEqual(len(new_files), 0, f'Файл не должен быть сохранён, но появился: {new_files}')

    def test_valid_photo_is_accepted(self):
        """POST с PNG 700×900 должен возвращать 200 (или 2xx) и успешный JSON."""
        png_bytes = _make_png(700, 900)
        client = self._client_logged_in()
        before = self._saved_files_before()

        resp = client.post(
            '/settings/product-defaults/upload-media',
            data={'file': (io.BytesIO(png_bytes), 'valid.png')},
            content_type='multipart/form-data',
        )

        self.assertIn(resp.status_code, (200, 201))
        payload = resp.get_json()
        self.assertIsNotNone(payload)
        self.assertTrue(payload.get('success'), f'Ожидали success=True, получили: {payload}')

        # Файл должен быть сохранён
        after = self._saved_files_before()
        new_files = after - before
        self.assertEqual(len(new_files), 1, f'Ожидали 1 новый файл, нашли: {new_files}')

    def test_large_photo_above_min_is_accepted(self):
        """POST с PNG 1200×1600 (больше минимума) тоже должен проходить."""
        png_bytes = _make_png(1200, 1600)
        client = self._client_logged_in()
        before = self._saved_files_before()

        resp = client.post(
            '/settings/product-defaults/upload-media',
            data={'file': (io.BytesIO(png_bytes), 'large.png')},
            content_type='multipart/form-data',
        )

        self.assertIn(resp.status_code, (200, 201))
        payload = resp.get_json()
        self.assertTrue(payload.get('success'))

        after = self._saved_files_before()
        new_files = after - before
        self.assertEqual(len(new_files), 1)

    def test_video_upload_skips_size_check(self):
        """Видеофайл не должен проверяться на размер изображения."""
        # Создаём минимальный «видео» файл (несколько байт — для теста достаточно,
        # route просто смотрит на расширение для _file_type)
        fake_video = b'\x00\x00\x00\x1c' + b'ftyp' + b'mp42' * 10  # псевдо mp4 header
        client = self._client_logged_in()
        before = self._saved_files_before()

        resp = client.post(
            '/settings/product-defaults/upload-media',
            data={'file': (io.BytesIO(fake_video), 'clip.mp4')},
            content_type='multipart/form-data',
        )

        # Видео должно проходить без проверки размера пикселей
        # (может упасть при чтении как изображение, если проверка применяется к видео)
        # Мы проверяем, что не возвращается ошибка про 700x900
        if resp.status_code == 400:
            payload = resp.get_json()
            msg = payload.get('error', '') if payload else ''
            self.assertNotIn('700', msg, 'Видео не должно проверяться на размер фото')
            self.assertNotIn('900', msg)


if __name__ == '__main__':
    unittest.main()
