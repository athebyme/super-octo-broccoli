#!/usr/bin/env python3
"""Offline review of source-bound Luna proposals; never admits or writes WB data."""

from __future__ import annotations

import argparse
from decimal import Decimal
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import unicodedata

VERSION = 'wb-luna-parsing-v1'
MULTICHANNEL_VERSION = 'supplier-luna-parsing-v2'
INFERENCE_NAMES = frozenset({
    'Область использования', 'Назначение товара 18+', 'Тип страпона',
    'Вид стимулятора', 'Пол', 'Основа состава', 'Вибрация',
    'Аромат 18+', 'Вкус презервативов, средств для взрослых', 'Тип', 'Материал',
    'Текстура', 'Бренд',
})
# Source label normalization only; this is not a material classifier.
MATERIAL_LABEL_ALIASES = {
    'Термопластичная резина (TPR)': ('TPR',),
    'ПВХ (поливинилхлорид)': ('ПВХ',),
    'Экокожа': ('Эко кожа',),
    'Искусственная кожа': ('Иск.кожа', 'Иск. кожа'),
}
TEXTURE_LABEL_ALIASES = {'Гелевая': ('гель',), 'Кремовая': ('крем',)}
# Narrow subset of the separately reviewed marketplace draft brand aliases.
BRAND_LABEL_ALIASES = {'Bioritmlab': ('BIORITM',)}
MAX_BYTES = 512 * 1024
MAX_ITEMS = 6
SOURCE_KEYS = frozenset({
    'title', 'description', 'brand', 'category', 'all_categories', 'country',
    'gender', 'colors', 'materials', 'sizes_raw', 'sizes', 'characteristics',
    'dimensions', 'season', 'age_group',
})


class ContractError(ValueError):
    """Only bounded, application-owned codes are exposed in reports."""


def require(condition, code):
    if not condition:
        raise ContractError(code)


def exact_keys(value, keys, code):
    require(isinstance(value, dict) and set(value) == set(keys), code)


def positive_id(value):
    return type(value) is int and value > 0


def _object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 'duplicate_json_key')
        result[key] = value
    return result


def _nonfinite(_value):
    raise ContractError('nonfinite_json')


def finite_number(value):
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _float(value):
    number = float(value)
    require(finite_number(number), 'nonfinite_json')
    return number


def read_json(path):
    # Read at most the limit even when a concurrently written file grows.
    with Path(path).open('rb') as stream:
        raw = stream.read(MAX_BYTES + 1)
    require(len(raw) <= MAX_BYTES, 'file_too_large')
    try:
        value = json.loads(raw, object_pairs_hook=_object, parse_constant=_nonfinite,
                           parse_float=_float)
    except ContractError:
        raise
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ContractError('invalid_json') from exc
    return value, hashlib.sha256(raw).hexdigest()


def pointer(source, path):
    require(isinstance(path, str) and 1 < len(path) <= 500 and path.startswith('/'),
            'invalid_evidence_path')
    value = source
    for token in path[1:].split('/'):
        require(re.search(r'~(?![01])', token) is None, 'invalid_evidence_path')
        token = token.replace('~1', '/').replace('~0', '~')
        if isinstance(value, dict):
            require(token in value, 'evidence_path_not_found')
            value = value[token]
        elif isinstance(value, list):
            require(re.fullmatch(r'0|[1-9][0-9]*', token) is not None,
                    'invalid_evidence_index')
            index = int(token)
            require(index < len(value), 'evidence_path_not_found')
            value = value[index]
        else:
            raise ContractError('evidence_path_not_found')
    require(isinstance(value, str) or type(value) in (int, float),
            'evidence_must_point_to_scalar')
    if type(value) is float:
        require(math.isfinite(value), 'nonfinite_evidence')
    return str(value)


def evidence_quotes(evidence, source):
    require(isinstance(evidence, list) and 1 <= len(evidence) <= 8,
            'missing_or_excessive_evidence')
    quotes = []
    for entry in evidence:
        exact_keys(entry, {'path', 'quote'}, 'invalid_evidence_fields')
        quote = entry['quote']
        require(isinstance(quote, str) and 1 <= len(quote.strip()) <= 3000,
                'invalid_evidence_quote')
        require(quote in pointer(source, entry['path']), 'quote_not_in_source')
        quotes.append(quote)
    return quotes


def normalized(value):
    return ' '.join(unicodedata.normalize('NFKC', str(value)).casefold().split())


def value_in_quote(value, quote):
    # Preserve word and decimal boundaries: 10 is not evidence for 100/10.5.
    needle, haystack = normalized(value), normalized(quote)
    if type(value) in (int, float):
        # Compare complete decimal tokens: 10.0 equals 10, but not 100/10.5.
        tokens = re.findall(r'(?<![\w.,+-])[+-]?\d+(?:[.,]\d+)?(?!\w|[.,]\d)', haystack)
        return any(Decimal(token.replace(',', '.')) == Decimal(str(value)) for token in tokens)
    else:
        pattern = r'(?<!\w)' + re.escape(needle) + r'(?!\w)'
    return bool(needle and re.search(pattern, haystack))


def value_grounded_in_source(value, evidence, source):
    """A clipped quote cannot turn part of a source token into a new value."""
    for entry in evidence:
        quote = entry['quote']
        if not value_in_quote(value, quote):
            continue
        text = pointer(source, entry['path'])
        offset = text.find(quote)
        while offset >= 0:
            # Two adjacent characters preserve signs, decimal separators and
            # word boundaries even if the quote starts/ends inside a token.
            context = text[max(0, offset - 2):offset + len(quote) + 2]
            if value_in_quote(value, context):
                return True
            offset = text.find(quote, offset + 1)
    return False


_MEASURE = re.compile(
    r'(?<![\w.,+-])(?P<value>\d{1,9}(?:[.,]\d{1,6})?)\s*'
    r'(?P<unit>мл|гр|г)(?!\w)')
_MULTIPACK_MASS = re.compile(
    r'(?<![\w.,+-])(?P<count>\d{1,6})\s*(?:шт\.?|штук[аи]?|саше)'
    r'\s+по\s*(?P<value>\d{1,9}(?:[.,]\d{1,6})?)\s*(?:гр|г)(?!\w)')


def _measurements(text):
    if not isinstance(text, str):
        return []
    return [('volume' if m['unit'] == 'мл' else 'mass',
             Decimal(m['value'].replace(',', '.')))
            for m in _MEASURE.finditer(normalized(text))]


def validate_measurement_evidence(field, row, source):
    """Narrow quantity gates; never convert units or calculate a pack total."""
    if row.get('type') != 'number':
        return
    name = row.get('name')
    if name in {'Объем, мл', 'Объем (мл)'} or (
            name == 'Объем' and normalized(row.get('unit')) == 'мл'):
        kind = 'volume'
    elif name in {'Вес товара, г', 'Вес товара без упаковки (г)',
                  'Вес товара без упаковки, г'}:
        kind = 'mass'
    else:
        return
    value = Decimal(str(field['value']))
    require(any((kind, value) in _measurements(e['quote']) for e in field['evidence']),
            'measurement_unit_not_in_evidence')
    title, size = source.get('title'), source.get('sizes_raw')
    title_kinds = {k for k, _ in _measurements(title)}
    size_kinds = {k for k, _ in _measurements(size)}
    require(not (title_kinds == {'mass'} and size_kinds == {'volume'}
                 or title_kinds == {'volume'} and size_kinds == {'mass'}),
            'source_mass_volume_conflict')
    if kind == 'mass':
        for text in (title, size):
            if not isinstance(text, str):
                continue
            for match in _MULTIPACK_MASS.finditer(normalized(text)):
                require(not (Decimal(match['count']) > 1
                             and Decimal(match['value'].replace(',', '.')) == value),
                        'multipack_per_unit_weight_not_total')


def check_issues(value):
    require(isinstance(value, list) and len(value) <= 40, 'invalid_issues')
    require(all(isinstance(code, str) and re.fullmatch(r'[a-z][a-z0-9_]{0,99}', code)
                for code in value), 'invalid_issue_code')


def category_proposal(item, original, categories):
    exact_keys(item, {'product_id', 'supplier_id', 'status', 'category', 'issues'},
               'invalid_category_item_fields')
    if item['status'] == 'needs_review':
        require(item['category'] is None and item['issues'], 'invalid_review_item')
        return 0
    choice = item['category']
    exact_keys(choice, {'subject_id', 'subject_name', 'evidence'},
               'invalid_category_fields')
    require(positive_id(choice['subject_id']), 'invalid_subject_id')
    allowed = categories.get(choice['subject_id'])
    require(allowed is not None, 'subject_outside_reference')
    require(choice['subject_name'] == allowed['subject_name'], 'subject_name_mismatch')
    evidence_quotes(choice['evidence'], original['source'])
    return 1


def characteristic_proposal(item, original, *, partial=False, inferred=False):
    exact_keys(item, {'product_id', 'supplier_id', 'status', 'characteristics', 'issues'},
               'invalid_characteristic_item_fields')
    schema = original.get('schema')
    require(isinstance(schema, list), 'missing_input_schema')
    allowed = {}
    for row in schema:
        require(isinstance(row, dict) and positive_id(row.get('id')),
                'invalid_input_characteristic_id')
        require(row['id'] not in allowed, 'duplicate_input_characteristic_id')
        allowed[row['id']] = row
    values = item['characteristics']
    require(isinstance(values, list) and len(values) <= 100, 'invalid_characteristics')
    if item['status'] == 'needs_review' and not partial:
        require(not values and item['issues'], 'invalid_review_item')
        return 0
    require(bool(values) or partial, 'empty_proposed_characteristics')
    seen = set()
    for field in values:
        keys = {'id', 'name', 'value', 'evidence'} | ({'reason'} if inferred else set())
        exact_keys(field, keys, 'invalid_characteristic_fields')
        require(positive_id(field['id']), 'invalid_characteristic_id')
        require(field['id'] not in seen, 'duplicate_characteristic_id')
        seen.add(field['id'])
        row = allowed.get(field['id'])
        require(row is not None, 'characteristic_outside_schema')
        require(row.get('usable') is True, 'characteristic_reference_unusable')
        require(field['name'] == row.get('name'), 'characteristic_name_mismatch')
        if inferred:
            require(row.get('inference_allowed') is True and row.get('constrained') is True
                    and field['name'] in INFERENCE_NAMES, 'inference_field_not_allowed')
            require(isinstance(field['reason'], str)
                    and 1 <= len(field['reason'].strip()) <= 500, 'invalid_inference_reason')
        value = field['value']
        if row.get('type') == 'number':
            require(finite_number(value), 'invalid_number')
            scalars = [value]
        else:
            require(row.get('type') == 'string_array', 'unsupported_characteristic_type')
            require(isinstance(value, list) and 1 <= len(value) <= 100,
                    'invalid_string_array')
            require(all(isinstance(v, str) and 1 <= len(v.strip()) <= 500 for v in value),
                    'invalid_string_value')
            require(len(set(value)) == len(value), 'duplicate_characteristic_value')
            scalars = value
        max_count = row.get('max_count', 0)
        require(type(max_count) is int and max_count >= 0, 'invalid_input_max_count')
        require(not max_count or len(scalars) <= max_count, 'characteristic_max_count')
        require(type(row.get('constrained')) is bool, 'missing_input_constraint')
        if row['constrained']:
            dictionary = row.get('allowed_values')
            require(isinstance(dictionary, list) and dictionary, 'missing_input_dictionary')
            require(all(v in dictionary for v in scalars), 'value_outside_dictionary')
        quotes = evidence_quotes(field['evidence'], original['source'])
        if inferred and field['name'] == 'Бренд':
            require((original.get('target') or {}).get('marketplace_code') == 'ozon'
                    and row.get('external_id') in ('31', '85') and len(scalars) == 1
                    and all(e['path'] == '/brand' for e in field['evidence']),
                    'brand_normalization_requires_observed_identity')
            require(isinstance(original['source'].get('brand'), str)
                    and normalized(original['source']['brand']) in {
                        normalized(alias) for alias in BRAND_LABEL_ALIASES.get(scalars[0], ())},
                    'brand_normalization_outside_reviewed_labels')
            require(any(normalized(quote) == normalized(original['source']['brand'])
                        for quote in quotes), 'brand_evidence_requires_full_identity')
            characteristics = original['source'].get('characteristics', [])
            if isinstance(characteristics, dict):
                characteristics = [{'name': key, 'value': value}
                                   for key, value in characteristics.items()]
            allowed_brands = {normalized(value) for value in
                              (scalars[0], *BRAND_LABEL_ALIASES[scalars[0]])}
            for observed in characteristics if isinstance(characteristics, list) else []:
                if (isinstance(observed, dict)
                        and normalized(observed.get('name', '')) in {'brand', 'бренд'}):
                    values = observed.get('value')
                    values = values if isinstance(values, list) else [values]
                    require(bool(values) and all(isinstance(value, str)
                            and normalized(value) in allowed_brands for value in values),
                            'observed_brand_identity_conflict')
        if inferred and field['name'] == 'Текстура':
            require((original.get('target') or {}).get('marketplace_code') == 'ozon'
                    and row.get('external_id') == '4552',
                    'texture_normalization_requires_ozon_texture')
            for scalar in scalars:
                require(any(value_grounded_in_source(candidate, field['evidence'],
                                                     original['source'])
                            for candidate in (scalar, *TEXTURE_LABEL_ALIASES.get(scalar, ()))),
                        'texture_normalization_outside_reviewed_labels')
        if inferred and field['name'] == 'Материал':
            require((original.get('target') or {}).get('marketplace_code') == 'ozon'
                    and all(e['path'].startswith('/materials/') for e in field['evidence']),
                    'material_normalization_requires_source_materials')
            for scalar in scalars:
                aliases = MATERIAL_LABEL_ALIASES.get(scalar, ())
                require(any(value_grounded_in_source(candidate, field['evidence'],
                                                     original['source'])
                            for candidate in (scalar, *aliases)),
                        'material_normalization_outside_reviewed_labels')
        if inferred and field['name'] == 'Тип':
            target = original.get('target') or {}
            require(target.get('marketplace_code') == 'ozon'
                    and row.get('external_id') == '8229'
                    and len(scalars) == 1
                    and normalized(scalars[0]) == normalized(target.get('name')),
                    'type_inference_must_match_reviewed_target')
        if inferred and field['name'] in {
                'Аромат 18+', 'Вкус презервативов, средств для взрослых'}:
            markers = ('аромат', 'запах') if field['name'] == 'Аромат 18+' else ('вкус',)
            require(any(marker in normalized(quote) for quote in quotes for marker in markers),
                    'sensory_inference_requires_explicit_source')
        require(inferred or all(value_grounded_in_source(v, field['evidence'],
                                                        original['source']) for v in scalars),
                'value_not_grounded_in_evidence')
        validate_measurement_evidence(field, row, original['source'])
    return len(values)


def validate_batch(batch, output):
    require(isinstance(batch, dict), 'invalid_input')
    exact_keys(output, {'contract_version', 'batch_id', 'items'}, 'invalid_output_fields')
    version = batch.get('contract_version')
    require(version == output['contract_version']
            and version in (VERSION, MULTICHANNEL_VERSION),
            'contract_version_mismatch')
    multichannel = version == MULTICHANNEL_VERSION
    if multichannel:
        require(batch.get('marketplace_code') in ('wb', 'ozon'), 'invalid_marketplace')
        require(batch.get('phase') == 'characteristics', 'invalid_multichannel_phase')
    require(isinstance(batch.get('batch_id'), str) and batch['batch_id'], 'invalid_batch_id')
    require(output['batch_id'] == batch['batch_id'], 'batch_id_mismatch')
    require(batch.get('model') == 'gpt-5.6-luna' and batch.get('reasoning_effort') == 'max',
            'input_model_mismatch')
    # This is an input contract, not proof of which model actually ran.
    phase = batch.get('phase')
    require(phase in ('category', 'characteristics'), 'invalid_phase')
    originals = batch.get('items')
    require(isinstance(originals, list) and 1 <= len(originals) <= MAX_ITEMS,
            'invalid_input_batch_size')
    by_id = {}
    for item in originals:
        require(isinstance(item, dict) and positive_id(item.get('product_id'))
                and positive_id(item.get('supplier_id')), 'invalid_input_identity')
        require(item['product_id'] not in by_id, 'duplicate_input_product_id')
        source = item.get('source')
        require(isinstance(source, dict) and source and set(source) <= SOURCE_KEYS,
                'invalid_source_fields')
        require(not item.get('required_reference_blockers'), 'required_reference_unusable')
        by_id[item['product_id']] = item
    require(len({i['supplier_id'] for i in originals}) == 1, 'mixed_supplier_batch')
    categories = {}
    if phase == 'category':
        refs = batch.get('categories')
        require(isinstance(refs, list) and 1 <= len(refs) <= 500, 'invalid_categories')
        for category in refs:
            require(isinstance(category, dict) and positive_id(category.get('subject_id')),
                    'invalid_reference_subject_id')
            require(category['subject_id'] not in categories, 'duplicate_reference_subject_id')
            require(isinstance(category.get('subject_name'), str) and category['subject_name'],
                    'invalid_reference_subject_name')
            categories[category['subject_id']] = category
    results = output['items']
    require(isinstance(results, list) and len(results) == len(originals), 'item_count_mismatch')
    seen, report = set(), []
    for item in results:
        require(isinstance(item, dict) and positive_id(item.get('product_id')),
                'invalid_product_id')
        pid = item['product_id']
        require(pid not in seen and pid in by_id, 'duplicate_or_foreign_product_id')
        seen.add(pid)
        original = by_id[pid]
        require(positive_id(item.get('supplier_id'))
                and item['supplier_id'] == original['supplier_id'], 'supplier_scope_mismatch')
        require(item.get('status') in ('proposed', 'needs_review'), 'invalid_status')
        check_issues(item.get('issues'))
        inference_count = 0
        if multichannel:
            exact_keys(item, {'product_id', 'supplier_id', 'status', 'characteristics',
                              'inferences', 'issues'}, 'invalid_multichannel_item_fields')
            require(item['status'] != 'needs_review' or item['issues'], 'invalid_review_item')
            observed = {k: v for k, v in item.items() if k != 'inferences'}
            count = characteristic_proposal(observed, original, partial=True)
            inferred = dict(observed, characteristics=item['inferences'])
            inference_count = characteristic_proposal(inferred, original, partial=True,
                                                       inferred=True)
            ids = [f['id'] for f in item['characteristics'] + item['inferences']]
            require(len(set(ids)) == len(ids), 'duplicate_characteristic_id')
            require(count + inference_count > 0 or item['status'] == 'needs_review',
                    'empty_proposed_characteristics')
        else:
            count = (category_proposal(item, original, categories) if phase == 'category'
                     else characteristic_proposal(item, original))
        report.append({'product_id': pid, 'supplier_id': item['supplier_id'],
                       'status': item['status'], 'proposal_count': count,
                       'inference_count': inference_count,
                       'issues': item['issues'], 'orchestrator_review_required': True})
    return {'contract_version': version, 'batch_id': batch['batch_id'], 'phase': phase,
            'valid': True, 'ready_for_wb': False, 'applied': False, 'items': report,
            'proposed': sum(i['status'] == 'proposed' for i in report),
            'needs_review': sum(i['status'] == 'needs_review' for i in report),
            'proposal_count': sum(i['proposal_count'] for i in report),
            'inference_count': sum(i['inference_count'] for i in report)}


def write_report(path, report):
    target = Path(path)
    # Caller chooses the private directory; atomic replacement never follows a
    # destination symlink and leaves a 0600 file after both create and replace.
    fd, temp_path = tempfile.mkstemp(prefix='.luna-validation-', dir=target.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write('\n')
        os.replace(temp_path, target)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--report', required=True, type=Path)
    args = parser.parse_args()
    if args.report.resolve() in {args.input.resolve(), args.output.resolve()}:
        print(json.dumps({'valid': False, 'ready_for_wb': False, 'applied': False,
                          'error_code': 'report_overwrites_input_or_output'}))
        return 1
    hashes = {}
    try:
        batch, hashes['input_sha256'] = read_json(args.input)
        output, hashes['output_sha256'] = read_json(args.output)
        report = validate_batch(batch, output)
    except (ContractError, OSError) as exc:
        report = {'valid': False, 'ready_for_wb': False, 'applied': False,
                  'error_code': str(exc) if isinstance(exc, ContractError) else 'file_error'}
    report.update(hashes)
    write_report(args.report, report)
    print(json.dumps({k: v for k, v in report.items() if k != 'items'}, ensure_ascii=False))
    return 0 if report['valid'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
