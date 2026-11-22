from email import policy
from email.parser import BytesParser
import json
from pathlib import Path
import re
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
state = {'mode': 'normal', 'catalog': [], 'posts': [], 'receipts': [], 'prepares': [], 'errors': []}


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
            state['prepares'].append({'ids': posted['imported_product_ids'], 'account_id': posted['account_id'],
                                      'location': response.headers['location']})
            route.fulfill(response=response)
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


def check_inline_ozon_preparation(page, selected, navigations, *, account_id, account_label, choose_account=False):
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
    assert 'ничего не отправлено' in receipt.inner_text().casefold()
    review = receipt.locator('[data-ozon-local-prepare-run]')
    expect(review).to_have_attribute('href', prepare['location'])
    assert not navigations and page.evaluate("window.supplierProofDocument === 'first'")
    assert page.evaluate("document.querySelector('#my-products-app')._vnode.component.proxy.selected") == selected
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
            check_inline_ozon_preparation(page, selected, navigations, account_id=fixture.FIXTURE['account_id'],
                                          account_label=fixture.OZON_ACCOUNT_LABEL)
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
            first = check_inline_ozon_preparation(page, selected, navigations, account_id=fixture.FIXTURE['account_id'],
                account_label=fixture.OZON_ACCOUNT_LABEL, choose_account=True)
            second = check_inline_ozon_preparation(page, selected, navigations, account_id=second_shop_id,
                account_label=second_shop_label, choose_account=True)
            assert first != second and len(state['prepares']) == 3
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
        report = {
            'status': 'passed', 'vue': '3.5.46', 'workspaces': [320, 390, 1440],
            'navigation_count_since_open': 0, 'source_rows_in_full_selection': 53,
            'initial_batch_count': 52, 'selected_scope_matches_committed_batch': True,
            'next_create_actions': channel_actions, 'local_commits': 3,
            'exact_inline_ozon_preparation': True,
            'same_selection_submitted_to_two_explicit_ozon_shops': True,
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
            'page_errors': state['errors'],
            'unexpected_http': fixture.REPORT['unexpected_http'],
            'unexpected_external_requests': fixture.REPORT['unexpected_external_requests'],
        }, ensure_ascii=False), flush=True)
        raise
    finally:
        fixture.server.shutdown()
        fixture.TEMP.cleanup()


if __name__ == '__main__':
    run()
