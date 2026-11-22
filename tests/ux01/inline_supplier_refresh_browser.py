"""Exercise the normal catalog update enhancer with no marketplace network."""

from email import policy
from email.parser import BytesParser
import json
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from playwright.sync_api import expect, sync_playwright

from tests.ux01 import supplier_filtered_import_browser as supplier
from models import ImportedProduct, db


fixture = supplier.fixture
artifacts = Path('/artifacts/inline-supplier-refresh')
artifacts.mkdir(parents=True, exist_ok=True)
review_path = supplier.SUPPLIER_REVIEW_PATH
confirm_path = supplier.SUPPLIER_CONFIRM_PATH
state = {'previews': [], 'applies': [], 'drop_next_confirmation': False, 'errors': []}


def form_fields(request):
    content_type = request.headers.get('content-type', '')
    assert content_type.startswith('multipart/form-data;')
    mime = BytesParser(policy=policy.default).parsebytes(
        b'Content-Type: ' + content_type.encode('ascii') +
        b'\r\nMIME-Version: 1.0\r\n\r\n' + request.post_data_buffer
    )
    result = {}
    for field in mime.iter_parts():
        name = field.get_param('name', header='Content-Disposition')
        result.setdefault(name, []).append(field.get_content().strip())
    return result


def bridge(route):
    request = route.request
    parsed = urlsplit(request.url)
    if parsed.netloc == urlsplit(fixture.BASE).netloc and request.method == 'POST':
        assert parsed.path in {review_path, confirm_path}
        assert request.headers['accept'] == 'application/json'
        fields = form_fields(request)
        assert len(fields['csrf_token']) == 1 and fields['csrf_token'][0]
        assert len(fields['supplier_id']) == 1
        assert len(fields['update_product_ids']) == 1
        assert ('review_token' in fields) == (parsed.path == confirm_path)
        response = route.fetch(max_redirects=0)
        assert response.status == 200, response.status
        assert 'no-store' in response.headers.get('cache-control', '')
        payload = response.json()
        assert payload['success'] is True
        result = {'source_ids': fields['update_product_ids'], 'payload': payload}
        if parsed.path == review_path:
            state['previews'].append(result)
        else:
            state['applies'].append(result)
            if state['drop_next_confirmation']:
                state['drop_next_confirmation'] = False
                route.abort('failed')
                return
        route.fulfill(response=response)
        return
    fixture.bridge(route)


def snapshot(ids):
    with fixture.app.app_context():
        return {
            product_id: (row.title, row.supplier_content_revision)
            for product_id in ids
            if (row := db.session.get(ImportedProduct, product_id)) is not None
        }


def open_catalog(browser, supplier_id, source_id, index, *, width, theme):
    context = browser.new_context(viewport={'width': width, 'height': 950})
    context.add_cookies([fixture.synthetic_login_cookie()])
    context.add_init_script(f"localStorage.setItem('sh-theme', '{theme}');")
    context.route('**/*', bridge)
    page = context.new_page()
    page.on('pageerror', lambda error: state['errors'].append(str(error)))
    query = urlencode({
        'intent': 'updates', 'updates_only': '1', 'stock_status': 'all',
        'search': f'R52-FILTERED-{index:03d}',
    })
    response = page.goto(fixture.BASE + f'/supplier-catalog/{supplier_id}/products?{query}')
    assert response.status == 200
    page.evaluate("window.inlineRefreshDocument = 'same'")
    document_requests = []
    page.on('request', lambda request: document_requests.append(request.url)
            if request.resource_type == 'document' and request.frame == page.main_frame else None)
    original_url = page.url
    row = page.locator(f'input[form="supplier-update-form"][name="update_product_ids"][value="{source_id}"]')
    row.check()
    trigger = page.locator('[data-supplier-update-preview]')
    expect(trigger).to_be_enabled()
    trigger.focus()
    with page.expect_response(lambda response: urlsplit(response.url).path == review_path):
        trigger.press('Enter')
    panel = page.locator('[data-supplier-update-inline-review]')
    panel.wait_for()
    expect(panel.locator('[data-supplier-review-row]').first).to_be_visible()
    expect(panel.locator('h2')).to_be_focused()
    return context, page, panel, trigger, document_requests, original_url


def check_stays(page, document_requests, original_url):
    assert page.url == original_url and not document_requests
    assert page.evaluate("window.inlineRefreshDocument === 'same'")
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')


def run():
    app_ids = supplier.seed_supplier_products()
    seeded = supplier.seed_filtered_supplier_update_rows(app_ids['supplier_id'])
    source_one, source_two, source_excluded = seeded['source_ids'][:3]
    copy_one, copy_two, copy_excluded = seeded['stale_copy_ids'][:3]
    duplicate = seeded['current_duplicate_copy_id']
    with fixture.app.app_context():
        second_copy = db.session.get(ImportedProduct, duplicate)
        second_copy.title = 'Synthetic second stale copy'
        second_copy.supplier_content_revision = 1
        db.session.commit()
    watch = [copy_one, duplicate, copy_two, copy_excluded]
    before = snapshot(watch)
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=fixture.CHROMIUM,
                args=['--no-sandbox', '--disable-dev-shm-usage'])
            context, page, panel, trigger, navigations, original_url = open_catalog(
                browser, app_ids['supplier_id'], source_one, 0, width=320, theme='dark',
            )
            assert len(state['previews']) == 1 and not state['applies']
            preview = state['previews'][0]
            assert preview['source_ids'] == [str(source_one)]
            assert preview['payload']['source_requested'] == 1
            assert preview['payload']['requested'] == preview['payload']['changed'] == 2
            assert {row['product_id'] for row in preview['payload']['items']} == {str(copy_one), str(duplicate)}
            assert snapshot(watch) == before
            expect(trigger).to_be_disabled()
            apply = panel.locator('[data-supplier-update-apply]')
            expect(apply).to_be_enabled()
            assert apply.bounding_box()['height'] >= 44
            page.screenshot(path=artifacts / 'review-two-copies-dark-320.png')
            with page.expect_response(lambda response: urlsplit(response.url).path == confirm_path):
                apply.click()
            expect(panel.locator('[data-supplier-update-state]')).to_contain_text('Локально обновлено 2 из 2')
            check_stays(page, navigations, original_url)
            persisted = snapshot(watch)
            assert all(persisted[row][1] == 2 for row in (copy_one, duplicate))
            assert all(persisted[row] == before[row] for row in (copy_two, copy_excluded))
            assert state['applies'][0]['source_ids'] == [str(source_one)]
            panel.locator('[data-supplier-update-close]').click()
            expect(panel).to_be_hidden()
            expect(trigger).to_be_focused()
            context.close()

            context, page, panel, trigger, navigations, original_url = open_catalog(
                browser, app_ids['supplier_id'], source_two, 1, width=1440, theme='light',
            )
            assert len(state['previews']) == 2 and len(state['applies']) == 1
            state['drop_next_confirmation'] = True
            panel.locator('[data-supplier-update-apply]').click()
            status = panel.locator('[data-supplier-update-state]')
            expect(status).to_contain_text('Результат сохранения неизвестен')
            assert snapshot(watch)[copy_two][1] == 2
            assert len(state['applies']) == 2 and len(state['previews']) == 2
            with page.expect_response(lambda response: urlsplit(response.url).path == review_path):
                panel.locator('[data-supplier-update-recheck]').click()
            expect(status).to_contain_text('сверка состояния')
            assert not panel.locator('[data-supplier-update-apply]').is_visible()
            assert len(state['applies']) == 2 and len(state['previews']) == 3
            assert state['previews'][-1]['source_ids'] == [str(source_two)]
            assert state['previews'][-1]['payload']['changed'] == 0
            assert snapshot(watch)[copy_excluded] == before[copy_excluded]
            check_stays(page, navigations, original_url)
            page.screenshot(path=artifacts / 'unknown-confirm-rechecked-light-1440.png')
            context.close()
            browser.close()

        assert not state['errors'], state['errors']
        assert not fixture.REPORT['unexpected_http'], fixture.REPORT['unexpected_http']
        assert not fixture.REPORT['unexpected_external_requests']
        assert fixture.REPORT['provider_attempts'] == 0
        report = {
            'status': 'passed', 'scope': 'synthetic_classic_supplier_inline_refresh',
            'preview_requests': len(state['previews']), 'confirm_requests': len(state['applies']),
            'source_count_in_first_review': 1, 'exact_local_copies_updated': 2,
            'preview_read_only': True, 'unselected_copy_stayed_unchanged': True,
            'lost_confirm_reported_unknown_then_read_only_recheck': True,
            'document_navigation_count': 0, 'provider_attempts': 0,
        }
        (artifacts / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        print(json.dumps(report, ensure_ascii=False), flush=True)
    finally:
        fixture.server.shutdown()
        fixture.TEMP.cleanup()


if __name__ == '__main__':
    run()
