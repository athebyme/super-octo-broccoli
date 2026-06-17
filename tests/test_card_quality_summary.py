# -*- coding: utf-8 -*-
"""Тест сводки качества карточек: is_weak + compute_quality_summary."""

import json
import unittest

from flask import Flask

from models import db, Product
from services.card_quality_scorer import (
    is_weak,
    compute_quality_summary,
    WEAK_QUALITY_THRESHOLD,
    WEAK_WB_RATING_THRESHOLD,
)


def _make_app():
    app = Flask(__name__)
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    db.init_app(app)
    return app


def _product(seller_id, quality_score, nm_rating=None, wb_feedback_rating=None):
    return Product(
        seller_id=seller_id,
        nm_id=100000 + int(quality_score * 100) + seller_id,
        title='Товар',
        photos_json=json.dumps([]),
        characteristics_json=json.dumps({}),
        quality_score=quality_score,
        nm_rating=nm_rating,
        wb_feedback_rating=wb_feedback_rating,
        is_active=True,
    )


class TestIsWeak(unittest.TestCase):
    def test_thresholds(self):
        self.assertEqual(WEAK_QUALITY_THRESHOLD, 50.0)
        self.assertEqual(WEAK_WB_RATING_THRESHOLD, 6.0)

    def test_weak_by_quality(self):
        self.assertTrue(is_weak(40.0, 9.0))

    def test_weak_by_rating(self):
        self.assertTrue(is_weak(90.0, 5.5))

    def test_strong_when_both_ok(self):
        self.assertFalse(is_weak(80.0, 8.0))

    def test_none_rating_ignored(self):
        self.assertFalse(is_weak(80.0, None))
        self.assertTrue(is_weak(30.0, None))

    def test_none_quality_ignored(self):
        self.assertFalse(is_weak(None, 8.0))
        self.assertTrue(is_weak(None, 4.0))


class TestComputeQualitySummary(unittest.TestCase):
    def setUp(self):
        self.app = _make_app()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        # seller 1: poor(40,nm=4), average(60,nm=9), good(75,nm=8), excellent(90,nm=10)
        db.session.add_all([
            _product(1, 40.0, nm_rating=4.0, wb_feedback_rating=3.0),
            _product(1, 60.0, nm_rating=9.0, wb_feedback_rating=4.0),
            _product(1, 75.0, nm_rating=8.0, wb_feedback_rating=4.5),
            _product(1, 90.0, nm_rating=10.0, wb_feedback_rating=5.0),
            # другой продавец — не должен попасть в сводку seller=1
            _product(2, 10.0, nm_rating=1.0, wb_feedback_rating=1.0),
        ])
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_averages_scoped_to_seller(self):
        s = compute_quality_summary(1)
        self.assertEqual(s['total'], 4)
        self.assertEqual(s['avg_quality'], round((40 + 60 + 75 + 90) / 4.0, 1))
        self.assertEqual(s['avg_wb_rating'], round((4 + 9 + 8 + 10) / 4.0, 1))

    def test_distribution_buckets(self):
        s = compute_quality_summary(1)
        self.assertEqual(s['distribution'],
                         {'poor': 1, 'average': 1, 'good': 1, 'excellent': 1})

    def test_need_attention_counts_weak(self):
        # weak: poor(40) -> quality<50, average(60,nm=9) ok, good ok, excellent ok
        s = compute_quality_summary(1)
        self.assertEqual(s['need_attention'], 1)

    def test_empty_seller(self):
        s = compute_quality_summary(999)
        self.assertEqual(s['total'], 0)
        self.assertIsNone(s['avg_quality'])
        self.assertIsNone(s['avg_wb_rating'])
        self.assertEqual(s['need_attention'], 0)
        self.assertEqual(s['distribution'],
                         {'poor': 0, 'average': 0, 'good': 0, 'excellent': 0})
