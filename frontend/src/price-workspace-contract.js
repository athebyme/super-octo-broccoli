const MAX_WB_SELECTION = 200;
const MAX_OZON_SELECTION = 100;
const MAX_WB_TOKEN_CHARS = 4096;
const MAX_OZON_TOKEN_CHARS = 131072;
const MAX_PENDING_RECORD_CHARS = 300000;
const MAX_PENDING_BODY_CHARS = 140000;
const TOKEN = /^[A-Za-z0-9_.-]+$/;
const OWNER = /^[1-9][0-9]{0,18}:[1-9][0-9]{0,18}$/;
const DECIMAL = /^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$/;
const WB_CHANGE_TYPES = new Set([
  'fixed', 'percent', 'set', 'formula', 'supplier_pricing',
]);
const WB_PRICE_SOURCES = new Set(['local', 'marketplace']);
const WB_STATUSES = new Set(['changed', 'no_op', 'skipped', 'invalid']);
const OZON_STATUSES = new Set(['eligible', 'noop', 'blocked']);
const PREVIEW_TTL_MAX = Object.freeze({ wb: 300, ozon: 600 });

function browserSessionStorage() {
  try {
    return globalThis.sessionStorage;
  } catch (_) {
    return null;
  }
}

export class PriceWorkspaceError extends Error {
  constructor(code, message, options = {}) {
    super(message);
    this.name = 'PriceWorkspaceError';
    this.code = code;
    this.status = Number.isSafeInteger(options.status) ? options.status : null;
    this.outcomeUnknown = options.outcomeUnknown === true;
  }
}

function object(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function boundedText(value, max = 500, { allowLines = true, empty = true } = {}) {
  if (typeof value !== 'string' || value.length > max || (!empty && !value.trim())) return false;
  const controls = allowLines ? /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/ : /[\u0000-\u001f\u007f]/;
  return !controls.test(value);
}

function safeId(value) {
  return Number.isSafeInteger(value) && value > 0;
}

function sameIds(left, right) {
  return Array.isArray(left) && Array.isArray(right) && left.length === right.length &&
    left.every((id, index) => id === right[index]);
}

function freezeDeep(value) {
  if (!value || typeof value !== 'object' || Object.isFrozen(value)) return value;
  Object.values(value).forEach(freezeDeep);
  return Object.freeze(value);
}

function freezeJson(value) {
  if (value === null || typeof value === 'string' || typeof value === 'boolean') return value;
  if (typeof value === 'number') return Number.isFinite(value) ? value : null;
  if (Array.isArray(value)) return value.map(freezeJson);
  if (!object(value)) return null;
  const result = {};
  for (const [key, item] of Object.entries(value)) {
    if (key === '__proto__' || key === 'constructor' || key === 'prototype') continue;
    result[key] = freezeJson(item);
  }
  return result;
}

function numericOrDecimal(value) {
  if (typeof value === 'number') return Number.isFinite(value) && Math.abs(value) < 1e15;
  if (typeof value !== 'string' || value.length > 100 || !DECIMAL.test(value.trim())) return false;
  return Number.isFinite(Number(value));
}

function normalizeDecimal(value, { signed = true, max = 32, maxFraction = null } = {}) {
  const text = typeof value === 'number' ? String(value) : value;
  if (typeof text !== 'string') return null;
  const normalized = text.trim();
  if (!normalized || normalized.length > max || !DECIMAL.test(normalized) ||
      (!signed && normalized.startsWith('-')) || !Number.isFinite(Number(normalized))) return null;
  if (maxFraction !== null && (normalized.split('.')[1]?.length ?? 0) > maxFraction) return null;
  return normalized;
}

function safePhoto(value, origin) {
  if (value === null || value === undefined || value === '') return null;
  if (!boundedText(value, 2048, { allowLines: false, empty: false })) return null;
  try {
    const base = origin ? new URL(origin).origin : 'https://seller.invalid';
    const target = new URL(value, base);
    if (!['http:', 'https:'].includes(target.protocol) || target.username || target.password) return null;
    return value;
  } catch (_) {
    return null;
  }
}

function normalizeRows(rows, ids, origin) {
  if (!Array.isArray(rows) || rows.length !== ids.length) return null;
  const normalized = [];
  for (let index = 0; index < rows.length; index += 1) {
    const row = rows[index];
    if (!object(row) || row.id !== ids[index] ||
        !(row.title === null || row.title === undefined || boundedText(row.title, 5000)) ||
        !(row.article === null || row.article === undefined || boundedText(row.article, 500)) ||
        !(row.photo === null || row.photo === undefined || typeof row.photo === 'string')) return null;
    normalized.push({
      id: ids[index],
      title: typeof row.title === 'string' ? row.title : '',
      article: typeof row.article === 'string' ? row.article : '',
      photo: safePhoto(row.photo, origin),
    });
  }
  return normalized;
}

export function normalizePriceWorkspaceInput(input, { origin } = {}) {
  if (!object(input) || !['wb', 'ozon'].includes(input.marketplace) ||
      !Array.isArray(input.productIds) || input.productIds.length === 0) return null;
  const maximum = input.marketplace === 'wb' ? MAX_WB_SELECTION : MAX_OZON_SELECTION;
  if (input.productIds.length > maximum || input.productIds.some(id => !safeId(id)) ||
      new Set(input.productIds).size !== input.productIds.length) return null;
  const rows = normalizeRows(input.rows, input.productIds, origin);
  if (!rows) return null;
  if (input.marketplace === 'wb') {
    if (input.accountId !== null) return null;
    if (input.accountLabel !== null && input.accountLabel !== undefined &&
        !boundedText(input.accountLabel, 200)) return null;
  } else if (!safeId(input.accountId) || !boundedText(input.accountLabel, 200, { empty: false })) {
    return null;
  }
  return freezeDeep({
    marketplace: input.marketplace,
    productIds: [...input.productIds],
    accountId: input.marketplace === 'ozon' ? input.accountId : null,
    accountLabel: typeof input.accountLabel === 'string' ? input.accountLabel : '',
    rows,
  });
}

function normalizeFormulaVariables(value) {
  if (!object(value) || Object.keys(value).length > 100) return null;
  const result = {};
  for (const [key, raw] of Object.entries(value)) {
    if (!/^[A-Z][A-Z0-9_]{0,15}$/.test(key) || key === 'P') return null;
    const decimal = normalizeDecimal(raw, { max: 64 });
    if (decimal === null) return null;
    result[key] = decimal;
  }
  return result;
}

function normalizeWbInputs(inputs) {
  if (!object(inputs) || !WB_CHANGE_TYPES.has(inputs.changeType) ||
      !WB_PRICE_SOURCES.has(inputs.priceSource)) return null;
  const formula = inputs.formula ?? '';
  if (!boundedText(formula, 200, { allowLines: false })) return null;
  const variables = normalizeFormulaVariables(inputs.formulaVariables ?? {});
  if (!variables) return null;
  const needsValue = !['formula', 'supplier_pricing'].includes(inputs.changeType);
  const changeValue = needsValue ? normalizeDecimal(inputs.changeValue, { max: 64 }) : null;
  if (needsValue && changeValue === null) return null;
  if (inputs.changeType === 'formula' && !formula.trim()) return null;
  if (inputs.changeType !== 'formula' && Object.keys(variables).length) return null;
  return {
    reviewed_write: true,
    product_ids: [...inputs.productIds],
    change_type: inputs.changeType,
    change_value: changeValue,
    formula,
    formula_variables: variables,
    price_source: inputs.priceSource,
    exact_selection: true,
  };
}

function normalizeOzonInputs(inputs, accountId, listingIds) {
  if (!object(inputs) || !['set', 'percent'].includes(inputs.mode)) return null;
  const value = normalizeDecimal(inputs.value, {
    signed: inputs.mode === 'percent', max: 32, maxFraction: 2,
  });
  const note = inputs.guardrailNote ?? '';
  if (value === null || (inputs.mode === 'set' && Number(value) <= 0) ||
      typeof inputs.allowPriceDecrease !== 'boolean' ||
      typeof inputs.allowLargeChange !== 'boolean' ||
      !boundedText(note, 1000)) return null;
  return {
    account_id: accountId,
    listing_ids: [...listingIds],
    mode: inputs.mode,
    value,
    allow_price_decrease: inputs.allowPriceDecrease,
    allow_large_change: inputs.allowLargeChange,
    guardrail_note: note,
  };
}

export function buildPricePreviewRequest(input, nativeInputs) {
  const scope = normalizePriceWorkspaceInput(input);
  if (!scope) return null;
  const body = scope.marketplace === 'wb'
    ? normalizeWbInputs({ ...nativeInputs, productIds: scope.productIds })
    : normalizeOzonInputs(nativeInputs, scope.accountId, scope.productIds);
  return body ? freezeDeep(body) : null;
}

function validToken(value, marketplace) {
  const maximum = marketplace === 'wb' ? MAX_WB_TOKEN_CHARS :
    marketplace === 'ozon' ? MAX_OZON_TOKEN_CHARS : 0;
  return typeof value === 'string' && value.length > 0 && value.length <= maximum &&
    TOKEN.test(value) && value.includes('.');
}

function nullableNumber(value) {
  return value === null || value === undefined ||
    (typeof value === 'number' && Number.isFinite(value));
}

function safeReason(value) {
  return value === null || value === undefined || boundedText(value, 2000);
}

function wbReceiptStats(value) {
  if (!object(value)) return null;
  const result = {};
  for (const [source, target] of [
    ['selected_count', 'selectedCount'], ['changed_count', 'changedCount'],
    ['safe', 'safeCount'], ['warning', 'warningCount'], ['dangerous', 'dangerousCount'],
    ['no_op_count', 'noOpCount'], ['skipped_count', 'skippedCount'],
    ['invalid_count', 'invalidCount'], ['total', 'total'],
  ]) {
    if (Object.hasOwn(value, source)) {
      if (!nonNegativeCount(value[source])) return null;
      result[target] = value[source];
    }
  }
  return result;
}

function wbItem(item, expectedId) {
  if (!object(item) || item.product_id !== expectedId || !WB_STATUSES.has(item.status) ||
      !safeReason(item.reason) ||
      !(item.source_kind === null || item.source_kind === undefined ||
        ['wb_seller_price', 'local_catalog_price', 'local_fallback'].includes(item.source_kind)) ||
      !(item.source_freshness === 'observed' || item.source_freshness === 'not_recorded') ||
      !(item.source_freshness_at === null || item.source_freshness_at === undefined ||
        boundedText(item.source_freshness_at, 100, { allowLines: false })) ||
      !nullableNumber(item.old_price) || !nullableNumber(item.new_price) ||
      !nullableNumber(item.change_amount) || !nullableNumber(item.change_percent) ||
      !(item.safety_level === null || item.safety_level === undefined ||
        ['safe', 'warning', 'dangerous'].includes(item.safety_level))) return null;
  if (item.status === 'changed' &&
      (!Number.isFinite(item.new_price) || !['safe', 'warning', 'dangerous'].includes(item.safety_level))) return null;
  return {
    productId: item.product_id,
    status: item.status,
    reason: item.reason ?? null,
    sourceKind: item.source_kind ?? null,
    sourceFreshness: item.source_freshness,
    sourceFreshnessAt: item.source_freshness_at ?? null,
    oldPrice: item.old_price ?? null,
    newPrice: item.new_price ?? null,
    changeAmount: item.change_amount ?? null,
    changePercent: item.change_percent ?? null,
    safetyLevel: item.safety_level ?? null,
  };
}

function nonNegativeCount(value) {
  return Number.isSafeInteger(value) && value >= 0;
}

function validateWbPreview(scope, requestBody, payload) {
  if (!object(payload) || payload.success !== true || payload.reviewed !== true ||
      !Array.isArray(payload.items) || payload.items.length !== scope.productIds.length ||
      !object(payload.stats) || !validToken(payload.review_token, 'wb') ||
      !Number.isSafeInteger(payload.review_max_age_seconds) ||
      payload.review_max_age_seconds < 1 || payload.review_max_age_seconds > PREVIEW_TTL_MAX.wb ||
      typeof payload.can_create !== 'boolean' || typeof payload.guardrail_blocked !== 'boolean') return null;
  const items = payload.items.map((item, index) => wbItem(item, scope.productIds[index]));
  if (items.some(item => item === null)) return null;
  const counts = {
    selectedCount: items.length,
    changedCount: items.filter(item => item.status === 'changed').length,
    noOpCount: items.filter(item => item.status === 'no_op').length,
    skippedCount: items.filter(item => item.status === 'skipped').length,
    invalidCount: items.filter(item => item.status === 'invalid').length,
    safeCount: items.filter(item => item.status === 'changed' && item.safetyLevel === 'safe').length,
    warningCount: items.filter(item => item.status === 'changed' && item.safetyLevel === 'warning').length,
    dangerousCount: items.filter(item => item.status === 'changed' && item.safetyLevel === 'dangerous').length,
  };
  const stats = payload.stats;
  if (stats.selected_count !== counts.selectedCount || stats.changed_count !== counts.changedCount ||
      stats.no_op_count !== counts.noOpCount || stats.skipped_count !== counts.skippedCount ||
      stats.invalid_count !== counts.invalidCount || stats.safe !== counts.safeCount ||
      stats.warning !== counts.warningCount || stats.dangerous !== counts.dangerousCount ||
      stats.total !== counts.changedCount ||
      ![stats.selected_count, stats.changed_count, stats.no_op_count, stats.skipped_count,
        stats.invalid_count, stats.safe, stats.warning, stats.dangerous, stats.total].every(nonNegativeCount) ||
      (payload.can_create && (counts.changedCount === 0 || payload.guardrail_blocked))) return null;
  return {
    items,
    summary: {
      ...counts,
      eligibleCount: counts.changedCount,
      blockedCount: payload.guardrail_blocked ? counts.dangerousCount : 0,
      total: counts.changedCount,
    },
    canSave: payload.can_create,
    guardrailBlocked: payload.guardrail_blocked,
    token: payload.review_token,
    expiresInSeconds: payload.review_max_age_seconds,
  };
}

function decimalString(value) {
  return typeof value === 'string' && value.length <= 100 && DECIMAL.test(value) &&
    Number.isFinite(Number(value));
}

function normalizeOzonPriceReview(value) {
  if (!object(value) || !decimalString(value.requested_price) ||
      !decimalString(value.price) || typeof value.rounded !== 'boolean' ||
      value.rounding_rule !== 'seller_hub_whole_rub_half_up_v1') return null;
  const result = {
    requestedPrice: value.requested_price,
    price: value.price,
    rounded: value.rounded,
    roundingRule: value.rounding_rule,
  };
  if (Object.hasOwn(value, 'calculation')) {
    const calc = value.calculation;
    if (!object(calc) || calc.mode !== 'percent' || !decimalString(calc.change_pct)) return null;
    result.calculation = { mode: 'percent', changePct: calc.change_pct };
  }
  return result;
}

function normalizeOzonGuardrails(value) {
  if (!object(value)) return null;
  const result = {};
  if (Object.hasOwn(value, 'direction')) {
    if (!['increase', 'decrease', 'same'].includes(value.direction)) return null;
    result.direction = value.direction;
  }
  for (const [source, target] of [
    ['change_pct', 'changePct'], ['max_change_pct', 'maxChangePct'],
    ['preserved_old_price', 'preservedOldPrice'], ['preserved_min_price', 'preservedMinPrice'],
  ]) {
    if (Object.hasOwn(value, source)) {
      if (!decimalString(value[source])) return null;
      result[target] = value[source];
    }
  }
  if (Object.hasOwn(value, 'supplier_floor')) {
    if (value.supplier_floor !== null && !decimalString(value.supplier_floor)) return null;
    result.supplierFloor = value.supplier_floor;
  }
  for (const [source, target] of [
    ['allow_price_decrease', 'allowPriceDecrease'],
    ['allow_large_change', 'allowLargeChange'],
  ]) {
    if (Object.hasOwn(value, source)) {
      if (typeof value[source] !== 'boolean') return null;
      result[target] = value[source];
    }
  }
  if (Object.hasOwn(value, 'override_note')) {
    if (!boundedText(value.override_note, 1000)) return null;
    result.overrideNote = value.override_note;
  }
  return result;
}

function ozonItem(item, expectedId) {
  if (!object(item) || item.listing_id !== expectedId || !OZON_STATUSES.has(item.status) ||
      !safeReason(item.reason) ||
      !(item.reason_code === null || item.reason_code === undefined ||
        /^[a-z0-9_]{1,100}$/.test(item.reason_code)) ||
      !numericOrDecimal(item.current_price) ||
      !(item.proposed_price === null || item.proposed_price === undefined || numericOrDecimal(item.proposed_price)) ||
      !boundedText(item.currency_code, 12, { allowLines: false, empty: false }) ||
      !(item.price_review === null || object(item.price_review)) || !object(item.guardrails)) return null;
  const priceReview = item.price_review === null ? null : normalizeOzonPriceReview(item.price_review);
  const guardrails = normalizeOzonGuardrails(item.guardrails);
  if ((item.price_review !== null && priceReview === null) || guardrails === null) return null;
  if (item.status === 'eligible' && (item.proposed_price === null || item.price_review === null)) return null;
  return {
    listingId: item.listing_id,
    status: item.status,
    reasonCode: item.reason_code ?? null,
    reason: item.reason ?? null,
    sourceKind: 'ozon_live_read',
    sourceFreshness: 'read_during_preview',
    sourceFreshnessAt: null,
    currentPrice: item.current_price,
    proposedPrice: item.proposed_price ?? null,
    currencyCode: item.currency_code,
    priceReview,
    guardrails,
  };
}

function validateOzonPreview(scope, payload) {
  if (!object(payload) || payload.success !== true || !Array.isArray(payload.items) ||
      payload.items.length !== scope.productIds.length || !validToken(payload.preview_token, 'ozon') ||
      !Number.isSafeInteger(payload.expires_in_seconds) || payload.expires_in_seconds < 1 ||
      payload.expires_in_seconds > PREVIEW_TTL_MAX.ozon) return null;
  const items = payload.items.map((item, index) => ozonItem(item, scope.productIds[index]));
  if (items.some(item => item === null)) return null;
  const summary = {
    selectedCount: items.length,
    eligibleCount: items.filter(item => item.status === 'eligible').length,
    noOpCount: items.filter(item => item.status === 'noop').length,
    blockedCount: items.filter(item => item.status === 'blocked').length,
    changedCount: items.filter(item => item.status === 'eligible').length,
    skippedCount: 0,
    invalidCount: 0,
    safeCount: 0,
    warningCount: 0,
    dangerousCount: 0,
    total: items.filter(item => item.status === 'eligible').length,
  };
  if (payload.selected_count !== summary.selectedCount ||
      payload.eligible_count !== summary.eligibleCount ||
      payload.noop_count !== summary.noOpCount ||
      payload.blocked_count !== summary.blockedCount ||
      ![payload.selected_count, payload.eligible_count, payload.noop_count,
        payload.blocked_count].every(nonNegativeCount)) return null;
  return {
    items,
    summary,
    canSave: summary.eligibleCount > 0,
    guardrailBlocked: false,
    hasBlockedRows: summary.blockedCount > 0,
    token: payload.preview_token,
    expiresInSeconds: payload.expires_in_seconds,
  };
}

export function validatePricePreviewResponse(input, requestBody, payload, { origin, now = Date.now() } = {}) {
  const scope = normalizePriceWorkspaceInput(input, { origin });
  if (!scope || !object(requestBody) || !Number.isFinite(now)) return null;
  let validated;
  if (scope.marketplace === 'wb') {
    if (requestBody.reviewed_write !== true || requestBody.exact_selection !== true ||
        !sameIds(requestBody.product_ids, scope.productIds)) return null;
    validated = validateWbPreview(scope, requestBody, payload);
  } else {
    if (requestBody.account_id !== scope.accountId ||
        !sameIds(requestBody.listing_ids, scope.productIds)) return null;
    validated = validateOzonPreview(scope, payload);
  }
  if (!validated) return null;
  const expiresAt = now + validated.expiresInSeconds * 1000;
  const items = validated.items.map((result, index) => freezeDeep({
    row: scope.rows[index],
    result,
  }));
  return freezeDeep({
    marketplace: scope.marketplace,
    productIds: scope.productIds,
    accountId: scope.accountId,
    accountLabel: scope.accountLabel,
    rows: scope.rows,
    items,
    summary: validated.summary,
    canSave: validated.canSave,
    guardrailBlocked: validated.guardrailBlocked,
    hasBlockedRows: validated.hasBlockedRows === true,
    requestBody: freezeJson(requestBody),
    token: validated.token,
    expiresInSeconds: validated.expiresInSeconds,
    expiresAt,
    eligibleIds: validated.items.filter(item =>
      scope.marketplace === 'wb' ? item.status === 'changed' : item.status === 'eligible'
    ).map(item => scope.marketplace === 'wb' ? item.productId : item.listingId),
  });
}

function previewContextIsValid(preview) {
  return object(preview) && ['wb', 'ozon'].includes(preview.marketplace) &&
    Array.isArray(preview.productIds) && preview.productIds.length > 0 &&
    preview.productIds.every(safeId) && validToken(preview.token, preview.marketplace) &&
    object(preview.summary) &&
    Array.isArray(preview.eligibleIds) &&
    preview.eligibleIds.every(safeId) && Number.isSafeInteger(preview.expiresAt);
}

function previewIsValid(preview) {
  return previewContextIsValid(preview) && object(preview.requestBody);
}

export function buildLocalSaveBody(preview, metadata = {}) {
  if (!previewIsValid(preview) || !object(metadata)) return null;
  if (preview.marketplace === 'wb') {
    const name = metadata.name ?? '';
    const description = metadata.description ?? '';
    if (!boundedText(name, 200) || !boundedText(description, 2000)) return null;
    return freezeDeep({
      ...freezeJson(preview.requestBody),
      review_token: preview.token,
      name: name.trim(),
      description: description.trim(),
    });
  }
  return freezeDeep({ preview_token: preview.token, confirm_save: true });
}

function sameOriginUrl(value, origin) {
  if (typeof value !== 'string' || !value || value.length > 9000 || !origin) return null;
  try {
    const base = new URL(origin);
    const target = new URL(value, base.origin);
    if (!['http:', 'https:'].includes(target.protocol) || target.origin !== base.origin ||
        target.username || target.password || target.hash) return null;
    return target;
  } catch (_) {
    return null;
  }
}

function validateWbRedirect(value, batchId, status, origin) {
  const target = sameOriginUrl(value, origin);
  if (!target || target.search || target.pathname !== `/prices/batch/${batchId}` &&
      target.pathname !== `/prices/batch/${batchId}/confirm`) return null;
  const isConfirm = target.pathname.endsWith('/confirm');
  if ((status === 'pending_review') !== isConfirm) return null;
  return target.pathname;
}

function reviewListUrl(value, { accountId, proposalIds }, origin) {
  const target = sameOriginUrl(value, origin);
  if (!target || target.pathname !== '/marketplaces/commercial/' || target.hash) return null;
  const params = target.searchParams;
  const expected = {
    account_id: String(accountId),
    proposal_kind: 'price',
    status: 'pending_review',
    proposal_ids: proposalIds.join(','),
  };
  if (Array.from(params.keys()).length !== 4) return null;
  for (const [key, expectedValue] of Object.entries(expected)) {
    if (params.getAll(key).length !== 1 || params.get(key) !== expectedValue) return null;
  }
  return target.pathname + target.search;
}

export function validateLocalSaveReceipt(preview, payload, { origin } = {}) {
  if (!previewContextIsValid(preview) || !object(payload) || payload.success !== true) return null;
  if (preview.marketplace === 'wb') {
    const batchId = payload.batch_id;
    const status = payload.status;
    if (payload.reviewed_write !== true || payload.reviewed_selection !== 'wb_reviewed_selection_v1' ||
        !safeId(batchId) || !boundedText(status, 40, { allowLines: false, empty: false }) ||
        typeof payload.replayed !== 'boolean' ||
        payload.selected_count !== preview.summary.selectedCount ||
        payload.changed_count !== preview.summary.changedCount || !object(payload.stats) ||
        payload.stats.selected_count !== payload.selected_count ||
        payload.stats.changed_count !== payload.changed_count) return null;
    const stats = wbReceiptStats(payload.stats);
    if (!stats || stats.selectedCount !== payload.selected_count ||
        stats.changedCount !== payload.changed_count) return null;
    const redirect = validateWbRedirect(payload.redirect, batchId, status, origin);
    if (!redirect) return null;
    return freezeDeep({
      marketplace: 'wb', batchId, status,
      selectedCount: payload.selected_count,
      changedCount: payload.changed_count,
      replayed: payload.replayed,
      redirect,
      destination: {
        href: redirect,
        label: status === 'pending_review' ? 'Проверить изменения' : 'Открыть пакет цен',
      },
      stats,
    });
  }

  const eligibleIds = preview.eligibleIds;
  if (payload.selected_count !== preview.summary.selectedCount ||
      payload.saved_count !== eligibleIds.length || !nonNegativeCount(payload.created_count) ||
      !nonNegativeCount(payload.replayed_count) ||
      payload.created_count + payload.replayed_count !== payload.saved_count ||
      payload.replayed !== (payload.replayed_count > 0 && payload.created_count === 0) ||
      payload.noop_count !== preview.summary.noOpCount ||
      payload.blocked_count !== preview.summary.blockedCount ||
      !Array.isArray(payload.proposal_ids) || !Array.isArray(payload.items) ||
      payload.items.length !== eligibleIds.length || payload.proposal_ids.length !== eligibleIds.length) return null;
  const proposals = [];
  for (let index = 0; index < eligibleIds.length; index += 1) {
    const item = payload.items[index];
    const proposalId = payload.proposal_ids[index];
    const proposal = item && item.proposal;
    if (!object(item) || item.listing_id !== eligibleIds[index] || !safeId(proposalId) ||
        !object(proposal) || proposal.id !== proposalId || proposal.listing_id !== eligibleIds[index] ||
        proposal.account_id !== preview.accountId || proposal.proposal_kind !== 'price' ||
        !boundedText(proposal.status, 40, { allowLines: false, empty: false }) ||
        item.status !== proposal.status) return null;
    proposals.push({ id: proposalId, listingId: eligibleIds[index], status: proposal.status });
  }
  const reviewUrl = payload.review_url === null || payload.review_url === undefined
    ? null
    : reviewListUrl(payload.review_url, { accountId: preview.accountId, proposalIds: payload.proposal_ids }, origin);
  if (payload.review_url !== null && payload.review_url !== undefined && !reviewUrl) return null;
  const allPendingReview = proposals.length > 0 && proposals.every(item => item.status === 'pending_review');
  if (allPendingReview && !reviewUrl) return null;
  if (payload.items.some(item =>
    item.review_url !== null && item.review_url !== undefined && item.review_url !== payload.review_url
  )) return null;
  return freezeDeep({
    marketplace: 'ozon',
    accountId: preview.accountId,
    selectedCount: payload.selected_count,
    savedCount: payload.saved_count,
    createdCount: payload.created_count,
    replayedCount: payload.replayed_count,
    replayed: payload.replayed,
    proposalIds: [...payload.proposal_ids],
    reviewUrl,
    destination: reviewUrl ? { href: reviewUrl, label: 'Проверить предложения' } : null,
    items: proposals,
  });
}

function endpoint(url, origin) {
  const resolved = sameOriginUrl(url, origin);
  if (!resolved || resolved.search || resolved.hash ||
      !resolved.pathname.startsWith('/') || resolved.pathname.startsWith('//')) return null;
  return resolved.href;
}

function keyFor(scope, owner) {
  return `seller-hub-price-local-save-v1:${encodeURIComponent(owner)}:${scope.marketplace}:${scope.accountId ?? 0}:${scope.productIds.join(',')}`;
}

function storedContext(preview) {
  return {
    marketplace: preview.marketplace,
    accountId: preview.accountId,
    productIds: [...preview.productIds],
    summary: freezeJson(preview.summary),
    eligibleIds: [...preview.eligibleIds],
    token: preview.token,
    expiresAt: preview.expiresAt,
  };
}

function parsePendingRecord(raw, scope, owner, now) {
  if (typeof raw !== 'string' || raw.length > MAX_PENDING_RECORD_CHARS) return null;
  let record;
  try {
    record = JSON.parse(raw);
  } catch (_) {
    return null;
  }
  if (!object(record) || record.v !== 1 || record.owner !== owner ||
      record.marketplace !== scope.marketplace || record.accountId !== scope.accountId ||
      !['sending', 'unknown'].includes(record.state) ||
      !Number.isSafeInteger(record.attemptedAt) || record.attemptedAt <= 0 ||
      typeof record.bodyText !== 'string' ||
      record.bodyText.length > MAX_PENDING_BODY_CHARS || !object(record.context) ||
      record.context.marketplace !== scope.marketplace || record.context.accountId !== scope.accountId ||
      !sameIds(record.context.productIds, scope.productIds) ||
      !safeIdArray(record.context.eligibleIds) ||
      !validToken(record.context.token, scope.marketplace) ||
      !Number.isSafeInteger(record.context.expiresAt) || !object(record.context.summary)) return null;
  let body;
  try {
    body = JSON.parse(record.bodyText);
  } catch (_) {
    return null;
  }
  if (!object(body)) return null;
  if (scope.marketplace === 'wb') {
    if (body.reviewed_write !== true || body.exact_selection !== true ||
        !sameIds(body.product_ids, scope.productIds) || body.review_token !== record.context.token) return null;
  } else if (Object.keys(body).length !== 2 || body.preview_token !== record.context.token || body.confirm_save !== true) {
    return null;
  }
  const preview = freezeDeep({
    ...record.context,
    accountLabel: scope.accountLabel,
    rows: scope.rows,
    items: [],
    canSave: true,
    guardrailBlocked: false,
  });
  const retryAvailable = scope.marketplace === 'wb' || now < record.context.expiresAt;
  return {
    key: keyFor(scope, owner),
    record,
    preview,
    descriptor: freezeDeep({
      state: 'unknown',
      retryAvailable,
      expired: !retryAvailable,
      expiresAt: record.context.expiresAt,
      attemptedAt: record.attemptedAt,
      marketplace: record.marketplace,
      accountLabel: scope.accountLabel,
      selectedCount: scope.productIds.length,
    }),
  };
}

function safeIdArray(values) {
  return Array.isArray(values) && values.every(safeId) && new Set(values).size === values.length;
}

function saveError(code, message, status) {
  return new PriceWorkspaceError(code, message, { status, outcomeUnknown: true });
}

export function createPriceWorkspaceAdapter({
  urls,
  owner,
  origin = globalThis.location?.origin,
  fetchImpl = globalThis.fetch?.bind(globalThis),
  sessionStorage = browserSessionStorage(),
  now = () => Date.now(),
} = {}) {
  if (!object(urls) || typeof owner !== 'string' || !OWNER.test(owner) ||
      typeof origin !== 'string' || !sameOriginUrl('/', origin) ||
      typeof fetchImpl !== 'function' || typeof now !== 'function') {
    throw new PriceWorkspaceError('adapter_configuration_invalid', 'Не удалось настроить изменение цены.');
  }

  const originUrl = new URL(origin).origin;

  async function postJson(urlKey, bodyText, { saveOperation = false } = {}) {
    const requestUrl = endpoint(urls[urlKey], originUrl);
    if (!requestUrl) throw new PriceWorkspaceError('endpoint_invalid', 'Адрес операции недоступен.');
    let response;
    try {
      response = await fetchImpl(requestUrl, {
        method: 'POST',
        credentials: 'same-origin',
        redirect: 'error',
        headers: { 'Content-Type': 'application/json' },
        body: bodyText,
      });
    } catch (_) {
      throw new PriceWorkspaceError(
        'transport_failed',
        'Ответ от сервера не получен.',
        { outcomeUnknown: saveOperation },
      );
    }
    let payload = null;
    try {
      payload = await response.json();
    } catch (_) {
      if (response.ok || response.status >= 500) {
        if (saveOperation) {
          throw saveError('response_unreadable', 'Не удалось прочитать ответ сервера.', response.status);
        }
        throw new PriceWorkspaceError('response_unreadable',
          'Не удалось прочитать ответ сравнения.', { status: response.status });
      }
    }
    return { response, payload };
  }

  function scopeOrThrow(input) {
    const scope = normalizePriceWorkspaceInput(input, { origin: originUrl });
    if (!scope) throw new PriceWorkspaceError('scope_invalid', 'Выбор товаров или магазина изменился.');
    return scope;
  }

  function putPending(key, record) {
    if (!sessionStorage) {
      throw new PriceWorkspaceError('pending_storage_unavailable',
        'Не удалось сохранить безопасный повтор запроса.');
    }
    try {
      sessionStorage.setItem(key, JSON.stringify(record));
    } catch (_) {
      throw new PriceWorkspaceError('pending_storage_unavailable',
        'Не удалось сохранить безопасный повтор запроса.');
    }
  }

  function removePending(key) {
    if (!sessionStorage) {
      throw new PriceWorkspaceError('pending_storage_unavailable',
        'Не удалось подтвердить локальный результат.');
    }
    try {
      sessionStorage.removeItem(key);
    } catch (_) {
      throw new PriceWorkspaceError('pending_storage_unavailable',
        'Не удалось подтвердить локальный результат.');
    }
  }

  function readPending(scope) {
    if (!sessionStorage) {
      throw new PriceWorkspaceError('pending_storage_unavailable', 'Не удалось проверить сохранение.');
    }
    const key = keyFor(scope, owner);
    let raw;
    try {
      raw = sessionStorage.getItem(key);
    } catch (_) {
      throw new PriceWorkspaceError('pending_storage_unavailable', 'Не удалось проверить сохранение.');
    }
    if (raw === null) return null;
    const parsed = parsePendingRecord(raw, scope, owner, now());
    if (!parsed) throw new PriceWorkspaceError('pending_record_invalid',
      'Состояние сохранения нельзя безопасно восстановить. Проверьте предложения вручную.');
    return parsed;
  }

  async function sendPending(scope, pending, { replay = false } = {}) {
    const record = { ...pending.record, state: 'sending', attemptedAt: now() };
    putPending(pending.key, record);
    let result;
    try {
      result = await postJson(
        scope.marketplace === 'wb' ? 'wbSave' : 'ozonSave',
        record.bodyText,
        { saveOperation: true },
      );
    } catch (error) {
      putPending(pending.key, { ...record, state: 'unknown' });
      if (error instanceof PriceWorkspaceError) {
        error.outcomeUnknown = true;
        throw error;
      }
      throw saveError('save_outcome_unknown', 'Результат сохранения пока неизвестен.');
    }
    const { response, payload } = result;
    if (!response.ok) {
      const typedNoCommit = !replay && response.status >= 400 && response.status < 500 &&
        object(payload) && payload.success === false &&
        boundedText(payload.code, 100, { allowLines: false, empty: false });
      if (!typedNoCommit) {
        putPending(pending.key, { ...record, state: 'unknown' });
        throw saveError('save_outcome_unknown', 'Результат сохранения пока неизвестен.', response.status);
      }
      removePending(pending.key);
      throw new PriceWorkspaceError(
        payload && boundedText(payload.error, 500) ? 'save_rejected' : 'save_http_error',
        payload && boundedText(payload.error, 500) ? payload.error : 'Сохранение не выполнено.',
        { status: response.status },
      );
    }
    const receipt = validateLocalSaveReceipt(pending.preview, payload, { origin: originUrl });
    if (!receipt) {
      putPending(pending.key, { ...record, state: 'unknown' });
      throw saveError('save_receipt_invalid', 'Результат сохранения не удалось подтвердить.', response.status);
    }
    removePending(pending.key);
    return receipt;
  }

  return Object.freeze({
    async preview(input, nativeInputs) {
      const scope = scopeOrThrow(input);
      const body = buildPricePreviewRequest(scope, nativeInputs);
      if (!body) throw new PriceWorkspaceError('preview_input_invalid', 'Проверьте параметры цены.');
      const urlKey = scope.marketplace === 'wb' ? 'wbPreview' : 'ozonPreview';
      const response = await postJson(urlKey, JSON.stringify(body));
      if (!response.response.ok) {
        const error = response.payload && boundedText(response.payload.error, 500)
          ? response.payload.error : 'Не удалось сравнить выбранные цены.';
        throw new PriceWorkspaceError('preview_rejected', error, { status: response.response.status });
      }
      const preview = validatePricePreviewResponse(scope, body, response.payload, { origin: originUrl, now: now() });
      if (!preview) throw new PriceWorkspaceError('preview_response_invalid', 'Сравнение вернуло неполный список товаров.');
      return preview;
    },

    async save(preview, metadata = {}) {
      if (!previewIsValid(preview) || !Array.isArray(preview.rows) ||
          preview.rows.length !== preview.productIds.length) {
        throw new PriceWorkspaceError('preview_required', 'Сначала сравните цены для этого выбора.');
      }
      const scope = scopeOrThrow({
        marketplace: preview.marketplace,
        productIds: preview.productIds,
        accountId: preview.accountId,
        accountLabel: preview.accountLabel,
        rows: preview.rows,
      });
      if (!preview.canSave) throw new PriceWorkspaceError('preview_not_savable', 'В сравнении нет изменений для сохранения.');
      const key = keyFor(scope, owner);
      if (readPending(scope)) throw new PriceWorkspaceError('pending_save_exists', 'Сначала проверьте прошлое сохранение.');
      const body = buildLocalSaveBody(preview, metadata);
      if (!body) throw new PriceWorkspaceError('save_body_invalid', 'Не удалось подготовить точное сохранение.');
      const bodyText = JSON.stringify(body);
      const context = storedContext(preview);
      const pending = {
        key,
        preview: freezeDeep({ ...context, accountLabel: scope.accountLabel, rows: scope.rows, items: [], canSave: true }),
        record: {
          v: 1,
          owner,
          marketplace: scope.marketplace,
          accountId: scope.accountId,
          productIds: [...scope.productIds],
          context,
          attemptedAt: now(),
          state: 'sending',
          bodyText,
        },
      };
      putPending(key, pending.record);
      return sendPending(scope, pending);
    },

    recover(input) {
      const scope = scopeOrThrow(input);
      const pending = readPending(scope);
      return pending ? pending.descriptor : null;
    },

    async retryPending(input) {
      const scope = scopeOrThrow(input);
      const pending = readPending(scope);
      if (!pending) throw new PriceWorkspaceError('pending_save_missing', 'Сохранение для этого выбора не найдено.');
      if (!pending.descriptor.retryAvailable) {
        throw new PriceWorkspaceError('pending_preview_expired',
          'Срок сравнения истёк. Сначала проверьте сохранённые предложения, затем сравните цены снова.');
      }
      return sendPending(scope, pending, { replay: true });
    },
  });
}
