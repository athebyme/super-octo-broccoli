<script>
import {
  MAX_IMPORT_SELECTION,
  canonicalBatchLocation,
  isProductId,
  isWorkspaceOwner,
  orderedUniqueIds,
  validateFilterSelection,
  validateImportResult,
  validateSupplierPage,
  validPendingImport,
} from './import-contract.js';

const PENDING_KEY = 'seller-hub-supplier-import-pending-v1';

function requestKey() {
  if (!globalThis.crypto?.getRandomValues) return '';
  try {
    if (typeof globalThis.crypto.randomUUID === 'function') {
      return globalThis.crypto.randomUUID().replace(/-/g, '').toLowerCase();
    }
    return Array.from(globalThis.crypto.getRandomValues(new Uint8Array(16)), byte =>
      byte.toString(16).padStart(2, '0')).join('');
  } catch (_) { return ''; }
}

function responseMessage(data, fallback) {
  return data && typeof data.error === 'string'
    ? data.error.slice(0, 500)
    : fallback;
}

export default {
  name: 'SupplierImportPanel',
  props: {
    open: { type: Boolean, default: false },
    suppliers: { type: Array, default: () => [] },
    owner: { type: String, required: true },
    urls: { type: Object, required: true },
  },
  emits: ['close', 'open', 'import-success'],
  data() {
    return {
      supplierId: '',
      page: 1,
      search: '',
      stockStatus: 'all',
      source: null,
      selectedIds: [],
      loading: false,
      loadingAll: false,
      submitting: false,
      readingPending: false,
      retryAllowed: false,
      sessionEnded: false,
      error: '',
      status: '',
      storageProblem: false,
      pending: null,
      requestRevision: 0,
      requestController: null,
      requestTimer: null,
      searchTimer: null,
      closeTimer: null,
    };
  },
  computed: {
    availableSuppliers() {
      return (Array.isArray(this.suppliers) ? this.suppliers : []).filter(row =>
        row && isProductId(String(row.id)) && typeof row.name === 'string' &&
        typeof row.catalog_url === 'string' && row.catalog_url.length <= 2000);
    },
    selectedSupplier() {
      return this.availableSuppliers.find(row => String(row.id) === this.supplierId) || null;
    },
    items() {
      return this.source && Array.isArray(this.source.items) ? this.source.items : [];
    },
    selectableItems() {
      const selected = new Set(this.selectedIds);
      return this.items.filter(item => item.importable && !item.imported && selected.has(item.id));
    },
    allPageSelected() {
      const rows = this.items.filter(item => item.importable && !item.imported);
      return rows.length > 0 && rows.every(item => this.selectedIds.includes(item.id));
    },
    canSelectAllFiltered() {
      const filterCount = this.filterSelectionCount;
      return !this.pending && !this.storageProblem && !this.loading && !this.loadingAll &&
        !!this.source && filterCount > this.selectedIds.length &&
        filterCount > (Number.isSafeInteger(this.source.eligible_count)
          ? this.source.eligible_count : this.importableOnPage) &&
        filterCount <= MAX_IMPORT_SELECTION;
    },
    filterSelectionCount() {
      if (!this.source) return 0;
      return Number.isSafeInteger(this.source.selection_block_total)
        ? this.source.selection_block_total
        : this.source.total;
    },
    selectionReason() {
      const reason = this.source && this.source.selection_block_reason;
      if (reason === 'limit') return 'По текущему фильтру больше 200 товаров. Уточните поиск или выберите товары на страницах.';
      if (reason === 'changed') return 'Состав каталога изменился во время проверки. Обновите список и выберите товары заново.';
      return typeof reason === 'string' ? reason.slice(0, 500) : '';
    },
    canSubmit() {
      return this.open && !this.loading && !this.loadingAll && !this.submitting &&
        !this.readingPending && !this.pending && !this.storageProblem &&
        !this.sessionEnded && isWorkspaceOwner(this.owner) && !!this.selectedSupplier &&
        this.selectedIds.length > 0 && this.selectedIds.length <= MAX_IMPORT_SELECTION &&
        this.source && String(this.source.supplier.id) === this.supplierId;
    },
    emptyMessage() {
      if (!this.selectedSupplier) return 'Выберите поставщика, чтобы открыть его товары.';
      if (this.search || this.stockStatus !== 'all') return 'По этим условиям товаров нет.';
      return 'В каталоге нет доступных товаров для импорта.';
    },
    importableOnPage() {
      return this.items.filter(item => item.importable && !item.imported).length;
    },
    currentPage() {
      return this.source && Number.isSafeInteger(this.source.page) ? this.source.page : this.page;
    },
    pages() {
      return this.source && Number.isSafeInteger(this.source.pages) ? Math.max(1, this.source.pages) : 1;
    },
  },
  watch: {
    open(isOpen) {
      if (!isOpen) return;
      this.initializeSupplier();
      if (this.selectedSupplier && !this.source && !this.loading) this.loadPage(1);
      this.$nextTick(() => this.$refs.title?.focus({ preventScroll: true }));
    },
    supplierId(next, previous) {
      if (next === previous) return;
      this.selectedIds = [];
      this.source = null;
      this.page = 1;
      this.error = '';
      this.status = '';
      if (next && this.open && !this.pending && !this.storageProblem) this.loadPage(1);
    },
    search() {
      this.scheduleFilterRead();
    },
    stockStatus() {
      this.scheduleFilterRead();
    },
  },
  mounted() {
    this.restorePending();
    if (this.open) {
      this.initializeSupplier();
      if (this.selectedSupplier) this.loadPage(1);
    }
  },
  beforeUnmount() {
    this.cancelRead();
    clearTimeout(this.searchTimer);
    clearTimeout(this.closeTimer);
  },
  methods: {
    initializeSupplier() {
      if (this.supplierId || this.availableSuppliers.length !== 1) return;
      this.supplierId = String(this.availableSuppliers[0].id);
    },
    storageKey() {
      return PENDING_KEY + ':' + this.owner;
    },
    restorePending() {
      let raw;
      try { raw = globalThis.sessionStorage.getItem(this.storageKey()); }
      catch (_) { this.storageProblem = true; return; }
      if (raw === null) return;
      try {
        const value = JSON.parse(raw);
        if (!validPendingImport(value, this.owner)) throw new Error('invalid pending receipt');
        this.pending = value;
        this.supplierId = String(value.supplier_id);
        this.status = 'Результат импорта не подтверждён. Сначала прочитайте сохранённый итог.';
      } catch (_) {
        this.storageProblem = true;
      }
    },
    savePending(value) {
      try {
        globalThis.sessionStorage.setItem(this.storageKey(), JSON.stringify(value));
        this.pending = value;
        return true;
      } catch (_) {
        this.error = 'Не удалось сохранить ключ запроса в этой вкладке. Импорт не отправлен.';
        return false;
      }
    },
    forgetPending() {
      try { globalThis.sessionStorage.removeItem(this.storageKey()); }
      catch (_) { this.storageProblem = true; return false; }
      this.pending = null;
      this.retryAllowed = false;
      return true;
    },
    catalogUrl(supplierId) {
      const row = this.availableSuppliers.find(item => String(item.id) === String(supplierId));
      if (!row) return null;
      try {
        const target = new URL(row.catalog_url, globalThis.location.origin);
        const expectedPath = '/supplier-catalog/' + supplierId + '/products';
        if (target.origin !== globalThis.location.origin || target.username || target.password ||
            target.hash || target.search ||
            !target.pathname.endsWith(expectedPath)) return null;
        return target;
      } catch (_) { return null; }
    },
    sameOriginActionUrl(value, expectedPath) {
      if (typeof value !== 'string' || value.length > 2000) return null;
      try {
        const target = new URL(value, globalThis.location.origin);
        if (target.origin !== globalThis.location.origin || target.username || target.password ||
            target.hash || target.search ||
            !target.pathname.endsWith(expectedPath)) return null;
        return target.pathname;
      } catch (_) { return null; }
    },
    setReadBusy(busy) {
      this.loading = busy;
      this.loadingAll = false;
    },
    cancelRead() {
      this.requestRevision += 1;
      if (this.requestController) this.requestController.abort();
      this.requestController = null;
      clearTimeout(this.requestTimer);
      this.requestTimer = null;
      this.loading = false;
      this.loadingAll = false;
    },
    async fetchCatalog({ page = 1, selectAll = false } = {}) {
      const supplier = this.selectedSupplier;
      const target = supplier && this.catalogUrl(supplier.id);
      if (!supplier || !target) throw new Error('Не удалось проверить ссылку каталога поставщика.');
      target.searchParams.set('page', String(selectAll ? 1 : page));
      target.searchParams.set('mode', 'import');
      const search = this.search.trim().slice(0, 200);
      if (search) target.searchParams.set('search', search);
      target.searchParams.set('stock_status', this.stockStatus);
      if (selectAll) target.searchParams.set('select_all', '1');

      const controller = new AbortController();
      this.requestController = controller;
      const revision = ++this.requestRevision;
      this.requestTimer = setTimeout(() => controller.abort(), 15000);
      const response = await fetch(target.pathname + target.search, {
        credentials: 'same-origin', cache: 'no-store', redirect: 'manual',
        headers: { Accept: 'application/json' }, signal: controller.signal,
      });
      if (revision !== this.requestRevision || controller.signal.aborted) return null;
      if (response.status === 401 || response.redirected || response.type === 'opaqueredirect') {
        this.sessionEnded = true;
        throw new Error('Сессия закончилась. Войдите снова и откройте импорт.');
      }
      if (response.status === 403) throw new Error('Нет доступа к этому каталогу поставщика.');
      if (!(response.headers.get('content-type') || '').includes('application/json')) {
        throw new Error('Каталог не вернул читаемый список.');
      }
      const data = await response.json();
      if (!response.ok || data.success !== true) {
        const error = new Error(responseMessage(data, 'Не удалось прочитать товары поставщика.'));
        error.status = response.status;
        throw error;
      }
      if (revision !== this.requestRevision) return null;
      return { data, revision };
    },
    async loadPage(page) {
      if (!this.open || this.pending || this.storageProblem || !this.supplierId) return;
      this.cancelRead();
      const revision = this.requestRevision + 1;
      this.loading = true;
      this.error = '';
      this.status = '';
      try {
        const result = await this.fetchCatalog({ page });
        if (!result || result.revision !== this.requestRevision) return;
        const validated = validateSupplierPage(result.data, this.supplierId, page);
        if (!validated) throw new Error('Состав страницы не совпал с выбранным поставщиком.');
        this.source = result.data;
        this.page = result.data.page;
      } catch (error) {
        if (this.requestRevision !== revision) return;
        if (error.name === 'AbortError') {
          this.error = 'Проверка каталога заняла слишком много времени. Повторите чтение.';
        } else {
          this.error = error.message || 'Не удалось прочитать товары поставщика.';
          if (error.message.includes('Сессия закончилась')) this.sessionEnded = true;
        }
      } finally {
        if (this.requestRevision === revision) {
          clearTimeout(this.requestTimer);
          this.requestTimer = null;
          this.requestController = null;
          this.loading = false;
        }
      }
    },
    scheduleFilterRead() {
      if (!this.open || this.pending || this.storageProblem || !this.supplierId) return;
      clearTimeout(this.searchTimer);
      this.searchTimer = setTimeout(() => {
        this.selectedIds = [];
        this.page = 1;
        this.source = null;
        this.loadPage(1);
      }, 250);
    },
    togglePage(checked) {
      const ids = this.items.filter(item => item.importable && !item.imported).map(item => item.id);
      const next = new Set(this.selectedIds);
      if (checked) {
        for (const id of ids) {
          if (next.has(id)) continue;
          if (next.size >= MAX_IMPORT_SELECTION) {
            this.error = 'За один импорт можно выбрать не больше 200 товаров.';
            break;
          }
          next.add(id);
        }
      }
      else ids.forEach(id => next.delete(id));
      this.selectedIds = Array.from(next);
    },
    toggleItem(item) {
      if (!item || !item.importable || item.imported || this.pending) return;
      const next = new Set(this.selectedIds);
      if (next.has(item.id)) next.delete(item.id);
      else if (next.size < MAX_IMPORT_SELECTION) next.add(item.id);
      else this.error = 'За один импорт можно выбрать не больше 200 товаров.';
      this.selectedIds = Array.from(next);
    },
    async selectAllFiltered() {
      if (!this.canSelectAllFiltered) return;
      this.cancelRead();
      const revision = this.requestRevision + 1;
      this.loadingAll = true;
      this.error = '';
      this.status = '';
      try {
        const result = await this.fetchCatalog({ page: 1, selectAll: true });
        if (!result || result.revision !== this.requestRevision) return;
        const validated = validateFilterSelection(result.data, this.supplierId);
        if (!validated) throw new Error('Не удалось подтвердить точный набор по фильтру.');
        if (validated.reason) {
          this.status = validated.reason;
          return;
        }
        if (!validated.selection.length || validated.selection.length > MAX_IMPORT_SELECTION) {
          throw new Error('Полный набор пуст или превышает лимит 200 товаров.');
        }
        this.selectedIds = validated.selection.slice();
        this.status = 'Выбран точный набор по текущему фильтру.';
      } catch (error) {
        if (this.requestRevision !== revision) return;
        if (error.name === 'AbortError') this.error = 'Проверка полного списка прервана. Повторите чтение.';
        else this.error = error.message || 'Не удалось проверить полный список.';
      } finally {
        if (this.requestRevision === revision) {
          this.loadingAll = false;
          clearTimeout(this.requestTimer);
          this.requestTimer = null;
          this.requestController = null;
        }
      }
    },
    close() {
      if (this.submitting || this.readingPending) return;
      this.$emit('close');
    },
    requestError(status, data) {
      const error = new Error(responseMessage(data, 'Импорт не подтверждён. Проверьте сохранённый результат.'));
      error.status = status;
      return error;
    },
    async postImport(pending) {
      const csrf = document.querySelector('meta[name="csrf-token"]')?.content || '';
      if (!csrf) throw new Error('Защита формы недоступна. Импорт не отправлен.');
      const endpoint = this.sameOriginActionUrl(this.urls.importPost, '/supplier-catalog/import');
      if (!endpoint) throw new Error('Ссылка импорта не прошла проверку. Запрос не отправлен.');
      const body = new FormData();
      body.append('supplier_id', pending.supplier_id);
      pending.product_ids.forEach(id => body.append('product_ids', id));
      body.append('csrf_token', csrf);
      body.append('request_key', pending.request_key);
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 60000);
      try {
        const response = await fetch(endpoint, {
          method: 'POST', credentials: 'same-origin', cache: 'no-store', redirect: 'manual',
          headers: { Accept: 'application/json' }, body, signal: controller.signal,
        });
        if (response.status === 401 || response.redirected || response.type === 'opaqueredirect') {
          this.sessionEnded = true;
          throw Object.assign(new Error('Сессия закончилась. Войдите снова и прочитайте результат импорта.'), { status: 401 });
        }
        if (!(response.headers.get('content-type') || '').includes('application/json')) {
          throw new Error('Ответ не подтверждён. Прочитайте результат по сохранённому ключу.');
        }
        const data = await response.json();
        if (!response.ok || data.success !== true) {
          const error = this.requestError(response.status, data);
          if (response.status === 400) {
            this.forgetPending();
          }
          throw error;
        }
        this.acceptKnownResult(data, pending);
      } finally { clearTimeout(timer); }
    },
    async submitImport() {
      if (!this.canSubmit) return;
      const ids = orderedUniqueIds(this.selectedIds);
      if (!ids || ids.length !== this.selectedIds.length || ids.length > MAX_IMPORT_SELECTION) {
        this.error = 'Список изменился. Отметьте товары заново.';
        return;
      }
      const key = requestKey();
      if (!key) {
        this.error = 'Защищённый ключ запроса недоступен. Импорт не отправлен.';
        return;
      }
      const pending = {
        v: 1,
        owner: this.owner,
        supplier_id: this.supplierId,
        product_ids: ids,
        request_key: key,
        created_at: Date.now(),
      };
      if (!this.savePending(pending)) return;
      this.submitting = true;
      this.retryAllowed = false;
      this.error = '';
      this.status = 'Отправляем точный выбранный набор…';
      try {
        await this.postImport(pending);
      } catch (error) {
        this.error = error.name === 'AbortError'
          ? 'Ответ не пришёл вовремя. Сначала прочитайте результат; импорт не повторяется автоматически.'
          : error.message || 'Ответ не подтверждён. Сначала прочитайте результат.';
      } finally {
        this.submitting = false;
      }
    },
    acceptKnownResult(raw, pending) {
      const result = validateImportResult(raw, pending.product_ids, pending.supplier_id);
      if (!result) throw new Error('Состав ответа не подтверждён. Прочитайте сохранённый результат.');
      if (result.import_batch) {
        const location = canonicalBatchLocation(
          result.batch_url, result.import_batch, this.urls.workspace, globalThis.location.origin,
        );
        if (!location) throw new Error('Ссылка на новый набор не совпала с этой рабочей областью. Прочитайте сохранённый результат.');
        result.batch_url = location;
      }
      if (!this.forgetPending()) throw new Error('Итог получен, но локальный ключ не удалось снять. Обновите состояние перед следующим импортом.');
      this.status = '';
      this.error = '';
      this.$emit('import-success', result);
    },
    async readPending() {
      if (!this.pending || this.readingPending || this.submitting || this.storageProblem) return;
      const pending = this.pending;
      this.readingPending = true;
      this.retryAllowed = false;
      this.error = '';
      this.status = 'Читаем сохранённый итог. Новая запись не отправляется.';
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 15000);
      try {
        const endpoint = this.sameOriginActionUrl(this.urls.importResult, '/supplier-catalog/import/result');
        if (!endpoint) throw new Error('Ссылка чтения результата не прошла проверку.');
        const response = await fetch(endpoint, {
          credentials: 'same-origin', cache: 'no-store', redirect: 'manual', signal: controller.signal,
          headers: { Accept: 'application/json', 'X-Import-Request-Key': pending.request_key },
        });
        if (response.status === 401 || response.redirected || response.type === 'opaqueredirect') {
          this.sessionEnded = true;
          throw new Error('Сессия закончилась. Войдите снова и прочитайте результат.');
        }
        if (response.status === 404) {
          this.retryAllowed = true;
          this.status = 'Сохранённый итог пока не найден. Повтор допустим только с тем же ключом и тем же набором.';
          return;
        }
        if (!(response.headers.get('content-type') || '').includes('application/json')) {
          throw new Error('Не удалось прочитать сохранённый итог.');
        }
        const data = await response.json();
        if (!response.ok || data.success !== true) throw this.requestError(response.status, data);
        this.acceptKnownResult(data, pending);
      } catch (error) {
        this.error = error.name === 'AbortError'
          ? 'Чтение заняло слишком много времени. Повторите только чтение.'
          : error.message || 'Не удалось прочитать сохранённый итог.';
      } finally {
        clearTimeout(timer);
        this.readingPending = false;
      }
    },
    async retrySameImport() {
      if (!this.pending || !this.retryAllowed || this.submitting || this.readingPending || this.sessionEnded) return;
      const pending = this.pending;
      if (!validPendingImport(pending, this.owner) ||
          pending.supplier_id !== this.supplierId || !orderedUniqueIds(pending.product_ids)) {
        this.storageProblem = true;
        this.error = 'Сохранённый запрос больше не совпадает с владельцем и точным набором.';
        return;
      }
      this.submitting = true;
      this.retryAllowed = false;
      this.error = '';
      this.status = 'Повторяем только прежний запрос с тем же ключом…';
      try { await this.postImport(pending); }
      catch (error) {
        this.error = error.name === 'AbortError'
          ? 'Ответ снова не пришёл. Прочитайте результат; новая попытка не запускается автоматически.'
          : error.message || 'Повтор не подтверждён. Прочитайте результат.';
      } finally { this.submitting = false; }
    },
    toggleAllOnPage(event) {
      this.togglePage(!!event.target.checked);
    },
    itemPrice(item) {
      if (item.purchase_price === null || item.purchase_price === undefined || item.purchase_price === '') return 'Цена не указана';
      const amount = Number(item.purchase_price);
      if (!Number.isFinite(amount) || amount < 0) return 'Цена не указана';
      const currency = typeof item.currency === 'string' && /^[A-Z]{3}$/.test(item.currency)
        ? item.currency : '';
      try {
        return new Intl.NumberFormat('ru-RU', currency
          ? { style: 'currency', currency, maximumFractionDigits: 2 }
          : { maximumFractionDigits: 2 }).format(amount);
      } catch (_) { return amount.toLocaleString('ru-RU'); }
    },
    stockLabel(item) {
      return Number.isFinite(item.stock) ? String(item.stock) : 'не указан';
    },
    closeRetryMessage() {
      this.status = '';
    },
  },
};
</script>

<template>
  <div class="mp-import-root" data-supplier-import-root>
    <section v-if="open" class="mp-import" aria-labelledby="mp-import-title" :aria-busy="loading || submitting ? 'true' : 'false'">
      <header class="mp-import-head">
        <div>
          <h2 id="mp-import-title" ref="title" tabindex="-1">Импорт товаров</h2>
          <p>Выберите товары поставщика. После импорта они откроются в «Моих товарах».</p>
        </div>
        <button type="button" class="mp-import-back" :disabled="submitting || readingPending" @click="close">
          К товарам
        </button>
      </header>

      <div v-if="storageProblem" class="mp-import-message mp-import-message--error" role="alert" data-supplier-import-pending>
        Не удалось безопасно прочитать сохранённый запрос. Новый импорт заблокирован; проверьте список товаров и повторно откройте эту страницу.
      </div>

      <div v-if="pending" class="mp-import-message mp-import-message--pending" role="status" aria-live="polite" data-supplier-import-pending>
        <div>
          <strong>Итог импорта пока неизвестен.</strong>
          <p>{{ pending.product_ids.length }} товаров · поставщик {{ pending.supplier_id }}. Сначала прочитайте результат по сохранённому ключу.</p>
          <p v-if="status">{{ status }}</p>
          <p v-if="error" class="mp-import-error-text">{{ error }}</p>
        </div>
        <div class="mp-import-message-actions">
          <button type="button" class="sh-btn sh-btn--secondary" data-supplier-import-reconcile :disabled="readingPending || submitting || sessionEnded" @click="readPending">
            {{ readingPending ? 'Читаем…' : 'Проверить результат' }}
          </button>
          <button v-if="retryAllowed" type="button" class="sh-btn sh-btn--primary" data-supplier-import-retry :disabled="readingPending || submitting || sessionEnded" @click="retrySameImport">
            {{ submitting ? 'Принимаем…' : 'Повторить тот же импорт' }}
          </button>
        </div>
      </div>

      <template v-if="!pending && !storageProblem">
        <div v-if="!availableSuppliers.length" class="mp-import-message mp-import-message--error" role="status">
          Сейчас нет доступного каталога для импорта.
          <a v-if="urls.supplierCatalog" class="mp-import-classic" :href="urls.supplierCatalog">Открыть классический каталог поставщика</a>
        </div>

        <div class="mp-import-controls">
          <div v-if="availableSuppliers.length" class="mp-import-source">
            <template v-if="availableSuppliers.length === 1">
              <span class="mp-import-label">Поставщик</span>
              <output>{{ availableSuppliers[0].name }}</output>
            </template>
            <label v-else>
              <span class="mp-import-label">Поставщик</span>
              <select v-model="supplierId" class="sh-input" data-supplier-import-source-select :disabled="loading || submitting">
                <option value="">Выберите поставщика</option>
                <option v-for="supplier in availableSuppliers" :key="supplier.id" :value="String(supplier.id)">
                  {{ supplier.name }}
                </option>
              </select>
            </label>
          </div>
          <label class="mp-import-search">
            <span class="mp-import-label">Найти товар</span>
            <input v-model="search" type="search" class="sh-input" data-supplier-import-search maxlength="200"
                   autocomplete="off" placeholder="Название или артикул" :disabled="!selectedSupplier || submitting">
          </label>
          <label class="mp-import-stock">
            <span class="mp-import-label">Остаток</span>
            <select v-model="stockStatus" class="sh-input" :disabled="!selectedSupplier || submitting">
              <option value="all">Любой</option>
              <option value="in_stock">В наличии</option>
              <option value="out_of_stock">Нет у поставщика</option>
            </select>
          </label>
        </div>

        <p v-if="error" class="mp-import-message mp-import-message--error" role="alert">{{ error }}</p>
        <p v-else-if="status" class="mp-import-message" role="status" aria-live="polite">{{ status }}</p>
        <p v-if="selectionReason" class="mp-import-selection-reason" role="status">{{ selectionReason }}</p>
        <p v-else-if="source && filterSelectionCount > 200" class="mp-import-selection-reason" role="status">
          По текущему фильтру доступно {{ filterSelectionCount }} товаров; за один импорт можно выбрать не больше 200.
        </p>

        <div class="mp-import-list-head">
          <label class="mp-import-page-check">
            <input type="checkbox" data-supplier-import-select-page :checked="allPageSelected" :indeterminate.prop="selectedIds.length > 0 && !allPageSelected"
                   :disabled="!importableOnPage || loading || submitting" @change="toggleAllOnPage">
            Выбрать доступные на странице
          </label>
          <span v-if="source">{{ source.total }} товаров · страница {{ currentPage }} из {{ pages }}</span>
        </div>

        <div class="mp-import-list" :aria-busy="loading || loadingAll">
          <div v-if="loading && !items.length" class="mp-import-empty" role="status">Читаем каталог…</div>
          <article v-for="item in items" :key="item.id" class="mp-import-row" :data-supplier-import-item-id="item.id"
                   :class="{ 'is-selected': selectedIds.includes(item.id), 'is-unavailable': !item.importable || item.imported }">
            <label class="mp-import-row-check">
              <input type="checkbox" :checked="selectedIds.includes(item.id)"
                     :disabled="!item.importable || item.imported || submitting || loadingAll"
                     :aria-label="'Выбрать ' + (item.title || item.article || item.id)"
                     @change="toggleItem(item)">
            </label>
            <div class="mp-import-product">
              <strong>{{ item.title || 'Без названия' }}</strong>
              <span>{{ item.article || 'Артикул не указан' }}</span>
              <small v-if="item.imported">Уже в «Моих товарах»</small>
              <small v-else-if="!item.importable">Недоступен для импорта</small>
            </div>
            <div class="mp-import-fact"><span>Закупка</span><strong>{{ itemPrice(item) }}</strong></div>
            <div class="mp-import-fact"><span>Остаток</span><strong>{{ stockLabel(item) }}</strong></div>
          </article>
          <div v-if="!loading && !error && !items.length" class="mp-import-empty">{{ emptyMessage }}</div>
        </div>

        <nav v-if="source && pages > 1" class="mp-import-pages" aria-label="Страницы каталога">
          <button type="button" class="sh-btn sh-btn--secondary" :disabled="loading || currentPage <= 1" @click="loadPage(currentPage - 1)">
            Назад
          </button>
          <span>Страница {{ currentPage }} из {{ pages }}</span>
          <button type="button" class="sh-btn sh-btn--secondary" :disabled="loading || currentPage >= pages" @click="loadPage(currentPage + 1)">
            Дальше
          </button>
        </nav>

        <details v-if="urls.supplierCatalog && availableSuppliers.length" class="mp-import-advanced">
          <summary>Расширенный каталог поставщика</summary>
          <a :href="urls.supplierCatalog">Открыть классический каталог поставщика</a>
        </details>
      </template>

      <form v-if="!pending && !storageProblem" class="mp-import-footer" @submit.prevent="submitImport">
        <div class="mp-import-count">
          <strong>{{ selectedIds.length }} из 200</strong>
          <span>выбрано для импорта</span>
          <button v-if="canSelectAllFiltered" type="button" class="mp-import-all" data-supplier-import-select-filtered
                  :disabled="loadingAll || submitting" @click="selectAllFiltered">
            {{ loadingAll ? 'Проверяем набор…' : 'Выбрать все ' + filterSelectionCount + ' по фильтру' }}
          </button>
        </div>
        <button type="submit" class="sh-btn sh-btn--primary mp-import-submit" data-supplier-import-submit :disabled="!canSubmit">
          {{ submitting ? 'Импортируем…' : 'Импортировать товары' }}
        </button>
      </form>
    </section>

    <div v-else-if="pending || storageProblem" class="mp-import-pending-compact" role="status">
      <div>
        <strong>{{ storageProblem ? 'Новый импорт заблокирован' : 'Результат импорта не подтверждён' }}</strong>
        <span>{{ storageProblem ? 'Сохранённый запрос нельзя прочитать.' : 'Проверьте сохранённый итог перед новым действием.' }}</span>
      </div>
      <button v-if="pending" type="button" class="sh-btn sh-btn--secondary" data-supplier-import-reconcile :disabled="readingPending || sessionEnded" @click="readPending">
        {{ readingPending ? 'Читаем…' : 'Проверить результат' }}
      </button>
      <button type="button" class="mp-import-open" data-supplier-import-pending-open @click="$emit('open')">Открыть импорт</button>
    </div>
  </div>
</template>

<style scoped>
.mp-import-root { min-width:0; }
.mp-import { --mi-border:var(--border); --mi-muted:var(--text-secondary); color:var(--text); min-width:0; padding:0 0 104px; }
.mp-import-head { display:flex; align-items:flex-start; justify-content:space-between; gap:18px; padding:18px 2px 16px; border-bottom:1px solid var(--mi-border); }
.mp-import-head h2 { margin:0; font-size:22px; line-height:1.2; font-weight:700; letter-spacing:-.02em; }
.mp-import-head h2:focus-visible { outline:2px solid var(--focus-outline); outline-offset:4px; border-radius:2px; }
.mp-import-head p { max-width:56ch; margin:7px 0 0; color:var(--mi-muted); font-size:13px; line-height:1.45; }
.mp-import-back,.mp-import-open { min-height:44px; border:1px solid var(--mi-border); border-radius:var(--r-sm); padding:0 13px; background:var(--bg-card); color:var(--text); font:inherit; font-size:13px; cursor:pointer; }
.mp-import-back:hover,.mp-import-open:hover { background:var(--bg-hover); }
.mp-import-back:focus-visible,.mp-import-open:focus-visible { outline:2px solid var(--focus-outline); outline-offset:2px; }
.mp-import-controls { display:grid; grid-template-columns:minmax(180px,.8fr) minmax(220px,1.35fr) minmax(150px,.55fr); gap:12px; align-items:end; padding:18px 0 14px; }
.mp-import-controls label,.mp-import-source { display:grid; gap:6px; min-width:0; }
.mp-import-label { display:block; color:var(--mi-muted); font-size:12px; font-weight:600; }
.mp-import-controls select,.mp-import-controls input { width:100%; min-height:42px; }
.mp-import-source output { display:flex; align-items:center; min-height:42px; padding:0 11px; border:1px solid var(--mi-border); border-radius:var(--r-sm); background:var(--bg-card); font-size:13px; font-weight:600; }
.mp-import-list-head { display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:10px; min-height:42px; padding:7px 2px; color:var(--mi-muted); font-size:12px; }
.mp-import-page-check { display:inline-flex; align-items:center; gap:9px; min-height:36px; color:var(--text); cursor:pointer; }
.mp-import-page-check input,.mp-import-row-check input { width:18px; height:18px; margin:0; accent-color:var(--accent); }
.mp-import-page-check input:focus-visible,.mp-import-row-check input:focus-visible { outline:2px solid var(--focus-outline); outline-offset:2px; }
.mp-import-list { border-top:1px solid var(--mi-border); border-bottom:1px solid var(--mi-border); background:var(--bg-card); }
.mp-import-row { display:grid; grid-template-columns:36px minmax(140px,1fr) minmax(110px,.34fr) minmax(90px,.25fr); gap:12px; align-items:center; min-height:76px; padding:11px 10px; border-bottom:1px solid var(--border-light); }
.mp-import-row:last-child { border-bottom:0; }
.mp-import-row.is-selected { background:var(--accent-light); }
.mp-import-row.is-unavailable:not(.is-selected) { color:var(--text-secondary); }
.mp-import-row-check { display:flex; align-items:center; justify-content:center; min-height:44px; }
.mp-import-product { display:grid; gap:4px; min-width:0; }
.mp-import-product strong { overflow-wrap:anywhere; color:var(--text); font-size:14px; line-height:1.35; }
.mp-import-product span,.mp-import-product small { color:var(--mi-muted); font-size:12px; overflow-wrap:anywhere; }
.mp-import-fact { display:grid; gap:4px; min-width:0; text-align:right; }
.mp-import-fact span { color:var(--mi-muted); font-size:11px; }
.mp-import-fact strong { overflow-wrap:anywhere; font-size:13px; font-variant-numeric:tabular-nums; }
.mp-import-empty { padding:26px 14px; color:var(--mi-muted); text-align:center; font-size:13px; }
.mp-import-pages { display:flex; align-items:center; justify-content:center; gap:14px; padding:14px 0; color:var(--mi-muted); font-size:12px; }
.mp-import-pages .sh-btn { min-height:44px; }
.mp-import-selection-reason { margin:0 0 9px; padding:10px 12px; border-left:3px solid var(--warn); background:var(--warn-bg); color:var(--text); font-size:13px; line-height:1.4; }
.mp-import-classic { display:inline-block; margin-left:8px; color:var(--accent-text); text-decoration:underline; text-underline-offset:2px; }
.mp-import-classic:focus-visible { outline:2px solid var(--focus-outline); outline-offset:2px; }
.mp-import-advanced { margin:10px 0 0; color:var(--mi-muted); font-size:12px; }
.mp-import-advanced summary { width:max-content; min-height:32px; display:flex; align-items:center; cursor:pointer; }
.mp-import-advanced summary:focus-visible { outline:2px solid var(--focus-outline); outline-offset:2px; }
.mp-import-advanced a { display:inline-block; margin:3px 0 5px; color:var(--accent-text); text-underline-offset:2px; }
.mp-import-advanced a:focus-visible { outline:2px solid var(--focus-outline); outline-offset:2px; }
.mp-import-message { margin:10px 0; padding:11px 13px; border:1px solid var(--mi-border); border-radius:var(--r-sm); color:var(--text); font-size:13px; line-height:1.45; }
.mp-import-message p { margin:5px 0 0; color:var(--mi-muted); }
.mp-import-message--error { border-color:var(--danger-border); background:var(--danger-bg); color:var(--danger); }
.mp-import-message--pending { display:flex; justify-content:space-between; align-items:center; gap:16px; border-color:var(--warn-border); background:var(--warn-bg); }
.mp-import-message-actions { display:flex; flex-wrap:wrap; gap:8px; }
.mp-import-message-actions .sh-btn { min-height:44px; }
.mp-import-error-text { color:var(--danger)!important; }
.mp-import-footer { position:sticky; bottom:8px; z-index:var(--z-sticky, 20); display:flex; justify-content:space-between; align-items:center; gap:16px; margin:12px 0 0; padding:11px 12px; border:1px solid var(--mi-border); border-radius:var(--r-md); background:var(--bg-card); box-shadow:var(--shadow-2); }
.mp-import-count { display:grid; grid-template-columns:auto auto; align-items:baseline; column-gap:7px; row-gap:3px; min-width:0; }
.mp-import-count strong { color:var(--text); font-size:14px; font-variant-numeric:tabular-nums; }
.mp-import-count span { color:var(--mi-muted); font-size:12px; }
.mp-import-all { grid-column:1/-1; justify-self:start; min-height:30px; padding:2px 0; border:0; background:transparent; color:var(--accent-text); font:inherit; font-size:12px; text-align:left; cursor:pointer; }
.mp-import-all:disabled { color:var(--text-muted); cursor:wait; }
.mp-import-all:focus-visible { outline:2px solid var(--focus-outline); outline-offset:2px; }
.mp-import-submit { min-height:46px; padding-inline:18px; white-space:nowrap; }
.mp-import-submit:focus-visible { outline:2px solid var(--focus-outline); outline-offset:2px; }
.mp-import-pending-compact { display:flex; flex-wrap:wrap; align-items:center; justify-content:space-between; gap:12px; margin:0 0 14px; padding:12px; border:1px solid var(--warn-border); border-radius:var(--r-sm); background:var(--warn-bg); }
.mp-import-pending-compact div { display:grid; gap:3px; min-width:180px; }
.mp-import-pending-compact span { color:var(--mi-muted); font-size:12px; }
.mp-import-pending-compact .sh-btn { min-height:44px; }
@media (max-width:700px) {
  .mp-import-controls { grid-template-columns:minmax(0,1fr) minmax(0,1fr); }
  .mp-import-source { grid-column:1/-1; }
  .mp-import-row { grid-template-columns:34px minmax(0,1fr) minmax(88px,.35fr); gap:9px; }
  .mp-import-product { grid-column:2/-1; }
  .mp-import-row-check { grid-row:1/3; }
  .mp-import-fact { text-align:left; padding-left:2px; }
  .mp-import-message--pending { align-items:flex-start; flex-direction:column; }
}
@media (max-width:420px) {
  .mp-import { padding-bottom:115px; }
  .mp-import-head { align-items:flex-start; flex-direction:column; gap:10px; }
  .mp-import-head h2 { font-size:20px; }
  .mp-import-back { align-self:flex-start; }
  .mp-import-controls { grid-template-columns:minmax(0,1fr); gap:9px; padding-top:13px; }
  .mp-import-source { grid-column:auto; }
  .mp-import-row { grid-template-columns:34px minmax(0,1fr) minmax(82px,.45fr); padding:10px 7px; }
  .mp-import-fact strong { font-size:12px; }
  .mp-import-footer { bottom:4px; align-items:stretch; flex-direction:column; gap:8px; padding:10px; }
  .mp-import-count { grid-template-columns:auto 1fr; }
  .mp-import-submit { width:100%; white-space:normal; }
  .mp-import-pending-compact { align-items:flex-start; flex-direction:column; }
}
@media (prefers-reduced-motion:reduce) {
  .mp-import * { scroll-behavior:auto!important; animation-duration:.01ms!important; animation-iteration-count:1!important; transition-duration:.01ms!important; }
}
</style>
