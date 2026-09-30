"""Admission tests: durable state, source drift, atomicity and legacy rollback."""

from copy import deepcopy
from datetime import datetime
import json
import unittest

from flask import Flask
from models import (db, User, Supplier, SupplierProduct, Marketplace, MarketplaceCategory,
                    MarketplaceCategoryCharacteristic, SupplierCatalogEnrichmentRun,
                    SupplierCatalogEnrichmentItem, MarketplaceTaxonomyCategory,
                    MarketplaceProductType, MarketplaceAttributeDefinition,
                    MarketplaceAttributeValue)
from scripts.validate_luna_parsing import ContractError, validate_batch, BRAND_LABEL_ALIASES
from services.supplier_luna_enrichment import prepare_batch, apply_batch, wb_parsing_reference
from services.supplier_catalog_enrichment import (
    SupplierCatalogEnrichmentService, SupplierCatalogEnrichmentError,
)


class LunaAdmissionTest(unittest.TestCase):
    def test_parser_brand_alias_is_already_reviewed_in_draft_policy(self):
        from services.marketplace_drafts import MarketplaceDraftService
        for canonical, aliases in BRAND_LABEL_ALIASES.items():
            for alias in aliases:
                self.assertEqual(MarketplaceDraftService.OBSERVED_BRAND_CANONICAL_ALIASES[
                    MarketplaceDraftService._normalized_text(alias)], canonical)

    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(TESTING=True, SQLALCHEMY_DATABASE_URI='sqlite://',
                               SQLALCHEMY_TRACK_MODIFICATIONS=False)
        db.init_app(self.app)
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        now = datetime.utcnow()
        admin = User(username='test-admin', email='admin@example.invalid',
                     is_active=True, is_admin=True, password_hash='synthetic')
        supplier = Supplier(name='Synthetic', code='luna-test')
        mp = Marketplace(name='WB', code='wb', is_active=True,
                         categories_sync_status='success', categories_synced_at=now,
                         categories_snapshot_hash='a' * 64)
        db.session.add_all([admin, supplier, mp])
        db.session.flush()
        category = MarketplaceCategory(
            marketplace_id=mp.id, subject_id=51001, subject_name='Тестовый предмет',
            is_enabled=True, is_available=True, is_leaf=True,
            characteristics_synced_at=now, characteristics_sync_status='success',
            characteristics_schema_hash='b' * 64)
        db.session.add(category)
        db.session.flush()
        db.session.add(MarketplaceCategoryCharacteristic(
            category_id=category.id, marketplace_id=mp.id, charc_id=200,
            name='Объем', charc_type=4, unit_name='мл', max_count=1,
            is_enabled=True, is_available=True))
        self.source = {'title': 'Тестовый предмет 100 мл'}
        self.product = SupplierProduct(
            supplier_id=supplier.id, external_id='synthetic-1', title=self.source['title'],
            original_data_json=json.dumps(self.source), content_revision=1,
            ai_marketplace_json=json.dumps({'unsupported_legacy_claim': 'synthetic'}))
        db.session.add(self.product)
        db.session.commit()
        self.admin_id = admin.id
        self.batch = {
            'contract_version': 'wb-luna-parsing-v1', 'batch_id': 'synthetic-admission',
            'model': 'gpt-5.6-luna', 'reasoning_effort': 'max', 'phase': 'characteristics',
            'items': [{'product_id': self.product.id, 'supplier_id': supplier.id,
                       'source': self.source, 'subject_id': 51001,
                       'subject_name': category.subject_name, 'schema_hash': 'b' * 64,
                       'schema': [{'id': 200, 'name': 'Объем', 'type': 'number',
                                   'usable': True, 'constrained': False, 'max_count': 1}]}]}
        self.output = {
            'contract_version': self.batch['contract_version'], 'batch_id': self.batch['batch_id'],
            'items': [{'product_id': self.product.id, 'supplier_id': supplier.id,
                       'status': 'proposed', 'issues': [], 'characteristics': [
                           {'id': 200, 'name': 'Объем', 'value': 100,
                            'evidence': [{'path': '/title', 'quote': '100 мл'}]}]}]}

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def apply(self, batch=None, **kwargs):
        return apply_batch(batch or prepare_batch(self.batch), self.output,
                           admin_user_id=kwargs.get('admin_id', self.admin_id),
                           reviewed_product_ids=kwargs.get('ids', [self.product.id]))

    def test_shared_state_audit_idempotence_and_existing_rollback(self):
        before = self.product.ai_marketplace_json
        batch = prepare_batch(self.batch)
        first = self.apply(batch)
        self.assertEqual(first['applied'], 1)
        self.assertEqual(self.product.content_revision, 2)
        data = self.product.get_ai_marketplace_data()
        self.assertEqual(data['Объем'], 100)
        self.assertNotIn('unsupported_legacy_claim', data)
        self.assertFalse(data['_meta']['channels']['wb']['ready_for_publication'])
        self.assertEqual(self.apply(batch)['reused'], 1)
        self.assertFalse(db.session.connection().connection.driver_connection.in_transaction)
        self.assertEqual(SupplierCatalogEnrichmentRun.query.count(), 1)
        item = SupplierCatalogEnrichmentItem.query.one()
        SupplierCatalogEnrichmentService.rollback_item(
            item_id=item.id, supplier_id=self.product.supplier_id)
        self.assertEqual(json.loads(self.product.ai_marketplace_json), json.loads(before))
        self.assertEqual(self.product.content_revision, 3)
        with self.assertRaisesRegex(ContractError, 'previously_rolled_back'):
            self.apply(batch)

    def test_source_addition_and_target_edit_both_reject_without_writes(self):
        for field in ('original_data_json', 'ai_marketplace_json'):
            batch = prepare_batch(self.batch)
            previous = getattr(self.product, field)
            setattr(self.product, field, json.dumps(dict(self.source, description='New fact')))
            db.session.commit()
            with self.assertRaises(ContractError):
                self.apply(batch)
            self.assertEqual(SupplierCatalogEnrichmentRun.query.count(), 0)
            setattr(self.product, field, previous)
            db.session.commit()

    def test_rollback_rejects_source_change_even_without_revision_change(self):
        self.apply()
        self.product.original_data_json = json.dumps(dict(self.source, description='New source'))
        db.session.commit()
        item = SupplierCatalogEnrichmentItem.query.one()
        with self.assertRaisesRegex(SupplierCatalogEnrichmentError, 'После запуска'):
            SupplierCatalogEnrichmentService.rollback_item(
                item_id=item.id, supplier_id=self.product.supplier_id)
        self.assertEqual(item.status, 'rollback_conflict')
        self.assertEqual(self.product.content_revision, 2)

    def test_later_partial_pass_preserves_admitted_facts_from_same_source(self):
        category = MarketplaceCategory.query.one()
        db.session.add(MarketplaceCategoryCharacteristic(
            category_id=category.id, marketplace_id=category.marketplace_id, charc_id=201,
            name='Название', charc_type=1, max_count=1, is_enabled=True, is_available=True))
        db.session.commit()
        self.batch['items'][0]['schema'].append({'id': 201, 'name': 'Название',
            'type': 'string_array', 'usable': True, 'constrained': False, 'max_count': 1})
        self.apply()
        self.batch['batch_id'] = self.output['batch_id'] = 'later-admission'
        self.output['items'][0]['characteristics'] = [{
            'id': 201, 'name': 'Название', 'value': [self.source['title']],
            'evidence': [{'path': '/title', 'quote': self.source['title']}]}]
        self.apply()
        data = self.product.get_ai_marketplace_data()
        self.assertEqual(data['Объем'], 100)
        self.assertEqual(data['Название'], [self.source['title']])
        self.assertEqual(len(data['_meta']['channels']['wb']['native_attributes']), 2)

    def test_explicit_field_withdrawal_is_audited_scoped_and_not_reintroduced(self):
        category = MarketplaceCategory.query.one()
        db.session.add(MarketplaceCategoryCharacteristic(
            category_id=category.id, marketplace_id=category.marketplace_id, charc_id=201,
            name='Название', charc_type=1, max_count=1, is_enabled=True, is_available=True))
        db.session.commit()
        self.batch['items'][0]['schema'].append({'id': 201, 'name': 'Название',
            'type': 'string_array', 'usable': True, 'constrained': False, 'max_count': 1})
        retained = {'id': 201, 'name': 'Название', 'value': [self.source['title']],
                    'evidence': [{'path': '/title', 'quote': self.source['title']}]}
        original = deepcopy(self.output['items'][0]['characteristics'][0])
        self.output['items'][0]['characteristics'].append(retained)
        self.apply()
        self.batch['batch_id'] = self.output['batch_id'] = 'reviewed-withdrawal'
        self.output['items'][0]['characteristics'] = [retained]
        sealed = prepare_batch(self.batch)
        pid = self.product.id
        for rejected, error in (
                ({True: {200: 'source_ambiguous'}}, 'foreign_product'),
                ({pid: {999: 'source_ambiguous'}}, 'not_in_channel'),
                ({pid: {200: 'not a code'}}, 'invalid_issue_code')):
            with self.subTest(rejected=rejected), self.assertRaisesRegex(ContractError, error):
                apply_batch(sealed, self.output, admin_user_id=self.admin_id,
                            reviewed_product_ids=[pid], field_rejections=rejected)
            db.session.rollback()
            self.assertEqual(self.product.content_revision, 2)
            self.assertEqual(SupplierCatalogEnrichmentRun.query.count(), 1)
        rejected = {pid: {200: 'source_ambiguous'}}
        result = apply_batch(sealed, self.output, admin_user_id=self.admin_id,
                             reviewed_product_ids=[pid], field_rejections=rejected)
        self.assertEqual(result['applied'], 1)
        data = self.product.get_ai_marketplace_data()
        self.assertNotIn('Объем', data)
        self.assertEqual(data['Название'], retained['value'])
        channel = data['_meta']['channels']['wb']
        self.assertEqual(len(channel['native_attributes']), 1)
        self.assertEqual(channel['rejected_fields'][0]['field'], original)
        self.assertEqual(channel['rejected_fields'][0]['reason'], 'source_ambiguous')
        self.assertEqual(channel['status'], 'partial')
        replay = apply_batch(sealed, self.output, admin_user_id=self.admin_id,
                             reviewed_product_ids=[pid], field_rejections=rejected)
        self.assertEqual(replay['reused'], 1)
        with self.assertRaisesRegex(ContractError, 'different_rejections'):
            apply_batch(sealed, self.output, admin_user_id=self.admin_id,
                        reviewed_product_ids=[pid], field_rejections={pid: {200: 'other_reason'}})
        self.batch['batch_id'] = self.output['batch_id'] = 'reintroduction'
        self.output['items'][0]['characteristics'] = [original]
        with self.assertRaisesRegex(ContractError, 'previously_rejected_field_reintroduced'):
            self.apply()
        self.assertEqual(self.product.content_revision, 3)
        self.assertEqual(SupplierCatalogEnrichmentRun.query.count(), 2)

    def test_second_item_failure_rolls_back_first_item_and_journal(self):
        batch = prepare_batch(self.batch)
        second = SupplierProduct(supplier_id=self.product.supplier_id, external_id='synthetic-2',
                                 title='Synthetic', original_data_json=json.dumps(self.source))
        db.session.add(second)
        db.session.commit()
        original = deepcopy(self.batch['items'][0])
        original['product_id'] = second.id
        self.batch['items'].append(original)
        output = deepcopy(self.output['items'][0])
        output['product_id'] = second.id
        self.output['items'].append(output)
        batch = prepare_batch(self.batch)
        second.content_revision = 9
        db.session.commit()
        with self.assertRaisesRegex(ContractError, 'target_state_drift'):
            self.apply(batch, ids=[self.product.id, second.id])
        self.assertEqual(self.product.content_revision, 1)
        self.assertEqual(SupplierCatalogEnrichmentItem.query.count(), 0)

    def test_admin_scope_review_and_schema_drift_fail_closed(self):
        for kwargs in ({'admin_id': 999}, {'ids': []}, {'ids': [True]}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ContractError):
                self.apply(**kwargs)
        batch = prepare_batch(self.batch)
        MarketplaceCategory.query.one().characteristics_schema_hash = 'c' * 64
        db.session.commit()
        with self.assertRaisesRegex(ContractError, 'schema_drift'):
            self.apply(batch)
        self.assertEqual(SupplierCatalogEnrichmentRun.query.count(), 0)

    def test_wb_parser_schema_excludes_disabled_types_and_keeps_legacy_required(self):
        category = MarketplaceCategory.query.one()
        for charc_id, charc_type, enabled, required in [
                (201, 0, True, False), (202, 2, True, False),
                (203, 1, False, True), (204, 1, False, False)]:
            db.session.add(MarketplaceCategoryCharacteristic(
                category_id=category.id, marketplace_id=category.marketplace_id,
                charc_id=charc_id, name=f'Field {charc_id}', charc_type=charc_type,
                max_count=1, is_enabled=enabled, required=required, is_available=True))
        db.session.commit()
        reference = wb_parsing_reference(51001)
        self.assertEqual({field['id'] for field in reference['schema']}, {200, 203})
        self.assertEqual({field['id']: field['code'] for field in reference['blocked_fields']},
                         {201: 'unused_characteristic', 202: 'unsupported_type'})
        self.assertEqual(reference['required_reference_blockers'], [])
        self.apply()
        channel = self.product.get_ai_marketplace_data()['_meta']['channels']['wb']
        self.assertEqual(channel['schema_field_count'], 2)
        self.assertEqual([f['name'] for f in channel['missing_required']], ['Field 203'])

    def test_partial_v2_keeps_inferences_separate_and_rejects_physical_guess(self):
        batch, output = deepcopy(self.batch), deepcopy(self.output)
        batch.update(contract_version='supplier-luna-parsing-v2', marketplace_code='wb')
        output['contract_version'] = batch['contract_version']
        output['items'][0].update(status='needs_review', issues=['missing_data'], inferences=[])
        self.assertTrue(validate_batch(batch, output)['valid'])
        field = deepcopy(output['items'][0]['characteristics'][0])
        field['reason'] = 'A plausible guess'
        output['items'][0].update(characteristics=[], inferences=[field])
        batch['items'][0]['schema'][0]['inference_allowed'] = True
        with self.assertRaisesRegex(ContractError, 'inference_field_not_allowed'):
            validate_batch(batch, output)

    def ozon_case(self):
        now = datetime.utcnow()
        mp = Marketplace(code='ozon', name='Ozon', is_active=True,
                         categories_synced_at=now, categories_snapshot_hash='o' * 64)
        db.session.add(mp)
        db.session.flush()
        category = MarketplaceTaxonomyCategory(marketplace_id=mp.id,
            external_category_id='c1', name='Test', full_path='Test')
        db.session.add(category)
        db.session.flush()
        pt = MarketplaceProductType(marketplace_id=mp.id, category_id=category.id,
            external_type_id='t1', name='Test type', attributes_synced_at=now,
            attributes_schema_hash='s' * 64, is_seller_selectable=True)
        db.session.add(pt)
        db.session.flush()
        field = MarketplaceAttributeDefinition(marketplace_id=mp.id, product_type_id=pt.id,
            external_attribute_id='101', name='Название', data_type='String',
            dictionary_id='d1', values_synced_at=now, values_snapshot_hash='v' * 64)
        missing = MarketplaceAttributeDefinition(marketplace_id=mp.id, product_type_id=pt.id,
            external_attribute_id='102', name='Required', data_type='String', is_required=True)
        inference = MarketplaceAttributeDefinition(marketplace_id=mp.id, product_type_id=pt.id,
            external_attribute_id='103', name='Пол', data_type='String', dictionary_id='d2',
            values_synced_at=now, values_snapshot_hash='v' * 64)
        db.session.add_all([field, missing, inference])
        db.session.flush()
        for attribute, external, value in [(field, 'official-title', self.source['title']),
                                           (inference, 'official-gender', 'Для нее')]:
            db.session.add(MarketplaceAttributeValue(marketplace_id=mp.id, product_type_id=pt.id,
                attribute_id=attribute.id, external_value_id=external,
                value=value, value_normalized=value.casefold()))
        db.session.commit()
        batch = deepcopy(self.batch)
        batch.update(contract_version='supplier-luna-parsing-v2', marketplace_code='ozon',
                     batch_id='synthetic-ozon')
        original = batch['items'][0]
        original.update(schema_hash=pt.attributes_schema_hash, target={
            'marketplace_code': 'ozon', 'product_type_id': pt.id, 'type_id': 't1',
            'description_category_id': 'c1', 'name': pt.name, 'full_path': 'Test'}, schema=[
                {'id': attribute.id, 'name': attribute.name, 'type': 'string_array',
                 'usable': True, 'constrained': True, 'max_count': 1,
                 'allowed_values': [value], 'inference_allowed': attribute == inference}
                for attribute, value in [(field, self.source['title']), (inference, 'Для нее')]])
        evidence = [{'path': '/title', 'quote': self.source['title']}]
        output = {'contract_version': batch['contract_version'], 'batch_id': batch['batch_id'],
            'items': [{'product_id': self.product.id, 'supplier_id': self.product.supplier_id,
                'status': 'needs_review', 'issues': ['required_fields_missing'],
                'characteristics': [{'id': field.id, 'name': field.name,
                                    'value': [self.source['title']], 'evidence': evidence}],
                'inferences': [{'id': inference.id, 'name': inference.name, 'value': ['Для нее'],
                                'evidence': evidence, 'reason': 'Synthetic review proposal'}]}]}
        return batch, output, field

    def test_ozon_exact_native_ids_shared_visibility_and_separate_inference(self):
        self.apply()
        batch, output, _ = self.ozon_case()
        apply_batch(prepare_batch(batch), output, admin_user_id=self.admin_id,
                    reviewed_product_ids=[self.product.id])
        data = self.product.get_ai_marketplace_data()
        self.assertEqual(data['Объем'], 100)
        channel = data['_meta']['channels']['ozon']
        self.assertEqual(channel['native_attributes'], [{'attribute_id': '101',
            'complex_id': '0', 'values': [{'dictionary_value_id': 'official-title',
                                         'value': self.source['title']}]}])
        self.assertEqual(len(channel['inferences']), 1)
        self.assertEqual(len(channel['missing_required']), 1)
        from services.supplier_service import SupplierService, _supplier_characteristics_payload
        groups = SupplierService.get_product_detail_fact_groups(self.product)
        self.assertEqual([result['code'] for result in groups['marketplace_results']], ['wb', 'ozon'])
        self.assertEqual(len(groups['marketplace_results'][1]['inferred']), 1)
        self.assertIn('Объем', _supplier_characteristics_payload(self.product))
        self.product.original_data_json = json.dumps(dict(self.source, description='New source'))
        groups = SupplierService.get_product_detail_fact_groups(self.product)
        self.assertFalse(groups['marketplace_results'][0]['source_current'])
        self.assertNotIn('Объем', _supplier_characteristics_payload(self.product) or '')

    def test_ozon_ambiguous_and_restricted_dictionary_values_rejected(self):
        batch, output, field = self.ozon_case()
        field.restriction_value_ids_json = '["different-official-id"]'
        db.session.commit()
        with self.assertRaisesRegex(ContractError, 'dictionary_value_missing_or_ambiguous'):
            apply_batch(prepare_batch(batch), output, admin_user_id=self.admin_id,
                        reviewed_product_ids=[self.product.id])
        self.assertEqual(SupplierCatalogEnrichmentRun.query.count(), 0)
        field.restriction_value_ids_json = None
        db.session.add(MarketplaceAttributeValue(marketplace_id=field.marketplace_id,
            product_type_id=field.product_type_id, attribute_id=field.id,
            external_value_id='duplicate-display', value=self.source['title'],
            value_normalized=self.source['title'].casefold()))
        db.session.commit()
        with self.assertRaisesRegex(ContractError, 'dictionary_value_missing_or_ambiguous'):
            apply_batch(prepare_batch(batch), output, admin_user_id=self.admin_id,
                        reviewed_product_ids=[self.product.id])

    def test_explicit_inference_review_is_audited_native_and_idempotency_bound(self):
        batch, output, _ = self.ozon_case()
        inference_id = output['items'][0]['inferences'][0]['id']
        sealed = prepare_batch(batch)
        kwargs = dict(admin_user_id=self.admin_id, reviewed_product_ids=[self.product.id])
        for invalid in ([inference_id], {True: [inference_id]}, {self.product.id: [99999]}):
            with self.subTest(invalid=invalid), self.assertRaises(ContractError):
                apply_batch(sealed, output, approved_inferences=invalid, **kwargs)
        applied = apply_batch(sealed, output,
            approved_inferences={self.product.id: [inference_id]}, **kwargs)
        channel = self.product.get_ai_marketplace_data()['_meta']['channels']['ozon']
        self.assertEqual(channel['inferences'], [])
        self.assertEqual(len(channel['approved_inferences']), 1)
        self.assertEqual(len(channel['native_attributes']), 2)
        journal = db.session.get(SupplierCatalogEnrichmentRun, applied['run_id'])
        self.assertEqual(json.loads(journal.selection_json)['inference_review'],
                         {str(self.product.id): [inference_id]})
        with self.assertRaisesRegex(ContractError, 'different_review'):
            apply_batch(sealed, output, **kwargs)

    def test_reparse_cannot_truncate_previously_approved_material_list(self):
        batch, output, _ = self.ozon_case()
        proposal = output['items'][0]['inferences'][0]
        field = db.session.get(MarketplaceAttributeDefinition, proposal['id'])
        field.name, field.is_collection = 'Материал', True
        values = ['Термопластичная резина (TPR)', 'Экокожа']
        dictionary = MarketplaceAttributeValue.query.filter_by(attribute_id=field.id).one()
        dictionary.value, dictionary.value_normalized = values[0], values[0].casefold()
        db.session.add(MarketplaceAttributeValue(marketplace_id=field.marketplace_id,
            product_type_id=field.product_type_id, attribute_id=field.id,
            external_value_id='second-material', value=values[1],
            value_normalized=values[1].casefold()))
        self.source['materials'] = ['TPR', 'Эко кожа']
        self.product.original_data_json = json.dumps(self.source)
        db.session.commit()
        batch['items'][0]['source'] = deepcopy(self.source)
        schema = next(f for f in batch['items'][0]['schema'] if f['id'] == field.id)
        schema.update(name=field.name, max_count=0, allowed_values=values)
        proposal.update(name=field.name, value=values, evidence=[
            {'path': '/materials/0', 'quote': 'TPR'},
            {'path': '/materials/1', 'quote': 'Эко кожа'}])
        kwargs = dict(admin_user_id=self.admin_id, reviewed_product_ids=[self.product.id],
                      approved_inferences={self.product.id: [field.id]})
        apply_batch(prepare_batch(batch), output, **kwargs)
        before = self.product.ai_marketplace_json
        batch['batch_id'] = output['batch_id'] = 'material-subset-reparse'
        proposal['value'] = [values[0]]
        with self.assertRaisesRegex(ContractError, 'complete_field_would_be_truncated'):
            apply_batch(prepare_batch(batch), output, **kwargs)
        self.assertEqual(self.product.ai_marketplace_json, before)
