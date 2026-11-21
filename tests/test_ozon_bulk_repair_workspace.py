"""Real repair routes and per-row receipts, with only synthetic source data."""
import json
from datetime import datetime, timedelta
from unittest.mock import Mock
import pytest
from flask_login import LoginManager
from werkzeug.datastructures import MultiDict

from tests import test_marketplace_publications as fixtures
from models import (
    MarketplaceOperation,
    MarketplaceProductDraft,
    OzonBulkUploadItem,
    OzonBulkUploadRun,
    SellerMarketplaceAccount,
    User,
    db,
)
from services.ozon_api_client import OzonSellerAPIClient
from services.ozon_bulk_repair import OzonBulkRepairService as Repair
from services.ozon_bulk_upload import OzonBulkUploadService as Upload, OzonBulkUploadConflict
from services.marketplace_drafts import MarketplaceDraftService
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


def url(case):return '/marketplaces/ozon/uploads/'+case.job.job_uid+'/repair'


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
    assert MarketplaceOperation.query.count() == 0


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
