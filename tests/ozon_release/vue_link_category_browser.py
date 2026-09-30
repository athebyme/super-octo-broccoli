"""Synthetic full-app Vue journeys for manual linking and category review."""
import base64
import hashlib
from io import BytesIO
import json
import logging
import os
from pathlib import Path
import socket
import tempfile
import threading
from urllib.parse import parse_qs, urlsplit

import requests
from PIL import Image
from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server

OUT = Path(os.environ.get('OZON_BROWSER_ARTIFACTS', '/artifacts'))
OUT.mkdir(parents=True, exist_ok=True)
ASSETS = Path(__file__).with_name('assets')
report = {'status':'running', 'checks':[], 'layouts':[], 'js_errors':[], 'unexpected_http':[], 'external':[],
          'provider_attempts':0, 'posts':{}, 'scope':'synthetic_vue_link_category',
          'visibility_simulated':True}
temporary = tempfile.TemporaryDirectory(prefix='ozon-vue-link-category-')
os.environ.update(
    DATABASE_URL='sqlite:///'+str(Path(temporary.name)/'app.sqlite'),
    SKIP_SCHEDULER='1', IMAGE_LAB_INLINE_WORKER='0',
    SECRET_KEY='synthetic-vue-link-category-ci',
    ENCRYPTION_KEY=base64.urlsafe_b64encode(b'0'*32).decode(),
    MARKETPLACE_OZON_ENABLED='1', MARKETPLACE_OZON_PUBLICATION_ENABLED='0',
    MARKETPLACE_OZON_COMMERCIAL_WRITES_ENABLED='0',
    MARKETPLACE_OZON_AUTO_PUBLISH_ENABLED='0',
    OZON_RATE_LIMIT_DIR=str(Path(temporary.name)/'limits'),
)

def no_network(*args, **kwargs):
    report['provider_attempts'] += 1
    raise AssertionError('External Python network forbidden')

requests.sessions.Session.request = no_network
socket.create_connection = no_network

from seller_platform import app
from models import db, MarketplaceListing, MarketplaceProductDraft, MarketplaceProductType
from tests.ozon_release.seed import seed, USERNAME, PASSWORD

app.config.update(TESTING=True, WTF_CSRF_ENABLED=True, SESSION_COOKIE_SECURE=False)
fixture = seed(app)
with app.app_context():
    listing = db.session.get(MarketplaceListing, fixture['listing_ids'][0])
    listing.link_status = 'ambiguous'
    draft = db.session.get(MarketplaceProductDraft, fixture['draft_id'])
    draft.attributes_json = json.dumps([{'attribute_id':'4191','complex_id':'0','values':[{'value':'Ручное описание для старого типа'}]}], ensure_ascii=False)
    draft.complex_attributes_json = json.dumps([{'attributes':[{'attribute_id':'700','complex_id':'2','values':[{'value':'Ручное составное значение'}]}]}], ensure_ascii=False)
    draft.attribute_removals_json = json.dumps([{'attribute_id':'701','complex_id':'0'}])
    db.session.commit()
    old_type_id = draft.product_type_id
    old_type_name = draft.product_type.name
    target_type = MarketplaceProductType.query.filter(
        MarketplaceProductType.id != old_type_id,
    ).order_by(MarketplaceProductType.id.asc()).first()
    assert target_type is not None
    target_type_id = target_type.id
    target_type_name = target_type.name

logging.getLogger('werkzeug').setLevel(logging.ERROR)
server = make_server('127.0.0.1', 0, app, threaded=True)
worker = threading.Thread(target=server.serve_forever, daemon=True)
worker.start()
base = 'http://127.0.0.1:'+str(server.server_port)
manifest = json.loads((ASSETS/'manifest.json').read_text())
for asset in manifest.values():
    assert hashlib.sha256((ASSETS/asset['file']).read_bytes()).hexdigest() == asset['sha256']
svg = b'<svg xmlns="http://www.w3.org/2000/svg" width="160" height="160"><rect width="160" height="160" fill="#e7e2d8"/><circle cx="80" cy="80" r="42" fill="#9b6d5a"/></svg>'
jpeg_buffer = BytesIO()
Image.new('RGB', (16, 16), '#9b6d5a').save(jpeg_buffer, format='JPEG')
jpeg = jpeg_buffer.getvalue()
fault = {'path':None}
photo = {'mode':'ready', 'calls':[]}

def bridge(route):
    request = route.request
    url = urlsplit(request.url)
    if url.hostname == '127.0.0.1' and url.port == server.server_port:
        if request.method not in ('GET', 'HEAD'):
            assert url.path == '/login' or url.path in (
                f'/marketplaces/listings/{fixture["listing_ids"][0]}/link',
                f'/marketplaces/listings/{fixture["listing_ids"][0]}/unlink',
                f'/marketplaces/listings/{fixture["foreign_listing_id"]}/link',
                f'/marketplaces/drafts/{fixture["draft_id"]}',
            ), url.path
            if url.path != '/login':
                report['posts'][url.path] = report['posts'].get(url.path, 0) + 1
        if url.path.startswith('/api/photos/imported-product/'):
            assert url.path == f'/api/photos/imported-product/{fixture["source_id"]}/0', url.path
            query = parse_qs(url.query)
            assert query.get('deferred') == ['1'] and set(query) <= {'deferred', 'preview_attempt', 'manual_retry'}, query
            photo['calls'].append(url.query)
            if photo['mode'] == 'pending' or (photo['mode'] == 'cold' and len(photo['calls']) == 1):
                route.fulfill(status=202, body=b'', content_type='image/jpeg', headers={
                    'X-Photo-Cache':'pending', 'Retry-After':'2', 'Cache-Control':'no-store',
                }); return
            route.fulfill(body=jpeg, content_type='image/jpeg', headers={'Cache-Control':'no-store'}); return
        response = route.fetch(max_redirects=0)
        if fault['path'] == url.path and request.method == 'POST':
            fault['path'] = None
            assert response.status == 200, (url.path, response.status, response.body()[:400])
            route.abort(); return
        route.fulfill(response=response); return
    asset = manifest.get(request.url) or manifest.get(request.url.rstrip('/'))
    if asset:
        route.fulfill(body=(ASSETS/asset['file']).read_bytes(), content_type=asset['content_type']); return
    if request.url == 'https://ozon-fixture.test/product.svg':
        route.fulfill(body=svg, content_type='image/svg+xml'); return
    report['external'].append({'host':url.hostname, 'path':url.path})
    route.abort()

def passed(name):
    assert not report['js_errors'], report['js_errors']
    report['checks'].append(name)
    print(json.dumps({'vue_link_category_check':name}), flush=True)

def photo_response_predicate(source_id, query):
    expected_path = f'/api/photos/imported-product/{source_id}/0'
    return lambda response: response.request.method == 'GET' \
        and urlsplit(response.url).path == expected_path \
        and urlsplit(response.url).query == query

def assert_pending_photo_response(response_info, source_id, query):
    response = response_info.value
    expected_path = f'/api/photos/imported-product/{source_id}/0'
    assert response.status == 202, (response.url, response.status)
    assert urlsplit(response.url).path == expected_path, response.url
    assert urlsplit(response.url).query == query, response.url
    assert query in photo['calls'], (query, photo['calls'])

def layout(page, name):
    page.bring_to_front()
    for theme in ('light', 'dark'):
        for width in (320, 390, 768, 1440):
            page.set_viewport_size({'width':width, 'height':1000})
            page.evaluate('(value)=>document.documentElement.dataset.theme=value', theme)
            page.wait_for_function('document.fonts.status === "loaded"')
            page.screenshot(path=str(OUT/f'vue-link-category-{name}-{theme}-{width}.png'), animations='disabled')
            overflow = page.evaluate('''() => Array.from(document.querySelectorAll('*'))
                .filter(node => node.getBoundingClientRect().right > innerWidth + 1)
                .slice(0, 8).map(node => ({tag:node.tagName, className:String(node.className).slice(0, 90),
                    right:Math.round(node.getBoundingClientRect().right), width:Math.round(node.getBoundingClientRect().width)}))''')
            if page.evaluate('document.documentElement.scrollWidth > innerWidth + 1'):
                page.screenshot(path=str(OUT/f'vue-link-category-overflow-{name}-{theme}-{width}.png'), animations='disabled')
                boxes = page.evaluate('''() => Object.fromEntries(['.main-content','#main-content','#marketplace-detail-app','.mdet-grid','.mdet-info','.mdet-price-card','.mdet-link-dialog'].map(selector => {let node=document.querySelector(selector), rect=node?.getBoundingClientRect(); return [selector, rect && {left:rect.left,right:rect.right,width:rect.width,scroll:node.scrollWidth,client:node.clientWidth}]}))''')
                raise AssertionError((name, theme, width, boxes, overflow))
            dialog = page.locator('dialog[open]')
            if dialog.count():
                assert dialog.evaluate('(node)=>node.scrollWidth <= node.clientWidth + 1'), (name, theme, width, 'dialog')
            if name == 'photo-fallback':
                thumb = page.locator(f'.mdet-link-candidate:has(#mdet-link-candidate-{fixture["source_id"]}) .mdet-link-thumb')
                assert thumb.evaluate('''node => {
                    const parent = node.getBoundingClientRect();
                    return node.scrollWidth <= node.clientWidth + 1 &&
                        Array.from(node.querySelectorAll('*')).every(child => {
                            const rect = child.getBoundingClientRect();
                            return child.scrollWidth <= child.clientWidth + 1 &&
                                rect.left >= parent.left - 1 && rect.right <= parent.right + 1;
                        });
                }'''), (name, theme, width, 'thumb-content')
            report['layouts'].append({'name':name, 'theme':theme, 'width':width})
    page.set_viewport_size({'width':1440, 'height':1000})
    page.evaluate('document.documentElement.dataset.theme="light"')

try:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=os.environ.get('OZON_BROWSER_CHROMIUM', '/usr/bin/chromium'),
            headless=True, args=['--no-sandbox','--disable-dev-shm-usage'],
        )
        context = browser.new_context(viewport={'width':1440, 'height':1000}, service_workers='block')
        context.route('**/*', bridge)
        page = context.new_page(); page.set_default_timeout(20000)
        page.on('pageerror', lambda error:report['js_errors'].append(str(error)))
        page.on('response', lambda response:report['unexpected_http'].append({'status':response.status, 'url':response.url}) if response.status >= 500 else None)
        page.goto(base+'/login?next=/marketplaces/listings/view/'+str(fixture['listing_ids'][0]))
        page.locator('[name=username]').fill(USERNAME)
        page.locator('[name=password]').fill(PASSWORD)
        page.get_by_role('button',name='Войти',exact=True).click()
        page.locator('#marketplace-detail-app:not([v-cloak])').wait_for()
        page.get_by_role('button',name='Выбрать вручную',exact=True).click()
        dialog = page.get_by_role('dialog')
        assert dialog.get_by_role('searchbox',name='Поиск сохранённой внутренней карточки').evaluate('(node)=>document.activeElement===node')
        page.keyboard.press('Escape')
        assert dialog.count() == 0
        assert page.get_by_role('button',name='Выбрать вручную',exact=True).evaluate('(node)=>document.activeElement===node')
        photo.update(mode='cold', calls=[])
        page.get_by_role('button',name='Выбрать вручную',exact=True).click()
        dialog = page.get_by_role('dialog')
        dialog.get_by_role('searchbox',name='Поиск сохранённой внутренней карточки').fill('Тестовый товар для подготовки')
        dialog.get_by_text('Тестовый товар для подготовки',exact=True).wait_for()
        thumb = dialog.locator(f'.mdet-link-candidate:has(#mdet-link-candidate-{fixture["source_id"]}) .mdet-link-thumb')
        thumb.get_by_text('Загружаем фото…', exact=True).wait_for()
        thumb.locator('img').wait_for()
        page.wait_for_function('''id => {
            const image = document.querySelector('#mdet-link-candidate-' + id)?.parentElement?.querySelector('.mdet-link-thumb img');
            return image?.complete && image.naturalWidth === 16 && image.naturalHeight === 16;
        }''', arg=fixture['source_id'])
        thumb.locator('.mdet-link-photo-state').wait_for(state='detached')
        assert thumb.locator('.mdet-link-photo-failed,.mdet-link-photo-retry').count() == 0
        assert photo['calls'] == ['deferred=1', 'deferred=1&preview_attempt=1'], photo['calls']
        assert thumb.get_by_text('Фото не найдено').count() == 0
        passed('candidate_photo_202_then_jpeg_ready_without_reload')

        dialog.get_by_role('button',name='Вернуться к карточке').click()
        photo.update(mode='pending', calls=[])
        with page.expect_response(photo_response_predicate(fixture['source_id'], 'deferred=1'), timeout=5000) as photo_response:
            page.get_by_role('button',name='Выбрать вручную',exact=True).click()
            dialog = page.get_by_role('dialog')
            searchbox = dialog.get_by_role('searchbox',name='Поиск сохранённой внутренней карточки')
            searchbox.fill('Тестовый товар для подготовки')
            dialog.get_by_text('Тестовый товар для подготовки',exact=True).wait_for()
            thumb = dialog.locator(f'.mdet-link-candidate:has(#mdet-link-candidate-{fixture["source_id"]}) .mdet-link-thumb')
            thumb.get_by_text('Загружаем фото…', exact=True).wait_for()
        assert_pending_photo_response(photo_response, fixture['source_id'], 'deferred=1')
        assert len(photo['calls']) == 1, photo['calls']
        page.evaluate("Object.defineProperty(document,'hidden',{configurable:true,value:true});document.dispatchEvent(new Event('visibilitychange'))")
        thumb.get_by_role('button',name=f'Продолжить загрузку фото внутренней карточки {fixture["source_id"]}').wait_for()
        page.wait_for_timeout(2300)
        assert len(photo['calls']) == 1, photo['calls']
        page.evaluate("Object.defineProperty(document,'hidden',{configurable:true,value:false});document.dispatchEvent(new Event('visibilitychange'))")
        thumb.get_by_role('button',name=f'Продолжить загрузку фото внутренней карточки {fixture["source_id"]}').click()
        thumb.get_by_text('Фото недоступно', exact=True).wait_for(timeout=18000)
        assert photo['calls'] == [
            'deferred=1', 'deferred=1&manual_retry=1',
            'deferred=1&preview_attempt=1&manual_retry=1',
            'deferred=1&preview_attempt=2&manual_retry=1',
            'deferred=1&preview_attempt=3&manual_retry=1',
        ], photo['calls']
        layout(page, 'photo-fallback')
        retry_button = thumb.get_by_role('button',name=f'Повторить загрузку фото внутренней карточки {fixture["source_id"]}')
        assert retry_button.evaluate('(node)=>node.getBoundingClientRect().height >= 44')
        retry_button.focus()
        assert retry_button.evaluate('(node)=>document.activeElement===node')
        retry_query = 'deferred=1&manual_retry=2'
        with page.expect_response(photo_response_predicate(fixture['source_id'], retry_query), timeout=5000) as photo_response:
            page.keyboard.press('Enter')
            thumb.get_by_text('Загружаем фото…', exact=True).wait_for()
        assert_pending_photo_response(photo_response, fixture['source_id'], retry_query)
        assert len(photo['calls']) == 6, photo['calls']
        assert dialog.get_by_role('radio').first.is_checked() is False
        dialog.get_by_role('button',name='Вернуться к карточке').click()
        page.wait_for_timeout(2300)
        assert len(photo['calls']) == 6, photo['calls']
        passed('candidate_photo_simulated_hidden_stop_bounded_fallback_manual_retry_and_unmount')

        photo.update(mode='pending', calls=[])
        page.evaluate("Object.defineProperty(document,'hidden',{configurable:true,value:true});document.dispatchEvent(new Event('visibilitychange'))")
        page.get_by_role('button',name='Выбрать вручную',exact=True).click()
        dialog = page.get_by_role('dialog')
        dialog.get_by_role('searchbox',name='Поиск сохранённой внутренней карточки').fill('Тестовый товар для подготовки')
        thumb = dialog.locator(f'.mdet-link-candidate:has(#mdet-link-candidate-{fixture["source_id"]}) .mdet-link-thumb')
        thumb.get_by_role('button',name=f'Продолжить загрузку фото внутренней карточки {fixture["source_id"]}').wait_for()
        assert not photo['calls'], photo['calls']
        page.evaluate("Object.defineProperty(document,'hidden',{configurable:true,value:false});document.dispatchEvent(new Event('visibilitychange'))")
        assert not photo['calls'], photo['calls']
        resume_query = 'deferred=1&manual_retry=1'
        with page.expect_response(photo_response_predicate(fixture['source_id'], resume_query), timeout=5000) as photo_response:
            thumb.get_by_role('button',name=f'Продолжить загрузку фото внутренней карточки {fixture["source_id"]}').click()
            thumb.get_by_text('Загружаем фото…',exact=True).wait_for()
        assert_pending_photo_response(photo_response, fixture['source_id'], resume_query)
        assert len(photo['calls']) == 1, photo['calls']
        dialog.get_by_role('button',name='Вернуться к карточке').click()
        passed('candidate_photo_simulated_initial_hidden_has_no_get_until_manual_resume')

        photo.update(mode='ready', calls=[])
        page.get_by_role('button',name='Выбрать вручную',exact=True).click()
        dialog = page.get_by_role('dialog')
        dialog.get_by_role('searchbox',name='Поиск сохранённой внутренней карточки').fill('Тестовый товар для подготовки')
        dialog.get_by_text('Тестовый товар для подготовки',exact=True).wait_for()
        layout(page, 'link-review')
        dialog.locator(f'.mdet-link-candidate:has(#mdet-link-candidate-{fixture["source_id"]}) .mdet-link-thumb').click()
        assert dialog.get_by_role('radio').first.is_checked()
        dialog.get_by_role('checkbox').check()
        link_path = f'/marketplaces/listings/{fixture["listing_ids"][0]}/link'
        before = report['posts'].get(link_path, 0)
        fault['path'] = link_path
        dialog.get_by_role('button',name='Связать карточки',exact=True).click()
        dialog.get_by_text('Результат записи неизвестен.',exact=False).wait_for()
        assert report['posts'].get(link_path, 0) == before + 1
        dialog.get_by_role('button',name='Прочитать сохранённую связь').click()
        dialog.get_by_role('button',name='Использовать просмотренное состояние').wait_for()
        assert report['posts'].get(link_path, 0) == before + 1
        photo.update(mode='cold', calls=[])
        dialog.get_by_role('button',name='Использовать просмотренное состояние').click()
        assert 'Тестовый товар для подготовки' in dialog.inner_text()
        canonical_thumb = page.locator('.mlink-card--canonical .mdet-link-thumb')
        canonical_thumb.get_by_text('Загружаем фото…', exact=True).wait_for()
        page.wait_for_function('''() => {
            const image = document.querySelector('.mlink-card--canonical .mdet-link-thumb img');
            return image?.complete && image.naturalWidth === 16 && image.naturalHeight === 16;
        }''')
        canonical_thumb.locator('.mdet-link-photo-state').wait_for(state='detached')
        assert canonical_thumb.locator('.mdet-link-photo-failed,.mdet-link-photo-retry').count() == 0
        assert photo['calls'] == ['deferred=1', 'deferred=1&preview_attempt=1'], photo['calls']
        passed('canonical_tile_202_then_jpeg_uses_same_exact_source')
        dialog.get_by_role('button',name='Вернуться к карточке').click()
        passed('manual_owned_link_lost_post_single_attempt_and_explicit_readback')

        foreign_id = fixture['foreign_listing_id']
        negative = page.evaluate('''async ({foreignId, ownedId, sourceId, version}) => {
            const get = path => fetch(path, {headers:{Accept:'application/json'}}).then(async r => ({status:r.status, body:await r.text()}));
            const foreignGet = await get('/marketplaces/listings/view/'+foreignId);
            const foreignSearch = await get('/marketplaces/listings/view/'+foreignId+'/link-candidates?q=secret');
            const foreignPost = await fetch('/marketplaces/listings/'+foreignId+'/link', {method:'POST',headers:{'Content-Type':'application/json','X-CSRFToken':document.querySelector('meta[name="csrf-token"]').content},body:JSON.stringify({imported_product_id:sourceId,expected_link_version:version})});
            const frame = document.createElement('iframe'); document.body.append(frame);
            const noCsrf = await frame.contentWindow.fetch('/marketplaces/listings/'+ownedId+'/unlink', {method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify({expected_link_version:version})});
            frame.remove();
            return {foreignGet,foreignSearch,foreignPost:foreignPost.status,noCsrf:noCsrf.status};
        }''', {'foreignId':foreign_id, 'ownedId':fixture['listing_ids'][0], 'sourceId':fixture['source_id'], 'version':1})
        assert negative['foreignGet']['status'] == 404 and negative['foreignSearch']['status'] == 404, negative
        assert negative['foreignPost'] == 404 and negative['noCsrf'] in (400, 403), negative
        assert 'FOREIGN-SECRET-OFFER' not in json.dumps(negative) and 'Foreign private title' not in json.dumps(negative), negative
        passed('foreign_scope_and_missing_csrf_fail_closed')

        page.get_by_role('button',name='Отвязать связь',exact=True).click()
        dialog = page.get_by_role('dialog')
        assert 'не удаляются' in dialog.inner_text()
        layout(page, 'unlink-review')
        passed('unlink_impact_review_without_implicit_rebind')

        old_csrf = page.locator('meta[name="csrf-token"]').get_attribute('content')
        login_tab = context.new_page()
        login_tab.goto(base+'/logout')
        context.clear_cookies()
        login_tab.goto(base+'/login')
        login_tab.locator('[name=username]').fill(USERNAME)
        login_tab.locator('[name=password]').fill(PASSWORD)
        with login_tab.expect_navigation(wait_until='domcontentloaded'):
            login_tab.get_by_role('button',name='Войти',exact=True).click()
        assert '/login' not in login_tab.url
        login_tab.locator('meta[name="csrf-token"]').wait_for(state='attached')
        assert login_tab.locator('meta[name="csrf-token"]').get_attribute('content') != old_csrf
        login_tab.close(); page.bring_to_front()
        unlink_path = f'/marketplaces/listings/{fixture["listing_ids"][0]}/unlink'
        unlink_before = report['posts'].get(unlink_path, 0)
        dialog.get_by_role('checkbox').check()
        dialog.get_by_role('button',name='Отвязать связь',exact=True).click()
        dialog.get_by_role('button',name='Прочитать сохранённую связь').wait_for()
        assert report['posts'].get(unlink_path, 0) == unlink_before + 1
        with app.app_context():
            assert db.session.get(MarketplaceListing, fixture['listing_ids'][0]).imported_product_id == fixture['source_id']
        dialog.get_by_role('button',name='Прочитать сохранённую связь').click()
        dialog.get_by_role('button',name='Использовать просмотренное состояние').wait_for()
        dialog.get_by_role('button',name='Использовать просмотренное состояние').click()
        dialog.get_by_role('checkbox').check()
        dialog.get_by_role('button',name='Отвязать связь',exact=True).click()
        page.get_by_role('button',name='Выбрать вручную',exact=True).wait_for()
        assert report['posts'].get(unlink_path, 0) == unlink_before + 2
        with app.app_context():
            assert db.session.get(MarketplaceListing, fixture['listing_ids'][0]).imported_product_id is None
        passed('new_session_csrf_readback_and_reviewed_unlink')

        page.goto(base+f'/marketplaces/drafts/{fixture["draft_id"]}')
        page.locator('#ozon-draft-editor:not([v-cloak])').wait_for()
        page.locator('#ode-name').fill('Несохранённое название остаётся здесь')
        page.get_by_text('Изменить категорию', exact=True).click()
        page.locator('#ode-type-search').fill(target_type_name)
        page.locator('.ode-type-option').filter(has_text=target_type_name).first.get_by_role('radio').check()
        page.get_by_role('button',name='Проверить смену категории').click()
        assert page.locator('#ode-name').input_value() == 'Несохранённое название остаётся здесь'
        assert page.get_by_text('Сначала сохраните текущие правки',exact=False).is_visible()
        page.get_by_role('button',name='Сохранить',exact=True).click()
        page.get_by_text('Все изменения сохранены',exact=True).wait_for()
        assert page.locator('.ode-type-option input:checked').count() == 1
        page.get_by_role('button',name='Проверить смену категории').click()
        dialog = page.get_by_role('dialog')
        dialog.get_by_text('Ручное составное значение',exact=False).wait_for()
        assert 'План удаления' in dialog.inner_text()
        assert 'Ручное описание для старого типа' in dialog.inner_text()
        layout(page, 'category-impact')
        with app.app_context():
            reviewed_version = db.session.get(MarketplaceProductDraft, fixture['draft_id']).version
        impact_base = f'/marketplaces/drafts/{fixture["draft_id"]}/category-impact'
        invalid_queries = page.evaluate('''async ({path,version,target}) => {
            const queries = [
                `expected_version=${version}&target_product_type_id=${target}&extra=1`,
                `expected_version=${version}&expected_version=${version}&target_product_type_id=${target}`,
                `expected_version=${'9'.repeat(5000)}&target_product_type_id=${target}`,
            ];
            return Promise.all(queries.map(query => fetch(path+'?'+query,{headers:{Accept:'application/json'}}).then(r=>r.status)));
        }''', {'path':impact_base, 'version':reviewed_version, 'target':target_type_id})
        assert invalid_queries == [400, 400, 400], invalid_queries
        passed('category_preview_rejects_unknown_duplicate_and_unbounded_query')
        page.keyboard.press('Escape')
        assert dialog.count() == 0
        assert page.locator('.ode-type-option input:checked').count() == 1
        page.get_by_role('button',name='Проверить смену категории').click()
        dialog = page.get_by_role('dialog')
        dialog.get_by_text('Ручное составное значение',exact=False).wait_for()
        draft_path = f'/marketplaces/drafts/{fixture["draft_id"]}'
        before = report['posts'].get(draft_path, 0)
        dialog.get_by_role('checkbox',name='Я проверил(а) перечисленные изменения сохранённого черновика').check()
        fault['path'] = draft_path
        dialog.get_by_role('button',name='Сохранить новую категорию').click()
        dialog.get_by_role('button',name='Прочитать сохранённое состояние').wait_for()
        assert report['posts'].get(draft_path, 0) == before + 1
        dialog.get_by_role('button',name='Прочитать сохранённое состояние').click()
        dialog.get_by_role('button',name='Использовать просмотренное состояние').wait_for()
        assert report['posts'].get(draft_path, 0) == before + 1
        dialog.get_by_role('button',name='Использовать просмотренное состояние').click()
        with app.app_context():
            draft = db.session.get(MarketplaceProductDraft, fixture['draft_id'])
            assert draft.product_type_id == target_type_id
            assert json.loads(draft.complex_attributes_json) == []
            assert json.loads(draft.attribute_removals_json) == []
        passed('dirty_input_separate_save_exact_impact_lost_post_and_readback')

        type_details = page.locator('.ode-category details')
        if not type_details.evaluate('(node)=>node.open'):
            type_details.locator('summary').click()
        page.locator('#ode-type-search').fill(old_type_name)
        page.locator('.ode-type-option').filter(has_text=old_type_name).first.get_by_role('radio').check()
        page.get_by_role('button',name='Проверить смену категории').click()
        dialog = page.get_by_role('dialog')
        dialog.get_by_role('checkbox',name='Я проверил(а) перечисленные изменения сохранённого черновика').wait_for()
        old_csrf = page.locator('meta[name="csrf-token"]').get_attribute('content')
        login_tab = context.new_page()
        login_tab.goto(base+'/logout')
        context.clear_cookies()
        login_tab.goto(base+'/login')
        login_tab.locator('[name=username]').fill(USERNAME)
        login_tab.locator('[name=password]').fill(PASSWORD)
        with login_tab.expect_navigation(wait_until='domcontentloaded'):
            login_tab.get_by_role('button',name='Войти',exact=True).click()
        assert '/login' not in login_tab.url
        fresh_csrf = login_tab.locator('meta[name="csrf-token"]').get_attribute('content')
        assert fresh_csrf != old_csrf
        login_tab.close(); page.bring_to_front()
        before = report['posts'].get(draft_path, 0)
        dialog.get_by_role('checkbox',name='Я проверил(а) перечисленные изменения сохранённого черновика').check()
        dialog.get_by_role('button',name='Сохранить новую категорию').click()
        dialog.get_by_role('button',name='Прочитать сохранённое состояние').wait_for()
        assert report['posts'].get(draft_path, 0) == before + 1
        with app.app_context():
            assert db.session.get(MarketplaceProductDraft, fixture['draft_id']).product_type_id == target_type_id
        dialog.get_by_role('button',name='Прочитать сохранённое состояние').click()
        dialog.get_by_role('button',name='Использовать просмотренное состояние').wait_for()
        dialog.get_by_role('button',name='Использовать просмотренное состояние').click()
        dialog.get_by_role('checkbox',name='Я проверил(а) перечисленные изменения сохранённого черновика').wait_for()
        dialog.get_by_role('checkbox',name='Я проверил(а) перечисленные изменения сохранённого черновика').check()
        dialog.get_by_role('button',name='Сохранить новую категорию').click()
        dialog.wait_for(state='hidden')
        assert report['posts'].get(draft_path, 0) == before + 2
        with app.app_context():
            assert db.session.get(MarketplaceProductDraft, fixture['draft_id']).product_type_id == old_type_id
        passed('new_session_csrf_readback_new_preview_and_category_apply')
        context.close(); browser.close()
    assert not report['external'] and report['provider_attempts'] == 0, report
    report['status'] = 'passed'
finally:
    server.shutdown(); worker.join(timeout=5); temporary.cleanup()
    (OUT/'vue-link-category-browser.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
