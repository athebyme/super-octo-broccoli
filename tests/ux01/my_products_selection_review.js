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
    catalogCategories: '/api/my-products/categories',
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

function makeApp(preserveWorkspaceReturn = false) {
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
  if (!preserveWorkspaceReturn) {
    app.searchInput = '';
    app.filters = {
      search: '', supplier: '', has_photos: '', stock: '', sort: '',
      marketplace: '', marketplace_state: '', catalog_category: '',
    };
    app.activeTab = '';
  }
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

function makeGroupIntentApp(selectionIds, responseItems, postImpl) {
  const app = makeApp();
  const feedRows = [row(90)];
  const feedPagination = {page: 1, per_page: 50, total: 1, pages: 1, has_next: false};
  app.items = feedRows;
  app.pagination = feedPagination;
  app.itemsScope = app.feedScopeKey();
  app.selected = selectionIds.slice();
  app.selectionReviewActive = false;
  app.selectionReviewRows = [];
  app.selectionReviewIds = [];
  app.activeBulkActionGroup = '';

  const posts = [];
  const started = [];
  app.$api.post = function (url, body, options) {
    const call = {
      url,
      body: JSON.parse(JSON.stringify(body)),
      signal: options && options.signal,
    };
    posts.push(call);
    return postImpl
      ? postImpl(call)
      : Promise.resolve({success: true, items: responseItems});
  };
  app.startAction = function (action, returnFocus) {
    started.push({action, returnFocus});
    return true;
  };
  return {app, feedRows, feedPagination, posts, started};
}

function rowsForGroup(groupKey, ids) {
  return ids.map(id => {
    const item = row(id);
    if (groupKey === 'cards' || groupKey === 'prices') {
      item.wb = {product_id: 5000 + id};
    }
    if (groupKey === 'refresh') item.has_supplier_update = true;
    return item;
  });
}

async function settle() {
  await new Promise(resolve => setImmediate(resolve));
  await new Promise(resolve => setImmediate(resolve));
}

async function main() {
  const defaultScopeApp = makeApp();
  defaultScopeApp.items = [row(4)];
  defaultScopeApp.itemsScope = defaultScopeApp.feedScopeKey();
  const defaultScope = defaultScopeApp.buildWorkspaceReturnScope();
  assert.ok(defaultScope);
  assert.equal(Object.hasOwn(defaultScope, 'marketplace'), false,
    'the legacy default scope shape omits optional marketplace filters');
  assert.equal(Object.hasOwn(defaultScope, 'marketplace_state'), false,
    'the legacy default scope shape omits the default common status');

  assert.equal(Object.hasOwn(defaultScope, 'catalog_category'), false,
    'default scopes remain compatible with earlier capsule readers');

  const categoryApp = makeApp();
  const firstLabel = {key: 'label:Посуда', label: 'Посуда'};
  const secondLabel = {key: 'label:Одежда', label: 'Одежда'};
  const optionPages = [];
  categoryApp.$api.get = async (url, {page}) => {
    assert.equal(url, boot.urls.catalogCategories);
    optionPages.push(page);
    return {
      success: true, page, has_next: page === 1, has_uncategorized: true,
      options: page === 1 ? [firstLabel] : [firstLabel, secondLabel],
    };
  };
  assert.equal(await categoryApp.loadCatalogCategories(), true);
  assert.equal(categoryApp.handleCatalogCategoryChange({currentTarget: {value: firstLabel.key}}), true);
  assert.equal(categoryApp.queryParams().catalog_category, firstLabel.key);
  categoryApp.items = [row(4)];
  categoryApp.itemsScope = categoryApp.feedScopeKey();
  const categoryScope = categoryApp.buildWorkspaceReturnScope();
  assert.equal(categoryScope.catalog_category, firstLabel.key);
  assert.equal(await categoryApp.loadCatalogCategories(), true);
  sameJson(optionPages, [1, 2]);
  sameJson(categoryApp.catalogCategoryOptions, [firstLabel, secondLabel]);
  assert.equal(categoryApp.workspaceReturnScopeIsCurrent(categoryScope), true,
    'reading a further category page cannot modify the filter scope');
  assert.equal(categoryApp.handleCatalogCategoryChange({currentTarget: {value: 'missing'}}), true);
  categoryApp.itemsScope = categoryApp.feedScopeKey();
  assert.equal(categoryApp.workspaceReturnScopeIsCurrent(categoryScope), false,
    'changing the category invalidates the captured scope');

  const capturedScopeApp = makeApp();
  capturedScopeApp.activeTab = 'pending';
  capturedScopeApp.searchInput = 'R71';
  capturedScopeApp.filters.search = 'R71';
  capturedScopeApp.filters.has_photos = 'yes';
  capturedScopeApp.filters.stock = 'in_stock';
  capturedScopeApp.filters.sort = 'title';
  capturedScopeApp.filters.marketplace = 'wb';
  capturedScopeApp.filters.marketplace_state = 'active';
  capturedScopeApp.items = [row(41), row(42)];
  capturedScopeApp.itemsScope = capturedScopeApp.feedScopeKey();
  let returnScopeCaptures = 0;
  const originalBuildReturnScope = capturedScopeApp.buildWorkspaceReturnScope;
  capturedScopeApp.buildWorkspaceReturnScope = function () {
    returnScopeCaptures += 1;
    return originalBuildReturnScope.call(this);
  };
  const capturedScope = capturedScopeApp.buildWorkspaceReturnScope();
  assert.ok(capturedScope);
  assert.equal(capturedScopeApp.workspaceReturnScopeIsCurrent(capturedScope), true,
    'current-scope checks compare status, source, marketplace, state, and visible count');
  assert.equal(returnScopeCaptures, 1,
    'checking a captured scope does not capture the filters again');
  capturedScopeApp.activeTab = 'validated';
  capturedScopeApp.itemsScope = capturedScopeApp.feedScopeKey();
  assert.equal(capturedScopeApp.workspaceReturnScopeIsCurrent(capturedScope), false,
    'a changed legacy WB status invalidates the captured scope');
  capturedScopeApp.activeTab = 'pending';
  capturedScopeApp.filters.marketplace_state = 'preparation';
  capturedScopeApp.itemsScope = capturedScopeApp.feedScopeKey();
  assert.equal(capturedScopeApp.workspaceReturnScopeIsCurrent(capturedScope), false,
    'a changed optional marketplace state invalidates the captured scope');
  capturedScopeApp.filters.marketplace_state = 'active';
  capturedScopeApp.itemsScope = capturedScopeApp.feedScopeKey();
  capturedScopeApp.items.push(row(43));
  assert.equal(capturedScopeApp.workspaceReturnScopeIsCurrent(capturedScope), false,
    'a changed visible row count invalidates the captured scope');

  const signedBatchDefault = makeApp();
  signedBatchDefault.items = [row(51)];
  signedBatchDefault.itemsScope = signedBatchDefault.feedScopeKey();
  const signedBatchDefaultScope = signedBatchDefault.buildWorkspaceReturnScope();
  signedBatchDefault.importBatchMode = true;
  signedBatchDefault.importBatch = 'signed-import-batch';
  signedBatchDefault.itemsScope = signedBatchDefault.feedScopeKey();
  assert.equal(signedBatchDefault.workspaceReturnScopeIsCurrent(signedBatchDefaultScope), true,
    'signed batch handoffs still accept only their default scope');

  const signedBatchFiltered = makeApp();
  signedBatchFiltered.items = [row(52)];
  signedBatchFiltered.filters.marketplace_state = 'active';
  signedBatchFiltered.itemsScope = signedBatchFiltered.feedScopeKey();
  const signedBatchFilteredScope = signedBatchFiltered.buildWorkspaceReturnScope();
  signedBatchFiltered.importBatchMode = true;
  signedBatchFiltered.importBatch = 'signed-import-batch';
  signedBatchFiltered.itemsScope = signedBatchFiltered.feedScopeKey();
  assert.equal(signedBatchFiltered.workspaceReturnScopeIsCurrent(signedBatchFilteredScope), false,
    'signed batch handoffs reject filtered scopes');

  const stateOnly = makeApp();
  stateOnly.filters.marketplace_state = 'attention';
  const stateOnlyParams = stateOnly.queryParams(1, 50);
  assert.equal(stateOnlyParams.marketplace_state, 'attention');
  assert.equal(Object.hasOwn(stateOnlyParams, 'marketplace'), false,
    'a common state alone does not narrow the provider set');

  const disabledOzon = makeApp();
  assert.equal(disabledOzon.ozonEnabled, false);
  assert.equal(disabledOzon.setMarketplace('ozon'), false,
    'a disabled Ozon provider cannot be selected');
  assert.equal(disabledOzon.filters.marketplace, '');

  const ozonSwitch = makeApp();
  ozonSwitch.ozonEnabled = true;
  ozonSwitch.activeTab = 'imported';
  assert.equal(ozonSwitch.setMarketplace('ozon'), true);
  assert.equal(ozonSwitch.activeTab, '',
    'switching to Ozon clears an incompatible legacy WB status');
  assert.equal(ozonSwitch.queryParams().marketplace, 'ozon');
  assert.equal(Object.hasOwn(ozonSwitch.queryParams(), 'status'), false);

  const updatesStatus = makeApp();
  updatesStatus.load = function () {};
  updatesStatus.setTab('updates');
  assert.equal(updatesStatus.selectedWbStatusLabel, '',
    'the WB status control is visually empty in the supplier updates view');
  assert.equal(updatesStatus.queryParams().updates, '1');
  updatesStatus.setWbStatus('pending');
  assert.equal(updatesStatus.activeTab, 'pending',
    'choosing a WB import status leaves the supplier updates view');
  assert.equal(updatesStatus.queryParams().status, 'pending');
  assert.equal(Object.hasOwn(updatesStatus.queryParams(), 'updates'), false);

  const resetLegacyStatus = makeApp();
  resetLegacyStatus.activeTab = 'pending';
  resetLegacyStatus.items = [row(5)];
  resetLegacyStatus.itemsScope = resetLegacyStatus.feedScopeKey();
  let resetFeedReads = 0;
  resetLegacyStatus.load = function () { resetFeedReads += 1; };
  const oldFilters = resetLegacyStatus.filters;
  resetLegacyStatus.resetFilters();
  assert.equal(resetLegacyStatus.activeTab, '',
    'reset clears a legacy WB status even when no other filter is active');
  assert.notEqual(resetLegacyStatus.filters, oldFilters,
    'reset replaces the filter object so Vue observes the status-only reset');
  assert.equal(resetFeedReads, 0, 'reset relies on the filter watcher for one feed read');
  component.watch.filters.handler.call(resetLegacyStatus);
  assert.equal(resetFeedReads, 1);

  const ozonOnlyStatus = makeApp();
  ozonOnlyStatus.filters.marketplace = 'ozon';
  assert.equal(ozonOnlyStatus.setWbStatus('pending'), false,
    'the WB-only status control cannot set an Ozon-only scope');

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

  const actionIntentIds = [22, 11];
  for (const groupKey of ['cards', 'prices', 'create', 'refresh']) {
    const reviewedRows = rowsForGroup(groupKey, actionIntentIds);
    const harness = makeGroupIntentApp(actionIntentIds, reviewedRows);
    const {app: intentApp, feedRows, feedPagination, posts: intentPosts, started} = harness;
    const group = intentApp.bulkIntentGroups.find(candidate => candidate.key === groupKey);
    assert.ok(group, groupKey + ' keeps its group-level entry point while rows are unverified');
    const trigger = {isConnected: true, focusCount: 0, focus() { this.focusCount += 1; }};
    let headingFocusCount = 0;
    intentApp.$refs.selectionReviewHeading.focus = () => { headingFocusCount += 1; };
    fakeDocument.activeElement = trigger;
    const assignmentCount = assignments.length;

    await intentApp.activateBulkIntent(group, {currentTarget: trigger});

    assert.equal(intentPosts.length, 1, groupKey + ' performs one exact read before opening its action');
    assert.equal(intentPosts[0].url, '/api/my-products/selection');
    sameJson(intentPosts[0].body.ids, actionIntentIds,
      groupKey + ' reads hidden selection members in the original ordered selection');
    assert.equal(intentPosts[0].signal.aborted, false);
    assert.equal(intentApp.selectionRowsVerified, true);
    sameJson(intentApp.selectedItems.map(item => item.id), actionIntentIds,
      groupKey + ' derives eligibility from the exact returned rows');
    assert.equal(intentApp.selectionReviewActive, false,
      'action-time verification does not activate the optional manual review view');
    assert.equal(intentApp.items, feedRows, 'action-time verification leaves the filtered feed rows intact');
    assert.equal(intentApp.pagination, feedPagination, 'action-time verification leaves feed pagination intact');
    assert.equal(headingFocusCount, 0, 'action-time verification does not steal focus for manual review');
    assert.equal(fakeDocument.activeElement, trigger, 'the group trigger retains keyboard focus');
    assert.equal(trigger.focusCount, 0, 'the action-time read does not move focus back to the trigger');
    assert.equal(assignments.length, assignmentCount, 'verification does not navigate or submit a provider action');
    assert.ok(intentPosts.every(call => call.url === '/api/my-products/selection'),
      'the group intent issues no provider write while verifying');

    const freshGroup = intentApp.bulkIntentGroups.find(candidate => candidate.key === groupKey);
    assert.ok(freshGroup.actions.length > 0, groupKey + ' uses eligibility from the newly read rows');
    assert.ok(started.every(entry => entry.action && entry.action.key !== groupKey),
      'a group key is never passed to the single-action dispatcher');
    if (freshGroup.actions.length === 1) {
      sameJson(started.map(entry => entry.action.key), [freshGroup.actions[0].key],
        groupKey + ' dispatches only its one currently eligible action');
    } else {
      assert.deepEqual(started, [], groupKey + ' opens a choice panel when multiple current actions are eligible');
      assert.equal(intentApp.activeBulkActionGroup, groupKey);
    }
  }

  const headerRefresh = makeGroupIntentApp(actionIntentIds, rowsForGroup('refresh', actionIntentIds));
  headerRefresh.app.startAction = component.methods.startAction.bind(headerRefresh.app);
  let refreshPreview;
  headerRefresh.app.openSupplierRefreshReview = (targetIds, selectedIds) => {
    refreshPreview = {targetIds, selectedIds};
  };
  assert.equal(headerRefresh.app.supplierRefreshSelectionAction, null);
  assert.equal(headerRefresh.app.supplierUpdatesEntryDisabled, false);
  await headerRefresh.app.activateSupplierUpdatesEntry({currentTarget: {}});
  assert.equal(headerRefresh.posts.length, 1);
  assert.equal(headerRefresh.posts[0].url, '/api/my-products/selection');
  sameJson(headerRefresh.posts[0].body.ids, actionIntentIds);
  sameJson(refreshPreview, {targetIds: actionIntentIds, selectedIds: actionIntentIds});
  assert.equal(headerRefresh.app.selectionReviewActive, false);

  const mismatchedIntentRows = rowsForGroup('cards', actionIntentIds).reverse();
  const mismatchedIntent = makeGroupIntentApp(actionIntentIds, mismatchedIntentRows);
  const mismatchedIntentGroup = mismatchedIntent.app.bulkIntentGroups.find(group => group.key === 'cards');
  let mismatchHeadingFocus = 0;
  mismatchedIntent.app.$refs.selectionReviewHeading.focus = () => { mismatchHeadingFocus += 1; };
  const mismatchTrigger = {isConnected: true, focus() { throw new Error('Unexpected trigger focus'); }};
  fakeDocument.activeElement = mismatchTrigger;
  const mismatchIntentResult = await mismatchedIntent.app.activateBulkIntent(
    mismatchedIntentGroup, {currentTarget: mismatchTrigger},
  );
  assert.equal(mismatchIntentResult, false, 'a reordered exact response cannot continue an action group');
  assert.equal(mismatchedIntent.posts.length, 1);
  sameJson(mismatchedIntent.posts[0].body.ids, actionIntentIds);
  sameJson(mismatchedIntent.app.selected, actionIntentIds,
    'a mismatched response preserves the visible ordered marks');
  assert.equal(mismatchedIntent.app.selectionReviewActive, false);
  assert.equal(mismatchedIntent.app.selectionRowsVerified, false);
  assert.match(mismatchedIntent.app.selectionReviewError, /точный список/);
  assert.deepEqual(mismatchedIntent.started, [], 'a mismatched read cannot dispatch an action');
  assert.equal(mismatchedIntent.app.activeBulkActionGroup, '', 'a mismatched read cannot open the action panel');
  assert.equal(mismatchHeadingFocus, 0);
  assert.equal(fakeDocument.activeElement, mismatchTrigger);

  let resolveRapidIntentRead;
  let rapidIntentSignal;
  const rapidIntent = makeGroupIntentApp(actionIntentIds, null, call => {
    rapidIntentSignal = call.signal;
    return new Promise(resolve => { resolveRapidIntentRead = resolve; });
  });
  const rapidIntentGroup = rapidIntent.app.bulkIntentGroups.find(group => group.key === 'refresh');
  const rapidIntentTrigger = {isConnected: true, focus() { throw new Error('Unexpected trigger focus'); }};
  fakeDocument.activeElement = rapidIntentTrigger;
  const firstIntentClick = rapidIntent.app.activateBulkIntent(
    rapidIntentGroup, {currentTarget: rapidIntentTrigger},
  );
  const secondIntentClick = rapidIntent.app.activateBulkIntent(
    rapidIntentGroup, {currentTarget: rapidIntentTrigger},
  );
  await settle();
  assert.equal(rapidIntent.posts.length, 1, 'rapid activation never duplicates the exact selection read');
  sameJson(rapidIntent.posts[0].body.ids, actionIntentIds);
  assert.equal(rapidIntent.app.selectionReviewLoading, true);
  assert.deepEqual(rapidIntent.started, [], 'no action is dispatched while the exact read is pending');

  rapidIntent.app.selected = [22];
  component.watch.selected.handler.call(rapidIntent.app, rapidIntent.app.selected);
  resolveRapidIntentRead({success: true, items: rowsForGroup('refresh', actionIntentIds)});
  const [firstIntentResult, secondIntentResult] = await Promise.all([
    Promise.resolve(firstIntentClick), Promise.resolve(secondIntentClick), settle(),
  ]);
  assert.equal(rapidIntentSignal.aborted, true, 'a changed selection aborts the pending exact read');
  assert.equal(firstIntentResult, false, 'a late response for a changed selection cannot continue');
  assert.equal(secondIntentResult, false, 'the repeated click cannot continue after selection drift');
  assert.equal(rapidIntent.posts.length, 1);
  sameJson(rapidIntent.app.selected, [22]);
  assert.equal(rapidIntent.app.selectionReviewActive, false);
  assert.deepEqual(rapidIntent.started, [], 'late rows cannot dispatch the previously selected action');
  assert.equal(rapidIntent.app.activeBulkActionGroup, '');
  assert.equal(fakeDocument.activeElement, rapidIntentTrigger);

  let resolveFilterDriftRead;
  let filterDriftSignal;
  const filterDrift = makeGroupIntentApp(actionIntentIds, null, call => {
    filterDriftSignal = call.signal;
    return new Promise(resolve => { resolveFilterDriftRead = resolve; });
  });
  const filterDriftGroup = filterDrift.app.bulkIntentGroups.find(group => group.key === 'create');
  const filterDriftTrigger = {isConnected: true, focus() { throw new Error('Unexpected trigger focus'); }};
  let filterFeedReads = 0;
  filterDrift.app.$api.get = function (url) {
    filterFeedReads += 1;
    assert.equal(url, '/api/my-products');
    return Promise.resolve(page([row(90)], 1));
  };
  fakeDocument.activeElement = filterDriftTrigger;
  const filterDriftClick = filterDrift.app.activateBulkIntent(
    filterDriftGroup, {currentTarget: filterDriftTrigger},
  );
  await settle();
  assert.equal(filterDrift.posts.length, 1);
  assert.equal(filterDrift.app.selectionReviewLoading, true);
  filterDrift.app.filters.sort = 'title';
  component.watch.filters.handler.call(filterDrift.app);
  resolveFilterDriftRead({success: true, items: rowsForGroup('create', actionIntentIds)});
  const filterDriftResult = await Promise.all([Promise.resolve(filterDriftClick), settle()]);
  assert.equal(filterFeedReads, 1, 'filter drift follows the ordinary single feed refresh');
  assert.equal(filterDriftSignal.aborted, true, 'a changed scope aborts the pending exact read');
  assert.equal(filterDriftResult[0], false, 'late rows for a changed filter scope cannot continue');
  assert.equal(filterDrift.posts.length, 1);
  sameJson(filterDrift.app.selected, actionIntentIds, 'filter changes preserve the selected IDs');
  assert.equal(filterDrift.app.selectionReviewActive, false);
  assert.deepEqual(filterDrift.started, [], 'late rows for an old filter cannot dispatch an action');
  assert.equal(filterDrift.app.activeBulkActionGroup, '');
  assert.equal(fakeDocument.activeElement, filterDriftTrigger);

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
  boot.ozonEnabled = true;
  const navigating = makeApp();
  navigating.urls.commonContent = '/my-products/common-content';
  navigating.items = [row(30)];
  navigating.filters.marketplace = 'ozon';
  navigating.filters.marketplace_state = 'active';
  navigating.itemsScope = navigating.feedScopeKey();
  navigating.selected = [11, 22];
  navigating.selectionReviewRows = [row(11), row(22)];
  navigating.selectionReviewIds = [11, 22];
  navigating.selectionReviewActive = true;
  assert.equal(navigating.navigateCommonContent([11], [11, 22]), true);
  assert.equal(returnPayload.owner, boot.workspaceOwner);
  assert.equal(returnPayload.scope.visibleCount, 1,
    'the capsule retains the filtered feed count rather than selected-only row count');
  assert.equal(returnPayload.scope.marketplace, 'ozon');
  assert.equal(returnPayload.scope.marketplace_state, 'active');
  sameJson(Object.keys(returnPayload.scope), [
    'activeTab', 'search', 'supplier', 'has_photos', 'stock', 'sort',
    'visibleCount', 'marketplace', 'marketplace_state',
  ], 'non-default provider scope keys serialize in a stable order');
  const marker = [...storage.values()].map(value => {
    try { return JSON.parse(value); } catch (_) { return null; }
  }).find(value => value && value.nonce === returnNonce && Array.isArray(value.ids));
  assert.ok(marker, 'review return metadata is stored with the existing capsule nonce');
  sameJson(marker.ids, [11, 22]);

  fakeWindow.location.hash = '#workspace-return=' + returnNonce;
  vm.runInContext(sourceText, context, {filename: sourcePath + ' return'});
  const returned = makeApp(true);
  assert.equal(returned.workspaceReturnPending, true);
  assert.equal(returned.filters.marketplace, 'ozon');
  assert.equal(returned.filters.marketplace_state, 'active');
  let returnedFeedReads = 0;
  let returnedSelectionReads = 0;
  returned.$api.get = function (url, params) {
    returnedFeedReads += 1;
    assert.equal(url, '/api/my-products');
    assert.equal(params.per_page, 50);
    assert.equal(params.marketplace, 'ozon');
    assert.equal(params.marketplace_state, 'active');
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
  assert.match(template, /data-selection-intent-verify/,
    'group launches advertise the exact-selection check they perform');
  assert.match(template, /:aria-busy="selectionReviewLoading \? 'true' : 'false'/,
    'the group launch exposes the in-flight exact read to assistive technology');
  assert.match(template, /data-selection-review-return/);
  assert.match(template, /data-selection-outside-count/);
  assert.match(template, /data-selection-review-row/);
  assert.match(template, /<sh-filter-bar ref="catalogSearch"/);
  assert.match(template, /data-catalog-marketplace="all"[\s\S]*data-catalog-marketplace="wb"[\s\S]*data-catalog-marketplace="ozon"/);
  assert.match(template, /data-catalog-marketplace-status/);
  assert.match(template, /data-catalog-category aria-label="Категория каталога"/);
  assert.match(template, /data-catalog-extra-filters/);
  assert.match(template, /data-catalog-wb-status/);
  assert.match(template, /:value="activeTab === 'updates' \? '' : activeTab"/);
  const sharedExtraFilters = template.match(/<details class="mp-extra-filters" data-catalog-extra-filters>([\s\S]*?)<\/details>/);
  assert.ok(sharedExtraFilters, 'advanced controls stay in one shared native disclosure');
  assert.match(sharedExtraFilters[1], /data-catalog-supplier aria-label="Поставщик"/);
  assert.match(sharedExtraFilters[1], /<select v-if="filters\.marketplace !== 'ozon'" data-catalog-wb-status/,
    'the WB-only status control is absent from Ozon-only scope');
  assert.match(sharedExtraFilters[1], /<select v-model="filters\.sort" class="mp-select" data-catalog-sort aria-label="Сортировка">/,
    'one actual sort select remains directly available inside the disclosure');
  assert.equal((sharedExtraFilters[1].match(/data-catalog-sort/g) || []).length, 1,
    'the sort control is not duplicated');
  assert.match(template, /data-catalog-marketplace-status aria-label="Статус карточек"/);
  assert.doesNotMatch(template, /class="mp-tabs"/,
    'the old competing status tabs are no longer rendered');
  assert.match(css, /\.mp-marketplace-choice\s*\{[^}]*min-height:\s*44px/);
  assert.match(css, /\.mp-extra-filters summary\s*\{[^}]*min-height:\s*44px/);
  assert.match(css, /\.mp-marketplace-choice:focus-visible/);
  assert.match(css, /\.mp-extra-filters summary:focus-visible/);
  assert.match(css, /\.mp-selection-review[\s\S]*--focus-outline/);
  assert.match(template, /filename='my-products-beta\.css', v='20261123-supplier-refresh-prepare-r1'/);
  assert.match(template, /filename='my-products-beta\.js', v='20261123-supplier-refresh-prepare-r1'/);

  const legacyScope = {
    activeTab: 'pending', search: '', supplier: '', has_photos: '', stock: '',
    sort: '', visibleCount: 1,
  };
  returnPayload = {
    owner: boot.workspaceOwner,
    batch: null,
    selectionIds: [],
    scope: legacyScope,
  };
  fakeWindow.location.hash = '#workspace-return=' + returnNonce;
  vm.runInContext(sourceText, context, {filename: sourcePath + ' legacy capsule'});
  const legacyReturn = makeApp(true);
  assert.equal(legacyReturn.workspaceReturnPending, true,
    'R70 capsules without optional provider fields remain valid');
  assert.equal(legacyReturn.queryParams().status, 'pending',
    'legacy status capsules still restore their WB status query');

  returnPayload = {
    owner: boot.workspaceOwner,
    batch: null,
    selectionIds: [],
    scope: Object.assign({}, legacyScope, {unexpected: 'ignored'}),
  };
  vm.runInContext(sourceText, context, {filename: sourcePath + ' invalid capsule'});
  const invalidReturn = makeApp();
  assert.equal(invalidReturn.workspaceReturnPending, false,
    'the expanded return scope still rejects keys outside its strict allowlist');

  returnPayload = {
    owner: boot.workspaceOwner,
    batch: null,
    selectionIds: [],
    scope: Object.assign({}, legacyScope, {marketplace: 'ozon'}),
  };
  vm.runInContext(sourceText, context, {filename: sourcePath + ' hidden WB filter capsule'});
  const hiddenWbStatusReturn = makeApp(true);
  assert.equal(hiddenWbStatusReturn.workspaceReturnPending, false,
    'an Ozon-only capsule cannot restore a hidden legacy WB status filter');

  process.stdout.write('My Products selection review regressions passed\n');
}

main().catch(error => {
  process.stderr.write(String(error.stack || error) + '\n');
  process.exitCode = 1;
});
