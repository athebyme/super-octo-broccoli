(function (global) {
  'use strict';
  const bootstrap = document.getElementById('oq-bootstrap');
  if (!bootstrap || !global.Vue) return;
  const config = JSON.parse(bootstrap.textContent);
  const kinds = {
    product_import: 'Создание карточки', product_import_rollback: 'Откат создания карточки',
    product_update: 'Обновление карточки', product_update_rollback: 'Откат обновления карточки',
    price_update: 'Изменение цены', price_rollback: 'Откат цены',
    stock_update: 'Изменение остатков', stock_rollback: 'Откат остатков'
  };
  const outcomes = {
    uncertain: 'Результат неизвестен', succeeded: 'Подтверждён успешно',
    failed: 'Подтверждена ошибка', partial: 'Частичный результат',
    queued: 'Ожидает отправки', submitting: 'Отправляется',
    submitted: 'Ожидает сверки', polling: 'Проверяется', cancelled: 'Отменена'
  };
  const events = {placed: 'Остановка новых изменений', note_added: 'Пояснение', released: 'Остановка снята'};
  let controller;
  let beforeUnloadHandler;
  function validReview(body) {
    const row = body?.review;
    if (body?.success !== true || row?.operation_id !== config.operationId || row?.account?.id !== config.accountId ||
        !Number.isSafeInteger(row.operation_version) || row.operation_version <= 0 ||
        !['product', 'account'].includes(row.scope?.kind) || !Array.isArray(row.events) ||
        (row.hold && (!Number.isSafeInteger(row.hold.version) || row.hold.version <= 0))) {
      throw Error('Не удалось подтвердить операцию и просмотренную область. Повторите чтение.');
    }
    return row;
  }
  const app = global.Vue.createApp({
    data() { return {
      review: null, pendingReview: null, events: [], nextBeforeId: null, loading: false, moreLoading: false,
      saving: false, needsCheck: false, sessionEnded: false, error: '', reason: '', confirmed: false,
      action: 'note_added', operationUrl: config.operationUrl, accountUrl: config.accountUrl,
      loginUrl: config.loginUrl
    }; },
    computed: {
      outcomeMessage() {
        if (this.review?.outcome === 'uncertain') return 'Ozon пока не подтвердил результат отправки. Здесь можно остановить новые изменения и сохранить заметку о проверке.';
        return 'Результат этой операции и решение об остановке хранятся отдельно.';
      }
    },
    methods: {
      kindLabel(value) { return kinds[value] || 'Операция Ozon'; },
      outcomeLabel(value, attempts = this.review?.attempt_count) { if (value === 'failed' && attempts === 0) return 'Не отправлено'; return outcomes[value] || 'Статус не определён'; },
      safeReviewUrl(value) { return typeof value === 'string' && /^\/marketplaces\/operations\/[1-9]\d*\/review$/.test(value) ? value : null; },
      eventLabel(value) { return events[value] || 'Решение'; },
      date(value) { if (!value) return 'Нет наблюдения'; const parsed = Date.parse(value); return Number.isFinite(parsed) ? new Intl.DateTimeFormat('ru-RU', {dateStyle:'medium',timeStyle:'short'}).format(parsed) : 'Нет наблюдения'; },
      chooseAction(value) { this.action = value; this.confirmed = false; },
      async fetchJson(url, options = {}) {
        const signal = options.signal;
        const response = await fetch(url, {credentials:'same-origin', cache:'no-store', headers:{Accept:'application/json', ...(options.body ? {'Content-Type':'application/json','X-CSRFToken':document.querySelector('meta[name="csrf-token"]')?.content || ''} : {})}, ...options, signal});
        if (response.redirected || response.status === 401) {
          this.sessionEnded = true;
          throw Error('Сессия завершилась. Войдите снова; введённый текст пока остаётся на этой странице.');
        }
        if (response.status === 403) throw Error('Доступ к этой операции закрыт. Проверьте аккаунт продавца.');
        let body;
        try { body = await response.json(); } catch (_) { throw Error('Сервер не вернул состояние решения. Проверьте его чтением.'); }
        if (!response.ok || body?.success !== true) {
          const err = Error(body?.error || 'Не удалось сохранить решение. Проверьте текущее состояние.');
          err.status = response.status;
          throw err;
        }
        return body;
      },
      adopt(row) {
        this.review = validReview({success:true, review:row});
        this.events = row.events;
        this.nextBeforeId = row.next_before_id;
        this.needsCheck = false;
        this.pendingReview = null;
        if (!row.can_release && this.action === 'released') this.action = 'note_added';
      },
      useCurrent() {
        if (!this.pendingReview || this.loading || this.saving || this.sessionEnded) return;
        this.adopt(this.pendingReview);
        this.confirmed = false;
        this.error = '';
        this.$nextTick(() => this.$refs.decisionHeading?.focus());
      },
      async readCurrent() {
        if (this.loading || this.moreLoading || this.saving || this.sessionEnded) return;
        const explicit = !!this.review || this.needsCheck;
        controller?.abort(); controller = new AbortController();
        const local = controller; const timeout = setTimeout(() => local.abort(), 10000);
        this.loading = true; this.error = '';
        try {
          const body = await this.fetchJson(config.api, {signal:local.signal});
          const row = validReview(body);
          if (this.needsCheck) {
            this.pendingReview = row;
            this.$nextTick(() => this.$refs.readback?.focus());
          }
          else {
            this.adopt(row);
            if (explicit) this.$nextTick(() => this.$refs.decisionHeading?.focus());
          }
        } catch (err) {
          this.error = err.name === 'AbortError' ? 'Чтение заняло слишком много времени. Повторите проверку.' : err instanceof TypeError ? 'Нет соединения с сервером. Введённый текст сохранён на этой странице.' : err.message;
          if (explicit) this.$nextTick(() => this.$refs.alert?.focus());
        } finally { clearTimeout(timeout); this.loading = false; }
      },
      async post(url, data) {
        if (this.saving || this.loading || this.moreLoading || this.sessionEnded || this.needsCheck || this.pendingReview) return;
        this.saving = true; this.error = '';
        const local = new AbortController(); const timeout = setTimeout(() => local.abort(), 12000);
        try {
          const body = await this.fetchJson(url, {method:'POST', body:JSON.stringify(data), signal:local.signal});
          this.adopt(validReview(body));
          this.reason = ''; this.confirmed = false; this.action = 'note_added';
          this.$nextTick(() => this.$refs.decisionHeading?.focus());
        } catch (err) {
          // A lost response cannot prove that the POST was rejected. Never repeat it.
          this.needsCheck = true;
          this.pendingReview = null;
          this.error = err.name === 'AbortError' ? 'Ответ не пришёл вовремя. Проверьте текущее состояние решения.' : err instanceof TypeError ? 'Соединение прервалось. Проверьте текущее состояние решения.' : err.message;
          this.$nextTick(() => this.$refs.alert?.focus());
        } finally { clearTimeout(timeout); this.saving = false; }
      },
      submitPlace() {
        if (!this.review?.can_place || this.loading || this.moreLoading || this.saving || this.sessionEnded || this.needsCheck || this.pendingReview || !this.confirmed || this.reason.trim().length < 10) return;
        this.post(config.place, {expected_version:this.review.operation_version, scope_token:this.review.scope_token,
          reason:this.reason, confirm_scope:true});
      },
      submitDecision() {
        const row = this.review;
        if (!row?.hold || this.loading || this.moreLoading || this.saving || this.sessionEnded || this.needsCheck || this.pendingReview || this.reason.trim().length < 10 || (this.action === 'released' && (!row.can_release || !this.confirmed))) return;
        this.post(config.decision, {expected_version:row.hold.version, expected_operation_version:row.operation_version,
          action:this.action, reason:this.reason, confirm_release:this.action === 'released'});
      },
      async loadMore() {
        if (!this.nextBeforeId || this.loading || this.moreLoading || this.saving || this.sessionEnded || this.needsCheck || this.pendingReview) return;
        this.moreLoading = true; this.error = '';
        const local = new AbortController(); const timeout = setTimeout(() => local.abort(), 10000);
        try {
          const row = validReview(await this.fetchJson(config.api + '?before_id=' + encodeURIComponent(this.nextBeforeId), {signal:local.signal}));
          if (row.hold?.id !== this.review?.hold?.id || row.hold?.version !== this.review?.hold?.version || row.operation_version !== this.review.operation_version) {
            this.needsCheck = true; this.pendingReview = null;
            throw Error('История изменилась. Проверьте текущее состояние перед новым решением.');
          }
          const existing = new Set(this.events.map(item => item.id));
          this.events.push(...row.events.filter(item => !existing.has(item.id)));
          this.nextBeforeId = row.next_before_id;
        } catch (err) { this.error = err.name === 'AbortError' ? 'История загружается слишком долго. Повторите чтение.' : err.message; }
        finally { clearTimeout(timeout); this.moreLoading = false; }
      }
    },
    mounted() {
      document.getElementById('oq-fallback')?.remove();
      beforeUnloadHandler = event => { if (this.reason.trim()) { event.preventDefault(); event.returnValue = ''; } };
      global.addEventListener('beforeunload', beforeUnloadHandler);
      this.readCurrent();
    },
    beforeUnmount() { controller?.abort(); global.removeEventListener('beforeunload', beforeUnloadHandler); }
  });
  app.mount('#oq-app');
})(window);
