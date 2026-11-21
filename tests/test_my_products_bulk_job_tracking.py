"""Deterministic Node regressions for the My Products bulk-job poll lifecycle."""

from pathlib import Path
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[1]


NODE_CASES = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

let definition;
let nextTimerId = 1;
const timers = new Map();
const fakeDocument = {hidden: false};
const boot = {
  wbConnected: true,
  ozonEnabled: false,
  ozonAccounts: [],
  importBatch: null,
  urls: {jobStatus: '/api/jobs/'},
};
const fakeWindow = {
  crypto: {randomUUID() { return 'a'.repeat(32); }},
  location: {search: '', hash: '', origin: 'https://seller.example'},
};
const context = {
  window: fakeWindow,
  document: fakeDocument,
  sessionStorage: {
    getItem() { return null; },
    setItem() {},
    removeItem() {},
  },
  URL,
  URLSearchParams,
  AbortController,
  fetch,
  setTimeout,
  clearTimeout,
  setInterval(callback, delay) {
    const id = nextTimerId++;
    timers.set(id, {callback, delay, active: true});
    return id;
  },
  clearInterval(id) {
    const timer = timers.get(id);
    if (timer) timer.active = false;
  },
  console,
};
vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), context);
fakeWindow.shVue.bootstrapData = () => boot;
fakeWindow.shVue.mount = (_selector, value) => { definition = value; return value; };
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), context);
assert.ok(definition && definition.methods && typeof definition.methods.trackJob === 'function');
assert.equal(typeof fakeWindow.shVue.isTerminalJobStatus, 'function');

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return {promise, resolve, reject};
}

function activeTimerIds() {
  return Array.from(timers.entries())
    .filter(([, timer]) => timer.active)
    .map(([id]) => id);
}

function fireTimer(id) {
  const timer = timers.get(id);
  assert.ok(timer && timer.active, `timer ${id} should still be active`);
  timer.callback();
}

function makeApp(getJob) {
  const app = Object.assign(definition.data(), definition.methods);
  const calls = {get: [], post: 0, loads: 0, facets: 0};
  app.$api = {
    get(url) {
      calls.get.push(url);
      return getJob(url);
    },
    post() {
      calls.post += 1;
      return Promise.reject(new Error('job polling must remain read-only'));
    },
  };
  app.load = function () { calls.loads += 1; };
  app.loadFacets = function () { calls.facets += 1; };
  return {app, calls};
}

async function flush() {
  await Promise.resolve();
  await Promise.resolve();
  await new Promise(resolve => setImmediate(resolve));
}

function assertTerminalRefresh(calls) {
  assert.equal(calls.loads, 1, 'a terminal poll refreshes the list once');
  assert.equal(calls.facets, 1, 'a terminal poll refreshes facets once');
}

(async () => {
  // Raw BackgroundJob DTOs use completed + failed_count. The UI adapts that
  // payload to its terminal progress contract and stops its interval.
  const mixedResponse = deferred();
  const mixed = makeApp(() => mixedResponse.promise);
  mixed.app.trackJob('bulk-mixed', 2);
  const mixedTimer = mixed.app._jobTimer;
  assert.ok(mixedTimer);
  assert.deepEqual(mixed.calls.get, ['/api/jobs/bulk-mixed']);
  mixedResponse.resolve({
    job_uid: 'bulk-mixed', job_type: 'bulk_wb_import', status: 'completed',
    total: 2, processed: 2, succeeded: 1, failed_count: 1,
  });
  await flush();
  assert.equal(mixed.app.job.status, 'done');
  assert.equal(mixed.app.job.failed, 1);
  assert.equal(fakeWindow.shVue.isTerminalJobStatus(mixed.app.job.status), true);
  assert.equal(mixed.app._jobTimer, null);
  assert.deepEqual(activeTimerIds(), []);
  assertTerminalRefresh(mixed.calls);
  assert.equal(mixed.calls.post, 0);

  // A fully failed batch is still a completed run with item failures, not a
  // worker-level failed job.
  const allFailed = makeApp(() => Promise.resolve({
    job_uid: 'bulk-all-failed', status: 'completed', total: 2,
    processed: 2, succeeded: 0, failed_count: 2,
  }));
  allFailed.app.trackJob('bulk-all-failed', 2);
  await flush();
  assert.equal(allFailed.app.job.status, 'done');
  assert.equal(allFailed.app.job.failed, 2);
  assert.equal(fakeWindow.shVue.isTerminalJobStatus(allFailed.app.job.status), true);
  assert.equal(allFailed.app._jobTimer, null);
  assertTerminalRefresh(allFailed.calls);

  // Keep the older UI status contract working alongside BackgroundJob DTOs.
  for (const status of ['done', 'failed', 'cancelled']) {
    const legacy = makeApp(() => Promise.resolve({
      job_uid: 'legacy-' + status, status, total: 1, processed: 1,
      succeeded: 0, failed_count: status === 'failed' ? 1 : 0,
    }));
    legacy.app.trackJob('legacy-' + status, 1);
    await flush();
    assert.equal(legacy.app.job.status, status);
    assert.equal(fakeWindow.shVue.isTerminalJobStatus(legacy.app.job.status), true);
    assert.equal(legacy.app._jobTimer, null);
    assert.deepEqual(activeTimerIds(), []);
    assertTerminalRefresh(legacy.calls);
  }

  // A failed status read is displayed as an unknown read-only outcome. It is
  // terminal for this poll, refreshes the visible list once, and never POSTs.
  const lostReadResponse = deferred();
  lostReadResponse.reject(new TypeError('network unavailable'));
  const confirmedAfterLoss = deferred();
  const lostReadResponses = [lostReadResponse, confirmedAfterLoss];
  const lostRead = makeApp(() => lostReadResponses.shift().promise);
  lostRead.app.trackJob('bulk-lost-read', 1);
  await flush();
  assert.equal(lostRead.calls.get.length, 1);
  assert.equal(lostRead.app.job.status, 'unknown');
  assert.ok(String(lostRead.app.job.message || '').trim().length > 0);
  assert.equal(fakeWindow.shVue.isTerminalJobStatus(lostRead.app.job.status), true);
  assert.equal(lostRead.app._jobTimer, null);
  assert.deepEqual(activeTimerIds(), []);
  assertTerminalRefresh(lostRead.calls);
  assert.equal(lostRead.calls.post, 0);

  // The explicit retry reads the same job UID, remains a GET-only action, and
  // replaces the unknown state only after the server returns a valid result.
  assert.equal(typeof lostRead.app.recheckJob, 'function');
  lostRead.app.recheckJob();
  lostRead.app.recheckJob();
  assert.deepEqual(lostRead.calls.get, [
    '/api/jobs/bulk-lost-read',
    '/api/jobs/bulk-lost-read',
  ], 'overlapping status checks coalesce to one GET for the same job UID');
  assert.equal(lostRead.app.job.status, 'unknown',
    'starting a recheck does not turn an unknown outcome into success');
  confirmedAfterLoss.resolve({
    job_uid: 'bulk-lost-read', job_type: 'bulk_wb_import', status: 'completed',
    total: 1, processed: 1, succeeded: 1, failed_count: 0,
  });
  await flush();
  assert.equal(lostRead.app.job.status, 'done');
  assert.equal(lostRead.app.job.succeeded, 1);
  assert.equal(lostRead.app._jobTimer, null);
  assert.deepEqual(activeTimerIds(), []);
  assert.equal(lostRead.calls.loads, 2,
    'the verified terminal recheck refreshes the list after the earlier unknown read');
  assert.equal(lostRead.calls.facets, 2);
  assert.equal(lostRead.calls.post, 0, 'status recheck never retries the create POST');

  // Malformed, foreign-UID, and unknown-state replies must not overwrite the
  // explicit unknown outcome with a false terminal result.
  const unsafeReplies = [
    unknownUid => ({job_uid: 'malformed-reply'}),
    unknownUid => ({
      job_uid: 'different-job', status: 'completed', total: 1,
      processed: 1, succeeded: 1, failed_count: 0,
    }),
    unknownUid => ({
      job_uid: unknownUid, status: 'awaiting-provider', total: 1,
      processed: 1, succeeded: 1, failed_count: 0,
    }),
  ];
  for (let index = 0; index < unsafeReplies.length; index += 1) {
    const unknownUid = 'unknown-state-' + index;
    const failedInitialRead = deferred();
    failedInitialRead.reject(new TypeError('network unavailable'));
    const recheckResponse = deferred();
    const replies = [failedInitialRead, recheckResponse];
    const guarded = makeApp(() => replies.shift().promise);
    guarded.app.trackJob(unknownUid, 1);
    await flush();
    assert.equal(guarded.app.job.status, 'unknown');
    guarded.app.recheckJob();
    recheckResponse.resolve(unsafeReplies[index](unknownUid));
    await flush();
    assert.equal(guarded.app.job.status, 'unknown');
    assert.ok(String(guarded.app.job.message || '').trim().length > 0);
    assert.equal(guarded.app._jobTimer, null);
    assert.deepEqual(activeTimerIds(), []);
    assertTerminalRefresh(guarded.calls);
    assert.equal(guarded.calls.get.length, 2);
    assert.equal(guarded.calls.post, 0);
  }

  // Repeated timer ticks do not start overlapping GETs. Once the first read
  // settles as pending/running, the next tick can make one new read.
  const firstPending = deferred();
  const secondPending = deferred();
  const pendingResponses = [firstPending, secondPending];
  const pending = makeApp(() => pendingResponses.shift().promise);
  pending.app.trackJob('bulk-pending', 2);
  const pendingTimer = pending.app._jobTimer;
  fireTimer(pendingTimer);
  assert.equal(pending.calls.get.length, 1, 'a second tick cannot overlap an in-flight GET');
  firstPending.resolve({
    job_uid: 'bulk-pending', status: 'running', total: 2,
    processed: 1, succeeded: 1, failed_count: 0,
  });
  await flush();
  assert.equal(pending.app.job.status, 'running');
  assert.equal(pending.calls.loads, 0);
  assert.equal(pending.calls.facets, 0);
  fireTimer(pendingTimer);
  assert.equal(pending.calls.get.length, 2);
  secondPending.resolve({
    job_uid: 'bulk-pending', status: 'completed', total: 2,
    processed: 2, succeeded: 2, failed_count: 0,
  });
  await flush();
  assert.equal(pending.app.job.status, 'done');
  assert.equal(pending.app._jobTimer, null);
  assertTerminalRefresh(pending.calls);

  // Starting a new job invalidates the old in-flight response; only the
  // current job may publish a terminal state or trigger the one refresh.
  const oldResponse = deferred();
  const newResponse = deferred();
  const byUrl = new Map([
    ['/api/jobs/old-job', oldResponse],
    ['/api/jobs/new-job', newResponse],
  ]);
  const replaced = makeApp(url => byUrl.get(url).promise);
  replaced.app.trackJob('old-job', 1);
  const oldTimer = replaced.app._jobTimer;
  replaced.app.trackJob('new-job', 3);
  const newTimer = replaced.app._jobTimer;
  assert.notEqual(newTimer, oldTimer);
  assert.equal(timers.get(oldTimer).active, false);
  oldResponse.resolve({
    job_uid: 'old-job', status: 'completed', total: 1,
    processed: 1, succeeded: 1, failed_count: 0,
  });
  await flush();
  assert.equal(replaced.app.job.status, 'running');
  assert.equal(replaced.app.job.total, 3);
  assert.equal(replaced.app._jobTimer, newTimer);
  assert.equal(replaced.calls.loads, 0);
  assert.equal(replaced.calls.facets, 0);
  newResponse.resolve({
    job_uid: 'new-job', status: 'completed', total: 3,
    processed: 3, succeeded: 3, failed_count: 0,
  });
  await flush();
  assert.equal(replaced.app.job.status, 'done');
  assertTerminalRefresh(replaced.calls);

  // Dismissal invalidates an in-flight response as well as the timer.
  const dismissedResponse = deferred();
  const dismissed = makeApp(() => dismissedResponse.promise);
  dismissed.app.trackJob('dismissed-job', 1);
  const dismissedTimer = dismissed.app._jobTimer;
  assert.equal(typeof dismissed.app.dismissJob, 'function');
  dismissed.app.dismissJob();
  assert.equal(dismissed.app.job, null);
  assert.equal(dismissed.app._jobTimer, null);
  assert.equal(timers.get(dismissedTimer).active, false);
  dismissedResponse.resolve({
    job_uid: 'dismissed-job', status: 'completed', total: 1,
    processed: 1, succeeded: 1, failed_count: 0,
  });
  await flush();
  assert.equal(dismissed.app.job, null);
  assert.equal(dismissed.calls.loads, 0);
  assert.equal(dismissed.calls.facets, 0);
})().catch(error => {
  console.error(error && error.stack || error);
  process.exitCode = 1;
});
"""


def test_my_products_bulk_job_poll_lifecycle_is_terminal_and_read_only():
    node = shutil.which("node")
    if not node:
        import pytest
        pytest.skip("Node.js is required for the isolated UI logic check")

    result = subprocess.run(
        [
            node,
            "-e",
            NODE_CASES,
            str(ROOT / "static" / "my-products-beta.js"),
            str(ROOT / "static" / "sh-vue-kit.js"),
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
