"""Read-only exact-set handoff from My Products to Ozon price comparison."""

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

let component;
const assigned = [];
const storedPriceReturns = [];
const returnNonce = '0123456789abcdef0123456789abcdef';
let returnStorageAvailable = true;
const location = {
  origin: 'https://seller.test',
  assign(value) { assigned.push(value); },
};
const document = {activeElement: {focus() {}}};
const context = {
  window: {
    location,
    SellerHubCommonContent: {
      storeWorkspaceReturn(payload) {
        storedPriceReturns.push(payload);
        return returnStorageAvailable ? returnNonce : null;
      },
    },
    shVue: {
      bootstrapData() { return {
        workspaceOwner: '7:9', ozonEnabled: true, ozonAccounts: [], urls: {},
      }; },
      mount(_selector, options) { component = options; },
    },
  },
  document,
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
function product(id, listingRows) {
  return {id, ozon: {listings: listingRows}};
}
function makeApp(items, accounts, selected = items.map(row => row.id)) {
  const dialog = {
    open: false,
    showModal() { this.open = true; },
    close() { this.open = false; },
  };
  const app = {
    items,
    selected,
    filters: {search: '', supplier: '', has_photos: '', stock: '', sort: ''},
    searchInput: '',
    activeTab: '',
    pagination: {page: 1, per_page: 50, total: items.length, has_next: false},
    loading: false,
    itemsScope: '',
    ozonAccounts: accounts,
    ozonEnabled: true,
    wbConnected: true,
    ozonPending: null,
    ozonStorageProblem: false,
    ozonBusy: '',
    ozonSessionEnded: false,
    urls: {ozonCommercial: '/marketplaces/commercial/'},
    ozonPriceError: '',
    ozonPriceDialogError: '',
    ozonPriceAccountId: '',
    ozonPriceHandoffReview: null,
    importBatchMode: false,
    importBatch: '',
    returnScopeBuildCount: 0,
    $fmt: {plural() { return 'товаров'; }, money() { return ''; }},
    $refs: {
      ozonDialog: {
        open: false,
        showModal() { this.open = true; },
        close() { this.open = false; },
      },
      ozonAccount: {focus() {}},
      ozonPriceAccountDialog: dialog,
      ozonPriceAccount: {focus() {}},
      ozonPriceError: {focus() {}},
    },
    $nextTick(callback) { callback.call(this); },
  };
  Object.defineProperty(app, 'selectedItems', {
    get() {
      const ids = new Set(this.selected);
      return this.items.filter(row => ids.has(row.id));
    },
  });
  Object.assign(app, component.methods);
  const buildWorkspaceReturnScope = app.buildWorkspaceReturnScope;
  app.buildWorkspaceReturnScope = function () {
    this.returnScopeBuildCount += 1;
    return buildWorkspaceReturnScope.call(this);
  };
  app.itemsScope = app.feedScopeKey();
  return app;
}
function assertHandoffUrl(accountId, listingIds, withReturnFragment = true) {
  assert.equal(assigned.length, 1);
  const target = new URL(assigned[0], location.origin);
  assert.equal(target.origin, location.origin);
  assert.equal(target.pathname, '/marketplaces/commercial/');
  assert.equal(target.searchParams.get('account_id'), String(accountId));
  assert.equal(target.searchParams.get('bulk_listing_ids'), listingIds.join(','));
  assert.equal(target.hash, withReturnFragment ? '#workspace-return=' + returnNonce : '');
  assert.equal(target.searchParams.has('preview'), false);
  assert.equal(target.searchParams.has('confirm'), false);
}

const accounts = [{id: 11, label: 'Shop A'}, {id: 12, label: 'Shop B'}];
const bothShops = [
  product(101, [listing(1001, 11), listing(2001, 12)]),
  product(102, [listing(1002, 11), listing(2002, 12)]),
];
const choicesApp = makeApp(bothShops, accounts);
const choices = choicesApp.resolveOzonPriceAccountChoices(bothShops, [101, 102]);
assert.equal(choices.reason, null);
assert.deepEqual(JSON.parse(JSON.stringify(choices.choices)), [
  {account_id: 11, account_label: 'Shop A', listing_ids: [1001, 1002]},
  {account_id: 12, account_label: 'Shop B', listing_ids: [2001, 2002]},
]);

// Exactly one complete shop navigates directly to a GET handoff.
assigned.length = 0;
const directApp = makeApp(bothShops, [accounts[0]]);
directApp.startAction({key: 'ozon_prices'});
assertHandoffUrl(11, [1001, 1002]);
assert.equal(directApp.returnScopeBuildCount, 1, 'Ozon return scope is captured once per launch');
assert.deepEqual(plain(storedPriceReturns.at(-1)), {
  owner: '7:9',
  priceTarget: {channel: 'ozon', ids: [1001, 1002], accountId: 11},
  selectionIds: [101, 102],
  scope: {
    activeTab: '', search: '', supplier: '', has_photos: '',
    stock: '', sort: '', visibleCount: 2,
  },
  batch: null,
});

// Multiple complete shops require a native choice and capture both exact sets.
assigned.length = 0;
const chooserApp = makeApp(bothShops, accounts);
chooserApp.startAction({key: 'ozon_prices'});
assert.equal(assigned.length, 0);
assert.equal(chooserApp.$refs.ozonPriceAccountDialog.open, true);
assert.deepEqual(chooserApp.ozonPriceHandoffReview.imported_product_ids, [101, 102]);
chooserApp.ozonPriceAccountId = '12';
chooserApp.continueOzonPriceHandoff();
assertHandoffUrl(12, [2001, 2002]);
assert.equal(chooserApp.returnScopeBuildCount, 1, 'cabinet choice creates one return scope');
assert.equal(storedPriceReturns.at(-1).priceTarget.accountId, 12);
assert.deepEqual(plain(storedPriceReturns.at(-1).priceTarget.ids), [2001, 2002]);

// A changed row/listing after review closes the chooser without navigation.
assigned.length = 0;
const staleApp = makeApp(bothShops.map(row => product(row.id, row.ozon.listings.slice())), accounts);
staleApp.startAction({key: 'ozon_prices'});
staleApp.ozonPriceAccountId = '12';
staleApp.items[1].ozon.listings[1].id = 9999;
staleApp.continueOzonPriceHandoff();
assert.equal(assigned.length, 0);
assert.match(staleApp.ozonPriceError, /изменились после проверки/);

// A signed import-batch return capsule preserves the internal selected IDs
// and chosen cabinet while targeting only the exact Ozon listing IDs.
assigned.length = 0;
const batchApp = makeApp(bothShops, [accounts[0]]);
batchApp.importBatchMode = true;
batchApp.importBatch = 'signed-import-batch-token';
batchApp.itemsScope = batchApp.feedScopeKey();
batchApp.startAction({key: 'ozon_prices'});
assertHandoffUrl(11, [1001, 1002]);
assert.equal(storedPriceReturns.at(-1).batch, 'signed-import-batch-token');
assert.deepEqual(plain(storedPriceReturns.at(-1).selectionIds), [101, 102]);

// If session storage is unavailable, the established provider/read target
// still opens unchanged, without a return fragment.
assigned.length = 0;
returnStorageAvailable = false;
const fallbackApp = makeApp(bothShops, [accounts[0]]);
fallbackApp.startAction({key: 'ozon_prices'});
assertHandoffUrl(11, [1001, 1002], false);
assert.equal(fallbackApp.returnScopeBuildCount, 1);
returnStorageAvailable = true;

// WB handoff stores imported-product selection separately from its Product IDs.
assigned.length = 0;
const wbRow = product(301, []);
wbRow.wb = {product_id: 4301};
const wbApp = makeApp([wbRow], accounts);
wbApp.urls.wbPriceChange = '/prices/change';
wbApp.startAction({key: 'wb_prices'});
assert.equal(assigned.length, 1);
assert.equal(wbApp.returnScopeBuildCount, 1, 'WB return scope is captured once per launch');
const wbTarget = new URL(assigned[0], location.origin);
assert.equal(wbTarget.pathname, '/prices/change');
assert.equal(wbTarget.searchParams.get('product_ids'), '4301');
assert.equal(wbTarget.hash, '#workspace-return=' + returnNonce);
assert.deepEqual(plain(storedPriceReturns.at(-1)), {
  owner: '7:9',
  priceTarget: {channel: 'wb', ids: [4301], accountId: null},
  selectionIds: [301],
  scope: {
    activeTab: '', search: '', supplier: '', has_photos: '',
    stock: '', sort: '', visibleCount: 1,
  },
  batch: null,
});

// Missing or invalid rows are never silently dropped to make a partial batch.
const partial = [
  product(101, [listing(1001, 11), listing(2001, 12)]),
  product(102, [listing(2002, 12)]),
];
const partialResult = makeApp(partial, accounts).resolveOzonPriceAccountChoices(partial, [101, 102]);
assert.deepEqual(JSON.parse(JSON.stringify(partialResult.choices)), [
  {account_id: 12, account_label: 'Shop B', listing_ids: [2001, 2002]},
]);
const noCommon = [
  product(101, [listing(1001, 11)]),
  product(102, [listing(2002, 12)]),
];
assigned.length = 0;
const noCommonApp = makeApp(noCommon, accounts);
noCommonApp.startAction({key: 'ozon_prices'});
assert.equal(assigned.length, 0);
assert.match(noCommonApp.ozonPriceError, /Не найден один магазин Ozon/);

assert.equal(
  makeApp(bothShops, [{id: 11, label: 'Shop A', connection_status: 'unchecked'}])
    .resolveOzonPriceAccountChoices(bothShops, [101, 102]).choices.length,
  0,
);

// A disconnected active account stays in the legacy draft-preparation list,
// while it is excluded from the price handoff chooser.
const disconnectedAccount = {
  id: 11, label: 'Shop A', connection_status: 'disconnected',
};
const disconnectedApp = makeApp(bothShops, [disconnectedAccount]);
assert.equal(
  disconnectedApp.resolveOzonPriceAccountChoices(bothShops, [101, 102]).choices.length,
  0,
);
const primaryActions = component.computed.actionGroups.call(disconnectedApp)
  .flatMap(group => group.items);
const legacyOzonAction = primaryActions.find(action => action.key === 'ozon');
assert.ok(legacyOzonAction && !legacyOzonAction.disabled);
disconnectedApp.startAction({key: 'ozon'});
assert.equal(disconnectedApp.ozonAccountId, '11');
assert.equal(disconnectedApp.$refs.ozonDialog.open, true);

const blockedFlags = [
  {is_available: false},
  {is_archived: true},
  {account_is_active: false},
  {link_status: 'ambiguous'},
  {marketplace_code: 'wb'},
];
for (const patch of blockedFlags) {
  const badItems = [product(101, [listing(1001, 11, patch)])];
  const result = makeApp(badItems, [accounts[0]]).resolveOzonPriceAccountChoices(badItems, [101]);
  assert.equal(result.choices.length, 0);
}

const duplicateTarget = [
  product(101, [listing(1001, 11)]),
  product(102, [listing(1001, 11)]),
];
assert.equal(
  makeApp(duplicateTarget, [accounts[0]])
    .resolveOzonPriceAccountChoices(duplicateTarget, [101, 102]).choices.length,
  0,
);
assert.equal(
  makeApp(bothShops, accounts)
    .resolveOzonPriceAccountChoices(bothShops, [101]).reason,
  'selection',
);
const tooMany = Array.from({length: 101}, (_, index) => {
  const id = index + 1;
  return product(id, [listing(id + 1000, 11)]);
});
assert.equal(
  makeApp(tooMany, [accounts[0]])
    .resolveOzonPriceAccountChoices(tooMany, tooMany.map(row => row.id)).reason,
  'limit',
);
"""


class MyProductsOzonPriceHandoffTest(unittest.TestCase):
    def test_exact_account_and_listing_mapping_is_fail_closed(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('Node.js is required for the isolated UI logic check')
        result = subprocess.run(
            [node, '-e', NODE_CASES, str(ROOT / 'static' / 'my-products-beta.js')],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_template_keeps_normal_price_entry_and_uses_read_only_get_target(self):
        template = (ROOT / 'templates' / 'my_products_beta.html').read_text()
        self.assertIn('Ценовые заявки Ozon', template)
        self.assertIn("'ozonCommercial': url_for('marketplace_commercial.index')", template)
        script = (ROOT / 'static' / 'my-products-beta.js').read_text()
        self.assertIn('Изменить цены Ozon', script)
        self.assertIn('bulk_listing_ids', script)
        script_asset = re.search(
            r"filename='my-products-beta\.js', v='([^']+)'",
            template,
        )
        self.assertIsNotNone(script_asset, 'My Products script must use a versioned static URL')
        self.assertRegex(script_asset.group(1), r'^\d{8}-[a-z0-9][a-z0-9-]*$')


if __name__ == '__main__':
    unittest.main()
