"""Offline Chromium regression for exact source photos in the Ozon draft editor.

This mounts the shipped Vue component and stylesheet without importing the app,
opening a DB, or making a provider request. All browser HTTP is intercepted.
"""
import json
import os
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from PIL import Image
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, sync_playwright


ROOT = Path(__file__).resolve().parents[2]
OUT = Path(os.environ.get('OZON_BROWSER_ARTIFACTS', '/artifacts'))
OUT.mkdir(parents=True, exist_ok=True)
report = {
    'status': 'running', 'checks': [], 'layouts': [], 'js_errors': [],
    'unexpected_http': [], 'external': [], 'provider_attempts': 0,
    'scope': 'synthetic_draft_editor_photo_component',
    'visibility_simulated': True, 'photo_gets': [],
}
origin = 'http://127.0.0.1:8765'
source = '/api/photos/imported-product/57/'
photo = {'mode': 'cold', 'slot0': 0, 'held': []}
buffer = BytesIO()
Image.new('RGB', (16, 16), '#9b6d5a').save(buffer, format='JPEG')
jpeg = buffer.getvalue()


def passed(name):
    assert not report['js_errors'], report['js_errors']
    report['checks'].append(name)
    print(json.dumps({'draft_photo_check': name}), flush=True)


def bridge(route):
    request = route.request
    url = urlsplit(request.url)
    if url.hostname != '127.0.0.1' or url.port != 8765:
        report['external'].append({'host': url.hostname, 'path': url.path})
        route.abort()
        return
    if request.method != 'GET':
        raise AssertionError(('unexpected mutation', request.method, url.path))
    if url.path == '/':
        route.fulfill(status=200, content_type='text/html', body='''<!doctype html><html data-theme="light"><head>
            <meta name="viewport" content="width=device-width, initial-scale=1"></head><body>
            <main class="ode" style="max-width:600px;margin:auto;padding:16px">
              <h1>Ozon draft photo preview</h1><div id="fixture"></div></main>
            </body></html>''')
        return
    if url.path not in (source + '0', source + '1'):
        report['unexpected_http'].append({'path': url.path, 'status': 404})
        route.fulfill(status=404, body=b'')
        return
    params = parse_qs(url.query)
    assert params.get('deferred') == ['1']
    assert set(params) <= {'deferred', 'preview_attempt', 'manual_retry'}
    report['photo_gets'].append({'slot': int(url.path[-1]), 'query': url.query, 'mode': photo['mode']})
    if url.path.endswith('/1'):
        route.fulfill(status=200, body=jpeg, content_type='image/jpeg')
        return
    photo['slot0'] += 1
    if photo['mode'] == 'hang':
        photo['held'].append(route)
        return
    if photo['mode'] == 'always202' or (photo['mode'] == 'cold' and photo['slot0'] == 1):
        route.fulfill(status=202, body=b'', content_type='image/jpeg', headers={
            'X-Photo-Cache': 'pending', 'Retry-After': '2', 'Cache-Control': 'no-store',
        })
        return
    route.fulfill(status=200, body=jpeg, content_type='image/jpeg', headers={'Cache-Control': 'no-store'})


def layout(page, name, selector='.ode-photo'):
    widths = (320, 390, 1024, 1280, 1440) if name == 'ready' else (320, 390)
    for theme in ('light', 'dark'):
        for width in widths:
            page.set_viewport_size({'width': width, 'height': 820})
            page.evaluate('(value) => document.documentElement.dataset.theme = value', theme)
            page.screenshot(path=str(OUT / f'draft-photo-{name}-{theme}-{width}.png'), animations='disabled')
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1'), (name, theme, width)
            assert page.locator(selector).evaluate('(node) => node.scrollWidth <= node.clientWidth + 1'), (name, theme, width)
            if name == 'ready':
                assert page.locator(selector).evaluate('''node => {
                    const rect = node.getBoundingClientRect();
                    return Math.abs(rect.width - rect.height) <= 1;
                }'''), (name, theme, width, 'loaded hero photo must remain square')
            report['layouts'].append({'name': name, 'theme': theme, 'width': width})
    page.set_viewport_size({'width': 390, 'height': 820})
    page.evaluate('document.documentElement.dataset.theme = "light"')


def ready(page):
    page.wait_for_function('''() => {
        const frame = document.querySelector('.ode-photo .ode-image-frame');
        const image = frame?.querySelector('img');
        return image?.complete && image.naturalWidth === 16 &&
            !frame.querySelector('.ode-image-state');
    }''')


def photo_status_text200(page, state, theme, width, expected_photo_width):
    page.set_viewport_size({'width': width, 'height': 820})
    page.evaluate('(value) => document.documentElement.dataset.theme = value', theme)
    page.evaluate('''state => {
        const photo = window.photoVm?.$refs?.photo;
        if (!photo || !['loading', 'pending', 'failed', 'paused'].includes(state)) {
            throw new Error('photo status fixture is unavailable');
        }
        photo.state = state;
    }''', state)
    scale = page.locator('.ode-photo .ode-image-state').evaluate('''state => {
        const counter = document.querySelector('.ode-photo > .ode-photo-counter');
        const targets = [state, state.querySelector('button'), counter].filter(Boolean);
        window.__photoStatusTextScale = targets.map(element => ({
            element, value:element.style.getPropertyValue('font-size'),
            priority:element.style.getPropertyPriority('font-size'),
            base:parseFloat(getComputedStyle(element).fontSize),
        }));
        window.__photoStatusTextScale.forEach(({element, base}) =>
            element.style.setProperty('font-size', `${base * 2}px`, 'important'));
        return window.__photoStatusTextScale.map(({base, element}) => ({
            base, scaled:parseFloat(getComputedStyle(element).fontSize),
        }));
    }''')
    page.evaluate('''() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))''')

    if state in ('failed', 'paused'):
        # Give Tab a deterministic native starting point. blur() alone does not
        # reset Chromium's sequential-focus cursor after earlier retry clicks.
        focus_start = page.locator('#photo-focus-start')
        focus_start.focus()
        assert page.evaluate("document.activeElement?.id === 'photo-focus-start'"), (
            'photo_status_keyboard_start', state, theme, width,
        )
        page.keyboard.press('Tab')
        try:
            page.wait_for_function(
                "() => document.activeElement?.matches('.ode-image-state button')",
                timeout=3000,
            )
        except PlaywrightTimeoutError as exc:
            focus_diagnostic = page.evaluate('''() => {
                const active = document.activeElement;
                const state = document.querySelector('.ode-image-state');
                return {
                    status:state?.innerText?.slice(0, 100) || '',
                    buttonCount:state?.querySelectorAll('button').length || 0,
                    activeTag:active?.tagName || null,
                    activeId:active?.id || null,
                    activeRole:active?.getAttribute('role') || null,
                    activeText:(active?.innerText || '').slice(0, 100),
                    startFocused:active?.id === 'photo-focus-start',
                };
            }''')
            screenshot = OUT / f'photo-status-focus-failure-{state}-{theme}-{width}.png'
            page.screenshot(path=str(screenshot), animations='disabled')
            raise AssertionError((
                'photo_status_keyboard_focus', state, theme, width,
                focus_diagnostic, screenshot.name,
            )) from exc

    metrics = page.evaluate('''() => {
        const photo = document.querySelector('.ode-photo');
        const state = photo?.querySelector('.ode-image-state');
        const counter = photo?.querySelector('.ode-photo-counter');
        const button = state?.querySelector('button');
        const rect = element => {
            if (!element) return null;
            const value = element.getBoundingClientRect();
            return {left:value.left, right:value.right, top:value.top, bottom:value.bottom,
                width:value.width, height:value.height};
        };
        const intersects = (a, b) => !!a && !!b && a.left < b.right && a.right > b.left &&
            a.top < b.bottom && a.bottom > b.top;
        const inside = (inner, outer, tolerance=1) => !!inner && !!outer &&
            inner.left >= outer.left - tolerance && inner.right <= outer.right + tolerance &&
            inner.top >= outer.top - tolerance && inner.bottom <= outer.bottom + tolerance;
        const textRects = element => {
            if (!element) return [];
            const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT);
            const result = [];
            while (walker.nextNode()) {
                const node = walker.currentNode;
                if (!node.textContent.trim()) continue;
                const range = document.createRange();
                range.selectNodeContents(node);
                for (const value of range.getClientRects()) result.push({
                    left:value.left, right:value.right, top:value.top, bottom:value.bottom,
                });
            }
            return result;
        };
        const clipped = (element, rects) => {
            const outside = [];
            for (let parent = element?.parentElement; parent; parent = parent.parentElement) {
                const style = getComputedStyle(parent), bounds = parent.getBoundingClientRect();
                for (const [axis, overflow, low, high] of [
                    ['x', style.overflowX, 'left', 'right'], ['y', style.overflowY, 'top', 'bottom'],
                ]) {
                    if (!['hidden', 'clip'].includes(overflow)) continue;
                    for (const line of rects) {
                        if (line[low] < bounds[low] - 1 || line[high] > bounds[high] + 1) {
                            outside.push({axis, overflow, ancestor:parent.className || parent.tagName});
                            break;
                        }
                    }
                }
            }
            return outside;
        };
        const photoRect = rect(photo), stateRect = rect(state), counterRect = rect(counter);
        const frameRect = rect(photo?.querySelector('.ode-image-frame'));
        const statusTextRects = textRects(state), counterTextRects = textRects(counter);
        const image = photo?.querySelector('.ode-image-frame > img');
        const imageStyle = image ? getComputedStyle(image) : null;
        const stateStyle = state ? getComputedStyle(state) : null;
        const imageRect = rect(image);
        const focusStyle = button ? getComputedStyle(button) : null;
        const buttonRect = rect(button);
        const statusCounterTextOverlap = statusTextRects.some(statusText =>
            counterTextRects.some(counterText => intersects(statusText, counterText)));
        const outlineWidth = focusStyle ? parseFloat(focusStyle.outlineWidth) || 0 : 0;
        const outlineOffset = focusStyle ? parseFloat(focusStyle.outlineOffset) || 0 : 0;
        const ring = buttonRect ? {
            left:buttonRect.left - outlineWidth - outlineOffset,
            right:buttonRect.right + outlineWidth + outlineOffset,
            top:buttonRect.top - outlineWidth - outlineOffset,
            bottom:buttonRect.bottom + outlineWidth + outlineOffset,
        } : null;
        return {
            stateText:state?.innerText || '', role:state?.getAttribute('role') || null,
            photo:photoRect, frame:frameRect, status:stateRect, counter:counterRect, button:buttonRect, focusRing:ring,
            image:image ? {rect:rect(image), display:imageStyle.display, opacity:imageStyle.opacity,
                loading:image.getAttribute('loading')} : null,
            stateBackground:stateStyle?.backgroundColor || null,
            photoContainsStatus:inside(stateRect, photoRect),
            photoContainsCounter:inside(counterRect, photoRect),
            photoContainsButton:inside(buttonRect, photoRect),
            photoContainsImage:inside(rect(image), photoRect),
            frameContainsImage:inside(rect(image), frameRect),
            statusContainsImage:inside(rect(image), stateRect),
            statusContainsButton:inside(buttonRect, stateRect),
            statusContainsText:statusTextRects.length > 0 && statusTextRects.every(line => inside(line, stateRect)),
            counterContainsText:counterTextRects.length > 0 && counterTextRects.every(line => inside(line, counterRect)),
            statusCounterOverlap:intersects(stateRect, counterRect),
            buttonCounterOverlap:intersects(buttonRect, counterRect),
            statusCounterTextOverlap,
            clippedStatusText:clipped(state, statusTextRects),
            clippedCounterText:clipped(counter, counterTextRects),
            pageWidth:document.documentElement.scrollWidth, viewportWidth:innerWidth,
            imageIntersectsViewport:!!imageRect && imageRect.right > 0 && imageRect.left < innerWidth &&
                imageRect.bottom > 0 && imageRect.top < innerHeight,
            focus:{
                active:!!button && document.activeElement === button,
                label:button?.getAttribute('aria-label') || null,
                visibleRing:!!focusStyle && focusStyle.outlineStyle !== 'none' && outlineWidth >= 1,
                ringInViewport:!!ring && ring.left >= -1 && ring.right <= innerWidth + 1,
                ringClipped:button ? clipped(button, [ring]).length > 0 : false,
            },
        };
    }''')
    assert all(1.99 <= item['scaled'] / item['base'] <= 2.01 for item in scale), (state, theme, width, scale)
    assert metrics['role'] == 'status', (state, theme, width, metrics)
    assert abs(metrics['photo']['width'] - expected_photo_width) <= 1, (state, theme, width, metrics)
    assert metrics['photoContainsStatus'] and metrics['photoContainsCounter'], (state, theme, width, metrics)
    assert metrics['statusContainsText'] and metrics['counterContainsText'], (state, theme, width, metrics)
    if width <= 760:
        assert not metrics['statusCounterOverlap'], (state, theme, width, metrics)
    else:
        assert not metrics['statusCounterTextOverlap'], (state, theme, width, metrics)
    assert not metrics['buttonCounterOverlap'], (state, theme, width, metrics)
    assert not metrics['clippedStatusText'] and not metrics['clippedCounterText'], (state, theme, width, metrics)
    assert metrics['pageWidth'] <= metrics['viewportWidth'] + 1, (state, theme, width, metrics)
    if state in ('loading', 'pending'):
        assert 'Загружаем фото…' in metrics['stateText'], (state, theme, width, metrics)
        assert metrics['button'] is None, (state, theme, width, metrics)
        if state == 'loading':
            image = metrics['image']
            assert image and image['rect']['width'] > 0 and image['rect']['height'] > 0, (state, theme, width, metrics)
            assert metrics['photoContainsImage'] and metrics['frameContainsImage'] and image['display'] != 'none', (state, theme, width, metrics)
            assert image['loading'] == 'lazy' and metrics['imageIntersectsViewport'], (state, theme, width, metrics)
            if width <= 760:
                assert image['opacity'] == '0', (state, theme, width, metrics)
            else:
                assert image['opacity'] != '0' and metrics['statusContainsImage'], (state, theme, width, metrics)
                assert metrics['stateBackground'] not in ('transparent', 'rgba(0, 0, 0, 0)'), (state, theme, width, metrics)
    else:
        expected = 'Повторить' if state == 'failed' else 'Продолжить'
        assert expected in metrics['stateText'] and metrics['photoContainsButton'] and metrics['statusContainsButton'], (state, theme, width, metrics)
        assert metrics['focus']['active'] and metrics['focus']['visibleRing'], (state, theme, width, metrics)
        assert metrics['focus']['ringInViewport'] and not metrics['focus']['ringClipped'], (state, theme, width, metrics)
        expected_label = 'Повторить загрузку фото' if state == 'failed' else 'Продолжить загрузку фото'
        assert metrics['focus']['label'].startswith(expected_label), (state, theme, width, metrics)
    screenshot = OUT / f"draft-photo-status-{state}-200-{theme}-{width}.png"
    page.screenshot(path=str(screenshot), animations='disabled')
    report['layouts'].append({
        'name':'photo-status-text-200', 'state':state, 'theme':theme, 'width':width,
        'text_scale':200, 'photo_height':round(metrics['photo']['height'], 2),
        'status_height':round(metrics['status']['height'], 2),
        'counter_overlap':metrics['statusCounterTextOverlap'],
        'control_focus':metrics['focus']['active'], 'screenshot':screenshot.name,
    })
    page.evaluate('''() => {
        const saved = window.__photoStatusTextScale || [];
        saved.forEach(({element, value, priority}) => value
            ? element.style.setProperty('font-size', value, priority)
            : element.style.removeProperty('font-size'));
        delete window.__photoStatusTextScale;
    }''')
    page.evaluate('''() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))''')


try:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=os.environ.get('OZON_BROWSER_CHROMIUM', '/usr/bin/chromium'),
            headless=True, args=['--no-sandbox', '--disable-dev-shm-usage'],
        )
        context = browser.new_context(viewport={'width': 390, 'height': 820}, service_workers='block')
        context.route('**/*', bridge)
        page = context.new_page()
        page.set_default_timeout(20000)
        page.on('pageerror', lambda error: report['js_errors'].append(str(error)))
        page.goto(origin + '/', wait_until='domcontentloaded')
        page.add_style_tag(path=str(ROOT / 'static/ozon-draft-editor.css'))
        page.add_style_tag(path=str(ROOT / 'static/ozon-bulk-repair-vue.css'))
        page.add_style_tag(content='''
            html[data-theme="light"]{--bg-card:#fff;--text-secondary:#555;--border:#bbb;--accent:#b06542}
            html[data-theme="dark"]{--bg-card:#272724;--text-secondary:#e5e0d6;--border:#666;--accent:#e0a17d}
            body{margin:0;background:var(--bg-card);color:var(--text-secondary);font:16px Arial,sans-serif}
            /* Match the global box-sizing reset from templates/base.html:52. */
            *,*::before,*::after{box-sizing:border-box}
            .ode-photo{margin:16px 0}
            @media(min-width:761px) and (max-width:1050px){.ode-photo{width:220px;height:220px}}
            @media(min-width:1051px){.ode-photo{width:280px;height:280px}}
        ''')
        page.add_script_tag(path=str(ROOT / 'static/vendor/vue-3.4.38.global.prod.js'))
        page.add_script_tag(path=str(ROOT / 'static/marketplace-beta-shared.js'))
        page.add_script_tag(path=str(ROOT / 'static/ozon-draft-editor.js'))
        page.evaluate('''() => {
            window.testHidden = false;
            Object.defineProperty(document, 'hidden', {configurable:true, get:() => window.testHidden});
            window.mountPhoto = () => {
                document.querySelector('#fixture').innerHTML = '<a id="photo-focus-start" href="#fixture">Начало фото</a><div class="ode-photo"><draft-photo ref="photo" :src="src" :source-id="57" alt="Тестовый товар"></draft-photo><span class="ode-photo-counter">1 / 1</span></div>';
                const app = Vue.createApp({
                    components:{'draft-photo':window.ozonDraftEditor.photoComponent},
                    data:() => ({src:'/api/photos/imported-product/57/0?deferred=1'})
                });
                window.photoVm = app.mount('#fixture'); window.photoApp = app;
            };
            window.mountPhoto();
        }''')
        frame = page.locator('.ode-photo .ode-image-frame')
        frame.get_by_text('Загружаем фото…', exact=True).wait_for()
        ready(page)
        assert photo['slot0'] == 2, photo
        layout(page, 'ready')
        passed('cold_cache_202_then_jpeg_without_reload_or_overlay')

        old = frame.locator('img').element_handle()
        page.evaluate('window.photoVm.src="/api/photos/imported-product/57/1?deferred=1"')
        ready(page)
        old.evaluate('(node) => node.dispatchEvent(new Event("error"))')
        assert frame.locator('.ode-image-state').count() == 0
        assert frame.locator('img').evaluate('(node) => node.currentSrc.endsWith("/1?deferred=1")')
        passed('late_old_image_error_cannot_change_new_source')

        photo.update(mode='hang', slot0=0)
        page.evaluate('window.photoVm.src="/api/photos/imported-product/57/0?deferred=1"')
        frame.get_by_text('Загружаем фото…', exact=True).wait_for()
        page.wait_for_function('''() => document.querySelector('.ode-image-frame')?.textContent.includes('Загружаем фото…') &&
            document.querySelector('.ode-image-frame img') === null''', timeout=16000)
        assert photo['slot0'] == 1, photo
        passed('hung_visible_image_has_12s_deadline_and_bounded_wait_state')
        page.evaluate('window.photoApp.unmount()')
        before = len(report['photo_gets'])
        page.wait_for_timeout(2500)
        assert len(report['photo_gets']) == before
        for held in photo['held']:
            try:
                held.abort()
            except Exception:
                pass
        photo['held'].clear()
        passed('unmount_cancels_pending_retry')

        photo.update(mode='always202', slot0=0)
        page.evaluate('window.mountPhoto()')
        frame = page.locator('.ode-photo .ode-image-frame')
        frame.locator('.ode-image-state').filter(has_text='Фото недоступно').wait_for(timeout=18000)
        assert photo['slot0'] == 4, photo
        layout(page, 'fallback')
        passed('four_bounded_gets_then_actionable_fallback')
        photo['mode'] = 'ready'
        frame.get_by_role('button', name='Повторить загрузку фото', exact=False).click()
        ready(page)
        assert photo['slot0'] == 5, photo
        passed('manual_retry_is_separate_and_succeeds')

        status_gets_before = len(report['photo_gets'])
        for state in ('loading', 'pending', 'failed', 'paused'):
            for theme in ('light', 'dark'):
                for width in (320, 390, 1024, 1280, 1440):
                    photo_width = 100 if width <= 760 else 220 if width <= 1050 else 280
                    photo_status_text200(page, state, theme, width, photo_width)
        assert len(report['photo_gets']) == status_gets_before, report['photo_gets']
        passed('hero_status_text200_grows_without_clipping_or_photo_requests')

        page.evaluate('window.photoApp.unmount(); window.testHidden=true; window.mountPhoto()')
        before = len(report['photo_gets'])
        page.wait_for_timeout(350)
        assert len(report['photo_gets']) == before
        page.evaluate('window.testHidden=false; document.dispatchEvent(new Event("visibilitychange"))')
        page.wait_for_timeout(350)
        assert len(report['photo_gets']) == before
        frame = page.locator('.ode-photo .ode-image-frame')
        frame.get_by_role('button', name='Продолжить загрузку фото', exact=False).click()
        ready(page)
        passed('hidden_mount_uses_no_get_and_requires_explicit_resume')

        photo.update(mode='always202', slot0=0)
        page.evaluate('''() => {
            window.photoApp.unmount();
            document.querySelector('#fixture').innerHTML =
                '<div class="orb-photo"><draft-photo :src="src" :source-id="57" alt="Тестовый товар" :compact="true" :retryable-compact="true"></draft-photo></div>';
            const app = Vue.createApp({
                components:{'draft-photo':window.ozonDraftEditor.photoComponent},
                data:() => ({src:'/api/photos/imported-product/57/0?deferred=1'})
            });
            window.photoVm = app.mount('#fixture'); window.photoApp = app;
        }''')
        compact = page.locator('.orb-photo .ode-image-frame')
        compact.get_by_role('button', name='Повторить загрузку фото', exact=False).wait_for(timeout=18000)
        assert photo['slot0'] == 4, photo
        assert page.locator('.orb-photo button button').count() == 0
        assert compact.locator('.ode-image-state').evaluate('(node) => node.scrollWidth <= node.clientWidth + 1')
        layout(page, 'compact-fallback', '.orb-photo')
        photo['mode'] = 'ready'
        compact.get_by_role('button', name='Повторить загрузку фото', exact=False).click()
        page.wait_for_function('''() => {
            const frame = document.querySelector('.orb-photo .ode-image-frame');
            const image = frame?.querySelector('img');
            return image?.complete && image.naturalWidth === 16 &&
                !frame.querySelector('.ode-image-state');
        }''')
        assert photo['slot0'] == 5, photo
        passed('compact_bulk_repair_fallback_has_independent_manual_retry')

        assert not report['js_errors'] and not report['unexpected_http'] and not report['external']
        assert report['provider_attempts'] == 0
        report['status'] = 'passed'
        context.close()
        browser.close()
finally:
    (OUT / 'draft-editor-photo-browser.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
