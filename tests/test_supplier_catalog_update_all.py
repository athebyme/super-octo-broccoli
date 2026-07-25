# -*- coding: utf-8 -*-
"""Кнопка «Обновить данные» в каталоге поставщика.

Форма на карточке поставщика не передаёт список ID — она просит обновить все
импортированные карточки этого поставщика. До появления явного флага роут
всегда отвечал «Не выбраны карточки для обновления», то есть кнопка не работала
вообще. Набор ID всегда собирается seller-scoped на сервере.
"""

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch


class SupplierCatalogUpdateAllTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ['DISABLE_SECURE_COOKIE'] = '1'
        os.environ.setdefault('SKIP_SCHEDULER', '1')
        import sqlalchemy as _sa
        from sqlalchemy.pool import StaticPool
        import seller_platform  # noqa
        from models import db
        cls.app = seller_platform.app
        cls.app.config.update(
            SQLALCHEMY_DATABASE_URI='sqlite:///:memory:',
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            SQLALCHEMY_ENGINE_OPTIONS={},
            WTF_CSRF_ENABLED=False,
            TESTING=True,
        )
        cls._engine = _sa.create_engine(
            'sqlite:///:memory:',
            connect_args={'check_same_thread': False},
            poolclass=StaticPool,
        )
        db._app_engines[cls.app] = {None: cls._engine}
        cls.db = db

    def setUp(self):
        from models import ImportedProduct, Seller, Supplier, User
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.db.create_all()
        self.client = self.app.test_client()

        user = User(username='cat-owner', email='cat@test.local', is_active=True)
        user.set_password('synthetic-password')
        seller = Seller(user=user, company_name='Каталожник')
        other_user = User(
            username='cat-foreign',
            email='foreign@test.local',
            is_active=True,
        )
        other_user.set_password('synthetic-password')
        other_seller = Seller(user=other_user, company_name='Чужой')
        supplier = Supplier(name='Опт', code='opt')
        self.db.session.add_all([seller, other_seller, supplier])
        self.db.session.flush()

        own = [
            ImportedProduct(
                seller_id=seller.id,
                supplier_id=supplier.id,
                external_id=f'own-{index}',
                source_type='synthetic',
                title=f'Товар {index}',
            )
            for index in range(3)
        ]
        foreign = ImportedProduct(
            seller_id=other_seller.id,
            supplier_id=supplier.id,
            external_id='foreign-1',
            source_type='synthetic',
            title='Чужой товар',
        )
        self.db.session.add_all(own + [foreign])
        self.db.session.commit()

        self.seller_id = seller.id
        self.supplier_id = supplier.id
        self.own_ids = sorted(row.id for row in own)
        self.foreign_id = foreign.id
        self.user = SimpleNamespace(
            id=user.id,
            username=user.username,
            seller=seller,
            is_authenticated=True,
            is_active=True,
            is_admin=False,
        )

    def tearDown(self):
        self.db.session.remove()
        self.db.drop_all()
        self.ctx.pop()

    def _post(self, data):
        with patch('flask_login.utils._get_user', return_value=self.user), \
                patch('routes.suppliers.current_user', self.user):
            return self.client.post(
                '/supplier-catalog/update',
                data=data,
                follow_redirects=False,
            )

    def test_update_all_collects_only_own_products(self):
        captured = {}

        def _fake_update(seller_id, product_ids):
            captured['seller_id'] = seller_id
            captured['product_ids'] = sorted(product_ids)
            return SimpleNamespace(imported=len(product_ids), errors=0)

        with patch(
            'services.supplier_service.SupplierService.update_seller_products',
            side_effect=_fake_update,
        ):
            response = self._post({
                'supplier_id': str(self.supplier_id),
                'update_all_for_supplier': '1',
            })

        self.assertEqual(response.status_code, 302)
        self.assertEqual(captured['seller_id'], self.seller_id)
        self.assertEqual(captured['product_ids'], self.own_ids)
        self.assertNotIn(self.foreign_id, captured['product_ids'])

    def test_without_flag_and_ids_nothing_is_updated(self):
        with patch(
            'services.supplier_service.SupplierService.update_seller_products',
        ) as updater:
            response = self._post({'supplier_id': str(self.supplier_id)})
        self.assertEqual(response.status_code, 302)
        updater.assert_not_called()


if __name__ == '__main__':
    unittest.main()
