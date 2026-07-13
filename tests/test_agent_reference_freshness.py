# -*- coding: utf-8 -*-
import json
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace

from flask import Flask
from sqlalchemy import event
from werkzeug.security import generate_password_hash

from agents.catalog.category_mapper import CategoryMapperAgent
from agents.catalog.characteristics_filler import CharacteristicsFillerAgent
from agents.catalog.size_normalizer import SizeNormalizerAgent
from agents.platform_client import (
    ReferenceDataUnavailableError,
    require_usable_reference,
)
from models import (
    AgentChangeSnapshot,
    AgentTask,
    Brand,
    BrandAlias,
    ImportedProduct,
    Marketplace,
    MarketplaceCategory,
    MarketplaceCategoryCharacteristic,
    MarketplaceDirectory,
    MarketplaceBrand,
    BrandCategoryLink,
    CardEditHistory,
    Product,
    Seller,
    ServiceAgent,
    Supplier,
    User,
    db,
)
from routes.internal_api import internal_api_bp
from services.marketplace_validator import (
    WBCharacteristicValidationError,
    build_wb_characteristic_patch,
)


class AgentReferenceFreshnessApiTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        self.app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
        db.init_app(self.app)
        self.app.register_blueprint(internal_api_bp)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

        user = User(username='reference-user', email='reference@example.com', password_hash='x')
        db.session.add(user)
        db.session.flush()
        self.seller = Seller(user_id=user.id, company_name='Reference Seller')
        self.supplier = Supplier(name='Reference Supplier', code='reference-supplier')
        self.agent = ServiceAgent(
            id='reference-agent', name='reference-agent', display_name='Reference Agent',
            api_key_hash=generate_password_hash('reference-key'),
        )
        db.session.add_all([self.seller, self.supplier, self.agent])
        db.session.flush()
        self.task = AgentTask(
            id='reference-task', agent_id=self.agent.id, seller_id=self.seller.id,
            task_type='fill_single', title='Reference write', status='running',
        )
        self.marketplace = Marketplace(
            name='Wildberries', code='wb', is_active=True,
            categories_synced_at=datetime.utcnow(), categories_sync_status='success',
            total_categories=2,
            brands_synced_at=datetime.utcnow(), brands_sync_status='success',
            brands_version=1,
        )
        db.session.add_all([self.task, self.marketplace])
        db.session.flush()
        reference_brand = Brand(
            name='Reference Anchor',
            name_normalized='reference anchor',
            status='verified',
        )
        db.session.add(reference_brand)
        db.session.flush()
        db.session.add(MarketplaceBrand(
            brand_id=reference_brand.id,
            marketplace_id=self.marketplace.id,
            marketplace_brand_name='Reference Anchor',
            marketplace_brand_id=999001,
            status='verified',
            is_available=True,
            last_seen_at=datetime.utcnow(),
        ))

        self.category = MarketplaceCategory(
            marketplace_id=self.marketplace.id,
            subject_id=1001,
            subject_name='Футболки',
            parent_name='Одежда',
            is_leaf=True,
            is_enabled=True,
            is_available=True,
            last_seen_at=datetime.utcnow(),
            characteristics_synced_at=datetime.utcnow(),
            characteristics_sync_status='success',
            characteristics_count=2,
        )
        self.removed_category = MarketplaceCategory(
            marketplace_id=self.marketplace.id,
            subject_id=1002,
            subject_name='Старая категория',
            parent_name='Одежда',
            is_leaf=True,
            is_enabled=True,
            is_available=False,
            last_seen_at=datetime.utcnow() - timedelta(days=4),
            characteristics_synced_at=datetime.utcnow(),
            characteristics_sync_status='success',
            characteristics_count=1,
        )
        db.session.add_all([self.category, self.removed_category])
        db.session.flush()
        self.color = MarketplaceCategoryCharacteristic(
            marketplace_id=self.marketplace.id,
            category_id=self.category.id,
            charc_id=2001,
            name='Цвет товара',
            charc_type=1,
            required=True,
            max_count=1,
            dictionary_json=json.dumps([{'value': 'Красный'}, {'value': 'Синий'}]),
            is_enabled=True,
            is_available=True,
            last_seen_at=datetime.utcnow(),
        )
        self.length = MarketplaceCategoryCharacteristic(
            marketplace_id=self.marketplace.id,
            category_id=self.category.id,
            charc_id=2003,
            name='Длина',
            charc_type=4,
            required=False,
            unit_name='см',
            is_enabled=True,
            is_available=True,
            last_seen_at=datetime.utcnow(),
        )
        removed_char = MarketplaceCategoryCharacteristic(
            marketplace_id=self.marketplace.id,
            category_id=self.category.id,
            charc_id=2002,
            name='Устаревшее поле',
            charc_type=1,
            max_count=1,
            is_enabled=True,
            is_available=False,
            last_seen_at=datetime.utcnow() - timedelta(days=4),
        )
        db.session.add_all([self.color, self.length, removed_char])
        db.session.add(MarketplaceDirectory(
            marketplace_id=self.marketplace.id,
            directory_type='colors',
            data_json=json.dumps([
                {'name': 'Красный'},
                {'name': 'Синий'},
            ]),
            synced_at=datetime.utcnow(),
            sync_status='success',
            items_count=2,
        ))

        self.imported = ImportedProduct(
            seller_id=self.seller.id,
            supplier_id=self.supplier.id,
            title='Тестовая футболка',
            wb_subject_id=self.category.subject_id,
            characteristics='{}',
        )
        self.product = Product(
            seller_id=self.seller.id,
            nm_id=12345,
            title='Тестовая футболка WB',
            subject_id=self.category.subject_id,
            characteristics_json='[]',
        )
        db.session.add_all([self.imported, self.product])
        db.session.commit()
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    @property
    def auth(self):
        return {'X-Agent-Id': self.agent.id, 'X-Agent-Key': 'reference-key'}

    @property
    def task_auth(self):
        return {**self.auth, 'X-Task-Id': self.task.id}

    def _add_brand_binding(
        self,
        *,
        name='Local Brand',
        alias='local brand alias',
        marketplace_name='LOCAL BRAND WB',
        binding_status='verified',
        link_verified_at=None,
        link_available=True,
    ):
        brand = Brand(
            name=name,
            name_normalized=name.casefold(),
            status='verified',
        )
        db.session.add(brand)
        db.session.flush()
        db.session.add(BrandAlias(
            brand_id=brand.id,
            alias=alias,
            alias_normalized=alias.casefold(),
            source='manual',
            is_active=True,
        ))
        binding = MarketplaceBrand(
            brand_id=brand.id,
            marketplace_id=self.marketplace.id,
            marketplace_brand_name=marketplace_name,
            marketplace_brand_id=9500 + brand.id,
            status=binding_status,
            is_available=True,
        )
        db.session.add(binding)
        db.session.flush()
        db.session.add(BrandCategoryLink(
            marketplace_brand_id=binding.id,
            category_id=self.category.subject_id,
            is_available=link_available,
            verified_at=link_verified_at or datetime.utcnow(),
        ))
        db.session.commit()
        return binding

    def test_category_and_schema_reads_filter_upstream_unavailable_rows(self):
        categories = self.client.get(
            '/internal/v1/categories/search?q=одежда', headers=self.auth,
        ).get_json()
        self.assertTrue(categories['reference_status']['usable'])
        self.assertEqual([item['subject_id'] for item in categories['categories']], [1001])

        schema = self.client.get(
            '/internal/v1/categories/1001/characteristics', headers=self.auth,
        ).get_json()
        self.assertTrue(schema['reference_status']['usable'])
        self.assertEqual(
            [item['charc_id'] for item in schema['characteristics']],
            [2001, 2003],
        )
        constraints = {
            item['charc_id']: item['constraint']
            for item in schema['characteristics']
        }
        self.assertEqual(constraints[2001], {
            'source': 'colors',
            'constrained': True,
            'usable': True,
            'count': 2,
            'values': ['Красный', 'Синий'],
            'truncated': False,
        })
        self.assertEqual(constraints[2003], {
            'source': 'free_text',
            'constrained': False,
            'usable': True,
            'count': 0,
            'values': [],
            'truncated': False,
        })
        self.assertNotIn('dictionary', schema['characteristics'][0])

    def test_effective_constraint_values_are_bounded(self):
        colors = MarketplaceDirectory.query.filter_by(
            marketplace_id=self.marketplace.id,
            directory_type='colors',
        ).one()
        values = [f'Цвет {index:02d}' for index in range(45)]
        colors.data_json = json.dumps(
            [{'name': value} for value in values],
            ensure_ascii=False,
        )
        colors.items_count = len(values)
        db.session.commit()

        payload = self.client.get(
            '/internal/v1/categories/1001/characteristics', headers=self.auth,
        ).get_json()

        self.assertTrue(payload['reference_status']['usable'])
        color = next(
            item for item in payload['characteristics']
            if item['charc_id'] == self.color.charc_id
        )
        self.assertEqual(color['constraint']['count'], 45)
        self.assertEqual(color['constraint']['values'], values[:40])
        self.assertTrue(color['constraint']['truncated'])

    def test_required_unusable_constraint_blocks_schema_before_llm(self):
        colors = MarketplaceDirectory.query.filter_by(
            marketplace_id=self.marketplace.id,
            directory_type='colors',
        ).one()
        colors.sync_status = 'failed'
        colors.sync_error = 'upstream unavailable'
        db.session.commit()

        payload = self.client.get(
            '/internal/v1/categories/1001/characteristics', headers=self.auth,
        ).get_json()

        self.assertFalse(payload['reference_status']['usable'])
        self.assertEqual(
            payload['reference_status']['reason'],
            'required_constraint_unusable',
        )
        self.assertEqual(payload['characteristics'], [])
        self.assertEqual(payload['count'], 0)
        self.assertEqual(payload['constraint_issues'][0]['charc_id'], 2001)
        self.assertEqual(
            payload['constraint_issues'][0]['code'],
            'directory_stale',
        )
        with self.assertRaises(ReferenceDataUnavailableError):
            require_usable_reference(
                payload,
                'wb_category_characteristics:1001',
            )

    def test_required_characteristic_is_visible_even_with_legacy_disabled_flag(self):
        self.color.is_enabled = False
        db.session.commit()

        schema = self.client.get(
            '/internal/v1/categories/1001/characteristics', headers=self.auth,
        ).get_json()

        self.assertTrue(schema['reference_status']['usable'])
        self.assertEqual(
            [item['charc_id'] for item in schema['characteristics']],
            [2001, 2003],
        )

    def test_stale_reference_reads_return_typed_empty_payload(self):
        self.marketplace.categories_synced_at = datetime.utcnow() - timedelta(hours=49)
        self.category.characteristics_synced_at = datetime.utcnow() - timedelta(hours=49)
        db.session.commit()

        categories = self.client.get(
            '/internal/v1/categories/search?q=одежда', headers=self.auth,
        ).get_json()
        schema = self.client.get(
            '/internal/v1/categories/1001/characteristics', headers=self.auth,
        ).get_json()

        self.assertEqual(categories['categories'], [])
        self.assertFalse(categories['reference_status']['usable'])
        self.assertEqual(categories['reference_status']['reason'], 'stale_cache')
        self.assertEqual(schema['characteristics'], [])
        self.assertFalse(schema['reference_status']['usable'])

    def test_tnved_is_explicitly_category_scoped_not_global(self):
        payload = self.client.get('/internal/v1/directories/tnved', headers=self.auth).get_json()
        self.assertEqual(payload['items'], [])
        self.assertFalse(payload['reference_status']['usable'])
        self.assertEqual(payload['reference_status']['reason'], 'category_scope_required')

    def test_agent_brand_lookup_ignores_upstream_unavailable_binding(self):
        brand = Brand(
            name='Visible Brand',
            name_normalized='visible brand',
            status='verified',
        )
        db.session.add(brand)
        db.session.flush()
        db.session.add(BrandAlias(
            brand_id=brand.id,
            alias='Visible Brand',
            alias_normalized='visible brand',
            source='manual',
            is_active=True,
        ))
        db.session.add(MarketplaceBrand(
            brand_id=brand.id,
            marketplace_id=self.marketplace.id,
            marketplace_brand_name='Hidden WB binding',
            marketplace_brand_id=9001,
            status='verified',
            is_available=False,
        ))
        db.session.commit()

        payload = self.client.get(
            '/internal/v1/brands/validate?brand=Visible%20Brand',
            headers=self.auth,
        ).get_json()['result']

        self.assertEqual(payload['status'], 'not_found')
        self.assertNotIn('marketplace_brand_id', payload)

    def test_agent_brand_lookup_requires_verified_marketplace_binding(self):
        for index, status in enumerate(('pending', 'rejected'), start=1):
            name = f'{status.title()} Local Brand'
            normalized = name.lower()
            brand = Brand(
                name=name,
                name_normalized=normalized,
                status='verified',
            )
            db.session.add(brand)
            db.session.flush()
            db.session.add(BrandAlias(
                brand_id=brand.id,
                alias=name,
                alias_normalized=normalized,
                source='manual',
                is_active=True,
            ))
            db.session.add(MarketplaceBrand(
                brand_id=brand.id,
                marketplace_id=self.marketplace.id,
                marketplace_brand_name=f'{name} WB',
                marketplace_brand_id=9200 + index,
                status=status,
                is_available=True,
            ))
        db.session.commit()

        for status in ('pending', 'rejected'):
            name = f'{status.title()} Local Brand'
            with self.subTest(status=status):
                payload = self.client.get(
                    '/internal/v1/brands/validate',
                    headers=self.auth,
                    query_string={'brand': name},
                ).get_json()['result']
                self.assertEqual(payload['status'], 'not_found')
                self.assertNotIn('marketplace_brand_id', payload)
                self.assertNotIn('marketplace_brand_name', payload)

    def test_recent_category_brand_verification_bypasses_stale_global_sweep(self):
        brand = Brand(
            name='Live Category Brand',
            name_normalized='live category brand',
            status='verified',
        )
        db.session.add(brand)
        db.session.flush()
        db.session.add(BrandAlias(
            brand_id=brand.id,
            alias='Live Category Brand',
            alias_normalized='live category brand',
            source='manual',
            is_active=True,
        ))
        binding = MarketplaceBrand(
            brand_id=brand.id,
            marketplace_id=self.marketplace.id,
            marketplace_brand_name='Live Category Brand WB',
            marketplace_brand_id=9301,
            status='verified',
            is_available=True,
        )
        db.session.add(binding)
        db.session.flush()
        db.session.add(BrandCategoryLink(
            marketplace_brand_id=binding.id,
            category_id=self.category.subject_id,
            is_available=True,
            verified_at=datetime.utcnow(),
        ))
        self.marketplace.brands_synced_at = datetime.utcnow() - timedelta(hours=49)
        self.marketplace.brands_sync_status = 'partial'
        db.session.commit()

        scoped = self.client.get(
            '/internal/v1/brands/validate',
            headers=self.auth,
            query_string={
                'brand': '  Live   Category Brand  ',
                'category_id': self.category.subject_id,
            },
        ).get_json()
        global_result = self.client.get(
            '/internal/v1/brands/validate',
            headers=self.auth,
            query_string={'brand': 'Live Category Brand'},
        ).get_json()
        batch = self.client.post(
            '/internal/v1/brands/validate-batch',
            headers=self.auth,
            json={'items': [
                {
                    'product_id': 100,
                    'brand': 'Live Category Brand',
                    'category_id': self.category.subject_id,
                },
                {
                    'product_id': 101,
                    'brand': 'Live Category Brand',
                },
            ]},
        ).get_json()

        self.assertTrue(scoped['reference_status']['usable'])
        self.assertEqual(scoped['reference_status']['scope'], 'category')
        self.assertEqual(scoped['result']['status'], 'found')
        self.assertTrue(scoped['result']['category_available'])
        self.assertEqual(global_result['result']['status'], 'unavailable')
        self.assertEqual(batch['count'], 2)
        self.assertEqual(batch['results'][0]['status'], 'found')
        self.assertTrue(batch['results'][0]['category_available'])
        self.assertEqual(batch['results'][1]['status'], 'unavailable')
        self.assertEqual(
            batch['results'][1]['reference_status']['reason'],
            'category_scope_required',
        )

        self.category.is_available = False
        db.session.commit()
        removed_scope = self.client.get(
            '/internal/v1/brands/validate',
            headers=self.auth,
            query_string={
                'brand': 'Live Category Brand',
                'category_id': self.category.subject_id,
            },
        ).get_json()
        self.assertEqual(removed_scope['result']['status'], 'unavailable')
        self.assertFalse(removed_scope['reference_status']['usable'])

    def test_inactive_brand_alias_is_not_agent_visible(self):
        brand = Brand(
            name='Inactive Alias Brand',
            name_normalized='inactive alias brand',
            status='verified',
        )
        db.session.add(brand)
        db.session.flush()
        db.session.add(BrandAlias(
            brand_id=brand.id,
            alias='Hidden Alias',
            alias_normalized='hidden alias',
            source='manual',
            is_active=False,
        ))
        db.session.add(MarketplaceBrand(
            brand_id=brand.id,
            marketplace_id=self.marketplace.id,
            marketplace_brand_name=brand.name,
            marketplace_brand_id=9302,
            status='verified',
            is_available=True,
        ))
        db.session.commit()

        payload = self.client.get(
            '/internal/v1/brands/validate',
            headers=self.auth,
            query_string={'brand': 'Hidden Alias'},
        ).get_json()['result']

        self.assertEqual(payload['status'], 'not_found')

    def test_agent_brand_lookup_blocks_stale_reference_data(self):
        brand = Brand(
            name='Stale Brand',
            name_normalized='stale brand',
            status='verified',
        )
        db.session.add(brand)
        db.session.flush()
        db.session.add(BrandAlias(
            brand_id=brand.id,
            alias='Stale Brand',
            alias_normalized='stale brand',
            source='manual',
            is_active=True,
        ))
        db.session.add(MarketplaceBrand(
            brand_id=brand.id,
            marketplace_id=self.marketplace.id,
            marketplace_brand_name='Stale Brand WB',
            marketplace_brand_id=9401,
            status='verified',
            is_available=True,
        ))
        self.marketplace.brands_synced_at = datetime.utcnow() - timedelta(hours=49)
        db.session.commit()

        payload = self.client.get(
            '/internal/v1/brands/validate',
            headers=self.auth,
            query_string={'brand': 'Stale Brand'},
        ).get_json()

        self.assertEqual(payload['result']['status'], 'unavailable')
        self.assertIsNone(payload['result']['brand_name'])
        self.assertFalse(payload['reference_status']['usable'])
        self.assertEqual(payload['reference_status']['reason'], 'stale_cache')
        self.assertEqual(payload['reference_status']['version'], 1)

    def test_explicit_brand_writes_are_canonicalized_for_both_card_types(self):
        self._add_brand_binding(
            alias='Mixed Case Alias',
            marketplace_name='CANONICAL WB BRAND',
        )

        imported = self.client.patch(
            f'/internal/v1/imported-products/{self.imported.id}',
            headers=self.task_auth,
            json={'brand': '  Mixed Case Alias  '},
        )
        product = self.client.patch(
            f'/internal/v1/sellers/{self.seller.id}/products/{self.product.id}',
            headers=self.task_auth,
            json={'brand': 'mixed case alias'},
        )

        self.assertEqual(imported.status_code, 200)
        self.assertEqual(product.status_code, 200)
        db.session.refresh(self.imported)
        db.session.refresh(self.product)
        self.assertEqual(self.imported.brand, 'CANONICAL WB BRAND')
        self.assertEqual(self.product.brand, 'CANONICAL WB BRAND')

    def test_explicit_brand_writes_reject_stale_and_unverified_bindings(self):
        self._add_brand_binding(
            name='Stale Local Brand',
            alias='stale local alias',
            marketplace_name='STALE WB BRAND',
            link_verified_at=datetime.utcnow() - timedelta(hours=49),
        )
        self._add_brand_binding(
            name='Pending Local Brand',
            alias='pending local alias',
            marketplace_name='PENDING WB BRAND',
            binding_status='pending',
        )
        self.marketplace.brands_synced_at = datetime.utcnow() - timedelta(hours=49)
        self.marketplace.brands_sync_status = 'partial'
        db.session.commit()

        preflight = self.client.post(
            '/internal/v1/brands/preflight',
            headers=self.auth,
            json={'category_ids': [self.category.subject_id]},
        )
        self.assertEqual(preflight.status_code, 200)
        self.assertFalse(
            preflight.get_json()['results'][0]['reference_status']['usable'],
        )

        endpoints = (
            f'/internal/v1/imported-products/{self.imported.id}',
            f'/internal/v1/sellers/{self.seller.id}/products/{self.product.id}',
        )
        for endpoint in endpoints:
            with self.subTest(endpoint=endpoint, reason='stale'):
                response = self.client.patch(
                    endpoint,
                    headers=self.task_auth,
                    json={'brand': 'stale local alias'},
                )
                self.assertEqual(response.status_code, 409)
                self.assertTrue(response.get_json()['reference_data_blocked'])

        imported_batch = self.client.patch(
            '/internal/v1/imported-products/batch',
            headers=self.task_auth,
            json={'updates': [{
                'product_id': self.imported.id,
                'brand': 'stale local alias',
            }]},
        )
        self.marketplace.brands_synced_at = datetime.utcnow()
        self.marketplace.brands_sync_status = 'success'
        db.session.commit()
        for endpoint in endpoints:
            with self.subTest(endpoint=endpoint, reason='unverified'):
                response = self.client.patch(
                    endpoint,
                    headers=self.task_auth,
                    json={'brand': 'pending local alias'},
                )
                self.assertEqual(response.status_code, 409)
                self.assertTrue(response.get_json()['reference_data_blocked'])

        product_batch = self.client.patch(
            f'/internal/v1/sellers/{self.seller.id}/products/batch',
            headers=self.task_auth,
            json={'updates': [{
                'product_id': self.product.id,
                'brand': 'pending local alias',
            }]},
        )
        self.assertEqual(imported_batch.get_json()['failed'], 1)
        self.assertTrue(
            imported_batch.get_json()['results'][0]['reference_data_blocked'],
        )
        self.assertEqual(product_batch.get_json()['failed'], 1)
        self.assertTrue(
            product_batch.get_json()['results'][0]['reference_data_blocked'],
        )

    def test_category_only_write_does_not_validate_old_raw_brand(self):
        self.imported.brand = 'Legacy raw supplier brand'
        self.product.brand = 'Legacy raw WB brand'
        db.session.commit()

        imported = self.client.patch(
            f'/internal/v1/imported-products/{self.imported.id}',
            headers=self.task_auth,
            json={'mapped_wb_category': 'ignored alias'},
        )
        product = self.client.patch(
            f'/internal/v1/sellers/{self.seller.id}/products/{self.product.id}',
            headers=self.task_auth,
            json={'wb_category_name': 'ignored alias'},
        )

        self.assertEqual(imported.status_code, 200)
        self.assertEqual(product.status_code, 200)
        db.session.refresh(self.imported)
        db.session.refresh(self.product)
        self.assertEqual(self.imported.brand, 'Legacy raw supplier brand')
        self.assertEqual(self.product.brand, 'Legacy raw WB brand')

    def test_brand_batch_write_gates_reuse_bulk_resolution(self):
        second_imported = ImportedProduct(
            seller_id=self.seller.id,
            supplier_id=self.supplier.id,
            title='Вторая импортированная карточка',
            wb_subject_id=self.category.subject_id,
        )
        second_product = Product(
            seller_id=self.seller.id,
            nm_id=54321,
            title='Вторая основная карточка',
            subject_id=self.category.subject_id,
        )
        db.session.add_all([second_imported, second_product])
        db.session.commit()
        self._add_brand_binding(
            alias='bulk brand alias',
            marketplace_name='BULK CANONICAL WB',
        )

        imported = self.client.patch(
            '/internal/v1/imported-products/batch',
            headers=self.task_auth,
            json={'updates': [
                {'product_id': self.imported.id, 'brand': 'bulk brand alias'},
                {'product_id': second_imported.id, 'brand': 'BULK BRAND ALIAS'},
            ]},
        )
        product = self.client.patch(
            f'/internal/v1/sellers/{self.seller.id}/products/batch',
            headers=self.task_auth,
            json={'updates': [
                {'product_id': self.product.id, 'brand': 'bulk brand alias'},
                {'product_id': second_product.id, 'brand': 'BULK BRAND ALIAS'},
            ]},
        )

        self.assertEqual(imported.status_code, 200)
        self.assertEqual(imported.get_json()['failed'], 0)
        self.assertEqual(product.status_code, 200)
        self.assertEqual(product.get_json()['failed'], 0)
        for card in (self.imported, second_imported, self.product, second_product):
            db.session.refresh(card)
            self.assertEqual(card.brand, 'BULK CANONICAL WB')

    def test_brand_batch_validation_query_count_is_constant(self):
        self._add_brand_binding(
            alias='constant query alias',
            marketplace_name='CONSTANT QUERY WB',
        )

        def measured_count(size):
            statements = []

            def record(connection, cursor, statement, parameters, context, executemany):
                normalized = statement.lower()
                if normalized.lstrip().startswith('select') and any(
                    table in normalized for table in (
                        'brand_aliases', 'marketplace_brands',
                        'brand_category_links', ' join brands',
                    )
                ):
                    statements.append(normalized)

            event.listen(db.engine, 'before_cursor_execute', record)
            try:
                response = self.client.post(
                    '/internal/v1/brands/validate-batch',
                    headers=self.auth,
                    json={'items': [
                        {
                            'product_id': index + 1,
                            'brand': 'constant query alias',
                            'category_id': self.category.subject_id,
                        }
                        for index in range(size)
                    ]},
                )
            finally:
                event.remove(db.engine, 'before_cursor_execute', record)
            self.assertEqual(response.status_code, 200)
            return len(statements)

        self.assertEqual(measured_count(1), measured_count(50))

    def test_brand_reference_endpoints_reject_lossy_numeric_coercion(self):
        for category_ids in ([], [True], [1.2], ['1.2'], ['01']):
            with self.subTest(endpoint='preflight', value=category_ids):
                response = self.client.post(
                    '/internal/v1/brands/preflight',
                    headers=self.auth,
                    json={'category_ids': category_ids},
                )
                self.assertEqual(response.status_code, 400)

        for field, value in (
            ('product_id', True),
            ('product_id', 1.2),
            ('product_id', '1.2'),
            ('product_id', '01'),
            ('category_id', True),
            ('category_id', 1001.5),
            ('category_id', '1001.0'),
            ('category_id', '01001'),
        ):
            item = {
                'product_id': 1,
                'brand': 'Reference Anchor',
                'category_id': self.category.subject_id,
            }
            item[field] = value
            with self.subTest(endpoint='validate-batch', field=field, value=value):
                response = self.client.post(
                    '/internal/v1/brands/validate-batch',
                    headers=self.auth,
                    json={'items': [item]},
                )
                self.assertEqual(response.status_code, 400)

        accepted = self.client.post(
            '/internal/v1/brands/validate-batch',
            headers=self.auth,
            json={'items': [{
                'product_id': '7',
                'brand': 'Reference Anchor',
                'category_id': str(self.category.subject_id),
            }]},
        )
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(accepted.get_json()['results'][0]['product_id'], 7)

        single = self.client.get(
            '/internal/v1/brands/validate',
            headers=self.auth,
            query_string={
                'brand': 'Reference Anchor',
                'category_id': '1001.5',
            },
        )
        self.assertEqual(single.status_code, 400)

    def test_imported_characteristic_write_checks_schema_and_dictionary(self):
        valid = self.client.patch(
            f'/internal/v1/imported-products/{self.imported.id}',
            headers=self.task_auth,
            json={'characteristics': json.dumps({'цВеТ ТоВаРа': 'красный'})},
        )
        self.assertEqual(valid.status_code, 200)

        invalid = self.client.patch(
            f'/internal/v1/imported-products/{self.imported.id}',
            headers=self.task_auth,
            json={'characteristics': json.dumps({'Цвет товара': 'Зелёный'})},
        )
        unknown = self.client.patch(
            f'/internal/v1/imported-products/{self.imported.id}',
            headers=self.task_auth,
            json={'characteristics': json.dumps({'Устаревшее поле': 'x'})},
        )
        self.assertEqual(invalid.status_code, 409)
        self.assertIn('словар', invalid.get_json()['error'].lower())
        self.assertEqual(unknown.status_code, 409)
        self.assertIn('отсутствует в wb-схеме', unknown.get_json()['error'].lower())

    def test_characteristic_patch_is_canonical_and_preserves_existing_values(self):
        existing = [{
            'id': self.length.charc_id,
            'name': self.length.name,
            'value': 12,
        }]
        self.imported.characteristics = json.dumps(existing, ensure_ascii=False)
        self.product.characteristics_json = json.dumps(existing, ensure_ascii=False)
        db.session.commit()

        imported = self.client.patch(
            f'/internal/v1/imported-products/{self.imported.id}',
            headers=self.task_auth,
            json={'characteristics': json.dumps(
                {'цВеТ ТоВаРа': 'красный'},
                ensure_ascii=False,
            )},
        )
        product = self.client.patch(
            f'/internal/v1/sellers/{self.seller.id}/products/{self.product.id}',
            headers=self.task_auth,
            json={'characteristics': [{
                'id': self.color.charc_id,
                'value': ['красный'],
            }]},
        )

        self.assertEqual(imported.status_code, 200)
        self.assertEqual(product.status_code, 200)
        expected = [
            existing[0],
            {'id': self.color.charc_id, 'value': ['Красный']},
        ]
        db.session.refresh(self.imported)
        db.session.refresh(self.product)
        self.assertEqual(json.loads(self.imported.characteristics), expected)
        self.assertEqual(json.loads(self.product.characteristics_json), expected)

        snapshot = AgentChangeSnapshot.query.filter_by(
            task_id=self.task.id,
            imported_product_id=self.imported.id,
        ).one()
        self.assertEqual(
            json.loads(json.loads(snapshot.new_values)['characteristics']),
            expected,
        )
        history = CardEditHistory.query.filter_by(
            product_id=self.product.id,
        ).one()
        self.assertEqual(
            json.loads(history.snapshot_after['characteristics_json']),
            expected,
        )

    def test_corrupt_stored_characteristics_block_patch_without_data_loss(self):
        self.imported.characteristics = '{broken'
        self.product.characteristics_json = json.dumps([1])
        db.session.commit()

        imported = self.client.patch(
            f'/internal/v1/imported-products/{self.imported.id}',
            headers=self.task_auth,
            json={'characteristics': json.dumps(
                {'Цвет товара': 'Красный'},
                ensure_ascii=False,
            )},
        )
        product = self.client.patch(
            f'/internal/v1/sellers/{self.seller.id}/products/{self.product.id}',
            headers=self.task_auth,
            json={'characteristics': [{
                'id': self.color.charc_id,
                'value': ['Красный'],
            }]},
        )

        self.assertEqual(imported.status_code, 409)
        self.assertEqual(product.status_code, 409)
        self.assertIn('текущие характеристики', imported.get_json()['error'].lower())
        self.assertIn('текущие характеристики', product.get_json()['error'].lower())
        db.session.refresh(self.imported)
        db.session.refresh(self.product)
        self.assertEqual(self.imported.characteristics, '{broken')
        self.assertEqual(self.product.characteristics_json, '[1]')

    def test_stale_schema_and_unavailable_category_block_writes(self):
        self.category.characteristics_synced_at = datetime.utcnow() - timedelta(hours=49)
        db.session.commit()
        stale = self.client.patch(
            f'/internal/v1/imported-products/{self.imported.id}',
            headers=self.task_auth,
            json={'characteristics': json.dumps({'Цвет товара': 'Красный'})},
        )
        unavailable = self.client.patch(
            f'/internal/v1/imported-products/{self.imported.id}',
            headers=self.task_auth,
            json={'wb_subject_id': self.removed_category.subject_id},
        )
        self.assertEqual(stale.status_code, 409)
        self.assertIn('устарел', stale.get_json()['error'].lower())
        self.assertEqual(unavailable.status_code, 409)
        self.assertIn('недоступ', unavailable.get_json()['error'].lower())

    def test_existing_disabled_category_cannot_bypass_characteristic_write_gate(self):
        self.category.is_enabled = False
        db.session.commit()

        imported = self.client.patch(
            f'/internal/v1/imported-products/{self.imported.id}',
            headers=self.task_auth,
            json={'characteristics': json.dumps({'Цвет товара': 'Красный'})},
        )
        product = self.client.patch(
            f'/internal/v1/sellers/{self.seller.id}/products/{self.product.id}',
            headers=self.task_auth,
            json={'characteristics': [
                {'id': self.color.charc_id, 'value': ['Красный']},
            ]},
        )

        self.assertEqual(imported.status_code, 409)
        self.assertIn('не включена', imported.get_json()['error'].lower())
        self.assertEqual(product.status_code, 409)
        self.assertIn('не включена', product.get_json()['error'].lower())

    def test_imported_batch_reuses_one_reference_schema_query_set(self):
        second = ImportedProduct(
            seller_id=self.seller.id,
            supplier_id=self.supplier.id,
            title='Вторая футболка',
            wb_subject_id=self.category.subject_id,
            characteristics='{}',
        )
        db.session.add(second)
        db.session.commit()

        reference_selects = []

        def record_reference_select(
            connection, cursor, statement, parameters, context, executemany,
        ):
            normalized = statement.lower()
            if normalized.lstrip().startswith('select') and any(
                table in normalized for table in (
                    ' from marketplaces',
                    ' from marketplace_categories',
                    ' from marketplace_category_characteristics',
                )
            ):
                reference_selects.append(normalized)

        event.listen(db.engine, 'before_cursor_execute', record_reference_select)
        try:
            response = self.client.patch(
                '/internal/v1/imported-products/batch',
                headers=self.task_auth,
                json={'updates': [
                    {
                        'product_id': self.imported.id,
                        'characteristics': json.dumps(
                            {'Цвет товара': 'Красный'}, ensure_ascii=False,
                        ),
                    },
                    {
                        'product_id': second.id,
                        'characteristics': json.dumps(
                            {'Цвет товара': 'Синий'}, ensure_ascii=False,
                        ),
                    },
                ]},
            )
        finally:
            event.remove(
                db.engine, 'before_cursor_execute', record_reference_select,
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['failed'], 0)
        self.assertLessEqual(len(reference_selects), 3)

    def test_category_alias_is_canonical_and_empty_characteristics_cannot_clear(self):
        canonical = self.client.patch(
            f'/internal/v1/imported-products/{self.imported.id}',
            headers=self.task_auth,
            json={'mapped_wb_category': 'Выдуманное имя'},
        )
        self.assertEqual(canonical.status_code, 200)
        db.session.refresh(self.imported)
        self.assertEqual(self.imported.mapped_wb_category, self.category.subject_name)

        before = self.imported.characteristics
        empty = self.client.patch(
            f'/internal/v1/imported-products/{self.imported.id}',
            headers=self.task_auth,
            json={'characteristics': '{}'},
        )
        self.assertEqual(empty.status_code, 409)
        self.assertTrue(empty.get_json()['reference_data_blocked'])
        db.session.refresh(self.imported)
        self.assertEqual(self.imported.characteristics, before)

    def test_product_write_uses_same_freshness_gate(self):
        self.category.characteristics_sync_status = 'failed'
        db.session.commit()
        response = self.client.patch(
            f'/internal/v1/sellers/{self.seller.id}/products/{self.product.id}',
            headers=self.task_auth,
            json={'characteristics': [{'id': self.color.charc_id, 'value': ['Красный']}]},
        )
        self.assertEqual(response.status_code, 409)
        self.assertTrue(response.get_json()['reference_data_blocked'])
        db.session.refresh(self.product)
        self.assertEqual(self.product.characteristics_json, '[]')

    def test_product_category_alias_is_canonical_and_empty_patch_is_blocked(self):
        canonical = self.client.patch(
            f'/internal/v1/sellers/{self.seller.id}/products/{self.product.id}',
            headers=self.task_auth,
            json={'wb_category_name': 'Выдуманное имя'},
        )
        self.assertEqual(canonical.status_code, 200)
        db.session.refresh(self.product)
        self.assertEqual(self.product.object_name, self.category.subject_name)

        empty = self.client.patch(
            f'/internal/v1/sellers/{self.seller.id}/products/{self.product.id}',
            headers=self.task_auth,
            json={'characteristics': []},
        )
        self.assertEqual(empty.status_code, 409)
        self.assertTrue(empty.get_json()['reference_data_blocked'])

    def test_characteristic_name_matching_is_exact_after_casefold(self):
        exact = build_wb_characteristic_patch(
            self.category.subject_id, {'цВеТ ТоВаРа': 'красный'},
        )
        self.assertEqual(exact, [{'id': self.color.charc_id, 'value': ['Красный']}])
        with self.assertRaises(WBCharacteristicValidationError):
            build_wb_characteristic_patch(
                self.category.subject_id, {'Цвет': 'Красный'},
            )


class _NoLlm:
    def __init__(self):
        self.calls = 0

    def chat(self, **kwargs):
        self.calls += 1
        raise AssertionError('LLM must not run for unusable reference data')


class _StaleCategoryPlatform:
    def search_categories(self, query, limit=20):
        return {
            'categories': [],
            'warning': 'Справочник категорий устарел',
            'reference_status': {'usable': False, 'stale': True, 'reason': 'stale_cache'},
        }


class _StaleSchemaPlatform:
    def get_imported_product(self, product_id):
        return {'product': {'id': product_id, 'wb_subject_id': 1001, 'title': 'Test'}}

    def get_product(self, seller_id, product_id):
        return {'id': product_id, 'subject_id': 1001, 'title': 'Test'}

    def get_category_characteristics(self, subject_id, required_only=False):
        return {
            'characteristics': [],
            'warning': 'Схема характеристик устарела',
            'reference_status': {'usable': False, 'stale': True, 'reason': 'stale_cache'},
        }


class _BatchSchemaPlatform:
    def __init__(self, *, stale=False, cancel_after_first_schema=False):
        self.stale = stale
        self.cancel_after_first_schema = cancel_after_first_schema
        self.cancelled = False
        self.brief_calls = []
        self.schema_calls = []

    def get_task_status(self, task_id):
        return {'task': {'status': 'cancelled' if self.cancelled else 'running'}}

    def get_imported_products_brief(self, product_ids):
        self.brief_calls.append(list(product_ids))
        return [
            {
                'id': product_id,
                'wb_subject_id': 1001 if product_id % 2 else 1002,
                'title': f'Test {product_id}',
            }
            for product_id in product_ids
        ]

    def get_category_characteristics(self, subject_id, required_only=False):
        self.schema_calls.append(subject_id)
        if self.cancel_after_first_schema:
            self.cancelled = True
        if self.stale:
            return {
                'characteristics': [],
                'warning': 'Схема характеристик устарела',
                'reference_status': {
                    'usable': False, 'stale': True, 'reason': 'stale_cache',
                },
            }
        return {
            'characteristics': [{'id': subject_id, 'name': 'Размер'}],
            'reference_status': {
                'usable': True, 'available': True, 'stale': False,
            },
        }


def _bare_agent(agent_class, platform):
    agent = object.__new__(agent_class)
    agent.platform = platform
    agent.llm = _NoLlm()
    agent.config = SimpleNamespace(RUN_TOKEN_BUDGET=30000, RUN_API_BUDGET=24)
    agent._tools = SimpleNamespace(get_tool_schemas=lambda: [])
    return agent


class AgentReferencePreflightTest(unittest.TestCase):
    def test_reference_validator_is_fail_closed_without_metadata(self):
        with self.assertRaises(ReferenceDataUnavailableError):
            require_usable_reference({'characteristics': []}, 'schema')

    def test_category_mapper_stops_before_llm(self):
        agent = _bare_agent(CategoryMapperAgent, _StaleCategoryPlatform())
        result = agent.execute_task({
            'id': 'task', 'task_type': 'map_single',
            'input_data': json.dumps({'imported_product_id': 1}),
        })
        self.assertEqual(result['status'], 'needs_clarification')
        self.assertTrue(result['reference_data_blocked'])
        self.assertEqual(agent.llm.calls, 0)
        self.assertEqual(result['_usage'].get('api_requests', 0), 0)

    def test_characteristics_filler_stops_before_llm(self):
        agent = _bare_agent(CharacteristicsFillerAgent, _StaleSchemaPlatform())
        result = agent.execute_task({
            'id': 'task', 'task_type': 'fill_single',
            'input_data': json.dumps({'imported_product_id': 1}),
        })
        self.assertEqual(result['status'], 'needs_clarification')
        self.assertTrue(result['partial'])
        self.assertEqual(agent.llm.calls, 0)

    def test_main_product_characteristics_stops_before_llm(self):
        agent = _bare_agent(CharacteristicsFillerAgent, _StaleSchemaPlatform())
        result = agent.execute_task({
            'id': 'task', 'seller_id': 7, 'task_type': 'fill_single',
            'input_data': json.dumps({'product_id': 1}),
        })
        self.assertEqual(result['status'], 'needs_clarification')
        self.assertTrue(result['reference_data_blocked'])
        self.assertEqual(agent.llm.calls, 0)

    def test_size_normalizer_stops_before_llm(self):
        agent = _bare_agent(SizeNormalizerAgent, _StaleSchemaPlatform())
        result = agent.execute_task({
            'id': 'task', 'seller_id': 1, 'task_type': 'normalize_single',
            'input_data': json.dumps({'imported_product_id': 1}),
        })
        self.assertEqual(result['status'], 'needs_clarification')
        self.assertTrue(result['reference_data_blocked'])
        self.assertEqual(agent.llm.calls, 0)

    def test_size_normalizer_large_batch_checks_freshness_before_chunked_react(self):
        product_ids = list(range(1, 12))
        platform = _BatchSchemaPlatform(stale=True)
        agent = _bare_agent(SizeNormalizerAgent, platform)
        chunk_calls = []
        agent._run_chunked_batch = lambda *args: chunk_calls.append(args)

        result = agent.execute_task({
            'id': 'large-stale', 'seller_id': 1,
            'task_type': 'normalize_batch',
            'input_data': json.dumps({'imported_product_ids': product_ids}),
        })

        self.assertEqual(result['status'], 'needs_clarification')
        self.assertTrue(result['reference_data_blocked'])
        self.assertEqual(result['failed'], len(product_ids))
        self.assertEqual(result['_usage'].get('api_requests', 0), 0)
        self.assertEqual(agent.llm.calls, 0)
        self.assertEqual(platform.brief_calls, [product_ids])
        self.assertEqual(platform.schema_calls, [1001])
        self.assertEqual(chunk_calls, [])

    def test_size_normalizer_large_fresh_batch_keeps_chunked_execution(self):
        product_ids = list(range(1, 12))
        platform = _BatchSchemaPlatform()
        agent = _bare_agent(SizeNormalizerAgent, platform)
        chunk_calls = []

        def run_chunked(task, ids):
            chunk_calls.append(list(ids))
            return {'processed': len(ids), 'saved': len(ids)}

        agent._run_chunked_batch = run_chunked
        result = agent.execute_task({
            'id': 'large-fresh', 'seller_id': 1,
            'task_type': 'normalize_batch',
            'input_data': json.dumps({'product_ids': product_ids}),
        })

        self.assertEqual(result['processed'], len(product_ids))
        self.assertEqual(platform.brief_calls, [product_ids])
        self.assertEqual(platform.schema_calls, [1001, 1002])
        self.assertEqual(chunk_calls, [product_ids])
        self.assertEqual(agent.llm.calls, 0)

    def test_size_normalizer_large_batch_cancels_between_schema_checks(self):
        product_ids = list(range(1, 12))
        platform = _BatchSchemaPlatform(cancel_after_first_schema=True)
        agent = _bare_agent(SizeNormalizerAgent, platform)
        chunk_calls = []
        agent._run_chunked_batch = lambda *args: chunk_calls.append(args)

        result = agent.execute_task({
            'id': 'large-cancelled', 'seller_id': 1,
            'task_type': 'normalize_batch',
            'input_data': json.dumps({'product_ids': product_ids}),
        })

        self.assertEqual(result['status'], 'cancelled')
        self.assertEqual(result['_usage'].get('api_requests', 0), 0)
        self.assertEqual(platform.schema_calls, [1001])
        self.assertEqual(chunk_calls, [])
        self.assertEqual(agent.llm.calls, 0)


if __name__ == '__main__':
    unittest.main()
