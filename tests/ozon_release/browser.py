"""Chromium + real local HTTP/Flask/routes/services/SQLite, synthetic and offline.

The test server binds container loopback only. Production Gunicorn/TLS and live
seller-data acceptance remain separate gates.
"""
from datetime import datetime, timezone
from contextlib import contextmanager
import hashlib
import html
import json
import os
import re
from pathlib import Path
import socket
import tempfile
import threading
import logging
from urllib.parse import urlsplit

import requests
from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server

OUT = Path('/artifacts')
ASSETS = Path(__file__).with_name('assets')
BASE = None
UA = 'SellerHubSyntheticBrowser/1.0'
REPORT = {'status':'running','checks':[],'layouts':[],'js_errors':[],
          'unexpected_http':[],'unexpected_external':[],'provider_attempts':0,
          'scope':'synthetic_full_app_local_http','mutations':[],'documents':[]}
temporary = tempfile.TemporaryDirectory(prefix='ozon-browser-')
os.environ['DATABASE_URL'] = 'sqlite:///' + str(Path(temporary.name)/'app.sqlite')
os.environ['SKIP_SCHEDULER'] = '1'
os.environ['IMAGE_LAB_INLINE_WORKER'] = '0'


def forbid_network(*args, **kwargs):
    REPORT['provider_attempts'] += 1
    raise AssertionError('External network forbidden in synthetic browser suite')


requests.sessions.Session.request = forbid_network
# Browser/driver can reach the loopback test server in this network=none container.
# Python application code has no reason to make an outbound HTTP connection.
socket.create_connection = forbid_network

from seller_platform import app
from models import db, BackgroundJob, MarketplaceOperation, MarketplaceListing, MarketplaceProductDraft
from services.ozon_quality_queue import run_quality_tick
from tests.ozon_release.seed import seed, USERNAME, PASSWORD, PHOTO, CATEGORIES

app.config.update(TESTING=True, WTF_CSRF_ENABLED=True, SESSION_COOKIE_SECURE=False)
fixture = seed(app)
aid = fixture['account_id']
logging.getLogger('werkzeug').setLevel(logging.ERROR)
server = make_server('127.0.0.1',0,app,threaded=True)
BASE = 'http://127.0.0.1:'+str(server.server_port)
server_thread = threading.Thread(target=server.serve_forever,daemon=True)
server_thread.start()
manifest = json.loads((ASSETS/'manifest.json').read_text())
for item in manifest.values():
    assert hashlib.sha256((ASSETS/item['file']).read_bytes()).hexdigest() == item['sha256']

SVG = b'<svg xmlns="http://www.w3.org/2000/svg" width="360" height="480" viewBox="0 0 360 480"><rect width="360" height="480" fill="#eee9df"/><rect x="80" y="90" width="200" height="280" rx="8" fill="#b86b51"/><circle cx="180" cy="230" r="54" fill="#faf9f7"/><path d="M160 230h40M180 210v40" stroke="#343434" stroke-width="8"/></svg>'
transport = {'fault':None,'expected_errors':set()}
page = None


def passed(name):
    assert not REPORT['js_errors'], REPORT['js_errors']
    REPORT['checks'].append(name)
    print(json.dumps({'browser_check':name}), flush=True)


def bridge(route):
    request = route.request
    url = urlsplit(request.url)
    if url.hostname == '127.0.0.1' and url.port == server.server_port:
        if request.method not in ('GET','HEAD'):
            allowed = {'/login', '/marketplaces/api/quality/refresh',
                       '/marketplaces/drafts/'+str(fixture['draft_id'])}
            assert url.path in allowed, ('Unexpected browser mutation',url.path)
            REPORT['mutations'].append({'method':request.method,'path':url.path})
        quality_list = url.path == '/marketplaces/api/quality/workspace'
        if quality_list and transport['fault'] == 'network':
            route.abort(); return
        if quality_list and transport['fault'] == 'session':
            route.fulfill(status=401,json={'error':'Session expired'}); return
        # Synthetic source image delivery is tested independently by photo routes.
        if url.path == f"/api/photos/imported-product/{fixture['source_id']}/0":
            route.fulfill(body=SVG,content_type='image/svg+xml'); return
        result = route.fetch(max_redirects=0)
        if request.resource_type=='document':
            REPORT['documents'].append({'path':url.path,'status':result.status,
                                        'redirect':urlsplit(result.headers.get('location','')).path})
        if result.status >= 400 and (url.path,result.status) not in transport['expected_errors']:
            REPORT['unexpected_http'].append({'path':url.path,'status':result.status})
        if quality_list and transport['fault'] == 'foreign':
            value=result.json();value['data']['scope']['account_id']=fixture['foreign_account_id']
            route.fulfill(status=200,json=value);return
        if url.path == '/marketplaces/api/quality/refresh' and request.method == 'POST' and transport['fault'] == 'lost-post':
            assert result.status==202
            route.abort();return
        route.fulfill(response=result)
        return
    if request.url == PHOTO:
        route.fulfill(body=SVG,content_type='image/svg+xml');return
    asset=manifest.get(request.url) or manifest.get(request.url.rstrip('/'))
    if asset:
        route.fulfill(body=(ASSETS/asset['file']).read_bytes(),content_type=asset['content_type']);return
    REPORT['unexpected_external'].append({'host':url.hostname,'path':url.path})
    route.abort()


def goto(path, selector):
    response=page.goto(BASE+path)
    assert response and response.status==200, (path,response.status if response else None)
    page.locator(selector).wait_for()
    page.wait_for_function('Array.from(document.querySelectorAll("[v-cloak]")).every(n=>!n.offsetParent)')
    page.wait_for_load_state('networkidle')
    page.evaluate('document.fonts.ready')


def layout(name, widths=(1440,390,320)):
    for theme in ('light','dark'):
        for width in widths:
            page.set_viewport_size({'width':width,'height':1000})
            page.evaluate('(theme)=>document.documentElement.dataset.theme=theme',theme)
            page.wait_for_timeout(80)
            # The shell animates margin-left for 200 ms across its breakpoint.
            # Measure settled layout; persistent overflow must still fail.
            page.wait_for_function("!document.querySelector('.main-content')?.getAnimations().some(a=>a.playState==='running')", timeout=2000)
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'), (name,theme,width,'page overflow')
            REPORT['layouts'].append({'name':name,'theme':theme,'width':width})
            if width in (1440,390):
                page.screenshot(path=str(OUT/f'{name}-{theme}-{width}.png'),animations='disabled')
    page.set_viewport_size({'width':1440,'height':1000})
    page.evaluate('document.documentElement.dataset.theme="light"')


def quality_loaded():
    page.wait_for_function("document.querySelector('#oqw-app:not([v-cloak]) section[aria-busy]')?.getAttribute('aria-busy')==='false'")


@contextmanager
def failure_artifact():
    try:
        yield
    except Exception:
        if page and not page.is_closed():
            try:
                page.screenshot(path=str(OUT/'failure.png'),animations='disabled')
                (OUT/'failure-context.json').write_text(json.dumps({'url':page.url,'title':page.title(),
                    'visible_text':page.locator('body').inner_text()[:4000]},ensure_ascii=False,indent=2))
            except Exception:
                pass
        raise


try:
    OUT.mkdir(exist_ok=True)
    with sync_playwright() as playwright, failure_artifact():
        browser=playwright.chromium.launch(executable_path='/usr/bin/chromium',headless=True,
                    args=['--no-sandbox','--disable-dev-shm-usage'])
        context=browser.new_context(user_agent=UA,viewport={'width':1440,'height':1000},
                    timezone_id='Europe/Moscow',service_workers='block')
        context.route('**/*',bridge)

        def new_page():
            result=context.new_page();result.set_default_timeout(15000)
            result.on('pageerror',lambda error:REPORT['js_errors'].append(str(error)))
            return result

        page=new_page()
        goto('/login?next=/marketplaces/listings/?account_id='+str(aid), 'input[name=username]')
        page.locator('input[name=username]').fill(USERNAME)
        page.locator('input[name=password]').fill('incorrect')
        page.locator('button[type=submit]').click()
        page.get_by_text('Неверное имя пользователя или пароль',exact=False).wait_for()
        page.locator('input[name=username]').fill(USERNAME)
        page.locator('input[name=password]').fill(PASSWORD)
        page.locator('button[type=submit]').click()
        page.locator('article.mcat-card:not(.mcat-skel)').first.wait_for()
        assert urlsplit(page.url).path=='/marketplaces/listings/' and 'account_id='+str(aid) in page.url
        assert page.locator('article.mcat-card:not(.mcat-skel)').count()==36
        passed('real_login_form_csrf_failure_and_success')
        for img in page.locator('article.mcat-card .mcat-photo img:first-child').all():
            img.scroll_into_view_if_needed()
        page.wait_for_function('Array.from(document.querySelectorAll("article.mcat-card .mcat-photo img:first-child")).every(i=>i.complete&&i.naturalWidth>0)')
        assert page.locator('article.mcat-card .mcat-photo img:first-child').count()==36
        page.evaluate('scrollTo(0,0)');layout('catalog')
        passed('thirty_six_catalog_photos_and_responsive_layout')
        for name in CATEGORIES:
            assert page.locator('article.mcat-card').filter(has_text=name).count()==6
        passed('six_distinct_categories_present_in_catalog')
        first=page.locator('article.mcat-card').filter(has_text='CI-000')
        for label in ['До скидок','С вашими акциями']:
            assert label in first.inner_text()
        assert first.locator('.mcat-disc').count()==0
        page.get_by_role('button',name='Таблица',exact=True).click()
        page.locator('.mcat-table tbody tr').first.wait_for()
        assert page.locator('.mcat-table tbody tr').count()==36
        layout('catalog-table');passed('same_price_facts_in_grid_and_table_without_false_marketplace_discount')
        page.get_by_role('button',name='Плитка',exact=True).click()
        search=page.get_by_role('searchbox',name='Поиск по каталогу')
        search.fill('чёрный')
        page.wait_for_function('document.querySelectorAll("article.mcat-card:not(.mcat-skel)").length===1')
        assert 'search=' in page.url
        page.reload();page.locator('article.mcat-card').wait_for()
        assert page.locator('article.mcat-card').count()==1
        passed('unicode_search_url_and_reload')
        for item in fixture['categories']:
            goto('/marketplaces/listings/view/'+str(item['listing_id']), '#marketplace-detail-app:not([v-cloak])')
            assert item['name'] in page.locator('main').inner_text()
            assert 'Для покупателя' in page.locator('main').inner_text()
        layout('listing-detail');passed('six_category_details_and_unknown_buyer_price')

        foreign=f"/marketplaces/listings/view/{fixture['foreign_listing_id']}"
        transport['expected_errors'].add((foreign,404))
        response=page.goto(BASE+foreign)
        assert response.status==404 and 'Foreign private title' not in page.content()
        passed('foreign_listing_denied_without_data_leak')

        goto('/marketplaces/drafts/'+str(fixture['draft_id']), '#ozon-draft-editor:not([v-cloak])')
        page.locator('#ode-name').wait_for()
        page.locator('#ode-name').fill('Проверенный синтетический черновик')
        page.get_by_role('button',name='Сохранить',exact=True).click()
        page.get_by_text('Все изменения сохранены',exact=True).wait_for()
        page.reload();page.locator('#ode-name').wait_for()
        assert page.locator('#ode-name').input_value()=='Проверенный синтетический черновик'
        assert page.get_by_role('button',name='Отправить в Ozon',exact=False).is_disabled()
        layout('draft-editor');passed('real_draft_save_csrf_reload_and_publication_gate')

        goto('/marketplaces/quality?account_id='+str(aid), '#oqw-app:not([v-cloak])')
        quality_loaded();assert page.locator('article.oqw-row').count()==25
        page.get_by_label('Выбрать страницу',exact=True).check()
        page.get_by_role('navigation',name='Страницы качества').get_by_role('link',name='Далее',exact=True).click()
        quality_loaded();assert page.locator('article.oqw-row').count()==11
        page.get_by_label('Выбрать страницу',exact=True).check();page.reload();quality_loaded()
        assert page.locator('.oqw-selection').inner_text().startswith('Выбрано 36')
        page.get_by_role('button',name='Снять выбор',exact=True).click()
        page.locator('select[name=state]').select_option('unassessed');quality_loaded()
        assert page.locator('article.oqw-row').count()==1
        page.locator('.oqw-detail-button').click();page.locator('dialog[open] .oqw-detail-score').wait_for()
        assert page.locator('.oqw-detail-score strong').inner_text()=='—'
        layout('quality-detail');page.keyboard.press('Escape')
        assert page.locator('dialog[open]').count()==0
        passed('quality_selection_pagination_reload_unassessed_and_keyboard_dialog')
        page.get_by_role('button',name='Сбросить фильтры',exact=True).click();quality_loaded()
        transport['fault']='network';page.get_by_role('button',name='Найти',exact=True).click()
        page.get_by_role('alert').wait_for();assert page.locator('article.oqw-row').count()==25
        transport['fault']='foreign';page.get_by_role('button',name='Повторить загрузку',exact=True).click()
        page.get_by_text('Не удалось подтвердить данные выбранного магазина.',exact=False).wait_for()
        assert page.locator('article.oqw-row').count()==25
        layout('quality-offline');passed('offline_and_wrong_scope_preserve_last_good_data')
        transport['fault']=None;page.get_by_role('button',name='Повторить загрузку',exact=True).click();quality_loaded()
        transport['fault']='lost-post'
        page.get_by_role('button',name='Пересчитать оценки',exact=True).click()
        page.get_by_text('До проверки состояния повторная отправка отключена.',exact=True).wait_for()
        assert page.locator('.oqw-header button').is_disabled()
        page.close();transport['fault']=None
        with app.app_context():
            assert BackgroundJob.query.filter_by(job_type='ozon_quality_recompute').count()==1
            assert MarketplaceOperation.query.count()==0
        result=run_quality_tick(app)
        assert result=={'processed':36,'completed':True}
        page=new_page();goto('/marketplaces/quality?account_id='+str(aid), '#oqw-app:not([v-cloak])')
        quality_loaded()
        page.get_by_text('Пересчёт завершён. Откройте обновлённые оценки.',exact=True).wait_for()
        layout('quality-completed');passed('lost_post_one_durable_job_closed_browser_completion')

        # Real empty/unavailable domain states are part of a new seller's journey.
        routes=[('/marketplaces/accounts/','#ozon-account-setup'),
                ('/marketplaces/drafts/','#ozon-drafts-list'),
                ('/marketplaces/commercial/','#ozon-commercial-app'),
                ('/marketplaces/analytics','#oan-app'),
                ('/marketplaces/finance','#ofn-app'),
                ('/marketplaces/finance/changes','#ofc-app'),
                ('/marketplaces/orders','#of-app'),
                ('/marketplaces/returns','#of-app'),
                ('/marketplaces/cancellations','#of-app'),
                ('/marketplaces/reviews','#oin-app'),
                ('/marketplaces/status','#oh-app')]
        for path,selector in routes:
            goto(path+'?account_id='+str(aid),selector+':not([v-cloak])')
            assert page.locator('main h1').count()>=1
            assert 'Foreign private title' not in page.locator('main').inner_text()
            layout(path.strip('/').replace('/','-'),widths=(1440,320))
        passed('eleven_full_app_workspaces_new_seller_states')

        goto('/marketplaces/quality?account_id='+str(aid),'#oqw-app:not([v-cloak])');quality_loaded()
        transport['fault']='session';page.get_by_role('button',name='Найти',exact=True).click()
        page.get_by_role('link',name='Войти снова',exact=True).wait_for()
        assert page.locator('.oqw-header button').is_disabled()
        passed('session_expiry_disables_new_writes')
        for target in ('https://outside.example/path','//outside.example/path','/\\outside.example','/%5coutside.example'):
            api=playwright.request.new_context(base_url=BASE)
            try:
                login=api.get('/login',params={'next':target})
                token=re.search(r'name="csrf_token"[^>]*value="([^"]+)"',login.text()).group(1)
                action=html.unescape(re.search(r'<form[^>]*action="([^"]+)"',login.text()).group(1))
                response=api.post(action,form={'username':USERNAME,'password':PASSWORD,'csrf_token':token},max_redirects=0)
                destination=response.headers.get('location','')
                assert response.status==302 and destination.startswith('/') and not destination.startswith('//')
                assert urlsplit(destination).netloc=='' and '\\' not in destination
            finally:
                api.dispose()
        passed('real_csrf_login_rejects_external_and_browser_normalized_redirects')
        with app.app_context():
            assert MarketplaceOperation.query.count()==0
            for row in MarketplaceListing.query.filter_by(account_id=aid):
                assert json.loads(row.price_summary_json)['values']=={'old_price':'1500','price':'1000','marketing_seller_price':'900'}
        assert not REPORT['unexpected_http'] and not REPORT['unexpected_external'], REPORT
        assert REPORT['provider_attempts']==0
        assert sum(r['path']=='/marketplaces/api/quality/refresh' for r in REPORT['mutations'])==1
        REPORT['status']='passed'
        browser.close()
except Exception as error:
    REPORT['status']='failed';REPORT['error']=str(error)[:4000]
    if page and not page.is_closed():
        try:page.screenshot(path=str(OUT/'failure.png'),animations='disabled')
        except Exception:pass
    raise
finally:
    REPORT['checked_at']=datetime.now(timezone.utc).isoformat()
    OUT.mkdir(exist_ok=True)
    (OUT/'browser.json').write_text(json.dumps(REPORT,ensure_ascii=False,indent=2)+'\n')
    server.shutdown();server_thread.join(timeout=5);server.server_close()
    temporary.cleanup()
