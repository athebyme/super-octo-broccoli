# -*- coding: utf-8 -*-
"""Focused regression tests for conflict-aware WB characteristic rollback."""

import json
import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock

from flask import Flask

from models import (
    Marketplace,
    MarketplaceCategory,
    MarketplaceCategoryCharacteristic,
    db,
)


class CardRollbackTestCase(unittest.TestCase):
    SUBJECT_ID = 777
    MATERIAL_ID = 10
    OPTIONAL_ID = 20
    NUMERIC_ID = 30
    REQUIRED_ID = 40
    UNRELATED_ID = 999

    def setUp(self):
        self.app = Flask(__name__)
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        self.app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
        self.app.config['SECRET_KEY'] = 'test-secret'
        db.init_app(self.app)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self._seed_wb_schema()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _seed_wb_schema(self):
        marketplace = Marketplace(
            name='Wildberries',
            code='wb',
            is_active=True,
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
            is_leaf=True,
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
                required=False,
                is_enabled=True,
                is_available=True,
            ),
            MarketplaceCategoryCharacteristic(
                marketplace_id=marketplace.id,
                category_id=category.id,
                charc_id=self.OPTIONAL_ID,
                name='Декор',
                charc_type=1,
                max_count=1,
                dictionary_json=json.dumps(
                    [{'value': 'Красный'}, {'value': 'Синий'}],
                    ensure_ascii=False,
                ),
                required=False,
                is_enabled=True,
                is_available=True,
            ),
            MarketplaceCategoryCharacteristic(
                marketplace_id=marketplace.id,
                category_id=category.id,
                charc_id=self.NUMERIC_ID,
                name='Количество элементов',
                charc_type=4,
                max_count=1,
                required=False,
                is_enabled=True,
                is_available=True,
            ),
            MarketplaceCategoryCharacteristic(
                marketplace_id=marketplace.id,
                category_id=category.id,
                charc_id=self.REQUIRED_ID,
                name='Обязательный признак',
                charc_type=1,
                max_count=1,
                dictionary_json=json.dumps(
                    [{'value': 'Да'}, {'value': 'Нет'}],
                    ensure_ascii=False,
                ),
                required=True,
                is_enabled=True,
                is_available=True,
            ),
        ])
        db.session.commit()

    def _card(self, characteristics):
        from services.wb_validators import _mark_wb_card_as_fetched

        return _mark_wb_card_as_fetched({
            'nmID': 1001,
            'subjectID': self.SUBJECT_ID,
            'vendorCode': 'VC-1001',
            'title': 'Карточка',
            'brand': 'Brand',
            'sizes': [{'chrtID': 1, 'skus': ['1234567890123']}],
            'characteristics': characteristics,
        })

    @staticmethod
    def _by_id(card):
        return {
            int(item['id']): item['value']
            for item in card['characteristics']
        }

    def test_restores_changed_characteristic_and_preserves_unrelated_live_value(self):
        from services.card_rollback import prepare_wb_card_history_rollback
        from services.wb_api_client import WildberriesAPIClient
        from services.wb_validators import WB_PREPARED_CONTEXT_KEY

        before = {'characteristics': [
            {'id': self.MATERIAL_ID, 'value': ['Силикон']},
            {'id': self.UNRELATED_ID, 'value': ['Старое соседнее']},
        ]}
        after = {'characteristics': [
            {'id': self.MATERIAL_ID, 'value': ['Пластик']},
            {'id': self.UNRELATED_ID, 'value': ['Старое соседнее']},
        ]}
        current = self._card([
            {'id': self.MATERIAL_ID, 'value': ['Пластик']},
            {'id': self.UNRELATED_ID, 'value': ['Новое соседнее']},
        ])

        prepared, fields = prepare_wb_card_history_rollback(
            current,
            before,
            after,
            ['characteristics'],
            self.SUBJECT_ID,
        )

        self.assertEqual(fields, {'characteristics'})
        self.assertEqual(self._by_id(prepared), {
            self.MATERIAL_ID: ['Силикон'],
            self.UNRELATED_ID: ['Новое соседнее'],
        })
        self.assertIn(WB_PREPARED_CONTEXT_KEY, prepared)

        response = MagicMock()
        response.json.return_value = {'error': False}
        client = WildberriesAPIClient('test-key')
        client._make_request = MagicMock(return_value=response)
        client.update_cards_batch([prepared])

        sent = client._make_request.call_args.kwargs['json'][0]
        self.assertNotIn(WB_PREPARED_CONTEXT_KEY, sent)
        self.assertEqual(self._by_id(sent), {
            self.MATERIAL_ID: ['Силикон'],
            self.UNRELATED_ID: ['Новое соседнее'],
        })

    def test_removes_characteristic_originally_added_by_history(self):
        from services.card_rollback import prepare_wb_card_history_rollback
        from services.wb_api_client import WildberriesAPIClient
        from services.wb_validators import prepare_card_for_update

        before = {'characteristics': [
            {'id': self.MATERIAL_ID, 'value': ['Силикон']},
        ]}
        after = {'characteristics': [
            {'id': self.MATERIAL_ID, 'value': ['Силикон']},
            {'id': self.OPTIONAL_ID, 'value': ['Красный']},
        ]}
        current = self._card([
            {'id': self.MATERIAL_ID, 'value': ['Силикон']},
            {'id': self.OPTIONAL_ID, 'value': ['Красный']},
            {'id': self.UNRELATED_ID, 'value': ['Не менять']},
        ])

        prepared, _fields = prepare_wb_card_history_rollback(
            current,
            before,
            after,
            ['characteristics'],
            self.SUBJECT_ID,
        )

        self.assertEqual(self._by_id(prepared), {
            self.MATERIAL_ID: ['Силикон'],
            self.UNRELATED_ID: ['Не менять'],
        })

        # Bulk rollback may apply another older history row to this already
        # prepared card. Its signed removal context must survive the chain.
        chained = prepare_card_for_update(
            prepared,
            {'title': 'Прежнее название'},
        )
        self.assertNotIn(self.OPTIONAL_ID, self._by_id(chained))

        response = MagicMock()
        response.json.return_value = {'error': False}
        client = WildberriesAPIClient('test-key')
        client._make_request = MagicMock(return_value=response)
        client.update_cards_batch([prepared])
        sent = client._make_request.call_args.kwargs['json'][0]
        self.assertNotIn(self.OPTIONAL_ID, self._by_id(sent))

    def test_rejects_rollback_when_changed_characteristic_has_live_conflict(self):
        from services.card_rollback import prepare_wb_card_history_rollback
        from services.wb_validators import WBValidationError

        before = {'characteristics': [
            {'id': self.MATERIAL_ID, 'value': ['Силикон']},
        ]}
        after = {'characteristics': [
            {'id': self.MATERIAL_ID, 'value': ['Пластик']},
        ]}
        current = self._card([
            {'id': self.MATERIAL_ID, 'value': ['Металл']},
        ])

        with self.assertRaisesRegex(
            WBValidationError,
            r'конфликт: characteristics\[10\]',
        ):
            prepare_wb_card_history_rollback(
                current,
                before,
                after,
                ['characteristics'],
                self.SUBJECT_ID,
            )

    def test_scalar_list_case_and_numeric_forms_compare_canonically(self):
        from services.card_rollback import prepare_wb_card_history_rollback

        before = {'characteristics': [
            {'id': self.MATERIAL_ID, 'value': 'Силикон'},
            {'id': self.NUMERIC_ID, 'value': 9},
        ]}
        after = {'characteristics': [
            {'id': self.MATERIAL_ID, 'value': 'ПЛАСТИК'},
            {'id': self.NUMERIC_ID, 'value': '10.0'},
        ]}
        current = self._card([
            {'id': self.MATERIAL_ID, 'value': ['  пластик  ']},
            {'id': self.NUMERIC_ID, 'value': 10},
        ])

        prepared, _fields = prepare_wb_card_history_rollback(
            current,
            before,
            after,
            ['characteristics'],
            self.SUBJECT_ID,
        )

        self.assertEqual(self._by_id(prepared), {
            self.MATERIAL_ID: ['Силикон'],
            self.NUMERIC_ID: 9,
        })

    def test_required_characteristic_removal_is_rejected_before_http(self):
        from services.card_rollback import prepare_wb_card_history_rollback
        from services.marketplace_validator import WBCharacteristicValidationError
        from services.wb_api_client import WildberriesAPIClient

        before = {'characteristics': []}
        after = {'characteristics': [
            {'id': self.REQUIRED_ID, 'value': ['Да']},
        ]}
        current = self._card([
            {'id': self.REQUIRED_ID, 'value': ['Да']},
        ])
        prepared, _fields = prepare_wb_card_history_rollback(
            current,
            before,
            after,
            ['characteristics'],
            self.SUBJECT_ID,
        )

        client = WildberriesAPIClient('test-key')
        client._make_request = MagicMock()
        with self.assertRaises(WBCharacteristicValidationError) as raised:
            client.update_cards_batch([prepared])

        self.assertEqual(
            raised.exception.result['issues'][0]['code'],
            'required_characteristic_removal',
        )
        client._make_request.assert_not_called()

    def test_dimensions_restore_exactly_with_numeric_conflict_normalization(self):
        from services.card_rollback import prepare_wb_card_history_rollback

        current = self._card([])
        current['dimensions'] = {
            'length': 20,
            'width': 5,
            'height': 3,
        }
        prepared, fields = prepare_wb_card_history_rollback(
            current,
            {'dimensions': {'length': 10, 'width': 5}},
            {'dimensions': {'length': '20.0', 'width': 5, 'height': 3}},
            ['dimensions'],
            self.SUBJECT_ID,
        )

        self.assertEqual(fields, {'dimensions'})
        self.assertEqual(prepared['dimensions'], {
            'length': 10,
            'width': 5,
            'height': 5,
            'weightBrutto': 0.1,
        })

        # A retry fetches the normalized full WB object. It must be accepted as
        # the already-restored state rather than reported as a conflict.
        retry_card = self._card([])
        retry_card['dimensions'] = dict(prepared['dimensions'])
        retried, retry_fields = prepare_wb_card_history_rollback(
            retry_card,
            {'dimensions': {'length': 10, 'width': 5}},
            {'dimensions': {'length': '20.0', 'width': 5, 'height': 3}},
            ['dimensions'],
            self.SUBJECT_ID,
        )
        self.assertEqual(retry_fields, {'dimensions'})
        self.assertEqual(retried['dimensions'], prepared['dimensions'])

    def test_retry_is_idempotent_when_wb_is_already_at_before_state(self):
        from services.card_rollback import prepare_wb_card_history_rollback

        before = {'characteristics': [
            {'id': self.MATERIAL_ID, 'value': ['Силикон']},
        ]}
        after = {'characteristics': [
            {'id': self.MATERIAL_ID, 'value': ['Пластик']},
        ]}
        already_reverted = self._card(before['characteristics'])

        prepared, fields = prepare_wb_card_history_rollback(
            already_reverted,
            before,
            after,
            ['characteristics'],
            self.SUBJECT_ID,
        )

        self.assertEqual(fields, {'characteristics'})
        self.assertEqual(
            self._by_id(prepared)[self.MATERIAL_ID],
            ['Силикон'],
        )

    def test_multi_history_retry_accepts_already_reverted_group_state(self):
        from services.card_rollback import prepare_wb_card_history_rollback

        first_before = {'characteristics': [{
            'id': self.MATERIAL_ID, 'value': ['Силикон'],
        }]}
        first_after = {'characteristics': [{
            'id': self.MATERIAL_ID, 'value': ['Пластик'],
        }]}
        second_before = first_after
        second_after = {'characteristics': [{
            'id': self.MATERIAL_ID, 'value': ['Металл'],
        }]}
        all_snapshots = [
            first_before, first_after, second_before, second_after,
        ]

        # WB already reached A during the first attempt, while DB still says
        # both A→B and B→C rows need reverting. Process newest-first again.
        current = self._card(first_before['characteristics'])
        for before, after in (
            (second_before, second_after),
            (first_before, first_after),
        ):
            current, _fields = prepare_wb_card_history_rollback(
                current,
                before,
                after,
                ['characteristics'],
                self.SUBJECT_ID,
                acceptable_snapshots=all_snapshots,
            )

        self.assertEqual(
            self._by_id(current)[self.MATERIAL_ID],
            ['Силикон'],
        )

    def test_history_only_advertises_supported_successful_rollback(self):
        from models import BulkEditHistory, CardEditHistory

        supported = CardEditHistory(
            action='update',
            changed_fields=['characteristics'],
            snapshot_before={'characteristics': []},
            snapshot_after={'characteristics': []},
            wb_synced=True,
            wb_sync_status='success',
        )
        photo = CardEditHistory(
            action='update',
            changed_fields=['photos'],
            snapshot_before={'photos': []},
            snapshot_after={'photos': ['x']},
            wb_synced=True,
            wb_sync_status='success',
        )
        failed = CardEditHistory(
            action='update',
            changed_fields=['title'],
            snapshot_before={'title': 'До'},
            snapshot_after={'title': 'После'},
            wb_synced=False,
            wb_sync_status='failed',
        )

        self.assertTrue(supported.can_revert())
        self.assertFalse(photo.can_revert())
        self.assertFalse(failed.can_revert())

        uncertain = CardEditHistory(
            action='update',
            changed_fields=['title'],
            snapshot_before={'title': 'До'},
            snapshot_after={'title': 'После'},
            wb_synced=False,
            wb_sync_status='uncertain',
            created_at=datetime.utcnow() - timedelta(minutes=6),
        )
        active_pending = CardEditHistory(
            action='update',
            changed_fields=['title'],
            snapshot_before={'title': 'До'},
            snapshot_after={'title': 'После'},
            wb_synced=False,
            wb_sync_status='pending',
            created_at=datetime.utcnow(),
        )
        self.assertTrue(uncertain.can_revert())
        self.assertFalse(active_pending.can_revert())

        bulk = BulkEditHistory(
            seller_id=1,
            operation_type='supplier_enrichment',
            status='completed',
        )
        db.session.add(bulk)
        db.session.flush()
        db.session.add_all([
            CardEditHistory(
                product_id=101,
                seller_id=1,
                bulk_edit_id=bulk.id,
                action='update',
                changed_fields=['title'],
                snapshot_before={'title': 'До'},
                snapshot_after={'title': 'После'},
                wb_synced=True,
                wb_sync_status='success',
            ),
            CardEditHistory(
                product_id=101,
                seller_id=1,
                bulk_edit_id=bulk.id,
                action='update',
                changed_fields=['photos'],
                snapshot_before={'photos': []},
                snapshot_after={'photos': ['x']},
                wb_synced=True,
                wb_sync_status='success',
            ),
        ])
        photo_only = BulkEditHistory(
            seller_id=1,
            operation_type='supplier_enrichment',
            status='completed',
        )
        db.session.add(photo_only)
        db.session.flush()
        db.session.add(CardEditHistory(
            product_id=102,
            seller_id=1,
            bulk_edit_id=photo_only.id,
            action='update',
            changed_fields=['photos'],
            snapshot_before={'photos': []},
            snapshot_after={'photos': ['x']},
            wb_synced=True,
            wb_sync_status='success',
        ))
        db.session.commit()

        self.assertTrue(bulk.can_revert())
        self.assertFalse(photo_only.can_revert())

    def test_history_state_classifier_uses_only_changed_characteristic_ids(self):
        from services.card_rollback import classify_wb_card_history_state

        before = {'characteristics': [
            {'id': self.MATERIAL_ID, 'value': ['Силикон']},
            {'id': self.UNRELATED_ID, 'value': ['До']},
        ]}
        after = {'characteristics': [
            {'id': self.MATERIAL_ID, 'value': ['Пластик']},
            {'id': self.UNRELATED_ID, 'value': ['До']},
        ]}
        live = {
            'characteristics': [
                {'id': self.MATERIAL_ID, 'value': ['Пластик']},
                {'id': self.UNRELATED_ID, 'value': ['Параллельно изменено']},
            ],
        }

        self.assertEqual(
            classify_wb_card_history_state(
                live, before, after, ['characteristics']),
            'after',
        )


if __name__ == '__main__':
    unittest.main()
