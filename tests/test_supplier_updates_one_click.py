# -*- coding: utf-8 -*-
"""Обновления поставщика в один клик: индикация, refresh, CTA, уведомление."""

import json
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace

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
                '/my-products/refresh-from-supplier',
                data={'selected_ids': ['abc']},
            )
        self.assertEqual(resp.status_code, 302)

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


if __name__ == '__main__':
    unittest.main()
