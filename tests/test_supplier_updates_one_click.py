# -*- coding: utf-8 -*-
"""Обновления поставщика в один клик: индикация, refresh, CTA, уведомление."""

import json
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from flask import Flask

from models import (
    ImportedProduct,
    Notification,
    Seller,
    Supplier,
    SupplierProduct,
    User,
    db,
)
from services.product_sync_scheduler import (
    SUPPLIER_UPDATES_NOTIFICATION_TITLE,
    notify_supplier_updates,
)


class SupplierUpdatesOneClickTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(
            TESTING=True,
            SECRET_KEY='one-click-test',
            SQLALCHEMY_DATABASE_URI='sqlite://',
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
        )
        db.init_app(self.app)
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()

        user = User(
            username='oneclick', email='oneclick@test.local', is_active=True,
        )
        user.set_password('synthetic-password')
        db.session.add(user)
        db.session.flush()
        seller = Seller(user_id=user.id, company_name='One Click')
        supplier = Supplier(name='S', code='one-click-supplier')
        db.session.add_all([seller, supplier])
        db.session.commit()
        self.seller_id = seller.id
        self.supplier_id = supplier.id

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def _pair(self, sp_revision=2, copied_revision=1, **imp_overrides):
        sp = SupplierProduct(
            supplier_id=self.supplier_id,
            external_id=f'ext-{SupplierProduct.query.count() + 1}',
            title='Товар',
            content_revision=sp_revision,
        )
        db.session.add(sp)
        db.session.flush()
        values = dict(
            seller_id=self.seller_id,
            supplier_product_id=sp.id,
            supplier_id=self.supplier_id,
            supplier_content_revision=copied_revision,
            external_id=sp.external_id,
            title='Товар',
            import_status='pending',
        )
        values.update(imp_overrides)
        imp = ImportedProduct(**values)
        db.session.add(imp)
        db.session.commit()
        return sp, imp

    def test_notify_creates_single_notification_per_day(self):
        self._pair(sp_revision=3, copied_revision=1)
        self._pair(sp_revision=2, copied_revision=1)
        fake_app = SimpleNamespace(app_context=self.app.app_context)

        notify_supplier_updates(fake_app)
        notes = Notification.query.filter_by(
            seller_id=self.seller_id,
            title=SUPPLIER_UPDATES_NOTIFICATION_TITLE,
        ).all()
        self.assertEqual(len(notes), 1)
        self.assertIn('2', notes[0].message)
        self.assertEqual(notes[0].link, '/my-products?updates=1')

        # Повторный тик в те же сутки не создаёт дубль
        notify_supplier_updates(fake_app)
        self.assertEqual(Notification.query.filter_by(
            seller_id=self.seller_id,
            title=SUPPLIER_UPDATES_NOTIFICATION_TITLE,
        ).count(), 1)

    def test_notify_skips_seller_without_updates(self):
        self._pair(sp_revision=1, copied_revision=1)
        fake_app = SimpleNamespace(app_context=self.app.app_context)
        notify_supplier_updates(fake_app)
        self.assertEqual(Notification.query.count(), 0)

    def test_notify_fires_again_after_24_hours(self):
        self._pair(sp_revision=3, copied_revision=1)
        fake_app = SimpleNamespace(app_context=self.app.app_context)
        notify_supplier_updates(fake_app)
        note = Notification.query.filter_by(
            title=SUPPLIER_UPDATES_NOTIFICATION_TITLE,
        ).one()
        note.created_at = datetime.utcnow() - timedelta(hours=25)
        db.session.commit()
        notify_supplier_updates(fake_app)
        self.assertEqual(Notification.query.filter_by(
            title=SUPPLIER_UPDATES_NOTIFICATION_TITLE,
        ).count(), 2)


class RefreshFromSupplierRouteTest(unittest.TestCase):
    """Тонкий route: валидация ids и tenant scope."""

    def setUp(self):
        import os
        os.environ.setdefault('SECRET_KEY', 'test-secret-key-for-unit-tests')
        os.environ.setdefault('DISABLE_SECURE_COOKIE', '1')
        import seller_platform as app_module
        self.app = app_module.app
        self.app.config['TESTING'] = True
        self.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.app.test_client()

    def _login(self):
        from unittest.mock import MagicMock
        seller = MagicMock()
        seller.id = 7
        user = MagicMock()
        user.is_authenticated = True
        user.seller = seller
        return user

    def test_rejects_non_numeric_ids(self):
        from unittest.mock import patch
        user = self._login()
        with patch('routes.suppliers.current_user', user), \
             patch('flask_login.utils._get_user', return_value=user):
            resp = self.client.post(
                '/my-products/refresh-from-supplier?updates=1&supplier=2',
                data={'selected_ids': ['abc']},
            )
        self.assertEqual(resp.status_code, 302)
        query = parse_qs(urlsplit(resp.headers['Location']).query)
        self.assertEqual(query, {'updates': ['1'], 'supplier': ['2']})

    def test_rejects_foreign_or_missing_ids(self):
        from unittest.mock import patch, MagicMock
        user = self._login()
        with patch('routes.suppliers.current_user', user), \
             patch('flask_login.utils._get_user', return_value=user), \
             patch('routes.suppliers.ImportedProduct') as MockImported, \
             patch('routes.suppliers.SupplierService') as MockService:
            MockImported.query.filter.return_value.all.return_value = []
            resp = self.client.post(
                '/my-products/refresh-from-supplier',
                data={'selected_ids': ['5']},
            )
            MockService.update_seller_products.assert_not_called()
        self.assertEqual(resp.status_code, 302)

    def test_success_keeps_updates_view_and_drops_conflicting_status(self):
        from unittest.mock import MagicMock, patch
        user = self._login()
        row = SimpleNamespace(
            supplier_product_id=13,
            import_status='imported',
            product_id=99,
        )
        with patch('routes.suppliers.current_user', user), \
             patch('flask_login.utils._get_user', return_value=user), \
             patch('routes.suppliers.ImportedProduct') as MockImported, \
             patch('routes.suppliers.SupplierService') as MockService:
            MockImported.query.filter.return_value.all.return_value = [row]
            MockService.update_seller_products.return_value = SimpleNamespace(
                imported=1,
            )
            resp = self.client.post(
                '/my-products/refresh-from-supplier'
                '?updates=1&status=imported&supplier=2',
                data={'selected_ids': ['5']},
            )

        self.assertEqual(resp.status_code, 302)
        query = parse_qs(urlsplit(resp.headers['Location']).query)
        self.assertEqual(query['updates'], ['1'])
        self.assertEqual(query['supplier'], ['2'])
        self.assertEqual(query['refreshed'], ['1'])
        self.assertEqual(query['wb_ready'], ['1'])
        self.assertNotIn('status', query)
        MockService.update_seller_products.assert_called_once_with(7, [13])

    def test_single_delete_keeps_regular_status_and_filters(self):
        from unittest.mock import MagicMock, patch
        user = self._login()
        product = SimpleNamespace(title='Удаляемый', external_id='ext')
        with patch('routes.suppliers.current_user', user), \
             patch('flask_login.utils._get_user', return_value=user), \
             patch('routes.suppliers.ImportedProduct') as MockImported, \
             patch('routes.suppliers.db') as MockDb:
            MockImported.query.filter_by.return_value.first_or_404.return_value = (
                product
            )
            resp = self.client.post(
                '/my-products/5/delete?status=failed&supplier=2',
            )

        self.assertEqual(resp.status_code, 302)
        query = parse_qs(urlsplit(resp.headers['Location']).query)
        self.assertEqual(
            query,
            {'status': ['failed'], 'supplier': ['2']},
        )
        MockDb.session.delete.assert_called_once_with(product)


class MyProductsSupplierUpdatesViewContractTest(unittest.TestCase):
    def test_updates_view_uses_full_bulk_budget(self):
        from routes.suppliers import (
            MY_PRODUCTS_PAGE_SIZE,
            MY_PRODUCTS_UPDATES_PAGE_SIZE,
        )

        self.assertEqual(MY_PRODUCTS_PAGE_SIZE, 200)
        self.assertEqual(MY_PRODUCTS_UPDATES_PAGE_SIZE, 200)

    def test_return_args_make_updates_and_status_mutually_exclusive(self):
        from routes.suppliers import _my_products_return_args

        self.assertEqual(
            _my_products_return_args({
                'updates': '1',
                'status': 'imported',
                'supplier': '9',
                'sort': 'oldest',
                'unknown': 'ignored',
            }),
            {'updates': 1, 'supplier': 9, 'sort': 'oldest'},
        )

    def test_template_keeps_and_focuses_supplier_updates_view(self):
        template = (
            Path(__file__).resolve().parents[1]
            / 'templates'
            / 'seller_my_products.html'
        ).read_text(encoding='utf-8')

        self.assertIn('name="updates" value="1"', template)
        self.assertIn(
            "url_for('my_products_refresh_from_supplier', **page_args)",
            template,
        )
        self.assertIn('Применить в мои карточки', template)
        self.assertIn('{% if not updates_filter %}', template)
        self.assertIn('WB и Ozon на этом шаге не меняются', template)

    def test_my_products_uses_one_click_ozon_upload_journal(self):
        template = (
            Path(__file__).resolve().parents[1]
            / 'templates'
            / 'seller_my_products.html'
        ).read_text(encoding='utf-8')

        self.assertIn("url_for('ozon_bulk_uploads.create')", template)
        self.assertIn('name="imported_product_ids"', template)
        self.assertIn('Синхронизировать Ozon', template)
        self.assertIn('Обновить Ozon', template)
        self.assertIn('name="confirm_write"', template)
        self.assertIn('Подключить Ozon', template)

    def test_characteristics_handoff_is_explicit_and_keeps_photos_off(self):
        template = (
            Path(__file__).resolve().parents[1]
            / 'templates'
            / 'products_enrich_bulk.html'
        ).read_text(encoding='utf-8')

        self.assertIn('Отправка характеристик на WB', template)
        self.assertIn(
            'характеристики и габариты. Название, описание, бренд '
            'и фотографии не изменятся',
            template,
        )
        self.assertIn('photos: false', template)
        self.assertIn('characteristics: true', template)
        self.assertIn('dimensions: true', template)


if __name__ == '__main__':
    unittest.main()
