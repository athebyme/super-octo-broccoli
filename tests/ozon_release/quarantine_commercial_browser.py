"""Offline Chromium acceptance for held Ozon commercial proposals."""

import hashlib
import json
import logging
import os
from pathlib import Path
import socket
import tempfile
import threading
from datetime import datetime
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
          'postcounts': {}, 'scope': 'synthetic_commercial_quarantine_browser'}
temporary = tempfile.TemporaryDirectory(prefix='ozon-commercial-quarantine-browser-')
os.environ.update(DATABASE_URL='sqlite:///' + str(Path(temporary.name) / 'app.sqlite'),
                  SKIP_SCHEDULER='1', IMAGE_LAB_INLINE_WORKER='0',
                  ENCRYPTION_KEY=Fernet.generate_key().decode(),
                  SECRET_KEY='synthetic-commercial-browser-session-key')


def forbid(*_args, **_kwargs):
    report['provider_attempts'] += 1
    raise AssertionError('Provider I/O forbidden in commercial browser check')


requests.sessions.Session.request = forbid
socket.create_connection = forbid

from seller_platform import app
from models import (db, MarketplaceCommercialProposal as Proposal, MarketplaceListing,
                    MarketplaceListingSnapshot as Snapshot, MarketplaceOperation as Operation,
                    MarketplaceWarehouse as Warehouse, MarketplaceWriteQuarantine as Hold,
                    SellerMarketplaceAccount as Account)
from services.marketplace_commercial import MarketplaceCommercialService as Commercial
from tests.ozon_release.seed import seed, USERNAME, PASSWORD, PHOTO

app.config.update(TESTING=True, WTF_CSRF_ENABLED=True, SESSION_COOKIE_SECURE=False,
                  MARKETPLACE_OZON_ENABLED=True,
                  MARKETPLACE_OZON_COMMERCIAL_WRITES_ENABLED=True)
fixture = seed(app)
account_id = fixture['account_id']
seller_id = fixture['seller_id']


def create_proposal(*, index, kind, amount, warehouse_id=None):
    with app.app_context():
        listing = db.session.get(MarketplaceListing, fixture['listing_ids'][index])
        identity = {'offer_id': listing.offer_id,
                    'product_id': listing.external_product_id}
        if kind == 'price':
            baseline = {**identity, 'kind': 'price', 'price': '1000',
                        'old_price': '1500', 'min_price': '900', 'currency_code': 'RUB'}
            proposed = dict(baseline, price=str(amount))
        else:
            baseline = {**identity, 'kind': 'stock', 'warehouse_id': '7001', 'stock': 8}
            proposed = dict(baseline, stock=amount)
        proposal = Proposal(seller_id=seller_id, marketplace_id=listing.marketplace_id,
            account_id=account_id, listing_id=listing.id, warehouse_id=warehouse_id,
            created_by_user_id=listing.account.seller.user_id,
            proposal_kind=kind, source='user', status='pending_review',
            idempotency_key=f'commercial-browser-{kind}-{index}',
            request_fingerprint=Commercial._fingerprint({'baseline':baseline,'proposed':proposed}),
            contract_version='synthetic-v1',
            baseline_fingerprint=Commercial._fingerprint(baseline),
            proposed_fingerprint=Commercial._fingerprint(proposed),
            baseline_state_json=json.dumps(baseline),
            proposed_state_json=json.dumps(proposed), guardrails_json='{}')
        db.session.add(proposal)
        db.session.commit()
        return proposal.id


def create_hold(*, index, kind):
    """Synthetic operator decision; no live provider outcome is fabricated."""
    with app.app_context():
        account = db.session.get(Account, account_id)
        listing = db.session.get(MarketplaceListing, fixture['listing_ids'][index])
        before = {'offer_id': listing.offer_id,
                  'product_id': listing.external_product_id}
        if kind == 'price':
            before.update(kind='price', price='1000', old_price='1500',
                          min_price='900', currency_code='RUB')
            proposed = dict(before, price='1100')
        else:
            before.update(kind='stock', warehouse_id='7001', stock=8)
            proposed = dict(before, stock=7)
        operation = Operation(seller_id=seller_id, marketplace_id=account.marketplace_id,
            account_id=account_id, listing_id=listing.id,
            operation_kind=f'{kind}_update', status='uncertain', attempt_count=1,
            idempotency_key=f'commercial-browser-hold-origin-{index}',
            request_fingerprint=Commercial._fingerprint(proposed),
            contract_version='synthetic-v1',
            request_summary_json=json.dumps({'offer_id':listing.offer_id,
                                             'before':before,'proposed':proposed}),
            next_poll_at=None)
        db.session.add(operation)
        db.session.flush()
        db.session.add(Snapshot(seller_id=seller_id,
            marketplace_id=account.marketplace_id, account_id=account_id,
            operation_id=operation.id, listing_id=listing.id,
            snapshot_kind=kind, source_fingerprint=listing.sync_fingerprint,
            before_state_json=json.dumps(before),
            submitted_state_json=json.dumps(proposed),
            before_fingerprint=Commercial._fingerprint(before),
            submitted_fingerprint=Commercial._fingerprint(proposed)))
        hold = Hold(seller_id=seller_id, marketplace_id=account.marketplace_id,
            account_id=account_id, operation_id=operation.id,
            scope_kind='product', offer_id=listing.offer_id,
            product_id=listing.external_product_id,
            scope_reason='immutable_target_verified', reviewed_scope_token='a'*64,
            status='active')
        db.session.add(hold)
        db.session.commit()
        return operation.id, hold.id


with app.app_context():
    warehouse = Warehouse(seller_id=seller_id,
        marketplace_id=db.session.get(Account, account_id).marketplace_id,
        account_id=account_id, external_warehouse_id='7001', name='Склад CI',
        status='created', warehouse_type='ORDINARY', flags_json='{}',
        limits_json='{}', is_available=True, sync_fingerprint='b'*64,
        last_seen_at=datetime.utcnow(), last_synced_at=datetime.utcnow())
    db.session.add(warehouse)
    db.session.commit()
    warehouse_id = warehouse.id

held_price = create_proposal(index=0, kind='price', amount='1100')
held_stock = create_proposal(index=1, kind='stock', amount=7,
                             warehouse_id=warehouse_id)
batch_first = create_proposal(index=2, kind='price', amount='1200')
batch_second = create_proposal(index=3, kind='price', amount='1300')
price_origin, price_hold = create_hold(index=0, kind='price')
stock_origin, stock_hold = create_hold(index=1, kind='stock')

logging.getLogger('werkzeug').setLevel(logging.ERROR)
server = make_server('127.0.0.1', 0, app, threaded=True)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
base = 'http://127.0.0.1:' + str(server.server_port)
manifest = json.loads((ASSETS / 'manifest.json').read_text())
for asset in manifest.values():
    assert hashlib.sha256((ASSETS / asset['file']).read_bytes()).hexdigest() == asset['sha256']
expected_http = set()
svg = b'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="120"><rect width="100" height="120" fill="#ded8cd"/></svg>'


def bridge(route):
    request = route.request
    url = urlsplit(request.url)
    if url.hostname == '127.0.0.1' and url.port == server.server_port:
        if request.method not in ('GET', 'HEAD'):
            if url.path not in {'/login', '/marketplaces/commercial/batch-approve'}:
                raise AssertionError(('Unexpected browser mutation', request.method, url.path))
            if url.path != '/login':
                report['postcounts'][url.path] = report['postcounts'].get(url.path, 0) + 1
        response = route.fetch(max_redirects=0)
        if response.status >= 400 and (url.path, response.status) not in expected_http:
            report['unexpected_http'].append({'path':url.path,'status':response.status})
        route.fulfill(response=response)
        return
    if request.url == PHOTO:
        route.fulfill(body=svg, content_type='image/svg+xml')
        return
    asset = manifest.get(request.url) or manifest.get(request.url.rstrip('/'))
    if asset:
        route.fulfill(body=(ASSETS / asset['file']).read_bytes(),
                      content_type=asset['content_type'])
        return
    report['external'].append({'host':url.hostname, 'path':url.path})
    route.abort()


def passed(name):
    assert not report['js_errors'], report['js_errors']
    report['checks'].append(name)
    print(json.dumps({'quarantine_commercial_check': name}), flush=True)


def layout(page, name):
    page.bring_to_front()
    for theme in ('light', 'dark'):
        for width in (320, 390, 768, 1440):
            page.set_viewport_size({'width':width, 'height':1000})
            page.evaluate('(value)=>document.documentElement.dataset.theme=value', theme)
            page.wait_for_timeout(80)
            dimensions = page.evaluate('({scroll:document.documentElement.scrollWidth,inner:innerWidth})')
            assert dimensions['scroll'] <= dimensions['inner'] + 1, (name, theme, width, dimensions)
            assert page.locator('#ozon-commercial-app').is_visible()
            report['layouts'].append({'name':name,'theme':theme,'width':width})
            if width in (390, 1440):
                page.screenshot(path=str(OUT / f'commercial-held-{name}-{theme}-{width}.png'),
                                animations='disabled', full_page=True)
    page.set_viewport_size({'width':1440,'height':1000})
    page.evaluate('document.documentElement.dataset.theme="light"')


def state():
    with app.app_context():
        proposals = {row.id:(row.status,row.operation_id)
                     for row in Proposal.query.filter(Proposal.id.in_(
                         [held_price,held_stock,batch_first,batch_second])).all()}
        return {'proposals':proposals,
                'operation_count':Operation.query.count(),
                'hold_count':Hold.query.count()}


try:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=os.environ.get('OZON_BROWSER_CHROMIUM','/usr/bin/chromium'),
            headless=True, args=['--no-sandbox','--disable-dev-shm-usage'])
        context = browser.new_context(viewport={'width':1440,'height':1000},
                                      service_workers='block')
        context.route('**/*', bridge)
        page = context.new_page()
        page.set_default_timeout(20000)
        page.on('pageerror', lambda error:report['js_errors'].append(str(error)))
        page.goto(base + '/login?next=/marketplaces/commercial/')
        page.locator('[name=username]').fill(USERNAME)
        page.locator('[name=password]').fill(PASSWORD)
        page.get_by_role('button',name='Войти',exact=True).click()

        price_list = f'/marketplaces/commercial/?account_id={account_id}&proposal_kind=price&status=pending_review'
        page.goto(base + price_list)
        page.locator('#ozon-commercial-app .oc-row').first.wait_for()
        held_row = page.locator('.oc-row').filter(has_text='CI-000')
        assert held_row.get_by_text('Новые изменения остановлены',exact=False).is_visible()
        assert held_row.locator('input[type=checkbox]').is_disabled()
        assert held_row.get_by_role('link',name='Разобрать исход').get_attribute('href') == f'/marketplaces/operations/{price_origin}/review'
        layout(page, 'price-list')
        passed('held_price_list_exact_link_checkbox_disabled')

        page.goto(base + f'/marketplaces/commercial/{held_price}')
        page.locator('.oc-decision').wait_for()
        assert page.get_by_text('остановлены решением №',exact=False).is_visible()
        assert page.get_by_role('button',name='Применить в Ozon').count() == 0
        assert page.get_by_role('link',name='Открыть разбор исходной операции').get_attribute('href') == f'/marketplaces/operations/{price_origin}/review'
        layout(page, 'price-detail')
        passed('held_price_detail_no_approve_exact_origin')

        page.goto(base + f'/marketplaces/commercial/{held_stock}')
        page.locator('.oc-decision').wait_for()
        assert page.get_by_text('остановлены решением №',exact=False).is_visible()
        assert page.get_by_role('button',name='Применить в Ozon').count() == 0
        assert page.get_by_role('link',name='Открыть разбор исходной операции').get_attribute('href') == f'/marketplaces/operations/{stock_origin}/review'
        layout(page, 'stock-detail')
        passed('held_stock_detail_no_approve_exact_origin')

        page.goto(base + price_list)
        page.locator('#ozon-commercial-app .oc-row').first.wait_for()
        for offer in ('CI-002','CI-003'):
            page.locator('.oc-row').filter(has_text=offer).locator('input[type=checkbox]').check()
        page.get_by_role('button',name='Проверить выбранные: 2').click()
        dialog = page.get_by_role('dialog',name='Проверка выбранных изменений')
        dialog.wait_for()
        assert dialog.get_by_role('button',name='Применить выбранные в Ozon').is_enabled()
        batch_origin, _batch_hold = create_hold(index=3, kind='price')
        before = state()
        expected_http.add(('/marketplaces/commercial/batch-approve',409))
        dialog.locator('input[name=confirm_batch]').check()
        dialog.get_by_role('button',name='Применить выбранные в Ozon').click()
        dialog.get_by_role('link',name='Разобрать исходную операцию').wait_for()
        assert dialog.get_by_role('link',name='Разобрать исходную операцию').get_attribute('href') == f'/marketplaces/operations/{batch_origin}/review'
        assert dialog.get_by_role('button',name='Применить выбранные в Ozon').is_disabled()
        assert state() == before
        assert report['postcounts'] == {'/marketplaces/commercial/batch-approve':1}
        layout(page, 'batch-conflict')
        passed('frozen_batch_new_hold_one_409_no_write_exact_link')

        assert report['provider_attempts'] == 0
        assert not report['js_errors'], report['js_errors']
        assert not report['unexpected_http'], report['unexpected_http']
        assert not report['external'], report['external']
        report['status'] = 'passed'
        browser.close()
except Exception as error:
    report['status'] = 'failed'
    report['error'] = str(error)[:3000]
    raise
finally:
    server.shutdown()
    temporary.cleanup()
    (OUT / 'quarantine-commercial-browser-report.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2))
