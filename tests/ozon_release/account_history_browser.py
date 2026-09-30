"""Real Flask/CSRF/Vue account settings; offline synthetic accounts only."""
import hashlib
import json
import logging
import os
from pathlib import Path
import socket
import tempfile
import threading
from datetime import datetime, timezone
from urllib.parse import urlsplit

import requests
from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server

OUT = Path('/artifacts'); OUT.mkdir(exist_ok=True)
ASSETS = Path(__file__).with_name('assets')
report = {'status':'running', 'checks':[], 'layouts':[], 'js_errors':[], 'unexpected_http':[],
          'external':[], 'provider_attempts':0, 'posts':0, 'history_gets':0,
          'scope':'synthetic_full_app_account_settings_history'}
temporary = tempfile.TemporaryDirectory(prefix='ozon-account-history-browser-')
os.environ.update(DATABASE_URL='sqlite:///'+str(Path(temporary.name)/'app.sqlite'), SKIP_SCHEDULER='1', IMAGE_LAB_INLINE_WORKER='0')
def forbid(*args, **kwargs):
    report['provider_attempts'] += 1
    raise AssertionError('Provider I/O forbidden')
requests.sessions.Session.request = forbid; socket.create_connection = forbid
from seller_platform import app
from models import db, SellerMarketplaceAccount as Account, MarketplaceAccountEvent, MarketplaceOperation, MarketplaceProductDraft
from services.marketplace_accounts import MarketplaceAccountService as Accounts
from tests.ozon_release.seed import seed, USERNAME, PASSWORD
app.config.update(TESTING=True, WTF_CSRF_ENABLED=True, SESSION_COOKIE_SECURE=False)
fixture = seed(app); aid = fixture['account_id']; path = f'/marketplaces/accounts/{aid}'
with app.app_context():
    account = db.session.get(Account, aid)
    operation = MarketplaceOperation(seller_id=account.seller_id, marketplace_id=account.marketplace_id,
        account_id=aid, operation_kind='product_import', status='uncertain', attempt_count=1,
        idempotency_key='account-history-browser-uncertain', request_fingerprint='a'*64,
        contract_version='ozon-product-import-v3-2026-07-10', request_summary_json='{}', quota_snapshot_json='{}',
        quota_reserved=1, provider_request_ids_json='[]', item_results_json='[]')
    db.session.add(operation); db.session.commit(); operation_id = operation.id

def protected_state():
    with app.app_context():
        account = db.session.get(Account, aid); operation = db.session.get(MarketplaceOperation, operation_id)
        draft = db.session.get(MarketplaceProductDraft, fixture['draft_id'])
        return {'account': [getattr(account, name) for name in ('_credentials_encrypted','credential_version',
                    'capabilities_json','roles_json','connection_status','is_active','is_default')],
                'operation':[operation.status,operation.attempt_count,operation.request_summary_json],
                'draft': {column.name:str(getattr(draft,column.name)) for column in draft.__table__.columns}}

protected = protected_state()
logging.getLogger('werkzeug').setLevel(logging.ERROR)
server = make_server('127.0.0.1', 0, app, threaded=True)
thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
base = 'http://127.0.0.1:'+str(server.server_port)
manifest = json.loads((ASSETS/'manifest.json').read_text())
for asset in manifest.values():
    assert hashlib.sha256((ASSETS/asset['file']).read_bytes()).hexdigest() == asset['sha256']
expected = set(); lose_next_save = False; history_response = None; page = None

def bridge(route):
    global lose_next_save, history_response
    request = route.request; url = urlsplit(request.url)
    if url.hostname == '127.0.0.1' and url.port == server.server_port:
        if request.method not in ('GET','HEAD'):
            assert url.path in ('/login', path, path+'/reconnect', path+'/default', path+'/disconnect')
            if url.path != '/login': report['posts'] += 1
        if url.path.endswith('/history'):
            report['history_gets'] += 1
            if history_response:
                mode = history_response; history_response = None
                if mode == 'abort': route.abort(); return
                if mode == 'foreign':
                    route.fulfill(status=200, content_type='application/json', body=json.dumps({'success':True,
                        'account_id':fixture['foreign_account_id'],'marketplace_code':'ozon','items':[], 'next_before_id':None})); return
                if mode == 'session':
                    route.fulfill(status=401, content_type='application/json', body='{"success":false}'); return
        response = route.fetch(max_redirects=0)
        if response.status >= 400 and (url.path, response.status) not in expected:
            report['unexpected_http'].append({'path':url.path, 'status':response.status})
        if lose_next_save and request.method == 'POST' and url.path == path:
            lose_next_save = False
            assert response.status == 200
            route.abort(); return
        route.fulfill(response=response); return
    asset = manifest.get(request.url) or manifest.get(request.url.rstrip('/'))
    if asset:
        route.fulfill(body=(ASSETS/asset['file']).read_bytes(), content_type=asset['content_type']); return
    report['external'].append({'host':url.hostname,'path':url.path}); route.abort()

def passed(name):
    assert not report['js_errors'], report['js_errors']
    report['checks'].append(name); print(json.dumps({'account_history_browser_check':name}), flush=True)

def loaded(tab):
    tab.locator('#ozon-account-setup:not([v-cloak]) article').first.wait_for()
    tab.wait_for_load_state('networkidle'); tab.evaluate('document.fonts.ready')

def form(tab):
    card = tab.locator(f'#ozon-account-{aid}')
    card.locator('[data-key-settings]').evaluate('(node)=>node.open=true')
    return card.locator('form[data-account-settings]')

def submit(tab, label, vat):
    current = form(tab)
    current.locator('[name=label]').fill(label); current.locator('[name=default_vat]').select_option(vat)
    current.get_by_role('button', name='Сохранить настройки', exact=True).click()

def event_count():
    with app.app_context():
        return MarketplaceAccountEvent.query.filter_by(account_id=aid).count()

def layout(name, target):
    # A second real tab was opened for conflicts. Explicitly activate the tab
    # whose compositor and screenshots we inspect.
    page.bring_to_front()
    for theme in ('light','dark'):
        for width in (320,390,768,1440):
            page.set_viewport_size({'width':width,'height':1000})
            page.evaluate('(theme)=>document.documentElement.dataset.theme=theme', theme)
            target.scroll_into_view_if_needed(); page.wait_for_timeout(100)
            page.wait_for_function("!document.querySelector('.main-content')?.getAnimations().some(a=>a.playState==='running')",timeout=2000)
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'), (name,theme,width)
            report['layouts'].append({'name':name,'theme':theme,'width':width})
            if width in (390,1440):
                page.screenshot(path=str(OUT/f'account-history-{name}-{theme}-{width}.png'), animations='disabled')
    page.set_viewport_size({'width':1440,'height':1000}); page.evaluate('document.documentElement.dataset.theme="light"')

try:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path='/usr/bin/chromium', headless=True,
            args=['--no-sandbox','--disable-dev-shm-usage'])
        context = browser.new_context(viewport={'width':1440,'height':1000}, service_workers='block')
        context.route('**/*',bridge)
        page = context.new_page(); page.set_default_timeout(20000)
        page.on('pageerror', lambda error:report['js_errors'].append(str(error)))
        page.goto(base+'/login?next=/marketplaces/accounts/')
        page.locator('[name=username]').fill(USERNAME); page.locator('[name=password]').fill(PASSWORD)
        page.get_by_role('button',name='Войти',exact=True).click(); loaded(page)
        assert report['history_gets'] == 0
        history = page.locator(f'#ozon-account-{aid} [data-account-history]')
        history.locator('summary').focus(); page.keyboard.press('Enter')
        history.get_by_text('История начнётся со следующего изменения.',exact=True).wait_for()
        assert event_count() == 0 and report['history_gets'] == 1
        passed('lazy_keyboard_history_empty_without_fabricated_events')

        submit(page, 'Магазин для новых карточек', '0.22')
        form(page).get_by_text('Настройки сохранены. Существующие карточки не изменены.',exact=True).wait_for()
        history.get_by_text('Настройки сохранены',exact=True).wait_for()
        assert 'Вы' in history.inner_text() and event_count() == 1
        assert protected_state() == protected
        layout('saved-history', form(page))
        passed('settings_save_with_uncertain_operation_preserves_key_and_existing_draft')

        second = context.new_page(); second.set_default_timeout(20000)
        second.on('pageerror', lambda error:report['js_errors'].append(str(error)))
        second.goto(base+'/marketplaces/accounts/'); loaded(second)
        submit(second, 'Сохранено из второй вкладки', '0.1')
        form(second).get_by_text('Настройки сохранены. Существующие карточки не изменены.',exact=True).wait_for()
        expected.add((path,409))
        submit(page, 'Мой ввод остаётся при конфликте', '0.07')
        current = form(page)
        current.get_by_role('button',name='Показать текущие настройки',exact=True).wait_for()
        viewed = current.locator('[name=expected_version]').input_value(); count = report['posts']
        current.get_by_role('button',name='Показать текущие настройки',exact=True).click()
        current.get_by_role('heading',name='Сейчас сохранено в магазине').wait_for()
        assert current.locator('[name=label]').input_value() == 'Мой ввод остаётся при конфликте'
        assert current.locator('[name=default_vat]').input_value() == '0.07'
        assert current.locator('[name=expected_version]').input_value() == viewed and report['posts'] == count
        assert 'Сохранено из второй вкладки' in current.locator('.ozon-settings-review').inner_text()
        assert current.locator('.ozon-settings-review').evaluate('(node)=>node===document.activeElement')
        layout('review-conflict', current.locator('.ozon-settings-review'))
        current.get_by_role('button',name='Продолжить с моими изменениями',exact=True).click()
        assert report['posts'] == count and current.locator('[name=label]').evaluate('(node)=>node===document.activeElement')
        current.get_by_role('button',name='Сохранить настройки',exact=True).click()
        current.get_by_text('Настройки сохранены. Существующие карточки не изменены.',exact=True).wait_for()
        assert event_count() == 3
        passed('two_real_tabs_explicit_read_compare_rebase_and_separate_save')

        lose_next_save = True
        submit(page, 'Ответ потерялся — изменение сохранено', '')
        current.get_by_role('button',name='Показать текущие настройки',exact=True).wait_for()
        assert event_count() == 4
        count = report['posts']; page.wait_for_timeout(600)
        assert report['posts'] == count
        current.get_by_role('button',name='Показать текущие настройки',exact=True).click()
        current.get_by_role('button',name='Загрузить сохранённые значения',exact=True).click()
        current.get_by_text('В форму загружены сохранённые настройки.',exact=True).wait_for()
        assert current.locator('[name=default_vat]').input_value() == '' and report['posts'] == count
        current.get_by_role('button',name='Сохранить настройки',exact=True).click()
        current.get_by_text('Настройки сохранены. Существующие карточки не изменены.',exact=True).wait_for()
        assert event_count() == 4
        passed('lost_post_response_readback_without_retry_or_duplicate_event')

        card = page.locator(f'#ozon-account-{aid}')
        key_form = card.locator('form[data-key-replacement]')
        key_form.locator('[name=api_key]').fill('synthetic-key-kept-only-in-input')
        key_version = key_form.locator('[name=expected_version]').input_value()
        settings_version = current.locator('[name=expected_version]').input_value()
        expected.add((path+'/reconnect',409))
        key_form.get_by_role('button',name='Заменить ключ и проверить',exact=True).click()
        card.get_by_role('button',name='Перечитать настройки',exact=True).wait_for()
        assert key_form.locator('[name=expected_version]').input_value() == key_version
        card.get_by_role('button',name='Перечитать настройки',exact=True).click()
        card.get_by_text('Статус перечитан.',exact=False).wait_for()
        assert current.locator('[name=expected_version]').input_value() == settings_version
        assert key_form.locator('[name=api_key]').input_value() == 'synthetic-key-kept-only-in-input'
        assert protected_state() == protected and event_count() == 4
        passed('key_review_independent_of_settings_no_secret_saved_or_operation_replayed')

        csrf = page.locator('meta[name=csrf-token]').get_attribute('content')
        page.locator('meta[name=csrf-token]').evaluate('(node)=>node.content="invalid"')
        current.locator('[name=csrf_token]').evaluate('(node)=>node.value="invalid"')
        expected.add((path,400))
        submit(page, 'CSRF должен отклонить', '0.2')
        current.locator('[role=alert]').wait_for()
        assert event_count() == 4
        page.locator('meta[name=csrf-token]').evaluate('(node,value)=>node.content=value',csrf)
        current.locator('[name=csrf_token]').evaluate('(node,value)=>node.value=value',csrf)
        passed('real_settings_csrf_failure_preserves_saved_state')

        # Append synthetic service events to exercise the 30+1 keyset boundary.
        with app.app_context():
            for number in range(30):
                account = db.session.get(Account,aid)
                Accounts.save_settings(seller_id=fixture['seller_id'], account_id=aid,
                    external_account_id=account.external_account_id, label=f'Сохранённая история {number}',
                    expected_version=account.version, default_vat=None)
        page.reload(); loaded(page)
        history = page.locator(f'#ozon-account-{aid} [data-account-history]')
        history.locator('summary').click()
        history.get_by_role('button',name='Показать более ранние изменения',exact=True).wait_for()
        assert history.locator('.ozon-history-list > li').count() == 30
        history.get_by_role('button',name='Показать более ранние изменения',exact=True).click()
        page.wait_for_function('(id)=>document.querySelectorAll("#ozon-account-"+id+" .ozon-history-list > li").length===34',arg=aid)
        assert history.get_by_role('button',name='Показать более ранние изменения',exact=True).count() == 0
        assert event_count() == 34
        layout('history-pages', history.locator('.ozon-history-list > li').first)
        cdp = context.new_cdp_session(page)
        cdp.send('Emulation.setDeviceMetricsOverride',{'width':720,'height':500,'deviceScaleFactor':2,
            'mobile':False,'screenWidth':1440,'screenHeight':1000})
        page.wait_for_timeout(300)
        assert page.evaluate('innerWidth===720 && devicePixelRatio===2 && document.documentElement.scrollWidth<=innerWidth+1')
        report['layouts'].append({'name':'history','css_width':720,'screen_width':1440,'emulated_zoom_percent':200})
        cdp.send('Emulation.clearDeviceMetricsOverride'); cdp.detach()
        passed('bounded_history_keyset_no_duplicates_responsive_and_200_percent')

        for mode in ('abort','foreign','session'):
            page.reload(); loaded(page); history_response = mode
            history = page.locator(f'#ozon-account-{aid} [data-account-history]'); history.locator('summary').click()
            history.locator('[role=alert]').wait_for()
            assert history.locator('.ozon-history-list > li').count() == 0
            assert event_count() == 34
            if mode != 'session':
                history.get_by_role('button',name='Повторить загрузку истории',exact=True).click()
                history.get_by_role('button',name='Показать более ранние изменения',exact=True).wait_for()
            else:
                assert history.get_by_role('button',name='Повторить загрузку истории',exact=True).is_disabled()
        assert protected_state() == protected
        passed('history_failure_foreign_response_retry_and_expired_session_gates')
        assert not report['unexpected_http'] and not report['external'] and report['provider_attempts'] == 0, report
        report['status'] = 'passed'; browser.close()
except Exception as error:
    report['status'] = 'failed'; report['error'] = str(error)[:3000]
    if page and not page.is_closed():
        try:
            page.screenshot(path=str(OUT/'account-history-failure.png'), animations='disabled')
        except Exception:
            pass
    raise
finally:
    report['checked_at'] = datetime.now(timezone.utc).isoformat()
    (OUT/'account-history-browser.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    server.shutdown(); thread.join(timeout=5); server.server_close(); temporary.cleanup()
