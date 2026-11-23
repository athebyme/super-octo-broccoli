"""Real HTTP/CSRF/Vue repair with a synthetic 200-row DB. No provider network."""
import base64
from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import socket
import tempfile
import threading
import time
from urllib.parse import parse_qs, urlsplit
from decimal import Decimal

import requests
from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server

OUT=Path('/artifacts');OUT.mkdir(exist_ok=True)
ASSETS=Path(__file__).with_name('assets')
report={'status':'running','checks':[],'layouts':[],'js_errors':[],'unexpected_http':[],
        'external':[],'provider_attempts':0,'provider_write_callbacks':0,
        'synthetic_photo_responses':0,'posts':0,'rows':200,'supplier_source':None,
        'source_refresh':None}
temporary=tempfile.TemporaryDirectory(prefix='ozon-bulk-repair-')
os.environ.update(DATABASE_URL='sqlite:///'+str(Path(temporary.name)/'app.sqlite'),SKIP_SCHEDULER='1',
    SECRET_KEY='synthetic-bulk-repair-ci',ENCRYPTION_KEY=base64.urlsafe_b64encode(b'0'*32).decode(),
    IMAGE_LAB_INLINE_WORKER='0',MARKETPLACE_OZON_ENABLED='1',MARKETPLACE_OZON_PUBLICATION_ENABLED='0',
    MARKETPLACE_OZON_COMMERCIAL_WRITES_ENABLED='0',MARKETPLACE_OZON_AUTO_PUBLISH_ENABLED='0',
    OZON_RATE_LIMIT_DIR=str(Path(temporary.name)/'limits'))

def no_network(*args,**kwargs):
    report['provider_attempts']+=1
    raise AssertionError('External Python network forbidden')
requests.sessions.Session.request=no_network
socket.create_connection=no_network
from seller_platform import app
from models import (db,BackgroundJob,ImportedProduct,MarketplaceAttributeDefinition,
                    MarketplaceAttributeValue,MarketplaceProductDraft,MarketplaceOperation)
from tests.ozon_release.bulk_repair_seed import bulk_seed, SOURCE_REVIEW_CASES
from tests.ozon_release.seed import USERNAME,PASSWORD,PHOTO
from services.ozon_bulk_upload import OzonBulkUploadService
from services.marketplace_drafts import MarketplaceDraftService

app.config.update(TESTING=True,WTF_CSRF_ENABLED=True,SESSION_COOKIE_SECURE=False)
fixture=bulk_seed(app)
logging.getLogger('werkzeug').setLevel(logging.ERROR)
server=make_server('127.0.0.1',0,app,threaded=True);base='http://127.0.0.1:'+str(server.server_port)
worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
path='/marketplaces/ozon/uploads/'+fixture['repair_job']+'/repair'
tnved_job_uid='ozon-upload-'+'c'*32
tnved_path='/marketplaces/ozon/uploads/'+tnved_job_uid+'/repair'
selection_apply_path='/marketplaces/ozon/uploads/selection/repair/apply'
source_review_draft_ids={index:fixture['supplier_source_drafts'][index]
                         for index in (196,197,198)}
source_review_selection=[source_review_draft_ids[index] for index in (196,197,198)]
source_review_control_id=fixture['repair_drafts'][195]
source_refresh_draft_ids=fixture['source_refresh_drafts']
source_refresh_source_ids=fixture['source_refresh_sources']
source_refresh_selection=[source_refresh_draft_ids[key] for key in (
    'stale_name','stale_description','fresh_selected','stale_unselected',
)]
source_refresh_target_ids=source_refresh_selection[:2]
source_refresh_before=None
manifest=json.loads((ASSETS/'manifest.json').read_text())
for item in manifest.values():assert hashlib.sha256((ASSETS/item['file']).read_bytes()).hexdigest()==item['sha256']
svg=b'<svg xmlns="http://www.w3.org/2000/svg" width="360" height="480"><rect width="360" height="480" fill="#eee9df"/><rect x="80" y="85" width="200" height="300" rx="12" fill="#678b9e"/><circle cx="180" cy="235" r="60" fill="#faf9f7"/></svg>'
fault={'mode':None}
page=None
supplier_source_apply_checks=[]
source_refresh_apply_checks=[]


def source_value_id(source_index, attribute_id, value):
    type_index=source_index%6
    ordinals={'Черный':1,'Синий':2,'Розовый':3,'Зелёный':4,'Белый':5,
              'Россия':1,'Китай':2,'Беларусь':3}
    attribute_offset=0 if attribute_id=='10096' else 1000
    return str(910_000+type_index*10_000+attribute_offset+ordinals[value])

def supplier_review_row(index,attribute_id):
    draft_id=fixture['supplier_source_drafts'][index]
    return page.locator(
        '[data-supplier-source-row][data-product-id="'+str(draft_id)+'"]'
        '[data-field="'+str(attribute_id)+'"]'
    )

def assert_review_dictionary_id_sets(index,attribute_id,expected):
    row=supplier_review_row(index,attribute_id)
    disclosure=row.locator('details.orb-source-ids')
    assert disclosure.count()==1
    disclosure.locator('summary').click()
    rendered=disclosure.evaluate('''node => Object.fromEntries(
        Array.from(node.querySelectorAll('dl > div')).map(section => [
            section.querySelector('dt')?.textContent.trim(),
            Array.from(section.querySelectorAll('dd span')).map(cell => {
                const match=cell.textContent.match(/ID ([0-9]+) в справочнике Ozon/);
                return match ? match[1] : '';
            })
        ])
    )''')
    expected_ids={label:[source_value_id(index,attribute_id,value) for value in values]
                  for label,values in expected.items()}
    assert rendered==expected_ids,(index,attribute_id,rendered,expected_ids)

def clear_supplier_dictionary_values(index,attribute_id):
    field=supplier_card(index).locator('.ode-attribute[data-attribute="'+attribute_id+'"]')
    while field.locator('.ode-value-chip button').count():
        field.locator('.ode-value-chip button').first.click()
    assert field.locator('.ode-value-chip').count()==0

def snapshot_drafts():
    with app.app_context():
        return {
            draft.id:{
                'version':draft.version,
                'product_type_id':draft.product_type_id,
                'status':draft.status,
                'content_json':draft.content_json,
                'attributes_json':draft.attributes_json,
                'complex_attributes_json':draft.complex_attributes_json,
                'attribute_removals_json':draft.attribute_removals_json,
                'media_json':draft.media_json,
                'dimensions_json':draft.dimensions_json,
                'barcodes_json':draft.barcodes_json,
                'commercial_json':draft.commercial_json,
                'offer_id':draft.offer_id,
            } for draft in MarketplaceProductDraft.query.order_by(MarketplaceProductDraft.id).all()
        }

def snapshot_source_refresh_drafts():
    basic=snapshot_drafts()
    with app.app_context():
        return {
            draft_id:{
                **basic[draft_id],
                'source_fact_hash':(draft:=db.session.get(MarketplaceProductDraft,draft_id)).source_fact_hash,
                'source_facts_json':draft.source_facts_json,
                'provenance_json':draft.provenance_json,
                'validation_result_json':draft.validation_result_json,
                'validation_status':draft.validation_status,
            }
            for draft_id in source_refresh_draft_ids.values()
        }

def snapshot_source_refresh_sources():
    with app.app_context():
        sources={
            name:db.session.get(ImportedProduct,source_id)
            for name,source_id in source_refresh_source_ids.items()
        }
        return {
            name:{
                'title':source.title,
                'description':source.description,
                'original_data':source.original_data,
            }
            for name,source in sources.items()
        }

def bridge(route):
    request=route.request;u=urlsplit(request.url)
    if u.hostname=='127.0.0.1' and u.port==server.server_port:
        if request.method not in ('GET','HEAD'):
            assert u.path in ('/login',path+'/apply',tnved_path+'/apply',selection_apply_path),u.path
            if u.path in (path+'/apply',tnved_path+'/apply',selection_apply_path):report['posts']+=1
            if u.path==selection_apply_path:
                query=parse_qs(u.query,keep_blank_values=True)
                assert query.get('account_id')==[str(fixture['account_id'])]
                form=parse_qs(request.post_data or '',keep_blank_values=True)
                if form.get('refresh_source')==['1']:
                    assert query.get('draft_ids')==[','.join(str(i) for i in source_refresh_selection)]
                    assert request.headers.get('accept')=='application/json'
                    assert form.get('confirm_local')==['1']
                    assert form.get('csrf_token') and len(form['csrf_token'])==1 and form['csrf_token'][0]
                    selected=[int(value) for value in form.get('selected_draft_id',[])]
                    assert selected==source_refresh_target_ids
                    assert len(selected)==len(set(selected))
                    expected_keys={
                        'csrf_token','refresh_source','confirm_local','selected_draft_id',
                        *{
                            key
                            for draft_id in source_refresh_target_ids
                            for key in (
                                f'row_{draft_id}_draft_version',
                                f'row_{draft_id}_imported_product_id',
                            )
                        },
                    }
                    assert set(form)==expected_keys,(set(form),expected_keys)
                    assert source_refresh_before is not None
                    for draft_id in source_refresh_target_ids:
                        assert form.get(f'row_{draft_id}_draft_version')==[
                            str(source_refresh_before[draft_id]['version'])
                        ]
                        source_name=next(
                            name for name,target_id in source_refresh_draft_ids.items()
                            if target_id==draft_id
                        )
                        assert form.get(f'row_{draft_id}_imported_product_id')==[
                            str(source_refresh_source_ids[source_name])
                        ]
                    source_refresh_apply_checks.append({
                        'account_and_selection_exact':True,
                        'stale_selected_targets_only_in_order':True,
                        'fresh_selected_and_unselected_stale_excluded':True,
                        'per_target_version_and_source_exact':True,
                        'no_ordinary_edit_fields':True,
                        'csrf_and_explicit_confirmation_present':True,
                    })
                    result=route.fetch(max_redirects=0)
                    if result.status>=400:
                        report['unexpected_http'].append({'path':u.path,'status':result.status})
                    assert result.status==200
                    response_data=result.json()
                    refresh_report=response_data['report']
                    response_rows=refresh_report['rows']
                    response_ids=[row['draft_id'] for row in response_rows]
                    assert response_data['success'] is True
                    assert response_data['mode']=='draft_selection'
                    assert re.fullmatch(r'[0-9a-f]{64}',response_data['scope_key'])
                    assert refresh_report['mode']=='draft_selection'
                    assert refresh_report['scope_key']==response_data['scope_key']
                    assert refresh_report['source_refresh'] is True
                    assert refresh_report['total']==len(source_refresh_target_ids)
                    assert response_ids==source_refresh_target_ids
                    for row in response_rows:
                        assert row['status']=='needs_input'
                        assert row['version']==source_refresh_before[row['draft_id']]['version']+2
                    source_refresh_apply_checks[-1]['selection_scope_and_exact_receipt']=True
                    route.fulfill(response=result)
                    return
                assert query.get('draft_ids')==[','.join(str(i) for i in source_review_selection)]
                selected=[int(value) for value in form.get('selected_draft_id',[])]
                assert len(selected)==3 and set(selected)==set(source_review_selection)
                assert len(selected)==len(set(selected))
                assert form.get('confirm_local')==['1']
                acknowledged_id=source_review_draft_ids[197]
                matched_id=source_review_draft_ids[196]
                excluded_id=source_review_draft_ids[198]
                unknown_id=source_review_draft_ids[196]
                ack_key=f'row_{acknowledged_id}_source_review_json'
                ack_rows=form.get(ack_key,[])
                assert len(ack_rows)==1
                decisions=json.loads(ack_rows[0])
                assert set(decisions)=={'10096'}
                decision=decisions['10096']
                assert set(decision)=={'receipt','ack','proposal_sha256'}
                assert decision['ack'] is True and isinstance(decision['receipt'],str) and decision['receipt']
                assert re.fullmatch(r'[0-9a-f]{64}',decision['proposal_sha256'])
                matched_key=f'row_{matched_id}_source_review_json'
                matched_rows=form.get(matched_key,[])
                assert len(matched_rows)==1
                matched_decisions=json.loads(matched_rows[0])
                assert set(matched_decisions)=={'10096'}
                matched_decision=matched_decisions['10096']
                assert set(matched_decision)=={'receipt','ack','proposal_sha256'}
                assert matched_decision['ack'] is False
                assert isinstance(matched_decision['receipt'],str) and matched_decision['receipt']
                assert re.fullmatch(r'[0-9a-f]{64}',matched_decision['proposal_sha256'])
                saved_match_country=json.loads(form[f'row_{matched_id}_source_review_json'][0])
                assert set(saved_match_country)=={'10096'}
                excluded_color=f'row_{excluded_id}_attribute_10096'
                excluded_color_id=f'row_{excluded_id}_attribute_value_id_10096'
                assert excluded_color not in form and excluded_color_id not in form
                assert f'row_{excluded_id}_attribute_4389' in form
                assert f'row_{unknown_id}_source_review_json' in form
                assert f'row_{unknown_id}_attribute_10096' in form
                assert f'row_{unknown_id}_attribute_4389' in form
                expected_fields={
                    (196,'4389'):('Россия',source_value_id(196,'4389','Россия')),
                    (197,'10096'):('Черный',source_value_id(197,'10096','Черный')),
                    (198,'4389'):('Беларусь',source_value_id(198,'4389','Беларусь')),
                }
                for (source_index,attribute_id),(value,value_id) in expected_fields.items():
                    draft_id=source_review_draft_ids[source_index]
                    assert form.get(f'row_{draft_id}_attribute_{attribute_id}')==[value]
                    assert form.get(f'row_{draft_id}_attribute_value_id_{attribute_id}')==[value_id]
                matched_color_key=f'row_{matched_id}_attribute_10096'
                assert form.get(matched_color_key)==['Белый\nРозовый']
                assert form.get(f'row_{matched_id}_attribute_value_id_10096')==['']
                excluded_country_review=json.loads(form[f'row_{excluded_id}_source_review_json'][0])
                assert set(excluded_country_review)=={'4389'}
                assert excluded_country_review['4389']['ack'] is False
                assert re.fullmatch(r'[0-9a-f]{64}',excluded_country_review['4389']['proposal_sha256'])
                supplier_source_apply_checks.append({
                    'selected_exact':True,
                    'ack_exact':True,
                    'matched_multi_value_exact_set':True,
                    'matched_country_no_ack':True,
                    'excluded_color_omitted':True,
                    'unknown_evidence_unacknowledged':True,
                    'exact_field_values':True,
                })
        if u.path==path and request.headers.get('accept')=='application/json':
            if fault['mode']=='offline':route.abort();return
            if fault['mode']=='session':route.fulfill(status=401,json={'success':False,'error':'Expired'});return
            if fault['mode']=='foreign':
                result=route.fetch(max_redirects=0);body=result.json();body['editor']['account_id']=fixture['foreign_account_id']
                route.fulfill(status=200,json=body);return
        if u.path.startswith('/api/photos/imported-product/'):
            parts=u.path.split('/')
            allowed_sources=(set(fixture['repair_sources'])
                             | set(fixture['supplier_source_sources'].values())
                             | set(source_refresh_source_ids.values()))
            assert int(parts[-2]) in allowed_sources
            route.fulfill(body=svg,content_type='image/svg+xml');return
        result=route.fetch(max_redirects=0)
        if result.status>=400:report['unexpected_http'].append({'path':u.path,'status':result.status})
        if u.path==path+'/apply' and fault['mode']=='lost':
            assert result.status==200;route.abort();return
        route.fulfill(response=result);return
    asset=manifest.get(request.url) or manifest.get(request.url.rstrip('/'))
    if asset:route.fulfill(body=(ASSETS/asset['file']).read_bytes(),content_type=asset['content_type']);return
    if request.method in ('GET','HEAD') and request.url==PHOTO:
        report['synthetic_photo_responses']+=1
        route.fulfill(body=svg,content_type='image/svg+xml');return
    report['external'].append({'host':u.hostname,'path':u.path});route.abort()

def passed(name):
    assert not report['js_errors'],report['js_errors']
    report['checks'].append(name);print(json.dumps({'bulk_browser_check':name}),flush=True)

def layout(name):
    for theme in ('light','dark'):
        for width in (1440,768,390,320):
            page.set_viewport_size({'width':width,'height':1000})
            page.evaluate('(v)=>document.documentElement.dataset.theme=v',theme)
            page.wait_for_timeout(60)
            page.wait_for_function("!document.querySelector('.main-content')?.getAnimations().some(a=>a.playState==='running')", timeout=2000)
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'),(name,theme,width)
            modal=page.locator('dialog[open]')
            if modal.count():assert modal.evaluate('(n)=>n.scrollWidth<=n.clientWidth+1'),(name,theme,width,'dialog overflow')
            report['layouts'].append({'name':name,'theme':theme,'width':width})
            if width in (1440,390):page.screenshot(path=str(OUT/f'bulk-{name}-{theme}-{width}.png'),animations='disabled')
    page.set_viewport_size({'width':1440,'height':1000});page.evaluate('document.documentElement.dataset.theme="light"')

def card(index):return page.locator('[data-draft="'+str(fixture['repair_drafts'][index])+'"]')
def set_search(value):page.get_by_role('searchbox',name='Поиск по названию или артикулу').fill(value)
def expanded(index):
    c=card(index)
    if not c.locator('.orb-editor').count():c.get_by_role('button',name='Изменить',exact=True).click()
    return c
def select(index):card(index).get_by_role('checkbox',name='Выбрать Набор для проверки '+f'{index:03}',exact=True).check()
def save():
    page.get_by_role('button',name='Проверить изменения',exact=True).click()
    page.get_by_role('dialog').get_by_role('button',name='Сохранить и проверить',exact=True).click()
    page.wait_for_function("document.querySelector('#ozon-bulk-repair section[aria-busy]')?.getAttribute('aria-busy')==='false'")
def copy_field(key,index):
    page.get_by_role('button',name='Заполнить поле',exact=True).click()
    dialog=page.get_by_role('dialog')
    dialog.get_by_label('Поле',exact=True).select_option(key)
    dialog.get_by_label('Взять из карточки',exact=True).check()
    dialog.get_by_label('Карточка с нужным значением',exact=True).select_option(str(fixture['repair_drafts'][index]))
    dialog.get_by_role('button',name='Посмотреть изменения',exact=True).click()
    return dialog

def supplier_card(index):
    draft_id=fixture['supplier_source_drafts'][index]
    return page.locator('[data-draft="'+str(draft_id)+'"]')

def choose_supplier_dictionary_value(index,attribute_id,value):
    field=supplier_card(index).locator('.ode-attribute[data-attribute="'+attribute_id+'"]')
    field.get_by_role('button',name='Выбрать из справочника',exact=True).click()
    dialog=page.get_by_role('dialog')
    dialog.get_by_role('searchbox',name='Поиск значения',exact=True).fill(value)
    option=dialog.locator('.orb-picker-results button').filter(has_text=value).first
    option.wait_for()
    option.click()

def supplier_review_mobile_layout():
    results=[]
    for theme in ('light','dark'):
        page.set_viewport_size({'width':320,'height':1000})
        page.evaluate('(v)=>document.documentElement.dataset.theme=v',theme)
        page.wait_for_timeout(60)
        page.wait_for_function("!document.querySelector('.main-content')?.getAnimations().some(a=>a.playState==='running')",timeout=2000)
        dialog=page.locator('dialog[open]')
        assert dialog.count()==1
        geometry=page.evaluate('''() => {
            const dialog=document.querySelector('dialog[open]');
            const rect=dialog.getBoundingClientRect();
            return {viewport:innerWidth,rootScrollWidth:document.documentElement.scrollWidth,
                dialogClientWidth:dialog.clientWidth,dialogScrollWidth:dialog.scrollWidth,
                left:rect.left,right:rect.right};
        }''')
        assert geometry['rootScrollWidth']<=321,(theme,geometry)
        assert geometry['dialogScrollWidth']<=geometry['dialogClientWidth']+1,(theme,geometry)
        assert geometry['left']>=-1 and geometry['right']<=321,(theme,geometry)
        page.keyboard.press('Tab')
        focus_info=page.evaluate('''() => {
            const node=document.activeElement;
            const style=node ? getComputedStyle(node) : null;
            const rect=node ? node.getBoundingClientRect() : null;
            const outline=style ? parseFloat(style.outlineWidth) || 0 : 0;
            return {inside:!!node?.closest('dialog[open]'),focusVisible:!!node?.matches(':focus-visible'),
                ring:!!style && ((outline>=2 && style.outlineStyle!=='none') || style.boxShadow!=='none'),
                outlineWidth:style ? outline : 0,boxShadow:style?.boxShadow || 'none',
                width:rect?.width || 0,height:rect?.height || 0,
                left:rect?.left ?? null,right:rect?.right ?? null};
        }''')
        assert focus_info and focus_info['inside'] and focus_info['focusVisible'] and focus_info['ring'],(theme,focus_info)
        assert focus_info['width']>0 and focus_info['height']>0
        assert focus_info['left']>=-1 and focus_info['right']<=321,(theme,focus_info)
        result={'width':320,'theme':theme,'overflow_px':max(0,geometry['rootScrollWidth']-320),
                'dialog_overflow_px':max(0,geometry['dialogScrollWidth']-geometry['dialogClientWidth']),
                'focus_visible':True,'outline_width_px':focus_info['outlineWidth'],
                'focus_box_shadow':focus_info['boxShadow'],'focused_inside_dialog':True}
        result['immediate_tab_focus']=True
        result['tab_steps']=1
        results.append(result)
        page.screenshot(path=str(OUT/f'bulk-source-review-{theme}-320.png'),animations='disabled')
    page.set_viewport_size({'width':1440,'height':1000})
    page.evaluate('document.documentElement.dataset.theme="light"')
    return results

@contextmanager
def evidence():
    try:yield
    except Exception:
        if page and not page.is_closed():
            page.screenshot(path=str(OUT/'bulk-failure.png'),animations='disabled')
            (OUT/'bulk-failure-context.json').write_text(json.dumps({'url':page.url,'text':page.locator('body').inner_text()[:8000]},ensure_ascii=False))
        raise

try:
    with sync_playwright() as playwright,evidence():
        browser=playwright.chromium.launch(executable_path='/usr/bin/chromium',headless=True,args=['--no-sandbox','--disable-dev-shm-usage'])
        context=browser.new_context(viewport={'width':1440,'height':1000},service_workers='block')
        context.route('**/*',bridge)
        page=context.new_page();page.set_default_timeout(20000)
        page.on('pageerror',lambda error:report['js_errors'].append(str(error)))
        page.on('dialog',lambda dialog:dialog.accept())
        page.goto(base+'/login?next='+path)
        page.locator('[name=username]').fill(USERNAME);page.locator('[name=password]').fill(PASSWORD)
        stamp=time.monotonic();page.get_by_role('button',name='Войти',exact=True).click()
        page.locator('#ozon-bulk-repair:not([v-cloak]) article').first.wait_for()
        report['initial_http_render_seconds']=round(time.monotonic()-stamp,2)
        assert page.locator('article.orb-row').count()==15
        assert page.locator('.orb-filters select[name=group] option').count()==7
        page.wait_for_load_state('networkidle')
        layout('list');passed('real_login_two_hundred_rows_six_categories_fifteen_rendered')
        page.get_by_role('checkbox',name='Выбрать страницу',exact=True).check()
        page.get_by_role('navigation',name='Страницы исправления').get_by_role('button',name='Далее',exact=True).click()
        page.get_by_role('checkbox',name='Выбрать страницу',exact=True).check()
        page.reload();page.locator('#ozon-bulk-repair:not([v-cloak])').wait_for()
        assert 'Выбрано 30' in page.locator('.orb-toolbar').inner_text()
        page.get_by_role('button',name='Снять выбор',exact=True).click()
        page.get_by_role('button',name='Выбрать все по фильтру (200)',exact=True).click()
        assert 'Выбрано 200' in page.locator('.orb-toolbar').inner_text()
        review_button=page.get_by_role('button',name='Проверить изменения',exact=True)
        review_button.click()
        assert page.get_by_role('dialog').locator('.orb-review-list > li').count()==200
        layout('review-two-hundred')
        page.keyboard.press('Escape')
        assert not page.locator('dialog[open]').count()
        assert review_button.evaluate('(n)=>n===document.activeElement')
        assert report['posts']==0
        page.get_by_role('button',name='Снять выбор',exact=True).click()
        set_search('для проверки 00');assert page.locator('article.orb-row').count()==10
        assert 'q=' in page.url
        passed('selection_across_pages_reload_all_two_hundred_and_unicode_search')
        select(0);select(1)
        first=expanded(0)
        first.get_by_label('Работает от сети',exact=True).select_option('false')
        dialog=copy_field('attribute:701',0);assert 'Будет заполнено: 1' in dialog.inner_text()
        dialog.get_by_role('button',name='Заполнить в форме',exact=True).click()
        first.locator('.orb-attribute').filter(has_text='Материал').get_by_role('button',name='Выбрать из справочника').click()
        page.get_by_role('dialog').get_by_role('button',name='Хлопок',exact=True).click()
        dialog=copy_field('attribute:703',0);assert 'Будет заполнено: 1' in dialog.inner_text()
        layout('bulk-review');dialog.get_by_role('button',name='Заполнить в форме',exact=True).click()
        for index in (0,1):expanded(index).get_by_label('Цена продавца, ₽',exact=True).fill('1200,50')
        layout('editor');save()
        page.locator('.orb-receipt').wait_for()
        with app.app_context():
            for index in (0,1):
                draft=db.session.get(MarketplaceProductDraft,fixture['repair_drafts'][index])
                attrs={a['attribute_id']:a['values'] for a in json.loads(draft.attributes_json)}
                assert attrs['701']==[{'value':'false'}]
                assert attrs['703']==[{'dictionary_value_id':str(9000+index),'value':'Хлопок'}]
                assert Decimal(json.loads(draft.commercial_json)['price'])==Decimal('1200.50')
            assert MarketplaceOperation.query.count()==0
        passed('real_csrf_save_false_decimal_and_type_scoped_dictionary_copy')
        for index in (0,1):expanded(index).get_by_label('Цена продавца, ₽',exact=True).fill('1400')
        with app.app_context():
            draft=db.session.get(MarketplaceProductDraft,fixture['repair_drafts'][0]);draft.version+=1;db.session.commit()
        save()
        card(0).get_by_role('button',name='Сравнить значения',exact=True).wait_for()
        assert expanded(0).get_by_label('Цена продавца, ₽',exact=True).input_value()=='1400'
        card(0).get_by_role('button',name='Сравнить значения',exact=True).click()
        layout('conflict')
        page.get_by_role('dialog').get_by_role('button',name='Перенести мои правки',exact=True).click()
        save()
        assert not card(0).locator('.orb-row-conflict').count()
        with app.app_context():
            assert all(json.loads(db.session.get(MarketplaceProductDraft,i).commercial_json)['price']=='1400' for i in fixture['repair_drafts'][:2])
        passed('partial_receipt_keeps_failed_input_and_explicit_conflict_rebase')
        page.get_by_role('button',name='Снять выбор',exact=True).click();select(2)
        third=expanded(2);third.get_by_role('button',name='Выбрать тип',exact=True).click()
        dialog=page.get_by_role('dialog');dialog.get_by_role('searchbox',name='Поиск типа товара',exact=True).fill('Освещение')
        dialog.get_by_role('button',name='Освещение',exact=False).first.click()
        assert 'Будут заменены характеристики' in dialog.inner_text()
        assert not dialog.get_by_role('checkbox').is_checked()
        layout('type-change');dialog.get_by_role('button',name='Применить тип в форме',exact=True).click()
        assert 'Сохраните его' in expanded(2).inner_text()
        save()
        assert not card(2).locator('.orb-row-conflict').count()
        passed('category_change_impact_review_and_mapping_never_auto_confirmed')
        third=expanded(2);third.get_by_label('Описание',exact=True).fill('Сохранено при потерянном ответе.')
        before_posts=report['posts'];fault['mode']='lost';save()
        page.get_by_text('Сначала нужно сверить сохранение',exact=True).wait_for()
        assert report['posts']==before_posts+1
        page.reload();page.locator('#ozon-bulk-repair:not([v-cloak])').wait_for()
        page.get_by_text('Сначала нужно сверить сохранение',exact=True).wait_for()
        assert page.get_by_role('button',name='Проверить изменения',exact=True).is_disabled()
        fault['mode']=None;page.get_by_role('button',name='Сверить сохранение',exact=True).click()
        page.wait_for_function("!document.querySelector('.orb-banner--warning strong')")
        assert report['posts']==before_posts+1
        assert expanded(2).get_by_label('Описание',exact=True).input_value()=='Сохранено при потерянном ответе.'
        passed('lost_post_no_retry_marker_survives_reload_and_real_readback')
        for mode in ('offline','foreign'):
            fault['mode']=mode
            page.locator('.orb-header').get_by_role('button',name='Прочитать текущее состояние',exact=True).click()
            page.locator('p.orb-banner[role=alert]').wait_for()
            assert page.locator('article.orb-row').count()==10
        layout('read-error');passed('offline_and_foreign_response_preserve_visible_input')
        expanded(2).get_by_label('Описание',exact=True).fill('Правка сохранится после повторного входа.')
        fault['mode']='session'
        page.locator('.orb-header').get_by_role('button',name='Прочитать текущее состояние',exact=True).click()
        page.get_by_role('link',name='Войти снова в новой вкладке',exact=True).wait_for()
        assert page.get_by_role('button',name='Проверить изменения',exact=True).is_disabled()
        layout('session');passed('session_expiry_preserves_form_and_blocks_saves')
        context.clear_cookies()
        login=context.new_page();login.goto(base+'/login?next='+path)
        login.locator('[name=username]').fill(USERNAME);login.locator('[name=password]').fill(PASSWORD)
        login.get_by_role('button',name='Войти',exact=True).click()
        login.locator('#ozon-bulk-repair:not([v-cloak]) article').first.wait_for();login.close()
        fault['mode']=None
        page.get_by_role('button',name='Проверить вход',exact=True).click()
        page.wait_for_function("document.querySelector('#ozon-bulk-repair section[aria-busy]')?.getAttribute('aria-busy')==='false'")
        assert expanded(2).get_by_label('Описание',exact=True).input_value()=='Правка сохранится после повторного входа.'
        before_posts=report['posts'];save();assert report['posts']==before_posts+1
        with app.app_context():
            saved=db.session.get(MarketplaceProductDraft,fixture['repair_drafts'][2])
            assert json.loads(saved.content_json)['description']=='Правка сохранится после повторного входа.'
        passed('real_relogin_refreshes_csrf_preserves_dirty_input_and_saves')
        with app.app_context():
            draft_ids=[fixture['repair_drafts'][0],fixture['repair_drafts'][6]]
            source_draft=db.session.get(MarketplaceProductDraft,draft_ids[0])
            product_type=source_draft.product_type
            target_draft=db.session.get(MarketplaceProductDraft,draft_ids[1])
            assert product_type is not None and target_draft.product_type_id==source_draft.product_type_id
            tnved=MarketplaceAttributeDefinition(
                marketplace_id=product_type.marketplace_id,
                product_type_id=product_type.id,
                external_attribute_id='22232',
                name='ТН ВЭД',
                data_type='String',
                is_required=True,
                dictionary_id='synthetic-duplicate-tnved',
                max_value_count=1,
                is_available=True,
                is_enabled=True,
                restriction_value_ids_json=json.dumps(['700001','700002']),
                last_seen_at=datetime.utcnow(),
                values_synced_at=datetime.utcnow(),
                values_sync_status='success',
                values_snapshot_hash='synthetic-duplicate-tnved-values',
                values_version=1,
                values_count=2,
            )
            db.session.add(tnved);db.session.flush()
            for external_id in ('700001','700002'):
                db.session.add(MarketplaceAttributeValue(
                    marketplace_id=product_type.marketplace_id,
                    product_type_id=product_type.id,
                    attribute_id=tnved.id,
                    external_value_id=external_id,
                    value='3304990000',
                    value_normalized='3304990000',
                    is_available=True,
                    last_seen_at=datetime.utcnow(),
                ))
            now=datetime.utcnow().isoformat()
            run=BackgroundJob(job_uid=tnved_job_uid,seller_id=fixture['seller_id'],
                job_type=OzonBulkUploadService.JOB_TYPE,status='completed',total=len(draft_ids))
            progress={'version':OzonBulkUploadService.DOCUMENT_VERSION,'source':'products',
                'account_id':fixture['account_id'],'account_label':'Synthetic TN VED','created_at':now,
                'updated_at':now,'items':[]}
            for draft_id in draft_ids:
                draft=db.session.get(MarketplaceProductDraft,draft_id)
                progress['items'].append({'imported_product_id':draft.imported_product_id,'draft_id':draft.id,
                    'offer_id':draft.offer_id,'title':draft.imported_product.title,'action':'create',
                    'status':'needs_input','code':'required_attribute_missing',
                    'message':'Выберите точное значение ТН ВЭД.','updated_at':now})
            OzonBulkUploadService._store_progress(run,progress)
            db.session.add(run);db.session.flush();OzonBulkUploadService._persist(run,progress)
            db.session.commit()
        page.goto(base+tnved_path);page.locator('#ozon-bulk-repair:not([v-cloak]) article').first.wait_for()
        for draft_id in draft_ids:
            page.locator('article[data-draft="'+str(draft_id)+'"]').get_by_role('checkbox').check()
        page.get_by_role('button',name='Заполнить поле',exact=True).click()
        dialog=page.get_by_role('dialog');dialog.get_by_label('Поле',exact=True).select_option('attribute:22232')
        dialog.get_by_role('button',name='Выбрать из справочника',exact=True).click()
        options=dialog.locator('.orb-inline-picker .orb-picker-results button')
        options.first.wait_for();assert options.count()==2
        assert 'ID 700001 в справочнике Ozon' in options.nth(0).inner_text()
        assert 'ID 700002 в справочнике Ozon' in options.nth(1).inner_text()
        options.nth(1).click();dialog.get_by_role('button',name='Посмотреть изменения',exact=True).click()
        assert 'Будет заполнено: 2' in dialog.inner_text()
        assert dialog.locator('.orb-review-list').inner_text().count('ID 700002 в справочнике Ozon')==2
        dialog.get_by_role('button',name='Заполнить в форме',exact=True).click()
        page.get_by_role('button',name='Проверить изменения',exact=True).click()
        review=page.get_by_role('dialog')
        assert 'ID 700002 в справочнике Ozon' in review.inner_text()
        before_posts=report['posts'];review.get_by_role('button',name='Сохранить и проверить',exact=True).click()
        page.wait_for_function("document.querySelector('#ozon-bulk-repair section[aria-busy]')?.getAttribute('aria-busy')==='false'")
        assert report['posts']==before_posts+1
        with app.app_context():
            for draft_id in draft_ids:
                saved=db.session.get(MarketplaceProductDraft,draft_id)
                attrs={item['attribute_id']:item['values'] for item in json.loads(saved.attributes_json)}
                assert attrs['22232']==[{'dictionary_value_id':'700002','value':'3304990000'}]
        passed('same_type_tnved_bulk_keeps_explicit_duplicate_label_id_through_local_save')

        # Enter through the seller's ordinary draft list and exact edit-selection
        # route. The three rows use three real synthetic type/category scopes.
        source_list_url=(base+'/marketplaces/drafts?account_id='
                         +str(fixture['account_id'])+'&per_page=100')
        page.goto(source_list_url)
        page.locator('#ozon-drafts-list:not([v-cloak])').wait_for()
        page.get_by_role('button',name='Изменить карточки',exact=True).click()
        with app.app_context():
            source_type_ids={
                db.session.get(MarketplaceProductDraft,source_review_draft_ids[index]).product_type_id
                for index in (196,197,198)
            }
            assert len(source_type_ids)==3
        for index in (196,197,198):
            draft_id=source_review_draft_ids[index]
            control=page.locator('[data-draft-selection-check="'+str(draft_id)+'"]')
            control.wait_for()
            control.check()
        assert 'Для изменения выбрано 3' in page.locator('[data-draft-selection-summary]').inner_text()
        page.locator('[data-draft-selection-start]').click()
        page.wait_for_url('**/marketplaces/ozon/uploads/selection/repair?*')
        selection_query=parse_qs(urlsplit(page.url).query)
        assert selection_query.get('account_id')==[str(fixture['account_id'])]
        assert selection_query.get('draft_ids')==[','.join(str(i) for i in source_review_selection)]
        page.locator('[data-draft-selection-editor]').wait_for()
        editor_bootstrap=json.loads(page.locator('#orb-bootstrap').text_content())
        editor_rows={
            row['draft_id']:row
            for group in editor_bootstrap['editor']['groups']
            for row in group['rows']
        }
        for index in (196,197,198):
            draft_id=source_review_draft_ids[index]
            row=editor_rows[draft_id]
            attributes={field['external_id']:field for field in row['attributes']}
            for attribute_id in ('10096','4389'):
                field=attributes[attribute_id]
                assert field['source_managed'] is True
                assert isinstance(field['required'],bool) and field['required'] is False
                assert isinstance(field['dictionary_value_ids'],list)
                assert all(isinstance(value_id,str) for value_id in field['dictionary_value_ids'])
            color_values=SOURCE_REVIEW_CASES[index]['current']['10096']
            color_values=color_values if isinstance(color_values,list) else [color_values]
            assert attributes['10096']['dictionary_value_ids']==[
                source_value_id(index,'10096',value) for value in color_values
            ]
            country_value=SOURCE_REVIEW_CASES[index]['current']['4389']
            assert attributes['4389']['dictionary_value_ids']==[
                source_value_id(index,'4389',country_value)
            ]
            comparisons=row['source_comparisons']
            assert comparisons['10096']['status']=='confirmed'
            assert comparisons['4389']['status']==(
                'unknown' if index==196 else 'confirmed'
            )
        for index in (196,197,198):
            supplier_card(index).wait_for()
            supplier_card(index).get_by_role('button',name='Изменить',exact=True).click()

        bulk_action_posts=report['posts']
        page.get_by_role('button',name='Заполнить поле',exact=True).click()
        bulk_dialog=page.get_by_role('dialog')
        bulk_dialog.get_by_label('Поле',exact=True).select_option('attribute:10096')
        bulk_dialog.get_by_role('button',name='Выбрать из справочника',exact=True).click()
        bulk_dialog.get_by_role('searchbox',name='Поиск значения',exact=True).fill('Черный')
        bulk_option=bulk_dialog.locator('.orb-inline-picker .orb-picker-results button').filter(has_text='Черный').first
        bulk_option.wait_for()
        bulk_option.click()
        bulk_dialog.get_by_role('button',name='Посмотреть изменения',exact=True).click()
        assert 'Будет заполнено: 3. Пропущено: 0' in bulk_dialog.locator('[data-bulk-preview-summary]').inner_text()
        assert bulk_dialog.locator('.orb-review-list > li').count()==3
        assert report['posts']==bulk_action_posts
        bulk_dialog.get_by_role('button',name='Заполнить в форме',exact=True).click()
        assert page.locator('dialog[open]').count()==0
        assert report['posts']==bulk_action_posts
        bulk_action_post_delta=report['posts']-bulk_action_posts
        assert bulk_action_post_delta==0
        for index in (196,197,198):
            staged_color=supplier_card(index).locator('.ode-attribute[data-attribute="10096"] .ode-value-chip-label')
            assert staged_color.all_text_contents()==['Черный']

        clear_supplier_dictionary_values(196,'10096')
        choose_supplier_dictionary_value(196,'10096','Белый')
        choose_supplier_dictionary_value(196,'10096','Розовый')
        clear_supplier_dictionary_values(196,'4389')
        choose_supplier_dictionary_value(196,'4389','Россия')
        clear_supplier_dictionary_values(197,'10096')
        choose_supplier_dictionary_value(197,'10096','Черный')
        clear_supplier_dictionary_values(198,'10096')
        choose_supplier_dictionary_value(198,'10096','Белый')
        clear_supplier_dictionary_values(198,'4389')
        choose_supplier_dictionary_value(198,'4389','Беларусь')
        source_review_description=(
            'Локальное описание сохранено при исключении конфликтного цвета.'
        )
        supplier_card(198).get_by_label('Описание',exact=True).fill(source_review_description)

        source_before=snapshot_drafts()
        source_before_operations=None
        with app.app_context():
            source_before_operations=MarketplaceOperation.query.count()
        before_source_posts=report['posts']
        page.get_by_role('button',name='Проверить изменения',exact=True).click()
        review_dialog=page.get_by_role('dialog')
        review_dialog.locator('[data-supplier-source-review]').wait_for()
        assert review_dialog.locator('[data-supplier-source-row]').count()==5
        assert review_dialog.locator('[data-supplier-source-row][data-evidence-state="confirmed"]').count()==4
        assert review_dialog.locator('[data-supplier-source-row][data-evidence-state="unknown"]').count()==1
        assert review_dialog.locator('details.orb-source-ids[open]').count()==0
        page.screenshot(path=str(OUT/'bulk-source-review-desktop-opening.png'),animations='disabled')

        generic_review_rows=review_dialog.locator('ul.orb-review-list > li')
        for index in (196,197):
            title=editor_rows[source_review_draft_ids[index]]['title']
            assert generic_review_rows.filter(has_text=title).count()==0,(index,title)

        matched_color=supplier_review_row(196,'10096')
        assert matched_color.get_attribute('data-evidence-state')=='confirmed'
        assert matched_color.get_attribute('data-decision')=='none'
        assert matched_color.locator('[data-supplier-current]').inner_text().find('Синий, Зелёный')>=0
        assert matched_color.locator('[data-supplier-source]').inner_text().find('Розовый, Белый')>=0
        assert matched_color.locator('[data-supplier-proposed]').inner_text().find('Белый, Розовый')>=0
        assert matched_color.locator('[data-supplier-conflict-ack]').count()==0
        assert matched_color.locator('[data-supplier-source-exclude]').count()==0
        assert_review_dictionary_id_sets(196,'10096',{
            'Сейчас':['Синий','Зелёный'],
            'Сопоставлено поставщику':['Розовый','Белый'],
            'Предложено':['Белый','Розовый'],
        })

        unknown_country=supplier_review_row(196,'4389')
        assert unknown_country.get_attribute('data-evidence-state')=='unknown'
        assert 'Китай' in unknown_country.locator('[data-supplier-current]').inner_text()
        assert 'Нет подтверждённых исходных данных' in unknown_country.locator('[data-supplier-source]').inner_text()
        assert 'Россия' in unknown_country.locator('[data-supplier-proposed]').inner_text()
        assert unknown_country.locator('[data-supplier-conflict-ack]').count()==0
        assert unknown_country.locator('[data-supplier-source-exclude]').count()==0
        assert_review_dictionary_id_sets(196,'4389',{
            'Сейчас':['Китай'],
            'Предложено':['Россия'],
        })

        ack_color=supplier_review_row(197,'10096')
        assert ack_color.get_attribute('data-evidence-state')=='confirmed'
        assert ack_color.get_attribute('data-decision')=='unresolved'
        assert 'Синий' in ack_color.locator('[data-supplier-current]').inner_text()
        assert 'Розовый' in ack_color.locator('[data-supplier-source]').inner_text()
        assert 'Черный' in ack_color.locator('[data-supplier-proposed]').inner_text()
        assert ack_color.locator('[data-supplier-conflict-ack]').count()==1
        assert ack_color.locator('[data-supplier-source-exclude]').count()==1
        assert_review_dictionary_id_sets(197,'10096',{
            'Сейчас':['Синий'],
            'Сопоставлено поставщику':['Розовый'],
            'Предложено':['Черный'],
        })

        excluded_color=supplier_review_row(198,'10096')
        assert excluded_color.get_attribute('data-evidence-state')=='confirmed'
        assert excluded_color.get_attribute('data-decision')=='unresolved'
        assert 'Синий' in excluded_color.locator('[data-supplier-current]').inner_text()
        assert 'Зелёный' in excluded_color.locator('[data-supplier-source]').inner_text()
        assert 'Белый' in excluded_color.locator('[data-supplier-proposed]').inner_text()
        no_conflict_country=supplier_review_row(198,'4389')
        assert 'Россия' in no_conflict_country.locator('[data-supplier-current]').inner_text()
        assert 'Беларусь' in no_conflict_country.locator('[data-supplier-source]').inner_text()
        assert 'Беларусь' in no_conflict_country.locator('[data-supplier-proposed]').inner_text()
        assert no_conflict_country.locator('[data-supplier-conflict-ack]').count()==0
        assert no_conflict_country.locator('[data-supplier-source-exclude]').count()==0
        assert_review_dictionary_id_sets(198,'10096',{
            'Сейчас':['Синий'],
            'Сопоставлено поставщику':['Зелёный'],
            'Предложено':['Белый'],
        })
        assert_review_dictionary_id_sets(198,'4389',{
            'Сейчас':['Россия'],
            'Сопоставлено поставщику':['Беларусь'],
            'Предложено':['Беларусь'],
        })

        assert review_dialog.locator('[data-supplier-conflict-ack]').count()==2
        assert review_dialog.locator('[data-supplier-source-exclude]').count()==2
        source_save=review_dialog.locator('[data-supplier-source-save]')
        assert source_save.is_disabled()
        ack_color.locator('[data-supplier-conflict-ack]').check()
        excluded_color.locator('[data-supplier-source-exclude]').check()
        assert source_save.is_enabled()
        assert ack_color.get_attribute('data-decision')=='acknowledged'
        assert excluded_color.get_attribute('data-decision')=='excluded'
        assert 'Останется текущее значение' in excluded_color.inner_text()

        source_layouts=supplier_review_mobile_layout()
        page.screenshot(path=str(OUT/'bulk-source-review-desktop.png'),animations='disabled')
        source_save.click()
        page.locator('.orb-receipt[data-draft-selection-receipt]').wait_for()
        assert report['posts']==before_source_posts+1
        with app.app_context():
            source_after=snapshot_drafts()
            assert MarketplaceOperation.query.count()==source_before_operations
        assert set(source_before)==set(source_after)
        expected_source_changes=set(source_review_selection)
        changed_versions={
            draft_id for draft_id in source_before
            if source_after[draft_id]['version']!=source_before[draft_id]['version']
        }
        assert changed_versions==expected_source_changes,(changed_versions,expected_source_changes)
        for draft_id in source_before:
            before=source_before[draft_id];after=source_after[draft_id]
            if draft_id not in expected_source_changes:
                assert after==before,(draft_id,before,after)
                continue
            assert after['version']==before['version']+2,(draft_id,before['version'],after['version'])
            for key in ('product_type_id','complex_attributes_json','attribute_removals_json',
                        'media_json','dimensions_json','barcodes_json','commercial_json','offer_id'):
                assert after[key]==before[key],(draft_id,key,before[key],after[key])
            if draft_id==source_review_draft_ids[198]:
                before_content=json.loads(before['content_json'])
                expected_content={**before_content,'description':source_review_description}
                assert json.loads(after['content_json'])==expected_content
            else:
                assert after['content_json']==before['content_json'],(
                    draft_id,'content_json',before['content_json'],after['content_json'],
                )
            before_attrs=json.loads(before['attributes_json'])
            after_attrs=json.loads(after['attributes_json'])
            unrelated_before=[item for item in before_attrs if item.get('attribute_id') not in ('10096','4389')]
            unrelated_after=[item for item in after_attrs if item.get('attribute_id') not in ('10096','4389')]
            assert unrelated_after==unrelated_before,(draft_id,unrelated_before,unrelated_after)

        def saved_values(index,attribute_id):
            draft_id=source_review_draft_ids[index]
            attributes=json.loads(source_after[draft_id]['attributes_json'])
            item=next(row for row in attributes if row.get('attribute_id')==attribute_id and row.get('complex_id','0')=='0')
            return item['values']

        assert saved_values(196,'10096')==[
            {'value':'Белый','dictionary_value_id':source_value_id(196,'10096','Белый')},
            {'value':'Розовый','dictionary_value_id':source_value_id(196,'10096','Розовый')},
        ]
        assert saved_values(196,'4389')==[
            {'value':'Россия','dictionary_value_id':source_value_id(196,'4389','Россия')},
        ]
        assert saved_values(197,'10096')==[
            {'value':'Черный','dictionary_value_id':source_value_id(197,'10096','Черный')},
        ]
        assert saved_values(197,'4389')==[
            {'value':'Россия','dictionary_value_id':source_value_id(197,'4389','Россия')},
        ]
        assert saved_values(198,'10096')==[
            {'value':'Синий','dictionary_value_id':source_value_id(198,'10096','Синий')},
        ]
        assert saved_values(198,'4389')==[
            {'value':'Беларусь','dictionary_value_id':source_value_id(198,'4389','Беларусь')},
        ]
        assert json.loads(source_after[source_review_draft_ids[198]]['content_json'])['description']==source_review_description
        assert supplier_source_apply_checks==[{
            'selected_exact':True,
            'ack_exact':True,
            'matched_multi_value_exact_set':True,
            'matched_country_no_ack':True,
            'excluded_color_omitted':True,
            'unknown_evidence_unacknowledged':True,
            'exact_field_values':True,
        }]
        assert report['provider_attempts']==0 and report['provider_write_callbacks']==0
        assert report['posts']==before_source_posts+1
        assert not report['external'] and not report['unexpected_http']
        report['supplier_source']={
            'selected_rows':3,
            'distinct_categories':3,
            'bulk_color_normal_action_seen':True,
            'bulk_preview_items':3,
            'bulk_preview_skipped':0,
            'bulk_action_posts':bulk_action_post_delta,
            'comparisons':5,
            'confirmed_comparisons':4,
            'confirmed_conflicts':2,
            'missing':1,
            'ack_checked':1,
            'exclude_checked':1,
            'multi_value_exact_set':True,
            'different_category_or_field_unchanged':True,
            'local_versions_exact':True,
            'version_increment_per_selected_draft':2,
            'apply_requests':1,
            'provider_attempts':0,
            'provider_write_callbacks':0,
            'opening_screenshot':'bulk-source-review-desktop-opening.png',
            'id_details_collapsed_at_open':True,
            'layouts':source_layouts,
        }
        passed('supplier_source_comparison_exact_local_save')

        # Refresh only the two explicitly selected stale drafts from their
        # latest saved Seller Hub products. A selected fresh draft and a stale
        # draft that is visible but deselected remain byte-for-byte unchanged.
        source_refresh_list_url=(base+'/marketplaces/drafts?account_id='
                                 +str(fixture['account_id'])+'&per_page=100')
        page.goto(source_refresh_list_url)
        page.locator('#ozon-drafts-list:not([v-cloak])').wait_for()
        edit_mode=page.locator('[data-draft-selection-mode]')
        if edit_mode.get_attribute('aria-pressed')!='true':edit_mode.click()
        selection_summary=page.locator('[data-draft-selection-summary]')
        if 'выбрано 0' not in selection_summary.inner_text().lower():
            selection_summary.get_by_role('button',name='Снять выбор',exact=True).click()
        assert 'выбрано 0' in selection_summary.inner_text().lower()
        for draft_id in source_refresh_selection:
            control=page.locator('[data-draft-selection-check="'+str(draft_id)+'"]')
            control.wait_for()
            control.check()
        assert 'Для изменения выбрано 4' in page.locator('[data-draft-selection-summary]').inner_text()
        page.locator('[data-draft-selection-start]').click()
        page.wait_for_url('**/marketplaces/ozon/uploads/selection/repair?*')
        selection_query=parse_qs(urlsplit(page.url).query)
        assert selection_query.get('account_id')==[str(fixture['account_id'])]
        assert selection_query.get('draft_ids')==[','.join(str(i) for i in source_refresh_selection)]
        page.locator('[data-draft-selection-editor]').wait_for()
        source_refresh_bootstrap=json.loads(page.locator('#orb-bootstrap').text_content())
        source_refresh_rows={
            row['draft_id']:row
            for group in source_refresh_bootstrap['editor']['groups']
            for row in group['rows']
        }
        assert set(source_refresh_rows)==set(source_refresh_selection)
        assert page.locator('[data-draft-selection-editor] article.orb-row').count()==4
        for case_name in ('stale_name','stale_description','stale_unselected'):
            assert source_refresh_rows[source_refresh_draft_ids[case_name]]['source_refresh_available'] is True
        assert source_refresh_rows[source_refresh_draft_ids['fresh_selected']]['source_refresh_available'] is False
        unselected_stale=page.locator('[data-draft="'+str(source_refresh_draft_ids['stale_unselected'])+'"]')
        unselected_stale.locator('.orb-select input').uncheck()
        assert not unselected_stale.locator('.orb-select input').is_checked()
        for case_name in ('stale_name','stale_description','fresh_selected'):
            assert page.locator('[data-draft="'+str(source_refresh_draft_ids[case_name])+'"]').locator(
                '.orb-select input'
            ).is_checked()
        source_refresh_card=page.locator('[data-draft="'+str(source_refresh_draft_ids['stale_name'])+'"]')
        source_refresh_card.get_by_role('button',name='Изменить',exact=True).click()
        source_refresh_description=source_refresh_card.locator(
            'textarea[name="description-'+str(source_refresh_draft_ids['stale_name'])+'"]'
        )
        assert source_refresh_description.input_value()==fixture['source_refresh_cases']['stale_name']['manual_value']

        source_refresh_before=snapshot_source_refresh_drafts()
        source_products_before=snapshot_source_refresh_sources()
        with app.app_context():
            source_refresh_operations_before=MarketplaceOperation.query.count()
            for case_name in ('stale_name','stale_description','stale_unselected'):
                draft=db.session.get(MarketplaceProductDraft,source_refresh_draft_ids[case_name])
                codes={
                    error['code']
                    for error in MarketplaceDraftService._build_validation_result(draft)['errors']
                    if isinstance(error,dict) and isinstance(error.get('code'),str)
                }
                assert 'source_facts_stale' in codes,(case_name,codes)
                assert 'required_attribute_missing' in codes,(case_name,codes)

        refresh_button=page.locator('[data-source-facts-refresh]')
        assert refresh_button.count()==1
        assert refresh_button.inner_text().strip()=='Обновить из товаров (2)'
        refresh_posts_before=report['posts']
        refresh_url_before=page.url
        refresh_button.click()
        refresh_dialog=page.locator('[data-source-facts-refresh-dialog]')
        refresh_dialog.wait_for()
        assert '2' in page.locator('[data-source-facts-refresh-count]').inner_text()
        confirmation_rows=refresh_dialog.locator('ul.orb-review-list > li')
        assert confirmation_rows.count()==2
        confirmation_text='\n'.join(confirmation_rows.all_text_contents())
        assert fixture['source_refresh_cases']['stale_name']['current_title'] in confirmation_text
        assert fixture['source_refresh_cases']['stale_description']['current_title'] in confirmation_text
        assert fixture['source_refresh_cases']['fresh_selected']['title'] not in confirmation_text
        assert fixture['source_refresh_cases']['stale_unselected']['current_title'] not in confirmation_text
        source_refresh_layout_count_before=len(report['layouts'])
        layout('source-facts-refresh')
        source_refresh_layout_count=len(report['layouts'])-source_refresh_layout_count_before
        page.locator('[data-source-facts-refresh-confirm]').click()
        page.locator('[data-source-facts-refresh-result]').wait_for()
        page.wait_for_function(
            "document.querySelector('#ozon-bulk-repair section[aria-busy]')?.getAttribute('aria-busy')==='false'"
        )
        assert page.url==refresh_url_before
        assert report['posts']==refresh_posts_before+1
        assert page.locator('[data-source-facts-refresh-result]').count()==1
        assert source_refresh_description.is_visible()
        assert source_refresh_description.input_value()==fixture['source_refresh_cases']['stale_name']['manual_value']
        assert page.locator('[data-source-facts-refresh]').count()==0
        with app.app_context():
            source_refresh_after=snapshot_source_refresh_drafts()
            source_products_after=snapshot_source_refresh_sources()
            assert MarketplaceOperation.query.count()==source_refresh_operations_before
        assert source_products_after==source_products_before
        expected_refresh_changes=set(source_refresh_target_ids)
        refreshed_versions={
            draft_id for draft_id in source_refresh_before
            if source_refresh_after[draft_id]['version']!=source_refresh_before[draft_id]['version']
        }
        assert refreshed_versions==expected_refresh_changes,(refreshed_versions,expected_refresh_changes)
        for draft_id in source_refresh_before:
            before=source_refresh_before[draft_id]
            after=source_refresh_after[draft_id]
            if draft_id not in expected_refresh_changes:
                assert after==before,(draft_id,before,after)
                continue
            assert after['version']==before['version']+2,(draft_id,before['version'],after['version'])
            assert after['source_fact_hash']!=before['source_fact_hash']
            content=json.loads(after['content_json'])
            validation=json.loads(after['validation_result_json'])
            error_codes={
                error['code'] for error in validation.get('errors',[])
                if isinstance(error,dict) and isinstance(error.get('code'),str)
            }
            assert 'source_facts_stale' not in error_codes,(draft_id,error_codes)
            assert 'required_attribute_missing' in error_codes,(draft_id,error_codes)
            if draft_id==source_refresh_draft_ids['stale_name']:
                assert content['name']==fixture['source_refresh_cases']['stale_name']['current_title']
                assert content['description']==fixture['source_refresh_cases']['stale_name']['manual_value']
            elif draft_id==source_refresh_draft_ids['stale_description']:
                assert content['name']==fixture['source_refresh_cases']['stale_description']['manual_value']
                assert content['description']==fixture['source_refresh_cases']['stale_description']['current_description']
        with app.app_context():
            unselected_draft=db.session.get(
                MarketplaceProductDraft,source_refresh_draft_ids['stale_unselected'],
            )
            unselected_codes={
                error['code']
                for error in MarketplaceDraftService._build_validation_result(unselected_draft)['errors']
                if isinstance(error,dict) and isinstance(error.get('code'),str)
            }
        assert 'source_facts_stale' in unselected_codes
        assert 'required_attribute_missing' in unselected_codes
        assert source_refresh_apply_checks==[{
            'account_and_selection_exact':True,
            'stale_selected_targets_only_in_order':True,
            'fresh_selected_and_unselected_stale_excluded':True,
            'per_target_version_and_source_exact':True,
            'no_ordinary_edit_fields':True,
            'csrf_and_explicit_confirmation_present':True,
            'selection_scope_and_exact_receipt':True,
        }]
        assert report['provider_attempts']==0 and report['provider_write_callbacks']==0
        assert not report['external'] and not report['unexpected_http']
        report['source_refresh']={
            'exact_selection_rows':4,
            'selected_rows':3,
            'stale_selected_targets':2,
            'fresh_selected_unchanged':True,
            'stale_unselected_unchanged':True,
            'seller_authored_fields_preserved':2,
            'inherited_fields_rebased':2,
            'stale_error_removed_and_missing_requirements_retained':True,
            'source_rows_unchanged':True,
            'confirmation_layout_checks':source_refresh_layout_count,
            'posts':report['posts']-refresh_posts_before,
            'provider_attempts':0,
            'provider_write_callbacks':0,
        }
        passed('draft_selection_source_refresh_exact_local_rebase')
        with app.app_context():assert MarketplaceOperation.query.count()==0
        assert report['provider_attempts']==0 and not report['unexpected_http'] and not report['external']
        report['status']='passed';browser.close()
except Exception as error:
    report['status']='failed';report['error']=str(error)[:3000]
    raise
finally:
    (OUT/'bulk-browser.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    server.shutdown();worker.join(timeout=5);server.server_close();temporary.cleanup()
