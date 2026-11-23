/* Exact selected-only My Products review and return regressions. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

let component;
const boot = {
  wbConnected: true,
  wbAccountId: 'wb-synthetic',
  ozonEnabled: false,
  ozonAccounts: [],
  suppliers: [],
  importBatch: null,
  batchKind: null,
  supplierRefreshBatch: null,
  supplierRefreshBatchInvalid: false,
  workspaceOwner: '9:10',
  urls: {
    feed: '/api/my-products',
    workspace: '/my-products/beta',
    facets: '/api/my-products/facets',
    selection: '/api/my-products/selection',
    commonContent: '/my-products/common-content',
  },
};
const storage = new Map();
let returnPayload = null;
const returnNonce = 'a'.repeat(32);
const assignments = [];
const fakeWindow = {
  shVue: {
    components: {},
    bootstrapData() { return boot; },
    mount(_selector, value) { component = value; return value; },
  },
  SellerHubCommonContent: {
    storeWorkspaceReturn(value) { returnPayload = value; return returnNonce; },
    workspaceReturnFromHash(hash, options) {
      if (hash !== '#workspace-return=' + returnNonce || !returnPayload ||
          returnPayload.owner !== options.owner) return null;
      return Object.assign({nonce: returnNonce}, returnPayload);
    },
    consumeWorkspaceReturn() {},
  },
  crypto: {randomUUID() { return 'b'.repeat(32); }},
  location: {
    search: '', hash: '', origin: 'https://seller.example',
    assign(value) { assignments.push(value); },
  },
};
const fakeDocument = {
  body: {},
  activeElement: null,
  querySelector() { return {content: 'csrf-test-token'}; },
};
const context = {
  window: fakeWindow,
  document: fakeDocument,
  sessionStorage: {
    getItem(key) { return storage.has(key) ? storage.get(key) : null; },
    setItem(key, value) { storage.set(key, String(value)); },
    removeItem(key) { storage.delete(key); },
  },
  localStorage: {
    getItem(key) { return storage.has(key) ? storage.get(key) : null; },
    setItem(key, value) { storage.set(key, String(value)); },
    removeItem(key) { storage.delete(key); },
  },
  AbortController,
  URL,
  URLSearchParams,
  setTimeout,
  clearTimeout,
  console,
  fetch() { throw new Error('Unexpected fetch'); },
};
vm.createContext(context);
const sourcePath = require('node:path').join(__dirname, '..', '..', 'static', 'my-products-beta.js');
const sourceText = fs.readFileSync(sourcePath, 'utf8');
vm.runInContext(sourceText, context, {filename: sourcePath});

function sameJson(left, right) {
  assert.deepEqual(JSON.parse(JSON.stringify(left)), JSON.parse(JSON.stringify(right)));
}

function row(id) {
  return {
    id,
    title: 'Product ' + id,
    vendor_code: 'SKU-' + id,
    brand: 'Test brand',
    photo: null,
    photo_count: 1,
    category: 'Category',
    supplier_price: 10,
    supplier_quantity: 4,
    import_status: 'validated',
    has_supplier_update: false,
    wb: null,
    ozon: {listings: [], drafts: []},
  };
}

function makeApp() {
  const app = Object.assign(component.data(), component.methods);
  app.$refs = {
    moreActions: {open: false},
    catalogListFocus: {focus() {}},
    catalogSearch: {$refs: {search: {focus() {}}}},
    selectionReviewHeading: {focus() {}},
  };
  app.$nextTick = callback => { if (callback) callback.call(app); };
  app.$fmt = {
    int: value => String(value),
    money: value => String(value),
    plural: (_value, one, few, many) => many || few || one,
  };
  app.$api = {get() { throw new Error('Unexpected catalog read'); }, post() { throw new Error('Unexpected post'); }};
  app.loading = false;
  app.loadingMore = false;
  app.selectAllLoading = false;
  app.searchInput = '';
  app.filters = {search: '', supplier: '', has_photos: '', stock: '', sort: ''};
  app.activeTab = '';
  app.items = [];
  app.itemsScope = app.feedScopeKey();
  app.pagination = {page: 1, per_page: 50, total: 0, pages: 1, has_next: false};
  app._feedRevision = 1;
  Object.entries(component.computed).forEach(([name, getter]) => {
    Object.defineProperty(app, name, {get() { return getter.call(app); }});
  });
  return app;
}

function page(items, total = items.length) {
  return {
    success: true,
    items,
    pagination: {
      page: 1, per_page: 50, total, pages: Math.ceil(total / 50), has_next: total > 50,
    },
  };
}

async function settle() {
  await new Promise(resolve => setImmediate(resolve));
  await new Promise(resolve => setImmediate(resolve));
}

async function main() {
  const app = makeApp();
  app.items = [row(11), row(12)];
  app.itemsScope = app.feedScopeKey();
  app.pagination = {page: 1, per_page: 50, total: 2, pages: 1, has_next: false};
  app.selected = [12, 11];
  app.filters.sort = 'title';
  app.$api.get = function (url, params) {
    assert.equal(url, '/api/my-products');
    assert.equal(params.sort, 'title');
    return Promise.resolve(page([row(30), row(31)], 2));
  };
  app.load();
  await settle();
  sameJson(app.selected, [12, 11]);
  sameJson(app.items.map(item => item.id), [30, 31]);
  assert.equal(app.selectedOutsideCurrentListCount, 2,
    'filter changes retain selected IDs and report rows outside the displayed list');
  assert.equal(app.selectionRowsVerified, false,
    'rows from another filter are unusable until the exact selected set is read');
  assert.ok(app.actionGroups.flatMap(group => group.items).every(action => action.disabled),
    'bulk actions stay disabled while selected rows are not exact');

  const sourceRows = app.items;
  const sourcePagination = app.pagination;
  const posts = [];
  app.$api.post = function (url, body, options) {
    posts.push({url, body: JSON.parse(JSON.stringify(body)), signal: options.signal});
    return Promise.resolve({success: true, items: [row(12), row(11)]});
  };
  assert.equal(await app.showSelected(), true);
  assert.equal(posts.length, 1);
  assert.equal(posts[0].url, '/api/my-products/selection');
  sameJson(posts[0].body.ids, [12, 11], 'the exact ordered selection is sent');
  assert.equal(posts[0].signal.aborted, false);
  assert.equal(app.selectionReviewActive, true);
  sameJson(app.visibleItems.map(item => item.id), [12, 11]);
  sameJson(app.selectedItems.map(item => item.id), [12, 11]);
  assert.equal(app.items, sourceRows, 'review rows do not replace the filtered feed rows');
  assert.equal(app.pagination, sourcePagination, 'review rows do not replace feed pagination');
  assert.equal(app.selectionRowsVerified, true);
  assert.equal(app.selectedOutsideCurrentListCount, 2,
    'the review heading can retain the source-list count while showing exact selected rows');

  const checkbox = {checked: false};
  assert.equal(app.onProductSelectionChange(12, {currentTarget: checkbox}), true,
    'unchecking a reviewed row removes that ID from the action set');
  sameJson(app.selected, [11]);
  sameJson(app.visibleItems.map(item => item.id), [11]);
  assert.equal(app.selectionRowsVerified, true, 'the remaining subset is still covered by the exact response');
  let listFocusCount = 0;
  app.$refs.catalogListFocus.focus = () => { listFocusCount += 1; };
  app.returnToCatalog();
  assert.equal(app.selectionReviewActive, false);
  assert.equal(app.items, sourceRows);
  assert.equal(app.pagination, sourcePagination);
  assert.equal(listFocusCount, 1, 'a nonempty catalog keeps its existing return focus');
  sameJson(app.selectedItems.map(item => item.id), [11]);
  app.clearSelection();
  sameJson(app.selected, []);
  assert.equal(app.items, sourceRows);
  assert.equal(app.pagination, sourcePagination);

  const emptyFilteredReturn = makeApp();
  emptyFilteredReturn.items = [];
  emptyFilteredReturn.selectionReviewActive = true;
  emptyFilteredReturn.$refs.catalogListFocus = null;
  let searchFocusCount = 0;
  emptyFilteredReturn.$refs.catalogSearch.$refs.search.focus = options => {
    assert.equal(options.preventScroll, true);
    searchFocusCount += 1;
  };
  emptyFilteredReturn.returnToCatalog();
  assert.equal(searchFocusCount, 1,
    'zero-result filters restore focus to the rendered search input');

  const emptyClear = makeApp();
  emptyClear.items = [];
  emptyClear.selected = [11];
  emptyClear.selectionReviewActive = true;
  emptyClear.$refs.catalogListFocus = null;
  let clearSearchFocusCount = 0;
  emptyClear.$refs.catalogSearch.$refs.search.focus = () => { clearSearchFocusCount += 1; };
  emptyClear.clearSelection();
  assert.equal(emptyClear.selectionReviewActive, false);
  assert.equal(clearSearchFocusCount, 1,
    'clearing the reviewed selection also falls back to search when the list is empty');

  const pageSelection = makeApp();
  pageSelection.items = [row(30), row(31)];
  pageSelection.itemsScope = pageSelection.feedScopeKey();
  pageSelection.selected = [11, 30];
  pageSelection.toggleAll();
  sameJson(pageSelection.selected, [11, 30, 31],
    'page select-all adds displayed IDs without broadening or replacing hidden IDs');
  pageSelection.toggleAll();
  sameJson(pageSelection.selected, [11],
    'page deselect removes only the displayed IDs');

  const capped = makeApp();
  capped.items = [row(201)];
  capped.itemsScope = capped.feedScopeKey();
  capped.selected = Array.from({length: 200}, (_, index) => index + 1);
  capped.toggleAll();
  assert.equal(capped.selected.length, 200, 'the union cap prevents adding a 201st ID');
  assert.match(capped.error, /не больше 200/);
  const checkboxCap = {checked: true};
  capped.onProductSelectionChange(201, {currentTarget: checkboxCap});
  assert.equal(checkboxCap.checked, false, 'individual checkboxes enforce the same cap');
  assert.equal(capped.selected.length, 200);

  const mismatched = makeApp();
  mismatched.items = [row(11), row(12)];
  mismatched.itemsScope = mismatched.feedScopeKey();
  mismatched.selected = [11, 12];
  mismatched.$api.post = () => Promise.resolve({success: true, items: [row(12), row(11)]});
  assert.equal(await mismatched.showSelected(), false, 'a reordered server response is rejected');
  assert.equal(mismatched.selectionReviewActive, false);
  assert.equal(mismatched.selectionRowsVerified, false,
    'a mismatched exact read blocks actions even if those IDs were visible before');
  assert.match(mismatched.selectionReviewError, /точный список/);

  const unavailable = makeApp();
  unavailable.items = [row(11), row(12)];
  unavailable.itemsScope = unavailable.feedScopeKey();
  unavailable.selected = [11, 12];
  unavailable.$api.post = () => Promise.reject({status: 404});
  assert.equal(await unavailable.showSelected(), false);
  sameJson(unavailable.selected, [11, 12], 'a failed read preserves the visible choice');
  assert.equal(unavailable.selectionRowsVerified, false);
  assert.match(unavailable.selectionReviewError, /недоступна/);

  const stale = makeApp();
  stale.items = [row(11), row(12)];
  stale.itemsScope = stale.feedScopeKey();
  stale.selected = [11];
  let resolveStale;
  let staleSignal;
  stale.$api.post = (_url, _body, options) => {
    staleSignal = options.signal;
    return new Promise(resolve => { resolveStale = resolve; });
  };
  const staleRead = stale.showSelected();
  stale.selected = [12];
  component.watch.selected.handler.call(stale, stale.selected);
  resolveStale({success: true, items: [row(11)]});
  assert.equal(await staleRead, false, 'a late response for an older selection is ignored');
  assert.equal(staleSignal.aborted, true);
  assert.equal(stale.selectionReviewActive, false);
  assert.equal(stale.selectionReviewRows.length, 0);

  const timedOut = makeApp();
  timedOut.items = [row(11)];
  timedOut.itemsScope = timedOut.feedScopeKey();
  timedOut.selected = [11];
  let timeoutCallback;
  let timeoutCleared = false;
  let timeoutSignal;
  timedOut.$api.post = (_url, _body, options) => {
    timeoutSignal = options.signal;
    return new Promise(() => {});
  };
  const originalSetTimeout = context.setTimeout;
  const originalClearTimeout = context.clearTimeout;
  context.setTimeout = (callback, delay) => {
    assert.equal(delay, 12000, 'the exact read has a bounded timeout');
    timeoutCallback = callback;
    return 901;
  };
  context.clearTimeout = timerId => {
    assert.equal(timerId, 901);
    timeoutCleared = true;
  };
  const timedOutRead = timedOut.showSelected();
  await settle();
  assert.equal(typeof timeoutCallback, 'function');
  timeoutCallback();
  assert.equal(await timedOutRead, false, 'a stalled exact read ends and can be retried');
  assert.equal(timeoutSignal.aborted, true, 'timeout aborts the transport');
  assert.match(timedOut.selectionReviewError, /слишком много времени/);
  assert.equal(timedOut.selectionReviewUnavailableIds[0], 11);
  assert.equal(timeoutCleared, true, 'completed reads clear their timer');
  context.setTimeout = originalSetTimeout;
  context.clearTimeout = originalClearTimeout;

  fakeDocument.activeElement = fakeDocument.body;
  const navigating = makeApp();
  navigating.urls.commonContent = '/my-products/common-content';
  navigating.items = [row(30)];
  navigating.itemsScope = navigating.feedScopeKey();
  navigating.selected = [11, 22];
  navigating.selectionReviewRows = [row(11), row(22)];
  navigating.selectionReviewIds = [11, 22];
  navigating.selectionReviewActive = true;
  assert.equal(navigating.navigateCommonContent([11], [11, 22]), true);
  assert.equal(returnPayload.owner, boot.workspaceOwner);
  assert.equal(returnPayload.scope.visibleCount, 1,
    'the capsule retains the filtered feed count rather than selected-only row count');
  const marker = [...storage.values()].map(value => {
    try { return JSON.parse(value); } catch (_) { return null; }
  }).find(value => value && value.nonce === returnNonce && Array.isArray(value.ids));
  assert.ok(marker, 'review return metadata is stored with the existing capsule nonce');
  sameJson(marker.ids, [11, 22]);

  fakeWindow.location.hash = '#workspace-return=' + returnNonce;
  vm.runInContext(sourceText, context, {filename: sourcePath + ' return'});
  const returned = makeApp();
  let returnedFeedReads = 0;
  let returnedSelectionReads = 0;
  returned.$api.get = function (url, params) {
    returnedFeedReads += 1;
    assert.equal(url, '/api/my-products');
    assert.equal(params.per_page, 50);
    return Promise.resolve(page([row(30)], 1));
  };
  returned.$api.post = function (url, body) {
    returnedSelectionReads += 1;
    assert.equal(url, '/api/my-products/selection');
    sameJson(body.ids, [11, 22]);
    return Promise.resolve({success: true, items: [row(11), row(22)]});
  };
  returned.load();
  await settle();
  assert.equal(returnedFeedReads, 1);
  assert.equal(returnedSelectionReads, 1,
    'workspace return re-reads hidden members instead of intersecting with the filtered page');
  sameJson(returned.items.map(item => item.id), [30]);
  sameJson(returned.selected, [11, 22]);
  sameJson(returned.visibleItems.map(item => item.id), [11, 22]);
  assert.equal(returned.selectionReviewActive, true);
  assert.equal(returned.workspaceReturnPending, false);
  assert.equal(storage.has('my-products-selection-review-return-v1'), false,
    'the one-time review marker is consumed after exact restoration');

  const templatePath = require('node:path').join(__dirname, '..', '..', 'templates', 'my_products_beta.html');
  const cssPath = require('node:path').join(__dirname, '..', '..', 'static', 'my-products-beta.css');
  const template = fs.readFileSync(templatePath, 'utf8');
  const css = fs.readFileSync(cssPath, 'utf8');
  assert.match(template, /'selection': url_for\('api_my_products_selection'\)/);
  assert.match(template, /data-selection-review-show/);
  assert.match(template, /data-selection-review-return/);
  assert.match(template, /data-selection-outside-count/);
  assert.match(template, /data-selection-review-row/);
  assert.match(template, /<sh-filter-bar ref="catalogSearch"/);
  assert.match(css, /\.mp-selection-review[\s\S]*--focus-outline/);
  assert.match(template, /filename='my-products-beta\.css', v='20261123-workspace-selection-r70'/);
  assert.match(template, /filename='my-products-beta\.js', v='20261123-workspace-selection-r70'/);

  process.stdout.write('My Products selection review regressions passed\n');
}

main().catch(error => {
  process.stderr.write(String(error.stack || error) + '\n');
  process.exitCode = 1;
});
