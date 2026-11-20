"""Real HTTP/CSRF/Vue repair with a synthetic 200-row DB. No provider network."""
import base64
from contextlib import contextmanager
import hashlib
import json
import logging
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
from urllib.parse import urlsplit
from decimal import Decimal

import requests
from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server

OUT=Path('/artifacts');OUT.mkdir(exist_ok=True)
ASSETS=Path(__file__).with_name('assets')
report={'status':'running','checks':[],'layouts':[],'js_errors':[],'unexpected_http':[],
        'external':[],'provider_attempts':0,'posts':0,'rows':200}
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
from models import (db,BackgroundJob,MarketplaceAttributeDefinition,MarketplaceAttributeValue,
                    MarketplaceProductDraft,MarketplaceOperation)
from tests.ozon_release.bulk_repair_seed import bulk_seed
from tests.ozon_release.seed import USERNAME,PASSWORD
from services.ozon_bulk_upload import OzonBulkUploadService

app.config.update(TESTING=True,WTF_CSRF_ENABLED=True,SESSION_COOKIE_SECURE=False)
fixture=bulk_seed(app)
logging.getLogger('werkzeug').setLevel(logging.ERROR)
server=make_server('127.0.0.1',0,app,threaded=True);base='http://127.0.0.1:'+str(server.server_port)
worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
path='/marketplaces/ozon/uploads/'+fixture['repair_job']+'/repair'
tnved_job_uid='ozon-upload-'+'c'*32
tnved_path='/marketplaces/ozon/uploads/'+tnved_job_uid+'/repair'
manifest=json.loads((ASSETS/'manifest.json').read_text())
for item in manifest.values():assert hashlib.sha256((ASSETS/item['file']).read_bytes()).hexdigest()==item['sha256']
svg=b'<svg xmlns="http://www.w3.org/2000/svg" width="360" height="480"><rect width="360" height="480" fill="#eee9df"/><rect x="80" y="85" width="200" height="300" rx="12" fill="#678b9e"/><circle cx="180" cy="235" r="60" fill="#faf9f7"/></svg>'
fault={'mode':None}
page=None

def bridge(route):
    request=route.request;u=urlsplit(request.url)
    if u.hostname=='127.0.0.1' and u.port==server.server_port:
        if request.method not in ('GET','HEAD'):
            assert u.path in ('/login',path+'/apply',tnved_path+'/apply'),u.path
            if u.path in (path+'/apply',tnved_path+'/apply'):report['posts']+=1
        if u.path==path and request.headers.get('accept')=='application/json':
            if fault['mode']=='offline':route.abort();return
            if fault['mode']=='session':route.fulfill(status=401,json={'success':False,'error':'Expired'});return
            if fault['mode']=='foreign':
                result=route.fetch(max_redirects=0);body=result.json();body['editor']['account_id']=fixture['foreign_account_id']
                route.fulfill(status=200,json=body);return
        if u.path.startswith('/api/photos/imported-product/'):
            parts=u.path.split('/');assert int(parts[-2]) in fixture['repair_sources']
            route.fulfill(body=svg,content_type='image/svg+xml');return
        result=route.fetch(max_redirects=0)
        if result.status>=400:report['unexpected_http'].append({'path':u.path,'status':result.status})
        if u.path==path+'/apply' and fault['mode']=='lost':
            assert result.status==200;route.abort();return
        route.fulfill(response=result);return
    asset=manifest.get(request.url) or manifest.get(request.url.rstrip('/'))
    if asset:route.fulfill(body=(ASSETS/asset['file']).read_bytes(),content_type=asset['content_type']);return
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
        with app.app_context():assert MarketplaceOperation.query.count()==0
        assert report['provider_attempts']==0 and not report['unexpected_http'] and not report['external']
        report['status']='passed';browser.close()
except Exception as error:
    report['status']='failed';report['error']=str(error)[:3000]
    raise
finally:
    (OUT/'bulk-browser.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    server.shutdown();worker.join(timeout=5);server.server_close();temporary.cleanup()
