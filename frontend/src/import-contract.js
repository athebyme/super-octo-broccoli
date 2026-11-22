export const MAX_IMPORT_SELECTION = 200;
export const MAX_SIGNED_TOKEN_CHARS = 8192;

const PRODUCT_ID = /^[1-9][0-9]{0,18}$/;
const MAX_SQLITE_ID = '9223372036854775807';
const SIGNED_TOKEN = /^[A-Za-z0-9_.-]+$/;

export function isProductId(value) {
  return typeof value === 'string' && PRODUCT_ID.test(value) &&
    (value.length < MAX_SQLITE_ID.length ||
      (value.length === MAX_SQLITE_ID.length && value <= MAX_SQLITE_ID));
}

export function isWorkspaceOwner(value) {
  if (typeof value !== 'string') return false;
  const parts = value.split(':');
  return parts.length === 2 && parts.every(isProductId);
}

export function orderedUniqueIds(values) {
  if (!Array.isArray(values)) return null;
  const result = [];
  const seen = new Set();
  for (const value of values) {
    const id = value && typeof value === 'object' && !Array.isArray(value)
      ? value.id
      : value;
    if (!isProductId(id) || seen.has(id)) return null;
    seen.add(id);
    result.push(id);
  }
  return result;
}

function nonNegativeInteger(value) {
  return Number.isSafeInteger(value) && value >= 0;
}

function boundedString(value, max = 500) {
  return typeof value === 'string' && value.length <= max &&
    !/[\u0000-\u001f\u007f]/.test(value);
}

function boundedDisplayString(value, max = 5000) {
  return typeof value === 'string' && value.length <= max &&
    !/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/.test(value);
}

export function validateSupplierPage(data, supplierId, requestedPage) {
  const emptyPage = data && data.page === 1 && data.pages === 0 && data.total === 0 &&
    Array.isArray(data.items) && data.items.length === 0;
  if (!data || typeof data !== 'object' || Array.isArray(data) ||
      data.success !== true || !data.supplier ||
      !isProductId(String(supplierId)) || !isProductId(data.supplier.id) ||
      data.supplier.id !== String(supplierId) ||
      !Array.isArray(data.items) || data.items.length > MAX_IMPORT_SELECTION ||
      !Number.isSafeInteger(data.page) || data.page !== requestedPage ||
      !Number.isSafeInteger(data.pages) || data.pages < 0 ||
      (!emptyPage && (data.pages < 1 || data.page > data.pages)) ||
      !Number.isSafeInteger(data.total) || data.total < data.items.length ||
      !boundedString(data.supplier.name, 200)) return null;

  const ids = orderedUniqueIds(data.items);
  if (!ids) return null;
  for (const item of data.items) {
    if (!(item.title === null || item.title === undefined || boundedDisplayString(item.title, 5000)) ||
        !(item.article === null || item.article === undefined || boundedDisplayString(item.article, 500)) ||
        !(item.photo_url === null || item.photo_url === undefined || boundedString(item.photo_url, 2000)) ||
        !(item.currency === null || item.currency === undefined || boundedString(item.currency, 12)) ||
        !(item.purchase_price === null || item.purchase_price === undefined ||
          (typeof item.purchase_price === 'string' && item.purchase_price.length <= 100) ||
          (typeof item.purchase_price === 'number' && Number.isFinite(item.purchase_price))) ||
        !(item.stock === null || item.stock === undefined || Number.isFinite(item.stock)) ||
        typeof item.importable !== 'boolean' || typeof item.imported !== 'boolean' ||
        typeof item.on_wb !== 'boolean') return null;
  }
  if (data.selection_block_reason !== undefined &&
      data.selection_block_reason !== null && !boundedString(data.selection_block_reason, 500)) return null;
  if (data.mode !== undefined && data.mode !== 'import') return null;
  if (data.eligible_count !== undefined &&
      (!nonNegativeInteger(data.eligible_count) || data.eligible_count > data.items.length)) return null;
  if (data.selection_block_total !== undefined && data.selection_block_total !== null &&
      !nonNegativeInteger(data.selection_block_total)) return null;
  if (data.draft_account_choices !== undefined &&
      (!Array.isArray(data.draft_account_choices) || data.draft_account_choices.length > 100)) return null;
  return { ids, items: data.items };
}

export function validateFilterSelection(data, supplierId) {
  const page = validateSupplierPage(data, supplierId, 1);
  if (!page || !Array.isArray(data.selection)) return null;
  const selection = orderedUniqueIds(data.selection);
  if (!selection || selection.length > MAX_IMPORT_SELECTION) return null;
  if (data.selection_block_reason) return { selection: null, reason: data.selection_block_reason };
  if (Number.isSafeInteger(data.eligible_count) &&
      data.eligible_count !== selection.length) return null;
  return { selection };
}

export function validateImportResult(data, requestedIds, supplierId) {
  const requested = orderedUniqueIds(requestedIds);
  if (!requested || !requested.length || requested.length > MAX_IMPORT_SELECTION ||
      !isProductId(supplierId) ||
      !data || typeof data !== 'object' || Array.isArray(data) || data.success !== true ||
      data.supplier_id !== supplierId ||
      !nonNegativeInteger(data.requested) || data.requested !== requested.length ||
      !nonNegativeInteger(data.imported) || data.imported > requested.length ||
      !nonNegativeInteger(data.skipped) || !nonNegativeInteger(data.errors) ||
      data.imported + data.skipped + data.errors !== requested.length ||
      !['pending', 'complete'].includes(data.drafts_status) ||
      (data.drafts_status === 'complete' &&
        (!nonNegativeInteger(data.drafts_created) || !nonNegativeInteger(data.draft_errors))) ||
      (data.drafts_status === 'pending' &&
        (data.drafts_created !== null || data.draft_errors !== null))) {
    return null;
  }
  const token = data.import_batch;
  const batchUrl = data.batch_url;
  if (data.imported === 0) {
    if (token !== null || batchUrl !== null) return null;
  } else if (typeof token !== 'string' || !token || token.length > MAX_SIGNED_TOKEN_CHARS ||
      !SIGNED_TOKEN.test(token) || typeof batchUrl !== 'string' || batchUrl.length > 9000) {
    return null;
  }
  return {
    supplier_id: data.supplier_id,
    imported: data.imported,
    requested: data.requested,
    skipped: data.skipped,
    errors: data.errors,
    drafts_status: data.drafts_status,
    drafts_created: data.drafts_created,
    draft_errors: data.draft_errors,
    import_batch: token,
    batch_url: batchUrl,
  };
}

export function canonicalBatchLocation(batchUrl, token, workspaceUrl, origin) {
  if (typeof batchUrl !== 'string' || !batchUrl || batchUrl.length > 9000 ||
      typeof token !== 'string' || !token || !SIGNED_TOKEN.test(token) ||
      token.length > MAX_SIGNED_TOKEN_CHARS || typeof workspaceUrl !== 'string' || !origin) return null;
  try {
    const workspace = new URL(workspaceUrl, origin);
    const target = new URL(batchUrl, origin);
    const params = target.searchParams;
    const keys = Array.from(params.keys());
    if (workspace.origin !== origin || target.origin !== origin ||
        workspace.username || workspace.password || workspace.hash ||
        target.username || target.password || target.hash ||
        target.pathname !== workspace.pathname ||
        keys.length !== 1 || keys[0] !== 'import_batch' ||
        params.getAll('import_batch').length !== 1 || params.get('import_batch') !== token) return null;
    return target.pathname + target.search;
  } catch (_) {
    return null;
  }
}

export function validPendingImport(value, owner) {
  if (!value || typeof value !== 'object' || Array.isArray(value) ||
      value.v !== 1 || !isWorkspaceOwner(owner) || value.owner !== owner ||
      !isProductId(value.supplier_id) ||
      typeof value.request_key !== 'string' || !/^[a-f0-9]{32}$/.test(value.request_key)) return false;
  const ids = orderedUniqueIds(value.product_ids);
  return !!ids && ids.length > 0 && ids.length <= MAX_IMPORT_SELECTION &&
    Number.isSafeInteger(value.created_at) && value.created_at > 0;
}
