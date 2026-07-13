# -*- coding: utf-8 -*-
"""Regression tests for supplier characteristics used by card enrichment."""

import json
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

from flask import Flask

from models import (
    Marketplace,
    MarketplaceCategory,
    MarketplaceCategoryCharacteristic,
    db,
)
from services.supplier_enrichment import EnrichmentService


class SupplierEnrichmentCharacteristicsTestCase(unittest.TestCase):
    SUBJECT_ID = 91234
    MATERIAL_ID = 101
    GENDER_ID = 102

    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(
            SQLALCHEMY_DATABASE_URI='sqlite:///:memory:',
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            SECRET_KEY='test-secret',
        )
        db.init_app(self.app)
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        self._seed_wb_schema()
        self.service = EnrichmentService()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def _seed_wb_schema(self):
        marketplace = Marketplace(
            name='Wildberries',
            code='wb',
            categories_sync_status='success',
            categories_synced_at=datetime.utcnow(),
        )
        db.session.add(marketplace)
        db.session.flush()

        category = MarketplaceCategory(
            marketplace_id=marketplace.id,
            subject_id=self.SUBJECT_ID,
            subject_name='Тестовый предмет',
            is_enabled=True,
            is_available=True,
            characteristics_sync_status='success',
            characteristics_synced_at=datetime.utcnow(),
        )
        db.session.add(category)
        db.session.flush()

        db.session.add_all([
            MarketplaceCategoryCharacteristic(
                marketplace_id=marketplace.id,
                category_id=category.id,
                charc_id=self.MATERIAL_ID,
                name='Материал изделия',
                charc_type=1,
                max_count=1,
                dictionary_json=json.dumps(
                    ['Силикон', 'Пластик'], ensure_ascii=False),
                is_enabled=True,
                is_available=True,
            ),
            MarketplaceCategoryCharacteristic(
                marketplace_id=marketplace.id,
                category_id=category.id,
                charc_id=self.GENDER_ID,
                name='Пол',
                charc_type=1,
                max_count=1,
                dictionary_json=json.dumps(
                    ['Женский', 'Мужской'], ensure_ascii=False),
                is_enabled=True,
                is_available=True,
            ),
        ])
        db.session.commit()

    @staticmethod
    def _product():
        return SimpleNamespace(
            id=501,
            nm_id=1000501,
            vendor_code='ANDREY-501',
            title='Карточка Андрея',
            brand='Brand',
            description='Описание',
            object_name='Тестовый предмет',
            price=None,
            discount_price=None,
            quantity=0,
            characteristics_json='[]',
            dimensions_json='{}',
            photos_json='[]',
            is_active=True,
            subject_id=SupplierEnrichmentCharacteristicsTestCase.SUBJECT_ID,
        )

    @staticmethod
    def _andrey_import(**overrides):
        values = {
            'id': 601,
            'external_id': 'ANDREY-501',
            'source_type': 'andrey',
            'title': 'Карточка Андрея',
            'brand': 'Brand',
            'description': 'Описание',
            'characteristics': '',
            'materials': json.dumps(['наилучшем виде'], ensure_ascii=False),
            'gender': 'Унисекс',
            'ai_seo_title': None,
            'ai_detected_brand': None,
            'ai_dimensions': None,
            'photo_urls': None,
            'created_at': None,
            'product_id': None,
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def test_preview_uses_materials_and_gender_when_characteristics_are_empty(self):
        preview = self.service.build_preview(
            self._product(), self._andrey_import())

        supplier_values = {
            item['name']: item['value']
            for item in preview['characteristics']['supplier_parsed']
        }
        self.assertEqual(
            supplier_values['Материал изделия'], 'наилучшем виде')
        self.assertEqual(supplier_values['Пол'], 'Унисекс')
        self.assertTrue(preview['characteristics']['has_change'])
        self.assertFalse(preview['characteristics']['validation']['valid'])
        self.assertIn(
            'наилучшем виде',
            preview['characteristics']['validation']['error'],
        )

    def test_invalid_andrey_material_and_gender_block_wb_update(self):
        wb_client = MagicMock()

        result = self.service.apply_enrichment(
            self._product(),
            self._andrey_import(),
            ['characteristics'],
            'replace',
            SimpleNamespace(id=1),
            wb_client,
        )

        self.assertFalse(result['success'])
        self.assertEqual(result['fields_applied'], [])
        self.assertFalse(result['wb_sync'])
        self.assertIn('наилучшем виде', result['error'])
        self.assertIn('Унисекс', result['error'])
        wb_client.update_card.assert_not_called()

    def test_material_does_not_match_schema_by_substring(self):
        material = MarketplaceCategoryCharacteristic.query.filter_by(
            charc_id=self.MATERIAL_ID,
        ).one()
        material.name = 'Материал корпуса декоративный'
        db.session.commit()
        wb_client = MagicMock()

        result = self.service.apply_enrichment(
            self._product(),
            self._andrey_import(
                materials=json.dumps(['Силикон'], ensure_ascii=False),
                gender=None,
            ),
            ['characteristics'],
            'replace',
            SimpleNamespace(id=1),
            wb_client,
        )

        self.assertFalse(result['success'])
        self.assertIn('не сопоставлен', result['error'])
        wb_client.update_card.assert_not_called()

    def test_valid_separate_fields_are_canonicalized(self):
        patch = self.service._map_characteristics(
            self._andrey_import(
                materials=json.dumps(['силикон'], ensure_ascii=False),
                gender='женский',
            ),
            self.SUBJECT_ID,
        )

        self.assertEqual(patch, [
            {'id': self.MATERIAL_ID, 'value': ['Силикон']},
            {'id': self.GENDER_ID, 'value': ['Женский']},
        ])

    def test_malformed_characteristics_fail_closed(self):
        wb_client = MagicMock()

        result = self.service.apply_enrichment(
            self._product(),
            self._andrey_import(
                characteristics='{broken',
                materials=None,
                gender=None,
            ),
            ['characteristics'],
            'replace',
            SimpleNamespace(id=1),
            wb_client,
        )

        self.assertFalse(result['success'])
        self.assertIn('Неподдерживаемый формат', result['error'])
        wb_client.update_card.assert_not_called()


if __name__ == '__main__':
    unittest.main()
