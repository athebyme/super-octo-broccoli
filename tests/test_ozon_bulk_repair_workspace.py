"""Real repair routes and per-row receipts, with only synthetic source data."""
import json
from unittest.mock import Mock
import pytest
from flask_login import LoginManager
from werkzeug.datastructures import MultiDict

from tests import test_marketplace_publications as fixtures
from models import db, User, MarketplaceOperation, MarketplaceProductDraft
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
