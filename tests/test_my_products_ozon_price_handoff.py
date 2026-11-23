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
const boundPriceReturns = [];
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
      bindPriceWorkspaceResult(hash, payload) {
        boundPriceReturns.push({hash, payload: plain(payload)});
        return hash;
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
  Object.defineProperty(app, 'selectionRowsVerified', {
    get() { return component.computed.selectionRowsVerified.call(this); },
  });
  Object.assign(app, component.methods);
  Object.defineProperty(app, 'actionGroups', {
    get() { return component.computed.actionGroups.call(this); },
  });
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
assert.deepEqual(JSON.parse(JSON.stringify(choices.choices.map(choice => ({
  account_id: choice.account_id,
  account_label: choice.account_label,
  listing_ids: choice.listing_ids,
})))), [
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
batchApp.importBatchSelectionVerified = true;
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
const wbPriceIntent = component.computed.bulkIntentGroups.call(wbApp)
  .find(group => group.key === 'prices');
assert.deepEqual(plain(wbPriceIntent.actions.map(action => action.key)), ['wb_prices'],
  'a linked WB-only selection has one direct price destination');
assert.equal(wbPriceIntent.actions[0].shortLabel, 'Цены WB');
const refreshIntent = component.computed.bulkIntentGroups.call(wbApp)
  .find(group => group.key === 'refresh');
assert.deepEqual(plain(refreshIntent.actions.map(action => action.key)), ['refresh_supplier'],
  'supplier refresh remains a single direct preview action');
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
assert.deepEqual(JSON.parse(JSON.stringify(partialResult.choices.map(choice => ({
  account_id: choice.account_id,
  account_label: choice.account_label,
  listing_ids: choice.listing_ids,
})))), [
  {account_id: 11, account_label: 'Shop A', listing_ids: [1001]},
  {account_id: 12, account_label: 'Shop B', listing_ids: [2001, 2002]},
]);
assert.equal(partialResult.partial, true,
  'a covering group and an eligible partial group are both presented');

// When one shop covers all products and another covers a subset, both appear
// and even the full set requires an explicit choice.
assigned.length = 0;
const fullAndPartialApp = makeApp(partial, accounts);
fullAndPartialApp.startAction({key: 'ozon_prices'});
assert.equal(assigned.length, 0);
assert.equal(fullAndPartialApp.$refs.ozonPriceAccountDialog.open, true);
assert.deepEqual(plain(fullAndPartialApp.ozonPriceHandoffReview.choices.map(choice => choice.partial)),
  [true, false]);
fullAndPartialApp.ozonPriceAccountId = '12';
fullAndPartialApp.continueOzonPriceHandoff();
assertHandoffUrl(12, [2001, 2002]);
assert.equal(Object.hasOwn(storedPriceReturns.at(-1), 'actionSelectionIds'), false,
  'the full account choice keeps the legacy exact-set capsule shape');

// Inline price review and save remain bound to the exact selected subset and
// full original selection; a changed excluded mapping blocks the save receipt.
assigned.length = 0;
const inlineItems = [product(101, [listing(1001, 11)]), product(102, [listing(2002, 12)])];
const inlinePartial = makeApp(inlineItems, [accounts[0]]);
inlinePartial.priceWorkspaceDialogAvailable = true;
inlinePartial.priceWorkspaceDialogOpen = false;
inlinePartial.startAction({key: 'ozon_prices'});
inlinePartial.ozonPriceAccountId = '11';
inlinePartial.continueOzonPriceHandoff();
assert.equal(inlinePartial.priceWorkspaceDialogOpen, true);
assert.deepEqual(plain(inlinePartial.priceWorkspaceLaunch.localSelectionIds), [101]);
assert.deepEqual(plain(inlinePartial.priceWorkspaceLaunch.selectionIds), [101, 102]);
assert.deepEqual(plain(inlinePartial.priceWorkspaceLaunch.productIds), [1001]);
const inlineHref = '/marketplaces/commercial/?account_id=11';
assert.equal(inlinePartial.handlePriceWorkspaceSaved({
  marketplace: 'ozon', accountId: 11, selectedCount: 1, savedCount: 1,
  proposalIds: [901], reviewUrl: inlineHref, destination: {href: inlineHref},
}), true);
assert.deepEqual(plain(storedPriceReturns.at(-1).selectionIds), [101, 102]);
assert.deepEqual(plain(storedPriceReturns.at(-1).actionSelectionIds), [101]);
assert.deepEqual(plain(boundPriceReturns.at(-1).payload.actionSelectionIds), [101]);

const staleInline = makeApp(inlineItems.map(row => product(row.id, row.ozon.listings.slice())), [accounts[0]]);
staleInline.priceWorkspaceDialogAvailable = true;
staleInline.priceWorkspaceDialogOpen = false;
staleInline.startAction({key: 'ozon_prices'});
staleInline.ozonPriceAccountId = '11';
staleInline.continueOzonPriceHandoff();
staleInline.items[1].ozon.listings.push(listing(1002, 11));
assert.equal(staleInline.handlePriceWorkspaceSaved({
  marketplace: 'ozon', accountId: 11, selectedCount: 1, savedCount: 1,
  proposalIds: [902], reviewUrl: inlineHref, destination: {href: inlineHref},
}), false, 'the inline save receipt is rejected if the chosen group changes');
const noCommon = [
  product(101, [listing(1001, 11)]),
  product(102, [listing(2002, 12)]),
];
const onePartialApp = makeApp([
  product(91, [listing(1091, 11)]),
  product(92, []),
], [accounts[0]]);
onePartialApp.startAction({key: 'ozon_prices'});
assert.equal(onePartialApp.ozonPriceHandoffReview.choices.length, 1);
assert.equal(onePartialApp.ozonPriceHandoffReview.choices[0].partial, true);
assert.equal(onePartialApp.$refs.ozonPriceAccountDialog.open, true,
  'a single partial group still asks for an explicit Continue');
assigned.length = 0;
const noCommonApp = makeApp(noCommon, accounts);
const unavailablePrices = component.computed.bulkIntentGroups.call(noCommonApp)
  .find(group => group.key === 'prices');
assert.ok(unavailablePrices.actions.some(action => action.key === 'ozon_prices'),
  'a safe partial account group remains available');
noCommonApp.startAction({key: 'ozon_prices'});
assert.equal(assigned.length, 0);
assert.equal(noCommonApp.ozonPriceHandoffReview.partial, true);
assert.equal(noCommonApp.$refs.ozonPriceAccountDialog.open, true,
  'one or more partial groups always require an explicit account choice');
assert.deepEqual(plain(noCommonApp.ozonPriceHandoffReview.choices.map(choice => ({
  account_id: choice.account_id,
  source_ids: choice.source_ids,
  listing_ids: choice.listing_ids,
  eligible_count: choice.eligible_count,
  total_count: choice.total_count,
  excluded_rows: choice.excluded_rows.map(row => ({id: row.id, reason: row.reason})),
}))), [
  {account_id: 11, source_ids: [101], listing_ids: [1001], eligible_count: 1, total_count: 2,
    excluded_rows: [{id: 102, reason: 'Нет связанной карточки в магазине'}]},
  {account_id: 12, source_ids: [102], listing_ids: [2002], eligible_count: 1, total_count: 2,
    excluded_rows: [{id: 101, reason: 'Нет связанной карточки в магазине'}]},
]);
noCommonApp.ozonPriceAccountId = '11';
noCommonApp.continueOzonPriceHandoff();
assertHandoffUrl(11, [1001]);
assert.deepEqual(plain(storedPriceReturns.at(-1).selectionIds), [101, 102],
  'the return capsule retains the full original selection');
assert.deepEqual(plain(storedPriceReturns.at(-1).actionSelectionIds), [101],
  'the provider handoff records its exact ordered subset');

// A mixed selection can use a 100-item shop group even when the original
// global selection has 101 rows. The explicit chooser shows what was excluded.
assigned.length = 0;
const mixed101 = Array.from({length: 101}, (_, index) => {
  const id = index + 1;
  return product(id, [index < 100 ? listing(id + 1000, 11) : listing(id + 2000, 12)]);
});
const mixed101App = makeApp(mixed101, accounts);
mixed101App.startAction({key: 'ozon_prices'});
assert.equal(mixed101App.ozonPriceHandoffReview.partial, true);
assert.deepEqual(plain(mixed101App.ozonPriceHandoffReview.choices[0].source_ids),
  Array.from({length: 100}, (_, index) => index + 1));
assert.equal(mixed101App.$refs.ozonPriceAccountDialog.open, true);
mixed101App.ozonPriceAccountId = '11';
mixed101App.continueOzonPriceHandoff();
assertHandoffUrl(11, Array.from({length: 100}, (_, index) => index + 1001));
assert.equal(storedPriceReturns.at(-1).selectionIds.length, 101);
assert.equal(storedPriceReturns.at(-1).actionSelectionIds.length, 100);

// A newly eligible selected row invalidates the reviewed subset instead of
// silently widening the request after the chooser was opened.
assigned.length = 0;
const staleMembership = makeApp([
  product(501, [listing(1501, 11)]),
  product(502, [listing(2502, 12)]),
], accounts);
staleMembership.startAction({key: 'ozon_prices'});
staleMembership.ozonPriceAccountId = '11';
staleMembership.items[1].ozon.listings.push(listing(1502, 11));
staleMembership.continueOzonPriceHandoff();
assert.equal(assigned.length, 0);
assert.match(staleMembership.ozonPriceError, /изменились после проверки/);

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
const cardsChoices = component.computed.bulkIntentGroups.call(disconnectedApp)
  .find(group => group.key === 'cards');
assert.ok(cardsChoices.actions.some(action => action.key === 'common_content'));
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
const duplicateAgainstArchived = [
  product(101, [listing(1001, 11)]),
  product(102, [listing(1001, 11, {is_archived: true})]),
];
assert.equal(
  makeApp(duplicateAgainstArchived, [accounts[0]])
    .resolveOzonPriceAccountChoices(duplicateAgainstArchived, [101, 102]).choices.length,
  0,
  'an archived selected mapping still reserves its duplicated provider ID',
);
const duplicateAgainstAmbiguous = [
  product(101, [listing(1001, 11)]),
  product(102, [listing(1001, 11), listing(1002, 11)]),
];
assert.equal(
  makeApp(duplicateAgainstAmbiguous, [accounts[0]])
    .resolveOzonPriceAccountChoices(duplicateAgainstAmbiguous, [101, 102]).choices.length,
  0,
  'an ambiguous selected mapping still reserves its duplicate provider ID',
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
  'no_common_account',
  'a complete 101-item account group is over the cap and is not truncated',
);
"""


PREPARE_RETURN_NODE_CASES = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const plain = value => JSON.parse(JSON.stringify(value));
const nonce = '0123456789abcdef0123456789abcdef';
const jobUid = 'ozon-upload-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa';

function jsonResponse(data, status = 200, beforeJson = null) {
  return {
    status, ok: status >= 200 && status < 300, redirected: false, type: 'basic',
    headers: {get() { return 'application/json'; }},
    async json() { if (beforeJson) beforeJson(); return data; },
  };
}

function harness({batch = null, storeNonce = nonce, bindResult = null} = {}) {
  let component;
  const session = new Map();
  const requests = [];
  const assigned = [];
  const stored = [];
  const bound = [];
  const location = {
    origin: 'https://seller.test',
    pathname: '/my-products/beta',
    search: batch ? '?import_batch=' + encodeURIComponent(batch) : '',
    hash: '',
    assign(value) { assigned.push(value); },
  };
  const storage = {
    getItem(key) { return session.has(key) ? session.get(key) : null; },
    setItem(key, value) { session.set(key, String(value)); },
    removeItem(key) { session.delete(key); },
  };
  const helpers = {
    storeWorkspaceReturn(payload) {
      stored.push(plain(payload));
      return storeNonce;
    },
    bindOzonPreparationWorkspaceResult(hash, payload) {
      bound.push({hash, payload: plain(payload)});
      if (bindResult !== null) return bindResult;
      const capsule = stored[stored.length - 1];
      return capsule && capsule.owner === payload.owner &&
        JSON.stringify(capsule.prepareTarget) === JSON.stringify(payload.prepareTarget)
        ? hash : '';
    },
    consumeWorkspaceReturn(value) { return value === nonce; },
  };
  const context = {
    window: {
      location,
      crypto: {randomUUID() { return '11111111-2222-4333-8444-555555555555'; }},
      SellerHubCommonContent: helpers,
      shVue: {
        bootstrapData() {
          return {
            workspaceOwner: '7:9', wbConnected: true, ozonEnabled: true,
            ozonAccounts: [{id: 11, label: 'Shop A'}], suppliers: [],
            importBatch: batch, batchKind: 'import', supplierRefreshBatch: null,
            supplierRefreshBatchInvalid: false,
            urls: {
              ozonUploads: '/marketplaces/ozon/bulk-uploads/',
              ozonByRequest: '/api/ozon/bulk-uploads/by-request',
              workspace: '/my-products/beta',
            },
          };
        },
        mount(_selector, options) { component = options; },
      },
    },
    location, sessionStorage: storage,
    document: {
      activeElement: {focus() {}},
      querySelector() { return {content: 'csrf-test-token', setAttribute() {}}; },
    },
    URL, URLSearchParams, AbortController, setTimeout, clearTimeout, setInterval, clearInterval, console,
  };
  context.window.sessionStorage = storage;
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), context);
  const responses = [];
  context.fetch = async (url, options = {}) => {
    const method = options.method || 'GET';
    const call = {url, method, headers: options.headers || {}, body: null};
    if (options.body) call.body = JSON.parse(options.body);
    requests.push(call);
    if (method === 'POST') {
      call.pendingAtSend = JSON.parse(session.get('ozon-source-prepare-v1') || 'null');
    }
    const next = responses.shift();
    if (next instanceof Error) throw next;
    return next;
  };

  function makeApp(productIds, selectionIds = productIds) {
    const app = component.data();
    Object.assign(app, component.methods);
    app.items = productIds.map(id => ({id, import_status: 'imported'}));
    app.selected = selectionIds.slice();
    app.ozonIds = productIds.slice();
    app.ozonAccountId = '11';
    app.ozonAccounts = [{id: 11, label: 'Shop A'}];
    app.ozonEnabled = true;
    app.ozonBusy = '';
    app.ozonPending = app.ozonPending || null;
    app.ozonStorageProblem = false;
    app.ozonSessionEnded = false;
    app.loading = false;
    app.itemsScope = app.feedScopeKey();
    app.ozonSelectionAtOpen = selectionIds.slice();
    app.ozonSelectionScopeAtOpen = app.feedScopeKey();
    app.$refs = {
      ozonDialog: {open: true, showModal() { this.open = true; }, close() { this.open = false; }},
      ozonDialogTitle: {focus() {}},
      ozonAccount: {focus() {}},
      ozonError: {focused: false, focus() { this.focused = true; }},
    };
    app.$nextTick = callback => callback.call(app);
    let scopeBuilds = 0;
    const originalBuildScope = app.buildWorkspaceReturnScope;
    app.buildWorkspaceReturnScope = function () {
      scopeBuilds += 1;
      return originalBuildScope.call(this);
    };
    return {app, scopeBuildCount: () => scopeBuilds};
  }
  return {session, requests, assigned, stored, bound, responses, makeApp};
}

(async () => {
  // A 75-item signed import selection is stored locally with the existing
  // return scope; only the existing four request fields go over the wire.
  const ids = Array.from({length: 75}, (_, index) => 5001 + index);
  const importBatch = 'signed.import.batch.token';
  const imported = harness({batch: importBatch});
  imported.responses.push(jsonResponse({success: true, run: {
    mode: 'source_prepare', account_id: 11, job_uid: jobUid,
  }}));
  const importedSetup = imported.makeApp(ids);
  await importedSetup.app.prepareOzon();
  assert.equal(importedSetup.scopeBuildCount(), 1);
  assert.deepEqual(imported.stored[0], {
    owner: '7:9',
    prepareTarget: {accountId: 11, productIds: ids},
    selectionIds: ids,
    scope: {
      activeTab: '', search: '', supplier: '', has_photos: '',
      stock: '', sort: '', visibleCount: 75,
    },
    batch: importBatch,
  });
  assert.deepEqual(Object.keys(imported.requests[0].body).sort(),
    ['account_id', 'confirm_prepare', 'imported_product_ids', 'request_key']);
  assert.deepEqual(imported.requests[0].body, {
    account_id: 11, imported_product_ids: ids,
    request_key: '11111111222243338444555555555555', confirm_prepare: true,
  });
  assert.equal(imported.requests[0].pendingAtSend.workspace_return_nonce, nonce,
    'capsule nonce is persisted before the POST');
  assert.equal(imported.bound.length, 1);
  assert.deepEqual(imported.bound[0], {
    hash: '#workspace-return=' + nonce,
    payload: {owner: '7:9', prepareTarget: {accountId: 11, productIds: ids}, jobUid},
  });
  assert.deepEqual(imported.assigned, [
    '/marketplaces/ozon/bulk-uploads/' + jobUid,
  ]);

  // Preparation cannot be called by a stale handler after the native dialog
  // closed or after the reviewed workspace selection changed.
  const closedDialog = harness();
  const closedSetup = closedDialog.makeApp([601, 602]);
  closedSetup.app.$refs.ozonDialog.open = false;
  await closedSetup.app.prepareOzon();
  assert.deepEqual(closedDialog.requests, []);
  assert.deepEqual(closedDialog.stored, []);

  const changedSelection = harness();
  const changedSetup = changedSelection.makeApp([701, 702]);
  changedSetup.app.selected = [701];
  await changedSetup.app.prepareOzon();
  assert.deepEqual(changedSelection.requests, []);
  assert.deepEqual(changedSelection.stored, []);
  assert.deepEqual(changedSelection.assigned, []);
  assert.equal(changedSetup.app.$refs.ozonDialog.open, false);
  assert.match(changedSetup.app.ozonError, /выбор изменился/i);
  assert.equal(changedSetup.app.$refs.ozonError.focused, true);

  // A lost response leaves the same local pending key and recovers with GET;
  // no second prepare POST is sent before the capsule is bound.
  const recovered = harness();
  recovered.responses.push(new TypeError('connection dropped'), jsonResponse({success: true, run: {
    mode: 'source_prepare', account_id: 11, job_uid: jobUid,
  }}));
  const recoveredSetup = recovered.makeApp([701, 702, 703]);
  await recoveredSetup.app.prepareOzon();
  assert.equal(recoveredSetup.app.$refs.ozonDialog.open, false,
    'a lost POST response closes the dialog before recovery is shown');
  assert.equal(recoveredSetup.app.$refs.ozonError.focused, true,
    'a lost POST response focuses the safe recovery message');
  assert.ok(recoveredSetup.app.ozonPending.workspace_return_nonce);
  await recoveredSetup.app.readOzonPending();
  assert.deepEqual(recovered.requests.map(call => call.method), ['POST', 'GET']);
  assert.equal(recovered.bound.length, 1);
  assert.deepEqual(recovered.assigned, [
    '/marketplaces/ozon/bulk-uploads/' + jobUid,
  ]);

  // Old pending records remain readable; a missing capsule only removes the
  // return fragment and does not block ordinary preparation/navigation.
  const noStorage = harness({storeNonce: null});
  noStorage.responses.push(jsonResponse({success: true, run: {
    mode: 'source_prepare', account_id: 11, job_uid: jobUid,
  }}));
  const noStorageSetup = noStorage.makeApp([801, 802]);
  await noStorageSetup.app.prepareOzon();
  assert.equal(noStorage.requests.length, 1);
  assert.equal(noStorage.requests[0].pendingAtSend.workspace_return_nonce, undefined);
  assert.equal(noStorage.bound.length, 0);
  assert.deepEqual(noStorage.assigned, ['/marketplaces/ozon/bulk-uploads/' + jobUid]);

  const legacyPending = harness();
  const legacyIds = [8501, 8502];
  legacyPending.session.set('ozon-source-prepare-v1', JSON.stringify({
    account_id: 11, imported_product_ids: legacyIds, request_key: 'a'.repeat(32),
  }));
  legacyPending.responses.push(jsonResponse({success: true, run: {
    mode: 'source_prepare', account_id: 11, job_uid: jobUid,
  }}));
  const legacySetup = legacyPending.makeApp(legacyIds);
  assert.deepEqual(plain(legacySetup.app.ozonPending), {
    account_id: 11, imported_product_ids: legacyIds, request_key: 'a'.repeat(32),
  }, 'the pre-capsule pending shape remains valid');
  await legacySetup.app.readOzonPending();
  assert.deepEqual(legacyPending.requests.map(call => call.method), ['GET']);
  assert.equal(legacyPending.bound.length, 0);
  assert.deepEqual(legacyPending.assigned, ['/marketplaces/ozon/bulk-uploads/' + jobUid]);

  // A mismatched/invalid server acknowledgement is never bound.
  const invalidAck = harness();
  invalidAck.responses.push(jsonResponse({success: true, run: {
    mode: 'source_prepare', account_id: 12, job_uid: jobUid,
  }}));
  const invalidAckSetup = invalidAck.makeApp([901]);
  await invalidAckSetup.app.prepareOzon();
  assert.equal(invalidAck.bound.length, 0);
  assert.deepEqual(invalidAck.assigned, []);

  // A capsule whose stored owner no longer matches degrades to the ordinary job URL.
  const rejectedCapsule = harness();
  rejectedCapsule.responses.push(jsonResponse({success: true, run: {
    mode: 'source_prepare', account_id: 11, job_uid: jobUid,
  }}, 200, () => { rejectedCapsule.stored[0].owner = '8:9'; }));
  const rejectedSetup = rejectedCapsule.makeApp([1001, 1002]);
  await rejectedSetup.app.prepareOzon();
  assert.equal(rejectedCapsule.bound.length, 1);
  assert.equal(rejectedCapsule.bound[0].payload.owner, '7:9');
  assert.deepEqual(rejectedCapsule.assigned, ['/marketplaces/ozon/bulk-uploads/' + jobUid]);

  // Invalid local return scope is optional and does not change request shape.
  const invalidScope = harness();
  invalidScope.responses.push(jsonResponse({success: true, run: {
    mode: 'source_prepare', account_id: 11, job_uid: jobUid,
  }}));
  const invalidScopeSetup = invalidScope.makeApp([1101]);
  invalidScopeSetup.app.buildWorkspaceReturnScope = () => null;
  await invalidScopeSetup.app.prepareOzon();
  assert.equal(invalidScope.stored.length, 0);
  assert.equal(invalidScope.bound.length, 0);
  assert.deepEqual(Object.keys(invalidScope.requests[0].body).sort(),
    ['account_id', 'confirm_prepare', 'imported_product_ids', 'request_key']);
  assert.deepEqual(invalidScope.assigned, ['/marketplaces/ozon/bulk-uploads/' + jobUid]);
  console.log('Ozon preparation workspace return and recovery: passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
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

    def test_ozon_prepare_binds_exact_workspace_return_and_recovers_by_request(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('Node.js is required for the isolated UI logic check')
        result = subprocess.run(
            [node, '-e', PREPARE_RETURN_NODE_CASES, str(ROOT / 'static' / 'my-products-beta.js')],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

        template = (ROOT / 'templates' / 'my_products_beta.html').read_text()
        self.assertRegex(
            template,
            r"filename='my-products-beta\.js', v='\d{8}-[a-z0-9][a-z0-9-]*'",
        )
        self.assertIn('data-bulk-action-group', template)
        self.assertIn('data-bulk-direct-action', template)
        self.assertIn('data-bulk-action-key', template)
        self.assertNotIn('<sh-action-menu', template)
        self.assertIn("filename='common-product-content.js'", template)

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

    def test_supplier_result_selects_only_exact_updated_batch_and_keeps_return_scope(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('Node.js is required for the isolated UI logic check')
        script = r"""
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const plain=value=>JSON.parse(JSON.stringify(value));
const assigned=[],returns=[],calls=[],returnNonce='0123456789abcdef0123456789abcdef',historyState={keep:'state'};
const location={origin:'https://seller.test',href:'https://seller.test/my-products/beta',pathname:'/my-products/beta',search:'',hash:'',assign(value){assigned.push(value);}};
let historyTarget='',historyStateArgument,feedData;
let component;
const session=new Map();
const context={window:{location,history:{state:historyState,replaceState(state,_title,url){historyStateArgument=state;historyTarget=url;}},
 SellerHubCommonContent:{storeWorkspaceReturn(payload){returns.push(payload);return returnNonce;}},
 shVue:{bootstrapData(){return {workspaceOwner:'7:9',wbConnected:true,ozonEnabled:false,ozonAccounts:[],
   urls:{feed:'/api/my-products',workspace:'/my-products/beta',wbPriceChange:'/prices/change'}};},mount(_selector,options){component=options;}}},
 document:{activeElement:{focus(){}}},sessionStorage:{getItem:key=>session.get(key)||null,setItem:(key,value)=>session.set(key,value),removeItem:key=>session.delete(key)},
 URL,URLSearchParams,AbortController,setTimeout,clearTimeout,setInterval,clearInterval,console};
vm.createContext(context);vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
const ids=Array.from({length:75},(_,index)=>500+index);
const rows=ids.map(id=>({id,wb:{product_id:id+10000},import_status:'imported'}));
const result={kind:'partial',updated:ids.length,updatedBatch:'signed-updated-result-token',
 entries:ids.map(productId=>({productId,status:'updated'}))};
function appFor(feed){
 feedData=feed;
 const app=component.data();Object.assign(app,component.methods);
 for(const [key,getter]of Object.entries(component.computed))Object.defineProperty(app,key,{get(){return getter.call(app);}});
 app.$api={get(url,params){calls.push({url,params});return Promise.resolve(feedData);}};
 app.$fmt={plural(){return 'товаров';},int(value){return String(value);},money(){return '';}};
     app.$nextTick=function(fn){fn.call(app);};app.$refs={};app.loading=false;
 app.supplierRefreshResult=result;
 return app;
}
const exactFeed={success:true,batch_kind:'supplier_refresh_result',import_batch_count:ids.length,items:rows,
 pagination:{page:1,per_page:200,total:ids.length,pages:1,has_next:false}};
async function flush(){await new Promise(resolve=>setImmediate(resolve));}
(async()=>{
 const unavailable=appFor(exactFeed);unavailable.supplierRefreshResult={...result,updatedBatch:null};
 assert.equal(unavailable.supplierRefreshCanSelectUpdated,false,'a missing signed token never opens a broad list');
 assert.equal(unavailable.selectUpdatedSupplierBatch(),false);assert.equal(calls.length,0);
 const failedTransition=appFor(exactFeed);
 failedTransition.supplierRefreshBatch='signed-pre-refresh-scope';
 failedTransition.supplierRefreshBatchMode=true;
 failedTransition.supplierRefreshBatchInvalid=false;
 failedTransition.urls={...failedTransition.urls,workspace:'https://other.test/my-products/beta'};
 assert.equal(failedTransition.selectUpdatedSupplierBatch(),false);
 assert.equal(failedTransition.supplierRefreshBatch,'signed-pre-refresh-scope');
 assert.equal(failedTransition.supplierRefreshBatchMode,true);
 const app=appFor(exactFeed);
 app.supplierRefreshBatch='signed-pre-refresh-scope';
 app.supplierRefreshBatchMode=true;
 app.supplierRefreshBatchInvalid=false;
 assert.equal(app.selectUpdatedSupplierBatch(),true);
 assert.equal(historyTarget,'/my-products/beta?import_batch=signed-updated-result-token#supplier-refresh-result');
 assert.equal(app.supplierRefreshBatch,'');
 assert.equal(app.supplierRefreshBatchMode,false);
 assert.equal(app.supplierRefreshBatchInvalid,false);
 await flush();
 assert.deepEqual(plain(app.selected),ids,'one explicit action selects every verified row over the first 50');
 assert.deepEqual(plain(app.items.map(row=>row.id)),ids);
 assert.equal(app.itemsScope,JSON.stringify({import_batch:'signed-updated-result-token'}));
 assert.equal(app.supplierRefreshResult,result,'the mixed result panel remains visible on batch entry');
 assert.equal(app.loading,false);assert.equal(app.allSelected,true);
 assert.deepEqual(plain(calls),[{url:'/api/my-products',params:{import_batch:'signed-updated-result-token'}}]);
 app.selected=[ids[0],ids[2]];
 app.startAction({key:'wb_prices'});
 assert.equal(returns.at(-1).batch,'signed-updated-result-token');
 assert.deepEqual(JSON.parse(JSON.stringify(returns.at(-1).selectionIds)),[ids[0],ids[2]],'return capsule keeps later user deselections');
 assert.deepEqual(assigned,['/prices/change?product_ids=10500%2C10502#workspace-return='+returnNonce]);

 session.set('supplier-refresh-resume-v1','resume-token');
 app.urls.supplierRefreshResult='/api/supplier-refresh-result';
 app.importBatchMode=true;app.supplierRefreshResultBatchMode=true;
 context.fetch=async()=>({status:200,ok:true,redirected:false,
   headers:{get(){return 'application/json';}},
   json:async()=>({success:true,status:'completed',requested:1,updated:1,skipped:0,errors:0,
     items:[{product_id:ids[0],status:'updated',reason:null}],
     labels:[{productId:ids[0],label:'Товар обновлённый'}],
     updated_batch:'signed-resumed-result-token'})});
 await app.restoreSupplierRefreshResult();
 assert.equal(app.supplierRefreshResult.updatedBatch,'signed-resumed-result-token',
   'receipt return restores the updated-only batch token with the mixed result');
 assert.equal(app.supplierRefreshCanSelectUpdated,true);

 location.search='?import_batch=signed-updated-result-token';
 location.hash='#supplier-refresh-result';
 app._supplierRefreshReturnAnchor=true;
 historyTarget='';historyStateArgument=null;
 app.hideSupplierRefreshResult();
 assert.equal(app.supplierRefreshResult,null,'hiding clears the saved mixed result');
 assert.equal(app._supplierRefreshReturnAnchor,false);
 assert.equal(historyTarget,'/my-products/beta?import_batch=signed-updated-result-token',
   'hide removes only the supplier result anchor and preserves path/query');
 assert.equal(historyStateArgument,historyState,'hide preserves the current history state object');

 const directReturn=appFor(exactFeed);
 directReturn._supplierRefreshReturnAnchor=true;
 await directReturn.restoreSupplierRefreshResult();
 assert.match(directReturn.supplierRefreshResumeNote,/истёк или недоступен/,
   'direct return without a stored receipt still explains that the saved result is unavailable');

 location.hash='#unrelated-section';historyTarget='';
 app.supplierRefreshResult=result;
 app.hideSupplierRefreshResult();
 assert.equal(historyTarget,'','hiding leaves unrelated anchors untouched');

 calls.length=0;historyTarget='';
 const bad=appFor({...exactFeed,batch_kind:'import',items:rows.slice().reverse()});
 assert.equal(bad.selectUpdatedSupplierBatch(),true);
 await flush();
 assert.deepEqual(plain(bad.items),[]);assert.deepEqual(plain(bad.selected),[]);
 assert.match(bad.error,/полный обновлённый список/);
 assert.equal(calls.length,1,'invalid result data never falls back to an unscoped feed');
 calls.length=0;
 const reordered=appFor({...exactFeed,items:rows.slice().reverse()});
 assert.equal(reordered.selectUpdatedSupplierBatch(),true);
 await flush();
 assert.deepEqual(plain(reordered.items),[]);assert.deepEqual(plain(reordered.selected),[]);
 assert.match(reordered.error,/не совпал с подтверждёнными обновлёнными карточками/);
 console.log('supplier result exact batch selection and workspace return: passed');
})().catch(error=>{console.error(error);process.exitCode=1});
"""
        result = subprocess.run(
            [node, '-e', script, str(ROOT / 'static' / 'my-products-beta.js')],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        template = (ROOT / 'templates' / 'my_products_beta.html').read_text()
        self.assertIn("'batchKind': import_batch_kind_context", template)
        self.assertIn('data-supplier-refresh-updated-select', template)
        self.assertIn("'supplier_refresh_result'", template)


if __name__ == '__main__':
    unittest.main()
