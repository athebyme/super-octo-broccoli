# -*- coding: utf-8 -*-
"""Real-DB tests for get_sparse_photo_candidates (bulk «Дополнить фото» flow).

Tests use a real in-memory SQLite DB — no ORM mocks.
"""

import json
import unittest
from unittest.mock import patch, MagicMock

from flask import Flask

from models import db, Product, ProductDefaults


import os as _os

_PROJECT_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))


def _make_app():
    app = Flask(__name__, template_folder=_os.path.join(_PROJECT_ROOT, 'templates'))
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    db.init_app(app)
    return app


def _product(seller_id, nm_id, photos=None, quality_score=None, is_active=True,
             vendor_code=None, title='Товар', subject_id=None):
    photos_list = photos if photos is not None else []
    return Product(
        seller_id=seller_id,
        nm_id=nm_id,
        vendor_code=vendor_code or f'VC-{nm_id}',
        title=title,
        photos_json=json.dumps(photos_list),
        characteristics_json=json.dumps({}),
        quality_score=quality_score,
        is_active=is_active,
        subject_id=subject_id,
    )


class TestGetSparsePhotoCandidates(unittest.TestCase):
    """Tests use a real in-memory SQLite DB — no ORM mocks."""

    def setUp(self):
        self.app = _make_app()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

        # Create a global ProductDefaults for seller 1 with min_photos=4
        global_rule = ProductDefaults(
            seller_id=1,
            rule_type='global',
            wb_category_name='',
            min_photos=4,
        )
        db.session.add(global_rule)

        # seller 1 products:
        #   nm_id=1001: 1 photo → sparse (< 4); quality_score=20
        #   nm_id=1002: 2 photos → sparse (< 4); quality_score=30
        #   nm_id=1003: 3 photos → sparse (< 4); quality_score=40
        #   nm_id=1004: 4 photos → NOT sparse (== 4); quality_score=50
        #   nm_id=1005: 5 photos → NOT sparse (> 4); quality_score=25
        #   nm_id=1006: 1 photo, is_active=False → excluded
        # seller 2:
        #   nm_id=2001: 0 photos → sparse but belongs to seller 2 — must not appear
        db.session.add_all([
            _product(1, nm_id=1001, photos=['url1'], quality_score=20.0, subject_id=10),
            _product(1, nm_id=1002, photos=['url1', 'url2'], quality_score=30.0, subject_id=10),
            _product(1, nm_id=1003, photos=['u1', 'u2', 'u3'], quality_score=40.0, subject_id=10),
            _product(1, nm_id=1004, photos=['u1', 'u2', 'u3', 'u4'], quality_score=50.0, subject_id=10),
            _product(1, nm_id=1005, photos=['u1', 'u2', 'u3', 'u4', 'u5'], quality_score=25.0, subject_id=10),
            _product(1, nm_id=1006, photos=['url1'], quality_score=10.0, is_active=False, subject_id=10),
            _product(2, nm_id=2001, photos=[], quality_score=5.0, subject_id=20),
        ])
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _make_seller(self, seller_id=1):
        """Create a minimal seller-like object for the helper."""
        seller = MagicMock()
        seller.id = seller_id
        return seller

    def _make_media(self):
        """Return one 'first'/'pin' media item so compose_card_photo_urls adds something."""
        return [{'filename': 'logo.jpg', 'type': 'photo', 'position': 'first', 'mode': 'pin', 'order': 0}]

    def _run(self, seller_id=1, limit=30, media=None, min_photos=4):
        """Call get_sparse_photo_candidates with patched get_standard_media / get_min_photos."""
        from routes.card_quality import get_sparse_photo_candidates
        seller = self._make_seller(seller_id)
        media_items = media if media is not None else self._make_media()

        with patch('routes.card_quality.get_standard_media', return_value=media_items), \
             patch('routes.card_quality.get_min_photos', return_value=min_photos):
            return get_sparse_photo_candidates(seller, db.session, limit=limit)

    # ── candidate selection ─────────────────────────────────────────────────

    def test_only_sparse_with_composition_returned(self):
        """Only products with len(own_photos) < min AND non-empty compose result appear."""
        candidates, total_m = self._run(seller_id=1, limit=30)
        nm_ids = {c[0].nm_id for c in candidates}
        # sparse: 1001 (1), 1002 (2), 1003 (3)
        self.assertIn(1001, nm_ids)
        self.assertIn(1002, nm_ids)
        self.assertIn(1003, nm_ids)
        # not sparse: 1004 (4 == min), 1005 (5 > min)
        self.assertNotIn(1004, nm_ids)
        self.assertNotIn(1005, nm_ids)

    def test_inactive_product_excluded(self):
        """Inactive products are never returned even if sparse."""
        candidates, _ = self._run(seller_id=1, limit=30)
        nm_ids = {c[0].nm_id for c in candidates}
        self.assertNotIn(1006, nm_ids)

    def test_seller_scoped(self):
        """Results are scoped to the requested seller — seller 2's products never appear."""
        candidates, _ = self._run(seller_id=1, limit=30)
        nm_ids = {c[0].nm_id for c in candidates}
        self.assertNotIn(2001, nm_ids)

    def test_returns_tuples_product_and_composed_urls(self):
        """Each item in candidates is a (Product, list[str]) tuple."""
        candidates, _ = self._run(seller_id=1, limit=30)
        self.assertTrue(len(candidates) > 0)
        for item in candidates:
            self.assertIsInstance(item, tuple)
            self.assertEqual(len(item), 2)
            product, composed = item
            self.assertTrue(hasattr(product, 'nm_id'))
            self.assertIsInstance(composed, list)
            self.assertTrue(len(composed) > 0, 'composed URLs must be non-empty')

    def test_total_m_is_total_sparse_with_composition(self):
        """total_M reflects all sparse+composable products, not just top-N."""
        candidates, total_m = self._run(seller_id=1, limit=30)
        # 3 sparse with composition: 1001, 1002, 1003
        self.assertEqual(total_m, 3)
        self.assertEqual(len(candidates), 3)

    def test_limit_caps_candidates_but_not_total_m(self):
        """When limit < total_m, candidates is capped at limit, total_m stays full."""
        candidates, total_m = self._run(seller_id=1, limit=2)
        self.assertEqual(len(candidates), 2)
        self.assertEqual(total_m, 3)  # still 3 sparse products total

    def test_ordered_by_photo_count_asc(self):
        """Candidates are ordered by photo count ascending (fewest photos first)."""
        candidates, _ = self._run(seller_id=1, limit=30)
        photo_counts = []
        for product, _ in candidates:
            own = json.loads(product.photos_json) if product.photos_json else []
            photo_counts.append(len(own))
        self.assertEqual(photo_counts, sorted(photo_counts))

    def test_no_composition_means_no_candidate(self):
        """If compose_card_photo_urls returns [] for all products, candidates is empty."""
        # Pass empty media so compose always returns []
        candidates, total_m = self._run(seller_id=1, limit=30, media=[])
        self.assertEqual(candidates, [])
        self.assertEqual(total_m, 0)

    def test_seller2_scoped_correctly(self):
        """Seller 2's sparse product 2001 appears only when queried for seller 2."""
        candidates, total_m = self._run(seller_id=2, limit=30)
        nm_ids = {c[0].nm_id for c in candidates}
        self.assertIn(2001, nm_ids)
        self.assertNotIn(1001, nm_ids)


class TestStandardPhotosBulkRouteLogic(unittest.TestCase):
    """Integration tests for route-level logic: verify the page builds the right candidate list.

    We avoid rendering the full base.html template (needs CSRF + Flask-Login)
    and instead exercise the route-level logic via get_sparse_photo_candidates
    with the same db/seller as the route would use.
    """

    def setUp(self):
        self.app = _make_app()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

        # Seed global rule
        rule = ProductDefaults(
            seller_id=1, rule_type='global', wb_category_name='', min_photos=4
        )
        db.session.add(rule)

        # Products: 1001 sparse, 1002 full
        db.session.add_all([
            _product(1, nm_id=1001, photos=['url1'], quality_score=20.0, subject_id=10),
            _product(1, nm_id=1002, photos=['u1', 'u2', 'u3', 'u4'], quality_score=50.0, subject_id=10),
        ])
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _make_seller(self, seller_id=1):
        seller = MagicMock()
        seller.id = seller_id
        seller.wb_api_key = 'fake-key-123'
        seller.has_valid_api_key.return_value = True
        return seller

    def test_get_route_candidate_list_contains_sparse_only(self):
        """Route candidate list: sparse product 1001 present, full product 1002 absent."""
        from routes.card_quality import get_sparse_photo_candidates
        seller = self._make_seller()
        media = [{'filename': 'logo.jpg', 'type': 'photo',
                  'position': 'first', 'mode': 'pin', 'order': 0}]

        with patch('routes.card_quality.get_standard_media', return_value=media), \
             patch('routes.card_quality.get_min_photos', return_value=4):
            candidates, total_m = get_sparse_photo_candidates(seller, db.session, limit=30)

        nm_ids = {c[0].nm_id for c in candidates}
        self.assertIn(1001, nm_ids)
        self.assertNotIn(1002, nm_ids)
        self.assertEqual(total_m, 1)

    def test_get_route_candidate_list_shows_total_m(self):
        """total_m returned by helper matches the count the route would pass to template."""
        from routes.card_quality import get_sparse_photo_candidates
        seller = self._make_seller()
        media = [{'filename': 'logo.jpg', 'type': 'photo',
                  'position': 'first', 'mode': 'pin', 'order': 0}]

        with patch('routes.card_quality.get_standard_media', return_value=media), \
             patch('routes.card_quality.get_min_photos', return_value=4):
            candidates, total_m = get_sparse_photo_candidates(seller, db.session, limit=30)

        # Only one sparse+composable product in test DB
        self.assertEqual(total_m, 1)
        self.assertEqual(len(candidates), 1)

    def test_route_limit_constant_is_30(self):
        """STANDARD_PHOTOS_BULK_LIMIT is 30 as required."""
        from routes.card_quality import STANDARD_PHOTOS_BULK_LIMIT
        self.assertEqual(STANDARD_PHOTOS_BULK_LIMIT, 30)


if __name__ == '__main__':
    unittest.main()
