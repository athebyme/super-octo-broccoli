"""Node-only regression for the exact Ozon handoff after supplier refresh."""

from pathlib import Path
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[1]


NODE_CASES = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

let component;
const account = {id: 55, label: 'Shop East'};
const boot = {
  wbConnected: true,
  ozonEnabled: true,
  ozonAccounts: [account],
  urls: {
    refreshFromSupplier: '/my-products/refresh-from-supplier',
    commonContent: '/my-products/common-content',
    enrichBulk: '/products/enrich-bulk',
  },
};
const fakeWindow = {
  shVue: {
    bootstrapData() { return boot; },
    mount(_selector, value) { component = value; return value; },
  },
  crypto: {randomUUID() { return 'a'.repeat(32); }},
};
const stored = new Map();
const submittedForms = [];
const fakeDocument = {
  activeElement: null,
  querySelector(selector) {
    assert.equal(selector, 'meta[name="csrf-token"]');
    return {content: 'csrf-test-token'};
  },
  createElement(tagName) {
    return {
      tagName,
      children: [],
      appendChild(child) { this.children.push(child); },
      submit() { this.submitted = true; },
    };
  },
  body: {appendChild(form) { submittedForms.push(form); }},
};
const context = {
  window: fakeWindow,
  document: fakeDocument,
  sessionStorage: {
    getItem(key) { return stored.has(key) ? stored.get(key) : null; },
    setItem(key, value) { stored.set(key, value); },
    removeItem(key) { stored.delete(key); },
  },
  AbortController,
  URL,
  URLSearchParams,
  setTimeout,
  clearTimeout,
  console,
};
vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), context);
assert.ok(component && component.methods);

function sameJson(left, right) {
  assert.deepEqual(JSON.parse(JSON.stringify(left)), JSON.parse(JSON.stringify(right)));
}

function makeApp() {
  const app = Object.assign(component.data(), component.methods);
  const refs = {
    account: {focused: false, focus() { this.focused = true; }},
    error: {focused: false, focus() { this.focused = true; }},
    result: {focused: false, focus() { this.focused = true; }},
    dialog: {
      open: false,
      showModal() { this.open = true; },
      close() { this.open = false; },
    },
  };
  app.$refs = {
    ozonAccount: refs.account,
    ozonError: refs.error,
    supplierRefreshResult: refs.result,
    ozonDialog: refs.dialog,
  };
  app.$nextTick = function (callback) { callback.call(this); };
  app.$fmt = {plural() { return 'товаров'; }};
  app.$api = {post() { return Promise.resolve({success: true}); }};
  app.load = function () {};
  app.loadFacets = function () {};
  app.feedScopeKey = function () { return 'current'; };
  app.loading = false;
  app.itemsScope = 'current';
  app.searchInput = '';
  app.filters.search = '';
  Object.entries(component.computed).forEach(([name, getter]) => {
    Object.defineProperty(app, name, {get() { return getter.call(app); }});
  });
  return {app, refs};
}

function setSupplierOutcome(app, data, ids) {
  app.selected = ids.slice();
  app.confirm = {
    open: true,
    busy: false,
    key: 'refresh_supplier',
    productIds: ids.slice(),
    selectionIds: ids.slice(),
    labels: ids.map(id => ({productId: id, label: 'Product ' + id})),
  };
  app.$api.post = function (url, body) {
    assert.equal(url, '/my-products/refresh-from-supplier');
    sameJson(body.product_ids, ids);
    return Promise.resolve(data);
  };
}

async function finishSupplierRefresh(data, ids) {
  const {app, refs} = makeApp();
  setSupplierOutcome(app, data, ids);
  app.runAction();
  await new Promise(resolve => setImmediate(resolve));
  return {app, refs};
}

(async () => {
  const ids = [101, 202, 303];
  const {app, refs} = makeApp();
  setSupplierOutcome(app, {
    success: true,
    status: 'completed_with_errors',
    requested: 3,
    updated: 1,
    skipped: 1,
    errors: 1,
    wb_ready: 1,
    wb_targets: [{imported_product_id: 101, product_id: 9001}],
    items: [
      {product_id: 303, status: 'error', reason: 'update_failed'},
      {product_id: 101, status: 'updated', reason: null},
      {product_id: 202, status: 'skipped', reason: 'no_supplier_link'},
    ],
  }, ids);
  app.runAction();
  await new Promise(resolve => setImmediate(resolve));

  assert.equal(app.supplierRefreshResult.kind, 'partial');
  sameJson(app.supplierRefreshOzonIds, [101]);
  sameJson(app.supplierRefreshWbProductIds, [9001]);
  sameJson(app.selected, []);

  // Both result actions use their exact verified subsets and leave the general
  // selection unchanged. The WB handoff uses the existing CSRF form.
  app.selected = [999];
  assert.equal(app.checkRefreshedWbCharacteristics(), true);
  assert.equal(submittedForms.length, 1);
  assert.equal(submittedForms[0].method, 'post');
  assert.equal(submittedForms[0].action, '/products/enrich-bulk');
  assert.equal(submittedForms[0].submitted, true);
  sameJson(submittedForms[0].children.map(input => [input.name, input.value]), [
    ['csrf_token', 'csrf-test-token'],
    ['selected_ids', '9001'],
    ['preset', 'characteristics'],
  ]);
  sameJson(app.selected, [999]);

  // The ordinary enrichment action still omits the preset and retains its
  // existing photos-first behavior.
  assert.equal(app.postSelectionForm('/products/enrich-bulk', [7001]), true);
  assert.equal(submittedForms.length, 2);
  sameJson(submittedForms[1].children.map(input => [input.name, input.value]), [
    ['csrf_token', 'csrf-test-token'],
    ['selected_ids', '7001'],
  ]);

  const trigger = {isConnected: true, focused: false, focus() { this.focused = true; }};
  assert.equal(app.prepareRefreshedForOzon({currentTarget: trigger}), true);
  sameJson(app.ozonIds, [101]);
  sameJson(app.selected, [999]);

  const multiIds = [101, 202, 303, 404];
  const multiBase = {
    success: true,
    status: 'completed_with_errors',
    requested: 4,
    updated: 2,
    skipped: 1,
    errors: 1,
    items: [
      {product_id: 404, status: 'error', reason: 'update_failed'},
      {product_id: 303, status: 'updated', reason: null},
      {product_id: 202, status: 'skipped', reason: 'no_supplier_link'},
      {product_id: 101, status: 'updated', reason: null},
    ],
  };
  const wbTarget = (imported, product) => ({imported_product_id: imported, product_id: product});
  const exactSubset = await finishSupplierRefresh({
    ...multiBase,
    wb_ready: 2,
    wb_targets: [wbTarget(101, 9101), wbTarget(303, 9303)],
  }, multiIds);
  assert.equal(exactSubset.app.supplierRefreshResult.kind, 'partial');
  sameJson(exactSubset.app.supplierRefreshOzonIds, [101, 303]);
  sameJson(exactSubset.app.supplierRefreshWbProductIds, [9101, 9303]);
  assert.equal(exactSubset.app.checkRefreshedWbCharacteristics(), true);
  const exactSubsetForm = submittedForms[submittedForms.length - 1];
  sameJson(exactSubsetForm.children.map(input => [input.name, input.value]), [
    ['csrf_token', 'csrf-test-token'],
    ['selected_ids', '9101'],
    ['selected_ids', '9303'],
    ['preset', 'characteristics'],
  ]);

  const malformedExtensions = [
    {wb_ready: 1, wb_targets: [wbTarget(101, 9101), wbTarget(303, 9303)]},
    {wb_ready: 1, wb_targets: [wbTarget(202, 9202)]}, // skipped row
    {wb_ready: 1, wb_targets: [wbTarget(404, 9404)]}, // error row
    {wb_ready: 1, wb_targets: [wbTarget(999, 9900)]}, // foreign row
    {wb_ready: 2, wb_targets: [wbTarget(303, 9303), wbTarget(101, 9101)]}, // wrong order
    {wb_ready: 2, wb_targets: [wbTarget(101, 9101), wbTarget(303, 9101)]}, // duplicate WB ID
    {wb_ready: 1, wb_targets: [wbTarget(101, 0)]},
    {wb_ready: 1, wb_targets: [{...wbTarget(101, 9101), extra: true}]},
    {}, // legacy response without the WB extension
  ];
  for (const extension of malformedExtensions) {
    const {app: wbApp} = await finishSupplierRefresh(
      Object.assign({}, multiBase, extension), multiIds,
    );
    assert.equal(wbApp.supplierRefreshResult.kind, 'partial');
    sameJson(wbApp.supplierRefreshOzonIds, [101, 303]);
    sameJson(wbApp.supplierRefreshWbProductIds, []);
    const formCount = submittedForms.length;
    assert.equal(wbApp.checkRefreshedWbCharacteristics(), false);
    assert.equal(submittedForms.length, formCount);
  }

  const disconnected = await finishSupplierRefresh({
    ...multiBase,
    wb_ready: 1,
    wb_targets: [{imported_product_id: 101, product_id: 9101}],
  }, multiIds);
  disconnected.app.wbConnected = false;
  sameJson(disconnected.app.supplierRefreshWbProductIds, []);
  assert.equal(disconnected.app.checkRefreshedWbCharacteristics(), false,
    'WB handoff is unavailable without a connected cabinet');
  assert.equal(app.ozonAccountId, String(account.id));
  assert.equal(app.ozonConfirmed, false);
  assert.equal(refs.dialog.open, true);
  assert.equal(refs.account.focused, true);
  assert.equal(trigger.focused, false);
  app.closeOzonDialog();
  assert.equal(refs.dialog.open, false);
  assert.equal(trigger.focused, true, 'dismissal returns focus to the result CTA');
  sameJson(app.selected, [999]);

  for (const result of [
    {kind: 'unknown', entries: [{productId: 101, status: 'updated'}], updated: 1},
    {kind: 'rejected', entries: [{productId: 101, status: 'updated'}], updated: 1},
    {kind: 'complete', requested: 1, updated: 0, skipped: 1, errors: 0,
      entries: [{productId: 202, status: 'skipped'}]},
    {kind: 'complete', requested: 2, updated: 2, skipped: 0, errors: 0,
      entries: [{productId: 101, status: 'updated'}, {productId: 202, status: 'skipped'}]},
  ]) {
    const blocked = makeApp();
    blocked.app.supplierRefreshResult = result;
    assert.equal(blocked.app.prepareRefreshedForOzon(), undefined);
    assert.equal(blocked.refs.dialog.open, false);
    sameJson(blocked.app.ozonIds, []);
  }

  const disabled = makeApp();
  disabled.app.ozonPending = {account_id: account.id, imported_product_ids: [101], request_key: 'a'.repeat(24)};
  assert.equal(disabled.app.ozonPreparationDisabled, true);
  assert.equal(disabled.app.openOzonPreparation([101]), false);
  assert.equal(disabled.refs.dialog.open, false);

  const unavailable = makeApp();
  unavailable.app.ozonEnabled = false;
  unavailable.app.supplierRefreshResult = {
    kind: 'complete', requested: 1, updated: 1, skipped: 0, errors: 0,
    entries: [{productId: 101, status: 'updated'}],
  };
  assert.equal(unavailable.app.prepareRefreshedForOzon(), false);
  assert.equal(unavailable.refs.dialog.open, false);

  const ordinary = makeApp();
  ordinary.app.items = [{id: 101}, {id: 202}];
  ordinary.app.selected = [202, 101];
  const selection = ordinary.app.selected.slice();
  assert.equal(ordinary.app.startAction({key: 'ozon'}), undefined);
  sameJson(ordinary.app.ozonIds, selection);
  sameJson(ordinary.app.selected, selection);
  assert.equal(ordinary.refs.dialog.open, true, 'the ordinary Ozon action still uses the shared dialog helper');

  console.log(JSON.stringify({status: 'passed', checks: [
    'only_exactly_verified_updated_ids_are_handed_to_the_existing_ozon_dialog',
    'wb_result_handoff_uses_exact_csrf_form_with_characteristics_preset',
    'wb_targets_are_exact_count_ordered_subset_of_updated_rows',
    'malformed_or_absent_wb_extension_preserves_local_and_ozon_result',
    'wb_handoff_requires_a_connected_cabinet',
    'ordinary_enrichment_form_keeps_photos_preset_default',
    'partial_success_keeps_skips_and_errors_out_of_handoff',
    'handoff_and_dismissal_leave_general_selection_unchanged_and_restore_focus',
    'unknown_rejected_zero_inconsistent_pending_and_disabled_states_do_not_open_the_dialog',
    'ordinary_ozon_action_reuses_shared_preparation_helper',
  ]}));
})().catch(error => { console.error(error.stack || String(error)); process.exitCode = 1; });
"""


def test_supplier_refresh_handoff_uses_only_verified_updated_ids():
    node = shutil.which("node")
    if not node:
        import pytest
        pytest.skip("Node.js is required for the isolated UI logic check")
    result = subprocess.run(
        [node, "-e", NODE_CASES, str(ROOT / "static" / "my-products-beta.js")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    assert '"status":"passed"' in result.stdout
