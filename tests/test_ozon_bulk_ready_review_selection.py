"""Node-only regression for Ozon's exact-set batch-ready review handlers."""

from pathlib import Path
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[1]


NODE_CASES = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

let component;
let requestMode = 'valid';
const apiCalls = [];
const posts = [];
const navigations = [];
const stored = new Map();
const draftIds = Array.from({length: 200}, (_, index) => index + 5001);
const parentUid = 'ozon-upload-' + 'a'.repeat(32);
const account = {id: 77, label: 'Shop North'};
const trigger = {isConnected: true, disabled: false, focus() { this.focused = true; }};
const dialog = {
  open: false,
  showModal() { this.open = true; },
  close() { this.open = false; },
};
const bootstrapReview = {
  account_id: account.id,
  account_label: account.label,
  draft_ids: draftIds,
  parent_prepare_job_uid: parentUid,
  csrf_token: 'csrf-bootstrap',
  checked_at: '2026-11-21T00:00:00',
  publication_enabled: true,
  pagination: {page: 10, per_page: 20, pages: 10, total: draftIds.length},
  items: [],
};
const bootstrap = {textContent: JSON.stringify({
  review: bootstrapReview,
  urls: {
    html: '/marketplaces/ozon/uploads/review',
    api: '/marketplaces/ozon/uploads/api/review',
    publish: '/marketplaces/ozon/uploads/from-drafts',
    byRequest: '/marketplaces/ozon/uploads/api/by-request',
    runBase: '/marketplaces/ozon/uploads/',
    editorBase: '/marketplaces/drafts/',
  },
})};

function row(id, index, selectable=true) {
  return {
    draft_id: id,
    version: 100 + index,
    selectable,
    action: index % 2 ? 'update' : 'create',
    title: 'Карточка ' + id,
    offer_id: 'offer-' + id,
    errors: selectable ? [] : [{code: 'required_missing'}],
    warnings: [],
  };
}
function response(status, body) {
  return {
    status,
    type: 'basic',
    redirected: false,
    ok: status >= 200 && status < 300,
    headers: {get(name) { return name === 'content-type' ? 'application/json' : null; }},
    async json() { return body; },
  };
}
async function fakeFetch(url, options={}) {
  const parsed = new URL(url, 'https://seller.test');
  if (options.method === 'POST') {
    const body = JSON.parse(options.body);
    posts.push({url: parsed.pathname, body});
    return response(202, {success: true, run: {
      mode: 'reviewed_drafts', account_id: account.id,
      job_uid: 'ozon-upload-' + 'b'.repeat(32),
    }});
  }
  apiCalls.push(parsed);
  const requestedIds = (parsed.searchParams.get('draft_ids') || '').split(',').map(Number);
  const page = Number(parsed.searchParams.get('page'));
  const pages = Math.ceil(requestedIds.length / 20);
  const pageIds = requestedIds.slice((page-1)*20, page*20);
  let items = pageIds.map(id => row(id, id - draftIds[0], id !== draftIds[117] && id !== draftIds[199] && id !== draftIds[44]));
  let accountId = account.id;
  let parent = parentUid;
  let pagination = {page, pages, per_page: 20, total: requestedIds.length};
  if (requestMode !== 'valid' && page === 2) {
    if (requestMode === 'invalid_shape') pagination = {...pagination, per_page: 19};
    if (requestMode === 'invalid_selectable') items[0] = {...items[0], selectable: 1};
    if (requestMode === 'foreign_account') accountId = account.id + 1;
    if (requestMode === 'foreign_parent') parent = null;
    if (requestMode === 'reordered') items = items.slice().reverse();
  }
  return response(200, {success: true, review: {
    account_id: accountId,
    account_label: account.label,
    draft_ids: requestedIds,
    parent_prepare_job_uid: parent,
    checked_at: '2026-11-21T00:00:0' + page,
    publication_enabled: true,
    pagination,
    items,
    csrf_token: 'csrf-page-' + page,
  }});
}

const fakeWindow = {
  Vue: {createApp(value) { component = value; return {component() { return this; }, mount() {}}; }},
  ozonDraftEditor: {photoComponent: {}},
  crypto: {randomUUID() { return 'c'.repeat(32); }},
  location: {assign(value) { navigations.push(value); }},
};
const fakeDocument = {
  activeElement: trigger,
  getElementById(id) { return id === 'our-bootstrap' ? bootstrap : null; },
};
const context = {
  window: fakeWindow,
  document: fakeDocument,
  fetch: fakeFetch,
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

function makeApp(selected) {
  const state = Object.assign(component.data(), component.methods);
  state.selected = selected || [];
  state.$refs = {
    dialog,
    confirm: {focus() { this.focused = true; }},
    dialogTitle: {focus() { this.focused = true; }},
    reviewButton: trigger,
    reviewAllButton: trigger,
  };
  state.$nextTick = function(callback) { callback.call(this); };
  Object.entries(component.computed).forEach(([name, getter]) => {
    Object.defineProperty(state, name, {get() { return getter.call(state); }});
  });
  return state;
}
function reset() {
  requestMode = 'valid'; apiCalls.length = 0; posts.length = 0; navigations.length = 0;
  stored.clear(); dialog.open = false; trigger.focused = false;
}
function sameJson(left, right) {
  assert.deepEqual(JSON.parse(JSON.stringify(left)), JSON.parse(JSON.stringify(right)));
}

(async () => {
  const blockedIds = new Set([draftIds[44], draftIds[117], draftIds[199]]);
  const oldSelection = [{id: draftIds[0], version: 7}, {id: draftIds[199], version: 8}];
  const app = makeApp(oldSelection.map(item => ({...item})));
  await app.reviewAllReady();

  assert.equal(apiCalls.length, 10, '200 exact IDs use ten bounded review reads');
  assert.deepEqual(apiCalls.map(url => Number(url.searchParams.get('page'))), [1,2,3,4,5,6,7,8,9,10]);
  for (const url of apiCalls) {
    assert.equal(url.searchParams.get('account_id'), String(account.id));
    assert.equal(url.searchParams.get('parent_prepare_job_uid'), parentUid);
    assert.equal(url.searchParams.get('draft_ids'), draftIds.join(','));
  }
  assert.equal(dialog.open, true);
  assert.equal(app.reviewedMode, 'all_ready');
  assert.equal(app.reviewSummary.checked, 200);
  assert.equal(app.reviewSummary.ready, 197);
  assert.equal(app.reviewSummary.needsWork, 3);
  assert.equal(app.reviewed.length, 197);
  assert.ok(app.reviewed.every(item => !blockedIds.has(item.draft_id)));
  assert.equal(app.review.items.length, 20);
  assert.equal(app.review.items[0].version, 100 + (draftIds[180] - draftIds[0]));
  assert.equal(app.review.checked_at, '2026-11-21T00:00:010');
  assert.equal(app.reviewedToSend.length, 197);
  assert.ok(app.selected.every(item => app.reviewed.some(row => row.draft_id === item.id && row.version === item.version)));

  const excluded = app.reviewed.find(item => item.draft_id === draftIds[50]);
  app.toggleReviewed(excluded, false);
  assert.equal(app.reviewedToSend.length, 196);
  const afterExclude = app.selected.map(item => ({id:item.id, version:item.version}));
  app.closeDialog();
  assert.equal(dialog.open, false);
  sameJson(app.selected, afterExclude);
  assert.ok(app.selected.every(item => item.id !== excluded.draft_id));

  // A confirmation checkbox is still required before the exact subset POST.
  await app.publish();
  assert.equal(posts.length, 0);
  app.confirmed = true;
  await app.publish();
  assert.equal(posts.length, 1);
  assert.equal(posts[0].url, '/marketplaces/ozon/uploads/from-drafts');
  const posted = posts[0].body;
  assert.equal(posted.account_id, account.id);
  assert.equal(posted.parent_prepare_job_uid, parentUid);
  assert.equal(posted.confirm_write, true);
  sameJson(posted.draft_ids, app.selected.map(item => item.id));
  sameJson(posted.expected_versions, Object.fromEntries(app.selected.map(item => [String(item.id), item.version])));
  assert.ok(posted.draft_ids.every(id => !blockedIds.has(id) && id !== excluded.draft_id));
  assert.equal(navigations.length, 1);

  // A failed later page leaves the pre-existing manual selection untouched and
  // never opens a partial confirmation dialog.
  for (const failure of ['invalid_shape','invalid_selectable','foreign_account','foreign_parent','reordered']) {
    reset(); requestMode = failure;
    const ids = draftIds.slice(0, 22);
    const prior = [{id: ids[0], version: 4}, {id: ids[21], version: 5}];
    const failed = makeApp(prior.map(item => ({...item})));
    const snapshot = failed.selected.map(item => ({id:item.id, version:item.version}));
    await failed.reviewAllReady();
    sameJson(failed.selected, snapshot);
    assert.equal(failed.reviewed, null, failure);
    assert.equal(failed.reviewSummary, null, failure);
    assert.equal(dialog.open, false, failure);
    assert.equal(apiCalls.length, 2, failure);
    assert.match(failed.error, /изменил|Состав|Магазин/);
  }

  // Manual exact-version review still removes a stale page selection.
  reset();
  const singleId = draftIds[0];
  bootstrapReview.draft_ids = [singleId];
  bootstrapReview.pagination = {page: 1, per_page: 20, pages: 1, total: 1};
  bootstrapReview.items = [row(singleId, 0)];
  const manual = makeApp([{id:singleId, version:99}]);
  await manual.reviewSelected();
  sameJson(manual.selected, []);
  assert.equal(manual.reviewed, null);
  assert.equal(dialog.open, false);
  assert.match(manual.error, /изменил|исправления/);

  console.log(JSON.stringify({status:'passed', checks:[
    'all_200_exact_rows_are_rechecked_in_ten_pages_before_selection',
    'blocked_rows_are_excluded_and_reviewed_action_subset_keeps_exact_versions',
    'later_page_contract_failures_preserve_selection_and_never_open_partial_modal',
    'manual_review_keeps_existing_page_version_invalidation',
  ]}));
})().catch(error => { console.error(error.stack || String(error)); process.exitCode = 1; });
"""


def test_batch_ready_review_is_exact_bounded_and_keeps_manual_version_guard():
    node = shutil.which("node")
    if not node:
        import pytest
        pytest.skip("Node.js is required for the isolated UI logic check")
    result = subprocess.run(
        [node, "-e", NODE_CASES, str(ROOT / "static" / "ozon-upload-review.js")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    assert '"status":"passed"' in result.stdout
