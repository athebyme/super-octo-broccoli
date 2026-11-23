from email import policy
from email.parser import BytesParser
import json
from pathlib import Path
import re
import secrets
import threading
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import expect, sync_playwright

from tests.ux01 import supplier_filtered_import_browser as supplier
from models import BackgroundJob, ImportedProduct, SellerMarketplaceAccount, SellerSupplier, db


fixture = supplier.fixture
artifacts = Path('/artifacts/inline-import')
artifacts.mkdir(parents=True, exist_ok=True)
post_path = '/supplier-catalog/import'
receipt_path = '/supplier-catalog/import/result'
prepare_path = '/marketplaces/ozon/uploads/'
prepare_status_pattern = re.compile(
    r'/marketplaces/ozon/uploads/api/(ozon-upload-[0-9a-f]{32})'
)
wb_create_path = '/my-products/push-to-wb-bulk'
wb_create_result_path = '/my-products/push-to-wb-bulk/result'
feed_path = '/api/my-products'
state = {
    'mode': 'normal', 'catalog': [], 'posts': [], 'receipts': [], 'prepares': [], 'errors': [],
    'prepare_status_next': 'all_prepared', 'prepare_status_by_uid': {},
    'prepare_status_reads': [],
    'wb_mode': 'normal', 'wb_posts': [], 'wb_reads': [], 'wb_feed_reads': [],
    'wb_worker_product_ids': [], 'wb_worker_errors': [],
}


def multipart(request):
    content_type = request.headers['content-type'].encode('ascii')
    raw = request.post_data_buffer
    message = BytesParser(policy=policy.default).parsebytes(
        b'Content-Type: ' + content_type + b'\r\nMIME-Version: 1.0\r\n\r\n' + raw
    )
    values = {}
    for field in message.iter_parts():
        name = field.get_param('name', header='Content-Disposition')
        assert name in {'supplier_id', 'product_ids', 'csrf_token', 'request_key', 'draft_account_ids'}
        values.setdefault(name, []).append(field.get_content().strip())
    return values


def bridge(route):
    request = route.request
    parsed = urlsplit(request.url)
    if parsed.netloc == urlsplit(fixture.BASE).netloc:
        if request.method == 'POST' and parsed.path == wb_create_path:
            assert not parsed.query
            assert request.headers.get('accept') == 'application/json'
            assert 'application/json' in (request.headers.get('content-type') or '')
            assert request.headers.get('x-csrftoken')
            posted = request.post_data_json
            assert set(posted) == {'product_ids', 'request_key'}
            assert isinstance(posted['product_ids'], list) and posted['product_ids']
            assert all(type(value) is int and value > 0 for value in posted['product_ids'])
            assert len(set(posted['product_ids'])) == len(posted['product_ids'])
            assert re.fullmatch(r'[0-9a-f]{32}', posted['request_key'])
            attempted = {
                'key': posted['request_key'], 'ids': posted['product_ids'][:],
                'status': None, 'code': None, 'transport_lost': False,
            }
            state['wb_posts'].append(attempted)
            mode, state['wb_mode'] = state['wb_mode'], 'normal'
            if mode == 'drop_before':
                attempted['transport_lost'] = True
                route.abort('failed')
                return
            response = route.fetch(max_redirects=0)
            attempted['status'] = response.status
            try:
                payload = response.json()
            except Exception:
                payload = None
            if isinstance(payload, dict):
                attempted['code'] = payload.get('code')
                attempted['receipt'] = {
                    key: payload.get(key) for key in (
                        'success', 'job_uid', 'total', 'product_ids', 'status',
                        'replayed', 'worker_exited', 'accepted', 'result_counts',
                    ) if key in payload
                }
            route.fulfill(response=response)
            return
        if request.method == 'GET' and parsed.path == wb_create_result_path:
            assert not parsed.query
            key = request.headers.get('x-wb-create-key')
            assert key and re.fullmatch(r'[0-9a-f]{32}', key)
            response = route.fetch(max_redirects=0)
            try:
                payload = response.json()
            except Exception:
                payload = None
            state['wb_reads'].append({
                'key': key, 'status': response.status,
                'receipt': {
                    field: payload.get(field) for field in (
                        'success', 'code', 'job_uid', 'total', 'product_ids',
                        'status', 'replayed', 'worker_exited', 'cancel_requested',
                        'result_counts',
                    ) if isinstance(payload, dict) and field in payload
                },
            })
            route.fulfill(response=response)
            return
        if request.method == 'GET' and parsed.path == feed_path:
            query = parse_qs(parsed.query, keep_blank_values=True)
            response = route.fetch(max_redirects=0)
            try:
                payload = response.json()
            except Exception:
                payload = None
            state['wb_feed_reads'].append({
                'query_keys': tuple(sorted(query)),
                'batch_tokens': tuple(query.get('import_batch', [])),
                'status': response.status,
                'ids': [row.get('id') for row in payload.get('items', [])]
                    if isinstance(payload, dict) and isinstance(payload.get('items'), list) else [],
                'batch_count': payload.get('import_batch_count') if isinstance(payload, dict) else None,
            })
            route.fulfill(response=response)
            return
        if request.method == 'POST' and parsed.path == post_path:
            assert request.headers.get('accept') == 'application/json'
            fields = multipart(request)
            assert len(fields['request_key']) == 1 and re.fullmatch(r'[0-9a-f]{32}', fields['request_key'][0])
            assert len(fields['csrf_token']) == 1 and fields['csrf_token'][0]
            assert len(fields['supplier_id']) == 1 and fields['product_ids']
            attempted = {'key': fields['request_key'][0], 'source_ids': fields['product_ids'],
                         'supplier_id': fields['supplier_id'][0], 'status': None}
            state['posts'].append(attempted)
            mode, state['mode'] = state['mode'], 'normal'
            if mode == 'drop_before':
                route.abort('failed')
                return
            response = route.fetch(max_redirects=0)
            attempted['status'] = response.status
            if mode == 'drop_after':
                assert response.status in {200, 201}
                attempted['confirmed_committed'] = response.json()['imported']
                route.abort('failed')
                return
            route.fulfill(response=response)
            return
        if request.method == 'GET' and parsed.path == receipt_path:
            assert not parsed.query
            key = request.headers.get('x-import-request-key')
            assert key and re.fullmatch(r'[0-9a-f]{32}', key)
            response = route.fetch(max_redirects=0)
            state['receipts'].append({'key': key, 'status': response.status})
            route.fulfill(response=response)
            return
        if request.method == 'POST' and parsed.path == prepare_path:
            posted = request.post_data_json
            assert set(posted) == {'account_id', 'imported_product_ids', 'request_key', 'confirm_prepare'}
            assert posted['confirm_prepare'] is True
            assert re.fullmatch(r'[0-9a-f]{32}', posted['request_key'])
            response = route.fetch(max_redirects=0)
            assert response.status == 202, response.status
            location = response.headers['location']
            job_uid = location.rsplit('/', 1)[-1]
            assert re.fullmatch(r'ozon-upload-[0-9a-f]{32}', job_uid)
            status_mode, state['prepare_status_next'] = (
                state['prepare_status_next'], 'all_prepared',
            )
            prepare = {
                'ids': posted['imported_product_ids'],
                'account_id': posted['account_id'],
                'location': location,
                'job_uid': job_uid,
                'status_mode': status_mode,
            }
            state['prepares'].append(prepare)
            state['prepare_status_by_uid'][job_uid] = prepare
            route.fulfill(response=response)
            return
        status_match = prepare_status_pattern.fullmatch(parsed.path)
        if request.method == 'GET' and status_match:
            assert not parsed.query
            job_uid = status_match.group(1)
            prepare = state['prepare_status_by_uid'].get(job_uid)
            assert prepare is not None, 'status GET must use this tab’s exact accepted receipt UID'
            response = route.fetch(max_redirects=0)
            assert response.status == 200, response.status
            payload = response.json()
            assert isinstance(payload, dict) and payload.get('success') is True
            run = payload.get('run')
            assert isinstance(run, dict)
            assert run.get('job_uid') == job_uid
            assert run.get('account_id') == prepare['account_id']
            assert run.get('mode') == 'source_prepare'
            summary = run.get('summary')
            assert isinstance(summary, dict) and summary.get('version') == 3
            assert summary.get('account_id') == prepare['account_id']
            assert summary.get('mode') == 'source_prepare'
            assert summary.get('total') == len(prepare['ids'])
            items = run.get('items')
            assert isinstance(items, list)
            assert [item.get('imported_product_id') for item in items] == prepare['ids']

            read = {
                'uid': job_uid,
                'query': parsed.query,
                'http_status': response.status,
                'account_id': run['account_id'],
                'mode': run['mode'],
                'summary_version': summary['version'],
                'total': summary['total'],
                'delivered_status': None,
                'delivered_account_id': None,
            }
            status_mode = prepare['status_mode']
            prior_reads = [
                row for row in state['prepare_status_reads']
                if row['uid'] == job_uid
            ]
            if status_mode == 'mixed' and not prior_reads:
                assert run.get('status') == 'running'
                assert summary.get('outcome') == 'running'
                assert summary.get('active', 0) > 0
                assert summary.get('processed', 0) + summary['active'] == summary['total']
                read.update({
                    'delivered_status': 'running',
                    'delivered_account_id': prepare['account_id'],
                    'processed': summary['processed'],
                    'active': summary['active'],
                    'prepared': summary['prepared'],
                    'needs_input': summary['needs_input'],
                })
                state['prepare_status_reads'].append(read)
                # Preserve the actual V3 running response for the first poll;
                # the next poll below supplies only the deterministic terminal fixture.
                route.fulfill(response=response)
                return
            if status_mode == 'mismatched_account':
                payload = json.loads(json.dumps(payload))
                payload['run']['account_id'] = prepare['account_id'] + 1
                payload['run']['summary']['account_id'] = prepare['account_id'] + 1
                read['delivered_account_id'] = prepare['account_id'] + 1
                state['prepare_status_reads'].append(read)
                route.fulfill(
                    status=response.status,
                    content_type='application/json; charset=utf-8',
                    headers={'Cache-Control': 'private, no-store'},
                    body=json.dumps(payload, ensure_ascii=False),
                )
                return

            prepared_count = len(prepare['ids'])
            needs_input_count = 0
            if status_mode == 'mixed':
                assert prepared_count >= 2
                prepared_count -= 1
                needs_input_count = 1
            for index, item in enumerate(payload['run']['items']):
                phase = 'prepared' if index < prepared_count else 'needs_input'
                if status_mode != 'mixed':
                    phase = 'prepared'
                item['phase'] = phase
                item['status'] = phase
                if phase == 'needs_input':
                    item['code'] = 'missing_required_attribute'
                    item['message'] = 'Остаётся заполнить подтверждённый факт'
            final_counts = {'prepared': prepared_count}
            if needs_input_count:
                final_counts['needs_input'] = needs_input_count
            final_summary = payload['run']['summary']
            final_summary.update({
                'processed': len(prepare['ids']),
                'prepared': prepared_count,
                'needs_input': needs_input_count,
                'failed': 0,
                'active': 0,
                'outcome': 'partial' if needs_input_count else 'success',
                'status_counts': final_counts,
            })
            payload['run']['status'] = 'completed'
            read['delivered_status'] = 'partial' if needs_input_count else 'success'
            read['delivered_account_id'] = prepare['account_id']
            read['prepared'] = prepared_count
            read['needs_input'] = needs_input_count
            state['prepare_status_reads'].append(read)
            route.fulfill(
                status=response.status,
                content_type='application/json; charset=utf-8',
                headers={'Cache-Control': 'private, no-store'},
                body=json.dumps(payload, ensure_ascii=False),
            )
            return
        if (request.method == 'GET' and re.fullmatch(r'/supplier-catalog/\d+/products', parsed.path)
                and request.headers.get('accept') == 'application/json'):
            response = route.fetch(max_redirects=0)
            assert response.status == 200
            payload = response.json()
            state['catalog'].append({
                'query': parse_qs(parsed.query),
                'selection': payload.get('selection', []),
                'ids': [row['id'] for row in payload['items']],
            })
            route.fulfill(response=response)
            return
        if request.method == 'GET' and parsed.path.startswith('/supplier-catalog/photo/'):
            route.fulfill(status=204)
            return
    fixture.bridge(route)


def page_for(browser, *, width, theme, supplier_id=None):
    context = browser.new_context(viewport={'width': width, 'height': 950})
    context.add_cookies([fixture.synthetic_login_cookie()])
    context.add_init_script(f"localStorage.setItem('sh-theme', '{theme}')")
    context.route('**/*', bridge)
    page = context.new_page()
    page.on('pageerror', lambda error: state['errors'].append(str(error)))
    page.set_default_timeout(12000)
    page.goto(fixture.BASE + '/my-products/beta', wait_until='networkidle')
    record = []
    catalog_requests = []
    page.on('request', lambda request: record.append(urlsplit(request.url).path)
            if request.resource_type == 'document' and request.frame == page.main_frame else None)
    page.on('request', lambda request: catalog_requests.append(request.url)
            if re.fullmatch(r'/supplier-catalog/\d+/products', urlsplit(request.url).path) else None)
    page.evaluate("window.supplierProofDocument = 'first'")
    assert page.evaluate('Vue.version') == '3.5.46'
    assert page.evaluate('document.documentElement.dataset.theme') == theme
    initial_reads = len(state['catalog'])
    launcher = page.locator('.mp-head-actions [data-supplier-import-open]')
    launcher.focus()
    launcher.press('Enter')
    panel = page.locator('[data-supplier-import-root]')
    panel.get_by_role('heading', name='Импорт товаров').wait_for()
    source = panel.locator('[data-supplier-import-source-select]')
    if supplier_id is None:
        assert source.count() == 0
    else:
        assert source.count() == 1 and source.input_value() == ''
        assert len(state['catalog']) == initial_reads and not catalog_requests
        source.select_option(str(supplier_id))
    panel.locator('[data-supplier-import-item-id]').first.wait_for()
    assert len(catalog_requests) == 1
    return context, page, panel, record


def search_for(panel, page, term):
    field = panel.locator('[data-supplier-import-search]')
    with page.expect_response(lambda response:
            response.request.method == 'GET' and
            parse_qs(urlsplit(response.url).query).get('search') == [term]) as wait:
        field.fill(term)
    payload = wait.value.json()
    assert wait.value.status == 200, wait.value.status
    return payload


def checkbox(panel, source_id):
    row = panel.locator(f'[data-supplier-import-item-id="{source_id}"]')
    row.wait_for()
    if row.evaluate('element => element instanceof HTMLInputElement'):
        return row
    return row.locator('input[type="checkbox"]')


def check_filter_invalidation(page, panel, search):
    first_source = next(row['id'] for row in search['items'] if row['importable'])
    checkbox(panel, first_source).check()
    assert panel.locator('[data-supplier-import-submit]').is_enabled()
    narrowed = 'R28 Mass batch item 001'
    with page.expect_response(lambda response:
            parse_qs(urlsplit(response.url).query).get('search') == [narrowed]) as next_read:
        invalidated = panel.locator('[data-supplier-import-search]').evaluate('''async (field, term) => {
            field.value = term;
            field.dispatchEvent(new Event('input', { bubbles: true }));
            await Promise.resolve();
            const importButton = document.querySelector('[data-supplier-import-submit]');
            return { blocked: importButton.disabled,
                     selected: document.querySelector('.mp-import-count strong').textContent.trim() };
        }''', narrowed)
        assert invalidated == {'blocked': True, 'selected': '0 из 200'}
    assert next_read.value.status == 200 and next_read.value.json()['total'] == 1


def confirm_exact_result(page, source_ids, navigations):
    page.locator('[data-supplier-import-result]').wait_for()
    page.wait_for_function("""expected => {
        const data = document.querySelector('#my-products-app')?._vnode?.component?.proxy;
        return data && data.importBatchMode && data.items.length === expected &&
          data.selected.length === expected;
    }""", arg=len(source_ids))
    actual = page.evaluate("""() => {
        const data = document.querySelector('#my-products-app')._vnode.component.proxy;
        return { items: data.items.map(item => item.id), selected: data.selected.slice() };
    }""")
    assert actual['items'] == actual['selected']
    with fixture.app.app_context():
        copies = ImportedProduct.query.filter(
            ImportedProduct.id.in_(actual['items']),
            ImportedProduct.seller_id == fixture.FIXTURE['seller_id'],
        ).all()
        assert {str(row.supplier_product_id) for row in copies} == set(source_ids)
        assert len(copies) == len(source_ids)
    assert page.evaluate("window.supplierProofDocument === 'first'") and not navigations
    assert parse_qs(urlsplit(page.url).query).keys() == {'import_batch'}
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
    return actual['selected']


def click_import_and_wait(page, panel, expected_status):
    submit = panel.locator('[data-supplier-import-submit]')
    assert submit.is_enabled()
    with page.expect_response(lambda response: urlsplit(response.url).path == post_path
                              and response.request.method == 'POST') as wait:
        submit.click()
    assert wait.value.status in expected_status
    return wait.value.json()


def check_channel_actions(page, count, navigations):
    next_steps = page.locator('[data-supplier-import-next-steps]')
    next_steps.wait_for()
    expect(page.get_by_text(f'Выбрано {count} товара', exact=True)).to_be_visible()
    before = len(state['posts']), len(fixture.REPORT['writes']), len(state['prepares'])
    for key, name in (('push_wb', 'Создать карточки WB'), ('ozon', f'Подготовить {count} товара')):
        button = next_steps.locator(f'[data-supplier-import-next-action="{key}"]')
        assert button.is_enabled()
        button.click()
        dialog = page.get_by_role('dialog' if key == 'ozon' else 'alertdialog', name=name, exact=True)
        dialog.wait_for()
        if key == 'ozon':
            assert dialog.locator('[data-ozon-preparation-account]').count() == 1
            assert dialog.locator('#mp-ozon-account').count() == 0
            assert dialog.get_by_role('button', name='Подготовить черновики').is_enabled()
            page.keyboard.press('Escape')
        else:
            assert str(count) in dialog.inner_text()
            dialog.get_by_role('button', name='Отмена', exact=True).click()
        dialog.wait_for(state='hidden')
        if key == 'ozon':
            expect(button).to_be_focused()
        assert (len(state['posts']), len(fixture.REPORT['writes']), len(state['prepares'])) == before
        assert not navigations
    return ['push_wb', 'ozon']


def check_inline_ozon_preparation(
    page, selected, navigations, *, account_id, account_label,
    choose_account=False, status_mode='all_prepared',
):
    previous_runs = len(state['prepares'])
    next_action = page.locator('[data-supplier-import-next-action="ozon"]')
    next_action.click()
    confirmation = page.locator('dialog[aria-labelledby="mp-ozon-dialog-title"]')
    confirm = confirmation.get_by_role('button', name='Подготовить черновики', exact=True)
    if choose_account:
        account_picker = confirmation.locator('#mp-ozon-account')
        assert account_picker.input_value() == '' and not confirm.is_enabled()
        assert account_picker.locator('option').count() == 3
        account_picker.select_option(str(account_id))
        assert confirm.is_enabled() and len(state['prepares']) == previous_runs
    state['prepare_status_next'] = status_mode
    with page.expect_response(lambda response: urlsplit(response.url).path == prepare_path
                              and response.request.method == 'POST') as queued:
        confirm.click()
    assert queued.value.status == 202 and len(state['prepares']) == previous_runs + 1
    prepare = state['prepares'][-1]
    assert prepare['ids'] == selected and prepare['account_id'] == account_id
    assert re.fullmatch(r'/marketplaces/ozon/uploads/ozon-upload-[0-9a-f]{32}', prepare['location'])
    receipt = page.locator('[data-ozon-local-prepare-receipt]')
    receipt.wait_for()
    scope = receipt.locator('[data-ozon-local-prepare-scope]')
    expect(scope).to_contain_text(account_label)
    expect(scope).to_contain_text(str(len(selected)))
    assert 'Отправка в Ozon требует отдельного подтверждения.' in receipt.inner_text()
    review = receipt.locator('[data-ozon-local-prepare-run]')
    expect(review).to_have_attribute('href', prepare['location'])
    assert not navigations and page.evaluate("window.supplierProofDocument === 'first'")
    assert urlsplit(page.url).path == '/my-products/beta'
    assert parse_qs(urlsplit(page.url).query).keys() == {'import_batch'}
    assert page.evaluate("document.querySelector('#my-products-app')._vnode.component.proxy.selected") == selected
    status = page.locator('[data-ozon-local-prepare-status]')
    if status_mode == 'mismatched_account':
        expect(status).to_have_attribute('data-status', 'unknown')
        expect(status).to_contain_text('Статус не удалось подтвердить.')
        assert page.locator('[data-ozon-local-prepare-counts]').count() == 0
        reads = [row for row in state['prepare_status_reads'] if row['uid'] == prepare['job_uid']]
        assert len(reads) == 1
        assert reads[0]['query'] == ''
        assert reads[0]['account_id'] == account_id
        assert reads[0]['mode'] == 'source_prepare'
        assert reads[0]['delivered_account_id'] == account_id + 1
    else:
        if status_mode == 'mixed':
            page.wait_for_function("""() => {
                const proxy = document.querySelector('#my-products-app')?._vnode?.component?.proxy;
                return proxy && proxy.ozonInlineStatus && proxy.ozonInlineStatus.state === 'running';
            }""")
            expect(status).to_have_attribute('data-status', 'running')
            expect(status).to_contain_text('Подготовка выполняется.')
            first_reads = [
                row for row in state['prepare_status_reads']
                if row['uid'] == prepare['job_uid']
            ]
            assert len(first_reads) == 1
            assert first_reads[0]['delivered_status'] == 'running'
            assert first_reads[0]['active'] > 0
            assert first_reads[0]['processed'] + first_reads[0]['active'] == len(selected)
        if status_mode == 'mixed':
            expect(status).to_have_attribute('data-status', 'completed', timeout=12000)
        else:
            expect(status).to_have_attribute('data-status', 'completed')
        expect(status).to_contain_text('Подготовка завершена.')
        counts = page.locator('[data-ozon-local-prepare-counts]')
        expect(counts).to_be_visible()
        ready_count = counts.locator('[data-ozon-local-prepare-ready-count]')
        attention_count = counts.locator('[data-ozon-local-prepare-attention-count]')
        if status_mode == 'mixed':
            expect(ready_count).to_have_text('Подготовлено: 51')
            expect(attention_count).to_have_text('Требуют внимания: 1')
        else:
            expect(ready_count).to_have_text(f'Подготовлено: {len(selected)}')
            expect(attention_count).to_have_text('Требуют внимания: 0')
        reads = [row for row in state['prepare_status_reads'] if row['uid'] == prepare['job_uid']]
        assert len(reads) == (2 if status_mode == 'mixed' else 1)
        assert reads[0]['query'] == ''
        assert reads[0]['account_id'] == account_id
        assert reads[0]['mode'] == 'source_prepare'
        assert reads[0]['delivered_account_id'] == account_id
        terminal_read = reads[-1]
        assert terminal_read['query'] == ''
        assert terminal_read['account_id'] == account_id
        assert terminal_read['mode'] == 'source_prepare'
        assert terminal_read['delivered_account_id'] == account_id
        assert terminal_read['delivered_status'] in {'partial', 'success'}
        assert terminal_read['total'] == len(selected)
        if status_mode == 'mixed':
            assert terminal_read['summary_version'] == 3
            assert terminal_read['prepared'] == 51 and terminal_read['needs_input'] == 1
            geometry = page.evaluate("""() => {
                const receipt = document.querySelector('[data-ozon-local-prepare-receipt]');
                const actions = [...receipt.querySelectorAll(
                    '[data-ozon-local-prepare-run], [data-ozon-local-prepare-dismiss]'
                )].map(element => {
                    const rect = element.getBoundingClientRect();
                    return {height: rect.height, width: rect.width};
                });
                return {
                    viewportWidth: window.innerWidth,
                    documentWidth: document.documentElement.scrollWidth,
                    actions,
                };
            }""")
            assert geometry['documentWidth'] <= geometry['viewportWidth'] + 1, geometry
            assert len(geometry['actions']) == 2 and all(
                action['height'] >= 44 and action['width'] >= 44
                for action in geometry['actions']
            ), geometry
            page.screenshot(path=artifacts / 'inline-ozon-mixed-dark-320.png')
    assert page.evaluate("document.querySelector('#my-products-app')._vnode.component.proxy.selected") == selected
    assert review.get_attribute('href') == prepare['location']
    assert len(state['prepares']) == previous_runs + 1, 'status polling must not create another prepare run'
    with fixture.app.app_context():
        job = BackgroundJob.query.filter_by(job_uid=prepare['location'].rsplit('/', 1)[-1],
                                            seller_id=fixture.FIXTURE['seller_id']).one()
        assert job.total == len(selected)
    if not choose_account:
        page.screenshot(path=artifacts / 'ozon-local-preparation-dark-320.png')
        page.locator('#my-products-app .mp-row input[type="checkbox"]').first.uncheck()
        assert page.evaluate("document.querySelector('#my-products-app')._vnode.component.proxy.selected.length") == len(selected) - 1
        assert len(state['prepares']) == previous_runs + 1
        assert str(len(selected)) in scope.inner_text()
        assert urlsplit(review.get_attribute('href')).path == prepare['location']
    return prepare['location']


def check_wb_create_selection_continuity(
    page, selected, navigations, *, first_account_id, first_account_label,
    second_account_id, second_account_label,
):
    """Exercise keyed WB recovery, overlap denial, and exact Ozon handoff on one signed batch."""
    from services.wb_product_importer import WBProductImporter

    started = threading.Event()
    release = threading.Event()
    importer_returned = threading.Event()
    original_batch_import = WBProductImporter.batch_import_products_to_wb
    state['wb_worker_started_event'] = started
    state['wb_worker_release_event'] = release
    state['wb_worker_returned_event'] = importer_returned
    state['wb_importer_class'] = WBProductImporter
    state['wb_importer_original'] = original_batch_import

    def deterministic_blocked_wb_import(self, imported_products, progress_callback=None):
        product_ids = [product.id for product in imported_products]
        state['wb_worker_product_ids'].append(product_ids)
        started.set()
        try:
            if not release.wait(20):
                state['wb_worker_errors'].append('worker_release_timeout')
                raise RuntimeError('synthetic WB worker release timed out')
            outcomes = [{
                'product_id': product.id,
                'title': (product.title or '')[:50],
                'status': 'failed',
                'error': 'Synthetic WB rejected this fixture card',
                'nm_id': None,
                'warnings': [],
            } for product in imported_products]
            if progress_callback:
                progress_callback('done', {'items': outcomes})
            return outcomes
        finally:
            importer_returned.set()

    WBProductImporter.batch_import_products_to_wb = deterministic_blocked_wb_import
    feed_reads_before = len(state['wb_feed_reads'])
    try:
        assert len(selected) == 1
        assert page.evaluate("document.querySelector('#my-products-app')._vnode.component.proxy.selected") == selected
        assert page.evaluate("window.supplierProofDocument === 'first'") and not navigations

        wb_action = page.locator('[data-supplier-import-next-action="push_wb"]')
        assert wb_action.count() == 1 and wb_action.is_enabled()
        wb_action.click()
        confirmation = page.get_by_role('alertdialog')
        confirmation.wait_for(state='visible')
        create = confirmation.get_by_role('button', name='Создать карточки', exact=True)
        assert create.is_enabled() and str(len(selected)) in confirmation.inner_text()
        state['wb_mode'] = 'drop_before'
        with page.expect_event(
            'requestfailed',
            predicate=lambda request: urlsplit(request.url).path == wb_create_path
            and request.method == 'POST',
        ) as lost:
            create.click()
        assert urlsplit(lost.value.url).path == wb_create_path
        confirmation.wait_for(state='hidden')

        retry = page.locator('[data-wb-create-retry]')
        retry.wait_for(state='visible')
        receipt = page.locator('[data-wb-create-receipt]')
        expect(receipt).to_have_attribute('data-wb-create-state', 'unknown')
        assert len(state['wb_posts']) == 1 and state['wb_posts'][0]['transport_lost']
        assert state['wb_posts'][0]['ids'] == selected
        absent_reads = [entry for entry in state['wb_reads'] if entry['status'] == 404]
        assert absent_reads and absent_reads[-1]['receipt'].get('code') == 'wb_create_receipt_not_found'
        assert absent_reads[-1]['key'] == state['wb_posts'][0]['key']
        assert len(state['wb_posts']) == 1, 'the lost response must recover by GET before explicit retry'
        assert page.evaluate("document.querySelector('#my-products-app')._vnode.component.proxy.selected") == selected

        with page.expect_response(
            lambda response: urlsplit(response.url).path == wb_create_path
            and response.request.method == 'POST',
        ) as retried:
            retry.click()
        retry_response = retried.value
        assert retry_response.status == 202
        accepted = retry_response.json()
        assert set(accepted) == {
            'success', 'job_uid', 'total', 'product_ids', 'status', 'replayed',
            'worker_exited', 'cancel_requested', 'result_counts',
        }
        assert accepted['success'] is True and accepted['replayed'] is False
        assert accepted['product_ids'] == selected and accepted['total'] == len(selected)
        assert accepted['status'] in {'pending', 'running'} and accepted['worker_exited'] is False
        assert accepted['result_counts'] is None
        assert re.fullmatch(r'bulk_wb_[0-9a-f]{56}', accepted['job_uid'])
        assert state['wb_posts'][1]['key'] == state['wb_posts'][0]['key']
        assert state['wb_posts'][1]['ids'] == state['wb_posts'][0]['ids'] == selected
        assert state['wb_posts'][1]['status'] == 202
        assert started.wait(10), 'the synthetic WB worker must be held inside the real background route'

        receipt.wait_for(state='visible')
        expect(receipt).to_have_attribute('data-wb-create-state', 'accepted')
        with page.expect_response(
            lambda response: urlsplit(response.url).path == wb_create_result_path
            and response.request.method == 'GET',
        ) as active_read:
            page.locator('[data-wb-create-reconcile]').click()
        active = active_read.value
        assert active.status == 200
        active_payload = active.json()
        assert active_payload['success'] is True and active_payload['replayed'] is True
        assert active_payload['product_ids'] == selected and active_payload['total'] == len(selected)
        assert active_payload['status'] == 'running' and active_payload['worker_exited'] is False
        assert active_payload['cancel_requested'] is False
        assert active_payload['result_counts'] is None
        assert not urlsplit(active.url).query
        assert state['wb_reads'][-1]['key'] == state['wb_posts'][0]['key']

        wb_button = page.locator('[data-supplier-import-next-action="push_wb"]')
        ozon_button = page.locator('[data-supplier-import-next-action="ozon"]')
        assert wb_button.count() == 0, 'the unresolved WB set must not offer another WB action'
        assert ozon_button.count() == 1 and ozon_button.is_enabled(), (
            'local Ozon preparation must remain available for the exact unresolved WB selection',
        )

        overlap_key = secrets.token_hex(16)
        overlap = page.evaluate("""async ({ ids, key, csrf }) => {
            const response = await fetch('/my-products/push-to-wb-bulk', {
                method: 'POST', credentials: 'same-origin', cache: 'no-store',
                headers: {
                    'Content-Type': 'application/json', Accept: 'application/json',
                    'X-CSRFToken': csrf,
                },
                body: JSON.stringify({ product_ids: ids, request_key: key }),
            });
            return { status: response.status, payload: await response.json() };
        }""", {
            'ids': selected,
            'key': overlap_key,
            'csrf': page.locator('meta[name="csrf-token"]').get_attribute('content'),
        })
        assert overlap['status'] == 409
        assert overlap['payload'].get('success') is False
        assert overlap['payload'].get('accepted') is False
        assert overlap['payload'].get('code') == 'wb_create_overlap'
        assert len(state['wb_posts']) == 3 and state['wb_posts'][2]['status'] == 409
        assert state['wb_posts'][2]['code'] == 'wb_create_overlap'
        assert state['wb_posts'][2]['key'] != state['wb_posts'][0]['key']
        assert state['wb_worker_product_ids'] == [selected]
        assert page.evaluate("document.querySelector('#my-products-app')._vnode.component.proxy.selected") == selected
        assert not navigations and page.evaluate("window.supplierProofDocument === 'first'")
        screenshot_name = 'wb-create-selection-continuity-light-1440.png'
        page.screenshot(path=artifacts / screenshot_name)

        first = check_inline_ozon_preparation(
            page, selected, navigations, account_id=first_account_id,
            account_label=first_account_label, choose_account=True,
        )
        assert receipt.is_visible() and receipt.get_attribute('data-wb-create-state') == 'accepted'
        assert page.evaluate("document.querySelector('#my-products-app')._vnode.component.proxy.selected") == selected

        release.set()
        assert importer_returned.wait(10), 'the synthetic WB importer should return after release'
        page.wait_for_function("""() => {
            const proxy = document.querySelector('#my-products-app')?._vnode?.component?.proxy;
            return proxy && (proxy.wbCreatePending === null ||
                (proxy.wbCreatePending.state === 'unknown' &&
                    !proxy.wbCreateBusy && !proxy.wbCreateReading));
        }""", timeout=15000)
        terminal_payload = None
        terminal_read_attempts = 0
        for _ in range(4):
            pending = page.evaluate(
                "document.querySelector('#my-products-app')._vnode.component.proxy.wbCreatePending !== null",
            )
            if not pending:
                break
            reconcile = page.locator('[data-wb-create-reconcile]')
            expect(reconcile).to_be_enabled()
            with page.expect_response(
                lambda response: urlsplit(response.url).path == wb_create_result_path
                and response.request.method == 'GET',
            ) as final_read:
                reconcile.click()
            terminal_read_attempts += 1
            payload = final_read.value.json()
            if (
                final_read.value.status == 200
                and payload.get('worker_exited') is True
                and payload.get('status') in {'completed', 'failed', 'cancelled'}
            ):
                terminal_payload = payload
                break
            assert final_read.value.status == 200
            assert payload.get('product_ids') == selected
            assert payload.get('worker_exited') is False
        if terminal_payload is None:
            observed_terminal = [
                entry['receipt'] for entry in state['wb_reads']
                if entry['status'] == 200
                and entry['receipt'].get('worker_exited') is True
                and entry['receipt'].get('status') in {'completed', 'failed', 'cancelled'}
            ]
            assert observed_terminal
            terminal_payload = observed_terminal[-1]
        assert terminal_payload is not None, 'terminal status must come from a typed receipt GET'
        page.wait_for_function("""expected => {
            const proxy = document.querySelector('#my-products-app')?._vnode?.component?.proxy;
            return proxy && proxy.wbCreatePending === null &&
                proxy.importBatchSelectionVerified &&
                proxy.selected.length === expected.length &&
                proxy.selected.every((id, index) => id === expected[index]);
        }""", arg=selected, timeout=15000)

        terminal_reads = [
            entry for entry in state['wb_reads']
            if entry['status'] == 200
            and entry['receipt'].get('worker_exited') is True
            and entry['receipt'].get('status') in {'completed', 'failed', 'cancelled'}
        ]
        assert terminal_reads and terminal_reads[-1]['key'] == state['wb_posts'][0]['key']
        terminal = terminal_reads[-1]['receipt']
        assert terminal['product_ids'] == selected and terminal['total'] == len(selected)
        assert terminal['status'] == 'completed' and terminal['worker_exited'] is True
        assert terminal['replayed'] is True and terminal['cancel_requested'] is False
        assert terminal['result_counts'] == {'processed': 1, 'succeeded': 0, 'failed': 1}
        assert terminal_payload['job_uid'] == terminal['job_uid']
        terminal_result = page.locator(
            '[data-wb-create-receipt][data-wb-create-state="terminal_result"]',
        )
        expect(terminal_result).to_be_visible()
        expect(terminal_result).to_contain_text('не созданы: 1')
        terminal_copy = terminal_result.inner_text()
        assert not re.search(r'успешно:\s*[1-9]\d*', terminal_copy, flags=re.IGNORECASE)
        assert 'Готово' not in terminal_copy
        assert page.locator('.shk-job').count() == 0
        terminal_view = page.evaluate("""() => {
            const proxy = document.querySelector('#my-products-app')._vnode.component.proxy;
            return proxy.wbCreateTerminalResult;
        }""")
        assert terminal_view == {
            'status': 'completed', 'total': 1,
            'counts': {'processed': 1, 'succeeded': 0, 'failed': 1},
        }
        assert state['wb_worker_errors'] == []
        assert state['wb_worker_product_ids'] == [selected]
        assert state['wb_posts'][-1]['status'] == 409 and len(state['wb_posts']) == 3
        expect(page.locator('[data-wb-create-receipt]')).to_have_attribute(
            'data-wb-create-state', 'terminal_result',
        )
        assert page.locator(
            '[data-wb-create-reconcile], [data-wb-create-retry], [data-wb-create-verify-feed]',
        ).count() == 0
        assert page.evaluate("document.querySelector('#my-products-app')._vnode.component.proxy.selected") == selected

        signed_refreshes = state['wb_feed_reads'][feed_reads_before:]
        assert signed_refreshes, 'verified terminal WB receipt must refresh the exact signed feed'
        assert all(read['query_keys'] == ('import_batch',) for read in signed_refreshes)
        assert all(read['status'] == 200 for read in signed_refreshes)
        assert signed_refreshes[-1]['ids'] == selected
        assert signed_refreshes[-1]['batch_count'] == len(selected)
        terminal_screenshot = 'wb-create-terminal-failure-light-1440.png'
        page.screenshot(path=artifacts / terminal_screenshot)
        second = check_inline_ozon_preparation(
            page, selected, navigations, account_id=second_account_id,
            account_label=second_account_label, choose_account=True,
            status_mode='mismatched_account',
        )
        assert first != second
        assert len(state['wb_posts']) == 3 and state['wb_worker_product_ids'] == [selected]
        assert not navigations and page.evaluate("window.supplierProofDocument === 'first'")
        return {
            'selected_count': len(selected),
            'exact_signed_ids_preserved': True,
            'lost_post_recovered_by_typed_get_before_retry': True,
            'typed_absence_get_count_before_retry': len(absent_reads),
            'explicit_same_key_retry_count': 1,
            'overlap_rejected_without_new_worker': True,
            'ozon_available_while_wb_unresolved': True,
            'two_ozon_accounts_used_same_selection': True,
            'terminal_receipt_requires_worker_exit': True,
            'terminal_receipt_get_count': len(terminal_reads),
            'explicit_terminal_read_count': terminal_read_attempts,
            'terminal_refresh_restored_exact_signed_feed': True,
            'screenshot': screenshot_name,
            'terminal_screenshot': terminal_screenshot,
            'wb_keyed_post_count_including_overlap': len(state['wb_posts']),
            'wb_worker_invocation_count': len(state['wb_worker_product_ids']),
            'wb_terminal_status': terminal['status'],
            'wb_result_counts': terminal['result_counts'],
            'wb_terminal_view_reports_no_success': True,
        }
    finally:
        release.set()


def run():
    identity = supplier.seed_supplier_products()
    with fixture.app.app_context():
        alternate = SellerSupplier.query.filter_by(seller_id=fixture.FIXTURE['seller_id']).filter(
            SellerSupplier.supplier_id != identity['supplier_id'],
        ).one()
        alternate_id = alternate.id
        alternate.is_active = False
        db.session.commit()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path='/usr/bin/chromium',
                args=['--no-sandbox', '--disable-dev-shm-usage'])
            context, page, panel, navigations = page_for(browser, width=320, theme='dark')
            search = search_for(panel, page, 'R28 Mass batch item')
            assert search['total'] == 53 and len(search['items']) == 50
            check_filter_invalidation(page, panel, search)
            search = search_for(panel, page, 'R28 Mass batch item')
            assert not navigations and not state['posts']
            with page.expect_response(lambda response:
                    parse_qs(urlsplit(response.url).query).get('select_all') == ['1']) as chosen:
                panel.locator('[data-supplier-import-select-filtered]').click()
            complete = chosen.value.json()
            assert len(complete['selection']) == 53
            assert set(complete['selection']) == set(identity['eligible_ids'])
            same_page_id = next(row['id'] for row in search['items'] if row['importable'])
            checkbox(panel, same_page_id).uncheck()
            exact = [value for value in complete['selection'] if value != same_page_id]
            page.screenshot(path=artifacts / 'choose-source-dark-320.png')
            added = click_import_and_wait(page, panel, {200, 201})
            assert added['imported'] == 52 and not added['skipped'] and not added['errors']
            assert len(state['posts']) == 1 and state['posts'][0]['source_ids'] == exact
            selected = confirm_exact_result(page, exact, navigations)
            page.screenshot(path=artifacts / 'exact-batch-dark-320.png')
            channel_actions = check_channel_actions(page, 52, navigations)
            page.screenshot(path=artifacts / 'exact-batch-marketplaces-dark-320.png')
            check_inline_ozon_preparation(
                page, selected, navigations,
                account_id=fixture.FIXTURE['account_id'],
                account_label=fixture.OZON_ACCOUNT_LABEL,
                status_mode='mixed',
            )
            context.close()

            with fixture.app.app_context():
                db.session.get(SellerSupplier, alternate_id).is_active = True
                first_store = db.session.get(SellerMarketplaceAccount, fixture.FIXTURE['account_id'])
                second_store = SellerMarketplaceAccount(
                    seller_id=first_store.seller_id, marketplace_id=first_store.marketplace_id,
                    external_account_id='100056', label='Ozon CI second shop', is_active=True,
                    connection_status='connected',
                )
                second_store.set_credentials({'client_id': '100056', 'api_key': 'synthetic-no-provider-key'})
                db.session.add(second_store)
                db.session.commit()
                second_shop_id, second_shop_label = second_store.id, second_store.label
            context, page, panel, navigations = page_for(
                browser, width=1440, theme='light', supplier_id=identity['supplier_id'],
            )
            result = search_for(panel, page, 'R28 Oversize batch item 200')
            assert len(result['items']) == 1
            row_id = result['items'][0]['id']
            checkbox(panel, row_id).check()
            state['mode'] = 'drop_after'
            panel.locator('[data-supplier-import-submit]').click()
            panel.locator('[data-supplier-import-pending]').wait_for()
            expect(panel.locator('[data-supplier-import-reconcile]')).to_be_enabled()
            expect(panel.locator('[data-supplier-import-pending]')).to_have_attribute(
                'data-supplier-import-state', 'unknown',
            )
            assert state['posts'][-1]['confirmed_committed'] == 1
            assert len(state['posts']) == 2
            with page.expect_response(lambda response: urlsplit(response.url).path == receipt_path) as rec:
                panel.locator('[data-supplier-import-reconcile]').click()
            assert rec.value.status == 200
            selected = confirm_exact_result(page, [row_id], navigations)
            assert len(state['posts']) == 2 and state['receipts'][-1]['key'] == state['posts'][-1]['key']
            wb_continuity = check_wb_create_selection_continuity(
                page, selected, navigations,
                first_account_id=fixture.FIXTURE['account_id'],
                first_account_label=fixture.OZON_ACCOUNT_LABEL,
                second_account_id=second_shop_id,
                second_account_label=second_shop_label,
            )
            assert len(state['prepares']) == 3
            page.screenshot(path=artifacts / 'ozon-second-shop-same-selection-light-1440.png')
            context.close()

            context, page, panel, navigations = page_for(
                browser, width=390, theme='light', supplier_id=identity['supplier_id'],
            )
            result = search_for(panel, page, 'R28 Oversize batch item 199')
            assert len(result['items']) == 1
            retry_id = result['items'][0]['id']
            checkbox(panel, retry_id).check()
            state['mode'] = 'drop_before'
            panel.locator('[data-supplier-import-submit]').click()
            panel.locator('[data-supplier-import-pending]').wait_for()
            expect(panel.locator('[data-supplier-import-reconcile]')).to_be_enabled()
            expect(panel.locator('[data-supplier-import-pending]')).to_have_attribute(
                'data-supplier-import-state', 'unknown',
            )
            assert len(state['posts']) == 3 and not state['posts'][-1]['status']
            with page.expect_response(lambda response: urlsplit(response.url).path == receipt_path) as rec:
                panel.locator('[data-supplier-import-reconcile]').click()
            assert rec.value.status == 404
            assert len(state['posts']) == 3 and not navigations
            with page.expect_response(lambda response: urlsplit(response.url).path == post_path) as retry:
                panel.locator('[data-supplier-import-retry]').click()
            assert retry.value.status in {200, 201}
            confirm_exact_result(page, [retry_id], navigations)
            assert len(state['posts']) == 4 and state['posts'][-1]['key'] == state['posts'][-2]['key']
            assert state['posts'][-1]['source_ids'] == state['posts'][-2]['source_ids'] == [retry_id]
            context.close()
            browser.close()

        with fixture.app.app_context():
            receipts = BackgroundJob.query.filter_by(
                seller_id=fixture.FIXTURE['seller_id'],
                job_type=BackgroundJob.SUPPLIER_IMPORT_RECEIPT_TYPE,
            ).all()
            assert len(receipts) == 3 and all(job.status == 'completed' for job in receipts)
            assert all(job.to_dict()['result'] == {} for job in receipts)
        assert not state['errors'], state['errors']
        assert not fixture.REPORT['unexpected_http'], fixture.REPORT['unexpected_http']
        assert not fixture.REPORT['unexpected_external_requests']
        assert fixture.REPORT['provider_attempts'] == 0
        mixed_status_uids = {
            row['uid'] for row in state['prepare_status_reads']
            if row['total'] == 52
        }
        mixed_status_reads = {
            uid: [row for row in state['prepare_status_reads'] if row['uid'] == uid]
            for uid in mixed_status_uids
        }
        report = {
            'status': 'passed', 'vue': '3.5.46', 'workspaces': [320, 390, 1440],
            'navigation_count_since_open': 0, 'source_rows_in_full_selection': 53,
            'initial_batch_count': 52, 'selected_scope_matches_committed_batch': True,
            'next_create_actions': channel_actions, 'local_commits': 3,
            'exact_inline_ozon_preparation': True,
            'inline_ozon_live_status': {
                'running_to_mixed_terminal': any(
                    len(rows) == 2
                    and rows[0]['delivered_status'] == 'running'
                    and rows[1]['delivered_status'] == 'partial'
                    and rows[1]['prepared'] == 51
                    and rows[1]['needs_input'] == 1
                    and rows[1]['total'] == 52
                    for rows in mixed_status_reads.values()
                ),
                'exact_mixed_prepared_needs_input': any(
                    row['delivered_status'] == 'partial'
                    and row['prepared'] == 51 and row['needs_input'] == 1
                    and row['summary_version'] == 3 and row['total'] == 52
                    for row in state['prepare_status_reads']
                ),
                'mismatched_account_status_rejected': any(
                    row['delivered_account_id'] != row['account_id']
                    for row in state['prepare_status_reads']
                ),
                'exact_uid_read_count': len(state['prepare_status_reads']),
                'prepare_posts': len(state['prepares']),
            },
            'same_selection_submitted_to_two_explicit_ozon_shops': True,
            'wb_create_selection_continuity': wb_continuity,
            'explicit_supplier_source_only_when_multiple': True,
            'one_initial_catalog_fetch': True,
            'inflight_filter_change_clears_selection_before_resubmission': True,
            'lost_response_recovered_read_only': True, 'missing_receipt_explicit_same_key_retry': True,
            'provider_attempts': 0,
        }
        (artifacts / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        print(json.dumps(report, ensure_ascii=False), flush=True)
    except Exception:
        print(json.dumps({
            'catalog_reads': len(state['catalog']),
            'import_posts': [entry['status'] for entry in state['posts']],
            'import_receipt_reads': [entry['status'] for entry in state['receipts']],
            'ozon_preparations': len(state['prepares']),
            'wb_create_posts': [
                {'status': entry['status'], 'code': entry['code'], 'ids': entry['ids'],
                 'transport_lost': entry['transport_lost']}
                for entry in state['wb_posts']
            ],
            'wb_receipt_reads': [
                {'status': entry['status'], 'receipt': entry['receipt']}
                for entry in state['wb_reads']
            ],
            'wb_worker_product_ids': state['wb_worker_product_ids'],
            'wb_worker_errors': state['wb_worker_errors'],
            'page_errors': state['errors'],
            'unexpected_http': fixture.REPORT['unexpected_http'],
            'unexpected_external_requests': fixture.REPORT['unexpected_external_requests'],
        }, ensure_ascii=False), flush=True)
        raise
    finally:
        release_event = state.get('wb_worker_release_event')
        started_event = state.get('wb_worker_started_event')
        returned_event = state.get('wb_worker_returned_event')
        if release_event:
            release_event.set()
        if any(entry['status'] == 202 for entry in state['wb_posts']):
            if started_event:
                started_event.wait(20)
            if returned_event:
                returned_event.wait(20)
        importer_class = state.get('wb_importer_class')
        original_importer = state.get('wb_importer_original')
        if importer_class is not None and original_importer is not None:
            importer_class.batch_import_products_to_wb = original_importer
        fixture.server.shutdown()
        fixture.TEMP.cleanup()


if __name__ == '__main__':
    run()
