# -*- coding: utf-8 -*-
"""HTTP boundary for shared matching: async-only and tenant-safe."""
import json
import unittest
from unittest.mock import MagicMock, patch

from flask import Flask

from models import (
    BackgroundJob,
    CompetitorGroup,
    CompetitorProduct,
    CompetitorProductMatch,
    Seller,
    Supplier,
    SupplierProduct,
    User,
    db,
)
from routes.competitors import register_competitor_routes


class CompetitorMatchingRoutesTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(
            TESTING=True, SECRET_KEY='test',
            SQLALCHEMY_DATABASE_URI='sqlite://',
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
        )
        db.init_app(self.app)
        register_competitor_routes(self.app)
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        self.user, self.seller = self._seller('one', 'one@route.test')
        self.other_user, self.other = self._seller('two', 'two@route.test')
        self.group = CompetitorGroup(seller_id=self.seller.id, name='Own group')
        self.foreign_group = CompetitorGroup(
            seller_id=self.other.id, name='Foreign group')
        db.session.add_all([self.group, self.foreign_group])
        db.session.flush()
        db.session.add_all([
            CompetitorProduct(
                seller_id=self.seller.id, group_id=self.group.id,
                nm_id=101, title='Observed one'),
            CompetitorProduct(
                seller_id=self.other.id, group_id=self.foreign_group.id,
                nm_id=202, title='Observed foreign'),
        ])
        supplier = Supplier(name='Supplier', code='route-supplier')
        db.session.add(supplier)
        db.session.flush()
        self.supplier_product = SupplierProduct(
            supplier_id=supplier.id,
            external_id='route-1',
            title='Edited normalized value',
            ai_seo_title='Secret AI title',
            original_data_json=json.dumps({
                'title': 'Observed supplier title',
                'vendor_code': 'OBS-1',
            }),
        )
        db.session.add(self.supplier_product)
        db.session.commit()
        self.client = self.app.test_client()
        self.login = MagicMock()
        self.login.is_authenticated = True
        self.login.id = self.user.id
        self.login.seller = self.seller

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def _seller(self, username, email):
        user = User(username=username, email=email, is_active=True)
        user.set_password('synthetic-password')
        seller = Seller(user=user, company_name=username)
        db.session.add(seller)
        db.session.commit()
        return user, seller

    def _as_user(self):
        return patch('flask_login.utils._get_user', return_value=self.login)

    def test_run_route_only_enqueues_background_job(self):
        with self._as_user(), patch(
            'services.competitor_matching.process_global_match',
        ) as provider_work:
            response = self.client.post(
                f'/api/competitors/groups/{self.group.id}/matches/run',
                json={},
            )
        self.assertEqual(response.status_code, 202)
        provider_work.assert_not_called()
        job = BackgroundJob.query.filter_by(
            seller_id=self.seller.id,
            job_type='competitor_matching',
        ).one()
        self.assertEqual(job.status, 'pending')
        self.assertEqual(job.total, 1)

    def test_review_is_scoped_by_monitored_nm_id(self):
        match = CompetitorProductMatch(
            nm_id=202,
            suggested_supplier_product_id=self.supplier_product.id,
            processing_status='completed', predicted_match_type='same',
            text_score=90, deterministic_score=90, final_score=90,
            candidates_json='[]', evidence_json='{}',
            marketplace_facts_json='{}', algorithm_version='test',
            llm_status='completed',
        )
        db.session.add(match)
        db.session.commit()
        with self._as_user():
            response = self.client.put(
                f'/api/competitors/matches/{match.id}/review',
                json={
                    'action': 'confirm',
                    'supplier_product_id': self.supplier_product.id,
                    'match_type': 'same',
                },
            )
        self.assertEqual(response.status_code, 404)

    def test_supplier_search_returns_observed_source_not_ai(self):
        with self._as_user():
            response = self.client.get(
                '/api/competitors/supplier-products/search?q=Observed')
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data['source_scope'], 'supplier_observed_only')
        self.assertEqual(data['items'][0]['title'], 'Observed supplier title')
        self.assertNotIn('Secret AI', json.dumps(data, ensure_ascii=False))


if __name__ == '__main__':
    unittest.main()
