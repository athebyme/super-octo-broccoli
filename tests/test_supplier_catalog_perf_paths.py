# -*- coding: utf-8 -*-
"""Тесты оптимизированных путей каталога поставщика.

- get_product_stats переписан в один агрегатный запрос: семантика счётчиков
  обязана совпадать со старой (9 отдельных запросов).
- has_photos — дешёвая проверка наличия фото без полного json.loads.
- /supplier-catalog/<id>/products: бейджи «на WB»/«импортирован» считаются
  постранично без wb-фильтра и по всему каталогу с wb-фильтром.
"""

import json
import os
import unittest


class _DBTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ['DISABLE_SECURE_COOKIE'] = '1'
        import sqlalchemy as _sa
        from sqlalchemy.pool import StaticPool
        import seller_platform  # noqa
        from models import db
        cls.app = seller_platform.app
        cls.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        cls.app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
        cls.app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {}
        cls.app.config['WTF_CSRF_ENABLED'] = False
        cls.app.config['TESTING'] = True
        cls._engine = _sa.create_engine(
            'sqlite:///:memory:',
            connect_args={'check_same_thread': False},
            poolclass=StaticPool,
        )
        db._app_engines[cls.app] = {None: cls._engine}
        cls.db = db

    def setUp(self):
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.db.create_all()

    def tearDown(self):
        self.db.session.remove()
        self.db.drop_all()
        self.ctx.pop()


class TestGetProductStatsAggregate(_DBTestCase):
    def test_counts_match_old_semantics(self):
        from models import Supplier, SupplierProduct
        from services.supplier_service import SupplierService

        supplier = Supplier(name='Опт', code='opt')
        other = Supplier(name='Другой', code='other')
        self.db.session.add_all([supplier, other])
        self.db.session.flush()

        rows = [
            # status, ai_validated, photos, brand, category
            ('draft', False, None, 'Alfa', 'Сумки'),
            ('draft', True, '["u1"]', 'Alfa', 'Сумки'),
            ('validated', False, '[]', '', 'Ремни'),
            ('ready', True, '["u2","u3"]', 'Beta', None),
            ('archived', False, None, None, ''),
        ]
        for idx, (status, ai_ok, photos, brand, category) in enumerate(rows):
            self.db.session.add(SupplierProduct(
                supplier_id=supplier.id, external_id=f'e-{idx}',
                title='Товар', status=status, ai_validated=ai_ok,
                photo_urls_json=photos, brand=brand, category=category,
            ))
        # Чужой поставщик не должен попадать в счётчики
        self.db.session.add(SupplierProduct(
            supplier_id=other.id, external_id='x', title='Чужой',
            status='ready', brand='Gamma', category='Обувь',
            photo_urls_json='["u"]',
        ))
        self.db.session.commit()

        stats = SupplierService.get_product_stats(supplier.id)
        self.assertEqual(stats['total'], 5)
        self.assertEqual(stats['draft'], 2)
        self.assertEqual(stats['validated'], 1)
        self.assertEqual(stats['ready'], 1)
        self.assertEqual(stats['archived'], 1)
        self.assertEqual(stats['ai_validated'], 2)
        # with_photos: непустой JSON-список ('[]' не считается)
        self.assertEqual(stats['with_photos'], 2)
        # brands: Alfa + Beta ('' и NULL не считаются, дубль Alfa — один)
        self.assertEqual(stats['brands'], 2)
        # categories: Сумки + Ремни
        self.assertEqual(stats['categories'], 2)

    def test_empty_supplier(self):
        from models import Supplier
        from services.supplier_service import SupplierService
        supplier = Supplier(name='Пустой', code='empty')
        self.db.session.add(supplier)
        self.db.session.commit()
        stats = SupplierService.get_product_stats(supplier.id)
        self.assertEqual(stats, {
            'total': 0, 'draft': 0, 'validated': 0, 'ready': 0,
            'archived': 0, 'ai_validated': 0, 'with_photos': 0,
            'brands': 0, 'categories': 0,
        })


class TestHasPhotos(_DBTestCase):
    def test_has_photos_cheap_check(self):
        from models import Supplier, SupplierProduct
        supplier = Supplier(name='Опт', code='opt2')
        self.db.session.add(supplier)
        self.db.session.flush()
        sp = SupplierProduct(supplier_id=supplier.id, external_id='p1',
                             title='Товар')
        for raw, expected in (
            (None, False), ('', False), ('[]', False), ('null', False),
            (json.dumps(['https://x/1.jpg']), True),
        ):
            sp.photo_urls_json = raw
            self.assertEqual(sp.has_photos(), expected, raw)
            # has_photos обязан совпадать с truthiness полного get_photos()
            self.assertEqual(sp.has_photos(), bool(sp.get_photos()), raw)


class TestSupplierCatalogBadges(_DBTestCase):
    def _seed(self):
        from models import (
            User, Seller, Supplier, SellerSupplier, SupplierProduct,
            ImportedProduct, Product,
        )
        user = User(username='cat_seller', email='cat@example.com',
                    password_hash='x')
        self.db.session.add(user)
        self.db.session.flush()
        seller = Seller(user_id=user.id, company_name='Каталог',
                        wb_seller_id='555')
        self.db.session.add(seller)
        self.db.session.flush()
        supplier = Supplier(name='ОптКат', code='optcat', is_active=True)
        self.db.session.add(supplier)
        self.db.session.flush()
        self.db.session.add(SellerSupplier(
            seller_id=seller.id, supplier_id=supplier.id, is_active=True,
        ))
        sp_on_wb = SupplierProduct(
            supplier_id=supplier.id, external_id='on-wb-1',
            title='Товар уже на WB', status='ready', supplier_quantity=5,
        )
        sp_free = SupplierProduct(
            supplier_id=supplier.id, external_id='free-1',
            title='Товар свободный', status='ready', supplier_quantity=5,
        )
        self.db.session.add_all([sp_on_wb, sp_free])
        self.db.session.flush()
        product = Product(seller_id=seller.id, nm_id=111222,
                          vendor_code='VC-ONWB', title='WB карточка',
                          is_active=True)
        self.db.session.add(product)
        self.db.session.flush()
        self.db.session.add(ImportedProduct(
            seller_id=seller.id, supplier_id=supplier.id,
            supplier_product_id=sp_on_wb.id, product_id=product.id,
            title='Товар уже на WB',
        ))
        self.db.session.commit()
        return user, supplier, sp_on_wb, sp_free

    def _client(self, user):
        client = self.app.test_client()
        with client.session_transaction() as sess:
            sess['_user_id'] = str(user.id)
        return client

    def test_page_badges_without_wb_filter(self):
        user, supplier, sp_on_wb, sp_free = self._seed()
        resp = self._client(user).get(
            f'/supplier-catalog/{supplier.id}/products'
            '?show_imported=1&stock_status=all'
        )
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn('Товар уже на WB', html)
        self.assertIn('Товар свободный', html)
        # Бейдж «на WB» строится постранично и обязан сохраниться
        self.assertIn('Товар уже на Wildberries', html)
        self.assertIn('На WB', html)

    def test_wb_filter_uses_full_catalog_set(self):
        user, supplier, sp_on_wb, sp_free = self._seed()
        html_on = self._client(user).get(
            f'/supplier-catalog/{supplier.id}/products'
            '?wb_filter=on_wb&stock_status=all&show_imported=1'
        ).get_data(as_text=True)
        self.assertIn('Товар уже на WB', html_on)
        self.assertNotIn('Товар свободный', html_on)

        html_off = self._client(user).get(
            f'/supplier-catalog/{supplier.id}/products'
            '?wb_filter=not_on_wb&stock_status=all&show_imported=1'
        ).get_data(as_text=True)
        self.assertIn('Товар свободный', html_off)
        self.assertNotIn('Товар уже на WB', html_off)


if __name__ == '__main__':
    unittest.main()
