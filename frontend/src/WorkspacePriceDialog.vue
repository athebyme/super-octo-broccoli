<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { createPriceWorkspaceAdapter } from './price-workspace-contract.js';

const props = defineProps({
  open: { type: Boolean, default: false },
  scope: { type: Object, required: true },
  owner: { type: String, required: true },
  urls: { type: Object, required: true },
  reviewHref: { type: String, default: '' },
});

const emit = defineEmits(['close', 'saved', 'legacy']);

const dialog = ref(null);
const heading = ref(null);
const amountInput = ref(null);
const activeScope = ref(null);
const adapter = ref(null);
const stage = ref('edit');
const busy = ref('');
const error = ref('');
const unknown = ref(null);
const preview = ref(null);
const receipt = ref(null);
const mode = ref('supplier_pricing');
const priceSource = ref('local');
const amount = ref('');
const allowPriceDecrease = ref(false);
const allowLargeChange = ref(false);
const guardrailNote = ref('');
const returnFocus = ref(null);
const bodyOverflowBeforeOpen = ref(null);
let requestRevision = 0;

const isOzon = computed(() => activeScope.value?.marketplace === 'ozon');
const marketplaceLabel = computed(() => isOzon.value ? 'Ozon' : 'Wildberries');
const selectedCount = computed(() => activeScope.value?.productIds?.length || 0);
const targetLabel = computed(() => isOzon.value
  ? (activeScope.value?.accountLabel || 'Кабинет Ozon')
  : 'Кабинет Wildberries');
const amountUnit = computed(() => mode.value === 'percent' ? '%' : '₽');
const normalizedAmount = computed(() => amount.value.trim().replace(/[\s\u00a0]/g, '').replace(',', '.'));
const amountLooksValid = computed(() => {
  if (!normalizedAmount.value || !/^[-+]?\d+(?:\.\d{1,2})?$/.test(normalizedAmount.value)) return false;
  const value = Number(normalizedAmount.value);
  return Number.isFinite(value) && Math.abs(value) < 1e12;
});
const canPreview = computed(() => {
  if (!adapter.value || !activeScope.value || busy.value || unknown.value || receipt.value || !selectedCount.value) return false;
  if (isOzon.value) {
    const guarded = allowPriceDecrease.value || allowLargeChange.value;
    const amountIsValidForMode = mode.value === 'set'
      ? Number(normalizedAmount.value) > 0 && Number(normalizedAmount.value) <= 999999999
      : Number(normalizedAmount.value) >= -99.99 && Number(normalizedAmount.value) <= 500 &&
        (Number(normalizedAmount.value) >= 0 || allowPriceDecrease.value);
    return amountLooksValid.value && amountIsValidForMode &&
      (!guarded || guardrailNote.value.trim().length >= 8);
  }
  if (mode.value === 'supplier_pricing') return true;
  if (mode.value === 'set' && Number(normalizedAmount.value) <= 0) return false;
  return amountLooksValid.value;
});
const canSave = computed(() => preview.value?.canSave === true && !!preview.value?.token &&
  !busy.value && !unknown.value);
const resultItems = computed(() => Array.isArray(preview.value?.items) ? preview.value.items : []);
const previewSummary = computed(() => preview.value?.summary || {});
const editProducts = computed(() => (activeScope.value?.rows || []).slice(0, 4));
const moreProducts = computed(() => Math.max(0, selectedCount.value - editProducts.value.length));
const receiptDestination = computed(() => {
  const destination = receipt.value?.destination;
  const safeHref = destination && typeof destination.href === 'string' ? destination.href : '';
  if (!safeHref) return '';
  if (!props.reviewHref || typeof window === 'undefined') return safeHref;
  try {
    const base = window.location.origin;
    const expected = new URL(safeHref, base);
    const candidate = new URL(props.reviewHref, base);
    if (expected.origin === base && !expected.username && !expected.password &&
        candidate.origin === base && !candidate.username && !candidate.password &&
        candidate.pathname + candidate.search === expected.pathname + expected.search &&
        /^#workspace-return=[a-f0-9]{32}$/.test(candidate.hash)) {
      return candidate.pathname + candidate.search + candidate.hash;
    }
  } catch (_) { /* Keep the adapter-validated destination below. */ }
  return safeHref;
});
const receiptDestinationLabel = computed(() => {
  const destination = receipt.value?.destination;
  if (destination && typeof destination.label === 'string') return destination.label;
  return isOzon.value ? 'Проверить ценовые заявки' : 'Открыть пакет цен';
});
const receiptHeadline = computed(() => isOzon.value
  ? 'Ценовые заявки сохранены'
  : 'Пакет изменения цен сохранён');

function cloneScope(scope) {
  if (!scope || typeof scope !== 'object') return null;
  return {
    marketplace: scope.marketplace,
    productIds: Array.isArray(scope.productIds) ? scope.productIds.slice() : [],
    accountId: scope.accountId ?? null,
    accountLabel: typeof scope.accountLabel === 'string' ? scope.accountLabel : '',
    rows: Array.isArray(scope.rows) ? scope.rows.map(row => ({
      id: row?.id,
      title: typeof row?.title === 'string' ? row.title : '',
      article: typeof row?.article === 'string' ? row.article : '',
      photo: typeof row?.photo === 'string' ? row.photo : '',
    })) : [],
  };
}

function makeAdapter() {
  if (typeof window === 'undefined') return null;
  try {
    return createPriceWorkspaceAdapter({
      urls: props.urls,
      owner: props.owner,
      origin: window.location.origin,
      fetchImpl: window.fetch.bind(window),
      sessionStorage: window.sessionStorage,
      now: () => Date.now(),
    });
  } catch (_) {
    error.value = 'Не удалось подготовить безопасное окно цен. Обновите страницу.';
    return null;
  }
}

function resetInputs(target) {
  mode.value = target?.marketplace === 'ozon' ? 'set' : 'supplier_pricing';
  priceSource.value = 'local';
  amount.value = '';
  allowPriceDecrease.value = false;
  allowLargeChange.value = false;
  guardrailNote.value = '';
}

function invalidatePreview() {
  if (unknown.value || receipt.value) return;
  preview.value = null;
  stage.value = 'edit';
  error.value = '';
}

function changeMode(value) {
  if (busy.value || unknown.value || receipt.value) return;
  mode.value = value;
  amount.value = '';
  preview.value = null;
  stage.value = 'edit';
  error.value = '';
  nextTick(() => amountInput.value?.focus());
}

function nativeInputs() {
  if (isOzon.value) {
    return {
      mode: mode.value,
      value: normalizedAmount.value,
      allowPriceDecrease: allowPriceDecrease.value,
      allowLargeChange: allowLargeChange.value,
      guardrailNote: (allowPriceDecrease.value || allowLargeChange.value)
        ? guardrailNote.value.trim() : null,
    };
  }
  return {
    changeType: mode.value,
    changeValue: mode.value === 'supplier_pricing' ? null : normalizedAmount.value,
    formula: '',
    formulaVariables: {},
    priceSource: priceSource.value,
  };
}

function closeDialog(emitClose = false) {
  if (busy.value) return;
  const node = dialog.value;
  if (node?.open) node.close();
  if (emitClose) emit('close');
}

function onCancel(event) {
  event.preventDefault();
  closeDialog(true);
}

function onNativeClose() {
  if (bodyOverflowBeforeOpen.value !== null && typeof document !== 'undefined') {
    document.body.style.overflow = bodyOverflowBeforeOpen.value;
    bodyOverflowBeforeOpen.value = null;
  }
  const target = returnFocus.value;
  returnFocus.value = null;
  if (target?.isConnected && typeof target.focus === 'function') {
    nextTick(() => target.focus({ preventScroll: true }));
  }
}

function closeIfParentRequested() {
  if (!props.open && !busy.value && dialog.value?.open) dialog.value.close();
}

function recoverPending(target, nextAdapter) {
  if (!target || !nextAdapter) return;
  try {
    const recovered = nextAdapter.recover(target);
    if (recovered?.state === 'unknown') {
      unknown.value = recovered;
      stage.value = 'unknown';
    }
  } catch (_) {
    error.value = 'Не удалось прочитать сохранённое состояние запроса. Изменения не повторены.';
  }
}

async function openDialog() {
  if (!props.open || typeof window === 'undefined') return;
  const node = dialog.value;
  if (!node || node.open) return;
  activeScope.value = cloneScope(props.scope);
  error.value = '';
  adapter.value = makeAdapter();
  preview.value = null;
  receipt.value = null;
  unknown.value = null;
  stage.value = 'edit';
  busy.value = '';
  resetInputs(activeScope.value);
  returnFocus.value = document.activeElement;
  bodyOverflowBeforeOpen.value = document.body.style.overflow;
  document.body.style.overflow = 'hidden';
  try {
    node.showModal();
  } catch (_) {
    const previousOverflow = bodyOverflowBeforeOpen.value;
    bodyOverflowBeforeOpen.value = null;
    document.body.style.overflow = previousOverflow ?? '';
    error.value = 'Не удалось открыть окно изменения цен.';
    return;
  }
  recoverPending(activeScope.value, adapter.value);
  await nextTick();
  heading.value?.focus({ preventScroll: true });
}

function onModeSelect(event) {
  changeMode(event.target.value);
}

async function runPreview() {
  if (!canPreview.value || !adapter.value || !activeScope.value) return;
  const revision = ++requestRevision;
  busy.value = 'preview';
  error.value = '';
  preview.value = null;
  stage.value = 'edit';
  try {
    const result = await adapter.value.preview(activeScope.value, nativeInputs());
    if (revision !== requestRevision || !dialog.value?.open) return;
    preview.value = result;
    stage.value = 'preview';
    await nextTick();
    document.getElementById('workspace-price-preview-heading')?.focus({ preventScroll: true });
  } catch (cause) {
    if (revision === requestRevision) error.value = cause?.message || 'Не удалось рассчитать изменения. Проверьте выбор и повторите.';
  } finally {
    if (revision === requestRevision) busy.value = '';
    closeIfParentRequested();
  }
}

async function saveLocally() {
  if (!canSave.value || !adapter.value || !preview.value) return;
  busy.value = 'save';
  error.value = '';
  try {
    const saved = await adapter.value.save(preview.value, {
      name: 'Изменение цены из «Мои товары»',
      description: 'Проверенный выбор из единого каталога Seller Hub.',
    });
    receipt.value = saved;
    unknown.value = null;
    stage.value = 'receipt';
    emit('saved', saved);
    await nextTick();
    document.getElementById('workspace-price-receipt-heading')?.focus({ preventScroll: true });
  } catch (cause) {
    error.value = cause?.message || 'Не удалось подтвердить локальное сохранение.';
    if (cause?.outcomeUnknown === true) {
      try { unknown.value = adapter.value.recover(activeScope.value); }
      catch (_) { unknown.value = { state: 'unknown', retryAvailable: false }; }
      stage.value = 'unknown';
    }
  } finally {
    busy.value = '';
    closeIfParentRequested();
  }
}

async function retryExactSave() {
  if (busy.value || !unknown.value?.retryAvailable || !adapter.value || !activeScope.value) return;
  busy.value = 'retry';
  error.value = '';
  try {
    const saved = await adapter.value.retryPending(activeScope.value);
    receipt.value = saved;
    unknown.value = null;
    stage.value = 'receipt';
    emit('saved', saved);
    await nextTick();
    document.getElementById('workspace-price-receipt-heading')?.focus({ preventScroll: true });
  } catch (cause) {
    error.value = cause?.message || 'Точный повтор не подтвердил сохранение.';
    try { unknown.value = adapter.value.recover(activeScope.value); }
    catch (_) { unknown.value = { state: 'unknown', retryAvailable: false }; }
  } finally {
    busy.value = '';
    closeIfParentRequested();
  }
}

function editAgain() {
  if (busy.value || unknown.value || receipt.value) return;
  preview.value = null;
  stage.value = 'edit';
  error.value = '';
  nextTick(() => amountInput.value?.focus());
}

function openLegacy() {
  if (busy.value) return;
  emit('legacy', activeScope.value);
}

function rowStatus(item) {
  const status = item?.result?.status || '';
  const safety = item?.result?.safetyLevel;
  if (status === 'changed' && safety === 'safe') return 'В пределах лимита';
  if (status === 'changed' && safety === 'warning') return 'Проверьте порог';
  if (status === 'changed' && safety === 'dangerous') return 'Опасное изменение';
  if (status === 'changed') return 'Будет изменено';
  if (status === 'eligible') return 'К заявке';
  if (['noop', 'unchanged'].includes(status)) return 'Без изменений';
  if (['skipped', 'invalid', 'blocked'].includes(status)) return status === 'skipped' ? 'Пропуск' : 'Заблокировано';
  return status || 'Проверено';
}

function rowStatusClass(item) {
  const status = item?.result?.status || '';
  const safety = item?.result?.safetyLevel;
  if (safety === 'warning' || safety === 'dangerous') return 'is-warning';
  if (['eligible', 'changed', 'safe'].includes(status)) return 'is-ready';
  if (['warning', 'dangerous', 'blocked', 'invalid'].includes(status)) return 'is-warning';
  return 'is-muted';
}

function formatMoney(value, currency = 'RUB') {
  if (value === null || value === undefined || value === '') return '—';
  const number = Number(value);
  if (!Number.isFinite(number)) return '—';
  try {
    return new Intl.NumberFormat('ru-RU', {
      style: 'currency', currency, maximumFractionDigits: 2,
    }).format(number);
  } catch (_) { return `${number.toLocaleString('ru-RU')} ${currency}`; }
}

function sourceLabel(result) {
  const sources = {
    local_catalog_price: 'Цена в каталоге',
    wb_seller_price: 'Цена продавца WB',
    local_fallback: 'Локальная цена',
  };
  return sources[result?.sourceKind] || (isOzon.value ? 'Цена продавца Ozon' : 'Источник не указан');
}

function oldPrice(item) {
  const result = item?.result || {};
  return isOzon.value ? result.currentPrice : result.oldPrice;
}

function newPrice(item) {
  const result = item?.result || {};
  return isOzon.value ? result.proposedPrice : result.newPrice;
}

function rowReason(item) {
  const result = item?.result || {};
  return [result.reason, result.reasonCode, result.priceReview?.reason]
    .find(value => typeof value === 'string' && value.trim()) || '';
}

function rowFreshness(item) {
  const result = item?.result || {};
  if (isOzon.value) return '';
  if (result.sourceFreshnessAt) return `Снимок ${formatDate(result.sourceFreshnessAt)}`;
  if (result.sourceFreshness === 'not_recorded') return 'Дата источника не записана';
  return '';
}

function safetyLabel(value) {
  return ({ safe: 'В пределах лимита', warning: 'Предупреждение', dangerous: 'Опасное изменение' })[value] || '';
}

function formatDate(value) {
  let normalized = value;
  if (typeof value === 'string' && /[T ]\d{2}:\d{2}/.test(value) &&
      !/(?:z|[+-]\d{2}(?::?\d{2})?)$/i.test(value.trim())) {
    normalized = `${value.trim()}Z`;
  }
  const date = new Date(normalized);
  if (Number.isNaN(date.getTime())) return 'Дата неизвестна';
  return new Intl.DateTimeFormat('ru-RU', { dateStyle: 'short', timeStyle: 'short' }).format(date);
}

watch(() => props.open, value => {
  if (value) openDialog();
  else if (dialog.value?.open && !busy.value) dialog.value.close();
});

onMounted(() => {
  if (props.open) openDialog();
});

onBeforeUnmount(() => {
  requestRevision += 1;
  if (dialog.value?.open) dialog.value.close();
  if (bodyOverflowBeforeOpen.value !== null && typeof document !== 'undefined') {
    document.body.style.overflow = bodyOverflowBeforeOpen.value;
    bodyOverflowBeforeOpen.value = null;
  }
});

defineExpose({ open: openDialog, close: () => closeDialog(true) });
</script>

<template>
  <Teleport to="body">
    <dialog
      ref="dialog"
      class="mp-price-dialog"
      data-workspace-price-dialog
      :data-price-stage="stage"
      :data-price-marketplace="activeScope?.marketplace || ''"
      aria-labelledby="workspace-price-title"
      aria-describedby="workspace-price-description"
      :aria-busy="busy ? 'true' : 'false'"
      @cancel="onCancel"
      @close="onNativeClose"
    >
      <div class="mp-price-dialog__shell">
        <header class="mp-price-dialog__header">
          <div class="mp-price-dialog__heading-block">
            <div class="mp-price-dialog__context">
              <span :class="['mp-price-dialog__market', isOzon ? 'is-ozon' : 'is-wb']">{{ marketplaceLabel }}</span>
              <span>{{ targetLabel }}</span>
              <span aria-hidden="true">·</span>
              <span>{{ selectedCount }} {{ selectedCount === 1 ? 'карточка' : 'карточек' }}</span>
            </div>
            <h2 id="workspace-price-title" ref="heading" tabindex="-1">Изменить цену</h2>
            <p id="workspace-price-description">Сначала сравнение, затем локальное сохранение.</p>
          </div>
          <button
            type="button"
            class="sh-btn sh-btn--secondary mp-price-dialog__close"
            aria-label="Закрыть изменение цены"
            :disabled="!!busy"
            @click="closeDialog(true)"
          >×</button>
        </header>

        <div class="mp-price-dialog__body" :inert="!!busy ? true : undefined">
          <p v-if="error" class="sh-alert sh-alert--error mp-price-dialog__alert" role="alert" data-price-error>{{ error }}</p>

          <template v-if="stage === 'edit'">
            <section class="mp-price-dialog__scope" aria-label="Выбранные карточки">
              <div class="mp-price-dialog__scope-rows">
                <div v-for="row in editProducts" :key="row.id" class="mp-price-dialog__scope-row">
                  <img v-if="row.photo" :src="row.photo" alt="" loading="lazy" width="36" height="36">
                  <span v-else class="mp-price-dialog__photo-placeholder" aria-hidden="true"></span>
                  <span class="mp-price-dialog__scope-title">{{ row.title || row.article || 'Карточка товара' }}</span>
                  <span class="mp-price-dialog__scope-article">{{ row.article || '' }}</span>
                </div>
                <p v-if="moreProducts" class="mp-price-dialog__more">И ещё {{ moreProducts }} {{ moreProducts === 1 ? 'карточка' : 'карточек' }} в этом точном выборе.</p>
              </div>
            </section>

            <form class="mp-price-dialog__form" @submit.prevent="runPreview">
              <label class="mp-price-dialog__field">
                <span>Как изменить</span>
                <select
                  :value="mode"
                  data-price-mode
                  :disabled="!!busy || !!unknown || !!receipt"
                  @change="onModeSelect"
                >
                  <template v-if="isOzon">
                    <option value="set">Установить цену</option>
                    <option value="percent">Изменить на процент</option>
                  </template>
                  <template v-else>
                    <option value="supplier_pricing">По формуле продавца</option>
                    <option value="set">Установить цену</option>
                    <option value="fixed">Изменить на сумму</option>
                    <option value="percent">Изменить на процент</option>
                  </template>
                </select>
              </label>

              <label v-if="!isOzon" class="mp-price-dialog__field">
                <span>От какой цены считать</span>
                <select v-model="priceSource" data-price-source :disabled="!!busy || !!unknown || !!receipt" @change="invalidatePreview">
                  <option value="local">Цена в каталоге</option>
                  <option value="marketplace">Наблюдённая цена WB</option>
                </select>
              </label>

              <label v-if="mode !== 'supplier_pricing'" class="mp-price-dialog__field mp-price-dialog__field--wide">
                <span>{{ mode === 'percent' ? 'Изменение, %' : mode === 'fixed' ? 'Изменение, ₽' : 'Новая цена, ₽' }}</span>
                <span class="mp-price-dialog__amount-wrap">
                  <input
                    ref="amountInput"
                    v-model="amount"
                    type="text"
                    inputmode="decimal"
                    autocomplete="off"
                    maxlength="32"
                    :placeholder="amountUnit"
                    data-price-amount
                    :disabled="!!busy || !!unknown || !!receipt"
                    @input="invalidatePreview"
                  >
                  <span aria-hidden="true">{{ amountUnit }}</span>
                </span>
              </label>

              <p v-if="!isOzon && mode === 'supplier_pricing'" class="mp-price-dialog__hint mp-price-dialog__field--wide">
                Используются сохранённые настройки формулы продавца и положительная закупочная цена для каждой строки. Карточки без исходных данных будут пропущены.
              </p>
              <p v-if="!isOzon && priceSource === 'marketplace'" class="mp-price-dialog__hint mp-price-dialog__field--wide">
                Берём последнюю сохранённую цену продавца WB. Это наблюдение кабинета, не публичная цена покупателя.
              </p>

              <details v-if="isOzon" class="mp-price-dialog__advanced mp-price-dialog__field--wide" :open="allowPriceDecrease || allowLargeChange">
                <summary>Дополнительные условия</summary>
                <fieldset class="mp-price-dialog__guardrails" :disabled="!!busy || !!unknown || !!receipt">
                  <label><input v-model="allowPriceDecrease" type="checkbox" data-price-allow-decrease @change="invalidatePreview">Разрешить снижение цены</label>
                  <label><input v-model="allowLargeChange" type="checkbox" data-price-allow-large @change="invalidatePreview">Разрешить крупное изменение</label>
                  <label v-if="allowPriceDecrease || allowLargeChange" class="mp-price-dialog__field">
                    <span>Почему нужно исключение</span>
                    <textarea v-model="guardrailNote" rows="2" maxlength="1000" data-price-guardrail-note @input="invalidatePreview"></textarea>
                  </label>
                </fieldset>
              </details>
            </form>
          </template>

          <template v-else-if="stage === 'preview' && preview">
            <section class="mp-price-dialog__summary" aria-label="Итог предпросмотра">
              <div><strong>{{ previewSummary.changedCount || previewSummary.eligibleCount || 0 }}</strong><span>{{ isOzon ? 'к заявке' : 'к изменению' }}</span></div>
              <div v-if="previewSummary.noOpCount"><strong>{{ previewSummary.noOpCount }}</strong><span>без изменений</span></div>
              <div v-if="previewSummary.skippedCount"><strong>{{ previewSummary.skippedCount }}</strong><span>пропуск</span></div>
              <div v-if="previewSummary.blockedCount || previewSummary.invalidCount">
                <strong>{{ (previewSummary.blockedCount || 0) + (previewSummary.invalidCount || 0) }}</strong><span>заблокировано</span>
              </div>
              <div v-if="previewSummary.warningCount"><strong>{{ previewSummary.warningCount }}</strong><span>с предупреждением</span></div>
              <div v-if="previewSummary.dangerousCount"><strong>{{ previewSummary.dangerousCount }}</strong><span>опасное изменение</span></div>
            </section>
            <p v-if="isOzon" class="mp-price-dialog__eligible-note">
              В локальную заявку попадут только {{ previewSummary.eligibleCount || 0 }} подходящих карточек из {{ selectedCount }}.
            </p>

            <h3 id="workspace-price-preview-heading" tabindex="-1" class="mp-price-dialog__section-title">
              Проверка по каждой карточке
            </h3>
            <ol class="mp-price-dialog__review-list" data-price-review-list>
              <li
                v-for="entry in resultItems"
                :key="entry.row.id"
                class="mp-price-dialog__review-row"
                :data-price-review-row="entry.row.id"
              >
                <div class="mp-price-dialog__review-product">
                  <img v-if="entry.row.photo" :src="entry.row.photo" alt="" loading="lazy" width="42" height="42">
                  <span v-else class="mp-price-dialog__photo-placeholder" aria-hidden="true"></span>
                  <span class="mp-price-dialog__review-title">
                    <strong>{{ entry.row.title || entry.row.article || 'Карточка товара' }}</strong>
                    <small>{{ entry.row.article || (isOzon ? 'Ozon' : 'WB') }}</small>
                  </span>
                  <span :class="['mp-price-dialog__status', rowStatusClass(entry)]">{{ rowStatus(entry) }}</span>
                </div>
                <div class="mp-price-dialog__review-values">
                  <div>
                    <span>{{ isOzon ? 'Цена продавца сейчас' : sourceLabel(entry.result) }}</span>
                    <strong>{{ formatMoney(oldPrice(entry), isOzon ? (entry.result.currencyCode || 'RUB') : 'RUB') }}</strong>
                    <small v-if="rowFreshness(entry)">{{ rowFreshness(entry) }}</small>
                  </div>
                  <span class="mp-price-dialog__arrow" aria-hidden="true">→</span>
                  <div>
                    <span>{{ isOzon ? 'Предложение' : 'После применения' }}</span>
                    <strong>{{ formatMoney(newPrice(entry), isOzon ? (entry.result.currencyCode || 'RUB') : 'RUB') }}</strong>
                    <small v-if="entry.result.safetyLevel">{{ safetyLabel(entry.result.safetyLevel) }}</small>
                  </div>
                </div>
                <p v-if="isOzon" class="mp-price-dialog__buyer-unknown-note">Цена покупателя Ozon: неизвестна.</p>
                <p v-if="rowReason(entry)" class="mp-price-dialog__row-reason">{{ rowReason(entry) }}</p>
              </li>
            </ol>
            <p class="mp-price-dialog__local-only">
              {{ isOzon
                ? 'Сохранение создаст только ценовые заявки. Отправка в Ozon потребует отдельного подтверждения.'
                : 'Сохранение создаст только локальный пакет цен. Применение к WB остаётся отдельным действием.' }}
            </p>
          </template>

          <template v-else-if="stage === 'unknown'">
            <section class="mp-price-dialog__uncertain" data-price-unknown>
              <span class="mp-price-dialog__uncertain-mark" aria-hidden="true">?</span>
              <div>
                <h3>Сохранение ещё не подтверждено</h3>
                <p>Запрос мог завершиться. Не меняйте состав, магазин или параметры. Проверьте результат только тем же защищённым подтверждением.</p>
                <p v-if="unknown?.attemptedAt">Попытка: {{ formatDate(unknown.attemptedAt) }}</p>
                <p v-if="unknown && !unknown.retryAvailable">Точный повтор недоступен. Новая заявка не отправлена; проверьте историю цен в расширенном разделе.</p>
              </div>
            </section>
          </template>

          <template v-else-if="stage === 'receipt' && receipt">
            <section class="mp-price-dialog__receipt" data-price-receipt>
              <div class="mp-price-dialog__receipt-mark" aria-hidden="true">✓</div>
              <div>
                <h3 id="workspace-price-receipt-heading" tabindex="-1">{{ receiptHeadline }}</h3>
                <p v-if="isOzon">Сохранено {{ receipt.savedCount }} из {{ receipt.selectedCount }} выбранных карточек. В Ozon ничего не отправлено.</p>
                <p v-else>Сохранён пакет №{{ receipt.batchId }}: {{ receipt.changedCount }} изменений из {{ receipt.selectedCount }} выбранных карточек.</p>
                <p v-if="receipt.replayed">Показан уже сохранённый точный результат.</p>
                <a v-if="receiptDestination" class="sh-btn sh-btn--primary mp-price-dialog__destination" :href="receiptDestination" data-price-review-link>{{ receiptDestinationLabel }}</a>
                <p v-else-if="isOzon">Для этих заявок нет списка на отдельную проверку; откройте ценовые предложения расширенного раздела.</p>
              </div>
            </section>
            <section v-if="preview && resultItems.length" class="mp-price-dialog__saved-evidence" aria-label="Состав сохранённого набора">
              <h3 class="mp-price-dialog__section-title">Состав сохранённого набора</h3>
              <ol class="mp-price-dialog__review-list" data-price-saved-rows>
                <li
                  v-for="entry in resultItems"
                  :key="entry.row.id"
                  class="mp-price-dialog__review-row"
                  :data-price-review-row="entry.row.id"
                >
                  <div class="mp-price-dialog__review-product">
                    <img v-if="entry.row.photo" :src="entry.row.photo" alt="" loading="lazy" width="42" height="42">
                    <span v-else class="mp-price-dialog__photo-placeholder" aria-hidden="true"></span>
                    <span class="mp-price-dialog__review-title">
                      <strong>{{ entry.row.title || entry.row.article || 'Карточка товара' }}</strong>
                      <small>{{ entry.row.article || (isOzon ? 'Ozon' : 'WB') }}</small>
                    </span>
                    <span :class="['mp-price-dialog__status', rowStatusClass(entry)]">{{ rowStatus(entry) }}</span>
                  </div>
                  <div class="mp-price-dialog__review-values">
                    <div>
                      <span>{{ isOzon ? 'Цена продавца до заявки' : sourceLabel(entry.result) }}</span>
                      <strong>{{ formatMoney(oldPrice(entry), isOzon ? (entry.result.currencyCode || 'RUB') : 'RUB') }}</strong>
                      <small v-if="rowFreshness(entry)">{{ rowFreshness(entry) }}</small>
                    </div>
                    <span class="mp-price-dialog__arrow" aria-hidden="true">→</span>
                    <div>
                      <span>{{ isOzon ? 'Сохранённое предложение' : 'Цена в сохранённом пакете' }}</span>
                      <strong>{{ formatMoney(newPrice(entry), isOzon ? (entry.result.currencyCode || 'RUB') : 'RUB') }}</strong>
                      <small v-if="entry.result.safetyLevel">{{ safetyLabel(entry.result.safetyLevel) }}</small>
                    </div>
                  </div>
                  <p v-if="rowReason(entry)" class="mp-price-dialog__row-reason">{{ rowReason(entry) }}</p>
                </li>
              </ol>
            </section>
          </template>
        </div>

        <footer class="mp-price-dialog__footer">
          <div class="mp-price-dialog__footer-links">
            <button v-if="stage === 'edit' || stage === 'preview'" type="button" class="sh-btn sh-btn--ghost" data-price-legacy :disabled="!!busy" @click="openLegacy">
              Расширенное ценообразование
            </button>
          </div>
          <div class="mp-price-dialog__footer-actions">
            <button type="button" class="sh-btn sh-btn--secondary" :disabled="!!busy" @click="closeDialog(true)">
              {{ stage === 'receipt' ? 'Готово' : 'Закрыть' }}
            </button>
            <button
              v-if="stage === 'edit'"
              type="button"
              class="sh-btn sh-btn--primary"
              data-price-preview-action
              :disabled="!canPreview"
              @click="runPreview"
            >{{ busy === 'preview' ? 'Считаем…' : 'Проверить изменения' }}</button>
            <template v-else-if="stage === 'preview'">
              <button type="button" class="sh-btn sh-btn--secondary" :disabled="!!busy" @click="editAgain">Изменить расчёт</button>
              <button
                type="button"
                class="sh-btn sh-btn--primary"
                data-price-local-save
                :disabled="!canSave"
                @click="saveLocally"
              >{{ busy === 'save' ? 'Сохраняем локально…' : (isOzon ? 'Сохранить ценовые заявки' : 'Сохранить пакет цен') }}</button>
            </template>
            <button
              v-else-if="stage === 'unknown' && unknown?.retryAvailable"
              type="button"
              class="sh-btn sh-btn--primary"
              data-price-retry-exact
              :disabled="!!busy"
              @click="retryExactSave"
            >{{ busy === 'retry' ? 'Проверяем тот же запрос…' : 'Повторить только этот запрос' }}</button>
          </div>
        </footer>
      </div>
    </dialog>
  </Teleport>
</template>

<style scoped>
.mp-price-dialog {
  --mp-price-gap: 16px;
  display: flex;
  flex-direction: column;
  width: min(760px, calc(100vw - 24px));
  max-width: none;
  max-height: min(820px, calc(100dvh - 24px));
  margin: auto;
  padding: 0;
  overflow: hidden;
  color: var(--text);
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: 12px;
  box-shadow: 0 24px 80px rgb(0 0 0 / 28%);
}
.mp-price-dialog:not([open]) { display: none; }
.mp-price-dialog::backdrop { background: rgb(13 12 11 / 54%); backdrop-filter: blur(2px); }
.mp-price-dialog__shell { display: flex; flex: 1 1 auto; min-height: 0; flex-direction: column; }
.mp-price-dialog__header { display: flex; flex: none; align-items: flex-start; justify-content: space-between; gap: 16px; padding: 20px 24px 16px; border-bottom: 1px solid var(--border-light); }
.mp-price-dialog__heading-block { min-width: 0; }
.mp-price-dialog__context { display: flex; flex-wrap: wrap; align-items: center; gap: 7px; margin-bottom: 6px; color: var(--text-secondary); font-size: 12px; line-height: 1.45; }
.mp-price-dialog__market { display: inline-flex; min-height: 22px; align-items: center; padding: 2px 8px; border: 1px solid var(--border); border-radius: 6px; color: var(--text); font-weight: 650; }
.mp-price-dialog__market.is-ozon { border-color: var(--accent); color: var(--accent-text); }
.mp-price-dialog__heading-block h2 { margin: 0; color: var(--text); font-size: 22px; font-weight: 620; line-height: 1.25; letter-spacing: -.025em; }
.mp-price-dialog__heading-block p { margin: 5px 0 0; color: var(--text-secondary); font-size: 13px; line-height: 1.45; }
.mp-price-dialog__close { flex: none; width: 44px; min-height: 44px; padding: 0; font-size: 22px; line-height: 1; }
.mp-price-dialog__body { flex: 1 1 auto; min-height: 0; overflow: auto; overscroll-behavior: contain; padding: 18px 24px 22px; }
.mp-price-dialog__alert { margin: 0 0 14px; }
.mp-price-dialog__scope { margin-bottom: 18px; padding: 12px; background: var(--bg); border: 1px solid var(--border); border-radius: 8px; }
.mp-price-dialog__scope-rows { display: grid; gap: 6px; }
.mp-price-dialog__scope-row { display: grid; min-width: 0; grid-template-columns: 36px minmax(0,1fr) minmax(60px,auto); align-items: center; gap: 9px; }
.mp-price-dialog__scope-row img, .mp-price-dialog__review-product img, .mp-price-dialog__photo-placeholder { width: 36px; height: 36px; border-radius: 6px; object-fit: cover; background: var(--bg-hover); }
.mp-price-dialog__scope-title { overflow: hidden; color: var(--text); font-size: 12px; text-overflow: ellipsis; white-space: nowrap; }
.mp-price-dialog__scope-article { max-width: 140px; overflow: hidden; color: var(--text-muted); font-size: 11px; text-align: right; text-overflow: ellipsis; white-space: nowrap; }
.mp-price-dialog__more { margin: 2px 0 0 45px; color: var(--text-secondary); font-size: 12px; }
.mp-price-dialog__form { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; }
.mp-price-dialog__field { display: grid; min-width: 0; gap: 6px; color: var(--text); font-size: 13px; font-weight: 540; }
.mp-price-dialog__field--wide { grid-column: 1 / -1; }
.mp-price-dialog__field select, .mp-price-dialog__field input, .mp-price-dialog__field textarea { box-sizing: border-box; width: 100%; min-height: 44px; padding: 9px 11px; color: var(--text); background: var(--bg-card); border: 1px solid var(--border); border-radius: 7px; font: inherit; font-weight: 400; }
.mp-price-dialog__field textarea { min-height: 62px; resize: vertical; }
.mp-price-dialog__field select:focus-visible, .mp-price-dialog__field input:focus-visible, .mp-price-dialog__field textarea:focus-visible, .mp-price-dialog__guardrails input:focus-visible { outline: 2px solid var(--focus-outline); outline-offset: 2px; }
.mp-price-dialog__field input:disabled, .mp-price-dialog__field select:disabled, .mp-price-dialog__field textarea:disabled { opacity: .62; }
.mp-price-dialog__amount-wrap { position: relative; display: block; }
.mp-price-dialog__amount-wrap input { padding-right: 38px; font-size: 18px; font-variant-numeric: tabular-nums; }
.mp-price-dialog__amount-wrap > span { position: absolute; top: 50%; right: 12px; color: var(--text-secondary); transform: translateY(-50%); }
.mp-price-dialog__hint { margin: 0; color: var(--text-secondary); font-size: 12px; line-height: 1.55; }
.mp-price-dialog__advanced { grid-column: 1 / -1; border: 1px solid var(--border); border-radius: 8px; }
.mp-price-dialog__advanced > summary { display: flex; min-height: 44px; align-items: center; padding: 0 13px; color: var(--text); cursor: pointer; font-size: 13px; font-weight: 580; }
.mp-price-dialog__advanced[open] > summary { border-bottom: 1px solid var(--border-light); }
.mp-price-dialog__guardrails { display: grid; gap: 5px; margin: 0; padding: 8px 13px 13px; border: 0; }
.mp-price-dialog__guardrails > label:not(.mp-price-dialog__field) { display: flex; min-height: 44px; align-items: center; gap: 9px; color: var(--text); font-size: 13px; }
.mp-price-dialog__guardrails input[type="checkbox"] { width: 18px; height: 18px; accent-color: var(--accent-strong); }
.mp-price-dialog__summary { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 18px; }
.mp-price-dialog__summary > div { display: flex; min-width: 84px; flex-direction: column; gap: 2px; padding: 9px 11px; background: var(--bg); border: 1px solid var(--border-light); border-radius: 7px; }
.mp-price-dialog__summary strong { color: var(--text); font-size: 16px; font-variant-numeric: tabular-nums; }
.mp-price-dialog__summary span { color: var(--text-secondary); font-size: 11px; }
.mp-price-dialog__section-title { margin: 0 0 9px; color: var(--text); font-size: 14px; font-weight: 620; }
.mp-price-dialog__eligible-note { margin: -8px 0 16px; color: var(--text-secondary); font-size: 12px; line-height: 1.45; }
.mp-price-dialog__buyer-unknown-note { margin: 5px 0 0 52px; color: var(--text-secondary); font-size: 11px; line-height: 1.4; }
.mp-price-dialog__review-list { display: grid; gap: 8px; margin: 0; padding: 0; list-style: none; }
.mp-price-dialog__review-row { min-width: 0; padding: 11px 12px; border: 1px solid var(--border); border-radius: 8px; }
.mp-price-dialog__review-product { display: grid; grid-template-columns: 42px minmax(0,1fr) auto; align-items: center; gap: 10px; }
.mp-price-dialog__review-product img, .mp-price-dialog__review-product .mp-price-dialog__photo-placeholder { width: 42px; height: 42px; }
.mp-price-dialog__review-title { display: grid; min-width: 0; gap: 3px; }
.mp-price-dialog__review-title strong { overflow: hidden; color: var(--text); font-size: 13px; font-weight: 580; text-overflow: ellipsis; white-space: nowrap; }
.mp-price-dialog__review-title small { color: var(--text-muted); font-size: 11px; }
.mp-price-dialog__status { display: inline-flex; min-height: 24px; align-items: center; padding: 2px 7px; border-radius: 5px; font-size: 11px; white-space: nowrap; }
.mp-price-dialog__status.is-ready { color: var(--ok); background: var(--ok-bg); }
.mp-price-dialog__status.is-warning { color: var(--warn); background: var(--warn-bg); }
.mp-price-dialog__status.is-muted { color: var(--text-secondary); background: var(--bg-hover); }
.mp-price-dialog__review-values { display: grid; grid-template-columns: minmax(0,1fr) 28px minmax(0,1fr); align-items: center; gap: 7px; margin: 10px 0 0 52px; }
.mp-price-dialog__review-values > div { display: grid; min-width: 0; gap: 3px; }
.mp-price-dialog__review-values > div:last-child { text-align: right; }
.mp-price-dialog__review-values span:not(.mp-price-dialog__arrow) { color: var(--text-secondary); font-size: 11px; }
.mp-price-dialog__review-values strong { overflow-wrap: anywhere; color: var(--text); font-size: 14px; font-variant-numeric: tabular-nums; }
.mp-price-dialog__review-values small { color: var(--text-muted); font-size: 10px; }
.mp-price-dialog__arrow { color: var(--text-muted); text-align: center; }
.mp-price-dialog__row-reason { margin: 8px 0 0 52px; color: var(--warn); font-size: 12px; line-height: 1.45; }
.mp-price-dialog__local-only { margin: 14px 0 0; padding: 10px 12px; color: var(--text-secondary); background: var(--bg); border-radius: 7px; font-size: 12px; line-height: 1.5; }
.mp-price-dialog__uncertain, .mp-price-dialog__receipt { display: grid; grid-template-columns: 42px minmax(0,1fr); align-items: start; gap: 13px; padding: 18px; background: var(--bg); border: 1px solid var(--border); border-radius: 9px; }
.mp-price-dialog__uncertain-mark, .mp-price-dialog__receipt-mark { display: grid; width: 38px; height: 38px; place-items: center; border-radius: 50%; color: var(--warn); background: var(--warn-bg); font-size: 20px; font-weight: 700; }
.mp-price-dialog__receipt-mark { color: var(--ok); background: var(--ok-bg); }
.mp-price-dialog__uncertain h3, .mp-price-dialog__receipt h3 { margin: 0; color: var(--text); font-size: 16px; font-weight: 640; line-height: 1.35; }
.mp-price-dialog__uncertain p, .mp-price-dialog__receipt p { margin: 6px 0 0; color: var(--text-secondary); font-size: 13px; line-height: 1.5; }
.mp-price-dialog__destination { display: inline-flex; min-height: 44px; align-items: center; margin-top: 14px; text-decoration: none; }
.mp-price-dialog__footer { display: flex; flex: none; align-items: center; justify-content: space-between; gap: 12px; padding: 13px 24px; border-top: 1px solid var(--border-light); background: var(--bg-card); }
.mp-price-dialog__footer-links, .mp-price-dialog__footer-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
.mp-price-dialog__footer .sh-btn { min-height: 44px; }
.mp-price-dialog__footer .sh-btn--ghost { padding-right: 8px; padding-left: 8px; color: var(--text-secondary); font-size: 12px; }

@media (max-width: 600px) {
  .mp-price-dialog { width: calc(100vw - 12px); max-height: calc(100dvh - 12px); border-radius: 10px; }
  .mp-price-dialog__header { gap: 10px; padding: 15px 15px 13px; }
  .mp-price-dialog__heading-block h2 { font-size: 19px; }
  .mp-price-dialog__heading-block p { font-size: 12px; }
  .mp-price-dialog__body { padding: 13px 14px 17px; }
  .mp-price-dialog__form { grid-template-columns: 1fr; gap: 12px; }
  .mp-price-dialog__field--wide { grid-column: auto; }
  .mp-price-dialog__footer { align-items: stretch; flex-direction: column; gap: 4px; padding: 10px 14px max(10px, env(safe-area-inset-bottom)); }
  .mp-price-dialog__footer-actions { display: grid; grid-template-columns: 1fr 1fr; }
  .mp-price-dialog__footer-actions .sh-btn { min-width: 0; padding-right: 8px; padding-left: 8px; font-size: 12px; }
  .mp-price-dialog__footer-actions [data-price-local-save], .mp-price-dialog__footer-actions [data-price-preview-action], .mp-price-dialog__footer-actions [data-price-retry-exact] { grid-column: 1 / -1; }
  .mp-price-dialog__footer-links .sh-btn { min-height: 44px; }
  .mp-price-dialog__review-product { grid-template-columns: 38px minmax(0,1fr) auto; gap: 8px; }
  .mp-price-dialog__review-product img, .mp-price-dialog__review-product .mp-price-dialog__photo-placeholder { width: 38px; height: 38px; }
  .mp-price-dialog__review-values { margin-left: 46px; }
  .mp-price-dialog__row-reason { margin-left: 46px; }
  .mp-price-dialog__status { padding: 2px 5px; font-size: 10px; }
}

@media (prefers-reduced-motion: reduce) {
  .mp-price-dialog, .mp-price-dialog * { scroll-behavior: auto !important; }
}
</style>
