/* Exact-period refresh status. Enqueue once; all subsequent requests are reads. */
(function (global) {
  'use strict';
  global.ozonReadRefresh = function ({accountId, domain, csrfToken, notify}) {
    let timer = null;
    let generation = 0;
    let disposed = false;
    let controller = null;
    let visibility = null;
    let lastLoadedId = null;
    let beforeSubmitId = null;
    const currentDomain = () => typeof domain === 'function' ? domain() : domain;
    const endpoint = () => '/marketplaces/api/' + currentDomain() + '/sync?account_id=' + accountId;
    const titles = {pending: 'Обновление в очереди', running: 'Обновляем данные',
      waiting: 'Ожидаем Ozon', completed: 'Данные обновлены', failed: 'Обновление остановлено',
      paused: 'Обновление приостановлено'};
    async function readResponse(response) {
      if (response.redirected || [401,403].includes(response.status)) {
        const error = new Error('Нет доступа к обновлению. Обновите страницу и войдите в Seller Hub снова.');
        error.accessEnded = true;
        throw error;
      }
      let body;
      try { body = await response.json(); }
      catch (_) { throw Object.assign(new Error('Не удалось получить ответ. Обновите страницу или проверьте состояние заявки.'), {rejected:response.status === 400}); }
      if (!response.ok) throw Object.assign(new Error(body.error || 'Не удалось выполнить обновление'), {rejected:[400,404,409,422].includes(response.status)});
      if (!body.data || typeof body.data.active !== 'boolean'
          || !['idle', 'pending', 'running', 'waiting', 'completed', 'failed', 'paused'].includes(body.data.status)) {
        throw new Error('Сервер не вернул состояние обновления. Проверьте его ещё раз.');
      }
      return body.data;
    }
    async function read(url, options = {}) {
      const activeController = controller;
      let expired = false;
      const timeout = setTimeout(() => { expired = true; activeController.abort(); }, 10000);
      try {
        return await readResponse(await fetch(url, {...options, signal:activeController.signal, cache:'no-store'}));
      } catch (error) {
        if (expired) throw new Error('Не дождались ответа. Проверьте состояние заявки: загрузка могла начаться.');
        throw error;
      } finally { clearTimeout(timeout); }
    }
    return {
      refreshState: null,
      refreshing: false,
      refreshStatusError: '',
      refreshUncertain: false,
      refreshSessionEnded: false,
      withRefreshPeriod(url) {
        const target = new URL(url, global.location.origin);
        target.searchParams.set('period', this.period);
        return target.pathname + target.search;
      },
      refreshTitle() { return titles[this.refreshState?.status] || 'Обновление данных'; },
      refreshPeriodLabel() {
        const state = this.refreshState;
        if (!state?.period_start) return '';
        const format = value => new Date(value + 'T00:00:00').toLocaleDateString('ru-RU');
        return format(state.period_start) + ' — ' + format(state.period_end);
      },
      refreshDueLabel() {
        const due = this.refreshState?.next_attempt_at;
        if (!due || this.refreshState?.status !== 'waiting') return '';
        return 'Следующая попытка не раньше ' + new Date(/[Zz]|[+-]\d\d:\d\d$/.test(due) ? due : due+'Z').toLocaleString('ru-RU');
      },
      async initRefresh() {
        disposed = false;
        visibility = () => {
          clearTimeout(timer);
          if (document.hidden) { generation++; controller?.abort(); }
          else this.loadRefreshStatus();
        };
        document.addEventListener('visibilitychange', visibility);
        await this.loadRefreshStatus();
      },
      changeRefreshPeriod() {
        generation++;
        clearTimeout(timer);
        controller?.abort();
        this.refreshState = null;
        this.refreshing = false;
        this.refreshStatusError = '';
        this.refreshUncertain = false;
        lastLoadedId = null;
        return this.loadRefreshStatus();
      },
      scheduleRefreshPoll() {
        clearTimeout(timer);
        if (disposed || document.hidden || this.refreshSessionEnded || (!this.refreshState?.active && !this.refreshUncertain)) return;
        const rawDue = this.refreshState?.next_attempt_at;
        const due = Date.parse(rawDue ? (/[Zz]|[+-]\d\d:\d\d$/.test(rawDue) ? rawDue : rawDue+'Z') : '');
        const delay = this.refreshState?.status === 'waiting' && Number.isFinite(due)
          ? Math.min(60000, Math.max(15000, due - Date.now())) : 15000;
        timer = setTimeout(() => this.loadRefreshStatus(), delay);
      },
      async acceptRefresh(data, revision, period, submitted) {
        if (disposed || revision !== generation || period !== this.period) return;
        if (data.account_id !== accountId || data.domain !== currentDomain() || data.period !== period) {
          throw new Error('Не удалось подтвердить магазин и раздел обновления. Проверьте состояние ещё раз.');
        }
        const wasActive = this.refreshState?.active && this.refreshState.id === data.id;
        const afterUncertain = this.refreshUncertain;
        if (afterUncertain && !submitted && (!data.id || (data.id === beforeSubmitId && !data.active))) {
          this.refreshStatusError = 'Подтверждения новой заявки пока нет. Повторный запрос остановлен; проверим состояние автоматически.';
          return;
        }
        if (JSON.stringify(this.refreshState) !== JSON.stringify(data)) this.refreshState = data;
        this.refreshing = Boolean(data.active);
        this.refreshUncertain = false;
        this.refreshStatusError = '';
        if (['failed','paused'].includes(data.status) && (wasActive || submitted || afterUncertain)) {
          await this.onRefreshSettled?.();
        }
        if (data.status === 'completed' && data.id !== lastLoadedId) {
          lastLoadedId = data.id;
          await this.onRefreshCompleted({fromActive:wasActive || submitted, afterUncertain});
          if (!disposed && revision === generation && period === this.period && (wasActive || submitted)) {
            if (notify) notify('Данные Ozon за выбранные даты обновлены');
            else this.$store?.toasts?.success('Данные Ozon за выбранные даты обновлены');
          }
        }
      },
      async loadRefreshStatus() {
        if (disposed || document.hidden || this.refreshSessionEnded) return;
        clearTimeout(timer);
        controller?.abort();
        controller = new AbortController();
        const revision = ++generation;
        const period = this.period;
        try {
          await this.acceptRefresh(await read(endpoint() + '&period=' + encodeURIComponent(period)), revision, period, false);
        } catch (error) {
          if (!disposed && revision === generation && error.name !== 'AbortError') {
            this.refreshSessionEnded = Boolean(error.accessEnded);
            this.refreshStatusError = error instanceof TypeError
              ? 'Не удалось проверить состояние обновления. Сохранённые данные доступны.' : error.message;
          }
        } finally {
          if (revision === generation) this.scheduleRefreshPoll();
        }
      },
      async requestRefresh() {
        if (disposed || this.refreshing || this.refreshUncertain || this.refreshSessionEnded) return;
        clearTimeout(timer);
        controller?.abort();
        controller = new AbortController();
        const revision = ++generation;
        const period = this.period;
        beforeSubmitId = this.refreshState?.id || null;
        this.refreshing = true;
        this.refreshUncertain = true;
        this.refreshStatusError = '';
        try {
          const data = await read(endpoint(), {method: 'POST',
            headers: {'Content-Type': 'application/json', 'X-CSRFToken': csrfToken},
            body: JSON.stringify({period, force: true})});
          await this.acceptRefresh(data, revision, period, true);
        } catch (error) {
          if (!disposed && revision === generation && error.name !== 'AbortError') {
            this.refreshSessionEnded = Boolean(error.accessEnded);
            this.refreshUncertain = !this.refreshSessionEnded && !error.rejected;
            this.refreshing = this.refreshUncertain;
            this.refreshStatusError = error instanceof TypeError
              ? 'Не удалось получить подтверждение. Проверьте состояние заявки.' : error.message;
          }
        } finally {
          if (revision === generation) this.scheduleRefreshPoll();
        }
      },
      destroyRefresh() {
        disposed = true;
        generation++;
        clearTimeout(timer);
        controller?.abort();
        if (visibility) document.removeEventListener('visibilitychange', visibility);
      },
    };
  };
})(window);
