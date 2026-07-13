# -*- coding: utf-8 -*-
"""Real-DB tests for _collect_bulk_candidates (bulk «Улучшить слабые» flow)."""

import json
import unittest
from unittest.mock import patch, MagicMock

from flask import Flask

from models import db, Product


def _make_app():
    app = Flask(__name__)
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    db.init_app(app)
    return app


def _product(seller_id, nm_id, quality_score=None, nm_rating=None, is_active=True,
             vendor_code=None, title='Товар', attention_reasons=None, quality_impact=None):
    return Product(
        seller_id=seller_id,
        nm_id=nm_id,
        vendor_code=vendor_code or f'VC-{nm_id}',
        title=title,
        photos_json=json.dumps([]),
        characteristics_json=json.dumps({}),
        quality_score=quality_score,
        nm_rating=nm_rating,
        is_active=is_active,
        attention_reasons=attention_reasons,
        quality_impact=quality_impact,
    )


class TestCollectBulkCandidatesRealDB(unittest.TestCase):
    """Tests use a real in-memory SQLite DB — no ORM mocks."""

    def setUp(self):
        self.app = _make_app()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

        # "Weak" is now defined by a non-empty attention_reasons (not raw quality_score/
        # nm_rating thresholds) — see services/card_quality_scorer.compute_attention.
        # seller 1 products:
        #   quality_score=20, attention_reasons='weak_chars', impact=35 → weak
        #   quality_score=35, nm_rating=5, attention_reasons='low_rating,weak_chars',
        #       impact=80 → weak
        #   quality_score=40, attention_reasons='weak_description', impact=50 → weak
        #   quality_score=60, nm_rating=5, attention_reasons='low_rating', impact=65 → weak
        #   quality_score=70, nm_rating=8, no attention_reasons → NOT weak
        #   quality_score=80, nm_rating=9, no attention_reasons → NOT weak
        #   is_active=False, attention_reasons='few_photos' → excluded (inactive)
        # seller 2 products:
        #   attention_reasons='low_rating' → weak for seller 2, NOT for seller 1
        db.session.add_all([
            _product(1, nm_id=1001, quality_score=20.0, nm_rating=None,
                     attention_reasons='weak_chars', quality_impact=35.0),
            _product(1, nm_id=1002, quality_score=35.0, nm_rating=5.0,
                     attention_reasons='low_rating,weak_chars', quality_impact=80.0),
            _product(1, nm_id=1003, quality_score=40.0, nm_rating=8.0,
                     attention_reasons='weak_description', quality_impact=50.0),
            _product(1, nm_id=1004, quality_score=60.0, nm_rating=5.0,
                     attention_reasons='low_rating', quality_impact=65.0),
            _product(1, nm_id=1005, quality_score=70.0, nm_rating=8.0),
            _product(1, nm_id=1006, quality_score=80.0, nm_rating=9.0),
            _product(1, nm_id=1007, quality_score=10.0, nm_rating=3.0, is_active=False,
                     attention_reasons='few_photos', quality_impact=90.0),
            _product(2, nm_id=2001, quality_score=10.0, nm_rating=3.0,
                     attention_reasons='low_rating', quality_impact=99.0),
        ])
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _run(self, seller_id=1, limit=30):
        from routes.card_quality import _collect_bulk_candidates
        # Patch enrichment service so we don't need a real supplier DB
        with patch('routes.card_quality.card_quality_detail') as mock_detail, \
             patch('routes.card_quality.collect_weak_dimensions', return_value=['photos']), \
             patch('routes.card_quality.get_enrichment_service') as mock_es:

            def _detail(p):
                return {
                    'product_id': p.id,
                    'nm_id': p.nm_id,
                    'quality_score': p.quality_score,
                    'title': p.title,
                    'vendor_code': p.vendor_code,
                }

            mock_detail.side_effect = _detail
            svc = MagicMock()
            svc.find_supplier_data.return_value = None
            mock_es.return_value = svc
            return _collect_bulk_candidates(seller_id=seller_id, limit=limit)

    def test_only_weak_rows_returned(self):
        """Only products with a non-empty attention_reasons are returned."""
        res = self._run(seller_id=1, limit=30)
        nm_ids = {c['nm_id'] for c in res['candidates']}
        # weak: 1001, 1002, 1003, 1004
        self.assertIn(1001, nm_ids)
        self.assertIn(1002, nm_ids)
        self.assertIn(1003, nm_ids)
        self.assertIn(1004, nm_ids)
        # not weak: 1005, 1006
        self.assertNotIn(1005, nm_ids)
        self.assertNotIn(1006, nm_ids)

    def test_inactive_product_excluded(self):
        """Inactive products (is_active=False) are excluded even if weak."""
        res = self._run(seller_id=1, limit=30)
        nm_ids = {c['nm_id'] for c in res['candidates']}
        self.assertNotIn(1007, nm_ids)

    def test_seller_scoped(self):
        """Results are scoped to the requested seller — seller 2's products never appear."""
        res = self._run(seller_id=1, limit=30)
        nm_ids = {c['nm_id'] for c in res['candidates']}
        self.assertNotIn(2001, nm_ids)

    def test_ordered_by_quality_impact_desc(self):
        """Candidates are ordered by quality_impact descending (highest-impact fix first)."""
        res = self._run(seller_id=1, limit=30)
        nm_ids = [c['nm_id'] for c in res['candidates']]
        # impacts: 1001=35, 1002=80, 1003=50, 1004=65 -> desc order by impact
        self.assertEqual(nm_ids, [1002, 1004, 1003, 1001])

    def test_total_weak_vs_shown_when_more_than_limit(self):
        """When total_weak > limit, shown == limit and total_weak reports full count."""
        # Add more weak products to seller 1 so we exceed limit=3
        db.session.add_all([
            _product(1, nm_id=1010, quality_score=10.0,
                     attention_reasons='weak_title', quality_impact=10.0),
            _product(1, nm_id=1011, quality_score=11.0,
                     attention_reasons='weak_title', quality_impact=11.0),
            _product(1, nm_id=1012, quality_score=12.0,
                     attention_reasons='weak_title', quality_impact=12.0),
        ])
        db.session.commit()

        # Now seller 1 has 7 weak products (1001,1002,1003,1004,1010,1011,1012)
        res = self._run(seller_id=1, limit=3)
        self.assertEqual(res['shown'], 3)
        self.assertGreater(res['total_weak'], 3)
        self.assertEqual(len(res['candidates']), 3)

    def test_all_returned_when_fewer_than_limit(self):
        """When weak count <= limit, shown == total_weak."""
        res = self._run(seller_id=1, limit=30)
        # 4 weak products for seller 1 (1001,1002,1003,1004)
        self.assertEqual(res['shown'], 4)
        self.assertEqual(res['total_weak'], 4)
        self.assertEqual(res['shown'], len(res['candidates']))

    def test_candidate_dict_shape(self):
        """Each candidate has the required keys."""
        res = self._run(seller_id=1, limit=30)
        for c in res['candidates']:
            self.assertIn('product_id', c)
            self.assertIn('nm_id', c)
            self.assertIn('quality_score', c)
            self.assertIn('weak_dims', c)
            self.assertIn('has_supplier', c)
            self.assertIn('supplier_diff', c)

    def test_supplier_diff_populated_when_available(self):
        """When enrichment service returns data, supplier_diff is set and has_supplier=True."""
        from routes.card_quality import _collect_bulk_candidates
        with patch('routes.card_quality.card_quality_detail') as mock_detail, \
             patch('routes.card_quality.collect_weak_dimensions', return_value=['title']), \
             patch('routes.card_quality.get_enrichment_service') as mock_es:

            def _detail(p):
                return {'product_id': p.id, 'nm_id': p.nm_id,
                        'quality_score': p.quality_score,
                        'title': p.title, 'vendor_code': p.vendor_code}
            mock_detail.side_effect = _detail

            svc = MagicMock()
            svc.find_supplier_data.return_value = MagicMock()
            svc.build_preview.return_value = {
                'title': {'current': 'Old', 'supplier': 'New', 'has_change': True}
            }
            mock_es.return_value = svc

            res = _collect_bulk_candidates(seller_id=1, limit=30)
            for c in res['candidates']:
                self.assertTrue(c['has_supplier'])
                self.assertEqual(c['supplier_diff']['title']['supplier'], 'New')


if __name__ == '__main__':
    unittest.main()
