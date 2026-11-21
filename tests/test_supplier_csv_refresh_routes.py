"""Registered admin routes for an explicit supplier CSV catalog refresh."""

from datetime import datetime
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from flask import Flask
from flask_login import LoginManager
from flask_wtf.csrf import CSRFProtect

from models import Supplier, User, db
from routes.suppliers import register_supplier_routes
from services.supplier_service import SupplierCSVParser, SyncResult, SupplierService


class SupplierCSVRefreshRoutesTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__, template_folder='../templates')
        self.app.config.update(
            TESTING=True,
            SECRET_KEY='supplier-csv-refresh-routes',
            SQLALCHEMY_DATABASE_URI='sqlite://',
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            WTF_CSRF_ENABLED=False,
        )
        db.init_app(self.app)
        LoginManager(self.app)
        CSRFProtect(self.app)
        register_supplier_routes(self.app)

        @self.app.route('/dashboard')
        def dashboard():
            return 'dashboard'

        self.client = self.app.test_client()
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()

        admin = User(
            username='csv-refresh-admin',
            email='csv-refresh-admin@test.local',
            is_admin=True,
            is_active=True,
        )
        admin.set_password('synthetic-password')
        supplier = Supplier(
            name='CSV fixture supplier',
            code='csv-refresh-fixture',
            csv_source_url='https://supplier.invalid/catalog.csv',
            last_sync_at=datetime(2026, 11, 20, 12, 34),
            last_sync_status='success',
        )
        db.session.add_all([admin, supplier])
        db.session.commit()
        self.admin_id = admin.id
        self.supplier_id = supplier.id

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def _auth(self, *, admin):
        user = SimpleNamespace(
            id=self.admin_id,
            is_authenticated=True,
            is_active=True,
            is_admin=admin,
            seller=None,
        )
        return (
            patch('routes.suppliers.current_user', user),
            patch('flask_login.utils._get_user', return_value=user),
        )

    @staticmethod
    def _flashes(client):
        with client.session_transaction() as session:
            return session.get('_flashes', [])

    def test_seller_is_refused_by_both_admin_post_routes(self):
        auth_patch, login_patch = self._auth(admin=False)
        with auth_patch, login_patch, \
             patch('routes.suppliers.SupplierService.sync_from_csv') as sync, \
             patch(
                 'routes.suppliers.SupplierService.get_supplier',
             ) as get_supplier, \
             patch.object(SupplierCSVParser, 'fetch_csv_raw') as fetch_raw:
            sync_response = self.client.post(
                f'/admin/suppliers/{self.supplier_id}/sync',
            )
            preview_response = self.client.post(
                f'/admin/suppliers/{self.supplier_id}/csv-preview',
            )

        self.assertEqual(sync_response.status_code, 302)
        self.assertEqual(preview_response.status_code, 302)
        sync.assert_not_called()
        get_supplier.assert_not_called()
        fetch_raw.assert_not_called()

    def test_both_admin_post_routes_reject_missing_csrf_token(self):
        self.app.config['WTF_CSRF_ENABLED'] = True
        auth_patch, login_patch = self._auth(admin=True)
        with auth_patch, login_patch, \
             patch('routes.suppliers.SupplierService.get_supplier') as get_supplier, \
             patch('routes.suppliers.SupplierService.sync_from_csv') as sync, \
             patch.object(SupplierCSVParser, 'fetch_csv_raw') as fetch_raw:
            sync_response = self.client.post(
                f'/admin/suppliers/{self.supplier_id}/sync',
            )
            preview_response = self.client.post(
                f'/admin/suppliers/{self.supplier_id}/csv-preview',
            )

        self.assertEqual(sync_response.status_code, 400)
        self.assertEqual(preview_response.status_code, 400)
        get_supplier.assert_not_called()
        sync.assert_not_called()
        fetch_raw.assert_not_called()

    def test_admin_sync_calls_once_for_exact_supplier_and_flashes_success(self):
        result = SyncResult(
            success=True,
            total_in_csv=5,
            added=2,
            updated=3,
            duration_seconds=0.4,
        )
        original_get_supplier = SupplierService.get_supplier
        auth_patch, login_patch = self._auth(admin=True)
        with auth_patch, login_patch, \
             patch.object(
                 SupplierService,
                 'get_supplier',
                 wraps=original_get_supplier,
             ) as get_supplier, \
             patch(
                 'routes.suppliers.SupplierService.sync_from_csv',
                 return_value=result,
             ) as sync, \
             patch('routes.suppliers.log_admin_action') as audit:
            response = self.client.post(
                f'/admin/suppliers/{self.supplier_id}/sync',
            )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith(
            f'/admin/suppliers/{self.supplier_id}/products',
        ))
        get_supplier.assert_called_once_with(self.supplier_id)
        sync.assert_called_once_with(self.supplier_id)
        audit.assert_called_once()
        self.assertEqual(
            audit.call_args.kwargs['details']['rows_in_file'],
            5,
        )
        flashes = self._flashes(self.client)
        self.assertEqual(flashes[-1][0], 'success')
        self.assertIn('Новых позиций — 2', flashes[-1][1])
        self.assertIn('Существующих позиций обработано — 3', flashes[-1][1])

    def test_busy_sync_flashes_warning_without_claiming_success(self):
        result = SyncResult(success=False, busy=True)
        auth_patch, login_patch = self._auth(admin=True)
        with auth_patch, login_patch, patch(
            'routes.suppliers.SupplierService.sync_from_csv',
            return_value=result,
        ) as sync, patch('routes.suppliers.log_admin_action'):
            response = self.client.post(
                f'/admin/suppliers/{self.supplier_id}/sync',
            )

        self.assertEqual(response.status_code, 302)
        sync.assert_called_once_with(self.supplier_id)
        flashes = self._flashes(self.client)
        self.assertEqual(flashes[-1][0], 'warning')
        self.assertEqual(flashes[-1][1], 'Каталог этого поставщика уже обновляется.')

    def test_preview_uses_parser_preflight_and_does_not_run_sync(self):
        raw_bytes = b'id;title\nSKU-1;Fixture'
        preflight = SimpleNamespace(
            is_valid=True,
            encoding_detected='utf-8',
            encoding_confidence=1.0,
            delimiter_detected=';',
            total_rows=1,
            columns_count=2,
            empty_rows=0,
            duplicate_ids=0,
            sample_products=[{'external_id': 'SKU-1', 'title': 'Fixture'}],
            warnings=[],
            errors=[],
            field_fill_rates={'title': 1.0},
        )
        original_get_supplier = SupplierService.get_supplier
        auth_patch, login_patch = self._auth(admin=True)
        with auth_patch, login_patch, \
             patch.object(
                 SupplierService,
                 'get_supplier',
                 wraps=original_get_supplier,
             ) as get_supplier, \
             patch.object(
                 SupplierCSVParser,
                 'fetch_csv_raw',
                 return_value=raw_bytes,
             ) as fetch_raw, \
             patch.object(
                 SupplierCSVParser,
                 'preflight_raw',
                 return_value=([], preflight),
                 create=True,
             ) as preflight_raw, \
             patch(
                 'routes.suppliers.SupplierService.sync_from_csv',
             ) as sync, \
             patch('routes.suppliers.log_admin_action') as audit, \
             patch.object(db.session, 'commit', wraps=db.session.commit) as commit:
            response = self.client.post(
                f'/admin/suppliers/{self.supplier_id}/csv-preview',
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['total_rows'], 1)
        self.assertEqual(response.get_json()['sample_products'], preflight.sample_products)
        get_supplier.assert_called_once_with(self.supplier_id)
        fetch_raw.assert_called_once_with()
        preflight_raw.assert_called_once_with(raw_bytes)
        sync.assert_not_called()
        audit.assert_not_called()
        commit.assert_not_called()


if __name__ == '__main__':
    unittest.main()
