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

    def test_characteristic_and_subject_ids_reject_numeric_coercion(self):
        from services.marketplace_validator import validate_wb_characteristics

        for raw_id in (True, float(self.MATERIAL_ID), str(self.MATERIAL_ID)):
            with self.subTest(charc_id=raw_id):
                result = validate_wb_characteristics(self.SUBJECT_ID, [{
                    'id': raw_id,
                    'value': ['Силикон'],
                }])
                self.assertFalse(result['valid'])
                self.assertEqual(result['issues'][0]['code'], 'invalid_payload')

        for raw_subject_id in (
            True, float(self.SUBJECT_ID), str(self.SUBJECT_ID),
        ):
            with self.subTest(subject_id=raw_subject_id):
                result = validate_wb_characteristics(raw_subject_id, [{
                    'id': self.MATERIAL_ID,
                    'value': ['Силикон'],
                }])
                self.assertFalse(result['valid'])

    def test_rollback_characteristic_ids_reject_numeric_coercion(self):
        from services.marketplace_validator import (
            WBCharacteristicValidationError,
            require_wb_characteristic_ids,
        )

        for raw_id in (True, float(self.MATERIAL_ID), str(self.MATERIAL_ID)):
            with self.subTest(charc_id=raw_id):
                with self.assertRaises(WBCharacteristicValidationError):
                    require_wb_characteristic_ids(
                        self.SUBJECT_ID, [raw_id],
                    )

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

    def test_admin_disabled_marketplace_or_category_fails_closed(self):
        from services.marketplace_validator import validate_wb_characteristics

        marketplace = Marketplace.query.filter_by(code='wb').one()
        category = MarketplaceCategory.query.filter_by(
            subject_id=self.SUBJECT_ID,
        ).one()
        payload = [{
            'id': self.MATERIAL_ID,
            'value': ['Силикон'],
        }]

        marketplace.is_active = False
        db.session.commit()
        inactive = validate_wb_characteristics(self.SUBJECT_ID, payload)
        self.assertFalse(inactive['valid'])
        self.assertEqual(inactive['issues'][0]['code'], 'marketplace_inactive')

        marketplace.is_active = True
        category.is_enabled = False
        db.session.commit()
        disabled = validate_wb_characteristics(self.SUBJECT_ID, payload)
        self.assertFalse(disabled['valid'])
        self.assertEqual(disabled['issues'][0]['code'], 'category_disabled')

        category.is_enabled = True
        category.is_leaf = False
        db.session.commit()
        parent = validate_wb_characteristics(self.SUBJECT_ID, payload)
        self.assertFalse(parent['valid'])
        self.assertEqual(parent['issues'][0]['code'], 'category_not_leaf')

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

    def test_composition_alias_is_treated_as_material_and_requires_dictionary(self):
        from services.marketplace_validator import validate_wb_characteristics

        characteristic = MarketplaceCategoryCharacteristic.query.filter_by(
            charc_id=self.MATERIAL_ID,
        ).one()
        characteristic.name = 'Состав'
        characteristic.dictionary_json = None
        db.session.commit()

        result = validate_wb_characteristics(self.SUBJECT_ID, [{
            'id': self.MATERIAL_ID,
            'value': ['наилучшем виде'],
        }])

        self.assertFalse(result['valid'])
        self.assertEqual(result['issues'][0]['code'], 'dictionary_not_synced')

    def test_legacy_disabled_required_characteristic_is_still_required_on_create(self):
        from services.marketplace_validator import (
            WBCharacteristicValidationError,
            build_wb_create_characteristics,
        )

        characteristic = MarketplaceCategoryCharacteristic.query.filter_by(
            charc_id=self.MATERIAL_ID,
        ).one()
        characteristic.required = True
        characteristic.is_enabled = False
        db.session.commit()

        with self.assertRaises(WBCharacteristicValidationError) as raised:
            build_wb_create_characteristics(self.SUBJECT_ID, [{
                'id': self.GENDER_ID,
                'value': ['Женский'],
            }])

        self.assertEqual(
            raised.exception.result['issues'][0]['code'],
            'required_characteristic_missing',
        )

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

    def test_global_directory_wins_for_global_characteristic(self):
        from services.marketplace_validator import validate_wb_characteristics

        marketplace = Marketplace.query.filter_by(code='wb').one()
        category = MarketplaceCategory.query.filter_by(
            subject_id=self.SUBJECT_ID,
        ).one()
        color_id = 12
        db.session.add(MarketplaceCategoryCharacteristic(
            marketplace_id=marketplace.id,
            category_id=category.id,
            charc_id=color_id,
            name='Цвет',
            charc_type=1,
            max_count=1,
            # Category payload не должен подменять общий справочник WB.
            dictionary_json=json.dumps(
                [{'value': 'Локальный цвет'}], ensure_ascii=False),
            is_enabled=True,
            is_available=True,
        ))
        db.session.add(MarketplaceDirectory(
            marketplace_id=marketplace.id,
            directory_type='colors',
            data_json=json.dumps(
                [{'name': 'Красный'}], ensure_ascii=False),
            items_count=1,
            synced_at=datetime.utcnow(),
            sync_status='success',
        ))
        db.session.commit()

        valid = validate_wb_characteristics(self.SUBJECT_ID, [{
            'id': color_id,
            'value': ['красный'],
        }])
        invalid = validate_wb_characteristics(self.SUBJECT_ID, [{
            'id': color_id,
            'value': ['Локальный цвет'],
        }])

        self.assertTrue(valid['valid'], valid['issues'])
        self.assertEqual(valid['normalized'][0]['value'], ['Красный'])
        self.assertEqual(valid['directories_used'], ['colors'])
        self.assertFalse(invalid['valid'])
        self.assertEqual(invalid['issues'][0]['code'], 'value_not_allowed')

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

    def test_update_card_persists_exact_snapshot_callback_before_http(self):
        from services.wb_api_client import WildberriesAPIClient

        response = MagicMock()
        response.json.return_value = {'error': False}
        events = []
        snapshot_context = {}
        client = WildberriesAPIClient('test-key')
        client.get_card_by_nm_id = MagicMock(return_value=self._full_card())

        def make_request(*_args, **_kwargs):
            self.assertEqual(events, ['history'])
            return response

        client._make_request = MagicMock(side_effect=make_request)
        client.update_card(
            1001,
            {'title': 'Новое название'},
            snapshot_context=snapshot_context,
            before_send_callback=lambda exact: events.append(
                'history' if exact == snapshot_context else 'mismatch'
            ),
        )

        self.assertEqual(events, ['history'])
        self.assertEqual(snapshot_context['before']['title'], 'Карточка')
        self.assertEqual(snapshot_context['after']['title'], 'Новое название')

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
            from services.wb_validators import _mark_wb_card_as_fetched
            full_card = self._full_card()
            full_card['characteristics'] = None
            _mark_wb_card_as_fetched(full_card)
            prepare_card_for_update(
                full_card,
                {'characteristics': None},
            )

    def test_legacy_named_full_card_cannot_be_signed_as_fresh_wb_source(self):
        from services.wb_validators import (
            WBValidationError,
            prepare_card_for_update,
        )

        full_card = self._full_card()
        full_card['characteristics'] = {
            'Материал изделия': 'силикон',
            'Пол': 'женский',
        }
        with self.assertRaises(WBValidationError):
            prepare_card_for_update(
                full_card,
                {'description': 'Новое описание'},
            )

    def test_direct_batch_cannot_bypass_admin_dictionary_validation(self):
        from services.wb_api_client import WildberriesAPIClient, WBAPIException

        client = WildberriesAPIClient('test-key')
        client._make_request = MagicMock()
        card = self._full_card()
        card['characteristics'] = [{
            'id': self.MATERIAL_ID,
            'value': ['наилучшем виде'],
        }]

        with self.assertRaises(WBAPIException):
            client.update_cards_batch([card])

        client._make_request.assert_not_called()

    def test_direct_batch_rejects_spoofed_internal_marker(self):
        from services.wb_api_client import WildberriesAPIClient, WBAPIException
        from services.wb_validators import WB_CHARACTERISTICS_CHANGED_KEY

        client = WildberriesAPIClient('test-key')
        client._make_request = MagicMock()
        card = self._full_card()
        card['characteristics'] = [{
            'id': self.MATERIAL_ID,
            'value': ['наилучшем виде'],
        }]
        card[WB_CHARACTERISTICS_CHANGED_KEY] = []

        with self.assertRaises(WBAPIException):
            client.update_cards_batch([card])

        client._make_request.assert_not_called()

    def test_prepared_batch_context_detects_characteristic_mutation(self):
        from services.wb_api_client import WildberriesAPIClient, WBAPIException
        from services.wb_validators import (
            _mark_wb_card_as_fetched,
            prepare_card_for_update,
        )

        client = WildberriesAPIClient('test-key')
        client._make_request = MagicMock()
        full_card = self._full_card()
        _mark_wb_card_as_fetched(full_card)
        prepared = prepare_card_for_update(
            full_card,
            {'description': 'Новое описание'},
        )
        prepared['characteristics'][1]['value'] = ['наилучшем виде']

        with self.assertRaises(WBAPIException):
            client.update_cards_batch([prepared])

        client._make_request.assert_not_called()

    def test_direct_batch_rejects_null_characteristics_and_missing_subject(self):
        from services.wb_api_client import WildberriesAPIClient, WBAPIException

        client = WildberriesAPIClient('test-key')
        client._make_request = MagicMock()
        null_card = self._full_card()
        null_card['characteristics'] = None
        with self.assertRaises(WBAPIException):
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

    def test_direct_batch_rejects_missing_characteristics_and_subject_spoof(self):
        from services.wb_api_client import WildberriesAPIClient, WBAPIException

        client = WildberriesAPIClient('test-key')
        client._make_request = MagicMock()

        without_characteristics = self._full_card()
        without_characteristics.pop('characteristics')
        with self.assertRaises(WBAPIException):
            client.update_cards_batch([without_characteristics])

        spoofed_category = self._full_card()
        spoofed_category['subjectID'] = 999999
        with self.assertRaises(WBAPIException):
            client.update_cards_batch([spoofed_category])

        client._make_request.assert_not_called()

    def test_prepared_batch_accepts_scalar_cleaned_before_context_signing(self):
        from services.wb_api_client import WildberriesAPIClient
        from services.wb_validators import (
            _mark_wb_card_as_fetched,
            prepare_card_for_update,
        )

        response = MagicMock()
        response.json.return_value = {'error': False}
        client = WildberriesAPIClient('test-key')
        client._make_request = MagicMock(return_value=response)
        full_card = self._full_card()
        full_card['characteristics'][1]['value'] = 'Силикон'
        _mark_wb_card_as_fetched(full_card)

        prepared = prepare_card_for_update(
            full_card,
            {'description': 'Новое описание'},
        )
        client.update_cards_batch([prepared])

        sent = client._make_request.call_args.kwargs['json'][0]
        self.assertEqual(
            sent['characteristics'][1]['value'],
            ['Силикон'],
        )

    def test_prepared_batch_context_cannot_be_transplanted_to_other_nm_id(self):
        from services.wb_api_client import WildberriesAPIClient, WBAPIException
        from services.wb_validators import (
            _mark_wb_card_as_fetched,
            prepare_card_for_update,
        )

        client = WildberriesAPIClient('test-key')
        client._make_request = MagicMock()
        full_card = self._full_card()
        _mark_wb_card_as_fetched(full_card)
        prepared = prepare_card_for_update(
            full_card,
            {'description': 'Новое описание'},
        )
        prepared['nmID'] = 1002

        with self.assertRaises(WBAPIException):
            client.update_cards_batch([prepared])

        client._make_request.assert_not_called()

    def test_prepare_cannot_rebind_fetched_source_to_other_nm_id(self):
        from services.wb_validators import (
            WBValidationError,
            _mark_wb_card_as_fetched,
            prepare_card_for_update,
        )

        full_card = self._full_card()
        _mark_wb_card_as_fetched(full_card)
        with self.assertRaises(WBValidationError):
            prepare_card_for_update(
                full_card,
                {'nmID': 1002, 'title': 'Новое название'},
            )

    def test_safe_batch_helper_uses_entire_fresh_wb_card(self):
        from services.wb_validators import prepare_batch_cards_safe

        product = SimpleNamespace(
            nm_id=1001,
            vendor_code='VC-1001',
            subject_id=self.SUBJECT_ID,
        )
        fresh = self._full_card()
        fresh['characteristics'].append({
            'id': 998,
            'value': ['Добавлено параллельно в WB'],
        })
        from services.wb_validators import _mark_wb_card_as_fetched
        _mark_wb_card_as_fetched(fresh)
        client = MagicMock()
        client.fetch_cards_by_nm_ids.return_value = {1001: fresh}

        cards, product_map, skipped = prepare_batch_cards_safe(
            [product],
            lambda _product, _full_card: {'title': 'Новое название'},
            client,
            seller_id=1,
        )

        self.assertEqual(skipped, [])
        self.assertIs(product_map[1001], product)
        self.assertEqual(cards[0]['title'], 'Новое название')
        self.assertIn(998, [item['id'] for item in cards[0]['characteristics']])
        client.fetch_cards_by_nm_ids.assert_called_once_with(
            [1001], log_to_db=True, seller_id=1)

    def test_update_boundaries_treat_error_true_as_failure(self):
        from services.wb_api_client import WildberriesAPIClient, WBAPIException
        from services.wb_validators import (
            _mark_wb_card_as_fetched,
            prepare_card_for_update,
        )

        response = MagicMock()
        response.json.return_value = {
            'error': True,
            'errorText': 'WB rejected',
        }
        client = WildberriesAPIClient('test-key')
        client.get_card_by_nm_id = MagicMock(return_value=self._full_card())
        client._make_request = MagicMock(return_value=response)

        with self.assertRaises(WBAPIException):
            client.update_card(1001, {'title': 'Новое название'})

        full_card = self._full_card()
        _mark_wb_card_as_fetched(full_card)
        prepared = prepare_card_for_update(
            full_card, {'title': 'Новое название'})
        with self.assertRaises(WBAPIException):
            client.update_cards_batch([prepared])

    def test_single_title_update_blocks_invalid_existing_material(self):
        from services.marketplace_validator import WBCharacteristicValidationError
        from services.wb_api_client import WildberriesAPIClient

        full_card = self._full_card()
        full_card['characteristics'][1]['value'] = ['наилучшем виде']
        client = WildberriesAPIClient('test-key')
        client.get_card_by_nm_id = MagicMock(return_value=full_card)
        client._make_request = MagicMock()

        with self.assertRaises(WBCharacteristicValidationError):
            client.update_card(1001, {'title': 'Новое название'})

        client._make_request.assert_not_called()

    def test_prepared_title_update_blocks_invalid_existing_material(self):
        from services.marketplace_validator import WBCharacteristicValidationError
        from services.wb_api_client import WildberriesAPIClient
        from services.wb_validators import (
            _mark_wb_card_as_fetched,
            prepare_card_for_update,
        )

        full_card = self._full_card()
        full_card['characteristics'][1]['value'] = ['наилучшем виде']
        _mark_wb_card_as_fetched(full_card)
        prepared = prepare_card_for_update(
            full_card, {'title': 'Новое название'})
        client = WildberriesAPIClient('test-key')
        client._make_request = MagicMock()

        with self.assertRaises(WBCharacteristicValidationError):
            client.update_cards_batch([prepared])

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

    def test_create_card_requires_admin_required_characteristics(self):
        from services.marketplace_validator import WBCharacteristicValidationError
        from services.wb_api_client import WildberriesAPIClient

        material = MarketplaceCategoryCharacteristic.query.filter_by(
            charc_id=self.MATERIAL_ID,
        ).one()
        material.required = True
        db.session.commit()
        client = WildberriesAPIClient('test-key')
        client._make_request = MagicMock()

        with self.assertRaises(WBCharacteristicValidationError) as caught:
            client.create_product_card(
                self.SUBJECT_ID,
                [{
                    'vendorCode': 'VC-REQUIRED',
                    'sizes': [{
                        'techSize': '0',
                        'skus': ['1234567890123'],
                    }],
                }],
            )

        self.assertEqual(
            caught.exception.result['issues'][0]['code'],
            'required_characteristic_missing',
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
