"""Synthetic source, reference, and no-overwrite gates for draft AI."""

import json
from datetime import datetime
from types import SimpleNamespace
import unittest

from flask import Flask

from models import (
    ImportedProduct, Marketplace, MarketplaceAttributeDefinition,
    MarketplaceAttributeValue, MarketplaceProductDraft, MarketplaceProductType,
    MarketplaceTaxonomyCategory, Seller, SellerMarketplaceAccount, Supplier,
    SupplierProduct, User, db,
)
from services.ozon_draft_ai_validation import (
    OzonDraftAIValidation, OzonDraftAIValidationError,
)
from services.ozon_reference_service import OzonReferenceService


class DraftAIValidationTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(TESTING=True, SQLALCHEMY_DATABASE_URI='sqlite://',
                               SQLALCHEMY_TRACK_MODIFICATIONS=False)
        db.init_app(self.app)
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        now = datetime.utcnow()
        user = User(username='draft-ai-test', email='draft-ai@test.local', is_active=True)
        user.set_password('synthetic-password')
        self.seller = Seller(user=user, company_name='Synthetic')
        self.supplier = Supplier(name='Source', code='draft-ai-source')
        self.marketplace = Marketplace(
            name='Ozon', code='ozon', adapter_code='ozon', is_active=True,
            categories_synced_at=now, categories_snapshot_hash='a' * 64,
        )
        db.session.add_all([self.seller, self.supplier, self.marketplace])
        db.session.flush()
        self.account = SellerMarketplaceAccount(
            seller_id=self.seller.id, marketplace_id=self.marketplace.id,
            external_account_id='synthetic-account', label='Synthetic',
            is_active=True, connection_status='connected',
        )
        category = MarketplaceTaxonomyCategory(
            marketplace_id=self.marketplace.id, external_category_id='10',
            name='Аксессуары', full_path='Аксессуары', depth=0,
            is_available=True, last_seen_at=now,
        )
        db.session.add_all([self.account, category])
        db.session.flush()
        self.product_type = MarketplaceProductType(
            marketplace_id=self.marketplace.id, category_id=category.id,
            external_type_id='777', name='Аксессуар', is_available=True,
            is_seller_selectable=True, attributes_synced_at=now,
            attributes_sync_status='success', attributes_schema_hash='b' * 64,
            attributes_version=3,
        )
        db.session.add(self.product_type)
        db.session.flush()
        self.color = MarketplaceAttributeDefinition(
            marketplace_id=self.marketplace.id, product_type_id=self.product_type.id,
            external_attribute_id='33', name='Цвет', data_type='String',
            dictionary_id='colors', max_value_count=1, is_available=True,
            is_enabled=True, values_synced_at=now, values_sync_status='success',
            values_snapshot_hash='c' * 64, values_version=2,
        )
        self.material = MarketplaceAttributeDefinition(
            marketplace_id=self.marketplace.id, product_type_id=self.product_type.id,
            external_attribute_id='34', name='Материал изделия', data_type='String',
            max_value_count=1, is_available=True, is_enabled=True,
        )
        description = MarketplaceAttributeDefinition(
            marketplace_id=self.marketplace.id, product_type_id=self.product_type.id,
            external_attribute_id='4191', name='Аннотация', data_type='String',
            is_required=True, max_value_count=1, is_available=True,
            is_enabled=True,
        )
        country = MarketplaceAttributeDefinition(
            marketplace_id=self.marketplace.id, product_type_id=self.product_type.id,
            external_attribute_id='32', name='Страна производства',
            data_type='String', max_value_count=1, is_available=True,
            is_enabled=True,
        )
        db.session.add_all([self.color, self.material, description, country])
        db.session.flush()
        self.red = MarketplaceAttributeValue(
            marketplace_id=self.marketplace.id, product_type_id=self.product_type.id,
            attribute_id=self.color.id, external_value_id='9001',
            value='Красный',
            value_normalized=OzonReferenceService.normalize_value('Красный'),
            is_available=True,
        )
        db.session.add(self.red)
        source = {
            'title': 'Красный аксессуар', 'description': 'Красный аксессуар из силикона',
            'category': 'Аксессуары',
            'characteristics': {'Цвет': 'Красный', 'Материал изделия': 'Силикон',
                                'Страна производства': 'Россия'},
            'price': 1200, 'barcodes': ['4600000000012'],
            'dimensions': {'package_width_cm': 10},
        }
        self.supplier_product = SupplierProduct(
            supplier_id=self.supplier.id, external_id='source-1',
            title='Supplier normalized poisoned', original_data_json=json.dumps(source, ensure_ascii=False),
        )
        db.session.add(self.supplier_product)
        db.session.flush()
        self.imported = ImportedProduct(
            seller_id=self.seller.id, supplier_id=self.supplier.id,
            supplier_product_id=self.supplier_product.id,
            external_id='source-1', title='Blue normalized poisoned',
            original_data=json.dumps(source, ensure_ascii=False),
            ai_detected_brand='"Poisoned brand"',
        )
        db.session.add(self.imported)
        db.session.flush()
        self.draft = MarketplaceProductDraft(
            seller_id=self.seller.id, marketplace_id=self.marketplace.id,
            account_id=self.account.id, imported_product_id=self.imported.id,
            product_type_id=self.product_type.id, offer_id='source-1',
            external_category_id='10', external_type_id='777', status='draft',
            source_fact_hash='f' * 64, source_facts_json='{}',
            content_json=json.dumps({'name': 'Ручное имя',
                                     'description': 'Ручное описание'}, ensure_ascii=False),
            attributes_json='[]', complex_attributes_json='[]',
            attribute_removals_json='[]',
        )
        db.session.add(self.draft)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    @staticmethod
    def _color_result(draft_id):
        return {'draft_id': draft_id, 'suggestions': [{
            'attribute_id': '33', 'complex_id': '0', 'group_ordinal': 0,
            'values': [{'value': 'Красный', 'dictionary_value_id': '9001'}],
            'evidence': [{'path': '/characteristics/0/value', 'quote': 'Красный'}],
            'provenance_code': 'literal_source',
        }]}

    def test_capture_uses_only_owned_source_and_excludes_engine_fields(self):
        context = OzonDraftAIValidation.capture(self.draft)
        self.assertEqual(context['source_kind'], 'imported')
        self.assertIn('Красный', json.dumps(context['source_facts'], ensure_ascii=False))
        self.assertNotIn('Blue normalized poisoned', json.dumps(context['source_facts']))
        self.assertNotIn('Poisoned brand', json.dumps(context['source_facts']))
        self.assertNotIn('1200', json.dumps(context['source_facts']))
        self.assertNotIn('Россия', json.dumps(context['source_facts'], ensure_ascii=False))
        self.assertNotIn('barcodes', context['source_facts'])
        self.assertNotIn('dimensions', context['source_facts'])
        open_ids = {row['attribute_id'] for row in context['schema']['attributes']}
        self.assertEqual(open_ids, {'33', '34'})
        self.assertNotIn('4191', open_ids)  # content.description is manual/implicit.

    def test_dictionary_literal_proposal_and_partial_apply_keep_seal(self):
        context = OzonDraftAIValidation.capture(self.draft)
        result = OzonDraftAIValidation.validate_result(
            context, self._color_result(self.draft.id),
        )
        self.assertEqual(result['rejections'], [])
        self.assertEqual(len(result['suggestions']), 1)
        patch = OzonDraftAIValidation.merge_selected(self.draft, result['suggestions'])
        self.assertEqual(patch['attributes'][-1]['attribute_id'], '33')
        self.draft.attributes_json = json.dumps(patch['attributes'], ensure_ascii=False)
        self.draft.version += 1
        db.session.commit()
        later = OzonDraftAIValidation.capture(self.draft)
        self.assertEqual(later['dictionary_hash'], context['dictionary_hash'])
        self.assertEqual(later['type_schema_hash'], context['type_schema_hash'])
        self.assertNotEqual(later['filled_slots_hash'], context['filled_slots_hash'])
        self.assertEqual(OzonDraftAIValidation.filled_slots_hash(self.draft),
                         later['filled_slots_hash'])
        self.assertEqual({row['attribute_id'] for row in later['schema']['attributes']},
                         {'34'})

    def test_large_dictionary_uses_bounded_exact_source_shortlist(self):
        for value_id in range(2, 72):
            value = f'Ненаблюдаемый цвет {value_id}'
            db.session.add(MarketplaceAttributeValue(
                marketplace_id=self.marketplace.id,
                product_type_id=self.product_type.id,
                attribute_id=self.color.id,
                external_value_id=str(9000 + value_id), value=value,
                value_normalized=OzonReferenceService.normalize_value(value),
                is_available=True,
            ))
        db.session.commit()
        context = OzonDraftAIValidation.capture(self.draft)
        color = next(row for row in context['schema']['attributes']
                     if row['attribute_id'] == '33')
        self.assertEqual(color['dictionary_values'], [
            {'dictionary_value_id': '9001', 'value': 'Красный'},
        ])
        self.assertEqual(len(OzonDraftAIValidation.validate_result(
            context, self._color_result(self.draft.id),
        )['suggestions']), 1)

    def test_imported_snapshot_present_but_empty_does_not_fallback(self):
        self.imported.original_data = '{}'
        db.session.commit()
        with self.assertRaises(OzonDraftAIValidationError) as raised:
            OzonDraftAIValidation.capture(self.draft)
        self.assertEqual(raised.exception.code, 'source_facts_missing')
        self.imported.original_data = None
        db.session.commit()
        context = OzonDraftAIValidation.capture(self.draft)
        self.assertEqual(context['source_kind'], 'supplier')
        self.assertEqual(context['source_product_id'], self.supplier_product.id)

    def test_nonfinite_or_duplicate_source_facts_fail_before_model(self):
        for raw, expected in (
            ('{"title":"Safe","title":"Changed"}', 'duplicate_source_key'),
            ('{"title":"Safe","description":1e999}', 'nonfinite_source'),
        ):
            self.imported.original_data = raw
            db.session.commit()
            with self.subTest(expected=expected), self.assertRaises(
                OzonDraftAIValidationError,
            ) as raised:
                OzonDraftAIValidation.capture(self.draft)
            self.assertEqual(raised.exception.code, expected)

    def test_sensitive_strings_embedded_in_source_text_are_removed(self):
        snapshot = json.loads(self.imported.original_data)
        snapshot['description'] = (
            'Красный аксессуар из силикона. '
            'Фото https://cdn.example.test/photo?signature=private-signature '
            'Пишите buyer@example.test, телефон +7 (999) 123-45-67. '
            'api_key=private-key Bearer private-token '
            'Штрихкод 4600000000012.'
        )
        self.imported.original_data = json.dumps(snapshot, ensure_ascii=False)
        db.session.commit()
        context = OzonDraftAIValidation.capture(self.draft)
        facts = json.dumps(context['source_facts'], ensure_ascii=False)
        self.assertIn('Красный аксессуар из силикона', facts)
        for sensitive in ('https://', 'private-signature', 'buyer@example.test',
                          '999', 'private-key', 'private-token', '4600000000012'):
            self.assertNotIn(sensitive, facts)

    def test_only_sensitive_source_text_has_no_model_evidence(self):
        self.imported.original_data = json.dumps({
            'description': 'https://cdn.example.test/image?token=private api_key=private-key',
        })
        db.session.commit()
        with self.assertRaises(OzonDraftAIValidationError) as raised:
            OzonDraftAIValidation.capture(self.draft)
        self.assertEqual(raised.exception.code, 'source_facts_missing')

    def test_model_cannot_propose_ungrounded_or_foreign_attribute(self):
        context = OzonDraftAIValidation.capture(self.draft)
        invalid = self._color_result(self.draft.id)
        invalid['suggestions'][0]['values'][0]['value'] = 'Синий'
        self.assertEqual(OzonDraftAIValidation.validate_result(context, invalid),
                         {'suggestions': [], 'rejections': ['dictionary_value_not_exact']})
        invalid = self._color_result(self.draft.id)
        invalid['suggestions'][0]['attribute_id'] = '32'
        self.assertEqual(OzonDraftAIValidation.validate_result(context, invalid),
                         {'suggestions': [], 'rejections': ['attribute_outside_sealed_schema']})
        invalid = self._color_result(self.draft.id)
        invalid['suggestions'][0]['attribute_id'] = []
        self.assertEqual(OzonDraftAIValidation.validate_result(context, invalid),
                         {'suggestions': [], 'rejections': ['invalid_suggestion_identity']})

    def test_named_source_field_must_match_target_attribute(self):
        context = OzonDraftAIValidation.capture(self.draft)
        wrong = {'draft_id': self.draft.id, 'suggestions': [{
            'attribute_id': '34', 'complex_id': '0', 'group_ordinal': 0,
            'values': [{'value': 'Красный'}],
            'evidence': [{'path': '/characteristics/0/value', 'quote': 'Красный'}],
            'provenance_code': 'literal_source',
        }]}
        self.assertEqual(OzonDraftAIValidation.validate_result(context, wrong),
                         {'suggestions': [], 'rejections': ['source_field_mismatch']})

        right = self._color_result(self.draft.id)
        self.assertEqual(len(OzonDraftAIValidation.validate_result(
            context, right)['suggestions']), 1)
        right['suggestions'][0].update({
            'attribute_id': '34', 'values': [{'value': 'Силикон'}],
            'evidence': [{'path': '/characteristics/1/value', 'quote': 'Силикон'}],
        })
        self.assertEqual(len(OzonDraftAIValidation.validate_result(
            context, right)['suggestions']), 1)  # Материал -> Материал изделия.

    def test_named_evidence_cannot_be_laundered_by_unlabeled_prose(self):
        context = OzonDraftAIValidation.capture(self.draft)
        mixed = {'draft_id': self.draft.id, 'suggestions': [{
            'attribute_id': '34', 'complex_id': '0', 'group_ordinal': 0,
            'values': [{'value': 'Красный'}],
            'evidence': [
                {'path': '/characteristics/1/value', 'quote': 'Силикон'},
                {'path': '/description', 'quote': 'Красный'},
            ],
            'provenance_code': 'literal_source',
        }]}
        self.assertEqual(OzonDraftAIValidation.validate_result(context, mixed),
                         {'suggestions': [], 'rejections': ['value_not_grounded']})
        prose = self._color_result(self.draft.id)
        prose['suggestions'][0]['evidence'] = [{'path': '/title', 'quote': 'Красный'}]
        self.assertEqual(len(OzonDraftAIValidation.validate_result(
            context, prose)['suggestions']), 1)  # Visible for mandatory seller review.

    def test_top_level_named_lists_bind_to_their_semantic_field(self):
        snapshot = json.loads(self.imported.original_data)
        snapshot['colors'] = ['Красный']
        snapshot['materials'] = ['Красный']
        self.imported.original_data = json.dumps(snapshot, ensure_ascii=False)
        db.session.commit()
        context = OzonDraftAIValidation.capture(self.draft)
        result = self._color_result(self.draft.id)
        result['suggestions'][0]['evidence'] = [{'path': '/materials/0', 'quote': 'Красный'}]
        self.assertEqual(OzonDraftAIValidation.validate_result(context, result),
                         {'suggestions': [], 'rejections': ['source_field_mismatch']})
        result['suggestions'][0]['evidence'] = [{'path': '/colors/0', 'quote': 'Красный'}]
        self.assertEqual(len(OzonDraftAIValidation.validate_result(
            context, result)['suggestions']), 1)

    def test_model_source_pointer_and_duplicate_json_fail_closed(self):
        context = OzonDraftAIValidation.capture(self.draft)
        invalid = self._color_result(self.draft.id)
        invalid['suggestions'][0]['evidence'][0]['path'] = '/price'
        result = OzonDraftAIValidation.validate_result(context, invalid)
        self.assertEqual(result['suggestions'], [])
        self.assertIn('evidence_path_not_found', result['rejections'])
        with self.assertRaises(OzonDraftAIValidationError) as raised:
            OzonDraftAIValidation.validate_result(context, '{"draft_id":1,"draft_id":1}')
        self.assertEqual(raised.exception.code, 'duplicate_source_key')

    def test_incomplete_complex_group_requires_manual_review(self):
        for external_id, name in (('501', 'Связанная текстура'),
                                  ('502', 'Связанный материал')):
            db.session.add(MarketplaceAttributeDefinition(
                marketplace_id=self.marketplace.id,
                product_type_id=self.product_type.id,
                external_attribute_id=external_id,
                name=name, data_type='String', is_required=True,
                attribute_complex_id='700', complex_is_collection=True,
                is_enabled=True, is_available=True,
            ))
        db.session.commit()
        context = OzonDraftAIValidation.capture(self.draft)
        self.assertIn('complex_group_requires_manual_review',
                      context['reference_issues'])
        self.assertNotIn('501', {row['attribute_id']
                                 for row in context['schema']['attributes']})
        forged = {'draft_id': self.draft.id, 'suggestions': [{
            'attribute_id': '501', 'complex_id': '700', 'group_ordinal': 1,
            'values': [{'value': 'Гладкий'}],
            'evidence': [{'path': '/description', 'quote': 'аксессуар'}],
            'provenance_code': 'literal_source',
        }]}
        self.assertEqual(OzonDraftAIValidation.validate_result(context, forged),
                         {'suggestions': [], 'rejections': [
                             'complex_group_requires_manual_review',
                         ]})
        before = self.draft.complex_attributes_json
        with self.assertRaises(OzonDraftAIValidationError) as raised:
            OzonDraftAIValidation.merge_selected(self.draft, forged['suggestions'])
        self.assertEqual(raised.exception.code,
                         'complex_group_requires_manual_review')
        self.assertEqual(self.draft.complex_attributes_json, before)

    def test_reference_or_unreviewed_draft_change_stales_seal(self):
        context = OzonDraftAIValidation.capture(self.draft)
        item = SimpleNamespace(
            id=999, seller_id=self.seller.id, account_id=self.account.id,
            draft_id=self.draft.id, imported_product_id=self.imported.id,
            product_type_id=self.product_type.id, expected_draft_version=self.draft.version,
            source_kind=context['source_kind'], source_product_id=context['source_product_id'],
            source_hash=context['source_hash'], type_schema_hash=context['type_schema_hash'],
            dictionary_hash=context['dictionary_hash'], filled_slots_hash=context['filled_slots_hash'],
            reviewed_filled_slots_hash=None,
        )
        self.assertEqual(OzonDraftAIValidation.check_seal(self.draft, item), context)
        self.material.is_enabled = False
        db.session.commit()
        with self.assertRaises(OzonDraftAIValidationError) as raised:
            OzonDraftAIValidation.check_seal(self.draft, item)
        self.assertEqual(raised.exception.code, 'source_or_schema_changed')
