"""Real repair routes and per-row receipts, with only synthetic source data."""
import json
from datetime import datetime, timedelta
import re
from unittest.mock import Mock
from urllib.parse import urlencode
import pytest
from flask_login import LoginManager
from werkzeug.datastructures import MultiDict
from sqlalchemy import event

from tests import test_marketplace_publications as fixtures
from models import (
    BackgroundJob,
    ImportedProduct,
    Marketplace,
    MarketplaceOperation,
    MarketplaceAttributeDefinition,
    MarketplaceProductDraft,
    MarketplaceProductType,
    OzonBulkUploadItem,
    OzonBulkUploadRun,
    SellerMarketplaceAccount,
    User,
    db,
)
from services.ozon_api_client import OzonSellerAPIClient
from services.ozon_bulk_repair import OzonBulkRepairService as Repair
from services.ozon_bulk_upload import (
    OzonBulkUploadConflict,
    OzonBulkUploadNotFound,
    OzonBulkUploadService as Upload,
    OzonBulkUploadValidationError,
)
from services.marketplace_drafts import (
    MarketplaceDraftService,
    MarketplaceDraftValidationError,
)
from routes.marketplace_drafts import register_marketplace_draft_routes
from routes.ozon_bulk_uploads import register_ozon_bulk_upload_routes


@pytest.fixture
def case(monkeypatch):
    value=fixtures.OzonBulkRepairServiceTest(methodName='runTest')
    value.setUp()
    value.app.secret_key='synthetic-bulk-repair-tests'
    register_ozon_bulk_upload_routes(value.app)
    manager=LoginManager(value.app)
    manager.user_loader(lambda user_id:db.session.get(User,int(user_id)))
    value.client=value.app.test_client()
    with value.client.session_transaction() as session:
        session['_user_id']=str(value.user.id)
        session['_fresh']=True
    monkeypatch.setattr(OzonSellerAPIClient,'request',Mock(side_effect=AssertionError('No provider I/O')))
    try:yield value
    finally:
        OzonSellerAPIClient.request.assert_not_called()
        value.tearDown()


def document(case):
    return Repair.editor_document(seller_id=case.seller.id,job_uid=case.job.job_uid)


def form(row, *, action='ИСПРАВИТЬ', **updates):
    p='row_'+str(row['draft_id'])+'_'
    data={'selected_draft_id':str(row['draft_id']),p+'draft_version':str(row['draft_version']),
          p+'imported_product_id':str(row['imported_product_id']),p+'action':action,
          p+'product_type_id':str(row['product_type_id'] or ''),p+'save_mapping':'0'}
    for key in Repair.EDITABLE_FIXED-{'action'}:data[p+key]=row[key]
    for field in row['attributes']:
        if field['editable']:
            data[p+'attribute_'+field['external_id']]=field['value']
            data[p+'attribute_value_id_'+field['external_id']]=field['dictionary_value_id']
    for key,value in updates.items():data[p+key]=str(value)
    return data


def source_refresh_form(*rows, **updates):
    data = {
        'refresh_source': '1',
        'confirm_local': '1',
        'selected_draft_id': [str(row['draft_id']) for row in rows],
    }
    for row in rows:
        prefix = 'row_'+str(row['draft_id'])+'_'
        data[prefix+'draft_version'] = str(row['draft_version'])
        data[prefix+'imported_product_id'] = str(row['imported_product_id'])
    data.update(updates)
    return data


def url(case):return '/marketplaces/ozon/uploads/'+case.job.job_uid+'/repair'


def selection(case, draft_ids):
    return {'account_id':case.account.id,'draft_ids':list(draft_ids)}


def selection_row(row, **updates):
    values = {
        key: row[key]
        for key in Repair.EDITABLE_FIXED
        if key != 'action'
    }
    values['action'] = 'ИСПРАВИТЬ'
    for field in row['attributes']:
        if field['editable']:
            values['attribute:'+field['external_id']] = field['value']
            values['attribute_value_id:'+field['external_id']] = (
                field['dictionary_value_id']
            )
    values.update(updates)
    return {
        'draft_id':row['draft_id'],
        'expected_version':row['draft_version'],
        'imported_product_id':row['imported_product_id'],
        'action':'ИСПРАВИТЬ',
        'product_type_id':row['product_type_id'],
        'save_mapping':False,
        'schema_cleanup':row['schema_cleanup'],
        'values':values,
    }


def mixed_attribute_selection(case):
    """Build three exact types with small, disjoint required schemas."""
    now = datetime.utcnow()
    product_types = [case.product_type]
    for index in range(2):
        product_type = MarketplaceProductType(
            marketplace_id=case.marketplace.id,
            category_id=case.product_type.category_id,
            external_type_id=str(778 + index),
            name=f"Тип mixed {index + 2}",
            is_available=True,
            is_enabled=True,
            is_seller_selectable=True,
            attributes_synced_at=now,
            attributes_sync_status="success",
            attributes_schema_hash=(str(index + 2) * 64),
            attributes_version=1,
            attributes_count=33,
            required_attributes_count=32,
        )
        db.session.add(product_type)
        db.session.flush()
        product_types.append(product_type)

    required_ids = [
        ["22232", *(str(value) for value in range(50000, 50031))],
        [str(value) for value in range(50100, 50132)],
        [str(value) for value in range(50200, 50232)],
    ]
    case.product_type.attributes_count = 33
    case.product_type.required_attributes_count = 32
    for row_index, (product_type, attribute_ids) in enumerate(
        zip(product_types, required_ids),
    ):
        for attribute_id in attribute_ids:
            if row_index == 0 and attribute_id == "22232":
                continue
            db.session.add(MarketplaceAttributeDefinition(
                marketplace_id=case.marketplace.id,
                product_type_id=product_type.id,
                external_attribute_id=attribute_id,
                name=f"Обязательное поле {attribute_id}",
                data_type="String",
                is_required=True,
                max_value_count=1,
                is_collection=False,
                is_available=True,
                is_enabled=True,
                last_seen_at=now,
            ))
        if row_index > 0:
            db.session.add(MarketplaceAttributeDefinition(
                marketplace_id=case.marketplace.id,
                product_type_id=product_type.id,
                external_attribute_id="4191",
                name="Аннотация",
                data_type="String",
                is_required=False,
                max_value_count=1,
                is_collection=False,
                is_available=True,
                is_enabled=True,
                last_seen_at=now,
            ))

    other_drafts = [
        fixtures.BulkEnqueueTest._second_draft(
            case,
            offer_id="mixed-type-second",
        ),
        fixtures.BulkEnqueueTest._second_draft(
            case,
            offer_id="mixed-type-third",
        ),
    ]
    for draft, product_type in zip(other_drafts, product_types[1:]):
        draft.product_type_id = product_type.id
        draft.external_type_id = product_type.external_type_id
        draft.schema_version = product_type.attributes_version
        draft.schema_hash = product_type.attributes_schema_hash
        draft.attributes_json = "[]"
    db.session.commit()
    return [case.draft, *other_drafts], required_ids


def source_prepare_case(case, *, active=False):
    prepared = fixtures.BulkEnqueueTest._second_draft(
        case, offer_id='source-prepare-prepared',
    )
    excluded = fixtures.BulkEnqueueTest._second_draft(
        case, offer_id='source-prepare-excluded',
    )
    failed = fixtures.BulkEnqueueTest._second_draft(
        case, offer_id='source-prepare-failed-local',
    )
    product_ids = [
        case.source.id,
        prepared.imported_product_id,
        excluded.imported_product_id,
        failed.imported_product_id,
    ]
    accepted = Upload.accept_source_prepare(
        seller_id=case.seller.id,
        account_id=case.account.id,
        imported_product_ids=product_ids,
        request_key='source-prepare-repair-fixture-0001',
        created_by_user_id=case.user.id,
    )
    job = accepted.job
    run = OzonBulkUploadRun.query.filter_by(job_id=job.id).one()
    items = OzonBulkUploadItem.query.filter_by(run_id=run.id).order_by(
        OzonBulkUploadItem.ordinal.asc(),
    ).all()
    assert len(items) == 4
    if active:
        run.state = 'active'
        run.next_due_at = datetime.utcnow() + timedelta(hours=1)
        job.status = 'running'
    else:
        run.state = 'completed'
        run.next_due_at = None
        assignments = [
            ('needs_input', case.draft.id, None),
            ('prepared', prepared.id, prepared.version),
            ('excluded', excluded.id, None),
            ('failed_local', None, None),
        ]
        for item, (phase, draft_id, prepared_version) in zip(items, assignments):
            item.draft_id = draft_id
            item.prepared_version = prepared_version
            item.error_code = 'synthetic_local_issue' if phase in {
                'needs_input', 'failed_local',
            } else None
            item.error_message = (
                'Синтетический локальный блокер'
                if phase in {'needs_input', 'failed_local'} else None
            )
            item.phase = phase
        job.status = 'completed'
    db.session.commit()
    return {
        'job': job,
        'run': run,
        'items': items,
        'drafts': [case.draft, prepared, excluded, None],
        'product_ids': product_ids,
    }


def test_schema_cleanup_maps_exact_value_paths_to_simple_and_complex_owners(monkeypatch):
    effective = {
        "attributes": [{
            "attribute_id": "4543",
            "complex_id": "0",
            "values": [{"value": "one"}],
        }],
        "complex_attributes": [{
            "attributes": [{
                "attribute_id": "7001",
                "complex_id": "17",
                "values": [{"value": "two"}],
            }],
        }],
    }

    class Draft:
        published_listing_id = 91
        product_type_id = 5
        attribute_removals_json = "[]"
        attributes_json = json.dumps(effective["attributes"])
        complex_attributes_json = json.dumps(effective["complex_attributes"])
        content_json = "{}"
        media_json = "{}"
        dimensions_json = "{}"
        barcodes_json = "[]"
        commercial_json = "{}"

    monkeypatch.setattr(
        Repair,
        "_baseline_documents",
        classmethod(lambda _cls, _draft: effective),
    )
    monkeypatch.setattr(
        MarketplaceDraftService,
        "publication_documents",
        classmethod(lambda _cls, _draft: (effective, None)),
    )

    result = Repair._schema_cleanup_candidates(
        draft=Draft(),
        definitions={},
        validation_result={
            "errors": [
                {
                    "code": "attribute_max_value_count",
                    "field": "attributes[0].values",
                    "message": "Five values exceed the limit of four.",
                },
                {
                    "code": "attribute_max_value_count",
                    "field": "complex_attributes[0].attributes[0].values",
                    "message": "Five values exceed the limit of four.",
                },
                {
                    "code": "attribute_max_value_count",
                    "field": "attributes[0].values[0]",
                    "message": "Malformed indexed values path.",
                },
                {
                    "code": "attribute_max_value_count",
                    "field": "attributes[0].values.extra",
                    "message": "Malformed suffix.",
                },
                {
                    "code": "attribute_max_value_count",
                    "field": "complex_attributes[0].attributes[0].values[0]",
                    "message": "Malformed complex indexed values path.",
                },
                {
                    "code": "attribute_max_value_count",
                    "field": "attributes[9].values",
                    "message": "No matching owning attribute.",
                },
            ],
        },
    )

    assert [
        (candidate["attribute_id"], candidate["complex_id"])
        for candidate in result
    ] == [("4543", "0"), ("7001", "17")]
    assert [candidate["issues"] for candidate in result] == [
        ["Five values exceed the limit of four."],
        ["Five values exceed the limit of four."],
    ]
    assert all(candidate["in_live_baseline"] for candidate in result)
    assert all(not candidate["already_confirmed"] for candidate in result)


def test_json_get_is_private_and_photo_is_owned_cache_identity(case):
    case.source.photo_urls=json.dumps(['https://source.example.test/photo.jpg'])
    db.session.commit()
    response=case.client.get(url(case),headers={'Accept':'application/json'})
    assert response.status_code==200 and response.headers['Cache-Control']=='private, no-store'
    row=response.json['editor']['groups'][0]['rows'][0]
    assert row['photo_url']==f'/api/photos/imported-product/{case.source.id}/0?deferred=1'
    assert row['product_type_name']=='Тип'
    assert row['type_change_impact']=={'attributes':0,'complex_groups':0,'removals':0}
    assert row['attributes'][0]['max_values']==1
    assert 'https://source.example.test' not in response.text
    assert MarketplaceOperation.query.count()==0


def test_foreign_source_cannot_leak_title_or_photo(case):
    case.source.seller_id=case.foreign_seller.id
    db.session.commit()
    with pytest.raises(OzonBulkUploadConflict):
        document(case)


@pytest.mark.parametrize('stored', ['null', '{}', '{"errors":null}', '{"errors":"bad"}'])
def test_dictionary_uses_current_form_when_saved_validation_is_absent(case, stored):
    case.draft.validation_result_json=stored
    db.session.commit()
    before=case.draft.version
    result=case.client.get(url(case)+f'/dictionaries/{case.draft.id}/22232',
                           headers={'Accept':'application/json'})
    assert result.status_code==200
    assert result.json['items']==[{'id':'1001','value':'3304990000'}]
    assert case.draft.version==before and case.draft.validation_result_json==stored
    assert Repair._validation_errors(case.draft)==[]


def test_json_form_post_returns_exact_row_receipt_without_redirect(case):
    row=document(case)['groups'][0]['rows'][0]
    response=case.client.post(url(case)+'/apply',data=form(row,action='ИСКЛЮЧИТЬ'),headers={'Accept':'application/json'})
    assert response.status_code==200 and 'Location' not in response.headers
    assert response.headers['Cache-Control']=='private, no-store'
    report=response.json['report']
    assert report['total']==report['excluded']==1 and report['failed']==0
    assert report['rows']==[{'draft_id':row['draft_id'],'status':'excluded','version':row['draft_version']}]
    assert MarketplaceOperation.query.count()==0


def test_partial_save_preserves_success_and_reports_every_row(case,monkeypatch):
    other=fixtures.BulkEnqueueTest._second_draft(case)
    progress=Upload._load_progress(case.job)
    item=dict(progress['items'][0]);item.update(draft_id=other.id,imported_product_id=other.imported_product_id,offer_id=other.offer_id)
    progress['items'].append(item);Upload._persist(case.job,progress)
    rows=[row for group in document(case)['groups'] for row in group['rows']]
    data=MultiDict()
    for row in rows:
        values=form(row,price_rub='1100',attribute_22232='3304990000')
        if row['draft_id']==other.id:values['row_'+str(other.id)+'_draft_version']=str(other.version+1)
        for key,value in values.items():data.add(key,value)
    monkeypatch.setattr(MarketplaceDraftService,'_build_validation_result',case._ready_validation)
    response=case.client.post(url(case)+'/apply',data=data,headers={'Accept':'application/json'})
    assert response.status_code==200
    report=response.json['report'];assert report['total']==2 and report['failed']==report['ready_to_retry']==1
    outcomes={row['draft_id']:row for row in report['rows']}
    assert outcomes[case.draft.id]['status']=='ready_to_retry'
    assert outcomes[case.draft.id]['version']==db.session.get(MarketplaceProductDraft,case.draft.id).version
    assert outcomes[other.id]['status']=='failed' and outcomes[other.id]['message']
    assert json.loads(db.session.get(MarketplaceProductDraft,case.draft.id).commercial_json)['price']=='1100'
    assert json.loads(db.session.get(MarketplaceProductDraft,other.id).commercial_json)['price']=='1000'
    assert MarketplaceOperation.query.count()==0


@pytest.mark.parametrize('change',['duplicate','unknown','wrong_source','json','oversize','disabled'])
def test_bad_form_never_mutates(case,change):
    row=document(case)['groups'][0]['rows'][0]
    before=case.draft.version
    data=MultiDict(form(row,action='ИСКЛЮЧИТЬ'))
    if change=='duplicate':data.add('selected_draft_id',str(row['draft_id']))
    if change=='unknown':data['seller_id']='999'
    if change=='wrong_source':data[f'row_{row["draft_id"]}_imported_product_id']='999'
    if change=='oversize':data[f'row_{row["draft_id"]}_description']='x'*(2*1024*1024+1)
    if change=='disabled':case.app.config['MARKETPLACE_OZON_ENABLED']=False
    kwargs={'json':dict(data)} if change=='json' else {'data':data}
    result=case.client.post(url(case)+'/apply',headers={'Accept':'application/json'},**kwargs)
    assert result.status_code in (400,413)
    assert case.draft.version==before and MarketplaceOperation.query.count()==0
    assert Upload._load_progress(case.job)['items'][0]['status']=='needs_input'


def test_foreign_seller_denied_for_read_and_write(case):
    row=document(case)['groups'][0]['rows'][0]
    with case.client.session_transaction() as session:session['_user_id']=str(case.foreign_user.id)
    assert case.client.get(url(case),headers={'Accept':'application/json'}).status_code==404
    assert case.client.post(url(case)+'/apply',data=form(row,action='ИСКЛЮЧИТЬ'),headers={'Accept':'application/json'}).status_code==404
    assert Upload._load_progress(case.job)['items'][0]['status']=='needs_input'


def test_csrf_rejects_before_local_repair(case):
    from flask_wtf.csrf import CSRFProtect
    CSRFProtect(case.app)
    row=document(case)['groups'][0]['rows'][0]
    response=case.client.post(url(case)+'/apply',data=form(row,action='ИСКЛЮЧИТЬ'),headers={'Accept':'application/json'})
    assert response.status_code==400
    assert Upload._load_progress(case.job)['items'][0]['status']=='needs_input'


def test_read_renews_csrf_after_session_changes_and_old_token_cannot_write(case):
    from flask_wtf.csrf import CSRFProtect
    CSRFProtect(case.app)
    # The ORM fixture keeps an outer app context; real requests get fresh g.
    with case.app.app_context():
        first=case.client.get(url(case),headers={'Accept':'application/json'}).json
    with case.client.session_transaction() as session:
        session.pop('csrf_token',None)
    with case.app.app_context():
        current=case.client.get(url(case),headers={'Accept':'application/json'}).json
    assert current['csrf']!=first['csrf']
    row=current['editor']['groups'][0]['rows'][0]
    data=form(row,action='ИСКЛЮЧИТЬ');data['csrf_token']=first['csrf']
    with case.app.app_context():
        assert case.client.post(url(case)+'/apply',data=data,headers={'Accept':'application/json'}).status_code==400
    data['csrf_token']=current['csrf']
    with case.app.app_context():
        response=case.client.post(url(case)+'/apply',data=data,headers={'Accept':'application/json'})
    assert response.status_code==200 and response.json['report']['excluded']==1
    assert MarketplaceOperation.query.count()==0


def test_source_prepare_editor_uses_ordered_exact_editable_v3_scope(case):
    v3 = source_prepare_case(case)
    response = case.client.get(
        '/marketplaces/ozon/uploads/'+v3['job'].job_uid+'/repair',
        headers={'Accept':'application/json'},
    )
    assert response.status_code == 200
    editor = response.json['editor']
    expected_ids = [case.draft.id, v3['drafts'][1].id]
    assert editor['mode'] == 'source_prepare'
    assert editor['review_draft_ids'] == expected_ids
    assert [
        row['draft_id'] for group in editor['groups'] for row in group['rows']
    ] == expected_ids
    assert editor['blocked_review_items'] == []
    assert MarketplaceOperation.query.count() == 0


def test_source_prepare_editor_get_does_not_reconcile_stale_job_summary(case):
    v3 = source_prepare_case(case)
    v3['job'].set_result({
        'version': 3,
        'mode': 'source_prepare',
        'total': 4,
        'prepared': 0,
        'outcome': 'stale_fixture_summary',
    })
    db.session.commit()
    job_result_before = v3['job'].result_data
    run_before = (v3['run'].state, v3['run'].next_due_at)
    items_before = [
        (item.phase, item.draft_id, item.prepared_version, item.error_code, item.error_message)
        for item in v3['items']
    ]
    write_tables = []

    def record_business_write(_conn, _cursor, statement, _parameters, _context, _executemany):
        match = re.match(
            r'\s*(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+["`]?([A-Za-z0-9_]+)',
            statement,
            re.IGNORECASE,
        )
        if match and match.group(1).lower() in {
            'background_jobs',
            'ozon_bulk_upload_runs',
            'ozon_bulk_upload_items',
            'marketplace_product_drafts',
            'marketplace_operations',
        }:
            write_tables.append(match.group(1).lower())

    event.listen(db.engine, 'before_cursor_execute', record_business_write)
    try:
        response = case.client.get(
            '/marketplaces/ozon/uploads/'+v3['job'].job_uid+'/repair',
            headers={'Accept':'application/json'},
        )
    finally:
        event.remove(db.engine, 'before_cursor_execute', record_business_write)

    assert response.status_code == 200
    assert response.json['editor']['mode'] == 'source_prepare'
    assert write_tables == []
    db.session.refresh(v3['job'])
    db.session.refresh(v3['run'])
    for item in v3['items']:
        db.session.refresh(item)
    assert v3['job'].result_data == job_result_before
    assert (v3['run'].state, v3['run'].next_due_at) == run_before
    assert [
        (item.phase, item.draft_id, item.prepared_version, item.error_code, item.error_message)
        for item in v3['items']
    ] == items_before


def test_source_prepare_rejects_exclusion_without_mutating_v3_item(case):
    v3 = source_prepare_case(case)
    path = '/marketplaces/ozon/uploads/'+v3['job'].job_uid+'/repair'
    row = case.client.get(path, headers={'Accept':'application/json'}).json[
        'editor'
    ]['groups'][0]['rows'][0]
    item = v3['items'][0]
    before = (item.phase, item.prepared_version, case.draft.version)
    response = case.client.post(
        path+'/apply',
        data=form(row, action='ИСКЛЮЧИТЬ'),
        headers={'Accept':'application/json'},
    )
    assert response.status_code == 400
    db.session.refresh(item)
    db.session.refresh(case.draft)
    assert (item.phase, item.prepared_version, case.draft.version) == before
    assert MarketplaceOperation.query.count() == 0


def test_source_prepare_local_edit_advances_only_exact_v3_prepared_version(case, monkeypatch):
    v3 = source_prepare_case(case)
    path = '/marketplaces/ozon/uploads/'+v3['job'].job_uid+'/repair'
    editor = case.client.get(path, headers={'Accept':'application/json'}).json['editor']
    rows = [row for group in editor['groups'] for row in group['rows']]
    row = next(row for row in rows if row['draft_id'] == case.draft.id)
    assert any(field['external_id'] == '22232' for field in row['attributes'])
    item = v3['items'][0]
    before_version = case.draft.version
    monkeypatch.setattr(
        MarketplaceDraftService, '_build_validation_result', case._ready_validation,
    )
    response = case.client.post(
        path+'/apply',
        data=form(
            row,
            description='Сохранено только в локальном черновике.',
            attribute_22232='3304990000',
            attribute_value_id_22232='1001',
        ),
        headers={'Accept':'application/json'},
    )
    assert response.status_code == 200
    report = response.json['report']
    assert report['total'] == report['ready_to_retry'] == report['updated'] == 1
    draft = db.session.get(MarketplaceProductDraft, case.draft.id)
    db.session.refresh(item)
    assert draft.version > before_version
    assert json.loads(draft.content_json)['description'] == (
        'Сохранено только в локальном черновике.'
    )
    assert item.phase == 'prepared'
    assert item.prepared_version == draft.version
    assert MarketplaceOperation.query.count() == 0


def test_source_prepare_stale_version_returns_failed_receipt_without_rebase(case, monkeypatch):
    v3 = source_prepare_case(case)
    path = '/marketplaces/ozon/uploads/'+v3['job'].job_uid+'/repair'
    editor = case.client.get(path, headers={'Accept':'application/json'}).json['editor']
    row = next(
        row for group in editor['groups'] for row in group['rows']
        if row['draft_id'] == case.draft.id
    )
    item = v3['items'][0]
    original_prepared_version = item.prepared_version
    original_error_code = item.error_code
    original_error_message = item.error_message
    original_content = case.draft.content_json
    stale_form = form(
        row,
        description='Эта правка не должна пройти по старой версии.',
        attribute_22232='3304990000',
        attribute_value_id_22232='1001',
    )
    case.draft.version += 1
    db.session.commit()
    current_version = case.draft.version
    monkeypatch.setattr(
        MarketplaceDraftService, '_build_validation_result', case._ready_validation,
    )
    response = case.client.post(
        path+'/apply', data=stale_form, headers={'Accept':'application/json'},
    )
    assert response.status_code == 200
    outcome = response.json['report']['rows'][0]
    assert outcome['draft_id'] == case.draft.id and outcome['status'] == 'failed'
    db.session.refresh(item)
    db.session.refresh(case.draft)
    assert case.draft.version == current_version
    assert case.draft.content_json == original_content
    assert item.phase == 'needs_input'
    assert item.prepared_version == original_prepared_version
    assert item.error_code == original_error_code
    assert item.error_message == original_error_message
    assert MarketplaceOperation.query.count() == 0


def test_source_prepare_refresh_rebases_current_source_and_keeps_local_edits(case):
    v3 = source_prepare_case(case)
    path = '/marketplaces/ozon/uploads/'+v3['job'].job_uid+'/repair'
    case.draft = MarketplaceDraftService.refresh_facts(
        seller_id=case.seller.id,
        draft_id=case.draft.id,
        expected_version=case.draft.version,
    )
    content = json.loads(case.draft.content_json)
    content['description'] = 'Описание продавца сохраняется при обновлении источника.'
    case.draft.content_json = json.dumps(content, ensure_ascii=False)
    before_hash = case.draft.source_fact_hash
    source_title = 'Текущее название сохранённого источника'
    original = json.loads(case.source.original_data or '{}')
    original['title'] = source_title
    case.source.original_data = json.dumps(original, ensure_ascii=False)
    case.source.title = source_title
    db.session.commit()

    editor_response = case.client.get(
        path,
        headers={'Accept':'application/json'},
    )
    assert editor_response.status_code == 200
    rows = [
        row for group in editor_response.json['editor']['groups']
        for row in group['rows']
    ]
    row = next(row for row in rows if row['draft_id'] == case.draft.id)
    sibling = next(row for row in rows if row['draft_id'] != case.draft.id)
    assert row['source_refresh_available'] is True
    sibling_draft = db.session.get(MarketplaceProductDraft, sibling['draft_id'])
    sibling_before = (
        sibling_draft.version,
        sibling_draft.content_json,
        sibling_draft.source_fact_hash,
    )

    response = case.client.post(
        path+'/apply',
        data=source_refresh_form(row),
        headers={'Accept':'application/json'},
    )
    assert response.status_code == 200
    report = response.json['report']
    assert report['source_refresh'] is True
    assert report['mode'] == 'source_prepare'
    assert report['total'] == report['updated'] == 1
    assert report['failed'] == report['excluded'] == 0

    refreshed = db.session.get(MarketplaceProductDraft, case.draft.id)
    db.session.refresh(v3['items'][0])
    db.session.refresh(sibling_draft)
    assert refreshed.source_fact_hash != before_hash
    refreshed_content = json.loads(refreshed.content_json)
    assert refreshed_content['name'] == source_title
    assert refreshed_content['description'] == (
        'Описание продавца сохраняется при обновлении источника.'
    )
    assert v3['items'][0].phase in {'prepared', 'needs_input'}
    if v3['items'][0].phase == 'prepared':
        assert v3['items'][0].prepared_version == refreshed.version
    assert (
        sibling_draft.version,
        sibling_draft.content_json,
        sibling_draft.source_fact_hash,
    ) == sibling_before
    assert MarketplaceOperation.query.count() == 0


def test_source_refresh_rejects_mixed_field_edits_and_freshness_noop(case, monkeypatch):
    v3 = source_prepare_case(case)
    path = '/marketplaces/ozon/uploads/'+v3['job'].job_uid+'/repair'
    case.source.title = 'Источник успел измениться'
    db.session.commit()
    editor = case.client.get(path, headers={'Accept':'application/json'}).json['editor']
    row = next(
        row for group in editor['groups'] for row in group['rows']
        if row['draft_id'] == case.draft.id
    )
    before = (case.draft.version, case.draft.content_json, case.draft.source_fact_hash)

    mixed = source_refresh_form(row)
    mixed['row_'+str(row['draft_id'])+'_description'] = 'Не смешивать с rebase'
    rejected = case.client.post(
        path+'/apply', data=mixed, headers={'Accept':'application/json'},
    )
    assert rejected.status_code == 400
    missing_version = source_refresh_form(row)
    missing_version.pop('row_'+str(row['draft_id'])+'_draft_version')
    missing = case.client.post(
        path+'/apply',
        data=missing_version,
        headers={'Accept':'application/json'},
    )
    assert missing.status_code == 400
    db.session.refresh(case.draft)
    assert (case.draft.version, case.draft.content_json, case.draft.source_fact_hash) == before

    rebase = Mock(return_value=case.draft)
    validate = Mock(side_effect=AssertionError('no-op rebase must not validate or write'))
    monkeypatch.setattr(MarketplaceDraftService, 'rebase_source_defaults', rebase)
    monkeypatch.setattr(MarketplaceDraftService, 'validate_draft', validate)
    response = case.client.post(
        path+'/apply',
        data=source_refresh_form(row),
        headers={'Accept':'application/json'},
    )
    assert response.status_code == 200
    report = response.json['report']
    assert report['source_refresh'] is True
    assert report['updated'] == 0
    assert report['failed'] == 1
    assert report['rows'][0]['status'] == 'failed'
    rebase.assert_called_once()
    validate.assert_not_called()
    db.session.refresh(case.draft)
    db.session.refresh(v3['items'][0])
    assert (case.draft.version, case.draft.content_json, case.draft.source_fact_hash) == before
    assert v3['items'][0].phase == 'needs_input'
    assert MarketplaceOperation.query.count() == 0


def test_draft_selection_source_refresh_keeps_exact_selection_scope(case):
    register_marketplace_draft_routes(case.app)
    case.source.title = 'Новое название выбранного источника'
    db.session.commit()
    query = urlencode({
        'account_id': case.account.id,
        'draft_ids': str(case.draft.id),
    })
    path = '/marketplaces/ozon/uploads/selection/repair?'+query
    editor_response = case.client.get(
        path,
        headers={'Accept':'application/json'},
    )
    assert editor_response.status_code == 200
    editor = editor_response.json['editor']
    row = editor['groups'][0]['rows'][0]
    assert editor['mode'] == 'draft_selection'
    assert row['source_refresh_available'] is True
    runs_before = OzonBulkUploadRun.query.count()
    items_before = OzonBulkUploadItem.query.count()
    before_hash = case.draft.source_fact_hash

    response = case.client.post(
        '/marketplaces/ozon/uploads/selection/repair/apply?'+query,
        data=source_refresh_form(row),
        headers={'Accept':'application/json'},
    )
    assert response.status_code == 200
    assert response.json['mode'] == 'draft_selection'
    assert response.json['scope_key'] == editor['scope_key']
    report = response.json['report']
    assert report['mode'] == 'draft_selection'
    assert report['scope_key'] == editor['scope_key']
    assert report['source_refresh'] is True
    assert report['total'] == report['updated'] == 1
    assert report['failed'] == 0
    db.session.refresh(case.draft)
    assert case.draft.source_fact_hash != before_hash
    assert OzonBulkUploadRun.query.count() == runs_before
    assert OzonBulkUploadItem.query.count() == items_before
    assert MarketplaceOperation.query.count() == 0


def test_source_refresh_rejects_active_reviewed_row(case):
    v3 = source_prepare_case(case)
    held = v3['drafts'][1]
    Upload.accept_reviewed_publish(
        seller_id=case.seller.id,
        account_id=case.account.id,
        draft_ids=[held.id],
        expected_versions={str(held.id): held.version},
        request_key='source-refresh-active-review-0001',
        parent_prepare_job_uid=v3['job'].job_uid,
        created_by_user_id=case.user.id,
    )
    operations_before = MarketplaceOperation.query.count()
    path = '/marketplaces/ozon/uploads/'+v3['job'].job_uid+'/repair'
    editor = case.client.get(path, headers={'Accept':'application/json'}).json['editor']
    assert all(
        row['draft_id'] != held.id
        for group in editor['groups']
        for row in group['rows']
    )
    before = (held.version, held.content_json, held.source_fact_hash)
    held_form = {
        'draft_id': held.id,
        'draft_version': held.version,
        'imported_product_id': held.imported_product_id,
    }
    response = case.client.post(
        path+'/apply',
        data=source_refresh_form(held_form),
        headers={'Accept':'application/json'},
    )
    assert response.status_code == 400
    db.session.refresh(held)
    assert (held.version, held.content_json, held.source_fact_hash) == before
    assert MarketplaceOperation.query.count() == operations_before


def test_source_prepare_draft_from_another_account_is_not_in_repair_scope(case):
    v3 = source_prepare_case(case)
    second_account = SellerMarketplaceAccount(
        seller_id=case.seller.id,
        marketplace_id=case.marketplace.id,
        external_account_id='synthetic-second-ozon-account',
        label='Second synthetic Ozon',
        is_active=True,
        connection_status='connected',
        settings_json='{}',
    )
    db.session.add(second_account)
    db.session.flush()
    v3['drafts'][1].account_id = second_account.id
    db.session.commit()
    response = case.client.get(
        '/marketplaces/ozon/uploads/'+v3['job'].job_uid+'/repair',
        headers={'Accept':'application/json'},
    )
    assert response.status_code == 409
    assert MarketplaceOperation.query.count() == 0


def test_source_prepare_active_run_is_not_opened_for_repair(case):
    v3 = source_prepare_case(case, active=True)
    response = case.client.get(
        '/marketplaces/ozon/uploads/'+v3['job'].job_uid+'/repair',
        headers={'Accept':'application/json'},
    )
    assert response.status_code == 409
    db.session.refresh(v3['run'])
    db.session.refresh(v3['job'])
    assert v3['run'].state == 'active'
    assert v3['job'].status == 'running'
    assert MarketplaceOperation.query.count() == 0


def test_source_prepare_active_reviewed_child_blocks_exact_draft(case):
    v3 = source_prepare_case(case)
    prepared = v3['drafts'][1]
    Upload.accept_reviewed_publish(
        seller_id=case.seller.id,
        account_id=case.account.id,
        draft_ids=[prepared.id],
        expected_versions={str(prepared.id): prepared.version},
        request_key='reviewed-child-repair-fixture-0001',
        parent_prepare_job_uid=v3['job'].job_uid,
        created_by_user_id=case.user.id,
    )
    response = case.client.get(
        '/marketplaces/ozon/uploads/'+v3['job'].job_uid+'/repair',
        headers={'Accept':'application/json'},
    )
    assert response.status_code == 200
    editor = response.json['editor']
    assert editor['review_draft_ids'] == [case.draft.id]
    assert len(editor['blocked_review_items']) == 1
    assert editor['blocked_review_items'][0]['draft_id'] == prepared.id
    assert editor['blocked_review_items'][0]['title']
    assert editor['blocked_review_items'][0]['message']
    assert MarketplaceOperation.query.count() == 0


def test_draft_selection_editor_is_exact_ordered_local_scope(case):
    other = fixtures.BulkEnqueueTest._second_draft(
        case,
        offer_id='selection-order-second',
    )
    scope = selection(case, [other.id, case.draft.id])
    jobs_before = BackgroundJob.query.count()
    runs_before = OzonBulkUploadRun.query.count()
    items_before = OzonBulkUploadItem.query.count()
    operations_before = MarketplaceOperation.query.count()
    legacy_result_before = case.job.result_data

    editor = Repair.editor_document(seller_id=case.seller.id, selection=scope)
    replay = Repair.editor_document(seller_id=case.seller.id, selection=scope)
    reversed_editor = Repair.editor_document(
        seller_id=case.seller.id,
        selection=selection(case, [case.draft.id, other.id]),
    )

    row_ids = [
        row['draft_id']
        for group in editor['groups']
        for row in group['rows']
    ]
    assert editor['mode'] == 'draft_selection'
    assert editor['account_id'] == case.account.id
    assert editor['account_label'] == case.account.label
    assert editor['review_draft_ids'] == [other.id, case.draft.id]
    assert row_ids == [other.id, case.draft.id]
    assert editor['total'] == 2
    assert 'job_uid' not in editor
    assert re.fullmatch(r'[0-9a-f]{64}', editor['scope_key'])
    assert replay['scope_key'] == editor['scope_key']
    assert reversed_editor['scope_key'] != editor['scope_key']
    assert BackgroundJob.query.count() == jobs_before
    assert OzonBulkUploadRun.query.count() == runs_before
    assert OzonBulkUploadItem.query.count() == items_before
    assert MarketplaceOperation.query.count() == operations_before
    assert case.job.result_data == legacy_result_before
    with pytest.raises(OzonBulkUploadValidationError):
        Repair.editor_document(
            seller_id=case.seller.id,
            job_uid=case.job.job_uid,
            selection=scope,
        )
    with pytest.raises(OzonBulkUploadValidationError):
        Repair.editor_document(seller_id=case.seller.id)


def test_native_mixed_type_editor_allows_cross_row_fields_over_xlsx_limit(
    case,
    monkeypatch,
):
    drafts, required_ids = mixed_attribute_selection(case)
    register_marketplace_draft_routes(case.app)
    query = urlencode({
        "account_id": case.account.id,
        "draft_ids": ",".join(str(draft.id) for draft in drafts),
    })
    path = "/marketplaces/ozon/uploads/selection/repair?" + query
    response = case.client.get(path, headers={"Accept": "application/json"})

    assert response.status_code == 200
    editor = response.json["editor"]
    rows = [row for group in editor["groups"] for row in group["rows"]]
    assert [row["draft_id"] for row in rows] == [draft.id for draft in drafts]
    assert [len(row["attributes"]) for row in rows] == [32, 32, 32]
    assert sum(len(row["attributes"]) for row in rows) == 96
    assert len(editor["bulk_fields"]) == 102
    assert Repair.MAX_COLUMNS == 100
    assert Repair.MAX_EDITOR_ATTRIBUTE_CELLS == 5_000
    assert len(editor["bulk_fields"]) > Repair.MAX_COLUMNS
    assert MarketplaceOperation.query.count() == 0

    with monkeypatch.context() as patcher:
        patcher.setattr(Repair, "MAX_EDITOR_ATTRIBUTE_CELLS", 95)
        limited = case.client.get(
            path,
            headers={"Accept": "application/json"},
        )
    assert limited.status_code == 409
    assert limited.json["code"] == "ozon_repair_editor_too_many_fields"

    target = rows[0]
    target_attribute = required_ids[0][1]
    assert any(
        field["external_id"] == target_attribute and field["editable"]
        for field in target["attributes"]
    )
    before = {
        draft.id: (
            draft.version,
            draft.attributes_json,
            draft.content_json,
            draft.commercial_json,
        )
        for draft in drafts
    }
    oversized_row = selection_row(target)
    while len(oversized_row["values"]) <= Repair.MAX_COLUMNS:
        oversized_row["values"][
            f"overflow_{len(oversized_row['values'])}"
        ] = ""
    with pytest.raises(
        OzonBulkUploadValidationError,
        match="слишком много полей",
    ):
        Repair.apply_editor_rows(
            seller_id=case.seller.id,
            selection=selection(case, [draft.id for draft in drafts]),
            rows=[oversized_row],
        )
    assert MarketplaceOperation.query.count() == 0

    data = MultiDict(form(target))
    data.add("confirm_local", "1")
    data[
        f"row_{target['draft_id']}_attribute_{target_attribute}"
    ] = "saved on exact selected row"
    saved = case.client.post(
        path.replace("/repair?", "/repair/apply?"),
        data=data,
        headers={"Accept": "application/json"},
    )

    assert saved.status_code == 200, saved.json
    report = saved.json["report"]
    assert report["total"] == report["updated"] == 1
    assert report["failed"] == 0
    changed = db.session.get(MarketplaceProductDraft, target["draft_id"])
    db.session.refresh(changed)
    changed_attributes = json.loads(changed.attributes_json)
    assert any(
        item.get("attribute_id") == target_attribute
        and item.get("values") == [{"value": "saved on exact selected row"}]
        for item in changed_attributes
        if isinstance(item, dict)
    )
    for draft in drafts[1:]:
        db.session.refresh(draft)
        assert (
            draft.version,
            draft.attributes_json,
            draft.content_json,
            draft.commercial_json,
        ) == before[draft.id]
    assert MarketplaceOperation.query.count() == 0


def test_native_single_row_with_48_fields_saves_edit_and_keeps_actual_value_cap(case):
    now = datetime.utcnow()
    required_ids = ["22232", *(str(value) for value in range(51000, 51047))]
    case.product_type.attributes_count = 49
    case.product_type.required_attributes_count = 48
    for attribute_id in required_ids[1:]:
        db.session.add(MarketplaceAttributeDefinition(
            marketplace_id=case.marketplace.id,
            product_type_id=case.product_type.id,
            external_attribute_id=attribute_id,
            name=f"Обязательное поле {attribute_id}",
            data_type="String",
            is_required=True,
            max_value_count=1,
            is_collection=False,
            is_available=True,
            is_enabled=True,
            last_seen_at=now,
        ))
    db.session.commit()
    register_marketplace_draft_routes(case.app)
    query = urlencode({
        "account_id": case.account.id,
        "draft_ids": str(case.draft.id),
    })
    path = "/marketplaces/ozon/uploads/selection/repair?" + query
    response = case.client.get(path, headers={"Accept": "application/json"})

    assert response.status_code == 200
    row = response.json["editor"]["groups"][0]["rows"][0]
    assert len(row["attributes"]) == 48
    assert len(response.json["editor"]["bulk_fields"]) == 54
    before = (
        case.draft.version,
        case.draft.attributes_json,
        case.draft.content_json,
        case.draft.commercial_json,
    )
    too_many_values = selection_row(row)
    for field in row["attributes"]:
        external_id = field["external_id"]
        too_many_values["values"][f"attribute:{external_id}"] = (
            f"nonempty value {external_id}"
        )
        too_many_values["values"][f"attribute_value_id:{external_id}"] = (
            f"nonempty dictionary ID {external_id}"
        )
    assert len(too_many_values["values"]) > Repair.MAX_COLUMNS
    assert all(
        value not in (None, "")
        for key, value in too_many_values["values"].items()
        if key.startswith(("attribute:", "attribute_value_id:"))
    )
    with pytest.raises(
        OzonBulkUploadValidationError,
        match="слишком много полей",
    ):
        Repair.apply_editor_rows(
            seller_id=case.seller.id,
            selection=selection(case, [case.draft.id]),
            rows=[too_many_values],
        )
    assert MarketplaceOperation.query.count() == 0

    target_attribute = required_ids[1]
    data = MultiDict(form(row))
    data.add("confirm_local", "1")
    data[f"row_{case.draft.id}_attribute_{target_attribute}"] = (
        "saved on a 48-field exact row"
    )
    saved = case.client.post(
        path.replace("/repair?", "/repair/apply?"),
        data=data,
        headers={"Accept": "application/json"},
    )

    assert saved.status_code == 200, saved.json
    report = saved.json["report"]
    assert report["total"] == report["updated"] == 1
    assert report["failed"] == 0
    db.session.refresh(case.draft)
    assert case.draft.version > before[0]
    assert case.draft.content_json == before[2]
    assert case.draft.commercial_json == before[3]
    saved_attributes = json.loads(case.draft.attributes_json)
    assert any(
        item.get("attribute_id") == target_attribute
        and item.get("values") == [{"value": "saved on a 48-field exact row"}]
        for item in saved_attributes
        if isinstance(item, dict)
    )
    assert MarketplaceOperation.query.count() == 0


def test_unvalidated_draft_without_stored_errors_is_not_reported_ready(case, monkeypatch):
    monkeypatch.setattr(
        MarketplaceDraftService,
        '_build_validation_result',
        Mock(return_value={
            'publishable': True, 'errors': [], 'warnings': [],
        }),
    )
    case.draft.status = 'needs_category'
    case.draft.product_type_id = None
    case.draft.external_category_id = None
    case.draft.external_type_id = None
    case.draft.validation_status = 'never_validated'
    case.draft.validation_result_json = json.dumps({'errors': []})
    case.draft.validated_at = None
    db.session.commit()
    editor = Repair.editor_document(
        seller_id=case.seller.id,
        selection=selection(case, [case.draft.id]),
    )
    row = next(
        row
        for group in editor['groups']
        for row in group['rows']
        if row['draft_id'] == case.draft.id
    )

    assert case.draft.status == 'needs_category'
    assert case.draft.validation_status == 'never_validated'
    assert json.loads(case.draft.validation_result_json)['errors'] == []
    assert row['status'] == 'needs_input'


def test_draft_selection_rejects_foreign_mixed_archived_and_oversized_scope(case):
    foreign_account = SellerMarketplaceAccount(
        seller_id=case.foreign_seller.id,
        marketplace_id=case.marketplace.id,
        external_account_id='foreign-selection-account',
        label='Foreign synthetic Ozon',
        is_active=True,
        connection_status='connected',
        settings_json='{}',
    )
    foreign_source = ImportedProduct(
        seller_id=case.foreign_seller.id,
        external_id='foreign-selection-source',
        external_vendor_code='foreign-selection-offer',
        source_type='synthetic',
        title='Foreign synthetic product',
    )
    db.session.add_all([foreign_account, foreign_source])
    db.session.flush()
    foreign_draft = MarketplaceProductDraft(
        seller_id=case.foreign_seller.id,
        marketplace_id=case.marketplace.id,
        account_id=foreign_account.id,
        imported_product_id=foreign_source.id,
        product_type_id=case.product_type.id,
        offer_id='foreign-selection-offer',
        source_fact_hash='f' * 64,
        content_json='{}',
        attributes_json='[]',
        complex_attributes_json='[]',
        media_json='{}',
        dimensions_json='{}',
        barcodes_json='[]',
        commercial_json='{}',
    )
    second_account = SellerMarketplaceAccount(
        seller_id=case.seller.id,
        marketplace_id=case.marketplace.id,
        external_account_id='same-seller-other-selection-account',
        label='Second synthetic Ozon',
        is_active=True,
        connection_status='connected',
        settings_json='{}',
    )
    db.session.add_all([foreign_draft, second_account])
    db.session.flush()
    other_account_draft = fixtures.BulkEnqueueTest._second_draft(
        case,
        offer_id='other-account-selection-draft',
    )
    other_account_draft.account_id = second_account.id
    db.session.commit()

    with pytest.raises(OzonBulkUploadNotFound):
        Repair.editor_document(
            seller_id=case.seller.id,
            selection=selection(case, [case.draft.id, foreign_draft.id]),
        )
    with pytest.raises(OzonBulkUploadNotFound):
        Repair.editor_document(
            seller_id=case.seller.id,
            selection=selection(
                case,
                [case.draft.id, other_account_draft.id],
            ),
        )
    other_marketplace = Marketplace(
        name='Wildberries',
        code='wb',
        adapter_code='wildberries',
        is_active=True,
    )
    db.session.add(other_marketplace)
    db.session.flush()
    other_marketplace_account = SellerMarketplaceAccount(
        seller_id=case.seller.id,
        marketplace_id=other_marketplace.id,
        external_account_id='selection-non-ozon-account',
        label='Non-Ozon synthetic account',
        is_active=True,
        connection_status='connected',
        settings_json='{}',
    )
    db.session.add(other_marketplace_account)
    db.session.commit()
    with pytest.raises(OzonBulkUploadNotFound):
        Repair.editor_document(
            seller_id=case.seller.id,
            selection={
                'account_id':other_marketplace_account.id,
                'draft_ids':[case.draft.id],
            },
        )
    with pytest.raises(OzonBulkUploadValidationError):
        Repair.editor_document(
            seller_id=case.seller.id,
            selection=selection(case, list(range(1, Repair.MAX_ROWS + 2))),
        )

    case.draft.status = 'archived'
    db.session.commit()
    with pytest.raises(OzonBulkUploadConflict):
        Repair.editor_document(
            seller_id=case.seller.id,
            selection=selection(case, [case.draft.id]),
        )
    assert MarketplaceOperation.query.count() == 0


def test_draft_selection_type_and_dictionary_search_are_local_and_read_only(case):
    scope = selection(case, [case.draft.id])
    before_version = case.draft.version
    before_validation = case.draft.validation_result_json

    types = Repair.search_product_types(
        seller_id=case.seller.id,
        selection=scope,
        query='Тип',
    )
    dictionary = Repair.search_dictionary_values(
        seller_id=case.seller.id,
        selection=scope,
        draft_id=case.draft.id,
        external_attribute_id='22232',
        query='330499',
    )

    assert any(item['id'] == case.product_type.id for item in types)
    assert dictionary['items'] == [{'id':'1001','value':'3304990000'}]
    assert case.draft.version == before_version
    assert case.draft.validation_result_json == before_validation
    assert OzonBulkUploadRun.query.count() == 0
    assert OzonBulkUploadItem.query.count() == 0
    assert MarketplaceOperation.query.count() == 0


def test_required_brand_search_is_exposed_and_bulk_resolution_requires_exact_id(
    case, monkeypatch,
):
    now = datetime.utcnow()
    brand = MarketplaceAttributeDefinition(
        marketplace_id=case.marketplace.id,
        product_type_id=case.product_type.id,
        external_attribute_id='31',
        name='Бренд',
        data_type='String',
        is_required=True,
        dictionary_id='brand-dictionary',
        max_value_count=1,
        is_available=True,
        is_enabled=True,
        last_seen_at=now,
        values_synced_at=now,
        values_sync_status='success',
        values_snapshot_hash='brand-values-hash',
        values_version=1,
        values_count=1,
        restriction_value_ids_json=json.dumps(['41001']),
    )
    db.session.add(brand)
    db.session.commit()
    choice = {'dictionary_value_id': '41001', 'value': 'Acme North'}
    monkeypatch.setattr(
        'services.ozon_brand_search.OzonBrandSearch.value_choices',
        lambda *args: [choice],
    )
    scope = selection(case, [case.draft.id])
    before = (case.draft.version, case.draft.validation_result_json)

    result = Repair.search_dictionary_values(
        seller_id=case.seller.id,
        selection=scope,
        draft_id=case.draft.id,
        external_attribute_id='31',
        query='Acme',
    )

    assert result['items'] == [{'id': '41001', 'value': 'Acme North'}]
    assert (case.draft.version, case.draft.validation_result_json) == before
    assert Repair._resolve_attribute_values(
        definition=brand,
        raw_value='Acme North',
        expected_external_value_id='41001',
        draft=case.draft,
    ) == [choice]
    assert Repair._row_patch(
        draft=case.draft,
        values={
            'attribute:31': 'Acme North',
            'attribute_value_id:31': '41001',
        },
        attribute_keys=['attribute:31'],
    )['attributes'] == [{
        'attribute_id': '31',
        'complex_id': '0',
        'values': [choice],
    }]
    with pytest.raises(MarketplaceDraftValidationError):
        Repair._resolve_attribute_values(
            definition=brand,
            raw_value='Acme North Ltd',
            expected_external_value_id='41001',
            draft=case.draft,
        )
    with pytest.raises(MarketplaceDraftValidationError):
        Repair._resolve_attribute_values(
            definition=brand,
            raw_value='Acme North',
            draft=case.draft,
        )


def test_draft_selection_marks_active_reviewed_draft_as_blocked(case):
    Upload.accept_reviewed_publish(
        seller_id=case.seller.id,
        account_id=case.account.id,
        draft_ids=[case.draft.id],
        expected_versions={str(case.draft.id):case.draft.version},
        request_key='selected-draft-active-review-0001',
        created_by_user_id=case.user.id,
    )

    editor = Repair.editor_document(
        seller_id=case.seller.id,
        selection=selection(case, [case.draft.id]),
    )

    assert editor['mode'] == 'draft_selection'
    assert editor['review_draft_ids'] == []
    assert editor['groups'] == []
    assert editor['total'] == 0
    assert [row['draft_id'] for row in editor['blocked_review_items']] == [
        case.draft.id,
    ]
    assert editor['blocked_review_items'][0]['title']
    assert editor['blocked_review_items'][0]['message']
    assert MarketplaceOperation.query.count() == 0


def test_draft_selection_local_edit_revalidates_and_never_touches_runs(case, monkeypatch):
    scope = selection(case, [case.draft.id])
    editor = Repair.editor_document(seller_id=case.seller.id, selection=scope)
    row = editor['groups'][0]['rows'][0]
    edit = selection_row(
        row,
        description='Сохранено только в выбранном локальном черновике.',
        **{
            'attribute:22232':'3304990000',
            'attribute_value_id:22232':'1001',
        },
    )
    jobs_before = BackgroundJob.query.count()
    runs_before = OzonBulkUploadRun.query.count()
    items_before = OzonBulkUploadItem.query.count()
    legacy_result_before = case.job.result_data
    monkeypatch.setattr(
        MarketplaceDraftService,
        '_build_validation_result',
        case._ready_validation,
    )

    report = Repair.apply_editor_rows(
        seller_id=case.seller.id,
        selection=scope,
        rows=[edit],
        corrected_by_user_id=case.user.id,
    )

    draft = db.session.get(MarketplaceProductDraft, case.draft.id)
    attributes = json.loads(draft.attributes_json)
    assert report['mode'] == 'draft_selection'
    assert report['scope_key'] == editor['scope_key']
    assert report['total'] == report['updated'] == report['ready_to_retry'] == 1
    assert report['failed'] == report['excluded'] == 0
    assert json.loads(draft.content_json)['description'] == (
        'Сохранено только в выбранном локальном черновике.'
    )
    assert {
        'attribute_id':'22232',
        'complex_id':'0',
        'values':[{'dictionary_value_id':'1001','value':'3304990000'}],
    } in attributes
    assert BackgroundJob.query.count() == jobs_before
    assert OzonBulkUploadRun.query.count() == runs_before
    assert OzonBulkUploadItem.query.count() == items_before
    assert case.job.result_data == legacy_result_before
    assert MarketplaceOperation.query.count() == 0


def test_draft_selection_rejects_shared_mapping_before_claim_or_write(case, monkeypatch):
    scope = selection(case, [case.draft.id])
    editor = Repair.editor_document(seller_id=case.seller.id, selection=scope)
    row = editor['groups'][0]['rows'][0]
    edit = selection_row(row, description='Локальная правка без mapping.')
    edit['save_mapping'] = True
    before = (
        case.draft.version,
        case.draft.content_json,
        case.draft.category_mapping_id,
    )
    writes = []

    def record_write(_conn, _cursor, statement, _parameters, _context, _executemany):
        if statement.lstrip().split(None, 1)[0].upper() in {
            'INSERT', 'UPDATE', 'DELETE', 'REPLACE',
        }:
            writes.append(statement)

    account_claim = Mock(side_effect=AssertionError(
        'invalid selected-mode mapping must fail before account claim'
    ))
    monkeypatch.setattr(
        'services.ozon_bulk_repair.try_account_operation_lock',
        account_claim,
    )
    event.listen(db.engine, 'before_cursor_execute', record_write)
    try:
        with pytest.raises(OzonBulkUploadValidationError, match='общую категорию'):
            Repair.apply_editor_rows(
                seller_id=case.seller.id,
                selection=scope,
                rows=[edit],
                corrected_by_user_id=case.user.id,
            )
    finally:
        event.remove(db.engine, 'before_cursor_execute', record_write)

    db.session.refresh(case.draft)
    assert account_claim.call_count == 0
    assert writes == []
    assert (
        case.draft.version,
        case.draft.content_json,
        case.draft.category_mapping_id,
    ) == before
    assert MarketplaceOperation.query.count() == 0


def test_draft_selection_stale_version_and_new_operation_hold_do_not_write(case, monkeypatch):
    scope = selection(case, [case.draft.id])
    editor = Repair.editor_document(seller_id=case.seller.id, selection=scope)
    row = editor['groups'][0]['rows'][0]
    stale_edit = selection_row(
        row,
        description='Старая версия не должна сохраниться.',
    )
    original_content = case.draft.content_json
    case.draft.version += 1
    db.session.commit()
    current_version = case.draft.version
    monkeypatch.setattr(
        MarketplaceDraftService,
        '_build_validation_result',
        case._ready_validation,
    )
    stale_report = Repair.apply_editor_rows(
        seller_id=case.seller.id,
        selection=scope,
        rows=[stale_edit],
        corrected_by_user_id=case.user.id,
    )
    assert stale_report['failed'] == 1
    db.session.refresh(case.draft)
    assert case.draft.version == current_version
    assert case.draft.content_json == original_content
    assert OzonBulkUploadRun.query.count() == 0
    assert OzonBulkUploadItem.query.count() == 0
    assert MarketplaceOperation.query.count() == 0

    fresh_editor = Repair.editor_document(
        seller_id=case.seller.id,
        selection=scope,
    )
    fresh_row = fresh_editor['groups'][0]['rows'][0]
    fresh_edit = selection_row(
        fresh_row,
        description='Операция уже удерживает этот черновик.',
    )

    def acquire_then_hold(account_id):
        db.session.add(MarketplaceOperation(
            seller_id=case.seller.id,
            marketplace_id=case.marketplace.id,
            account_id=account_id,
            draft_id=case.draft.id,
            operation_kind='product_update',
            status='queued',
            idempotency_key='selection-hold-operation',
            request_fingerprint='b' * 64,
            contract_version='selection-hold-v1',
            request_summary_json='{}',
        ))
        db.session.commit()
        return object()

    monkeypatch.setattr(
        'services.ozon_bulk_repair.try_account_operation_lock',
        acquire_then_hold,
    )
    monkeypatch.setattr(
        'services.ozon_bulk_repair.release_account_operation_lock',
        lambda _claim: None,
    )
    held_report = Repair.apply_editor_rows(
        seller_id=case.seller.id,
        selection=scope,
        rows=[fresh_edit],
        corrected_by_user_id=case.user.id,
    )

    db.session.refresh(case.draft)
    assert held_report['failed'] == 1
    assert 'занят' in held_report['rows'][0]['message'].lower()
    assert case.draft.version == current_version
    assert case.draft.content_json == original_content
    assert OzonBulkUploadRun.query.count() == 0
    assert OzonBulkUploadItem.query.count() == 0
