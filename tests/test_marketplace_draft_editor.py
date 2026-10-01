"""Exact local editor reads and preservation of draft/publication boundaries."""
from datetime import datetime, timedelta
import json
from pathlib import Path
import shutil
import subprocess
from unittest.mock import patch
from types import SimpleNamespace

import pytest
from tests import test_marketplace_drafts as fixtures
from models import db, MarketplaceAttributeValue
from services.marketplace_draft_editor import MarketplaceDraftEditor
from services.marketplace_drafts import MarketplaceDraftService, MarketplaceDraftConflict, MarketplaceDraftNotFound


@pytest.fixture
def draft_fixture():
    fixture = fixtures.MarketplaceDraftServiceTest()
    fixture.setUp()
    try:
        product = fixture._product()
        fixture.draft = MarketplaceDraftService.create_draft(
            seller_id=fixture.seller1_id, account_id=fixture.account1.id,
            imported_product_id=product.id, product_type_id=fixture.product_type.id,
        )
        yield fixture
    finally:
        fixture.tearDown()


def test_editor_reads_exact_scope_without_provider_and_preserves_documents(draft_fixture):
    f = draft_fixture
    with patch('requests.sessions.Session.request', side_effect=AssertionError('No network')):
        doc = MarketplaceDraftEditor.document(seller_id=f.seller1_id, draft_id=f.draft.id)
        assert doc['documents']['content']['name'] == f.draft.to_public_dict(detail=True)['content']['name']
        assert {row['id'] for row in doc['definitions']} == {'31', '32', '4191'}
        assert doc['baseline_attribute_identities'] == []
        with pytest.raises(MarketplaceDraftNotFound):
            MarketplaceDraftEditor.document(seller_id=f.seller2_id, draft_id=f.draft.id)


def test_current_validation_blocks_legacy_ready_and_preserves_saved_history(draft_fixture):
    f = draft_fixture
    f.draft.status = 'ready'
    f.draft.validation_status = 'valid'
    f.draft.validated_at = datetime(2026, 7, 25, 12, 0, 0)
    f.draft.validation_result_json = json.dumps({
        'version': 1, 'marketplace': 'ozon', 'publishable': True,
        'errors': [], 'warnings': [], 'validated_at': '2026-07-25T12:00:00',
    })
    f.draft.dimensions_json = json.dumps({})
    f.draft.commercial_json = json.dumps({
        'price': '1000', 'currency_code': 'RUB',
    })
    db.session.commit()
    draft_id = f.draft.id
    stored_before = {
        'status': f.draft.status,
        'validation_status': f.draft.validation_status,
        'validation_result_json': f.draft.validation_result_json,
        'validated_at': f.draft.validated_at,
        'dimensions_json': f.draft.dimensions_json,
        'commercial_json': f.draft.commercial_json,
        'version': f.draft.version,
    }

    with patch('requests.sessions.Session.request', side_effect=AssertionError('No network')):
        document = MarketplaceDraftEditor.document(
            seller_id=f.seller1_id, draft_id=draft_id,
        )

    current = document['current_validation']
    current_codes = {item['code'] for item in current['errors']}
    current_fields = {
        item['field'] for item in current['errors']
        if item['code'] == 'physical_fact_required'
    }
    assert not current['publishable']
    assert 'vat_required' in current_codes
    assert current_fields >= {
        'dimensions.width', 'dimensions.height',
        'dimensions.depth', 'dimensions.weight',
    }
    assert document['draft']['status'] == 'ready'
    assert document['draft']['validation_status'] == 'valid'
    assert document['draft']['validation']['publishable'] is True
    assert document['draft']['validated_at'].startswith('2026-07-25')
    assert document['operations'] == []

    db.session.expire_all()
    persisted = db.session.get(type(f.draft), draft_id)
    assert persisted.status == stored_before['status']
    assert persisted.validation_status == stored_before['validation_status']
    assert persisted.validation_result_json == stored_before['validation_result_json']
    assert persisted.validated_at == stored_before['validated_at']
    assert persisted.dimensions_json == stored_before['dimensions_json']
    assert persisted.commercial_json == stored_before['commercial_json']
    assert persisted.version == stored_before['version']


def test_cached_photo_preview_matches_delivery_slot_without_replacing_media():
    from services.source_photo_display import imported_photo_previews
    source = SimpleNamespace(id=17, photo_urls=json.dumps(['https://old.test/a', 'https://old.test/b']),
        supplier_product=SimpleNamespace(photo_urls_json=json.dumps([
            {'sexoptovik':'https://auth.test/a', 'original':'https://public.test/a'}, None,
        ])))
    previews = imported_photo_previews(source)
    assert previews == {
        'https://auth.test/a':'/api/photos/imported-product/17/0?deferred=1',
        'https://public.test/a':'/api/photos/imported-product/17/0?deferred=1',
        'https://old.test/b':'/api/photos/imported-product/17/1?deferred=1',
    }
    assert 'https://old.test/a' not in previews
    source.supplier_product.photo_urls_json = '{malformed'
    assert len(imported_photo_previews(source)) == 2
    source.photo_urls = json.dumps(['https://photo.test/' + str(i) for i in range(50)])
    assert len(imported_photo_previews(source)) == 30


def test_dictionary_requires_current_type_freshness_restrictions_and_literal_query(draft_fixture):
    f = draft_fixture
    def search(**extra):
        return MarketplaceDraftEditor.dictionary(**{
            'seller_id':f.seller1_id, 'draft_id':f.draft.id,
            'attribute_id':'32', 'product_type_id':f.product_type.id, **extra,
        })
    assert search(query='рОсСиЯ')['items'] == [{'id':'9001', 'value':'Россия'}]
    assert search(query='%')['items'] == []
    with pytest.raises(MarketplaceDraftConflict):
        search(product_type_id=f.product_type.id + 1)
    with pytest.raises(MarketplaceDraftNotFound):
        search(seller_id=f.seller2_id)
    f.country_attribute.restriction_value_ids_json = json.dumps(['999'])
    db.session.commit()
    assert search()['items'] == []
    f.country_attribute.values_synced_at = f.now - timedelta(days=100)
    db.session.commit()
    with pytest.raises(MarketplaceDraftConflict):
        search()


def test_dictionary_result_is_bounded_and_schema_search_handles_cyrillic(draft_fixture):
    f = draft_fixture
    for index in range(35):
        db.session.add(MarketplaceAttributeValue(
            marketplace_id=f.marketplace.id, product_type_id=f.product_type.id,
            attribute_id=f.country_attribute.id, external_value_id=str(10000 + index),
            value=f'Страна {index:02}', value_normalized=f'страна {index:02}', is_available=True,
        ))
    db.session.commit()
    result = MarketplaceDraftEditor.dictionary(seller_id=f.seller1_id, draft_id=f.draft.id,
        attribute_id='32', product_type_id=f.product_type.id, query='сТрАнА')
    assert len(result['items']) == 30 and result['has_more']
    assert [row['id'] for row in MarketplaceDraftService.search_product_types(
        seller_id=f.seller1_id, query='фУтБоЛкА')] == [f.product_type.id]
    assert not MarketplaceDraftService.search_product_types(seller_id=f.seller1_id, query='%')


def test_published_editor_discloses_preserved_media_and_exact_deletion_identities():
    from tests import test_marketplace_publications as publication_fixtures
    fixture = publication_fixtures.MarketplacePublicationServiceTest()
    fixture.setUp()
    try:
        prior = fixture.prior_payload()
        fixture.attach_listing(prior)
        with patch('requests.sessions.Session.request', side_effect=AssertionError('No network')):
            plain, baseline = MarketplaceDraftService.publication_documents(fixture.draft)
            documents, editor = MarketplaceDraftService.publication_documents(
                fixture.draft, include_baseline_editor_data=True)
        assert plain == documents
        assert 'preserved_media' not in baseline
        assert editor['preserved_barcodes'] == [prior['items'][0]['barcode']]
        assert editor['preserved_media']['primary_image'] == prior['items'][0].get('primary_image', prior['items'][0]['images'][0])
        assert {'attribute_id':'4191', 'complex_id':'0'} in editor['attribute_identities']
        assert len({(row['attribute_id'], row['complex_id']) for row in editor['attribute_identities']}) == len(editor['attribute_identities'])
    finally:
        fixture.tearDown()
        fixture.doCleanups()


NODE_SETUP = r'''
const assert=require('node:assert/strict'); global.window=global;
global.document={getElementById:()=>null}; global.confirm=()=>true;
require('./static/ozon-draft-editor.js');
const config={enabled:true,publicationEnabled:true,csrf:'synthetic',idempotencyKey:'stable',urls:{editor:'/editor',save:'/save',validate:'/validate',dictionary:'/dict/',types:'/types',publish:'/publish',updatePublication:'/update',operations:'/operations/'}};
const definition=ozonDraftEditor.createOptions(config);const page=definition.data();
for(const [k,v] of Object.entries(definition.methods))page[k]=v.bind(page);
for(const [k,v] of Object.entries(definition.computed))Object.defineProperty(page,k,{get:v.bind(page)});
page.$refs={dictionaryDialog:{close(){},showModal(){}},publicationDialog:{close(){},showModal(){}},dictionarySearch:{focus(){}}};page.$nextTick=fn=>fn();
const fixture={draft:{id:1,version:3,offer_id:'offer',product_type_id:7,status:'ready',validation_status:'valid',validation:{publishable:true},attribute_removals:[]},documents:{content:{name:'Товар',description:'Описание'},attributes:[{attribute_id:'31',complex_id:'0',values:[{value:'Бренд'}]}],complex_attributes:[],media:{primary_image:'https://img.test/1.jpg',images:[]},dimensions:{width:'10',height:'20',depth:'30',weight:'200',dimension_unit:'MILLIMETERS',weight_unit:'GRAMS'},commercial:{price:'100.50',vat:'0',currency_code:'RUB'},barcodes:['123']},current_validation:{publishable:true,errors:[],warnings:[],validated_at:'2026-10-01T00:00:00'},readiness:{overall:'ready',schema:{fresh:true}},definitions:[{id:'31',complex_id:'0',name:'Бренд',editable:true,dictionary:true,dictionary_fresh:true,collection:false,max_values:1}],operations:[],active_operation_id:null,baseline_attribute_identities:[],suggestions:[]};
page.hydrate(structuredClone(fixture));page.loading=false;
'''


def run_node(source):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node required for editor behavior checks')
    result = subprocess.run([node, '-e', NODE_SETUP + source], cwd=Path(__file__).parents[1],
                            text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr


def test_editor_patch_changes_only_edited_blocks_and_live_deletion_is_explicit():
    run_node(r'''
assert.equal(page.dirty,false);assert.equal(page.canPublish,true);page.data.readiness.overall='account_blocked';assert.equal(page.canPublish,false);page.data.readiness.overall='ready';
page.form.content.name='Новое название';assert.deepEqual(Object.keys(page.patch()),['content']);assert.equal(page.canPublish,false);
assert.equal(page.form.commercial.price,'100.50');page.form.commercial.price='100,50';assert.equal(page.patch().commercial,undefined);page.form.commercial.price='120,75';assert.equal(page.patch().commercial.price,'120.75');
page.data.baseline_attribute_identities=[{attribute_id:'31',complex_id:'0'}];
global.confirm=()=>false;page.removeAttribute(page.data.definitions[0]);assert.equal(page.form.attribute_removals.length,0);
global.confirm=()=>true;page.removeAttribute(page.data.definitions[0]);assert.deepEqual(page.form.attribute_removals,[{attribute_id:'31',complex_id:'0'}]);assert.equal(page.form.attributes.length,0);
page.setValues(page.data.definitions[0],[{value:'Вернули бренд'}]);assert.equal(page.form.attribute_removals.length,0);
let changed;const field={values:[],definition:{data_type:'Boolean'},$emit:(_event,values)=>changed=values};definition.components['attribute-field'].methods.change.call(field,0,'false');assert.deepEqual(changed,[{value:'false'}]);field.definition.data_type='Decimal';definition.components['attribute-field'].methods.change.call(field,0,'12,5');assert.deepEqual(changed,[{value:'12.5'}]);
assert.equal(ozonDraftEditor.safeImage('javascript:alert(1)'),'');assert.equal(ozonDraftEditor.safeImage('https://user:secret@test/1.jpg'),'');
''')


def test_current_validation_not_saved_ready_snapshot_gates_publication():
    run_node(r'''
(async()=>{
const savedValidation=structuredClone(page.draft.validation);
page.data.current_validation={publishable:false,errors:[
 {code:'physical_fact_required',field:'dimensions.width'},
 {code:'physical_fact_required',field:'dimensions.height'},
 {code:'physical_fact_required',field:'dimensions.depth'},
 {code:'physical_fact_required',field:'dimensions.weight'},
 {code:'vat_required',field:'commercial.vat'}
],warnings:[]};
assert.equal(page.draft.status,'ready');assert.equal(page.draft.validation_status,'valid');assert.equal(page.draft.validation.publishable,true);
assert.equal(page.canPublish,false);assert.equal(page.statusLabel,'Заполните упаковку и выберите ставку НДС');
assert.match(page.issueLabel(page.errors[0]),/Ширина упаковки/);assert.match(page.issueLabel(page.errors[4]),/ставку НДС/);
let opened=[];global.document.getElementById=id=>({scrollIntoView(){},focus(){opened.push(id)}});
page.goToIssue(page.errors[0]);page.goToIssue(page.errors[4]);assert.deepEqual(opened,['ode-width','ode-vat']);assert.equal(page.section,'delivery');
let requests=0;page.request=async()=>{requests++;return {operation:{id:1}}};page.confirmedWrite=true;await page.publish();assert.equal(requests,0);
assert.deepEqual(page.draft.validation,savedValidation,'read-only current evaluation leaves historical validation intact');
page.data.current_validation=undefined;assert.equal(page.currentValidationAvailable,false);assert.equal(page.canPublish,false);assert.equal(page.statusLabel,'Текущая проверка не подтверждена');
page.draft.status='published';assert.equal(page.statusLabel,'Опубликован');
page.draft.status='archived';assert.equal(page.statusLabel,'Архив');
})().catch(error=>{console.error(error);process.exitCode=1});
''')


def test_validation_date_used_by_template_is_a_callable_method():
    run_node(r'''
assert.equal(typeof definition.methods.validationDate,'function');
assert.equal('validationDate' in definition.computed,false);
assert.equal(page.validationDate('2026-07-25T12:00:00'),'25.07.2026');
assert.equal(page.validationDate('2026-07-25'),'25.07.2026');
assert.equal(page.validationDate('not-a-date'),'');
assert.equal(page.validationDate(null),'');
''')


def test_conflict_and_unknown_write_result_preserve_edits_and_disable_repeat():
    run_node(r'''
(async()=>{
page.form.content.name='Несохранённое';let calls=0;
page.request=async()=>{calls++;throw Object.assign(Error('Конфликт'),{status:409})};await page.save();
assert.equal(page.form.content.name,'Несохранённое');assert.equal(page.conflict,true);assert.equal(page.locked,true);await page.save();assert.equal(calls,1);
page.hydrate(structuredClone(fixture));page.form.content.name='Новая попытка';
page.request=async()=>{calls++;if(calls===2)return {success:true};throw TypeError('Network failed')};await page.save();
assert.equal(page.form.content.name,'Новая попытка');assert.equal(page.uncertain,true);assert.equal(page.locked,true);await page.save();assert.equal(calls,3);
})().catch(e=>{console.error(e);process.exitCode=1});
''')


def test_active_operation_notice_uses_exact_operation_and_never_unlocks_uncertain():
    run_node(r'''
assert.equal(page.activeOperationNotice,null);assert.equal(page.canPublish,true);
page.data.active_operation_id=9;
page.data.operations=[{id:8,status:'submitting'},{id:9,status:'uncertain',next_poll_at:null}];
assert.equal(page.statusLabel,'Нужна сверка');assert.match(page.activeOperationNotice.description,/Автоматическая проверка остановлена/);
assert.equal(page.locked,true);assert.equal(page.canPublish,false);
page.data.operations[1].next_poll_at='2026-09-26T10:00:00';
assert.equal(page.statusLabel,'Нужна сверка');assert.match(page.activeOperationNotice.description,/Проверка запланирована/);
for(const [status,label] of Object.entries({queued:'В очереди на отправку',submitting:'Отправка выполняется',submitted:'Проверяем результат отправки',polling:'Проверяем результат отправки'})){
 page.data.operations[1].status=status;assert.equal(page.statusLabel,label);assert.equal(page.canPublish,false);
}
for(const status of ['new_provider_status','succeeded','failed']){
 page.data.operations[1].status=status;assert.equal(page.statusLabel,'Нужно проверить состояние отправки');assert.equal(page.locked,true);
}
page.data.operations.pop();assert.equal(page.statusLabel,'Нужно проверить состояние отправки');assert.equal(page.locked,true);
page.data.active_operation_id=null;assert.equal(page.activeOperationNotice,null);assert.equal(page.canPublish,true);
''')


def test_dictionary_late_result_and_unobserved_value_are_rejected():
    run_node(r'''
(async()=>{
const pending=[];page.request=(url,options)=>new Promise((resolve,reject)=>pending.push({url,options,resolve,reject}));
page.openDictionary(page.data.definitions[0]);await new Promise(r=>setTimeout(r,270));
page.dictionary.query='новый';page.searchDictionary();await new Promise(r=>setTimeout(r,270));
assert.equal(pending[0].options.signal.aborted,true);
pending[1].resolve({items:[{id:'2',value:'Новый'}],product_type_id:7,attribute_id:'31'});await new Promise(setImmediate);
pending[0].resolve({items:[{id:'1',value:'Старый'}],product_type_id:7,attribute_id:'31'});await new Promise(setImmediate);
assert.deepEqual(page.dictionary.items,[{id:'2',value:'Новый'}]);page.chooseValue({id:'3',value:'Выдуманный'});assert.equal(page.form.attributes[0].values[0].value,'Бренд');
page.chooseValue({id:'2',value:'Новый'});assert.deepEqual(page.form.attributes[0].values,[{dictionary_value_id:'2',value:'Новый'}]);
})().catch(e=>{console.error(e);process.exitCode=1});
''')


def test_publication_requires_explicit_confirmation_and_uses_stable_key_once():
    run_node(r'''
(async()=>{
page.draft.published_listing_id=42;let calls=[];global.location={assign:url=>{assert.equal(url,'/operations/9')}};
page.request=async(url,options)=>{calls.push({url,body:JSON.parse(options.body)});return {operation:{id:9}}};
await page.publish();assert.equal(calls.length,0);
page.confirmedWrite=true;await page.publish();await page.publish();assert.equal(calls.length,1);
assert.deepEqual(calls[0],{url:'/update',body:{expected_version:3,idempotency_key:'stable:v3',confirm_write:true}});
})().catch(e=>{console.error(e);process.exitCode=1});
''')


def test_user_issue_labels_preserve_meaning_and_weak_path_matches_are_not_suggestions():
    run_node(r'''
assert.equal(page.issueLabel({code:'physical_fact_required',field:'dimensions.width',message:'technical'}),'Ширина упаковки: укажите подтверждённое значение больше нуля.');
assert.equal(page.issueLabel({code:'physical_fact_not_integer',field:'dimensions.weight',message:'technical'}),'Вес с упаковкой: в выбранной единице нужно целое число.');
assert.equal(page.issueLabel({code:'currency_code_required',message:'technical'}),'Выберите валюту «Российский рубль».');
assert.equal(page.issueLabel({code:'new_provider_rule',message:'Особое требование площадки'}),'Особое требование площадки');
page.data.suggestions=[{id:1,score:30},{id:2,score:55},{id:3,score:100},{id:4}];
assert.deepEqual(page.initialTypeOptions().map(x=>x.id),[2,3]);
assert.deepEqual(page.data.suggestions.map(x=>x.id),[1,2,3,4]);
''')


def test_preflight_issue_navigation_uses_attribute_metadata_and_focuses_exact_field():
    run_node(r'''
const focused=[];
const makeField = id => ({
    dataset:{attribute:id},
    closest:()=>null,
    querySelector:()=>({focus:()=>focused.push('input:'+id)}),
    scrollIntoView:()=>focused.push('scroll:'+id),
    focus:()=>focused.push('field:'+id),
});
const fields=['22232','23536'].map(makeField);
global.document.querySelectorAll=()=>fields;
page.form.attributes=[{attribute_id:'31'},{attribute_id:'32'},{attribute_id:'22232'}];
page.goToIssue({field:'attributes[2].values',code:'attribute_max_value_count',attribute_id:'22232',actual_count:3,max_value_count:1});
assert.equal(page.section,'attributes');
assert.equal(page.attrQuery,'22232');
assert.deepEqual(focused,['scroll:22232','input:22232']);
focused.length=0;
page.goToIssue({field:'attributes.23536',code:'required_attribute_missing'});
assert.equal(page.section,'attributes');
assert.equal(page.attrQuery,'23536');
assert.deepEqual(focused,['scroll:23536','input:23536']);
focused.length=0;
const title={scrollIntoView:()=>focused.push('scroll:title'),focus:()=>focused.push('title')};
global.document.querySelectorAll=()=>[];
global.document.getElementById=id=>id==='ode-name'?title:null;
page.goToIssue({field:'content.name',code:'name_required'});
assert.equal(page.section,'content');
assert.deepEqual(focused,['scroll:title','title']);
''')
