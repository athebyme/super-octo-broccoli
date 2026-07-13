# -*- coding: utf-8 -*-
"""Регрессия: supplier characteristics проходят строгие WB admin-словари."""

import json
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

from flask import Flask

from models import (
    db, ImportedProduct, Marketplace, MarketplaceCategory,
    MarketplaceCategoryCharacteristic, MarketplaceDirectory,
)


class WBCharacteristicDictionaryTestCase(unittest.TestCase):
    SUBJECT_ID = 777
    MATERIAL_ID = 10
    GENDER_ID = 11

    def setUp(self):
        self.app = Flask(__name__)
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        self.app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
        self.app.config['SECRET_KEY'] = 'test-secret'
        db.init_app(self.app)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self._seed_admin_schema()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _seed_admin_schema(self):
        marketplace = Marketplace(
            name='Wildberries',
            code='wb',
            categories_synced_at=datetime.utcnow(),
            categories_sync_status='success',
        )
        db.session.add(marketplace)
        db.session.flush()
        category = MarketplaceCategory(
            marketplace_id=marketplace.id,
            subject_id=self.SUBJECT_ID,
            subject_name='Тестовый предмет',
            is_enabled=True,
            is_available=True,
            characteristics_synced_at=datetime.utcnow(),
            characteristics_sync_status='success',
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
                    [{'value': 'Силикон'}, {'value': 'Пластик'}, {'value': 'Металл'}],
                    ensure_ascii=False,
                ),
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
                    [{'value': 'Женский'}, {'value': 'Мужской'}],
                    ensure_ascii=False,
                ),
                is_enabled=True,
                is_available=True,
            ),
        ])
        db.session.add(MarketplaceDirectory(
            marketplace_id=marketplace.id,
            directory_type='kinds',
            # Глобальный WB kinds шире конкретной категории. Даже если в нём
            # есть «Унисекс», category-scoped allowlist выше имеет приоритет.
            data_json=json.dumps(
                ['Женский', 'Мужской', 'Унисекс'], ensure_ascii=False),
            items_count=3,
            synced_at=datetime.utcnow(),
            sync_status='success',
        ))
        db.session.commit()

    def _full_card(self):
        return {
            'nmID': 1001,
            'subjectID': self.SUBJECT_ID,
            'vendorCode': 'VC-1001',
            'title': 'Карточка',
            'brand': 'Brand',
            'sizes': [{'chrtID': 1, 'skus': ['1234567890123']}],
            'characteristics': [
                {'id': 999, 'value': ['Существующее значение']},
                {'id': self.MATERIAL_ID, 'value': ['Силикон']},
            ],
        }

    def test_invalid_material_and_unlisted_unisex_are_rejected(self):
        from services.marketplace_validator import validate_wb_characteristics

        material = validate_wb_characteristics(self.SUBJECT_ID, [{
            'id': self.MATERIAL_ID,
            'value': ['наилучшем виде'],
        }])
        self.assertFalse(material['valid'])
        self.assertEqual(material['issues'][0]['code'], 'value_not_allowed')

        gender = validate_wb_characteristics(self.SUBJECT_ID, [{
            'id': self.GENDER_ID,
            'value': ['Унисекс'],
        }])
        self.assertFalse(gender['valid'])
        self.assertEqual(gender['issues'][0]['code'], 'value_not_allowed')

    def test_exact_dictionary_match_canonicalizes_case(self):
        from services.marketplace_validator import validate_wb_characteristics

        result = validate_wb_characteristics(self.SUBJECT_ID, [
            {'id': self.MATERIAL_ID, 'value': 'силикон'},
            {'id': self.GENDER_ID, 'value': 'женский'},
        ])

        self.assertTrue(result['valid'], result['issues'])
        self.assertEqual(result['normalized'], [
            {'id': self.MATERIAL_ID, 'value': ['Силикон']},
            {'id': self.GENDER_ID, 'value': ['Женский']},
        ])
        self.assertEqual(set(result['directories_used']), {'category'})

    def test_fuzzy_and_substring_characteristic_names_are_rejected(self):
        from services.marketplace_validator import (
            WBCharacteristicValidationError,
            build_wb_characteristic_patch,
        )

        with self.assertRaises(WBCharacteristicValidationError):
            build_wb_characteristic_patch(self.SUBJECT_ID, {
                'Материал': 'Силикон',
            })
        with self.assertRaises(WBCharacteristicValidationError):
            build_wb_characteristic_patch(self.SUBJECT_ID, {
                'Материал изделия': 'Силиконн',
            })

    def test_dictionary_value_does_not_match_after_punctuation_rewrite(self):
        from services.marketplace_validator import validate_wb_characteristics

        characteristic = MarketplaceCategoryCharacteristic.query.filter_by(
            charc_id=self.MATERIAL_ID,
        ).one()
        characteristic.dictionary_json = json.dumps(
            [{'value': 'ABS-пластик'}], ensure_ascii=False)
        db.session.commit()

        result = validate_wb_characteristics(self.SUBJECT_ID, [{
            'id': self.MATERIAL_ID,
            'value': ['ABS пластик'],
        }])

        self.assertFalse(result['valid'])
        self.assertEqual(result['issues'][0]['code'], 'value_not_allowed')

    def test_stale_or_unavailable_reference_fails_closed(self):
        from services.marketplace_validator import validate_wb_characteristics

        category = MarketplaceCategory.query.filter_by(
            subject_id=self.SUBJECT_ID,
        ).one()
        category.characteristics_synced_at = datetime.utcnow() - timedelta(hours=49)
        db.session.commit()

        stale = validate_wb_characteristics(self.SUBJECT_ID, [{
            'id': self.MATERIAL_ID,
            'value': ['Силикон'],
        }])
        self.assertFalse(stale['valid'])
        self.assertEqual(stale['issues'][0]['code'], 'schema_stale')

        category.characteristics_synced_at = datetime.utcnow()
        category.is_available = False
        db.session.commit()
        unavailable = validate_wb_characteristics(self.SUBJECT_ID, [{
            'id': self.MATERIAL_ID,
            'value': ['Силикон'],
        }])
        self.assertFalse(unavailable['valid'])
        self.assertEqual(unavailable['issues'][0]['code'], 'category_unavailable')

    def test_unavailable_characteristic_is_not_writable(self):
        from services.marketplace_validator import validate_wb_characteristics

        characteristic = MarketplaceCategoryCharacteristic.query.filter_by(
            charc_id=self.MATERIAL_ID,
        ).one()
        characteristic.is_available = False
        db.session.commit()

        result = validate_wb_characteristics(self.SUBJECT_ID, [{
            'id': self.MATERIAL_ID,
            'value': ['Силикон'],
        }])
        self.assertFalse(result['valid'])
        self.assertEqual(result['issues'][0]['code'], 'unknown_characteristic')

    def test_missing_admin_schema_fails_closed(self):
        from services.marketplace_validator import validate_wb_characteristics

        result = validate_wb_characteristics(999999, [{
            'id': self.MATERIAL_ID,
            'value': ['Силикон'],
        }])

        self.assertFalse(result['valid'])
        self.assertEqual(result['issues'][0]['code'], 'category_not_found')

    def test_missing_material_dictionary_fails_closed(self):
        from services.marketplace_validator import validate_wb_characteristics

        characteristic = MarketplaceCategoryCharacteristic.query.filter_by(
            charc_id=self.MATERIAL_ID,
        ).one()
        characteristic.dictionary_json = None
        db.session.commit()

        result = validate_wb_characteristics(self.SUBJECT_ID, [{
            'id': self.MATERIAL_ID,
            'value': ['Силикон'],
        }])

        self.assertFalse(result['valid'])
        self.assertEqual(result['issues'][0]['code'], 'dictionary_not_synced')

    def test_gender_requires_category_allowlist_even_if_global_kinds_has_value(self):
        from services.marketplace_validator import validate_wb_characteristics

        characteristic = MarketplaceCategoryCharacteristic.query.filter_by(
            charc_id=self.GENDER_ID,
        ).one()
        characteristic.dictionary_json = None
        db.session.commit()

        result = validate_wb_characteristics(self.SUBJECT_ID, [{
            'id': self.GENDER_ID,
            'value': ['Унисекс'],
        }])

        self.assertFalse(result['valid'])
        self.assertEqual(
            result['issues'][0]['code'],
            'category_dictionary_not_configured',
        )

    def test_optional_invalid_value_is_not_persisted_by_marketplace_validator(self):
        from services.marketplace_validator import MarketplaceValidator

        marketplace = Marketplace.query.filter_by(code='wb').one()
        product = SimpleNamespace(
            wb_subject_id=self.SUBJECT_ID,
            marketplace_fields_json=None,
            marketplace_validation_status=None,
            marketplace_fill_pct=None,
            get_ai_parsed_data=lambda: {
                'Материал изделия': ['наилучшем виде'],
            },
            get_ai_marketplace_data=lambda: {},
        )

        result = MarketplaceValidator.validate_product_for_marketplace(
            product, marketplace.id)

        self.assertEqual(result['validation_status'], 'invalid')
        self.assertTrue(any('Материал изделия' in error
                            for error in result['validation_errors']))
        self.assertNotIn(
            'Материал изделия', json.loads(product.marketplace_fields_json))

    def test_characteristic_patch_preserves_other_values(self):
        from services.marketplace_validator import merge_wb_characteristics

        merged = merge_wb_characteristics(
            self._full_card()['characteristics'],
            [{'id': self.GENDER_ID, 'value': ['Женский']}],
        )

        self.assertEqual([item['id'] for item in merged], [999, self.MATERIAL_ID, self.GENDER_ID])
        self.assertEqual(merged[0]['value'], ['Существующее значение'])
        self.assertEqual(merged[1]['value'], ['Силикон'])

    def test_characteristic_patch_preserves_local_name_metadata(self):
        from services.marketplace_validator import merge_wb_characteristics

        merged = merge_wb_characteristics(
            [{
                'id': self.MATERIAL_ID,
                'name': 'Материал изделия',
                'value': ['Силикон'],
            }],
            [{'id': self.MATERIAL_ID, 'value': ['Пластик']}],
        )

        self.assertEqual(merged, [{
            'id': self.MATERIAL_ID,
            'name': 'Материал изделия',
            'value': ['Пластик'],
        }])

    def test_legacy_named_characteristics_are_preserved_and_mapped(self):
        from services.marketplace_validator import merge_wb_characteristics

        merged = merge_wb_characteristics(
            {
                'Материал изделия': ['Силикон'],
                'Локальное поле': 'legacy',
            },
            [{'id': self.MATERIAL_ID, 'value': ['Пластик']}],
            subject_id=self.SUBJECT_ID,
        )

        self.assertEqual(merged[0], {
            'id': self.MATERIAL_ID,
            'name': 'Материал изделия',
            'value': ['Пластик'],
        })
        self.assertEqual(merged[1], {
            'name': 'Локальное поле',
            'value': 'legacy',
        })

    def test_update_card_blocks_invalid_patch_before_network(self):
        from services.marketplace_validator import WBCharacteristicValidationError
        from services.wb_api_client import WildberriesAPIClient

        client = WildberriesAPIClient('test-key')
        client.get_card_by_nm_id = MagicMock(return_value=self._full_card())
        client._make_request = MagicMock()

        with self.assertRaises(WBCharacteristicValidationError):
            client.update_card(1001, {
                'characteristics': {
                    'Материал изделия': 'наилучшем виде',
                },
            })

        client._make_request.assert_not_called()

    def test_supplier_enrichment_blocks_invalid_material_before_wb(self):
        from services.supplier_enrichment import EnrichmentService

        product = SimpleNamespace(
            nm_id=1001,
            vendor_code='VC-1001',
            title='Карточка',
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
            subject_id=self.SUBJECT_ID,
        )
        imported = SimpleNamespace(
            characteristics=json.dumps({
                'Материал изделия': 'наилучшем виде',
            }, ensure_ascii=False),
        )
        wb_client = MagicMock()

        result = EnrichmentService().apply_enrichment(
            product,
            imported,
            ['characteristics'],
            'replace',
            SimpleNamespace(id=1),
            wb_client,
        )

        self.assertFalse(result['success'])
        self.assertEqual(result['fields_applied'], [])
        self.assertIn('наилучшем виде', result['error'])
        wb_client.update_card.assert_not_called()

    def test_update_card_sends_canonical_patch_without_erasing_existing(self):
        from services.wb_api_client import WildberriesAPIClient

        response = MagicMock()
        response.json.return_value = {'error': False}
        client = WildberriesAPIClient('test-key')
        client.get_card_by_nm_id = MagicMock(return_value=self._full_card())
        client._make_request = MagicMock(return_value=response)

        client.update_card(1001, {
            'characteristics': {
                'Материал изделия': 'пластик',
            },
        })

        sent = client._make_request.call_args.kwargs['json'][0]
        chars = {item['id']: item['value'] for item in sent['characteristics']}
        self.assertEqual(chars[999], ['Существующее значение'])
        self.assertEqual(chars[self.MATERIAL_ID], ['Пластик'])

    def test_empty_characteristic_object_is_an_empty_patch_not_erase(self):
        from services.wb_api_client import WildberriesAPIClient

        response = MagicMock()
        response.json.return_value = {'error': False}
        client = WildberriesAPIClient('test-key')
        client.get_card_by_nm_id = MagicMock(return_value=self._full_card())
        client._make_request = MagicMock(return_value=response)

        client.update_card(1001, {'characteristics': {}})

        sent = client._make_request.call_args.kwargs['json'][0]
        self.assertEqual(
            sent['characteristics'],
            self._full_card()['characteristics'],
        )

    def test_null_characteristics_are_rejected_before_network(self):
        from services.marketplace_validator import WBCharacteristicValidationError
        from services.wb_api_client import WildberriesAPIClient

        client = WildberriesAPIClient('test-key')
        client.get_card_by_nm_id = MagicMock(return_value=self._full_card())
        client._make_request = MagicMock()

        with self.assertRaises(WBCharacteristicValidationError):
            client.update_card(1001, {'characteristics': None})

        client._make_request.assert_not_called()

    def test_prepare_rejects_non_list_characteristic_replacement(self):
        from services.wb_validators import (
            WBValidationError,
            prepare_card_for_update,
        )

        with self.assertRaises(WBValidationError):
            prepare_card_for_update(
                self._full_card(),
                {'characteristics': None},
            )

    def test_prepare_maps_legacy_named_characteristics_before_wire_cleanup(self):
        from services.wb_validators import prepare_card_for_update

        full_card = self._full_card()
        full_card['characteristics'] = {
            'Материал изделия': 'силикон',
            'Пол': 'женский',
        }

        prepared = prepare_card_for_update(
            full_card,
            {'description': 'Новое описание'},
        )

        self.assertEqual(prepared['characteristics'], [
            {'id': self.MATERIAL_ID, 'value': ['Силикон']},
            {'id': self.GENDER_ID, 'value': ['Женский']},
        ])

    def test_direct_batch_cannot_bypass_admin_dictionary_validation(self):
        from services.marketplace_validator import WBCharacteristicValidationError
        from services.wb_api_client import WildberriesAPIClient

        client = WildberriesAPIClient('test-key')
        client._make_request = MagicMock()
        card = self._full_card()
        card['characteristics'] = [{
            'id': self.MATERIAL_ID,
            'value': ['наилучшем виде'],
        }]

        with self.assertRaises(WBCharacteristicValidationError):
            client.update_cards_batch([card])

        client._make_request.assert_not_called()

    def test_direct_batch_rejects_null_characteristics_and_missing_subject(self):
        from services.marketplace_validator import WBCharacteristicValidationError
        from services.wb_api_client import WildberriesAPIClient, WBAPIException

        client = WildberriesAPIClient('test-key')
        client._make_request = MagicMock()
        null_card = self._full_card()
        null_card['characteristics'] = None
        with self.assertRaises(WBCharacteristicValidationError):
            client.update_cards_batch([null_card])

        no_subject = self._full_card()
        no_subject.pop('subjectID')
        no_subject['characteristics'] = [{
            'id': self.MATERIAL_ID,
            'value': ['Силикон'],
        }]
        with self.assertRaises(WBAPIException):
            client.update_cards_batch([no_subject])

        empty_card = self._full_card()
        empty_card['characteristics'] = []
        with self.assertRaises(WBAPIException):
            client.update_cards_batch([empty_card])

        client._make_request.assert_not_called()

    def test_create_card_blocks_invalid_characteristics_before_network(self):
        from services.marketplace_validator import WBCharacteristicValidationError
        from services.wb_api_client import WildberriesAPIClient

        client = WildberriesAPIClient('test-key')
        client._make_request = MagicMock()

        with self.assertRaises(WBCharacteristicValidationError):
            client.create_product_card(
                self.SUBJECT_ID,
                [{
                    'vendorCode': 'VC-CREATE',
                    'sizes': [{'techSize': '0', 'skus': ['1234567890123']}],
                    'characteristics': [{
                        'id': self.MATERIAL_ID,
                        'value': ['наилучшем виде'],
                    }],
                }],
            )

        client._make_request.assert_not_called()

    def test_create_batch_canonicalizes_characteristics(self):
        from services.wb_api_client import WildberriesAPIClient

        response = MagicMock()
        response.json.return_value = {'error': False}
        client = WildberriesAPIClient('test-key')
        client._make_request = MagicMock(return_value=response)

        client.create_product_cards_batch([{
            'subjectID': self.SUBJECT_ID,
            'variants': [{
                'vendorCode': 'VC-BATCH',
                'brand': 'Brand',
                'title': 'Тестовая карточка',
                'sizes': [{'techSize': '0', 'skus': ['1234567890123']}],
                'characteristics': [{
                    'id': self.MATERIAL_ID,
                    'value': ['пластик'],
                }],
            }],
        }])

        sent = client._make_request.call_args.kwargs['json']
        self.assertEqual(
            sent[0]['variants'][0]['characteristics'],
            [{'id': self.MATERIAL_ID, 'value': ['Пластик']}],
        )

    def test_andrey_material_is_checked_during_card_build(self):
        from services.marketplace_validator import WBCharacteristicValidationError
        from services.wb_product_importer import WBProductImporter

        imp = ImportedProduct(
            seller_id=1,
            external_id='ANDREY-1',
            wb_subject_id=self.SUBJECT_ID,
            materials=json.dumps(['наилучшем виде'], ensure_ascii=False),
        )
        db.session.add(imp)
        db.session.commit()

        importer = WBProductImporter.__new__(WBProductImporter)
        importer.seller = SimpleNamespace(id=1)
        importer._chars_config_cache = {}
        importer._wb_directories_cache = None
        importer.api_client = SimpleNamespace(
            get_card_characteristics_config=lambda _subject_id: {'data': [
                {
                    'charcID': self.MATERIAL_ID,
                    'name': 'Материал изделия',
                    'charcType': 1,
                    'maxCount': 1,
                },
                {
                    'charcID': self.GENDER_ID,
                    'name': 'Пол',
                    'charcType': 1,
                    'maxCount': 1,
                },
            ]},
        )

        with self.assertRaises(WBCharacteristicValidationError):
            importer._build_wb_characteristics(imp)

    def test_adult_category_does_not_invent_unisex_gender(self):
        from services.wb_product_importer import WBProductImporter

        defaults = WBProductImporter._get_category_default_chars(
            5067, SimpleNamespace())
        self.assertNotIn('Пол', defaults)


class TestDescriptionMaterialExtraction(unittest.TestCase):
    def test_prose_phrase_is_not_treated_as_material(self):
        from services.description_enricher import DescriptionEnricher

        text = 'Храните изделие аккуратно, чтобы сохранить материал в наилучшем виде.'
        self.assertEqual(DescriptionEnricher._extract_materials(text), [])

    def test_explicit_material_label_is_still_extracted(self):
        from services.description_enricher import DescriptionEnricher

        self.assertEqual(
            DescriptionEnricher._extract_materials('Материал: силикон.'),
            ['Силикон'],
        )
        self.assertEqual(
            DescriptionEnricher._extract_materials(
                'Материал изделия: пластик.'),
            ['Пластик'],
        )


if __name__ == '__main__':
    unittest.main()
