"""Offline Chromium journey for the shipped two-step Ozon upload review UI.

The app, login and CSRF are real. Review rows and queue acceptance are synthetic
so the browser can exercise 25 cross-page cards without a provider operation.
Backend scope and queue semantics are covered separately by route/service tests.
"""
import base64
import hashlib
import json
import logging
import os
from pathlib import Path
import socket
import tempfile
import threading
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit

import requests
from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server

OUT = Path(os.environ.get('OZON_BROWSER_ARTIFACTS', '/artifacts'))
OUT.mkdir(parents=True, exist_ok=True)
ASSETS = Path(__file__).with_name('assets')
manifest = json.loads((ASSETS/'manifest.json').read_text())
for asset in manifest.values():
    assert hashlib.sha256((ASSETS/asset['file']).read_bytes()).hexdigest() == asset['sha256']
report = {'status':'running', 'checks':[], 'layouts':[], 'js_errors':[],
          'unexpected_http':[], 'external':[], 'provider_attempts':0,
          'posts':{}, 'scope':'synthetic_upload_two_step_review'}
temporary = tempfile.TemporaryDirectory(prefix='ozon-upload-review-')
os.environ.update(
    DATABASE_URL='sqlite:///'+str(Path(temporary.name)/'app.sqlite'),
    SKIP_SCHEDULER='1', IMAGE_LAB_INLINE_WORKER='0',
    SECRET_KEY='synthetic-upload-review-ci',
    ENCRYPTION_KEY=base64.urlsafe_b64encode(b'0'*32).decode(),
    MARKETPLACE_OZON_ENABLED='1', MARKETPLACE_OZON_PUBLICATION_ENABLED='1',
    MARKETPLACE_OZON_COMMERCIAL_WRITES_ENABLED='0',
    MARKETPLACE_OZON_AUTO_PUBLISH_ENABLED='0',
    OZON_RATE_LIMIT_DIR=str(Path(temporary.name)/'limits'),
)

def no_network(*_args, **_kwargs):
    report['provider_attempts'] += 1
    raise AssertionError('External Python network forbidden')

requests.sessions.Session.request = no_network
socket.create_connection = no_network

from seller_platform import app
from models import db, MarketplaceOperation
from services.ozon_bulk_upload import OzonBulkUploadNotFound, OzonBulkUploadService
from services.ozon_upload_review import OzonUploadReviewService
from tests.ozon_release.seed import seed, USERNAME, PASSWORD

app.config.update(TESTING=True, WTF_CSRF_ENABLED=True, SESSION_COOKIE_SECURE=False)
fixture = seed(app)
account_id = fixture['account_id']
draft_ids = list(range(101, 126))
versions = {pk:3 for pk in draft_ids}
blocked = {105, 122}
categories = ['Посуда', 'Текстиль', 'Канцелярия', 'Аксессуары', 'Хранение', 'Освещение']
job_uid = 'ozon-upload-'+'a'*32
job = SimpleNamespace(job_uid=job_uid)
original_public_document = OzonBulkUploadService.public_document
original_find_by_request = OzonBulkUploadService.find_by_request_key
state = {'lost_post':False, 'drop_before_post':False, 'found':False,
         'accepts':0, 'post_bodies':[], 'review_keys':set()}

def document(*, seller_id, account_id: int, draft_ids: list[int], page=1,
             parent_prepare_job_uid=None):
    assert seller_id == fixture['seller_id'] and account_id == fixture['account_id']
    assert 1 <= len(draft_ids) <= 200 and len(set(draft_ids)) == len(draft_ids)
    assert all(pk in versions for pk in draft_ids)
    assert parent_prepare_job_uid is None
    pages = (len(draft_ids)+19)//20
    assert 1 <= page <= pages
    items = []
    for pk in draft_ids[(page-1)*20:page*20]:
        invalid = pk in blocked
        items.append({
            'draft_id':pk, 'version':versions[pk], 'imported_product_id':pk,
            'title':categories[(pk-101)%6]+' · проверочный товар '+str(pk),
            'primary_image':None, 'offer_id':'CI-REVIEW-'+str(pk),
            'action':'update' if pk%3 == 0 else 'create',
            'product_type_id':(pk-101)%6+1, 'type_name':categories[(pk-101)%6],
            'external_category_id':str(100+(pk-101)%6),
            'external_type_id':str(1000+(pk-101)%6),
            'selectable':not invalid,
            'errors':[{'code':'required_missing', 'message':'Нужно заполнить ТН ВЭД код'}] if invalid else [],
            'warnings':[{'code':'photo_count', 'message':'Проверьте набор фотографий'}] if pk == 101 else [],
            'checked_at':'2026-09-27T00:00:00', 'schema':{'hash':'synthetic-schema'},
            'active_operation_id':None,
            'commercial':{'price':str(1000+pk), 'old_price':str(1400+pk),
                          'vat':'0.22', 'currency_code':'RUB'},
            'dimensions':{'width':'200','height':'30','depth':'300',
                          'dimension_unit':'MILLIMETERS','weight':'250','weight_unit':'GRAMS'},
            'photos_count':2 if pk%2 else 1,
        })
    return {'account_id':account_id, 'account_label':'Ozon CI 0', 'draft_ids':draft_ids,
            'parent_prepare_job_uid':None, 'items':items, 'publication_enabled':True,
            'checked_at':'2026-09-27T00:00:00',
            'pagination':{'page':page,'per_page':20,'pages':pages,'total':len(draft_ids)}}

def accept(**kwargs):
    assert kwargs['seller_id'] == fixture['seller_id']
    assert kwargs['account_id'] == account_id
    assert kwargs['draft_ids'] == [101]
    assert kwargs['expected_versions'] == {'101':versions[101]}
    assert len(kwargs['request_key']) >= 24
    state['review_keys'].add(kwargs['request_key'])
    state['accepts'] += 1
    state['found'] = True
    return SimpleNamespace(job=job, replayed=False)

def find(**kwargs):
    assert kwargs['seller_id'] == fixture['seller_id'] and kwargs['account_id'] == account_id
    if kwargs['request_key'] not in state['review_keys']:
        return original_find_by_request(**kwargs)
    if not state['found']:
        raise OzonBulkUploadNotFound('Запуск не найден')
    return job

def public(_job, detail=False):
    assert detail
    if _job is not job:
        return original_public_document(_job, detail=detail)
    return {'job_uid':job_uid, 'mode':'reviewed_drafts', 'account_id':account_id}

logging.getLogger('werkzeug').setLevel(logging.ERROR)
server = make_server('127.0.0.1', 0, app, threaded=True)
worker = threading.Thread(target=server.serve_forever, daemon=True)
worker.start()
base = 'http://127.0.0.1:'+str(server.server_port)

def bridge(route):
    request = route.request
    url = urlsplit(request.url)
    if url.hostname != '127.0.0.1' or url.port != server.server_port:
        asset = manifest.get(request.url) or manifest.get(request.url.rstrip('/'))
        if asset:
            route.fulfill(body=(ASSETS/asset['file']).read_bytes(), content_type=asset['content_type']); return
        report['external'].append({'host':url.hostname, 'path':url.path})
        route.abort(); return
    if request.method not in ('GET','HEAD'):
        assert url.path in ('/login','/marketplaces/ozon/uploads/from-drafts',
                            '/marketplaces/ozon/uploads/'), url.path
        if url.path != '/login':
            report['posts'][url.path] = report['posts'].get(url.path,0)+1
            state['post_bodies'].append(json.loads(request.post_data))
            if state['drop_before_post']:
                state['drop_before_post'] = False
                route.abort(); return
    if request.method == 'GET' and url.path == '/marketplaces/ozon/uploads/'+job_uid:
        route.fulfill(status=200, content_type='text/html',
                      body='<html><body><h1>Синтетический принятый запуск</h1></body></html>')
        return
    response = route.fetch(max_redirects=0)
    if state['lost_post'] and request.method == 'POST' and url.path.endswith('/from-drafts'):
        state['lost_post'] = False
        assert response.status == 202, (response.status, response.body()[:300])
        route.abort(); return
    route.fulfill(response=response)

def passed(name):
    assert not report['js_errors'], report['js_errors']
    report['checks'].append(name)
    print(json.dumps({'upload_review_check':name}), flush=True)

def layout(page, name):
    page.bring_to_front()
    for theme in ('light','dark'):
        for width in (320,390,768,1440):
            page.set_viewport_size({'width':width,'height':980})
            page.evaluate('(value)=>document.documentElement.dataset.theme=value', theme)
            page.wait_for_function('document.fonts.status === "loaded"')
            page.evaluate('''() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))''')
            assert page.locator('#ozon-upload-review:not([v-cloak])').count() == 1
            page.wait_for_function('document.documentElement.scrollWidth <= innerWidth + 1', timeout=3000)
            if page.evaluate('document.documentElement.scrollWidth > innerWidth + 1'):
                page.screenshot(path=str(OUT/f'upload-review-overflow-{name}-{theme}-{width}.png'))
                offenders = page.evaluate('''() => Array.from(document.querySelectorAll('*'))
                    .filter(node => node.getBoundingClientRect().right > innerWidth + 1)
                    .slice(0,12).map(node => ({tag:node.tagName,
                        className:String(node.className).slice(0,100),
                        right:Math.round(node.getBoundingClientRect().right),
                        scroll:node.scrollWidth,client:node.clientWidth}))''')
                metrics = page.evaluate('''() => ({inner:innerWidth,html:document.documentElement.scrollWidth,
                    body:document.body.scrollWidth, wide:Array.from(document.querySelectorAll('*'))
                    .filter(node=>node.scrollWidth>node.clientWidth+1).slice(0,15)
                    .map(node=>({tag:node.tagName,className:String(node.className).slice(0,90),
                        scroll:node.scrollWidth,client:node.clientWidth,left:Math.round(node.getBoundingClientRect().left)}))})''')
                raise AssertionError((name,theme,width,'page overflow',offenders,metrics))
            dialog = page.locator('dialog[open]')
            if dialog.count():
                assert dialog.evaluate('(node)=>node.scrollWidth <= node.clientWidth+1'), (name,theme,width,'dialog overflow')
            page.screenshot(path=str(OUT/f'upload-review-{name}-{theme}-{width}.png'), animations='disabled')
            report['layouts'].append({'name':name,'theme':theme,'width':width})
    page.set_viewport_size({'width':1440,'height':980})
    page.evaluate('document.documentElement.dataset.theme="light"')

def beta_layout(page, name):
    for theme in ('light','dark'):
        for width in (320,390,768,1440):
            page.set_viewport_size({'width':width,'height':980})
            page.evaluate('(value)=>document.documentElement.dataset.theme=value', theme)
            page.wait_for_function('document.documentElement.scrollWidth <= innerWidth + 1', timeout=3000)
            assert page.get_by_role('dialog').evaluate('(node)=>node.scrollWidth <= node.clientWidth+1')
            page.screenshot(path=str(OUT/f'upload-source-{name}-{theme}-{width}.png'), animations='disabled')
            report['layouts'].append({'name':name,'theme':theme,'width':width})
    page.set_viewport_size({'width':1440,'height':980})
    page.evaluate('document.documentElement.dataset.theme="light"')

try:
    with patch.object(OzonUploadReviewService, 'document', side_effect=document), \
         patch.object(OzonBulkUploadService, 'accept_reviewed_publish', side_effect=accept), \
         patch.object(OzonBulkUploadService, 'find_by_request_key', side_effect=find), \
         patch.object(OzonBulkUploadService, 'public_document', side_effect=public):
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=os.environ.get('OZON_BROWSER_CHROMIUM','/usr/bin/chromium'),
                headless=True, args=['--no-sandbox','--disable-dev-shm-usage'])
            context = browser.new_context(viewport={'width':1440,'height':980}, service_workers='block')
            context.route('**/*', bridge)
            page = context.new_page(); page.set_default_timeout(20000)
            page.on('pageerror', lambda error:report['js_errors'].append(str(error)))
            page.on('response', lambda response:report['unexpected_http'].append({
                'status':response.status,'path':urlsplit(response.url).path}) if response.status >= 500 else None)
            deep = ('/marketplaces/ozon/uploads/review?account_id='+str(account_id)+
                    '&draft_ids='+','.join(map(str,draft_ids)))
            page.goto(base+'/login')
            page.locator('[name=username]').fill(USERNAME)
            page.locator('[name=password]').fill(PASSWORD)
            page.get_by_role('button',name='Войти',exact=True).click()
            page.goto(base+deep)
            page.locator('#ozon-upload-review:not([v-cloak])').wait_for()
            assert page.locator('.our-card').count() == 20
            assert 'ТН ВЭД код' in page.locator('.our-card--blocked').first.inner_text()
            assert page.locator('#our-select-105').is_disabled()
            assert 'Цена продавца' in page.locator('.our-facts').first.inner_text()
            layout(page,'first-page')
            passed('fresh_first_page_shows_real_facts_and_blocks_partial_rows')

            nojs = browser.new_context(viewport={'width':390,'height':820},
                                       java_script_enabled=False, storage_state=context.storage_state())
            nojs.route('**/*', bridge)
            plain = nojs.new_page(); plain.goto(base+deep)
            classic = plain.locator('.our-noscript form')
            assert classic.is_visible()
            assert classic.locator('[name=expected_versions]').input_value()
            assert classic.locator('[name=request_key]').input_value()
            assert classic.locator('[name=draft_ids]').count() == 19
            assert classic.locator('[name=confirm_write]').get_attribute('required') is not None
            plain.screenshot(path=str(OUT/'upload-review-classic-light-390.png'))
            nojs.close()
            passed('classic_page_requires_selected_ids_versions_and_explicit_confirmation')

            page.locator('#our-select-101').check()
            page.get_by_role('link',name='Следующие 20 →').click()
            page.locator('#ozon-upload-review:not([v-cloak])').wait_for()
            assert page.locator('.our-card').count() == 5
            assert page.get_by_text('К отправке выбрано 1').count() == 1
            page.locator('#our-select-121').check()
            page.get_by_role('link',name='← Предыдущие 20').click()
            page.locator('#ozon-upload-review:not([v-cloak])').wait_for()
            assert page.locator('#our-select-101').is_checked()
            assert page.get_by_text('К отправке выбрано 2').count() == 1
            layout(page,'cross-page')
            passed('selection_persists_across_pages_without_selecting_unseen_rows')

            versions[121] += 1
            page.get_by_role('button',name='Проверить выбранные').click()
            page.get_by_role('alert').filter(has_text='Часть карточек изменилась').wait_for()
            assert report['posts'] == {}
            assert page.get_by_text('К отправке выбрано 1').count() == 1
            versions[121] -= 1
            layout(page,'stale-selection')
            passed('stale_cross_page_version_requires_new_explicit_selection')

            page.get_by_role('button',name='Проверить выбранные').click()
            dialog = page.get_by_role('dialog')
            dialog.wait_for()
            assert dialog.get_by_text('Цена продавца:',exact=False).count() == 1
            assert dialog.get_by_text('200 × 30 × 300 мм',exact=False).count() == 1
            assert dialog.get_by_role('checkbox').evaluate('(node)=>document.activeElement===node')
            layout(page,'confirmation')
            page.keyboard.press('Escape')
            assert dialog.count() == 0
            assert page.get_by_role('button',name='Проверить выбранные').evaluate('(node)=>document.activeElement===node')
            passed('dialog_keyboard_escape_restores_focus_without_post')

            page.get_by_role('button',name='Проверить выбранные').click()
            dialog = page.get_by_role('dialog'); dialog.get_by_role('checkbox').check()
            state['lost_post'] = True
            dialog.get_by_role('button',name='Отправить проверенные карточки').click()
            page.get_by_text('Результат отправки пока неизвестен.').wait_for()
            assert report['posts'].get('/marketplaces/ozon/uploads/from-drafts') == 1
            assert state['accepts'] == 1
            layout(page,'unknown-result')
            page.reload()
            page.locator('#ozon-upload-review:not([v-cloak])').wait_for()
            assert page.get_by_text('Результат отправки пока неизвестен.').count() == 1
            assert page.get_by_role('button',name='Проверить выбранные').is_disabled()
            page.get_by_role('button',name='Проверить результат').click()
            page.wait_for_url('**/'+job_uid)
            assert report['posts'].get('/marketplaces/ozon/uploads/from-drafts') == 1
            passed('lost_post_survives_reload_and_readback_never_duplicates_publication')

            state['found'] = False
            page.goto(base+deep)
            page.locator('#ozon-upload-review:not([v-cloak])').wait_for()
            page.get_by_role('button',name='Проверить выбранные').click()
            dialog = page.get_by_role('dialog'); dialog.get_by_role('checkbox').check()
            state['drop_before_post'] = True
            dialog.get_by_role('button',name='Отправить проверенные карточки').click()
            page.get_by_text('Результат отправки пока неизвестен.').wait_for()
            assert state['accepts'] == 1 and report['posts']['/marketplaces/ozon/uploads/from-drafts'] == 2
            page.get_by_role('button',name='Проверить результат').click()
            page.get_by_role('button',name='Повторить тот же запрос').wait_for()
            assert state['accepts'] == 1
            page.get_by_role('button',name='Повторить тот же запрос').click()
            page.wait_for_url('**/'+job_uid)
            assert state['post_bodies'][-1] == state['post_bodies'][-2]
            assert state['accepts'] == 2 and report['posts']['/marketplaces/ozon/uploads/from-drafts'] == 3
            passed('unreached_post_404_then_explicit_same_key_exact_payload_retry')

            page.goto(base+'/my-products/beta')
            page.locator('#my-products-app:not([v-cloak])').wait_for()
            source_row = page.locator('.mp-row').filter(has_text='Тестовый товар для подготовки').first
            source_row.get_by_role('checkbox').check()
            page.get_by_role('button',name='Что сделать').click()
            page.get_by_role('menuitem',name='Подготовить для Ozon',exact=False).click()
            source_dialog = page.get_by_role('dialog')
            source_dialog.wait_for()
            assert source_dialog.get_by_text('Ничего не отправится в Ozon.',exact=False).count() == 1
            assert source_dialog.get_by_role('combobox',name='Магазин Ozon').input_value() == str(account_id)
            beta_layout(page,'prepare-confirmation')
            source_dialog.get_by_role('checkbox').check()
            state['drop_before_post'] = True
            source_dialog.get_by_role('button',name='Подготовить черновики').click()
            page.get_by_text('Результат локальной подготовки пока неизвестен.').wait_for()
            page.get_by_role('button',name='Проверить результат').click()
            page.get_by_role('button',name='Повторить тот же запрос').wait_for()
            page.get_by_role('button',name='Повторить тот же запрос').click()
            page.wait_for_url('**/marketplaces/ozon/uploads/ozon-upload-*')
            assert job_uid not in page.url
            assert report['posts'].get('/marketplaces/ozon/uploads/') == 2
            assert state['post_bodies'][-1] == state['post_bodies'][-2]
            with app.app_context():
                assert MarketplaceOperation.query.count() == 0
            passed('beta_local_prepare_404_manual_same_key_recovery_without_provider_write')

            assert not report['unexpected_http'], report['unexpected_http']
            assert not report['external'], report['external']
            assert report['provider_attempts'] == 0
            browser.close()
    report['status'] = 'passed'
finally:
    server.shutdown(); worker.join(timeout=5); temporary.cleanup()
    (OUT/'upload-two-step-browser.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
