"""Exact committed identity determines the proposed scope, never current edits."""
import json
import pytest

from services.ozon_quarantine_scope import operation_scope, media_scope, incoming_scope, MAX_DOCUMENT_BYTES


def documents(kind='product_import'):
    summary = {'offer_id':'Артикул-1'}
    submitted = {'items':[{'offer_id':'Артикул-1', 'name':'Not identity evidence'}]}
    before = {'offer_id':'Артикул-1', 'exists':False, 'items':[]}
    if kind in ('product_update','product_update_rollback'):
        summary['external_product_id'] = '123'
        before = {'identity':{'offer_id':'Артикул-1','product_id':'123'},
                  'payload':{'items':[{'offer_id':'Артикул-1'}]}}
    elif kind == 'product_import_rollback':
        summary['external_product_id'] = '123'
        before = {'identity':{'offer_id':'Артикул-1','product_id':'123'}}
        submitted = {'product_id':[123]}
    elif kind in ('price_update','price_rollback','stock_update','stock_rollback'):
        before = {'offer_id':'Артикул-1','product_id':'123','price':'100','warehouse_id':'7001'}
        submitted = {**before, 'price':'200'}
        summary.update(before=before.copy(), proposed=submitted.copy(), warehouse_id=1)
    return summary, submitted, before


def scope(kind='product_import', changes=None):
    summary, submitted, before = documents(kind)
    if changes:
        changes(summary, submitted, before)
    return operation_scope(kind=kind, summary_json=json.dumps(summary),
        submitted_json=json.dumps(submitted), before_json=json.dumps(before))


@pytest.mark.parametrize('kind', ['product_import','product_update','product_update_rollback',
    'product_import_rollback','price_update','price_rollback','stock_update','stock_rollback'])
def test_all_current_physical_write_kinds_use_immutable_identity(kind):
    result = scope(kind)
    assert result.kind == 'product'
    assert result.offer_id == 'Артикул-1'
    assert result.product_id == (None if kind == 'product_import' else '123')
    assert result.reason_code == 'immutable_target_verified'
    assert result.document()['includes_all_warehouses']


@pytest.mark.parametrize('kind', ['product_import','product_update','product_update_rollback',
    'product_import_rollback','price_update','price_rollback','stock_update','stock_rollback'])
def test_incoming_queued_target_does_not_require_completed_preflight(kind):
    summary, submitted, _before = documents(kind)
    incoming = incoming_scope(kind=kind, summary_json=json.dumps(summary), submitted_json=json.dumps(submitted))
    assert incoming == scope(kind)


@pytest.mark.parametrize('kind', ['product_import','product_update','product_update_rollback',
    'product_import_rollback','price_update','price_rollback','stock_update','stock_rollback'])
def test_incoming_conflicting_target_cannot_exclude_an_active_fence(kind):
    summary, submitted, _before = documents(kind)
    if kind == 'product_import_rollback':
        summary['external_product_id'] = '456'
    else:
        summary['offer_id'] = 'DIFFERENT'
    incoming = incoming_scope(kind=kind, summary_json=json.dumps(summary), submitted_json=json.dumps(submitted))
    assert incoming.kind == 'account'
    assert incoming.reason_code == 'identity_conflict'


@pytest.mark.parametrize('kind', ['future_write','product_import','price_update','stock_update'])
def test_unknown_incoming_target_has_no_product_exemption(kind):
    assert incoming_scope(kind=kind, summary_json='{}', submitted_json='{}').kind == 'account'


@pytest.mark.parametrize('kind', ['product_import','product_update','product_update_rollback',
    'product_import_rollback','price_update','price_rollback','stock_update','stock_rollback'])
def test_disagreeing_summary_cannot_choose_one_of_the_targets(kind):
    result = scope(kind, lambda summary, submitted, before: summary.update(offer_id='Другой-товар'))
    assert (result.kind, result.offer_id, result.product_id) == ('account',None,None)
    assert result.reason_code == 'identity_conflict'


@pytest.mark.parametrize('value', [None,'',False,1,' x','x ','x\n','x\u202e','x\x00','x'*201])
def test_invalid_or_invisible_offer_proposes_explicit_wide_scope(value):
    result = scope(changes=lambda summary, submitted, before: summary.update(offer_id=value))
    assert result.kind == 'account'
    assert result.reason_code == 'identity_unknown'


@pytest.mark.parametrize('value', [None,False,0,-1,1.1,'0','-1','12x',' 123','1e3','١٢٣','9'*101])
def test_product_ids_are_strict_and_never_guessed(value):
    result = scope('product_update', lambda summary, submitted, before: summary.update(external_product_id=value))
    assert result.kind == 'account'
    assert result.reason_code == 'identity_unknown'


@pytest.mark.parametrize('field', ['summary_json','submitted_json','before_json'])
@pytest.mark.parametrize('raw', ['null','[]','{"offer_id":"one","offer_id":"two"}',
    '{"value":NaN}', '{"value":Infinity}', '[', '"secret-that-must-not-appear"', None])
def test_unknown_documents_fail_closed_without_echoing_body(field, raw):
    summary, submitted, before = documents()
    args = dict(kind='product_import',summary_json=json.dumps(summary),submitted_json=json.dumps(submitted),
                before_json=json.dumps(before))
    args[field] = raw
    result = operation_scope(**args)
    assert result.kind == 'account' and result.reason_code == 'document_invalid'
    assert 'secret-that-must-not-appear' not in json.dumps(result.document())


def test_document_size_and_recursion_are_bounded():
    for raw in (' '*(MAX_DOCUMENT_BYTES+1), json.dumps({'padding':'я'*MAX_DOCUMENT_BYTES},ensure_ascii=False),
                '{"deep":'+'['*2000+'0'+']'*2000+'}'):
        result = operation_scope(kind='product_import',summary_json=raw,submitted_json='{}',before_json='{}')
        assert result.kind == 'account' and result.reason_code == 'document_invalid'


@pytest.mark.parametrize('bad_payload', [None,[],{'items':[]},{'items':[{},{}]},{'items':[1]}])
def test_update_requires_reconstructable_before_identity(bad_payload):
    result = scope('product_update', lambda summary, submitted, before: before.update(payload=bad_payload))
    assert result.kind == 'account'


@pytest.mark.parametrize('before_change', [{'exists':True},{'items':[{'product_id':123}]},{'exists':None}])
def test_create_scope_requires_committed_absence_shape(before_change):
    result = scope(changes=lambda summary, submitted, before: before.update(before_change))
    assert result.kind == 'account'


def test_commercial_before_and_proposed_identity_must_agree_in_every_document():
    result = scope('price_update', lambda summary, submitted, before: summary['proposed'].update(product_id=456))
    assert result.reason_code == 'identity_conflict'
    assert scope('stock_update').kind == 'product'  # local warehouse FK != provider ID is valid


def test_mutable_names_and_non_identity_fields_never_choose_the_scope():
    result = scope(changes=lambda summary, submitted, before: summary.update(
        current_draft={'offer_id':'current-edit','product_id':999}, title='Changed title',
        api_key='synthetic-secret-must-not-echo', raw_provider_body={'other_product_id':999}))
    assert result == scope()
    assert 'synthetic-secret' not in json.dumps(result.document())


def test_multi_target_archive_never_picks_the_first_item():
    result = scope('product_import_rollback', lambda summary, submitted, before: submitted.update(product_id=[123,456]))
    assert result.kind == 'account'


def test_unknown_operation_kind_is_not_silently_treated_as_create():
    result = operation_scope(kind='future-write',summary_json='{}',submitted_json='{}',before_json='{}')
    assert result.kind == 'account' and result.reason_code == 'unsupported_kind'


def test_review_token_binds_exact_origin_owner_account_revision_and_scope():
    context = dict(seller_id=1,marketplace_id=2,account_id=3,origin_type='operation',origin_id=4,version=5)
    first = scope().review_token(**context)
    assert len(first) == 64 and first == scope().review_token(**context)
    for key in ('seller_id','marketplace_id','account_id','origin_id','version'):
        assert scope().review_token(**{**context,key:context[key]+1}) != first
        with pytest.raises(ValueError):
            scope().review_token(**{**context,key:True})
    assert scope().review_token(**{**context,'origin_type':'media_operation'}) != first
    assert scope('price_update').review_token(**context) != first


def test_media_identity_does_not_enable_ozon_write_and_requires_same_account():
    target = dict(entity_kind='marketplace_listing',marketplace_code='ozon',account_id=3,
                  offer_id='media-offer',external_product_id='123')
    result = media_scope(account_id=3,external_item_id='123',target_json=json.dumps(target))
    assert result.kind == 'product' and result.product_id == '123'
    for changes in ({'account_id':4},{'account_id':True},{'marketplace_code':'wb'},
                    {'external_product_id':'456'},{'offer_id':None}):
        result = media_scope(account_id=3,external_item_id='123',target_json=json.dumps({**target,**changes}))
        assert result.kind == 'account'
