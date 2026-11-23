"""Exact My Products handoff to the Ozon existing-card editor."""

from pathlib import Path
import re
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]


NODE_CASES = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const plain = value => JSON.parse(JSON.stringify(value));
const nonce = '0123456789abcdef0123456789abcdef';

let component;
let postHandler = () => Promise.reject(new Error('unexpected POST'));
const requests = [];
const assigned = [];
const stored = [];
const consumed = [];
let storageAvailable = true;
const location = {
  origin: 'https://seller.test',
  assign(value) { assigned.push(value); },
};
const context = {
  window: {
    location,
    SellerHubCommonContent: {
      storeWorkspaceReturn(payload) {
        stored.push(plain(payload));
        return storageAvailable ? nonce : null;
      },
      consumeWorkspaceReturn(value) { consumed.push(value); return value === nonce; },
    },
    shVue: {
      bootstrapData() { return {
        workspaceOwner: '7:9',
        wbConnected: true,
        ozonEnabled: true,
        ozonAccounts: [],
        urls: {
          feed: '/api/my-products',
          workspace: '/my-products/beta',
          ozonWorkspaceUpdateHandoff: '/marketplaces/listings/update-drafts/workspace-handoff',
        },
      }; },
      mount(_selector, options) { component = options; },
    },
  },
  document: {
    activeElement: {focus() {}},
  },
  sessionStorage: {getItem() { return null; }, setItem() {}, removeItem() {}},
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
const sourceText = fs.readFileSync(process.argv[1], 'utf8');
vm.runInContext(sourceText, context);
assert.ok(component && component.methods);

function listing(id, accountId, patch = {}) {
  return {
    id,
    account_id: accountId,
    marketplace_code: 'ozon',
    link_status: 'linked',
    account_is_active: true,
    is_available: true,
    is_archived: false,
    ...patch,
  };
}
function product(id, listings) { return {id, ozon: {listings}}; }
function makeApp(items, accounts, {batch = null} = {}) {
  const app = component.data();
  Object.assign(app, component.methods);
  Object.entries(component.computed).forEach(([name, getter]) => {
    Object.defineProperty(app, name, {get() { return getter.call(app); }});
  });
  app.items = items;
  app.selected = items.map(row => row.id);
  app.ozonAccounts = plain(accounts);
  app.importBatchMode = batch !== null;
  app.importBatch = batch || '';
  app.importBatchSelectionVerified = batch !== null;
  app.loading = false;
  app.loadingMore = false;
  app.selectAllLoading = false;
  app.itemsScope = app.feedScopeKey();
  app.$fmt = {plural() { return 'товаров'; }};
  app.$nextTick = callback => callback.call(app);
  const dialog = {
    open: false,
    showModal() { this.open = true; },
    close() { this.open = false; },
  };
  app.$refs = {
    ozonPriceAccountDialog: dialog,
    ozonPriceAccount: {focus() {}},
  };
  app.$api = {
    post(url, body, options = {}) {
      const call = {url, body: plain(body), signal: options.signal};
      requests.push(call);
      return postHandler(call);
    },
  };
  return app;
}
function assertSafeTarget(accountId, token = 'signed.update.token') {
  assert.equal(assigned.length, 1);
  const target = new URL(assigned[0], location.origin);
  assert.equal(target.origin, location.origin);
  assert.equal(target.pathname, '/marketplaces/listings/');
  assert.equal(target.searchParams.get('marketplace'), 'ozon');
  assert.equal(target.searchParams.get('account_id'), String(accountId));
  assert.equal(target.searchParams.get('workspace_update'), '1');
  assert.equal(target.hash, '#workspace-update=' + token);
}

(async () => {
  const accounts = [
    {id: 11, label: 'Shop A', connection_status: 'connected'},
    {id: 12, label: 'Shop B', connection_status: 'connected'},
  ];
  const bothShops = [
    product(101, [listing(1001, 11), listing(2001, 12)]),
    product(102, [listing(1002, 11), listing(2002, 12)]),
  ];

  // One exact connected shop starts one POST directly, without a chooser.
  requests.length = 0;
  assigned.length = 0;
  stored.length = 0;
  const direct = makeApp(bothShops, [accounts[0]], {batch: 'signed-import-batch'});
  postHandler = () => Promise.resolve({success: true,
    url: '/marketplaces/listings/?marketplace=ozon&account_id=11&workspace_update=1#workspace-update=signed.update.token'});
  direct.startAction({key: 'ozon_workspace_update'});
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.equal(requests.length, 1);
  assert.equal(requests[0].url, '/marketplaces/listings/update-drafts/workspace-handoff');
  assert.deepEqual(Object.keys(requests[0].body).sort(),
    ['account_id', 'listing_ids', 'return_to', 'source_ids']);
  assert.deepEqual(requests[0].body.source_ids, [101, 102]);
  assert.deepEqual(requests[0].body.listing_ids, [1001, 1002]);
  assert.equal(requests[0].body.account_id, 11);
  assert.equal(requests[0].body.return_to,
    '/my-products/beta?import_batch=signed-import-batch#workspace-return=' + nonce);
  assert.deepEqual(stored[0], {
    owner: '7:9',
    ozonUpdateTarget: {accountId: 11, listingIds: [1001, 1002]},
    selectionIds: [101, 102],
    scope: {activeTab: '', search: '', supplier: '', has_photos: '', stock: '', sort: '', visibleCount: 2},
    batch: 'signed-import-batch',
  });
  assertSafeTarget(11);
  assert.equal(direct.ozonWorkspaceUpdateBusy, false);

  // Multiple complete shops use the existing account chooser. A changed exact
  // mapping after the choice is refused before the endpoint is called.
  requests.length = 0;
  assigned.length = 0;
  const chooser = makeApp(plain(bothShops), accounts);
  assert.equal(chooser.openOzonWorkspaceUpdate(chooser.selectedItems, chooser.selected), true);
  assert.equal(requests.length, 0);
  assert.equal(chooser.$refs.ozonPriceAccountDialog.open, true);
  assert.equal(chooser.ozonAccountChooserKind, 'workspace_update');
  chooser.ozonPriceAccountId = '12';
  chooser.items[1].ozon.listings[1].id = 2999;
  assert.equal(chooser.submitOzonWorkspaceUpdate(12, chooser.ozonWorkspaceUpdateReview), false);
  assert.equal(requests.length, 0);
  assert.equal(assigned.length, 0);

  // A valid explicit choice is bound to that account's ordered listing set.
  const explicit = makeApp(bothShops, accounts);
  explicit.openOzonWorkspaceUpdate(explicit.selectedItems, explicit.selected);
  explicit.ozonPriceAccountId = '12';
  requests.length = 0;
  assigned.length = 0;
  postHandler = () => Promise.resolve({success: true,
    url: '/marketplaces/listings/?marketplace=ozon&account_id=12&workspace_update=1#workspace-update=selected.shop.token'});
  await explicit.continueOzonPriceHandoff();
  assert.deepEqual(requests[0].body.listing_ids, [2001, 2002]);
  assert.equal(requests[0].body.account_id, 12);
  assertSafeTarget(12, 'selected.shop.token');

  // URL-safe signed fragments can exceed the old 8 KiB query-token limit.
  requests.length = 0;
  assigned.length = 0;
  const longToken = 'x'.repeat(9000);
  const longTokenApp = makeApp([product(121, [listing(1121, 11)])], [accounts[0]]);
  postHandler = () => Promise.resolve({success: true,
    url: '/marketplaces/listings/?marketplace=ozon&account_id=11&workspace_update=1#workspace-update=' + longToken});
  await longTokenApp.openOzonWorkspaceUpdate(longTokenApp.selectedItems, longTokenApp.selected);
  assertSafeTarget(11, longToken);

  // Partial account groups are offered with exact counts and remain an
  // explicit choice even when only one group could be used.
  const mixed = makeApp([
    product(1, [listing(101, 11)]),
    product(2, [listing(202, 12)]),
  ], accounts);
  const mixedAction = mixed.actionGroups.flatMap(group => group.items)
    .find(action => action.key === 'ozon_workspace_update');
  assert.equal(mixedAction.disabled, false);
  assert.equal(mixed.openOzonWorkspaceUpdate(mixed.selectedItems, mixed.selected), true);
  assert.equal(mixed.ozonWorkspaceUpdateReview.partial, true);
  assert.equal(mixed.ozonWorkspaceUpdateReview.total, 2);
  assert.deepEqual(plain(mixed.ozonWorkspaceUpdateReview.choices.map(choice => [
    choice.account_id, choice.eligible_count, choice.total_count, choice.excluded_rows[0].id,
  ])), [[11, 1, 2, 2], [12, 1, 2, 1]]);
  assert.equal(mixed.$refs.ozonPriceAccountDialog.open, true);
  const onePartial = makeApp([
    product(5, [listing(105, 11)]),
    product(6, []),
  ], [accounts[0]]);
  assert.equal(onePartial.openOzonWorkspaceUpdate(onePartial.selectedItems, onePartial.selected), true);
  assert.equal(onePartial.ozonWorkspaceUpdateReview.choices.length, 1);
  assert.equal(onePartial.ozonWorkspaceUpdateReview.choices[0].partial, true);
  assert.equal(onePartial.$refs.ozonPriceAccountDialog.open, true,
    'one partial account still requires an explicit Continue');
  const fullAndPartial = makeApp([
    product(7, [listing(107, 11), listing(207, 12)]),
    product(8, [listing(208, 12)]),
  ], accounts);
  assert.equal(fullAndPartial.openOzonWorkspaceUpdate(
    fullAndPartial.selectedItems, fullAndPartial.selected,
  ), true);
  assert.deepEqual(plain(fullAndPartial.ozonWorkspaceUpdateReview.choices.map(choice => choice.partial)),
    [true, false], 'a full shop and a partial shop both appear in account order');
  assert.equal(fullAndPartial.$refs.ozonPriceAccountDialog.open, true);
  const focusTrigger = {count: 0, focus() { this.count += 1; }};
  const focusApp = makeApp([
    product(3, [listing(103, 11)]),
    product(4, [listing(204, 12)]),
  ], accounts);
  focusApp.openOzonWorkspaceUpdate(focusApp.selectedItems, focusApp.selected, focusTrigger);
  focusApp.cancelOzonWorkspaceUpdateReview();
  assert.equal(focusTrigger.count, 1,
    'closing the partial chooser returns keyboard focus to the bulk toolbar trigger');

  // The existing-card POST and return capsule carry the exact chosen subset,
  // while the original full global selection stays checked.
  requests.length = 0;
  assigned.length = 0;
  stored.length = 0;
  const partial = makeApp([
    product(11, [listing(111, 11)]),
    product(12, [listing(212, 12)]),
    product(13, [listing(113, 11)]),
  ], accounts, {batch: 'signed-import-batch'});
  assert.equal(partial.openOzonWorkspaceUpdate(partial.selectedItems, partial.selected), true);
  partial.ozonPriceAccountId = '11';
  postHandler = () => Promise.resolve({success: true,
    url: '/marketplaces/listings/?marketplace=ozon&account_id=11&workspace_update=1#workspace-update=partial.token'});
  await partial.continueOzonPriceHandoff();
  assert.deepEqual(requests.at(-1).body.source_ids, [11, 13]);
  assert.deepEqual(requests.at(-1).body.listing_ids, [111, 113]);
  assert.deepEqual(stored.at(-1).selectionIds, [11, 12, 13]);
  assert.deepEqual(stored.at(-1).actionSelectionIds, [11, 13]);
  assert.deepEqual(partial.selected, [11, 12, 13]);
  assertSafeTarget(11, 'partial.token');

  // A row becoming eligible after review invalidates the partial group. It
  // cannot be silently widened at the explicit Continue step.
  requests.length = 0;
  assigned.length = 0;
  const staleMembership = makeApp([
    product(21, [listing(121, 11)]),
    product(22, [listing(222, 12)]),
  ], accounts);
  assert.equal(staleMembership.openOzonWorkspaceUpdate(
    staleMembership.selectedItems, staleMembership.selected,
  ), true);
  staleMembership.ozonPriceAccountId = '11';
  staleMembership.items[1].ozon.listings.push(listing(122, 11));
  assert.equal(await staleMembership.continueOzonPriceHandoff(), false);
  assert.equal(requests.length, 0);
  assert.equal(assigned.length, 0);

  // A complete 201-item group still exceeds the existing global bulk limit.
  const tooManyRows = Array.from({length: 201}, (_, index) => {
    const id = index + 1;
    return product(id, [listing(id + 1000, 11)]);
  });
  const tooMany = makeApp(tooManyRows, [accounts[0]]);
  const tooManyAction = tooMany.actionGroups.flatMap(group => group.items)
    .find(action => action.key === 'ozon_workspace_update');
  assert.equal(tooManyAction.disabled, true);
  assert.match(tooManyAction.hint, /не больше 200/);
  assert.equal(tooMany.resolveOzonPriceAccountChoices(
    tooManyRows.slice(0, 101), tooManyRows.slice(0, 101).map(row => row.id),
  ).reason, 'no_common_account', 'an exact 101-item price group is not truncated to 100');

  // Browser storage failure still posts a safe batch return URL without a
  // workspace fragment; it never changes the target or drops the batch.
  storageAvailable = false;
  requests.length = 0;
  assigned.length = 0;
  const fallback = makeApp(bothShops, [accounts[0]], {batch: 'signed-import-batch'});
  postHandler = () => Promise.resolve({success: true,
    url: '/marketplaces/listings/?marketplace=ozon&account_id=11&workspace_update=1#workspace-update=fallback.token'});
  await fallback.openOzonWorkspaceUpdate(fallback.selectedItems, fallback.selected);
  assert.equal(requests[0].body.return_to, '/my-products/beta?import_batch=signed-import-batch');
  assertSafeTarget(11, 'fallback.token');
  storageAvailable = true;

  // A response for a stale selection is ignored and its local capsule removed.
  requests.length = 0;
  assigned.length = 0;
  consumed.length = 0;
  let resolvePost;
  postHandler = () => new Promise(resolve => { resolvePost = resolve; });
  const stale = makeApp([product(301, [listing(1301, 11)])], [accounts[0]]);
  const pending = stale.openOzonWorkspaceUpdate(stale.selectedItems, stale.selected);
  stale.selected = [];
  stale.invalidateOzonWorkspaceUpdate();
  resolvePost({success: true,
    url: '/marketplaces/listings/?marketplace=ozon&account_id=11&workspace_update=1#workspace-update=stale.token'});
  assert.equal(await pending, false);
  assert.equal(assigned.length, 0);
  assert.ok(consumed.includes(nonce));

  // A changed filter or connected-account state also makes the response stale.
  for (const staleState of ['filter', 'account']) {
    requests.length = 0;
    assigned.length = 0;
    let resolveStale;
    postHandler = () => new Promise(resolve => { resolveStale = resolve; });
    const app = makeApp([product(351, [listing(1351, 11)])], [accounts[0]]);
    const pendingHandoff = app.openOzonWorkspaceUpdate(app.selectedItems, app.selected);
    if (staleState === 'filter') app.filters.search = 'changed';
    else app.ozonAccounts[0].connection_status = 'disconnected';
    resolveStale({success: true,
      url: '/marketplaces/listings/?marketplace=ozon&account_id=11&workspace_update=1#workspace-update=stale.state.token'});
    assert.equal(await pendingHandoff, false);
    assert.equal(assigned.length, 0);
  }

  // A server response cannot redirect to another origin or an unexpected route.
  requests.length = 0;
  assigned.length = 0;
  const unsafe = makeApp([product(401, [listing(1401, 11)])], [accounts[0]]);
  postHandler = () => Promise.resolve({success: true,
    url: 'https://attacker.test/marketplaces/listings/?marketplace=ozon&account_id=11&workspace_update=1#workspace-update=bad'});
  await unsafe.openOzonWorkspaceUpdate(unsafe.selectedItems, unsafe.selected);
  assert.equal(assigned.length, 0);
  assert.match(unsafe.error, /Не удалось проверить точный выбор/);

  const queryToken = makeApp([product(402, [listing(1402, 11)])], [accounts[0]]);
  postHandler = () => Promise.resolve({success: true,
    url: '/marketplaces/listings/?marketplace=ozon&account_id=11&workspace_update=abc'});
  await queryToken.openOzonWorkspaceUpdate(queryToken.selectedItems, queryToken.selected);
  assert.equal(assigned.length, 0);
  assert.match(queryToken.error, /Не удалось проверить точный выбор/);
})().catch(error => { console.error(error); process.exitCode = 1; });
"""


CAPSULE_CASES = r"""
const assert = require('node:assert/strict');
const helpers = require(process.argv[1]);
const storage = new Map();
const fakeStorage = {
  getItem(key) { return storage.has(key) ? storage.get(key) : null; },
  setItem(key, value) { storage.set(key, value); },
  removeItem(key) { storage.delete(key); },
};
const deterministicCrypto = {getRandomValues(bytes) { bytes.fill(0x11); return bytes; }};
const scope = {activeTab: '', search: 'shoe', supplier: 'one', has_photos: '', stock: '', sort: 'title', visibleCount: 50};
const payload = {
  owner: '7:9',
  ozonUpdateTarget: {accountId: 11, listingIds: [101, 102]},
  selectionIds: [1, 2],
  scope,
  batch: null,
};
const nonce = helpers.storeWorkspaceReturn(payload, {
  now: 10, storage: fakeStorage, crypto: deterministicCrypto,
});
assert.equal(nonce, '11'.repeat(16));
const capsule = JSON.parse(storage.get(helpers.WORKSPACE_RETURN_SESSION_KEY));
assert.equal(capsule.v, 6);
assert.deepEqual(Object.keys(capsule).sort(), [
  'batch', 'expiresAt', 'nonce', 'owner', 'ozonUpdateTarget', 'scope', 'selectionIds', 'v',
]);
assert.deepEqual(capsule.ozonUpdateTarget, payload.ozonUpdateTarget);
assert.deepEqual(helpers.workspaceReturnFromHash('#workspace-return=' + nonce, {
  owner: '7:9', now: 11, storage: fakeStorage,
}), capsule);
assert.equal(helpers.workspaceReturnFromHash('#workspace-return=' + nonce, {
  owner: '7:9', now: 11, storage: fakeStorage,
  ozonUpdateTarget: {accountId: 12, listingIds: [101, 102]},
}), null);
assert.equal(storage.has(helpers.WORKSPACE_RETURN_SESSION_KEY), false,
  'a mismatched account consumes the return capsule');

// The My Products reader restores the source selection only when the current
// feed still has the same exact connected-account listing IDs in order.
assert.equal(helpers.storeWorkspaceReturn(payload, {
  now: Date.now(), storage: fakeStorage, crypto: deterministicCrypto,
}), nonce);
assert.ok(fakeStorage.getItem(helpers.WORKSPACE_RETURN_SESSION_KEY));
let pageComponent;
const pageWindow = {
  location: {origin: 'https://seller.test', search: '', hash: '#workspace-return=' + nonce},
  sessionStorage: fakeStorage,
  SellerHubCommonContent: Object.assign({}, helpers),
  shVue: {
    bootstrapData() { return {
      workspaceOwner: '7:9', wbConnected: true, ozonEnabled: true,
      ozonAccounts: [{id: 11, label: 'Shop A', connection_status: 'connected'}],
      importBatch: null, batchKind: null, supplierRefreshBatch: null,
      supplierRefreshBatchInvalid: false,
      urls: {feed: '/api/my-products', workspace: '/my-products/beta'},
    }; },
    mount(_selector, options) { pageComponent = options; },
  },
};
const pageContext = {
  window: pageWindow,
  sessionStorage: fakeStorage,
  document: {activeElement: {focus() {}}, getElementById() { return null; }},
  URL, URLSearchParams, AbortController, TextEncoder,
  setTimeout, clearTimeout, setInterval, clearInterval, console,
};
vm.createContext(pageContext);
vm.runInContext(require('node:fs').readFileSync(process.argv[1], 'utf8'), pageContext);
const pageCapsule = pageWindow.SellerHubCommonContent.workspaceReturnFromHash(
  '#workspace-return=' + nonce, {owner: '7:9', storage: fakeStorage},
);
assert.equal(pageCapsule && pageCapsule.v, 6);
vm.runInContext(require('node:fs').readFileSync(process.argv[2], 'utf8'), pageContext);
const returnedApp = pageComponent.data();
Object.assign(returnedApp, pageComponent.methods);
returnedApp.ozonAccounts = [{id: 11, label: 'Shop A', connection_status: 'connected'}];
const returnedRows = [
  {id: 2, ozon: {listings: [{id: 102, account_id: 11, marketplace_code: 'ozon', link_status: 'linked', account_is_active: true, is_available: true, is_archived: false}]}},
  {id: 3, ozon: {listings: [{id: 103, account_id: 11, marketplace_code: 'ozon', link_status: 'linked', account_is_active: true, is_available: true, is_archived: false}]}},
  {id: 1, ozon: {listings: [{id: 101, account_id: 11, marketplace_code: 'ozon', link_status: 'linked', account_is_active: true, is_available: true, is_archived: false}]}},
];
// A returned feed page may contain unselected rows, in another order.
assert.equal(returnedApp.workspaceReturnOzonUpdateMatches(returnedRows, [1, 2]), true);
returnedRows[0].ozon.listings[0].id = 999;
assert.equal(returnedApp.workspaceReturnOzonUpdateMatches(returnedRows, [1, 2]), false);
returnedRows[0].ozon.listings[0].id = 102;
assert.equal(returnedApp.workspaceReturnOzonUpdateMatches(
  returnedRows.filter(row => row.id !== 2), [1, 2],
), false, 'a missing selected row fails closed');
assert.equal(returnedApp.workspaceReturnOzonUpdateMatches(
  returnedRows.concat([JSON.parse(JSON.stringify(returnedRows[2]))]), [1, 2],
), false, 'duplicate feed rows fail closed');

const twoHundred = Array.from({length: 200}, (_, index) => index + 1);
assert.ok(helpers.storeWorkspaceReturn({
  owner: '7:9',
  ozonUpdateTarget: {accountId: 11, listingIds: twoHundred.map(id => id + 1000)},
  selectionIds: twoHundred,
  scope: {activeTab: '', search: '', supplier: '', has_photos: '', stock: '', sort: '', visibleCount: 200},
  batch: null,
}, {now: 10, storage: fakeStorage, crypto: deterministicCrypto}));
assert.equal(helpers.storeWorkspaceReturn({
  owner: '7:9',
  ozonUpdateTarget: {accountId: 11, listingIds: [1, 1]},
  selectionIds: [1, 2], scope, batch: null,
}, {now: 10, storage: fakeStorage, crypto: deterministicCrypto}), null);
"""


class MyProductsOzonWorkspaceUpdateHandoffTest(unittest.TestCase):
    def test_exact_account_handoff_and_stale_guard(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is required for the isolated UI logic check")
        result = subprocess.run(
            [node, "-e", NODE_CASES, str(ROOT / "static" / "my-products-beta.js")],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_workspace_return_capsule_is_typed_and_ordered(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is required for the isolated UI logic check")
        result = subprocess.run(
            [node, "-e", CAPSULE_CASES, str(ROOT / "static" / "common-product-content.js"), str(ROOT / "static" / "my-products-beta.js")],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_template_adds_workspace_action_and_versions_both_assets(self):
        template = (ROOT / "templates" / "my_products_beta.html").read_text()
        script = (ROOT / "static" / "my-products-beta.js").read_text()
        helper = (ROOT / "static" / "common-product-content.js").read_text()
        self.assertIn("label: 'Изменить карточки Ozon'", script)
        self.assertIn("key: 'ozon_workspace_update'", script)
        self.assertIn('сначала проверим выбранные карточки; применение подтвердите отдельно', script)
        self.assertIn("'ozonWorkspaceUpdateHandoff': url_for('marketplace_listings.workspace_update_handoff')", template)
        self.assertIn('Переход к редактору не отправит изменения автоматически.', template)
        self.assertIn("workspaceReturnOzonUpdateMatches", script)
        self.assertIn("WORKSPACE_RETURN_V6_CAPSULE_KEYS", helper)
        self.assertIn("Изменение цены не начнётся автоматически.", template)
        self.assertIn("'ozonCommercial': url_for('marketplace_commercial.index')", template)
        self.assertIn("label: 'Изменить цены Ozon'", script)
        js_asset = re.search(r"filename='my-products-beta\.js', v='([^']+)'", template)
        helper_asset = re.search(r"filename='common-product-content\.js', v='([^']+)'", template)
        self.assertIsNotNone(js_asset)
        self.assertIsNotNone(helper_asset)
        self.assertRegex(js_asset.group(1), r"^\d{8}-[a-z0-9][a-z0-9-]*$")
        self.assertRegex(helper_asset.group(1), r"^\d{8}-[a-z0-9][a-z0-9-]*$")
