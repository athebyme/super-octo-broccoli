# -*- coding: utf-8 -*-
"""Admin HTTP boundary for durable supplier catalog enrichment."""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch
import unittest

from flask import Flask
from flask_login import LoginManager

from models import (
    Marketplace,
    MarketplaceCategory,
    Supplier,
    SupplierCatalogEnrichmentItem,
    SupplierCatalogEnrichmentRun,
    SupplierProduct,
    User,
    db,
)
from routes.supplier_catalog_enrichment import (
    register_supplier_catalog_enrichment_routes,
)


class _ConfiguredAI:
    config = SimpleNamespace(model='route-test-model')

    def close(self):
        return None


class SupplierCatalogEnrichmentRoutesTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__, template_folder='../templates')
        self.app.config.update(
            TESTING=True,
            SECRET_KEY='supplier-catalog-enrichment-routes',
            SQLALCHEMY_DATABASE_URI='sqlite://',
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
        )
        db.init_app(self.app)
        LoginManager(self.app)
        register_supplier_catalog_enrichment_routes(self.app)
        self.client = self.app.test_client()
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()

        admin = User(
            username='enrichment-route-admin',
            email='enrichment-route-admin@test.local',
            is_admin=True,
            is_active=True,
        )
        admin.set_password('synthetic-password')
        supplier = Supplier(
            name='Owned supplier', code='owned-enrichment-route',
            ai_enabled=True,
        )
        foreign_supplier = Supplier(
            name='Foreign supplier', code='foreign-enrichment-route',
            ai_enabled=True,
        )
        marketplace = Marketplace(
            name='Wildberries', code='wb', is_active=True,
            categories_sync_status='success',
            categories_synced_at=datetime.utcnow(),
            categories_version=2,
            categories_snapshot_hash='c' * 64,
        )
        db.session.add_all([admin, supplier, foreign_supplier, marketplace])
        db.session.flush()
        category = MarketplaceCategory(
            marketplace_id=marketplace.id,
            subject_id=91001,
            subject_name='Анальные пробки',
            parent_name='Товары для взрослых',
            is_leaf=True,
            is_enabled=True,
            is_available=True,
        )
        owned_product = SupplierProduct(
            supplier_id=supplier.id,
            external_id='owned-route-product',
            title='Анальная пробка',
            category='Товары для взрослых',
            wb_category_name='Товары для взрослых',
        )
        foreign_product = SupplierProduct(
            supplier_id=foreign_supplier.id,
            external_id='foreign-route-product',
            title='Foreign product',
        )
        db.session.add_all([category, owned_product, foreign_product])
        db.session.commit()
        self.admin_id = admin.id
        self.supplier_id = supplier.id
        self.product_id = owned_product.id
        self.foreign_product_id = foreign_product.id

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def _auth(self, *, admin=True):
        user = SimpleNamespace(
            id=self.admin_id,
            is_authenticated=True,
            is_active=True,
            is_admin=admin,
            seller=None,
        )
        return (
            patch('routes.supplier_catalog_enrichment.current_user', user),
            patch('flask_login.utils._get_user', return_value=user),
        )

    def test_page_is_admin_only_and_exposes_problem_count(self):
        user_patch, login_patch = self._auth(admin=False)
        with user_patch, login_patch:
            denied = self.client.get(
                f'/admin/suppliers/{self.supplier_id}/catalog-enrichment'
            )
        self.assertEqual(denied.status_code, 403)

        captured = {}

        def render(_template, **context):
            captured.update(context)
            return 'ok'

        user_patch, login_patch = self._auth()
        with user_patch, login_patch, patch(
            'routes.supplier_catalog_enrichment.render_template',
            side_effect=render,
        ):
            response = self.client.get(
                f'/admin/suppliers/{self.supplier_id}/catalog-enrichment'
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured['adult_parent_count'], 1)
        self.assertTrue(captured['reference_status']['usable'])

    def test_selected_run_persists_exact_owned_set_and_kicks_worker(self):
        user_patch, login_patch = self._auth()
        with user_patch, login_patch, patch(
            'services.supplier_service.SupplierService._get_ai_service',
            return_value=_ConfiguredAI(),
        ), patch(
            'routes.supplier_catalog_enrichment.SupplierCatalogEnrichmentService.kick'
        ) as kick, patch(
            'routes.supplier_catalog_enrichment.log_admin_action'
        ):
            response = self.client.post(
                f'/admin/suppliers/{self.supplier_id}/catalog-enrichment/runs',
                data={
                    'mode': 'category_only',
                    'selection_scope': 'selected',
                    'product_ids': [str(self.product_id)],
                },
            )

        self.assertEqual(response.status_code, 302)
        run = SupplierCatalogEnrichmentRun.query.one()
        item = SupplierCatalogEnrichmentItem.query.one()
        self.assertEqual(run.supplier_id, self.supplier_id)
        self.assertEqual(run.total, 1)
        self.assertEqual(item.supplier_product_id, self.product_id)
        kick.assert_called_once()

    def test_cross_supplier_selection_is_rejected_before_run_creation(self):
        user_patch, login_patch = self._auth()
        with user_patch, login_patch, patch(
            'services.supplier_service.SupplierService._get_ai_service',
            return_value=_ConfiguredAI(),
        ), patch(
            'routes.supplier_catalog_enrichment.SupplierCatalogEnrichmentService.kick'
        ) as kick:
            response = self.client.post(
                f'/admin/suppliers/{self.supplier_id}/catalog-enrichment/runs',
                data={
                    'mode': 'category_only',
                    'selection_scope': 'selected',
                    'product_ids': [
                        str(self.product_id), str(self.foreign_product_id),
                    ],
                },
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(SupplierCatalogEnrichmentRun.query.count(), 0)
        kick.assert_not_called()

    def test_reference_search_returns_only_fresh_leaf_rows(self):
        user_patch, login_patch = self._auth()
        with user_patch, login_patch:
            response = self.client.get(
                f'/admin/suppliers/{self.supplier_id}/catalog-enrichment/'
                'categories/search?q=пробки'
            )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(
            [(row['subject_id'], row['subject_name'])
             for row in payload['categories']],
            [(91001, 'Анальные пробки')],
        )


if __name__ == '__main__':
    unittest.main()
