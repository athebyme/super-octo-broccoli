"""Offline real-Chromium acceptance of durable Ozon warehouse/FBS refresh UI."""

import hashlib
import json
import logging
import os
from pathlib import Path
import socket
import tempfile
import threading
from urllib.parse import urlsplit

import requests
from cryptography.fernet import Fernet
from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server


OUT = Path(os.environ.get('OZON_BROWSER_ARTIFACTS', '/artifacts'))
OUT.mkdir(parents=True, exist_ok=True)
ASSETS = Path(__file__).with_name('assets')
report = {'status': 'running', 'checks': [], 'layouts': [], 'js_errors': [],
          'unexpected_http': [], 'external': [], 'provider_attempts': 0,
          'postcounts': {}, 'scope': 'synthetic_warehouse_read_browser'}
temporary = tempfile.TemporaryDirectory(prefix='ozon-warehouse-read-browser-')
os.environ.update(DATABASE_URL='sqlite:///' + str(Path(temporary.name) / 'app.sqlite'),
                  SKIP_SCHEDULER='1', IMAGE_LAB_INLINE_WORKER='0',
                  ENCRYPTION_KEY=Fernet.generate_key().decode(),
                  SECRET_KEY='synthetic-warehouse-browser-session-key')


def forbid(*_args, **_kwargs):
    report['provider_attempts'] += 1
    raise AssertionError('Real provider I/O forbidden in warehouse browser acceptance')


requests.sessions.Session.request = forbid
socket.create_connection = forbid

from seller_platform import app
from models import (db, MarketplaceListing, MarketplaceWarehouseReadJob,
                    MarketplaceWarehouseReadItem, MarketplaceWarehouseStock)
from services import ozon_warehouse_reads as reads
from tests.ozon_release.seed import seed, USERNAME, PASSWORD, PHOTO

app.config.update(TESTING=True, WTF_CSRF_ENABLED=True, SESSION_COOKIE_SECURE=False,
                  MARKETPLACE_OZON_ENABLED=True)
fixture = seed(app)
seller_id = fixture['seller_id']
account_id = fixture['account_id']
listing_id = fixture['listing_ids'][0]


class FakeAdapter:
    def __init__(self, *, stock=False):
        self.stock = stock
        self.calls = []

    def read_warehouses(self, credentials, payload):
        self.calls.append(('warehouses', payload['cursor']))
        return {'warehouses': [{'warehouse_id': 7001, 'name': 'Склад CI FBS',
                                'status': 'created', 'warehouse_type': 'ORDINARY'}],
                'cursor': '', 'has_next': False}

    def read_stocks_by_warehouse_fbs(self, credentials, payload):
        self.calls.append(('fbs_stock', payload['cursor']))
        with app.app_context():
            listing = db.session.get(MarketplaceListing, listing_id)
            offer_id, product_id = listing.offer_id, listing.external_product_id
        return {'products': [{'sku': 800000, 'offer_id': offer_id,
                              'product_id': int(product_id), 'warehouse_id': 7001,
                              'warehouse_name': 'Склад CI FBS',
                              'present': 11, 'reserved': 3, 'free_stock': 8}],
                'cursor': '', 'has_next': False}

    def close(self):
        pass


def advance(kind):
    with app.app_context():
        row = MarketplaceWarehouseReadJob.query.filter_by(
            seller_id=seller_id, kind=kind,
            account_id=account_id,
            listing_id=listing_id if kind == 'fbs_stock' else None,
        ).order_by(MarketplaceWarehouseReadJob.id.desc()).first()
        assert row and row.status == 'queued', (kind, row.status if row else None)
        fake = FakeAdapter(stock=kind == 'fbs_stock')
        result = reads.run_read_step(row.id, adapter_factory=lambda credentials: fake)
        assert result['outcome'] == 'completed', result
        assert len(fake.calls) == 1, fake.calls
        return row.id


logging.getLogger('werkzeug').setLevel(logging.ERROR)
server = make_server('127.0.0.1', 0, app, threaded=True)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
base = 'http://127.0.0.1:' + str(server.server_port)
manifest = json.loads((ASSETS / 'manifest.json').read_text())
for asset in manifest.values():
    assert hashlib.sha256((ASSETS / asset['file']).read_bytes()).hexdigest() == asset['sha256']
svg = b'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="120"><rect width="100" height="120" fill="#ded8cd"/></svg>'
allowed_post = {
    '/marketplaces/commercial/accounts/' + str(account_id) + '/warehouses/sync',
    '/marketplaces/commercial/listings/' + str(listing_id) + '/stocks/refresh',
}
expected_http = set()
lost_post = {'path': None, 'armed': False}
get_counts = {}


def bridge(route):
    request = route.request
    url = urlsplit(request.url)
    if url.hostname == '127.0.0.1' and url.port == server.server_port:
        if request.method == 'GET':
            get_counts[url.path] = get_counts.get(url.path, 0) + 1
        elif request.method != 'HEAD':
            if url.path != '/login' and url.path not in allowed_post:
                raise AssertionError(('Unexpected browser mutation', request.method, url.path))
            if url.path != '/login':
                report['postcounts'][url.path] = report['postcounts'].get(url.path, 0) + 1
        response = route.fetch(max_redirects=0)
        if lost_post['armed'] and request.method == 'POST' and url.path == lost_post['path']:
            lost_post['armed'] = False
            route.abort()
            return
        if response.status >= 400 and (url.path, response.status) not in expected_http:
            report['unexpected_http'].append({'path': url.path, 'status': response.status})
        route.fulfill(response=response)
        return
    if request.url == PHOTO:
        route.fulfill(body=svg, content_type='image/svg+xml')
        return
    asset = manifest.get(request.url) or manifest.get(request.url.rstrip('/'))
    if asset:
        route.fulfill(body=(ASSETS / asset['file']).read_bytes(), content_type=asset['content_type'])
        return
    report['external'].append({'host': url.hostname, 'path': url.path})
    route.abort()


def passed(name):
    assert not report['js_errors'], report['js_errors']
    report['checks'].append(name)
    print(json.dumps({'warehouse_read_check': name}), flush=True)


def layout(page, name, selector):
    for theme in ('light', 'dark'):
        for width in (320, 390, 768, 1440):
            page.set_viewport_size({'width': width, 'height': 1000})
            page.evaluate('(value)=>document.documentElement.dataset.theme=value', theme)
            page.wait_for_timeout(80)
            dims = page.evaluate('({scroll:document.documentElement.scrollWidth,inner:innerWidth})')
            if dims['scroll'] > dims['inner'] + 1:
                offenders = page.evaluate('''() => [...document.querySelectorAll('body *')].map(e => ({tag:e.tagName,cls:typeof e.className === 'string' ? e.className.slice(0,100) : '',text:(e.innerText || '').slice(0,70),right:Math.round(e.getBoundingClientRect().right),width:Math.round(e.getBoundingClientRect().width)})).filter(e => e.right > innerWidth + 1).sort((a,b)=>b.right-a.right).slice(0,12)''')
                print(json.dumps({'overflow':name,'theme':theme,'width':width,'offenders':offenders},ensure_ascii=False),flush=True)
            assert dims['scroll'] <= dims['inner'] + 1, (name, theme, width, dims)
            assert page.locator(selector).is_visible(), (name, theme, width)
            report['layouts'].append({'name': name, 'theme': theme, 'width': width})
            if width in (390, 1440):
                page.screenshot(path=str(OUT / f'warehouse-read-{name}-{theme}-{width}.png'),
                                animations='disabled', full_page=True)
    page.set_viewport_size({'width': 1440, 'height': 1000})
    page.evaluate('document.documentElement.dataset.theme="light"')


try:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=os.environ.get('OZON_BROWSER_CHROMIUM', '/usr/bin/chromium'),
            headless=True, args=['--no-sandbox', '--disable-dev-shm-usage'])
        context = browser.new_context(viewport={'width': 1440, 'height': 1000}, service_workers='block')
        context.route('**/*', bridge)
        page = context.new_page()
        page.set_default_timeout(25000)
        page.on('pageerror', lambda error: report['js_errors'].append(str(error)))
        page.goto(base + '/login?next=/marketplaces/commercial/')
        page.locator('[name=username]').fill(USERNAME)
        page.locator('[name=password]').fill(PASSWORD)
        page.get_by_role('button', name='Войти', exact=True).click()

        list_url = f'/marketplaces/commercial/?account_id={account_id}'
        page.goto(base + list_url)
        page.locator('.oc-warehouses').wait_for()
        page.locator('.oc-warehouses summary').click()
        button = page.locator('.oc-warehouses').get_by_role('button', name='Обновить склады')
        button.click()
        page.locator('.oc-warehouses .oc-refresh-state').get_by_text('очередь', exact=False).wait_for()
        assert button.is_disabled()
        with app.app_context():
            assert MarketplaceWarehouseReadJob.query.count() == 1
            assert MarketplaceWarehouseStock.query.count() == 0
        layout(page, 'warehouse-queued', '.oc-warehouses')
        passed('vue_202_is_pending_and_projection_unchanged')

        advance('warehouses')
        page.reload()
        page.locator('.oc-warehouses summary').click()
        page.locator('.oc-warehouse-list').get_by_text('Склад CI FBS').wait_for()
        page.locator('.oc-warehouses .oc-refresh-state').get_by_text('Полный снимок', exact=False).wait_for()
        layout(page, 'warehouse-completed', '.oc-warehouses')
        passed('vue_reload_get_completed_exact_scope')

        page.goto(base + list_url + f'&listing_id={listing_id}')
        dialog = page.get_by_role('dialog', name='Подготовить изменение')
        dialog.wait_for()
        dialog.get_by_label('Остаток', exact=True).check()
        dialog.get_by_role('button', name='Обновить FBS/rFBS остатки').click()
        dialog.locator('.oc-refresh-state').get_by_text('очередь', exact=False).wait_for()
        with app.app_context():
            assert MarketplaceWarehouseStock.query.count() == 0
        layout(page, 'stock-queued', 'dialog[open]')
        passed('stock_form_202_pending_last_good_not_fabricated')

        advance('fbs_stock')
        page.reload()
        dialog = page.get_by_role('dialog', name='Подготовить изменение')
        dialog.wait_for()
        dialog.get_by_label('Остаток', exact=True).check()
        dialog.get_by_label('Склад FBS/rFBS').select_option(label='Склад CI FBS')
        dialog.locator('.oc-current strong').get_by_text('8 шт.', exact=True).wait_for()
        dialog.locator('.oc-refresh-actions .oc-refresh-state').get_by_text('Полный снимок', exact=False).wait_for()
        layout(page, 'stock-completed', 'dialog[open]')
        passed('stock_form_reload_get_completed_exact_listing')

        # Another tab can replace the browser session while this form retains
        # unsent input and the original bootstrap CSRF token.
        dialog.get_by_label('Новое доступное количество', exact=False).fill('12')
        old_csrf = page.locator('meta[name="csrf-token"]').get_attribute('content')
        expected_http.update({
            (f'/marketplaces/commercial/accounts/{account_id}/warehouses/refresh', 401),
            (f'/marketplaces/commercial/listings/{listing_id}/stocks/refresh', 401),
        })
        context.clear_cookies()
        page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
        dialog.locator('.oc-refresh-actions .oc-alert button').wait_for()
        assert 'Сессия завершена' in dialog.locator('.oc-refresh-actions .oc-alert').inner_text()
        auth_page = context.new_page()
        auth_page.goto(base + '/login')
        auth_page.locator('[name=username]').fill(USERNAME)
        auth_page.locator('[name=password]').fill(PASSWORD)
        auth_page.get_by_role('button', name='Войти', exact=True).click()
        auth_page.close()
        dialog.locator('.oc-refresh-actions .oc-alert button').click()
        page.wait_for_function('(old)=>document.querySelector("meta[name=csrf-token]")?.content !== old', arg=old_csrf)
        assert dialog.get_by_label('Новое доступное количество', exact=False).input_value() == '12'
        stock_post = f'/marketplaces/commercial/listings/{listing_id}/stocks/refresh'
        before_stock_posts = report['postcounts'].get(stock_post, 0)
        dialog.get_by_role('button', name='Обновить FBS/rFBS остатки').click()
        dialog.locator('.oc-refresh-state').get_by_text('очередь', exact=False).wait_for()
        assert report['postcounts'].get(stock_post, 0) == before_stock_posts + 1
        passed('session_relogin_scoped_get_renews_csrf_preserves_stock_input_and_post_succeeds')

        page.goto(base + list_url)
        page.locator('.oc-warehouses summary').click()
        lost_post.update(path=f'/marketplaces/commercial/accounts/{account_id}/warehouses/sync', armed=True)
        before_posts = report['postcounts'].get(lost_post['path'], 0)
        page.locator('.oc-warehouses').get_by_role('button', name='Обновить склады').click()
        page.locator('.oc-warehouses .oc-refresh-state').get_by_text('очередь', exact=False).wait_for()
        assert report['postcounts'].get(lost_post['path'], 0) == before_posts + 1
        assert not lost_post['armed']
        passed('lost_post_recovers_by_get_without_second_post')

        status_path = f'/marketplaces/commercial/accounts/{account_id}/warehouses/refresh'
        page.evaluate("Object.defineProperty(document,'hidden',{configurable:true,value:true});document.dispatchEvent(new Event('visibilitychange'))")
        before_get = get_counts.get(status_path, 0)
        page.wait_for_timeout(16100)
        assert get_counts.get(status_path, 0) == before_get
        page.evaluate("Object.defineProperty(document,'hidden',{configurable:true,value:false});document.dispatchEvent(new Event('visibilitychange'))")
        page.wait_for_timeout(500)
        assert get_counts.get(status_path, 0) >= before_get + 1
        passed('hidden_tab_stops_poll_visible_tab_resumes_get')

        page.goto(base + f'/marketplaces/commercial/classic?account_id={account_id}')
        page.locator('[data-oc-refresh-status]').get_by_text('очередь', exact=False).wait_for()
        layout(page, 'classic-warehouse-pending', '[data-oc-refresh-status]')
        page.goto(base + f'/marketplaces/listings/{listing_id}')
        page.locator('[data-oc-refresh-status]').get_by_text('Полный снимок', exact=False).wait_for()
        passed('classic_warehouse_and_listing_status_are_local_reads')

        foreign = f'/marketplaces/commercial/accounts/{fixture["foreign_account_id"]}/warehouses/refresh'
        expected_http.add((foreign, 404))
        foreign_status = page.evaluate('async path => (await fetch(path,{headers:{Accept:"application/json"}})).status', foreign)
        # The base page adds CSRF to every browser fetch; APIRequestContext
        # shares the cookie but bypasses that wrapper to prove server rejection.
        csrf_probe = context.request.post(base + lost_post['path'], data='{}',
                                          headers={'Accept': 'application/json', 'Content-Type': 'application/json'})
        report['postcounts'][lost_post['path']] += 1
        denial = [foreign_status, csrf_probe.status]
        assert denial == [404, 400], denial
        passed('tenant_denial_and_csrf_reject_without_provider_io')

        assert report['provider_attempts'] == 0
        assert not report['js_errors'], report['js_errors']
        assert not report['unexpected_http'], report['unexpected_http']
        assert not report['external'], report['external']
        report['status'] = 'passed'
        browser.close()
finally:
    (OUT / 'warehouse-read-browser-report.json').write_text(json.dumps(report, indent=2, ensure_ascii=False))
    server.shutdown()
    temporary.cleanup()
