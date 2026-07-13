# -*- coding: utf-8 -*-
"""Тест сводки качества карточек v2: причины, распределение, тренд."""
import json
import unittest
from datetime import datetime, timedelta

from flask import Flask

from models import db, Product, CardRatingHistory
from services.card_quality_scorer import compute_quality_summary, REASON_LABELS


def _make_app():
    app = Flask(__name__)
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    db.init_app(app)
    return app


def _product(seller_id, nm_id, quality_score, nm_rating=None, reasons=''):
    return Product(
        seller_id=seller_id, nm_id=nm_id, title='Товар',
        photos_json=json.dumps([]), characteristics_json=json.dumps({}),
        quality_score=quality_score, nm_rating=nm_rating,
        attention_reasons=reasons, is_active=True,
    )


class TestQualitySummaryV2(unittest.TestCase):
    def setUp(self):
        self.app = _make_app()
        self.ctx = self.app.app_context(); self.ctx.push()
        db.create_all()
        db.session.add_all([
            _product(1, 101, 40.0, reasons='few_photos,weak_chars'),
            _product(1, 102, 65.0, reasons='no_views'),
            _product(1, 103, 90.0, reasons=''),
            _product(1, 104, None, reasons='no_sales_signal'),
            _product(2, 201, 30.0, reasons='few_photos'),  # чужой продавец
        ])
        now = datetime.utcnow()
        for d, score in ((2, 60.0), (1, 65.0), (0, 70.0)):
            db.session.add(CardRatingHistory(
                seller_id=1, nm_id=101, quality_score=score,
                captured_at=now - timedelta(days=d)))
        db.session.commit()

    def tearDown(self):
        db.session.remove(); db.drop_all(); self.ctx.pop()

    def test_need_attention_by_reasons(self):
        s = compute_quality_summary(1)
        self.assertEqual(s['need_attention'], 3)
        self.assertEqual(s['total'], 4)

    def test_reason_counts(self):
        s = compute_quality_summary(1)
        self.assertEqual(s['reason_counts']['few_photos'], 1)
        self.assertEqual(s['reason_counts']['weak_chars'], 1)
        self.assertEqual(s['reason_counts']['no_views'], 1)
        self.assertEqual(s['reason_counts']['no_sales_signal'], 1)
        self.assertEqual(s['reason_counts']['low_rating'], 0)

    def test_reason_labels_passthrough(self):
        self.assertEqual(compute_quality_summary(1)['reason_labels'], REASON_LABELS)

    def test_distribution_unchanged(self):
        s = compute_quality_summary(1)
        self.assertEqual(s['distribution'], {'poor': 1, 'average': 1, 'good': 0, 'excellent': 1})

    def test_trend_daily_avg(self):
        s = compute_quality_summary(1)
        self.assertEqual(len(s['trend']), 3)
        self.assertEqual(s['trend'][-1]['avg_quality'], 70.0)

    def test_tenant_scope(self):
        s = compute_quality_summary(2)
        self.assertEqual(s['need_attention'], 1)
        self.assertEqual(s['total'], 1)


class TestIsWeakRemoved(unittest.TestCase):
    def test_is_weak_gone(self):
        import services.card_quality_scorer as scorer
        self.assertFalse(hasattr(scorer, 'is_weak'))
