(() => {
  'use strict';

  const form = document.querySelector('[data-supplier-update-review-form]');
  const panel = document.querySelector('[data-supplier-update-inline-review]');
  if (!form || !panel) return;

  const heading = panel.querySelector('#supplier-update-inline-title');
  const status = panel.querySelector('[data-supplier-update-state]');
  const summary = panel.querySelector('[data-supplier-update-summary]');
  const items = panel.querySelector('[data-supplier-update-items]');
  const applyButton = panel.querySelector('[data-supplier-update-apply]');
  const recheckButton = panel.querySelector('[data-supplier-update-recheck]');
  const closeButton = panel.querySelector('[data-supplier-update-close]');
  const previewButton = document.querySelector('[data-supplier-update-preview]');
  const FIELD_LABELS = {
    title: 'Название',
    description: 'Описание',
    photos: 'Фотографии',
    characteristics: 'Характеристики',
    category: 'Категория',
    product_details: 'Данные карточки',
    purchase_price: 'Закупочная цена',
    supplier_quantity: 'Остаток у поставщика',
  };
  const SUMMARY_CHANGE_COPY = {
    description: 'Описание изменится.',
    photos: 'Набор фотографий изменится.',
    characteristics: 'Значения характеристик изменятся.',
    category: 'Категория и её параметры изменятся.',
    product_details: 'Дополнительные данные карточки изменятся.',
    source_snapshot: 'Данные каталога обновятся.',
  };
  const REASON_LABELS = {
    no_supplier_link: 'Нет связи с каталогом поставщика',
    already_current: 'Данные уже совпадают',
    update_failed: 'Не удалось обновить эту карточку',
  };

  let trigger = null;
  let scopeSnapshot = null;
  let reviewToken = null;
  let previewById = new Map();
  let requestInFlight = false;
  let frozenControls = [];
  let selectionLocked = false;
  let previewWasDisabled = false;
  let phase = 'closed';
  let unknownOutcome = false;

  function setPhase(value) {
    phase = value;
    panel.dataset.phase = value;
  }

  function cloneFormData(source) {
    const copy = new FormData();
    for (const [key, value] of source.entries()) copy.append(key, value);
    return copy;
  }

  function safeCount(value) {
    return Number.isSafeInteger(value) && value >= 0 ? value : null;
  }

  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  function text(node, value) {
    node.textContent = value == null ? '' : String(value);
  }

  function valueText(value) {
    if (value == null || value === '') return '—';
    if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') {
      return String(value);
    }
    if (Array.isArray(value)) return value.map(valueText).join(', ');
    return 'Содержимое изменится';
  }

  function hasDisplayValue(value) {
    return value !== null && value !== undefined && value !== ''
      && (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean');
  }

  function lockSelection(locked) {
    if (locked) {
      if (selectionLocked) return;
      selectionLocked = true;
      frozenControls = Array.from(document.querySelectorAll(
        '#select-all-updates, input[data-selection-kind="update"]',
      )).map((control) => ({ control, disabled: control.disabled }));
      frozenControls.forEach(({ control }) => { control.disabled = true; });
      return;
    }
    selectionLocked = false;
    frozenControls.forEach(({ control, disabled }) => {
      control.disabled = disabled;
    });
    frozenControls = [];
  }

  function openPanel(sourceTrigger) {
    trigger = sourceTrigger || previewButton;
    previewWasDisabled = Boolean(previewButton && previewButton.disabled);
    if (previewButton) previewButton.disabled = true;
    panel.hidden = false;
    panel.setAttribute('aria-busy', 'false');
    lockSelection(true);
    if (heading) {
      heading.focus({ preventScroll: true });
      panel.scrollIntoView({ block: 'start', behavior: 'smooth' });
    }
  }

  function closePanel() {
    panel.hidden = true;
    panel.setAttribute('aria-busy', 'false');
    scopeSnapshot = null;
    reviewToken = null;
    previewById = new Map();
    setPhase('closed');
    lockSelection(false);
    if (previewButton) previewButton.disabled = previewWasDisabled;
    if (trigger && !trigger.disabled && typeof trigger.focus === 'function') {
      trigger.focus({ preventScroll: true });
    }
  }

  function setBusy(busy) {
    requestInFlight = busy;
    panel.setAttribute('aria-busy', String(busy));
    if (applyButton) applyButton.disabled = busy;
    if (recheckButton) recheckButton.disabled = busy;
    if (closeButton) closeButton.disabled = busy;
  }

  function appendCount(dl, label, value) {
    const row = document.createElement('div');
    const term = document.createElement('dt');
    const detail = document.createElement('dd');
    text(term, label);
    text(detail, value == null ? '—' : value);
    row.append(term, detail);
    dl.appendChild(row);
  }

  function renderCounts(values) {
    clear(summary);
    const dl = document.createElement('dl');
    values.forEach(([label, value]) => appendCount(dl, label, value));
    summary.appendChild(dl);
  }

  function renderPreviewRows(payload) {
    clear(items);
    previewById = new Map();
    const rows = Array.isArray(payload.items) ? payload.items : [];
    const title = document.createElement('h3');
    text(title, payload.items_truncated
      ? `Первые ${rows.length} карточек`
      : 'Карточки и изменения');
    items.appendChild(title);

    rows.forEach((item) => {
      if (!item || typeof item.product_id !== 'string' || typeof item.label !== 'string') return;
      previewById.set(item.product_id, item);
      const article = document.createElement('article');
      article.className = 'sc-inline-review-row';
      article.dataset.supplierReviewRow = '';
      article.dataset.localProductId = item.product_id;
      article.dataset.rowState = item.state || 'unknown';

      const rowHeading = document.createElement('div');
      rowHeading.className = 'sc-inline-review-row-heading';
      const name = document.createElement('h4');
      text(name, item.label);
      const badge = document.createElement('span');
      badge.className = 'sc-inline-review-state';
      text(badge, item.state === 'changed'
        ? 'Изменится'
        : item.state === 'unchanged' ? 'Без изменений' : 'Нет связи');
      rowHeading.append(name, badge);
      article.appendChild(rowHeading);

      const allChanges = Array.isArray(item.changes) ? item.changes : [];
      let changes = allChanges.filter((change) => change.field !== 'source_snapshot');
      if (!changes.length && item.state === 'changed'
          && allChanges.some((change) => change.field === 'source_snapshot')) {
        changes = allChanges.filter((change) => change.field === 'source_snapshot');
      }
      if (changes.length) {
        const list = document.createElement('ul');
        changes.forEach((change) => {
          const entry = document.createElement('li');
          if (hasDisplayValue(change.before) || hasDisplayValue(change.after)) {
            const field = document.createElement('strong');
            text(field, `${FIELD_LABELS[change.field] || 'Данные карточки'}: `);
            const before = document.createElement('span');
            before.className = 'sc-inline-review-before';
            text(before, hasDisplayValue(change.before) ? valueText(change.before) : '—');
            const arrow = document.createElement('span');
            arrow.className = 'sc-inline-review-arrow';
            text(arrow, ' → ');
            const after = document.createElement('span');
            after.className = 'sc-inline-review-after';
            text(after, hasDisplayValue(change.after) ? valueText(change.after) : '—');
            entry.append(field, before, arrow, after);
          } else {
            text(entry, SUMMARY_CHANGE_COPY[change.field] || 'Содержимое группы изменится.');
          }
          list.appendChild(entry);
        });
        article.appendChild(list);
      } else if (item.state === 'unlinked') {
        const note = document.createElement('p');
        text(note, 'Нет связи с локальной карточкой.');
        article.appendChild(note);
      } else if (item.state === 'unchanged') {
        const note = document.createElement('p');
        text(note, 'Сохранённые данные уже совпадают с карточкой.');
        article.appendChild(note);
      }
      items.appendChild(article);
    });

    if (payload.items_truncated) {
      const note = document.createElement('p');
      note.className = 'sc-inline-review-truncated';
      text(note, `Показаны первые ${rows.length} из ${payload.requested} локальных карточек. Счётчики включают весь выбранный набор.`);
      items.appendChild(note);
    }
  }

  function responseMessage(payload, fallback) {
    return payload && typeof payload.error === 'string' ? payload.error : fallback;
  }

  async function postForm(url, data, timeoutMs) {
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await fetch(url, {
        method: 'POST',
        body: data,
        credentials: 'same-origin',
        cache: 'no-store',
        headers: { Accept: 'application/json' },
        signal: controller.signal,
      });
      const contentType = response.headers.get('content-type') || '';
      if (response.redirected || !contentType.includes('application/json')) {
        throw new Error('Ответ сервера не удалось проверить.');
      }
      return { response, payload: await response.json() };
    } finally {
      window.clearTimeout(timeout);
    }
  }

  async function requestPreview({ afterUnknown = false } = {}) {
    if (requestInFlight || !scopeSnapshot) return;
    afterUnknown = afterUnknown || unknownOutcome;
    setPhase('previewing');
    reviewToken = null;
    applyButton.hidden = true;
    recheckButton.hidden = true;
    text(status, afterUnknown
      ? 'Сверяю текущие данные. Это не повторяет запрос сохранения.'
      : 'Сверяю выбранные товары с каталогом поставщика…');
    setBusy(true);
    try {
      const { response, payload } = await postForm(
        form.action,
        cloneFormData(scopeSnapshot),
        30000,
      );
      if (!response.ok || !payload || payload.success !== true) {
        setPhase(afterUnknown ? 'unknown' : 'preview-error');
        text(status, afterUnknown
          ? 'Текущее состояние проверить не удалось. Результат прежнего сохранения всё ещё не подтверждён.'
          : responseMessage(payload, 'Не удалось проверить выбранный набор.'));
        recheckButton.hidden = false;
        text(recheckButton, afterUnknown ? 'Проверить текущее состояние' : 'Повторить проверку');
        return;
      }

      setPhase(afterUnknown ? 'rechecked' : 'ready');
      reviewToken = typeof payload.review_token === 'string' ? payload.review_token : null;
      const requested = safeCount(payload.requested);
      const eligible = safeCount(payload.eligible);
      const changed = safeCount(payload.changed);
      const skipped = safeCount(payload.skipped);
      const sourceCount = safeCount(payload.source_requested);
      renderCounts([
        ['Выбрано товаров каталога', sourceCount == null ? 'Весь поставщик' : sourceCount],
        ['Проверено локальных карточек', requested],
        ['Связано', eligible],
        ['Изменится', changed],
        ['Без изменений или связи', skipped],
      ]);
      renderPreviewRows(payload);

      if (afterUnknown) {
        text(status, changed === 0
          ? 'Текущие карточки совпадают с каталогом. Это сверка состояния, а не подтверждение прошлого запроса.'
          : 'Текущее сравнение показывает изменения. Для сохранения требуется новое явное подтверждение.');
      } else {
        text(status, changed > 0
          ? 'Проверьте набор. Изменения сохранятся в Seller Hub, без отправки в WB или Ozon.'
          : 'Изменений нет. Сохранять нечего.');
      }
      applyButton.hidden = !(changed > 0 && reviewToken);
      applyButton.disabled = false;
      text(applyButton, changed == null
        ? 'Сохранить выбранные изменения'
        : `Сохранить изменения (${changed})`);
      recheckButton.hidden = false;
      text(recheckButton, afterUnknown ? 'Проверить ещё раз' : 'Перепроверить данные');
    } catch (_error) {
      setPhase(afterUnknown ? 'unknown' : 'preview-error');
      reviewToken = null;
      unknownOutcome = afterUnknown;
      text(status, afterUnknown
        ? 'Текущее состояние проверить не удалось. Результат прежнего сохранения всё ещё не подтверждён.'
        : 'Проверка не выполнилась; этот запрос не меняет локальные карточки. Попробуйте проверить ещё раз.');
      recheckButton.hidden = false;
      text(recheckButton, afterUnknown ? 'Проверить текущее состояние' : 'Повторить проверку');
    } finally {
      setBusy(false);
    }
  }

  function renderResult(payload) {
    setPhase('completed');
    reviewToken = null;
    applyButton.hidden = true;
    const requested = safeCount(payload.requested);
    const updated = safeCount(payload.updated);
    const skipped = safeCount(payload.skipped);
    const errors = safeCount(payload.errors);
    renderCounts([
      ['Запрошено локальных карточек', requested],
      ['Обновлено', updated],
      ['Пропущено', skipped],
      ['Ошибок', errors],
    ]);
    clear(items);
    const rows = Array.isArray(payload.items) ? payload.items : [];
    const title = document.createElement('h3');
    text(title, payload.items_truncated
      ? `Первые ${rows.length} результатов`
      : 'Результаты по локальным карточкам');
    items.appendChild(title);
    rows.forEach((item) => {
      if (!item || typeof item.product_id !== 'string') return;
      const prior = previewById.get(item.product_id);
      const article = document.createElement('article');
      article.className = 'sc-inline-review-row';
      article.dataset.supplierReviewRow = '';
      article.dataset.localProductId = item.product_id;
      article.dataset.rowState = item.status || 'unknown';
      const name = document.createElement('h4');
      text(name, prior && typeof prior.label === 'string'
        ? prior.label
        : `Локальная карточка ${item.product_id}`);
      const resultText = document.createElement('p');
      const statusCopy = item.status === 'updated'
        ? 'Обновлено'
        : item.status === 'skipped' ? 'Пропущено' : 'Ошибка';
      const reasonCopy = REASON_LABELS[item.reason] || '';
      text(resultText, reasonCopy ? `${statusCopy}: ${reasonCopy}` : statusCopy);
      article.append(name, resultText);
      items.appendChild(article);
    });
    text(status, payload.status === 'completed_with_errors'
      ? `Обновление завершено с ошибками: локально обновлено ${updated}, пропущено ${skipped}, ошибок ${errors}.`
      : `Локально обновлено ${updated} из ${requested}. WB и Ozon не менялись.`);
    recheckButton.hidden = false;
    text(recheckButton, 'Проверить оставшиеся изменения');
  }

  async function applyReview() {
    if (requestInFlight || !scopeSnapshot || !reviewToken) return;
    setPhase('applying');
    const data = cloneFormData(scopeSnapshot);
    data.set('review_token', reviewToken);
    applyButton.hidden = true;
    recheckButton.hidden = true;
    text(status, 'Сохраняю проверенные изменения в Seller Hub…');
    setBusy(true);
    try {
      const { response, payload } = await postForm(
        form.dataset.confirmUrl,
        data,
        60000,
      );
      if (
        response.ok && payload && payload.success === true
        && ['completed', 'completed_with_errors'].includes(payload.status)
      ) {
        unknownOutcome = false;
        renderResult(payload);
        return;
      }

      reviewToken = null;
      if (response.status >= 500 || response.ok || !payload) {
        setPhase('unknown');
        unknownOutcome = true;
        text(status, 'Не удалось подтвердить результат сохранения. Проверьте текущее состояние перед любым новым сохранением.');
        recheckButton.hidden = false;
        text(recheckButton, 'Проверить текущее состояние');
      } else {
        setPhase('stale');
        text(status, responseMessage(
          payload,
          'Проверка устарела или выбор изменился. Текущие данные не сохранены; запустите новую проверку.',
        ));
        recheckButton.hidden = false;
        text(recheckButton, 'Проверить изменения заново');
      }
    } catch (_error) {
      reviewToken = null;
      setPhase('unknown');
      unknownOutcome = true;
      text(status, 'Результат сохранения неизвестен. Ничего не отправляю повторно. Сначала проверьте текущее состояние.');
      recheckButton.hidden = false;
      text(recheckButton, 'Проверить текущее состояние');
    } finally {
      setBusy(false);
      if (heading) heading.focus({ preventScroll: true });
    }
  }

  form.addEventListener('submit', (event) => {
    event.preventDefault();
    if (requestInFlight || phase !== 'closed') return;
    scopeSnapshot = new FormData(form);
    reviewToken = null;
    openPanel(event.submitter || previewButton);
    requestPreview();
  });

  applyButton.addEventListener('click', applyReview);
  recheckButton.addEventListener('click', () => {
    if (phase === 'unknown' || phase === 'stale') requestPreview({ afterUnknown: phase === 'unknown' });
    else requestPreview();
  });
  closeButton.addEventListener('click', closePanel);
  document.addEventListener('keydown', (event) => {
    if (!panel.hidden && event.key === 'Escape' && !requestInFlight) {
      event.preventDefault();
      closePanel();
    }
  });
})();
