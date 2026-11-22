import test from 'node:test';
import assert from 'node:assert/strict';
import {
  PriceWorkspaceError,
  buildLocalSaveBody,
  buildPricePreviewRequest,
  createPriceWorkspaceAdapter,
  normalizePriceWorkspaceInput,
  validateLocalSaveReceipt,
  validatePricePreviewResponse,
} from '../src/price-workspace-contract.js';

const ORIGIN = 'https://seller.test';
const OWNER = '7:9';
const URLS = {
  wbPreview: '/prices/api/preview',
  wbSave: '/prices/create-batch',
  ozonPreview: '/marketplaces/commercial/price-proposals/bulk-preview',
  ozonSave: '/marketplaces/commercial/price-proposals/bulk-confirm',
};

function wbScope(overrides = {}) {
  return {
    marketplace: 'wb',
    productIds: [11, 12],
    accountId: null,
    accountLabel: 'Wildberries',
    rows: [
      { id: 11, title: 'Товар один', article: 'A-1', photo: null },
      { id: 12, title: 'Товар два', article: 'A-2', photo: '/static/two.png' },
    ],
    ...overrides,
  };
}

function ozonScope(overrides = {}) {
  return {
    marketplace: 'ozon',
    productIds: [201, 202],
    accountId: 77,
    accountLabel: 'Магазин Ozon',
    rows: [
      { id: 201, title: 'Товар один', article: 'A-1', photo: null },
      { id: 202, title: 'Товар два', article: 'A-2', photo: null },
    ],
    ...overrides,
  };
}

function wbInputs(overrides = {}) {
  return {
    changeType: 'percent',
    changeValue: '10',
    formula: '',
    formulaVariables: {},
    priceSource: 'marketplace',
    ...overrides,
  };
}

function ozonInputs(overrides = {}) {
  return {
    mode: 'set',
    value: '3590.00',
    allowPriceDecrease: false,
    allowLargeChange: false,
    guardrailNote: '',
    ...overrides,
  };
}

function wbPreviewBody(scope = wbScope()) {
  return buildPricePreviewRequest(scope, wbInputs());
}

function wbPreviewResponse(overrides = {}) {
  return {
    success: true,
    reviewed: true,
    items: [
      {
        product_id: 11,
        status: 'changed',
        reason: 'Будет добавлено в предложение.',
        source_kind: 'wb_seller_price',
        source_freshness: 'observed',
        source_freshness_at: '2026-11-23T10:00:00',
        old_price: 100,
        new_price: 110,
        change_amount: 10,
        change_percent: 10,
        safety_level: 'safe',
      },
      {
        product_id: 12,
        status: 'no_op',
        reason: 'Цена не изменится.',
        source_kind: 'local_catalog_price',
        source_freshness: 'not_recorded',
        source_freshness_at: null,
        old_price: 200,
        new_price: 200,
        change_amount: 0,
        change_percent: 0,
        safety_level: 'safe',
      },
    ],
    stats: {
      selected_count: 2,
      changed_count: 1,
      no_op_count: 1,
      skipped_count: 0,
      invalid_count: 0,
      safe: 1,
      warning: 0,
      dangerous: 0,
      total: 1,
    },
    can_create: true,
    guardrail_blocked: false,
    review_token: 'signed.review.token',
    review_max_age_seconds: 300,
    ...overrides,
  };
}

function ozonPreviewBody(scope = ozonScope()) {
  return buildPricePreviewRequest(scope, ozonInputs());
}

function ozonPreviewResponse(overrides = {}) {
  return {
    success: true,
    selected_count: 2,
    eligible_count: 1,
    noop_count: 0,
    blocked_count: 1,
    preview_token: 'signed.ozon.preview',
    expires_in_seconds: 600,
    items: [
      {
        listing_id: 201,
        offer_id: 'offer-a',
        product_id: '9001',
        title: 'Товар один',
        status: 'eligible',
        reason_code: null,
        reason: null,
        current_price: '3300.00',
        proposed_price: '3590.00',
        currency_code: 'RUB',
        price_review: {
          price: '3590', requested_price: '3590.00', rounded: true,
          rounding_rule: 'seller_hub_whole_rub_half_up_v1',
          calculation: { mode: 'percent', change_pct: '8.50', internal_provider_data: 'omit' },
        },
        guardrails: { supplier_floor: '2400.00', change_pct: '8.79' },
      },
      {
        listing_id: 202,
        offer_id: 'offer-b',
        product_id: '9002',
        title: 'Товар два',
        status: 'blocked',
        reason_code: 'min_price_limit',
        reason: 'Ниже минимальной цены.',
        current_price: '1200.00',
        proposed_price: null,
        currency_code: 'RUB',
        price_review: {
          price: '3590', requested_price: '3590.00', rounded: true,
          rounding_rule: 'seller_hub_whole_rub_half_up_v1',
        },
        guardrails: { min_price: '1300.00' },
      },
    ],
    ...overrides,
  };
}

function wbReceipt(overrides = {}) {
  return {
    success: true,
    reviewed_write: true,
    reviewed_selection: 'wb_reviewed_selection_v1',
    replayed: false,
    selected_count: 2,
    changed_count: 1,
    batch_id: 301,
    status: 'confirmed',
    stats: { selected_count: 2, changed_count: 1, total: 1 },
    redirect: '/prices/batch/301',
    ...overrides,
  };
}

function ozonReceipt(overrides = {}) {
  const reviewUrl = '/marketplaces/commercial/?account_id=77&proposal_kind=price&status=pending_review&proposal_ids=901';
  return {
    success: true,
    selected_count: 2,
    created_count: 1,
    saved_count: 1,
    replayed_count: 0,
    replayed: false,
    noop_count: 0,
    blocked_count: 1,
    proposal_ids: [901],
    review_url: reviewUrl,
    items: [{
      listing_id: 201,
      status: 'pending_review',
      review_url: reviewUrl,
      proposal: {
        id: 901,
        listing_id: 201,
        account_id: 77,
        proposal_kind: 'price',
        status: 'pending_review',
      },
    }],
    ...overrides,
  };
}

function response(payload, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    async json() { return payload; },
  };
}

function memoryStorage() {
  const values = new Map();
  return {
    values,
    getItem(key) { return values.has(key) ? values.get(key) : null; },
    setItem(key, value) { values.set(key, value); },
    removeItem(key) { values.delete(key); },
  };
}

function adapter({ handler, storage = memoryStorage(), now = () => 1000, urls = URLS } = {}) {
  const calls = [];
  const fetchImpl = async (url, options) => {
    calls.push({ url, options });
    return handler({ url, options, call: calls.length });
  };
  const client = createPriceWorkspaceAdapter({
    urls, owner: OWNER, origin: ORIGIN, fetchImpl, sessionStorage: storage, now,
  });
  return { client, calls, storage };
}

function validatedWbPreview(scope = wbScope(), now = 1000) {
  const body = wbPreviewBody(scope);
  return validatePricePreviewResponse(scope, body, wbPreviewResponse(), { origin: ORIGIN, now });
}

function validatedOzonPreview(scope = ozonScope(), now = 1000) {
  const body = ozonPreviewBody(scope);
  return validatePricePreviewResponse(scope, body, ozonPreviewResponse(), { origin: ORIGIN, now });
}

test('normalizes an exact ordered scope and rejects ID, account, and row mismatches', () => {
  const scope = normalizePriceWorkspaceInput(wbScope());
  assert.deepEqual(scope.productIds, [11, 12]);
  assert.deepEqual(scope.rows.map(row => row.id), [11, 12]);
  assert.ok(Object.isFrozen(scope) && Object.isFrozen(scope.rows[0]));
  assert.equal(normalizePriceWorkspaceInput(wbScope({ productIds: [12, 11] })), null);
  assert.equal(normalizePriceWorkspaceInput(wbScope({ productIds: [11, 11] })), null);
  assert.equal(normalizePriceWorkspaceInput(wbScope({ rows: [{ id: 12 }, { id: 11 }] })), null);
  assert.equal(normalizePriceWorkspaceInput(wbScope({ accountId: 9 })), null);
  assert.equal(normalizePriceWorkspaceInput(ozonScope({ accountId: null })), null);
  assert.equal(normalizePriceWorkspaceInput(ozonScope({ rows: [{ id: 201 }, { id: 203 }] })), null);
  assert.equal(normalizePriceWorkspaceInput(wbScope({ productIds: Array(201).fill(1) })), null);
  assert.equal(normalizePriceWorkspaceInput(ozonScope({ productIds: Array(101).fill(1) })), null);
});

test('builds only native WB request fields, including supplier and formula modes', () => {
  const body = wbPreviewBody();
  assert.deepEqual(Object.keys(body).sort(), [
    'change_type', 'change_value', 'exact_selection', 'formula', 'formula_variables',
    'price_source', 'product_ids', 'reviewed_write',
  ].sort());
  assert.deepEqual(body, {
    reviewed_write: true,
    product_ids: [11, 12],
    change_type: 'percent',
    change_value: '10',
    formula: '',
    formula_variables: {},
    price_source: 'marketplace',
    exact_selection: true,
  });
  assert.equal(buildPricePreviewRequest(wbScope(), wbInputs({
    changeType: 'supplier_pricing', changeValue: null,
  })).change_value, null);
  assert.deepEqual(buildPricePreviewRequest(wbScope(), wbInputs({
    changeType: 'formula', changeValue: null, formula: 'P * RATE', formulaVariables: { RATE: '1.2' },
  })).formula_variables, { RATE: '1.2' });
  assert.equal(buildPricePreviewRequest(wbScope(), wbInputs({ changeType: 'ozon_percent' })), null);
  assert.equal(buildPricePreviewRequest(wbScope(), wbInputs({
    changeType: 'formula', changeValue: null, formula: 'P + X', formulaVariables: { P: '1' },
  })), null);
});

test('builds only Ozon bulk-preview fields and requires a concrete account', () => {
  const body = ozonPreviewBody();
  assert.deepEqual(Object.keys(body).sort(), [
    'account_id', 'allow_large_change', 'allow_price_decrease', 'guardrail_note',
    'listing_ids', 'mode', 'value',
  ].sort());
  assert.deepEqual(body, {
    account_id: 77,
    listing_ids: [201, 202],
    mode: 'set',
    value: '3590.00',
    allow_price_decrease: false,
    allow_large_change: false,
    guardrail_note: '',
  });
  assert.equal(buildPricePreviewRequest(ozonScope(), ozonInputs({ mode: 'fixed' })), null);
  assert.equal(buildPricePreviewRequest(ozonScope(), ozonInputs({ value: '5.555' })), null);
  assert.equal(buildPricePreviewRequest(ozonScope(), ozonInputs({ allowLargeChange: 'yes' })), null);
});

test('validates WB preview identity, counts, safety levels, and visible source freshness', () => {
  const scope = wbScope();
  const preview = validatePricePreviewResponse(scope, wbPreviewBody(scope), wbPreviewResponse(), {
    origin: ORIGIN, now: 1000,
  });
  assert.equal(preview.marketplace, 'wb');
  assert.deepEqual(preview.items.map(item => item.row.id), [11, 12]);
  assert.deepEqual(preview.items.map(item => item.result.productId), [11, 12]);
  assert.equal(preview.items[0].result.sourceKind, 'wb_seller_price');
  assert.equal(preview.items[0].result.sourceFreshnessAt, '2026-11-23T10:00:00');
  assert.equal(preview.summary.changedCount, 1);
  assert.equal(preview.summary.noOpCount, 1);
  assert.equal(preview.expiresAt, 301000);
  assert.equal(validatePricePreviewResponse(scope, wbPreviewBody(scope), wbPreviewResponse({
    items: [wbPreviewResponse().items[1], wbPreviewResponse().items[0]],
  }), { origin: ORIGIN, now: 1000 }), null);
  assert.equal(validatePricePreviewResponse(scope, wbPreviewBody(scope), wbPreviewResponse({
    stats: { ...wbPreviewResponse().stats, changed_count: 2 },
  }), { origin: ORIGIN, now: 1000 }), null);
  assert.equal(validatePricePreviewResponse(scope, wbPreviewBody(scope), wbPreviewResponse({
    review_token: 'bad token',
  }), { origin: ORIGIN, now: 1000 }), null);
});

test('validates Ozon account-scoped listing identities and retains row guardrails', () => {
  const scope = ozonScope();
  const preview = validatePricePreviewResponse(scope, ozonPreviewBody(scope), ozonPreviewResponse(), {
    origin: ORIGIN, now: 1000,
  });
  assert.equal(preview.marketplace, 'ozon');
  assert.equal(preview.accountId, 77);
  assert.deepEqual(preview.items.map(item => item.row.id), [201, 202]);
  assert.deepEqual(preview.items.map(item => item.result.listingId), [201, 202]);
  assert.equal(preview.items[0].result.sourceKind, 'ozon_live_read');
  assert.equal(preview.items[0].result.sourceFreshness, 'read_during_preview');
  assert.equal(preview.items[0].result.sourceFreshnessAt, null);
  assert.equal(preview.items[0].result.currentPrice, '3300.00');
  assert.deepEqual(preview.items[0].result.guardrails, { supplierFloor: '2400.00', changePct: '8.79' });
  assert.deepEqual(preview.items[0].result.priceReview, {
    requestedPrice: '3590.00', price: '3590', rounded: true,
    roundingRule: 'seller_hub_whole_rub_half_up_v1',
    calculation: { mode: 'percent', changePct: '8.50' },
  });
  assert.equal(preview.summary.eligibleCount, 1);
  assert.equal(preview.summary.blockedCount, 1);
  assert.equal(preview.canSave, true);
  assert.equal(preview.guardrailBlocked, false);
  assert.equal(preview.hasBlockedRows, true);
  assert.equal(preview.expiresAt, 601000);
  assert.equal(validatePricePreviewResponse(scope, ozonPreviewBody(scope), ozonPreviewResponse({
    selected_count: 1,
  }), { origin: ORIGIN, now: 1000 }), null);
  assert.equal(validatePricePreviewResponse(scope, ozonPreviewBody(scope), ozonPreviewResponse({
    items: [ozonPreviewResponse().items[1], ozonPreviewResponse().items[0]],
  }), { origin: ORIGIN, now: 1000 }), null);
});

test('accepts the backend maximum Ozon token while keeping the smaller WB limit', () => {
  const ozonToken = `a.${'b'.repeat(131070)}`;
  assert.equal(ozonToken.length, 131072);
  const ozon = validatePricePreviewResponse(ozonScope(), ozonPreviewBody(),
    ozonPreviewResponse({ preview_token: ozonToken }), { origin: ORIGIN, now: 1000 });
  assert.ok(ozon);
  const wbToken = `a.${'b'.repeat(4094)}`;
  assert.equal(wbToken.length, 4096);
  assert.ok(validatePricePreviewResponse(wbScope(), wbPreviewBody(),
    wbPreviewResponse({ review_token: wbToken }), { origin: ORIGIN, now: 1000 }));
  assert.equal(validatePricePreviewResponse(wbScope(), wbPreviewBody(),
    wbPreviewResponse({ review_token: `${wbToken}x` }), { origin: ORIGIN, now: 1000 }), null);
  assert.equal(validatePricePreviewResponse(ozonScope(), ozonPreviewBody(),
    ozonPreviewResponse({ preview_token: `${ozonToken}x` }), { origin: ORIGIN, now: 1000 }), null);
});

test('builds immutable local-save bodies without provider-write fields', () => {
  const wb = buildLocalSaveBody(validatedWbPreview(), { name: 'Проверка', description: 'Обоснование' });
  assert.deepEqual(Object.keys(wb).sort(), [
    'change_type', 'change_value', 'description', 'exact_selection', 'formula',
    'formula_variables', 'name', 'price_source', 'product_ids', 'review_token', 'reviewed_write',
  ].sort());
  assert.equal(wb.review_token, 'signed.review.token');
  assert.equal(Object.isFrozen(wb), true);
  const ozon = buildLocalSaveBody(validatedOzonPreview(), { name: 'ignored' });
  assert.deepEqual(ozon, { preview_token: 'signed.ozon.preview', confirm_save: true });
  assert.deepEqual(Object.keys(ozon).sort(), ['confirm_save', 'preview_token']);
});

test('validates WB local batch receipt and exact same-origin destination', () => {
  const preview = validatedWbPreview();
  const receipt = validateLocalSaveReceipt(preview, wbReceipt(), { origin: ORIGIN });
  assert.deepEqual(receipt.destination, { href: '/prices/batch/301', label: 'Открыть пакет цен' });
  assert.equal(receipt.batchId, 301);
  assert.equal(validateLocalSaveReceipt(preview, wbReceipt({
    status: 'pending_review', redirect: '/prices/batch/301/confirm',
  }), { origin: ORIGIN }).destination.label, 'Проверить изменения');
  assert.equal(validateLocalSaveReceipt(preview, wbReceipt({ redirect: 'https://evil.test/prices/batch/301' }), { origin: ORIGIN }), null);
  assert.equal(validateLocalSaveReceipt(preview, wbReceipt({ redirect: '/prices/batch/999' }), { origin: ORIGIN }), null);
  assert.equal(validateLocalSaveReceipt(preview, wbReceipt({ changed_count: 2 }), { origin: ORIGIN }), null);
  assert.equal(validateLocalSaveReceipt(preview, wbReceipt({ reviewed_selection: 'other' }), { origin: ORIGIN }), null);
});

test('validates Ozon saved proposals and the filtered review link against exact IDs/store', () => {
  const receipt = validateLocalSaveReceipt(validatedOzonPreview(), ozonReceipt(), { origin: ORIGIN });
  assert.deepEqual(receipt.destination, {
    href: '/marketplaces/commercial/?account_id=77&proposal_kind=price&status=pending_review&proposal_ids=901',
    label: 'Проверить предложения',
  });
  assert.deepEqual(receipt.proposalIds, [901]);
  assert.equal(validateLocalSaveReceipt(validatedOzonPreview(), ozonReceipt({
    proposal_ids: [902],
  }), { origin: ORIGIN }), null);
  assert.equal(validateLocalSaveReceipt(validatedOzonPreview(), ozonReceipt({
    review_url: 'https://evil.test/marketplaces/commercial/?account_id=77&proposal_kind=price&status=pending_review&proposal_ids=901',
  }), { origin: ORIGIN }), null);
  assert.equal(validateLocalSaveReceipt(validatedOzonPreview(), ozonReceipt({
    items: [{ ...ozonReceipt().items[0], proposal: { ...ozonReceipt().items[0].proposal, account_id: 78 } }],
  }), { origin: ORIGIN }), null);
  assert.equal(validateLocalSaveReceipt(validatedOzonPreview(), ozonReceipt({
    items: [{ ...ozonReceipt().items[0], proposal: { ...ozonReceipt().items[0].proposal, listing_id: 202 } }],
  }), { origin: ORIGIN }), null);
});

test('posts preview only when called, with same-origin JSON and no duplicate CSRF implementation', async () => {
  const { client, calls } = adapter({
    handler: async ({ url, options }) => {
      assert.equal(url, `${ORIGIN}${URLS.wbPreview}`);
      assert.equal(options.method, 'POST');
      assert.equal(options.credentials, 'same-origin');
      assert.deepEqual(options.headers, { 'Content-Type': 'application/json' });
      return response(wbPreviewResponse());
    },
  });
  assert.equal(calls.length, 0);
  const preview = await client.preview(wbScope(), wbInputs());
  assert.equal(calls.length, 1);
  assert.equal(preview.summary.selectedCount, 2);
});

test('refuses cross-origin endpoint URLs and malformed API responses', async () => {
  const bad = adapter({
    urls: { ...URLS, wbPreview: 'https://evil.test/prices/api/preview' },
    handler: async () => { throw new Error('must not be called'); },
  });
  await assert.rejects(bad.client.preview(wbScope(), wbInputs()),
    error => error instanceof PriceWorkspaceError && error.code === 'endpoint_invalid');
  const malformed = adapter({ handler: async () => response({ success: true, items: [] }) });
  await assert.rejects(malformed.client.preview(wbScope(), wbInputs()),
    error => error.code === 'preview_response_invalid');
});

test('unknown WB save outcome stays pending and explicit retry replays identical bytes', async () => {
  let now = 1000;
  let saveCalls = 0;
  const state = adapter({
    now: () => now,
    handler: async ({ url }) => {
      if (url.endsWith(URLS.wbPreview)) return response(wbPreviewResponse());
      saveCalls += 1;
      if (saveCalls === 1) throw new TypeError('connection lost');
      return response(wbReceipt({ replayed: true }));
    },
  });
  const scope = wbScope();
  const preview = await state.client.preview(scope, wbInputs());
  let error;
  try {
    await state.client.save(preview, { name: 'Batch', description: 'Why' });
  } catch (caught) {
    error = caught;
  }
  assert.equal(error.code, 'transport_failed');
  assert.equal(error.outcomeUnknown, true);
  const descriptor = state.client.recover(scope);
  assert.equal(descriptor.state, 'unknown');
  assert.equal(descriptor.retryAvailable, true);
  assert.equal(saveCalls, 1);
  const firstBody = state.calls[1].options.body;
  const receipt = await state.client.retryPending(scope);
  assert.equal(saveCalls, 2);
  assert.equal(state.calls[2].options.body, firstBody);
  assert.equal(receipt.replayed, true);
  assert.equal(state.client.recover(scope), null);
});

test('a typed rejection on explicit retry after an unknown save does not clear pending scope', async () => {
  let saveCalls = 0;
  const state = adapter({
    handler: async ({ url }) => {
      if (url.endsWith(URLS.wbPreview)) return response(wbPreviewResponse());
      saveCalls += 1;
      if (saveCalls === 1) throw new TypeError('connection lost');
      if (saveCalls === 2) return response({ success: false, code: 'review_stale', error: 'Сравните снова.' }, 409);
      return response(wbReceipt({ replayed: true }));
    },
  });
  const scope = wbScope();
  const preview = await state.client.preview(scope, wbInputs());
  await assert.rejects(state.client.save(preview), error => error.outcomeUnknown === true);
  const body = state.calls[1].options.body;
  await assert.rejects(state.client.retryPending(scope), error =>
    error.outcomeUnknown === true && error.code === 'save_outcome_unknown');
  assert.equal(state.client.recover(scope).state, 'unknown');
  assert.equal(saveCalls, 2);
  const receipt = await state.client.retryPending(scope);
  assert.equal(receipt.replayed, true);
  assert.equal(state.calls[2].options.body, body);
  assert.equal(state.calls[3].options.body, body);
  assert.equal(state.client.recover(scope), null);
});

test('large Ozon preview token survives pending storage and explicit recovery', async () => {
  const token = `a.${'b'.repeat(131070)}`;
  let saveCalls = 0;
  const state = adapter({
    handler: async ({ url }) => {
      if (url.endsWith(URLS.ozonPreview)) return response(ozonPreviewResponse({ preview_token: token }));
      saveCalls += 1;
      throw new TypeError('response lost');
    },
  });
  const scope = ozonScope();
  const preview = await state.client.preview(scope, ozonInputs());
  assert.equal(preview.token.length, 131072);
  await assert.rejects(state.client.save(preview), error => error.outcomeUnknown === true);
  const serialized = [...state.storage.values.values()][0];
  assert.ok(serialized.length < 300000);
  assert.equal(state.client.recover(scope).retryAvailable, true);
  assert.equal(saveCalls, 1);
});

test('Ozon local save recovery is owner, store, lane, selection, and expiry scoped', async () => {
  let now = 1000;
  let saveCalls = 0;
  const state = adapter({
    now: () => now,
    handler: async ({ url }) => {
      if (url.endsWith(URLS.ozonPreview)) return response(ozonPreviewResponse());
      saveCalls += 1;
      if (saveCalls === 1) throw new TypeError('response lost');
      return response(ozonReceipt({ replayed: true, created_count: 0, replayed_count: 1 }));
    },
  });
  const scope = ozonScope();
  const preview = await state.client.preview(scope, ozonInputs());
  await assert.rejects(state.client.save(preview), error => error.outcomeUnknown === true);
  assert.equal(state.client.recover(ozonScope({ accountId: 78 })), null);
  assert.equal(state.client.recover(ozonScope({ productIds: [202, 201], rows: [
    { id: 202, title: 'Товар два' }, { id: 201, title: 'Товар один' },
  ] })), null);
  const recovered = state.client.recover(scope);
  assert.equal(recovered.state, 'unknown');
  assert.equal(recovered.retryAvailable, true);
  const savedBytes = state.calls[1].options.body;
  const receipt = await state.client.retryPending(scope);
  assert.equal(state.calls[2].options.body, savedBytes);
  assert.equal(receipt.replayed, true);
  assert.equal(state.client.recover(scope), null);
  now += 601000;
  assert.equal(saveCalls, 2);
});

test('recovery inspection never sends and an expired Ozon preview cannot retry', async () => {
  let now = 1000;
  let saveCalls = 0;
  const state = adapter({
    now: () => now,
    handler: async ({ url }) => {
      if (url.endsWith(URLS.ozonPreview)) return response(ozonPreviewResponse());
      saveCalls += 1;
      throw new TypeError('lost');
    },
  });
  const scope = ozonScope();
  const preview = await state.client.preview(scope, ozonInputs());
  await assert.rejects(state.client.save(preview), error => error.outcomeUnknown === true);
  const countBeforeRecover = state.calls.length;
  now = preview.expiresAt + 1;
  const recovered = state.client.recover(scope);
  assert.equal(recovered.state, 'unknown');
  assert.equal(recovered.retryAvailable, false);
  assert.equal(recovered.expired, true);
  assert.equal(state.calls.length, countBeforeRecover);
  await assert.rejects(state.client.retryPending(scope), error => error.code === 'pending_preview_expired');
  assert.equal(saveCalls, 1);
});

test('known local-save rejection clears pending without an automatic retry', async () => {
  let saveCalls = 0;
  const state = adapter({
    handler: async ({ url }) => {
      if (url.endsWith(URLS.wbPreview)) return response(wbPreviewResponse());
      saveCalls += 1;
      return response({ success: false, code: 'review_stale', error: 'Сравните снова.' }, 409);
    },
  });
  const scope = wbScope();
  const preview = await state.client.preview(scope, wbInputs());
  await assert.rejects(state.client.save(preview), error => error.outcomeUnknown === false && error.status === 409);
  assert.equal(state.client.recover(scope), null);
  assert.equal(saveCalls, 1);
});

test('5xx and malformed successful receipts retain exact pending save for explicit replay', async () => {
  let saveCalls = 0;
  const state = adapter({
    handler: async ({ url }) => {
      if (url.endsWith(URLS.wbPreview)) return response(wbPreviewResponse());
      saveCalls += 1;
      return saveCalls === 1 ? response({ success: false }, 503) : response(wbReceipt({ replayed: true }));
    },
  });
  const scope = wbScope();
  const preview = await state.client.preview(scope, wbInputs());
  await assert.rejects(state.client.save(preview), error => error.outcomeUnknown === true && error.status === 503);
  assert.equal(state.client.recover(scope).retryAvailable, true);
  const body = state.calls[1].options.body;
  await state.client.retryPending(scope);
  assert.equal(state.calls[2].options.body, body);
  assert.equal(saveCalls, 2);
});

test('adapter refuses a second new local save while an unresolved exact receipt exists', async () => {
  let saveCalls = 0;
  const state = adapter({
    handler: async ({ url }) => {
      if (url.endsWith(URLS.wbPreview)) return response(wbPreviewResponse());
      saveCalls += 1;
      throw new TypeError('lost');
    },
  });
  const scope = wbScope();
  const preview = await state.client.preview(scope, wbInputs());
  await assert.rejects(state.client.save(preview), error => error.outcomeUnknown === true);
  await assert.rejects(state.client.save(preview), error => error.code === 'pending_save_exists');
  assert.equal(saveCalls, 1);
});

test('Ozon receipt replay requires exact ordered eligible listing results and account binding', () => {
  const preview = validatedOzonPreview();
  assert.ok(validateLocalSaveReceipt(preview, ozonReceipt({
    replayed: true, created_count: 0, replayed_count: 1,
  }), { origin: ORIGIN }));
  assert.equal(validateLocalSaveReceipt(preview, ozonReceipt({
    replayed: true, created_count: 0, replayed_count: 0,
  }), { origin: ORIGIN }), null);
  assert.equal(validateLocalSaveReceipt(preview, ozonReceipt({
    review_url: '/marketplaces/commercial/?account_id=78&proposal_kind=price&status=pending_review&proposal_ids=901',
  }), { origin: ORIGIN }), null);
  assert.equal(validateLocalSaveReceipt(preview, ozonReceipt({
    items: [{ ...ozonReceipt().items[0], status: 'applied' }],
  }), { origin: ORIGIN }), null);
});

test('preview remains available without session storage, save fails closed, and corrupt recovery fails closed', async () => {
  const noStorage = createPriceWorkspaceAdapter({
    urls: URLS, owner: OWNER, origin: ORIGIN, fetchImpl: async () => response(wbPreviewResponse()),
    sessionStorage: null, now: () => 1000,
  });
  const noStoragePreview = await noStorage.preview(wbScope(), wbInputs());
  await assert.rejects(noStorage.save(noStoragePreview), error => error.code === 'pending_storage_unavailable');
  const storage = memoryStorage();
  const state = adapter({ storage, handler: async () => response(wbPreviewResponse()) });
  const scope = wbScope();
  const preview = await state.client.preview(scope, wbInputs());
  storage.setItem('seller-hub-price-local-save-v1:7%3A9:wb:0:11,12', '{bad json');
  assert.throws(() => state.client.recover(scope), error => error.code === 'pending_record_invalid');
  await assert.rejects(state.client.save(preview), error => error.code === 'pending_record_invalid');
});
