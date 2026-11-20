"""Focused local regressions for the WB analytics snapshot refresh boundary."""

from datetime import datetime, timedelta
import unittest
from unittest.mock import patch

from flask import Flask
from flask_login import LoginManager

from models import AnalyticsSnapshot, ProductAnalytics, Seller, User, db
from routes.analytics import register_analytics_routes
from services.analytics_service import (
    AnalyticsRefreshError,
    AnalyticsService,
)
from services.wb_api_client import WBAuthException


class FakeWBClient:
    def __init__(self, *, result=None, failure=None):
        self.result = result
        self.failure = failure
        self.calls = 0
        self.closed = False

    def get_sales_funnel_products(self, **kwargs):
        self.calls += 1
        if self.failure:
            raise self.failure
        return self.result

    def close(self):
        self.closed = True


def products_response(nm_id=101, order_sum=321, order_count=3):
    return {
        'data': {
            'products': [{
                'product': {'nmId': nm_id, 'title': 'Synthetic product'},
                'statistic': {
                    'selected': {
                        'orderSum': order_sum,
                        'orderCount': order_count,
                        'buyoutCount': 2,
                        'buyoutSum': 200,
                        'cancelCount': 0,
                        'cancelSum': 0,
                        'openCount': 10,
                        'cartCount': 4,
                        'conversions': {},
                    },
                    'comparison': {
                        'orderSum': 100,
                        'orderCount': 1,
                        'buyoutCount': 1,
                    },
                },
            }],
        },
    }


class WBAnalyticsRefreshTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(
            TESTING=True,
            SECRET_KEY='synthetic-analytics-tests',
            SQLALCHEMY_DATABASE_URI='sqlite://',
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
        )
        db.init_app(self.app)
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()

        self.login_manager = LoginManager(self.app)
        self.login_manager.user_loader(self._load_user)
        user = User(username='analytics-seller', email='analytics@test.local')
        user.set_password('synthetic-password')
        self.seller = Seller(user=user, company_name='Synthetic seller')
        self.seller._wb_api_key_encrypted = 'synthetic-key'
        other_user = User(username='analytics-other', email='other@test.local')
        other_user.set_password('synthetic-password')
        self.other_seller = Seller(user=other_user, company_name='Other seller')
        db.session.add_all([user, self.seller, other_user, self.other_seller])
        db.session.commit()

        register_analytics_routes(self.app)
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session['_user_id'] = str(user.id)
            session['_fresh'] = True

        self.period_start, self.period_end = AnalyticsService._calc_period('30d')

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def _load_user(self, user_id):
        return db.session.get(User, int(user_id))

    def _add_snapshot(self, seller_id, start, end, revenue, *, created_at=None):
        row = AnalyticsSnapshot(
            seller_id=seller_id,
            period_start=start,
            period_end=end,
            revenue=revenue,
            orders_count=1,
            top_products=[],
            created_at=created_at or datetime.utcnow(),
        )
        db.session.add(row)
        return row

    def _add_product(self, seller_id, start, end, nm_id, title):
        row = ProductAnalytics(
            seller_id=seller_id,
            nm_id=nm_id,
            period_start=start,
            period_end=end,
            title=title,
            orders_sum=1,
        )
        db.session.add(row)
        return row

    def test_provider_denial_preserves_exact_projection_and_neighbors(self):
        old = self._add_snapshot(
            self.seller.id, self.period_start, self.period_end, 111,
        )
        other_period_start = self.period_start - timedelta(days=31)
        other_period_end = self.period_end - timedelta(days=31)
        other_period = self._add_snapshot(
            self.seller.id, other_period_start, other_period_end, 222,
        )
        other_seller = self._add_snapshot(
            self.other_seller.id, self.period_start, self.period_end, 333,
        )
        old_detail = self._add_product(
            self.seller.id, self.period_start, self.period_end, 11, 'old',
        )
        other_period_detail = self._add_product(
            self.seller.id, other_period_start, other_period_end, 22, 'other-period',
        )
        other_seller_detail = self._add_product(
            self.other_seller.id, self.period_start, self.period_end, 33, 'other-seller',
        )
        db.session.commit()

        fake = FakeWBClient(failure=WBAuthException('synthetic private detail'))
        with patch.object(AnalyticsService, '_get_wb_client', return_value=fake):
            with self.assertRaises(AnalyticsRefreshError) as raised:
                AnalyticsService.fetch_and_cache_snapshot(
                    self.seller, '30d', force=True,
                )

        self.assertEqual(raised.exception.code, 'provider_unavailable')
        self.assertTrue(fake.closed)
        self.assertEqual(
            AnalyticsService.get_latest_snapshot(
                self.seller.id, self.period_start, self.period_end,
            ).id,
            old.id,
        )
        self.assertEqual(AnalyticsSnapshot.query.count(), 3)
        self.assertEqual(ProductAnalytics.query.count(), 3)
        self.assertEqual(
            {row.id for row in ProductAnalytics.query.all()},
            {old_detail.id, other_period_detail.id, other_seller_detail.id},
        )
        self.assertEqual(
            {row.id for row in (other_period, other_seller)},
            {row.id for row in AnalyticsSnapshot.query.all() if row.id != old.id},
        )

    def test_malformed_canonical_products_does_not_fall_through_or_write(self):
        old = self._add_snapshot(
            self.seller.id, self.period_start, self.period_end, 111,
        )
        old_detail = self._add_product(
            self.seller.id, self.period_start, self.period_end, 11, 'old',
        )
        db.session.commit()

        fake = FakeWBClient(result={
            'data': {'products': None},
            'products': products_response()['data']['products'],
        })
        with patch.object(AnalyticsService, '_get_wb_client', return_value=fake):
            with self.assertRaises(AnalyticsRefreshError) as raised:
                AnalyticsService.fetch_and_cache_snapshot(
                    self.seller, '30d', force=True,
                )

        self.assertEqual(raised.exception.code, 'invalid_provider_response')
        self.assertEqual(
            AnalyticsService.get_latest_snapshot(
                self.seller.id, self.period_start, self.period_end,
            ).id,
            old.id,
        )
        self.assertEqual([row.id for row in ProductAnalytics.query.all()], [old_detail.id])

    def test_non_forced_success_keeps_prior_snapshot_history(self):
        old = self._add_snapshot(
            self.seller.id,
            self.period_start,
            self.period_end,
            111,
            created_at=datetime.utcnow() - timedelta(hours=6),
        )
        self._add_product(
            self.seller.id, self.period_start, self.period_end, 11, 'old',
        )
        db.session.commit()
        fake = FakeWBClient(result=products_response(nm_id=202, order_sum=654))

        with patch.object(AnalyticsService, '_get_wb_client', return_value=fake):
            fresh = AnalyticsService.fetch_and_cache_snapshot(
                self.seller, '30d', force=False,
            )

        snapshots = AnalyticsSnapshot.query.filter_by(
            seller_id=self.seller.id,
            period_start=self.period_start,
            period_end=self.period_end,
        ).all()
        details = ProductAnalytics.query.filter_by(
            seller_id=self.seller.id,
            period_start=self.period_start,
            period_end=self.period_end,
        ).all()
        self.assertEqual(len(snapshots), 2)
        self.assertIn(old.id, {row.id for row in snapshots})
        self.assertEqual(fresh['kpi']['revenue'], 654)
        self.assertEqual([row.nm_id for row in details], [202])

    def test_forced_success_replaces_only_exact_snapshot_scope(self):
        old = self._add_snapshot(
            self.seller.id, self.period_start, self.period_end, 111,
        )
        other_period_start = self.period_start - timedelta(days=31)
        other_period_end = self.period_end - timedelta(days=31)
        other_period = self._add_snapshot(
            self.seller.id, other_period_start, other_period_end, 222,
        )
        other_seller = self._add_snapshot(
            self.other_seller.id, self.period_start, self.period_end, 333,
        )
        old_detail = self._add_product(
            self.seller.id, self.period_start, self.period_end, 11, 'old',
        )
        other_period_detail = self._add_product(
            self.seller.id, other_period_start, other_period_end, 22, 'other-period',
        )
        other_seller_detail = self._add_product(
            self.other_seller.id, self.period_start, self.period_end, 33, 'other-seller',
        )
        db.session.commit()
        old_id = old.id
        other_period_id = other_period.id
        other_seller_id = other_seller.id
        old_detail_id = old_detail.id
        other_period_detail_id = other_period_detail.id
        other_seller_detail_id = other_seller_detail.id
        fake = FakeWBClient(result=products_response(nm_id=202, order_sum=654))

        with patch.object(AnalyticsService, '_get_wb_client', return_value=fake):
            fresh = AnalyticsService.fetch_and_cache_snapshot(
                self.seller, '30d', force=True,
            )

        snapshots = AnalyticsSnapshot.query.all()
        details = ProductAnalytics.query.all()
        self.assertEqual(len(snapshots), 3)
        self.assertNotIn(old_id, {row.id for row in snapshots})
        self.assertIn(other_period_id, {row.id for row in snapshots})
        self.assertIn(other_seller_id, {row.id for row in snapshots})
        self.assertEqual(fresh['kpi']['revenue'], 654)
        self.assertEqual(len(details), 3)
        new_detail = next(row for row in details if row.nm_id == 202)
        self.assertEqual(
            {row.id for row in details},
            {other_period_detail_id, other_seller_detail_id, new_detail.id},
        )
        self.assertNotIn(old_detail_id, {row.id for row in details})

    def test_nonempty_product_with_no_numeric_observations_is_rejected(self):
        old = self._add_snapshot(
            self.seller.id, self.period_start, self.period_end, 111,
        )
        db.session.commit()
        malformed = products_response()
        malformed['data']['products'][0]['statistic']['selected'] = {}
        fake = FakeWBClient(result=malformed)

        with patch.object(AnalyticsService, '_get_wb_client', return_value=fake):
            with self.assertRaises(AnalyticsRefreshError) as raised:
                AnalyticsService.fetch_and_cache_snapshot(
                    self.seller, '30d', force=True,
                )

        self.assertEqual(raised.exception.code, 'invalid_provider_response')
        self.assertEqual(
            AnalyticsService.get_latest_snapshot(
                self.seller.id, self.period_start, self.period_end,
            ).id,
            old.id,
        )

    def test_commit_failure_rolls_back_snapshot_and_detail_replacement(self):
        old = self._add_snapshot(
            self.seller.id, self.period_start, self.period_end, 111,
        )
        old_detail = self._add_product(
            self.seller.id, self.period_start, self.period_end, 11, 'old',
        )
        db.session.commit()
        fake = FakeWBClient(result=products_response())

        with patch.object(AnalyticsService, '_get_wb_client', return_value=fake):
            with patch.object(db.session(), 'commit', side_effect=RuntimeError('synthetic commit failure')):
                with self.assertRaises(AnalyticsRefreshError) as raised:
                    AnalyticsService.fetch_and_cache_snapshot(
                        self.seller, '30d', force=True,
                    )

        self.assertEqual(raised.exception.code, 'persistence_failed')
        self.assertEqual(
            AnalyticsService.get_latest_snapshot(
                self.seller.id, self.period_start, self.period_end,
            ).id,
            old.id,
        )
        self.assertEqual([row.id for row in ProductAnalytics.query.all()], [old_detail.id])

    def test_explicit_empty_products_is_cached_as_a_real_zero(self):
        fake = FakeWBClient(result={'data': {'products': []}})
        with patch.object(AnalyticsService, '_get_wb_client', return_value=fake):
            first = AnalyticsService.fetch_and_cache_snapshot(
                self.seller, '30d', force=True,
            )
            second = AnalyticsService.fetch_and_cache_snapshot(
                self.seller, '30d', force=False,
            )

        self.assertEqual(first['kpi']['orders'], 0)
        self.assertEqual(first['topProducts'], [])
        self.assertEqual(second, first)
        self.assertEqual(fake.calls, 1)
        self.assertEqual(AnalyticsSnapshot.query.count(), 1)
        self.assertEqual(ProductAnalytics.query.count(), 0)

    def test_normal_summary_serves_exact_stale_cache_with_visible_stale_state(self):
        old = self._add_snapshot(
            self.seller.id,
            self.period_start,
            self.period_end,
            111,
            created_at=datetime.utcnow() - timedelta(hours=6),
        )
        db.session.commit()
        fake = FakeWBClient(failure=WBAuthException('synthetic private detail'))

        with patch.object(AnalyticsService, '_get_wb_client', return_value=fake):
            response = self.client.get('/api/analytics/summary?period=30d')

        payload = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload['stale'])
        self.assertEqual(payload['data']['kpi']['revenue'], 111)
        self.assertEqual(
            AnalyticsService.get_latest_snapshot(
                self.seller.id, self.period_start, self.period_end,
            ).id,
            old.id,
        )

    def test_cold_summary_and_forced_refresh_fail_safely_without_zero_snapshot(self):
        fake = FakeWBClient(failure=WBAuthException('synthetic private detail'))
        with patch.object(AnalyticsService, '_get_wb_client', return_value=fake):
            cold = self.client.get('/api/analytics/summary?period=30d')
            forced = self.client.post(
                '/api/analytics/refresh', json={'period': '30d'},
            )

        self.assertEqual(cold.status_code, 502)
        self.assertEqual(forced.status_code, 502)
        for response in (cold, forced):
            payload = response.get_json()
            self.assertFalse(payload['success'])
            self.assertEqual(payload['error_code'], 'provider_unavailable')
            self.assertNotIn('synthetic private detail', response.get_data(as_text=True))
        self.assertEqual(AnalyticsSnapshot.query.count(), 0)
        self.assertEqual(ProductAnalytics.query.count(), 0)

        malformed = FakeWBClient(result={'data': {'products': None}})
        with patch.object(AnalyticsService, '_get_wb_client', return_value=malformed):
            response = self.client.get('/api/analytics/summary?period=30d')
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.get_json()['error_code'], 'invalid_provider_response')
        self.assertEqual(AnalyticsSnapshot.query.count(), 0)

    def test_daily_cold_summary_failure_uses_safe_typed_response(self):
        fake = FakeWBClient(failure=WBAuthException('synthetic private detail'))
        with patch.object(AnalyticsService, '_get_wb_client', return_value=fake):
            response = self.client.get('/api/analytics/daily?period=30d')

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.get_json()['error_code'], 'provider_unavailable')
        self.assertNotIn('synthetic private detail', response.get_data(as_text=True))
        self.assertEqual(AnalyticsSnapshot.query.count(), 0)


if __name__ == '__main__':
    unittest.main()
