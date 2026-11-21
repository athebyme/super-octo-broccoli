(() => {
  'use strict';

  const panel = document.getElementById('catalog-refresh-status');
  if (!panel) return;

  const headline = panel.querySelector('[data-testid="catalog-refresh-headline"]');
  const detail = panel.querySelector('[data-refresh-detail]');
  const counts = panel.querySelector('[data-testid="catalog-refresh-counts"]');
  const price = panel.querySelector('[data-testid="catalog-refresh-price"]');
  const submit = document.querySelector('[data-testid="catalog-refresh-submit"]');
  const copyElement = panel.querySelector('[data-refresh-copy]');
  const priceLabelsElement = panel.querySelector('[data-refresh-price-labels]');
  const errorCopyElement = panel.querySelector('[data-refresh-error-copy]');
  const ACTIVE = new Set(['pending', 'running']);
  const TERMINAL = new Set(['completed', 'partial', 'failed', 'interrupted']);
  const MAX_POLLS = 90;
  const MAX_DURATION_MS = 12 * 60 * 1000;
  const FETCH_TIMEOUT_MS = 8000;

  function readJson(element) {
    try {
      return JSON.parse(element?.textContent || '{}');
    } catch (_error) {
      return {};
    }
  }

  const copy = readJson(copyElement);
  const priceLabels = readJson(priceLabelsElement);
  const errorCopy = readJson(errorCopyElement);

  function copyKey(status, rawCommitted) {
    if (status === 'running') return rawCommitted ? 'running_after' : 'running_before';
    if (status === 'interrupted') return rawCommitted ? 'interrupted_after' : 'interrupted_before';
    return status;
  }

  function writeCopy(key) {
    const value = copy[key] || copy.unknown || {};
    if (headline) headline.textContent = value.headline || 'Не удалось подтвердить состояние.';
    if (detail) detail.textContent = value.detail || 'Обновите страницу, чтобы проверить его снова.';
  }

  function safeCount(value) {
    return Number.isSafeInteger(value) && value >= 0 ? value : null;
  }

  function setField(name, value) {
    const field = panel.querySelector(`[data-refresh-field="${name}"]`);
    if (field) field.textContent = value == null ? '' : String(value);
  }

  function clearDetails() {
    if (counts) counts.hidden = true;
    ['total', 'added', 'updated', 'postprocess-errors'].forEach((name) => setField(name, ''));
    const postprocessWrap = panel.querySelector('[data-refresh-field-wrap="postprocess-errors"]');
    if (postprocessWrap) postprocessWrap.hidden = true;
    if (price) price.hidden = true;
    setField('price-status', '');
    setField('price-conflicts', '');
    const conflictsWrap = panel.querySelector('[data-refresh-conflicts-wrap]');
    if (conflictsWrap) conflictsWrap.hidden = true;
  }

  function updateButtonState(status, runId) {
    if (!submit) return;
    const latestRunId = panel.dataset.refreshLatestRunId || '';
    const isLatest = Boolean(runId && latestRunId && runId === latestRunId);
    if (isLatest) {
      const active = ACTIVE.has(status);
      panel.dataset.refreshLatestActive = String(active);
      submit.disabled = active;
    } else {
      submit.disabled = panel.dataset.refreshLatestActive === 'true';
    }
  }

  function applyPayload(payload) {
    const runId = panel.dataset.refreshRunId || '';
    if (!payload || payload.id !== runId || !runId || !ACTIVE.has(payload.status) && !TERMINAL.has(payload.status)) {
      return false;
    }

    const rawCommitted = typeof payload.raw_committed_at === 'string' && payload.raw_committed_at.length > 0;
    panel.dataset.refreshState = payload.status;
    panel.dataset.refreshRawCommitted = String(rawCommitted);
    const key = copyKey(payload.status, rawCommitted);
    writeCopy(key);
    if (TERMINAL.has(payload.status) && errorCopy[payload.error_code] && detail) {
      detail.textContent = errorCopy[payload.error_code];
    }
    clearDetails();

    const total = safeCount(payload.total);
    const added = safeCount(payload.added);
    const updated = safeCount(payload.updated);
    if (rawCommitted && total !== null && added !== null && updated !== null && counts) {
      setField('total', total);
      setField('added', added);
      setField('updated', updated);
      counts.hidden = false;
      const postprocessErrors = TERMINAL.has(payload.status)
        ? safeCount(payload.postprocess_errors)
        : null;
      const postprocessWrap = panel.querySelector('[data-refresh-field-wrap="postprocess-errors"]');
      if (postprocessWrap && postprocessErrors !== null) {
        setField('postprocess-errors', postprocessErrors);
        postprocessWrap.hidden = false;
      }
    }

    if (
      rawCommitted && TERMINAL.has(payload.status)
      && Object.prototype.hasOwnProperty.call(priceLabels, payload.price_status)
      && price
    ) {
      setField('price-status', priceLabels[payload.price_status]);
      const conflicts = safeCount(payload.price_conflicts);
      const conflictsWrap = panel.querySelector('[data-refresh-conflicts-wrap]');
      if (conflictsWrap && conflicts !== null) {
        setField('price-conflicts', conflicts);
        conflictsWrap.hidden = false;
      }
      price.hidden = false;
    }

    updateButtonState(payload.status, runId);
    return true;
  }

  function stopUnverified(sessionExpired) {
    panel.dataset.refreshState = sessionExpired ? 'session' : 'unknown';
    panel.dataset.refreshRunId = '';
    panel.dataset.refreshRawCommitted = 'false';
    panel.dataset.refreshStatusUrl = '';
    writeCopy(sessionExpired ? 'session' : 'unknown');
    clearDetails();
    if (submit) {
      submit.disabled = panel.dataset.refreshLatestActive === 'true';
    }
  }

  function waitUntilVisible() {
    if (!document.hidden) return Promise.resolve(true);
    return new Promise((resolve) => {
      const onChange = () => {
        if (document.hidden) return;
        document.removeEventListener('visibilitychange', onChange);
        resolve(true);
      };
      document.addEventListener('visibilitychange', onChange);
    });
  }

  async function poll() {
    const runId = panel.dataset.refreshRunId || '';
    const statusUrl = panel.dataset.refreshStatusUrl || '';
    if (!runId || !statusUrl || !ACTIVE.has(panel.dataset.refreshState)) return;

    const startedAt = Date.now();
    let delay = 650;
    let polls = 0;
    while (polls < MAX_POLLS && Date.now() - startedAt < MAX_DURATION_MS) {
      await waitUntilVisible();
      if (!ACTIVE.has(panel.dataset.refreshState) || panel.dataset.refreshRunId !== runId) return;
      await new Promise((resolve) => window.setTimeout(resolve, delay));
      if (document.hidden) continue;
      if (Date.now() - startedAt >= MAX_DURATION_MS) break;
      polls += 1;

      const controller = new AbortController();
      const timeout = window.setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS);
      try {
        const response = await fetch(statusUrl, {
          method: 'GET',
          credentials: 'same-origin',
          cache: 'no-store',
          headers: { Accept: 'application/json' },
          signal: controller.signal,
        });
        if (response.status === 401 || response.status === 403 || response.redirected) {
          stopUnverified(true);
          return;
        }
        if (response.status === 404 || response.status === 400) {
          stopUnverified(false);
          return;
        }
        if (response.status >= 500) {
          delay = Math.min(delay * 1.5, 10000);
          continue;
        }
        if (!response.ok || !response.headers.get('content-type')?.includes('application/json')) {
          stopUnverified(true);
          return;
        }
        const payload = await response.json();
        if (!applyPayload(payload)) {
          stopUnverified(false);
          return;
        }
        if (TERMINAL.has(payload.status)) return;
        delay = Math.min(delay * 1.5, 10000);
      } catch (_error) {
        if (document.hidden) continue;
        delay = Math.min(delay * 1.5, 10000);
      } finally {
        window.clearTimeout(timeout);
      }
    }

    if (ACTIVE.has(panel.dataset.refreshState)) stopUnverified(false);
  }

  poll();
})();
