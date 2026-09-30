# -*- coding: utf-8 -*-
"""Old admin parser selections reach strict Flash runs without launching legacy jobs."""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch
import unittest

from flask import Flask
from flask_login import LoginManager

from models import (
    Marketplace, MarketplaceCategory, Supplier, SupplierCatalogEnrichmentItem,
    SupplierCatalogEnrichmentRun,
    SupplierProduct, User, db,
)
from routes.supplier_catalog_enrichment import (
    register_supplier_catalog_enrichment_routes,
)
from routes.suppliers import register_supplier_routes
from services.supplier_catalog_enrichment import SupplierCatalogEnrichmentService


class _ConfiguredAI:
    config = SimpleNamespace(model='deepseek-flash')

    def close(self):
        pass


class AdminFlashHandoffTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__, template_folder='../templates')
        self.app.config.update(
            TESTING=True, SECRET_KEY='admin-flash-handoff',
            SQLALCHEMY_DATABASE_URI='sqlite://',
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
        )
        db.init_app(self.app)
        LoginManager(self.app)
        register_supplier_routes(self.app)
        register_supplier_catalog_enrichment_routes(self.app)
        self.client = self.app.test_client()
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        admin = User(
            username='flash-handoff-admin',
            email='flash-handoff-admin@test.local',
            is_admin=True, is_active=True,
        )
        admin.set_password('synthetic-password')
        supplier = Supplier(name='Owned', code='flash-owned', ai_enabled=True)
        foreign = Supplier(name='Foreign', code='flash-foreign', ai_enabled=True)
        marketplace = Marketplace(
            name='Wildberries', code='wb', is_active=True,
            categories_sync_status='success',
            categories_synced_at=datetime.utcnow(),
            categories_version=1, categories_snapshot_hash='d' * 64,
        )
        db.session.add_all([admin, supplier, foreign, marketplace])
        db.session.flush()
        db.session.add(MarketplaceCategory(
            marketplace_id=marketplace.id, subject_id=91001,
            subject_name='Прочее', parent_name='Каталог', is_leaf=True,
            is_enabled=True, is_available=True,
        ))
        first = SupplierProduct(
            supplier_id=supplier.id, external_id='flash-first',
            title='A product', brand='A brand', supplier_status='in_stock',
        )
        second = SupplierProduct(
            supplier_id=supplier.id, external_id='flash-second',
            title='B product', brand='B brand', supplier_status='in_stock',
            ai_parsed_data_json='{}',
        )
        other = SupplierProduct(
            supplier_id=foreign.id, external_id='flash-foreign-product',
            title='Foreign product',
        )
        db.session.add_all([first, second, other])
        db.session.commit()
        self.admin_id = admin.id
        self.supplier_id = supplier.id
        self.first_id = first.id
        self.second_id = second.id
        self.foreign_id = other.id

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def _auth(self):
        user = SimpleNamespace(
            id=self.admin_id, is_authenticated=True,
            is_active=True, is_admin=True, seller=None,
        )
        return (
            patch('routes.suppliers.current_user', user),
            patch('routes.supplier_catalog_enrichment.current_user', user),
            patch('flask_login.utils._get_user', return_value=user),
        )

    def test_old_start_endpoints_return_actionable_conflict_without_job(self):
        with self._auth()[0], self._auth()[1], self._auth()[2], patch(
            'routes.suppliers.SupplierService.start_ai_parse_job'
        ) as old_start:
            selected = self.client.post(
                f'/admin/suppliers/{self.supplier_id}/ai/parse',
                data={'product_id': str(self.first_id)},
            )
            filtered = self.client.post(
                f'/admin/suppliers/{self.supplier_id}/ai/parse-by-filter',
                data={'parse_status': 'not_parsed', 'brand': 'A'},
            )
        self.assertEqual(selected.status_code, 409)
        self.assertEqual(selected.get_json()['code'], 'legacy_parser_retired')
        self.assertIn(f'product_ids={self.first_id}', selected.get_json()['next_url'])
        self.assertEqual(filtered.status_code, 409)
        self.assertIn('legacy_parse_status=not_parsed', filtered.get_json()['next_url'])
        old_start.assert_not_called()

    def test_exact_selection_preview_rejects_foreign_id(self):
        captured = {}
        def render(_template, **context):
            captured.update(context)
            return 'ok'
        with self._auth()[0], self._auth()[1], self._auth()[2], patch(
            'routes.supplier_catalog_enrichment.render_template', side_effect=render,
        ):
            response = self.client.get(
                f'/admin/suppliers/{self.supplier_id}/catalog-enrichment'
                f'?handoff=ids&product_ids={self.first_id},{self.foreign_id}'
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn('не принадлежит', captured['handoff']['error'])
        self.assertEqual(captured['handoff']['count'], 0)

    def test_legacy_filter_preview_and_run_use_same_exact_bounded_set(self):
        captured = {}
        def render(_template, **context):
            captured.update(context)
            return 'ok'
        path = f'/admin/suppliers/{self.supplier_id}/catalog-enrichment'
        with self._auth()[0], self._auth()[1], self._auth()[2], patch(
            'routes.supplier_catalog_enrichment.render_template', side_effect=render,
        ):
            response = self.client.get(
                path + '?handoff=filter&legacy_parse_status=not_parsed'
                '&legacy_stock_status=in_stock'
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured['handoff']['count'], 1)
        self.assertEqual(captured['handoff']['ids'], [self.first_id])
        with self._auth()[0], self._auth()[1], self._auth()[2], patch(
            'services.supplier_service.SupplierService._get_ai_service',
            return_value=_ConfiguredAI(),
        ), patch(
            'routes.supplier_catalog_enrichment.SupplierCatalogEnrichmentService.kick'
        ), patch('routes.supplier_catalog_enrichment.log_admin_action'):
            launched = self.client.post(path + '/runs', data={
                'selection_scope': 'handoff', 'handoff': 'filter',
                'mode': 'category_and_characteristics',
                'legacy_parse_status': 'not_parsed',
                'legacy_stock_status': 'in_stock',
            })
        self.assertEqual(launched.status_code, 302)
        run = SupplierCatalogEnrichmentRun.query.one()
        self.assertEqual(run.total, 1)
        self.assertEqual(run.items[0].supplier_product_id, self.first_id)

    def test_large_filter_is_visible_and_cannot_launch_or_truncate(self):
        path = f'/admin/suppliers/{self.supplier_id}/catalog-enrichment'
        captured = {}
        def render(_template, **context):
            captured.update(context)
            return 'ok'
        with patch('routes.supplier_catalog_enrichment.MAX_CHARACTERISTIC_SELECTION', 1), \
                self._auth()[0], self._auth()[1], self._auth()[2], patch(
                    'routes.supplier_catalog_enrichment.render_template',
                    side_effect=render,
                ):
            response = self.client.get(path + '?handoff=filter')
            launched = self.client.post(path + '/runs', data={
                'selection_scope': 'handoff', 'handoff': 'filter',
                'mode': 'category_and_characteristics',
            })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured['handoff']['count'], 2)
        self.assertIn('Сузьте', captured['handoff']['error'])
        self.assertEqual(launched.status_code, 302)
        self.assertEqual(SupplierCatalogEnrichmentRun.query.count(), 0)

    def test_admin_capacity_is_checked_before_shared_ledger_claim(self):
        with patch(
            'services.supplier_service.SupplierService._get_ai_service',
            return_value=_ConfiguredAI(),
        ):
            run = SupplierCatalogEnrichmentService.create_run(
                supplier_id=self.supplier_id, admin_user_id=self.admin_id,
                product_ids=[self.first_id],
                mode='category_and_characteristics',
            )
        item = SupplierCatalogEnrichmentItem.query.filter_by(run_id=run.id).one()
        item.status = 'running'
        item.attempt_count = 1
        db.session.commit()
        with patch(
            'services.ozon_draft_ai_transport.validate_flash_request'
        ), patch(
            'services.ozon_draft_ai_transport.try_acquire_flash_permit',
            return_value=None,
        ), patch('services.ai_parsing_budget.reserve_attempt') as reserve:
            claim = SupplierCatalogEnrichmentService._reserve_admin_flash(
                run.id, [item.id], [{'role': 'user', 'content': 'test'}],
                _ConfiguredAI(), 100,
            )
        self.assertIsNone(claim)
        reserve.assert_not_called()
        db.session.refresh(run)
        db.session.refresh(item)
        self.assertEqual(run.llm_calls, 0)
        self.assertEqual(item.status, 'pending')
        self.assertEqual(item.attempt_count, 0)
        self.assertEqual(item.error_code, 'ai_local_capacity')


if __name__ == '__main__':
    unittest.main()
