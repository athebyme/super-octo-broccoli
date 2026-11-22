import test from 'node:test';
import assert from 'node:assert/strict';
import {
  canonicalBatchLocation,
  orderedUniqueIds,
  validateFilterSelection,
  validateImportResult,
  validateSupplierPage,
  isWorkspaceOwner,
  validPendingImport,
} from '../src/import-contract.js';

function page(overrides = {}) {
  return {
    success: true,
    supplier: { id: '7', name: 'Каталог' },
    items: [{ id: '9007199254740993', title: 'Товар', article: 'A-1',
      photo_url: null, purchase_price: '12.50', currency: 'RUB', stock: 3,
      importable: true, imported: false, on_wb: false }],
    page: 1,
    pages: 1,
    total: 1,
    eligible_count: 1,
    selection: ['9007199254740993'],
    selection_block_reason: null,
    selection_block_total: null,
    ...overrides,
  };
}

test('keeps long database ids as canonical decimal strings in ordered sets', () => {
  assert.deepEqual(orderedUniqueIds(['9007199254740993', '2']), ['9007199254740993', '2']);
  assert.equal(orderedUniqueIds(['02']), null);
  assert.equal(orderedUniqueIds(['2', '2']), null);
  assert.equal(orderedUniqueIds([2]), null);
});

test('accepts only the requested exact supplier page and unique rows', () => {
  assert.deepEqual(validateSupplierPage(page(), '7', 1)?.ids, ['9007199254740993']);
  assert.equal(validateSupplierPage(page(), '8', 1), null);
  assert.equal(validateSupplierPage(page({ page: 2 }), '7', 1), null);
  assert.equal(validateSupplierPage(page({ items: [{ ...page().items[0] }, { ...page().items[0] }] }), '7', 1), null);
});

test('accepts a bounded all-filter selection only from its exact supplier response', () => {
  assert.deepEqual(validateFilterSelection(page(), '7')?.selection, ['9007199254740993']);
  assert.deepEqual(validateFilterSelection(page({ selection_block_reason: 'Больше лимита' }), '7'),
    { selection: null, reason: 'Больше лимита' });
  assert.equal(validateFilterSelection(page({ selection: Array.from({ length: 201 }, (_, i) => String(i + 1)) }), '7'), null);
  const ids = Array.from({ length: 200 }, (_, i) => String(i + 1));
  const rows = ids.map(id => ({ ...page().items[0], id }));
  assert.equal(validateFilterSelection(page({ items: rows, selection: ids, total: 200,
    eligible_count: 200, selection_block_total: 200 }), '7')?.selection.length, 200);
  assert.ok(validateSupplierPage(page({
    pages: 0, total: 0, items: [], selection: [], eligible_count: 0,
  }), '7', 1));
  assert.ok(validateSupplierPage(page({
    items: [{ id: '1', title: 'Название\r\nна двух строках', purchase_price: null,
      currency: null, stock: null, importable: true, imported: false, on_wb: false }],
  }), '7', 1));
  assert.equal(validateSupplierPage(page({ supplier: { id: '9223372036854775808', name: 'x' } }), '7', 1), null);
});

test('requires a signed batch only for a positive imported count', () => {
  const imported = validateImportResult({ success: true, supplier_id: '7', requested: 1, imported: 1, skipped: 0, errors: 0,
    drafts_status: 'complete', drafts_created: 0, draft_errors: 0, import_batch: 'a.b.c',
    batch_url: '/my-products/beta?import_batch=a.b.c' }, ['1'], '7');
  assert.equal(imported?.import_batch, 'a.b.c');
  assert.equal(validateImportResult({ success: true, supplier_id: '7', requested: 1, imported: 1, skipped: 0, errors: 0,
    drafts_status: 'complete', drafts_created: 0, draft_errors: 0,
    import_batch: null, batch_url: null }, ['1'], '7'), null);
  assert.equal(validateImportResult({ success: true, supplier_id: '7', requested: 1, imported: 0, skipped: 1, errors: 0,
    drafts_status: 'pending', drafts_created: null, draft_errors: null,
    import_batch: null, batch_url: null }, ['1'], '7')?.imported, 0);
  assert.equal(validateImportResult({ success: true, supplier_id: '8', requested: 2, imported: 1, skipped: 0, errors: 0,
    drafts_status: 'complete', drafts_created: 0, draft_errors: 0,
    import_batch: 'a.b.c', batch_url: '/my-products/beta?import_batch=a.b.c' }, ['1'], '7'), null);
});

test('rejects cross-origin, over-scoped, duplicate-token, and wrong-path batch URLs', () => {
  const origin = 'https://seller.test';
  const workspace = '/my-products/beta';
  assert.equal(canonicalBatchLocation('/my-products/beta?import_batch=a.b.c', 'a.b.c', workspace, origin),
    '/my-products/beta?import_batch=a.b.c');
  assert.equal(canonicalBatchLocation('https://other.test/my-products/beta?import_batch=a.b.c',
    'a.b.c', workspace, origin), null);
  assert.equal(canonicalBatchLocation('/my-products/beta?import_batch=a.b.c&x=1',
    'a.b.c', workspace, origin), null);
  assert.equal(canonicalBatchLocation('/my-products/beta?import_batch=a.b.c&import_batch=a.b.c',
    'a.b.c', workspace, origin), null);
  assert.equal(canonicalBatchLocation('/supplier-catalog?import_batch=a.b.c',
    'a.b.c', workspace, origin), null);
});

test('pending import is owner-bound and exact', () => {
  const value = { v: 1, owner: '4:9', supplier_id: '7', product_ids: ['3', '2'],
    request_key: 'a'.repeat(32), created_at: 1 };
  assert.equal(validPendingImport(value, '4:9'), true);
  assert.equal(validPendingImport(value, '4:8'), false);
  assert.equal(validPendingImport({ ...value, product_ids: ['3', '3'] }, '4:9'), false);
  assert.equal(isWorkspaceOwner('4:9'), true);
  assert.equal(isWorkspaceOwner('4:0'), false);
  assert.equal(validPendingImport(value, ''), false);
  assert.equal(validPendingImport({ ...value, supplier_id: 7 }, '4:9'), false);
});
