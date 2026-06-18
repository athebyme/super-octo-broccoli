# -*- coding: utf-8 -*-
import json, unittest
from flask import Flask
from models import db, ProductDefaults


def _app():
    app = Flask(__name__)
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    db.init_app(app)
    return app


class StandardMediaModelTest(unittest.TestCase):
    def setUp(self):
        self.app = _app(); self.ctx = self.app.app_context(); self.ctx.push(); db.create_all()

    def tearDown(self):
        db.session.remove(); db.drop_all(); self.ctx.pop()

    def _media(self, fn, position=None, mode=None, order=None, typ='photo'):
        d = {'filename': fn, 'original_name': fn, 'type': typ, 'size': 1}
        if position is not None: d['position'] = position
        if mode is not None: d['mode'] = mode
        if order is not None: d['order'] = order
        return d

    def test_normalize_backward_compat(self):
        from models import normalize_media_item
        n = normalize_media_item({'filename': 'a.jpg', 'type': 'photo'})
        self.assertEqual(n['position'], 'last')
        self.assertEqual(n['mode'], 'fill')
        self.assertEqual(n['order'], 0)

    def test_get_min_photos_default_and_value(self):
        from models import get_min_photos
        self.assertEqual(get_min_photos(1), 4)  # нет правила → дефолт
        db.session.add(ProductDefaults(seller_id=1, rule_type='global', min_photos=6))
        db.session.commit()
        self.assertEqual(get_min_photos(1), 6)

    def test_get_standard_media_union_global_and_category(self):
        from models import get_standard_media
        db.session.add(ProductDefaults(seller_id=1, rule_type='global',
            global_media=json.dumps([self._media('g.jpg', 'first', 'pin', 0)])))
        db.session.add(ProductDefaults(seller_id=1, rule_type='category', wb_subject_id=105,
            global_media=json.dumps([self._media('c.jpg', 'last', 'fill', 1)])))
        db.session.commit()
        media = get_standard_media(1, 105)
        names = {m['filename'] for m in media}
        self.assertEqual(names, {'g.jpg', 'c.jpg'})
        # для другой категории — только глобальное
        self.assertEqual({m['filename'] for m in get_standard_media(1, 999)}, {'g.jpg'})
