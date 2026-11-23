"""Registered admin routes for an explicit supplier CSV catalog refresh."""

from datetime import datetime
import hashlib
import json
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from flask import Flask
from flask_login import LoginManager
from flask_wtf.csrf import CSRFProtect
from werkzeug.datastructures import MultiDict

from models import (
    ImportedProduct, Seller, SellerSupplier, Supplier, SupplierProduct, User, db,
)
from routes.suppliers import register_supplier_routes
from services.supplier_catalog_refresh_models import SupplierCatalogRefreshRun
from services.supplier_catalog_schedule_models import (  # noqa: F401
    SupplierCatalogScheduledRun,
)
from services.supplier_price_feed import SupplierPriceFeed, SupplierPriceRow
from services.supplier_service import SupplierCSVParser, SupplierService


RUN_ID = '123e4567-e89b-42d3-a456-426614174000'


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
            username='csv-refresh-admin',
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
             patch('services.supplier_catalog_refresh.enqueue_catalog_refresh') as enqueue, \
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
        enqueue.assert_not_called()
        sync.assert_not_called()
        fetch_raw.assert_not_called()

    def test_admin_sync_enqueues_once_and_redirects_to_exact_status(self):
        payload = {'id': RUN_ID, 'status': 'pending'}
        original_get_supplier = SupplierService.get_supplier
        auth_patch, login_patch = self._auth(admin=True)
        with auth_patch, login_patch, \
             patch.object(
                 SupplierService,
                 'get_supplier',
                 wraps=original_get_supplier,
             ) as get_supplier, \
             patch(
                 'services.supplier_catalog_refresh.enqueue_catalog_refresh',
                 return_value=(payload, True),
             ) as enqueue, \
             patch('routes.suppliers.SupplierService.sync_from_csv') as sync, \
             patch('routes.suppliers.log_admin_action') as audit:
            response = self.client.post(
                f'/admin/suppliers/{self.supplier_id}/sync',
                data={'catalog_refresh_baseline': ''},
            )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(
            response.location,
            f'/admin/suppliers/{self.supplier_id}/products'
            f'?catalog_refresh_run={RUN_ID}#catalog-refresh-status',
        )
        get_supplier.assert_called_once_with(self.supplier_id)
        enqueue.assert_called_once_with(
            supplier_id=self.supplier_id,
            admin_user_id=self.admin_id,
            expected_latest_id=None,
            ip_address='127.0.0.1',
        )
        sync.assert_not_called()
        audit.assert_not_called()
        flashes = self._flashes(self.client)
        self.assertEqual(flashes[-1][0], 'success')
        self.assertIn('поставлено в очередь', flashes[-1][1])

    def test_stale_form_reuses_current_run_without_second_enqueue(self):
        payload = {'id': RUN_ID, 'status': 'running'}
        auth_patch, login_patch = self._auth(admin=True)
        with auth_patch, login_patch, patch(
            'services.supplier_catalog_refresh.enqueue_catalog_refresh',
            return_value=(payload, False),
        ) as enqueue, patch('routes.suppliers.SupplierService.sync_from_csv') as sync:
            response = self.client.post(
                f'/admin/suppliers/{self.supplier_id}/sync',
                data={'catalog_refresh_baseline': '123e4567-e89b-42d3-a456-426614174001'},
            )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(
            response.location,
            f'/admin/suppliers/{self.supplier_id}/products'
            f'?catalog_refresh_run={RUN_ID}#catalog-refresh-status',
        )
        enqueue.assert_called_once_with(
            supplier_id=self.supplier_id,
            admin_user_id=self.admin_id,
            expected_latest_id='123e4567-e89b-42d3-a456-426614174001',
            ip_address='127.0.0.1',
        )
        sync.assert_not_called()
        flashes = self._flashes(self.client)
        self.assertEqual(flashes[-1][0], 'info')
        self.assertIn('уже выполняется', flashes[-1][1])

    def test_sync_rejects_duplicate_baseline_fields(self):
        auth_patch, login_patch = self._auth(admin=True)
        with auth_patch, login_patch, patch(
            'services.supplier_catalog_refresh.enqueue_catalog_refresh',
        ) as enqueue:
            response = self.client.post(
                f'/admin/suppliers/{self.supplier_id}/sync',
                data=MultiDict([
                    ('catalog_refresh_baseline', ''),
                    ('catalog_refresh_baseline', ''),
                ]),
            )

        self.assertEqual(response.status_code, 303)
        enqueue.assert_not_called()

    def test_status_get_is_exact_supplier_scoped_read_only_and_uncached(self):
        now = datetime(2026, 11, 21, 10, 0)
        db.session.add(SupplierCatalogRefreshRun(
            id=RUN_ID,
            supplier_id=self.supplier_id,
            admin_user_id=self.admin_id,
            status='completed',
            config_digest='a' * 64,
            created_at=now,
            started_at=now,
            raw_committed_at=now,
            completed_at=now,
            total=5,
            added=2,
            updated=3,
            postprocess_errors=0,
            price_status='partial',
            price_conflicts=1,
        ))
        db.session.commit()
        auth_patch, login_patch = self._auth(admin=True)
        with auth_patch, login_patch, \
             patch.object(db.session, 'commit', wraps=db.session.commit) as commit:
            response = self.client.get(
                f'/admin/suppliers/{self.supplier_id}/sync-status?run_id={RUN_ID}',
            )
            wrong_scope = self.client.get(
                f'/admin/suppliers/{self.supplier_id + 100}/sync-status?run_id={RUN_ID}',
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.get_json()), {
            'id', 'status', 'created_at', 'started_at', 'raw_committed_at',
            'completed_at', 'trigger_kind', 'due_at', 'total', 'added',
            'updated', 'postprocess_errors', 'price_status', 'price_conflicts',
            'error_code',
        })
        self.assertEqual(response.get_json()['id'], RUN_ID)
        self.assertEqual(response.get_json()['status'], 'completed')
        self.assertEqual(response.get_json()['trigger_kind'], 'manual')
        self.assertIsNone(response.get_json()['due_at'])
        self.assertEqual(response.get_json()['total'], 5)
        self.assertIn('no-store', response.headers['Cache-Control'])
        self.assertEqual(wrong_scope.status_code, 404)
        commit.assert_not_called()

    def test_status_get_requires_one_canonical_run_id_and_admin(self):
        auth_patch, login_patch = self._auth(admin=True)
        with auth_patch, login_patch:
            missing = self.client.get(
                f'/admin/suppliers/{self.supplier_id}/sync-status',
            )
            repeated = self.client.get(
                f'/admin/suppliers/{self.supplier_id}/sync-status'
                '?run_id=bad&run_id=bad',
            )
            malformed = self.client.get(
                f'/admin/suppliers/{self.supplier_id}/sync-status?run_id=not-a-uuid',
            )

        self.assertEqual(missing.status_code, 400)
        self.assertEqual(repeated.status_code, 400)
        self.assertEqual(malformed.status_code, 400)

        seller_auth, seller_login = self._auth(admin=False)
        with seller_auth, seller_login:
            denied = self.client.get(
                f'/admin/suppliers/{self.supplier_id}/sync-status?run_id={RUN_ID}',
            )
        self.assertEqual(denied.status_code, 302)

    def test_products_page_renders_exact_latest_baseline_and_active_state(self):
        now = datetime(2026, 11, 21, 10, 0)
        db.session.add(SupplierCatalogRefreshRun(
            id=RUN_ID,
            supplier_id=self.supplier_id,
            admin_user_id=self.admin_id,
            status='pending',
            config_digest='a' * 64,
            created_at=now,
        ))
        db.session.commit()
        auth_patch, login_patch = self._auth(admin=True)
        with auth_patch, login_patch:
            response = self.client.get(
                f'/admin/suppliers/{self.supplier_id}/products',
            )

        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn('id="catalog-refresh-status"', page)
        self.assertIn('data-refresh-state="pending"', page)
        self.assertIn(f'data-refresh-run-id="{RUN_ID}"', page)
        self.assertIn(f'data-refresh-latest-run-id="{RUN_ID}"', page)
        self.assertIn(f'name="catalog_refresh_baseline" value="{RUN_ID}"', page)
        self.assertIn('data-refresh-latest-active="true"', page)
        self.assertIn('data-testid="catalog-refresh-submit"', page)
        self.assertIn('data-testid="catalog-refresh-counts" hidden', page)
        self.assertIn(
            f'/admin/suppliers/{self.supplier_id}/sync-status?run_id={RUN_ID}',
            page,
        )

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

    def test_edit_shows_only_sanitized_current_color_observation_status(self):
        digest = 'a' * 64
        status = {
            'last_attempt': {
                'status': 'succeeded',
                'outcome': 'snapshot_saved',
                'fetch_attempted': True,
                'reserved_at': '2026-11-23T09:00:00Z',
                'finished_at': '2026-11-23T09:00:04Z',
                'cooldown_remaining_seconds': 86_396,
                'config_matches': True,
                'raw_body': 'PRIVATE_RAW_BODY_MARKER',
                'error': 'PRIVATE_ERROR_MARKER',
                'url': 'https://private.invalid/PRIVATE_URL_MARKER',
                'token': 'PRIVATE_TOKEN_MARKER',
            },
            'last_snapshot': {
                'status': 'fresh',
                'at': '2026-11-23T09:00:04Z',
                'age_seconds': 5,
                'observation_count': 7,
                'fresh': True,
                'config_matches': True,
                'colors': ['PRIVATE_COLOR_MARKER'],
                'supplier_product_id': 987654321,
                'external_id_sha256': 'PRIVATE_HASH_MARKER',
            },
        }
        store = SimpleNamespace(
            read_status=Mock(return_value=status),
            reserve=Mock(),
            finish=Mock(),
        )
        auth_patch, login_patch = self._auth(admin=True)
        with auth_patch, login_patch, \
             patch(
                 'services.supplier_color_observation_store.'
                 'configured_supplier_color_observation_store',
                 return_value=store,
             ) as configured_store, \
             patch(
                 'services.supplier_service.supplier_catalog_config_digest',
                 return_value=digest,
             ) as config_digest, \
             patch.object(db.session, 'commit', wraps=db.session.commit) as commit:
            response = self.client.get(
                f'/admin/suppliers/{self.supplier_id}/edit',
            )

        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn('data-supplier-color-observation-status', page)
        self.assertIn('data-supplier-color-last-attempt', page)
        self.assertIn('снимок сохранён', page)
        self.assertIn('data-supplier-color-last-snapshot', page)
        self.assertIn('товаров: 7', page)
        for private_value in (
            'PRIVATE_RAW_BODY_MARKER', 'PRIVATE_ERROR_MARKER',
            'PRIVATE_URL_MARKER', 'PRIVATE_TOKEN_MARKER',
            'PRIVATE_COLOR_MARKER', 'PRIVATE_HASH_MARKER',
            '987654321',
        ):
            self.assertNotIn(private_value, page)
        configured_store.assert_called_once_with()
        config_digest.assert_called_once()
        store.read_status.assert_called_once()
        read_args, read_kwargs = store.read_status.call_args
        self.assertEqual(read_args, (self.supplier_id, digest))
        self.assertEqual(set(read_kwargs), {'now'})
        self.assertIsInstance(read_kwargs['now'], datetime)
        store.reserve.assert_not_called()
        store.finish.assert_not_called()
        commit.assert_not_called()

    def test_edit_reports_unknown_color_status_without_fabricating_counts(self):
        store = SimpleNamespace(read_status=Mock(return_value=None))
        auth_patch, login_patch = self._auth(admin=True)
        with auth_patch, login_patch, patch(
            'services.supplier_color_observation_store.'
            'configured_supplier_color_observation_store',
            return_value=store,
        ):
            response = self.client.get(
                f'/admin/suppliers/{self.supplier_id}/edit',
            )

        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn(
            'Состояние проверки пока неизвестно.', page,
        )
        status_panel = page.split(
            'data-supplier-color-observation-status', 1,
        )[1].split('</details>', 1)[0]
        self.assertNotIn('товаров:', status_panel)
        self.assertNotIn('data-supplier-color-last-attempt', status_panel)

    def test_edit_hides_counts_for_stale_or_config_mismatched_snapshots(self):
        cases = (
            ('stale', True, False),
            ('config_mismatch', False, False),
        )
        auth_patch, login_patch = self._auth(admin=True)
        for snapshot_status, config_matches, fresh in cases:
            payload = {
                'last_attempt': {
                    'status': 'never_reserved',
                    'outcome': None,
                    'config_matches': None,
                    'reserved_at': None,
                    'finished_at': None,
                },
                'last_snapshot': {
                    'status': snapshot_status,
                    'at': '2026-11-20T09:00:00Z',
                    'age_seconds': 400_000,
                    'observation_count': 7,
                    'fresh': fresh,
                    'config_matches': config_matches,
                },
            }
            store = SimpleNamespace(read_status=Mock(return_value=payload))
            with auth_patch, login_patch, patch(
                'services.supplier_color_observation_store.'
                'configured_supplier_color_observation_store',
                return_value=store,
            ):
                response = self.client.get(
                    f'/admin/suppliers/{self.supplier_id}/edit',
                )

            self.assertEqual(response.status_code, 200)
            panel = response.get_data(as_text=True).split(
                'data-supplier-color-observation-status', 1,
            )[1].split('</details>', 1)[0]
            self.assertNotIn('товаров:', panel)

    def test_catalog_only_digest_excludes_price_settings_and_legacy_digest_is_unchanged(self):
        supplier = SimpleNamespace(
            id=17,
            created_at=datetime(2026, 11, 21, 12, 34, 56),
            code='digest-fixture',
            csv_source_url='https://supplier.invalid/catalog.csv',
            csv_delimiter='|',
            csv_encoding='utf-8',
            csv_column_mapping={'title': {'type': 'string', 'column': 1}},
            csv_has_header=True,
            is_active=True,
            price_file_url='https://supplier.invalid/prices.csv',
            price_file_encoding='utf-8-sig',
            price_file_delimiter=',',
        )
        legacy_snapshot = {
            'supplier_id': 17,
            'created_at': '2026-11-21T12:34:56',
            'code': 'digest-fixture',
            'csv_source_url': 'https://supplier.invalid/catalog.csv',
            'csv_delimiter': '|',
            'csv_encoding': 'utf-8',
            'csv_column_mapping': {'title': {'type': 'string', 'column': 1}},
            'csv_has_header': True,
            'is_active': True,
            'price_file_url': 'https://supplier.invalid/prices.csv',
            'price_file_encoding': 'utf-8-sig',
            'price_file_delimiter': ',',
        }
        legacy_canonical = json.dumps(
            legacy_snapshot,
            sort_keys=True,
            ensure_ascii=False,
            separators=(',', ':'),
            allow_nan=False,
        )
        expected_legacy = hashlib.sha256(
            legacy_canonical.encode('utf-8'),
        ).hexdigest()

        legacy_digest = SupplierService.supplier_catalog_config_digest(supplier)
        catalog_digest = SupplierService.supplier_catalog_config_digest(
            supplier, catalog_only=True,
        )
        supplier.price_file_url = 'https://supplier.invalid/prices-v2.csv'
        supplier.price_file_encoding = 'cp1251'
        supplier.price_file_delimiter = ';'

        self.assertEqual(legacy_digest, expected_legacy)
        self.assertNotEqual(
            SupplierService.supplier_catalog_config_digest(supplier),
            expected_legacy,
        )
        self.assertEqual(
            SupplierService.supplier_catalog_config_digest(
                supplier, catalog_only=True,
            ),
            catalog_digest,
        )

    def test_catalog_only_sync_checks_scope_before_get_and_skips_price_followup(self):
        supplier = db.session.get(Supplier, self.supplier_id)
        supplier.price_file_url = 'https://supplier.invalid/prices.csv'
        supplier.price_file_encoding = 'utf-8'
        supplier.price_file_delimiter = '|'
        user = User(username='catalog-only-seller', email='catalog-only@test.local')
        user.set_password('synthetic-password')
        seller = Seller(user=user, company_name='Catalog-only seller')
        source = SupplierProduct(
            supplier_id=self.supplier_id,
            external_id='123',
            title='Previous catalog title',
            supplier_price=44.0,
            supplier_quantity=8,
            original_data_json='{"old":true}',
        )
        db.session.add_all([seller, source])
        db.session.flush()
        db.session.add(SellerSupplier(
            seller_id=seller.id,
            supplier_id=self.supplier_id,
            is_active=True,
        ))
        imported = ImportedProduct(
            seller_id=seller.id,
            supplier_id=self.supplier_id,
            supplier_product_id=source.id,
            external_id='seller-copy-123',
            supplier_price=44.0,
            supplier_quantity=8,
            calculated_price=100.0,
            content_overrides_json='{"schema_version":1,"fields":{}}',
        )
        db.session.add(imported)
        seller_id = seller.id
        db.session.commit()
        expected_digest = SupplierService.supplier_catalog_config_digest(
            supplier, catalog_only=True,
        )
        events = []
        product_data = {
            'external_id': '123',
            'vendor_code': 'SKU-123',
            'title': 'Scheduled catalog item',
            'description': 'Catalog description',
            'supplier_price': 31.0,
            'supplier_quantity': 6,
            'all_categories': ['Fixture'],
            'colors': [],
            'materials': [],
            'barcodes': [],
            'photo_urls': [],
            'characteristics': {},
        }
        preflight = SimpleNamespace(is_valid=True, warnings=[], errors=[])

        def pre_fetch_check():
            events.append('scope')
            checked_supplier = Supplier.query.filter_by(
                id=self.supplier_id,
            ).populate_existing().first()
            self.assertIsNotNone(checked_supplier)
            self.assertTrue(db.session().in_transaction())

        def fetch_raw():
            events.append('fetch')
            self.assertFalse(db.session().in_transaction())
            return b'synthetic catalog'

        def checkpoint(result, raw_at):
            events.append('checkpoint')
            self.assertTrue(db.session().in_transaction())
            self.assertEqual(result.total_in_csv, 1)
            self.assertIsInstance(raw_at, datetime)

        parse_result = SimpleNamespace(
            errors=0,
            brand_resolved_count=0,
            category_mapped_count=0,
            avg_readiness_score=0.0,
        )
        with patch.object(
            SupplierCSVParser,
            'fetch_csv_raw',
            side_effect=fetch_raw,
        ), patch.object(
            SupplierCSVParser,
            'preflight_raw',
            return_value=([product_data], preflight),
        ), patch(
            'services.smart_product_parser.SmartProductParser.parse_and_apply_bulk',
            return_value=parse_result,
        ), patch.object(
            SupplierService,
            'sync_prices_and_stock',
        ) as price_followup:
            result = SupplierService._sync_from_csv_locked(
                self.supplier_id,
                start_time=time.time(),
                expected_config_digest=expected_digest,
                catalog_only=True,
                pre_fetch_check=pre_fetch_check,
                raw_checkpoint=checkpoint,
            )

        self.assertTrue(result.success)
        self.assertEqual(result.price_followup_status, 'skipped')
        self.assertEqual(events, ['scope', 'fetch', 'checkpoint'])
        price_followup.assert_not_called()
        db.session.expire_all()
        source = SupplierProduct.query.filter_by(
            supplier_id=self.supplier_id,
            external_id='123',
        ).one()
        imported = ImportedProduct.query.filter_by(
            seller_id=seller_id,
            supplier_id=self.supplier_id,
        ).one()
        self.assertEqual(source.title, 'Scheduled catalog item')
        self.assertEqual(source.supplier_price, 31.0)
        self.assertEqual(source.supplier_quantity, 6)
        self.assertEqual(imported.supplier_price, 44.0)
        self.assertEqual(imported.supplier_quantity, 8)
        self.assertEqual(imported.calculated_price, 100.0)

    def test_catalog_pre_fetch_fence_preserves_safe_error_and_blocks_get(self):
        class FenceError(Exception):
            code = 'scheduled_consent_revoked'

        supplier = db.session.get(Supplier, self.supplier_id)
        expected_digest = SupplierService.supplier_catalog_config_digest(
            supplier, catalog_only=True,
        )
        with patch.object(SupplierCSVParser, 'fetch_csv_raw') as fetch_raw:
            result = SupplierService._sync_from_csv_locked(
                self.supplier_id,
                start_time=time.time(),
                expected_config_digest=expected_digest,
                catalog_only=True,
                pre_fetch_check=lambda: (_ for _ in ()).throw(FenceError()),
            )

        self.assertFalse(result.success)
        self.assertEqual(result.catalog_error_code, 'scheduled_consent_revoked')
        self.assertEqual(result.price_followup_status, 'skipped')
        fetch_raw.assert_not_called()

    def test_manual_catalog_sync_runs_price_followup_under_held_shared_lock(self):
        supplier = db.session.get(Supplier, self.supplier_id)
        supplier.price_file_url = 'https://supplier.invalid/prices.csv'
        supplier.price_file_encoding = 'utf-8'
        supplier.price_file_delimiter = '|'
        db.session.commit()
        product_data = {
            'external_id': '123',
            'vendor_code': 'SKU-123',
            'title': 'Manual catalog item',
            'description': 'Catalog description',
            'supplier_price': 31.0,
            'supplier_quantity': 6,
            'all_categories': ['Fixture'],
            'colors': [],
            'materials': [],
            'barcodes': [],
            'photo_urls': [],
            'characteristics': {},
        }
        preflight = SimpleNamespace(is_valid=True, warnings=[], errors=[])
        parse_result = SimpleNamespace(
            errors=0,
            brand_resolved_count=0,
            category_mapped_count=0,
            avg_readiness_score=0.0,
        )
        feed = SupplierPriceFeed(
            rows={123: SupplierPriceRow(
                vendor_code=None,
                price=27.0,
                quantity=2,
                status=None,
                additional_vendor_code=None,
                barcode=None,
                rrp=None,
            )},
            conflicting_ids=frozenset(),
        )
        with patch.object(
            SupplierCSVParser,
            'fetch_csv_raw',
            return_value=b'synthetic catalog',
        ), patch.object(
            SupplierCSVParser,
            'preflight_raw',
            return_value=([product_data], preflight),
        ), patch(
            'services.smart_product_parser.SmartProductParser.parse_and_apply_bulk',
            return_value=parse_result,
        ), patch(
            'services.supplier_price_sync.read_supplier_price_feed',
            return_value=feed,
        ) as read_price_feed:
            result = SupplierService.sync_from_csv(self.supplier_id)

        self.assertTrue(result.success)
        self.assertEqual(result.price_followup_status, 'success')
        product = SupplierProduct.query.filter_by(
            supplier_id=self.supplier_id,
            external_id='123',
        ).one()
        self.assertEqual(product.supplier_price, 27.0)
        read_price_feed.assert_called_once_with(
            'https://supplier.invalid/prices.csv',
            encoding='utf-8',
            delimiter='|',
        )

    def test_non_admin_edit_does_not_read_color_observation_store(self):
        auth_patch, login_patch = self._auth(admin=False)
        with auth_patch, login_patch, patch(
            'services.supplier_color_observation_store.'
            'configured_supplier_color_observation_store',
        ) as configured_store:
            response = self.client.get(
                f'/admin/suppliers/{self.supplier_id}/edit',
            )

        self.assertEqual(response.status_code, 302)
        configured_store.assert_not_called()


if __name__ == '__main__':
    unittest.main()
