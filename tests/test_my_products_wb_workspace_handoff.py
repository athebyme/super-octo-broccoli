"""Focused UI contract checks for the My Products → WB editor handoff."""

from pathlib import Path
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[1]


NODE_CASES = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

let component;
const requests = [];
const forms = [];
const returnPayloads = [];
const consumedNonces = [];
let storageAvailable = true;
let nonceIndex = 0;
const nonces = [
  '11111111111111111111111111111111',
  '22222222222222222222222222222222',
  '33333333333333333333333333333333',
];

function element(tag) {
  return {
    tagName: tag,
    children: [],
    appendChild(child) { this.children.push(child); },
    submit() { forms.push(this); },
  };
}
const context = {
  window: {
    location: {origin: 'https://seller.test', search: '', hash: ''},
    SellerHubCommonContent: {
      storeWorkspaceReturn(payload) {
        returnPayloads.push(JSON.parse(JSON.stringify(payload)));
        return storageAvailable ? nonces[nonceIndex++] : null;
      },
      consumeWorkspaceReturn(nonce) { consumedNonces.push(nonce); return true; },
    },
    shVue: {
      bootstrapData() { return {
        workspaceOwner: '7:9', wbConnected: true, wbAccountId: null,
        ozonEnabled: false, ozonAccounts: [], suppliers: [],
        urls: {
          workspace: '/my-products/beta',
          wbSelectionResolve: '/products/selection/resolve',
          wbBulkEdit: '/products/bulk-edit',
        },
      }; },
      mount(_selector, options) { component = options; },
    },
  },
  document: {
    querySelector(selector) {
      return selector === 'meta[name="csrf-token"]' ? {content: 'synthetic-csrf'} : null;
    },
    createElement: element,
    body: {appendChild(form) { forms.push({appended: form}); }},
  },
  URL,
  URLSearchParams,
  AbortController,
  setTimeout,
  clearTimeout,
  setInterval,
  clearInterval,
  console,
};
vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), context);
assert.ok(component && component.methods);

function makeApp(selection = [22, 21], {batch = false} = {}) {
  const items = [
    {id: 21, title: 'WB source A', wb: {product_id: 501}},
    {id: 22, title: 'WB source B', wb: {product_id: 502}},
    {id: 23, title: 'Unselected row', wb: {product_id: 503}},
  ];
  const app = {
    items, selected: selection.slice(),
    filters: {search: '', supplier: '', has_photos: '', stock: '', sort: ''},
    searchInput: '', activeTab: '',
    pagination: {page: 1, per_page: 50, total: items.length, has_next: false},
    loading: false, loadingMore: false, selectAllLoading: false,
    itemsScope: '', _feedRevision: 4,
    wbConnected: true, wbAccountKnown: true, wbAccountId: null, wbEditBusy: false,
    importBatchMode: batch, importBatch: batch ? 'signed.import-batch_token' : '',
    supplierRefreshBatchMode: false,
    urls: component.data.call({}).urls,
    error: '',
    $fmt: {plural() { return 'товаров'; }},
    $api: {post(url, body, options) {
      return new Promise((resolve, reject) => requests.push({url, body, options, resolve, reject}));
    }},
    consumeWorkspaceReturn(nonce) {
      if (nonce) consumedNonces.push(nonce);
    },
    buildWorkspaceReturnScope: component.methods.buildWorkspaceReturnScope,
    feedScopeKey: component.methods.feedScopeKey,
    queryParams: component.methods.queryParams,
  };
  Object.defineProperty(app, 'selectedItems', {
    get() {
      const chosen = new Set(this.selected);
      return this.items.filter(row => chosen.has(row.id));
    },
  });
  Object.assign(app, component.methods);
  app.itemsScope = app.feedScopeKey();
  return app;
}
function exactResponse(ids, token) {
  return {selection_token: token, ids, count: ids.length, marketplace: 'wb'};
}
function flush() { return new Promise(resolve => setImmediate(resolve)); }

(async () => {
  // The visible row order differs from click order. Resolver IDs and the
  // token-only editor form must retain the selected order exactly.
  const app = makeApp([22, 21]);
  const firstPromise = app.startAction({key: 'wb_edit'});
  assert.equal(requests[0].url, '/products/selection/resolve');
  assert.deepEqual(JSON.parse(JSON.stringify(requests[0].body)), {
    mode: 'workspace', ids: [22, 21], wb_ids: [502, 501],
    return_to: '/my-products/beta#workspace-return=11111111111111111111111111111111',
  });
  assert.deepEqual(returnPayloads[0].wbEditTarget, {
    accountId: null, productIds: [502, 501],
  });
  const firstHandoff = app._wbEditHandoff;

  // Start a fresh exact selection while the earlier resolver is outstanding.
  // Its late response must not abort the newer handle or open the editor.
  app.selected = [21, 22];
  app.invalidateWbEditHandoff();
  const secondPromise = app.startAction({key: 'wb_edit'});
  const secondHandoff = app._wbEditHandoff;
  assert.ok(secondHandoff && secondHandoff !== firstHandoff, {
    error: app.error, requests: requests.length, returnPayloads: returnPayloads.length,
  });
  assert.equal(firstHandoff.abort.signal.aborted, true,
    JSON.stringify({firstHandoff, current: app._wbEditHandoff}));
  requests[0].resolve(exactResponse([502, 501], 'old-signed-selection-token'));
  await flush();
  assert.equal(app._wbEditHandoff, secondHandoff,
    'an old resolver response cannot invalidate a newer pending handoff');
  assert.equal(secondHandoff.abort.signal.aborted, false);
  assert.equal(forms.length, 0, 'a stale result never posts to the editor');

  requests[1].resolve(exactResponse([501, 502], 'signed-selection-token'));
  await flush();
  assert.equal(requests[1].body.ids.join(','), '21,22');
  assert.equal(requests[1].body.wb_ids.join(','), '501,502');
  assert.equal(forms.length, 2, 'the form is appended once and submitted once');
  const submitted = forms[0].appended ? forms[1] : forms[0];
  assert.equal(submitted.method, 'post');
  assert.equal(submitted.action, '/products/bulk-edit');
  assert.deepEqual(submitted.children.map(input => [input.name, input.value]), [
    ['csrf_token', 'synthetic-csrf'],
    ['selection_token', 'signed-selection-token'],
  ]);
  assert.equal(returnPayloads[1].wbEditTarget.accountId, null,
    'the exact nullable WB account remains bound to the return');
  assert.equal(requests.length, 2, 'a stored same-tab capsule proceeds without a prompt');

  // A blocked sessionStorage automatically uses the safe signed batch return.
  // The seller can reselect after return; the exact server selection remains signed.
  storageAvailable = false;
  const fallback = makeApp([22], {batch: true});
  const fallbackPromise = fallback.startAction({key: 'wb_edit'});
  assert.equal(requests.length, 3, 'storage failure does not block or prompt for the handoff');
  assert.match(requests[2].body.return_to,
    /^\/my-products\/beta\?import_batch=signed\.import-batch_token$/);
  requests[2].resolve(exactResponse([502], 'signed-fallback-selection-token'));
  await flush();
  assert.equal(requests[2].body.ids.join(','), '22');
  assert.equal(requests[2].body.wb_ids.join(','), '502');
  assert.equal(forms.length, 4);
  console.log('WB workspace exact handoff and fallback: passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
"""


def test_workspace_handoff_is_exact_and_stale_results_do_not_cross_requests():
    node = shutil.which("node")
    assert node, "Node is required for the isolated workspace handoff contract check"
    result = subprocess.run(
        [node, "-e", NODE_CASES, str(ROOT / "static" / "my-products-beta.js")],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr
