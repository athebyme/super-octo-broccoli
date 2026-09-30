/* Local reply workspace. Marketplace publication is deliberately a separate contract. */
(function (global) {
  'use strict';
  const kinds = ['review', 'question'], statuses = ['', 'NEW', 'VIEWED', 'PROCESSED'];
  const positive = v => /^[1-9]\d{0,8}$/.test(String(v || '')) ? Number(v) : null;
  function instant(value) {
    const d = value ? new Date(/[Zz]|[+-]\d\d:\d\d$/.test(value) ? value : value + 'Z') : null;
    return d && Number.isFinite(d.getTime()) ? d.toLocaleString('ru-RU', {day:'numeric', month:'short', year:'numeric', hour:'2-digit', minute:'2-digit'}) : 'Дата не указана';
  }
  function imageURL(value) {
    try { const u = new URL(value); return ['https:', 'http:'].includes(u.protocol) && !u.username && !u.password ? u.href : ''; } catch (_) { return ''; }
  }
  function createOptions(config) {
    let alive = true, revision = 0, detailRevision = 0, controller, detailController, postController, pop, unload, returnFocus;
    const pendingKey = 'ozon-inbox-pending:' + config.accountId;
    function storedPending() {
      try {
        const value = JSON.parse(global.sessionStorage.getItem(pendingKey) || '{}');
        return Object.fromEntries(Object.entries(value).slice(0, 20).filter(([id, row]) => positive(id) && row && ['ai','template','save'].includes(row.mode) && (row.previous === null || positive(row.previous))));
      } catch (_) { return {}; }
    }
    function fromURL() {
      const q = new URLSearchParams(location.search);
      return {kind:kinds.includes(q.get('source_kind')) ? q.get('source_kind') : 'review', status:statuses.includes(q.get('status')) ? q.get('status') : '',
        search:(q.get('search') || '').slice(0, 200), page:Math.min(100000, positive(q.get('page')) || 1), listingId:positive(q.get('listing_id')), itemId:positive(q.get('item'))};
    }
    let refreshDomain = fromURL().kind === 'review' ? 'reviews' : 'questions';
    const refresh = global.ozonReadRefresh({accountId:config.accountId, domain:() => refreshDomain, csrfToken:config.csrfToken});
    const refreshData = {}, refreshMethods = {};
    for (const [key, value] of Object.entries(refresh)) (typeof value === 'function' ? refreshMethods : refreshData)[key] = value;
    async function request(url, current, payload, timeout = 10000) {
      const post = payload !== undefined;
      let expired = false;
      const timer = setTimeout(() => { expired = true; current.abort(); }, timeout);
      try {
        const response = await fetch(url, {method:post ? 'POST' : 'GET', signal:current.signal, credentials:'same-origin', cache:'no-store',
          headers:{Accept:'application/json', ...(post ? {'Content-Type':'application/json','X-CSRFToken':config.csrfToken} : {})}, ...(post ? {body:JSON.stringify(payload)} : {})});
        if (response.redirected || response.status === 401) throw Object.assign(Error('Сессия завершилась. Войдите снова, чтобы продолжить.'), {sessionEnded:true, uncertain:post});
        if (!(response.headers.get('content-type') || '').includes('application/json')) {
          // Flask-WTF rejects an expired/missing CSRF token before the handler.
          // A known 400 must not leave an unrecoverable pending-generation marker.
          if (post && response.status === 400) throw Error('Не удалось проверить форму. Скопируйте правки из поля ответа и обновите страницу, затем сохраните снова.');
          throw Object.assign(Error('Не удалось прочитать ответ сервера.'), {uncertain:post});
        }
        const body = await response.json();
        if (!response.ok || body.success === false) {
          const providerDenied = body.code === 'ozon_inbox_access_denied';
          throw Object.assign(Error(body.error || 'Не удалось выполнить действие.'), {sessionEnded:response.status === 403 && !providerDenied, uncertain:post && response.status >= 500, providerDenied});
        }
        return body.data;
      } catch (e) {
        if (expired) throw Object.assign(Error('Сервер не ответил вовремя.'), {uncertain:post});
        if (e instanceof TypeError || e instanceof SyntaxError) throw Object.assign(Error('Соединение прервалось.'), {uncertain:post});
        throw e;
      } finally { clearTimeout(timer); }
    }
    return {
      directives:{'image-deadline':global.mcatShared.imageDeadline},
      data() { const filters = fromURL(); return {...refreshData, period:'90d', config, filters, searchDraft:filters.search, items:[], pagination:{page:1,pages:0,total:0}, stats:null, capability:null, sync:null,
        loading:false, loaded:false, error:'', sessionEnded:false, observed:null, failedImages:{}, selected:null, detailLoading:false, detailError:'', editor:'', savedText:'',
        saving:false, actionError:'', notice:'', uncertainDrafts:storedPending(), dialogOpen:false}; },
      computed:{
        uncertainDraft() { return this.uncertainDrafts[this.filters.itemId] || null; },
        dirty() { return this.editor !== this.savedText; },
        staleScope() { return this.observed && ['kind','status','search','page','listingId'].some(k => this.observed[k] !== this.filters[k]); },
        canSync() { return this.loaded && !this.loading && !this.staleScope && !this.sessionEnded && !this.saving && !this.refreshing && !this.refreshUncertain && !this.refreshSessionEnded && this.capability?.available && this.capability?.account_ready; },
        canDraft() { return this.selected?.reply_eligible && !this.saving && !this.detailLoading && !this.detailError && !this.uncertainDraft && !this.sessionEnded; },
        loginUrl() { return '/login?next=' + encodeURIComponent(location.pathname + location.search); },
        syncLabel() {
          if (!this.sync) return 'Ещё не загружено из Ozon';
          if (this.sync.status === 'running') return 'Загрузка ленты не завершена. Сохранено страниц: ' + this.sync.page_count;
          if (this.sync.status !== 'completed') return 'Последнее обновление не завершилось';
          return 'Обновлено ' + instant(this.sync.completed_at);
        },
        emptyTitle() { return this.filters.search || this.filters.status || this.filters.listingId ? 'Ничего не нашлось' : this.capability?.live_access_denied || !this.capability?.available ? 'Доступ к разделу не подтверждён' : !this.sync ? 'Начните с загрузки обращений' : 'Сохранённых обращений нет'; },
      },
      methods:{
        ...refreshMethods,
        instant, imageURL,
        rememberPending(id, value) {
          if (value) this.uncertainDrafts[id] = value; else delete this.uncertainDrafts[id];
          try { global.sessionStorage.setItem(pendingKey, JSON.stringify(this.uncertainDrafts)); } catch (_) { /* Storage is optional; no customer text is persisted. */ }
        },
        statusLabel(value) { return {NEW:'Новый', VIEWED:'Просмотрен', PROCESSED:'Обработан'}[value] || value; },
        title(item) { return item.listing?.title || item.listing?.offer_id || 'Товар с SKU ' + item.external_sku; },
        matchLabel(item) { return {ambiguous:'Несколько карточек с этим SKU', unmatched:'Карточка пока не найдена', unavailable:'Связь с карточкой недоступна'}[item.match_status] || ''; },
        url(filters = this.filters) {
          const q = new URLSearchParams({account_id:config.accountId, source_kind:filters.kind});
          if (filters.status) q.set('status', filters.status); if (filters.search) q.set('search', filters.search);
          if (filters.page > 1) q.set('page', filters.page); if (filters.listingId) q.set('listing_id', filters.listingId); if (filters.itemId) q.set('item', filters.itemId);
          return config.page + '?' + q;
        },
        navigate(patch) {
          const oldKind = this.filters.kind;
          this.filters = {...this.filters, ...patch, itemId:null}; this.searchDraft = this.filters.search;
          history.pushState({}, '', this.url()); this.load();
          if (oldKind !== this.filters.kind) this.changeRefreshKind();
        },
        changeKind(kind) { this.navigate({kind, status:'', search:'', page:1, listingId:null}); },
        applySearch() { this.navigate({search:this.searchDraft.trim(), page:1}); },
        resetFilters() { this.navigate({search:'', status:'', listingId:null, page:1}); },
        pageLink(event, page) { if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return; event.preventDefault(); this.navigate({page}); },
        async load() {
          if (!alive || this.sessionEnded) return;
          controller?.abort(); const current = controller = new AbortController(), version = ++revision, scope = {...this.filters};
          this.loading = true; this.error = '';
          const q = new URLSearchParams({account_id:config.accountId, source_kind:scope.kind, page:scope.page, per_page:20});
          if (scope.search) q.set('search', scope.search); if (scope.status) q.set('status', scope.status); if (scope.listingId) q.set('listing_id', scope.listingId);
          try {
            const data = await request(config.api + '?' + q, current);
            if (!alive || version !== revision) return;
            if (data?.scope?.account_id !== config.accountId || data.scope.marketplace !== 'ozon' || data.filters?.source_kind !== scope.kind || data.filters.search !== scope.search || data.filters.status !== scope.status || data.filters.listing_id !== scope.listingId || !Array.isArray(data.items) || data.items.length > 20 || data.items.some(i => i.account_id !== config.accountId || i.source_kind !== scope.kind) || data.pagination?.page !== scope.page || !data.capability) throw Error('Получена другая выборка. Повторите загрузку.');
            this.items = data.items; this.pagination = data.pagination; this.stats = data.stats; this.capability = data.capability; this.sync = data.sync; this.observed = scope; this.loaded = true;
          } catch (e) { if (alive && version === revision && e.name !== 'AbortError') { this.error = e.message; if (e.sessionEnded) this.sessionEnded = true; } }
          finally { if (alive && version === revision) this.loading = false; }
        },
        async openItem(item, event) {
          if (event && (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey)) return;
          event?.preventDefault(); this.filters.itemId = item.id; history.pushState({}, '', this.url()); returnFocus = event?.currentTarget || document.activeElement;
          this.selected = null; this.editor = ''; this.savedText = ''; this.notice = ''; this.actionError = ''; this.dialogOpen = true;
          this.$refs.detail.showModal(); await this.readItem();
        },
        async readItem() {
          if (!alive || this.sessionEnded || !this.filters.itemId) return;
          detailController?.abort(); const current = detailController = new AbortController(), version = ++detailRevision, id = this.filters.itemId;
          const preserve = this.dirty || !!this.uncertainDraft;
          this.detailLoading = true; this.detailError = '';
          try {
            const data = await request(config.api + '/' + id + '?account_id=' + config.accountId, current);
            if (!alive || version !== detailRevision || this.filters.itemId !== id) return;
            if (data?.account_id !== config.accountId || data.id !== id || data.draft && (data.draft.account_id !== config.accountId || data.draft.inbox_item_id !== id)) throw Error('Не удалось подтвердить входящее выбранного магазина.');
            this.selected = data; this.savedText = data.draft?.text || '';
            if (!preserve) this.editor = this.savedText;
            if (this.uncertainDraft && data.draft?.id !== this.uncertainDraft.previous && data.draft) {
              const generated = this.uncertainDraft.mode !== 'save';
              this.rememberPending(id, null); this.notice = 'Сохранённая версия загружена. Проверьте её перед дальнейшими действиями.';
              // A recovered generation has no human input to preserve.
              if (generated || !this.editor) this.editor = this.savedText;
            }
            const index = this.items.findIndex(i => i.id === id); if (index !== -1) this.items[index] = data;
          } catch (e) { if (alive && version === detailRevision && e.name !== 'AbortError') { this.detailError = e.message; if (e.sessionEnded) this.sessionEnded = true; } }
          finally { if (alive && version === detailRevision) this.detailLoading = false; }
        },
        closeDialog() {
          if (this.saving) { this.actionError = 'Дождитесь результата подготовки или сохранения.'; return; }
          if ((this.dirty || this.uncertainDraft) && !global.confirm('Закрыть редактор? Несохранённый текст будет потерян.')) return;
          detailRevision++; detailController?.abort(); this.filters.itemId = null; this.dialogOpen = false; this.$refs.detail.close();
          this.selected = null; this.editor = this.savedText = ''; history.replaceState(history.state, '', this.url()); returnFocus?.focus();
        },
        useSaved() { if (this.dirty && !global.confirm('Заменить введённый текст сохранённой версией?')) return; this.editor = this.savedText; this.notice = ''; },
        async writeDraft(mode) {
          if (!this.canDraft || mode === 'save' && !this.selected.draft) return;
          if (mode !== 'save' && (this.selected.draft || this.dirty) && !global.confirm('Подготовить новый вариант ответа? Текущий сохранённый черновик останется в истории.')) return;
          const id = this.selected.id, previous = this.selected.draft?.id || null;
          const payload = mode === 'save' ? {draft_id:previous, expected_content_hash:this.selected.draft.content_hash, text:this.editor} : {generation_mode:mode, expected_draft_id:previous};
          postController = new AbortController(); this.saving = true; this.actionError = ''; this.notice = '';
          this.rememberPending(id, {previous, mode});
          try {
            const data = await request(config.api + '/' + id + '/draft' + (mode === 'save' ? '/save' : '') + '?account_id=' + config.accountId, postController, payload, mode === 'ai' ? 45000 : 15000);
            if (!alive) return;
            if (data?.account_id !== config.accountId || data.inbox_item_id !== id || data.status !== 'draft' || !positive(data.id) || typeof data.text !== 'string' || !/^[a-f0-9]{64}$/.test(data.content_hash || '')) throw Object.assign(Error('Не удалось подтвердить сохранение.'), {uncertain:true});
            this.selected.draft = data; this.savedText = this.editor = data.text; this.notice = 'Черновик сохранён в Seller Hub. В Ozon ничего не отправлено.';
            this.rememberPending(id, null);
            const item = this.items.find(i => i.id === id); if (item) item.draft = data;
          } catch (e) {
            if (alive) { if (!e.uncertain) this.rememberPending(id, null); this.actionError = e.message + (e.uncertain ? ' Не повторяйте действие: сначала проверьте сохранённый ответ.' : ''); if (e.sessionEnded) this.sessionEnded = true; }
          } finally { if (alive) this.saving = false; }
        },
        async copyDraft() {
          try { await navigator.clipboard.writeText(this.editor); this.notice = 'Текст скопирован. Проверьте и отправьте его в Ozon Seller.'; }
          catch (_) { this.actionError = 'Браузер не разрешил копирование. Выделите текст в поле ответа и скопируйте вручную.'; this.$refs.editor?.focus(); this.$refs.editor?.select(); }
        },
        async syncNow() { if (this.canSync) await this.requestRefresh(); },
        changeRefreshKind() {
          refreshDomain = this.filters.kind === 'review' ? 'reviews' : 'questions';
          this.changeRefreshPeriod();
        },
        async onRefreshCompleted() { await this.load(); },
        async onRefreshSettled() { await this.load(); },

      },
      mounted() {
        document.getElementById('oin-fallback')?.remove(); this.load(); this.initRefresh();
        if (this.filters.itemId) { this.dialogOpen = true; this.$refs.detail.showModal(); this.readItem(); }
        unload = e => { if (this.dirty || this.saving || this.uncertainDraft) { e.preventDefault(); e.returnValue = ''; } };
        pop = () => {
          if (this.saving || (this.dirty || this.uncertainDraft) && !global.confirm('Уйти из редактора и потерять несохранённый текст?')) { history.pushState({}, '', this.url()); return; }
          const oldKind = this.filters.kind;
          this.filters = fromURL(); this.searchDraft = this.filters.search; this.editor = this.savedText = ''; this.selected = null;
          if (this.filters.itemId) { this.dialogOpen = true; if (!this.$refs.detail.open) this.$refs.detail.showModal(); this.readItem(); }
          else { detailRevision++; detailController?.abort(); this.dialogOpen = false; this.$refs.detail.close(); }
          this.load();
          if (oldKind !== this.filters.kind) this.changeRefreshKind();
        };
        global.addEventListener('beforeunload', unload); global.addEventListener('popstate', pop);
      },
      beforeUnmount() { this.destroyRefresh(); alive = false; revision++; detailRevision++; controller?.abort(); detailController?.abort(); postController?.abort(); global.removeEventListener('beforeunload', unload); global.removeEventListener('popstate', pop); },
    };
  }
  global.ozonInbox = {createOptions, instant, imageURL};
  const bootstrap = document.getElementById('oin-bootstrap');
  if (bootstrap && global.Vue) global.Vue.createApp(createOptions(JSON.parse(bootstrap.textContent))).mount('#oin-app');
})(window);
