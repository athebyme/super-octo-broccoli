"""Audited, local admission of source-bound Codex Luna parsing results.

No model, marketplace transport, scheduler, or seller-owned writes live here.
The existing enrichment journal and rollback snapshot format are reused.
"""

from copy import deepcopy
from datetime import datetime
import hashlib
import json
import uuid

from sqlalchemy import text, update

from models import (
    db, User, SupplierProduct, SupplierCatalogEnrichmentRun,
    SupplierCatalogEnrichmentItem, Marketplace, MarketplaceCategory,
    MarketplaceCategoryCharacteristic, MarketplaceProductType,
    MarketplaceAttributeDefinition, MarketplaceAttributeValue,
)
from scripts.validate_luna_parsing import (
    SOURCE_KEYS, MAX_ITEMS, ContractError, check_issues, normalized, positive_id,
    require, validate_batch,
)
from services.supplier_catalog_enrichment import (
    SupplierCatalogEnrichmentService, _enrichment_state,
)
from services.marketplace_validator import (
    validate_wb_characteristics, get_wb_characteristic_constraint,
    _allowed_values_for_characteristic,
)
from services.ozon_reference_service import OzonReferenceService
from services.marketplace_drafts import MarketplaceDraftService

DRIVER = 'codex_luna_v2'
STATE_COLUMNS = (
    'wb_subject_id', 'wb_subject_name', 'wb_category_name', 'category_confidence',
    'ai_marketplace_json', 'marketplace_fields_json',
    'marketplace_validation_status', 'marketplace_fill_pct', 'content_revision',
)
PACKAGE_CHARACTERISTIC_IDS = frozenset({88952, 90745, 90846, 90849})


def dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


def fingerprint(value):
    return hashlib.sha256(dump(value).encode()).hexdigest()


def source_snapshot(product):
    try:
        original = json.loads(product.original_data_json or 'null')
    except (ValueError, TypeError):
        original = None
    require(isinstance(original, dict) and original, 'source_snapshot_missing')
    source = {key: original[key] for key in SOURCE_KEYS
              if key in original and original[key] not in (None, '', [], {})}
    require(bool(source), 'source_snapshot_empty')
    return source


def source_matches(product, metadata):
    try:
        return fingerprint(source_snapshot(product)) == metadata.get('source_sha256')
    except ContractError:
        return False


def _admin(admin_user_id):
    require(positive_id(admin_user_id), 'invalid_admin_id')
    admin = db.session.get(User, admin_user_id)
    require(admin is not None and admin.is_active and admin.is_admin,
            'active_admin_required')


def wb_parsing_reference(subject_id):
    """Build a source parser schema from usable current WB types and values."""
    require(positive_id(subject_id), 'invalid_subject_id')
    require(validate_wb_characteristics(subject_id, [])['valid'], 'wb_reference_unusable')
    mp = Marketplace.query.filter_by(code='wb').one()
    category = MarketplaceCategory.query.filter_by(
        marketplace_id=mp.id, subject_id=subject_id).one()
    definitions = MarketplaceCategoryCharacteristic.query.filter_by(
        category_id=category.id, is_available=True,
    ).order_by(MarketplaceCategoryCharacteristic.charc_id).all()
    schema, blocked, cache = [], [], {}
    for field in definitions:
        if not (field.is_enabled or field.required):
            continue
        code = None
        if field.charc_type not in (1, 4):
            code = 'unused_characteristic' if field.charc_type == 0 else 'unsupported_type'
        elif field.charc_id in PACKAGE_CHARACTERISTIC_IDS:
            code = 'package_dimensions_separate_deterministic_flow'
        if code is None:
            allowed, _, issue = _allowed_values_for_characteristic(mp, field, cache)
            code = issue['code'] if issue else None
        if code:
            blocked.append({'id': field.charc_id, 'name': field.name,
                            'required': field.required, 'code': code})
            continue
        schema.append({
            'id': field.charc_id, 'name': field.name,
            'type': 'number' if field.charc_type == 4 else 'string_array',
            'unit': field.unit_name, 'required': field.required,
            'max_count': field.max_count, 'usable': True,
            'constrained': allowed is not None, 'allowed_values': allowed or [],
            'allowed_values_truncated': False, 'inference_allowed': False,
        })
    return {
        'subject_id': subject_id, 'subject_name': category.subject_name,
        'schema': schema, 'schema_hash': category.characteristics_schema_hash,
        'blocked_fields': blocked,
        'required_reference_blockers': [field['code'] for field in blocked
            if field['required'] and field['code'] != 'package_dimensions_separate_deterministic_flow'],
    }


def _reference(original, code):
    """Read current provider-owned schema, including effective dictionary state."""
    if code == 'wb':
        subject_id = original.get('subject_id')
        require(positive_id(subject_id), 'invalid_subject_id')
        checked = validate_wb_characteristics(subject_id, [])
        require(checked['valid'], 'wb_reference_unusable')
        mp = Marketplace.query.filter_by(code='wb').one()
        category = MarketplaceCategory.query.filter_by(
            marketplace_id=mp.id, subject_id=subject_id).one()
        require(category.subject_name == original.get('subject_name'), 'subject_name_drift')
        require(category.characteristics_schema_hash == original.get('schema_hash'),
                'schema_drift')
        definitions = [field for field in MarketplaceCategoryCharacteristic.query.filter_by(
            category_id=category.id, is_available=True,
        ).order_by(MarketplaceCategoryCharacteristic.charc_id).all()
            if field.is_enabled or field.required]
        cache = {}
        reference = {
            'target': {'subject_id': subject_id, 'name': category.subject_name},
            'schema_hash': category.characteristics_schema_hash,
            'category_hash': mp.categories_snapshot_hash,
            'constraints': [{
                'id': field.charc_id, 'name': field.name,
                'type': field.charc_type, 'required': field.required,
                'max_count': field.max_count, 'unit': field.unit_name,
                'constraint': get_wb_characteristic_constraint(mp, field, cache),
            } for field in definitions],
        }
        return reference, category, definitions
    require(code == 'ozon', 'invalid_marketplace')
    target = original.get('target') or {}
    require(positive_id(target.get('product_type_id')), 'invalid_product_type_id')
    product_type = db.session.get(MarketplaceProductType, target['product_type_id'])
    require(product_type is not None and product_type.marketplace.code == 'ozon'
            and OzonReferenceService.tree_is_fresh(product_type.marketplace)
            and OzonReferenceService.reference_is_fresh(product_type),
            'ozon_reference_unusable')
    require(product_type.is_seller_selectable, 'ozon_type_not_selectable')
    require(target == {
        'marketplace_code': 'ozon', 'product_type_id': product_type.id,
        'type_id': product_type.external_type_id,
        'description_category_id': product_type.category.external_category_id,
        'name': product_type.name, 'full_path': product_type.category.full_path,
    }, 'ozon_target_drift')
    require(product_type.attributes_schema_hash == original.get('schema_hash'),
            'schema_drift')
    definitions = MarketplaceAttributeDefinition.query.filter_by(
        product_type_id=product_type.id, is_available=True,
    ).order_by(MarketplaceAttributeDefinition.id).all()
    require(all(not field.is_required or not field.dictionary_id
                or OzonReferenceService.dictionary_is_fresh(field)
                for field in definitions), 'required_dictionary_unusable')
    return {
        'target': target, 'schema_hash': product_type.attributes_schema_hash,
        'dictionaries': [{
            'id': field.id, 'enabled': field.is_enabled,
            'hash': field.values_snapshot_hash,
            'restrictions': field.restriction_value_ids,
        } for field in definitions if field.dictionary_id],
    }, product_type, definitions


def prepare_batch(batch):
    """Seal the exact current state after root's review; performs no writes."""
    batch = deepcopy(batch)
    require(batch.get('phase') == 'characteristics', 'characteristics_phase_required')
    require(isinstance(batch.get('items'), list)
            and 1 <= len(batch['items']) <= MAX_ITEMS, 'invalid_input_batch_size')
    code = batch.get('marketplace_code', 'wb')
    for item in batch['items']:
        require(positive_id(item.get('product_id'))
                and positive_id(item.get('supplier_id')), 'invalid_input_identity')
        product = SupplierProduct.query.filter_by(
            id=item['product_id'], supplier_id=item['supplier_id']).first()
        require(product is not None, 'supplier_scope_mismatch')
        require(source_snapshot(product) == item.get('source'), 'source_drift')
        reference, _, _ = _reference(item, code)
        item['admission'] = {
            'source_sha256': fingerprint(item['source']),
            'state_sha256': fingerprint(_enrichment_state(product)),
            'reference_sha256': fingerprint(reference),
            'expected_revision': product.content_revision,
        }
    return batch


def _ozon_values(product_type, definitions, fields):
    by_id = {field.id: field for field in definitions}
    attributes = []
    for proposal in fields:
        field = by_id.get(proposal['id'])
        require(field is not None and field.is_enabled, 'attribute_outside_schema')
        require(field.name == proposal['name'], 'attribute_name_drift')
        require(not field.attribute_complex_id, 'complex_attribute_requires_review')
        scalars = proposal['value'] if isinstance(proposal['value'], list) else [proposal['value']]
        values = []
        for value in scalars:
            if field.dictionary_id:
                require(OzonReferenceService.dictionary_is_fresh(field),
                        'dictionary_unusable')
                query = MarketplaceAttributeValue.query.filter_by(
                    product_type_id=product_type.id, attribute_id=field.id,
                    is_available=True, value=value,
                )
                if field.restriction_value_ids:
                    query = query.filter(MarketplaceAttributeValue.external_value_id.in_(
                        field.restriction_value_ids))
                matches = query.limit(2).all()
                require(len(matches) == 1, 'dictionary_value_missing_or_ambiguous')
                values.append({'dictionary_value_id': matches[0].external_value_id,
                               'value': matches[0].value})
            else:
                values.append({'value': str(value)})
        attributes.append({'attribute_id': field.external_attribute_id,
                           'complex_id': '0', 'values': values})
    errors = []
    MarketplaceDraftService._validate_attributes(
        product_type=product_type, attributes=attributes, complex_groups=[], errors=errors,
    )
    require(all(error['code'] == 'required_attribute_missing' for error in errors),
            'ozon_attribute_validation_failed')
    return attributes, errors


def _channel_result(original, result, code, *, approved_inference_ids=()):
    reference, target, definitions = _reference(original, code)
    require(fingerprint(reference) == original['admission']['reference_sha256'],
            'reference_drift')
    fields = result['characteristics']
    inferences = result.get('inferences', [])
    approved_ids = set(approved_inference_ids)
    approved = [field for field in inferences if field['id'] in approved_ids]
    require({field['id'] for field in approved} == approved_ids, 'inference_review_outside_result')
    inferences = [field for field in inferences if field['id'] not in approved_ids]
    admitted = fields + approved
    if code == 'wb':
        checked = validate_wb_characteristics(target.subject_id, [
            {'id': field['id'], 'value': field['value']} for field in admitted])
        require(checked['valid'], 'wb_characteristic_validation_failed')
        native = checked['normalized']
        # Inferences are checked against current schema too, but stay quarantined.
        if inferences:
            inferred = validate_wb_characteristics(target.subject_id, [
                {'id': field['id'], 'value': field['value']} for field in inferences])
            require(inferred['valid'], 'wb_inference_validation_failed')
        present = {field['id'] for field in admitted}
        missing = [{'code': 'required_attribute_missing', 'name': field.name}
                   for field in definitions if field.required and field.charc_id not in present]
    else:
        native, missing = _ozon_values(target, definitions, admitted)
        if inferences:
            _ozon_values(target, definitions, admitted + inferences)
    return {
        'target': reference['target'], 'schema_hash': reference['schema_hash'],
        'source_sha256': original['admission']['source_sha256'],
        'fields': fields, 'native_attributes': native, 'inferences': inferences,
        'approved_inferences': approved,
        'missing_required': missing, 'issues': result['issues'],
        'status': 'partial' if missing or inferences or result['issues'] else 'attributes_validated',
        'ready_for_publication': False,
        'filled_count': len(admitted),
        'schema_field_count': sum(field.charc_type in (1, 4) for field in definitions)
                             if code == 'wb' else len(definitions),
        'model': 'gpt-5.6-luna', 'reasoning_effort': 'max',
    }, target


def apply_batch(batch, output, *, admin_user_id, reviewed_product_ids,
                approved_inferences=None, field_rejections=None):
    """All-or-nothing CAS admission of one reviewed chunk, at most six products."""
    validate_batch(batch, output)
    require(all(item['characteristics'] or item.get('inferences') for item in output['items']),
            'no_fields_to_admit')
    _admin(admin_user_id)
    require(batch.get('phase') == 'characteristics', 'characteristics_phase_required')
    ids = [item['product_id'] for item in batch['items']]
    approved_inferences = {} if approved_inferences is None else approved_inferences
    require(isinstance(approved_inferences, dict), 'invalid_inference_review')
    output_by_id = {item['product_id']: item for item in output['items']}
    for product_id, field_ids in approved_inferences.items():
        require(positive_id(product_id) and product_id in output_by_id,
                'inference_review_foreign_product')
        require(isinstance(field_ids, list) and all(positive_id(value) for value in field_ids)
                and len(set(field_ids)) == len(field_ids), 'invalid_inference_review')
        available = {field['id'] for field in output_by_id[product_id].get('inferences', [])}
        require(set(field_ids) <= available, 'inference_review_outside_result')
    review = {str(key): sorted(value) for key, value in approved_inferences.items() if value}
    field_rejections = {} if field_rejections is None else field_rejections
    require(isinstance(field_rejections, dict), 'invalid_field_rejection_review')
    for product_id, rejected in field_rejections.items():
        require(positive_id(product_id) and product_id in output_by_id,
                'field_rejection_foreign_product')
        require(isinstance(rejected, dict) and 1 <= len(rejected) <= 100
                and all(positive_id(key) for key in rejected), 'invalid_field_rejection_review')
        for reason in rejected.values():
            check_issues([reason])
        incoming_ids = {f['id'] for f in output_by_id[product_id]['characteristics']
                        + output_by_id[product_id].get('inferences', [])}
        require(not incoming_ids.intersection(rejected), 'rejected_field_in_new_result')
    rejection_review = {str(pid): {str(fid): reason for fid, reason in fields.items()}
                        for pid, fields in field_rejections.items()}
    require(isinstance(reviewed_product_ids, list)
            and all(positive_id(value) for value in reviewed_product_ids)
            and len(reviewed_product_ids) == len(ids)
            and set(reviewed_product_ids) == set(ids), 'exact_root_review_required')
    supplier_id = batch['items'][0]['supplier_id']
    code = batch.get('marketplace_code', 'wb')
    run_id = str(uuid.uuid5(uuid.NAMESPACE_URL,
                          f'{DRIVER}:{supplier_id}:{code}:{batch["batch_id"]}'))
    output_hash = fingerprint(output)
    with SupplierCatalogEnrichmentService._supplier_lock(supplier_id) as acquired:
        require(acquired, 'supplier_busy')
        try:
            require(not db.session.new and not db.session.dirty and not db.session.deleted,
                    'admission_requires_clean_session')
            if db.engine.dialect.name == 'sqlite':
                # Reserve the short local transaction before checking references:
                # a concurrent reference refresh cannot slip between validation
                # and the product CAS. No network/filesystem I/O follows here.
                connection = db.session.connection().connection.driver_connection
                require(not connection.in_transaction, 'admission_requires_fresh_transaction')
                db.session.execute(text('BEGIN IMMEDIATE'))
            db.session.expire_all()
            _admin(admin_user_id)
            prior = db.session.get(SupplierCatalogEnrichmentRun, run_id)
            if prior:
                require(json.loads(prior.selection_json).get('output_sha256') == output_hash,
                        'batch_id_reused_with_different_output')
                require(json.loads(prior.selection_json).get('inference_review', {}) == review,
                        'batch_id_reused_with_different_review')
                require(json.loads(prior.selection_json).get('field_rejection_review', {})
                        == rejection_review, 'batch_id_reused_with_different_rejections')
                require(all(item.status == 'applied' for item in prior.items.all()),
                        'batch_previously_rolled_back')
                report = {'run_id': run_id, 'applied': 0, 'reused': prior.applied,
                          'ready_for_publication': False}
                db.session.rollback()
                return report
            require(not SupplierCatalogEnrichmentRun.query.filter(
                SupplierCatalogEnrichmentRun.supplier_id == supplier_id,
                SupplierCatalogEnrichmentRun.status.in_(('pending', 'running', 'cancelling')),
            ).first(), 'supplier_run_active')
            now = datetime.utcnow()
            run = SupplierCatalogEnrichmentRun(
                id=run_id, supplier_id=supplier_id, admin_user_id=admin_user_id,
                mode='category_and_characteristics', status='completed',
                selection_json=dump({
                    'driver': DRIVER, 'batch_id': batch['batch_id'], 'marketplace_code': code,
                    'input_sha256': fingerprint(batch), 'output_sha256': output_hash,
                    'reviewed_product_ids': reviewed_product_ids, 'reasoning_effort': 'max',
                    'inference_review': review,
                    'field_rejection_review': rejection_review,
                    'external_provider_usage': None, 'physical_requests_known': False,
                }), model_used='gpt-5.6-luna', total=len(ids), processed=len(ids),
                applied=len(ids), llm_calls=0, llm_call_limit=0,
                started_at=now, completed_at=now,
            )
            # Terminal runs cannot be picked up by the legacy LLM scheduler.
            db.session.add(run)
            by_id = {item['product_id']: item for item in output['items']}
            for ordinal, original in enumerate(batch['items']):
                product = SupplierProduct.query.filter_by(
                    id=original['product_id'], supplier_id=supplier_id).first()
                require(product is not None, 'supplier_scope_mismatch')
                admission = original.get('admission') or {}
                require(source_snapshot(product) == original['source'], 'source_drift')
                require(fingerprint(original['source']) == admission.get('source_sha256'),
                        'source_seal_missing')
                before = _enrichment_state(product)
                require(fingerprint(before) == admission.get('state_sha256')
                        and product.content_revision == admission.get('expected_revision'),
                        'target_state_drift')
                old = before.get('ai_marketplace_json') or {}
                meta = old.get('_meta', {}) if isinstance(old, dict) else {}
                trusted = (isinstance(meta, dict) and meta.get('workflow') == DRIVER
                           and meta.get('source_sha256') == admission['source_sha256'])
                data = deepcopy(old) if trusted else {}
                channels = deepcopy(meta.get('channels', {})) if trusted else {}
                candidate = deepcopy(by_id[product.id])
                previous = channels.get(code, {})
                approved_ids = set(approved_inferences.get(product.id, []))
                target_key = 'subject_id' if code == 'wb' else 'product_type_id'
                target_id = (original.get('subject_id') if code == 'wb'
                             else original['target']['product_type_id'])
                rejected = field_rejections.get(product.id, {})
                withdrawals = {entry['field_id']: entry
                               for entry in previous.get('rejected_fields', [])}
                incoming_ids = {f['id'] for f in candidate['characteristics']
                                + candidate.get('inferences', [])}
                require(not incoming_ids.intersection(withdrawals),
                        'previously_rejected_field_reintroduced')
                if rejected:
                    require(trusted and previous.get('schema_hash') == original.get('schema_hash')
                            and previous.get('target', {}).get(target_key) == target_id,
                            'field_rejection_requires_current_channel')
                    existing = {field['id']: (lane, field) for lane in
                                ('fields', 'approved_inferences', 'inferences')
                                for field in previous.get(lane, [])}
                    require(set(rejected) <= set(existing), 'field_rejection_not_in_channel')
                    for field_id, reason in rejected.items():
                        lane, field = existing[field_id]
                        withdrawals[field_id] = {
                            'field_id': field_id, 'field': deepcopy(field), 'lane': lane,
                            'reason': reason, 'reviewed_by': admin_user_id, 'run_id': run_id,
                            'source_sha256': admission['source_sha256'],
                        }
                    # Explicit root withdrawal is the only exception to omission-preserves.
                    previous = deepcopy(previous)
                    for lane in ('fields', 'approved_inferences', 'inferences'):
                        previous[lane] = [f for f in previous.get(lane, [])
                                          if f['id'] not in rejected]
                require(len(withdrawals) <= 100, 'field_rejection_history_limit')
                if (previous.get('schema_hash') == original.get('schema_hash')
                        and previous.get('target', {}).get(target_key) == target_id):
                    # A second parsing pass may add facts, but an omission does
                    # not erase previously admitted facts from the same source.
                    merged = {field['id']: field for field in previous.get('fields', [])}
                    prior_admitted = {field['id']: field for field in
                                      previous.get('fields', [])
                                      + previous.get('approved_inferences', [])}
                    new_admitted = candidate['characteristics'] + [
                        field for field in candidate.get('inferences', [])
                        if field['id'] in approved_ids]
                    for field in new_admitted:
                        prior_field = prior_admitted.get(field['id'])
                        if (prior_field and normalized(field['name']) in {
                                'материал', 'материал изделия', 'состав', 'комплектация'}
                                and isinstance(field['value'], list)
                                and isinstance(prior_field['value'], list)):
                            require(not {normalized(v) for v in field['value']}
                                    < {normalized(v) for v in prior_field['value']},
                                    'complete_field_would_be_truncated')
                    for field in candidate['characteristics']:
                        merged[field['id']] = field
                    candidate['characteristics'] = list(merged.values())
                    old_approved = {field['id']: field
                                    for field in previous.get('approved_inferences', [])}
                    for field in candidate.get('inferences', []):
                        if field['id'] in old_approved and field['id'] not in approved_ids:
                            require(field['value'] == old_approved[field['id']]['value'],
                                    'approved_inference_changed_requires_review')
                    inferred = {field['id']: field for field in previous.get('inferences', [])}
                    inferred.update(old_approved)
                    inferred.update({field['id']: field for field in candidate.get('inferences', [])})
                    candidate['inferences'] = [field for key, field in inferred.items()
                                               if key not in merged]
                    approved_ids.update(old_approved)
                    approved_ids.difference_update(merged)
                result, target = _channel_result(original, candidate, code,
                                                approved_inference_ids=approved_ids)
                if withdrawals:
                    result['rejected_fields'] = list(withdrawals.values())
                    if 'reviewed_fields_withdrawn' not in result['issues']:
                        require(len(result['issues']) < 40, 'issue_budget_exceeded')
                        result['issues'] = result['issues'] + ['reviewed_fields_withdrawn']
                    result['status'] = 'partial'
                channels[code] = dict(result, run_id=run_id, reviewed_by=admin_user_id)
                if code == 'wb':
                    data = {field['name']: field['value'] for field in
                            result['fields'] + result['approved_inferences']}
                data['_meta'] = {
                    'source': 'supplier_catalog_enrichment', 'workflow': DRIVER,
                    'model': 'gpt-5.6-luna', 'reasoning_effort': 'max',
                    'source_sha256': admission['source_sha256'], 'channels': channels,
                }
                values = {'ai_marketplace_json': dump(data),
                          'content_revision': int(product.content_revision or 1) + 1}
                if code == 'wb':
                    values.update(
                        wb_subject_id=target.subject_id, wb_subject_name=target.subject_name,
                        wb_category_name=target.subject_name, category_confidence=None,
                        marketplace_fields_json=dump({
                            k: '; '.join(str(value) for value in v) if isinstance(v, list) else v
                            for k, v in data.items() if not k.startswith('_')}),
                        marketplace_validation_status='partial',
                        marketplace_fill_pct=(100 * result['filled_count'] /
                                              max(1, result['schema_field_count'])),
                    )
                elif not trusted:
                    values.update(marketplace_fields_json='{}',
                                  marketplace_validation_status='partial', marketplace_fill_pct=0)
                conditions = [SupplierProduct.id == product.id,
                              SupplierProduct.supplier_id == supplier_id,
                              SupplierProduct.original_data_json == product.original_data_json]
                conditions.extend(getattr(SupplierProduct, name) == getattr(product, name)
                                  for name in STATE_COLUMNS)
                changed = db.session.execute(update(SupplierProduct).where(*conditions)
                                             .values(**values),
                                             execution_options={'synchronize_session': False})
                require(changed.rowcount == 1, 'concurrent_product_change')
                db.session.refresh(product)
                db.session.add(SupplierCatalogEnrichmentItem(
                    run_id=run_id, supplier_product_id=product.id, ordinal=ordinal,
                    phase='done', status='applied', attempt_count=1,
                    source_fingerprint=admission['source_sha256'],
                    proposed_subject_id=product.wb_subject_id if code == 'wb' else None,
                    proposed_subject_name=product.wb_subject_name if code == 'wb' else None,
                    before_json=dump(before), after_json=dump(_enrichment_state(product)),
                    reference_json=dump(result), evidence=dump(result['fields']),
                    inference_json=dump(result['inferences'] + result['approved_inferences']),
                    category_changed=before['wb_subject_id'] != product.wb_subject_id,
                    characteristics_changed=True, applied_revision=product.content_revision,
                    started_at=now, completed_at=now,
                ))
            db.session.commit()
            return {'run_id': run_id, 'applied': len(ids), 'reused': 0,
                    'ready_for_publication': False}
        except Exception:
            db.session.rollback()
            raise
