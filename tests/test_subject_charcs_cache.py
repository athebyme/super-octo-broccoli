# -*- coding: utf-8 -*-
"""Тесты кэша конфигов характеристик и scoring context."""
import json
import unittest
from datetime import datetime, timedelta

from flask import Flask

from models import db, Product, WbSubjectCharcsCache
from services.subject_charcs_cache import get_available_charcs, refresh_subject_charcs, CHARCS_TTL_DAYS
from services.card_quality_scorer import build_seller_scoring_context, product_to_card_input


def _make_app():
    app = Flask(__name__)
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    db.init_app(app)
    return app


class _FakeClient:
    def __init__(self):
        self.calls = []

    def get_card_characteristics_config(self, subject_id):
        self.calls.append(subject_id)
        return {'data': [
            {'name': 'Цвет', 'required': True, 'charcID': 1},
            {'name': 'Состав', 'required': False, 'charcID': 2},
        ]}


class TestCharcsCache(unittest.TestCase):
    def setUp(self):
        self.app = _make_app()
        self.ctx = self.app.app_context(); self.ctx.push()
        db.create_all()

    def tearDown(self):
        db.session.remove(); db.drop_all(); self.ctx.pop()

    def test_refresh_and_get(self):
        client = _FakeClient()
        refresh_subject_charcs(client, {5})
        self.assertEqual(client.calls, [5])
        charcs = get_available_charcs(5)
        self.assertEqual(charcs, [{'name': 'Цвет', 'required': True},
                                  {'name': 'Состав', 'required': False}])

    def test_fresh_cache_not_refetched(self):
        client = _FakeClient()
        refresh_subject_charcs(client, {5})
        refresh_subject_charcs(client, {5})
        self.assertEqual(client.calls, [5])  # второй раз из кэша

    def test_stale_cache_refetched(self):
        client = _FakeClient()
        refresh_subject_charcs(client, {5})
        row = db.session.get(WbSubjectCharcsCache, 5)
        row.fetched_at = datetime.utcnow() - timedelta(days=CHARCS_TTL_DAYS + 1)
        db.session.commit()
        refresh_subject_charcs(client, {5})
        self.assertEqual(client.calls, [5, 5])

    def test_wb_error_swallowed(self):
        class Boom:
            def get_card_characteristics_config(self, sid):
                raise RuntimeError('WB down')
        refresh_subject_charcs(Boom(), {7})  # не должно бросить
        self.assertIsNone(get_available_charcs(7))

    def test_get_missing_returns_none(self):
        self.assertIsNone(get_available_charcs(999))
        self.assertIsNone(get_available_charcs(None))

    def test_network_fetch_does_not_flush_pending_writes(self):
        """Сетевые вызовы идут до любой записи: autoflush незакоммиченных
        строк вызывающего кода не должен открывать write-транзакцию на время
        сети (инцидент 2026-07-20, «database is locked»)."""
        product = Product(seller_id=1, nm_id=300, title='Товар',
                          subject_id=9, is_active=True)
        db.session.add(product)
        db.session.commit()
        product.title = 'Изменён до синка'  # незакоммиченная запись вызывающего

        outer = self

        class AssertingClient:
            def __init__(self):
                self.calls = []

            def get_card_characteristics_config(self, sid):
                self.calls.append(sid)
                # Во время сети изменение не сброшено и кэш-строк не создано.
                outer.assertIn(product, db.session.dirty)
                outer.assertFalse(db.session.new)
                return {'data': [{'name': 'Цвет', 'required': True}]}

        client = AssertingClient()
        refresh_subject_charcs(client, {9})
        self.assertEqual(client.calls, [9])
        self.assertEqual(
            get_available_charcs(9), [{'name': 'Цвет', 'required': True}])


class TestScoringContext(unittest.TestCase):
    def setUp(self):
        self.app = _make_app()
        self.ctx = self.app.app_context(); self.ctx.push()
        db.create_all()
        long_desc = 'Одинаковое описание товара достаточной длины для проверки ' * 3
        for i in range(3):
            db.session.add(Product(seller_id=1, nm_id=100 + i, title='Товар',
                                   description=long_desc, subject_id=5, is_active=True))
        db.session.add(Product(seller_id=1, nm_id=200, title='Товар',
                               description='Уникальное описание достаточной длины для теста дубликатов ок', subject_id=5, is_active=True))
        db.session.add(WbSubjectCharcsCache(
            subject_id=5,
            charcs_json=json.dumps([{'name': 'Цвет', 'required': True}]),
            fetched_at=datetime.utcnow()))
        db.session.commit()
        self.long_desc = long_desc

    def tearDown(self):
        db.session.remove(); db.drop_all(); self.ctx.pop()

    def test_context_marks_duplicates_and_charcs(self):
        ctx = build_seller_scoring_context(1)
        self.assertIn(self.long_desc.strip(), ctx['dup_descriptions'])
        self.assertEqual(ctx['charcs_by_subject'][5], [{'name': 'Цвет', 'required': True}])

    def test_card_input_gets_context_fields(self):
        ctx = build_seller_scoring_context(1)
        dup = Product.query.filter_by(nm_id=100).first()
        card = product_to_card_input(dup, ctx)
        self.assertTrue(card['description_dup'])
        self.assertEqual(card['available_charcs'], [{'name': 'Цвет', 'required': True}])
        uniq = Product.query.filter_by(nm_id=200).first()
        self.assertFalse(product_to_card_input(uniq, ctx)['description_dup'])

    def test_recompute_persists_reasons_and_impact(self):
        from services.card_quality_scorer import recompute_and_persist
        p = Product.query.filter_by(nm_id=100).first()
        cq = recompute_and_persist(p, capture_history=False,
                                   context=build_seller_scoring_context(1))
        self.assertIsInstance(cq['reasons'], list)
        self.assertIn('few_photos', p.attention_reasons)
        self.assertIsNotNone(p.quality_impact)
