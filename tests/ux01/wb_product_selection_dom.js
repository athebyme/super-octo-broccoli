/* Minimal DOM regression for the exact-selection controller embedded in products.html. */
const fs = require('fs');
const vm = require('vm');
const path = require('path');
const assert = require('assert/strict');

const templatePath = path.join(__dirname, '..', '..', 'templates', 'products.html');
const template = fs.readFileSync(templatePath, 'utf8');
const start = template.indexOf('const selectionConfig =');
const stop = template.indexOf('function bulkEnrich()', start);
assert(start >= 0 && stop > start, 'selection script boundaries must exist');
const controllerSource = template.slice(start, stop);

class MemoryStorage {
  constructor(entries = []) { this.values = new Map(entries); }
  get length() { return this.values.size; }
  getItem(key) { return this.values.has(key) ? this.values.get(key) : null; }
  setItem(key, value) { this.values.set(String(key), String(value)); }
  removeItem(key) { this.values.delete(key); }
  key(index) { return [...this.values.keys()][index] ?? null; }
}

function checkbox(id) {
  return {
    value: String(id), checked: false, indeterminate: false,
    matches: selector => selector === '.product-checkbox',
  };
}

function mount({ids, storage = new MemoryStorage(), resolveIds = [101, 102, 103]}) {
  const boxes = ids.map(checkbox);
  const hiddenInputs = [];
  const elements = {
    'selection-config': {textContent: JSON.stringify({
      sellerId: 9, marketplace: 'wb', wbAccountId: 'wb-synthetic',
      selectionLimit: 200, filterFingerprint: 'filter-a', filters: {brand: 'Pipedream'},
      sort: 'title', order: 'asc', page: 1, per_page: 25,
      returnTo: '/products?brand=Pipedream&page=1', resolveUrl: '/resolve',
      bulkEditUrl: '/bulk-edit', filteredCount: resolveIds.length,
    })},
    selectionStatus: {textContent: ''},
    selectedCount: {textContent: '0'},
    bulkActionBar: {hidden: true, classList: {toggle(_name, hidden) { this.owner.hidden = hidden; }}},
    bulkActionForm: {
      querySelectorAll(selector) {
        assert.equal(selector, 'input[name="product_ids"]');
        return [...hiddenInputs];
      },
      appendChild(input) { hiddenInputs.push(input); return input; },
    },
    selectAll: {checked: false, indeterminate: false},
    selectFilteredButton: {disabled: false},
  };
  elements.bulkActionBar.classList.owner = elements.bulkActionBar;
  const document = {
    getElementById(id) { return elements[id] || null; },
    querySelectorAll(selector) {
      assert.equal(selector, '.product-checkbox');
      return boxes;
    },
    createElement(tag) {
      assert.equal(tag, 'input');
      return {type: '', name: '', value: '', remove() {
        const index = hiddenInputs.indexOf(this);
        if (index >= 0) hiddenInputs.splice(index, 1);
      }};
    },
    querySelector() { return null; },
  };
  const context = {
    document,
    sessionStorage: storage,
    fetch: async () => ({
      ok: true,
      json: async () => ({ids: resolveIds, selection_token: 'synthetic-token'}),
    }),
    console,
    confirm: () => true,
    Number, Set, Array, JSON, Error, Promise,
  };
  vm.createContext(context);
  vm.runInContext(`${controllerSource}\nthis.controller = {
    initializeSelection, ingestSelectionFromPage, toggleSelectAll, clearSelection, selectAllFiltered,
  };`, context, {filename: 'products.html selection controller'});
  context.controller.initializeSelection();
  return {context, boxes, elements, hiddenInputs, storage};
}

(async () => {
  const firstPage = mount({ids: [11, 12]});
  firstPage.boxes[0].checked = true;
  firstPage.context.controller.ingestSelectionFromPage(firstPage.boxes[0]);
  assert.equal(firstPage.elements.selectedCount.textContent, 1);
  const secondPage = mount({ids: [13], storage: firstPage.storage});
  assert.equal(secondPage.elements.selectedCount.textContent, 1, `exact ID survives page navigation: ${JSON.stringify([...firstPage.storage.values])}`);
  secondPage.boxes[0].checked = true;
  secondPage.context.controller.ingestSelectionFromPage(secondPage.boxes[0]);
  assert.equal(secondPage.elements.selectedCount.textContent, 2, 'manual selection merges exact IDs across pages');

  const allFiltered = mount({ids: [21, 22], resolveIds: [21, 22, 23]});
  await allFiltered.context.controller.selectAllFiltered();
  assert.equal(allFiltered.elements.selectedCount.textContent, 3, 'server-resolved filtered IDs are retained');
  assert.deepEqual(allFiltered.boxes.map(box => box.checked), [true, true]);
  assert.equal(allFiltered.hiddenInputs.length, 3);

  const pageToggle = mount({ids: [31, 32]});
  pageToggle.context.controller.toggleSelectAll({checked: true});
  assert.equal(pageToggle.elements.selectedCount.textContent, 2, 'page select-all enters IDs once');
  pageToggle.context.controller.clearSelection();
  assert.equal(pageToggle.elements.selectedCount.textContent, 0, 'clear empties the selected set');
  assert.deepEqual(pageToggle.boxes.map(box => box.checked), [false, false], 'clear synchronizes rendered checkboxes');
  assert.equal(pageToggle.hiddenInputs.length, 0, 'clear removes stale hidden IDs');

  process.stdout.write('WB product selection DOM regressions passed\n');
})().catch(error => {
  process.stderr.write(String(error.stack || error) + '\n');
  process.exitCode = 1;
});
