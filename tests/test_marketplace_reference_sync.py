# -*- coding: utf-8 -*-
"""Deterministic marketplace reference synchronization tests (no WB calls)."""

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

from flask import Flask
from sqlalchemy import event

from migrations.migrate_add_marketplace_reference_freshness import apply_migration
from models import (
    Brand,
    db,
    Marketplace,
    MarketplaceCategory,
    MarketplaceCategoryCharacteristic,
    MarketplaceDirectory,
    MarketplaceBrand,
)
from services.brand_engine import BrandEngine
from services.marketplace_service import MarketplaceService


class FakeWBClient:
    def __init__(self):
        self.category_pages = {}
        self.category_calls = []
        self.characteristics = {}
        self.characteristic_calls = []
        self.directory_payloads = {
            'colors': [{'name': 'red'}],
            'countries': [{'name': 'RU'}],
            'kinds': [{'name': 'female'}],
            'seasons': [{'name': 'all'}],
            'vat': [{'value': 20}],
        }
        self.directory_failures = set()
        self.directory_calls = []
        self.brand_result = {'data': [], 'complete': True, 'errors': []}

    def get_subjects_list(self, limit, offset):
        self.category_calls.append((limit, offset))
        value = self.category_pages.get(offset, [])
        if isinstance(value, Exception):
            raise value
        return {'data': value}

    def get_card_characteristics_config(self, subject_id):
        self.characteristic_calls.append(subject_id)
        value = self.characteristics.get(subject_id, [])
        if isinstance(value, Exception):
            raise value
        return {'data': value}

    def _directory(self, name):
        self.directory_calls.append(name)
        if name in self.directory_failures:
            raise RuntimeError(f'{name} unavailable')
        return {'data': self.directory_payloads[name]}

    def get_directory_colors(self):
        return self._directory('colors')

    def get_directory_countries(self):
        return self._directory('countries')

    def get_directory_kinds(self):
        return self._directory('kinds')

    def get_directory_seasons(self):
        return self._directory('seasons')

    def get_directory_vat(self):
        return self._directory('vat')

    def fetch_all_brands(self, subject_ids, top=5000, progress_callback=None):
        if progress_callback:
            progress_callback(len(subject_ids), len(subject_ids), len(self.brand_result['data']))
        return self.brand_result


def charc(
    charc_id,
    name,
    *,
    charc_type=1,
    required=False,
    dictionary=None,
    max_count=1,
):
    return {
        'charcID': charc_id,
        'name': name,
        'charcType': charc_type,
        'required': required,
        'unitName': None,
        'maxCount': max_count,
        'popular': False,
        'hasFilter': False,
        'isVariable': False,
        'dictionary': dictionary,
    }


class MarketplaceReferenceSyncTestCase(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        self.app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
        self.app.config['SECRET_KEY'] = 'marketplace-reference-test'
        self.app.config['WTF_CSRF_ENABLED'] = False
        db.init_app(self.app)
        self.app.add_url_rule(
            '/dashboard', endpoint='dashboard', view_func=lambda: 'dashboard',
        )
        from routes.marketplaces import marketplaces_bp
        self.app.register_blueprint(marketplaces_bp)
        self.http = self.app.test_client()
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        self.marketplace = Marketplace(name='Wildberries', code='wb')
        db.session.add(self.marketplace)
        db.session.commit()
        self.client = FakeWBClient()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def add_category(self, subject_id, name, **kwargs):
        category = MarketplaceCategory(
            marketplace_id=self.marketplace.id,
            subject_id=subject_id,
            subject_name=name,
            **kwargs,
        )
        db.session.add(category)
        db.session.commit()
        return category

    @staticmethod
    def admin_user(is_admin=True):
        user = MagicMock()
        user.is_authenticated = True
        user.is_admin = is_admin
        return user

    def test_category_sync_is_idempotent_and_marks_missing_unavailable(self):
        now = datetime(2026, 7, 13, 8, 0, 0)
        kept = self.add_category(
            10, 'Old name', is_enabled=True, is_available=True,
        )
        removed = self.add_category(
            20, 'Removed', is_enabled=True, is_available=True,
        )
        self.client.category_pages[0] = [
            {
                'subjectID': 10,
                'subjectName': 'Renamed',
                'parentID': 1,
                'parentName': 'Parent',
            },
            {
                'subjectID': 30,
                'subjectName': 'New',
                'parentID': 1,
                'parentName': 'Parent',
            },
        ]

        first = MarketplaceService.sync_categories(
            self.marketplace.id, client=self.client, now=now,
        )
        db.session.refresh(kept)
        db.session.refresh(removed)
        created = MarketplaceCategory.query.filter_by(subject_id=30).one()

        self.assertTrue(first['success'])
        self.assertEqual(first['added'], 1)
        self.assertEqual(first['updated'], 2)
        self.assertEqual(kept.subject_name, 'Renamed')
        self.assertTrue(kept.is_enabled)
        self.assertTrue(kept.is_available)
        self.assertFalse(removed.is_available)
        self.assertTrue(removed.is_enabled)  # admin intent is not destroyed
        self.assertTrue(created.is_available)
        self.assertFalse(created.is_enabled)
        self.assertEqual(self.marketplace.categories_version, 1)

        second = MarketplaceService.sync_categories(
            self.marketplace.id,
            client=self.client,
            now=now + timedelta(hours=1),
        )
        self.assertTrue(second['success'])
        self.assertEqual(second['added'], 0)
        self.assertEqual(second['updated'], 0)
        self.assertEqual(self.marketplace.categories_version, 1)

    def test_empty_duplicate_and_anomalous_shrink_preserve_category_cache(self):
        first = self.add_category(1, 'One', is_enabled=True, is_available=True)
        second = self.add_category(2, 'Two', is_enabled=True, is_available=True)

        self.client.category_pages[0] = []
        empty = MarketplaceService.sync_categories(
            self.marketplace.id, client=self.client,
        )
        self.assertFalse(empty['success'])
        self.assertTrue(first.is_available)
        self.assertTrue(second.is_available)

        self.client.category_pages[0] = [
            {'subjectID': 1, 'subjectName': 'One'},
            {'subjectID': 1, 'subjectName': 'One duplicate'},
        ]
        duplicate = MarketplaceService.sync_categories(
            self.marketplace.id, client=self.client,
        )
        self.assertFalse(duplicate['success'])
        self.assertIn('duplicated', duplicate['error'])
        self.assertEqual(first.subject_name, 'One')

        self.client.category_pages[0] = [
            {'subjectID': 1, 'subjectName': 'One'},
        ]
        with patch.object(MarketplaceService, 'CATEGORY_SHRINK_GUARD_MIN', 2):
            shrink = MarketplaceService.sync_categories(
                self.marketplace.id, client=self.client,
            )
        self.assertFalse(shrink['success'])
        self.assertIn('shrank anomalously', shrink['error'])
        self.assertTrue(second.is_available)

    def test_category_pagination_uses_offset_and_requires_unique_ids(self):
        self.client.category_pages = {
            0: [
                {'subjectID': 1, 'subjectName': 'One'},
                {'subjectID': 2, 'subjectName': 'Two'},
            ],
            2: [{'subjectID': 3, 'subjectName': 'Three'}],
        }
        with patch.object(MarketplaceService, 'CATEGORY_PAGE_SIZE', 2):
            result = MarketplaceService.sync_categories(
                self.marketplace.id,
                client=self.client,
                sleep_fn=lambda _seconds: None,
            )

        self.assertTrue(result['success'])
        self.assertEqual(self.client.category_calls, [(2, 0), (2, 2)])
        self.assertEqual(result['total'], 3)

    def test_characteristics_sync_tracks_schema_and_preserves_custom_instruction(self):
        now = datetime(2026, 7, 13, 9, 0, 0)
        category = self.add_category(
            100, 'Category', is_enabled=True, is_available=True,
        )
        custom = MarketplaceCategoryCharacteristic(
            category_id=category.id,
            marketplace_id=self.marketplace.id,
            charc_id=1,
            name='Color',
            charc_type=1,
            required=False,
            max_count=1,
            dictionary_json='[{"value":"Old"}]',
            ai_instruction='Manual instruction',
            ai_instruction_source='custom',
            is_enabled=True,
            is_available=True,
        )
        removed = MarketplaceCategoryCharacteristic(
            category_id=category.id,
            marketplace_id=self.marketplace.id,
            charc_id=2,
            name='Removed',
            charc_type=1,
            ai_instruction='Generated old',
            ai_instruction_source='generated',
            is_available=True,
        )
        generated = MarketplaceCategoryCharacteristic(
            category_id=category.id,
            marketplace_id=self.marketplace.id,
            charc_id=3,
            name='Weight',
            charc_type=4,
            required=False,
            ai_instruction='old generated value',
            ai_instruction_source='generated',
            is_available=True,
        )
        db.session.add_all([custom, removed, generated])
        db.session.commit()
        self.client.characteristics[100] = [
            charc(
                1, 'Colour', required=True,
                dictionary=[{'value': 'Black'}, {'value': 'White'}],
            ),
            charc(3, 'Weight', charc_type=4, required=True, dictionary=None),
            charc(4, 'Material', dictionary=[{'value': 'Cotton'}]),
        ]

        first = MarketplaceService.sync_category_characteristics(
            category.id, client=self.client, now=now,
        )
        db.session.refresh(custom)
        db.session.refresh(removed)
        db.session.refresh(generated)

        self.assertTrue(first['success'])
        self.assertEqual(custom.name, 'Colour')
        self.assertTrue(custom.required)
        self.assertEqual(
            custom.dictionary_json,
            '[{"value":"Black"},{"value":"White"}]',
        )
        self.assertEqual(custom.ai_instruction, 'Manual instruction')
        self.assertEqual(custom.ai_instruction_source, 'custom')
        self.assertFalse(removed.is_available)
        self.assertIn('не выдумывай', generated.ai_instruction)
        self.assertEqual(generated.ai_instruction_source, 'generated')
        self.assertEqual(category.characteristics_count, 3)
        self.assertEqual(category.required_count, 2)
        self.assertEqual(category.characteristics_version, 1)
        self.assertEqual(len(category.characteristics_schema_hash), 64)

        second = MarketplaceService.sync_category_characteristics(
            category.id,
            client=self.client,
            now=now + timedelta(hours=1),
        )
        self.assertTrue(second['success'])
        self.assertEqual(second['updated'], 0)
        self.assertEqual(category.characteristics_version, 1)

    def test_characteristic_that_becomes_required_is_reenabled(self):
        category = self.add_category(
            104, 'Category', is_enabled=True, is_available=True,
        )
        characteristic = MarketplaceCategoryCharacteristic(
            category_id=category.id,
            marketplace_id=self.marketplace.id,
            charc_id=7,
            name='Optional before',
            charc_type=1,
            required=False,
            is_enabled=False,
            is_available=True,
        )
        db.session.add(characteristic)
        db.session.commit()
        self.client.characteristics[category.subject_id] = [
            charc(7, 'Required now', required=True),
        ]

        result = MarketplaceService.sync_category_characteristics(
            category.id, client=self.client,
        )
        db.session.refresh(characteristic)

        self.assertTrue(result['success'])
        self.assertTrue(characteristic.required)
        self.assertTrue(characteristic.is_enabled)

    def test_admin_cannot_disable_required_characteristic(self):
        category = self.add_category(
            112, 'Category', is_enabled=True, is_available=True,
        )
        characteristic = MarketplaceCategoryCharacteristic(
            category_id=category.id,
            marketplace_id=self.marketplace.id,
            charc_id=17,
            name='Required field',
            charc_type=1,
            required=True,
            is_enabled=True,
            is_available=True,
        )
        db.session.add(characteristic)
        db.session.commit()
        user = self.admin_user()

        with patch('routes.marketplaces.current_user', user), patch(
            'flask_login.utils._get_user', return_value=user,
        ):
            response = self.http.post(
                f'/admin/marketplaces/characteristics/{characteristic.id}/update',
                json={'is_enabled': False},
            )

        self.assertEqual(response.status_code, 409)
        self.assertTrue(response.get_json()['error'])
        self.assertTrue(characteristic.is_enabled)

    def test_characteristics_sync_preserves_manual_allowlist_when_wb_omits_dictionary(self):
        category = self.add_category(
            105, 'Category', is_enabled=True, is_available=True,
        )
        dictionary_json = '[{"value":"Хлопок"},{"value":"Шерсть"}]'
        material = MarketplaceCategoryCharacteristic(
            category_id=category.id,
            marketplace_id=self.marketplace.id,
            charc_id=10,
            name='Материал изделия',
            charc_type=1,
            required=False,
            max_count=1,
            dictionary_json=dictionary_json,
            ai_instruction=MarketplaceService.generate_ai_instruction(
                name='Материал изделия',
                charc_type=1,
                unit_name=None,
                max_count=1,
                required=False,
                dictionary_json=dictionary_json,
            ),
            ai_instruction_source='generated',
            is_available=True,
        )
        db.session.add(material)
        db.session.commit()
        initial_hash = MarketplaceService._category_characteristics_schema_hash(
            category.id,
        )
        category.characteristics_schema_hash = initial_hash
        initial_version = category.characteristics_version
        db.session.commit()
        upstream = charc(10, 'Материал изделия')
        upstream.pop('dictionary')
        self.client.characteristics[category.subject_id] = [upstream]

        result = MarketplaceService.sync_category_characteristics(
            category.id,
            client=self.client,
        )
        db.session.refresh(material)

        self.assertTrue(result['success'])
        self.assertEqual(result['updated'], 0)
        self.assertEqual(material.dictionary_json, dictionary_json)
        self.assertIn('Хлопок', material.ai_instruction)
        self.assertIn('Шерсть', material.ai_instruction)
        self.assertEqual(result['schema_hash'], initial_hash)
        self.assertEqual(category.characteristics_schema_hash, initial_hash)
        self.assertEqual(category.characteristics_version, initial_version)

    def test_save_characteristic_allowlist_normalizes_and_preserves_custom_instruction(self):
        category = self.add_category(
            106,
            'Category',
            is_enabled=True,
            is_available=True,
            characteristics_version=4,
        )
        material = MarketplaceCategoryCharacteristic(
            category_id=category.id,
            marketplace_id=self.marketplace.id,
            charc_id=11,
            name='Материал изделия',
            charc_type=1,
            max_count=1,
            ai_instruction='Проверенная ручная инструкция',
            ai_instruction_source='custom',
            is_available=True,
        )
        db.session.add(material)
        db.session.commit()
        initial_hash = MarketplaceService._category_characteristics_schema_hash(
            category.id,
        )
        category.characteristics_schema_hash = initial_hash
        db.session.commit()

        result = MarketplaceService.save_characteristic_allowlist(
            material.id,
            ['  Хлопок  ', 'хЛоПоК', '', 'Шерсть'],
        )
        db.session.refresh(material)
        db.session.refresh(category)

        self.assertTrue(result['success'])
        self.assertTrue(result['changed'])
        self.assertEqual(result['dictionary_values'], ['Хлопок', 'Шерсть'])
        self.assertEqual(
            json.loads(material.dictionary_json),
            [{'value': 'Хлопок'}, {'value': 'Шерсть'}],
        )
        self.assertEqual(material.ai_instruction, 'Проверенная ручная инструкция')
        self.assertEqual(material.ai_instruction_source, 'custom')
        self.assertEqual(category.characteristics_version, 5)
        self.assertNotEqual(category.characteristics_schema_hash, initial_hash)
        self.assertEqual(
            result['schema_hash'], category.characteristics_schema_hash,
        )
        saved_hash = category.characteristics_schema_hash

        stable = MarketplaceService.save_characteristic_allowlist(
            material.id,
            ['Хлопок', 'Шерсть'],
        )
        db.session.refresh(category)
        self.assertFalse(stable['changed'])
        self.assertEqual(category.characteristics_version, 5)
        self.assertEqual(category.characteristics_schema_hash, saved_hash)
        self.assertEqual(stable['schema_hash'], saved_hash)

    def test_save_characteristic_allowlist_rejects_unsafe_payload_without_mutation(self):
        category = self.add_category(
            107, 'Category', is_enabled=True, is_available=True,
        )
        material = MarketplaceCategoryCharacteristic(
            category_id=category.id,
            marketplace_id=self.marketplace.id,
            charc_id=12,
            name='Материал изделия',
            charc_type=1,
            dictionary_json='[{"value":"Хлопок"}]',
            is_available=True,
        )
        db.session.add(material)
        db.session.commit()

        with self.assertRaisesRegex(ValueError, r'values\[1\] должен быть строкой'):
            MarketplaceService.save_characteristic_allowlist(
                material.id,
                ['Шерсть', {'value': 'Силикон'}],
            )

        self.assertEqual(material.dictionary_json, '[{"value":"Хлопок"}]')

    def test_save_characteristic_allowlist_can_override_global_gender_values(self):
        category = self.add_category(
            108, 'Category', is_enabled=True, is_available=True,
        )
        gender = MarketplaceCategoryCharacteristic(
            category_id=category.id,
            marketplace_id=self.marketplace.id,
            charc_id=13,
            name='Пол товара',
            charc_type=1,
            max_count=1,
            is_available=True,
        )
        kinds = MarketplaceDirectory(
            marketplace_id=self.marketplace.id,
            directory_type='kinds',
            data_json=json.dumps(
                ['Женский', 'Мужской', 'Унисекс'], ensure_ascii=False,
            ),
            items_count=3,
            sync_status='success',
        )
        db.session.add_all([gender, kinds])
        db.session.commit()

        result = MarketplaceService.save_characteristic_allowlist(
            gender.id,
            ['Женский', 'Мужской'],
        )

        self.assertTrue(result['success'])
        self.assertEqual(result['dictionary_values'], ['Женский', 'Мужской'])
        self.assertEqual(
            json.loads(gender.dictionary_json),
            [{'value': 'Женский'}, {'value': 'Мужской'}],
        )
        self.assertIn('Унисекс', json.loads(kinds.data_json))
        self.assertNotIn('Унисекс', result['dictionary_values'])

    def test_admin_route_saves_characteristic_allowlist(self):
        category = self.add_category(
            109, 'Category', is_enabled=True, is_available=True,
        )
        material = MarketplaceCategoryCharacteristic(
            category_id=category.id,
            marketplace_id=self.marketplace.id,
            charc_id=14,
            name='Материал изделия',
            charc_type=1,
            max_count=1,
            is_available=True,
        )
        db.session.add(material)
        db.session.commit()
        user = self.admin_user()

        with patch('routes.marketplaces.current_user', user), patch(
            'flask_login.utils._get_user', return_value=user,
        ):
            response = self.http.post(
                f'/admin/marketplaces/characteristics/{material.id}/update',
                json={'dictionary_values': [' Хлопок ', 'Шерсть']},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json()['dictionary_values'], ['Хлопок', 'Шерсть'],
        )
        self.assertEqual(
            json.loads(material.dictionary_json),
            [{'value': 'Хлопок'}, {'value': 'Шерсть'}],
        )

    def test_admin_route_rejects_malformed_and_invalid_allowlist_payloads(self):
        category = self.add_category(
            110, 'Category', is_enabled=True, is_available=True,
        )
        material = MarketplaceCategoryCharacteristic(
            category_id=category.id,
            marketplace_id=self.marketplace.id,
            charc_id=15,
            name='Материал изделия',
            charc_type=1,
            is_available=True,
        )
        db.session.add(material)
        db.session.commit()
        user = self.admin_user()
        url = f'/admin/marketplaces/characteristics/{material.id}/update'

        with patch('routes.marketplaces.current_user', user), patch(
            'flask_login.utils._get_user', return_value=user,
        ):
            malformed = self.http.post(
                url,
                data='{',
                content_type='application/json',
            )
            invalid = self.http.post(
                url,
                json={'dictionary_values': 'Хлопок'},
            )

        self.assertEqual(malformed.status_code, 400)
        self.assertEqual(invalid.status_code, 400)
        self.assertFalse(malformed.get_json()['success'])
        self.assertIn('массивом строк', invalid.get_json()['error'])
        self.assertIsNone(material.dictionary_json)

    def test_non_admin_route_cannot_save_characteristic_allowlist(self):
        category = self.add_category(
            111, 'Category', is_enabled=True, is_available=True,
        )
        material = MarketplaceCategoryCharacteristic(
            category_id=category.id,
            marketplace_id=self.marketplace.id,
            charc_id=16,
            name='Материал изделия',
            charc_type=1,
            is_available=True,
        )
        db.session.add(material)
        db.session.commit()
        user = self.admin_user(is_admin=False)

        with patch('routes.marketplaces.current_user', user), patch(
            'flask_login.utils._get_user', return_value=user,
        ):
            response = self.http.post(
                f'/admin/marketplaces/characteristics/{material.id}/update',
                json={'dictionary_values': ['Хлопок']},
            )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers['Location'].endswith('/dashboard'))
        self.assertIsNone(material.dictionary_json)

    def test_empty_characteristics_response_fails_without_disabling_existing(self):
        category = self.add_category(
            101, 'Category', is_enabled=True, is_available=True,
        )
        existing = MarketplaceCategoryCharacteristic(
            category_id=category.id,
            marketplace_id=self.marketplace.id,
            charc_id=1,
            name='Color',
            charc_type=1,
            is_available=True,
        )
        db.session.add(existing)
        db.session.commit()

        result = MarketplaceService.sync_category_characteristics(
            category.id, client=self.client,
        )

        self.assertFalse(result['success'])
        self.assertEqual(category.characteristics_sync_status, 'failed')
        self.assertTrue(existing.is_available)

    def test_refresh_ahead_selects_oldest_schema_before_hard_ttl(self):
        now = datetime(2026, 7, 13, 12, 0, 0)
        recent = self.add_category(
            201,
            'Recent',
            is_enabled=True,
            is_available=True,
            characteristics_synced_at=now - timedelta(hours=29),
            characteristics_sync_status='success',
        )
        refresh_ahead = self.add_category(
            202,
            'Refresh ahead',
            is_enabled=True,
            is_available=True,
            characteristics_synced_at=now - timedelta(hours=31),
            characteristics_sync_status='success',
        )
        overdue = self.add_category(
            203,
            'Overdue',
            is_enabled=True,
            is_available=True,
            characteristics_synced_at=now - timedelta(hours=49),
            characteristics_sync_status='success',
        )
        for subject_id in (201, 202, 203):
            self.client.characteristics[subject_id] = [charc(1, 'Color')]

        first = MarketplaceService.sync_stale_characteristics(
            self.marketplace.id,
            limit=1,
            client=self.client,
            now=now,
            sleep_fn=lambda _seconds: None,
        )
        self.assertEqual(first['selected'], 1)
        self.assertEqual(self.client.characteristic_calls, [overdue.subject_id])

        second = MarketplaceService.sync_stale_characteristics(
            self.marketplace.id,
            limit=5,
            client=self.client,
            now=now,
            sleep_fn=lambda _seconds: None,
        )
        self.assertEqual(second['selected'], 1)
        self.assertEqual(self.client.characteristic_calls[-1], refresh_ahead.subject_id)
        self.assertEqual(recent.characteristics_synced_at, now - timedelta(hours=29))

    def test_stale_schema_batch_paces_each_request_after_the_first(self):
        now = datetime(2026, 7, 13, 12, 0, 0)
        for subject_id in (301, 302, 303):
            self.add_category(
                subject_id,
                f'Category {subject_id}',
                is_enabled=True,
                is_available=True,
                characteristics_synced_at=now - timedelta(hours=31),
                characteristics_sync_status='success',
            )
            self.client.characteristics[subject_id] = [charc(1, 'Color')]
        sleeps = []

        result = MarketplaceService.sync_stale_characteristics(
            self.marketplace.id,
            limit=3,
            client=self.client,
            now=now,
            sleep_fn=sleeps.append,
        )

        self.assertTrue(result['success'])
        self.assertEqual(result['synced'], 3)
        self.assertEqual(
            sleeps,
            [
                MarketplaceService.CHARACTERISTIC_REQUEST_INTERVAL_SECONDS,
                MarketplaceService.CHARACTERISTIC_REQUEST_INTERVAL_SECONDS,
            ],
        )

    def test_directory_sync_preserves_failed_cache_and_is_idempotent(self):
        old_sync = datetime(2026, 7, 10, 8, 0, 0)
        countries = MarketplaceDirectory(
            marketplace_id=self.marketplace.id,
            directory_type='countries',
            data_json='[{"name":"OLD"}]',
            data_hash='old-hash',
            version=3,
            synced_at=old_sync,
            sync_status='success',
            items_count=1,
        )
        db.session.add(countries)
        db.session.commit()
        self.client.directory_failures.add('countries')

        partial = MarketplaceService.sync_directories(
            self.marketplace.id,
            client=self.client,
            sleep_fn=lambda _seconds: None,
        )
        db.session.refresh(countries)

        self.assertTrue(partial['success'])
        self.assertIn('warning', partial)
        self.assertEqual(self.marketplace.directories_sync_status, 'partial')
        self.assertIsNone(self.marketplace.directories_synced_at)
        self.assertEqual(countries.data_json, '[{"name":"OLD"}]')
        self.assertEqual(countries.synced_at, old_sync)
        self.assertEqual(countries.sync_status, 'failed')
        self.assertNotIn('tnved', self.client.directory_calls)

        first_version = self.marketplace.directories_version
        self.client.directory_failures.clear()
        complete = MarketplaceService.sync_directories(
            self.marketplace.id,
            client=self.client,
            sleep_fn=lambda _seconds: None,
        )
        self.assertTrue(complete['success'])
        self.assertEqual(self.marketplace.directories_sync_status, 'success')
        self.assertIsNotNone(self.marketplace.directories_synced_at)

        stable_version = self.marketplace.directories_version
        MarketplaceService.sync_directories(
            self.marketplace.id,
            client=self.client,
            sleep_fn=lambda _seconds: None,
        )
        self.assertEqual(self.marketplace.directories_version, stable_version)
        self.assertGreater(stable_version, first_version)

    def test_brand_sync_updates_rename_by_stable_external_id(self):
        self.add_category(
            401, 'Brand category', is_enabled=True, is_available=True,
        )
        engine = BrandEngine(self.app)
        self.client.brand_result = {
            'data': [{'id': 9001, 'name': 'Old Brand'}],
            'complete': True,
            'errors': [],
        }

        first = engine.sync_marketplace_brands(self.marketplace.id, self.client)
        binding = MarketplaceBrand.query.one()
        self.assertEqual(first['mp_created'], 1)
        self.assertEqual(binding.marketplace_brand_name, 'Old Brand')
        self.assertTrue(binding.is_available)
        self.assertIsNotNone(binding.last_seen_at)
        self.assertEqual(self.marketplace.brands_sync_status, 'success')
        self.assertEqual(self.marketplace.brands_version, 1)

        self.client.brand_result = {
            'data': [{'id': 9001, 'name': 'Renamed Brand'}],
            'complete': True,
            'errors': [],
        }
        second = engine.sync_marketplace_brands(self.marketplace.id, self.client)

        self.assertEqual(second['mp_created'], 0)
        self.assertEqual(second['mp_updated'], 1)
        self.assertEqual(MarketplaceBrand.query.count(), 1)
        self.assertEqual(Brand.query.count(), 1)
        self.assertEqual(binding.marketplace_brand_name, 'Renamed Brand')
        self.assertEqual(self.marketplace.brands_version, 2)

        binding.is_available = False
        db.session.commit()
        engine.invalidate_cache()
        self.assertIsNone(
            engine.get_marketplace_brand(binding.brand_id, self.marketplace.id)
        )

    def test_empty_complete_brand_sweep_preserves_last_good_cache_and_freshness(self):
        self.add_category(
            402, 'Brand category', is_enabled=True, is_available=True,
        )
        old_sync = datetime(2026, 7, 10, 12, 0, 0)
        self.marketplace.brands_synced_at = old_sync
        self.marketplace.brands_sync_status = 'success'
        self.marketplace.brands_version = 7
        brand = Brand(
            name='Known Brand',
            name_normalized='known brand',
            status='verified',
        )
        db.session.add(brand)
        db.session.flush()
        binding = MarketplaceBrand(
            brand_id=brand.id,
            marketplace_id=self.marketplace.id,
            marketplace_brand_name='Known Brand',
            marketplace_brand_id=9002,
            status='verified',
            verified_at=old_sync,
            is_available=True,
            last_seen_at=old_sync,
        )
        db.session.add(binding)
        db.session.commit()

        engine = BrandEngine(self.app)
        self.client.brand_result = {'data': [], 'complete': True, 'errors': []}
        stats = engine.sync_marketplace_brands(self.marketplace.id, self.client)
        db.session.refresh(self.marketplace)
        db.session.refresh(binding)

        self.assertEqual(stats['total_fetched'], 0)
        self.assertEqual(self.marketplace.brands_sync_status, 'failed')
        self.assertIn('no usable data', self.marketplace.brands_sync_error)
        self.assertEqual(self.marketplace.brands_synced_at, old_sync)
        self.assertEqual(self.marketplace.brands_version, 7)
        self.assertEqual(engine.get_sync_progress(self.marketplace.id)['status'], 'error')
        self.assertTrue(binding.is_available)
        self.assertIsNotNone(
            engine.get_marketplace_brand(brand.id, self.marketplace.id)
        )

    def test_partial_brand_sweep_does_not_merge_unverified_snapshot(self):
        self.add_category(
            404, 'Brand category', is_enabled=True, is_available=True,
        )
        old_sync = datetime(2026, 7, 10, 12, 0, 0)
        self.marketplace.brands_synced_at = old_sync
        self.marketplace.brands_sync_status = 'success'
        self.marketplace.brands_version = 4
        brand = Brand(
            name='Last Good Brand',
            name_normalized='last good brand',
            status='verified',
        )
        db.session.add(brand)
        db.session.flush()
        binding = MarketplaceBrand(
            brand_id=brand.id,
            marketplace_id=self.marketplace.id,
            marketplace_brand_name='Last Good Brand',
            marketplace_brand_id=9301,
            status='verified',
            is_available=True,
        )
        db.session.add(binding)
        db.session.commit()
        self.client.brand_result = {
            'data': [{'id': 9302, 'name': 'Partial Brand'}],
            'complete': False,
            'errors': [{'code': 'request_budget_exhausted'}],
        }

        stats = BrandEngine(self.app).sync_marketplace_brands(
            self.marketplace.id, self.client,
        )
        db.session.refresh(self.marketplace)
        db.session.refresh(binding)

        self.assertEqual(stats['total_fetched'], 1)
        self.assertEqual(self.marketplace.brands_sync_status, 'partial')
        self.assertEqual(self.marketplace.brands_synced_at, old_sync)
        self.assertEqual(self.marketplace.brands_version, 4)
        self.assertTrue(binding.is_available)
        self.assertEqual(MarketplaceBrand.query.count(), 1)
        self.assertEqual(Brand.query.count(), 1)

    def test_brand_cache_only_exposes_verified_available_bindings(self):
        engine = BrandEngine(self.app)
        bindings = {}
        for index, status in enumerate(('verified', 'pending', 'rejected'), start=1):
            brand = Brand(
                name=f'{status.title()} Brand',
                name_normalized=f'{status} brand',
                status='verified',
            )
            db.session.add(brand)
            db.session.flush()
            binding = MarketplaceBrand(
                brand_id=brand.id,
                marketplace_id=self.marketplace.id,
                marketplace_brand_name=brand.name,
                marketplace_brand_id=9100 + index,
                status=status,
                is_available=True,
            )
            db.session.add(binding)
            bindings[status] = (brand, binding)
        db.session.commit()

        self.assertIsNotNone(engine.get_marketplace_brand(
            bindings['verified'][0].id, self.marketplace.id,
        ))
        self.assertIsNone(engine.get_marketplace_brand(
            bindings['pending'][0].id, self.marketplace.id,
        ))
        self.assertIsNone(engine.get_marketplace_brand(
            bindings['rejected'][0].id, self.marketplace.id,
        ))

    def test_brand_sync_prefetches_registry_without_per_brand_selects(self):
        self.add_category(
            403, 'Brand category', is_enabled=True, is_available=True,
        )
        self.client.brand_result = {
            'data': [
                {'id': 10000 + index, 'name': f'Batch Brand {index}'}
                for index in range(40)
            ],
            'complete': True,
            'errors': [],
        }
        registry_selects = []

        def count_registry_selects(_conn, _cursor, statement, *_args):
            normalized = statement.lower()
            if statement.lstrip().upper().startswith('SELECT') and any(
                marker in normalized
                for marker in (
                    ' from brands',
                    ' from marketplace_brands',
                    ' from brand_aliases',
                )
            ):
                registry_selects.append(statement)

        event.listen(db.engine, 'before_cursor_execute', count_registry_selects)
        try:
            stats = BrandEngine(self.app).sync_marketplace_brands(
                self.marketplace.id, self.client,
            )
        finally:
            event.remove(
                db.engine, 'before_cursor_execute', count_registry_selects,
            )

        self.assertEqual(stats['total_fetched'], 40)
        self.assertEqual(stats['mp_created'], 40)
        self.assertLessEqual(len(registry_selects), 3)


class MarketplaceReferenceMigrationTestCase(unittest.TestCase):
    def test_migration_is_idempotent_and_non_destructive(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / 'db.sqlite'
            connection = sqlite3.connect(path)
            connection.executescript(
                """
                CREATE TABLE marketplaces (
                    id INTEGER PRIMARY KEY,
                    categories_synced_at DATETIME,
                    categories_sync_status VARCHAR(50),
                    total_categories INTEGER,
                    directories_synced_at DATETIME
                );
                CREATE TABLE marketplace_categories (
                    id INTEGER PRIMARY KEY,
                    marketplace_id INTEGER,
                    is_enabled BOOLEAN,
                    characteristics_synced_at DATETIME,
                    created_at DATETIME,
                    updated_at DATETIME
                );
                CREATE TABLE marketplace_category_characteristics (
                    id INTEGER PRIMARY KEY,
                    category_id INTEGER,
                    ai_instruction TEXT,
                    created_at DATETIME,
                    updated_at DATETIME
                );
                CREATE TABLE marketplace_directories (
                    id INTEGER PRIMARY KEY,
                    directory_type VARCHAR(50),
                    synced_at DATETIME
                );
                CREATE TABLE marketplace_brands (
                    id INTEGER PRIMARY KEY,
                    marketplace_id INTEGER,
                    verified_at DATETIME,
                    created_at DATETIME,
                    updated_at DATETIME
                );
                INSERT INTO marketplace_categories
                    (id, marketplace_id, is_enabled, created_at)
                    VALUES (1, 1, 1, '2026-01-01');
                INSERT INTO marketplace_category_characteristics
                    (id, category_id, ai_instruction, created_at)
                    VALUES (1, 1, 'manual text', '2026-01-01');
                INSERT INTO marketplace_directories
                    (id, directory_type, synced_at)
                    VALUES (1, 'tnved', '2026-01-01');
                """
            )

            first = apply_migration(connection, verbose=False)
            second = apply_migration(connection, verbose=False)
            connection.commit()

            self.assertGreater(first, 0)
            self.assertEqual(second, 0)
            category = connection.execute(
                'SELECT is_enabled, is_available FROM marketplace_categories WHERE id=1'
            ).fetchone()
            instruction = connection.execute(
                'SELECT ai_instruction, ai_instruction_source '
                'FROM marketplace_category_characteristics WHERE id=1'
            ).fetchone()
            self.assertEqual(category, (1, 1))
            self.assertEqual(instruction, ('manual text', 'legacy'))
            tnved = connection.execute(
                'SELECT sync_status, sync_error FROM marketplace_directories WHERE id=1'
            ).fetchone()
            self.assertEqual(tnved, (
                'unsupported_global_scope',
                'WB TNVED requires a typed subjectID',
            ))
            connection.close()

    def test_migration_does_not_backfill_non_verified_brands_as_available(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / 'db.sqlite'
            connection = sqlite3.connect(path)
            connection.executescript(
                """
                CREATE TABLE marketplace_brands (
                    id INTEGER PRIMARY KEY,
                    marketplace_id INTEGER,
                    status VARCHAR(20),
                    verified_at DATETIME,
                    created_at DATETIME,
                    updated_at DATETIME
                );
                INSERT INTO marketplace_brands
                    (id, marketplace_id, status, verified_at, created_at)
                    VALUES (1, 1, 'verified', '2026-01-01', '2026-01-01');
                INSERT INTO marketplace_brands
                    (id, marketplace_id, status, created_at)
                    VALUES (2, 1, 'pending', '2026-01-01');
                INSERT INTO marketplace_brands
                    (id, marketplace_id, status, created_at)
                    VALUES (3, 1, 'rejected', '2026-01-01');
                """
            )

            apply_migration(connection, verbose=False)
            apply_migration(connection, verbose=False)
            rows = connection.execute(
                'SELECT status, is_available FROM marketplace_brands ORDER BY id'
            ).fetchall()

            self.assertEqual(rows, [
                ('verified', 1),
                ('pending', 0),
                ('rejected', 0),
            ])
            connection.close()


if __name__ == '__main__':
    unittest.main()
