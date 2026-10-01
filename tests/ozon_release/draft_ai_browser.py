"""Offline browser contract for optional seller AI suggestions.

Real Flask session/routes/CSRF and shipped Vue mount; synthetic service documents
and a blocked provider boundary. No model call or Ozon operation is performed.
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
report = {'status':'running','checks':[],'layouts':[],'js_errors':[],
          'unexpected_http':[],'external':[],'provider_attempts':0,'posts':{},
          'scope':'synthetic_seller_ai_review'}
temporary = tempfile.TemporaryDirectory(prefix='ozon-ai-browser-')
os.environ.update(DATABASE_URL='sqlite:///'+str(Path(temporary.name)/'app.sqlite'),
    SKIP_SCHEDULER='1', IMAGE_LAB_INLINE_WORKER='0',
    SECRET_KEY='synthetic-ai-review-ci',
    ENCRYPTION_KEY=base64.urlsafe_b64encode(b'0'*32).decode(),
    MARKETPLACE_OZON_ENABLED='1', MARKETPLACE_OZON_PUBLICATION_ENABLED='1',
    MARKETPLACE_OZON_AUTO_PUBLISH_ENABLED='0',
    OZON_RATE_LIMIT_DIR=str(Path(temporary.name)/'limits'))

def no_network(*_args, **_kwargs):
    report['provider_attempts'] += 1
    raise AssertionError('External Python network forbidden')

requests.sessions.Session.request = no_network
socket.create_connection = no_network

from seller_platform import app
from models import (db, MarketplaceProductDraft, MarketplaceOperation,
                    MarketplaceAttributeDefinition)
from services.marketplace_drafts import MarketplaceDraftService
from services.ozon_draft_ai_completion import OzonDraftAICompletionService as Service
from services.ozon_draft_ai_completion import DraftAIError
from tests.ozon_release.seed import seed, USERNAME, PASSWORD

app.config.update(TESTING=True, WTF_CSRF_ENABLED=True, SESSION_COOKIE_SECURE=False)
fixture = seed(app)
account_id, draft_id = fixture['account_id'], fixture['draft_id']
with app.app_context():
    draft = db.session.get(MarketplaceProductDraft,draft_id)
    version = draft.version
    # Make the forbidden-brand issue a live source fact, not just an old
    # validation snapshot. The editor now renders current validation errors.
    source = draft.imported_product
    original = MarketplaceDraftService._stored_json(source.original_data, dict)
    original['brand'] = 'SVAKOM'
    source.brand = 'SVAKOM'
    source.original_data = MarketplaceDraftService._canonical_json(original, dict)
    schema = [
        ('909001', 'Цвет', False),
        ('909002', 'Фактура', False),
        ('909003', 'Размеры', False),
        ('22232', 'ТН ВЭД', True),
        ('23536', 'Маркировка', True),
    ]
    db.session.add_all([
        MarketplaceAttributeDefinition(
            marketplace_id=draft.marketplace_id,
            product_type_id=draft.product_type_id,
            external_attribute_id=attribute_id,
            name=name,
            data_type='String',
            is_required=required,
            max_value_count=1,
            attribute_complex_id=None,
            is_available=True,
            is_enabled=True,
        )
        for attribute_id, name, required in schema
    ])
    draft.attributes_json = json.dumps([{
        'attribute_id':'909003','complex_id':'0',
        'values':[{'value':'S'},{'value':'M'},{'value':'L'}],
    }])
    draft.validation_status = 'invalid'
    draft.validation_result_json = json.dumps({
        'publishable':False,
        'errors':[
            {'code':'ozon_brand_forbidden','field':'brand',
             'message':'Бренд запрещён для этой категории.'},
            {'code':'attribute_max_value_count','field':'attributes[0].values',
             'attribute_id':'909003','attribute_name':'Размеры',
             'actual_count':3,'max_value_count':1,
             'message':'Для характеристики «Размеры» допустимо одно значение.'},
            {'code':'required_attribute_missing','field':'attributes.22232',
             'attribute_id':'22232','message':'Требуется подтверждённый код ТН ВЭД.'},
            {'code':'required_attribute_missing','field':'attributes.23536',
             'attribute_id':'23536','message':'Нужно проверить признак маркировки.'},
        ],
        'warnings':[],
    })
    source_facts, source_provenance, source_fact_hash = MarketplaceDraftService._fact_snapshot(source)
    draft.source_facts_json = MarketplaceDraftService._canonical_json(source_facts, dict)
    draft.provenance_json = MarketplaceDraftService._canonical_json(source_provenance, dict)
    draft.source_fact_hash = source_fact_hash
    db.session.commit()
    version = draft.version
run_uid = 'ozon-ai-'+'a'*32
run = SimpleNamespace(job=SimpleNamespace(job_uid=run_uid))
state = {'accepted':False,'accepts':0,'keys':[], 'review_effects':0,'review_keys':{},
         'suggestions':{11:'proposed',12:'proposed'},'drop_next':False,'lose_next':False,
         'generation_bodies':[]}

def run_document(_run):
    assert _run is run
    return {'job_uid':run_uid,'account_id':account_id,'mode':'draft_suggestions',
        'status':'completed','model':'deepseek-flash','total':1,'physical_calls':0,
        'max_calls':1,'items':[{'id':21,'draft_id':draft_id,'expected_version':version,
            'title':'Тестовый товар для подготовки','status':'proposed','code':None,
            'next_due_at':None}], 'summary':{},'created_at':'2026-09-27T00:00:00Z'}

def accept(**kwargs):
    assert kwargs['seller_id'] == fixture['seller_id']
    assert kwargs['account_id'] == account_id and kwargs['draft_ids'] == [draft_id]
    assert kwargs['expected_versions'] == {str(draft_id):version}
    state['keys'].append(kwargs['request_key'])
    if not state['accepted']:
        state['accepts'] += 1
    state['accepted'] = True
    return run, state['accepts'] > 1

def by_request(**kwargs):
    assert kwargs['seller_id'] == fixture['seller_id'] and kwargs['account_id'] == account_id
    if not state['accepted'] or kwargs['request_key'] not in state['keys']:
        raise DraftAIError('ai_run_not_found','Запуск не найден.',404)
    return run

def get_run(**kwargs):
    assert kwargs['seller_id'] == fixture['seller_id'] and kwargs['job_uid'] == run_uid
    return run

def suggestions(**kwargs):
    assert kwargs['seller_id'] == fixture['seller_id'] and kwargs['draft_id'] == draft_id
    assert kwargs['item_id'] in (None,21)
    rows = []
    for id_, label in ((11,'Красный'),(12,'Гладкая')):
        rows.append({'id':id_,'attribute_id':'909001' if id_ == 11 else '909002',
            'complex_id':'0','group_ordinal':0,'values':[{'value':label}],
            'evidence':[{'path':'/description','quote':'Цвет — Красный. Фактура — Гладкая.'}],
            'provenance_code':'literal_source','status':state['suggestions'][id_],
            'name':'Цвет' if id_ == 11 else 'Фактура',
            'label':label,'applicable':state['suggestions'][id_] == 'proposed'})
    return {'draft_id':draft_id,'account_id':account_id,'version':version,
        'item':{'id':21,'status':'proposed','code':None,'expected_version':version,'run_uid':run_uid},
        'suggestions':rows,'review_token':'synthetic-review-token','code':None}

def review(**kwargs):
    assert kwargs['seller_id'] == fixture['seller_id'] and kwargs['draft_id'] == draft_id
    assert kwargs['expected_version'] == version and kwargs['review_token'] == 'synthetic-review-token'
    ids = kwargs['suggestion_ids']
    assert ids and set(ids) <= {11,12}
    old = state['review_keys'].get(kwargs['request_key'])
    if old:
        assert old['suggestion_ids'] == ids and old['action'] == kwargs['action']
        return {**old,'replayed':True}
    state['review_effects'] += 1
    for id_ in ids:
        state['suggestions'][id_] = 'accepted' if kwargs['action'] == 'apply' else 'rejected'
    result = {'review_id':state['review_effects'],'draft_id':draft_id,
        'action':kwargs['action'],'version_before':version,
        'version_after':version if kwargs['action'] == 'apply' else None,
        'suggestion_ids':ids,'replayed':False,'requires_validation':kwargs['action']=='apply'}
    state['review_keys'][kwargs['request_key']] = result
    return result

logging.getLogger('werkzeug').setLevel(logging.ERROR)
server = make_server('127.0.0.1',0,app,threaded=True)
threading.Thread(target=server.serve_forever,daemon=True).start()
base = 'http://127.0.0.1:'+str(server.server_port)

def bridge(route):
    request = route.request
    url = urlsplit(request.url)
    if url.hostname != '127.0.0.1' or url.port != server.server_port:
        if url.hostname == 'ozon-fixture.test' and url.path == '/product.svg' and request.method == 'GET':
            route.fulfill(status=200, content_type='image/svg+xml',body='''<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64"><rect width="64" height="64" fill="#b86b51"/></svg>'''); return
        asset = manifest.get(request.url) or manifest.get(request.url.rstrip('/'))
        if asset:
            route.fulfill(body=(ASSETS/asset['file']).read_bytes(),content_type=asset['content_type']); return
        report['external'].append({'host':url.hostname,'path':url.path}); route.abort(); return
    if request.method not in ('GET','HEAD'):
        allowed = ('/login','/marketplaces/api/drafts/ai-completions',
            '/marketplaces/api/drafts/'+str(draft_id)+'/ai-suggestions/apply',
            '/marketplaces/api/drafts/'+str(draft_id)+'/ai-suggestions/reject')
        assert url.path in allowed, (request.method,url.path)
        if url.path != '/login':
            report['posts'][url.path] = report['posts'].get(url.path,0)+1
            if url.path == '/marketplaces/api/drafts/ai-completions':
                state['generation_bodies'].append(json.loads(request.post_data))
            if state['drop_next']:
                state['drop_next'] = False; route.abort(); return
    response = route.fetch(max_redirects=0)
    if request.method == 'POST' and state['lose_next']:
        state['lose_next'] = False
        assert response.status in (200,202), (response.status,response.body()[:300])
        route.abort(); return
    route.fulfill(response=response)

def passed(name):
    assert not report['js_errors'],report['js_errors']
    report['checks'].append(name)
    print(json.dumps({'ai_browser_check':name}),flush=True)

def editor_document(page):
    config = page.locator('#ode-bootstrap').evaluate('node => JSON.parse(node.textContent)')
    response = page.evaluate('''async (url) => {
        const response = await fetch(url, {credentials:'same-origin', headers:{Accept:'application/json'}});
        let document = null;
        try { document = await response.json(); } catch (_) {}
        return {status:response.status, document};
    }''', config['urls']['editor'])
    assert response['status'] == 200 and isinstance(response.get('document'), dict), {
        'status': response['status'],
        'code': (response.get('document') or {}).get('code'),
    }
    document = response['document']
    assert document.get('draft', {}).get('id') == config.get('draftId')
    return document

def layout(page,name):
    page.bring_to_front()
    for theme in ('light','dark'):
        for width in (320,390,768,1440):
            page.set_viewport_size({'width':width,'height':980})
            page.evaluate('(value)=>document.documentElement.dataset.theme=value',theme)
            page.wait_for_function('document.fonts.status === "loaded"')
            page.evaluate('''() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))''')
            page.wait_for_function('document.documentElement.scrollWidth <= innerWidth + 1',timeout=3000)
            if not page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1'):
                page.screenshot(path=str(OUT/f'ai-overflow-{name}-{theme}-{width}.png'))
                offenders = page.evaluate('''() => ({viewport:innerWidth,html:document.documentElement.scrollWidth,
                    body:document.body.scrollWidth,nodes:Array.from(document.querySelectorAll('*')).filter(
                    node => node.getBoundingClientRect().right > innerWidth + 1 ||
                    node.getBoundingClientRect().left < -1 || node.scrollWidth > node.clientWidth + 1).slice(0,20).map(
                    node => ({tag:node.tagName,className:String(node.className).slice(0,90),
                    left:Math.round(node.getBoundingClientRect().left),right:Math.round(node.getBoundingClientRect().right),
                    width:node.scrollWidth,client:node.clientWidth}))})''')
                raise AssertionError((name,theme,width,'overflow',offenders))
            for dialog in page.locator('dialog[open]').all():
                assert dialog.evaluate('(node)=>node.scrollWidth <= node.clientWidth+1'),(name,theme,width)
            page.screenshot(path=str(OUT/f'ai-{name}-{theme}-{width}.png'),animations='disabled')
            report['layouts'].append({'name':name,'theme':theme,'width':width})
    page.set_viewport_size({'width':1440,'height':980})
    page.evaluate('document.documentElement.dataset.theme="light"')

try:
    with patch.object(Service,'accept',side_effect=accept), \
         patch.object(Service,'find_by_request',side_effect=by_request), \
         patch.object(Service,'get_run',side_effect=get_run), \
         patch.object(Service,'document',side_effect=run_document), \
         patch.object(Service,'suggestions_document',side_effect=suggestions), \
         patch.object(Service,'review',side_effect=review):
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=os.environ.get('OZON_BROWSER_CHROMIUM','/usr/bin/chromium'),
                headless=True,args=['--no-sandbox','--disable-dev-shm-usage'])
            context = browser.new_context(viewport={'width':1440,'height':980},service_workers='block')
            context.route('**/*',bridge)
            page = context.new_page(); page.set_default_timeout(20000)
            page.on('pageerror',lambda error:report['js_errors'].append(str(error)))
            page.on('response',lambda response:report['unexpected_http'].append({
                'status':response.status,'path':urlsplit(response.url).path}) if response.status >= 500 else None)
            page.goto(base+'/login')
            page.locator('[name=username]').fill(USERNAME)
            page.locator('[name=password]').fill(PASSWORD)
            page.get_by_role('button',name='Войти',exact=True).click()
            page.goto(base+'/marketplaces/drafts/?account_id='+str(account_id))
            page.locator('#ozon-drafts-list:not([v-cloak])').wait_for()
            page.get_by_role('button',name='Для AI-дополнения').click()
            assert page.get_by_role('checkbox',name='Выбрать для AI').count() >= 1
            page.get_by_role('checkbox',name='Выбрать для AI').first.check()
            assert page.get_by_text('Для AI выбрано 1').is_visible()
            page.get_by_role('button',name='Для отправки').click()
            assert page.get_by_role('checkbox',name='Выбрать для AI').count() == 0
            page.get_by_role('button',name='Для AI-дополнения').click()
            assert page.get_by_text('Для AI выбрано 1').is_visible()
            layout(page,'selection-mode')
            passed('one_visible_checkbox_mode_preserves_independent_ai_selection')

            page.get_by_role('button',name='Предложить характеристики').click()
            page.get_by_role('dialog').get_by_role('checkbox').check()
            state['lose_next'] = True
            page.get_by_role('dialog').get_by_role('button',name='Запустить поиск предложений').click()
            page.get_by_text('Результат AI-запуска пока неизвестен.').wait_for()
            assert state['accepts'] == 1
            page.get_by_role('button',name='Проверить результат').click()
            page.wait_for_url(base+'/marketplaces/drafts/ai-completions/'+run_uid)
            assert page.locator('#ozon-ai-run:not([v-cloak])').count() == 1
            assert state['accepts'] == 1
            layout(page,'run-progress')
            passed('lost_accepted_generate_reads_same_key_without_second_post')

            page.get_by_role('link',name='Просмотреть предложения').click()
            page.wait_for_url('**/marketplaces/drafts/'+str(draft_id)+'?ai_item_id=21')
            page.locator('#ozon-draft-editor:not([v-cloak])').wait_for()
            page.locator('.ode-ai-item').first.wait_for()
            assert page.locator('.ode-ai-item').count() == 2
            page.locator('.ode-ai-item details').first.locator('summary').click()
            assert page.locator('.ode-ai-item q').first.is_visible()
            assert page.locator('.ode-ai-item q').first.inner_text() == 'Цвет — Красный. Фактура — Гладкая.'

            scope = page.locator('.ode-ai-scope')
            scope.get_by_role('heading',name='Может предложить').wait_for()
            assert 'пустых простых характеристик' in scope.inner_text()
            assert 'Этот помощник черновика не предлагает название и не меняет бренд' in scope.inner_text()
            assert 'ТН ВЭД и маркировка требуют подтверждённых данных' in scope.inner_text()
            assert 'не гарантируют устранение ошибок проверки' in page.locator('.ode-ai-scope-foot').inner_text()
            assert page.locator('.ode-ai-preflight-note').get_attribute('aria-live') == 'polite'
            assert page.locator('.ode-issues').get_attribute('aria-live') == 'polite'
            before_ai = editor_document(page)
            current_validation = before_ai.get('current_validation') or {}
            current_errors = current_validation.get('errors')
            historical_errors = (before_ai.get('draft', {}).get('validation') or {}).get('errors')
            assert current_validation.get('publishable') is False
            assert isinstance(current_errors, list) and current_errors
            assert isinstance(historical_errors, list) and len(historical_errors) == 4
            known_current = {
                (item.get('code'), item.get('field'))
                for item in current_errors if isinstance(item, dict)
            }
            known_historical = {
                (item.get('code'), item.get('field'))
                for item in historical_errors if isinstance(item, dict)
            }
            expected_manual_states = {
                ('ozon_brand_forbidden', 'brand'),
                ('attribute_max_value_count', 'attributes[0].values'),
                ('required_attribute_missing', 'attributes.22232'),
                ('required_attribute_missing', 'attributes.23536'),
            }
            assert expected_manual_states <= known_current, {
                'missing_current_states': sorted(expected_manual_states - known_current),
                'current_codes_fields': sorted(known_current),
            }
            assert expected_manual_states == known_historical, {
                'historical_codes_fields': sorted(known_historical),
            }
            max_values_error = next(
                item for item in current_errors
                if item.get('code') == 'attribute_max_value_count'
                and item.get('field') == 'attributes[0].values'
            )
            assert max_values_error.get('attribute_id') == '909003'
            assert page.locator('.ode-issues li').count() == len(current_errors), {
                'rendered_current_errors': page.locator('.ode-issues li').count(),
                'current_validation_errors': len(current_errors),
            }
            assert page.locator('.ode-savebar button.sh-btn--primary').is_disabled()
            current_issue_text = page.locator('.ode-issues').inner_text()
            assert 'Удалите лишние значения вручную' in current_issue_text
            assert 'не обходит запрет' in current_issue_text
            assert 'уточните у администратора соответствия' in current_issue_text
            assert f"внутреннюю карточку № {fixture['source_id']}" in current_issue_text
            report['preflight_states'] = {
                'current_publishable': current_validation.get('publishable'),
                'current_errors': [
                    {'code': item.get('code'), 'field': item.get('field')}
                    for item in current_errors if isinstance(item, dict)
                ],
                'historical_errors': [
                    {'code': item.get('code'), 'field': item.get('field')}
                    for item in historical_errors if isinstance(item, dict)
                ],
                'send_enabled': page.locator('.ode-savebar button.sh-btn--primary').is_enabled(),
            }
            source_link = page.get_by_role('link',name='К внутренним товарам')
            assert source_link.get_attribute('href') == '/my-products?account_id='+str(account_id)
            assert 'search=' not in source_link.get_attribute('href')

            page.get_by_role('link',name='Посмотреть проверку').click()
            assert page.evaluate('location.hash') == '#ode-validation'
            cardinality_row = page.locator('.ode-issues li').filter(has_text='Размеры')
            cardinality_row.get_by_role('button').click()
            page.wait_for_function("document.activeElement?.closest('.ode-attribute')?.dataset.attribute === '909003'")
            page.get_by_text('Проверить поле ТН ВЭД',exact=True).click()
            page.wait_for_function("document.activeElement?.closest('.ode-attribute')?.dataset.attribute === '22232'")
            page.get_by_text('Проверить поле маркировки',exact=True).click()
            page.wait_for_function("document.activeElement?.closest('.ode-attribute')?.dataset.attribute === '23536'")
            page.locator('.ode-ai-scope').scroll_into_view_if_needed()
            layout(page,'literal-evidence')
            passed('exact_item_evidence_and_values_visible_without_auto_apply')

            page.locator('.ode-ai-item').first.get_by_role('checkbox').check()
            page.get_by_role('button',name='Принять выбранные').click()
            page.locator('.ode-ai-dialog[open]').get_by_role('checkbox').check()
            state['lose_next'] = True
            page.locator('.ode-ai-dialog[open]').get_by_role('button',name='Подтвердить решение').click()
            page.get_by_text('Результат решения неизвестен.').wait_for()
            assert state['review_effects'] == 1
            page.get_by_role('button',name='Прочитать состояние').click()
            page.get_by_role('button',name='Повторить то же решение').click()
            page.get_by_text('Выбранные значения сохранены.').wait_for()
            page.locator('.ode-ai-item.is-accepted').wait_for()
            assert page.locator('.ode-ai-item.is-accepted').count() == 1
            assert page.locator('.ode-ai-item.is-proposed').count() == 1
            assert state['review_effects'] == 1
            after_ai = editor_document(page)
            assert after_ai['draft']['version'] == before_ai['draft']['version']
            assert after_ai['draft']['source_fact_hash'] == before_ai['draft']['source_fact_hash']
            assert after_ai['documents']['content'] == before_ai['documents']['content']
            assert after_ai['documents']['attributes'] == before_ai['documents']['attributes']
            assert after_ai['documents']['media'] == before_ai['documents']['media']
            assert after_ai['current_validation']['publishable'] is False
            assert report['posts']['/marketplaces/api/drafts/'+str(draft_id)+'/ai-suggestions/apply'] == 2
            layout(page,'accepted-review')
            passed('lost_apply_replays_exact_key_once_and_never_auto_publishes')

            page.get_by_role('button',name='Найти значения характеристик').click()
            page.locator('.ode-ai-dialog[open]').get_by_role('checkbox').check()
            state['drop_next'] = True
            dialog = page.locator('.ode-ai-dialog[open]')
            assert 'пустых простых характеристик' in dialog.inner_text()
            assert 'В рамках этого действия название, бренд, заполненные поля, ТН ВЭД и маркировка не меняются.' in dialog.inner_text()
            assert 'только после вашего отдельного принятия' in dialog.inner_text()
            assert 'отправки в Ozon не будет' in dialog.inner_text()
            dialog.get_by_role('button',name='Запустить поиск').click()
            page.get_by_text('Результат AI-запуска неизвестен.').wait_for()
            page.get_by_role('button',name='Проверить запуск').click()
            page.get_by_role('button',name='Повторить тот же запрос').wait_for()
            assert report['posts']['/marketplaces/api/drafts/ai-completions'] == 2
            page.get_by_role('button',name='Повторить тот же запрос').click()
            page.wait_for_url(base+'/marketplaces/drafts/ai-completions/'+run_uid)
            assert state['generation_bodies'][-2] == state['generation_bodies'][-1]
            assert report['posts']['/marketplaces/api/drafts/ai-completions'] == 3
            layout(page,'manual-same-key-retry')
            passed('not_arrived_generate_404_requires_explicit_same_payload_retry')

            with app.app_context():
                assert MarketplaceOperation.query.count() == 0
            assert report['provider_attempts'] == 0 and not report['external']
            browser.close()
    report['status'] = 'passed'
finally:
    server.shutdown()
    temporary.cleanup()
    (OUT/'draft-ai-browser.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
