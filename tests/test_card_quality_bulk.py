# -*- coding: utf-8 -*-
"""Real-DB tests for _collect_bulk_candidates (bulk «Улучшить слабые» flow)."""

import json
import os
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


class TestParseIdsParam(unittest.TestCase):
    """Unit tests for the _parse_ids_param helper (GET ?ids=1,2,3 parsing)."""

    def _parse(self, raw, limit=30):
        from routes.card_quality import _parse_ids_param
        return _parse_ids_param(raw, limit)

    def test_parses_comma_separated_digits(self):
        self.assertEqual(self._parse('1,2,3'), [1, 2, 3])

    def test_trims_whitespace_around_chunks(self):
        self.assertEqual(self._parse(' 1 , 2 ,3'), [1, 2, 3])

    def test_ignores_non_digit_chunks(self):
        self.assertEqual(self._parse('1,abc,3'), [1, 3])

    def test_empty_string_returns_none(self):
        self.assertIsNone(self._parse(''))

    def test_none_input_returns_none(self):
        self.assertIsNone(self._parse(None))

    def test_only_non_digit_chunks_returns_none(self):
        self.assertIsNone(self._parse('a,b,c'))

    def test_caps_at_limit(self):
        self.assertEqual(self._parse('1,2,3,4,5', limit=2), [1, 2])


class TestBulkImprovePageIdsParam(unittest.TestCase):
    """HTTP-level tests: GET /card-quality/bulk-improve?ids=... wiring + tenant scope."""

    @classmethod
    def setUpClass(cls):
        os.environ['DISABLE_SECURE_COOKIE'] = '1'
        import sqlalchemy as _sa
        from sqlalchemy.pool import StaticPool
        import seller_platform  # noqa
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
        from models import User, Seller

        user = User(username='bulkseller1', email='bulkseller1@example.com', password_hash='x')
        cls.db.session.add(user)
        cls.db.session.flush()
        seller = Seller(user_id=user.id, company_name='ООО Тест', wb_seller_id='321')
        seller.wb_api_key = 'test-api-key'
        cls.db.session.add(seller)
        cls.db.session.flush()
        cls.user_id = user.id
        cls.seller_id = seller.id

        other_user = User(username='bulkseller2', email='bulkseller2@example.com', password_hash='x')
        cls.db.session.add(other_user)
        cls.db.session.flush()
        other_seller = Seller(user_id=other_user.id, company_name='ООО Чужой', wb_seller_id='654')
        other_seller.wb_api_key = 'other-api-key'
        cls.db.session.add(other_seller)
        cls.db.session.flush()
        cls.other_seller_id = other_seller.id

        p1 = _product(seller.id, nm_id=5001, attention_reasons='weak_chars', quality_impact=10.0)
        p2 = _product(seller.id, nm_id=5002, attention_reasons='low_rating', quality_impact=20.0)
        p3 = _product(seller.id, nm_id=5003, attention_reasons='weak_description', quality_impact=30.0)
        other = _product(other_seller.id, nm_id=6001, attention_reasons='low_rating', quality_impact=99.0)
        cls.db.session.add_all([p1, p2, p3, other])
        cls.db.session.commit()
        cls.p1_id, cls.p2_id, cls.p3_id = p1.id, p2.id, p3.id
        cls.other_id = other.id

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

    def _get(self, client, ids):
        with patch('routes.card_quality.render_template', return_value='OK') as mock_render, \
             patch('routes.card_quality.card_quality_detail') as mock_detail, \
             patch('routes.card_quality.collect_weak_dimensions', return_value=[]), \
             patch('routes.card_quality.get_enrichment_service') as mock_es:

            def _detail(p):
                return {'product_id': p.id, 'nm_id': p.nm_id,
                        'quality_score': p.quality_score, 'title': p.title,
                        'vendor_code': p.vendor_code}
            mock_detail.side_effect = _detail
            svc = MagicMock()
            svc.find_supplier_data.return_value = None
            mock_es.return_value = svc

            resp = client.get(f'/card-quality/bulk-improve?ids={ids}')
            return resp, mock_render

    def test_ids_param_selects_exactly_own_two_cards(self):
        client = self._client_logged_in()
        resp, mock_render = self._get(client, f'{self.p1_id},{self.p2_id}')
        self.assertEqual(resp.status_code, 200)
        candidates = mock_render.call_args.kwargs['candidates']
        nm_ids = {c['nm_id'] for c in candidates}
        self.assertEqual(nm_ids, {5001, 5002})

    def test_foreign_product_id_in_ids_is_dropped(self):
        client = self._client_logged_in()
        resp, mock_render = self._get(client, f'{self.p1_id},{self.other_id}')
        self.assertEqual(resp.status_code, 200)
        candidates = mock_render.call_args.kwargs['candidates']
        nm_ids = {c['nm_id'] for c in candidates}
        self.assertEqual(nm_ids, {5001})
        self.assertNotIn(6001, nm_ids)

    def test_without_ids_falls_back_to_top_impact(self):
        client = self._client_logged_in()
        resp, mock_render = self._get(client, '')
        self.assertEqual(resp.status_code, 200)
        candidates = mock_render.call_args.kwargs['candidates']
        nm_ids = {c['nm_id'] for c in candidates}
        # Prior behaviour preserved: all own weak cards returned (top-impact), none foreign.
        self.assertEqual(nm_ids, {5001, 5002, 5003})


if __name__ == '__main__':
    unittest.main()
