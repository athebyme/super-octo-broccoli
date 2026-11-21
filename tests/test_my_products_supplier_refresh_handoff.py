"""Node-only regressions for supplier refresh handoff and import batch scope."""

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
  importBatch: null,
  urls: {
    feed: '/api/my-products',
    workspace: '/my-products/beta',
    refreshFromSupplier: '/my-products/refresh-from-supplier',
    supplierRefreshPreview: '/api/my-products/supplier-refresh-preview',
    supplierRefreshResult: '/api/my-products/supplier-refresh-result',
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
  location: {search: '', hash: '', origin: 'https://seller.example'},
};
const stored = new Map();
const submittedForms = [];
const fetchCalls = [];
let fetchHandler = () => Promise.reject(new Error('unexpected fetch'));
const fakeFetch = (url, options) => {
  fetchCalls.push({url, options});
  return fetchHandler(url, options);
};
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
  fetch: fakeFetch,
  AbortController,
  URL,
  URLSearchParams,
  setTimeout,
  clearTimeout,
  console,
};
vm.createContext(context);
const sourceText = fs.readFileSync(process.argv[1], 'utf8');
vm.runInContext(sourceText, context);
assert.ok(component && component.methods);

function sameJson(left, right) {
  assert.deepEqual(JSON.parse(JSON.stringify(left)), JSON.parse(JSON.stringify(right)));
}

function jsonResponse(status, data) {
  return {
    status,
    ok: status >= 200 && status < 300,
    redirected: false,
    type: 'basic',
    headers: {get(name) { return name.toLowerCase() === 'content-type' ? 'application/json' : null; }},
    json() { return Promise.resolve(data); },
  };
}

function savedRefreshResponse() {
  return {
    success: true,
    status: 'completed_with_errors',
    requested: 4,
    updated: 2,
    skipped: 1,
    errors: 1,
    wb_ready: 1,
    wb_unavailable: 1,
    wb_targets: [{imported_product_id: 101, product_id: 9101}],
    items: [
      {product_id: 404, status: 'error', reason: 'update_failed'},
      {product_id: 303, status: 'updated', reason: null},
      {product_id: 202, status: 'skipped', reason: 'no_supplier_link'},
      {product_id: 101, status: 'updated', reason: null},
    ],
    labels: [101, 202, 303, 404].map(productId => ({productId, label: 'Product ' + productId})),
  };
}

function makeApp() {
  const app = Object.assign(component.data(), component.methods);
  const refs = {
    account: {focused: false, focus() { this.focused = true; }},
    error: {focused: false, focus() { this.focused = true; }},
    result: {
      focused: false,
      focusOptions: null,
      scrollOptions: null,
      focus(options) { this.focused = true; this.focusOptions = options || null; },
      scrollIntoView(options) { this.scrollOptions = options; },
    },
    resume: {
      focused: false,
      focusOptions: null,
      scrollOptions: null,
      focus(options) { this.focused = true; this.focusOptions = options || null; },
      scrollIntoView(options) { this.scrollOptions = options; },
    },
    dialog: {
      open: false,
      showModal() { this.open = true; },
      close() { this.open = false; },
    },
    supplierRefreshDialog: {
      open: false,
      showModal() { this.open = true; },
      close() { this.open = false; },
    },
    supplierRefreshCancel: {focused: false, focus() { this.focused = true; }},
    supplierRefreshRetry: {focused: false, focus() { this.focused = true; }},
  };
  app.$refs = {
    ozonAccount: refs.account,
    ozonError: refs.error,
    supplierRefreshResult: refs.result,
    supplierRefreshResume: refs.resume,
    supplierRefreshDialog: refs.supplierRefreshDialog,
    supplierRefreshCancel: refs.supplierRefreshCancel,
    supplierRefreshReviewRetry: refs.supplierRefreshRetry,
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

function supplierPreview(ids, overrides = {}) {
  const items = ids.map(product_id => ({
    product_id,
    label: 'Product ' + product_id,
    state: 'changed',
    changes: [{field: 'title', before: 'Old title', after: 'New title'}],
  }));
  return Object.assign({
    success: true,
    requested: ids.length,
    eligible: ids.length,
    changed: ids.length,
    skipped: 0,
    items,
    review_token: 'preview.review.token',
  }, overrides);
}

function setSupplierOutcome(app, data, ids) {
  app.selected = ids.slice();
  app._supplierRefreshDialogGeneration = 1;
  app.supplierRefreshReview = {
    generation: 1,
    phase: 'ready',
    error: '',
    requested: ids.length,
    eligible: ids.length,
    changed: ids.length,
    skipped: 0,
    items: supplierPreview(ids).items,
    reviewToken: 'preview.review.token',
    productIds: ids.slice(),
    selectionIds: ids.slice(),
    labels: ids.map(id => ({productId: id, label: 'Product ' + id})),
    scope: 'current',
  };
  app.$refs.supplierRefreshDialog.open = true;
  app.$api.post = function (url, body) {
    assert.equal(url, '/my-products/refresh-from-supplier');
    sameJson(body.product_ids, ids);
    assert.equal(body.review_token, 'preview.review.token');
    return Promise.resolve(data);
  };
}

function remount(importBatch, search) {
  boot.importBatch = importBatch;
  fakeWindow.location.search = search;
  vm.runInContext(sourceText, context);
  return component;
}

function makeFeedApp(definition, getResponse) {
  const app = Object.assign(definition.data(), definition.methods);
  const getCalls = [];
  app.$refs = {};
  app.$api = {
    get(url, params, options) {
      getCalls.push({
        url,
        params: params === undefined ? null : JSON.parse(JSON.stringify(params)),
        signal: !!options?.signal,
      });
      return typeof getResponse === 'function' ? getResponse(url, params) : Promise.resolve(getResponse);
    },
  };
  app.$nextTick = function (callback) { if (callback) callback.call(this); };
  Object.entries(definition.computed).forEach(([name, getter]) => {
    Object.defineProperty(app, name, {get() { return getter.call(app); }});
  });
  return {app, getCalls};
}

async function finishSupplierRefresh(data, ids) {
  const {app, refs} = makeApp();
  setSupplierOutcome(app, data, ids);
  app.confirmSupplierRefreshReview();
  await new Promise(resolve => setImmediate(resolve));
  return {app, refs};
}

(async () => {
  const previewSelection = [202, 101];
  const reviewed = makeApp();
  reviewed.app.items = [
    {id: 101, title: 'Product 101', import_status: 'validated'},
    {id: 202, title: 'Product 202', import_status: 'validated'},
  ];
  reviewed.app.selected = previewSelection.slice();
  const previewCalls = [];
  let applyCalls = 0;
  const reviewResponse = supplierPreview(previewSelection, {
    changed: 2,
    items: [
      {product_id: 202, label: 'Product 202', state: 'changed', changes: [
        {field: 'title', before: 'Old name', after: 'New name'},
        {field: 'purchase_price', before: '0', after: '19.90'},
        {field: 'supplier_quantity', before: 3, after: 0},
        {field: 'description', before: null, after: null},
      ]},
      {product_id: 101, label: 'Product 101', state: 'changed', changes: [
        {field: 'photos', before: null, after: null},
      ]},
    ],
  });
  reviewed.app.$api.post = function (url, body, options) {
    previewCalls.push({url, body, signal: !!options?.signal});
    if (url === '/api/my-products/supplier-refresh-preview') {
      sameJson(body.product_ids, previewSelection);
      assert.equal(previewCalls.length, 1);
      return Promise.resolve(reviewResponse);
    }
    assert.equal(url, '/my-products/refresh-from-supplier');
    applyCalls += 1;
    sameJson(body.product_ids, previewSelection);
    assert.equal(body.review_token, 'preview.review.token');
    return Promise.resolve({
      success: true,
      status: 'completed',
      requested: 2,
      updated: 2,
      skipped: 0,
      errors: 0,
      items: previewSelection.map(product_id => ({product_id, status: 'updated', reason: null})),
    });
  };
  reviewed.app.startAction({key: 'refresh_supplier'});
  assert.equal(reviewed.refs.supplierRefreshDialog.open, true,
    'the native preview dialog opens before the read-only preview completes');
  assert.equal(reviewed.app.supplierRefreshReview.phase, 'loading');
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(reviewed.app.supplierRefreshReview.phase, 'ready');
  assert.equal(previewCalls.length, 1, 'opening review performs one preview POST');
  assert.equal(previewCalls[0].url, '/api/my-products/supplier-refresh-preview');
  assert.equal(previewCalls[0].signal, true, 'preview read has an abort signal');
  assert.equal(applyCalls, 0, 'preview never applies without the explicit confirmation');
  assert.equal(reviewed.app.supplierRefreshReview.items[0].changes[1].label, 'Закупочная цена');
  assert.equal(reviewed.app.supplierRefreshReview.items[0].changes[2].label, 'Остаток у поставщика');
  assert.equal(reviewed.app.supplierRefreshReview.items[0].changes[3].groupOnly, true);
  assert.equal(reviewed.app.supplierRefreshValueLabel(0), '0', 'numeric zero remains visible');
  await reviewed.app.confirmSupplierRefreshReview();
  assert.equal(applyCalls, 1, 'the explicit confirm sends exactly one apply');
  assert.equal(reviewed.refs.supplierRefreshDialog.open, false);
  assert.equal(reviewed.app.supplierRefreshResult.kind, 'complete');

  const invalidPreviews = [
    supplierPreview([11, 22], {items: supplierPreview([22, 11]).items}),
    supplierPreview([11, 22], {eligible: 1}),
    supplierPreview([11, 22], {review_token: ''}),
    supplierPreview([11, 22], {items: [
      {product_id: 11, label: 'A', state: 'changed', changes: [{field: 'unknown', before: null, after: null}]},
      supplierPreview([11, 22]).items[1],
    ]}),
    supplierPreview([11, 22], {items: [
      {product_id: 11, label: 'A', state: 'changed', changes: [{field: 'photos', before: 'old', after: null}]},
      supplierPreview([11, 22]).items[1],
    ]}),
  ];
  for (const invalidPreview of invalidPreviews) {
    const malformed = makeApp();
    malformed.app.items = [{id: 11, title: 'A'}, {id: 22, title: 'B'}];
    malformed.app.selected = [11, 22];
    let malformedApplyCalls = 0;
    malformed.app.$api.post = function (url) {
      if (url === '/api/my-products/supplier-refresh-preview') return Promise.resolve(invalidPreview);
      malformedApplyCalls += 1;
      return Promise.resolve({});
    };
    malformed.app.startAction({key: 'refresh_supplier'});
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(malformed.app.supplierRefreshReview.phase, 'error');
    assert.equal(malformedApplyCalls, 0, 'malformed preview cannot reach the apply endpoint');
    assert.equal(malformed.refs.supplierRefreshRetry.focused, true,
      'an invalid preview directs focus to the read-only retry');
  }

  const unlinked = makeApp();
  unlinked.app.items = [{id: 31, title: 'No supplier'}, {id: 32, title: 'No supplier 2'}];
  unlinked.app.selected = [31, 32];
  let unlinkedApplyCalls = 0;
  unlinked.app.$api.post = function (url) {
    if (url === '/api/my-products/supplier-refresh-preview') {
      return Promise.resolve(supplierPreview([31, 32], {
        eligible: 0, changed: 0, skipped: 2,
        items: [31, 32].map(product_id => ({
          product_id, label: 'No supplier', state: 'unlinked', changes: [],
        })),
      }));
    }
    unlinkedApplyCalls += 1;
    return Promise.resolve({});
  };
  unlinked.app.startAction({key: 'refresh_supplier'});
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(unlinked.app.supplierRefreshReview.phase, 'ready');
  assert.equal(unlinked.app.supplierRefreshReview.eligible, 0);
  assert.equal(unlinked.app.confirmSupplierRefreshReview(), false);
  assert.equal(unlinkedApplyCalls, 0, 'all-unlinked review exposes no apply path');
  unlinked.app.cancelSupplierRefreshReview();
  assert.equal(unlinked.refs.supplierRefreshDialog.open, false);

  let resolveLatePreview;
  const cancelledPreview = makeApp();
  cancelledPreview.app.items = [{id: 41, title: 'A'}];
  cancelledPreview.app.selected = [41];
  cancelledPreview.app.$api.post = function () {
    return new Promise(resolve => { resolveLatePreview = resolve; });
  };
  cancelledPreview.app.startAction({key: 'refresh_supplier'});
  assert.equal(cancelledPreview.refs.supplierRefreshDialog.open, true);
  cancelledPreview.app.cancelSupplierRefreshReview();
  resolveLatePreview(supplierPreview([41]));
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(cancelledPreview.app.supplierRefreshReview, null,
    'a preview arriving after cancel cannot restore the dialog');
  assert.equal(cancelledPreview.app.supplierRefreshResult, null);

  let resolveScopePreview;
  const changedScopePreview = makeApp();
  changedScopePreview.app.items = [{id: 42, title: 'A'}];
  changedScopePreview.app.selected = [42];
  changedScopePreview.app.$api.post = function () {
    return new Promise(resolve => { resolveScopePreview = resolve; });
  };
  changedScopePreview.app.startAction({key: 'refresh_supplier'});
  component.watch.searchInput.call(changedScopePreview.app, 'new scope');
  assert.equal(changedScopePreview.app.supplierRefreshReview, null,
    'changing search scope closes and invalidates a pending review');
  resolveScopePreview(supplierPreview([42]));
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(changedScopePreview.app.supplierRefreshReview, null,
    'a late preview cannot restore after scope changes');

  const staleApply = makeApp();
  staleApply.app.selected = [51];
  staleApply.app._supplierRefreshDialogGeneration = 1;
  staleApply.app.supplierRefreshReview = {
    generation: 1, phase: 'ready', error: '', requested: 1, eligible: 1, changed: 1, skipped: 0,
    items: supplierPreview([51]).items, reviewToken: 'old.review.token',
    productIds: [51], selectionIds: [51], labels: [{productId: 51, label: 'Product 51'}], scope: 'current',
  };
  staleApply.refs.supplierRefreshDialog.open = true;
  let stalePreviewReads = 0;
  let staleApplyWrites = 0;
  staleApply.app.$api.post = function (url, body) {
    if (url === '/api/my-products/supplier-refresh-preview') {
      stalePreviewReads += 1;
      return Promise.resolve(supplierPreview([51], {review_token: 'new.review.token'}));
    }
    staleApplyWrites += 1;
    assert.equal(body.review_token, 'old.review.token');
    return Promise.reject(Object.assign(new Error('stale preview'), {
      status: 409, code: 'supplier_refresh_review_stale',
    }));
  };
  await staleApply.app.confirmSupplierRefreshReview();
  assert.equal(staleApply.app.supplierRefreshReview.phase, 'stale');
  assert.equal(staleApply.app.supplierRefreshReview.reviewToken, '');
  assert.equal(staleApply.app.supplierRefreshReview.error,
    'Данные товаров изменились. Проверьте обновление снова.');
  assert.equal(staleApplyWrites, 1);
  await staleApply.app.loadSupplierRefreshPreview();
  assert.equal(stalePreviewReads, 1, '409 offers a fresh preview read only');
  assert.equal(staleApplyWrites, 1, 'stale apply is never retried');
  assert.equal(staleApply.app.supplierRefreshReview.phase, 'ready');
  assert.equal(staleApply.app.supplierRefreshReview.reviewToken, 'new.review.token');

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
  app.confirmSupplierRefreshReview();
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
    ['from_supplier_refresh', '1'],
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
    ['from_supplier_refresh', '1'],
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

  const invalidWbCount = await finishSupplierRefresh({
    success: true,
    status: 'completed',
    requested: 1,
    updated: 1,
    skipped: 0,
    errors: 0,
    wb_ready: 1,
    wb_unavailable: 1,
    wb_targets: [wbTarget(101, 9101)],
    items: [{product_id: 101, status: 'updated', reason: null}],
  }, [101]);
  sameJson(invalidWbCount.app.supplierRefreshOzonIds, [101]);
  sameJson(invalidWbCount.app.supplierRefreshWbProductIds, []);
  assert.doesNotMatch(invalidWbCount.app.supplierRefreshResult.summary, /исходных ссылок WB/);

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

  const templateText = fs.readFileSync(require('node:path').join(
    require('node:path').dirname(process.argv[1]), '..', 'templates', 'my_products_beta.html',
  ), 'utf8');
  const commonTemplateText = fs.readFileSync(require('node:path').join(
    require('node:path').dirname(process.argv[1]), '..', 'templates', 'common_product_content.html',
  ), 'utf8');
  const stylesheetText = fs.readFileSync(require('node:path').join(
    require('node:path').dirname(process.argv[1]), 'my-products-beta.css',
  ), 'utf8');
  assert.match(templateText, /'supplierRefreshResult': url_for\('api_my_products_supplier_refresh_result'\)/);
  assert.match(templateText, /'supplierRefreshPreview': url_for\('api_my_products_supplier_refresh_preview'\)/);
  assert.match(templateText, /id="supplier-refresh-result"/);
  assert.match(templateText, /<dialog ref="supplierRefreshDialog" class="mp-supplier-refresh-dialog"/);
  assert.match(templateText, /aria-labelledby="mp-supplier-refresh-dialog-title"/);
  assert.match(templateText, /Данные на площадках не меняются/);
  assert.match(templateText, /filename='my-products-beta\.css', v='20261121-supplier-refresh-review-r40'/);
  assert.match(templateText, /filename='my-products-beta\.js', v='20261121-supplier-refresh-review-r40'/);
  assert.match(templateText, /filename='common-product-content\.js', v='20261121-price-workspace-return-r39'/);
  assert.match(commonTemplateText, /filename='common-product-content\.js', v='20261121-common-content-workspace-return-r36'/);
  assert.match(stylesheetText, /\.mp-refresh-result\s*\{[^}]*scroll-margin-top:\s*72px/s);

  const receiptKey = 'supplier-refresh-resume-v1';
  const previousReceipt = 'v1.previous.receipt';
  const nextReceipt = 'v1.next.receipt';
  stored.set(receiptKey, previousReceipt);
  const confirmOnly = makeApp();
  const priorResult = {kind: 'complete', entries: [], updated: 1};
  confirmOnly.app.supplierRefreshResult = priorResult;
  confirmOnly.app.items = [{id: 101, title: 'Product 101', import_status: 'validated'}];
  confirmOnly.app.selected = [101];
  confirmOnly.app.startAction({key: 'refresh_supplier'});
  assert.equal(confirmOnly.refs.supplierRefreshDialog.open, true);
  assert.equal(confirmOnly.app.supplierRefreshReview.phase, 'loading');
  assert.equal(stored.get(receiptKey), previousReceipt,
    'opening preview must retain the prior result receipt');
  assert.equal(confirmOnly.app.supplierRefreshResult, priorResult,
    'opening preview must retain the prior visible result');
  confirmOnly.app.cancelSupplierRefreshReview();
  assert.equal(stored.get(receiptKey), previousReceipt,
    'cancel before POST must retain the prior result receipt');
  assert.equal(confirmOnly.app.supplierRefreshResult, priorResult,
    'cancel before POST must retain the prior visible result');

  const postResult = makeApp();
  setSupplierOutcome(postResult.app, {
    success: true,
    status: 'completed',
    requested: 1,
    updated: 1,
    skipped: 0,
    errors: 0,
    resume_receipt: nextReceipt,
    items: [{product_id: 101, status: 'updated', reason: null}],
  }, [101]);
  stored.set(receiptKey, previousReceipt);
  postResult.app.confirmSupplierRefreshReview();
  assert.equal(stored.has(receiptKey), false,
    'starting the actual supplier POST clears the old receipt immediately');
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(stored.get(receiptKey), nextReceipt,
    'only the verified updated result receipt is stored, as a raw token');
  assert.equal(postResult.app.supplierRefreshResult.resumeWarning, undefined);

  const oversizedReceipt = makeApp();
  setSupplierOutcome(oversizedReceipt.app, {
    success: true,
    status: 'completed',
    requested: 1,
    updated: 1,
    skipped: 0,
    errors: 0,
    resume_receipt: 'x'.repeat(7501),
    items: [{product_id: 101, status: 'updated', reason: null}],
  }, [101]);
  oversizedReceipt.app.confirmSupplierRefreshReview();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(stored.has(receiptKey), false, 'oversized receipts are never persisted');
  assert.match(oversizedReceipt.app.supplierRefreshResult.resumeWarning, /нельзя будет восстановить/);

  const receipt = 'v1.saved.receipt_1';
  stored.set(receiptKey, receipt);
  fakeWindow.location.hash = '#supplier-refresh-result';
  fetchCalls.length = 0;
  fetchHandler = (url, options) => {
    assert.equal(url, '/api/my-products/supplier-refresh-result');
    assert.equal(options.method, 'GET');
    assert.equal(options.credentials, 'same-origin');
    assert.equal(options.cache, 'no-store');
    assert.equal(options.headers['X-Supplier-Refresh-Receipt'], receipt);
    assert.ok(options.signal);
    return Promise.resolve(jsonResponse(200, savedRefreshResponse()));
  };
  const restored = makeApp();
  restored.app._supplierRefreshReturnAnchor = true;
  restored.app.selected = [777];
  let restorePosts = 0;
  restored.app.$api.post = function () { restorePosts += 1; return Promise.resolve({}); };
  await restored.app.restoreSupplierRefreshResult();
  assert.equal(fetchCalls.length, 1, 'restoring one tab result performs one bounded GET');
  assert.equal(restored.app.supplierRefreshResult.saved, true);
  assert.equal(restored.app.supplierRefreshResult.title, 'Сохранённый итог обновления из поставщика');
  sameJson(restored.app.supplierRefreshOzonIds, [101, 303]);
  sameJson(restored.app.supplierRefreshWbProductIds, [9101]);
  sameJson(restored.app.supplierRefreshResult.entries.map(row => [row.productId, row.label]), [
    [101, 'Product 101'], [202, 'Product 202'], [303, 'Product 303'], [404, 'Product 404'],
  ]);
  assert.match(restored.app.supplierRefreshResult.summary, /Недоступны исходные ссылки WB: 1/);
  assert.equal(restored.refs.result.focused, true, 'return-anchor restoration moves focus to the result');
  sameJson(restored.refs.result.focusOptions, {preventScroll: true});
  sameJson(restored.refs.result.scrollOptions, {block: 'start', inline: 'nearest'});
  sameJson(restored.app.selected, [777], 'the ordinary catalog selection remains independent');
  assert.equal(restorePosts, 0, 'restoration never prepares or publishes');
  assert.equal(stored.get(receiptKey), receipt, 'a read does not consume the saved receipt');

  const foreignUrl = makeApp();
  foreignUrl.app.urls = Object.assign({}, foreignUrl.app.urls, {
    supplierRefreshResult: 'https://foreign.example/receipt',
  });
  const beforeForeignUrl = fetchCalls.length;
  await foreignUrl.app.restoreSupplierRefreshResult();
  assert.equal(fetchCalls.length, beforeForeignUrl, 'a foreign result URL is never requested');
  assert.equal(foreignUrl.app.supplierRefreshResumeRetry, false);

  const invalidLabels = makeApp();
  stored.set(receiptKey, receipt);
  invalidLabels.app._supplierRefreshReturnAnchor = true;
  fetchHandler = () => Promise.resolve(jsonResponse(200, {
    ...savedRefreshResponse(),
    labels: [{productId: 101, label: 'Product 101'}, {productId: 101, label: 'duplicate'}],
  }));
  await invalidLabels.app.restoreSupplierRefreshResult();
  assert.equal(invalidLabels.app.supplierRefreshResult, null);
  assert.equal(invalidLabels.app.supplierRefreshResumeRetry, true,
    'invalid server labels must leave a read-only retry available');
  assert.equal(stored.get(receiptKey), receipt,
    'a malformed transient read retains its receipt for read-only retry');

  const retryRead = makeApp();
  retryRead.app._supplierRefreshReturnAnchor = true;
  fetchHandler = () => Promise.reject(new TypeError('network unavailable'));
  await retryRead.app.restoreSupplierRefreshResult();
  assert.equal(retryRead.app.supplierRefreshResumeRetry, true);
  fetchHandler = () => Promise.resolve(jsonResponse(200, savedRefreshResponse()));
  await retryRead.app.restoreSupplierRefreshResult();
  assert.equal(retryRead.app.supplierRefreshResult.saved, true,
    'the retry repeats only the read and can recover the saved outcome');

  const expired = makeApp();
  expired.app._supplierRefreshReturnAnchor = true;
  fetchHandler = () => Promise.resolve(jsonResponse(410, {}));
  await expired.app.restoreSupplierRefreshResult();
  assert.equal(stored.has(receiptKey), false, 'an expired receipt is discarded');
  assert.match(expired.app.supplierRefreshResumeNote, /истёк или недоступен/);
  assert.equal(expired.app.supplierRefreshResumeRetry, false);
  assert.doesNotMatch(expired.app.supplierRefreshResumeNote, /повторите обновление/);

  let resolveStaleRead;
  stored.set(receiptKey, receipt);
  const hidden = makeApp();
  hidden.app._supplierRefreshReturnAnchor = true;
  fetchHandler = () => new Promise(resolve => { resolveStaleRead = resolve; });
  const staleRead = hidden.app.restoreSupplierRefreshResult();
  hidden.app.hideSupplierRefreshResult();
  assert.equal(stored.has(receiptKey), false, 'hiding clears the saved receipt');
  resolveStaleRead(jsonResponse(200, savedRefreshResponse()));
  await staleRead;
  assert.equal(hidden.app.supplierRefreshResult, null,
    'a response arriving after hide cannot restore a result');

  let resolveUnmountedRead;
  stored.set(receiptKey, receipt);
  const unmounted = makeApp();
  fetchHandler = () => new Promise(resolve => { resolveUnmountedRead = resolve; });
  const unmountedRead = unmounted.app.restoreSupplierRefreshResult();
  component.beforeUnmount.call(unmounted.app);
  resolveUnmountedRead(jsonResponse(200, savedRefreshResponse()));
  await unmountedRead;
  assert.equal(unmounted.app.supplierRefreshResult, null,
    'a response arriving after unmount cannot restore a result');

  stored.set(receiptKey, receipt);
  fakeWindow.location.hash = '';
  const batchReceiptDefinition = remount('batch-receipt', '?import_batch=batch-receipt');
  const batchRestore = makeFeedApp(batchReceiptDefinition, {success: true, items: [], pagination: {}});
  const fetchCountBeforeBatch = fetchCalls.length;
  batchReceiptDefinition.mounted.call(batchRestore.app);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(fetchCalls.length, fetchCountBeforeBatch,
    'import batch mode never restores an unrelated supplier receipt');
  assert.equal(stored.get(receiptKey), receipt,
    'import batch mode leaves the other tab result token untouched');
  boot.importBatch = null;

  const batchToken = 'supplier-import-receipt-7';
  const batchPage = {
    success: true,
    import_batch_count: 2,
    items: [{id: 101, title: 'Imported A'}, {id: 202, title: 'Imported B'}],
    pagination: {page: 1, per_page: 200, total: 2, pages: 1, has_next: false},
  };
  let batchDefinition = remount(batchToken, '?import_batch=' + encodeURIComponent(batchToken));
  let batch = makeFeedApp(batchDefinition, batchPage);
  assert.equal(batch.app.importBatchMode, true);
  assert.equal(batch.app.importBatchInvalid, false);
  sameJson(batch.app.selected, []);
  sameJson(batch.app.queryParams(99, 5), {import_batch: batchToken});
  batch.app.load();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(batch.getCalls.length, 1);
  assert.equal(batch.getCalls[0].url, '/api/my-products');
  sameJson(batch.getCalls[0].params, {import_batch: batchToken});
  sameJson(batch.app.items.map(item => item.id), [101, 202]);
  sameJson(batch.app.selected, []);
  batch.app.loadFacets();
  batch.app.loadMore();
  batch.app.selectAllFiltered();
  assert.equal(batch.getCalls.length, 1, 'batch mode does not fetch facets or another page');
  batch.app.toggleAll();
  sameJson(batch.app.selected, [101, 202]);
  batch.app.selected = batch.app.selected.filter(id => id !== 202);
  sameJson(batch.app.selected, [101], 'the seller can exclude a row from the exact batch');
  assert.equal(batch.app.feedScopeKey(), JSON.stringify({import_batch: batchToken}));

  for (const invalid of [
    {token: '', search: '?import_batch=', response: batchPage, reason: 'empty sentinel'},
    {token: null, search: '?import_batch=expired', response: batchPage, reason: 'missing bootstrap token'},
    {token: 'valid-receipt', search: '?import_batch=valid-receipt', response: {
      ...batchPage, import_batch_count: 3,
    }, reason: 'count mismatch'},
    {token: 'valid-receipt', search: '?import_batch=valid-receipt', response: {
      ...batchPage, import_batch_count: 0, items: [],
    }, reason: 'empty batch'},
    {token: 'valid-receipt', search: '?import_batch=valid-receipt', response: {
      ...batchPage, items: [{id: 101}, {id: 101}],
    }, reason: 'duplicate IDs'},
    {token: 'valid-receipt', search: '?import_batch=valid-receipt', response: {
      ...batchPage, pagination: {...batchPage.pagination, page: 2},
    }, reason: 'wrong page'},
    {token: 'valid-receipt', search: '?import_batch=valid-receipt', response: {
      ...batchPage, pagination: {...batchPage.pagination, has_next: true},
    }, reason: 'partial page'},
    {token: 'valid-receipt', search: '?import_batch=valid-receipt', response: {
      ...batchPage, pagination: {...batchPage.pagination, per_page: 50},
    }, reason: 'wrong page size'},
    {token: 'valid-receipt', search: '?import_batch=valid-receipt', response: {
      ...batchPage, pagination: {...batchPage.pagination, pages: 2},
    }, reason: 'multiple pages'},
    {token: 'x'.repeat(8193), search: '?import_batch=long', response: batchPage, reason: 'oversized token'},
  ]) {
    batchDefinition = remount(invalid.token, invalid.search);
    batch = makeFeedApp(batchDefinition, invalid.response);
    batch.app.items = [{id: 999, title: 'Stale row'}];
    batch.app.selected = [999];
    batch.app.load();
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(batch.app.importBatchMode, true, invalid.reason);
    sameJson(batch.app.items, [], invalid.reason + ' must not leave stale rows visible');
    sameJson(batch.app.selected, [], invalid.reason + ' must clear selection');
    assert.ok(batch.app.error, invalid.reason + ' must show a recovery error');
    if (invalid.token && invalid.token.length <= 8192) {
      assert.equal(batch.getCalls.length, 1, invalid.reason);
      sameJson(batch.getCalls[0].params, {import_batch: invalid.token});
    } else {
      assert.equal(batch.getCalls.length, 0, invalid.reason + ' must not query an unrestricted list');
    }
  }

  batchDefinition = remount('valid-receipt', '?import_batch=valid-receipt');
  batch = makeFeedApp(batchDefinition, () => Promise.reject(Object.assign(Error('gone'), {status: 404})));
  batch.app.items = [{id: 999, title: 'Stale row'}];
  batch.app.selected = [999];
  batch.app.load();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(batch.getCalls.length, 1);
  sameJson(batch.getCalls[0].params, {import_batch: 'valid-receipt'});
  sameJson(batch.app.items, [], 'expired receipt must not fall back to all products');
  sameJson(batch.app.selected, []);

  batch = makeFeedApp(batchDefinition, () => Promise.reject(Object.assign(
    Error('Срок действия набора импорта истёк'), {status: 410}
  )));
  batch.app.load();
  await new Promise(resolve => setImmediate(resolve));
  assert.match(batch.app.error, /Срок действия набора импорта истёк/);
  assert.match(batch.app.error, /Все мои товары/);

  const savedUrlSearchParams = context.URLSearchParams;
  context.URLSearchParams = undefined;
  batchDefinition = remount(null, '?import_batch=expired');
  context.URLSearchParams = savedUrlSearchParams;
  assert.equal(batchDefinition.data().importBatchMode, true, 'query fallback must fail closed without URLSearchParams');
  assert.equal(batchDefinition.data().importBatchInvalid, true);

  const normalDefinition = remount(null, '');
  const normal = makeFeedApp(normalDefinition, {
    success: true,
    items: [],
    pagination: {page: 1, per_page: 50, total: 0, pages: 0, has_next: false},
  });
  assert.equal(normal.app.importBatchMode, false);
  assert.equal(normal.app.importBatchInvalid, false);
  sameJson(normal.app.queryParams(1), {
    search: '', supplier: '', has_photos: '', stock: '', sort: '', page: 1, per_page: 50,
  });
  normal.app.load();
  normal.app.loadFacets();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(normal.getCalls.length, 2, 'ordinary workspace still loads feed and facets');
  assert.ok(normal.getCalls.every(call => call.params === null || !Object.hasOwn(call.params, 'import_batch')));

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
    'supplier_refresh_receipt_is_tab_scoped_and_replaced_only_after_verified_post',
    'cancel_before_supplier_post_keeps_receipt_and_actual_post_replaces_it',
    'supplier_refresh_resume_reads_exact_labels_and_restores_result_without_mutation',
    'transient_resume_failure_keeps_receipt_for_read_only_retry',
    'expired_resume_receipt_is_cleared_and_reported_on_return_anchor',
    'hide_and_unmount_fence_stale_resume_responses',
    'supplier_refresh_wb_handoff_marks_its_fixed_return_flow',
    'import_batch_mode_does_not_restore_supplier_receipt',
    'import_receipt_mode_reads_one_exact_authorized_page_without_auto_selection',
    'import_receipt_mode_uses_explicit_exact_selection_and_allows_exclusion',
    'invalid_or_stale_import_receipts_clear_rows_and_never_fall_back_to_global_feed',
    'ordinary_workspace_keeps_its_existing_feed_and_facets',
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
