"""Admin-only UI for explicit supplier catalog schedule consent."""

from datetime import datetime
import re
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from flask import Flask
from flask_login import LoginManager
from flask_wtf.csrf import CSRFProtect
from werkzeug.datastructures import MultiDict

from models import AdminAuditLog, Supplier, User, db
from routes.suppliers import register_supplier_routes
from services.supplier_catalog_refresh_models import SupplierCatalogRefreshRun
from services.supplier_catalog_schedule_models import (
    SupplierCatalogSchedule,
    SupplierCatalogScheduledRun,
)


RUN_ID = '123e4567-e89b-42d3-a456-426614174000'


class SupplierCatalogScheduleRoutesTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__, template_folder='../templates')
        self.app.config.update(
            TESTING=True,
            SECRET_KEY='supplier-catalog-schedule-routes',
            SQLALCHEMY_DATABASE_URI='sqlite://',
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            WTF_CSRF_ENABLED=False,
        )
        db.init_app(self.app)
        LoginManager(self.app)
        CSRFProtect(self.app)

        def template_url_fallback(_error, endpoint, _values):
            return f'/test/{endpoint}'

        self.app.url_build_error_handlers.append(template_url_fallback)
        register_supplier_routes(self.app)

        @self.app.route('/dashboard')
        def dashboard():
            return 'dashboard'

        @self.app.route('/notifications')
        def notifications_page():
            return 'notifications'

        self.client = self.app.test_client()
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()

        admin = User(
            username='catalog-schedule-admin',
            email='catalog-schedule-admin@test.local',
            is_admin=True,
            is_active=True,
        )
        admin.set_password('synthetic-password')
        supplier = Supplier(
            name='Schedule fixture supplier',
            code='catalog-schedule-fixture',
            csv_source_url='https://supplier.invalid/catalog.csv',
            is_active=True,
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
            username='catalog-schedule-admin',
            is_authenticated=True,
            is_active=True,
            is_admin=admin,
            seller=None,
        )
        return (
            patch('routes.suppliers.current_user', user),
            patch('flask_login.utils._get_user', return_value=user),
        )

    def _post_values(self, *, enabled, interval='24', revision=''):
        return {
            'catalog_schedule_enabled': enabled,
            'catalog_schedule_interval_hours': interval,
            'catalog_schedule_revision': revision,
        }

    def _csrf_token_from_page(self):
        auth_patch, login_patch = self._auth(admin=True)
        with auth_patch, login_patch:
            response = self.client.get(
                f'/admin/suppliers/{self.supplier_id}/products',
            )
        self.assertEqual(response.status_code, 200)
        match = re.search(
            r'name="csrf_token" value="([^"]+)"',
            response.get_data(as_text=True),
        )
        self.assertIsNotNone(match)
        return match.group(1)

    def test_default_schedule_is_off_and_scheduled_run_origin_is_readable(self):
        now = datetime(2026, 11, 23, 8, 15)
        db.session.add(SupplierCatalogRefreshRun(
            id=RUN_ID,
            supplier_id=self.supplier_id,
            admin_user_id=self.admin_id,
            status='pending',
            config_digest='a' * 64,
            created_at=now,
        ))
        db.session.add(SupplierCatalogScheduledRun(
            run_id=RUN_ID,
            schedule_revision=1,
            due_at=now,
        ))
        db.session.commit()

        auth_patch, login_patch = self._auth(admin=True)
        with auth_patch, login_patch, patch.object(
            db.session, 'commit', wraps=db.session.commit,
        ) as commit:
            response = self.client.get(
                f'/admin/suppliers/{self.supplier_id}/products',
            )
        commit.assert_not_called()

        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn('data-testid="catalog-schedule"', page)
        self.assertIn('data-schedule-enabled="false"', page)
        self.assertIn('Расписание полного каталога', page)
        self.assertIn('По умолчанию расписание выключено', page)
        self.assertIn('Выключено · каждые 24 ч', page)
        self.assertIn(
            'data-refresh-trigger-kind="scheduled">По расписанию</span>',
            page,
        )
        self.assertIn('плановая дата 23.11.2026 08:15 UTC', page)
        self.assertIn('data-testid="catalog-refresh-submit"', page)
        self.assertEqual(SupplierCatalogSchedule.query.count(), 0)

    def test_admin_post_requires_csrf_and_stale_revision_does_not_enable(self):
        self.app.config['WTF_CSRF_ENABLED'] = True
        auth_patch, login_patch = self._auth(admin=True)
        with auth_patch, login_patch, patch(
            'services.supplier_catalog_refresh.set_catalog_schedule',
        ) as set_schedule:
            missing_csrf = self.client.post(
                f'/admin/suppliers/{self.supplier_id}/catalog-schedule',
                data=self._post_values(enabled='1'),
            )
        self.assertEqual(missing_csrf.status_code, 400)
        set_schedule.assert_not_called()

        token = self._csrf_token_from_page()
        auth_patch, login_patch = self._auth(admin=True)
        with auth_patch, login_patch:
            enabled = self.client.post(
                f'/admin/suppliers/{self.supplier_id}/catalog-schedule',
                data={
                    **self._post_values(enabled='1'),
                    'csrf_token': token,
                },
            )
        self.assertEqual(enabled.status_code, 303)
        self.assertEqual(
            enabled.location,
            f'/admin/suppliers/{self.supplier_id}/products#catalog-schedule',
        )
        schedule = SupplierCatalogSchedule.query.one()
        self.assertTrue(schedule.enabled)
        self.assertEqual(schedule.interval_hours, 24)
        self.assertEqual(schedule.authorized_by_user_id, self.admin_id)
        self.assertEqual(schedule.revision, 1)
        self.assertEqual(SupplierCatalogRefreshRun.query.count(), 0)
        self.assertEqual(AdminAuditLog.query.filter_by(
            action='set_supplier_catalog_schedule',
        ).count(), 1)

        auth_patch, login_patch = self._auth(admin=True)
        with auth_patch, login_patch:
            stale = self.client.post(
                f'/admin/suppliers/{self.supplier_id}/catalog-schedule',
                data={
                    **self._post_values(enabled='1', revision='2'),
                    'csrf_token': token,
                },
            )
        self.assertEqual(stale.status_code, 303)
        db.session.refresh(schedule)
        self.assertTrue(schedule.enabled)
        self.assertEqual(schedule.revision, 1)
        self.assertEqual(SupplierCatalogRefreshRun.query.count(), 0)

    def test_form_rejects_duplicates_and_invalid_values_without_service_calls(self):
        auth_patch, login_patch = self._auth(admin=True)
        duplicate_enabled = MultiDict([
            ('catalog_schedule_enabled', '1'),
            ('catalog_schedule_enabled', '0'),
            ('catalog_schedule_interval_hours', '24'),
            ('catalog_schedule_revision', ''),
        ])
        duplicate_interval = MultiDict([
            ('catalog_schedule_enabled', '1'),
            ('catalog_schedule_interval_hours', '12'),
            ('catalog_schedule_interval_hours', '24'),
            ('catalog_schedule_revision', ''),
        ])
        duplicate_revision = MultiDict([
            ('catalog_schedule_enabled', '1'),
            ('catalog_schedule_interval_hours', '24'),
            ('catalog_schedule_revision', '1'),
            ('catalog_schedule_revision', '2'),
        ])
        with auth_patch, login_patch, patch(
            'services.supplier_catalog_refresh.set_catalog_schedule',
        ) as set_schedule:
            duplicate = self.client.post(
                f'/admin/suppliers/{self.supplier_id}/catalog-schedule',
                data=duplicate_enabled,
            )
            duplicate_interval_response = self.client.post(
                f'/admin/suppliers/{self.supplier_id}/catalog-schedule',
                data=duplicate_interval,
            )
            duplicate_revision_response = self.client.post(
                f'/admin/suppliers/{self.supplier_id}/catalog-schedule',
                data=duplicate_revision,
            )
            invalid_interval = self.client.post(
                f'/admin/suppliers/{self.supplier_id}/catalog-schedule',
                data=self._post_values(enabled='1', interval='24x'),
            )
            out_of_range_interval = self.client.post(
                f'/admin/suppliers/{self.supplier_id}/catalog-schedule',
                data=self._post_values(enabled='1', interval='5'),
            )
            invalid_consent = self.client.post(
                f'/admin/suppliers/{self.supplier_id}/catalog-schedule',
                data=self._post_values(enabled='yes'),
            )

        self.assertEqual(duplicate.status_code, 303)
        self.assertEqual(duplicate_interval_response.status_code, 303)
        self.assertEqual(duplicate_revision_response.status_code, 303)
        self.assertEqual(invalid_interval.status_code, 303)
        self.assertEqual(out_of_range_interval.status_code, 303)
        self.assertEqual(invalid_consent.status_code, 303)
        self.assertTrue(all(
            response.location.endswith('#catalog-schedule')
            for response in (
                duplicate, duplicate_interval_response,
                duplicate_revision_response, invalid_interval,
                out_of_range_interval, invalid_consent,
            )
        ))
        set_schedule.assert_not_called()
        self.assertEqual(SupplierCatalogSchedule.query.count(), 0)

    def test_admin_can_disable_when_supplier_is_inactive_and_csv_is_removed(self):
        auth_patch, login_patch = self._auth(admin=True)
        with auth_patch, login_patch:
            enabled = self.client.post(
                f'/admin/suppliers/{self.supplier_id}/catalog-schedule',
                data=self._post_values(enabled='1', interval='12'),
            )
        self.assertEqual(enabled.status_code, 303)
        schedule = SupplierCatalogSchedule.query.one()
        supplier = db.session.get(Supplier, self.supplier_id)
        supplier.is_active = False
        supplier.csv_source_url = None
        db.session.commit()

        auth_patch, login_patch = self._auth(admin=True)
        with auth_patch, login_patch:
            disabled = self.client.post(
                f'/admin/suppliers/{self.supplier_id}/catalog-schedule',
                data=self._post_values(
                    enabled='0', interval='12', revision='1',
                ),
            )
        self.assertEqual(disabled.status_code, 303)
        db.session.refresh(schedule)
        self.assertFalse(schedule.enabled)
        self.assertEqual(schedule.revision, 2)
        self.assertEqual(SupplierCatalogRefreshRun.query.count(), 0)

        auth_patch, login_patch = self._auth(admin=True)
        with auth_patch, login_patch:
            page = self.client.get(
                f'/admin/suppliers/{self.supplier_id}/products',
            )
        self.assertEqual(page.status_code, 200)
        body = page.get_data(as_text=True)
        self.assertIn('data-testid="catalog-schedule-status"', body)
        self.assertIn('Выключено · каждые 12 ч', body)
        self.assertIn('Расписание выключено; автоматическая попытка не запланирована.', body)
        self.assertIn('data-testid="catalog-schedule-form"', body)

    def test_seller_cannot_read_or_change_admin_schedule(self):
        auth_patch, login_patch = self._auth(admin=False)
        with auth_patch, login_patch, patch(
            'services.supplier_catalog_refresh.set_catalog_schedule',
        ) as set_schedule:
            read_response = self.client.get(
                f'/admin/suppliers/{self.supplier_id}/products',
            )
            post_response = self.client.post(
                f'/admin/suppliers/{self.supplier_id}/catalog-schedule',
                data=self._post_values(enabled='1'),
            )

        self.assertEqual(read_response.status_code, 302)
        self.assertEqual(post_response.status_code, 302)
        set_schedule.assert_not_called()
        self.assertEqual(SupplierCatalogSchedule.query.count(), 0)

    def test_unavailable_schedule_read_has_safe_copy_and_no_form(self):
        auth_patch, login_patch = self._auth(admin=True)
        with auth_patch, login_patch, patch(
            'services.supplier_catalog_refresh.get_catalog_schedule',
            side_effect=RuntimeError('private database detail'),
        ):
            response = self.client.get(
                f'/admin/suppliers/{self.supplier_id}/products',
            )

        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn('Не удалось прочитать настройки расписания', page)
        self.assertNotIn('data-testid="catalog-schedule-form"', page)
        self.assertNotIn('private database detail', page)

    def test_enabled_schedule_is_paused_for_invalid_source_and_revoked_admin(self):
        due_at = datetime(2026, 11, 27, 9, 0)
        db.session.add(SupplierCatalogSchedule(
            supplier_id=self.supplier_id,
            enabled=True,
            interval_hours=24,
            next_due_at=due_at,
            authorized_by_user_id=self.admin_id,
            revision=3,
            updated_at=due_at,
        ))
        admin = db.session.get(User, self.admin_id)
        admin.is_admin = False
        supplier = db.session.get(Supplier, self.supplier_id)
        supplier.csv_source_url = 'file:///private/catalog.csv'
        db.session.commit()

        auth_patch, login_patch = self._auth(admin=True)
        with auth_patch, login_patch, patch.object(
            db.session, 'commit', wraps=db.session.commit,
        ) as commit:
            response = self.client.get(
                f'/admin/suppliers/{self.supplier_id}/products',
            )

        self.assertEqual(response.status_code, 200)
        commit.assert_not_called()
        page = response.get_data(as_text=True)
        self.assertIn('data-schedule-enabled="true"', page)
        self.assertRegex(
            page,
            r'data-testid="catalog-schedule-status"[^>]*>\s*Приостановлено',
        )
        self.assertIn('Адрес CSV не проходит проверку.', page)
        self.assertIn(
            'Администратор, подтвердивший расписание, больше не активен или утратил права.',
            page,
        )
        self.assertIn(
            'Следующая попытка не запланирована, пока не устранена причина паузы.',
            page,
        )
        self.assertNotIn('27.11.2026 09:00 UTC', page)
        self.assertIn('data-testid="catalog-schedule-form"', page)


if __name__ == '__main__':
    unittest.main()
