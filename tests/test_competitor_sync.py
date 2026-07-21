# -*- coding: utf-8 -*-
"""Sync-ядро v2: честные наблюдения, снимки только при изменении, алерты."""
import unittest
import unittest.mock
from datetime import datetime, timedelta
from unittest.mock import MagicMock

from flask import Flask

from models import (
    CompetitorAlert, CompetitorGroup, CompetitorMonitorSettings,
    CompetitorPriceSnapshot, CompetitorProduct, Seller, User, db,
)
from services import competitor_fetch as cf
from services import competitor_monitor as cm


class SyncTestBase(unittest.TestCase):
    def setUp(self):
        cf.clear_observation_cache()
        self.app = Flask(__name__)
        self.app.config.update(
            TESTING=True,
            SQLALCHEMY_DATABASE_URI='sqlite://',
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
        )
        db.init_app(self.app)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        user = User(username='sync-user', email='sync@test.local', is_active=True)
        user.set_password('synthetic-password')
        self.seller = Seller(user=user, company_name='SyncShop')
        db.session.add(self.seller)
        db.session.flush()
        self.settings = CompetitorMonitorSettings(
            seller_id=self.seller.id, is_enabled=True,
            price_change_alert_percent=5.0, discount_alert_pp=5.0,
            sync_interval_minutes=60)
        self.group = CompetitorGroup(seller_id=self.seller.id, name='G1')
        db.session.add_all([self.settings, self.group])
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _product(self, nm_id=111, **kwargs):
        defaults = dict(
            seller_id=self.seller.id, group_id=self.group.id, nm_id=nm_id,
            title='Товар', brand='BrandX', wb_supplier_id=500,
            metadata_synced_at=datetime.utcnow(),
            current_price=None, current_sale_price=None,
            price_miss_count=0, fetch_error_count=0)
        defaults.update(kwargs)
        p = CompetitorProduct(**defaults)
        db.session.add(p)
        db.session.commit()
        return p

    def _fetch_mock(self, supplier_prices=None, brand_prices=None, metadata=None):
        svc = MagicMock()
        svc.fetch_supplier_prices.return_value = supplier_prices or {}
        svc.fetch_brand_prices.return_value = brand_prices or {}
        svc.fetch_basket_metadata.side_effect = (
            lambda nm: (metadata or {}).get(nm))
        svc.fetch_seller_catalog_page.return_value = []
        return svc

    def _obs(self, price=2000, sale=1500, stock=10, rating=4.5):
        return {'price': price, 'sale_price': sale, 'rating': rating,
                'feedbacks_count': 5, 'total_stock': stock}


class ObservationPolicyTest(SyncTestBase):
    def test_first_observation_creates_snapshot(self):
        p = self._product()
        svc = self._fetch_mock(supplier_prices={111: self._obs()})
        result = cm.sync_seller_competitors(
            self.seller.id, self.app, fetch_service=svc)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['snapshots'], 1)
        db.session.refresh(p)
        self.assertEqual(p.current_sale_price, 1500)
        self.assertEqual(p.price_miss_count, 0)
        self.assertIsNotNone(p.last_price_at)

    def test_unchanged_observation_no_snapshot(self):
        self._product(current_price=2000, current_sale_price=1500,
                      current_total_stock=10, current_rating=4.5,
                      last_price_at=datetime.utcnow())
        svc = self._fetch_mock(supplier_prices={111: self._obs()})
        result = cm.sync_seller_competitors(
            self.seller.id, self.app, fetch_service=svc)
        self.assertEqual(result['snapshots'], 0)
        self.assertEqual(CompetitorPriceSnapshot.query.count(), 0)

    def test_miss_does_not_null_current_and_no_snapshot(self):
        p = self._product(current_price=2000, current_sale_price=1500,
                          last_price_at=datetime.utcnow() - timedelta(hours=2))
        svc = self._fetch_mock()  # источники ничего не вернули
        result = cm.sync_seller_competitors(
            self.seller.id, self.app, fetch_service=svc)
        db.session.refresh(p)
        self.assertEqual(p.current_sale_price, 1500)      # не затёрто
        self.assertEqual(p.price_miss_count, 1)
        self.assertEqual(CompetitorPriceSnapshot.query.count(), 0)
        self.assertEqual(result['misses'], 1)

    def test_changed_price_snapshot_and_alert(self):
        self._product(current_price=2000, current_sale_price=1500,
                      current_total_stock=10, current_rating=4.5)
        svc = self._fetch_mock(supplier_prices={111: self._obs(sale=1200)})  # -20%
        result = cm.sync_seller_competitors(
            self.seller.id, self.app, fetch_service=svc)
        self.assertEqual(result['snapshots'], 1)
        alerts = CompetitorAlert.query.all()
        self.assertTrue(any(a.alert_type == 'price_drop' for a in alerts))
        snap = CompetitorPriceSnapshot.query.one()
        self.assertEqual(snap.sale_price, 1200)
        self.assertAlmostEqual(snap.price_change_percent, -20.0)

    def test_gone_product_deactivates_after_threshold(self):
        p = self._product(price_miss_count=cm.PRICE_MISS_RECHECK_THRESHOLD,
                          fetch_error_count=cm.DEACTIVATE_AFTER_GONE - 1)
        svc = self._fetch_mock(metadata={111: 'gone'})
        cm.sync_seller_competitors(self.seller.id, self.app, fetch_service=svc)
        db.session.refresh(p)
        self.assertFalse(p.is_active)

    def test_stale_metadata_refreshed(self):
        p = self._product(metadata_synced_at=datetime.utcnow() - timedelta(days=8))
        meta = {'nm_id': 111, 'title': 'Новое имя', 'brand': 'B',
                'supplier_name': 'S', 'wb_supplier_id': 500,
                'image_url': 'http://x', 'is_adult': True,
                'subject_name': 'Категория'}
        svc = self._fetch_mock(metadata={111: meta},
                               supplier_prices={111: self._obs()})
        cm.sync_seller_competitors(self.seller.id, self.app, fetch_service=svc)
        db.session.refresh(p)
        self.assertEqual(p.title, 'Новое имя')
        self.assertTrue(p.is_adult)
        self.assertIsNotNone(p.metadata_synced_at)

    def test_settings_updated_and_next_due_scheduled(self):
        self._product()
        svc = self._fetch_mock(supplier_prices={111: self._obs()})
        before = datetime.utcnow()
        cm.sync_seller_competitors(self.seller.id, self.app, fetch_service=svc)
        db.session.refresh(self.settings)
        self.assertEqual(self.settings.last_sync_status, 'success')
        self.assertFalse(self.settings.is_running)
        self.assertGreaterEqual(
            self.settings.next_sync_due_at,
            before + timedelta(minutes=59))

    def test_disabled_seller_skipped(self):
        self.settings.is_enabled = False
        db.session.commit()
        svc = self._fetch_mock()
        result = cm.sync_seller_competitors(
            self.seller.id, self.app, fetch_service=svc)
        self.assertEqual(result['status'], 'disabled')
        svc.fetch_supplier_prices.assert_not_called()


class AlertRulesTest(SyncTestBase):
    def test_discount_alert_pp_threshold(self):
        p = self._product(current_price=2000, current_sale_price=1800)  # скидка 10%
        obs = self._obs(price=2000, sale=1500)  # скидка 25% => +15 п.п.
        alerts = cm._generate_alerts(p, obs, self.settings)
        self.assertTrue(any(a.alert_type == 'discount_increase' for a in alerts))

    def test_out_of_stock_and_back(self):
        p = self._product(current_total_stock=5, current_price=100,
                          current_sale_price=90)
        alerts = cm._generate_alerts(
            p, self._obs(price=100, sale=90, stock=0), self.settings)
        self.assertTrue(any(a.alert_type == 'out_of_stock' for a in alerts))
        p.current_total_stock = 0
        alerts = cm._generate_alerts(
            p, self._obs(price=100, sale=90, stock=7), self.settings)
        self.assertTrue(any(a.alert_type == 'back_in_stock' for a in alerts))

    def test_below_threshold_no_alert(self):
        p = self._product(current_price=2000, current_sale_price=1500)
        alerts = cm._generate_alerts(
            p, self._obs(price=2000, sale=1470), self.settings)  # -2%
        self.assertEqual([a for a in alerts if 'price' in a.alert_type], [])


class TickTest(SyncTestBase):
    def test_picks_due_sellers_only(self):
        # наш seller due (next_sync_due_at NULL), второй — не due
        user2 = User(username='tick-user2', email='tick2@test.local',
                     is_active=True)
        user2.set_password('synthetic-password')
        seller2 = Seller(user=user2, company_name='NotDue')
        db.session.add(seller2)
        db.session.flush()
        db.session.add(CompetitorMonitorSettings(
            seller_id=seller2.id, is_enabled=True,
            next_sync_due_at=datetime.utcnow() + timedelta(hours=1)))
        db.session.commit()
        with unittest.mock.patch.object(cm, 'sync_seller_competitors',
                                        return_value={'status': 'ok'}) as sync:
            out = cm.run_competitor_monitor_tick(self.app)
        self.assertEqual(out['synced'], [self.seller.id])
        sync.assert_called_once()

    def test_disabled_never_picked(self):
        self.settings.is_enabled = False
        db.session.commit()
        with unittest.mock.patch.object(cm, 'sync_seller_competitors') as sync:
            out = cm.run_competitor_monitor_tick(self.app)
        self.assertEqual(out['synced'], [])
        sync.assert_not_called()


class NotificationTest(SyncTestBase):
    def _alert(self, severity='warning'):
        return CompetitorAlert(
            seller_id=self.seller.id, alert_type='price_drop',
            severity=severity, message='x')

    def test_aggregated_notification_created(self):
        with unittest.mock.patch.object(
                cm, '_create_notification_compat') as create:
            cm._notify_new_alerts(
                self.seller.id, [self._alert(), self._alert('critical')])
        create.assert_called_once()
        kwargs = create.call_args.kwargs
        self.assertEqual(kwargs['category'], 'error')      # max severity critical
        self.assertEqual(kwargs['title'], cm.COMPETITOR_NOTIFICATION_TITLE)
        self.assertEqual(kwargs['link'], '/competitors/alerts')

    def test_dedup_within_4h(self):
        from models import Notification
        db.session.add(Notification(
            seller_id=self.seller.id, category='warning',
            title=cm.COMPETITOR_NOTIFICATION_TITLE, message='старое',
            created_at=datetime.utcnow() - timedelta(hours=1)))
        db.session.commit()
        with unittest.mock.patch.object(
                cm, '_create_notification_compat') as create:
            cm._notify_new_alerts(self.seller.id, [self._alert()])
        create.assert_not_called()

    def test_no_alerts_no_notification(self):
        with unittest.mock.patch.object(
                cm, '_create_notification_compat') as create:
            cm._notify_new_alerts(self.seller.id, [])
        create.assert_not_called()


class SellerImportTest(SyncTestBase):
    def test_import_creates_products_and_clears_flag(self):
        self.group.import_requested = True
        self.group.auto_source = 'seller'
        self.group.auto_source_value = '332183'
        db.session.commit()
        page = [{'nm_id': 900 + i, 'title': f'T{i}', 'brand': 'B',
                 'supplier_name': 'S', 'wb_supplier_id': 332183,
                 'image_url': 'http://x', 'price': 100, 'sale_price': 90,
                 'rating': 4.0, 'feedbacks_count': 1, 'total_stock': 5}
                for i in range(30)]  # < 100 => каталог исчерпан
        svc = self._fetch_mock()
        svc.fetch_seller_catalog_page.return_value = page
        cm.sync_seller_competitors(self.seller.id, self.app, fetch_service=svc)
        db.session.refresh(self.group)
        self.assertFalse(self.group.import_requested)
        self.assertEqual(
            CompetitorProduct.query.filter_by(group_id=self.group.id).count(),
            30)
        # у созданных сразу есть метаданные и наблюдение
        p = CompetitorProduct.query.filter_by(nm_id=900).one()
        self.assertEqual(p.current_sale_price, 90)
        self.assertIsNotNone(p.metadata_synced_at)

    def test_import_respects_max_products(self):
        self.group.import_requested = True
        self.group.auto_source_value = '332183'
        db.session.commit()
        full_page = [{'nm_id': 10_000 + i, 'title': 'T', 'brand': 'B',
                      'supplier_name': 'S', 'wb_supplier_id': 332183,
                      'image_url': None, 'price': 100, 'sale_price': 90,
                      'rating': None, 'feedbacks_count': 0, 'total_stock': 1}
                     for i in range(100)]
        svc = self._fetch_mock()
        svc.fetch_seller_catalog_page.side_effect = [
            full_page,
            [dict(x, nm_id=x['nm_id'] + 100) for x in full_page],
            [dict(x, nm_id=x['nm_id'] + 200) for x in full_page],
        ]
        cm.sync_seller_competitors(self.seller.id, self.app, fetch_service=svc)
        self.assertLessEqual(
            CompetitorProduct.query.filter_by(group_id=self.group.id).count(),
            cm.IMPORT_MAX_PRODUCTS)


class NormalizeIntervalTest(unittest.TestCase):
    def test_bounds(self):
        self.assertEqual(cm.normalize_sync_interval_minutes(60), 60)
        self.assertEqual(cm.normalize_sync_interval_minutes(5), 30)
        self.assertEqual(cm.normalize_sync_interval_minutes(999999), 1440)
        self.assertEqual(cm.normalize_sync_interval_minutes('мусор'), 60)
        self.assertEqual(cm.normalize_sync_interval_minutes(None), 60)


if __name__ == '__main__':
    unittest.main()
