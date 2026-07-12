# -*- coding: utf-8 -*-
"""Тесты самообучающегося резолвера корзин WB CDN (services/wb_media.py).

Таблица корзин WB конечна и устаревает (vol 9059 → basket 39, а старая
таблица давала 21) — для vol за пределами проверенных диапазонов корзина
определяется пробой CDN и кешируется навсегда (маппинг неизменяем).
"""

import unittest
from unittest.mock import patch

from flask import Flask

from models import db, SystemSettings


def _make_app():
    app = Flask(__name__)
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    db.init_app(app)
    return app


class TestStaticRanges(unittest.TestCase):
    def test_known_vols_resolve_from_table_without_probe(self):
        from services import wb_media
        with patch.object(wb_media, '_probe_vol') as mock_probe:
            # vol 1690 → basket 12 (проверено живой пробой CDN)
            self.assertEqual(wb_media.resolve_basket(169060325), '12')
            self.assertEqual(wb_media.resolve_basket(14300000), '01')   # vol 143
            self.assertEqual(wb_media.resolve_basket(456500000), '25')  # vol 4565
        mock_probe.assert_not_called()

    def test_zero_nm_id_returns_empty_url(self):
        from services import wb_media
        self.assertEqual(wb_media.wb_photo_url(0), '')
        self.assertEqual(wb_media.wb_photo_url(None), '')


class TestProbeResolution(unittest.TestCase):
    def setUp(self):
        from services import wb_media
        wb_media._reset_caches_for_tests()

    def test_unknown_vol_probes_and_caches(self):
        from services import wb_media
        calls = []

        def fake_probe(vol, nm_id):
            calls.append(vol)
            return '39'

        with patch.object(wb_media, '_probe_vol', side_effect=fake_probe):
            b1 = wb_media.resolve_basket(905908816)  # vol 9059
            b2 = wb_media.resolve_basket(905908816)  # из кеша
        self.assertEqual(b1, '39')
        self.assertEqual(b2, '39')
        self.assertEqual(calls, [9059])  # проба ровно один раз

    def test_failed_probe_falls_back_to_estimate_without_caching(self):
        from services import wb_media
        with patch.object(wb_media, '_probe_vol', return_value=None) as mock_probe:
            b1 = wb_media.resolve_basket(905908816)
            b2 = wb_media.resolve_basket(905908816)
        self.assertTrue(b1.isdigit())
        self.assertEqual(b1, b2)
        # неудача НЕ кешируется — пробуем снова при следующем обращении
        self.assertEqual(mock_probe.call_count, 2)

    def test_estimate_uses_nearest_known_vol(self):
        """Оценка отталкивается от ближайшего известного vol, а не от формулы."""
        from services import wb_media
        with patch.object(wb_media, '_probe_vol', return_value='39'):
            wb_media.resolve_basket(905908816)  # vol 9059 → 39 в кеше
        # сосед vol 9100 должен стартовать с оценки ~39 (±1), а не с формульной
        est = wb_media._estimate_basket(9100)
        self.assertIn(est, (39, 40))

    def test_wb_photo_url_format(self):
        from services import wb_media
        with patch.object(wb_media, '_probe_vol', return_value='39'):
            url = wb_media.wb_photo_url(905908816, 2, 'c246x328')
        self.assertEqual(
            url,
            'https://basket-39.wbbasket.ru/vol9059/part905908/905908816/images/c246x328/2.webp')


class TestPersistence(unittest.TestCase):
    def setUp(self):
        from services import wb_media
        wb_media._reset_caches_for_tests()
        self.app = _make_app()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_probed_basket_persisted_and_reloaded(self):
        from services import wb_media
        with patch.object(wb_media, '_probe_vol', return_value='39'):
            wb_media.resolve_basket(905908816)
        # персист в SystemSettings
        s = SystemSettings.query.filter_by(key='wb_basket_vol_map').first()
        self.assertIsNotNone(s)
        self.assertIn('"9059"', s.value)
        # новый процесс (сброшенный кеш) читает из персиста без пробы
        wb_media._reset_caches_for_tests()
        with patch.object(wb_media, '_probe_vol') as mock_probe:
            self.assertEqual(wb_media.resolve_basket(905908816), '39')
        mock_probe.assert_not_called()

    def test_persist_merges_with_existing_entries(self):
        from services import wb_media
        s = SystemSettings(key='wb_basket_vol_map', value='{"5000": "27"}',
                           value_type='json')
        db.session.add(s)
        db.session.commit()
        with patch.object(wb_media, '_probe_vol', return_value='39'):
            wb_media.resolve_basket(905908816)
        s = SystemSettings.query.filter_by(key='wb_basket_vol_map').first()
        self.assertIn('"5000"', s.value)
        self.assertIn('"9059"', s.value)


if __name__ == '__main__':
    unittest.main()
