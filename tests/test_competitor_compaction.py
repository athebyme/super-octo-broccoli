# -*- coding: utf-8 -*-
"""Компакция снимков: чанки, NULL-мусор, подряд-дубли, ретеншн алертов."""
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from flask import Flask

from models import (
    CompetitorAlert, CompetitorGroup, CompetitorPriceSnapshot,
    CompetitorProduct, Seller, User, db,
)
from services import competitor_monitor as cm


class CompactionTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(
            TESTING=True, SQLALCHEMY_DATABASE_URI='sqlite://',
            SQLALCHEMY_TRACK_MODIFICATIONS=False)
        db.init_app(self.app)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        user = User(username='cmp-user', email='cmp@test.local', is_active=True)
        user.set_password('synthetic-password')
        self.seller = Seller(user=user, company_name='CmpShop')
        db.session.add(self.seller)
        db.session.flush()
        self.group = CompetitorGroup(seller_id=self.seller.id, name='G')
        db.session.add(self.group)
        db.session.flush()
        self.product = CompetitorProduct(
            seller_id=self.seller.id, group_id=self.group.id, nm_id=1)
        db.session.add(self.product)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _snap(self, ts, price=None, sale=None, stock=None, rating=None):
        s = CompetitorPriceSnapshot(
            product_id=self.product.id, seller_id=self.seller.id,
            price=price, sale_price=sale, total_stock=stock, rating=rating,
            created_at=ts)
        db.session.add(s)
        return s

    def test_all_null_snapshots_deleted(self):
        base = datetime.utcnow() - timedelta(days=1)
        for i in range(10):
            self._snap(base + timedelta(minutes=i))          # мусор
        self._snap(base + timedelta(hours=1), price=100, sale=90, stock=5)
        db.session.commit()
        out = cm.compact_competitor_snapshots(self.app)
        self.assertEqual(out['deleted_null'], 10)
        self.assertEqual(CompetitorPriceSnapshot.query.count(), 1)

    def test_consecutive_duplicates_deleted_null_safe(self):
        base = datetime.utcnow() - timedelta(days=1)
        self._snap(base, price=100, sale=90, stock=5)
        self._snap(base + timedelta(minutes=1), price=100, sale=90, stock=5)   # дубль
        self._snap(base + timedelta(minutes=2), price=100, sale=90, stock=5)   # дубль
        self._snap(base + timedelta(minutes=3), price=100, sale=80, stock=5)   # изменение
        self._snap(base + timedelta(minutes=4), price=100, sale=80, stock=5)   # дубль
        db.session.commit()
        out = cm.compact_competitor_snapshots(self.app)
        self.assertEqual(out['deleted_dup'], 3)
        remaining = CompetitorPriceSnapshot.query.order_by(
            CompetitorPriceSnapshot.created_at).all()
        self.assertEqual([s.sale_price for s in remaining], [90, 80])

    def test_read_alerts_retention(self):
        old = CompetitorAlert(
            seller_id=self.seller.id, alert_type='price_drop', severity='info',
            message='x', is_read=True,
            created_at=datetime.utcnow() - timedelta(days=91))
        fresh_read = CompetitorAlert(
            seller_id=self.seller.id, alert_type='price_drop', severity='info',
            message='y', is_read=True,
            created_at=datetime.utcnow() - timedelta(days=30))
        old_unread = CompetitorAlert(
            seller_id=self.seller.id, alert_type='price_drop', severity='info',
            message='z', is_read=False,
            created_at=datetime.utcnow() - timedelta(days=120))
        db.session.add_all([old, fresh_read, old_unread])
        db.session.commit()
        out = cm.compact_competitor_snapshots(self.app)
        self.assertEqual(out['deleted_alerts'], 1)
        self.assertEqual(CompetitorAlert.query.count(), 2)

    def test_time_budget_returns_incomplete(self):
        base = datetime.utcnow() - timedelta(days=1)
        for i in range(50):
            self._snap(base + timedelta(seconds=i))
        db.session.commit()
        out = cm.compact_competitor_snapshots(self.app, max_seconds=0)
        self.assertFalse(out['complete'])


class OneShotMigrationTest(unittest.TestCase):
    def test_migration_cleans_null_and_dups(self):
        tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        tmp.close()
        try:
            con = sqlite3.connect(tmp.name)
            con.executescript("""
                CREATE TABLE competitor_price_snapshots (
                    id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL,
                    seller_id INTEGER NOT NULL, price INTEGER,
                    sale_price INTEGER, rating FLOAT, feedbacks_count INTEGER,
                    total_stock INTEGER, price_change_percent FLOAT,
                    created_at DATETIME);
            """)
            rows = []
            # 100 all-NULL + пары дублей + значащие переходы
            for i in range(100):
                rows.append((1, 1, None, None, None, None,
                             f'2026-05-01 10:{i // 60:02d}:{i % 60:02d}'))
            rows += [
                (1, 1, 100, 90, 4.5, 5, '2026-05-02 10:00:00'),
                (1, 1, 100, 90, 4.5, 5, '2026-05-02 11:00:00'),   # дубль
                (1, 1, 100, 80, 4.5, 5, '2026-05-02 12:00:00'),   # переход
            ]
            con.executemany(
                'INSERT INTO competitor_price_snapshots '
                '(product_id, seller_id, price, sale_price, rating, '
                ' total_stock, created_at) '
                'VALUES (?,?,?,?,?,?,?)', rows)
            con.commit()
            con.close()

            from migrations.migrate_compact_competitor_snapshots import migrate
            self.assertTrue(migrate(tmp.name))
            con = sqlite3.connect(tmp.name)
            count = con.execute(
                'SELECT COUNT(*) FROM competitor_price_snapshots').fetchone()[0]
            con.close()
            self.assertEqual(count, 2)  # остались только значащие переходы
            # идемпотентность
            self.assertTrue(migrate(tmp.name))
        finally:
            Path(tmp.name).unlink(missing_ok=True)


if __name__ == '__main__':
    unittest.main()
