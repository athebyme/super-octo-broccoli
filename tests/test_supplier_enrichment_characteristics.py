# -*- coding: utf-8 -*-
"""Regression tests for supplier characteristics used by card enrichment."""

import json
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

from flask import Flask

from models import (
    CardEditHistory,
    Marketplace,
    MarketplaceCategory,
    MarketplaceCategoryCharacteristic,
    MarketplaceDirectory,
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
        db.session.add(MarketplaceDirectory(
            marketplace_id=marketplace.id,
            directory_type='kinds',
            data_json=json.dumps(
                ['Женский', 'Мужской'], ensure_ascii=False,
            ),
            synced_at=datetime.utcnow(),
            sync_status='success',
            items_count=2,
            version=1,
        ))
        db.session.commit()

    @staticmethod
    def _product():
        return SimpleNamespace(
            id=501,
            seller_id=1,
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
            'seller_id': 1,
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
            'original_data': None,
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
        # Fuzzy/substring сопоставление по-прежнему запрещено: материал не
        # уходит в чужую характеристику. Но отсутствие точного поля в схеме —
        # это skip с отчётом, а не блокировка всей записи.
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

        self.assertTrue(result['success'])
        self.assertNotIn('characteristics', result['fields_applied'])
        skipped_names = [
            item['name'] for item in result['skipped_characteristics']
        ]
        self.assertIn('Материал', skipped_names)
        wb_client.update_card.assert_not_called()

    def test_package_dimension_names_routed_to_dimensions_not_characteristics(self):
        # «Ширина упаковки, см» и прочие габариты — объект dimensions WB,
        # а не характеристики: они не должны валить весь патч.
        imp = self._andrey_import(
            characteristics=json.dumps({
                'Ширина упаковки, см': '10',
                'Высота упаковки, см': '19',
                'Длина упаковки, см': '6',
                'Вес упаковки, кг': '0.11',
                'Материал изделия': 'Силикон',
            }, ensure_ascii=False),
            materials=None,
            gender=None,
        )
        source = self.service._build_supplier_characteristic_source(imp)
        patch = self.service._map_characteristics(
            imp, self.SUBJECT_ID, source=source)

        self.assertEqual(patch, [
            {'id': self.MATERIAL_ID, 'value': ['Силикон']},
        ])
        dims = source['extracted_dimensions']
        self.assertEqual(dims['width'], 10)
        self.assertEqual(dims['height'], 19)
        self.assertEqual(dims['length'], 6)
        self.assertAlmostEqual(dims['weightBrutto'], 0.11)
        reasons = {
            item['name']: item['reason']
            for item in source['skipped_characteristics']
        }
        self.assertEqual(reasons.get('Ширина упаковки, см'), 'dimension_field')

    def test_dimensions_only_extracts_feed_dimensions_without_sending_chars(self):
        product = self._product()
        imp = self._andrey_import(
            characteristics=json.dumps({
                'Ширина упаковки, см': '10',
                'Материал изделия': 'Силикон',
            }, ensure_ascii=False),
            materials=None,
            gender=None,
        )
        before = {
            'nmID': product.nm_id,
            'subjectID': self.SUBJECT_ID,
            'vendorCode': product.vendor_code,
            'title': product.title,
            'brand': product.brand,
            'description': product.description,
            'sizes': [{'chrtID': 1, 'skus': ['1234567890123']}],
            'characteristics': [],
            'dimensions': {},
        }
        after = {**before, 'dimensions': {'width': 10}}
        wb_client = MagicMock()

        def update_card(_nm_id, updates, **kwargs):
            self.assertEqual(updates, {'dimensions': {'width': 10}})
            snapshot = {
                'before': before,
                'after': after,
                'applied_update_fields': ['dimensions'],
                'write_required': True,
            }
            kwargs['snapshot_context'].update(snapshot)
            kwargs['before_send_callback'](snapshot)
            return {'error': False}

        wb_client.update_card.side_effect = update_card
        result = self.service.apply_enrichment(
            product,
            imp,
            ['dimensions'],
            'smart_merge',
            SimpleNamespace(id=1),
            wb_client,
        )

        self.assertTrue(result['success'], result['error'])
        self.assertEqual(result['fields_applied'], ['dimensions'])
        sent_updates = wb_client.update_card.call_args.args[1]
        self.assertNotIn('characteristics', sent_updates)

    def test_observed_original_package_weight_reaches_dimensions_update(self):
        product = self._product()
        imp = self._andrey_import(
            characteristics='',
            materials=None,
            gender=None,
            original_data=json.dumps({
                'external_id': 'ANDREY-501',
                'dimensions': {
                    'Вес упаковки, кг': '0.18',
                },
            }, ensure_ascii=False),
        )
        before = {
            'nmID': product.nm_id,
            'subjectID': self.SUBJECT_ID,
            'vendorCode': product.vendor_code,
            'title': product.title,
            'brand': product.brand,
            'description': product.description,
            'sizes': [{'chrtID': 1, 'skus': ['1234567890123']}],
            'characteristics': [],
            'dimensions': {'weightBrutto': 0},
        }
        after = {**before, 'dimensions': {'weightBrutto': 0.18}}
        wb_client = MagicMock()

        def update_card(_nm_id, updates, **kwargs):
            self.assertEqual(updates, {
                'dimensions': {'weightBrutto': 0.18},
            })
            snapshot = {
                'before': before,
                'after': after,
                'applied_update_fields': ['dimensions'],
                'write_required': True,
            }
            kwargs['snapshot_context'].update(snapshot)
            kwargs['before_send_callback'](snapshot)
            return {'error': False}

        wb_client.update_card.side_effect = update_card
        result = self.service.apply_enrichment(
            product,
            imp,
            ['dimensions'],
            'smart_merge',
            SimpleNamespace(id=1),
            wb_client,
        )

        self.assertTrue(result['success'], result['error'])
        self.assertEqual(result['fields_applied'], ['dimensions'])

    def test_out_of_schema_optional_name_skipped_with_report(self):
        # Имя вне схемы категории («Состав» здесь не существует) пропускается
        # с отчётом; валидные поля записываются, весь патч не блокируется.
        imp = self._andrey_import(
            characteristics=json.dumps({
                'Состав': 'Хлопок',
                'Материал изделия': 'Силикон',
            }, ensure_ascii=False),
            materials=None,
            gender=None,
        )
        source = self.service._build_supplier_characteristic_source(imp)
        patch = self.service._map_characteristics(
            imp, self.SUBJECT_ID, source=source)

        self.assertEqual(patch, [
            {'id': self.MATERIAL_ID, 'value': ['Силикон']},
        ])
        skipped = {
            item['name']: item['reason']
            for item in source['skipped_characteristics']
        }
        self.assertEqual(skipped.get('Состав'), 'not_in_schema')

    def test_name_value_list_filters_unknown_and_routes_dimensions(self):
        # Реальные supplier rows могут хранить тот же name-keyed payload
        # массивом объектов, а не словарём. Он обязан проходить тот же
        # предфильтр без потери валидных значений и габаритов.
        imp = self._andrey_import(
            characteristics=json.dumps([
                {'name': 'Состав', 'value': 'Хлопок'},
                {'name': 'Ширина упаковки, см', 'value': '10'},
                {'name': 'Материал изделия', 'value': 'Силикон'},
            ], ensure_ascii=False),
            materials=None,
            gender=None,
        )
        source = self.service._build_supplier_characteristic_source(imp)
        patch = self.service._map_characteristics(
            imp, self.SUBJECT_ID, source=source)

        self.assertEqual(patch, [
            {'id': self.MATERIAL_ID, 'value': ['Силикон']},
        ])
        self.assertEqual(source['extracted_dimensions']['width'], 10)
        skipped = {
            item['name']: item['reason']
            for item in source['skipped_characteristics']
        }
        self.assertEqual(skipped.get('Состав'), 'not_in_schema')
        self.assertEqual(
            skipped.get('Ширина упаковки, см'),
            'dimension_field',
        )

    def test_supplier_list_shape_skips_absent_composition_and_gender(self):
        # Форма, из-за которой падала реальная задача sexoptovik: «Состав»
        # и «Пол» находятся в name/value-массиве и продублированы отдельными
        # supplier-полями. В категории есть только «Материал изделия».
        gender_char = MarketplaceCategoryCharacteristic.query.filter_by(
            charc_id=self.GENDER_ID,
        ).one()
        db.session.delete(gender_char)
        db.session.commit()

        imp = self._andrey_import(
            source_type='sexoptovik',
            characteristics=json.dumps([
                {'name': 'Состав', 'value': 'Силикон'},
                {'name': 'Пол', 'value': 'для женщин'},
            ], ensure_ascii=False),
            materials=json.dumps(['Силикон'], ensure_ascii=False),
            gender='для женщин',
        )
        source = self.service._build_supplier_characteristic_source(imp)
        patch = self.service._map_characteristics(
            imp, self.SUBJECT_ID, source=source)

        self.assertEqual(patch, [
            {'id': self.MATERIAL_ID, 'value': ['Силикон']},
        ])
        skipped = {
            (item['name'], item['reason'])
            for item in source['skipped_characteristics']
        }
        self.assertIn(('Состав', 'not_in_schema'), skipped)
        self.assertIn(('Пол', 'not_in_schema'), skipped)
        self.assertEqual(
            sum(
                item['name'] == 'Пол'
                and item['reason'] == 'not_in_schema'
                for item in source['skipped_characteristics']
            ),
            1,
        )

    def test_typed_gender_overrides_raw_duplicate_before_validation(self):
        # В категориях, где «Пол» реально есть, raw supplier-дубль не должен
        # валидироваться раньше dedicated gender. Сначала выбираем источник и
        # канонизируем фразу, затем один раз проверяем итоговый patch.
        imp = self._andrey_import(
            characteristics=json.dumps([{
                'name': 'Пол',
                'value': 'для женщин',
            }], ensure_ascii=False),
            materials=None,
            gender='для женщин',
        )

        patch = self.service._map_characteristics(imp, self.SUBJECT_ID)

        self.assertEqual(patch, [{
            'id': self.GENDER_ID,
            'value': ['Женский'],
        }])

    def test_id_based_list_remains_strict(self):
        # Нельзя маскировать неизвестный WB id как необязательное supplier-имя.
        imp = self._andrey_import(
            characteristics=json.dumps([{
                'id': 999999,
                'name': 'Состав',
                'value': 'Силикон',
            }], ensure_ascii=False),
            materials=None,
            gender=None,
        )
        source = self.service._build_supplier_characteristic_source(imp)

        from services.marketplace_validator import (
            WBCharacteristicValidationError,
        )
        with self.assertRaises(WBCharacteristicValidationError) as raised:
            self.service._map_characteristics(
                imp, self.SUBJECT_ID, source=source)

        self.assertIn('id=999999', str(raised.exception))
        self.assertEqual(source['skipped_characteristics'], [])

    def test_name_value_list_duplicates_remain_strict(self):
        # Предфильтр не превращает массив в dict: одинаковые имена должны
        # остаться дублями и быть отклонены строгим downstream-валидатором.
        imp = self._andrey_import(
            characteristics=json.dumps([
                {'name': 'Материал изделия', 'value': 'Силикон'},
                {'name': 'Материал изделия', 'value': 'Пластик'},
            ], ensure_ascii=False),
            materials=None,
            gender=None,
        )

        from services.marketplace_validator import (
            WBCharacteristicValidationError,
        )
        with self.assertRaises(WBCharacteristicValidationError) as raised:
            self.service._map_characteristics(imp, self.SUBJECT_ID)

        self.assertIn('передана более одного раза', str(raised.exception))

    def test_valid_separate_fields_are_canonicalized(self):
        patch = self.service._map_characteristics(
            self._andrey_import(
                materials=json.dumps(['силикон'], ensure_ascii=False),
                gender='для женщин',
            ),
            self.SUBJECT_ID,
        )

        self.assertEqual(patch, [
            {'id': self.MATERIAL_ID, 'value': ['Силикон']},
            {'id': self.GENDER_ID, 'value': ['Женский']},
        ])

    def test_accepted_history_waits_for_live_confirmation_before_local_mirror(self):
        product = self._product()
        product.characteristics_json = json.dumps([{
            'id': 777,
            'value': ['Устаревшее локальное значение'],
        }], ensure_ascii=False)
        before = {
            'nmID': product.nm_id,
            'subjectID': self.SUBJECT_ID,
            'vendorCode': product.vendor_code,
            'title': product.title,
            'brand': product.brand,
            'description': product.description,
            'sizes': [{'chrtID': 1, 'skus': ['1234567890123']}],
            'characteristics': [
                {'id': self.MATERIAL_ID, 'value': ['Пластик']},
                {'id': 999, 'value': ['Только в свежей WB-карточке']},
            ],
        }
        after = dict(before)
        after['characteristics'] = [
            {'id': self.MATERIAL_ID, 'value': ['Силикон']},
            {'id': 999, 'value': ['Только в свежей WB-карточке']},
            {'id': self.GENDER_ID, 'value': ['Женский']},
        ]
        wb_client = MagicMock()

        def update_card(_nm_id, _updates, **kwargs):
            context = kwargs['snapshot_context']
            context.update({'before': before, 'after': after})
            kwargs['before_send_callback']({
                'before': before,
                'after': after,
            })
            pending = CardEditHistory.query.one()
            self.assertEqual(pending.wb_sync_status, 'pending')
            return {'error': False}

        wb_client.update_card.side_effect = update_card

        result = self.service.apply_enrichment(
            product,
            self._andrey_import(
                materials=json.dumps(['силикон'], ensure_ascii=False),
                gender='женский',
            ),
            ['characteristics'],
            'replace',
            SimpleNamespace(id=1),
            wb_client,
        )

        self.assertTrue(result['success'], result['error'])
        self.assertEqual(
            json.loads(product.characteristics_json),
            [{'id': 777, 'value': ['Устаревшее локальное значение']}],
        )
        history = CardEditHistory.query.one()
        self.assertFalse(history.wb_synced)
        self.assertEqual(history.wb_sync_status, 'submitted')
        self.assertIsNotNone(history.wb_reconcile_due_at)
        self.assertEqual(
            history.snapshot_before['characteristics'],
            before['characteristics'],
        )
        self.assertEqual(
            history.snapshot_after['characteristics'],
            after['characteristics'],
        )

    def test_equal_length_live_characteristic_is_preserved_and_audited(self):
        from services.wb_api_client import WildberriesAPIClient

        product = self._product()
        client = WildberriesAPIClient('test-key')
        client.get_card_by_nm_id = MagicMock(return_value={
            'nmID': product.nm_id,
            'subjectID': self.SUBJECT_ID,
            'vendorCode': product.vendor_code,
            'title': product.title,
            'brand': product.brand,
            'description': product.description,
            'sizes': [{'chrtID': 1, 'skus': ['1234567890123']}],
            'characteristics': [{
                'id': self.MATERIAL_ID,
                'name': 'Материал изделия',
                'value': ['Пластик'],
            }],
            'photos': [],
        })
        client._make_request = MagicMock()

        result = self.service.apply_enrichment(
            product,
            self._andrey_import(
                materials=json.dumps(['Силикон'], ensure_ascii=False),
                gender=None,
            ),
            ['characteristics'],
            'smart_merge',
            SimpleNamespace(id=1),
            client,
        )

        self.assertTrue(result['success'], result['error'])
        self.assertEqual(result['fields_applied'], [])
        self.assertFalse(result['wb_sync'])
        client._make_request.assert_not_called()
        self.assertEqual(
            json.loads(product.characteristics_json),
            client.get_card_by_nm_id.return_value['characteristics'],
        )
        history = CardEditHistory.query.one()
        self.assertEqual(history.changed_fields, [])
        self.assertEqual(history.wb_sync_status, 'skipped')
        self.assertEqual(
            history.merge_decisions['characteristics']['counts'][
                'preserved_existing'
            ],
            1,
        )

    def test_transport_error_stays_uncertain_until_durable_reconciliation(self):
        product = self._product()
        before = {
            'nmID': product.nm_id,
            'subjectID': self.SUBJECT_ID,
            'vendorCode': product.vendor_code,
            'title': product.title,
            'brand': product.brand,
            'description': product.description,
            'sizes': [{'chrtID': 1, 'skus': ['1234567890123']}],
            'characteristics': [{
                'id': self.MATERIAL_ID,
                'value': ['Пластик'],
            }],
        }
        after = dict(before)
        after['characteristics'] = [{
            'id': self.MATERIAL_ID,
            'value': ['Силикон'],
        }, {
            'id': self.GENDER_ID,
            'value': ['Женский'],
        }]
        wb_client = MagicMock()

        def timed_out_update(_nm_id, _updates, **kwargs):
            kwargs['snapshot_context'].update({
                'before': before,
                'after': after,
            })
            kwargs['before_send_callback']({
                'before': before,
                'after': after,
            })
            raise TimeoutError('ответ WB потерян')

        wb_client.update_card.side_effect = timed_out_update
        wb_client.get_card_by_nm_id.return_value = after

        result = self.service.apply_enrichment(
            product,
            self._andrey_import(
                materials=json.dumps(['силикон'], ensure_ascii=False),
                gender='женский',
            ),
            ['characteristics'],
            'replace',
            SimpleNamespace(id=1),
            wb_client,
        )

        self.assertFalse(result['success'])
        self.assertFalse(result['wb_sync'])
        self.assertTrue(result['reconciliation_pending'])
        history = CardEditHistory.query.one()
        self.assertFalse(history.wb_synced)
        self.assertEqual(history.wb_sync_status, 'uncertain')
        self.assertIsNotNone(history.wb_reconcile_due_at)
        self.assertEqual(
            json.loads(product.characteristics_json),
            [],
        )

    def test_uncertain_content_keeps_photo_followup_deferred(self):
        product = self._product()
        before = {
            'nmID': product.nm_id,
            'subjectID': self.SUBJECT_ID,
            'vendorCode': product.vendor_code,
            'title': product.title,
            'brand': product.brand,
            'description': product.description,
            'sizes': [{'chrtID': 1, 'skus': ['1234567890123']}],
            'characteristics': [],
        }
        after = {**before, 'title': 'Карточка Андрея полная'}
        wb_client = MagicMock()

        def timed_out_update(_nm_id, _updates, **kwargs):
            kwargs['snapshot_context'].update({
                'before': before,
                'after': after,
                'applied_update_fields': ['title'],
                'write_required': True,
            })
            kwargs['before_send_callback']({
                'before': before,
                'after': after,
                'applied_update_fields': ['title'],
                'write_required': True,
            })
            raise TimeoutError('ответ WB потерян')

        wb_client.update_card.side_effect = timed_out_update
        result = self.service.apply_enrichment(
            product,
            self._andrey_import(
                title='Карточка Андрея полная',
                photo_urls=json.dumps(['https://supplier.invalid/one.jpg']),
            ),
            ['title', 'photos'],
            'smart_merge',
            SimpleNamespace(id=1),
            wb_client,
        )

        self.assertTrue(result['deferred'])
        self.assertTrue(result['reconciliation_pending'])
        self.assertIn('photos', result['fields_pending'])
        self.assertEqual(
            result['photos']['reason'],
            'awaiting_content_reconciliation',
        )
        wb_client.upload_photos_to_card.assert_not_called()

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

    def test_cross_tenant_import_is_rejected_before_side_effect(self):
        wb_client = MagicMock()

        result = self.service.apply_enrichment(
            self._product(),
            self._andrey_import(
                seller_id=2,
                materials=json.dumps(['Силикон'], ensure_ascii=False),
                gender='Женский',
            ),
            ['characteristics'],
            'replace',
            SimpleNamespace(id=1),
            wb_client,
        )

        self.assertFalse(result['success'])
        self.assertIn('seller scope mismatch', result['error'])
        wb_client.update_card.assert_not_called()

    def test_malformed_local_product_json_does_not_break_preview(self):
        product = self._product()
        product.characteristics_json = '{broken'
        product.dimensions_json = '[]'
        product.photos_json = '{broken'

        preview = self.service.build_preview(
            product,
            self._andrey_import(
                materials=json.dumps(['Силикон'], ensure_ascii=False),
                gender='Женский',
            ),
        )

        self.assertEqual(preview['characteristics']['current'], [])
        self.assertEqual(preview['dimensions']['current'], {})
        self.assertEqual(preview['photos']['current_count'], 0)


if __name__ == '__main__':
    unittest.main()
