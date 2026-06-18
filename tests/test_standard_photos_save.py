# -*- coding: utf-8 -*-
"""
Тест роутов сохранения стандартных медиа:
  POST /settings/product-defaults/save-media-meta  → position/mode/order в global_media
  POST /settings/product-defaults/save-global       → min_photos в ProductDefaults(rule_type='global')
"""

import json
import os
import unittest


class TestStandardPhotosSave(unittest.TestCase):
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
        cls.app.config['SECRET_KEY'] = 'test-secret-key-for-unit-tests'
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
        from models import User, Seller, ProductDefaults
        user = User(username='seller_media', email='seller_media@example.com', password_hash='x')
        cls.db.session.add(user)
        cls.db.session.flush()
        seller = Seller(user_id=user.id, company_name='Медиа Тест', wb_seller_id='777')
        seller.wb_api_key = 'test-api-key'
        cls.db.session.add(seller)
        cls.db.session.flush()
        cls.user_id = user.id
        cls.seller_id = seller.id

        # Создаём глобальное правило с одним медиа-элементом
        existing_media = [
            {
                'filename': 'abc123.jpg',
                'original_name': 'brand.jpg',
                'type': 'photo',
                'size': 50000,
            }
        ]
        rule = ProductDefaults(
            seller_id=seller.id,
            rule_type='global',
            wb_subject_id=None,
            global_media=json.dumps(existing_media, ensure_ascii=False),
        )
        cls.db.session.add(rule)
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

    # ------------------------------------------------------------------ #
    # 1. Сохранение метаданных медиа (position / mode / order)            #
    # ------------------------------------------------------------------ #

    def test_save_media_meta_sets_position_mode_order(self):
        """POST save-media-meta с position=first/mode=pin/order=2 → БД содержит эти значения."""
        client = self._client_logged_in()
        resp = client.post(
            '/settings/product-defaults/save-media-meta',
            data={
                'filename': 'abc123.jpg',
                'position': 'first',
                'mode': 'pin',
                'order': '2',
            },
        )
        self.assertEqual(resp.status_code, 200, resp.data)
        payload = resp.get_json()
        self.assertTrue(payload.get('success'), payload)

        # Проверяем реальное состояние БД
        with self.app.app_context():
            from models import ProductDefaults
            rule = ProductDefaults.query.filter_by(
                seller_id=self.seller_id, rule_type='global'
            ).first()
            self.assertIsNotNone(rule)
            media = rule.get_global_media_list()
            item = next((m for m in media if m['filename'] == 'abc123.jpg'), None)
            self.assertIsNotNone(item, f"Файл 'abc123.jpg' не найден в global_media: {media}")
            self.assertEqual(item['position'], 'first')
            self.assertEqual(item['mode'], 'pin')
            self.assertEqual(item['order'], 2)

    def test_save_media_meta_defaults_to_last_fill(self):
        """POST save-media-meta без position/mode → дефолты last/fill через normalize_media_item."""
        # Добавляем новый файл для этого теста
        with self.app.app_context():
            from models import ProductDefaults
            rule = ProductDefaults.query.filter_by(
                seller_id=self.seller_id, rule_type='global'
            ).first()
            media = rule.get_global_media_list()
            media.append({
                'filename': 'default_test.jpg',
                'original_name': 'default_test.jpg',
                'type': 'photo',
                'size': 1000,
            })
            import json as _json
            rule.global_media = _json.dumps(media, ensure_ascii=False)
            self.db.session.commit()

        client = self._client_logged_in()
        resp = client.post(
            '/settings/product-defaults/save-media-meta',
            data={'filename': 'default_test.jpg'},
        )
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertTrue(payload.get('success'), payload)

        with self.app.app_context():
            from models import ProductDefaults
            rule = ProductDefaults.query.filter_by(
                seller_id=self.seller_id, rule_type='global'
            ).first()
            item = next(
                (m for m in rule.get_global_media_list() if m['filename'] == 'default_test.jpg'),
                None
            )
            self.assertIsNotNone(item)
            self.assertEqual(item['position'], 'last')
            self.assertEqual(item['mode'], 'fill')
            self.assertEqual(item['order'], 0)

    def test_save_media_meta_unknown_file_returns_404(self):
        """POST save-media-meta с несуществующим filename → 404."""
        client = self._client_logged_in()
        resp = client.post(
            '/settings/product-defaults/save-media-meta',
            data={'filename': 'nonexistent.jpg', 'position': 'first'},
        )
        self.assertEqual(resp.status_code, 404)

    def test_save_media_meta_requires_login(self):
        """Без логина — редирект или 403."""
        resp = self.app.test_client().post(
            '/settings/product-defaults/save-media-meta',
            data={'filename': 'abc123.jpg'},
        )
        self.assertIn(resp.status_code, (302, 401, 403))

    # ------------------------------------------------------------------ #
    # 2. Сохранение min_photos в save-global                              #
    # ------------------------------------------------------------------ #

    def test_save_global_persists_min_photos(self):
        """POST save-global с min_photos=6 → ProductDefaults(global).min_photos == 6."""
        client = self._client_logged_in()
        resp = client.post(
            '/settings/product-defaults/save-global',
            data={'min_photos': '6'},
        )
        self.assertEqual(resp.status_code, 200, resp.data)
        payload = resp.get_json()
        self.assertTrue(payload.get('success'), payload)

        with self.app.app_context():
            from models import ProductDefaults
            rule = ProductDefaults.query.filter_by(
                seller_id=self.seller_id, rule_type='global'
            ).first()
            self.assertIsNotNone(rule)
            self.assertEqual(rule.min_photos, 6)

    def test_save_global_min_photos_zero_clears(self):
        """POST save-global с min_photos=0 → min_photos хранится как 0 или None."""
        client = self._client_logged_in()
        resp = client.post(
            '/settings/product-defaults/save-global',
            data={'min_photos': '0'},
        )
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertTrue(payload.get('success'), payload)

        with self.app.app_context():
            from models import ProductDefaults
            rule = ProductDefaults.query.filter_by(
                seller_id=self.seller_id, rule_type='global'
            ).first()
            # 0 или None — оба допустимы
            self.assertIn(rule.min_photos, (0, None))


if __name__ == '__main__':
    unittest.main()
