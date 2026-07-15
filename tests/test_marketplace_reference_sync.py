# -*- coding: utf-8 -*-
"""Deterministic marketplace reference synchronization tests (no WB calls)."""

import json
import fcntl
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

from flask import Flask
from sqlalchemy import event

from migrations.migrate_add_marketplace_reference_freshness import apply_migration
from migrations.migrate_add_wb_dictionary_provenance import (
    apply_migration as apply_wb_dictionary_migration,
)
from models import (
    Brand,
    BrandAlias,
    db,
    Marketplace,
    MarketplaceCategory,
    MarketplaceCategoryCharacteristic,
    MarketplaceDirectory,
    MarketplaceBrand,
    BrandCategoryLink,
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
            'colors': [{'name': 'красный', 'parentName': 'красный'}],
            'countries': [{
                'id': 643,
                'name': 'Россия',
                'fullName': 'Российская Федерация',
            }],
            'kinds': ['Женский'],
            'seasons': ['круглогодичный'],
            'vat': ['20'],
        }
        self.directory_failures = set()
        self.directory_calls = []
        self.tnved_payloads = {}
        self.tnved_calls = []
        self.brand_result = {'data': [], 'complete': True, 'errors': []}
        self.brand_subject_calls = []

    def get_subjects_list(self, limit, offset):
        self.category_calls.append((limit, offset))
        value = self.category_pages.get(offset, [])
        if isinstance(value, Exception):
            raise value
        return {
            'data': value,
            'error': False,
            'errorText': '',
            'additionalErrors': None,
        }

    def get_card_characteristics_config(self, subject_id):
        self.characteristic_calls.append(subject_id)
        value = self.characteristics.get(subject_id, [])
        if isinstance(value, Exception):
            raise value
        return {
            'data': value,
            'error': False,
            'errorText': '',
            'additionalErrors': None,
        }

    def _directory(self, name):
        self.directory_calls.append(name)
        if name in self.directory_failures:
            raise RuntimeError(f'{name} unavailable')
        return {
            'data': self.directory_payloads[name],
            'error': False,
            'errorText': '',
            'additionalErrors': None,
        }

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

    def get_directory_tnved(self, subject_id):
        self.tnved_calls.append(subject_id)
        value = self.tnved_payloads.get(subject_id, [])
        if isinstance(value, Exception):
            raise value
        return {
            'data': value,
            'error': False,
            'errorText': '',
            'additionalErrors': None,
        }

    def fetch_all_brands(self, subject_ids, top=5000, progress_callback=None):
        self.brand_subject_calls.append(list(subject_ids))
        result = dict(self.brand_result)
        if 'subject_brands' not in result and result.get('complete') is True:
            if len(subject_ids) == 1:
                subject_id = int(subject_ids[0])
                result['subject_brands'] = {
                    subject_id: list(result.get('data') or []),
                }
                result['completed_subject_ids'] = [subject_id]
            else:
                result['complete'] = False
                result['subject_brands'] = {}
                result['completed_subject_ids'] = []
                result['errors'] = [{
                    'code': 'fake_requires_typed_subject_snapshots',
                }]
        if progress_callback:
            progress_callback(
                len(result.get('completed_subject_ids') or []),
                len(subject_ids),
                len(result.get('data') or []),
            )
        return result


def charc(
    charc_id,
    name,
    *,
    subject_id,
    subject_name='Category',
    charc_type=1,
    required=False,
    dictionary=None,
    max_count=1,
):
    return {
        'charcID': charc_id,
        'subjectID': subject_id,
        'subjectName': subject_name,
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
        self.brand_lock_dir = tempfile.TemporaryDirectory()
        self.brand_lock_env = patch.dict(os.environ, {
            'BRAND_SYNC_LOCK_FILE': str(
                Path(self.brand_lock_dir.name) / 'brand-sync.lock'
            ),
        })
        self.brand_lock_env.start()
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
        self.brand_lock_env.stop()
        self.brand_lock_dir.cleanup()

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

    def test_category_sync_rejects_coerced_ids_and_untyped_flags(self):
        existing = self.add_category(
            41, 'Last good', is_enabled=True, is_available=True,
        )
        invalid_items = [
            {'subjectID': True, 'subjectName': 'Bool ID'},
            {'subjectID': 41.0, 'subjectName': 'Float ID'},
            {'subjectID': '41', 'subjectName': 'String ID'},
            {'subjectID': 41, 'subjectName': 'Bad parent', 'parentID': 1.0},
            {'subjectID': 41, 'subjectName': 'Bad flag', 'isVisible': 'false'},
            {'subjectID': 41, 'subjectName': 'Bad flag', 'isEnabled': 1},
            {'subjectID': 41, 'subjectName': 'Bad flag', 'disabled': 0},
        ]

        for item in invalid_items:
            with self.subTest(item=item):
                self.client.category_pages[0] = [item]
                result = MarketplaceService.sync_categories(
                    self.marketplace.id, client=self.client,
                )
                db.session.refresh(existing)

                self.assertFalse(result['success'])
                self.assertEqual(existing.subject_name, 'Last good')
                self.assertTrue(existing.is_available)

    def test_category_sync_rejects_top_level_wb_error_without_mutating_cache(self):
        existing = self.add_category(
            41, 'Last good', is_enabled=True, is_available=True,
        )
        response = {
            'data': [{'subjectID': 41, 'subjectName': 'Corrupted rename'}],
            'error': True,
            'errorText': 'upstream failure',
        }

        with patch.object(
            self.client, 'get_subjects_list', return_value=response,
        ):
            result = MarketplaceService.sync_categories(
                self.marketplace.id, client=self.client,
            )
        db.session.refresh(existing)

        self.assertFalse(result['success'])
        self.assertIn('reports an error', result['error'])
        self.assertEqual(existing.subject_name, 'Last good')
        self.assertTrue(existing.is_available)

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
                subject_id=category.subject_id,
                dictionary=[{'value': 'Black'}, {'value': 'White'}],
            ),
            charc(
                3, 'Weight', subject_id=category.subject_id,
                charc_type=4, required=True, dictionary=None,
            ),
            charc(
                4, 'Material', subject_id=category.subject_id,
                dictionary=[{'value': 'Cotton'}],
            ),
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
            charc(
                7, 'Required now', subject_id=category.subject_id, required=True,
            ),
        ]

        result = MarketplaceService.sync_category_characteristics(
            category.id, client=self.client,
        )
        db.session.refresh(characteristic)

        self.assertTrue(result['success'])
        self.assertTrue(characteristic.required)
        self.assertTrue(characteristic.is_enabled)

    def test_characteristics_reject_wrong_subject_and_untyped_fields(self):
        category = self.add_category(
            106, 'Typed category', is_enabled=True, is_available=True,
        )
        existing = MarketplaceCategoryCharacteristic(
            category_id=category.id,
            marketplace_id=self.marketplace.id,
            charc_id=1,
            name='Last good',
            charc_type=1,
            is_available=True,
        )
        db.session.add(existing)
        db.session.commit()

        wrong_subject = charc(2, 'Wrong scope', subject_id=999)
        self.client.characteristics[category.subject_id] = [wrong_subject]
        mismatch = MarketplaceService.sync_category_characteristics(
            category.id, client=self.client,
        )
        db.session.refresh(existing)

        self.assertFalse(mismatch['success'])
        self.assertIn('does not match', mismatch['error'])
        self.assertTrue(existing.is_available)
        self.assertIsNone(
            MarketplaceCategoryCharacteristic.query.filter_by(charc_id=2).first()
        )

        untyped = charc(2, 'Untyped', subject_id=category.subject_id)
        untyped['required'] = 'false'
        self.client.characteristics[category.subject_id] = [untyped]
        typed = MarketplaceService.sync_category_characteristics(
            category.id, client=self.client,
        )

        self.assertFalse(typed['success'])
        self.assertIn('required is not a boolean', typed['error'])
        self.assertTrue(existing.is_available)

    def test_characteristics_reject_coerced_integer_and_boolean_fields(self):
        category = self.add_category(
            108, 'Strict schema', is_enabled=True, is_available=True,
        )
        existing = MarketplaceCategoryCharacteristic(
            category_id=category.id,
            marketplace_id=self.marketplace.id,
            charc_id=1,
            name='Last good',
            charc_type=1,
            is_available=True,
        )
        db.session.add(existing)
        db.session.commit()

        mutations = [
            ('charcID', True),
            ('charcID', 2.0),
            ('charcID', '2'),
            ('subjectID', 108.0),
            ('subjectID', '108'),
            ('charcType', True),
            ('charcType', 1.0),
            ('charcType', '1'),
            ('maxCount', False),
            ('maxCount', 1.0),
            ('maxCount', '1'),
            ('required', 1),
            ('popular', 'false'),
            ('hasFilter', 0),
            ('isVariable', None),
            ('existNamedField', 'true'),
        ]

        for field_name, value in mutations:
            with self.subTest(field=field_name, value=value):
                payload = charc(
                    2, 'Rejected', subject_id=category.subject_id,
                )
                payload[field_name] = value
                self.client.characteristics[category.subject_id] = [payload]
                result = MarketplaceService.sync_category_characteristics(
                    category.id, client=self.client,
                )
                db.session.refresh(existing)

                self.assertFalse(result['success'])
                self.assertTrue(existing.is_available)
                self.assertIsNone(
                    MarketplaceCategoryCharacteristic.query.filter_by(
                        category_id=category.id, charc_id=2,
                    ).first()
                )

    def test_characteristics_accept_missing_optional_boolean_fields(self):
        category = self.add_category(
            109, 'Official minimal schema', is_enabled=True, is_available=True,
        )
        payload = charc(
            2, 'Размер', subject_id=category.subject_id,
        )
        payload.pop('hasFilter')
        payload.pop('isVariable')
        self.client.characteristics[category.subject_id] = [payload]

        result = MarketplaceService.sync_category_characteristics(
            category.id, client=self.client,
        )

        self.assertTrue(result['success'])
        characteristic = MarketplaceCategoryCharacteristic.query.filter_by(
            category_id=category.id, charc_id=2,
        ).one()
        self.assertFalse(characteristic.has_filter)
        self.assertFalse(characteristic.is_variable)

    def test_characteristics_reject_top_level_error_and_anomalous_shrink(self):
        old_sync = datetime(2026, 7, 10, 8, 0, 0)
        category = self.add_category(
            107,
            'Stable schema',
            is_enabled=True,
            is_available=True,
            characteristics_synced_at=old_sync,
            characteristics_sync_status='success',
            characteristics_count=8,
            characteristics_version=3,
        )
        existing = []
        for charc_id in range(1, 9):
            characteristic = MarketplaceCategoryCharacteristic(
                category_id=category.id,
                marketplace_id=self.marketplace.id,
                charc_id=charc_id,
                name=f'Field {charc_id}',
                charc_type=1,
                is_available=True,
            )
            existing.append(characteristic)
        db.session.add_all(existing)
        db.session.commit()

        with patch.object(
            self.client,
            'get_card_characteristics_config',
            return_value={
                'data': [charc(1, 'Field 1', subject_id=category.subject_id)],
                'error': True,
            },
        ):
            upstream_error = MarketplaceService.sync_category_characteristics(
                category.id, client=self.client,
            )
        self.assertFalse(upstream_error['success'])
        self.assertTrue(all(item.is_available for item in existing))

        self.client.characteristics[category.subject_id] = [
            charc(1, 'Field 1', subject_id=category.subject_id),
        ]
        shrink = MarketplaceService.sync_category_characteristics(
            category.id, client=self.client,
        )
        db.session.refresh(category)
        for item in existing:
            db.session.refresh(item)

        self.assertFalse(shrink['success'])
        self.assertIn('shrank anomalously', shrink['error'])
        self.assertEqual(category.characteristics_synced_at, old_sync)
        self.assertEqual(category.characteristics_version, 3)
        self.assertTrue(all(item.is_available for item in existing))

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
            dictionary_source='admin',
            dictionary_hash=MarketplaceService._dictionary_hash(dictionary_json),
            dictionary_version=1,
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
        upstream = charc(
            10, 'Материал изделия', subject_id=category.subject_id,
        )
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

        upstream['dictionary'] = []
        self.client.characteristics[category.subject_id] = [upstream]
        explicit_empty = MarketplaceService.sync_category_characteristics(
            category.id,
            client=self.client,
        )
        db.session.refresh(material)

        self.assertTrue(explicit_empty['success'])
        self.assertEqual(explicit_empty['updated'], 0)
        self.assertEqual(material.dictionary_json, dictionary_json)
        self.assertEqual(explicit_empty['schema_hash'], initial_hash)

    def test_tnved_dictionary_is_category_scoped_versioned_and_fail_closed(self):
        category = self.add_category(
            111, 'Category', is_enabled=True, is_available=True,
        )
        self.client.characteristics[category.subject_id] = [
            charc(71, 'ТНВЭД', subject_id=category.subject_id),
        ]
        self.client.tnved_payloads[category.subject_id] = [
            {'tnved': '1234567890', 'isKiz': False},
            {'tnved': '9876543210', 'isKiz': True},
        ]
        now = datetime(2026, 7, 15, 8, 0, 0)

        first = MarketplaceService.sync_category_characteristics(
            category.id, client=self.client, now=now, sleep_fn=lambda _seconds: None,
        )
        characteristic = MarketplaceCategoryCharacteristic.query.filter_by(
            category_id=category.id, charc_id=71,
        ).one()

        self.assertTrue(first['success'])
        self.assertEqual(self.client.tnved_calls, [category.subject_id])
        self.assertEqual(characteristic.dictionary_source, 'wb_directory')
        self.assertEqual(characteristic.dictionary_version, 1)
        self.assertEqual(json.loads(characteristic.dictionary_json), [
            {'isKiz': False, 'value': '1234567890'},
            {'isKiz': True, 'value': '9876543210'},
        ])
        saved_json = characteristic.dictionary_json
        saved_hash = characteristic.dictionary_hash

        second = MarketplaceService.sync_category_characteristics(
            category.id, client=self.client, now=now + timedelta(hours=1),
            sleep_fn=lambda _seconds: None,
        )
        db.session.refresh(characteristic)
        self.assertTrue(second['success'])
        self.assertEqual(second['updated'], 0)
        self.assertEqual(characteristic.dictionary_version, 1)

        self.client.tnved_payloads[category.subject_id] = RuntimeError('tnved down')
        failed = MarketplaceService.sync_category_characteristics(
            category.id, client=self.client, now=now + timedelta(hours=2),
            sleep_fn=lambda _seconds: None,
        )
        db.session.refresh(characteristic)
        self.assertFalse(failed['success'])
        self.assertEqual(characteristic.dictionary_json, saved_json)
        self.assertEqual(characteristic.dictionary_hash, saved_hash)
        self.assertEqual(characteristic.dictionary_version, 1)

    def test_batch_reference_preflight_refreshes_each_scope_once(self):
        now = datetime(2026, 7, 15, 9, 0, 0)
        self.add_category(
            112, 'Old category name', is_enabled=True, is_available=True,
        )
        self.client.category_pages[0] = [{
            'subjectID': 112,
            'subjectName': 'Leaf category',
            'parentID': 1,
            'parentName': 'Parent',
        }]
        self.client.characteristics[112] = [
            charc(80, 'Материал изделия', subject_id=112),
        ]

        first = MarketplaceService.ensure_wb_references_current(
            [112, 112], client=self.client, now=now,
            sleep_fn=lambda _seconds: None,
        )
        self.assertTrue(first['success'], first)
        self.assertEqual(first['subjects'], [112])
        self.assertEqual(first['refreshed']['schemas'], [112])
        self.assertEqual(self.client.characteristic_calls, [112])
        self.assertEqual(
            set(self.client.directory_calls),
            {'colors', 'countries', 'kinds', 'seasons', 'vat'},
        )

        call_counts = (
            len(self.client.category_calls),
            len(self.client.directory_calls),
            len(self.client.characteristic_calls),
        )
        second = MarketplaceService.ensure_wb_references_current(
            [112], client=self.client, now=now + timedelta(hours=1),
            sleep_fn=lambda _seconds: None,
        )
        self.assertTrue(second['success'], second)
        self.assertEqual(call_counts, (
            len(self.client.category_calls),
            len(self.client.directory_calls),
            len(self.client.characteristic_calls),
        ))

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

    def test_save_characteristic_allowlist_cannot_override_global_gender_values(self):
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

        with self.assertRaisesRegex(ValueError, 'Официальный словарь WB'):
            MarketplaceService.save_characteristic_allowlist(
                gender.id,
                ['Женский', 'Мужской'],
            )

        self.assertIsNone(gender.dictionary_json)
        self.assertIn('Унисекс', json.loads(kinds.data_json))

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
            self.client.characteristics[subject_id] = [
                charc(1, 'Color', subject_id=subject_id),
            ]

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
            self.client.characteristics[subject_id] = [
                charc(1, 'Color', subject_id=subject_id),
            ]
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

    def test_stale_schema_batch_does_not_retry_failed_null_before_other_work(self):
        now = datetime(2026, 7, 13, 12, 0, 0)
        failed = self.add_category(
            311,
            'Failed before',
            is_enabled=True,
            is_available=True,
            characteristics_sync_status='failed',
        )
        untouched = self.add_category(
            312,
            'Untouched',
            is_enabled=True,
            is_available=True,
        )
        stale_success = self.add_category(
            313,
            'Stale success',
            is_enabled=True,
            is_available=True,
            characteristics_synced_at=now - timedelta(hours=31),
            characteristics_sync_status='success',
        )
        self.client.characteristics[untouched.subject_id] = [
            charc(1, 'Color', subject_id=untouched.subject_id),
        ]
        self.client.characteristics[stale_success.subject_id] = [
            charc(1, 'Color', subject_id=stale_success.subject_id),
        ]

        result = MarketplaceService.sync_stale_characteristics(
            self.marketplace.id,
            limit=2,
            client=self.client,
            now=now,
            sleep_fn=lambda _seconds: None,
        )

        self.assertTrue(result['success'])
        self.assertEqual(result['synced'], 2)
        self.assertEqual(
            self.client.characteristic_calls,
            [stale_success.subject_id, untouched.subject_id],
        )
        self.assertNotIn(failed.subject_id, self.client.characteristic_calls)

    def test_reference_sync_claim_skips_overlapping_refresh_without_api_calls(self):
        category = self.add_category(
            314, 'Claimed', is_enabled=True, is_available=True,
        )
        operations = {
            'categories': lambda: MarketplaceService.sync_categories(
                self.marketplace.id, client=self.client,
            ),
            'directory': lambda: MarketplaceService.sync_directories(
                self.marketplace.id, client=self.client,
                sleep_fn=lambda _seconds: None,
            ),
            'category schema': lambda: MarketplaceService.sync_category_characteristics(
                category.id, client=self.client,
            ),
            'stale sweep': lambda: MarketplaceService.sync_stale_characteristics(
                self.marketplace.id,
                client=self.client,
                sleep_fn=lambda _seconds: None,
            ),
        }

        with patch(
            'services.marketplace_service._try_reference_sync_claim',
            return_value=None,
        ):
            for name, operation in operations.items():
                with self.subTest(name=name):
                    result = operation()
                    self.assertFalse(result['success'])
                    self.assertTrue(result['skipped'])

        self.assertEqual(self.client.category_calls, [])
        self.assertEqual(self.client.directory_calls, [])
        self.assertEqual(self.client.characteristic_calls, [])
        self.assertIsNone(self.marketplace.categories_sync_status)
        self.assertIsNone(self.marketplace.directories_sync_status)
        self.assertIsNone(category.characteristics_sync_status)

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

    def test_directory_payloads_require_official_shapes_and_unique_items(self):
        invalid_payloads = {
            'colors': [{'name': 'красный', 'parentName': 7}],
            'countries': [{
                'id': '643',
                'name': 'Россия',
                'fullName': 'Российская Федерация',
            }],
            'kinds': [{'name': 'Женский'}],
            'seasons': [1],
            'vat': [20],
        }
        for directory_type, payload in invalid_payloads.items():
            with self.subTest(directory_type=directory_type):
                with self.assertRaises(ValueError):
                    MarketplaceService._normalize_directory_snapshot(
                        directory_type, payload,
                    )

        normalized_colors = MarketplaceService._normalize_directory_snapshot(
            'colors',
            [
                {'name': 'Белый', 'parentName': None},
                {'name': 'Черный'},
            ],
        )
        self.assertEqual(
            [item['parentName'] for item in normalized_colors],
            ['', ''],
        )

        with self.assertRaisesRegex(ValueError, 'duplicate'):
            MarketplaceService._normalize_directory_snapshot(
                'colors',
                [
                    {'name': 'Красный', 'parentName': 'Красный'},
                    {'name': 'красный', 'parentName': 'Другой'},
                ],
            )
        with self.assertRaisesRegex(ValueError, 'duplicated name'):
            MarketplaceService._normalize_directory_snapshot(
                'countries',
                [
                    {'id': 1, 'name': 'Россия', 'fullName': 'Россия'},
                    {'id': 2, 'name': 'россия', 'fullName': 'РФ'},
                ],
            )

    def test_directory_error_and_anomalous_shrink_preserve_last_good(self):
        old_sync = datetime(2026, 7, 10, 8, 0, 0)
        old_items = [
            {
                'id': item_id,
                'name': f'Country {item_id}',
                'fullName': f'Country {item_id} full',
            }
            for item_id in range(1, 9)
        ]
        countries = MarketplaceDirectory(
            marketplace_id=self.marketplace.id,
            directory_type='countries',
            data_json=json.dumps(old_items, ensure_ascii=False),
            data_hash='last-good-hash',
            version=4,
            synced_at=old_sync,
            sync_status='success',
            items_count=len(old_items),
        )
        db.session.add(countries)
        db.session.commit()
        original_json = countries.data_json

        original_fetcher = self.client.get_directory_countries
        self.client.get_directory_countries = lambda: {
            'data': [{
                'id': 1,
                'name': 'Corrupted',
                'fullName': 'Corrupted',
            }],
            'error': True,
        }
        upstream_error = MarketplaceService.sync_directories(
            self.marketplace.id,
            client=self.client,
            sleep_fn=lambda _seconds: None,
        )
        db.session.refresh(countries)

        self.assertTrue(upstream_error['success'])
        self.assertEqual(countries.data_json, original_json)
        self.assertEqual(countries.data_hash, 'last-good-hash')
        self.assertEqual(countries.synced_at, old_sync)
        self.assertEqual(countries.version, 4)

        self.client.get_directory_countries = original_fetcher
        self.client.directory_payloads['countries'] = [{
            'id': 1,
            'name': 'Only one',
            'fullName': 'Only one country',
        }]
        shrink = MarketplaceService.sync_directories(
            self.marketplace.id,
            client=self.client,
            sleep_fn=lambda _seconds: None,
        )
        db.session.refresh(countries)

        self.assertTrue(shrink['success'])
        self.assertIn('warning', shrink)
        self.assertIn('shrank anomalously', countries.sync_error)
        self.assertEqual(countries.data_json, original_json)
        self.assertEqual(countries.data_hash, 'last-good-hash')
        self.assertEqual(countries.synced_at, old_sync)
        self.assertEqual(countries.version, 4)

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
        renamed_alias = BrandAlias.query.filter_by(
            alias_normalized='renamed brand',
        ).one()
        self.assertEqual(renamed_alias.brand_id, binding.brand_id)
        self.assertEqual(renamed_alias.source, 'marketplace_sync')
        self.assertTrue(renamed_alias.is_active)

        engine.invalidate_cache()
        resolution = engine.resolve(
            'Renamed Brand',
            marketplace_id=self.marketplace.id,
            category_id=401,
        )
        self.assertEqual(resolution.status, 'exact')
        self.assertEqual(resolution.brand_id, binding.brand_id)
        self.assertEqual(
            resolution.marketplace_brand_name, 'Renamed Brand',
        )

    def test_brand_sync_rename_does_not_take_over_manual_alias(self):
        self.add_category(
            402, 'Brand category', is_enabled=True, is_available=True,
        )
        engine = BrandEngine(self.app)
        self.client.brand_result = {
            'data': [{'id': 9002, 'name': 'Original Brand'}],
            'complete': True,
            'errors': [],
        }
        engine.sync_marketplace_brands(self.marketplace.id, self.client)
        binding = MarketplaceBrand.query.one()

        manual_brand = Brand(
            name='Manual Brand',
            name_normalized='manual brand',
            status='verified',
        )
        db.session.add(manual_brand)
        db.session.flush()
        manual_alias = BrandAlias(
            brand_id=manual_brand.id,
            alias='Renamed Brand',
            alias_normalized='renamed brand',
            source='manual',
            confidence=1.0,
        )
        db.session.add(manual_alias)
        db.session.commit()

        self.client.brand_result = {
            'data': [{'id': 9002, 'name': 'Renamed Brand'}],
            'complete': True,
            'errors': [],
        }
        result = engine.sync_marketplace_brands(
            self.marketplace.id, self.client,
        )
        db.session.refresh(binding)
        db.session.refresh(manual_alias)

        self.assertGreater(result['errors'], 0)
        self.assertEqual(binding.marketplace_brand_name, 'Original Brand')
        self.assertEqual(manual_alias.brand_id, manual_brand.id)
        self.assertEqual(manual_alias.source, 'manual')
        self.assertTrue(manual_alias.is_active)

        binding.is_available = False
        db.session.commit()
        engine.invalidate_cache()
        self.assertIsNone(
            engine.get_marketplace_brand(binding.brand_id, self.marketplace.id)
        )

    def test_brand_sync_persists_and_invalidates_category_membership(self):
        self.add_category(
            405, 'Scoped brands', is_enabled=True, is_available=True,
        )
        engine = BrandEngine(self.app)
        self.client.brand_result = {
            'data': [
                {'id': 9401, 'name': 'Present Brand'},
                {'id': 9402, 'name': 'Removed Brand'},
            ],
            'complete': True,
            'errors': [],
        }
        engine.sync_marketplace_brands(self.marketplace.id, self.client)

        bindings = {
            item.marketplace_brand_id: item
            for item in MarketplaceBrand.query.all()
        }
        self.assertTrue(BrandCategoryLink.query.filter_by(
            marketplace_brand_id=bindings[9401].id,
            category_id=405,
        ).one().is_available)
        self.assertEqual(BrandCategoryLink.query.filter_by(
            marketplace_brand_id=bindings[9401].id,
            category_id=405,
        ).one().category_name, 'Scoped brands')
        removed_link = BrandCategoryLink.query.filter_by(
            marketplace_brand_id=bindings[9402].id,
            category_id=405,
        ).one()
        self.assertTrue(removed_link.is_available)

        other_marketplace = Marketplace(name='Ozon', code='ozon')
        other_brand = Brand(
            name='Other Marketplace Brand',
            name_normalized='other marketplace brand',
            status='verified',
        )
        db.session.add_all([other_marketplace, other_brand])
        db.session.flush()
        other_binding = MarketplaceBrand(
            brand_id=other_brand.id,
            marketplace_id=other_marketplace.id,
            marketplace_brand_name=other_brand.name,
            marketplace_brand_id=9402,
            status='verified',
            is_available=True,
        )
        db.session.add(other_binding)
        db.session.flush()
        other_link = BrandCategoryLink(
            marketplace_brand_id=other_binding.id,
            category_id=405,
            is_available=True,
        )
        db.session.add(other_link)
        db.session.commit()

        self.client.brand_result = {
            'data': [{'id': 9401, 'name': 'Present Brand'}],
            'complete': True,
            'errors': [],
        }
        stats = engine.sync_marketplace_brands(self.marketplace.id, self.client)
        db.session.refresh(removed_link)
        db.session.refresh(bindings[9402])
        db.session.refresh(other_link)

        self.assertEqual(stats['category_links_removed'], 1)
        self.assertFalse(removed_link.is_available)
        self.assertFalse(bindings[9402].is_available)
        self.assertTrue(other_link.is_available)

    def test_brand_sync_resumes_from_durable_category_checkpoint(self):
        self.add_category(501, 'First scope', is_enabled=True, is_available=True)
        self.add_category(502, 'Second scope', is_enabled=True, is_available=True)
        engine = BrandEngine(self.app)
        self.client.brand_result = {
            'data': [{'id': 9501, 'name': 'First Brand'}],
            'subject_brands': {
                501: [{'id': 9501, 'name': 'First Brand'}],
            },
            'completed_subject_ids': [501],
            'complete': False,
            'errors': [{'code': 'request_budget_exhausted'}],
        }
        first = engine.sync_marketplace_brands(self.marketplace.id, self.client)
        db.session.refresh(self.marketplace)

        self.assertEqual(first['categories_completed_this_run'], 1)
        self.assertEqual(self.marketplace.brands_sync_status, 'partial')
        self.assertIsNone(self.marketplace.brands_synced_at)
        checkpoint = json.loads(self.marketplace.brands_sync_checkpoint)
        self.assertEqual(checkpoint['next_index'], 1)
        self.assertEqual(self.client.brand_subject_calls[-1], [501, 502])

        self.client.brand_result = {
            'data': [{'id': 9502, 'name': 'Second Brand'}],
            'subject_brands': {
                502: [{'id': 9502, 'name': 'Second Brand'}],
            },
            'completed_subject_ids': [502],
            'complete': True,
            'errors': [],
        }
        second = engine.sync_marketplace_brands(self.marketplace.id, self.client)
        db.session.refresh(self.marketplace)

        self.assertEqual(second['categories_completed_this_run'], 1)
        self.assertEqual(self.client.brand_subject_calls[-1], [502])
        self.assertEqual(self.marketplace.brands_sync_status, 'success')
        self.assertIsNotNone(self.marketplace.brands_synced_at)
        self.assertIsNone(self.marketplace.brands_sync_checkpoint)
        self.assertEqual(BrandCategoryLink.query.filter_by(
            is_available=True,
        ).count(), 2)

    def test_category_validation_is_fail_closed_without_live_evidence(self):
        brand = Brand(
            name='Manual Review Brand',
            name_normalized='manual review brand',
            status='verified',
        )
        db.session.add(brand)
        db.session.flush()
        binding = MarketplaceBrand(
            brand_id=brand.id,
            marketplace_id=self.marketplace.id,
            marketplace_brand_name=brand.name,
            marketplace_brand_id=9601,
            status='verified',
            is_available=True,
        )
        db.session.add(binding)
        db.session.commit()
        engine = BrandEngine(self.app)

        self.assertIsNone(engine.validate_brand_for_category(
            binding.id, 601, marketplace_client=None,
        ))
        failing_client = MagicMock()
        failing_client.search_brands.side_effect = RuntimeError('WB unavailable')
        self.assertIsNone(engine.validate_brand_for_category(
            binding.id, 601, marketplace_client=failing_client,
        ))
        self.assertIsNone(BrandCategoryLink.query.filter_by(
            marketplace_brand_id=binding.id,
            category_id=601,
        ).first())

    def test_unexpected_brand_sync_error_never_leaves_running_status(self):
        self.add_category(701, 'Failure scope', is_enabled=True, is_available=True)
        self.client.brand_result = {
            'data': [{'id': 9701, 'name': 'Failure Brand'}],
            'complete': True,
            'errors': [],
        }
        engine = BrandEngine(self.app)
        with patch(
            'services.brand_engine.normalize_for_comparison',
            side_effect=RuntimeError('sensitive upstream detail'),
        ):
            result = engine.sync_marketplace_brands(
                self.marketplace.id, self.client,
            )
        db.session.refresh(self.marketplace)

        self.assertEqual(result['errors'], 1)
        self.assertEqual(self.marketplace.brands_sync_status, 'failed')
        self.assertNotEqual(self.marketplace.brands_sync_status, 'running')
        self.assertNotIn('sensitive upstream detail', self.marketplace.brands_sync_error)
        self.assertEqual(engine.get_sync_progress(self.marketplace.id)['status'], 'error')

    def test_brand_sync_honors_cross_process_advisory_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            lock_path = str(Path(directory) / 'brand-sync.lock')
            lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                with patch.dict(os.environ, {
                    'BRAND_SYNC_LOCK_FILE': lock_path,
                }):
                    result = BrandEngine(self.app).sync_marketplace_brands(
                        self.marketplace.id, self.client,
                    )
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)

            self.assertTrue(result['skipped'])
            self.assertEqual(result['reason'], 'brand_sync_already_running')
            self.assertEqual(self.client.brand_subject_calls, [])

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

        self.assertEqual(stats['total_fetched'], 0)
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
    def test_wb_dictionary_provenance_migration_is_idempotent(self):
        connection = sqlite3.connect(':memory:')
        connection.executescript(
            """
            CREATE TABLE marketplace_category_characteristics (
                id INTEGER PRIMARY KEY,
                category_id INTEGER NOT NULL,
                dictionary_json TEXT,
                created_at DATETIME,
                updated_at DATETIME
            );
            INSERT INTO marketplace_category_characteristics
                (id, category_id, dictionary_json, created_at, updated_at)
                VALUES (
                    1, 10, '[{"value":"Хлопок"}]',
                    '2026-01-01', '2026-02-01'
                );
            INSERT INTO marketplace_category_characteristics
                (id, category_id, dictionary_json, created_at, updated_at)
                VALUES (2, 10, '[]', '2026-01-01', '2026-02-01');
            """
        )

        first = apply_wb_dictionary_migration(connection, verbose=False)
        second = apply_wb_dictionary_migration(connection, verbose=False)
        rows = connection.execute(
            'SELECT id, dictionary_source, dictionary_version, '
            'dictionary_hash, dictionary_synced_at '
            'FROM marketplace_category_characteristics ORDER BY id'
        ).fetchall()
        connection.close()

        self.assertGreater(first, 0)
        self.assertEqual(second, 0)
        self.assertEqual(rows[0][1], 'admin')
        self.assertEqual(rows[0][2], 1)
        self.assertEqual(
            rows[0][3],
            MarketplaceService._dictionary_hash('[{"value":"Хлопок"}]'),
        )
        self.assertEqual(rows[0][4], '2026-02-01')
        self.assertEqual(rows[1][1:], ('none', 0, None, None))

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
