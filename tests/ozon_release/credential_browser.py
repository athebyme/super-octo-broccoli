"""Expiry -> scoped notification -> Vue recovery -> reviewed key rotation.

Real local HTTP, auth and CSRF; synthetic SQLite only, network-none container.
"""
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
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
from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server

OUT=Path('/artifacts');OUT.mkdir(exist_ok=True)
ASSETS=Path(__file__).with_name('assets')
report={'status':'running','checks':[],'layouts':[],'js_errors':[],'unexpected_http':[],
        'external':[],'provider_attempts':0,'posts':0,'scope':'synthetic_full_app_credential_recovery'}
temporary=tempfile.TemporaryDirectory(prefix='ozon-credential-browser-')
os.environ['DATABASE_URL']='sqlite:///'+str(Path(temporary.name)/'app.sqlite')
os.environ['SKIP_SCHEDULER']='1';os.environ['IMAGE_LAB_INLINE_WORKER']='0'
def forbid(*a,**kw):
    report['provider_attempts']+=1
    raise AssertionError('Provider I/O forbidden')
requests.sessions.Session.request=forbid;socket.create_connection=forbid
from seller_platform import app
from models import db,SellerMarketplaceAccount as Account,Notification,MarketplaceOperation,MarketplaceCredentialNotice
from services.ozon_credential_notices import notify_due_credentials
from services.marketplace_accounts import MarketplaceAccountService
from tests.ozon_release.seed import seed,USERNAME,PASSWORD
app.config.update(TESTING=True,WTF_CSRF_ENABLED=True,SESSION_COOKIE_SECURE=False)
fixture=seed(app);now=datetime.utcnow()
with app.app_context():
    original=db.session.get(Account,fixture['account_id'])
    ids={'unknown':original.id}
    for state,delta in [('scheduled',60),('fourteen',10),('seven',5),('day',0.5),('expired',-1)]:
        row=Account(seller_id=fixture['seller_id'],marketplace_id=original.marketplace_id,
            external_account_id=str(100010+len(ids)),label='Магазин с длинным русским названием — '+state,
            is_active=True,connection_status='connected',credential_expires_at=now+timedelta(days=delta))
        row.set_credentials({'api_key':'synthetic-key-not-valid-at-provider'});db.session.add(row);db.session.flush();ids[state]=row.id
    foreign=db.session.get(Account,fixture['foreign_account_id']);foreign.credential_expires_at=now+timedelta(hours=12)
    operation=MarketplaceOperation(seller_id=fixture['seller_id'],marketplace_id=original.marketplace_id,
        account_id=ids['expired'],operation_kind='product_import',status='uncertain',attempt_count=1,
        idempotency_key='credential-browser-uncertain',request_fingerprint='a'*64,
        contract_version='ozon-product-import-v3-2026-07-10',request_summary_json='{}',quota_snapshot_json='{}',
        quota_reserved=1,provider_request_ids_json='[]',item_results_json='[]')
    db.session.add(operation);db.session.commit();operation_id=operation.id
    assert notify_due_credentials(now=now)==5  # Four own, one foreign.
    assert notify_due_credentials(now=now)==0
logging.getLogger('werkzeug').setLevel(logging.ERROR)
server=make_server('127.0.0.1',0,app,threaded=True)
thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
base='http://127.0.0.1:'+str(server.server_port)
manifest=json.loads((ASSETS/'manifest.json').read_text())
for entry in manifest.values():assert hashlib.sha256((ASSETS/entry['file']).read_bytes()).hexdigest()==entry['sha256']
page=None;expected=set()

def bridge(route):
    request=route.request;url=urlsplit(request.url)
    if url.hostname=='127.0.0.1' and url.port==server.server_port:
        if request.method not in ('GET','HEAD'):
            assert url.path=='/login' or url.path==f'/marketplaces/accounts/{ids["expired"]}/reconnect'
            if url.path!='/login':report['posts']+=1
        response=route.fetch(max_redirects=0)
        if response.status>=400 and (url.path,response.status) not in expected:
            report['unexpected_http'].append({'path':url.path,'status':response.status})
        route.fulfill(response=response);return
    asset=manifest.get(request.url) or manifest.get(request.url.rstrip('/'))
    if asset:route.fulfill(body=(ASSETS/asset['file']).read_bytes(),content_type=asset['content_type']);return
    report['external'].append({'host':url.hostname,'path':url.path});route.abort()


def passed(name):
    assert not report['js_errors'],report['js_errors']
    report['checks'].append(name);print(json.dumps({'credential_browser_check':name}),flush=True)


def loaded():
    page.locator('#ozon-account-setup:not([v-cloak]) article').first.wait_for()
    page.wait_for_load_state('networkidle');page.evaluate('document.fonts.ready')


def layout(name):
    for theme in ('light','dark'):
        for width in (320,390,768,1440):
            page.set_viewport_size({'width':width,'height':1000})
            page.evaluate('(theme)=>document.documentElement.dataset.theme=theme',theme)
            page.wait_for_timeout(80)
            page.wait_for_function("!document.querySelector('.main-content')?.getAnimations().some(a=>a.playState==='running')",timeout=2000)
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'),(name,theme,width)
            assert page.locator('.ozon-key-expiry[data-state="expiring"],.ozon-key-expiry[data-state="expired"]').evaluate_all("""nodes=>{
                const luminance=rgb=>rgb.match(/[\d.]+/g).slice(0,3).map(Number).map(v=>v/255).map(v=>v<=.04045?v/12.92:((v+.055)/1.055)**2.4).reduce((s,v,i)=>s+v*[.2126,.7152,.0722][i],0);
                return nodes.every(n=>{const fg=luminance(getComputedStyle(n.querySelector('strong')).color),bg=luminance(getComputedStyle(n).backgroundColor);return (Math.max(fg,bg)+.05)/(Math.min(fg,bg)+.05)>=4.5;});
            }"""), (name,theme,width,'warning contrast')
            report['layouts'].append({'name':name,'theme':theme,'width':width})
            if width in (390,1440):page.screenshot(path=str(OUT/f'credential-{name}-{theme}-{width}.png'),animations='disabled')
    page.set_viewport_size({'width':1440,'height':1000});page.evaluate('document.documentElement.dataset.theme="light"')


@contextmanager
def evidence():
    try:
        yield
    except Exception:
        if page and not page.is_closed():
            try:
                page.screenshot(path=str(OUT/'credential-failure.png'),animations='disabled')
                (OUT/'credential-failure-context.json').write_text(json.dumps({'url':page.url,'text':page.locator('body').inner_text()[:6000],
                    'metrics':page.evaluate('({inner:innerWidth,scroll:document.documentElement.scrollWidth,dpr:devicePixelRatio})')},ensure_ascii=False))
            except Exception:
                pass
        raise


try:
    with sync_playwright() as playwright,evidence():
        browser=playwright.chromium.launch(executable_path='/usr/bin/chromium',headless=True,args=['--no-sandbox','--disable-dev-shm-usage'])
        context=browser.new_context(viewport={'width':1440,'height':1000},service_workers='block')
        context.route('**/*',bridge)
        page=context.new_page();page.set_default_timeout(20000)
        page.on('pageerror',lambda e:report['js_errors'].append(str(e)))
        page.goto(base+'/login?next=/marketplaces/accounts/')
        page.locator('[name=username]').fill(USERNAME);page.locator('[name=password]').fill(PASSWORD)
        page.get_by_role('button',name='Войти',exact=True).click();loaded()
        assert page.locator('#ozon-account-setup article').count()==6
        for state,expected_state in [('unknown','unknown'),('scheduled','scheduled'),('fourteen','expiring'),('seven','expiring'),('day','expiring'),('expired','expired')]:
            assert page.locator(f'#ozon-account-{ids[state]} .ozon-key-expiry').get_attribute('data-state')==expected_state
        assert page.locator('#ozon-account-setup article .ozon-key-expiry button').count()==4
        layout('expiry-states');passed('unknown_future_and_three_warning_windows_expired')
        expired=page.locator(f'#ozon-account-{ids["expired"]}')
        expired.scroll_into_view_if_needed()
        button=expired.get_by_role('button',name='Заменить ключ',exact=True);button.focus();page.keyboard.press('Enter')
        key=expired.locator('form[data-key-replacement] input[name=api_key]')
        assert key.evaluate('(node)=>node===document.activeElement')
        assert expired.locator('[data-key-settings]').get_attribute('open') is not None
        assert 'action=replace-key' in page.url
        assert 'неподтверждённом результате' in expired.inner_text()
        layout('key-form');passed('keyboard_cta_focus_exact_form_uncertain_explanation')
        # Emulate the CSS viewport and device scale of a 1440px screen at 200%.
        # Root CSS zoom is NOT browser zoom: it retains different media-query bounds.
        cdp=context.new_cdp_session(page)
        cdp.send('Emulation.setDeviceMetricsOverride', {'width':720,'height':500,'deviceScaleFactor':2,
            'mobile':False,'screenWidth':1440,'screenHeight':1000})
        page.wait_for_function("!document.querySelector('.main-content')?.getAnimations().some(a=>a.playState==='running')",timeout=2000)
        assert page.evaluate('innerWidth===720 && devicePixelRatio===2 && document.documentElement.scrollWidth<=innerWidth+1')
        report['layouts'].append({'name':'key-form','css_width':720,'screen_width':1440,'emulated_zoom_percent':200,'device_scale_factor':2})
        page.screenshot(path=str(OUT/'credential-key-form-200-percent.png'),animations='disabled')
        passed('two_hundred_percent_reflow_without_horizontal_scroll')
        cdp.send('Emulation.clearDeviceMetricsOverride');cdp.detach()
        page.set_viewport_size({'width':1440,'height':1000})
        notices=context.request.get(base+'/api/notifications').json()['items']
        assert len(notices)==4 and {n['metadata']['account_id'] for n in notices}=={ids[k] for k in ('fourteen','seven','day','expired')}
        link=next(n['link'] for n in notices if n['metadata']['account_id']==ids['expired'])
        # The earlier CTA already replaced the current URL with this link.
        # Leave that document to exercise an actual notification landing,
        # not same-document fragment navigation retaining the previous focus.
        page.goto(base+'/marketplaces/accounts/');loaded()
        page.goto(base+link);loaded();expired=page.locator(f'#ozon-account-{ids["expired"]}')
        assert expired.locator('[data-key-settings]').get_attribute('open') is not None
        assert expired.locator('[data-key-settings] summary').evaluate('(node)=>node===document.activeElement')
        assert expired.locator('input[name=api_key]').input_value()==''
        passed('seller_notification_scope_and_deep_link_without_secret')
        key=expired.locator('form[data-key-replacement] input[name=api_key]')
        key.fill('candidate-kept-in-input-only')
        with app.app_context():
            a=db.session.get(Account,ids['expired']);old_version=a.version;external=a.external_account_id
            a.label='Магазин после изменения в другой вкладке';a.version+=1;db.session.commit()
            updated=MarketplaceAccountService.rotate_ozon_key(seller_id=fixture['seller_id'],account_id=a.id,
                external_account_id=external,api_key='other-tab-current-key',expected_version=old_version+1)
            newer_version=updated.version
        path=f'/marketplaces/accounts/{ids["expired"]}/reconnect';expected.add((path,409))
        expired.get_by_role('button',name='Заменить ключ и проверить',exact=True).click()
        expired.get_by_role('button',name='Перечитать настройки',exact=True).wait_for()
        assert key.input_value()=='candidate-kept-in-input-only'
        with app.app_context():assert db.session.get(Account,ids['expired']).get_credentials()['api_key']=='other-tab-current-key'
        before=report['posts'];expired.get_by_role('button',name='Перечитать настройки',exact=True).click()
        expired.get_by_text('Статус перечитан.',exact=False).wait_for()
        assert report['posts']==before and key.input_value()=='candidate-kept-in-input-only'
        assert expired.locator('form[data-key-replacement] input[name=expected_version]').input_value()==str(newer_version)
        assert expired.locator('h3').inner_text()=='Магазин после изменения в другой вкладке'
        assert expired.locator('.ozon-key-expiry').get_attribute('data-state')=='unknown'
        layout('reviewed-conflict');passed('two_tabs_conflict_keeps_input_and_explicit_get_review')
        # Hidden form CSRF and header CSRF are both invalid: no mutation.
        original_csrf=page.locator('meta[name=csrf-token]').get_attribute('content')
        page.locator('meta[name=csrf-token]').evaluate('(node)=>node.content="invalid"')
        expired.locator('form[data-key-replacement] input[name=csrf_token]').evaluate('(node)=>node.value="invalid"')
        expected.add((path,400));expired.get_by_role('button',name='Заменить ключ и проверить',exact=True).click()
        page.wait_for_function('(id)=>!document.querySelector("#ozon-account-"+id+" form[data-key-replacement] button").disabled', arg=ids['expired'])
        with app.app_context():assert db.session.get(Account,ids['expired']).version==newer_version
        assert key.input_value()=='candidate-kept-in-input-only'
        page.locator('meta[name=csrf-token]').evaluate('(node,value)=>node.content=value',original_csrf)
        expired.locator('form[data-key-replacement] input[name=csrf_token]').evaluate('(node,value)=>node.value=value',original_csrf)
        passed('real_csrf_rejection_preserves_key_and_version')
        target_url=base+'/marketplaces/accounts/?account_id='+str(ids['expired'])+'#ozon-account-'+str(ids['expired'])
        assert 'action=replace-key' in page.url and page.url!=target_url
        with page.expect_navigation(url=target_url,wait_until='domcontentloaded'):
            expired.get_by_role('button',name='Заменить ключ и проверить',exact=True).click()
        loaded()
        assert page.locator(f'#ozon-account-{ids["expired"]} input[name=api_key]').input_value()==''
        with app.app_context():
            a=db.session.get(Account,ids['expired']);op=db.session.get(MarketplaceOperation,operation_id)
            assert a.get_credentials()['api_key']=='candidate-kept-in-input-only' and a.version==newer_version+1
            assert a.credential_expires_at is None and a.connection_status=='unchecked'
            assert (op.status,op.attempt_count)==('uncertain',1)
            assert notify_due_credentials(now=now)==0
        assert 'candidate-kept-in-input-only' not in page.content()
        passed('reviewed_rotation_clears_input_and_preserves_uncertain_history')
        assert not report['unexpected_http'] and not report['external'] and report['provider_attempts']==0,report
        report['status']='passed';browser.close()
except Exception as error:
    report['status']='failed';report['error']=str(error)[:3000]
    raise
finally:
    report['checked_at']=datetime.now(timezone.utc).isoformat()
    (OUT/'credential-browser.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    server.shutdown();thread.join(timeout=5);server.server_close();temporary.cleanup()
