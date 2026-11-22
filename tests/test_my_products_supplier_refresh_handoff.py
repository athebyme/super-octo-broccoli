"""Supplier refresh handoff regressions for browser logic and route scope."""

from pathlib import Path
import os
import shutil
import subprocess
import unittest
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlsplit
from unittest.mock import patch


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
  supplierRefreshBatch: null,
  supplierRefreshBatchInvalid: false,
  supplierRefreshReturnUrl: '/my-products?updates=1',
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
    updated_batch: 'signed-updated-batch',
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

function remount(importBatch, search, supplierRefreshBatch = null, supplierRefreshBatchInvalid = false) {
  boot.importBatch = importBatch;
  boot.batchKind = importBatch === null ? null : 'import';
  boot.supplierRefreshBatch = supplierRefreshBatch;
  boot.supplierRefreshBatchInvalid = supplierRefreshBatchInvalid;
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
  assert.match(templateText, /filename='my-products-beta\.css', v='20261122-bulk-intent-rail-r58'/);
  assert.match(templateText, /filename='my-products-beta\.js', v='20261122-bulk-intent-rail-r58'/);
  assert.match(templateText, /filename='common-product-content\.js', v='20261122-ozon-update-return-r56'/);
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
    updated_batch: 'signed-updated-batch',
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
    updated_batch: 'signed-updated-batch',
    items: [{product_id: 101, status: 'updated', reason: null}],
  }, [101]);
  oversizedReceipt.app.confirmSupplierRefreshReview();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(stored.has(receiptKey), false, 'oversized receipts are never persisted');
  assert.match(oversizedReceipt.app.supplierRefreshResult.resumeWarning, /нельзя будет восстановить/);

  const zeroFailureReceipt = 'v1.zero.failure.receipt';
  const zeroFailureResponse = {
    success: true,
    status: 'completed_with_errors',
    requested: 3,
    updated: 0,
    skipped: 1,
    errors: 2,
    resume_receipt: zeroFailureReceipt,
    items: [
      {product_id: 404, status: 'error', reason: 'update_failed'},
      {product_id: 202, status: 'skipped', reason: 'no_supplier_link'},
      {product_id: 303, status: 'error', reason: 'update_failed'},
    ],
  };
  const zeroFailure = makeApp();
  let zeroFailurePosts = 0;
  setSupplierOutcome(zeroFailure.app, zeroFailureResponse, [404, 202, 303]);
  zeroFailure.app.$api.post = function () {
    zeroFailurePosts += 1;
    return Promise.resolve(zeroFailureResponse);
  };
  zeroFailure.app.confirmSupplierRefreshReview();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(zeroFailure.app.supplierRefreshResult.kind, 'partial',
    'an awaited HTTP 200 with exact error outcomes is a terminal result');
  sameJson([
    zeroFailure.app.supplierRefreshResult.updated,
    zeroFailure.app.supplierRefreshResult.skipped,
    zeroFailure.app.supplierRefreshResult.errors,
  ], [0, 1, 2], 'skipped and failed rows keep distinct counters');
  sameJson(zeroFailure.app.supplierRefreshResult.entries.map(entry => [
    entry.productId, entry.status, entry.href || null,
  ]), [
    [404, 'error', '/my-products/common-content?product_id=404'],
    [202, 'skipped', null],
    [303, 'error', '/my-products/common-content?product_id=303'],
  ], 'only failed rows keep their existing product-specific repair links');
  assert.equal(stored.get(receiptKey), zeroFailureReceipt,
    'a verified zero-update result is saved with the existing receipt key');
  sameJson(zeroFailure.app.supplierRefreshOzonIds, []);
  sameJson(zeroFailure.app.supplierRefreshWbProductIds, []);
  assert.equal(zeroFailure.app.supplierRefreshCanSelectUpdated, false);
  const formsBeforeZeroActions = submittedForms.length;
  assert.equal(zeroFailure.app.prepareRefreshedForOzon({currentTarget: {}}), undefined);
  assert.equal(zeroFailure.app.checkRefreshedWbCharacteristics(), false);
  assert.equal(zeroFailure.app.selectUpdatedSupplierBatch(), false);
  assert.equal(zeroFailurePosts, 1, 'failed and skipped rows never trigger another provider action');
  assert.equal(submittedForms.length, formsBeforeZeroActions,
    'failed and skipped rows never submit a WB action');

  const zeroSkippedReceipt = 'v1.zero.skipped.receipt';
  const zeroSkippedResponse = {
    success: true,
    status: 'completed',
    requested: 2,
    updated: 0,
    skipped: 2,
    errors: 0,
    resume_receipt: zeroSkippedReceipt,
    items: [
      {product_id: 801, status: 'skipped', reason: 'no_supplier_link'},
      {product_id: 802, status: 'skipped', reason: 'no_supplier_link'},
    ],
  };
  const zeroSkipped = makeApp();
  setSupplierOutcome(zeroSkipped.app, zeroSkippedResponse, [801, 802]);
  zeroSkipped.app.confirmSupplierRefreshReview();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(zeroSkipped.app.supplierRefreshResult.kind, 'complete');
  sameJson([
    zeroSkipped.app.supplierRefreshResult.updated,
    zeroSkipped.app.supplierRefreshResult.skipped,
    zeroSkipped.app.supplierRefreshResult.errors,
  ], [0, 2, 0]);
  assert.equal(stored.get(receiptKey), zeroSkippedReceipt,
    'all-skipped terminal results also retain a read-only resume receipt');
  sameJson(zeroSkipped.app.supplierRefreshOzonIds, []);
  sameJson(zeroSkipped.app.supplierRefreshWbProductIds, []);
  assert.equal(zeroSkipped.app.supplierRefreshCanSelectUpdated, false);

  let resolveChangedScopeApply;
  const changedScopeApply = makeApp();
  const changedScopeReceipt = 'v1.changed.scope.receipt';
  const changedScopeResponse = {
    ...zeroSkippedResponse,
    resume_receipt: changedScopeReceipt,
    items: [{product_id: 803, status: 'skipped', reason: 'no_supplier_link'}],
    requested: 1,
    skipped: 1,
  };
  setSupplierOutcome(changedScopeApply.app, changedScopeResponse, [803]);
  changedScopeApply.app.feedScopeKey = function () {
    return this.filters.search ? JSON.stringify({search: this.filters.search}) : 'current';
  };
  const loadedScopes = [];
  changedScopeApply.app.load = function () {
    this.cancelSupplierRefreshReview({restoreFocus: false});
    this.selected = [];
    loadedScopes.push(this.feedScopeKey());
  };
  changedScopeApply.app.$api.post = function () {
    return new Promise(resolve => { resolveChangedScopeApply = resolve; });
  };
  const changedScopePending = changedScopeApply.app.confirmSupplierRefreshReview();
  assert.equal(changedScopeApply.app.supplierRefreshReview.phase, 'applying');
  changedScopeApply.app.filters.search = 'current user scope';
  changedScopeApply.app.load();
  assert.equal(changedScopeApply.app.supplierRefreshReview.phase, 'applying',
    'loading a changed feed scope does not cancel an already-sent refresh');
  resolveChangedScopeApply(changedScopeResponse);
  await changedScopePending;
  assert.equal(changedScopeApply.app.supplierRefreshResult.updated, 0,
    'a late verified response remains visible after the user changes scope');
  assert.equal(changedScopeApply.app.filters.search, 'current user scope',
    'showing the terminal result preserves the current search scope');
  sameJson(loadedScopes, [
    JSON.stringify({search: 'current user scope'}),
    JSON.stringify({search: 'current user scope'}),
  ], 'scope changes and the post-result refresh both use the current scope');
  assert.equal(stored.get(receiptKey), changedScopeReceipt);

  let resolveUnmountedApply;
  const unmountedApply = makeApp();
  const unmountedApplyReceipt = 'v1.unmounted.zero.receipt';
  const unmountedApplyResponse = {
    ...zeroSkippedResponse,
    resume_receipt: unmountedApplyReceipt,
    items: [{product_id: 804, status: 'skipped', reason: 'no_supplier_link'}],
    requested: 1,
    skipped: 1,
  };
  setSupplierOutcome(unmountedApply.app, unmountedApplyResponse, [804]);
  unmountedApply.app.$api.post = function () {
    return new Promise(resolve => { resolveUnmountedApply = resolve; });
  };
  const unmountedApplyPending = unmountedApply.app.confirmSupplierRefreshReview();
  component.beforeUnmount.call(unmountedApply.app);
  resolveUnmountedApply(unmountedApplyResponse);
  await unmountedApplyPending;
  assert.equal(stored.get(receiptKey), unmountedApplyReceipt,
    'a verified zero-update response arriving after unmount remains resumable');

  const zeroRestoredReceipt = 'v1.zero.restored.receipt';
  const zeroRestored = makeApp();
  const zeroRestoredData = {
    ...zeroFailureResponse,
    labels: [404, 202, 303].map(productId => ({productId, label: 'Product ' + productId})),
  };
  stored.set(receiptKey, zeroRestoredReceipt);
  fakeWindow.location.hash = '#supplier-refresh-result';
  fetchCalls.length = 0;
  let restoreMutationCalls = 0;
  zeroRestored.app.$api.post = function () { restoreMutationCalls += 1; return Promise.resolve({}); };
  fetchHandler = (url, options) => {
    assert.equal(url, '/api/my-products/supplier-refresh-result');
    assert.equal(options.headers['X-Supplier-Refresh-Receipt'], zeroRestoredReceipt);
    return Promise.resolve(jsonResponse(200, zeroRestoredData));
  };
  zeroRestored.app._supplierRefreshReturnAnchor = true;
  await zeroRestored.app.restoreSupplierRefreshResult();
  assert.equal(fetchCalls.length, 1, 'zero-update restoration performs only one receipt GET');
  assert.equal(zeroRestored.app.supplierRefreshResult.saved, true);
  sameJson([
    zeroRestored.app.supplierRefreshResult.updated,
    zeroRestored.app.supplierRefreshResult.skipped,
    zeroRestored.app.supplierRefreshResult.errors,
  ], [0, 1, 2]);
  sameJson(zeroRestored.app.supplierRefreshResult.entries.map(entry => [
    entry.productId, entry.href || null,
  ]), [
    [404, '/my-products/common-content?product_id=404'],
    [202, null],
    [303, '/my-products/common-content?product_id=303'],
  ]);
  sameJson(zeroRestored.app.supplierRefreshOzonIds, []);
  sameJson(zeroRestored.app.supplierRefreshWbProductIds, []);
  assert.equal(zeroRestored.app.supplierRefreshCanSelectUpdated, false);
  assert.equal(restoreMutationCalls, 0, 'restoration reads the saved receipt without replaying the update');
  assert.equal(stored.get(receiptKey), zeroRestoredReceipt, 'restoration leaves the receipt reusable');

  const unknown500 = makeApp();
  setSupplierOutcome(unknown500.app, zeroFailureResponse, [404, 202, 303]);
  unknown500.app.$api.post = function () {
    return Promise.reject(Object.assign(new Error('server error'), {status: 500}));
  };
  unknown500.app.confirmSupplierRefreshReview();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(unknown500.app.supplierRefreshResult.kind, 'unknown',
    'an HTTP 500 remains an unconfirmed result rather than a terminal all-failed result');
  assert.equal(stored.has(receiptKey), false, 'an HTTP 500 does not create a result receipt');

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
  assert.equal(restored.app.supplierRefreshResult.title, 'Сохранённый итог обновления от поставщика');
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
    batch_kind: 'import',
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

  const supplierToken = 'signed-classic-refresh-selection';
  const supplierPage = {
    success: true,
    supplier_refresh_batch_count: 2,
    items: [{id: 202, title: 'Second'}, {id: 101, title: 'First'}],
    pagination: {page: 1, per_page: 200, total: 2, pages: 1, has_next: false},
  };
  const supplierDefinition = remount(
    null, '?supplier_refresh_batch=' + encodeURIComponent(supplierToken), supplierToken,
  );
  const supplierBatch = makeFeedApp(supplierDefinition, supplierPage);
  assert.equal(supplierBatch.app.supplierRefreshBatchMode, true);
  assert.equal(supplierBatch.app.supplierRefreshBatchInvalid, false);
  assert.equal(supplierBatch.app.supplierRefreshReturnUrl, '/my-products?updates=1');
  sameJson(supplierBatch.app.queryParams(99, 5), {
    supplier_refresh_batch: supplierToken,
  });
  supplierBatch.app.load();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(supplierBatch.getCalls.length, 1);
  sameJson(supplierBatch.getCalls[0].params, {
    supplier_refresh_batch: supplierToken,
  });
  sameJson(supplierBatch.app.items.map(item => item.id), [202, 101]);
  sameJson(supplierBatch.app.selected, [202, 101],
    'classic handoff restores the exact order and preselects the signed rows');
  assert.equal(supplierBatch.app.feedScopeKey(), JSON.stringify({
    supplier_refresh_batch: supplierToken,
  }));
  supplierBatch.app.loadFacets();
  supplierBatch.app.loadMore();
  supplierBatch.app.selectAllFiltered();
  assert.equal(supplierBatch.getCalls.length, 1,
    'exact handoff does not widen through a filtered or paginated fetch');
  const handoffPreviewCalls = [];
  supplierBatch.app.$refs = {
    supplierRefreshDialog: {open: false, showModal() { this.open = true; }},
    supplierRefreshCancel: {focus() {}},
    supplierRefreshReviewRetry: {focus() {}},
  };
  supplierBatch.app.$api.post = function (url, body) {
    handoffPreviewCalls.push({url, body});
    return Promise.resolve(supplierPreview(body.product_ids));
  };
  assert.equal(handoffPreviewCalls.length, 0, 'handoff itself does not open or submit a review');
  supplierBatch.app.openSupplierRefreshReview(
    [202, 101], [202, 101], [202, 101].map(id => ({productId: id, label: 'Product ' + id})),
  );
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(handoffPreviewCalls.length, 1, 'the existing action click opens the review preview');
  assert.equal(handoffPreviewCalls[0].url, '/api/my-products/supplier-refresh-preview');
  sameJson(handoffPreviewCalls[0].body.product_ids, [202, 101]);

  const invalidSupplierDefinition = remount(
    null, '?supplier_refresh_batch=', null, true,
  );
  const invalidSupplierBatch = makeFeedApp(invalidSupplierDefinition, supplierPage);
  invalidSupplierBatch.app.items = [{id: 999, title: 'Stale'}];
  invalidSupplierBatch.app.selected = [999];
  invalidSupplierBatch.app.load();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(invalidSupplierBatch.app.supplierRefreshBatchMode, true);
  assert.equal(invalidSupplierBatch.getCalls.length, 0,
    'invalid handoff never falls back to an unrestricted feed');
  sameJson(invalidSupplierBatch.app.items, []);
  sameJson(invalidSupplierBatch.app.selected, []);

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
    'classic_supplier_refresh_handoff_preselects_exact_order_without_preview_until_click',
    'invalid_supplier_refresh_handoff_never_falls_back_to_global_feed',
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


class SupplierRefreshHandoffRouteTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ['DISABLE_SECURE_COOKIE'] = '1'
        os.environ.setdefault('SKIP_SCHEDULER', '1')
        import sqlalchemy as sa
        from sqlalchemy.pool import StaticPool
        import seller_platform
        from models import db

        cls.app = seller_platform.app
        cls.db = db
        cls.previous_engine_map = db._app_engines.get(cls.app)
        cls.previous_config = {
            name: cls.app.config.get(name)
            for name in (
                'SQLALCHEMY_DATABASE_URI', 'SQLALCHEMY_TRACK_MODIFICATIONS',
                'SQLALCHEMY_ENGINE_OPTIONS', 'WTF_CSRF_ENABLED', 'TESTING',
            )
        }
        cls.app.config.update(
            SQLALCHEMY_DATABASE_URI='sqlite:///:memory:',
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            SQLALCHEMY_ENGINE_OPTIONS={},
            WTF_CSRF_ENABLED=False,
            TESTING=True,
        )
        cls.engine = sa.create_engine(
            'sqlite:///:memory:',
            connect_args={'check_same_thread': False},
            poolclass=StaticPool,
        )
        db._app_engines[cls.app] = {None: cls.engine}

    @classmethod
    def tearDownClass(cls):
        with cls.app.app_context():
            cls.db.session.remove()
        cls.engine.dispose()
        if cls.previous_engine_map is None:
            cls.db._app_engines.pop(cls.app, None)
        else:
            cls.db._app_engines[cls.app] = cls.previous_engine_map
        for name, value in cls.previous_config.items():
            cls.app.config[name] = value

    def setUp(self):
        from models import ImportedProduct, Seller, Supplier, User, db

        self.ctx = self.app.app_context()
        self.ctx.push()
        db.session.remove()
        db.drop_all()
        db.create_all()
        self.client = self.app.test_client()

        owner = User(
            username='refresh-handoff-owner',
            email='refresh-handoff-owner@test.local',
            is_active=True,
        )
        owner.set_password('synthetic-password')
        seller = Seller(user=owner, company_name='Handoff seller')
        other_user = User(
            username='refresh-handoff-other',
            email='refresh-handoff-other@test.local',
            is_active=True,
        )
        other_user.set_password('synthetic-password')
        other_seller = Seller(user=other_user, company_name='Other seller')
        supplier = Supplier(name='Handoff supplier', code='handoff')
        db.session.add_all([seller, other_seller, supplier])
        db.session.flush()

        first = ImportedProduct(
            seller_id=seller.id, supplier_id=supplier.id,
            external_id='handoff-first', source_type='synthetic',
            title='First selected product', brand='Acme',
            import_status='validated',
        )
        second = ImportedProduct(
            seller_id=seller.id, supplier_id=supplier.id,
            external_id='handoff-second', source_type='synthetic',
            title='Second selected product', brand='Acme',
            import_status='validated',
        )
        foreign = ImportedProduct(
            seller_id=other_seller.id, supplier_id=supplier.id,
            external_id='handoff-foreign', source_type='synthetic',
            title='Foreign product', import_status='validated',
        )
        db.session.add_all([first, second, foreign])
        db.session.commit()

        self.user = SimpleNamespace(
            id=owner.id,
            username=owner.username,
            seller=seller,
            is_authenticated=True,
            is_active=True,
            is_admin=False,
        )
        self.seller_id = seller.id
        self.supplier_id = supplier.id
        self.first_id = first.id
        self.second_id = second.id
        self.foreign_id = foreign.id

    def tearDown(self):
        self.db.session.remove()
        self.db.drop_all()
        self.ctx.pop()

    def _auth(self):
        return patch('flask_login.utils._get_user', return_value=self.user)

    def test_classic_form_redirects_exact_selection_to_read_only_beta_review(self):
        from models import ImportedProduct, db
        from routes.suppliers import _load_supplier_refresh_handoff_token

        classic_query = urlencode({
            'updates': '1',
            'search': '  product  ',
            'supplier': str(self.supplier_id),
            'brand': 'Acme',
            'wb_category': 'Candles',
            'has_photos': 'yes',
            'stock': 'in_stock',
            'price_min': '10.5',
            'price_max': '40',
            'sort': 'title',
        })
        with self._auth(), patch(
            'routes.suppliers.SupplierService.update_imported_products',
        ) as update, patch(
            'routes.suppliers.SupplierService.preview_imported_products',
        ) as preview:
            response = self.client.post(
                '/my-products/refresh-from-supplier/review?' + classic_query,
                data={'selected_ids': [str(self.second_id), str(self.first_id)]},
            )
        self.assertEqual(response.status_code, 303)
        update.assert_not_called()
        preview.assert_not_called()
        self.assertIn('private, no-store', response.headers['Cache-Control'])
        query = parse_qs(urlsplit(response.location).query, keep_blank_values=True)
        self.assertEqual(set(query), {'supplier_refresh_batch'})
        token = query['supplier_refresh_batch'][0]
        handoff = _load_supplier_refresh_handoff_token(
            token, user_id=self.user.id, seller_id=self.seller_id,
        )
        self.assertEqual(handoff['ids'], [self.second_id, self.first_id])
        self.assertEqual(handoff['return_args'], {
            'updates': 1,
            'search': 'product',
            'supplier': self.supplier_id,
            'brand': 'Acme',
            'wb_category': 'Candles',
            'has_photos': 'yes',
            'stock': 'in_stock',
            'price_min': '10.5',
            'price_max': '40',
            'sort': 'title',
        })
        rows = ImportedProduct.query.filter(
            ImportedProduct.seller_id == self.seller_id,
            ImportedProduct.id.in_([self.first_id, self.second_id]),
        ).all()
        self.assertEqual(len(rows), 2)
        self.assertEqual(db.session.query(ImportedProduct).count(), 3)

        with self._auth(), patch(
            'routes.suppliers.render_template', return_value='beta-page',
        ) as render:
            page = self.client.get(response.location)
        self.assertEqual(page.status_code, 200)
        self.assertIn('no-store', page.headers['Cache-Control'])
        self.assertEqual(render.call_args.kwargs['supplier_refresh_batch'], token)
        returned = urlsplit(render.call_args.kwargs['supplier_refresh_return_url'])
        self.assertEqual(returned.path, '/my-products')
        self.assertEqual(parse_qs(returned.query), {
            'updates': ['1'],
            'search': ['product'],
            'supplier': [str(self.supplier_id)],
            'brand': ['Acme'],
            'wb_category': ['Candles'],
            'has_photos': ['yes'],
            'stock': ['in_stock'],
            'price_min': ['10.5'],
            'price_max': ['40'],
            'sort': ['title'],
        })

        with self._auth():
            feed = self.client.get(
                '/api/my-products?' + urlencode([
                    ('supplier_refresh_batch', token),
                    ('status', 'failed'),
                    ('search', 'unmatched'),
                ]),
            )
        self.assertEqual(feed.status_code, 200)
        self.assertIn('no-store', feed.headers['Cache-Control'])
        payload = feed.get_json()
        self.assertEqual(
            [item['id'] for item in payload['items']],
            [self.second_id, self.first_id],
        )
        self.assertEqual(payload['supplier_refresh_batch_count'], 2)
        self.assertNotIn('import_batch_count', payload)

    def test_classic_handoff_and_signed_feed_fail_closed_for_whole_scope(self):
        from models import ImportedProduct, db
        from routes.suppliers import _issue_supplier_refresh_handoff_token

        with self._auth(), patch(
            'routes.suppliers.SupplierService.update_imported_products',
        ) as update:
            foreign_form = self.client.post(
                '/my-products/refresh-from-supplier/review?updates=1&search=Acme',
                data={'selected_ids': [str(self.first_id), str(self.foreign_id)]},
            )
            missing_form = self.client.post(
                '/my-products/refresh-from-supplier/review?updates=1&search=Acme',
                data={'selected_ids': [str(self.first_id), '9223372036854775000']},
            )
        update.assert_not_called()
        self.assertEqual(foreign_form.status_code, 302)
        self.assertEqual(missing_form.status_code, 302)
        self.assertEqual(urlsplit(foreign_form.location).path, '/my-products')
        self.assertEqual(urlsplit(missing_form.location).path, '/my-products')
        self.assertNotIn('supplier_refresh_batch', foreign_form.location)
        self.assertNotIn('supplier_refresh_batch', missing_form.location)

        token = _issue_supplier_refresh_handoff_token(
            user_id=self.user.id,
            seller_id=self.seller_id,
            imported_product_ids=[self.first_id, self.second_id],
            return_args={'updates': 1},
        )
        db.session.query(ImportedProduct).filter_by(id=self.second_id).delete()
        db.session.commit()
        with self._auth():
            stale = self.client.get('/api/my-products?' + urlencode({
                'supplier_refresh_batch': token,
            }))
        self.assertEqual(stale.status_code, 404)
        self.assertIn('no-store', stale.headers['Cache-Control'])
        self.assertNotIn('items', stale.get_json())

    def test_signed_handoff_rejects_tampered_expired_foreign_and_ambiguous_tokens(self):
        from routes.suppliers import _issue_supplier_refresh_handoff_token

        token = _issue_supplier_refresh_handoff_token(
            user_id=self.user.id,
            seller_id=self.seller_id,
            imported_product_ids=[self.first_id],
            return_args={'updates': 1, 'supplier': self.supplier_id},
        )
        token_body, signature = token.rsplit('.', 1)
        changed = ('a' if signature[0] != 'a' else 'b') + signature[1:]
        cases = [
            (urlencode({'supplier_refresh_batch': f'{token_body}.{changed}'}), 400),
            (urlencode([
                ('supplier_refresh_batch', token),
                ('supplier_refresh_batch', token),
            ]), 400),
            (urlencode({'supplier_refresh_batch': ''}), 400),
            (urlencode([
                ('supplier_refresh_batch', token), ('import_batch', 'other'),
            ]), 400),
        ]
        for query, status in cases:
            with self.subTest(status=status, query=query[:60]), self._auth():
                response = self.client.get('/api/my-products?' + query)
            self.assertEqual(response.status_code, status)
            self.assertIn('no-store', response.headers['Cache-Control'])
            self.assertNotIn('items', response.get_json())

        with patch(
            'routes.suppliers.SUPPLIER_REFRESH_HANDOFF_MAX_AGE_SECONDS', -1,
        ), self._auth():
            expired = self.client.get('/api/my-products?' + urlencode({
                'supplier_refresh_batch': token,
            }))
        self.assertEqual(expired.status_code, 410)
        self.assertIn('no-store', expired.headers['Cache-Control'])

        with self._auth():
            foreign_owner = self.client.get('/api/my-products?' + urlencode({
                'supplier_refresh_batch': _issue_supplier_refresh_handoff_token(
                    user_id=self.user.id + 1,
                    seller_id=self.seller_id,
                    imported_product_ids=[self.first_id],
                    return_args={},
                ),
            }))
        self.assertEqual(foreign_owner.status_code, 404)

    def test_account_scope_is_validated_and_tokenless_legacy_route_is_unchanged(self):
        from types import SimpleNamespace

        with self._auth():
            foreign_account = self.client.post(
                '/my-products/refresh-from-supplier/review?account_id=999&updates=1',
                data={'selected_ids': [str(self.first_id)]},
            )
        self.assertEqual(foreign_account.status_code, 404)

        legacy_result = SimpleNamespace(
            imported=1,
            skipped=0,
            errors=0,
            total_requested=1,
            item_results=[{'product_id': self.first_id, 'status': 'updated'}],
            success=True,
        )
        with self._auth(), patch(
            'routes.suppliers.SupplierService.update_imported_products',
            return_value=legacy_result,
        ) as update:
            legacy = self.client.post(
                '/my-products/refresh-from-supplier?updates=1',
                data={'selected_ids': [str(self.first_id)]},
            )
        self.assertEqual(legacy.status_code, 302)
        update.assert_called_once_with(self.seller_id, [self.first_id])

    def test_supplier_refresh_result_batch_is_exact_and_resumable(self):
        from models import ImportedProduct, db
        from routes.suppliers import (
            _issue_supplier_import_batch_token,
            _supplier_import_batch_serializer,
        )
        from itsdangerous import BadSignature

        third = ImportedProduct(
            seller_id=self.seller_id, supplier_id=self.supplier_id,
            external_id='handoff-third', source_type='synthetic',
            title='Third selected product', brand='Acme',
            import_status='validated',
        )
        fourth = ImportedProduct(
            seller_id=self.seller_id, supplier_id=self.supplier_id,
            external_id='handoff-fourth', source_type='synthetic',
            title='Fourth selected product', brand='Acme',
            import_status='validated',
        )
        db.session.add_all([third, fourth])
        db.session.commit()

        requested_ids = [self.second_id, self.first_id, third.id, fourth.id]
        mixed_result = SimpleNamespace(
            imported=2,
            skipped=1,
            errors=1,
            total_requested=4,
            item_results=[
                {'product_id': self.second_id, 'status': 'updated', 'reason': None},
                {
                    'product_id': self.first_id,
                    'status': 'skipped',
                    'reason': 'no_supplier_link',
                },
                {
                    'product_id': third.id,
                    'status': 'error',
                    'reason': 'update_failed',
                },
                {'product_id': fourth.id, 'status': 'updated', 'reason': None},
            ],
            success=True,
        )
        with self._auth(), patch(
            'routes.suppliers.SupplierService.update_imported_products',
            return_value=mixed_result,
        ) as update:
            post = self.client.post(
                '/my-products/refresh-from-supplier',
                json={'product_ids': requested_ids},
            )
        self.assertEqual(post.status_code, 200)
        update.assert_called_once_with(self.seller_id, requested_ids)
        post_payload = post.get_json()
        self.assertIsInstance(post_payload['resume_receipt'], str)
        self.assertIsInstance(post_payload['updated_batch'], str)
        self.assertEqual(
            [item['status'] for item in post_payload['items']],
            ['updated', 'skipped', 'error', 'updated'],
        )

        updated_batch = post_payload['updated_batch']
        with self.assertRaises(BadSignature):
            _supplier_import_batch_serializer().loads(updated_batch)

        query = urlencode({'import_batch': updated_batch})
        with self._auth(), patch(
            'routes.suppliers.SupplierService.update_imported_products',
        ) as update, patch(
            'routes.suppliers.SupplierService.preview_imported_products',
        ) as preview, patch(
            'routes.suppliers.render_template', return_value='rendered',
        ) as render:
            feed = self.client.get('/api/my-products?' + query)
            page = self.client.get('/my-products/beta?' + query)
            resumed = self.client.get(
                '/api/my-products/supplier-refresh-result',
                headers={'X-Supplier-Refresh-Receipt': post_payload['resume_receipt']},
            )
        update.assert_not_called()
        preview.assert_not_called()
        self.assertEqual(feed.status_code, 200)
        self.assertIn('no-store', feed.headers['Cache-Control'])
        feed_payload = feed.get_json()
        self.assertEqual(feed_payload['batch_kind'], 'supplier_refresh_result')
        self.assertEqual(feed_payload['import_batch_count'], 2)
        self.assertEqual(
            [item['id'] for item in feed_payload['items']],
            [self.second_id, fourth.id],
        )
        self.assertEqual(feed_payload['pagination']['pages'], 1)
        self.assertEqual(feed_payload['pagination']['has_next'], False)
        self.assertEqual(page.status_code, 200)
        self.assertIn('no-store', page.headers['Cache-Control'])
        self.assertEqual(render.call_args.kwargs['batch_kind'], 'supplier_refresh_result')
        self.assertEqual(render.call_args.kwargs['import_batch'], updated_batch)
        self.assertEqual(resumed.status_code, 200)
        self.assertIn('no-store', resumed.headers['Cache-Control'])
        resumed_payload = resumed.get_json()
        self.assertIsInstance(resumed_payload['updated_batch'], str)
        self.assertEqual(
            [item['product_id'] for item in resumed_payload['items']],
            requested_ids,
        )
        self.assertEqual(
            [item['status'] for item in resumed_payload['items']],
            ['updated', 'skipped', 'error', 'updated'],
        )

        with self._auth():
            resumed_feed = self.client.get(
                '/api/my-products?' + urlencode({
                    'import_batch': resumed_payload['updated_batch'],
                }),
            )
        self.assertEqual(resumed_feed.status_code, 200)
        self.assertEqual(
            [item['id'] for item in resumed_feed.get_json()['items']],
            [self.second_id, fourth.id],
        )

        regular_import = _issue_supplier_import_batch_token(
            user_id=self.user.id,
            seller_id=self.seller_id,
            imported_product_ids=[self.first_id],
        )
        with self._auth():
            regular_feed = self.client.get(
                '/api/my-products?' + urlencode({'import_batch': regular_import}),
            )
            with patch('routes.suppliers.render_template', return_value='rendered') as render:
                regular_page = self.client.get(
                    '/my-products/beta?' + urlencode({'import_batch': regular_import}),
                )
        self.assertEqual(regular_feed.status_code, 200)
        self.assertEqual(regular_feed.get_json()['batch_kind'], 'import')
        self.assertEqual(regular_page.status_code, 200)
        self.assertEqual(render.call_args.kwargs['batch_kind'], 'import')

    def test_supplier_refresh_result_batch_reads_more_than_one_feed_page_exactly(self):
        from sqlalchemy import event
        from models import ImportedProduct, db
        from routes.suppliers import SupplierService

        rows = [
            ImportedProduct(
                seller_id=self.seller_id,
                supplier_id=self.supplier_id,
                external_id=f'result-batch-{index}',
                source_type='synthetic',
                title=f'Result batch product {index}',
                import_status='validated',
            )
            for index in range(51)
        ]
        db.session.add_all(rows)
        db.session.flush()
        ordered_ids = [row.id for row in reversed(rows)]
        db.session.commit()
        result = SimpleNamespace(
            imported=len(ordered_ids),
            skipped=0,
            errors=0,
            total_requested=len(ordered_ids),
            item_results=[
                {'product_id': product_id, 'status': 'updated', 'reason': None}
                for product_id in ordered_ids
            ],
            success=True,
        )
        with self._auth(), patch(
            'routes.suppliers.SupplierService.update_imported_products',
            return_value=result,
        ) as refresh:
            post = self.client.post(
                '/my-products/refresh-from-supplier',
                json={'product_ids': ordered_ids},
            )
        self.assertEqual(post.status_code, 200)
        refresh.assert_called_once_with(self.seller_id, ordered_ids)
        batch_token = post.get_json()['updated_batch']
        self.assertIsInstance(batch_token, str)

        writes = []

        def capture_write(_conn, _cursor, statement, _parameters, _context, _many):
            if statement.lstrip().upper().startswith(
                ('INSERT', 'UPDATE', 'DELETE', 'REPLACE'),
            ):
                writes.append(statement)

        event.listen(self.engine, 'before_cursor_execute', capture_write)
        try:
            with self._auth(), patch.object(
                SupplierService, 'update_imported_products',
            ) as refresh, patch.object(
                SupplierService, 'preview_imported_products',
            ) as preview:
                response = self.client.get('/api/my-products?' + urlencode({
                    'import_batch': batch_token,
                }))
            refresh.assert_not_called()
            preview.assert_not_called()
        finally:
            event.remove(self.engine, 'before_cursor_execute', capture_write)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(writes, [], 'opening the exact result batch is read-only')
        payload = response.get_json()
        self.assertEqual(payload['batch_kind'], 'supplier_refresh_result')
        self.assertEqual(payload['import_batch_count'], 51)
        self.assertEqual([item['id'] for item in payload['items']], ordered_ids)
        self.assertEqual(payload['pagination'], {
            'page': 1,
            'per_page': 200,
            'pages': 1,
            'total': 51,
            'has_next': False,
        })

    def test_supplier_refresh_post_without_verified_updates_returns_null_batch(self):
        from routes.suppliers import _load_supplier_refresh_receipt

        result = SimpleNamespace(
            imported=0,
            skipped=1,
            errors=0,
            total_requested=1,
            item_results=[{
                'product_id': self.first_id,
                'status': 'skipped',
                'reason': 'no_supplier_link',
            }],
            success=True,
        )
        with self._auth(), patch(
            'routes.suppliers.SupplierService.update_imported_products',
            return_value=result,
        ) as update:
            response = self.client.post(
                '/my-products/refresh-from-supplier',
                json={'product_ids': [self.first_id]},
            )
        update.assert_called_once_with(self.seller_id, [self.first_id])
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertIn('updated_batch', payload)
        self.assertIsNone(payload['updated_batch'])
        self.assertIsInstance(payload['resume_receipt'], str)
        self.assertEqual(_load_supplier_refresh_receipt(
            payload['resume_receipt'],
            user_id=self.user.id,
            seller_id=self.seller_id,
        ), {
            'ids': [self.first_id],
            'statuses': ['skipped'],
            'wb_targets': [],
        })

        with self._auth(), patch(
            'routes.suppliers.SupplierService.update_imported_products',
        ) as update, patch(
            'routes.suppliers.SupplierService.preview_imported_products',
        ) as preview:
            resumed = self.client.get(
                '/api/my-products/supplier-refresh-result',
                headers={'X-Supplier-Refresh-Receipt': payload['resume_receipt']},
            )
        update.assert_not_called()
        preview.assert_not_called()
        self.assertEqual(resumed.status_code, 200)
        self.assertIn('no-store', resumed.headers['Cache-Control'])
        resumed_payload = resumed.get_json()
        self.assertEqual(resumed_payload['status'], 'completed')
        self.assertEqual(resumed_payload['requested'], 1)
        self.assertEqual(resumed_payload['updated'], 0)
        self.assertEqual(resumed_payload['skipped'], 1)
        self.assertEqual(resumed_payload['errors'], 0)
        self.assertEqual(resumed_payload['items'], [{
            'product_id': self.first_id,
            'status': 'skipped',
            'reason': 'no_supplier_link',
        }])
        self.assertEqual(resumed_payload['labels'], [{
            'productId': self.first_id,
            'label': 'First selected product · handoff-first',
        }])
        self.assertEqual(resumed_payload['wb_targets'], [])
        self.assertEqual(resumed_payload['wb_ready'], 0)
        self.assertIsNone(resumed_payload['updated_batch'])

    def test_supplier_refresh_result_batch_rejects_scope_expiry_and_row_drift(self):
        from models import ImportedProduct, db
        from routes.suppliers import _issue_supplier_refresh_result_batch_token

        result_token = _issue_supplier_refresh_result_batch_token(
            user_id=self.user.id,
            seller_id=self.seller_id,
            updated_product_ids=[self.second_id, self.first_id],
        )
        self.assertIsInstance(result_token, str)
        self.assertIsNone(_issue_supplier_refresh_result_batch_token(
            user_id=self.user.id,
            seller_id=self.seller_id,
            updated_product_ids=[],
        ))
        self.assertIsNone(_issue_supplier_refresh_result_batch_token(
            user_id=self.user.id,
            seller_id=self.seller_id,
            updated_product_ids=[self.first_id, self.first_id],
        ))
        self.assertIsNone(_issue_supplier_refresh_result_batch_token(
            user_id=self.user.id,
            seller_id=self.seller_id,
            updated_product_ids=list(range(1, 202)),
        ))
        token_body, signature = result_token.rsplit('.', 1)
        changed_signature = ('a' if signature[0] != 'a' else 'b') + signature[1:]
        cases = [
            (
                urlencode({'import_batch': f'{token_body}.{changed_signature}'}),
                400,
            ),
            (
                urlencode({'import_batch': _issue_supplier_refresh_result_batch_token(
                    user_id=self.user.id + 1,
                    seller_id=self.seller_id,
                    updated_product_ids=[self.first_id],
                )}),
                404,
            ),
            (
                urlencode({'import_batch': _issue_supplier_refresh_result_batch_token(
                    user_id=self.user.id,
                    seller_id=self.seller_id + 1,
                    updated_product_ids=[self.first_id],
                )}),
                404,
            ),
        ]
        for query, expected_status in cases:
            with self.subTest(expected_status=expected_status), self._auth():
                response = self.client.get('/api/my-products?' + query)
            self.assertEqual(response.status_code, expected_status)
            self.assertIn('no-store', response.headers['Cache-Control'])
            self.assertNotIn('items', response.get_json())

        with patch(
            'routes.suppliers.SUPPLIER_REFRESH_RESULT_BATCH_MAX_AGE_SECONDS', -1,
        ), self._auth():
            expired = self.client.get('/api/my-products?' + urlencode({
                'import_batch': result_token,
            }))
        self.assertEqual(expired.status_code, 410)
        self.assertIn('no-store', expired.headers['Cache-Control'])

        db.session.query(ImportedProduct).filter_by(id=self.second_id).delete()
        db.session.commit()
        with self._auth():
            drifted = self.client.get('/api/my-products?' + urlencode({
                'import_batch': result_token,
            }))
        self.assertEqual(drifted.status_code, 404)
        self.assertIn('no-store', drifted.headers['Cache-Control'])
        self.assertNotIn('items', drifted.get_json())
