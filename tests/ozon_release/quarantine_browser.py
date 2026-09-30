"""Offline real Flask/CSRF/Vue review of synthetic Ozon quarantine decisions."""
import hashlib
import json
import logging
import os
from pathlib import Path
import socket
import tempfile
import threading
from datetime import datetime, timedelta
from urllib.parse import urlsplit

import requests
from cryptography.fernet import Fernet
from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server

OUT = Path(os.environ.get('OZON_BROWSER_ARTIFACTS', '/artifacts')); OUT.mkdir(parents=True, exist_ok=True)
ASSETS = Path(__file__).with_name('assets')
report = {'status':'running', 'checks':[], 'layouts':[], 'js_errors':[], 'unexpected_http':[],
          'external':[], 'provider_attempts':0, 'posts':0, 'post_paths':{},
          'scope':'synthetic_full_app_quarantine_review'}
temporary = tempfile.TemporaryDirectory(prefix='ozon-quarantine-browser-')
os.environ.update(DATABASE_URL='sqlite:///'+str(Path(temporary.name)/'app.sqlite'),
                  SKIP_SCHEDULER='1', IMAGE_LAB_INLINE_WORKER='0',
                  ENCRYPTION_KEY=Fernet.generate_key().decode(), SECRET_KEY='synthetic-browser-session-key')
def forbid(*args, **kwargs):
    report['provider_attempts'] += 1
    raise AssertionError('Provider I/O forbidden')
requests.sessions.Session.request = forbid
socket.create_connection = forbid

from seller_platform import app
from models import db, MarketplaceOperation, MarketplaceListingSnapshot, MarketplaceWriteQuarantine as Hold, MarketplaceWriteQuarantineEvent as Event, MarketplaceProductDraft, SellerMarketplaceAccount as Account
from services.marketplace_commercial import MarketplaceCommercialService as Commercial
from services import ozon_write_quarantine as Quarantine
from tests.ozon_release.seed import seed, USERNAME, PASSWORD

app.config.update(TESTING=True, WTF_CSRF_ENABLED=True, SESSION_COOKIE_SECURE=False)
fixture = seed(app)
aid = fixture['account_id']

def create_operation(*, offer, product, wide=False, seller_id=None, account_id=None):
    with app.app_context():
        account = db.session.get(Account, account_id or aid)
        before = {'kind':'price','offer_id':offer,'product_id':product,'price':'1000',
                  'old_price':'1500','min_price':'900','currency_code':'RUB'}
        proposed = dict(before, price='1100')
        summary = {'offer_id':offer,'before':before,'proposed':proposed}
        operation = MarketplaceOperation(seller_id=seller_id or account.seller_id,
            marketplace_id=account.marketplace_id, account_id=account.id, operation_kind='price_update',
            status='uncertain', attempt_count=1, idempotency_key='browser-'+offer,
            request_fingerprint='a'*64, contract_version='synthetic',
            request_summary_json=json.dumps(summary), quota_snapshot_json='{}',
            provider_request_ids_json='[]', item_results_json='[]', next_poll_at=datetime.utcnow(),
            version=1)
        db.session.add(operation); db.session.flush()
        db.session.add(MarketplaceListingSnapshot(operation_id=operation.id,
            seller_id=account.seller_id, marketplace_id=account.marketplace_id, account_id=account.id,
            snapshot_kind='price', source_fingerprint='a'*64,
            before_state_json='{}' if wide else json.dumps(before),
            submitted_state_json=json.dumps(proposed),
            submitted_fingerprint=Commercial._fingerprint(proposed)))
        db.session.commit()
        return operation.id

product_id = create_operation(offer='CI-000', product='900000')
wide_id = create_operation(offer='CI-UNKNOWN', product='902222', wide=True)
foreign_id = create_operation(offer='FOREIGN-SECRET-OFFER', product='999999',
    account_id=fixture['foreign_account_id'])

def state(operation_id):
    with app.app_context():
        row = db.session.get(MarketplaceOperation, operation_id)
        hold = Hold.query.filter_by(operation_id=operation_id).first()
        return {'status':row.status,'attempts':row.attempt_count,'version':row.version,
                'hold':None if hold is None else (hold.status,hold.version),
                'events':0 if hold is None else Event.query.filter_by(quarantine_id=hold.id).count()}

logging.getLogger('werkzeug').setLevel(logging.ERROR)
server = make_server('127.0.0.1', 0, app, threaded=True)
thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
base = 'http://127.0.0.1:'+str(server.server_port)
manifest = json.loads((ASSETS/'manifest.json').read_text())
for asset in manifest.values():
    assert hashlib.sha256((ASSETS/asset['file']).read_bytes()).hexdigest() == asset['sha256']
expected = set(); lose_next = False; preview_mode = None

def bridge(route):
    global lose_next, preview_mode
    request = route.request; url = urlsplit(request.url)
    if url.hostname == '127.0.0.1' and url.port == server.server_port:
        if request.method not in ('GET','HEAD'):
            assert url.path == '/login' or url.path.startswith('/marketplaces/operations/api/')
            if url.path != '/login':
                report['posts'] += 1
                report['post_paths'][url.path] = report['post_paths'].get(url.path,0) + 1
        if preview_mode and '/api/' in url.path and url.path.endswith('/review') and request.method == 'GET':
            mode = preview_mode; preview_mode = None
            if mode == 'session':
                route.fulfill(status=401, content_type='application/json', body='{"success":false}'); return
            if mode == 'timeout': route.abort(); return
        response = route.fetch(max_redirects=0)
        if response.status >= 400 and (url.path,response.status) not in expected:
            report['unexpected_http'].append({'path':url.path,'status':response.status})
        if lose_next and request.method == 'POST' and '/quarantine' in url.path:
            lose_next = False
            assert response.status == 200
            route.abort(); return
        route.fulfill(response=response); return
    asset = manifest.get(request.url) or manifest.get(request.url.rstrip('/'))
    if asset:
        route.fulfill(body=(ASSETS/asset['file']).read_bytes(), content_type=asset['content_type']); return
    report['external'].append({'host':url.hostname,'path':url.path}); route.abort()

def passed(name):
    assert not report['js_errors'], report['js_errors']
    report['checks'].append(name); print(json.dumps({'quarantine_browser_check':name}), flush=True)

def loaded(tab):
    tab.locator('#oq-app .oq-hero').wait_for()
    tab.wait_for_load_state('networkidle')
    tab.evaluate('document.fonts.ready')

def layout(tab, name):
    tab.bring_to_front()
    for theme in ('light','dark'):
        for width in (320,390,768,1440):
            tab.set_viewport_size({'width':width,'height':1000})
            tab.evaluate('(value)=>document.documentElement.dataset.theme=value', theme)
            tab.wait_for_timeout(80)
            assert tab.evaluate('document.documentElement.scrollWidth <= innerWidth + 1'), (name,theme,width)
            assert tab.locator('#oq-app').is_visible()
            report['layouts'].append({'name':name,'theme':theme,'width':width})
            if width in (390,1440):
                tab.screenshot(path=str(OUT/f'quarantine-{name}-{theme}-{width}.png'), animations='disabled')
    tab.set_viewport_size({'width':1440,'height':1000})
    tab.evaluate('document.documentElement.dataset.theme="light"')

def reason_and_confirm(tab, reason='Проверили неизвестный результат и останавливаем новые записи.'):
    tab.locator('.oq-form textarea').fill(reason)
    tab.locator('.oq-form input[type=checkbox]').check()

try:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=os.environ.get('OZON_BROWSER_CHROMIUM','/usr/bin/chromium'), headless=True,
            args=['--no-sandbox','--disable-dev-shm-usage'])
        context = browser.new_context(viewport={'width':1440,'height':1000}, service_workers='block')
        context.route('**/*', bridge)
        page = context.new_page(); page.set_default_timeout(20000)
        page.on('pageerror',lambda error:report['js_errors'].append(str(error)))
        page.goto(base+'/login?next=/marketplaces/operations/'+str(product_id)+'/review')
        page.locator('[name=username]').fill(USERNAME)
        page.locator('[name=password]').fill(PASSWORD)
        page.get_by_role('button',name='Войти',exact=True).click(); loaded(page)
        own_place = f'/marketplaces/operations/api/{product_id}/quarantine'
        foreign_review = f'/marketplaces/operations/api/{foreign_id}/review'
        foreign_place = f'/marketplaces/operations/api/{foreign_id}/quarantine'
        expected.update({(own_place,400),(foreign_review,404),(foreign_place,404)})
        # APIRequestContext shares the browser session, but bypasses the base
        # page's global fetch wrapper that auto-injects CSRF into every POST.
        missing_csrf = context.request.post(base+own_place, data='{}', headers={'Content-Type':'application/json'})
        assert missing_csrf.status == 400 and 'csrf' in missing_csrf.text().lower(), missing_csrf.text()
        foreign = page.evaluate("""async paths => {const csrf=document.querySelector('meta[name="csrf-token"]').content;const get=await fetch(paths[0],{credentials:'same-origin'});const post=await fetch(paths[1],{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','X-CSRFToken':csrf},body:JSON.stringify({expected_version:1,scope_token:'a'.repeat(64),reason:'Проверяем чужую область без записи.',confirm_scope:true})});return {get:get.status,getText:await get.text(),post:post.status,postText:await post.text()}}""",[foreign_review,foreign_place])
        assert foreign['get'] == 404 and foreign['post'] == 404
        assert 'FOREIGN-SECRET-OFFER' not in foreign['getText']+foreign['postText']
        assert state(product_id)['events'] == 0
        passed('csrf_and_foreign_account_denied_without_leak')
        assert page.get_by_text('CI-000',exact=True).is_visible()
        assert page.get_by_text('Результат неизвестен',exact=True).first.is_visible()
        assert 'FOREIGN-SECRET-OFFER' not in page.content()
        layout(page,'product-preview')
        page.locator('#oq-reason').focus()
        assert page.evaluate('document.activeElement.id') == 'oq-reason'
        assert page.locator('#oq-reason').evaluate('(node)=>getComputedStyle(node).outlineStyle') != 'none'
        # A 320 CSS px viewport is the effective layout width of a 640px
        # viewport at 200% browser zoom; CSS zoom keeps breakpoints unchanged.
        page.set_viewport_size({'width':320,'height':1000})
        page.wait_for_timeout(100)
        dimensions = page.evaluate('({scroll:document.documentElement.scrollWidth,inner:innerWidth,body:document.body.scrollWidth,offenders:[...document.querySelectorAll("body *")].filter(e=>e.getBoundingClientRect().right>innerWidth+1).slice(0,8).map(e=>[e.tagName,e.className,e.getBoundingClientRect().right])})')
        assert dimensions['scroll'] <= dimensions['inner'] + 1, dimensions
        page.set_viewport_size({'width':1440,'height':1000})
        passed('product_preview_keyboard_200_percent_reflow')

        reason_and_confirm(page)
        lose_next = True
        page.get_by_role('button',name='Остановить новые изменения').click()
        page.get_by_text('Ответ о сохранении не получен или состояние изменилось.').wait_for()
        assert page.locator('#oq-reason').input_value().startswith('Проверили')
        assert state(product_id)['events'] == 1
        assert page.get_by_role('button',name='Остановить новые изменения').is_disabled()
        page.get_by_role('button',name='Проверить текущее состояние').click()
        page.get_by_role('button',name='Использовать просмотренное состояние').wait_for()
        assert page.locator('#oq-reason').input_value().startswith('Проверили')
        assert page.get_by_role('button',name='Остановить новые изменения').is_disabled()
        page.get_by_role('button',name='Использовать просмотренное состояние').click()
        page.get_by_text('Новые изменения остановлены').wait_for()
        assert state(product_id)['events'] == 1
        assert page.get_by_text('Результат неизвестен',exact=True).first.is_visible()
        layout(page,'product-held')
        passed('lost_post_readback_single_decision_unknown_preserved')

        with app.app_context():
            draft = db.session.get(MarketplaceProductDraft, fixture['draft_id'])
            draft.offer_id = 'CI-000'; draft.published_listing_id = fixture['listing_ids'][0]
            db.session.commit()
        draft_tab = context.new_page()
        draft_tab.on('pageerror',lambda error:report['js_errors'].append(str(error)))
        draft_tab.goto(base+f'/marketplaces/drafts/{fixture["draft_id"]}')
        draft_tab.get_by_role('link',name='Открыть решение №',exact=False).wait_for()
        assert draft_tab.get_by_text('Новые изменения этого товара остановлены.',exact=False).is_visible()
        draft_tab.get_by_role('link',name='Открыть решение №',exact=False).click()
        assert draft_tab.url.endswith(f'/marketplaces/operations/{product_id}/review')
        draft_tab.close(); page.bring_to_front()
        passed('held_draft_links_to_exact_origin_without_write')

        page.locator('#oq-note').fill('Добавили сведения после ручной сверки операции.')
        page.get_by_role('button',name='Сохранить запись').click()
        page.get_by_text('Добавили сведения после ручной сверки операции.').wait_for()
        assert state(product_id)['events'] == 2
        assert page.get_by_text('Снять остановку',exact=True).count() == 0
        passed('note_journal_and_unproven_release_hidden')

        with app.app_context():
            current = Quarantine.preview(seller_id=fixture['seller_id'], origin_id=product_id,
                viewer_user_id=db.session.get(Account, aid).seller.user_id)
            Quarantine.update_decision(seller_id=fixture['seller_id'], origin_id=product_id,
                expected_version=current['hold']['version'], expected_operation_version=current['operation_version'],
                action='note_added', reason='Другая просмотренная запись оператора.',
                actor_user_id=db.session.get(Account, aid).seller.user_id)
        page.locator('#oq-note').fill('Моё пояснение после обновления другим окном.')
        expected.add((f'/marketplaces/operations/api/{product_id}/quarantine/decision',409))
        page.get_by_role('button',name='Сохранить запись').click()
        page.get_by_text('Ответ о сохранении не получен или состояние изменилось.').wait_for()
        assert page.locator('#oq-note').input_value().startswith('Моё пояснение')
        page.get_by_role('button',name='Проверить текущее состояние').click()
        page.get_by_role('button',name='Использовать просмотренное состояние').wait_for()
        assert page.get_by_role('button',name='Сохранить запись').is_disabled()
        page.get_by_role('button',name='Использовать просмотренное состояние').click()
        assert page.locator('#oq-note').input_value().startswith('Моё пояснение')
        page.get_by_role('button',name='Сохранить запись').click()
        page.get_by_text('Моё пояснение после обновления другим окном.').wait_for()
        assert state(product_id)['events'] == 4
        passed('stale_decision_needs_separate_review_choice')

        with app.app_context():
            op = db.session.get(MarketplaceOperation, product_id)
            proposed = json.loads(op.snapshot.submitted_state_json)
            op.status = 'succeeded'; op.completed_at = datetime.utcnow()+timedelta(seconds=2)
            op.next_poll_at = None; op.error_code = None
            op.snapshot.confirmed_state_json = json.dumps(proposed)
            op.snapshot.confirmed_fingerprint = Commercial._fingerprint(proposed)
            db.session.commit()
        page.get_by_role('link',name='История операции').first.focus()
        # A reviewed revision changes only after the seller explicitly reads it.
        assert page.get_by_text('Снять остановку',exact=True).count() == 0
        page.reload(); loaded(page)
        page.get_by_role('button',name='Снять остановку',exact=True).click()
        reason_and_confirm(page,'Результат исходной операции подтверждён штатной сверкой.')
        page.get_by_role('button',name='Снять остановку',exact=True).last.click()
        page.get_by_text('Остановка снята',exact=True).first.wait_for()
        assert state(product_id)['hold'][0] == 'released'
        layout(page,'product-released')
        passed('release_requires_read_proven_original_outcome')

        page.goto(base+f'/marketplaces/operations/{wide_id}/review'); loaded(page)
        assert page.get_by_text('Остановка охватит весь выбранный магазин.').is_visible()
        assert page.get_by_text('Новые изменения в Ozon для этого магазина',exact=False).is_visible()
        layout(page,'account-preview')
        reason_and_confirm(page,'Цель в снимке неизвестна; останавливаем весь магазин.')
        page.get_by_role('button',name='Остановить новые изменения').click()
        page.get_by_text('Новые изменения остановлены').wait_for()
        with app.app_context():
            assert Hold.query.filter_by(operation_id=wide_id).one().scope_kind == 'account'
        layout(page,'account-held')
        passed('explicit_account_wide_scope_and_hold')

        with app.app_context():
            actor_id = db.session.get(Account, aid).seller.user_id
            for index in range(31):
                current = Quarantine.preview(seller_id=fixture['seller_id'], origin_id=wide_id,
                    viewer_user_id=actor_id)
                Quarantine.update_decision(seller_id=fixture['seller_id'], origin_id=wide_id,
                    expected_version=current['hold']['version'], expected_operation_version=current['operation_version'],
                    action='note_added', reason=f'Проверка без новой записи номер {index+1}.', actor_user_id=actor_id)
        page.reload(); loaded(page)
        assert page.locator('.oq-journal li').count() == 30
        page.get_by_role('button',name='Показать ранние записи').click()
        page.get_by_text('Цель в снимке неизвестна; останавливаем весь магазин.').wait_for()
        assert page.locator('.oq-journal li').count() == 32
        passed('bounded_journal_pagination')

        preview_mode='session'
        page.reload()
        page.get_by_text('Сессия завершилась.',exact=False).wait_for()
        assert page.get_by_role('link',name='Войти снова').is_visible()
        passed('session_expiry_clear_action')
        assert not report['external'], report['external']
        assert not report['unexpected_http'], report['unexpected_http']
        assert report['provider_attempts'] == 0
        assert report['post_paths'] == {own_place:1, foreign_place:1,
            f'/marketplaces/operations/api/{product_id}/quarantine/decision':4,
            f'/marketplaces/operations/api/{wide_id}/quarantine':1}, report['post_paths']
        assert report['posts'] == 7
        report['status']='passed'; browser.close()
except Exception as error:
    report['status']='failed'; report['error']=str(error)[:3000]
    raise
finally:
    server.shutdown(); temporary.cleanup()
    (OUT/'quarantine-browser-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
